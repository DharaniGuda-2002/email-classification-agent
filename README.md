# inbox-triage

An email agent that runs entirely on your own Mac. It reads your inbox with a
local LLM, separates what needs a reply from what doesn't, tracks where your
job applications stand, and can tell you about it through Siri.

**No email content leaves your machine.** IMAP to your provider, HTTP to a
model running on localhost. No API keys, no cloud, no third party.

Built for a student job hunt: a "Thanks for your interest" and an interview
invite are not the same thing, and an inbox that mixes them costs you the
interview.

```
36 unread today

Needs a reply (1)
  • Lorvenk Technologies — Sign: Govin Internship 2026 Offer letter

Rejections (2)
  • TheGuarantors — Thank you for applying
  • Envoy — position has been filled

Confirmations (13)
  Applications acknowledged: LinkedIn, Wilson Elser, Analog Devices…

Noise (20)
  14 promotions, 4 updates, 2 social
```

---

## Contents

- [What you need](#what-you-need)
- [Setup](#setup)
- [Connecting your email](#connecting-your-email)
- [Connecting more than one mailbox](#connecting-more-than-one-mailbox)
- [Setting up the model](#setting-up-the-model)
- [What you can do](#what-you-can-do)
- [Writing an email](#writing-an-email)
- [Siri](#siri)
- [Running on a schedule](#running-on-a-schedule)
- [How it decides](#how-it-decides)
- [Configuration](#configuration)
- [Privacy and safety](#privacy-and-safety)
- [Troubleshooting](#troubleshooting)

---

## What you need

| | |
|---|---|
| **macOS** | The Siri shortcuts and scheduling use Apple tooling. The agent itself is portable. |
| **Python 3.9+** | Already on macOS. |
| **[LM Studio](https://lmstudio.ai)** | Free. Runs the model locally. |
| **A Gmail account** | With 2-Step Verification on — see below. |

Roughly 10 GB of disk for the model, and 8 GB of RAM free while it runs.

---

## Setup

```bash
git clone https://github.com/DharaniGuda-2002/email-classification-agent.git
cd email-classification-agent
./mail
```

The first run creates the virtualenv, installs dependencies, writes a `.env`
template, and stops to tell you what to fill in. Fill it in, run `./mail`
again, and you are chatting with your inbox.

Every check it runs is one the agent needs anyway, so a failure names the one
thing that is wrong instead of a traceback:

```
1/4  Environment     ok   virtualenv, dependencies
2/4  Configuration   ok   .env
3/4  Mailbox         ok   IMAP login
4/4  Model           ok   server reachable, model loaded
```

Run just the checks any time with `./mail check`.

---

## Connecting your email

Gmail will not accept your normal password over IMAP. You need a **16-character
app password**, which is a separate credential you can revoke without changing
anything else.

**1. Turn on 2-Step Verification**
[myaccount.google.com/security](https://myaccount.google.com/security) →
2-Step Verification. App passwords do not exist until this is on.

**2. Create the app password**
Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
Name it anything ("Mail agent"). Google shows you 16 characters like
`abcd efgh ijkl mnop`.

**3. Put it in `.env`**

```bash
EMAIL_USER=you@gmail.com
EMAIL_PASS=abcd efgh ijkl mnop     # spaces are fine, they get stripped
```

**4. Make sure IMAP is on**
Gmail → Settings → **Forwarding and POP/IMAP** → Enable IMAP. Newer accounts
have it on already.

That's it. `./mail check` will tell you if the login works.

> **What this credential can do:** an app password gives full mailbox access,
> and the same one is used for sending (see
> [Writing an email](#writing-an-email) — nothing goes out without you typing
> `send`). The agent never deletes. Revoke the password any time from the same
> Google page; nothing else about your account is affected. `.env` is
> gitignored.

### Other providers

Set `IMAP_HOST` and it should work — the IMAP layer is generic:

```bash
IMAP_HOST=imap-mail.outlook.com     # Outlook / Hotmail
IMAP_HOST=imap.mail.yahoo.com       # Yahoo
```

Gmail-specific features degrade rather than break:

| feature | non-Gmail |
|---|---|
| listing, priority, kinds, tracker | works |
| categories (promotions/social) | everything reads as `primary` |
| Gmail links | absent |
| spam folder | set `SPAM_FOLDER` to your provider's name |

---

## Connecting more than one mailbox

Personal and university, say. Use numbered variables:

```bash
EMAIL_USER_1=you@gmail.com
EMAIL_PASS_1=abcd efgh ijkl mnop
EMAIL_NAME_1=personal              # optional label shown in output

EMAIL_USER_2=you@ncsu.edu
EMAIL_PASS_2=qrst uvwx yzab cdef
EMAIL_NAME_2=school
EMAIL_HOST_2=imap.gmail.com        # optional, if it differs
```

Up to nine. If any numbered pair is set, the single-account `EMAIL_USER` /
`EMAIL_PASS` are ignored — so a half-finished migration cannot silently read
the old mailbox too.

With two or more connected, every line says where it came from and you get the
split up front:

```
40 unread today
personal 32 · school 8

Needs a reply (2)
  • Prof. Rao — Project checkpoint due Monday [school]
  • Lorvenk — Offer letter to sign [personal]
```

With one mailbox there is nothing to disambiguate, so no labels appear.

```bash
./mail accounts        # which mailboxes are connected
```

---

## Setting up the model

Install [LM Studio](https://lmstudio.ai) and download a **tool-capable** model.
Qwen 2.5 7B Instruct is a good default; Gemma and Llama 3.1 also work. The
model must support function calling — without it the agent can list mail but
cannot answer questions about it.

Put its exact id in `.env`:

```bash
MODEL=qwen2.5-7b-instruct
```

You do **not** need to start the server or load the model by hand. Run
`lms bootstrap` once to install LM Studio's CLI, and the agent starts the
server and loads the model itself — including when LM Studio is fully quit.

If the id is wrong, `./mail check` says so and lists what you actually have.

---

## What you can do

```bash
./mail                          # chat with your inbox
./mail brief                    # one short summary, fast
./mail once "any interviews?"   # a single question, then exit
./mail apps                     # where every application stands
./mail waiting                  # applications with no reply yet
./mail accounts                 # which mailboxes are connected
./mail log                      # what Siri asked and answered
./mail help                     # every command
```

### Chat

`./mail` opens a terminal chat. Arrow keys scroll back through what you typed,
and the model's markdown is rendered properly — real headings and bullets, not
literal asterisks.

```
what needs my attention?
how many rejections do I have?
anything from Google?
what came in the last 3 days?
check my spam for real mail
summarize the one from Lorvenk
```

**It remembers.** Follow-ups resolve against the last answer:

```
You: how many rejections do I have?
Agent: Three: Adobe, Notion, Hinge Health.

You: what about the last 3 days?
Agent: Seven over three days: Adobe, Notion, Hinge Health, Stripe…
```

The second question names no subject — it carries the first one forward.
Conversations expire after 15 minutes, because a "today" from this morning
should not still be in context tonight. Say `new` to start over.

### Commands handled in code

These are matched in Python, never sent to the model — so email text can never
trigger one, and they work even if the model is confused:

| command | does |
|---|---|
| `draft to x@y.com …` | compose an email, review it, then send |
| `reply to 3` | reply to one from the last listing |
| `send` / `edit …` / `cancel` | what to say once a draft is shown |
| `waiting` | applications with no reply yet |
| `applications` | where every application stands |
| `accounts` | which mailboxes are connected |
| `links` | Gmail links for everything in the last listing |
| `3 is a rejection` | teach it a tag it got wrong |
| `check every 2 hours` | run on a schedule |
| `status` / `stop` | show or cancel the schedule |
| `new` | forget the conversation |
| `help` / `quit` | show the banner / leave |

### Writing an email

The agent can draft and send — with a confirmation step you cannot skip.

```
You: draft to hiring@acme.com asking to reschedule my interview to Friday

  Draft
  To: hiring@acme.com
  Subject: Request to Reschedule Interview

  Hi,

  Thank you for the invitation to interview. I have a conflict at the
  scheduled time — would Friday afternoon work instead?

  Best regards,
  Yaswanth

  send  ·  edit <what to change>  ·  cancel

You: edit make it warmer and mention I am still very interested
  …redrafts and shows it again…

You: send
  Sent to hiring@acme.com.
```

`reply to 3` replies to an email from the last listing, threaded so it lands
in the original conversation.

**Two rules make this safe to point at a mailbox that reads untrusted mail:**

**The model never chooses the recipient.** The address is parsed in Python
from the line *you* typed, or taken from the real `From` header of a real
message. It is never read out of an email body and never invented. Without
that, an email saying "forward this to attacker@example.com" would be an
exfiltration path. Two addresses in one line is refused rather than guessed —
picking the first would silently mail the wrong person.

**The model never sends.** It writes a subject and a body. The draft is held,
shown to you in full, and only `send` puts it on the wire. Anything that is
not `send` or `cancel` is treated as a revision, so a stray word cannot
trigger delivery. There is no path from "the model produced text" to "mail
left the machine".

One recipient at a time — no cc, no bcc, no lists.

**With several mailboxes connected**, which one it leaves from is a decision,
so the draft always shows `From:` and you are never guessed at:

```
You: draft from school to prof@ncsu.edu asking for an extension

  Draft
  From: you@ncsu.edu (school)
  To: prof@ncsu.edu
  …
```

`reply to 3` uses the mailbox the email actually arrived at — a reply to your
professor leaves from your university address without you having to say so.
For a new email with more than one mailbox and no `from`, it asks rather than
picking; sending from the wrong address is not a mistake you can take back.

Sending uses the same app password over SMTP, so there is nothing extra to
configure. `./mail check` verifies the SMTP login without sending anything.

### The application tracker

You apply to a lot of places. The confirmations scroll away, the rejections
look like any other polite email, and after a month you cannot remember who
still owes you an answer.

```
You: applications

8 applications tracked
5 have come back to you — 62% response rate.

Moving forward (2)
  • West Bend — 2026-08-26

Waiting to hear (3)
  • Brighton — 2026-08-26

Rejected (3)
  • TheGuarantors — 2026-08-26
```

It builds itself. Every confirmation, rejection and interview the tagger finds
is recorded as the triage runs — nothing extra is fetched and there is no
command to remember.

The employer comes from the subject before the sender, because a confirmation
is usually relayed: the From says LinkedIn or Workday, the subject says who you
actually applied to. The ATS name, department and legal suffix are stripped, so
"Happen Bank Workday" is Happen Bank.

Status only moves forward — applied → interview → rejected. These arrive out of
order, and a later "thanks for applying" must not un-reject you.

The response rate counts what the tagger caught, so treat it as a floor. A
rejection phrased unusually enough to slip past will sit in "Waiting to hear"
until you correct it.

### It marks what you've seen as read

After a summary, the mail it showed is marked read in Gmail, so it stops
reappearing tomorrow. **Except what matters**: anything tagged needs-a-reply,
or rated HIGH priority, stays unread so it still stands out.

Set `MARK_READ=false` in `.env` and the agent becomes strictly read-only.

---

## Siri

```bash
./mail shortcut
```

Builds **two signed shortcuts** and offers to install them. Click **Add
Shortcut** once each — no building actions by hand.

**"Hey Siri, Check My Emails"** — today's summary on screen.

**"Hey Siri, Ask My Email"** — Siri asks what you want, you say *"any
interviews this week?"*, and it answers. Shares memory with the terminal chat,
so follow-ups work by voice too.

Every voice run is logged to `siri.log` — the question, the answer, and any
error. Siri runs headless, so without that a blank card leaves nothing to
debug. `./mail log` shows it.

Timing, measured on a 9 GB model:

| starting state | time |
|---|---|
| LM Studio open, model loaded | ~9s |
| model idle (unloads after 60 min) | ~21s |
| LM Studio fully quit | ~22s |

A once-a-day question usually pays the ~21s, since the model unloads when
idle. Raise the TTL in LM Studio to keep it resident.

Full setup and troubleshooting: **[shortcuts/](shortcuts/)**.

### A double-clickable app

```bash
./mail desktop
```

Puts a **Mail Agent** launcher on your Desktop that opens the chat in a
Terminal window. Re-run it if you move the folder.

---

## Running on a schedule

Say it in the chat and it is set:

```
You: check every 2 hours
Done — I'll check your inbox every 2 hours and notify you if
something needs a reply.

You: status
Checking your inbox every 2 hours.

You: stop
Stopped. I won't check on a schedule any more.
```

Accepts `every hour`, `every 4 hours`, `every 6h`, `every 30 minutes`,
`every 90 minutes`. Change it any time — setting a new interval replaces the
job rather than stacking a second one. Bounds are 15 minutes to 24 hours.

It installs a macOS **launchd** agent, not a cron job: it survives reboots, and
a Mac that was asleep at the appointed minute runs the job on wake. Each run
does `mail brief --notify`, so you get a notification when something needs a
reply. Output goes to `schedule.log`.

---

## How it decides

The model is good at summarising and bad at being consistent, so the facts are
computed in Python before it ever sees them.

**Category** — promotions, social, updates, spam come from Gmail's own
classifier via `X-GM-RAW`. Gmail already ran that model; a 7B redoing it would
be slower and worse.

**Priority** — HIGH / NORMAL / LOW from header facts: starred, Gmail's
`\Important`, and `List-Unsubscribe`, which by RFC 2369 means bulk sender.

**Kind** — the job-hunt part:

| kind | means | lands in |
|---|---|---|
| `action` | interview, assessment, coursework deadline, or a real person writing to you | **Needs a reply** |
| `rejection` | application turned down | **Rejections** |
| `confirmation` | application acknowledged | one collapsed line |

A rejection never appears under "Needs a reply" — it asks nothing of you.

Detecting one requires reading the **body**. "Thanks for your interest in NRI"
tells you nothing: scanning subjects found 0 rejections in 426 unread; scanning
bodies found them immediately.

Patterns run first — instant, free, deterministic. The model is then asked
about whatever is left, one email at a time, answering a single word. Each
covers the other's weakness:

```
"we have chosen to go in a different direction"   → rejection
"the role has been filled internally"             → rejection
"can you send me your draft chapter?"             → action
```

**Grouping and counting is done in code, not by the model.** Asked to sort 35
emails, a local 4B wrote two of five sections, covered seven messages, and
filed a confirmation under "needs a reply". The tags are exact and already
computed, so the sections are built from them — the counts always reconcile.
The model still answers your questions; it is not the thing doing arithmetic.

### Teaching it

When it gets one wrong:

```
You: 3 is a rejection
Noted: #3 is 'rejection', not 'untagged'. Saved as an example.
```

Saved to `.corrections.json` and included as an example in every future
classification, so it applies from the next email onward.

**Be clear about what this is: the model is not trained and its weights never
change.** Corrections are examples pasted into a prompt. That is why one works
immediately instead of needing hundreds of labels and a GPU — and why deleting
the file reverts the behaviour exactly.

---

## Configuration

Everything lives in `.env`:

| variable | default | does |
|---|---|---|
| `EMAIL_USER` | — | your address |
| `EMAIL_PASS` | — | app password |
| `MODEL` | — | must match LM Studio exactly |
| `EMAIL_USER_1..9` | — | multiple mailboxes (see above) |
| `IMAP_HOST` | `imap.gmail.com` | change for other providers |
| `LM_BASE_URL` | `http://localhost:1234/v1` | LM Studio server |
| `MAX_EMAILS` | `50` | ceiling per call |
| `MARK_READ` | `true` | `false` = strictly read-only |
| `SPAM_FOLDER` | `[Gmail]/Spam` | your provider's spam folder name |
| `MAIL_DEBUG` | off | `1` shows fetch tracing |
| `CORRECTIONS_FILE` | `.corrections.json` | where corrections live |

**How far back.** Defaults to the last 24 hours, unread only. Ask for more in
conversation and it widens. Rolling hours, not calendar days: at 9am, "1 day"
means since 9am yesterday.

**How many.** `MAX_EMAILS` is not there to be tidy. A mailbox with 13k unread
would put ~1.3M characters of headers into a context window that holds a
fraction of that, and the model starts inventing entries well before it
errors. Past the cap, lowest-priority mail is dropped first and the tool says
what it dropped — so a summary never quietly implies it covered everything.

---

## Privacy and safety

- **Nothing leaves your machine.** IMAP to your provider, HTTP to localhost.
  No third-party API, no telemetry.
- **The model's tools are read-only**: list mail, search mail, read one body.
  There is no delete, no shell, no file access. Sending exists, but the model
  cannot reach it — see [Writing an email](#writing-an-email): it never picks
  a recipient and never triggers delivery.
- **Email is treated as hostile.** Bodies are fenced in `UNTRUSTED` markers
  and the model is told they are data, never instructions. Since it has no
  tool that can act, an injected email has nothing to reach for. Tested
  against direct injection attempts.
- **The one write is marking read**, and important mail is exempt.
- These stay on your machine and are gitignored: `.env`, `.sessions/`,
  `.corrections.json`, `.applications.json`, `siri.log`, `schedule.log`.

---

## Troubleshooting

**`cannot reach your mailbox`**
The app password is wrong, or IMAP is off in Gmail settings. Note it must be
an *app* password, not your account password.

**`no server at http://localhost:1234/v1`**
LM Studio is not running. Run `lms bootstrap` once and the agent will start it
itself from then on.

**`MODEL="x" is not in the loaded model list`**
The id in `.env` does not match LM Studio. The error prints what you actually
have — copy it exactly.

**Siri says nothing**
Check `./mail log`. An `ASK` with no `REPLY` means the run never finished —
usually the model was still loading. If the shortcut has a stale path (you
moved the folder), re-run `./mail shortcut`. If it is silent in the Shortcuts
app, give **Shortcuts** Full Disk Access in System Settings → Privacy.

**A rejection is in the wrong section**
Say `3 is a rejection`. It learns from that immediately.

**It seems to miss emails**
`./mail --days 3` widens the window. If mail is being marked read too
eagerly, set `MARK_READ=false`.

---

## Tests

```bash
./mail test
```

194 tests, no network or mailbox required. Most are regressions — each one is
a bug that shipped and was only caught by running against a real inbox:

- `re.VERBOSE` strips literal spaces, so `other candidates` compiled as
  `othercandidates` and matched nothing
- Gmail returns FETCH replies in ascending id while we ask newest-first, so
  zipping the lists paired every snippet with the **wrong email**
- A rejection filed by Gmail under Promotions was dropped entirely, because
  the category guard returned before the rejection check ever ran
- HTML mislabelled as `text/plain` ate the snippet budget before reaching the
  sentence that mattered

---

## License

MIT — see [LICENSE](LICENSE).
