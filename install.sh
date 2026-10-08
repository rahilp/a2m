#!/usr/bin/env bash
# Install a2m from this checkout with one command, so `a2m` and `a2m tui` work in any new shell.
#
#   ./install.sh               install, or upgrade the earlier install in place
#   ./install.sh --with-mule   also install Java, Maven and the Mule runtime that verification needs
#   ./install.sh --no-tui      install without the terminal UI (Textual)
#   ./install.sh --uninstall   remove what this script installed, and nothing else
#
# It uses the first of: uv (`uv tool install`), pipx, or a private virtual environment under
# ~/.local/share/a2m/venv with a ~/.local/bin/a2m symlink. The install is editable, so a
# `git pull` updates the code; rerun this script when the dependencies change. It never uses
# sudo and never edits a shell file: when the command's folder is not on PATH it prints the
# line to add. Works with the bash 3.2 that ships with macOS.
#
# --with-mule downloads Temurin JDK 17, Maven 3.9 and Mule Kernel CE 4.9.0 (the versions a2m is
# verified against), checks each against its published SHA-256 or SHA-512 checksum before unpacking
# it, and puts them in ~/.local/share/a2m/toolchain. a2m reads toolchain.env there at start-up, so
# nothing has to be added to a shell file. When neither uv nor Python 3.11 or newer is found, it
# first installs uv with uv's own installer into ~/.local/bin. It ends by migrating and verifying
# the sample proxy catalog-api (--skip-check skips that). Windows runs it inside WSL2.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: ./install.sh [--with-mule [--skip-check]] [--no-tui] [--uninstall] [--help]

  (no option)   Install a2m from this checkout, or upgrade the earlier install in place.
  --with-mule   Also install Java (Temurin 17), Maven 3.9 and Mule Kernel CE 4.9.0 under
                ~/.local/share/a2m/toolchain, so a2m can build and verify apps, then check the
                install on the sample proxy catalog-api (about 2 to 3 minutes the first time).
  --skip-check  With --with-mule: skip that final check.
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
# The PATH new shells get; installing uv adds ~/.local/bin to this script's PATH only.
SHELL_PATH="${PATH:-}"
SELF_CHECK_DIR="$STATE_DIR/self-check"

# --with-mule: the toolchain a2m is verified against. The base URLs can be overridden (tests point
# them at a local file:// mirror); each archive is checked against the checksum file next to it.
TOOLCHAIN="$STATE_DIR/toolchain"
TOOLCHAIN_ENV="$TOOLCHAIN/toolchain.env"
JDK_VERSION="17.0.20.1+1"
MAVEN_VERSION="3.9.16"
MULE_VERSION="4.9.0"
JDK_BASE_URL="${A2M_JDK_BASE_URL:-https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.20.1%2B1}"
# Maven's own distribution on Maven Central: the same file and SHA-512 as archive.apache.org, which
# throttles downloads to a crawl, and a host the Maven builds need anyway.
MAVEN_BASE_URL="${A2M_MAVEN_BASE_URL:-https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/$MAVEN_VERSION}"
MULE_BASE_URL="${A2M_MULE_BASE_URL:-https://repository.mulesoft.org/nexus/content/repositories/releases/org/mule/distributions/mule-standalone/$MULE_VERSION}"
UV_INSTALLER_URL="${A2M_UV_INSTALLER_URL:-https://astral.sh/uv/install.sh}"
# Written into a piece's folder after it is fully in place; a folder without it is not installed.
INSTALLED_MARK=".a2m-installed"

ACTION=install
WITH_TUI=1
WITH_MULE=0
SELF_CHECK=1
for arg in "$@"; do
    case "$arg" in
        --uninstall) ACTION=uninstall ;;
        --no-tui) WITH_TUI=0 ;;
        --with-mule) WITH_MULE=1 ;;
        --skip-check) SELF_CHECK=0 ;;
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
        if [ -n "$RECORD_TOOLCHAIN" ]; then
            printf 'toolchain=%s\n' "$RECORD_TOOLCHAIN"
        fi
        if [ "$RECORD_UV" = 1 ]; then
            printf 'uv_installed=1\n'
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

# Prints why removing something --with-mule installed failed; the caller keeps the install record.
toolchain_removal_failed() {
    printf 'install.sh: could not remove %s (see the messages above); the install record at %s was kept so you can fix the problem and run ./install.sh --uninstall again.\n' "$1" "$RECORD" >&2
}

