/* netmon frontend — dashboard, network detail, comparison.
   Look and thresholds carried over from the original report-html.sh. */

const TARGET_COLORS = {gateway:'#38bdf8', quad9:'#a78bfa', google:'#f472b6',
                       _0:'#34d399', _1:'#fbbf24', _2:'#fb7185'};
const NET_COLORS = ['#38bdf8', '#f472b6', '#34d399', '#fbbf24', '#a78bfa', '#fb7185'];
const PUBLIC_TARGETS = ['quad9', 'google'];

const colorForTarget = (name, i) => TARGET_COLORS[name] || TARGET_COLORS['_' + (i % 3)];
const colorForNet = i => NET_COLORS[i % NET_COLORS.length];

const fmtDur = s => s >= 3600 ? (s / 3600).toFixed(1) + ' h'
                  : s >= 60 ? (s / 60).toFixed(1) + ' min' : s + ' s';
const lossCls = l => l > 1 ? 'bad' : l > 0.1 ? 'warn' : 'ok';
const lossLbl = l => l > 1 ? 'problem' : l > 0.1 ? 'minor loss' : 'OK';

function fmtTs(epoch, longRange) {
  const d = new Date(epoch * 1000);
  const hm = d.toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
  if (!longRange) return hm;
  return d.toLocaleDateString('en-GB', {day: 'numeric', month: 'short'}) + ' ' + hm;
}
const fmtIso = iso => (iso || '').replace('T', ' ').slice(0, 19);

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + ' → HTTP ' + r.status);
  return r.json();
}

const baseOpts = yLabel => ({
  responsive: true, interaction: {mode: 'index', intersect: false},
  scales: {
    x: {ticks: {color: '#94a3b8', maxTicksLimit: 12, maxRotation: 0}, grid: {color: '#1e293b'}},
    y: {title: {display: true, text: yLabel, color: '#94a3b8'},
        ticks: {color: '#94a3b8'}, grid: {color: '#1e293b'}, beginAtZero: true}
  },
  plugins: {legend: {labels: {color: '#e2e8f0'}}},
  elements: {point: {radius: 0}}, spanGaps: true, animation: false
});

/* ---------- chart overlays: note markers + outage bands ---------- */

const NOTE_COLOR = '#eab308';
const BAND_COLORS = {local: 'rgba(239,68,68,.18)', internet: 'rgba(245,158,11,.18)'};

/* Fractional position of epoch t on a category axis whose ticks sit at `epochs`.
   Returns null when t falls outside the axis span (plus one median step of slack);
   with clamp=true out-of-range values snap to the axis edges instead. */
function noteAxisPos(epochs, t, clamp) {
  const n = epochs.length;
  if (!n) return null;
  if (n === 1) return (clamp || Math.abs(t - epochs[0]) < 1) ? 0 : null;
  if (!clamp) {
    const step = (epochs[n - 1] - epochs[0]) / (n - 1);
    if (t < epochs[0] - step || t > epochs[n - 1] + step) return null;
  }
  if (t <= epochs[0]) return 0;
  if (t >= epochs[n - 1]) return n - 1;
  let i = 0;
  while (i < n - 2 && epochs[i + 1] < t) i++;
  return i + (t - epochs[i]) / (epochs[i + 1] - epochs[i]);
}

function epochToPx(xs, epochs, t, clamp) {
  const pos = noteAxisPos(epochs, t, clamp);
  if (pos == null) return null;
  const i0 = Math.floor(pos), i1 = Math.min(i0 + 1, epochs.length - 1);
  const p0 = xs.getPixelForValue(i0);
  return p0 + (pos - i0) * (xs.getPixelForValue(i1) - p0);
}

function wrapText(ctx, text, maxW) {
  const lines = [];
  for (const para of text.split('\n')) {
    let line = '';
    for (const word of para.split(' ')) {
      const cand = line ? line + ' ' + word : word;
      if (ctx.measureText(cand).width > maxW && line) { lines.push(line); line = word; }
      else line = cand;
    }
    lines.push(line);
  }
  return lines;
}

