"""
Run the triage on a schedule, set from the chat.

macOS launchd rather than cron: it survives reboots, and a Mac that was
asleep at the appointed minute still runs the job when it wakes, which cron
does not. The job is a plain LaunchAgent, so `launchctl list` and Console
show it like anything else.

Everything here is idempotent. Setting an interval twice replaces the job
rather than stacking a second one, because the whole point is that you can
change your mind mid-conversation.
"""

import plistlib
import re
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
LABEL = "com.yaswanth.mabel"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = PROJECT / "schedule.log"

MIN_HOURS = 0.25      # 15 minutes; below this it is a busy loop, not a digest
MAX_HOURS = 24

# "every 2 hours", "check every 90 minutes", "run every 4h", "every hour"
INTERVAL_RE = re.compile(r"""
    (?:check|run|triage|update)? \s* every \s+
    (?: (?P<n>\d+(?:\.\d+)?) \s* )?
    (?P<unit> hours? | hrs? | h | minutes? | mins? | m )\b
""", re.I | re.X)

STOP_RE = re.compile(
    r"^(?:stop|cancel|disable|unschedule)"
    r"(?:\s+(?:the\s+)?(?:checking|checks|schedule|scheduling|it))?$", re.I)

STATUS_RE = re.compile(r"^(?:schedule|scheduling|status)\??$", re.I)


def parse_interval(text):
    """
    Hours as a float from plain English, or None when nothing matched.

    None rather than a guess: a misread "every" would silently schedule
    something the user never asked for.
    """
    match = INTERVAL_RE.search(text or "")
    if not match:
        return None

    # "every hour" carries no number; the unit alone means one of them.
    n = float(match.group("n")) if match.group("n") else 1.0
    # Units are h/hr/hour(s) or m/min(s)/minute(s), so the first letter
    # separates them.
    hours = n / 60 if match.group("unit").lower().startswith("m") else n
    return max(MIN_HOURS, min(hours, MAX_HOURS))


def _launchctl(*args):
    return subprocess.run(["launchctl", *args],
                          capture_output=True, text=True, check=False)


def status():
    """A sentence describing the current schedule."""
    if not PLIST.exists():
        return "Not scheduled. Say 'check every 2 hours' to start."
    try:
        interval = plistlib.loads(PLIST.read_bytes()).get("StartInterval", 0)
    except (OSError, ValueError):
        return "Scheduled, but the job file is unreadable. Say 'stop' to reset."

    loaded = LABEL in _launchctl("list").stdout
    every = _describe(interval / 3600)
    return (f"Checking your inbox every {every}."
            if loaded else
            f"Scheduled every {every}, but not loaded. Say 'stop' then set it again.")


def _describe(hours):
    if hours < 1:
        return f"{int(round(hours * 60))} minutes"
    if hours == int(hours):
        return "hour" if hours == 1 else f"{int(hours)} hours"
    return f"{hours:g} hours"


def schedule(hours):
    """
    Install or replace the job. Returns a sentence for the user.
    """
    seconds = int(hours * 3600)
    runner = PROJECT / "mabel"
    if not runner.exists():
        return f"Cannot schedule: {runner} is missing."

    args = [str(runner), "brief", "--notify"]
    desc = "notify you"

    job = {
        "Label": LABEL,
        "ProgramArguments": args,
        "StartInterval": seconds,
        "RunAtLoad": False,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "WorkingDirectory": str(PROJECT),
    }

    try:
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(plistlib.dumps(job))
    except OSError as exc:
        return f"Could not write the schedule: {exc}"

    _launchctl("bootout", f"gui/{_uid()}/{LABEL}")
    result = _launchctl("bootstrap", f"gui/{_uid()}", str(PLIST))
    if result.returncode != 0:
        return (f"Could not start the schedule: "
                f"{result.stderr.strip() or result.returncode}")

    return (f"Done — I'll check your inbox every {_describe(hours)} "
            f"and {desc} if something needs a reply.")


def unschedule():
    """Remove the job. Returns a sentence for the user."""
    if not PLIST.exists():
        return "There was no schedule running."
    _launchctl("bootout", f"gui/{_uid()}/{LABEL}")
    try:
        PLIST.unlink()
    except OSError:
        pass
    return "Stopped. I won't check on a schedule any more."


def _uid():
    return subprocess.run(["id", "-u"], capture_output=True,
                          text=True).stdout.strip() or "501"


def _apple_string(text):
    """Make text safe inside a double-quoted AppleScript literal.

    The brief is multi-line, and a bare newline is a syntax error inside such
    a literal — unguarded, osascript fails and the notification is silently
    dropped, which is the exact failure mode the docstring warns about.
    """
    return text.replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def notify(title, message):
    """
    A macOS notification. Best effort — never raises.

    Truncated because Notification Center silently drops an over-long body,
    which looks identical to the notification never firing.
    """
    try:
        body = _apple_string(message)[:240]
        script = (f'display notification "{body}" '
                  f'with title "{_apple_string(title)}"')
        subprocess.run(["osascript", "-e", script],
                       capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
