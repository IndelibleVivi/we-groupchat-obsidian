#!/bin/bash
# 双击此文件即可启动微信总结
# 默认分发方式：保留整个源码目录，首次安装会先征得同意再创建 .venv

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
source "$PROJECT_DIR/scripts/startup_helpers.sh"

VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
REQ_FILE="$PROJECT_DIR/requirements.txt"
REQ_STAMP="$VENV_DIR/.requirements.sha256"
USER_DATA_DIR="$HOME/.we-groupchat-obsidian"
PYTHON3_CMD="python3"
SETUP_ONLY=0
CONFIGURE_MONITOR=0
BACKFILL_HISTORY=0
ORGANIZE_OBSIDIAN=0
HEALTH_CHECK=0
REFRESH_DATA_SOURCE=0
WECHAT_APP_PATH_ARG="${WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH-}"
WECHAT_EXPLICIT_TARGET=0
if [[ "${WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH+x}" == "x" ]]; then
    WECHAT_EXPLICIT_TARGET=1
fi
INSTALL_AUTOSTART=0
UNINSTALL_AUTOSTART=0
AUTOSTART_MODE=0
ALLOW_WECHAT_RESIGN="${WE_GROUPCHAT_OBSIDIAN_ALLOW_RESIGN:-${WECHAT_SUMMARY_ALLOW_RESIGN:-0}}"
export WE_GROUPCHAT_OBSIDIAN_DATA_DIR="$USER_DATA_DIR"

for arg in "$@"; do
    case "$arg" in
        --setup-only)
            SETUP_ONLY=1
            ;;
        --configure-monitor)
            CONFIGURE_MONITOR=1
            ;;
        --backfill-history)
            BACKFILL_HISTORY=1
            ;;
        --organize-obsidian)
            ORGANIZE_OBSIDIAN=1
            ;;
        --health-check)
            HEALTH_CHECK=1
            ;;
        --refresh-data-source)
            REFRESH_DATA_SOURCE=1
            ;;
        --install-autostart)
            INSTALL_AUTOSTART=1
            ;;
        --uninstall-autostart)
            UNINSTALL_AUTOSTART=1
            ;;
        --autostart)
            AUTOSTART_MODE=1
            ;;
        --allow-wechat-resign)
            ALLOW_WECHAT_RESIGN=1
            ;;
        --wechat-app=*)
            WECHAT_APP_PATH_ARG="${arg#--wechat-app=}"
            WECHAT_EXPLICIT_TARGET=1
            ;;
    esac
done

# Carry the same binding into health/refresh entrypoints too. An explicit empty
# value is retained so the shared resolver rejects it instead of rediscovering.
if [[ "$WECHAT_EXPLICIT_TARGET" -eq 1 ]]; then
    export WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH="$WECHAT_APP_PATH_ARG"
fi

pause_and_exit() {
    local exit_code="$1"
    if [[ "${WE_GROUPCHAT_OBSIDIAN_NO_PAUSE:-${WECHAT_SUMMARY_NO_PAUSE:-0}}" == "1" || "$AUTOSTART_MODE" -eq 1 ]]; then
        exit "$exit_code"
    fi
    read -r -p "按回车键关闭..." || true
    exit "$exit_code"
}

get_wechat_app_path() {
    "$PYTHON_BIN" -c '
from core.key_extractor import get_wechat_app_path
import sys
path = get_wechat_app_path()
if not path:
    sys.exit(1)
print(path)
'
}

ensure_xcode_cli() {
    if xcode-select -p &>/dev/null; then
        return 0
    fi

    echo "需要安装 Xcode Command Line Tools，请在弹出窗口中点击「安装」"
    xcode-select --install
    echo ""
    echo "安装完成后，请再次双击「启动.command」"
    pause_and_exit 0
}