const overlaysPlugin = {
  id: 'overlays',
  beforeDatasetsDraw(chart) {
    const o = chart.options.plugins.overlays;
    if (!o || !o.bands || !o.bands.length || o.epochs.length < 2) return;
    const xs = chart.scales.x, area = chart.chartArea, ctx = chart.ctx;
    const first = o.epochs[0], last = o.epochs[o.epochs.length - 1];
    ctx.save();
    for (const b of o.bands) {
      if (b.t1 < first || b.t0 > last) continue;
      let x0 = epochToPx(xs, o.epochs, b.t0, true);
      let x1 = epochToPx(xs, o.epochs, b.t1, true);
      if (x1 - x0 < 2) { const c = (x0 + x1) / 2; x0 = c - 1; x1 = c + 1; }
      ctx.fillStyle = BAND_COLORS[b.scope] || BAND_COLORS.internet;
      ctx.fillRect(x0, area.top, x1 - x0, area.bottom - area.top);
    }
    ctx.restore();
  },
  afterEvent(chart, args) {
    const marks = chart.$noteXs;
    if (!marks || !marks.length) return;
    let hover = null;
    if (args.event.type === 'mousemove' && args.inChartArea) {
      let best = 9;
      for (const m of marks) {
        const d = Math.abs(args.event.x - m.px);
        if (d < best) { best = d; hover = m; }
      }
    }
    if (hover !== chart.$noteHover) { chart.$noteHover = hover; args.changed = true; }
  },
  afterDatasetsDraw(chart) {
    const o = chart.options.plugins.overlays;
    if (!o || !o.marks || !o.marks.length) return;
    const xs = chart.scales.x, area = chart.chartArea, ctx = chart.ctx;
    chart.$noteXs = [];
    ctx.save();
    for (const m of o.marks) {
      const px = epochToPx(xs, o.epochs, m.t);
      if (px == null || px < area.left - 1 || px > area.right + 1) continue;
      ctx.strokeStyle = NOTE_COLOR;
      ctx.lineWidth = 1.2;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(px, area.top);
      ctx.lineTo(px, area.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = '11px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('📝', px, area.top - 3);
      chart.$noteXs.push({px, note: m});
    }
    const h = chart.$noteHover;
    if (h && chart.$noteXs.includes(h)) {
      ctx.font = '12px system-ui, sans-serif';
      const lines = [h.note.when + ' · ' + h.note.who, ...wrapText(ctx, h.note.text, 260)];
      const w = Math.min(280, Math.max(...lines.map(l => ctx.measureText(l).width)) + 20);
      const lh = 17, boxH = lines.length * lh + 12;
      const x = Math.max(area.left, Math.min(h.px + 8, area.right - w));
      const y = area.top + 8;
      ctx.fillStyle = 'rgba(15,23,42,.95)';
      ctx.strokeStyle = NOTE_COLOR;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(x, y, w, boxH, 6);
      ctx.fill();
      ctx.stroke();
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      lines.forEach((l, i) => {
        ctx.fillStyle = i === 0 ? '#94a3b8' : '#e2e8f0';
        ctx.fillText(l, x + 10, y + 7 + i * lh);
      });
    }
    ctx.restore();
  }
};
Chart.register(overlaysPlugin);

/* Marks for the plugin: which epoch each note sits at plus tooltip strings. */
const noteMarks = notes => notes.map(n => ({
  t: n.ts_epoch,
  text: n.text,
  when: fmtTs(n.ts_epoch, true),
  who: n.networks.length ? n.networks.map(w => w.label).join(', ') : 'all networks',
}));

function epochToLocalInput(t) {
  const d = new Date(t * 1000);
  d.setSeconds(0, 0);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

/* Clicking a chart prefills the note form with the clicked moment. */
function prefillNoteAt(evt, chart, epochs) {
  const xs = chart.scales.x, area = chart.chartArea, n = epochs.length;
  const form = document.getElementById('noteForm');
  if (!form || n < 2) return;
  const x = Math.min(Math.max(evt.x, area.left), area.right);
  const p0 = xs.getPixelForValue(0), p1 = xs.getPixelForValue(n - 1);
  if (p1 <= p0) return;
  const pos = Math.min(Math.max((x - p0) / (p1 - p0) * (n - 1), 0), n - 1);
  const i = Math.min(Math.floor(pos), n - 2);
  const t = epochs[i] + (pos - i) * (epochs[i + 1] - epochs[i]);
  document.getElementById('noteTs').value = epochToLocalInput(t);
  form.scrollIntoView({behavior: 'smooth', block: 'center'});
  document.getElementById('noteText').focus({preventScroll: true});
}

function lineChart(id, labels, datasets, yLabel, overlays) {
  const el = document.getElementById(id);
  if (!el) return;
  const opts = baseOpts(yLabel);
  if (overlays) {
    opts.plugins.overlays = overlays;
    opts.onClick = (evt, els, chart) => prefillNoteAt(evt, chart, overlays.epochs);
  }
  new Chart(el, {type: 'line', data: {labels, datasets}, options: opts});
}

/* ---------- cards and panels of the network detail (per report-html.sh) ---------- */

function renderCards(sum) {
  const el = document.getElementById('cards');
  el.innerHTML = '';
  sum.targets.forEach(s => {
    el.insertAdjacentHTML('beforeend', `
      <div class="card">
        <h3>${s.target}</h3>
        <div class="metric"><span>Packet loss</span><span class="pill ${lossCls(s.loss)}">${s.loss.toFixed(2)}% · ${lossLbl(s.loss)}</span></div>
        <div class="metric"><span>Latency avg</span><span class="v">${s.avg == null ? '—' : s.avg.toFixed(1) + ' ms'}</span></div>
        <div class="metric"><span>Latency min / max</span><span class="v">${s.min == null ? '—' : s.min.toFixed(1) + ' / ' + s.max.toFixed(1) + ' ms'}</span></div>
        <div class="metric"><span>Samples</span><span class="v">${s.samples.toLocaleString('en')}</span></div>
      </div>`);
  });
  if (sum.speed.n) {
    el.insertAdjacentHTML('beforeend', `
      <div class="card">
        <h3>speed ⬇</h3>
        <div class="big">${sum.speed.avg.toFixed(0)} <span style="font-size:14px;color:var(--mut)">Mbit/s avg</span></div>
        <div class="metric"><span>min / max</span><span class="v">${sum.speed.min.toFixed(0)} / ${sum.speed.max.toFixed(0)}</span></div>
        <div class="metric"><span>tests</span><span class="v">${sum.speed.n}</span></div>
      </div>`);
  }
  const u = sum.uptime;
  if (u.coverage != null) {
    const covCls = u.coverage >= 99 ? 'ok' : u.coverage >= 90 ? 'warn' : 'bad';
    el.insertAdjacentHTML('beforeend', `
      <div class="card">
        <h3>measurement uptime</h3>
        <div class="big">${u.coverage.toFixed(1)}<span style="font-size:14px;color:var(--mut)"> % coverage</span></div>
        <div class="metric"><span>Running time</span><span class="v">${fmtDur(u.span_s - u.down_s)}</span></div>
        <div class="metric"><span>Downtime</span><span class="pill ${covCls}">${fmtDur(u.down_s)}</span></div>
        <div class="metric"><span>Interruptions</span><span class="v">${u.gaps.length}×</span></div>
      </div>`);
  }
}

function renderUptime(u) {
  const el = document.getElementById('uptime');
  if (u.coverage == null) {
    el.innerHTML = '<p class="empty" style="margin:0">No measurement uptime records in this period.</p>';
    return;
  }
  const covCls = u.coverage >= 99 ? 'ok' : u.coverage >= 90 ? 'warn' : 'bad';
  const crash = u.gaps.filter(g => g.cause === 'crash').length;
  const stopped = u.gaps.filter(g => g.cause === 'stopped').length;
  let head = '<div style="margin-bottom:10px">';
  head += `<span class="pill ${covCls}">coverage ${u.coverage.toFixed(1)}% · downtime ${fmtDur(u.down_s)}</span> `;
  if (crash) head += `<span class="pill bad">unexpected interruptions: ${crash}×</span> `;
  if (stopped) head += `<span class="pill warn">controlled stops: ${stopped}×</span>`;
  head += '</div>';
  if (!u.gaps.length) {
    el.innerHTML = head + '<p style="color:var(--ok);margin:0">Measuring ran without interruption. 🎉</p>';
    return;
  }
  const rows = u.gaps.slice().sort((a, b) => b.dur - a.dur).map(g => {
    const isCrash = g.cause === 'crash';
    return `<tr><td>${fmtIso(g.from)}</td><td>${fmtIso(g.to)}</td>` +
           `<td style="text-align:right">${fmtDur(g.dur)}</td>` +
           `<td><span class="pill ${isCrash ? 'bad' : 'warn'}">${isCrash ? 'crash / powered-off host' : 'script stopped'}</span></td></tr>`;
  }).join('');
  el.innerHTML = head + `<table class="evt"><thead><tr><th>from</th><th>to (came back up)</th>` +
    `<th style="text-align:right">duration</th><th>cause</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderEvents(events) {
  const el = document.getElementById('events');
  if (!events.length) {
    el.innerHTML = '<p style="color:var(--ok);margin:0">No outages during the measurement. 🎉</p>';
    return;
  }
  const tot = {};
  events.forEach(e => tot[e.scope] = (tot[e.scope] || 0) + e.dur);
  let head = '<div style="margin-bottom:10px">';
  if (tot.local) head += `<span class="pill bad">local: ${events.filter(e => e.scope === 'local').length}× · ${fmtDur(tot.local)}</span> `;
  if (tot.internet) head += `<span class="pill warn">internet: ${events.filter(e => e.scope === 'internet').length}× · ${fmtDur(tot.internet)}</span>`;
  head += '</div>';
  const rows = events.slice().sort((a, b) => b.dur - a.dur).map(e => {
    const local = e.scope === 'local';
    return `<tr><td>${fmtIso(e.start)}</td><td>${fmtIso(e.end).slice(11)}</td>` +
           `<td style="text-align:right">${fmtDur(e.dur)}</td>` +
           `<td><span class="pill ${local ? 'bad' : 'warn'}">${local ? 'local link' : 'internet / ISP'}</span></td></tr>`;
  }).join('');
  el.innerHTML = head + `<table class="evt"><thead><tr><th>start</th><th>end</th>` +
    `<th style="text-align:right">duration</th><th>scope</th></tr></thead><tbody>${rows}</tbody></table>`;
}

/* ---------- notes panel ---------- */

function renderNotes(notes) {
  const el = document.getElementById('noteList');
  if (!el) return;
  if (!notes.length) {
    el.innerHTML = '<p class="empty" style="margin:0 0 10px">No notes in this period.</p>';
    return;
  }
  const rows = notes.slice().sort((a, b) => b.ts_epoch - a.ts_epoch).map(n => {
    const nets = n.networks.length
      ? n.networks.map(w => `<span class="pill mutpill">${w.label}</span>`).join(' ')
      : '<span class="pill ok">general</span>';
    return `<tr><td style="white-space:nowrap">${fmtTs(n.ts_epoch, true)}</td>` +
           `<td style="width:100%">${n.text}</td><td>${nets}</td>` +
           `<td><button class="notedel" data-id="${n.id}" title="Delete note">✕</button></td></tr>`;
  }).join('');
  el.innerHTML = `<table class="evt"><thead><tr><th>when</th><th>note</th>` +
    `<th>networks</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  el.querySelectorAll('.notedel').forEach(btn => btn.addEventListener('click', async () => {
    if (!confirm('Delete this note?')) return;
    const r = await fetch('/api/notes/' + btn.dataset.id, {method: 'DELETE'});
    if (r.ok) location.reload(); else alert('Delete failed: HTTP ' + r.status);
  }));
}

function initNoteForm() {
  const form = document.getElementById('noteForm');
  if (!form) return;
  const ts = document.getElementById('noteTs');
  ts.value = epochToLocalInput(Date.now() / 1000);
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const body = {
      text: document.getElementById('noteText').value,
      ts_epoch: new Date(ts.value).getTime() / 1000,
      networks: [...form.querySelectorAll('.notenets input:checked')].map(i => i.value),
    };
    const r = await fetch('/api/notes', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    if (r.ok) location.reload();
    else alert('Saving the note failed: HTTP ' + r.status + ' ' + (await r.text()));
  });
}

/* ---------- calendar heatmap (GitHub-style, one cell per local day) ---------- */

function renderHeatmap(days) {
  const el = document.getElementById('heatmap');
  if (!el) return;
  if (!days.some(d => d.samples > 0)) {
    el.innerHTML = '<p class="empty" style="margin:0">No data yet.</p>';
    return;
  }
  const cls = d => d.loss == null ? 'hm-none'
             : d.loss > 1 ? 'hm-bad' : d.loss > 0.1 ? 'hm-warn' : 'hm-ok';
  const noon = d => new Date(d.day + 'T12:00:00');
  const offset = (noon(days[0]).getDay() + 6) % 7;   // week columns start on Monday
  const cells = [...Array(offset).fill(null), ...days];
  const weeks = [];
  for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));

  let lastMonth = -1;
  const months = weeks.map(w => {
    const d0 = w.find(Boolean);
    if (!d0) return '';
    const m = noon(d0).getMonth();
    if (m === lastMonth) return '';
    lastMonth = m;
    return noon(d0).toLocaleDateString('en-GB', {month: 'short'});
  });

  const cell = d => {
    if (!d) return '<span class="hm"></span>';
    const tip = d.day + ' · ' + (d.loss == null ? 'no data'
      : `loss ${d.loss.toFixed(2)}% · ${d.samples.toLocaleString('en')} samples`);
    return `<a class="hm ${cls(d)}" href="/net/${window.PAGE.name}?range=day&date=${d.day}" title="${tip}"></a>`;
  };
  el.innerHTML =
    `<div class="hm-months">${months.map(m => `<span>${m}</span>`).join('')}</div>` +
    `<div class="hm-grid">${weeks.map(w => `<div class="hm-week">${w.map(cell).join('')}</div>`).join('')}</div>` +
    `<div class="hm-legend">Internet packet loss per day:
       <span class="hm hm-ok"></span> ≤ 0.1%
       <span class="hm hm-warn"></span> ≤ 1%
       <span class="hm hm-bad"></span> &gt; 1%
       <span class="hm hm-none"></span> no data · click a day to open it</div>`;
}

