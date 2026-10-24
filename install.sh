#!/bin/sh

set -eu

repository="https://github.com/Leeminjing/Focus.git"
focus_home="${HOME}/.focus"
app_dir="${focus_home}/app"
bin_dir="${focus_home}/bin"
metadata_path="${focus_home}/install.json"
staging_root="${focus_home}/staging"
staging_app="${staging_root}/app-$$"
python_runtime=""
channel="${FOCUS_CHANNEL:-}"
case "$#:${1:-}" in
    0:) ;;
    1:--dev) channel=dev ;;
    1:--stable) channel=stable ;;
    *) printf 'Usage: install.sh [--dev | --stable]\n' >&2; exit 1 ;;
esac
controller_app="$staging_root/controller-$$"

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

# electron 43 的安装器依赖 @electron/get 5（ESM-only），要求 Node >= 22.12.0；
# 更低版本无法执行安装器，会留下没有二进制的残缺 node_modules。
require_node_runtime() {
    require_command node
    node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 12) ? 0 : 1)' >/dev/null 2>&1 ||
        fail "Node.js 22.12.0 or newer is required (found $(node --version 2>/dev/null || printf 'unknown'))."
}

cleanup_staging() {
    case "$staging_app" in
        "$staging_root"/app-[0-9]*)
            if [ -e "$staging_app" ]; then
                rm -rf -- "$staging_app"
            fi
            ;;
    esac
    case "$controller_app" in
        "$staging_root"/controller-[0-9]*) rm -rf -- "$controller_app" ;;
    esac
}

managed_install_matches() {
    [ -f "$metadata_path" ] || return 1
    "$python_runtime" - "$metadata_path" "$repository" "$release_tag" "$app_dir" <<'PY'
import json
import os
import sys

metadata_path, repository, release_tag, app_dir = sys.argv[1:]

def normalize_repository(value):
    value = str(value).strip().rstrip("/").lower()
    return value[:-4] if value.endswith(".git") else value

try:
    with open(metadata_path, encoding="utf-8-sig") as handle:
        metadata = json.load(handle)
    matches = (
        metadata.get("managed") is True
        and normalize_repository(metadata.get("repository", "")) == normalize_repository(repository)
        and (metadata.get("channel") in ("dev", "stable") or ("channel" not in metadata and metadata.get("branch") == "main"))
        and os.path.realpath(metadata.get("appPath", "")) == os.path.realpath(app_dir)
    )
except (OSError, TypeError, ValueError):
    matches = False

raise SystemExit(0 if matches else 1)
PY
}

write_metadata() {
    "$python_runtime" - "$metadata_path" "$repository" "$channel" "$app_dir" <<'PY'
import datetime
import json
import sys

metadata_path, repository, channel, app_dir = sys.argv[1:]
metadata = {
    "schema": 1,
    "managed": True,
    "repository": repository,
    "channel": channel,
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
require_node_runtime
require_command npm
require_command docker
docker compose version >/dev/null 2>&1 || fail "Required command 'docker compose' is not available."
resolve_python_runtime
"$python_runtime" -m venv --help >/dev/null 2>&1 || fail "Python's venv module is not available."

if [ -z "$channel" ] && [ -f "$metadata_path" ]; then
    channel="$(node -e 'const m=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8").replace(/^\uFEFF/,"")); const c=m.channel ?? (m.branch === "main" ? "dev" : "invalid"); console.log(c)' "$metadata_path")"
fi
channel="${channel:-stable}"
case "$channel" in dev|stable) ;; *) fail "FOCUS_CHANNEL must be dev or stable." ;; esac
release_tag=main
if [ "$channel" = stable ]; then
release_tag="$("$python_runtime" - <<'PY'
import json, re, urllib.request
request = urllib.request.Request("https://api.github.com/repos/Leeminjing/Focus/releases/latest", headers={"User-Agent": "Focus-installer"})
with urllib.request.urlopen(request, timeout=15) as response:
    release = json.load(response)
if release.get("draft") is not False or release.get("prerelease") is not False or not release.get("published_at") or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", release.get("tag_name", "")):
    raise SystemExit("No published stable Focus release is available.")
print(release["tag_name"])
PY
)"
fi

mkdir -p "$focus_home" "$bin_dir" "$focus_home/plugins" "$focus_home/users"

existing_install=false
if [ -e "$app_dir" ]; then
    managed_install_matches || fail "$app_dir already exists but is not a Focus managed installation. Move it elsewhere, then run the installer again."
    existing_install=true
fi
printf 'Cloning %s Focus %s...\n' "$channel" "$release_tag"
mkdir -p "$staging_root"
trap cleanup_staging EXIT
trap 'exit 130' HUP INT TERM
git clone --branch "$release_tag" --single-branch "$repository" "$staging_app"
cli_source="$staging_app"
if [ "$existing_install" = false ]; then
    mv "$staging_app" "$app_dir"
    cli_source="$app_dir"
    write_metadata
fi
cli_dir="$focus_home/runtime/cli"
mkdir -p "$cli_dir"
if [ ! -f "$cli_source/scripts/cli-protocol.json" ]; then
    git clone --branch main --single-branch "$repository" "$controller_app"
    cli_source="$controller_app"
fi
export FOCUS_UPDATE_APP="$app_dir"

[ -f "$focus_home/config.yaml" ] || printf '# Focus global configuration fallback layer.\n' >"$focus_home/config.yaml"
[ -f "$focus_home/extensions_config.json" ] || printf '{"mcpServers": {}}\n' >"$focus_home/extensions_config.json"
[ -f "$focus_home/.env" ] || cp "$app_dir/.env.example" "$focus_home/.env"

cli_script="$cli_source/scripts/focus.sh"
[ -f "$cli_script" ] || fail "The installed repository does not contain scripts/focus.sh."

was_recovery=false
[ ! -f "$focus_home/update-state.json" ] || was_recovery=true
sh "$cli_script" update "--$channel"
[ "$was_recovery" = false ] && [ -f "$cli_dir/current.json" ] || fail "Recovery completed. Run this installer again to finish the upgrade."
cp "$cli_source/scripts/cli-launch.cjs" "$cli_dir/cli-launch.cjs.tmp"
mv "$cli_dir/cli-launch.cjs.tmp" "$cli_dir/cli-launch.cjs"
cat >"$bin_dir/focus.tmp" <<'SH'
#!/bin/sh
export FOCUS_UPDATE_APP="$HOME/.focus/app"
exec node "$HOME/.focus/runtime/cli/cli-launch.cjs" "$@"
SH
chmod 755 "$bin_dir/focus.tmp"
mv "$bin_dir/focus.tmp" "$bin_dir/focus"
configure_shell_path
export PATH="$bin_dir:$PATH"


printf '\nFocus installed successfully.\n\n'
printf 'On first launch, Model Settings will open. Enter your API key and save to verify chat and curation.\n'
printf 'Optional environment-file configuration: %s\n\n' "$focus_home/.env"
printf 'Open a new terminal, then run:\n    focus\n\n'
printf 'Update later with:\n    focus update\n'
