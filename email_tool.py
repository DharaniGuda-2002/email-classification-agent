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

Reading is non-mutating: BODY.PEEK fetches never set the seen flag. The one
deliberate write is mark_read() — mail the agent summarizes is marked read
unless it is important (needs a reply, or HIGH priority). MARK_READ=false
in .env turns that off.
"""

import email
import imaplib
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

from dotenv import load_dotenv

import classifier

load_dotenv()


def _env_str(name, default=""):
    """Read env var, stripping inline comments so `host # note` stays valid."""
    raw = os.environ.get(name, default)
    if raw:
        raw = raw.split("#")[0].strip()
    return raw


IMAP_HOST = _env_str("IMAP_HOST", "imap.gmail.com")

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
        print(f"   [config] {name}={raw!r} is not a number; using {default}",
              file=sys.stderr)
        return default


HARD_LIMIT = _env_int("MAX_EMAILS", 50)

# The escape hatch: marking mail read is the default so what you've seen stops
# reappearing. MARK_READ=false keeps the agent strictly read-only.
MARK_READ = os.environ.get("MARK_READ", "true").strip().lower() \
    not in ("0", "false", "no", "off")


# Rolling hours, not calendar days: "1 day" at 9am should not mean nine
# hours of mail. IMAP SINCE is date-granular, so the server narrows to whole
# days and _within_window makes the exact cut.
DEFAULT_DAYS = 1     # default rolling window for read_emails()
SOFT_MAX_DAYS = 3    # what the model reaches for unasked; not a clamp

PRIORITY_RANK = {"HIGH": 0, "NORMAL": 1, "LOW": 2}

TAG_SCAN_MAX = 80


def get_accounts():
    """
    Return list of configured email accounts from environment.

    Supports two formats:
    1. Legacy single account: EMAIL_USER, EMAIL_PASS, [EMAIL_NAME], [IMAP_HOST]
    2. Multi-account: EMAIL_USER_1..9, EMAIL_PASS_1..9, [EMAIL_NAME_1..9], [EMAIL_HOST_1..9]

    If any EMAIL_USER_N is set, the legacy vars are ignored. Each account dict
    contains: user, pass, name, host. Name defaults to email local-part.
    Host defaults to global IMAP_HOST.
    """
    accounts = []

    # First check numbered accounts (1-9)
    for i in range(1, 10):
        user = os.environ.get(f"EMAIL_USER_{i}")
        password = os.environ.get(f"EMAIL_PASS_{i}")
        if user and password:
            name = os.environ.get(f"EMAIL_NAME_{i}") or user.split("@")[0]
            host = os.environ.get(f"EMAIL_HOST_{i}") or IMAP_HOST
            accounts.append({"user": user, "pass": password, "name": name, "host": host})

    # Fallback to legacy single-account config
    if not accounts:
        user = os.environ.get("EMAIL_USER")
        password = os.environ.get("EMAIL_PASS")
        if user and password:
            name = os.environ.get("EMAIL_NAME") or "default"
            accounts.append({"user": user, "pass": password, "name": name, "host": IMAP_HOST})

    return accounts


# Rejections hide in the body and read as polite, so they are checked before
# everything else: a rejection usually thanks you for applying too.
REJECTION_RE = re.compile(r"""
    # --- Job application rejections ---
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
  | we \s+ have \s+ decided \s+ not \s+ to \s+ proceed
  | unable \s+ to \s+ offer
  | not \s+ a \s+ match
  | not \s+ the \s+ right \s+ fit

    # --- Email delivery failures / bounces ---
  | undeliverable
  | delivery \s+ status \s+ notification
  | delivery \s+ failed
  | mail \s+ delivery \s+ failed
  | returned \s+ mail
  | return \s+ to \s+ sender
  | mail .{0,20} returned .{0,20} sender
  | bounced
  | bounce \s+ message
  | non.?delivery
  | could \s+ not \s+ be \s+ delivered
  | failed \s+ to \s+ deliver
  | recipient \s+ (?:rejected|unknown|not \s+ found)
  | mailbox \s+ (?:full|unavailable|not \s+ found)
  | host \s+ (?:unknown|unreachable)
  | connection \s+ (?:timed \s+ out|refused)
  | spam \s+ (?:rejected|blocked)
  | policy \s+ (?:rejection|violation)
  | blacklist
  | greylist

    # --- LinkedIn / job platform rejections ---
  | linkedin .{0,30} (?:not \s+ selected|unfortunately|rejected|declined)
  | indeed .{0,30} (?:not \s+ selected|unfortunately|rejected|declined)
  | glassdoor .{0,30} (?:not \s+ selected|unfortunately|rejected|declined)
  | ziprecruiter .{0,30} (?:not \s+ selected|unfortunately|rejected|declined)
  | monster .{0,30} (?:not \s+ selected|unfortunately|rejected|declined)
  | careerbuilder .{0,30} (?:not \s+ selected|unfortunately|rejected|declined)
  | your \s+ application \s+ was \s+ (?:not \s+ )?selected
  | application \s+ (?:status|update): \s* (?:rejected|declined|not \s+ selected)
  | we \s+ regret \s+ to \s+ inform \s+ you \s+ that \s+ your \s+ application
