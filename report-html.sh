#!/usr/bin/env bash
# report-html.sh — vygeneruje hezký vizuální HTML přehled z nasbíraných dat.
# Použití: ./report-html.sh   ->  vytvoří report.html (otevři v prohlížeči)
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_ROOT="$DIR/log"; OUT="$DIR/report.html"

# Sloučí denní CSV (log/RRRRMMDD/<jméno>) do jednoho proudu — hlavička jen jednou.
merge_logs() {
  local name="$1" first=1 f
  for f in "$LOG_ROOT"/*/"$name"; do
    [ -f "$f" ] || continue
    if [ "$first" = 1 ]; then cat "$f"; first=0; else tail -n +2 "$f"; fi
  done
}
has_logs() { local f; for f in "$LOG_ROOT"/*/"$1"; do [ -f "$f" ] && return 0; done; return 1; }

has_logs latency.csv || { echo "Chybí logy v $LOG_ROOT — nejdřív spusť měření."; exit 1; }

# --- Souhrnné karty (per cíl): vzorky, ztráta %, avg/min/max latence ---
SUMMARY_JSON=$(awk -F, '
  NR>1 && $4!="" && $2!="--" {
    tot[$2]++
    if ($4=="LOSS") loss[$2]++
    else if ($5!="") { sum[$2]+=$5; n[$2]++
      if (min[$2]==""||$5<min[$2]) min[$2]=$5
      if ($5>max[$2]) max[$2]=$5 }
  }
  END{
    first=1; printf "["
    for (t in tot) {
      if(!first)printf ","; first=0
      printf "{\"target\":\"%s\",\"samples\":%d,\"loss\":%.2f,\"avg\":%.2f,\"min\":%.2f,\"max\":%.2f}",
        t, tot[t], (loss[t]/tot[t])*100, (n[t]?sum[t]/n[t]:0), min[t]+0, max[t]+0
    }
    printf "]"
  }' <(merge_logs latency.csv))

# --- Časová osa: latence (avg za minutu) a ztráty (% za minutu) per cíl ---
LAT_SERIES=$(awk -F, '
  NR>1 && $4!="" && $2!="--" {
    b=substr($1,1,16)            # bucket = minuta
    key=$2 SUBSEP b
    tot[key]++; targets[$2]=1; buckets[b]=1
    if ($4=="LOSS") loss[key]++
    else if ($5!=""){ sum[key]+=$5; n[key]++ }
  }
  END{
    # seřazené minutové bukety
    m=0; for(b in buckets) ord[m++]=b
    for(i=0;i<m;i++) for(j=i+1;j<m;j++) if(ord[j]<ord[i]){t=ord[i];ord[i]=ord[j];ord[j]=t}
    printf "{\"labels\":["
    for(i=0;i<m;i++){ printf "%s\"%s\"", (i?",":""), ord[i] }
    printf "],\"targets\":{"
    ft=1
    for(t in targets){
      if(!ft)printf ","; ft=0
      printf "\"%s\":{\"rtt\":[", t
      for(i=0;i<m;i++){ k=t SUBSEP ord[i]; v=(k in n && n[k]?sum[k]/n[k]:"null"); printf "%s%s",(i?",":""),v }
      printf "],\"loss\":["
      for(i=0;i<m;i++){ k=t SUBSEP ord[i]; v=((k in tot)?(loss[k]+0)/tot[k]*100:"null"); printf "%s%s",(i?",":""),v }
      printf "]}"
    }
    printf "}}"
  }' <(merge_logs latency.csv))

# --- Rychlost v čase ---
SPD_SERIES='{"labels":[],"mbps":[]}'
if has_logs speed.csv; then
  SPD_SERIES=$(awk -F, '
    BEGIN{ n=0 }
    NR>1 && $2!="" { lab[n]=substr($1,1,16); val[n]=$2; n++ }
    END{
      printf "{\"labels\":["
      for(i=0;i<n;i++) printf "%s\"%s\"",(i?",":""),lab[i]
      printf "],\"mbps\":["
      for(i=0;i<n;i++) printf "%s%s",(i?",":""),val[i]
      printf "]}"
    }' <(merge_logs speed.csv))
fi

# --- Dosažitelnost (DNS/TCP/TLS) v čase: avg za minutu ---
RCH_SERIES='{"labels":[],"dns":[],"tcp":[],"tls":[],"fails":0}'
if has_logs reach.csv; then
  RCH_SERIES=$(awk -F, '
    NR>1 && $6=="ok" { b=substr($1,1,16); if(!(b in seen)){seen[b]=1;ord[m++]=b}
      d[b]+=$2; t[b]+=$3; l[b]+=$4; n[b]++ }
    NR>1 && $6=="FAIL" { fails++ }
    END{
      for(i=0;i<m;i++)for(j=i+1;j<m;j++)if(ord[j]<ord[i]){x=ord[i];ord[i]=ord[j];ord[j]=x}
      printf "{\"labels\":["
      for(i=0;i<m;i++)printf "%s\"%s\"",(i?",":""),ord[i]
      printf "],\"dns\":["; for(i=0;i<m;i++){b=ord[i];printf "%s%.1f",(i?",":""),d[b]/n[b]}
      printf "],\"tcp\":["; for(i=0;i<m;i++){b=ord[i];printf "%s%.1f",(i?",":""),t[b]/n[b]}
      printf "],\"tls\":["; for(i=0;i<m;i++){b=ord[i];printf "%s%.1f",(i?",":""),l[b]/n[b]}
      printf "],\"fails\":%d}", fails+0
    }' <(merge_logs reach.csv))
fi

# --- Výpadky: přegeneruj events.csv a načti jako JSON ---
[ -x "$DIR/events.sh" ] && "$DIR/events.sh" >/dev/null 2>&1
EVT="$DIR/events.csv"
EVENTS_JSON='[]'
if [ -f "$EVT" ]; then
  EVENTS_JSON=$(awk -F, 'NR>1{ if(!f){printf "["; f=1}else printf ","
    printf "{\"start\":\"%s\",\"end\":\"%s\",\"dur\":%d,\"scope\":\"%s\"}",$1,$2,$3,$4 }
    END{ if(!f)printf "["; printf "]" }' "$EVT")
fi

# --- Běh skriptu: z uptime.csv odvoď, kdy měření neběželo (mezery mezi tepy) ---
# Mezera mezi sousedními záznamy větší než DOWN_THRESHOLD = skript byl vypnutý
# nebo neběžel celý počítač. STOP před mezerou = řízené zastavení; jinak pád/odpojení.
UPTIME_JSON='{"first":"","last":"","spanSec":0,"downSec":0,"gaps":[]}'
if has_logs uptime.csv; then
  UPTIME_JSON=$(awk -F, -v thr=150 '
    BEGIN{ ng=0; down=0 }
    function epoch(t,   Y,Mo,D,h,mi,s){
      Y=substr(t,1,4); Mo=substr(t,6,2); D=substr(t,9,2)
      h=substr(t,12,2); mi=substr(t,15,2); s=substr(t,18,2)
      return mktime(Y" "Mo" "D" "h" "mi" "s)
    }
    NR>1 && $1!="" {
      ts=$1; ev=$2; e=epoch(ts)
      if(first=="") first=ts
      if(prevTs!=""){
        gap=e-prevE
        if(gap>thr){
          cause=(prevEv=="STOP") ? "stopped" : "crash"
          g[ng]=sprintf("{\"from\":\"%s\",\"to\":\"%s\",\"dur\":%d,\"cause\":\"%s\"}",prevTs,ts,gap,cause)
          ng++; down+=gap
        }
      }
      prevTs=ts; prevE=e; prevEv=ev; last=ts; lastE=e; firstE=(firstE==""?e:firstE)
    }
    END{
      span=(lastE>firstE)?lastE-firstE:0
      printf "{\"first\":\"%s\",\"last\":\"%s\",\"spanSec\":%d,\"downSec\":%d,\"gaps\":[",first,last,span,down+0
      for(i=0;i<ng;i++) printf "%s%s",(i?",":""),g[i]
      printf "]}"
    }' <(merge_logs uptime.csv))
fi

# --- Meta: rozsah a délka měření ---
META_JSON=$(awk -F, 'NR>1 && $1!="" && $2!="--"{ if(first=="")first=$1; last=$1 } END{
  printf "{\"first\":\"%s\",\"last\":\"%s\"}", first, last }' <(merge_logs latency.csv))
GEN_TS=$(date "+%Y-%m-%d %H:%M:%S %Z")

cat > "$OUT" <<HTMLEOF
<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>netmon — přehled kvality připojení</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--mut:#94a3b8;--fg:#e2e8f0;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--accent:#38bdf8}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;padding:24px}
  h1{margin:0 0 4px;font-size:24px}
  .sub{color:var(--mut);margin-bottom:24px;font-size:13px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:28px}
  .card{background:var(--card);border-radius:12px;padding:16px 18px;border:1px solid #334155}
  .card h3{margin:0 0 10px;font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
  .metric{display:flex;justify-content:space-between;margin:4px 0;font-size:14px}
  .metric .v{font-variant-numeric:tabular-nums;font-weight:600}
  .big{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .ok{background:rgba(34,197,94,.15);color:var(--ok)}
  .warn{background:rgba(245,158,11,.15);color:var(--warn)}
  .bad{background:rgba(239,68,68,.15);color:var(--bad)}
  .panel{background:var(--card);border-radius:12px;padding:18px;margin-bottom:22px;border:1px solid #334155}
  .panel h2{margin:0 0 14px;font-size:16px}
  canvas{max-height:320px}
  table.evt{width:100%;border-collapse:collapse;font-size:14px}
  table.evt th{text-align:left;color:var(--mut);font-weight:600;padding:6px 10px;border-bottom:1px solid #334155}
  table.evt td{padding:6px 10px;border-bottom:1px solid #233044;font-variant-numeric:tabular-nums}
  .foot{color:var(--mut);font-size:12px;text-align:center;margin-top:30px}
</style>
</head>
<body>
  <h1>📡 Kvalita připojení</h1>
  <div class="sub" id="period"></div>
  <div class="cards" id="cards"></div>

  <div class="panel"><h2>⏱️ Běh měření</h2><div id="uptime"></div></div>
  <div class="panel"><h2>🛑 Výpadky</h2><div id="events"></div></div>
  <div class="panel"><h2>Latence v čase (ms)</h2><canvas id="latChart"></canvas></div>
  <div class="panel"><h2>Ztráta paketů (% za minutu)</h2><canvas id="lossChart"></canvas></div>
  <div class="panel"><h2>Dosažitelnost služeb — DNS / TCP / TLS (ms)</h2><canvas id="rchChart"></canvas></div>
  <div class="panel"><h2>Rychlost stahování (Mbit/s)</h2><canvas id="spdChart"></canvas></div>

  <div class="foot">Vygenerováno: ${GEN_TS} · netmon</div>

<script>
const SUMMARY = ${SUMMARY_JSON};
const LAT = ${LAT_SERIES};
const SPD = ${SPD_SERIES};
const RCH = ${RCH_SERIES};
const EVENTS = ${EVENTS_JSON};
const UPTIME = ${UPTIME_JSON};
const META = ${META_JSON};
const COLORS = {gateway:'#38bdf8', quad9:'#a78bfa', google:'#f472b6', _0:'#34d399', _1:'#fbbf24', _2:'#fb7185'};
const colorFor = (name,i)=> COLORS[name] || COLORS['_'+(i%3)];

// Období
document.getElementById('period').textContent =
  'Měřeno: ' + (META.first||'?').replace('T',' ') + '  →  ' + (META.last||'?').replace('T',' ');

// Karty
const cardsEl = document.getElementById('cards');
SUMMARY.sort((a,b)=>a.target.localeCompare(b.target)).forEach(s=>{
  const cls = s.loss>1 ? 'bad' : s.loss>0.1 ? 'warn' : 'ok';
  const lbl = s.loss>1 ? 'problém' : s.loss>0.1 ? 'drobné ztráty' : 'OK';
  cardsEl.insertAdjacentHTML('beforeend', \`
    <div class="card">
      <h3>\${s.target}</h3>
      <div class="metric"><span>Ztráta paketů</span><span class="pill \${cls}">\${s.loss.toFixed(2)}% · \${lbl}</span></div>
      <div class="metric"><span>Latence avg</span><span class="v">\${s.avg.toFixed(1)} ms</span></div>
      <div class="metric"><span>Latence min / max</span><span class="v">\${s.min.toFixed(1)} / \${s.max.toFixed(1)} ms</span></div>
      <div class="metric"><span>Vzorků</span><span class="v">\${s.samples.toLocaleString('cs')}</span></div>
    </div>\`);
});

// Karta rychlosti
if (SPD.mbps.length){
  const m=SPD.mbps, avg=m.reduce((a,b)=>a+b,0)/m.length, mn=Math.min(...m), mx=Math.max(...m);
  cardsEl.insertAdjacentHTML('beforeend', \`
    <div class="card">
      <h3>rychlost ⬇</h3>
      <div class="big">\${avg.toFixed(0)} <span style="font-size:14px;color:var(--mut)">Mbit/s avg</span></div>
      <div class="metric"><span>min / max</span><span class="v">\${mn.toFixed(0)} / \${mx.toFixed(0)}</span></div>
      <div class="metric"><span>měření</span><span class="v">\${m.length}</span></div>
    </div>\`);
}

// Běh měření — kdy skript/počítač neběžel (mezery mezi tepy v uptime.csv)
(function(){
  const el=document.getElementById('uptime');
  const fmtDur=s=> s>=3600 ? (s/3600).toFixed(1)+' h' : s>=60 ? (s/60).toFixed(1)+' min' : s+' s';
  if(!UPTIME.first){ el.innerHTML='<p style="color:var(--mut);margin:0">Zatím žádný záznam o běhu (uptime.csv).</p>'; return; }
  const up = UPTIME.spanSec - UPTIME.downSec;
  const cov = UPTIME.spanSec>0 ? (up/UPTIME.spanSec*100) : 100;
  const stopped = UPTIME.gaps.filter(g=>g.cause==='stopped').length;
  const crash   = UPTIME.gaps.filter(g=>g.cause==='crash').length;
  // Souhrnná karta pokrytí
  const covCls = cov>=99 ? 'ok' : cov>=90 ? 'warn' : 'bad';
  document.getElementById('cards').insertAdjacentHTML('beforeend', \`
    <div class="card">
      <h3>běh měření</h3>
      <div class="big">\${cov.toFixed(1)}<span style="font-size:14px;color:var(--mut)"> % pokrytí</span></div>
      <div class="metric"><span>Doba běhu</span><span class="v">\${fmtDur(up)}</span></div>
      <div class="metric"><span>Mimo provoz</span><span class="pill \${covCls}">\${fmtDur(UPTIME.downSec)}</span></div>
      <div class="metric"><span>Přerušení</span><span class="v">\${UPTIME.gaps.length}×</span></div>
    </div>\`);

  let head='<div style="margin-bottom:10px">';
  head+=\`<span class="pill \${covCls}">pokrytí \${cov.toFixed(1)}% · mimo provoz \${fmtDur(UPTIME.downSec)}</span> \`;
  if(crash)   head+=\`<span class="pill bad">neočekávaná přerušení: \${crash}×</span> \`;
  if(stopped) head+=\`<span class="pill warn">řízená zastavení: \${stopped}×</span>\`;
  head+='</div>';
  if(!UPTIME.gaps.length){
    el.innerHTML=head+'<p style="color:var(--ok);margin:0">Měření běželo bez přerušení. 🎉</p>';
    return;
  }
  const rows=UPTIME.gaps.slice().sort((a,b)=>b.dur-a.dur).map(g=>{
    const isCrash=g.cause==='crash';
    const cls=isCrash?'bad':'warn';
    const lbl=isCrash?'pád / vypnutý počítač':'skript zastaven';
    return \`<tr><td>\${g.from.replace('T',' ')}</td><td>\${g.to.replace('T',' ')}</td>\`+
           \`<td style="text-align:right">\${fmtDur(g.dur)}</td><td><span class="pill \${cls}">\${lbl}</span></td></tr>\`;
  }).join('');
  el.innerHTML=head+\`<table class="evt"><thead><tr><th>od</th><th>do (znovu naběhlo)</th><th style="text-align:right">trvání</th><th>příčina</th></tr></thead><tbody>\${rows}</tbody></table>\`;
})();

// Tabulka výpadků
(function(){
  const el=document.getElementById('events');
  if(!EVENTS.length){ el.innerHTML='<p style="color:var(--ok);margin:0">Žádné výpadky během měření. 🎉</p>'; return; }
  const fmtDur=s=> s>=60 ? (s/60).toFixed(1)+' min' : s+' s';
  const tot={}; EVENTS.forEach(e=>tot[e.scope]=(tot[e.scope]||0)+e.dur);
  let head='<div style="margin-bottom:10px">';
  if(tot.local) head+=\`<span class="pill bad">lokál: \${EVENTS.filter(e=>e.scope==='local').length}× · \${fmtDur(tot.local)}</span> \`;
  if(tot.internet) head+=\`<span class="pill warn">internet: \${EVENTS.filter(e=>e.scope==='internet').length}× · \${fmtDur(tot.internet)}</span>\`;
  head+='</div>';
  let rows=EVENTS.slice().sort((a,b)=>b.dur-a.dur).map(e=>{
    const cls=e.scope==='local'?'bad':'warn';
    const lbl=e.scope==='local'?'lokální linka':'internet / ISP';
    return \`<tr><td>\${e.start.replace('T',' ')}</td><td>\${e.end.replace('T',' ').slice(11)}</td>\`+
           \`<td style="text-align:right">\${fmtDur(e.dur)}</td><td><span class="pill \${cls}">\${lbl}</span></td></tr>\`;
  }).join('');
  el.innerHTML=head+\`<table class="evt"><thead><tr><th>začátek</th><th>konec</th><th style="text-align:right">trvání</th><th>rozsah</th></tr></thead><tbody>\${rows}</tbody></table>\`;
})();

const baseOpts = (yLabel)=>({
  responsive:true, interaction:{mode:'index',intersect:false},
  scales:{
    x:{ticks:{color:'#94a3b8',maxTicksLimit:12,maxRotation:0},grid:{color:'#1e293b'}},
    y:{title:{display:true,text:yLabel,color:'#94a3b8'},ticks:{color:'#94a3b8'},grid:{color:'#1e293b'},beginAtZero:true}
  },
  plugins:{legend:{labels:{color:'#e2e8f0'}}},
  elements:{point:{radius:0}}, spanGaps:true
});

// Latence
new Chart(document.getElementById('latChart'), {
  type:'line',
  data:{ labels:LAT.labels, datasets:Object.keys(LAT.targets).map((t,i)=>({
    label:t, data:LAT.targets[t].rtt, borderColor:colorFor(t,i),
    backgroundColor:colorFor(t,i), borderWidth:1.6, tension:.25 })) },
  options: baseOpts('ms')
});

// Ztráty
new Chart(document.getElementById('lossChart'), {
  type:'line',
  data:{ labels:LAT.labels, datasets:Object.keys(LAT.targets).map((t,i)=>({
    label:t, data:LAT.targets[t].loss, borderColor:colorFor(t,i),
    backgroundColor:colorFor(t,i), borderWidth:1.6, tension:.25, fill:false })) },
  options: baseOpts('% ztrát / min')
});

// Dosažitelnost DNS/TCP/TLS
new Chart(document.getElementById('rchChart'), {
  type:'line',
  data:{ labels:RCH.labels, datasets:[
    {label:'DNS', data:RCH.dns, borderColor:'#34d399', backgroundColor:'#34d399', borderWidth:1.6, tension:.25},
    {label:'TCP', data:RCH.tcp, borderColor:'#fbbf24', backgroundColor:'#fbbf24', borderWidth:1.6, tension:.25},
    {label:'TLS', data:RCH.tls, borderColor:'#fb7185', backgroundColor:'#fb7185', borderWidth:1.6, tension:.25} ]},
  options: baseOpts('ms')
});

// Rychlost
new Chart(document.getElementById('spdChart'), {
  type:'line',
  data:{ labels:SPD.labels, datasets:[{
    label:'download', data:SPD.mbps, borderColor:'#38bdf8',
    backgroundColor:'rgba(56,189,248,.15)', borderWidth:2, tension:.3, fill:true, pointRadius:3 }] },
  options: baseOpts('Mbit/s')
});
</script>
</body>
</html>
HTMLEOF

echo "Hotovo → $OUT"
echo "Otevři v prohlížeči:  xdg-open \"$OUT\""
