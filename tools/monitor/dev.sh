#!/bin/sh
# Development run of the monitor with the credits portal, on http://127.0.0.1:8199/ (GUFO_DEV_PORT).
# Everything it writes (keys, tokens, databases, log file, .env) lives in tools/monitor/.dev/,
# which is git-ignored; it never touches ~/.config/gufo*. Gufo itself need not run: the page
# shows it as down. Point --gufo at a real server with GUFO_DEV_URL=http://127.0.0.1:1234.
# Admin token, the only way into /monitor: tools/monitor/.dev/admin-token. Extra arguments
# go to gufo-monitor.py.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
DEV=$HERE/.dev
mkdir -p "$DEV"
[ -f "$DEV/gufo.log" ] || : > "$DEV/gufo.log"
if [ ! -f "$DEV/api_keys" ]; then
  # `owner` is required next to portal keys; its credential is not needed in development.
  printf 'owner sha256:%s\n' "$(printf 'dev-owner-key' | sha256sum | cut -d' ' -f1)" > "$DEV/api_keys"
fi
# The admin page saves prices to this file, so it is a copy of the example.
[ -f "$DEV/pricing.json" ] || cp "$HERE/pricing.example.json" "$DEV/pricing.json"
if [ ! -f "$DEV/billing.json" ]; then
  cat > "$DEV/billing.json" <<EOF
{
  "db": "$DEV/billing.sqlite",
  "pricing": "$DEV/pricing.json",
  "signup_grant_micro": 990000,
  "max_keys_per_user": 5
}
EOF
fi
PORT=${GUFO_DEV_PORT:-8199}
# Usage is metered from Gufo's log: GUFO_DEV_LOG, else the production log if present, else an empty file.
LOGFILE=${GUFO_DEV_LOG:-$HOME/.local/state/gufo/gufo-flash-next.log}
[ -f "$LOGFILE" ] || LOGFILE=$DEV/gufo.log
[ -f "$DEV/.env" ] || printf 'GUFO_PUBLIC_URL=http://127.0.0.1:%s\nGUFO_API_URL=%s/v1\n' "$PORT" "${GUFO_DEV_URL:-http://127.0.0.1:1234}" > "$DEV/.env"
# The monitor polls Gufo with a key: GUFO_DEV_KEY_FILE, else your monitor key if present.
KEY=${GUFO_DEV_KEY_FILE:-$HOME/.config/gufo/monitor_key}
[ -f "$KEY" ] && set -- --gufo-key-file "$KEY" "$@"
echo "portal  http://127.0.0.1:$PORT/   monitor  http://127.0.0.1:$PORT/monitor"
echo "admin token: $DEV/admin-token (created on first start)"
exec python3 "$HERE/gufo-monitor.py" \
  --gufo "${GUFO_DEV_URL:-http://127.0.0.1:1234}" --bind 127.0.0.1 --port "$PORT" \
  --admin-token-file "$DEV/admin-token" --env-file "$DEV/.env" \
  --keys-file "$DEV/api_keys" --log-source "file:$LOGFILE" --log-utc \
  --billing-config "$DEV/billing.json" --stats-file "$DEV/stats.sqlite" "$@"
