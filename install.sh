#!/usr/bin/env bash
# Install a2m from this checkout with one command, so `a2m` and `a2m tui` work in any new shell.
#
#   ./install.sh               install, or upgrade the earlier install in place
#   ./install.sh --no-tui      install without the terminal UI (Textual)
#   ./install.sh --uninstall   remove what this script installed, and nothing else
#
# It uses the first of: uv (`uv tool install`), pipx, or a private virtual environment under
# ~/.local/share/a2m/venv with a ~/.local/bin/a2m symlink. The install is editable, so a
# `git pull` updates the code; rerun this script when the dependencies change. It never uses
# sudo and never edits a shell file: when the command's folder is not on PATH it prints the
# line to add. Works with the bash 3.2 that ships with macOS.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./install.sh [--no-tui] [--uninstall] [--help]

  (no option)   Install a2m from this checkout, or upgrade the earlier install in place.
  --no-tui      Install without the terminal UI extra (Textual); `a2m tui` then says what is missing.
  --uninstall   Remove the a2m command and what this script installed, and nothing else.
  -h, --help    Show this help.
EOF
}

die() {
    printf 'install.sh: %s\n' "$1" >&2
    exit 1
}

REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
[ -n "${HOME:-}" ] || die "HOME is not set; cannot choose where to install a2m."
STATE_DIR="$HOME/.local/share/a2m"
RECORD="$STATE_DIR/install-record"
VENV="$STATE_DIR/venv"
LOCAL_BIN="$HOME/.local/bin"

ACTION=install
WITH_TUI=1
for arg in "$@"; do
    case "$arg" in
        --uninstall) ACTION=uninstall ;;
        --no-tui) WITH_TUI=0 ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            printf 'install.sh: unknown option: %s\n' "$arg" >&2
            usage >&2
            exit 2
            ;;
    esac
done

have() {
    command -v "$1" >/dev/null 2>&1
}

NEED_PYTHON="a2m needs Python 3.11 or newer (or uv, https://docs.astral.sh/uv/, which brings its own Python): install one of them and run ./install.sh again."

# True when the given interpreter (a name on PATH or a path) runs and is Python 3.11 or newer.
python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

# The first Python 3.11 or newer on PATH, or nothing.
find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        if have "$candidate" && python_ok "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 0
}

# Reads one key from the install record (lines of key=value); prints nothing when absent.
record_get() {
    local key value
    [ -f "$RECORD" ] || return 0
    while IFS='=' read -r key value || [ -n "$key" ]; do
        if [ "$key" = "$1" ]; then
            printf '%s\n' "$value"
            return 0
        fi
    done <"$RECORD"
    return 0
}

# Returns non-zero when the record cannot be written (callers run it under `||`, where errexit is off).
write_record() {
    mkdir -p "$STATE_DIR" || return 1
    {
        printf 'method=%s\n' "$1"
        printf 'bin=%s\n' "$2"
        printf 'repo=%s\n' "$REPO"
        if [ "$1" = venv ]; then
            printf 'venv=%s\n' "$VENV"
            printf 'link=%s\n' "$LOCAL_BIN/a2m"
        fi
    } >"$RECORD" || return 1
}

