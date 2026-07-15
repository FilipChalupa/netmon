#!/usr/bin/env bash
#
# report-daily.sh — vygeneruje report za PŘEDCHOZÍ den (nebo za den z argumentu)
# a — je-li vyplněný SMTP v .env — pošle ho e-mailem.
#
# Spuštění ručně:
#   ./report-daily.sh            # za včerejšek
#   ./report-daily.sh 20260622   # za konkrétní den (RRRRMMDD)
#
# Automaticky běží ve 3:00 přes systemd user timer (netmon-report.timer),
# viz install-report-timer.sh.
#
# E-mail se pošle jen když je v souboru .env (vedle skriptu) vyplněný aspoň
# SMTP_HOST a SMTP_TO. Bez nich se report jen vygeneruje na disk. Posílá se
# přes curl (žádná další závislost) — text reportu v těle, HTML jako příloha.
set -u
PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_ROOT="$DIR/log"

# --- Konfigurace e-mailu z .env (volitelné) -------------------------------
# Očekávané proměnné (viz .env.example):
#   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM, SMTP_TO, SMTP_TLS
if [ -f "$DIR/.env" ]; then
  set -a; . "$DIR/.env"; set +a
fi

# --- Který den reportovat -------------------------------------------------
DAY="${1:-$(date -d 'yesterday' +%Y%m%d)}"
if ! printf '%s' "$DAY" | grep -Eq '^[0-9]{8}$'; then
  echo "Chybný den: '$DAY' (čekám RRRRMMDD)" >&2; exit 1
fi
DAYDIR="$LOG_ROOT/$DAY"
DAY_HUMAN="${DAY:0:4}-${DAY:4:2}-${DAY:6:2}"

if [ ! -f "$DAYDIR/latency.csv" ]; then
  echo "Pro den $DAY_HUMAN nejsou žádná data ($DAYDIR/latency.csv chybí) — nic negeneruji."
  exit 0
fi

# --- Vygeneruj report omezený na tento den --------------------------------
export NETMON_DAY="$DAY"
export NETMON_OUT="$DAYDIR/report-$DAY.html"
export NETMON_EVENTS_OUT="$DAYDIR/events-$DAY.csv"
TXT="$DAYDIR/report-$DAY.txt"

"$DIR/report-html.sh" >/dev/null
"$DIR/report.sh" > "$TXT"

echo "Report za $DAY_HUMAN:"
echo "  HTML: $NETMON_OUT"
echo "  text: $TXT"

# --- E-mail (jen když je vyplněný SMTP) -----------------------------------
SMTP_HOST="${SMTP_HOST:-}"; SMTP_TO="${SMTP_TO:-}"
if [ -z "$SMTP_HOST" ] || [ -z "$SMTP_TO" ]; then
  echo "SMTP není vyplněný (SMTP_HOST/SMTP_TO v .env) — e-mail neposílám."
  exit 0
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl není dostupný — e-mail nemůžu poslat." >&2; exit 1
fi

SMTP_FROM="${SMTP_FROM:-${SMTP_USER:-netmon@localhost}}"
SMTP_TLS="${SMTP_TLS:-starttls}"
case "$SMTP_TLS" in
  ssl|smtps) PROTO="smtps"; SMTP_PORT="${SMTP_PORT:-465}" ;;
  none)      PROTO="smtp";  SMTP_PORT="${SMTP_PORT:-25}"  ;;
  *)         PROTO="smtp";  SMTP_PORT="${SMTP_PORT:-587}" ;;   # starttls
esac

HOST="$(hostname 2>/dev/null || echo netmon)"
SUBJECT="netmon report $DAY_HUMAN ($HOST)"

# Sestav MIME zprávu: text/plain tělo + HTML příloha (obojí base64/UTF-8).
MSG="$(mktemp "${TMPDIR:-/tmp}/netmon-mail.XXXXXX")"
trap 'rm -f "$MSG"' EXIT
BOUNDARY="netmon_$(date +%s)_$$"
{
  printf 'From: %s\r\n' "$SMTP_FROM"
  printf 'To: %s\r\n' "$SMTP_TO"
  printf 'Subject: %s\r\n' "$SUBJECT"
  printf 'Date: %s\r\n' "$(date -R)"
  printf 'MIME-Version: 1.0\r\n'
  printf 'Content-Type: multipart/mixed; boundary="%s"\r\n' "$BOUNDARY"
  printf '\r\n'
  # Tělo: textový souhrn
  printf -- '--%s\r\n' "$BOUNDARY"
  printf 'Content-Type: text/plain; charset=UTF-8\r\n'
  printf 'Content-Transfer-Encoding: base64\r\n'
  printf '\r\n'
  base64 "$TXT"
  printf '\r\n'
  # Příloha: HTML report
  printf -- '--%s\r\n' "$BOUNDARY"
  printf 'Content-Type: text/html; charset=UTF-8; name="report-%s.html"\r\n' "$DAY"
  printf 'Content-Transfer-Encoding: base64\r\n'
  printf 'Content-Disposition: attachment; filename="report-%s.html"\r\n' "$DAY"
  printf '\r\n'
  base64 "$NETMON_OUT"
  printf '\r\n'
  printf -- '--%s--\r\n' "$BOUNDARY"
} > "$MSG"

# Příjemci: oddělené čárkou nebo mezerou.
RCPT_ARGS=()
for r in ${SMTP_TO//,/ }; do RCPT_ARGS+=(--mail-rcpt "$r"); done

CURL_ARGS=(--silent --show-error --url "$PROTO://$SMTP_HOST:$SMTP_PORT"
           --mail-from "$SMTP_FROM" "${RCPT_ARGS[@]}" --upload-file "$MSG")
[ "$SMTP_TLS" = "none" ] || CURL_ARGS+=(--ssl-reqd)
[ -n "${SMTP_USER:-}" ] && CURL_ARGS+=(--user "$SMTP_USER:${SMTP_PASS:-}")

if curl "${CURL_ARGS[@]}"; then
  echo "E-mail odeslán na: $SMTP_TO"
else
  echo "Odeslání e-mailu selhalo (curl skončil s chybou)." >&2
  exit 1
fi
