#!/usr/bin/env bash
#
# One command to go from a bare checkout to a running agent.
#
# Everything here is a precondition the agent needs anyway. Checking them in
# order means a failure names the one thing that is wrong, instead of a
# traceback from three layers down — the IMAP smoke test is separate from the
# model check for exactly that reason.
#
#   ./run.sh           preflight, then start the agent
#   ./run.sh --check   preflight only, exit without starting
#   ./run.sh --test    preflight, then run the test suite
#   ./run.sh --once    triage once and exit (for cron)

set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python

# Generate and offer to install the Siri shortcut. Needs no preflight, so it
# runs before the checks — you can build the shortcut without a model loaded.
if [ "${1:-}" = "--shortcut" ]; then
    "$PY" shortcuts/make_shortcut.py || exit 1
    printf '\nOpen them now to install? [y/N] '
    read -r reply
    if [ "$reply" = "y" ] || [ "$reply" = "Y" ]; then
        # One at a time: Shortcuts shows a modal per import, and opening both
        # at once stacks them so the second looks like nothing happened.
        open "Check My Emails.shortcut"
        printf 'Click Add Shortcut, then press Return for the second one. '
        read -r _
        open "Ask My Email.shortcut"
    fi
    exit 0
fi

# Progress goes to stderr; stdout is reserved for the agent's own output, so a
# caller can pipe it clean — the Siri brief is multi-line and must not be
# mixed with preflight noise.
step() { printf '\n\033[1m%s\033[0m\n' "$1" >&2; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1" >&2; }
bad()  { printf '  \033[31mfail\033[0m %s\n' "$1" >&2; }
die()  { bad "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2" >&2; exit 1; }

# ---------------------------------------------------------------- 1. python
step "1/4  Environment"
if [ ! -x "$PY" ]; then
    printf '  creating .venv...\n' >&2
    python3 -m venv .venv
fi
ok "virtualenv"

# Reinstall only when requirements.txt is newer than the marker we drop after
# a successful install. Saves ~3s on every subsequent run.
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
    printf '  installing dependencies...\n' >&2
    $PY -m pip install -q -r requirements.txt
    touch .venv/.installed
fi
ok "dependencies"

# Tests need dependencies and nothing else — no mailbox, no model, no .env.
# Gated here rather than at the end so someone who has just cloned the repo
# can verify it before handing over a password.
if [ "${1:-}" = "--test" ]; then
    step "Tests"
    exec $PY test_agent.py
fi

# ------------------------------------------------------------------ 2. .env
step "2/4  Configuration"
if [ ! -f .env ]; then
    cp .env.example .env
    die ".env was missing — created one from .env.example" \
        "Fill in EMAIL_USER and EMAIL_PASS, then run again."
fi

missing=$($PY - <<'EOF'
import os
from dotenv import load_dotenv
# Explicit path: find_dotenv() walks the call stack to locate .env, and there
# is no stack to walk when the script arrives on stdin.
load_dotenv(".env")
print(" ".join(v for v in ("EMAIL_USER", "EMAIL_PASS", "MODEL")
                 if not os.environ.get(v)))
EOF
)
[ -n "$missing" ] && die "missing in .env: $missing" "See .env.example."
ok ".env"

# ------------------------------------------------------------------ 3. IMAP
step "3/4  Mailbox"
if ! $PY - <<'EOF'
import sys, email_tool
try:
    m = email_tool._connect()
    m.logout()
except Exception as exc:
    print(f"       {exc}", file=sys.stderr)
    sys.exit(1)
EOF
then
    die "cannot reach your mailbox" \
        "Check EMAIL_PASS is an app password, and that IMAP is enabled in Gmail."
fi
ok "IMAP login"

# -------------------------------------------------------------- 4. LM Studio
step "4/4  Model"
url=$($PY -c "import agent; print(agent.ENDPOINT)")
model=$($PY -c "import agent; print(agent.MODEL)")
base=${url%/chat/completions}

# LM Studio ships a CLI. If it is there, bring the server up rather than
# telling a human to go and click Start Server — a Siri shortcut or a cron
# job has nobody to read that message.
LMS=$(command -v lms || echo "$HOME/.lmstudio/bin/lms")

if ! curl -sf --max-time 5 "$base/models" >/dev/null 2>&1; then
    if [ -x "$LMS" ]; then
        printf '  starting LM Studio server...\n' >&2
        "$LMS" server start >/dev/null 2>&1 || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            curl -sf --max-time 2 "$base/models" >/dev/null 2>&1 && break
            sleep 1
        done
    fi
fi

if ! curl -sf --max-time 5 "$base/models" >/dev/null 2>&1; then
    die "no server at $base" \
        "Open LM Studio -> Developer tab -> Start Server."
fi
ok "server reachable"

# A loaded model whose id does not match MODEL is the most common silent
# failure: the server answers, the request 404s, and the error is opaque.
if curl -sf --max-time 5 "$base/models" | grep -q "\"$model\""; then
    ok "model $model loaded"
elif [ -x "$LMS" ]; then
    # Loading several GB takes a while, so only do it when it is missing.
    printf '  loading %s (this can take a minute)...\n' "$model" >&2
    if "$LMS" load "$model" >/dev/null 2>&1; then
        ok "model $model loaded"
    else
        printf '  \033[33mwarn\033[0m could not load "%s"\n' "$model" >&2
        printf '       available: %s\n' "$("$LMS" ls 2>/dev/null | \
            awk 'NF && !/^(You have|LLM|EMBEDDING|$)/ {print $1}' | paste -sd, -)" >&2
    fi
else
    printf '  \033[33mwarn\033[0m MODEL="%s" is not in the loaded model list\n' "$model" >&2
    printf '       loaded: %s\n' "$(curl -sf "$base/models" | $PY -c \
        'import json,sys; print(", ".join(m["id"] for m in json.load(sys.stdin)["data"]))')" >&2
fi

if [ "${1:-}" = "--check" ]; then
    printf '\n\033[32mAll checks passed.\033[0m\n' >&2
    exit 0
fi

step "Starting agent"
exec $PY agent.py "$@"
