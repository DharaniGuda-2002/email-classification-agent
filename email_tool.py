"""
IMAP extraction, split on a security boundary:

    read_emails()      -> headers + Gmail's own category. No attacker prose,
                          unless you ask for snippets, which are marked.
    read_email_body()  -> one body, explicitly labeled untrusted.

Categories (promotions/social/updates/spam) come from Gmail, not from the
model guessing. Gmail already ran that classifier; asking a 7B to redo it
would be slower and worse.

Priority is a *hint* computed from headers — factual signals only. Ranking
is the model's job; this just gives it something true to rank on.

Reading never mutates the inbox: readonly=True + BODY.PEEK.
"""

import email
import imaplib
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from dotenv import load_dotenv

load_dotenv()

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")

MAX_BODY_CHARS = 1500
SNIPPET_CHARS = 300   # enough to summarize from, short enough to batch
DEFAULT_LIMIT = 15    # latest N emails

HARD_LIMIT = int(os.environ.get("MAX_EMAILS", "50"))


DEFAULT_DAYS = 1 # default rolling window for read_emails()
SOFT_MAX_DAYS = 3   # soft max limit for checking up to 3 days of mail, to avoid long fetches and model timeouts

PRIORITY_RANK = {"HIGH": 0, "NORMAL": 1, "LOW": 2}

TAG_SCAN_MAX = 80

# rejection email keywords, action keywords, and confirmation keywords are all regexes that match
REJECTION_RE = re.compile(r"""
    unfortunately
  | regret \s+ to \s+ inform
  | not \s+ moving \s+ forward
  | other \s+ candidates
  | no \s+ longer \s+ under \s+ consideration
  | not \s+ (?:be \s+)? selected
  | will \s+ not \s+ be \s+ (?:moving|progressing)
  | decided \s+ (?:to \s+ (?:pursue|proceed)|not \s+ to \s+ proceed)
  | pursue \s+ other | move \s+ forward \s+ with \s+ other
  | (?:was|were) \s+ unsuccessful
  | not \s+ to \s+ move \s+ forward
""", re.I | re.X)

# Things that want something from you, with a clock attached.
ACTION_RE = re.compile(r"""
    \binterview\b | \bassessment\b | \bexam\b
  | coding \s+ challenge | take.?home | online \s+ test
  | hackerrank | codesignal | \bkarat\b
  | schedule \s+ (?:a \s+)? (?:call|chat|time|meeting)
  | your \s+ availability | book \s+ a \s+ time
  # "offer" alone is the native tongue of every promotion in the inbox —
  # Amex's "earn $300" matched it. Only job-shaped offers count.
  | (?:job|employment|internship) \s+ offer
  | offer \s+ (?:letter | of \s+ employment)
  | next \s+ steps | action \s+ required
  | please \s+ (?:complete|submit|confirm|respond|reply)
  | due \s+ (?:by|on) | deadline
""", re.I | re.X)

CONFIRMATION_RE = re.compile(r"""
    application \s+ (?:was \s+ | has \s+ been \s+)? (?:received|submitted)
  | thank \s+ you \s+ for \s+ applying
  | received \s+ your \s+ application
  | successfully \s+ submitted
  # "talent community" is deliberately absent: the recurring jobs2web alerts
  # all cite it in their footer, so it tagged weekly job blasts as though
  # they were application acknowledgements.
""", re.I | re.X)

# Bulk-mail fingerprints in the headers. Feedback-ID and the ESP-specific
# X- headers are the reliable ones — Starbucks, UnitedHealth and AliExpress
# marketing all carry them while looking perfectly personal otherwise. Any
# List-* header means a mailing list by RFC 2369.
BULK_HEADER_PREFIXES = ("list-", "x-ses", "x-sfmc", "x-campaign", "x-mailgun",
                        "x-sendgrid", "x-sg-", "x-alidm", "x-250ok", "x-mailer")
BULK_HEADERS = ("feedback-id", "auto-submitted", "errors-to",
                "x-csa-complaints", "x-report-abuse")