""", re.I | re.X)

# Markup that means the body is HTML whatever the Content-Type claims.
HTML_RE = re.compile(r"<\s*(?:p|div|br|img|table|span|html|body|a)\b", re.I)

# Gmail's own thread id, returned alongside the labels in the same FETCH.
THRID_RE = re.compile(r"X-GM-THRID\s+(\d+)")

# The stable, per-message id. Sequence numbers shift whenever anything is
# expunged, so a write issued from a second connection must address by UID —
# by position it would silently mark the wrong email read.
UID_RE = re.compile(r"\bUID\s+(\d+)")

# Unambiguous action: an interview, a test, a real ask. These outrank a
# confirmation — "thanks for applying, now do this assessment" needs a reply.
ACTION_STRONG_RE = re.compile(r"""
    \binterview\b | \bassessment\b | \bexam\b
  | coding \s+ challenge | take.?home | online \s+ test
  | hackerrank | codesignal | \bkarat\b
  | schedule \s+ (?:a \s+)? (?:call|chat|time|meeting)
  | your \s+ availability | book \s+ a \s+ time
  # "offer" alone is the native tongue of every promotion in the inbox —
  # Amex's "earn $300" matched it. Only job-shaped offers count.
  | (?:job|employment|internship) \s+ offer
  | offer \s+ (?:letter | of \s+ employment)
  | action \s+ required
""", re.I | re.X)

# Weak action: boilerplate that also litters acknowledgement emails ("please
# complete your profile", "we'll share next steps"). Checked AFTER
# confirmation, so a pure "thanks for applying" is not dragged into the
# needs-a-reply pile by its own footer.
ACTION_WEAK_RE = re.compile(r"""
    next \s+ steps
  | please \s+ (?:complete|submit|confirm|respond|reply)
  | due \s+ (?:by|on) | deadline
""", re.I | re.X)

CONFIRMATION_RE = re.compile(r"""
    application \s+ (?:was \s+ | has \s+ been \s+)? (?:received|submitted)
  | thank \s+ you \s+ for \s+ (?:applying | your \s+ (?:application|interest))
  | thanks \s+ for \s+ applying
  # "received your job application" — allow a word between, or the exact match
  # misses the most common phrasing.
  | received \s+ your \s+ (?:\w+ \s+)? application
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
# Non-Gmail providers name the folder differently (e.g. Outlook's "Junk").
SPAM_FOLDER = os.environ.get("SPAM_FOLDER", "[Gmail]/Spam")

# Maps the numbers the model sees (1, 2, 3...) to real IMAP UIDs. UIDs, not
# sequence numbers: body reads and mark-read reopen the folder, and sequence
# numbers shift on expunge — by position you'd fetch or mark the wrong mail.
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
    if ACTION_STRONG_RE.search(blob):      # interview, assessment — always a reply
        return "action"
    # Confirmation beats weak-action boilerplate and the _is_personal guess:
    # a "thanks for applying" whose footer says "next steps" is still just an
    # acknowledgement. Reversing this put six of them under "needs a reply".
    if CONFIRMATION_RE.search(blob):
        return "confirmation"
    if ACTION_WEAK_RE.search(blob):
        return "action"
    if _is_personal(msg):          # last resort: a human seems to have written
        return "action"
    return ""


def _uid(prefix):
    """Stable message id from a FETCH response line. '' if the server omits it."""
    match = UID_RE.search(prefix or "")
    return match.group(1) if match else ""