# Removes what --with-mule installed, as the record names it: the toolchain folder (only when it is
# the one this script uses), the folder of the last self-check, and uv when this script installed
# it. Runs under `||`, where errexit is off, so every removal checks its own status.
remove_with_mule() {
    local recorded tool receipt="${XDG_CONFIG_HOME:-$HOME/.config}/uv/uv-receipt.json"
    recorded="$(record_get toolchain)"
    if [ -n "$recorded" ]; then
        if [ "$recorded" != "$TOOLCHAIN" ]; then
            printf 'Left %s alone: it is not the toolchain folder this script uses (%s).\n' "$recorded" "$TOOLCHAIN"
        elif [ -e "$TOOLCHAIN" ] || [ -L "$TOOLCHAIN" ]; then
            if ! rm -rf "$TOOLCHAIN" || [ -e "$TOOLCHAIN" ]; then
                toolchain_removal_failed "$TOOLCHAIN"
                return 1
            fi
            printf 'Removed %s (Java, Maven and the Mule runtime)\n' "$TOOLCHAIN"
            printf "Left Maven's download cache in ~/.m2, which other Maven builds share; delete it to free the space if nothing else uses it.\n"
        fi
    fi
    if [ -e "$SELF_CHECK_DIR" ]; then
        rm -rf "$SELF_CHECK_DIR" || {
            toolchain_removal_failed "$SELF_CHECK_DIR"
            return 1
        }
    fi
    if [ "$(record_get uv_installed)" = 1 ]; then
        for tool in uv uvx; do
            if [ -e "$LOCAL_BIN/$tool" ] || [ -L "$LOCAL_BIN/$tool" ]; then
                rm -f "$LOCAL_BIN/$tool" || {
                    toolchain_removal_failed "$LOCAL_BIN/$tool"
                    return 1
                }
                printf 'Removed %s\n' "$LOCAL_BIN/$tool"
            fi
        done
        # The uv installer's own record of where it put uv.
        if [ -f "$receipt" ]; then
            rm -f "$receipt" || {
                toolchain_removal_failed "$receipt"
                return 1
            }
            rm -d "${receipt%/*}" 2>/dev/null || true
        fi
        printf 'Left the Python and the cache uv downloaded (in ~/.local/share/uv and ~/.cache/uv); delete those folders to free the space.\n'
    fi
    return 0
}

uninstall() {
    local method
    if [ ! -f "$RECORD" ]; then
        printf 'Nothing to uninstall: a2m was not installed by this script (no install record at %s).\n' "$RECORD"
        exit 0
    fi
    method="$(record_get method)"
    remove_install "$method" || exit 1
    remove_with_mule || exit 1
    rm -f "$RECORD" || die "a2m was removed, but the install record at $RECORD could not be deleted; delete it by hand."
    rm -d "$STATE_DIR" 2>/dev/null || true
    printf 'a2m is uninstalled (it was installed with %s). Shell files and this checkout were left alone.\n' "$method"
}

# The uv this script installed lives in ~/.local/bin, which may not be on PATH in this shell: use
# it from there, so an upgrade or --uninstall finds it instead of installing it again or failing.
if [ "$(record_get uv_installed)" = 1 ] && [ -x "$LOCAL_BIN/uv" ] && ! have uv; then
    PATH="$LOCAL_BIN:$PATH"
    export PATH
fi

if [ "$ACTION" = uninstall ]; then
    uninstall
    exit 0
fi

# ---------------------------------------------------------------- toolchain (--with-mule)

# Sets JDK_PLATFORM (the Temurin download name, e.g. x64_linux) or stops, before anything changes,
# on a platform the toolchain is not published for or when a tool the downloads need is missing.
check_platform() {
    local os arch missing=""
    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os" in
        Linux) os=linux ;;
        Darwin) os=mac ;;
        MINGW* | MSYS* | CYGWIN*) die "--with-mule runs inside WSL2 on Windows: open Ubuntu (wsl --install -d Ubuntu) and run ./install.sh --with-mule there." ;;
        *) die "--with-mule supports Linux and macOS (and Windows through WSL2), not $os." ;;
    esac
    case "$arch" in
        x86_64 | amd64) arch=x64 ;;
        aarch64 | arm64) arch=aarch64 ;;
        *) die "--with-mule supports x86-64 and ARM64 machines, not $arch." ;;
    esac
    JDK_PLATFORM="${arch}_$os"
    have curl || have wget || missing="$missing curl"
    have tar || missing="$missing tar"
    have gzip || missing="$missing gzip"
    { have sha256sum && have sha512sum; } || have shasum || missing="$missing sha256sum"
    [ -z "$missing" ] || die "--with-mule needs these tools, which were not found:$missing. Install them (on Debian or Ubuntu: sudo apt install curl tar gzip coreutils) and run ./install.sh --with-mule again."
}

