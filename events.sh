#!/usr/bin/env bash
# events.sh — odvodí z latency.csv čitelný seznam výpadků do events.csv
#   scope=local    … nedostupná brána (problém na tvé straně: kabel/switch/router)
#   scope=internet … brána OK, ale oba veřejné cíle nedostupné (problém u providera)
# Spusť kdykoliv: ./events.sh   (přegeneruje events.csv a vypíše souhrn)
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_ROOT="$DIR/log"; OUT="$DIR/events.csv"

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

awk -F, -v interval=2 '
  function epoch(t,   Y,Mo,D,h,mi,s){
    Y=substr(t,1,4); Mo=substr(t,6,2); D=substr(t,9,2)
    h=substr(t,12,2); mi=substr(t,15,2); s=substr(t,18,2)
    return mktime(Y" "Mo" "D" "h" "mi" "s)
  }
  function emit(scope,st,en,   dur,note){
    dur=epoch(en)-epoch(st); if(dur<interval) dur=interval
    note=(scope=="local") ? "lokální linka (brána nedostupná)" \
                          : "internet (oba veřejné cíle nedostupné)"
    printf "%s,%s,%d,%s,%s\n", st, en, dur, scope, note >> OUT
    cnt[scope]++; total[scope]+=dur; if(dur>longest[scope]){longest[scope]=dur; longwhen[scope]=st}
  }
  NR>1 && $2!="--" {
    ts=$1
    if (!(ts in seen)) { seen[ts]=1; order[no++]=ts }   # latency.csv je chronologické
    if ($4=="LOSS") {
      if ($2=="gateway") gw[ts]=1
      else if ($2=="quad9") q[ts]=1
      else if ($2=="google") g[ts]=1
    }
  }
  END{
    print "start,end,duration_s,scope,note" > OUT
    cur=""; startts=""; prevts=""
    for(i=0;i<no;i++){
      ts=order[i]
      st = gw[ts] ? "local" : ((q[ts] && g[ts]) ? "internet" : "ok")
      if (st!="ok") {
        if (cur=="") { cur=st; startts=ts }
        else if (st!=cur) { emit(cur,startts,prevts); cur=st; startts=ts }
      } else if (cur!="") { emit(cur,startts,prevts); cur="" }
      prevts=ts
    }
    if (cur!="") emit(cur,startts,prevts)

    # Souhrn na stdout
    printf "===== VÝPADKY — souhrn =====\n\n"
    if (cnt["local"]+cnt["internet"]==0) { print "Žádné výpadky. 🎉"; }
    else {
      printf "%-10s %8s %14s %14s   %s\n","rozsah","počet","celkem","nejdelší","kdy nejdelší"
      for (s in cnt) {
        printf "%-10s %8d %12ds %12ds   %s\n", s, cnt[s], total[s], longest[s], longwhen[s]
      }
      printf "\nDetail v events.csv (%s)\n", OUT
    }
  }
' OUT="$OUT" <(merge_logs latency.csv)