def _gmail_url(prefix, account_index=0):
    """
    Permalink to the original message, from the thread id in a FETCH reply.

    Gmail's web UI addresses threads by the hex form of X-GM-THRID.
    Multi-account uses numeric indices: /u/0/, /u/1/, etc.
    """
    match = THRID_RE.search(prefix or "")
    if not match:
        return ""
    return f"https://mail.google.com/mail/u/{account_index}/#all/{int(match.group(1)):x}"


def _priority(msg, labels, category, kind="", account_user=None):
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
    if account_user is None:
        accounts = get_accounts()
        account_user = accounts[0]["user"] if accounts else ""
    user = (account_user or "").lower()
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

def _connect(account=None, folder="INBOX", readonly=True):
    """
    Authenticated IMAP session on `folder`. Read-only unless asked otherwise.

    `readonly=False` exists for mark_read(), the one write in the process.
    Every read path passes the default and stays non-mutating.

    Auth is an app password. Not your account password with a 2FA code —
    Google disabled basic auth for Gmail, and IMAP cannot carry an
    interactive challenge anyway: one LOGIN command, one string, no round
    trip to prompt you on. The app password is the supported stand-in.

    If account is None, uses the first configured account.
    """
    if account is None:
        accounts = get_accounts()
        if not accounts:
            raise RuntimeError(
                "No email accounts configured. Copy .env.example to .env and set "
                "EMAIL_USER/EMAIL_PASS or EMAIL_USER_1/EMAIL_PASS_1. "
                "Generate app passwords at myaccount.google.com/apppasswords."
            )
        account = accounts[0]

    user = account["user"]
    password = account["pass"]
    host = account.get("host", IMAP_HOST)

    m = imaplib.IMAP4_SSL(host, 993)
    m.login(user, password.replace(" ", ""))  # Gmail rejects the displayed spaces
    m.select(folder, readonly=readonly)
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