ensure_python() {
    local min_major=3
    local min_minor=10

    if [[ -x "$PYTHON_BIN" ]]; then
        local venv_version=""
        venv_version="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)" || venv_version=""
        local venv_major="${venv_version%%.*}"
        local venv_minor="${venv_version##*.}"
        if version_at_least "$venv_major" "$venv_minor" "$min_major" "$min_minor"; then
            PYTHON3_CMD="$PYTHON_BIN"
            return 0
        fi
    fi

    # 尝试找到可用的 python3 路径（优先用满足版本要求的）
    _find_suitable_python() {
        # 候选路径：pyenv / Homebrew / 官方安装包 / 系统默认
        local candidates=(
            "$HOME/.pyenv/shims/python3"
            "/opt/homebrew/bin/python3.12"
            "/opt/homebrew/bin/python3.11"
            "/opt/homebrew/bin/python3.10"
            "/usr/local/bin/python3.12"
            "/usr/local/bin/python3.11"
            "/usr/local/bin/python3.10"
            "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
            "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
            "/Library/Frameworks/Python.framework/Versions/3.10/bin/python3"
            "python3"
        )
        for candidate in "${candidates[@]}"; do
            local ver=""
            ver="$($candidate -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)" || continue
            local maj="${ver%%.*}"
            local min="${ver##*.}"
            if version_at_least "$maj" "$min" "$min_major" "$min_minor"; then
                echo "$candidate"
                return 0
            fi
        done
        return 1
    }

    if ! command -v python3 &>/dev/null; then
        echo "❌ 未找到 Python3，请先安装："
        echo ""
        echo "   方法一（推荐）：去 https://www.python.org/downloads/ 下载最新版本"
        echo "   方法二：如果已装 Homebrew，运行: brew install python@3.12"
        echo ""
        echo "   安装完成后重新双击「启动.command」"
        pause_and_exit 1
    fi

    # 先看当前 python3 是否满足要求
    local py_version=""
    py_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"
    local py_major="${py_version%%.*}"
    local py_minor="${py_version##*.}"

    if ! version_at_least "$py_major" "$py_minor" "$min_major" "$min_minor"; then
        echo "⚠️  默认 Python 版本较低（${py_version}），正在查找更高版本..."

        # 先在已有路径中查找合适的版本
        local suitable=""
        suitable="$(_find_suitable_python)" || true

        # 没找到且有 Homebrew，得到明确同意后再安装
        if [[ -z "$suitable" ]] && command -v brew &>/dev/null; then
            if confirm_homebrew_python_install; then
                echo "  正在安装 Python 3.12..."
                brew install python@3.12 2>&1 | tail -3
                suitable="$(_find_suitable_python)" || true
            else
                echo "  已跳过 Homebrew 安装。"
            fi
        fi

        if [[ -n "$suitable" ]]; then
            local found_ver=""
            found_ver="$($suitable -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"
            echo "  ✓ 找到 Python ${found_ver}：${suitable}"
            # 创建别名让后续脚本用这个版本
            python3() { "$suitable" "$@"; }
            export -f python3 2>/dev/null || true
            PYTHON3_CMD="$suitable"
            return 0
        fi

        echo ""
        echo "❌ 未找到 Python ${min_major}.${min_minor} 以上版本"
        echo ""
        echo "   方法一（推荐）：去 https://www.python.org/downloads/ 下载最新版本"
        echo "   方法二：运行 brew install python@3.12（需先安装 Homebrew）"
        echo ""
        echo "   安装完成后重新双击「启动.command」"
        pause_and_exit 1
    fi
    PYTHON3_CMD="python3"
}

ensure_venv() {
    mkdir -p "$USER_DATA_DIR"

    local current_hash=""
    local installed_hash=""
    local needs_install=0
    current_hash="$(shasum -a 256 "$REQ_FILE" | awk '{print $1}')"
    if [[ -f "$REQ_STAMP" ]]; then
        installed_hash="$(cat "$REQ_STAMP")"
    fi

    if [[ ! -x "$PYTHON_BIN" || "$current_hash" != "$installed_hash" ]]; then
        needs_install=1
    fi

    if [[ "$needs_install" -eq 1 ]]; then
        echo "[1/3] 需要创建或更新项目 .venv，并安装 requirements.txt 中的 dependencies。"
        if ! confirm_dependency_install; then
            echo "已取消安装，程序不会启动。"
            pause_and_exit 1
        fi
    fi

    if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "[1/3] 创建项目隔离环境..."
        "$PYTHON3_CMD" -m venv "$VENV_DIR"
    fi

    if [[ "$current_hash" != "$installed_hash" ]]; then
        echo "[1/3] 安装 Python dependencies（首次需要几分钟）..."
        "$PYTHON_BIN" -m pip install --upgrade pip
        "$PYTHON_BIN" -m pip install -r "$REQ_FILE"
        printf '%s\n' "$current_hash" > "$REQ_STAMP"
    else
        echo "[1/3] Python dependencies 已就绪"
    fi
}

is_wechat_signed() {
    "$PYTHON_BIN" -c '
from core.wechat_signature import inspect_wechat_signature
import sys
sys.exit(0 if inspect_wechat_signature(sys.argv[1]).ok else 1)
' "$1"
}

