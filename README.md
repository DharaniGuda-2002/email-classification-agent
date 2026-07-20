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

### How a kind is decided

Patterns run first — instant, free, deterministic. The model is then asked
about whatever they left untagged, one email at a time, answering a single
word. That costs about 0.7s per untagged email.

The split exists because each is good at what the other is not. Patterns
never drift and cost nothing; the model handles wording nobody wrote a rule
for. Real examples the patterns miss and the model catches:

```
"we have chosen to go in a different direction"   -> rejection
"the role has been filled internally"             -> rejection
"can you send me your draft chapter?"             -> action
```

### Correcting it

When it gets one wrong, tell it:

```
You: 3 is a rejection
Noted: #3 is 'rejection', not 'untagged'. Saved as an example.
```

The correction is stored in `.corrections.json` and included as an example in
every future classification, so it applies from the next email onward.

Be clear about what this is: **the model is not trained and its weights never
change.** Corrections are examples pasted into a prompt. That is why one
correction works immediately instead of needing hundreds of labels and a GPU
run — and why deleting `.corrections.json` reverts the behaviour exactly.

Real fine-tuning is possible but a different project: a few hundred labelled
emails, a LoRA run, and reloading the tuned weights. Not worth it while a
prompt gets these right.

Detection is patterns plus header structure plus the model, so it is good but
not perfect.
Unusual phrasing will slip through. "A real person wrote to you" is decided
structurally — no `List-*` or `Feedback-ID` bulk headers, not a role address
like `careers@` — because marketing from Starbucks and AliExpress is addressed
to you by name and reads personal otherwise.

## Setup

```bash
./run.sh
```

That is the whole thing. It creates the virtualenv, installs dependencies,
creates `.env` from the template if it is missing, checks your mailbox login
and the model server, then starts the agent. Each check names the one thing
that is wrong rather than failing with a traceback.

```bash
./run.sh --check    # run the checks, don't start the agent
```

First run will stop and ask you to fill in `.env`. Everything below is what
those values mean; you do not need to run any of it by hand.

<details>
<summary>Doing it manually</summary>

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in
```
</details>

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
./run.sh
```

The individual pieces, if you are debugging one layer:

```bash
python email_tool.py      # IMAP smoke test, no model involved
python agent.py           # the agent, skipping all preflight checks
```

Get the smoke test printing headers before touching the agent.