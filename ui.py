"""
Terminal presentation for the interactive chat.

All colour is decorative and strictly optional. It turns on only when stdout
is a real terminal and NO_COLOR is unset, so piped output (Siri, cron, a log
file) stays byte-clean — no ANSI codes to strip.

The thinking indicator is the same idea applied to stderr: it only draws when
stderr is a terminal, and it always clears itself before the answer prints,
so a non-interactive run never sees it.
"""

import os
import sys

_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[38;5;39m"
_GREEN = "\033[38;5;40m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _on():
    """Colour only on a real terminal, and never under NO_COLOR."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _wrap(code, text):
    return f"{code}{text}{_RESET}" if _on() else text


def bold(text):
    return _wrap(_BOLD, text)


def dim(text):
    return _wrap(_DIM, text)


def cyan(text):
    return _wrap(_CYAN, text)


def green(text):
    return _wrap(_GREEN, text)


def red(text):
    return _wrap(_RED, text)


def thinking(message="thinking…"):
    """
    Show a transient status line on its own line. Returns a token to clear it
    with, or None when nothing was drawn (stderr is not a terminal).
    """
    if not sys.stderr.isatty():
        return None
    sys.stderr.write("\n" + dim(message) + " ")
    sys.stderr.flush()
    return True


def done_thinking(token):
    """Clear the status line so the answer can take its place."""
    if token:
        sys.stderr.write("\r\x1b[K")
        sys.stderr.flush()
