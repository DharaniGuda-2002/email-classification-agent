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

import classifier

load_dotenv()

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")

MAX_BODY_CHARS = 1500
SNIPPET_CHARS = 300   # enough to summarize from, short enough to batch
DEFAULT_LIMIT = 15    # latest N emails


def _env_int(name, default):
    """A typo in .env should not be an unexplained ValueError at import."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        print(f"   [config] {name}={raw!r} is not a number; using {default}")
        return default


HARD_LIMIT = _env_int("MAX_EMAILS", 50)


# Rolling hours, not calendar days: "1 day" at 9am should not mean nine
# hours of mail. IMAP SINCE is date-granular, so the server narrows to whole
# days and _within_window makes the exact cut.
DEFAULT_DAYS = 1     # default rolling window for read_emails()
SOFT_MAX_DAYS = 3    # what the model reaches for unasked; not a clamp

PRIORITY_RANK = {"HIGH": 0, "NORMAL": 1, "LOW": 2}

TAG_SCAN_MAX = 80

# Rejections hide in the body and read as polite, so they are checked before
# everything else: a rejection usually thanks you for applying too.
REJECTION_RE = re.compile(r"""
    unfortunately
  | regret \s+ to \s+ inform
  | not \s+ moving \s+ forward
  | other \s+ candidates
  | no \s+ longer \s+ under \s+ consideration
  | not \s+ (?:be \s+)? selected
  | will \s+ not \s+ be \s+ (?:moving|progressing)
  | decided \s+ (?:to \s+ (?:pursue|proceed)|not \s+ to \s+ proceed)
  | pursue \s+ (?:other|another)
  # "another candidate", singular, is as common as the plural and matched
  # nothing until an STV rejection went untagged.
  | (?:with|selected|chosen) \s+ another \s+ (?:candidate|applicant)
  | move \s+ forward \s+ with \s+ (?:other|another)
  | another \s+ candidate
  | (?:was|were) \s+ unsuccessful
  | not \s+ to \s+ move \s+ forward
  | position \s+ has \s+ been \s+ filled
""", re.I | re.X)

# Markup that means the body is HTML whatever the Content-Type claims.
HTML_RE = re.compile(r"<\s*(?:p|div|br|img|table|span|html|body|a)\b", re.I)

# Gmail's own thread id, returned alongside the labels in the same FETCH.
THRID_RE = re.compile(r"X-GM-THRID\s+(\d+)")

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
# Separators must allow . _ and - : "no_reply@" and "do.not.reply@" are as
# common as the hyphenated spellings, and matching only "-" let a Grifols
# rejection through as though a human had written it.
ROLE_ADDRESS_RE = re.compile(r"""^(?:
    no[-._]?reply | do[-._]?not[-._]?reply
  | hiring | careers? | jobs? | hr | recruit\w*
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

# Enough of the last listing to correct a tag by its number, without a
# second IMAP round trip.
_LAST_RECORDS = {}


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
    addressed = f"{msg.get('To', '')} {msg.get('Cc', '')}".lower()
    return bool(user and user in addressed)


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


def _gmail_url(prefix):
    """
    Permalink to the original message, from the thread id in a FETCH reply.

    Gmail's web UI addresses threads by the hex form of X-GM-THRID. The
    account is keyed by address rather than the usual /u/0/ — with more than
    one Google account signed in, /u/0/ opens the wrong mailbox.
    """
    match = THRID_RE.search(prefix or "")
    if not match:
        return ""
    user = os.environ.get("EMAIL_USER", "0")
    return f"https://mail.google.com/mail/u/{user}/#all/{int(match.group(1)):x}"


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
    addressed = f"{msg.get('To', '')} {msg.get('Cc', '')}".lower()
    if user and user in addressed:
        return "NORMAL"

    return "LOW"   # not addressed to you and not important: bcc'd bulk


def _decode_payload(payload, charset):
    """
    Bytes -> text, surviving a charset the sender invented.

    Python raises LookupError for an unknown encoding, and a malformed
    charset is common enough in spam that letting it propagate means one bad
    email takes down the whole triage.
    """
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except (LookupError, AttributeError):
        return payload.decode("utf-8", errors="replace")


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
            text = _decode_payload(payload, part.get_content_charset())
            if part.get_content_type() == "text/plain" and not plain:
                plain = text
            elif part.get_content_type() == "text/html" and not html:
                html = text
    else:
        payload = msg.get_payload(decode=True) or b""
        plain = _decode_payload(payload, msg.get_content_charset())

    text = plain or html
    # Senders mislabel HTML as text/plain often enough to matter: a Grifols
    # rejection arrived as raw <p> and <img> tags, and the markup ate the
    # whole snippet budget before reaching the sentence that mattered. Trust
    # the content, not the Content-Type.
    if HTML_RE.search(text):
        text = _strip_html(text)
    return _clean(text)


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
                    "url": _gmail_url(prefix), "kind": "",
                    "priority": _priority(msg, prefix, cat)})
    return out


