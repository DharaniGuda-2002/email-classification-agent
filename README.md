# inbox-triage

A local LLM triages your Gmail. Read-only, runs entirely on your machine, no
email content leaves it.

Built for a student job hunt — it separates **rejections** from things that
actually **need a reply**, so a "Thanks for your interest" doesn't sit in the
same pile as an interview invite.

```
Reading your inbox...

### Needs a reply
None.

### Rejections
Grifols, U.S. Data Engineer (544250)
STV, role not specified

### Worth knowing
* Google security alert — an app password was created on your account.

### Confirmations
4 applications acknowledged (Nscale, Starbucks, Workday x2)

### Noise
21 (job alerts, promotions, social)

Open in Gmail:
  15. [rejection] Thank You For Your Interest In Grifols
      https://mail.google.com/mail/u/you@gmail.com/#all/19f7b02065a8d740

You: what about the last 3 days?
```

---

## Quickstart

You need [LM Studio](https://lmstudio.ai) with a tool-capable model loaded
(Qwen 2.5 7B Instruct works well), and a Gmail account.

```bash
git clone <your-repo-url> && cd inbox-triage
./run.sh
```

The first run creates `.env` and stops. Fill in three values:

```bash
EMAIL_USER=you@gmail.com
EMAIL_PASS=abcdefghijklmnop        # app password, see below
MODEL=qwen2.5-7b-instruct          # must match the model loaded in LM Studio
```

Then `./run.sh` again. That's it.

### Getting the app password

Not your Google password — a 16-character app password:

1. Enable 2-Step Verification on your Google account (app passwords do not
   appear until you do).
2. Generate one at
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Paste it into `.env`. Spaces are stripped for you.

If Gmail rejects it: Gmail Settings → Forwarding and POP/IMAP → **Enable
IMAP**.

### In LM Studio

Download a tool-capable model. You do **not** need to start the server or
load the model by hand — if LM Studio's `lms` CLI is installed (run
`lms bootstrap` once), `run.sh` starts the server and loads `MODEL` for you.

The model id must match `MODEL` in `.env` exactly. If it does not, `run.sh`
tries to load it and lists what you actually have.

---

## Using it

```bash
./run.sh              # preflight checks, then the agent
./run.sh --once       # triage once and exit — for cron
./run.sh --check      # checks only, don't start
./run.sh --test       # run the test suite
```

Ask it anything:

```
what needs my attention?
summarize my unread mail
what came in the last 3 days?
anything in promotions worth keeping?
check my spam for real mail
```

Three commands are handled in code rather than by the model:

| command | does |
|---|---|
| `links` | Gmail links for every email in the last listing |
| `3 is a rejection` | teaches it a tag it got wrong |
| `help` | shows the banner again |

### Asking Siri

```bash
./run.sh --shortcut
```

Generates a **signed** "Check My Emails" shortcut and offers to install it —
click **Add Shortcut** once, no building by hand. Then "Hey Siri, Check My
Emails" runs the agent and speaks a one-line summary back:

```
26 new emails. Nothing needs a reply. 1 rejection: grifols.
1 application acknowledged. 24 others.
```

That paragraph (`--brief`) takes about **20 seconds** — the counts and tags
are computed in code, so it skips the slow part of a full triage, which is the
model writing prose nobody needs spoken at them.

Setup, timing, and troubleshooting are in **[shortcuts/](shortcuts/)**.

**You do not need to start the server yourself.** If `run.sh` finds the `lms`
CLI (shipped with LM Studio, at `~/.lmstudio/bin/lms`) it starts the server
and loads `MODEL` before doing anything else. Without that CLI it falls back
to telling you to press Start Server, which is no use to a shortcut —
`lms bootstrap` puts it on your PATH.

What it costs, measured on an 8.97 GB model:

| state | time |
|---|---|
| server up, model loaded | ~9s |
| server up, model unloaded | ~21s |
| server stopped | ~9s (plus load if needed) |

LM Studio unloads an idle model after its TTL, 60 minutes by default. So a
once-a-day Siri question usually pays the 21 seconds, not the 9. Raising the
TTL in LM Studio keeps it resident and fast, at the cost of holding the
model in RAM.

Quitting the LM Studio app entirely is untested — `lms` is a separate binary
and is expected to bring the backend up on its own, but confirm it on your
machine before relying on it.

### A daily digest

`--once` prints and exits, so cron works:

```bash
0 8 * * * cd /path/to/inbox-triage && ./run.sh --once >> ~/triage.log 2>&1
```

---

## How it works

```
run.sh          preflight: venv, .env, IMAP login, model server
  └── agent.py         model loop, prompt, tool schemas
       ├── email_tool.py    IMAP, categories, priority, kind tags
       └── classifier.py    model-judged tags + your corrections
```

The model gets exactly two tools — `read_emails` and `read_email_body`. There
is no send, no delete, no shell, no file access. Not as a rule it is asked to
follow: **those tools do not exist in the process.**

