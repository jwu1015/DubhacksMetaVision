#!/bin/bash
# Launch the segmentation server in a detached tmux session, unless it's already running.
# Usage: ./run_tmux.sh [--restart]
#   --restart  kill the existing session first (e.g. after pulling new code)
# Attach with: tmux attach -t segmentation   (detach: Ctrl-b d)

SESSION="${SESSION:-segmentation}"
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${WORKSPACE:-/workspace}/.env"
VENV="${VENV:-/venv/main}"

if [ "$1" = "--restart" ] && tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux kill-session -t "$SESSION"
    sleep 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "tmux session '$SESSION' already exists (use --restart to relaunch)"
    exit 0
fi

tmux new-session -d -s "$SESSION"
tmux send-keys -t "$SESSION" "cd '$DIR' && source '$VENV/bin/activate' && set -a && { [ -f '$ENV_FILE' ] && . '$ENV_FILE'; } ; set +a && python server.py" Enter
echo "Started server in tmux session '$SESSION' (attach: tmux attach -t $SESSION)"

# If a Cloudflare tunnel token is set in .env, run the named tunnel in a second window.
# cloudflared reads TUNNEL_TOKEN from the environment, so the token never shows up in `ps`.
if [ -f "$ENV_FILE" ] && grep -q '^CF_TUNNEL_TOKEN=' "$ENV_FILE"; then
    tmux new-window -t "$SESSION" -n tunnel
    tmux send-keys -t "$SESSION:tunnel" "set -a && . '$ENV_FILE' && set +a && TUNNEL_TOKEN=\"\$CF_TUNNEL_TOKEN\" cloudflared tunnel --no-autoupdate run" Enter
    echo "Started Cloudflare tunnel in window '$SESSION:tunnel'"
fi
