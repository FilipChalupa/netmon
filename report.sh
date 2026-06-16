#!/usr/bin/env bash
# report.sh — souhrn z nasbíraných dat. Spusť kdykoliv: ./report.sh
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAT="$DIR/latency.csv"; SPD="$DIR/speed.csv"

echo "===== KVALITA PŘIPOJENÍ — souhrn ====="
echo
echo "--- Latence & výpadky (podle cíle) ---"
awk -F, 'NR>1 && $4!="" {
  tot[$2]++
  if ($4=="LOSS") loss[$2]++
  else if ($5!="") { sum[$2]+=$5; n[$2]++
    if (min[$2]==""||$5<min[$2]) min[$2]=$5
    if ($5>max[$2]) max[$2]=$5 }
}
END{
  printf "%-12s %8s %8s %9s %9s %9s\n","cíl","vzorků","ztráta%","avg ms","min ms","max ms"
  for (t in tot) if (t!="--")
    printf "%-12s %8d %7.2f%% %9.1f %9.1f %9.1f\n", t, tot[t],
      (loss[t]/tot[t])*100, (n[t]?sum[t]/n[t]:0), min[t]+0, max[t]+0
}' "$LAT" | sort

echo
echo "--- Nejdelší souvislé výpadky (cíl 'google') ---"
awk -F, '$2=="google"{
  if($4=="LOSS"){ if(start=="")start=$1; cnt++ }
  else { if(cnt>1) print cnt" kol v řadě  od "start; start=""; cnt=0 }
}' "$LAT" | sort -rn | head -5
[ -s /dev/stdin ] || true

echo
echo "--- Rychlost stahování (Mbit/s) ---"
awk -F, 'NR>1 && $2!=""{ s+=$2; n++; if(min==""||$2<min)min=$2; if($2>max)max=$2 }
END{ if(n) printf "měření: %d   avg: %.1f   min: %.1f   max: %.1f\n", n, s/n, min, max
     else print "zatím žádná úspěšná měření" }' "$SPD"
fails=$(awk -F, 'NR>1 && $2==""{c++}END{print c+0}' "$SPD")
echo "neúspěšná měření rychlosti: $fails"

RCH="$DIR/reach.csv"
if [ -f "$RCH" ]; then
  echo
  echo "--- Dosažitelnost služeb (DNS / TCP / TLS, ms) ---"
  awk -F, 'NR>1 && $6=="ok"{ d+=$2;t+=$3;l+=$4;n++ } NR>1 && $6=="FAIL"{f++}
    END{ if(n) printf "úspěšných: %d   avg DNS: %.1f   avg TCP: %.1f   avg TLS: %.1f\n", n, d/n, t/n, l/n
         printf "selhání (nedostupné služby): %d\n", f+0 }' "$RCH"
fi

if [ -x "$DIR/events.sh" ]; then
  echo
  "$DIR/events.sh" | sed -n '/=====/,$p'
fi

echo
echo "Tip: vizuální přehled → ./report-html.sh && xdg-open report.html"
