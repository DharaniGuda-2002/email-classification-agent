"""
Tests for the parts that can be wrong quietly.

No network, no mailbox, no model — everything here runs on constructed
messages, so it works on a fresh clone with no .env.

Most of these are regressions. Each one is a bug that shipped, survived
review, and was only caught by running against a real inbox. The comment on
each says what broke, because a test whose purpose is forgotten gets deleted
the first time it becomes inconvenient.

    python test_agent.py
"""

import email as E
import os
import sys
from datetime import datetime, timedelta, timezone

import email_tool as t

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    mark = "\033[32m✓\033[0m" if condition else "\033[31m✗\033[0m"
    print(f"  {mark} {name}" + (f"\n      {detail}" if not condition else ""))


def msg(**headers):
    """An email.Message with the given headers and no body."""
    m = E.message.Message()
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    return m


# --------------------------------------------------------------- rejections

def test_rejections():
    print("\nrejection detection")
    # re.VERBOSE strips literal spaces, so "other candidates" once compiled
    # as "othercandidates" and matched nothing. Every gap needs an explicit \s+.
    for text in ("we chose other candidates",
                 "moving forward with another candidate",
                 "we regret to inform you",
                 "unfortunately we cannot proceed",
                 "you were not selected",
                 "the position has been filled"):
        check(f"matches: {text[:38]}", bool(t.REJECTION_RE.search(text)))

    for text in ("we would like to schedule an interview",
                 "your application was received"):
        check(f"ignores: {text[:38]}", not t.REJECTION_RE.search(text))


def _is_action(text):
    return bool(t.ACTION_STRONG_RE.search(text) or t.ACTION_WEAK_RE.search(text))


def test_actions():
    print("\naction detection")
    for text in ("please complete the assessment by Friday",
                 "schedule a call to discuss next steps",
                 "your assignment deadline is tomorrow"):
        check(f"matches: {text[:38]}", _is_action(text))

    # Bare "offer" is the native tongue of every promotion in an inbox;
    # an Amex "earn $300" ad was tagged as an action item because of it.
    check("ignores promotional 'offer'",
          not _is_action("earn $300 with our best offer today"))
    check("matches job offer",
          _is_action("we are pleased to extend a job offer"))

    # interview/assessment are STRONG and outrank a confirmation; the please/
    # deadline boilerplate is WEAK and yields to one.
    check("interview is a strong signal",
          bool(t.ACTION_STRONG_RE.search("we'd like to schedule an interview")))
    check("'next steps' is only weak",
          not t.ACTION_STRONG_RE.search("we'll share next steps soon")
          and bool(t.ACTION_WEAK_RE.search("we'll share next steps soon")))


# ------------------------------------------------------------------ senders

def test_sender_classification():
    print("\nsender classification")
    # "no_reply@" with an underscore slipped past a "-?" separator, so a
    # Grifols rejection was tagged as though a human had written it.
    for addr in ("no_reply@x.com", "no-reply@x.com", "noreply@x.com",
                 "do_not_reply@x.com", "careers@x.com", "hiring@x.com"):
        check(f"role address: {addr}", bool(t.ROLE_ADDRESS_RE.match(addr)))

    for addr in ("jane.smith@uni.edu", "yaswanth@ncsu.edu"):
        check(f"person: {addr}", not t.ROLE_ADDRESS_RE.match(addr))

    # Marketing from Starbucks and AliExpress is addressed to you by name and
    # reads personal; the ESP headers are what actually give it away.
    check("Feedback-ID means bulk", t._is_bulk(msg(Feedback_ID="1:2:3")))
    check("List-Unsubscribe means bulk", t._is_bulk(msg(List_Unsubscribe="<u>")))
    check("X-SES-Outgoing means bulk", t._is_bulk(msg(X_SES_Outgoing="1")))
    check("plain headers are not bulk", not t._is_bulk(msg(Subject="hi")))


# ------------------------------------------------------------------- bodies

