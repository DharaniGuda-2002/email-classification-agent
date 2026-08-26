"""
Where your applications actually stand.

The triage answers "what arrived today". This answers "what happened to the
thing I applied for three weeks ago" — a question the inbox alone cannot,
because by then the confirmation has scrolled into the past and the rejection
looks like any other polite email.

Nothing new is fetched. Every confirmation, rejection and interview the
tagger already identifies is recorded as it goes by, so the pipeline builds
itself from mail you were reading anyway.

Status only moves forward: applied -> interview -> rejected. A later
"thanks for applying" from the same company cannot undo a rejection, because
these arrive out of order and the worst outcome is the true one.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

STORE = Path(__file__).resolve().parent / ".applications.json"

# Ranked: a status never moves to a lower rank.
RANK = {"applied": 0, "interview": 1, "rejected": 2}

KIND_STATUS = {"confirmation": "applied",
               "action": "interview",
               "rejection": "rejected"}

# LinkedIn and the ATS platforms put the company in the subject, not the
# From header — "LinkedIn" is not the company you applied to.
COMPANY_RE = re.compile(r"""
    application \s+ was \s+ sent \s+ to \s+ (?P<a>.+?) \s*$
  | thank \s+ you \s+ for \s+ applying \s+ (?:to|at) \s+ (?P<b>.+?) \s*[!.]?\s*$
  # The terminator must be a group, not a bare "|$" alternative: that binds to
  # the whole pattern and matches the empty string at end of input, so every
  # subject "matched" with no company in it.
  | you(?:'ve|\s+have)? \s+ applied \s+ to \s+
    (?:a \s+ position \s+ at \s+)? (?P<c>.+?) (?:\s*[-–—]|\s*$)
  | thank \s+ you \s+ for \s+ your \s+ interest \s+ in \s+ (?P<d>.+?) \s*[,.!]
  | \bapplication \s+ to \s+ (?P<e>.+?) \s*$
""", re.I | re.X)

# Fragments that survive a bad parse and are never an employer.
NOT_A_COMPANY = {"my", "the", "a", "an", "this", "your", "our", "us", "it",
                 "position", "role", "job", "application", "unknown"}

# The employer's name arrives wrapped in the machinery that sent it: the ATS
# ("@ icims", "Workday"), the department ("Recruiting Team", "Talent
# Acquisition"), and the legal suffix. None of that is the company.
ATS = (r"workday|icims|greenhouse|lever|taleo|jobvite|smartrecruiters|"
       r"ashby|bamboohr|successfactors|brassring|jobs2web|myworkday")

NOISE_SUFFIX = re.compile(
    r"\s*(?:"
    # The separator is optional: "Happen Bank Workday" has none.
    r"[-–—|,@]?\s*(?:" + ATS + r")\b"
    r"|[-–—|,]?\s*(?:careers?|recruiting(?:\s+team)?|talent(?:\s+acquisition)?"
    r"|human\s+resources|hr|jobs?|hiring(?:\s+team)?|team|notifications?)\b"
    r"|[-–—|,]?\s+(?:inc|llc|ltd|corp|plc|gmbh)\.?\s*$"
    r").*$", re.I)

# A bare address is the sender's mail server, not an employer worth listing.
LOOKS_LIKE_ADDRESS = re.compile(r"^[^\s]+@[^\s]+$")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def company_of(record):
    """
    Best guess at the employer, from the subject first and the sender after.

    Subject first because an application confirmation is usually relayed by a
    platform: the From says LinkedIn or Workday, the subject says who you
    actually applied to.
    """
    subject = " ".join((record.get("subject") or "").split())
    match = COMPANY_RE.search(subject)
    if match:
        name = next((g for g in match.groups() if g), "")
        name = NOISE_SUFFIX.sub("", name).strip(" .,-–—:")
        if 2 < len(name) <= 60 and name.lower() not in NOT_A_COMPANY:
            return name

    sender = (record.get("from") or "").strip()
    # "Acme Careers <no-reply@acme.com>" -> "Acme Careers"
    sender = sender.split("<")[0].strip().strip('"')
    # A From with no display name leaves the raw address; listing
    # "no-reply@us.greenhouse-mail.io" as an employer helps nobody.
    if not sender or LOOKS_LIKE_ADDRESS.match(sender):
        return "unknown"
    cleaned = NOISE_SUFFIX.sub("", sender).strip(" .,-–—:")
    if len(cleaned) < 3 or cleaned.lower() in NOT_A_COMPANY:
        return "unknown"
    return cleaned


def _key(name):
    """
    Merge key. "Wilson Elser" and "wilsonelser" are the same employer, and
    they arrive spelled both ways — one from the subject line, one from the
    sender — so a display-name key would list them twice.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _load():
    try:
        data = json.loads(STORE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}      # missing or corrupt is not worth failing a triage over


def _save(data):
    try:
        STORE.write_text(json.dumps(data, indent=1, sort_keys=True))
    except OSError:
        pass           # the tracker is a convenience, never a blocker


def record(records):
    """
    Fold today's tagged mail into the pipeline. Returns what changed.

    Called from the triage, so it stays current without a separate command
    to remember to run.
    """
    data, changed = _load(), []

    for r in records:
        status = KIND_STATUS.get(r.get("kind"))
        if not status:
            continue

        name = company_of(r)
        if name == "unknown":
            continue

        key = _key(name)
        if not key:
            continue

        entry = data.get(key)
        if entry is None:
            data[key] = {"name": name, "status": status, "first_seen": _now(),
                         "last_seen": _now(),
                         "subject": (r.get("subject") or "")[:120]}
            changed.append((name, None, status))
            continue

        entry["last_seen"] = _now()
        # Prefer the spaced spelling: "Wilson Elser" over "wilsonelser".
        if " " in name and " " not in entry.get("name", ""):
            entry["name"] = name
        if RANK[status] > RANK.get(entry.get("status", "applied"), 0):
            changed.append((name, entry["status"], status))
            entry["status"] = status
            entry["subject"] = (r.get("subject") or "")[:120]

    if changed:
        _save(data)
    return changed


def report(limit=40):
    """The pipeline as markdown for ui.render()."""
    data = _load()
    if not data:
        return ("No applications tracked yet. They are recorded automatically "
                "as confirmations and rejections arrive.")

    buckets = {"interview": [], "applied": [], "rejected": []}
    for key, e in data.items():
        buckets.get(e.get("status", "applied"), buckets["applied"]).append(
            (e.get("name", key), e))

    for items in buckets.values():
        items.sort(key=lambda kv: kv[1].get("last_seen", ""), reverse=True)

    total = len(data)
    replied = len(buckets["interview"]) + len(buckets["rejected"])
    out = [f"**{total} applications tracked**"]

    if total:
        rate = round(100 * replied / total)
        out.append(f"{replied} have come back to you — {rate}% response rate.")

    labels = [("interview", "Moving forward"),
              ("applied", "Waiting to hear"),
              ("rejected", "Rejected")]
    for key, label in labels:
        items = buckets[key]
        if not items:
            continue
        out.append(f"\n**{label} ({len(items)})**")
        for name, e in items[:limit]:
            out.append(f"* {name} — {e.get('last_seen', '?')}")
        if len(items) > limit:
            out.append(f"* …and {len(items) - limit} more")

    return "\n".join(out)


def forget():
    """Delete the pipeline. Returns True if there was one."""
    try:
        STORE.unlink()
        return True
    except OSError:
        return False


STALE_DAYS = 14        # a fortnight of silence is worth a nudge


def stale(days=STALE_DAYS):
    """
    Applications with no reply after `days`. Markdown for ui.render().

    The pipeline knows when each one was first seen, so it can answer the
    question the inbox cannot: who has gone quiet long enough to be worth a
    follow-up. Only "applied" counts — an interview or a rejection has
    already come back to you.
    """
    from datetime import date

    data = _load()
    today = date.today()
    waiting = []

    for key, e in data.items():
        if e.get("status") != "applied":
            continue
        try:
            seen = date.fromisoformat(e.get("first_seen", ""))
        except ValueError:
            continue
        age = (today - seen).days
        if age >= days:
            waiting.append((age, e.get("name", key)))

    if not waiting:
        return (f"Nothing has been waiting longer than {days} days. "
                "Everything recent has either replied or is still fresh.")

    waiting.sort(reverse=True)
    out = [f"**{len(waiting)} waiting longer than {days} days**"]
    out += [f"* {name} — {age} days" for age, name in waiting]
    out.append("\nNo reply is not a no. A short follow-up is normal.")
    return "\n".join(out)
