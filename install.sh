#!/usr/bin/env bash
# SushiStack one-script installer (Linux / WSL).
#
# Bootstraps Python, pip, and Git, clones the SushiStack workspace, installs the
# `ss` CLI, then provisions the shared dependency tree with `ss install`. The
# portable CMake/Ninja and the SYCL toolchains are downloaded by `ss install`
# into <workspace>/dependencies, so only Python and Git need bootstrapping here.
#
# `ss install` provisions everything (all SYCL toolchains + CUDA) — SYCL is heavy
# by nature and a missing toolchain only causes confusion. To choose a subset,
# run `ss install --customize` interactively after this script.
#
# Supports Debian/Ubuntu (apt), Fedora/RHEL (dnf/yum), Arch (pacman), and
# openSUSE (zypper).
#
# Module checkouts (cloned into the workspace after deps are provisioned):
#   --add "sushiruntime sushiengine"  space- or comma-separated list (default: none)
#
# Usage (bare machine):
#   curl -fsSL https://sushisystems.io/install.sh | bash -s -- --add "sushiruntime sushiengine"
#
# Usage (inside a checkout):
#   bash install.sh [--add "..."] [--dry-run]
set -euo pipefail

REPO_URL="${SUSHISTACK_REPO_URL:-https://github.com/sushisystems/sushistack.git}"
DRY_FLAG=""
MODULES=""
expect_add=0
for arg in "$@"; do
  if [ "$expect_add" -eq 1 ]; then MODULES="$arg"; expect_add=0; continue; fi
  case "$arg" in
    --add)         expect_add=1 ;;
    --add=*)       MODULES="${arg#*=}" ;;
    --dry-run)     DRY_FLAG="--dry-run" ;;
    *) printf '[WARN] unknown argument: %s\n' "$arg" ;;
  esac
done
# Normalise commas to spaces so `--add a,b` and `--add "a b"` both work.
MODULES="${MODULES//,/ }"

log() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

bootstrap_apt()    { log "Installing Python and Git via apt...";    $SUDO apt-get update -qq && $SUDO apt-get install -y python3 python3-pip git; }
bootstrap_dnf()    { log "Installing Python and Git via dnf...";    $SUDO dnf install -y python3 python3-pip git; }
bootstrap_yum()    { log "Installing Python and Git via yum...";    $SUDO yum install -y python3 python3-pip git; }
bootstrap_pacman() { log "Installing Python and Git via pacman..."; $SUDO pacman -Sy --noconfirm python python-pip git; }
bootstrap_zypper() { log "Installing Python and Git via zypper..."; $SUDO zypper install -y python3 python3-pip git; }

# Only Python and Git need bootstrapping; everything else is downloaded by `ss
# install` into the shared dependencies/ tree.
need_bootstrap=0
for tool in python3 git; do
  command -v "$tool" >/dev/null 2>&1 || need_bootstrap=1
done
python3 -m pip --version >/dev/null 2>&1 || need_bootstrap=1

if [ "$need_bootstrap" -eq 1 ]; then
  if   command -v apt-get  >/dev/null 2>&1; then bootstrap_apt
  elif command -v dnf      >/dev/null 2>&1; then bootstrap_dnf
  elif command -v yum      >/dev/null 2>&1; then bootstrap_yum
  elif command -v pacman   >/dev/null 2>&1; then bootstrap_pacman
  elif command -v zypper   >/dev/null 2>&1; then bootstrap_zypper
  else
    err "No supported package manager found (apt, dnf, yum, pacman, zypper). Install python3, pip, and git manually, then re-run."
  fi
fi

# Locate or clone the workspace. The SushiStack repo is identified by its
# cli/manifests tree (it ships no CMakeLists.txt).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -d "$SCRIPT_DIR/cli/manifests" ]; then
  WORKSPACE_DIR="$SCRIPT_DIR"
else
  WORKSPACE_DIR="${SUSHISTACK_DIR:-$HOME/sushistack}"
  if [ ! -d "$WORKSPACE_DIR/.git" ]; then
    log "Cloning $REPO_URL -> $WORKSPACE_DIR"
    git clone "$REPO_URL" "$WORKSPACE_DIR"
  fi
fi
cd "$WORKSPACE_DIR"
log "Workspace: $WORKSPACE_DIR"

# Ensure the shared CLI presentation layer (sushicli) is present. It is not
# published to any index, so every Sushi* CLI injects it from a checkout. The
# umbrella fetches it into the workspace so an end user never handles it; a
# developer can override with SUSHICLI_DIR or `ss link sushicli <path>`.
SUSHICLI_REPO_URL="${SUSHICLI_REPO_URL:-https://github.com/sushisystems/sushicli.git}"
if [ -n "${SUSHICLI_DIR:-}" ]; then
  log "Using sushicli from SUSHICLI_DIR: $SUSHICLI_DIR"
elif [ -d "$WORKSPACE_DIR/sushicli/.git" ]; then
  log "Updating sushicli in workspace"
  git -C "$WORKSPACE_DIR/sushicli" pull --ff-only || log "sushicli pull skipped"
elif [ -f "$WORKSPACE_DIR/../sushicli/pyproject.toml" ]; then
  log "Using sibling sushicli checkout: $WORKSPACE_DIR/../sushicli"
else
  log "Cloning sushicli -> $WORKSPACE_DIR/sushicli"
  git clone "$SUSHICLI_REPO_URL" "$WORKSPACE_DIR/sushicli"
fi

# Install the ss CLI.
log "Installing the ss CLI..."
python3 cli/install.py

PIPX_BIN_DIR=$(python3 -m pipx environment --value PIPX_BIN_DIR)
SS_CMD="$PIPX_BIN_DIR/ss"
if [ ! -x "$SS_CMD" ]; then SS_CMD="ss"; fi

# Mark the workspace, then provision the shared dependency tree (everything).
"$SS_CMD" init
log "Running: ss install $DRY_FLAG"
"$SS_CMD" install $DRY_FLAG
SS_EXIT=$?
if [ "$SS_EXIT" -ne 0 ]; then exit "$SS_EXIT"; fi

# Optionally clone the requested modules into the workspace.
if [ -n "$MODULES" ]; then
  log "Adding modules: $MODULES"
  # shellcheck disable=SC2086
  "$SS_CMD" add $MODULES
fi

log "Done. Workspace ready at $WORKSPACE_DIR"
if [ -z "$MODULES" ]; then
  log "Next: ss add sushiruntime   (then: cd sushiruntime && sr build)"
fi