# Nobody is sitting behind these waiting for your reply.
ROLE_ADDRESS_RE = re.compile(r"""^(?:
    no-?reply | do-?not-?reply | hiring | careers? | jobs? | hr | recruit\w*
  | info | support | help | team | news(?:letter)? | alerts? | notify
  | notifications? | marketing | mailer | updates? | billing | admin
  | contact | hello | service | email | mail
)(?:[.\-+_]|@|$)""", re.I | re.X)

# A tag outranks the header-derived priority: an interview invite is HIGH
# whatever else it looks like, and a rejection needs nothing from you, so it
# stays visible without crowding out real work.
KIND_PRIORITY = {"action": "HIGH", "rejection": "NORMAL", "confirmation": "LOW"}

# Gmail's tabs. Not stored in X-GM-LABELS, so they need a search to resolve.
CATEGORIES = ("promotions", "social", "updates", "forums")
SPAM_FOLDER = "[Gmail]/Spam"

# Maps the numbers the model sees (1, 2, 3...) to real IMAP ids.
# Module state because the agent process is single-threaded and short-lived.
# If that stops being true, this is the first thing that breaks.
_LAST_FETCH = {}
_LAST_FOLDER = "INBOX"   # read_email_body must reopen the same folder


# --------------------------------------------------------------- pure funcs
# No network, no config. Keep it that way.

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _strip_html(html):
    p = _Stripper()
    try:
        p.feed(html)
        return " ".join("".join(p.parts).split())
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)


def _clean(text):
    """Cut quoted reply chains and signatures — they eat context for nothing."""
    for marker in (r"\nOn .{,80}wrote:", r"\n-{2,}\s*Original Message",
                   r"\n_{5,}", r"\n--\s*\n"):
        text = re.split(marker, text, maxsplit=1)[0]
    return " ".join(text.split())[:MAX_BODY_CHARS]


def _decode(raw):
    """Headers arrive RFC2047-encoded (=?utf-8?B?...?=). Make them text."""
    if not raw:
        return ""
    try:
        text = str(make_header(decode_header(raw)))
    except Exception:
        text = str(raw)
    # Long headers arrive folded across lines. Left in, they break the
    # one-line-per-email listing the model counts on to number things.
    return " ".join(text.split())


