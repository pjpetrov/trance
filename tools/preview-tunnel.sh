#!/usr/bin/env bash
# Put the running preview behind an HTTPS tunnel with a password on it.
#
# The preview server picks a fresh port for every session and folder, so there
# is nothing fixed to point a tunnel at. This asks trance which port is serving
# right now and tunnels that one.
#
#   tools/preview-tunnel.sh                 # the only session with a preview
#   tools/preview-tunnel.sh s_4e3949375b    # a named session
#   tools/preview-tunnel.sh --google        # sign in with Google, no password
#   tools/preview-tunnel.sh --open          # no access control at all
#
# Requires an ngrok authtoken (one-off, from your ngrok account):
#   ngrok config add-authtoken <token>
set -euo pipefail

export TRANCE_URL="${TRANCE_URL:-http://localhost:8080}"
POLICY="${NGROK_POLICY:-$HOME/.config/ngrok/trance-preview.yml}"
SESSION=""
OPEN=""
for arg in "$@"; do
  case "$arg" in
    --google) POLICY="$HOME/.config/ngrok/trance-preview-google.yml" ;;
    --open)   OPEN=1 ;;
    -*)       echo "unknown option: $arg" >&2; exit 2 ;;
    *)        SESSION="$arg" ;;
  esac
done

if ! command -v ngrok >/dev/null; then
  echo "ngrok is not on PATH. It was installed to ~/.local/bin/ngrok." >&2
  exit 1
fi
if [ -z "$OPEN" ] && [ ! -f "$POLICY" ]; then
  echo "No traffic policy at $POLICY — that file is what keeps the tunnel closed" >&2
  echo "to everyone else. Refusing to publish your project folder without one." >&2
  echo "Pass --open if you really mean to publish it to anyone with the URL." >&2
  exit 1
fi

# Which session, and is anything actually being served?
if [ -z "$SESSION" ]; then
  SESSION=$(curl -fsS "$TRANCE_URL/api/sessions" | python3 -c '
import json, os, sys, urllib.request
base = os.environ["TRANCE_URL"]
live = []
for s in json.load(sys.stdin):
    url = base + "/api/sessions/" + s["id"] + "/preview"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            if (json.load(r).get("port") or 0) > 0:
                live.append((s["id"], s["name"]))
    except Exception:
        pass
if len(live) == 1:
    print(live[0][0])
elif not live:
    sys.exit("No preview is running. Open a page with the ▷ button in the Files tab first.")
else:
    sys.exit("More than one preview is running — name a session:\n" +
             "\n".join(f"  {i}  {n}" for i, n in live))')
fi

read -r PORT ROOT < <(curl -fsS "$TRANCE_URL/api/sessions/$SESSION/preview" |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("port") or 0, d.get("root",""))')

if [ "$PORT" = "0" ]; then
  echo "Session $SESSION has no preview running." >&2
  exit 1
fi

if [ -n "$OPEN" ]; then
  # Asked for explicitly. Said plainly, because the URL is the only thing
  # between this folder and anyone who finds it.
  echo "Publishing $ROOT (port $PORT) over HTTPS with NO access control."
  echo "Anyone with the URL can read every file in that folder."
  exec ngrok http "$PORT"
fi

echo "Publishing $ROOT (port $PORT) over HTTPS."
echo "Access is controlled by $POLICY."
exec ngrok http "$PORT" --traffic-policy-file "$POLICY"
