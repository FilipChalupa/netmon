/* netmon frontend — dashboard, detail sítě, porovnání.
   Vzhled a prahy převzaty z původního report-html.sh. */

const TARGET_COLORS = {gateway:'#38bdf8', quad9:'#a78bfa', google:'#f472b6',
                       _0:'#34d399', _1:'#fbbf24', _2:'#fb7185'};
const NET_COLORS = ['#38bdf8', '#f472b6', '#34d399', '#fbbf24', '#a78bfa', '#fb7185'];
const PUBLIC_TARGETS = ['quad9', 'google'];

const colorForTarget = (name, i) => TARGET_COLORS[name] || TARGET_COLORS['_' + (i % 3)];
const colorForNet = i => NET_COLORS[i % NET_COLORS.length];

const fmtDur = s => s >= 3600 ? (s / 3600).toFixed(1) + ' h'
                  : s >= 60 ? (s / 60).toFixed(1) + ' min' : s + ' s';
const lossCls = l => l > 1 ? 'bad' : l > 0.1 ? 'warn' : 'ok';
const lossLbl = l => l > 1 ? 'problém' : l > 0.1 ? 'drobné ztráty' : 'OK';

function fmtTs(epoch, longRange) {
  const d = new Date(epoch * 1000);
  const hm = d.toLocaleTimeString('cs-CZ', {hour: '2-digit', minute: '2-digit'});
  if (!longRange) return hm;
  return d.toLocaleDateString('cs-CZ', {day: 'numeric', month: 'numeric'}) + ' ' + hm;
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

function lineChart(id, labels, datasets, yLabel) {
  const el = document.getElementById(id);
  if (el) new Chart(el, {type: 'line', data: {labels, datasets}, options: baseOpts(yLabel)});
}

/* ---------- karty a panely detailu sítě (dle report-html.sh) ---------- */

function renderCards(sum) {
  const el = document.getElementById('cards');
  el.innerHTML = '';
  sum.targets.forEach(s => {
    el.insertAdjacentHTML('beforeend', `
      <div class="card">
        <h3>${s.target}</h3>
        <div class="metric"><span>Ztráta paketů</span><span class="pill ${lossCls(s.loss)}">${s.loss.toFixed(2)}% · ${lossLbl(s.loss)}</span></div>
        <div class="metric"><span>Latence avg</span><span class="v">${s.avg == null ? '—' : s.avg.toFixed(1) + ' ms'}</span></div>
        <div class="metric"><span>Latence min / max</span><span class="v">${s.min == null ? '—' : s.min.toFixed(1) + ' / ' + s.max.toFixed(1) + ' ms'}</span></div>
        <div class="metric"><span>Vzorků</span><span class="v">${s.samples.toLocaleString('cs')}</span></div>
      </div>`);
  });
  if (sum.speed.n) {
    el.insertAdjacentHTML('beforeend', `
      <div class="card">
        <h3>rychlost ⬇</h3>
        <div class="big">${sum.speed.avg.toFixed(0)} <span style="font-size:14px;color:var(--mut)">Mbit/s avg</span></div>
        <div class="metric"><span>min / max</span><span class="v">${sum.speed.min.toFixed(0)} / ${sum.speed.max.toFixed(0)}</span></div>
        <div class="metric"><span>měření</span><span class="v">${sum.speed.n}</span></div>
      </div>`);
  }
  const u = sum.uptime;
  if (u.coverage != null) {
    const covCls = u.coverage >= 99 ? 'ok' : u.coverage >= 90 ? 'warn' : 'bad';
    el.insertAdjacentHTML('beforeend', `
      <div class="card">
        <h3>běh měření</h3>
        <div class="big">${u.coverage.toFixed(1)}<span style="font-size:14px;color:var(--mut)"> % pokrytí</span></div>
        <div class="metric"><span>Doba běhu</span><span class="v">${fmtDur(u.span_s - u.down_s)}</span></div>
        <div class="metric"><span>Mimo provoz</span><span class="pill ${covCls}">${fmtDur(u.down_s)}</span></div>
        <div class="metric"><span>Přerušení</span><span class="v">${u.gaps.length}×</span></div>
      </div>`);
  }
}

function renderUptime(u) {
  const el = document.getElementById('uptime');
  if (u.coverage == null) {
    el.innerHTML = '<p class="empty" style="margin:0">V tomto období žádný záznam o běhu měření.</p>';
    return;
  }
  const covCls = u.coverage >= 99 ? 'ok' : u.coverage >= 90 ? 'warn' : 'bad';
  const crash = u.gaps.filter(g => g.cause === 'crash').length;
  const stopped = u.gaps.filter(g => g.cause === 'stopped').length;
  let head = '<div style="margin-bottom:10px">';
  head += `<span class="pill ${covCls}">pokrytí ${u.coverage.toFixed(1)}% · mimo provoz ${fmtDur(u.down_s)}</span> `;
  if (crash) head += `<span class="pill bad">neočekávaná přerušení: ${crash}×</span> `;
  if (stopped) head += `<span class="pill warn">řízená zastavení: ${stopped}×</span>`;
  head += '</div>';
  if (!u.gaps.length) {
    el.innerHTML = head + '<p style="color:var(--ok);margin:0">Měření běželo bez přerušení. 🎉</p>';
    return;
  }
  const rows = u.gaps.slice().sort((a, b) => b.dur - a.dur).map(g => {
    const isCrash = g.cause === 'crash';
    return `<tr><td>${fmtIso(g.from)}</td><td>${fmtIso(g.to)}</td>` +
           `<td style="text-align:right">${fmtDur(g.dur)}</td>` +
           `<td><span class="pill ${isCrash ? 'bad' : 'warn'}">${isCrash ? 'pád / vypnutý počítač' : 'skript zastaven'}</span></td></tr>`;
  }).join('');
  el.innerHTML = head + `<table class="evt"><thead><tr><th>od</th><th>do (znovu naběhlo)</th>` +
    `<th style="text-align:right">trvání</th><th>příčina</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderEvents(events) {
  const el = document.getElementById('events');
  if (!events.length) {
    el.innerHTML = '<p style="color:var(--ok);margin:0">Žádné výpadky během měření. 🎉</p>';
    return;
  }
  const tot = {};
  events.forEach(e => tot[e.scope] = (tot[e.scope] || 0) + e.dur);
  let head = '<div style="margin-bottom:10px">';
  if (tot.local) head += `<span class="pill bad">lokál: ${events.filter(e => e.scope === 'local').length}× · ${fmtDur(tot.local)}</span> `;
  if (tot.internet) head += `<span class="pill warn">internet: ${events.filter(e => e.scope === 'internet').length}× · ${fmtDur(tot.internet)}</span>`;
  head += '</div>';
  const rows = events.slice().sort((a, b) => b.dur - a.dur).map(e => {
    const local = e.scope === 'local';
    return `<tr><td>${fmtIso(e.start)}</td><td>${fmtIso(e.end).slice(11)}</td>` +
           `<td style="text-align:right">${fmtDur(e.dur)}</td>` +
           `<td><span class="pill ${local ? 'bad' : 'warn'}">${local ? 'lokální linka' : 'internet / ISP'}</span></td></tr>`;
  }).join('');
  el.innerHTML = head + `<table class="evt"><thead><tr><th>začátek</th><th>konec</th>` +
    `<th style="text-align:right">trvání</th><th>rozsah</th></tr></thead><tbody>${rows}</tbody></table>`;
}

/* ---------- stránky ---------- */

async function pageNetwork() {
  const {name, t0, t1} = window.PAGE;
  const longRange = t1 - t0 > 86400 * 1.5;
  const q = `t0=${t0}&t1=${t1}`;
  const [sum, series] = await Promise.all([
    getJSON(`/api/net/${name}/summary?${q}`),
    getJSON(`/api/net/${name}/series?${q}`),
  ]);
  if (sum.period.first) {
    document.getElementById('period').textContent =
      'Měřeno: ' + fmtIso(sum.period.first) + '  →  ' + fmtIso(sum.period.last);
  }
  renderCards(sum);
  renderUptime(sum.uptime);
  renderEvents(sum.events);

  const lat = series.latency;
  const labels = lat.buckets.map(b => fmtTs(b, longRange));
  const targetNames = Object.keys(lat.targets).sort();
  lineChart('latChart', labels, targetNames.map((t, i) => ({
    label: t, data: lat.targets[t].rtt, borderColor: colorForTarget(t, i),
    backgroundColor: colorForTarget(t, i), borderWidth: 1.6, tension: .25,
  })), 'ms');
  lineChart('lossChart', labels, targetNames.map((t, i) => ({
    label: t, data: lat.targets[t].loss, borderColor: colorForTarget(t, i),
    backgroundColor: colorForTarget(t, i), borderWidth: 1.6, tension: .25, fill: false,
  })), '% ztrát');

  const rch = series.reach;
  lineChart('rchChart', rch.buckets.map(b => fmtTs(b, longRange)), [
    {label: 'DNS', data: rch.dns, borderColor: '#34d399', backgroundColor: '#34d399', borderWidth: 1.6, tension: .25},
    {label: 'TCP', data: rch.tcp, borderColor: '#fbbf24', backgroundColor: '#fbbf24', borderWidth: 1.6, tension: .25},
    {label: 'TLS', data: rch.tls, borderColor: '#fb7185', backgroundColor: '#fb7185', borderWidth: 1.6, tension: .25},
  ], 'ms');

  const spd = series.speed;
  lineChart('spdChart', spd.ts.map(t => fmtTs(t, longRange)), [{
    label: 'download', data: spd.mbps, borderColor: '#38bdf8',
    backgroundColor: 'rgba(56,189,248,.15)', borderWidth: 2, tension: .3,
    fill: true, pointRadius: 3,
  }], 'Mbit/s');
}

async function pageDashboard() {
  const nets = await getJSON('/api/networks');
  const el = document.getElementById('netcards');
  if (!nets.length) {
    el.innerHTML = '<p class="empty">Zatím žádné sítě — přidej monitory do monitors.toml ' +
                   'nebo naimportuj historická data.</p>';
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
      statePill = '<span class="pill mutpill">dnes bez dat</span>';
    } else if (n.sync.configured && !n.sync.online) {
      statePill = '<span class="pill mutpill">monitor nedostupný</span>';
    } else if ((gw && gw.loss > 1) || worstLoss > 1 || s.events.length) {
      statePill = '<span class="pill bad">výpadky</span>';
    } else if (worstLoss > 0.1) {
      statePill = '<span class="pill warn">drobné ztráty</span>';
    } else {
      statePill = '<span class="pill ok">OK</span>';
    }
    el.insertAdjacentHTML('beforeend', `
      <div class="card">
        <h3><a href="/net/${n.name}">${n.label}</a> ${statePill}</h3>
        <div class="metric"><span>Ztráta (internet)</span><span class="pill ${lossCls(worstLoss)}">${worstLoss.toFixed(2)}%</span></div>
        <div class="metric"><span>Latence avg</span><span class="v">${pub && pub.avg != null ? pub.avg.toFixed(1) + ' ms' : '—'}</span></div>
        <div class="metric"><span>Poslední rychlost</span><span class="v">${s.speed.last != null ? s.speed.last.toFixed(0) + ' Mbit/s' : '—'}</span></div>
        <div class="metric"><span>Pokrytí dnes</span><span class="v">${s.uptime.coverage != null ? s.uptime.coverage.toFixed(1) + ' %' : '—'}</span></div>
        <div class="metric"><span>Výpadky dnes</span><span class="v">${s.events.length}×</span></div>
      </div>`);
  });
}

/* Porovnání: latence a ztráty = průměr veřejných cílů per síť, na sjednocené ose. */
async function pageCompare() {
  const {nets, t0, t1} = window.PAGE;
  const longRange = t1 - t0 > 86400 * 1.5;
  const q = `t0=${t0}&t1=${t1}`;
  const series = await Promise.all(
    nets.map(n => getJSON(`/api/net/${n}/series?${q}`).catch(() => null)));

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

  lineChart('cmpLat', labels, ds(s => mkSeries(s, t => t.rtt)), 'ms');
  lineChart('cmpLoss', labels, ds(s => mkSeries(s, t => t.loss)), '% ztrát');

  // rychlost: sjednocená osa ze surových bodů
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
    }).filter(Boolean), 'Mbit/s');
}

function netmonInit() {
  const fn = {network: pageNetwork, dashboard: pageDashboard, compare: pageCompare}[window.PAGE.type];
  fn().catch(err => {
    console.error(err);
    document.body.insertAdjacentHTML('beforeend',
      `<div class="panel" style="border-color:var(--bad)">Chyba načítání dat: ${err.message}</div>`);
  });
}
