#!/usr/bin/env bash
#
# The script a Siri Shortcut runs. Prints ONE line, nothing else.
#
# Paste the absolute path to this file into a Shortcuts "Run Shell Script"
# action — see README.md in this folder.
#
# Why a script instead of pasting commands into the Shortcut:
#
#   - it finds the project itself, so moving or renaming the folder does not
#     silently break your Shortcut
#   - Shortcuts runs with a minimal PATH, and everything here is absolute
#   - failures come back as one speakable sentence rather than a stack trace
#     read aloud, or worse, silence

set -uo pipefail

# This file lives in <project>/shortcuts/, so the project is one level up.
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || { echo "Could not find the email agent folder."; exit 1; }

# Shortcuts does not source your shell profile, so Homebrew and the LM Studio
# CLI are not on PATH. Add the usual locations explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.lmstudio/bin:$PATH"

OUTPUT=$(./run.sh --brief 2>/dev/null | tail -1)

# An empty result means the preflight failed before the brief ever printed —
# usually LM Studio missing, or credentials not set. Say something out loud
# rather than leaving Siri silent.
if [ -z "$OUTPUT" ]; then
    echo "Could not check your email. The mail agent could not start."
    exit 1
fi

echo "$OUTPUT"