### What is decided in code, not by the model

The model is good at summarising and bad at being consistent, so the facts
are computed before it ever sees them:

**Category** — promotions / social / updates / spam come from Gmail's own
classifier via `X-GM-RAW` search. Gmail already ran that model; a 7B redoing
it would be slower and worse.

**Priority** — HIGH / NORMAL / LOW from header facts: starred, Gmail's
`\Important`, and `List-Unsubscribe`, which by RFC 2369 means bulk sender.

**Kind** — the job-hunt part:

| kind | means | lands in |
|---|---|---|
| `action` | interview, assessment, coursework deadline, or a real person writing to you | **Needs a reply** |
| `rejection` | application turned down | **Rejections** |
| `confirmation` | application acknowledged | one collapsed line |

A rejection never appears under "Needs a reply" — it asks nothing of you.

### How a kind is decided

Patterns run first: instant, free, deterministic. The model is then asked
about whatever is left, one email at a time, answering a single word
(~0.7s each).

Each covers the other's weakness. Patterns never drift; the model handles
wording nobody wrote a rule for:

```
"we have chosen to go in a different direction"   -> rejection
"the role has been filled internally"             -> rejection
"can you send me your draft chapter?"             -> action
```

Detecting a rejection requires reading the **body**. "Thanks for your interest
in NRI" tells you nothing — scanning subjects found 0 rejections in 426
unread; scanning bodies found them immediately.

### Correcting it

```
You: 3 is a rejection
Noted: #3 is 'rejection', not 'untagged'. Saved as an example.
```

Saved to `.corrections.json` and included as an example in every future
classification, so it applies from the next email onward.

**Be clear about what this is: the model is not trained and its weights never
change.** Corrections are examples pasted into a prompt. That is why one
correction works immediately instead of needing hundreds of labels and a GPU
run — and why deleting `.corrections.json` reverts the behaviour exactly.

### Links

After each answer, Gmail links print for anything important. Built in code
from `X-GM-THRID` — deliberately never shown to the model, because a small
model garbles long URLs when repeating them, and a subtly wrong link is worse
than none. The numbers match what the model saw.

---

## Configuration

Everything optional lives in `.env`:

| variable | default | does |
|---|---|---|
| `EMAIL_USER` | — | your address |
| `EMAIL_PASS` | — | app password |
| `MODEL` | — | must match LM Studio exactly |
| `IMAP_HOST` | `imap.gmail.com` | change for other providers |
| `LM_BASE_URL` | `http://localhost:1234/v1` | LM Studio server |
| `MAX_EMAILS` | `50` | ceiling per call |
| `CORRECTIONS_FILE` | `.corrections.json` | where corrections live |

### How far back

Defaults to the **last 24 hours**, unread only. Ask for more in conversation
and it widens. Rolling hours, not calendar days: at 9am, "1 day" means since
9am yesterday.

### How many

`MAX_EMAILS` is not there to be tidy. A mailbox with 13k unread would put
~1.3M characters of headers into a context window that holds a fraction of
that, and the model starts dropping or inventing entries well before it
errors. Raise it as far as your model actually holds.

Past the cap, lowest-priority mail is dropped first and the tool says what it
dropped — so a summary never quietly implies it covered everything.

---

## Privacy and safety

- **Read-only, structurally.** `readonly=True` and `BODY.PEEK` mean the agent
  cannot mark mail as read, let alone change it.
- **Nothing leaves your machine.** IMAP to your provider, HTTP to localhost.
  No third-party API, no telemetry.
- **Email content is treated as hostile.** Bodies are fenced in
  `UNTRUSTED` markers and the model is told they are data, never
  instructions. Since it has no tools that can act, an injected email has
  nothing to reach for.
- `.env` and `.corrections.json` are gitignored. Corrections contain email
  subjects and snippets — keep them local.

---

## Other providers

The IMAP layer is generic; the Gmail-specific parts degrade rather than break:

| feature | non-Gmail |
|---|---|
| listing, priority, kinds | works |
| categories | everything reads as `primary` |
| spam folder | needs the right folder name in `SPAM_FOLDER` |
| Gmail links | absent |

Set `IMAP_HOST` and try it — `python email_tool.py` is a mailbox-only smoke
test with no model involved.

---

## Tests

```bash
./run.sh --test
```

51 tests, no network or mailbox required. Most are regressions — each one is
a bug that shipped and was only caught by running against a real inbox:

- `re.VERBOSE` strips literal spaces, so `other candidates` compiled as
  `othercandidates` and matched nothing
- Gmail returns FETCH replies in ascending id while we ask newest-first, so
  zipping the lists paired every snippet with the **wrong email**
- HTML mislabelled as `text/plain` ate the snippet budget before reaching the
  sentence that mattered
- `no_reply@` with an underscore slipped past the role-address check

---

## License

MIT — see [LICENSE](LICENSE).
