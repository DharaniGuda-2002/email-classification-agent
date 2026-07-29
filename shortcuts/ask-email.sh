#!/usr/bin/env bash
#
# The script the "Ask My Email" Siri shortcut runs. Reads the spoken question
# on stdin, prints the answer, nothing else.
#
# Separate from check-emails.sh because that one is the fixed fast summary;
# this one is a conversation — it passes --session so "what about the last
# three days?" resolves against the answer before it.

set -uo pipefail

# This file lives in <project>/shortcuts/, so the project is one level up.
PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT" || { echo "Could not find the email agent folder."; exit 1; }

# Shortcuts does not source your shell profile, so Homebrew and the LM Studio
# CLI are not on PATH. Add the usual locations explicitly.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.lmstudio/bin:$PATH"

QUESTION="$(cat)"
[ -z "$QUESTION" ] && QUESTION="What needs my attention?"

# run.sh sends preflight noise to stderr, so stdout is only the answer.
OUTPUT=$(./run.sh --once "$QUESTION" --session siri 2>/dev/null)

if [ -z "$OUTPUT" ]; then
    echo "Could not check your email. The mail agent could not start."
    exit 1
fi

echo "$OUTPUT"