def _tag(emails, snippets, use_model=True, max_model_calls=None):
    """
    Assign each email a [kind] and re-derive priority from it, in place.

    Patterns first: instant, free, and deterministic where they fire. The
    model is asked only about what is left over, which is where the patterns
    were failing anyway — "another candidate" needed two rule fixes before it
    tagged, and the model got it first try.
    """
    for e in emails:
        e["kind"] = _kind(e["msg"], snippets.get(e["id"], ""), e["category"])

    if use_model:
        filled = classifier.refine(emails, snippets, _decode,
                                   max_calls=max_model_calls
                                   or classifier.MAX_CALLS)
        if filled:
            print(f"   [model] tagged {filled} the patterns missed")

    for e in emails:
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
                include_snippets=False, days=DEFAULT_DAYS,
                max_model_calls=None):
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
        heads = _fetch_map(m, ids, "(BODY.PEEK[HEADER] X-GM-LABELS X-GM-THRID)")
        cats = {} if is_spam else _category_map(m, ids, days)

        emails = _collect(heads, ids, cats, cutoff, category, is_spam)
        if not emails:
            return f"No {scope}."

        # Bodies are read before the cut, not after: a rejection or an
        # interview invite is only identifiable from its body, and tagging
        # after selection would mean the tag could never save an email from
        # being dropped. Bounded so a wide window stays fast.
        snippets = _snippets(m, [e["id"] for e in emails[:TAG_SCAN_MAX]])
        _tag(emails, snippets, max_model_calls=max_model_calls)

        total = len(emails)
        emails, dropped = _select(emails, limit)

        # _select ranks by priority across every candidate, so an email past
        # TAG_SCAN_MAX can survive the cut with no snippet and no kind. Fill
        # those in now — bounded by `limit`, and only when the window was
        # wide enough to exceed the scan ceiling in the first place.
        missing = [e["id"] for e in emails if e["id"] not in snippets]
        if missing:
            snippets.update(_snippets(m, missing))
            _tag([e for e in emails if e["id"] in missing], snippets)

        _LAST_FETCH.update({n: e["id"] for n, e in enumerate(emails, start=1)})
        _LAST_RECORDS.clear()
        _LAST_RECORDS.update({n: {
            "from": _decode(e["msg"]["From"]),
            "subject": _decode(e["msg"]["Subject"]),
            "snippet": snippets.get(e["id"], ""),
            "kind": e["kind"],
            "priority": e["priority"],
            "url": e["url"],
        } for n, e in enumerate(emails, start=1)})

        head = f"{total} {scope}. Showing {len(emails)}"
        if dropped:
            detail = ", ".join(f"{n} {c}" for c, n in sorted(dropped.items()))
            head += f", lowest priority omitted ({detail})"
        return head + ":\n" + "\n".join(
            _format(emails, snippets, include_snippets))
    finally:
        m.logout()


def links(important_only=True):
    """
    Clickable Gmail links for the last listing, built here rather than by
    the model.

    Deliberately not part of the listing the model reads: a 70-character URL
    is the kind of thing a small model garbles when it repeats it back, and a
    subtly wrong link is worse than none. Numbers match what it saw, so it can
    say "email 3" and the link below it is guaranteed intact.
    """
    rows = [(n, r) for n, r in sorted(_LAST_RECORDS.items())
            if r["url"] and (not important_only
                             or r["kind"] in ("action", "rejection")
                             or r["priority"] == "HIGH")]
    if not rows:
        return ""

    lines = ["Open in Gmail:"]
    for n, r in rows:
        tag = f"[{r['kind']}] " if r["kind"] else ""
        lines.append(f"  {n}. {tag}{r['subject'][:58]}")
        lines.append(f"     {r['url']}")
    return "\n".join(lines)


