#!/bin/sh

set -eu

app_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
app_dir="${FOCUS_UPDATE_APP:-$app_dir}"
focus_home="$(dirname "$app_dir")"
desktop_dir="$app_dir/desktop"
runtime_dir="$focus_home/runtime"
venv_dir="$runtime_dir/venv"
venv_python="$venv_dir/bin/python"
metadata_path="$focus_home/install.json"
locks_dir="$focus_home/locks"
expected_repository="https://github.com/Leeminjing/Focus.git"
expected_branch="main"
held_lock=""
python_runtime=""

fail() {
    printf 'Focus: %s\n' "$*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "Required command '$1' was not found on PATH."
}

resolve_python_runtime() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            python_runtime="$(command -v "$candidate")"
            return
        fi
    done
    fail "Python 3.11 or newer was not found. Install Python and run 'focus update' again."
}

# electron 43 的安装器依赖 @electron/get 5（ESM-only），要求 Node >= 22.12.0；
# 更低版本无法执行安装器，会留下没有二进制的残缺 node_modules。
require_node_runtime() {
    require_command node
    node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 12) ? 0 : 1)' >/dev/null 2>&1 ||
        fail "Node.js 22.12.0 or newer is required (found $(node --version 2>/dev/null || printf 'unknown'))."
}

metadata_python() {
    if [ -x "$venv_python" ]; then
        printf '%s\n' "$venv_python"
        return
    fi
    resolve_python_runtime
    printf '%s\n' "$python_runtime"
}

assert_managed_install() {
    [ -f "$metadata_path" ] || fail "This is not a managed Focus installation: $metadata_path is missing. Reinstall Focus with install.sh."
    validator="$(metadata_python)"
    "$validator" - "$metadata_path" "$expected_repository" "$expected_branch" "$app_dir" <<'PY' ||
import json
import os
import sys

metadata_path, repository, branch, app_dir = sys.argv[1:]

def normalize_repository(value):
    value = str(value).strip().rstrip("/").lower()
    return value[:-4] if value.endswith(".git") else value

try:
    with open(metadata_path, encoding="utf-8-sig") as handle:
        metadata = json.load(handle)
    matches = (
        metadata.get("managed") is True
        and normalize_repository(metadata.get("repository", "")) == normalize_repository(repository)
        and (metadata.get("channel") in ("dev", "stable") or ("channel" not in metadata and metadata.get("branch") == branch))
        and os.path.realpath(metadata.get("appPath", "")) == os.path.realpath(app_dir)
    )
except (OSError, TypeError, ValueError):
    matches = False

raise SystemExit(0 if matches else 1)
PY
        fail "Focus refused to manage this directory because install.json does not match the expected app, repository, and branch."
    [ -d "$app_dir/.git" ] || fail "Focus managed app is not a Git checkout: $app_dir"
}

lock_is_active() {
    lock_path="$1"
    [ -d "$lock_path" ] || return 1
    owner=""
    if [ -f "$lock_path/pid" ]; then
        owner="$(sed -n '1p' "$lock_path/pid" 2>/dev/null || true)"
    fi
    case "$owner" in
        # Allow the owner time to write its PID; a killed shell can otherwise
        # leave this acquisition window behind permanently.
        ''|*[!0-9]*)
            command -v node >/dev/null 2>&1 || return 0
            if node -e 'process.exit(Date.now() - require("node:fs").statSync(process.argv[1]).mtimeMs < 30000 ? 0 : 1)' "$lock_path"; then
                return 0
            fi
            ;;
        *)
            if kill -0 "$owner" 2>/dev/null; then
                return 0
            fi
            ;;
    esac
    rm -rf -- "$lock_path"
    return 1
}

release_lock() {
    if [ -n "$held_lock" ] && [ -d "$held_lock" ]; then
        owner="$(sed -n '1p' "$held_lock/pid" 2>/dev/null || true)"
        if [ "$owner" = "$$" ]; then
            rm -rf -- "$held_lock"
        fi
    fi
    held_lock=""
}

acquire_lock() {
    lock_path="$locks_dir/$1"
    mkdir -p "$locks_dir"
    if lock_is_active "$lock_path"; then
        return 1
    fi
    if ! mkdir "$lock_path" 2>/dev/null; then
        return 1
    fi
    printf '%s\n' "$$" >"$lock_path/pid"
    held_lock="$lock_path"
    return 0
}

electron_runtime_ready() (
    electron_dir="$desktop_dir/node_modules/electron"
    electron_path_file="$electron_dir/path.txt"
    [ -f "$electron_dir/package.json" ] || return 1
    [ -f "$electron_path_file" ] || return 1
    electron_relative_path="$(cat "$electron_path_file")" || return 1
    [ -n "$electron_relative_path" ] || return 1
    [ -f "$electron_dir/dist/$electron_relative_path" ]
)

dependencies_ready() {
    [ -x "$venv_python" ] || return 1
    electron_runtime_ready || return 1
    # Full imports run during install/update and native first-run preparation.
    return 0
}


start_focus() {
    assert_managed_install
    acquire_lock "running.lock" || fail "Focus is already running."
    trap release_lock EXIT
    trap 'exit 130' HUP INT TERM
    [ ! -f "$focus_home/update-state.json" ] || fail "Focus recovery is incomplete. Run 'focus update' before starting Focus."
    require_node_runtime
    updater="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/managed-update.cjs"
    node "$updater" check "$focus_home" "$app_dir"
    dependencies_ready || fail "Focus dependencies are missing. Run 'focus update' first."
    require_command npm
    export FOCUS_GLOBAL_HOME="$focus_home"
    export FOCUS_MANAGED_APP="$app_dir"
    export FOCUS_PYTHON="$venv_python"
    cd "$desktop_dir"
    npm start
}

update_focus() {
    assert_managed_install
    if lock_is_active "$locks_dir/running.lock"; then
        fail "Focus is running. Close it before running 'focus update'."
    fi
    acquire_lock "running.lock" || fail "Focus is running or another update is already running."
    trap release_lock EXIT
    trap 'exit 130' HUP INT TERM

    require_node_runtime
    updater="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/managed-update.cjs"
    node "$updater" update "$focus_home" "$app_dir" "$venv_python" "$@"

}

doctor_focus() {
    if [ -x "$venv_python" ]; then
        doctor_python="$venv_python"
    elif [ -n "${FOCUS_PYTHON:-}" ]; then
        doctor_python="$FOCUS_PYTHON"
    else
        resolve_python_runtime
        doctor_python="$python_runtime"
    fi
    if [ -f "$metadata_path" ]; then
        export FOCUS_GLOBAL_HOME="$focus_home"
    fi
    export FOCUS_DOCTOR_CWD="$PWD"
    cd "$app_dir"
    exec "$doctor_python" -m backend.app.doctor "$@"
}

if [ "${1:-}" = "doctor" ]; then
    shift
    doctor_focus "$@"
fi

[ "$(uname -s)" = "Darwin" ] || fail "This command currently supports macOS only."

case "$#:$*" in
    0:) start_focus ;;
    1:update) update_focus ;;
    2:"update --dev"|2:"update --stable") shift; update_focus "$@" ;;
    *) fail "Usage: focus [update [--dev | --stable] | doctor [--json] [--output FILE] [--url URL]]" ;;
esac
