#!/bin/sh

set -eu

app_dir="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
focus_home="$(dirname "$app_dir")"
desktop_dir="$app_dir/desktop"
harness_dir="$app_dir/backend/packages/harness"
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
        and metadata.get("branch") == branch
        and os.path.realpath(metadata.get("appPath", "")) == os.path.realpath(app_dir)
    )
except (OSError, TypeError, ValueError):
    matches = False

raise SystemExit(0 if matches else 1)
PY
        fail "Focus refused to manage this directory because install.json does not match the expected app, repository, and branch."
    [ -d "$app_dir/.git" ] || fail "Focus managed app is not a Git checkout: $app_dir"
}

normalize_repository() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's:/*$::' -e 's:\.git$::'
}

lock_is_active() {
    lock_path="$1"
    [ -d "$lock_path" ] || return 1
    owner=""
    if [ -f "$lock_path/pid" ]; then
        owner="$(sed -n '1p' "$lock_path/pid" 2>/dev/null || true)"
    fi
    case "$owner" in
        ''|*[!0-9]*) ;;
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

ensure_python_environment() {
    [ -x "$venv_python" ] && return
    mkdir -p "$runtime_dir"
    resolve_python_runtime
    printf 'Creating the managed Python environment...\n'
    "$python_runtime" -m venv "$venv_dir"
}

dependencies_ready() {
    [ -x "$venv_python" ] || return 1
    [ -f "$desktop_dir/node_modules/electron/package.json" ] || return 1
    "$venv_python" -c 'import alembic, asyncpg, fastapi, psycopg, uvicorn' >/dev/null 2>&1
}

sync_focus_dependencies() {
    require_command npm
    ensure_python_environment || return $?
    printf 'Updating desktop dependencies...\n'
    npm --prefix "$desktop_dir" ci || return $?
    printf 'Updating Python dependencies...\n'
    "$venv_python" -m pip install --disable-pip-version-check -e "${harness_dir}[desktop]" || return $?
    "$venv_python" -c 'import alembic, asyncpg, fastapi, psycopg, uvicorn' || return $?
}

start_focus() {
    assert_managed_install
    dependencies_ready || fail "Focus dependencies are missing. Run 'focus update' first."
    require_command npm
    acquire_lock "running.lock" || fail "Focus is already running."
    trap release_lock EXIT
    trap 'exit 130' HUP INT TERM
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
    acquire_lock "update.lock" || fail "Another Focus update is already running."
    trap release_lock EXIT
    trap 'exit 130' HUP INT TERM

    require_command git
    origin="$(git -C "$app_dir" remote get-url origin)"
    [ "$(normalize_repository "$origin")" = "$(normalize_repository "$expected_repository")" ] ||
        fail "Focus refused to update because origin is not $expected_repository."

    previous_commit="$(git -C "$app_dir" rev-parse HEAD)"
    printf 'Fetching Focus...\n'
    git -C "$app_dir" fetch --prune origin "$expected_branch"
    target_commit="$(git -C "$app_dir" rev-parse "origin/$expected_branch")"
    printf 'Updating %.7s -> %.7s...\n' "$previous_commit" "$target_commit"
    git -C "$app_dir" reset --hard "origin/$expected_branch"

    if ! sync_focus_dependencies; then
        if [ "$previous_commit" != "$target_commit" ]; then
            printf 'Dependency update failed. Restoring %.7s...\n' "$previous_commit" >&2
            git -C "$app_dir" reset --hard "$previous_commit"
            if ! sync_focus_dependencies; then
                printf 'Warning: the previous dependency set could not be fully restored.\n' >&2
            fi
        fi
        fail "Focus update failed while synchronizing dependencies."
    fi

    if [ "$previous_commit" = "$target_commit" ]; then
        printf 'Focus is up to date (%.7s).\n' "$target_commit"
    else
        printf 'Focus updated successfully: %.7s -> %.7s.\n' "$previous_commit" "$target_commit"
    fi
}

[ "$(uname -s)" = "Darwin" ] || fail "This command currently supports macOS only."

case "$#:$*" in
    0:) start_focus ;;
    1:update) update_focus ;;
    *) fail "Usage: focus [update]" ;;
esac
