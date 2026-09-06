#!/bin/bash
# Open the stable menu app after any required exact-target re-sign.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

exec "$PROJECT_DIR/启动.command" --allow-wechat-resign --refresh-data-source "$@"