/* ---------- pages ---------- */

async function pageNetwork() {
  const {name, t0, t1} = window.PAGE;
  const longRange = t1 - t0 > 86400 * 1.5;
  const q = `t0=${t0}&t1=${t1}`;
  const [sum, series, notes] = await Promise.all([
    getJSON(`/api/net/${name}/summary?${q}`),
    getJSON(`/api/net/${name}/series?${q}`),
    getJSON(`/api/notes?${q}&nets=${name}`).catch(() => []),
  ]);
  if (sum.period.first) {
    document.getElementById('period').textContent =
      'Measured: ' + fmtIso(sum.period.first) + '  →  ' + fmtIso(sum.period.last);
  }
  renderCards(sum);
  renderUptime(sum.uptime);
  renderEvents(sum.events);
  renderNotes(notes);
  const marks = noteMarks(notes);
  const bands = sum.events.map(e => ({t0: e.start_epoch, t1: e.end_epoch, scope: e.scope}));

  const lat = series.latency;
  const labels = lat.buckets.map(b => fmtTs(b, longRange));
  const targetNames = Object.keys(lat.targets).sort();
  lineChart('latChart', labels, targetNames.map((t, i) => ({
    label: t, data: lat.targets[t].rtt, borderColor: colorForTarget(t, i),
    backgroundColor: colorForTarget(t, i), borderWidth: 1.6, tension: .25,
  })), 'ms', {epochs: lat.buckets, marks, bands});
  lineChart('lossChart', labels, targetNames.map((t, i) => ({
    label: t, data: lat.targets[t].loss, borderColor: colorForTarget(t, i),
    backgroundColor: colorForTarget(t, i), borderWidth: 1.6, tension: .25, fill: false,
  })), '% loss', {epochs: lat.buckets, marks, bands});

  const rch = series.reach;
  lineChart('rchChart', rch.buckets.map(b => fmtTs(b, longRange)), [
    {label: 'DNS', data: rch.dns, borderColor: '#34d399', backgroundColor: '#34d399', borderWidth: 1.6, tension: .25},
    {label: 'TCP', data: rch.tcp, borderColor: '#fbbf24', backgroundColor: '#fbbf24', borderWidth: 1.6, tension: .25},
    {label: 'TLS', data: rch.tls, borderColor: '#fb7185', backgroundColor: '#fb7185', borderWidth: 1.6, tension: .25},
  ], 'ms', {epochs: rch.buckets, marks, bands});

  const spd = series.speed;
  lineChart('spdChart', spd.ts.map(t => fmtTs(t, longRange)), [{
    label: 'download', data: spd.mbps, borderColor: '#38bdf8',
    backgroundColor: 'rgba(56,189,248,.15)', borderWidth: 2, tension: .3,
    fill: true, pointRadius: 3,
  }], 'Mbit/s', {epochs: spd.ts, marks});

  // the year heatmap aggregates a lot of history — load it after the charts
  getJSON(`/api/net/${name}/heatmap`)
    .then(h => renderHeatmap(h.days))
    .catch(() => renderHeatmap([]));
}

