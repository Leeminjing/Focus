#!/bin/sh

set -eu

repository="https://github.com/Leeminjing/Focus.git"
branch="main"
focus_home="${HOME}/.focus"
app_dir="${focus_home}/app"
bin_dir="${focus_home}/bin"
metadata_path="${focus_home}/install.json"
staging_root="${focus_home}/staging"
staging_app="${staging_root}/app-$$"
python_runtime=""

fail() {
    printf 'Focus installation failed: %s\n' "$*" >&2
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
    fail "Python 3.11 or newer was not found."
}

cleanup_staging() {
    case "$staging_app" in
        "$staging_root"/app-[0-9]*)
            if [ -e "$staging_app" ]; then
                rm -rf -- "$staging_app"
            fi
            ;;
    esac
}

managed_install_matches() {
    [ -f "$metadata_path" ] || return 1
    "$python_runtime" - "$metadata_path" "$repository" "$branch" "$app_dir" <<'PY'
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
}

write_metadata() {
    "$python_runtime" - "$metadata_path" "$repository" "$branch" "$app_dir" <<'PY'
import datetime
import json
import sys

metadata_path, repository, branch, app_dir = sys.argv[1:]
metadata = {
    "schema": 1,
    "managed": True,
    "repository": repository,
    "branch": branch,
    "appPath": app_dir,
    "installedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
with open(metadata_path, "w", encoding="utf-8") as handle:
    json.dump(metadata, handle, indent=2)
    handle.write("\n")
PY
}

ensure_profile_path() {
    profile="$1"
    path_line='export PATH="$HOME/.focus/bin:$PATH"'
    if [ ! -f "$profile" ] || ! grep -Fqx "$path_line" "$profile"; then
        printf '\n%s\n' "$path_line" >>"$profile"
    fi
}

configure_shell_path() {
    shell_name="$(basename "${SHELL:-}")"
    case "$shell_name" in
        zsh)
            ensure_profile_path "$HOME/.zprofile"
            ensure_profile_path "$HOME/.zshrc"
            ;;
        bash)
            ensure_profile_path "$HOME/.bash_profile"
            ensure_profile_path "$HOME/.bashrc"
            ;;
        *)
            ensure_profile_path "$HOME/.profile"
            ;;
    esac
}

[ "$(uname -s)" = "Darwin" ] || fail "This installer currently supports macOS only."
require_command git
require_command node
require_command npm
require_command docker
docker compose version >/dev/null 2>&1 || fail "Required command 'docker compose' is not available."
resolve_python_runtime
"$python_runtime" -m venv --help >/dev/null 2>&1 || fail "Python's venv module is not available."

mkdir -p "$focus_home" "$bin_dir" "$focus_home/plugins" "$focus_home/users"

if [ -e "$app_dir" ]; then
    managed_install_matches || fail "$app_dir already exists but is not a Focus managed installation. Move it elsewhere, then run the installer again."
    printf 'Existing Focus installation found. Updating it...\n'
else
    printf 'Cloning Focus into %s...\n' "$app_dir"
    mkdir -p "$staging_root"
    trap cleanup_staging EXIT
    trap 'exit 130' HUP INT TERM
    git clone --branch "$branch" --single-branch "$repository" "$staging_app"
    mv "$staging_app" "$app_dir"
    write_metadata
fi

[ -f "$focus_home/config.yaml" ] || printf '# Focus global configuration fallback layer.\n' >"$focus_home/config.yaml"
[ -f "$focus_home/extensions_config.json" ] || printf '{"mcpServers": {}}\n' >"$focus_home/extensions_config.json"
[ -f "$focus_home/.env" ] || cp "$app_dir/.env.example" "$focus_home/.env"

cli_script="$app_dir/scripts/focus.sh"
[ -f "$cli_script" ] || fail "The installed repository does not contain scripts/focus.sh."
sh "$cli_script" update

cat >"$bin_dir/focus" <<'SH'
#!/bin/sh
exec sh "$HOME/.focus/app/scripts/focus.sh" "$@"
SH
chmod 755 "$bin_dir/focus"
configure_shell_path
export PATH="$bin_dir:$PATH"

printf '\nFocus installed successfully.\n\n'
printf 'Before the first run, add your API key to:\n    %s\n\n' "$focus_home/.env"
printf 'Open a new terminal, then run:\n    focus\n\n'
printf 'Update later with:\n    focus update\n'
