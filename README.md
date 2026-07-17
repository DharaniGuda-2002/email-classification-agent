# email-agent

A local model reads your inbox over IMAP. Read-only.

Scope for now: extract emails, pass them to the agent. Nothing else.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

`.env` holds five variables — see `.env.example` for how to obtain each:

```bash
EMAIL_PASS=abcdefghijklmnop            # SECRET — full read access to your mailbox
EMAIL_USER=you@gmail.com
IMAP_HOST=imap.gmail.com               # change for other providers
LM_BASE_URL=http://localhost:1234/v1   # LM Studio server
MODEL=qwen2.5-7b-instruct              # must match the loaded model exactly
```

Gmail needs an **App Password**, not your account password — enable 2FA first,
then generate one at `myaccount.google.com/apppasswords`.

LM Studio: load a tool-capable model, Developer tab → Start Server.

## Run

```bash
python email_tool.py      # IMAP smoke test, no model involved
python agent.py           # the agent
```

Get the smoke test printing headers before touching the agent.