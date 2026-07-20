#!/usr/bin/env bash
#
# One command to go from a bare checkout to a running agent.
#
# Everything here is a precondition the agent needs anyway. Checking them in
# order means a failure names the one thing that is wrong, instead of a
# traceback from three layers down — the IMAP smoke test is separate from the
# model check for exactly that reason.
#
#   ./run.sh          preflight, then start the agent
#   ./run.sh --check  preflight only, exit without starting

set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m %s\n' "$1"; }
die()  { bad "$1"; [ $# -gt 1 ] && printf '       %s\n' "$2"; exit 1; }

# ---------------------------------------------------------------- 1. python
step "1/4  Environment"
if [ ! -x "$PY" ]; then
    printf '  creating .venv...\n'
    python3 -m venv .venv
fi
ok "virtualenv"

# Reinstall only when requirements.txt is newer than the marker we drop after
# a successful install. Saves ~3s on every subsequent run.
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
    printf '  installing dependencies...\n'
    $PY -m pip install -q -r requirements.txt
    touch .venv/.installed
fi
ok "dependencies"

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

if ! curl -sf --max-time 5 "$base/models" >/dev/null 2>&1; then
    die "no server at $base" \
        "Open LM Studio -> Developer tab -> Start Server."
fi
ok "server reachable"

# A loaded model whose id does not match MODEL is the most common silent
# failure: the server answers, the request 404s, and the error is opaque.
if ! curl -sf --max-time 5 "$base/models" | grep -q "\"$model\""; then
    printf '  \033[33mwarn\033[0m MODEL="%s" is not in the loaded model list\n' "$model"
    printf '       loaded: %s\n' "$(curl -sf "$base/models" | $PY -c \
        'import json,sys; print(", ".join(m["id"] for m in json.load(sys.stdin)["data"]))')"
else
    ok "model $model loaded"
fi

if [ "${1:-}" = "--check" ]; then
    printf '\n\033[32mAll checks passed.\033[0m\n'
    exit 0
fi

step "Starting agent"
exec $PY agent.py
