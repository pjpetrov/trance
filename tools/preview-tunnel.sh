#!/usr/bin/env bash
# Put the running preview behind an HTTPS tunnel with a password on it.
#
# The preview server picks a fresh port for every session and folder, so there
# is nothing fixed to point a tunnel at. This asks trance which port is serving
# right now and tunnels that one.
#
#   tools/preview-tunnel.sh                 # the only session with a preview
#   tools/preview-tunnel.sh s_4e3949375b    # a named session
#
# Requires an ngrok authtoken (one-off, from your ngrok account):
#   ngrok config add-authtoken <token>
set -euo pipefail

export TRANCE_URL="${TRANCE_URL:-http://localhost:8080}"
POLICY="${NGROK_POLICY:-$HOME/.config/ngrok/trance-preview.yml}"
SESSION="${1:-}"

if ! command -v ngrok >/dev/null; then
  echo "ngrok is not on PATH. It was installed to ~/.local/bin/ngrok." >&2
  exit 1
fi
if [ ! -f "$POLICY" ]; then
  echo "No traffic policy at $POLICY — that file is what puts a password on the" >&2
  echo "tunnel. Refusing to publish your project folder without one." >&2
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

echo "Publishing $ROOT (port $PORT) over HTTPS, password-protected."
echo "Credentials are in $POLICY."
exec ngrok http "$PORT" --traffic-policy-file "$POLICY"
