#!/usr/bin/env bash
# Install or update claudette. Safe to re-run: an existing install is
# fast-forwarded to the latest version.
#
#   curl -fsSL https://raw.githubusercontent.com/raed667/claudette/main/install.sh | bash
#
# Env overrides: CLAUDETTE_REPO, CLAUDETTE_BRANCH, CLAUDETTE_HOME, CLAUDETTE_BIN_DIR
set -euo pipefail

# Everything runs from main(), called on the last line, so bash parses the
# whole file before acting. That keeps a truncated download from running half
# a script, and lets `git pull` replace this file while it runs.
main() {
  local repo=${CLAUDETTE_REPO:-https://github.com/raed667/claudette.git}
  local branch=${CLAUDETTE_BRANCH:-main}
  local home=${CLAUDETTE_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/claudette}
  local bin_dir=${CLAUDETTE_BIN_DIR:-$HOME/.local/bin}
  local config=${CLAUDETTE_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/claudette}

  need git "https://git-scm.com/downloads"
  need uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
  need curl ""
  need openssl ""

  local before="" after
  if [ -d "$home/.git" ]; then
    before=$(git -C "$home" rev-parse --short HEAD)
    if [ -n "$(git -C "$home" status --porcelain --untracked-files=no)" ]; then
      die "$home has local changes; commit or discard them, then re-run"
    fi
    say "updating $home"
    git -C "$home" fetch --quiet origin "$branch"
    git -C "$home" checkout --quiet "$branch"
    git -C "$home" merge --quiet --ff-only "origin/$branch" \
      || die "$home has diverged from origin/$branch; resolve it by hand"
  elif [ -e "$home" ]; then
    die "$home exists but is not a git checkout; move it aside or set CLAUDETTE_HOME"
  else
    say "installing into $home"
    mkdir -p "$(dirname "$home")"
    git clone --quiet --branch "$branch" "$repo" "$home"
  fi
  after=$(git -C "$home" rev-parse --short HEAD)

  say "installing Python dependencies"
  uv sync --quiet --frozen --project "$home"

  mkdir -p "$bin_dir"
  local link=$bin_dir/claudette
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    die "$link exists and is not a symlink; remove it or set CLAUDETTE_BIN_DIR"
  fi
  ln -sfn "$home/bin/claudette" "$link"

  # A running proxy keeps the old config and dependencies until restarted.
  if "$link" proxy status >/dev/null 2>&1 && [ "$before" != "$after" ]; then
    say "restarting the proxy to pick up the update"
    "$link" proxy restart >/dev/null || warn "proxy restart failed; run: claudette proxy restart"
  fi

  maybe_store_key "$config"

  if [ -z "$before" ]; then
    say "installed claudette $after"
  elif [ "$before" = "$after" ]; then
    say "claudette is up to date ($after)"
  else
    say "updated claudette $before -> $after"
  fi

  command -v claude >/dev/null \
    || warn "Claude Code is not installed; get it from https://code.claude.com"
  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) warn "$bin_dir is not on your PATH; add it to your shell profile" ;;
  esac
}

# Prompt for the key only on an interactive terminal. Under `curl | bash`
# stdin is the script itself, so read from /dev/tty instead.
maybe_store_key() {
  local config=$1
  if [ -n "${MISTRAL_API_KEY:-}" ] || [ -s "$config/mistral_api_key" ]; then return 0; fi
  if ! { : </dev/tty; } 2>/dev/null; then
    warn "no Mistral API key; export MISTRAL_API_KEY or write it to $config/mistral_api_key"
    return 0
  fi
  local key
  printf 'Mistral API key (from console.mistral.ai, input hidden, Enter to skip): ' >/dev/tty
  IFS= read -rs key </dev/tty || key=""
  printf '\n' >/dev/tty
  if [ -z "$key" ]; then
    warn "skipped; export MISTRAL_API_KEY or write it to $config/mistral_api_key"
    return 0
  fi
  mkdir -p "$config"
  (umask 077; printf '%s\n' "$key" >"$config/mistral_api_key")
  say "saved key to $config/mistral_api_key (mode 600)"
}

need() {
  command -v "$1" >/dev/null && return 0
  if [ -n "$2" ]; then die "$1 is required. Install it with: $2"; fi
  die "$1 is required"
}
say() { printf 'claudette: %s\n' "$*"; }
warn() { printf 'claudette: warning: %s\n' "$*" >&2; }
die() { printf 'claudette: error: %s\n' "$*" >&2; exit 1; }

main "$@"