ensure_wechat_signed() {
    local app_path=""
    if ! app_path="$(get_wechat_app_path)"; then
        echo "[2/3] ❌ 未找到 WeChat.app，请先安装并登录微信"
        pause_and_exit 1
    fi

    export WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH="$app_path"

    if is_wechat_signed "$app_path"; then
        echo "[2/3] 微信授权状态正常"
        return 0
    fi

    echo "[2/3] 检测到微信需要重新授权..."
    echo "  为了读取本地微信数据库，本项目需要对 WeChat.app 做 ad-hoc re-sign。"
    echo "  这是高影响操作：会修改下列 exact target 的签名，微信更新后可能失效。"
    echo "  Target: $app_path"
    echo ""
    if [[ "$ALLOW_WECHAT_RESIGN" != "1" ]]; then
        echo "  privacy-first 版本不会在双击启动时自动执行这一步。"
        echo "  确认要继续时，请在项目目录手动运行："
        echo ""
        echo "    ./启动.command --allow-wechat-resign"
        echo "  多个 WeChat.app 并存时，请显式绑定："
        echo "    ./启动.command --allow-wechat-resign --wechat-app=/exact/path/WeChat.app"
        echo ""
        echo "  或仅做环境检查："
        echo ""
        echo "    ./启动.command --setup-only"
        pause_and_exit 1
    fi

    echo "  即将对上述 target 执行重签名，需要输入电脑登录密码；输入时终端不会显示字符，这是正常的"
    local resign_args=(
        "$PROJECT_DIR/scripts/resign_wechat.py"
        --app "$app_path"
        --allow-wechat-resign
    )
    if [[ "$WECHAT_EXPLICIT_TARGET" -eq 1 ]]; then
        resign_args+=(--explicit-target)
    fi

    if "$PYTHON_BIN" "${resign_args[@]}"; then
        echo "  ✓ 微信 exact target 已重签名、验签并重新打开"
        return 0
    fi

    echo ""
    echo "============================================"
    echo "  ❌ 微信重新授权失败"
    echo "============================================"
    echo ""
    echo "Target 没有被当作重签成功，也不会自动扩大权限。"
    echo "如果错误是 macOS 拒绝修改 app，请在「系统设置 → 隐私与安全性 → App 管理」"
    echo "检查当前终端的权限后重试；不要为这一步默认开启「完全磁盘访问权限」。"
    pause_and_exit 1
}

run_setup() {
    if [[ "$(uname)" != "Darwin" ]]; then
        echo "❌ 此工具仅支持 macOS"
        pause_and_exit 1
    fi

    echo "============================================"
    if [[ "$SETUP_ONLY" -eq 1 ]]; then
        echo "  微信群聊 AI 总结 - 环境检查"
    else
        echo "  微信群聊 AI 总结"
    fi
    echo "============================================"
    echo ""

    ensure_xcode_cli
    ensure_python
    ensure_venv
    ensure_wechat_signed

    echo "[3/3] 环境已就绪"
    echo ""
}

if [[ "$(uname)" != "Darwin" ]]; then
    echo "❌ 此工具仅支持 macOS"
    pause_and_exit 1
fi

if [[ "$HEALTH_CHECK" -eq 1 || "$INSTALL_AUTOSTART" -eq 1 || "$UNINSTALL_AUTOSTART" -eq 1 ]]; then
    ensure_xcode_cli
    ensure_python
    ensure_venv
    if [[ "$HEALTH_CHECK" -eq 1 ]]; then
        exec "$PYTHON_BIN" "$PROJECT_DIR/scripts/health_check.py"
    fi
    if [[ "$INSTALL_AUTOSTART" -eq 1 ]]; then
        exec "$PYTHON_BIN" "$PROJECT_DIR/scripts/autostart.py" install
    fi
    if [[ "$UNINSTALL_AUTOSTART" -eq 1 ]]; then
        exec "$PYTHON_BIN" "$PROJECT_DIR/scripts/autostart.py" uninstall
    fi
fi

run_setup

if [[ "$SETUP_ONLY" -eq 1 ]]; then
    echo "配置完成。后续直接双击「启动.command」即可。"
    pause_and_exit 0
fi

if [[ "$CONFIGURE_MONITOR" -eq 1 ]]; then
    exec "$PYTHON_BIN" "$PROJECT_DIR/scripts/configure_monitor.py"
fi

if [[ "$BACKFILL_HISTORY" -eq 1 ]]; then
    exec "$PYTHON_BIN" "$PROJECT_DIR/scripts/backfill_history.py"
fi

if [[ "$ORGANIZE_OBSIDIAN" -eq 1 ]]; then
    exec "$PYTHON_BIN" "$PROJECT_DIR/scripts/organize_obsidian.py"
fi

if [[ "$REFRESH_DATA_SOURCE" -eq 1 ]]; then
    exec "$PYTHON_BIN" "$PROJECT_DIR/scripts/refresh_data_source.py"
fi

echo "正在启动微信总结..."
echo "菜单栏会出现 💬 图标"
echo "（关闭此窗口会退出程序，Ctrl+C 也可退出）"
echo ""
exec "$PYTHON_BIN" "$PROJECT_DIR/app.py"
