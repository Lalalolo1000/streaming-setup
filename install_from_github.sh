#!/usr/bin/env bash
# Recommended one-time deployment path once the project has been pushed to GitHub.
set -euo pipefail
REPO_URL="${1:-}"
TARGET="${2:-$HOME/streaming-setup}"
BRANCH="${3:-main}"
if [ -z "$REPO_URL" ]; then
    echo "Usage: $0 https://github.com/OWNER/REPO.git [target-dir] [branch]" >&2
    echo "Private repo: an authenticated HTTPS setup or a GitHub deploy key must already work." >&2
    exit 2
fi
if [ -e "$TARGET" ]; then
    echo "Target already exists: $TARGET" >&2
    exit 1
fi
command -v git >/dev/null 2>&1 || { sudo apt-get update; sudo apt-get install -y git; }
GIT_TERMINAL_PROMPT=1 git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$TARGET"
cd "$TARGET"
./install_master.sh
STREAM_MASTER_GIT_BRANCH="$BRANCH" ./install_service.sh
cat <<MSG

Installation abgeschlossen.
Code: $TARGET
GitHub: $REPO_URL ($BRANCH)
Der Master prüft beim Boot und danach regelmäßig auf Updates.
MSG