# Downloads a URL to a file with curl, or wget when there is no curl. Returns non-zero on failure.
fetch() {
    if have curl; then
        if [ -t 2 ]; then
            curl -fL --retry 3 --connect-timeout 30 --progress-bar -o "$2" "$1"
        else
            curl -fsSL --retry 3 --connect-timeout 30 -o "$2" "$1"
        fi
    else
        wget -q -O "$2" "$1"
    fi
}

# Prints the lowercase hex SHA-256 or SHA-512 (first argument 256 or 512) of a file. The file is
# read on stdin so that no file name can change the output format.
digest() {
    local sum rest
    if have "sha$1sum"; then
        read -r sum rest < <("sha$1sum" <"$2") || return 1
    elif have shasum; then
        read -r sum rest < <(shasum -a "$1" <"$2") || return 1
    else
        return 1
    fi
    printf '%s\n' "$sum"
}

# The folder a piece's bin/ lives in: a macOS JDK keeps it in Contents/Home.
piece_home() {
    if [ -d "$1/Contents/Home" ]; then
        printf '%s\n' "$1/Contents/Home"
    else
        printf '%s\n' "$1"
    fi
}

# True when the piece in $TOOLCHAIN/<name> is fully installed at the given version: its marker
# names that version and the given program (relative to the piece's home) is there.
piece_ok() {
    local dest="$TOOLCHAIN/$1" mark=""
    [ -f "$dest/$INSTALLED_MARK" ] || return 1
    read -r mark <"$dest/$INSTALLED_MARK" || return 1
    [ "$mark" = "$2" ] || return 1
    [ -x "$(piece_home "$dest")/$3" ]
}