async function pageDashboard() {
  const nets = await getJSON('/api/networks');
  const el = document.getElementById('netcards');
  if (!nets.length) {
    el.innerHTML = '<p class="empty">No networks yet — add monitors to monitors.toml ' +
                   'or import historical data.</p>';
    return;
  }
  el.innerHTML = '';
  nets.forEach(n => {
    const s = n.today;
    const worstLoss = Math.max(0, ...s.targets
      .filter(t => PUBLIC_TARGETS.includes(t.target)).map(t => t.loss));
    const gw = s.targets.find(t => t.target === 'gateway');
    const pub = s.targets.find(t => t.target === 'google') ||
                s.targets.find(t => PUBLIC_TARGETS.includes(t.target));
    let statePill;
    if (!s.targets.length) {
      statePill = '<span class="pill mutpill">no data today</span>';
    } else if (n.sync.configured && !n.sync.online) {
      statePill = '<span class="pill mutpill">monitor unreachable</span>';
    } else if ((gw && gw.loss > 1) || worstLoss > 1 || s.events.length) {
      statePill = '<span class="pill bad">outages</span>';
    } else if (worstLoss > 0.1) {
      statePill = '<span class="pill warn">minor loss</span>';
    } else {
      statePill = '<span class="pill ok">OK</span>';
    }
    el.insertAdjacentHTML('beforeend', `
      <div class="card">
        <h3><a href="/net/${n.name}">${n.label}</a> ${statePill}</h3>
        <div class="metric"><span>Loss (internet)</span><span class="pill ${lossCls(worstLoss)}">${worstLoss.toFixed(2)}%</span></div>
        <div class="metric"><span>Latency avg</span><span class="v">${pub && pub.avg != null ? pub.avg.toFixed(1) + ' ms' : '—'}</span></div>
        <div class="metric"><span>Last speed</span><span class="v">${s.speed.last != null ? s.speed.last.toFixed(0) + ' Mbit/s' : '—'}</span></div>
        <div class="metric"><span>Coverage today</span><span class="v">${s.uptime.coverage != null ? s.uptime.coverage.toFixed(1) + ' %' : '—'}</span></div>
        <div class="metric"><span>Outages today</span><span class="v">${s.events.length}×</span></div>
      </div>`);
  });
}

