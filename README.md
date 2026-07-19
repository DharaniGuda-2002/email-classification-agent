# email-agent

A local model triages your inbox over IMAP. Read-only.

Ask it "what needs my attention?" and it groups unread mail into what to act
on, what to know, and a count of noise — with a one-line summary each.

Categories (promotions, social, updates, spam) come from Gmail's own
classifier, not from the model guessing at subject lines. Priority is computed
from headers: starred, Gmail's `\Important`, and `List-Unsubscribe`, which by
RFC 2369 means bulk sender.

### Kinds

Built for a student job hunt. Each email also gets a `[kind]`, computed in
Python from its body:

| kind | means | lands in |
|---|---|---|
| `action` | interview, assessment, coursework deadline, or a real person writing to you | **Needs a reply** |
| `rejection` | application turned down | **Rejections** |
| `confirmation` | application acknowledged | one collapsed line |

A rejection never appears under "Needs a reply" — it asks nothing of you.

This has to read the body: "Thanks for your interest in NRI" is a rejection
and its subject gives you nothing. Scanning subjects alone found 0 rejections
in 426 unread; scanning bodies found them immediately.

Detection is patterns plus header structure, so it is good but not perfect.
Unusual phrasing will slip through. "A real person wrote to you" is decided
structurally — no `List-*` or `Feedback-ID` bulk headers, not a role address
like `careers@` — because marketing from Starbucks and AliExpress is addressed
to you by name and reads personal otherwise.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

```bash
EMAIL_USER=you@gmail.com
IMAP_HOST=imap.gmail.com               # change for other providers
LM_BASE_URL=http://localhost:1234/v1   # LM Studio server
MODEL=qwen2.5-7b-instruct              # must match the loaded model exactly

EMAIL_PASS=abcdefghijklmnop            # app-password mode; omit to use OAuth
```

LM Studio: load a tool-capable model, Developer tab → Start Server.

### How far back

Defaults to the **last 24 hours**, unread only. Ask for more in conversation —
"the last three days", "anything I missed this week" — and the agent widens
the window. It prefers 3 days or fewer unless you say otherwise.

Rolling hours, not calendar days: at 9am, "1 day" means since 9am yesterday,
not nine hours of mail. IMAP's `SINCE` is date-granular, so the server narrows
to whole days and the exact cut happens on the `Date` header.

### How many emails

`MAX_EMAILS` (default 50) caps what one call can pull back. It is not there to
be tidy — a mailbox with 13k unread would put ~1.3M characters of headers into
a context window that holds a fraction of that, and the model starts dropping
or inventing entries well before it errors. Raise it as far as your model
actually holds; the limit is the model's, not this code's.

Past the cap, the lowest-priority mail is dropped first — promotions and
social before anything addressed to you — and the tool says what it dropped
("showing 15 of 41, omitted 3 promotions, 1 social, 22 updates") so a summary
never quietly implies it covered the whole inbox.

```bash
MAX_EMAILS=100
```

## Auth

An app password, in `EMAIL_PASS`:

1. Enable 2-Step Verification on your Google account (app passwords do not
   appear until you do).
2. Generate one at `myaccount.google.com/apppasswords`.
3. Put it in `.env`. Spaces are stripped for you.

Note what this is *not*: your account password plus a 2FA code. Google turned
off basic auth for Gmail, and IMAP could not carry an interactive challenge
anyway — a single LOGIN command, no round trip to prompt you on. The app
password is the supported substitute for exactly that situation, and the 2FA
you enabled to create it is what backs it.

It never expires. The cost is a long-lived secret on disk with full mailbox
access; revoke it by deleting it from that page. `.env` is gitignored.

Read-only is enforced here rather than by the credential — `readonly=True`,
`BODY.PEEK`, and no send or delete tool existing in the process.

If Gmail rejects it, check IMAP is enabled: Gmail Settings → Forwarding and
POP/IMAP → Enable IMAP.

## Run

```bash
python email_tool.py      # IMAP smoke test, no model involved
python agent.py           # the agent
```

Get the smoke test printing headers before touching the agent.