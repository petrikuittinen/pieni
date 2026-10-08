#!/usr/bin/env bash
#
# Install pieni on Ubuntu Linux.
#
# It checks the launcher in this checkout, makes sure the Python dependencies
# (openai, openrouter, rich) are importable, and puts a `pieni` command into
# ~/.local/bin as a symlink to this checkout's launcher. No sudo is needed.
#
#   scripts/install.sh                  install into ~/.local/bin
#   scripts/install.sh --prefix /usr/local
#   scripts/install.sh --dry-run        show the actions, change nothing
#   scripts/install.sh --skip-deps      do not check or install dependencies
#
# Uninstall: rm the printed symlink, e.g. ~/.local/bin/pieni. The saved sessions
# in each workspace's .pieni/ directory are not touched.
set -eu

usage() {
    cat <<'EOF'
usage: scripts/install.sh [options]

  --prefix DIR    install into DIR/bin (default: $PREFIX or ~/.local)
  --skip-deps     do not check or install the Python dependencies
  --dry-run       print the actions without changing anything
  -h, --help      show this help

Uninstall: remove the printed symlink, e.g. rm ~/.local/bin/pieni.

The Python dependencies are installed into <checkout>/.venv, which the launcher
picks up automatically. The default prefix needs no sudo.
EOF
}

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Run a command, or only describe it when --dry-run is set.
do_it() {
    if [ "$dry_run" -eq 1 ]; then
        printf '  [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

# --- Options ---------------------------------------------------------------

: "${HOME:?HOME is not set}"
prefix="${PREFIX:-$HOME/.local}"
skip_deps=0
dry_run=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)
            [ $# -ge 2 ] || die "--prefix needs a directory"
            prefix="$2"
            shift 2
            ;;
        --prefix=*)
            prefix="${1#*=}"
            [ -n "$prefix" ] || die "--prefix needs a directory"
            shift
            ;;
        --skip-deps) skip_deps=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h | --help) usage; exit 0 ;;
        *) usage >&2; die "unknown option '$1'" ;;
    esac
done

# --- The checkout this script lives in -------------------------------------

# An absolute prefix keeps the symlink and the PATH hint usable from anywhere.
if [ -d "$prefix" ]; then
    prefix="$(cd "$prefix" && pwd)"
fi
case "$prefix" in
    /*) ;;
    *) prefix="$PWD/$prefix" ;;
esac

repo="$(cd "$(dirname "$0")/.." && pwd)"
for file in pieni pieni.py requirements.txt; do
    [ -e "$repo/$file" ] || die "$repo/$file is missing: run this script from a pieni checkout"
done
say "checkout: $repo"
say "target:   $prefix/bin/pieni"

case "$(uname -s)" in
    Linux) ;;
    *) warn "this installer is written for Ubuntu Linux; continuing on $(uname -s)" ;;
esac
if [ -r /etc/os-release ] && ! grep -qi '^ID=ubuntu' /etc/os-release; then
    warn "this installer is written for Ubuntu Linux; continuing anyway"
fi

# --- Python ----------------------------------------------------------------

python="${PYTHON:-python3}"
command -v "$python" >/dev/null 2>&1 \
    || die "$python not found: sudo apt install python3"
"$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
    || die "$python is too old: pieni needs Python 3.8 or newer"
say "python:   $("$python" -c 'import sys; print(sys.version.split()[0])') ($(command -v "$python"))"

# --- The launcher must be usable before we link to it ----------------------

launcher="$repo/pieni"
[ -f "$launcher" ] || die "$launcher is not a regular file"
head -n 1 "$launcher" | grep -q '^#!' || die "$launcher has no shebang line"
bash -n "$launcher" || die "$launcher has a shell syntax error"
if [ -x "$launcher" ]; then
    say "launcher: executable"
else
    say "launcher: adding the executable bit"
    do_it chmod +x "$launcher" || die "cannot make $launcher executable"
fi

# --- Dependencies ----------------------------------------------------------

dependencies="not checked (--skip-deps)"
if [ "$skip_deps" -eq 0 ]; then
    deps_python=""
    for candidate in "$repo/.venv/bin/python" "$(command -v "$python" 2>/dev/null || true)"; do
        [ -n "$candidate" ] && [ -x "$candidate" ] || continue
        if "$candidate" -c 'import openai, openrouter, rich' >/dev/null 2>&1; then
            deps_python="$candidate"
            break
        fi
    done
    if [ -n "$deps_python" ]; then
        dependencies="already available with $deps_python"
        say "deps:     $dependencies"
    else
        venv="$repo/.venv"
        say "deps:     missing; installing openai, openrouter and rich into $venv"
        if [ ! -x "$venv/bin/python" ]; then
            do_it "$python" -m venv "$venv" \
                || die "cannot create $venv: sudo apt install python3-venv"
            # `python3 -m venv` can return success without producing an interpreter.
            if [ "$dry_run" -eq 0 ] && [ ! -x "$venv/bin/python" ]; then
                die "cannot create $venv: sudo apt install python3-venv"
            fi
        fi
        if [ "$dry_run" -eq 1 ]; then
            printf '  [dry-run] %s -m pip install --requirement %s\n' \
                "$venv/bin/python" "$repo/requirements.txt"
        else
            "$venv/bin/python" -m pip install --requirement "$repo/requirements.txt" \
                || die "pip install failed: check your network connection and try again"
            "$venv/bin/python" -c 'import openai, openrouter, rich' >/dev/null 2>&1 \
                || die "dependencies are still missing: $venv/bin/pip install -r $repo/requirements.txt"
            dependencies="installed into $venv"
            say "deps:     $dependencies"
        fi
    fi
else
    warn "--skip-deps: dependencies were not checked or installed"
fi

# --- Install the command ---------------------------------------------------

bin_dir="$prefix/bin"
dest="$bin_dir/pieni"
if [ -L "$dest" ] && [ "$(readlink -f "$dest")" = "$launcher" ]; then
    say "command:  $dest already points here"
elif [ -e "$dest" ] || [ -L "$dest" ]; then
    die "$dest already exists and is not this checkout's launcher: move it away and retry"
else
    do_it mkdir -p "$bin_dir" || die "cannot create $bin_dir"
    do_it ln -s "$launcher" "$dest" || die "cannot create the symlink $dest"
    say "command:  $dest -> $launcher"
fi

# --- Prove the installed command runs --------------------------------------

if [ "$dry_run" -eq 1 ]; then
    say "check:    smoke test skipped (--dry-run)"
else
    [ -x "$dest" ] || die "$dest is not executable"
    output="$("$dest" --help 2>&1)" || die "smoke test failed: $dest --help did not exit cleanly"
    case "$output" in
        *pieni*) say "check:    $dest --help responds" ;;
        *) die "smoke test failed: unexpected output from $dest --help" ;;
    esac
fi

# --- Tell the user where things are ----------------------------------------

case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
        warn "$bin_dir is not on your PATH yet; add this line to ~/.profile and open a new shell:"
        printf '  export PATH="%s:$PATH"\n' "$bin_dir" >&2
        ;;
esac

say ""
say "installed:    $dest"
say "dependencies: $dependencies"
say "next:         pieni --help"
say '              pieni deepseek -m "deepseek-chat"   (needs DEEPSEEK_API_KEY)'
say "uninstall:    rm $dest"
