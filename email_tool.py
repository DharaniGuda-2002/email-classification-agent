"""
IMAP extraction. Two tools, split on a security boundary:

    read_emails()      -> headers only. Nothing an attacker wrote.
    read_email_body()  -> one body, explicitly labeled untrusted.

Reading never mutates the inbox: readonly=True + BODY.PEEK.
"""

import email
import imaplib
import os
import re
from email.header import decode_header, make_header
from html.parser import HTMLParser

from dotenv import load_dotenv

load_dotenv()

IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")

MAX_BODY_CHARS = 1500
DEFAULT_LIMIT = 3     # latest N emails
HARD_LIMIT = 10       # ceiling the model cannot argue past

# Maps the numbers the model sees (1, 2, 3...) to real IMAP ids.
# Module state because the agent process is single-threaded and short-lived.
# If that stops being true, this is the first thing that breaks.
_LAST_FETCH = {}


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
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


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

def _connect():
    user = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")
    if not user or not password:
        raise RuntimeError(
            "EMAIL_USER and EMAIL_PASS must be set. Copy .env.example to .env."
        )
    m = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    m.login(user, password)
    m.select("INBOX", readonly=True)  # cannot set \Seen, structurally
    return m


def read_emails(unread_only=True, limit=DEFAULT_LIMIT):
    """Headers only. Safe against a hostile inbox."""
    print("   [tool] read_emails")
    _LAST_FETCH.clear()

    # The model supplies this. Clamp it rather than trusting it.
    try:
        limit = max(1, min(int(limit), HARD_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT

    m = _connect()
    try:
        _, data = m.search(None, "UNSEEN" if unread_only else "ALL")
        ids = data[0].split()
        if not ids:
            return "No emails found."

        lines = []
        for n, msg_id in enumerate(ids[-limit:][::-1], start=1):
            _, raw = m.fetch(msg_id, "(BODY.PEEK[HEADER])")
            msg = email.message_from_bytes(raw[0][1])
            _LAST_FETCH[n] = msg_id
            lines.append(
                f"{n}. From: {_decode(msg['From'])} | "
                f"Subject: {_decode(msg['Subject'])} | "
                f"Date: {msg['Date']}"
            )
        return "\n".join(lines)
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

    m = _connect()
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