def brief(days=DEFAULT_DAYS, limit=HARD_LIMIT):
    """
    One short paragraph, plain text, meant to be spoken aloud.

    Built entirely from the tags, with no model loop — the counts and kinds
    are already computed in code, so this returns in seconds instead of the
    minute-plus a full triage takes. That matters when something is waiting
    to read it out.
    """
    # Fewer model calls than a full triage: something is waiting to read
    # this out, and 12 of the 17 seconds a full pass takes is the classifier.
    read_emails(unread_only=True, limit=limit, days=days, max_model_calls=8)
    records = list(_LAST_RECORDS.values())
    if not records:
        window = "today" if days == 1 else f"the last {days} days"
        return f"No new mail {window}."

    by_kind = Counter(r["kind"] for r in records)
    actions = [r for r in records if r["kind"] == "action"]
    rejections = [r for r in records if r["kind"] == "rejection"]

    parts = [f"{len(records)} new email{'s' if len(records) != 1 else ''}."]

    if actions:
        parts.append(f"{len(actions)} need{'s' if len(actions) == 1 else ''} a reply:")
        parts.extend(f"{_sender_name(r['from'])}, {r['subject'][:70]}."
                     for r in actions[:3])
    else:
        parts.append("Nothing needs a reply.")

    if rejections:
        who = ", ".join(_sender_name(r["from"]) for r in rejections[:4])
        parts.append(f"{len(rejections)} rejection"
                     f"{'s' if len(rejections) != 1 else ''}: {who}.")

    if by_kind.get("confirmation"):
        parts.append(f"{by_kind['confirmation']} application"
                     f"{'s' if by_kind['confirmation'] != 1 else ''} acknowledged.")

    tagged = sum(by_kind[k] for k in ("action", "rejection", "confirmation"))
    rest = len(records) - tagged
    if rest > 0:
        parts.append(f"{rest} other{'s' if rest != 1 else ''}.")

    return " ".join(parts)


def _sender_name(from_header):
    """
    "Acme Careers <no-reply@acme.com>" -> "Acme Careers". For speech.

    With no display name, fall back to the second-level domain rather than
    the first label: "no-reply@us.greenhouse-mail.io" is greenhouse-mail, not
    "us". Generic display names get the same treatment — "Human Resources"
    names nobody.
    """
    name, addr = parseaddr(from_header or "")
    domain = addr.split("@")[-1] if "@" in (addr or "") else ""
    labels = [p for p in domain.split(".") if p]
    company = labels[-2] if len(labels) >= 2 else (labels[0] if labels else "")

    generic = {"human resources", "hr", "recruiting", "careers", "talent",
               "no reply", "noreply", "notifications", "jobs"}
    if name and name.strip().lower() not in generic:
        return name.strip()
    return company or name or "unknown"


def correct(number, label):
    """
    Teach it that email #N is really a `label`. Returns a status line.

    Stored as an example, not as a rule about this one email — the point is
    that the next email phrased like it gets classified right too.
    """
    try:
        record = _LAST_RECORDS.get(int(number))
    except (TypeError, ValueError):
        return f"ERROR: '{number}' is not a valid email number."
    if not record:
        return f"ERROR: no email #{number} in the current listing."

    if not classifier.add_correction(record["from"], record["subject"],
                                     record["snippet"], label):
        return (f"ERROR: '{label}' is not a kind. Use one of: "
                f"{', '.join(classifier.LABELS)}.")

    was = record["kind"] or "untagged"
    return (f"Noted: #{number} is '{label}', not '{was}'. "
            f"Saved as an example for future emails.")


def read_email_body(number):
    """Body of ONE email, by the number shown in read_emails."""
    print(f"   [tool] read_email_body #{number}")
    try:
        number = int(number)
    except (TypeError, ValueError):
        return f"ERROR: '{number}' is not a valid email number."

    msg_id = _LAST_FETCH.get(number)
    if not msg_id:
        return (f"ERROR: no email #{number} in the current listing. "
                "Call read_emails first.")

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
