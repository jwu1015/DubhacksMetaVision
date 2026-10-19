#!/bin/bash
# Deploy the segmentation server, the backend and the voice agent on the GPU box, in one tmux session,
# configured by deploy.yaml (copy deploy.example.yaml).
# Usage: ./deploy.sh [--restart] [segmentation|backend|voice|tunnel ...]
#   no names    every service (the voice agent only if enabled; the tunnel unless tunnel.mode is none)
#   --restart   restart the named services that are already running (e.g. after pulling new code)
# Each service runs in its own window of the session; attach with: tmux attach -t <session>  (detach: Ctrl-b d)
# Env: CONFIG=path/to/deploy.yaml (default: next to this script)

set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${CONFIG:-$DIR/deploy.yaml}"
RUN="$DIR/.deploy"  # each service's environment, written from deploy.yaml
TUNNEL_LOG="$DIR/logs/tunnel.log"

RESTART=0
WANT=()
for arg in "$@"; do
    case "$arg" in
        --restart) RESTART=1 ;;
        segmentation|backend|voice|tunnel) WANT+=("$arg") ;;
        *) echo "usage: $0 [--restart] [segmentation|backend|voice|tunnel ...]" >&2; exit 2 ;;
    esac
done
[ ${#WANT[@]} -gt 0 ] || WANT=(segmentation backend voice tunnel)
wants() { [[ " ${WANT[*]} " == *" $1 "* ]]; }

for tool in tmux uv curl; do
    command -v "$tool" > /dev/null || { echo "error: $tool is not installed" >&2; exit 1; }
done
[ -f "$CONFIG" ] || { echo "error: $CONFIG is missing (copy deploy.example.yaml)" >&2; exit 1; }

# Read deploy.yaml: sets SESSION, SEG_VENV, SEG_PORT, BACKEND_VENV, BACKEND_PORT, TUNNEL_MODE, PUBLIC_URL, VOICE
SETTINGS="$(uv run --quiet --no-project --with pyyaml python "$DIR/scripts/deploy_config.py" "$CONFIG" "$RUN")" || exit 1
eval "$SETTINGS"

if [ "$TUNNEL_MODE" != none ] && wants tunnel; then
    command -v cloudflared > /dev/null || { echo "error: cloudflared is not installed" >&2; exit 1; }
fi

sha() { { sha256sum "$1" 2>/dev/null || shasum -a 256 "$1"; } | cut -d' ' -f1; }

install_requirements() {  # install_requirements VENV REQUIREMENTS; only when the file changed since last time
    local stamp="$1/.requirements.sha" want
    want="$(sha "$2")"
    [ "$(cat "$stamp" 2>/dev/null)" = "$want" ] && return 0
    echo "Installing $2 into $1"
    [ -x "$1/bin/python" ] || uv venv "$1" || return 1
    VIRTUAL_ENV="$1" uv pip install -r "$2" || return 1
    echo "$want" > "$stamp"
}

running() { tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -qx "$1"; }

start() {  # start WINDOW COMMAND: run COMMAND in its own window, unless it's running and we aren't restarting
    if running "$1"; then
        if [ "$RESTART" = 0 ]; then
            echo "$1: already running (use --restart to relaunch)"
            return 1
        fi
        tmux kill-window -t "$SESSION:$1"
        sleep 1
    fi
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        tmux new-window -d -t "$SESSION" -n "$1"
    else
        tmux new-session -d -s "$SESSION" -n "$1"
    fi
    tmux send-keys -t "$SESSION:$1" "$2" Enter
    echo "$1: started"
}

wait_for() {  # wait_for NAME URL SECONDS
    for _ in $(seq "$3"); do
        curl -sf -m 2 "$2" > /dev/null && { echo "$1: healthy at $2"; return 0; }
        sleep 1
    done
    echo "warning: $1 not answering at $2 after $3s (tmux attach -t $SESSION to see why)" >&2
    return 1
}

mkdir -p "$DIR/logs"
with_env() { echo "set -a && . '$RUN/$1.env' && set +a"; }

# Segmentation server: loads SAM onto the GPU, so it takes a while to come up
if wants segmentation; then
    install_requirements "$SEG_VENV" "$DIR/segmentation/requirements.txt" || exit 1
    start segmentation "cd '$DIR/segmentation' && source '$SEG_VENV/bin/activate' && $(with_env segmentation) && python server.py"
fi

# Tunnel: named, or a quick tunnel whose random URL becomes the backend's PUBLIC_BASE_URL
if [ "$TUNNEL_MODE" = named ] && wants tunnel; then
    start tunnel "$(with_env tunnel) && cloudflared tunnel --no-autoupdate run"
elif [ "$TUNNEL_MODE" = quick ]; then
    if wants tunnel && { [ "$RESTART" = 1 ] || ! running tunnel; }; then
        : > "$TUNNEL_LOG"
        start tunnel "cloudflared tunnel --no-autoupdate --url http://127.0.0.1:$BACKEND_PORT 2>&1 | tee '$TUNNEL_LOG'"
        if ! wants backend && running backend; then
            echo "warning: the quick tunnel has a new URL; restart the backend too: $0 --restart backend" >&2
        fi
    fi
    if running tunnel; then  # the URL shows up in its log a few seconds after it starts
        for _ in $(seq 30); do
            PUBLIC_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1)"
            [ -n "$PUBLIC_URL" ] && break
            sleep 1
        done
    fi
    if [ -n "$PUBLIC_URL" ]; then
        echo "export PUBLIC_BASE_URL='$PUBLIC_URL'" >> "$RUN/backend.env"
    else
        echo "warning: no quick-tunnel URL (see $TUNNEL_LOG); PUBLIC_BASE_URL falls back to localhost" >&2
    fi
fi

# Backend and voice agent share one venv and one environment
if wants backend || { wants voice && [ "$VOICE" = 1 ]; }; then
    install_requirements "$BACKEND_VENV" "$DIR/requirements.txt" || exit 1
fi
BACKEND_SHELL="cd '$DIR' && source '$BACKEND_VENV/bin/activate' && $(with_env backend)"
if wants backend; then
    start backend "$BACKEND_SHELL && uvicorn storefront.app:app --host 127.0.0.1 --port $BACKEND_PORT --timeout-graceful-shutdown 1"
fi
if wants voice && [ "$VOICE" = 1 ]; then
    start voice "$BACKEND_SHELL && while true; do python -m storefront.voice.agent start; echo 'voice agent exited; restarting in 3s'; sleep 3; done"
elif wants voice && running voice; then
    tmux kill-window -t "$SESSION:voice" && echo "voice: stopped (disabled in deploy.yaml)"
fi

wants backend && wait_for backend "http://127.0.0.1:$BACKEND_PORT/health" 30
wants segmentation && wait_for segmentation "http://127.0.0.1:$SEG_PORT/health" 300

echo "Public URL: ${PUBLIC_URL:-none}   (attach: tmux attach -t $SESSION)"
