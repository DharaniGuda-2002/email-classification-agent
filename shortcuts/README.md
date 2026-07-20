# Asking Siri to check your email

"Hey Siri, check my emails" → it reads a one-line summary aloud:

> "26 new emails. Nothing needs a reply. 1 rejection: grifols.
> 1 application acknowledged. 24 others."

Takes about 20 seconds. Everything runs on your Mac.

---

## Before you start

Get the agent itself working first:

```bash
cd ..
./run.sh --brief
```

If that prints a sentence, carry on. If it does not, fix that first — the
Shortcut only wraps this command, so it cannot work until this does.

---

## Step 1 — copy the script path

```bash
cd shortcuts
chmod +x check-emails.sh     # once, so macOS will run it
pwd                          # copy this, you need it in step 3
```

Your full path is that, plus `/check-emails.sh`. For example:

```
/Users/you/Desktop/agent/shortcuts/check-emails.sh
```

## Step 2 — make the Shortcut

Open **Shortcuts** (already on your Mac) → **File → New Shortcut**.

Name it **exactly what you want to say to Siri**. The name *is* the voice
command, so name it `Check my emails`, not `Email Agent v2`.

## Step 3 — add the two actions

**Action 1: Run Shell Script**

Search the right-hand panel for "Run Shell Script" and drag it in.

| field | value |
|---|---|
| Shell | `/bin/zsh` |
| Input | leave empty |
| Pass Input | *as arguments* |
| Script | the full path from step 1 |

The script box holds one line and nothing else:

```
/Users/you/Desktop/agent/shortcuts/check-emails.sh
```

**Action 2: Speak Text**

Drag **Speak Text** in below it. Set its input to **Shell Script Result** —
click the text field, and pick it from the variables that appear.

Order matters: Run Shell Script first, Speak Text second.

## Step 4 — run it

Press ▶ in Shortcuts. macOS will ask permission to run shell scripts the
first time — allow it.

Then say **"Hey Siri, check my emails."**

---

## What to expect

| starting state | time |
|---|---|
| LM Studio open, model loaded | ~9s |
| LM Studio open, model idle | ~21s |
| LM Studio fully quit | ~22s |

**Quitting LM Studio is fine** — the script starts it. It launches the app
rather than a background service, so expect the LM Studio window to appear.

LM Studio unloads an idle model after 60 minutes, so a once-a-day question
almost always pays the ~21 seconds. Raising the TTL in LM Studio keeps it
loaded and fast, at the cost of holding the model in RAM.

---

## If it does not work

**Siri says "Could not check your email."**
The agent failed before producing a summary. Run it by hand to see why:

```bash
./shortcuts/check-emails.sh
cd .. && ./run.sh --check
```

**Nothing is spoken at all.**
The Speak Text action is missing, or its input is not set to Shell Script
Result.

**"The operation couldn't be completed"**
The script is not executable. Run `chmod +x check-emails.sh`.

**It works in Terminal but not in the Shortcut.**
Shortcuts runs with a minimal environment and does not read your shell
profile. The script already adds Homebrew and the LM Studio CLI to `PATH`; if
your setup lives somewhere else, add that path near the top of
`check-emails.sh`.

**Siri opens the Shortcuts app instead of running it.**
The name is ambiguous. Rename it to something distinctive — "Check my emails"
rather than "Email".

---

## Variations

**Show it instead of speaking it** — swap Speak Text for **Show Result**.

**A different window** — edit the last line of `check-emails.sh`:

```bash
OUTPUT=$(./run.sh --brief --days 3 2>/dev/null | tail -1)
```

**On your iPhone** — this cannot work as-is. The script needs your Mac's
Python, LM Studio and mailbox credentials. Running it on iPhone would mean
exposing the Mac over SSH and calling it remotely, which is a different
project with real security tradeoffs.