def _collect(heads, ids, cats, cutoff, category, is_spam, account=None, account_index=0):
    """
    Parsed headers -> the email records the rest of the function works on.

    Applies the two filters that need a parsed message: the exact time cut
    (SINCE only narrows to whole days) and "primary", which means "in none
    of the other tabs" — Gmail's own category:primary search overlaps the
    other categories instead of partitioning against them.

    Priority is provisional here; _tag revises it once bodies are read.
    """
    out = []
    account_user = account["user"] if account else None
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
        out.append({"id": msg_id, "uid": _uid(prefix), "msg": msg,
                    "labels": prefix, "category": cat, "url": _gmail_url(prefix, account_index=account_index),
                    "kind": "", "priority": _priority(msg, prefix, cat, account_user=account_user)})
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
            print(f"   [model] tagged {filled} the patterns missed", file=sys.stderr)

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
                max_model_calls=None, account=None):
    """
    List email from a rolling time window, with category and priority.

    days: how far back, rolling from now. Defaults to 1 (24 hours).
    category: primary | promotions | social | updates | forums | spam | None
    include_snippets: whether body text is *shown*. Bodies are read either
    way, because the [kind] tag needs them — but with this off the model
    sees only tags derived here in code, and none of the stranger-written
    prose they came from. On, it is fenced in untrusted markers.
    account: specific account dict, or None to check all accounts

    Over `limit`, low-priority mail is shed first and the drop is reported.
    """
    accounts = get_accounts() if account is None else [account]
    if not accounts:
        raise RuntimeError("No email accounts configured.")

    global _LAST_FOLDER
    print(f"   [tool] read_emails days={days} category={category} "
          f"snippets={include_snippets} accounts={[a['name'] for a in accounts]}",
          file=sys.stderr)
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
    _LAST_RECORDS.clear()   # a fresh listing invalidates old numbers and links

    all_emails = []
    all_snippets = {}
    total_count = 0

    for acct_idx, acct in enumerate(accounts):
        is_spam = category == "spam"
        folder = SPAM_FOLDER if is_spam else "INBOX"
        cutoff, since = _window(days)

        m = _connect(acct, folder)
        try:
            ids = _search_ids(m, unread_only, category, since)
            if not ids:
                m.logout()
                continue

            # One fetch for every header, not one per email. Headers are needed
            # for the window check, the priority, and the listing alike, so they
            # are pulled once before anything is narrowed.
            heads = _fetch_map(m, ids, "(BODY.PEEK[HEADER] UID X-GM-LABELS X-GM-THRID)")
            cats = {} if is_spam else _category_map(m, ids, days)

            emails = _collect(heads, ids, cats, cutoff, category, is_spam, acct, account_index=acct_idx)
            if not emails:
                m.logout()
                continue

            total_count += len(emails)

            # Bodies are read before the cut, not after: a rejection or an
            # interview invite is only identifiable from its body, and tagging
            # after selection would mean the tag could never save an email from
            # being dropped. Bounded so a wide window stays fast.
            scan_ids = [e["id"] for e in emails[:TAG_SCAN_MAX]]
            acct_snippets = _snippets(m, scan_ids)
            all_snippets.update(acct_snippets)
            _tag(emails, acct_snippets, max_model_calls=max_model_calls)

            # Add account info to each email
            for e in emails:
                e["account"] = acct["name"]
                e["account_user"] = acct["user"]

            all_emails.extend(emails)
        finally:
            m.logout()

    if not all_emails:
        scope = _describe(days, category, unread_only)
        return f"No {scope}."

    # Re-derive priority now that we have account_user on each email
    for e in all_emails:
        e["priority"] = _priority(
            e["msg"], e["labels"], e["category"], e.get("kind", ""),
            account_user=e.get("account_user")
        )

    # Sort all emails globally by priority, then by date (newest first)
    all_emails.sort(key=lambda e: (
        PRIORITY_RANK.get(e["priority"], 99),
        e["msg"].get("Date", "")
    ))

    # Apply global limit
    emails, dropped = _select(all_emails, limit)

    # Fill in snippets for any selected emails that don't have them yet
    missing = [e["id"] for e in emails if e["id"] not in all_snippets]
    if missing:
        # We need to fetch from the right account for each missing email
        # For simplicity, fetch from the first account that has it
        for e in emails:
            if e["id"] in missing:
                acct_name = e["account"]
                acct = next((a for a in accounts if a["name"] == acct_name), accounts[0])
                m = _connect(acct, "INBOX")
                try:
                    acct_snippets = _snippets(m, [e["id"]])
                    all_snippets.update(acct_snippets)
                finally:
                    m.logout()
        _tag([e for e in emails if e["id"] in missing], all_snippets)

    _LAST_FETCH.update({n: e["uid"] for n, e in enumerate(emails, start=1)})
    _LAST_RECORDS.update({n: {
        "from": _decode(e["msg"]["From"]),
        "subject": _decode(e["msg"]["Subject"]),
        "snippet": all_snippets.get(e["id"], ""),
        "kind": e["kind"],
        "priority": e["priority"],
        "url": e["url"],
        "account": e.get("account", ""),
    } for n, e in enumerate(emails, start=1)})

    # The one deliberate write: what you've seen stops reappearing. Mail
    # that needs a reply, or is HIGH priority, is skipped — it stays
    # unread so it still needs you. Dropped (never-shown) mail is not
    # touched either.
    # Note: mark_read is per-account, so we'd need to group by account
    # For now, skip mark_read in multi-account mode to be safe
    if account is not None:  # single account mode
        mark_read(_readable_ids(emails), folder)

    head = f"{total_count} emails total across {len(accounts)} account(s). Showing {len(emails)}"
    if dropped:
        detail = ", ".join(f"{n} {c}" for c, n in sorted(dropped.items()))
        head += f", lowest priority omitted ({detail})"
    return head + ":\n" + "\n".join(
        _format(emails, all_snippets, include_snippets))


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
        # Subject line
        lines.append(f"  {n}. {tag}{r['subject'][:72]}")
        # 2-line summary from snippet
        snippet = r.get("snippet", "").strip()
        if snippet:
            # Wrap to ~72 chars for two lines
            import textwrap
            wrapped = textwrap.wrap(snippet, width=72)
            for i, line in enumerate(wrapped[:2]):
                lines.append(f"     {line}")
            if len(wrapped) > 2:
                lines.append(f"     ...")
        # URL
        lines.append(f"     {r['url']}")
        lines.append("")  # blank line between entries
    return "\n".join(lines)


