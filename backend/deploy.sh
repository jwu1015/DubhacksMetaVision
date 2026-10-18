#!/bin/bash
# Run the backend (storefront.app) on the GPU box next to the segmentation server, behind its own Cloudflare tunnel.
# The backend reaches the segmentation server over localhost, so it skips its tunnel and Cloudflare Access.
# Usage: ./deploy.sh [--restart]
#   --restart  kill the existing session first (e.g. after pulling new code)
# Attach with: tmux attach -t backend   (windows: tunnel unless shared, server, voice; detach: Ctrl-b d)
#
# Settings come from backend/.env as usual (ANTHROPIC_API_KEY, SERPAPI_KEY, ...). This script overrides
# MODEL_SERVICE_URL and clears CF_ACCESS_CLIENT_ID/SECRET. Tunnel:
#   BACKEND_CF_TUNNEL_TOKEN set (backend/.env) -> that named tunnel; set PUBLIC_BASE_URL to its hostname.
#     Point its ingress at http://127.0.0.1:$BACKEND_PORT.
#   BACKEND_TUNNEL=inference (backend/.env) -> no tunnel of its own: the segmentation server's tunnel
#     (CF_TUNNEL_TOKEN, run_tmux.sh) also routes to http://localhost:$BACKEND_PORT. Add that route in the
#     Cloudflare dashboard and keep Cloudflare Access off it; set PUBLIC_BASE_URL to its public URL.
#   otherwise -> a quick tunnel (https://<random>.trycloudflare.com, new on every start), used as PUBLIC_BASE_URL.
# Voice: with GOOGLE_API_KEY and LIVEKIT_URL in backend/.env, the voice agent's worker (storefront.voice.agent)
#   runs too, in the voice window.

SESSION="${SESSION:-backend}"
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$DIR/.env"
SEG_ENV_FILE="${WORKSPACE:-/workspace}/.env"  # the segmentation server's settings (run_tmux.sh)
VENV="${VENV:-/venv/backend}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
TUNNEL_LOG="$DIR/logs/tunnel.log"

env_value() {  # env_value FILE NAME -> NAME's value in FILE, quotes stripped; empty if unset
    [ -f "$1" ] && grep -E "^$2=" "$1" | tail -1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

SEG_PORT="$(env_value "$SEG_ENV_FILE" PORT)"
SEG_URL="http://127.0.0.1:${SEG_PORT:-10100}"

if [ "$1" = "--restart" ] && tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux kill-session -t "$SESSION"
    sleep 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session '$SESSION' already exists (use --restart to relaunch)"
    exit 0
fi

# Own venv: the backend pins av/aiortc, which shouldn't touch the torch stack in /venv/main.
# Reinstalls only when requirements.txt changes.
STAMP="$VENV/.requirements.sha"
REQ_SHA="$(sha256sum "$DIR/requirements.txt" | cut -d' ' -f1)"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$REQ_SHA" ]; then
    [ -x "$VENV/bin/python" ] || uv venv "$VENV" || exit 1
    VIRTUAL_ENV="$VENV" uv pip install -r "$DIR/requirements.txt" || exit 1
    echo "$REQ_SHA" > "$STAMP"
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "warning: $ENV_FILE is missing (copy .env.example); running on defaults" >&2
fi
EXTRA_ENV=""
if [ -z "$(env_value "$ENV_FILE" ANTHROPIC_API_KEY)" ] && grep -q '^ANTHROPIC_API_KEY=' "$SEG_ENV_FILE" 2>/dev/null; then
    # Fall back to the segmentation server's key; read from the file at launch so it never shows up in `ps`
    EXTRA_ENV="export ANTHROPIC_API_KEY=\"\$(. '$SEG_ENV_FILE' && echo \"\$ANTHROPIC_API_KEY\")\" && "
    echo "ANTHROPIC_API_KEY: none in backend/.env, using the one in $SEG_ENV_FILE"
fi

curl -sf -m 3 "$SEG_URL/health" > /dev/null \
    || echo "warning: segmentation server not answering at $SEG_URL/health (start it with segmentation/run_tmux.sh)" >&2

mkdir -p "$DIR/logs"
if [ "$(env_value "$ENV_FILE" BACKEND_TUNNEL)" = "inference" ]; then
    PUBLIC_URL="$(env_value "$ENV_FILE" PUBLIC_BASE_URL)"
    [ -n "$PUBLIC_URL" ] || echo "warning: BACKEND_TUNNEL=inference needs PUBLIC_BASE_URL in backend/.env" >&2
    pgrep -f "cloudflared tunnel --no-autoupdate run" > /dev/null \
        || echo "warning: the segmentation server's tunnel isn't running (segmentation/run_tmux.sh starts it)" >&2
    tmux new-session -d -s "$SESSION" -n server
    echo "Using the segmentation server's tunnel (route -> http://localhost:$BACKEND_PORT)"
elif [ -n "$(env_value "$ENV_FILE" BACKEND_CF_TUNNEL_TOKEN)" ]; then
    tmux new-session -d -s "$SESSION" -n tunnel
    # cloudflared reads TUNNEL_TOKEN from the environment, so the token never shows up in `ps`
    tmux send-keys -t "$SESSION:tunnel" "set -a && . '$ENV_FILE' && set +a && TUNNEL_TOKEN=\"\$BACKEND_CF_TUNNEL_TOKEN\" cloudflared tunnel --no-autoupdate run" Enter
    PUBLIC_URL="$(env_value "$ENV_FILE" PUBLIC_BASE_URL)"
    echo "Started named tunnel (ingress -> http://127.0.0.1:$BACKEND_PORT)"
else
    tmux new-session -d -s "$SESSION" -n tunnel
    : > "$TUNNEL_LOG"
    tmux send-keys -t "$SESSION:tunnel" "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:$BACKEND_PORT 2>&1 | tee '$TUNNEL_LOG'" Enter
    for _ in $(seq 30); do
        PUBLIC_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1)"
        [ -n "$PUBLIC_URL" ] && break
        sleep 1
    done
    [ -n "$PUBLIC_URL" ] || echo "warning: no quick-tunnel URL after 30s; see $TUNNEL_LOG" >&2
fi

# Real environment variables win over backend/.env (storefront/config.py), so these overrides stick
OVERRIDES="MODEL_SERVICE_URL='$SEG_URL' CF_ACCESS_CLIENT_ID= CF_ACCESS_CLIENT_SECRET="
[ -n "$PUBLIC_URL" ] && OVERRIDES="$OVERRIDES PUBLIC_BASE_URL='$PUBLIC_URL'"
tmux list-windows -t "$SESSION" -F '#W' | grep -qx server || tmux new-window -t "$SESSION" -n server
tmux send-keys -t "$SESSION:server" "cd '$DIR' && source '$VENV/bin/activate' && ${EXTRA_ENV}$OVERRIDES uvicorn storefront.app:app --host 127.0.0.1 --port $BACKEND_PORT --timeout-graceful-shutdown 1" Enter

if [ -n "$(env_value "$ENV_FILE" GOOGLE_API_KEY)" ] && [ -n "$(env_value "$ENV_FILE" LIVEKIT_URL)" ]; then
    tmux new-window -t "$SESSION" -n voice
    tmux send-keys -t "$SESSION:voice" "cd '$DIR' && source '$VENV/bin/activate' && python -m storefront.voice.agent start" Enter
    echo "Started the voice agent (window '$SESSION:voice')"
fi

echo "Started backend on 127.0.0.1:$BACKEND_PORT -> segmentation at $SEG_URL (attach: tmux attach -t $SESSION)"
echo "Public URL: ${PUBLIC_URL:-unknown}"
