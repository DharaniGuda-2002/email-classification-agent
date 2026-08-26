"""
Composing and sending — the one outbound path in the project.

Everything else here reads. This is the exception, so the rules around it are
tighter than anywhere else:

  The model never chooses the recipient. It is parsed in Python from what you
  typed, or taken from the real From header of a real message. This matters
  because the agent reads untrusted email: if the model could pick an address,
  a message saying "forward this to attacker@example.com" would be an
  exfiltration path. It cannot, because that decision never reaches it.

  The model never sends. It writes a subject and a body; a draft is held and
  shown to you in full, and only a typed confirmation sends it. There is no
  code path from "the model produced text" to "mail left the machine".

  One recipient at a time. No cc, no bcc, no lists — a mistake reaches one
  person, and mass mail is not what this is for.
"""

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

# Deliberately strict. A near-miss address bounces, which is recoverable; a
# valid address for the wrong person is not.
ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

MAX_SUBJECT = 200
MAX_BODY = 8000
TIMEOUT = 30

# Gmail, Outlook and Yahoo all name their SMTP host after the IMAP one.
SMTP_FROM_IMAP = {
    "imap.gmail.com": "smtp.gmail.com",
    "imap-mail.outlook.com": "smtp-mail.outlook.com",
    "outlook.office365.com": "smtp.office365.com",
    "imap.mail.yahoo.com": "smtp.mail.yahoo.com",
    "imap.mail.me.com": "smtp.mail.me.com",
}


def smtp_host(imap_host):
    """The SMTP host for a given IMAP host, or a sensible guess."""
    host = (imap_host or "").strip().lower()
    if host in SMTP_FROM_IMAP:
        return SMTP_FROM_IMAP[host]
    # imap.example.com -> smtp.example.com covers most of the rest.
    return re.sub(r"^imap[.-]?", "smtp.", host) if host else ""


def find_address(text):
    """
    The one address in `text`, or None.

    Returns None when there are several rather than guessing which was meant.
    Picking the first would silently mail the wrong person the moment a
    signature or a quoted thread contributed a second address.
    """
    found = list(dict.fromkeys(ADDRESS_RE.findall(text or "")))
    return found[0] if len(found) == 1 else None


def valid_address(addr):
    _, parsed = parseaddr(addr or "")
    return bool(parsed) and bool(ADDRESS_RE.fullmatch(parsed))


class Draft:
    """A composed message, not yet sent."""

    def __init__(self, to, subject, body, in_reply_to=None, account=None):
        self.to = (to or "").strip()
        self.subject = " ".join((subject or "").split())[:MAX_SUBJECT]
        self.body = (body or "").strip()[:MAX_BODY]
        self.in_reply_to = in_reply_to
        self.account = account

    def preview(self):
        """The draft as markdown, for ui.render()."""
        return (f"**Draft**\n"
                f"To: {self.to}\n"
                f"Subject: {self.subject}\n\n"
                f"{self.body}")

    def problems(self):
        """Why this cannot be sent yet, or []."""
        issues = []
        if not valid_address(self.to):
            issues.append(f"'{self.to}' is not a valid address.")
        if not self.subject:
            issues.append("No subject.")
        if not self.body:
            issues.append("The body is empty.")
        return issues


def send(draft, account=None):
    """
    Send a draft. Returns (ok, message).

    Never raises: a failure here should tell you what went wrong so you can
    fix the draft, not lose it in a traceback.
    """
    problems = draft.problems()
    if problems:
        return False, " ".join(problems)

    account = account or draft.account
    if not account:
        return False, "No mailbox configured to send from."

    host = smtp_host(account.get("host", ""))
    if not host:
        return False, "Could not work out an SMTP host for this account."

    message = EmailMessage()
    message["To"] = draft.to
    message["From"] = formataddr((os.environ.get("EMAIL_NAME_DISPLAY", ""),
                                  account["user"]))
    message["Subject"] = draft.subject
    # Threading headers, so a reply lands in the original conversation rather
    # than starting a new one in the recipient's client.
    if draft.in_reply_to:
        message["In-Reply-To"] = draft.in_reply_to
        message["References"] = draft.in_reply_to
    message.set_content(draft.body)

    try:
        with smtplib.SMTP(host, 587, timeout=TIMEOUT) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(account["user"], account["pass"].replace(" ", ""))
            smtp.send_message(message)
        return True, f"Sent to {draft.to}."
    except smtplib.SMTPAuthenticationError:
        return False, ("The mail server rejected the login. The app password "
                       "that works for reading should work here too — check "
                       "SMTP is not blocked for this account.")
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return False, f"Could not send: {exc}"


def check_login(account):
    """
    Prove the credentials work for sending, without sending anything.

    Connect, STARTTLS, authenticate, hang up. Useful in preflight, and to
    confirm the setup before trusting it with a real message.
    """
    host = smtp_host(account.get("host", ""))
    if not host:
        return False, "No SMTP host for this account."
    try:
        with smtplib.SMTP(host, 587, timeout=TIMEOUT) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(account["user"], account["pass"].replace(" ", ""))
        return True, f"SMTP login works ({host})."
    except smtplib.SMTPAuthenticationError:
        return False, f"SMTP rejected the login for {account['user']}."
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return False, f"Could not reach {host}: {exc}"