def brief(days=DEFAULT_DAYS, limit=HARD_LIMIT, account=None):
    """
    A short, multi-line summary meant to be shown on screen (or spoken).

    Built entirely from the tags, with no model loop writing prose — the
    counts and kinds are already computed in code, so this returns in seconds
    instead of the minute-plus a full triage takes. That matters when Siri is
    waiting to display it.

    Newlines read as sections on a Show Result card and as pauses in Speak
    Text, so one format serves both.
    """
    # Fewer model calls than a full triage: something is waiting on this, and
    # most of a full pass's time is the classifier.
    read_emails(unread_only=True, limit=limit, days=days, max_model_calls=8, account=account)
    records = list(_LAST_RECORDS.values())

    window = "today" if days == 1 else f"the last {days} days"
    if not records:
        return f"No new mail {window}."

    by_kind = Counter(r["kind"] for r in records)
    actions = [r for r in records if r["kind"] == "action"]
    rejections = [r for r in records if r["kind"] == "rejection"]

    # Count accounts represented
    accounts_seen = set(r.get("account", "") for r in records if r.get("account"))
    acct_suffix = f" (across {len(accounts_seen)} account(s))" if len(accounts_seen) > 1 else ""

    lines = [f"{len(records)} new email{'s' if len(records) != 1 else ''} {window}{acct_suffix}."]

    if actions:
        lines.append(f"\nNeeds a reply ({len(actions)}):")
        for r in actions[:5]:
            acct_tag = f" [{r['account']}]" if r.get("account") else ""
            lines.append(f"• {_sender_name(r['from'])} — {r['subject'][:60]}{acct_tag}")
    else:
        lines.append("\nNothing needs a reply.")

    if rejections:
        who = ", ".join(f"{_sender_name(r['from'])}" + (f" [{r['account']}]" if r.get("account") else "") for r in rejections[:5])
        lines.append(f"\nRejections ({len(rejections)}): {who}")

    if by_kind.get("confirmation"):
        n = by_kind["confirmation"]
        lines.append(f"\n{n} application{'s' if n != 1 else ''} acknowledged.")

    tagged = sum(by_kind[k] for k in ("action", "rejection", "confirmation"))
    rest = len(records) - tagged
    if rest > 0:
        lines.append(f"{rest} other{'s' if rest != 1 else ''}.")

    return "\n".join(lines)


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
    print(f"   [tool] read_email_body #{number}", file=sys.stderr)
    try:
        number = int(number)
    except (TypeError, ValueError):
        return f"ERROR: '{number}' is not a valid email number."

    uid = _LAST_FETCH.get(number)
    if not uid:
        return (f"ERROR: no email #{number} in the current listing. "
                "Call read_emails first.")

    m = _connect(_LAST_FOLDER)  # spam listings live in another folder
    try:
        _, raw = m.uid("FETCH", uid, "(BODY.PEEK[])")
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


def _readable_ids(emails):
    """Stable ids to mark read: shown mail that needs nothing from you."""
    return [e["uid"] for e in emails
            if e["kind"] != "action" and e["priority"] != "HIGH"]


def mark_read(ids, folder="INBOX"):
    """
    Set \\Seen on the given UIDs. Best effort — never raises.

    This is the process's one deliberate write: mail the agent has shown is
    marked read so it stops reappearing. Important mail (needs a reply, or
    HIGH priority) is filtered out by the caller and stays unread. A failed
    flag write must not lose the listing it follows, so failures are logged
    to stderr and swallowed. Addressed by UID (never sequence number): this
    reconnects, and sequence numbers shift when anything is expunged — by
    position it would silently mark the wrong email read.
    """
    ids = [i if isinstance(i, bytes) else str(i).encode("ascii")
           for i in ids if i]
    if not ids or not MARK_READ:
        return
    try:
        m = _connect(folder, readonly=False)
        try:
            typ, _ = m.uid("STORE", b",".join(ids), "+FLAGS", r"(\Seen)")
            if typ != "OK":
                print(f"   [warn] Gmail refused to mark {len(ids)} message(s) "
                      f"read: {typ}", file=sys.stderr)
        finally:
            m.logout()
    except Exception as exc:
        print(f"   [warn] could not mark {len(ids)} message(s) read: {exc}",
              file=sys.stderr)


if __name__ == "__main__":
    # Smoke check: does IMAP work at all, without involving the model?
    print(read_emails())