# ~/.local/bin/a2m is ours only when it is a symlink pointing into the private venv.
link_is_ours() {
    local target
    [ -L "$LOCAL_BIN/a2m" ] || return 1
    target="$(readlink "$LOCAL_BIN/a2m")"
    case "$target" in
        "$VENV"/*) return 0 ;;
        *) return 1 ;;
    esac
}

# Prints why removing part of the private venv install failed; the caller keeps the install record.
venv_removal_failed() {
    printf 'install.sh: could not remove %s (see the messages above), so a2m is not fully uninstalled; the install record at %s was kept so you can fix the problem and run ./install.sh --uninstall again.\n' "$1" "$RECORD" >&2
}

# Deletes the venv folder, keeping pyvenv.cfg until everything else is gone, so a removal that
# fails part way still looks like our venv to a retry. Returns non-zero on any failure.
remove_venv_dir() {
    local entry failed=0
    for entry in "$VENV"/* "$VENV"/.[!.]* "$VENV"/..?*; do
        [ -e "$entry" ] || [ -L "$entry" ] || continue
        [ "$entry" = "$VENV/pyvenv.cfg" ] && continue
        rm -rf "$entry" || failed=1
    done
    [ "$failed" = 0 ] || return 1
    rm -f "$VENV/pyvenv.cfg" || return 1
    rm -d "$VENV" || return 1
}

# Removes the private venv install: the symlink only if it points into the venv, and the venv
# only if it really is a virtual environment (not a symlink, holds pyvenv.cfg). It runs under
# `||`, where errexit is off, so every removal checks its own status and returns non-zero on failure.
remove_venv_install() {
    if link_is_ours; then
        rm -f "$LOCAL_BIN/a2m" || {
            venv_removal_failed "$LOCAL_BIN/a2m"
            return 1
        }
        printf 'Removed %s\n' "$LOCAL_BIN/a2m"
    elif [ -e "$LOCAL_BIN/a2m" ] || [ -L "$LOCAL_BIN/a2m" ]; then
        printf 'Left %s alone: it does not point into %s\n' "$LOCAL_BIN/a2m" "$VENV"
    fi
    if [ -d "$VENV" ] && [ ! -L "$VENV" ] && [ -f "$VENV/pyvenv.cfg" ]; then
        remove_venv_dir || {
            venv_removal_failed "$VENV"
            return 1
        }
        printf 'Removed %s\n' "$VENV"
    fi
    return 0
}

# True only when absence is positively established after a failed manager uninstall: the
# manager's own listing (the command given as arguments) runs, does not list a2m, and the
# recorded a2m command is gone. Any doubt (the listing fails, the command is still there) is false.
manager_reports_absent() {
    local listing bin
    listing="$("$@" 2>/dev/null)" || return 1
    if grep -q '^a2m\( \|$\)' <<<"$listing"; then
        return 1
    fi
    bin="$(record_get bin)"
    if [ -n "$bin" ] && { [ -e "$bin/a2m" ] || [ -L "$bin/a2m" ]; }; then
        return 1
    fi
    return 0
}

# Prints why a manager uninstall failed; the caller keeps the install record so it can be retried.
uninstall_failed() {
    printf 'install.sh: %s could not uninstall a2m (see the messages above) and a2m is still installed; the install record at %s was kept so you can fix the problem and run ./install.sh --uninstall again.\n' "$1" "$RECORD" >&2
}

# Undoes one recorded install. Returns non-zero when the tool needed to undo it is missing or the
# uninstall failed and a2m may still be installed.
remove_install() {
    case "$1" in
        uv)
            have uv || {
                printf 'install.sh: uv is no longer on PATH; run `uv tool uninstall a2m` once it is.\n' >&2
                return 1
            }
            if ! uv tool uninstall a2m; then
                manager_reports_absent uv tool list || {
                    uninstall_failed uv
                    return 1
                }
                printf 'uv reports a2m is not installed and its command is gone; treating it as already uninstalled.\n'
            fi
            ;;
        pipx)
            have pipx || {
                printf 'install.sh: pipx is no longer on PATH; run `pipx uninstall a2m` once it is.\n' >&2
                return 1
            }
            if ! pipx uninstall a2m; then
                manager_reports_absent pipx list --short || {
                    uninstall_failed pipx
                    return 1
                }
                printf 'pipx reports a2m is not installed and its command is gone; treating it as already uninstalled.\n'
            fi
            ;;
        venv) remove_venv_install ;;
        *)
            printf 'install.sh: the install record names an unknown method %s; nothing removed.\n' "$1" >&2
            return 1
            ;;
    esac
}

uninstall() {
    local method
    if [ ! -f "$RECORD" ]; then
        printf 'Nothing to uninstall: a2m was not installed by this script (no install record at %s).\n' "$RECORD"
        exit 0
    fi
    method="$(record_get method)"
    remove_install "$method" || exit 1
    rm -f "$RECORD" || die "a2m was removed, but the install record at $RECORD could not be deleted; delete it by hand."
    rm -d "$STATE_DIR" 2>/dev/null || true
    printf 'a2m is uninstalled (it was installed with %s). Shell files and this checkout were left alone.\n' "$method"
}

if [ "$ACTION" = uninstall ]; then
    uninstall
    exit 0
fi

# ---------------------------------------------------------------- install

if [ "$WITH_TUI" = 1 ]; then
    TARGET="$REPO[tui,claude]"
else
    TARGET="$REPO[claude]"
fi

# Rerunning upgrades the same install: keep the recorded method while its tool is still there.
PREVIOUS="$(record_get method)"
METHOD=""
case "$PREVIOUS" in
    uv) have uv && METHOD=uv ;;
    pipx) have pipx && METHOD=pipx ;;
    venv) [ -n "$(find_python)" ] && METHOD=venv ;;
esac
if [ -z "$METHOD" ]; then
    if have uv; then
        METHOD=uv
    elif have pipx; then
        METHOD=pipx
    elif [ -n "$(find_python)" ]; then
        METHOD=venv
    else
        die "$NEED_PYTHON"
    fi
fi
# pipx needs a Python 3.11 or newer to build a2m's environment: the first one on PATH, or else
# pipx's own default interpreter. Check before changing anything, so an old Python stops here
# with the same line as having no tools at all. When pipx cannot say which interpreter it uses
# (older pipx), PY stays empty: its install still runs, and a failure names the requirement.
PY=""
if [ "$METHOD" = pipx ]; then
    PY="$(find_python)"
    if [ -z "$PY" ]; then
        PIPX_PY="$(pipx environment --value PIPX_DEFAULT_PYTHON 2>/dev/null || true)"
        if [ -n "$PIPX_PY" ]; then
            python_ok "$PIPX_PY" || die "$NEED_PYTHON"
            PY="$PIPX_PY"
        fi
    fi
fi
if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$METHOD" ]; then
    printf 'Replacing the earlier %s install with a %s install.\n' "$PREVIOUS" "$METHOD"
    remove_install "$PREVIOUS" || die "could not remove the earlier $PREVIOUS install (see the messages above); the new install was not started."
    rm -f "$RECORD" || die "could not delete the old install record at $RECORD; the new install was not started."
fi

case "$METHOD" in
    uv)
        printf 'Installing a2m from %s with uv...\n' "$REPO"
        uv tool install --force --editable "$TARGET" || die "uv tool install failed (see the messages above)."
        BIN="$(uv tool dir --bin)" || die "uv tool dir --bin failed; cannot find the a2m command."
        ;;
    pipx)
        printf 'Installing a2m from %s with pipx...\n' "$REPO"
        if [ -n "$PY" ]; then
            pipx install --force --python "$PY" --editable "$TARGET" || die "pipx install failed (see the messages above)."
        else
            pipx install --force --editable "$TARGET" ||
                die "pipx install failed (see the messages above). $NEED_PYTHON"
        fi
        BIN="${PIPX_BIN_DIR:-$LOCAL_BIN}"
        ;;
    venv)
        PY="$(find_python)"
        printf 'Installing a2m from %s into %s with %s...\n' "$REPO" "$VENV" "$PY"
        if [ -e "$LOCAL_BIN/a2m" ] || [ -L "$LOCAL_BIN/a2m" ]; then
            link_is_ours || die "$LOCAL_BIN/a2m already exists and was not made by this script; remove it or install uv or pipx, then run ./install.sh again."
        fi
        mkdir -p "$STATE_DIR" || die "could not create $STATE_DIR."
        if [ ! -x "$VENV/bin/python" ]; then
            "$PY" -m venv "$VENV" ||
                die "could not create a virtual environment with $PY (on Debian or Ubuntu install python3-venv), or install uv."
        fi
        "$VENV/bin/python" -m pip install --upgrade -e "$TARGET" || die "pip install failed (see the messages above)."
        mkdir -p "$LOCAL_BIN" || die "could not create $LOCAL_BIN."
        ln -sfn "$VENV/bin/a2m" "$LOCAL_BIN/a2m" || die "could not link $LOCAL_BIN/a2m to $VENV/bin/a2m."
        BIN="$LOCAL_BIN"
        ;;
esac

write_record "$METHOD" "$BIN" || die "a2m was installed, but the install record at $RECORD could not be written, so --uninstall would not know about it; fix the problem and run ./install.sh again."

[ -x "$BIN/a2m" ] || die "the install finished but $BIN/a2m was not found."
VERSION="$("$BIN/a2m" --version)" || die "a2m was installed at $BIN/a2m but \`a2m --version\` failed."

printf 'a2m is installed at %s (editable: a git pull updates the code; rerun ./install.sh when dependencies change).\n' "$BIN/a2m"
if [ "$WITH_TUI" = 0 ]; then
    printf 'Installed without the terminal UI; run ./install.sh again without --no-tui to add it.\n'
fi
printf 'Remove it with: %s/install.sh --uninstall\n' "$REPO"

case ":${PATH:-}:" in
    *":$BIN:"*)
        FOUND="$(command -v a2m || true)"
        if [ -n "$FOUND" ] && [ "$FOUND" != "$BIN/a2m" ]; then
            printf 'Note: another a2m at %s comes first on PATH; it runs instead of this one.\n' "$FOUND"
        fi
        ;;
    *)
        case "${SHELL:-}" in
            */zsh) SHELL_FILE="~/.zshrc" ;;
            */fish) SHELL_FILE="~/.config/fish/config.fish" ;;
            */bash)
                if [ "$(uname -s)" = Darwin ]; then
                    SHELL_FILE="~/.bash_profile"
                else
                    SHELL_FILE="~/.bashrc"
                fi
                ;;
            *) SHELL_FILE="~/.profile" ;;
        esac
        printf '\n%s is not on your PATH. Add this line to %s, then open a new shell:\n\n' "$BIN" "$SHELL_FILE"
        # Quote the folder for the target shell so the printed line works for any folder name
        # (spaces, quotes, $, backslashes): fish single quotes escape only \ and '; inside
        # sh/bash/zsh double quotes \ " $ and ` are escaped, so $PATH itself still expands.
        if [ "$SHELL_FILE" = "~/.config/fish/config.fish" ]; then
            QUOTED_BIN="$(printf '%s' "$BIN" | sed "s/[\\\\']/\\\\&/g")"
            printf "    fish_add_path '%s'\n\n" "$QUOTED_BIN"
        else
            QUOTED_BIN="$(printf '%s' "$BIN" | sed 's/[\\"$`]/\\&/g')"
            printf '    export PATH="%s:$PATH"\n\n' "$QUOTED_BIN"
        fi
        printf 'Until then, run it as %s\n' "$BIN/a2m"
        ;;
esac

printf 'Installed version: %s\n' "$VERSION"