def test_body_extraction():
    print("\nbody extraction")
    # A Grifols rejection arrived as raw <p> tags labelled text/plain. The
    # markup ate the whole snippet budget before reaching the real sentence.
    m = E.message_from_string("Content-Type: text/plain\n\n"
                              "<p>Dear X</p><p>Unfortunately we chose another.</p>")
    body = t._extract_body(m)
    check("strips html mislabelled as text/plain", "<p>" not in body, body[:60])
    check("keeps the actual text", "Unfortunately" in body, body[:60])

    # Long headers arrive folded across lines; left in, they break the
    # one-line-per-email listing the model counts on to number things.
    check("collapses folded headers", "\n" not in t._decode("Subject\r\n continued"))
    check("decodes rfc2047", t._decode("=?utf-8?q?caf=C3=A9?=") == "café")


def test_hostile_input():
    print("\nhostile and malformed input")
    # A charset the sender invented raises LookupError. Unguarded, one bad
    # email took down the whole triage — and spam is full of them.
    for charset in ("nonsense", "utf-99", "", "x" * 200):
        m = E.message_from_string(
            f"Content-Type: text/plain; charset={charset}\n\nhello")
        check(f"survives charset={charset[:12] or 'empty'}",
              t._extract_body(m) == "hello")

    check("empty message", t._extract_body(E.message.Message()) == "")
    check("null bytes survive", "a" in t._clean("a\x00b"))
    check("4MB body is capped", len(t._clean("x " * 2_000_000)) <= t.MAX_BODY_CHARS)
    check("emoji and RTL survive", t._decode("🎉 مرحبا 🎉").startswith("🎉"))
    check("broken rfc2047 does not crash",
          isinstance(t._decode("=?utf-8?B?!!!?="), str))
    check("unclosed html", t._strip_html("<p><div><span>text") == "text")
    check("script contents dropped",
          t._strip_html("<script>evil()</script>safe") == "safe")
    check("sender with no @", t._sender_name("just-a-name") == "unknown")
    check("garbage thread id", t._gmail_url("X-GM-THRID notanumber") == "")
    check("select on empty list", t._select([], 5) == ([], {}))
    check("fetch_map on empty ids", t._fetch_map(None, [], "(X)") == {})


def test_clean():
    print("\nbody cleaning")
    # Bodies go into a context window, so the cap is not cosmetic.
    check("truncates to MAX_BODY_CHARS",
          len(t._clean("x " * 5000)) <= t.MAX_BODY_CHARS)
    # Quoted chains and signatures are pure context cost.
    check("cuts quoted reply chains",
          "old message" not in t._clean("New reply.\nOn Monday, X wrote:\nold message"))
    check("cuts signatures",
          "sig here" not in t._clean("Body text.\n--\nsig here"))


# -------------------------------------------------------------------- kinds

def test_kind():
    print("\nkind tagging")
    m = msg(Subject="Update", From="careers@acme.com")

    check("rejection from body",
          t._kind(m, "we chose another candidate", "primary") == "rejection")
    check("action from body",
          t._kind(m, "please complete the assessment", "primary") == "action")
    check("confirmation from body",
          t._kind(m, "your application was received", "primary") == "confirmation")
    check("nothing to tag", t._kind(m, "here is our newsletter", "primary") == "")

    # "received your JOB application" — the exact "received your application"
    # pattern missed the commonest phrasing.
    check("confirmation with a word between",
          t._kind(m, "we received your job application", "primary") == "confirmation")

    # A confirmation that looks personal must stay a confirmation: the content
    # signal beats the _is_personal guess. Six acknowledgements landed under
    # "needs a reply" when this order was reversed.
    human = msg(Subject="Thank you for applying", From="jane.recruiter@startup.com")
    check("confirmation beats the personal guess",
          t._kind(human, "Thank you for applying to Thatch", "primary")
          == "confirmation")

    # "deadline" and "offer" are the native tongue of marketing, so a
    # promotion matching an action pattern must still not become an action.
    check("promotions are never tagged",
          t._kind(m, "please complete your order, deadline today", "promotions") == "")
    check("social is never tagged",
          t._kind(m, "please confirm your connection", "social") == "")


