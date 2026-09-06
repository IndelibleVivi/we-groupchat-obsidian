#!/bin/bash
# Install the macOS LaunchAgent for we-groupchat-obsidian autostart.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

exec "$PROJECT_DIR/启动.command" --install-autostart "$@"