def _window(days):
    """(cutoff datetime, IMAP SINCE string) for a rolling window of `days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # SINCE is inclusive and date-only, so ask the server for the whole day
    # the cutoff falls in and trim the extra hours locally.
    return cutoff, cutoff.strftime("%d-%b-%Y")


def _within_window(msg, cutoff):
    """Did this arrive after the cutoff? Undated mail is kept, not dropped."""
    raw = msg.get("Date")
    if not raw:
        return True
    try:
        sent = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return True
    if sent.tzinfo is None:               # naive Date headers are legal
        sent = sent.replace(tzinfo=timezone.utc)
    return sent >= cutoff


def _select(items, limit):
    """
    Trim to `limit`, shedding lowest priority first.

    Returns (kept, dropped_by_category). Newest-first order survives inside
    whatever is kept — priority decides who stays, not the order you read.
    """
    if len(items) <= limit:
        return items, {}

    order = sorted(range(len(items)),
                   key=lambda i: PRIORITY_RANK.get(items[i]["priority"], 1))
    keep = set(order[:limit])

    kept, dropped = [], Counter()
    for i, item in enumerate(items):
        if i in keep:
            kept.append(item)
        else:
            dropped[item["category"]] += 1
    return kept, dropped


def _is_bulk(msg):
    """Any header that only a bulk sender would set."""
    if msg.get("Precedence", "").lower() in ("bulk", "list", "junk"):
        return True
    for name in msg.keys():
        low = name.lower()
        if low in BULK_HEADERS or low.startswith(BULK_HEADER_PREFIXES):
            return True
    return False


def _is_personal(msg):
    """
    Did a human write this to you, rather than a system about you?

    Structural, not keyword-based, so it catches a professor or a recruiter
    writing in their own words with no trigger phrase anywhere.

    Being addressed in To: is necessary but nowhere near sufficient — nearly
    all mail is addressed to you, marketing included. The work is done by the
    bulk headers and the role-address check.
    """
    if _is_bulk(msg):
        return False

    _, addr = parseaddr(msg.get("From", ""))
    if not addr or ROLE_ADDRESS_RE.match(addr):
        return False

    user = (os.environ.get("EMAIL_USER") or "").lower()
    return bool(user and user in f"{msg.get('To','')} {msg.get('Cc','')}".lower())


def _kind(msg, text, category):
    """
    rejection | action | confirmation | "" — what this email *is*.

    Sits alongside Gmail's category, which says where mail came from but not
    what it wants. Marketing is never tagged: "deadline" and "offer" are the
    native tongue of promotional mail and would flood the action list.
    """
    if category in ("promotions", "social", "spam"):
        return ""

    blob = f"{_decode(msg.get('Subject'))}\n{text}"
    if REJECTION_RE.search(blob):
        return "rejection"
    if ACTION_RE.search(blob):
        return "action"
    if _is_personal(msg):          # a human wrote to you; that is the signal
        return "action"
    if CONFIRMATION_RE.search(blob):
        return "confirmation"
    return ""


def _priority(msg, labels, category, kind=""):
    """
    HIGH / NORMAL / LOW from header facts. No judgment, no guessing.

    The model does the actual ranking. This exists so it ranks on something
    real instead of vibes about subject lines.
    """
    if "\\Starred" in labels:
        return "HIGH"
    if category in ("promotions", "social", "spam"):
        return "LOW"
    if kind:
        return KIND_PRIORITY[kind]

    # List-Unsubscribe means bulk sender, by definition — RFC 2369. The most
    # reliable newsletter signal there is, and it works off Gmail too.
    bulk = bool(msg.get("List-Unsubscribe")) or \
        msg.get("Precedence", "").lower() in ("bulk", "list", "junk")

    if "\\Important" in labels:      # Gmail's own importance model
        return "NORMAL" if bulk else "HIGH"
    if bulk:
        return "LOW"

    # Deliberately NOT promoted to HIGH for merely being in To:. Almost
    # everything is addressed to you, marketing included — it separates
    # nothing. HIGH stays rare enough to mean something: starred, or Gmail
    # judged it important.
    user = (os.environ.get("EMAIL_USER") or "").lower()
    if user and user in f"{msg.get('To','')} {msg.get('Cc','')}".lower():
        return "NORMAL"

    return "LOW"   # not addressed to you and not important: bcc'd bulk


def _extract_body(msg):
    """email.Message -> cleaned text. Prefers text/plain, falls back to HTML."""
    plain, html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get("Content-Disposition", "").startswith("attachment"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = payload.decode(part.get_content_charset() or "utf-8",
                                  errors="replace")
            if part.get_content_type() == "text/plain" and not plain:
                plain = text
            elif part.get_content_type() == "text/html" and not html:
                html = text
    else:
        payload = msg.get_payload(decode=True) or b""
        plain = payload.decode(msg.get_content_charset() or "utf-8",
                               errors="replace")

    return _clean(plain or _strip_html(html))


# -------------------------------------------------------------------- edges
# Network lives here. Config is validated here, not at import.

def _connect(folder="INBOX"):
    """
    Authenticated, read-only IMAP session on `folder`.

    Auth is an app password. Not your account password with a 2FA code —
    Google disabled basic auth for Gmail, and IMAP cannot carry an
    interactive challenge anyway: one LOGIN command, one string, no round
    trip to prompt you on. The app password is the supported stand-in.
    """
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    if not user or not password:
        raise RuntimeError(
            "EMAIL_USER and EMAIL_PASS must be set. Copy .env.example to .env. "
            "Generate an app password at myaccount.google.com/apppasswords."
        )

    m = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    m.login(user, password.replace(" ", ""))  # Gmail rejects the displayed spaces
    m.select(folder, readonly=True)           # cannot set \Seen, structurally
    return m


def _category_map(m, ids, days):
    """
    Which of these ids sit in which Gmail tab.

    Gmail does not put tabs in X-GM-LABELS, so each one costs a search. Four
    searches regardless of how many emails you asked for, and each is scoped
    to the same window as the listing — unscoped they returned every matching
    message in the mailbox (12k for updates) just to intersect away all but a
    handful.
    """
    wanted, found = set(ids), {}
    for cat in CATEGORIES:
        try:
            _, d = m.search(
                None, "X-GM-RAW", f'"in:inbox category:{cat} newer_than:{days}d"'
            )
        except imaplib.IMAP4.error:
            continue  # not Gmail; everything stays "primary"
        for i in d[0].split():
            if i in wanted:
                found[i] = cat
    return found


def _fetch_map(m, ids, spec):
    """
    One FETCH for many ids -> {id: (response_prefix, payload)}.

    Keyed off the id inside each response, never off response order. The
    server may answer in any order, and Gmail answers ascending while we ask
    newest-first — zipping the two lists pairs every result with the wrong
    email. That bug is silent, so it lives behind this one function rather
    than at each call site.
    """
    out = {}
    if not ids:
        return out
    _, raw = m.fetch(b",".join(ids), spec)
    for part in raw:
        if not isinstance(part, tuple):
            continue
        # part[0] looks like: b'12 (UID 34 X-GM-LABELS (...) BODY[HEADER] {567}'
        match = re.match(rb"\s*(\d+)\s", part[0])
        if match:
            out[match.group(1)] = (str(part[0]), part[1])
    return out


def _snippets(m, ids):
    """First few hundred chars of each body, in one round trip."""
    return {i: _extract_body(email.message_from_bytes(payload))[:SNIPPET_CHARS]
            for i, (_, payload) in _fetch_map(m, ids, "(BODY.PEEK[])").items()}


def _search_ids(m, unread_only, category, since):
    """Candidate ids for this query, narrowed server-side as far as possible."""
    if category in CATEGORIES:
        query = f"in:inbox category:{category}"
        if unread_only:
            query += " is:unread"
        _, data = m.search(None, "X-GM-RAW", f'"{query}"', "SINCE", since)
    else:
        _, data = m.search(None, "UNSEEN" if unread_only else "ALL",
                           "SINCE", since)
    return data[0].split()


def _describe(days, category, unread_only):
    """Plain-language account of what was actually searched."""
    window = "the last 24 hours" if days == 1 else f"the last {days} days"
    what = "unread " if unread_only else ""
    return f"{what}{category or 'inbox'} mail from {window}"


def _collect(heads, ids, cats, cutoff, category, is_spam):
    """
    Parsed headers -> the email records the rest of the function works on.

    Applies the two filters that need a parsed message: the exact time cut
    (SINCE only narrows to whole days) and "primary", which means "in none
    of the other tabs" — Gmail's own category:primary search overlaps the
    other categories instead of partitioning against them.

    Priority is provisional here; _tag revises it once bodies are read.
    """
    out = []
    for msg_id in reversed(ids):                 # newest first
        entry = heads.get(msg_id)
        if not entry:
            continue
        if category == "primary" and msg_id in cats:
            continue

        prefix, payload = entry
        msg = email.message_from_bytes(payload)
        if not _within_window(msg, cutoff):
            continue

        cat = "spam" if is_spam else cats.get(msg_id, "primary")
        out.append({"id": msg_id, "msg": msg, "labels": prefix, "category": cat,
                    "kind": "", "priority": _priority(msg, prefix, cat)})
    return out


def _tag(emails, snippets):
    """Assign each email a [kind] and re-derive priority from it, in place."""
    for e in emails:
        e["kind"] = _kind(e["msg"], snippets.get(e["id"], ""), e["category"])
        e["priority"] = _priority(e["msg"], e["labels"], e["category"], e["kind"])


def _format(emails, snippets, show_snippets):
    """Email records -> the numbered listing the model reads."""
    lines = []
    for n, e in enumerate(emails, start=1):
        msg = e["msg"]
        kind = f"[{e['kind']}] " if e["kind"] else ""
        lines.append(
            f"{n}. [{e['priority']}] [{e['category']}] {kind}"
            f"From: {_decode(msg['From'])} | "
            f"Subject: {_decode(msg['Subject'])} | "
            f"Date: {msg['Date']}"
        )
        if show_snippets:
            text = snippets.get(e["id"], "").strip() or "(no readable body)"
            lines.append(f"   --- UNTRUSTED SNIPPET --- {text}")
    return lines


def read_emails(unread_only=True, limit=DEFAULT_LIMIT, category=None,
                include_snippets=False, days=DEFAULT_DAYS):
    """
    List email from a rolling time window, with category and priority.

    days: how far back, rolling from now. Defaults to 1 (24 hours).
    category: primary | promotions | social | updates | forums | spam | None
    include_snippets: whether body text is *shown*. Bodies are read either
    way, because the [kind] tag needs them — but with this off the model
    sees only tags derived here in code, and none of the stranger-written
    prose they came from. On, it is fenced in untrusted markers.

    Over `limit`, low-priority mail is shed first and the drop is reported.
    """
    global _LAST_FOLDER
    print(f"   [tool] read_emails days={days} category={category} "
          f"snippets={include_snippets}")
    # The model supplies these. Clamp rather than trust.
    try:
        limit = max(1, min(int(limit), HARD_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS

    # Validated before anything is discarded: a typo'd category should not
    # invalidate the numbering of the listing the user is looking at.
    category = (category or "").lower().strip() or None
    if category and category not in CATEGORIES + ("primary", "spam"):
        return (f"ERROR: unknown category '{category}'. Use one of: "
                f"{', '.join(CATEGORIES)}, primary, spam.")

    _LAST_FETCH.clear()
    is_spam = category == "spam"
    folder = _LAST_FOLDER = SPAM_FOLDER if is_spam else "INBOX"
    cutoff, since = _window(days)
    scope = _describe(days, category, unread_only)

    m = _connect(folder)
    try:
        ids = _search_ids(m, unread_only, category, since)
        if not ids:
            return f"No {scope}."

        # One fetch for every header, not one per email. Headers are needed
        # for the window check, the priority, and the listing alike, so they
        # are pulled once before anything is narrowed.
        heads = _fetch_map(m, ids, "(BODY.PEEK[HEADER] X-GM-LABELS)")
        cats = {} if is_spam else _category_map(m, ids, days)

        emails = _collect(heads, ids, cats, cutoff, category, is_spam)
        if not emails:
            return f"No {scope}."

        # Bodies are read before the cut, not after: a rejection or an
        # interview invite is only identifiable from its body, and tagging
        # after selection would mean the tag could never save an email from
        # being dropped. Bounded so a wide window stays fast.
        snippets = _snippets(m, [e["id"] for e in emails[:TAG_SCAN_MAX]])
        _tag(emails, snippets)

        total = len(emails)
        emails, dropped = _select(emails, limit)
        _LAST_FETCH.update({n: e["id"] for n, e in enumerate(emails, start=1)})

        head = f"{total} {scope}. Showing {len(emails)}"
        if dropped:
            detail = ", ".join(f"{n} {c}" for c, n in sorted(dropped.items()))
            head += f", lowest priority omitted ({detail})"
        return head + ":\n" + "\n".join(
            _format(emails, snippets, include_snippets))
    finally:
        m.logout()


def read_email_body(number):
    """Body of ONE email, by the number shown in read_emails."""
    print(f"   [tool] read_email_body #{number}")
    try:
        number = int(number)
    except (TypeError, ValueError):
        return f"ERROR: '{number}' is not a valid email number."

    msg_id = _LAST_FETCH.get(number)
    if not msg_id:
        return f"ERROR: no email #{number} in the current listing. Call read_emails first."

    m = _connect(_LAST_FOLDER)  # spam listings live in another folder
    try:
        _, raw = m.fetch(msg_id, "(BODY.PEEK[])")
        msg = email.message_from_bytes(raw[0][1])
        content = _extract_body(msg) or "(this email has no readable body)"
        return (
            f"From: {_decode(msg['From'])}\n"
            f"Subject: {_decode(msg['Subject'])}\n\n"
            f"--- BEGIN UNTRUSTED EMAIL CONTENT ---\n"
            f"{content}\n"
            f"--- END UNTRUSTED EMAIL CONTENT ---"
        )
    finally:
        m.logout()


if __name__ == "__main__":
    # Smoke check: does IMAP work at all, without involving the model?
    print(read_emails())