def test_priority():
    print("\npriority")
    plain = msg(Subject="hi", From="a@b.com")
    bulk = msg(Subject="hi", From="a@b.com", List_Unsubscribe="<u>")

    check("starred is HIGH", t._priority(plain, "\\Starred", "primary") == "HIGH")
    check("promotions are LOW", t._priority(plain, "", "promotions") == "LOW")
    check("spam is LOW", t._priority(plain, "", "spam") == "LOW")
    check("gmail important is HIGH",
          t._priority(plain, "\\Important", "primary") == "HIGH")
    # Important *and* bulk is a newsletter Gmail happens to like.
    check("important bulk is only NORMAL",
          t._priority(bulk, "\\Important", "primary") == "NORMAL")
    check("bulk is LOW", t._priority(bulk, "", "primary") == "LOW")
    # A tag outranks the header signals.
    check("action tag forces HIGH",
          t._priority(bulk, "", "primary", "action") == "HIGH")
    check("rejection tag is NORMAL",
          t._priority(plain, "", "primary", "rejection") == "NORMAL")


# ------------------------------------------------------------------ speech

def test_sender_name():
    print("\nsender names (spoken output)")
    # Falling back to the first domain label spoke "us" for a greenhouse
    # rejection; the second-level domain is the company.
    check("second-level domain, not the first label",
          t._sender_name("no-reply@us.greenhouse-mail.io") == "greenhouse-mail")
    check("generic display name is skipped",
          t._sender_name("Human Resources <no_reply@humanresources.grifols.com>")
          == "grifols")
    check("real display name is kept",
          t._sender_name("Starbucks Careers <hiring@jobs.starbucks.com>")
          == "Starbucks Careers")
    check("person's name is kept",
          t._sender_name("Jane Smith <jane@ncsu.edu>") == "Jane Smith")
    check("empty header does not crash", t._sender_name("") == "unknown")


# ------------------------------------------------------------------ windows

def test_window():
    print("\ntime window")
    cutoff, since = t._window(1)

    def sent(hours_ago):
        d = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return msg(Date=d.strftime("%a, %d %b %Y %H:%M:%S %z"))

    check("2h ago is inside", t._within_window(sent(2), cutoff))
    check("23h ago is inside", t._within_window(sent(23), cutoff))
    check("30h ago is outside", not t._within_window(sent(30), cutoff))
    # Discarding real mail over a malformed header is the worse failure.
    check("undated mail is kept", t._within_window(msg(Subject="x"), cutoff))
    check("unparseable date is kept", t._within_window(msg(Date="not a date"), cutoff))


# ---------------------------------------------------------------- selection

def test_selection():
    print("\npriority selection")
    items = [{"category": "promotions", "priority": "LOW"},
             {"category": "primary", "priority": "HIGH"},
             {"category": "social", "priority": "LOW"},
             {"category": "updates", "priority": "NORMAL"}]

    kept, dropped = t._select(items, 2)
    check("keeps highest priority", [e["priority"] for e in kept] == ["HIGH", "NORMAL"])
    check("preserves newest-first order", kept == [items[1], items[3]])
    check("reports what it dropped", dict(dropped) == {"promotions": 1, "social": 1})
    check("no-op under the limit", t._select(items, 9) == (items, {}))


# -------------------------------------------------------------------- fetch

def test_fetch_map():
    print("\nfetch mapping")

    class FakeIMAP:
        # Gmail answers in ascending id while we ask newest-first. Zipping the
        # two lists paired every snippet with the wrong email — silently.
        def fetch(self, ids, spec):
            return ("OK", [(b"2 (BODY[] {2}", b"b2"), (b"1 (BODY[] {2}", b"b1")])

    got = t._fetch_map(FakeIMAP(), [b"1", b"2"], "(BODY.PEEK[])")
    check("keys off response id, not order",
          got[b"1"][1] == b"b1" and got[b"2"][1] == b"b2", str(got))


# --------------------------------------------------------------------- urls

def test_gmail_links():
    print("\ngmail links")
    prefix = "1 (X-GM-THRID 1871208223318298130 X-GM-LABELS ())"
    url = t._gmail_url(prefix)
    check("hex-encodes the thread id", url.endswith("19f7ddf70bad4e12"), url)
    # /u/0/ opens whichever account is first, which is often the wrong mailbox.
    check("keys the account by address", "/mail/u/" in url)
    check("no thread id means no link", t._gmail_url("1 (BODY[HEADER] {5}") == "")
    check("empty prefix means no link", t._gmail_url("") == "")


# ------------------------------------------------------------------- config