# Installs one piece into $TOOLCHAIN/<name> unless it is already there at this version:
#   install_piece NAME LABEL VERSION ARCHIVE_URL CHECKSUM_URL BITS PROGRAM
# The archive and its checksum file are downloaded into $WORK (inside the toolchain folder, so the
# final move is a rename), the checksum is compared before anything is unpacked, and the marker is
# written last. A download, check or unpack that fails part way leaves no piece that counts as
# installed, and the next run starts that piece again.
install_piece() {
    local name="$1" label="$2" version="$3" url="$4" sums_url="$5" bits="$6" program="$7"
    local dest="$TOOLCHAIN/$1" archive="$WORK/$1.tar.gz" unpack="$WORK/$1.unpack" expected="" rest actual top
    if piece_ok "$name" "$version" "$program"; then
        printf '%s %s is already installed in %s\n' "$label" "$version" "$dest"
        return 0
    fi
    printf 'Downloading %s %s from %s\n' "$label" "$version" "$url"
    fetch "$sums_url" "$WORK/$name.checksum" ||
        die "could not download the $label checksum from $sums_url (see the messages above). If a proxy or firewall blocks it, see Troubleshooting in the README; then run ./install.sh --with-mule again."
    fetch "$url" "$archive" ||
        die "could not download $label from $url (see the messages above). If a proxy or firewall blocks it, see Troubleshooting in the README; then run ./install.sh --with-mule again."
    read -r expected rest <"$WORK/$name.checksum" || [ -n "$expected" ] ||
        die "the $label checksum file from $sums_url is empty; nothing was installed for $label."
    expected="$(printf '%s' "$expected" | tr 'ABCDEF' 'abcdef')"
    case "$bits:${#expected}:$expected" in
        *:*:*[!0-9a-f]*) die "the $label checksum file from $sums_url does not hold a SHA-$bits checksum; nothing was installed for $label." ;;
        256:64:* | 512:128:*) ;;
        *) die "the $label checksum file from $sums_url does not hold a SHA-$bits checksum; nothing was installed for $label." ;;
    esac
    actual="$(digest "$bits" "$archive")" || die "could not compute a SHA-$bits checksum of the $label download."
    [ "$actual" = "$expected" ] ||
        die "the $label download does not match its published SHA-$bits checksum, so it was not unpacked and nothing was installed for $label (a proxy that rewrites downloads, or a broken download, causes this); run ./install.sh --with-mule again."
    mkdir "$unpack" || die "could not create $unpack."
    tar -xzf "$archive" -C "$unpack" || die "could not unpack the $label download (see the messages above); nothing was installed for $label. Is the disk full?"
    rm -f "$archive"
    # The archive holds one top-level folder; anything else is not the archive we expect.
    set -- "$unpack"/*
    if [ "$#" != 1 ] || [ ! -d "$1" ] || [ ! -x "$(piece_home "$1")/$program" ]; then
        die "the $label download does not hold the folder with $program expected; nothing was installed for $label."
    fi
    top="$1"
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        rm -rf "$dest" || die "could not remove the incomplete or older $dest; remove it by hand and run ./install.sh --with-mule again."
    fi
    mv "$top" "$dest" || die "could not move $label into $dest."
    printf '%s\n' "$version" >"$dest/$INSTALLED_MARK" || die "could not write $dest/$INSTALLED_MARK."
    rm -rf "$unpack"
    printf 'Installed %s %s in %s\n' "$label" "$version" "$dest"
}

# Writes toolchain.env, which a2m reads at start-up (a2m/toolchain.py), through a temporary file.
write_toolchain_env() {
    {
        printf '# Written by install.sh --with-mule. a2m reads this at start-up; A2M_NO_TOOLCHAIN=1 turns that off.\n'
        printf 'JAVA_HOME=%s\n' "$(piece_home "$TOOLCHAIN/jdk")"
        printf 'MAVEN_HOME=%s\n' "$TOOLCHAIN/maven"
        printf 'MULE_HOME=%s\n' "$TOOLCHAIN/mule"
        printf 'JAVA_VERSION=%s\n' "$JDK_VERSION"
        printf 'MAVEN_VERSION=%s\n' "$MAVEN_VERSION"
        printf 'MULE_VERSION=%s\n' "$MULE_VERSION"
    } >"$TOOLCHAIN_ENV.tmp" && mv -f "$TOOLCHAIN_ENV.tmp" "$TOOLCHAIN_ENV"
}

WORK=""
# Removes the download folder on every exit, including a stop with Ctrl-C.
cleanup_work() {
    [ -z "$WORK" ] || rm -rf "$WORK"
}

install_toolchain() {
    local jdk_file="OpenJDK17U-jdk_${JDK_PLATFORM}_hotspot_17.0.20.1_1.tar.gz"
    local maven_file="apache-maven-$MAVEN_VERSION-bin.tar.gz"
    local mule_file="mule-standalone-$MULE_VERSION.tar.gz"
    mkdir -p "$TOOLCHAIN" || die "could not create $TOOLCHAIN."
    rm -rf "$TOOLCHAIN"/.download.* 2>/dev/null || true
    trap cleanup_work EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    WORK="$(mktemp -d "$TOOLCHAIN/.download.XXXXXX")" || die "could not create a download folder in $TOOLCHAIN."
    install_piece jdk "Java (Temurin JDK)" "$JDK_VERSION" \
        "$JDK_BASE_URL/$jdk_file" "$JDK_BASE_URL/$jdk_file.sha256.txt" 256 bin/java
    install_piece maven "Maven" "$MAVEN_VERSION" \
        "$MAVEN_BASE_URL/$maven_file" "$MAVEN_BASE_URL/$maven_file.sha512" 512 bin/mvn
    install_piece mule "Mule Kernel CE" "$MULE_VERSION" \
        "$MULE_BASE_URL/$mule_file" "$MULE_BASE_URL/$mule_file.sha256" 256 bin/mule
    rm -rf "$WORK"
    WORK=""
    write_toolchain_env || die "could not write $TOOLCHAIN_ENV."
    printf 'The toolchain is in %s; a2m finds it through %s.\n' "$TOOLCHAIN" "$TOOLCHAIN_ENV"
}

# Installs uv with its official installer into ~/.local/bin. UV_NO_MODIFY_PATH=1 keeps it from
# editing any shell file; this script puts the folder on its own PATH so it can use uv right away.
install_uv() {
    local script
    printf 'Neither uv nor Python 3.11 or newer was found; installing uv into %s (no shell file is changed).\n' "$LOCAL_BIN"
    mkdir -p "$STATE_DIR" "$LOCAL_BIN" || die "could not create $STATE_DIR or $LOCAL_BIN."
    script="$(mktemp "$STATE_DIR/uv-installer.XXXXXX")" || die "could not create a temporary file in $STATE_DIR."
    if ! fetch "$UV_INSTALLER_URL" "$script"; then
        rm -f "$script"
        die "could not download the uv installer from $UV_INSTALLER_URL. $NEED_PYTHON"
    fi
    if ! UV_INSTALL_DIR="$LOCAL_BIN" UV_NO_MODIFY_PATH=1 sh "$script"; then
        rm -f "$script"
        die "the uv installer failed (see the messages above). $NEED_PYTHON"
    fi
    rm -f "$script"
    PATH="$LOCAL_BIN:$PATH"
    export PATH
    have uv || die "the uv installer finished but $LOCAL_BIN/uv was not found. $NEED_PYTHON"
}

# Migrates and verifies the sample proxy catalog-api with the installed a2m and the toolchain.
# Exits non-zero when it does not land in verified, leaving everything installed.
self_check() {
    local input="$REPO/tests/fixtures/e2e/input"
    if [ ! -d "$input/catalog-api" ]; then
        printf 'Skipped the self-check: the sample proxy %s is not in this checkout.\n' "$input/catalog-api"
        return 0
    fi
    printf '\nChecking the install: migrating the sample proxy catalog-api and verifying it on the local Mule runtime.\n'
    printf 'The first run takes about 2 to 3 minutes while Maven downloads its plugins...\n'
    rm -rf "$SELF_CHECK_DIR" || die "could not clear the old self-check results in $SELF_CHECK_DIR."
    if "$BIN/a2m" migrate "$input" --only catalog-api --llm none --mock-backends --out "$SELF_CHECK_DIR" &&
        [ -d "$SELF_CHECK_DIR/verified/catalog-api" ]; then
        rm -rf "$SELF_CHECK_DIR"
        printf 'Self-check passed: catalog-api was built with Maven, deployed on Mule %s and passed its tests (verified).\n' "$MULE_VERSION"
        return 0
    fi
    printf '\ninstall.sh: self-check failed: catalog-api did not land in verified. a2m and the toolchain stay installed. See %s and %s for what went wrong, then run ./install.sh --with-mule again.\n' \
        "$SELF_CHECK_DIR/run.log" "$SELF_CHECK_DIR/needs-review/catalog-api/REPORT.md" >&2
    exit 1
}

# ---------------------------------------------------------------- install

if [ "$WITH_TUI" = 1 ]; then
    TARGET="$REPO[tui,claude]"
else
    TARGET="$REPO[claude]"
fi

# Read before anything changes: a rerun without --with-mule keeps what an earlier one installed.
RECORD_TOOLCHAIN="$(record_get toolchain)"
RECORD_UV="$(record_get uv_installed)"
[ "$RECORD_UV" = 1 ] || RECORD_UV=0
if [ "$WITH_MULE" = 1 ]; then
    check_platform
    RECORD_TOOLCHAIN="$TOOLCHAIN"
    if ! have uv && [ -z "$(find_python)" ]; then
        install_uv
        RECORD_UV=1
    fi
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

if [ "$WITH_MULE" = 1 ]; then
    install_toolchain
fi

printf 'a2m is installed at %s (editable: a git pull updates the code; rerun ./install.sh when dependencies change).\n' "$BIN/a2m"
if [ "$WITH_TUI" = 0 ]; then
    printf 'Installed without the terminal UI; run ./install.sh again without --no-tui to add it.\n'
fi
printf 'Remove it with: %s/install.sh --uninstall\n' "$REPO"

case ":$SHELL_PATH:" in
    *":$BIN:"*)
        FOUND="$(PATH="$SHELL_PATH" command -v a2m || true)"
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

if [ "$WITH_MULE" = 1 ] && [ "$SELF_CHECK" = 1 ]; then
    self_check
fi
