# Asking Siri to check your email

"Hey Siri, Check My Emails" → it runs the agent and reads a one-line summary
aloud:

> "50 new emails. 3 need a reply: Brex, Guidehouse, Axle. 3 rejections:
> Walker, Guidehouse, Claritev. 11 applications acknowledged. 33 others."

Siri waits for the model to finish, then speaks the real result — about 20
seconds. Everything runs on your Mac.

---

## Install it (no manual building)

```bash
./run.sh --shortcut
```

That generates a **signed** `Check My Emails.shortcut`, then asks if you want
to open it. Say yes, click **Add Shortcut** in the window that appears, and
that is the whole setup. You never build actions by hand.

If you skipped the prompt, install it later with:

```bash
open "Check My Emails.shortcut"
```

Then say **"Hey Siri, Check My Emails."**

The shortcut embeds this project's path, so if you move the folder, run
`./run.sh --shortcut` again.

### What it contains

Two actions: **Run Shell Script** (runs `shortcuts/check-emails.sh`, which
does `run.sh --brief`) and **Speak Text** (reads the result). The shell action
blocks until the model replies, which is why Siri waits for the real answer.

---

## Before it will work

- **Run the agent once in a terminal first** — `./run.sh` — so the mailbox
  login and the model are known to work. Siri can only run what already runs.
- **LM Studio does not need to be open.** `run.sh` starts it via the `lms`
  CLI. First run of the day is slower (~20s) because the model loads cold.
- **Full Disk Access.** The project lives under `~/Desktop`, which macOS
  protects. If the shortcut is silent, give **Shortcuts** Full Disk Access in
  System Settings → Privacy & Security.

---

## Timing

| starting state | time |
|---|---|
| LM Studio open, model loaded | ~9s |
| model idle (unloaded after 60 min) | ~20s |
| LM Studio fully quit | ~22s |

A once-a-day question almost always pays the ~20s, since the model unloads
after 60 minutes idle. Raise the TTL in LM Studio to keep it loaded.

---

## If it doesn't work

**Test with ▶ first.** Open the shortcut in the Shortcuts app and click ▶.
That surfaces errors in a window instead of as silence from Siri.

| symptom | fix |
|---|---|
| "Could not check your email" | agent failed to start — run `./run.sh --check` |
| silence | Run Shell Script path is wrong, or Shortcuts lacks Full Disk Access |
| Siri opens the app instead of running | name collides — rename the shortcut |
| nothing spoken | the Speak Text action lost its input; regenerate |

---

## Building it by hand

Only if `./run.sh --shortcut` fails to sign. In the **Shortcuts** app, new
shortcut named **Check My Emails**, two actions:

1. **Run Shell Script** — Shell `/bin/zsh`, script:
   ```
   /Users/you/Desktop/agent/shortcuts/check-emails.sh
   ```
2. **Speak Text** — input set to **Shell Script Result**.

---

## On your iPhone

Not as-is. The script needs this Mac's Python, LM Studio and mailbox
credentials, and the Run Shell Script action does not exist in Shortcuts on
iOS. Reaching it from a phone means exposing the Mac over SSH — a separate
project with real security tradeoffs.
