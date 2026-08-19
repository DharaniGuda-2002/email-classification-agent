# inbox-triage

A local LLM triages your Gmail. Runs entirely on your machine, no email
content leaves it. It marks the mail you've seen as read — unless it's
important.

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

One command for everything:

```bash
./mail                    # start chatting with your inbox
./mail brief              # one short summary, fast
./mail once "any interviews?"   # a single question, then exit
./mail log                # what Siri asked and answered
./mail desktop            # put a double-clickable launcher on the Desktop
./mail help               # every command
```

`./mail` opens a colored, readline-enabled terminal chat — arrow keys scroll
back through what you typed, and a status line ("Reading your inbox…") shows
what it is doing while the model works. Prefer an app to click? `./mail
desktop` drops a **Mail Agent.command** launcher on your Desktop that opens
the chat in a Terminal window (re-run it if you move the folder). Colours turn
off automatically when output is piped, so Siri and cron never see them.

Ask it anything:

```
what needs my attention?
how many rejections do I have?
anything from Google?
what came in the last 3 days?
check my spam for real mail
```

### It remembers

The chat keeps context, so follow-ups work:

```
You: how many rejections do I have?
Agent: Three: Adobe, Notion, Hinge Health.

You: what about the last 3 days?
Agent: Seven over three days: Adobe, Notion, Hinge Health, Stripe, …
```

The second question names no subject — it carries the first one forward.
Conversations expire after 15 minutes, because a "today" from this morning
should not still be in context tonight. Say `new` to start fresh, or
`./mail forget`.

Four commands are handled in code rather than by the model, so email text can
never trigger them:

| command | does |
|---|---|
| `links` | Gmail links for every email in the last listing |
| `3 is a rejection` | teaches it a tag it got wrong |
| `new` | forget the conversation and start over |
| `help` | shows the banner again |

The same applies to scheduling (see [A daily digest](#a-daily-digest)) — these
are parsed in code, not sent to the model:

| command | does |
|---|---|
| `check every 2 hours` | install a launchd job on that interval |
| `schedule every 30 minutes` | same, any plain-English interval (`h`, `m`, `mins`) |
| `status` | show the current schedule |
| `stop` / `cancel checking` | remove the launchd job |

### It marks what you've seen as read

After each summary, the mail it showed is marked **read** in Gmail — so it
stops reappearing the next day. Except the mail that matters: anything tagged
needs-a-reply, or rated HIGH priority, stays **unread** so it still stands
out. Rejections, confirmations and noise are marked read. Set
`MARK_READ=false` in `.env` and the agent is strictly read-only again.

### Asking Siri

```bash
./mail shortcut
```

Generates **two signed shortcuts** and offers to install them — click **Add
Shortcut** once each, no building by hand.

**"Hey Siri, Check My Emails"** — the fixed summary, on screen in ~20s:

```
26 new emails today.

Needs a reply (1):
• Acme — Interview Thursday

Rejections (2): grifols, newsela
12 applications acknowledged.
```

Fast because the counts and tags are computed in code, skipping the slow part
of a triage — the model writing prose nobody needs.

**"Hey Siri, Ask My Email"** — a conversation. Siri asks what you want, you
say *"any interviews this week?"*, and it answers. It shares the same memory
as the terminal chat, so follow-ups work by voice too.

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

**From chat or voice** — just say it and the schedule is set:

- "check every 2 hours"
- "schedule every 30 minutes"
- "check every 4h"
- "status" — what's the current schedule?
- "stop" / "cancel checking" — turn it off

This uses macOS **launchd** (a LaunchAgent) rather than cron: it survives
reboots, and a Mac that was asleep at the appointed minute still runs the job
when it wakes. The job fires `./mail brief --notify`, which checks your inbox
and raises a macOS notification if anything needs a reply.

**By hand** — `--once` prints and exits, so cron works too:

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
| `SPAM_FOLDER` | `[Gmail]/Spam` | where the mailbox keeps spam (non-Gmail only) |
| `MARK_READ` | `true` | mark summarized mail as read; `false` keeps the agent strictly read-only |

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

- **Read-only, structurally — with one deliberate write.** `BODY.PEEK` means
  fetching never sets `\Seen`, and the agent can't send, delete, or modify
  anything. The single exception: mail it summarizes is marked read unless it
  needs a reply or is HIGH priority. `MARK_READ=false` turns even that off.
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

133 tests, no network or mailbox required. Most are regressions — each one is
a bug that shipped and was only caught by running against a real inbox:

- `re.VERBOSE` strips literal spaces, so `other candidates` compiled as
  `othercandidates` and matched nothing
- Gmail returns FETCH replies in ascending id while we ask newest-first, so
  zipping the lists paired every snippet with the **wrong email**
- HTML mislabelled as `text/plain` ate the snippet budget before reaching the
  sentence that mattered
- `no_reply@` with an underscore slipped past the role-address check

---

## Documentation

Every feature, how the pieces fit together (with flowcharts), the tasks it
can do, and how to use them is in **[FEATURES.md](FEATURES.md)**.

---

## License

MIT — see [LICENSE](LICENSE).