def test_siri_log():
    print("\nsiri logging")
    import agent
    tmp = "/tmp/siri_test.log"
    if os.path.exists(tmp):
        os.remove(tmp)
    original = agent.SIRI_LOG
    try:
        agent.SIRI_LOG = __import__("pathlib").Path(tmp)
        agent.siri_log("ASK   test")
        agent.siri_log("REPLY 'hi'")
        body = open(tmp).read()
        check("writes a timestamped ASK line",
              "ASK   test" in body and body[:4].isdigit())
        check("appends rather than overwrites", body.count("\n") == 2)
    finally:
        agent.SIRI_LOG = original
        if os.path.exists(tmp):
            os.remove(tmp)

    # A broken log path must never raise — logging can't break a request.
    agent.SIRI_LOG = __import__("pathlib").Path("/nonexistent-dir/x.log")
    try:
        agent.siri_log("should not raise")
        check("bad log path does not raise", True)
    except Exception:
        check("bad log path does not raise", False)
    finally:
        agent.SIRI_LOG = original


def test_session():
    print("\nconversation memory")
    import session
    original = session.SESSION_DIR
    tmp = __import__("pathlib").Path("/tmp/sess_test")
    try:
        session.SESSION_DIR = tmp
        tmp.mkdir(exist_ok=True)

        check("no session yet", session.load("x") == [])
        session.save("x", [{"role": "user", "content": "hi"}])
        check("round-trips", len(session.load("x")) == 1)

        # A name arrives from a shortcut argument, so it must never escape
        # the session directory.
        check("path traversal is sanitised",
              session._path("../../etc/passwd").parent == tmp)
        check("empty name falls back", session._path("").name == "default.json")

        # A long chat must not run away with the context window.
        session.save("x", [{"role": "user", "content": str(i)}
                           for i in range(session.MAX_MESSAGES + 20)])
        check("trims to the cap", len(session.load("x")) == session.MAX_MESSAGES)

        # Stale context silently answering the wrong question is worse than
        # starting over.
        import json
        import time
        (tmp / "old.json").write_text(json.dumps({
            "updated_at": time.time() - (session.TTL_MINUTES + 5) * 60,
            "messages": [{"role": "user", "content": "stale"}]}))
        check("expired session is dropped", session.load("old") == [])

        (tmp / "bad.json").write_text("{not json")
        check("corrupt session does not raise", session.load("bad") == [])

        check("clear removes it", session.clear("x") and session.load("x") == [])
    finally:
        session.SESSION_DIR = original
        __import__("shutil").rmtree(tmp, ignore_errors=True)


def test_config():
    print("\nconfiguration")
    # A typo in .env used to be an unexplained ValueError at import time.
    #
    # These set the variable for real. Both assertions here once read
    # _env_int("NOPE_NOT_SET", 50), which returns the default from the
    # "unset" branch without ever reaching the try/except they claimed to
    # cover — deleting the fallback entirely left the suite green.
    var = "TEST_ENV_INT"
    try:
        os.environ[var] = "abc"
        check("non-numeric falls back to the default", t._env_int(var, 50) == 50)
        os.environ[var] = "0"
        check("zero clamps up to 1", t._env_int(var, 50) == 1)
        os.environ[var] = "-5"
        check("negative clamps up to 1", t._env_int(var, 50) == 1)
        os.environ[var] = "120"
        check("a real number is used", t._env_int(var, 50) == 120)
        os.environ[var] = ""
        check("empty falls back to the default", t._env_int(var, 50) == 50)
    finally:
        os.environ.pop(var, None)
    check("unset falls back to the default", t._env_int(var, 50) == 50)

    import agent
    # "3 emails", "5 more" and "2 minutes" all parse as number-then-word and
    # were being swallowed as tag corrections instead of reaching the model.
    for text in ("3 emails", "5 more", "2 minutes"):
        check(f"not a correction: {text}", not agent.CORRECT_RE.match(text))
    for text in ("3 is a rejection", "correct 7 action", "2 none"):
        check(f"is a correction: {text}", bool(agent.CORRECT_RE.match(text)))


if __name__ == "__main__":
    print("Running tests — no network, no mailbox, no model.")
    for fn in (test_rejections, test_actions, test_sender_classification,
               test_body_extraction, test_hostile_input, test_clean,
               test_kind, test_priority, test_sender_name,
               test_siri_log, test_session, test_window, test_selection,
               test_fetch_map, test_gmail_links, test_config):
        fn()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("\nfailed:")
        for name in FAIL:
            print(f"  - {name}")
    sys.exit(1 if FAIL else 0)