/* Comparison: latency and loss = average of public targets per network, on a unified axis. */
async function pageCompare() {
  const {nets, t0, t1} = window.PAGE;
  const longRange = t1 - t0 > 86400 * 1.5;
  const q = `t0=${t0}&t1=${t1}`;
  const [series, notes] = await Promise.all([
    Promise.all(nets.map(n => getJSON(`/api/net/${n}/series?${q}`).catch(() => null))),
    getJSON(`/api/notes?${q}&nets=${nets.join(',')}`).catch(() => []),
  ]);
  const marks = noteMarks(notes);

  const bucketSet = new Set();
  series.forEach(s => s && s.latency.buckets.forEach(b => bucketSet.add(b)));
  const buckets = [...bucketSet].sort((a, b) => a - b);
  const idx = new Map(buckets.map((b, i) => [b, i]));
  const labels = buckets.map(b => fmtTs(b, longRange));

  const mkSeries = (s, pick) => {
    const out = new Array(buckets.length).fill(null);
    s.latency.buckets.forEach((b, j) => {
      const vals = PUBLIC_TARGETS
        .map(t => s.latency.targets[t]).filter(Boolean)
        .map(t => pick(t)[j]).filter(v => v != null);
      if (vals.length) out[idx.get(b)] = vals.reduce((a, v) => a + v, 0) / vals.length;
    });
    return out;
  };

  const netLabel = n => (window.PAGE.labels || {})[n] || n;
  const ds = fn => nets.map((n, i) => series[i] && ({
    label: netLabel(n), data: fn(series[i]), borderColor: colorForNet(i),
    backgroundColor: colorForNet(i), borderWidth: 1.8, tension: .25,
  })).filter(Boolean);

  lineChart('cmpLat', labels, ds(s => mkSeries(s, t => t.rtt)), 'ms', {epochs: buckets, marks});
  lineChart('cmpLoss', labels, ds(s => mkSeries(s, t => t.loss)), '% loss', {epochs: buckets, marks});

  // speed: unified axis from raw points
  const spdTs = new Set();
  series.forEach(s => s && s.speed.ts.forEach(t => spdTs.add(t)));
  const spdAxis = [...spdTs].sort((a, b) => a - b);
  const spdIdx = new Map(spdAxis.map((t, i) => [t, i]));
  lineChart('cmpSpd', spdAxis.map(t => fmtTs(t, longRange)),
    nets.map((n, i) => {
      if (!series[i]) return null;
      const data = new Array(spdAxis.length).fill(null);
      series[i].speed.ts.forEach((t, j) => data[spdIdx.get(t)] = series[i].speed.mbps[j]);
      return {label: netLabel(n), data, borderColor: colorForNet(i), backgroundColor: colorForNet(i),
              borderWidth: 2, tension: .3, pointRadius: 3, spanGaps: true};
    }).filter(Boolean), 'Mbit/s', {epochs: spdAxis, marks});
}

function netmonInit() {
  initNoteForm();
  document.addEventListener('keydown', e => {
    if (e.metaKey || e.ctrlKey || e.altKey ||
        /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.key === 'ArrowLeft') document.getElementById('prevRange')?.click();
    if (e.key === 'ArrowRight') document.getElementById('nextRange')?.click();
  });
  const fn = {network: pageNetwork, dashboard: pageDashboard, compare: pageCompare}[window.PAGE.type];
  fn().catch(err => {
    console.error(err);
    document.body.insertAdjacentHTML('beforeend',
      `<div class="panel" style="border-color:var(--bad)">Failed to load data: ${err.message}</div>`);
  });
}
