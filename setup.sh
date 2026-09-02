#!/usr/bin/env bash
# ============================================================================
# setup.sh — one-shot setup for a new server instance.
#
# Usage:      bash setup.sh        (from the repo root; run as root)
#             Idempotent: safe to re-run.
#
# Automates the previous manual startup sequence, ordered for speed:
#   1. GITHUB_TOKEN: exported for this run AND persisted to ~/.bashrc.
#   2. apt: ONE session does update + install (single dpkg lock, quiet,
#      noninteractive).
#   3. pip: requirements.txt + playwright in a SINGLE pass (one resolver
#      run, not two).
#   4. Playwright: the ~150 MB Chromium browser downloads in the background
#      while the apt-based system-library install runs — the two overlap.
# ============================================================================
set -euo pipefail

# Repo root (this script lives there).
cd "$(dirname "$0")"

# ---------------------------------------------------------------------------
# GITHUB_TOKEN — same value as the previous manual export.
# Pre-setting the GITHUB_TOKEN env var overrides the hard-coded default.
# ---------------------------------------------------------------------------
GITHUB_TOKEN="${GITHUB_TOKEN:-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}"
export GITHUB_TOKEN

# Re-exec with sudo if not root (preserving GITHUB_TOKEN).
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo env GITHUB_TOKEN="$GITHUB_TOKEN" bash "$0" "$@"
    fi
    echo "ERROR: please run this as root (sudo not found)." >&2
    exit 1
fi

# Persist the token for future shells (idempotent: one line, only if absent).
if ! grep -qs 'GITHUB_TOKEN' "$HOME/.bashrc" 2>/dev/null; then
    echo "export GITHUB_TOKEN=\"$GITHUB_TOKEN\"" >> "$HOME/.bashrc"
fi

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# 1) System packages — a single apt session (one lock, one pass).
# ---------------------------------------------------------------------------
apt-get update -qq
apt-get install -qq -y \
    gh nano python3-pip git-all python3 \
    libavformat60 libswscale7

# ---------------------------------------------------------------------------
# 2) Python dependencies — one pip pass for repo requirements + playwright.
#    On PEP 668-managed systems the first attempt fails fast; the fallback
#    uses --break-system-packages.
# ---------------------------------------------------------------------------
if [ -f requirements.txt ]; then
    python3 -m pip install --quiet -r requirements.txt playwright \
        || python3 -m pip install --quiet --break-system-packages \
           -r requirements.txt playwright
else
    python3 -m pip install --quiet playwright \
        || python3 -m pip install --quiet --break-system-packages playwright
fi

# ---------------------------------------------------------------------------
# 3) Playwright — overlap the browser download with the apt-based library
#    install (the download needs no apt lock).
# ---------------------------------------------------------------------------
playwright install chromium &      # background: download only
PW_BROWSER=$!
playwright install-deps chromium   # foreground: apt-based
wait "$PW_BROWSER"

echo "setup complete: packages, python deps and playwright are ready."
