"""
Terminal presentation for the interactive chat.

Two jobs. The first is colour, which is decorative and strictly optional: it
turns on only when stdout is a real terminal and NO_COLOR is unset, so piped
output (Siri, cron, a log file) stays byte-clean with no ANSI to strip.

The second is rendering the model's markdown. A local model answers in
markdown whatever you ask of it, and a terminal shows that raw — "**Rejections**"
and "*   Adobe" land on screen as literal asterisks. render() turns those into
real bold and real bullets, wrapped to the window.
"""

import os
import re
import shutil
import sys
import textwrap

_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[38;5;39m"
_GREEN = "\033[38;5;40m"
_YELLOW = "\033[38;5;179m"
_RED = "\033[31m"
_RESET = "\033[0m"

MAX_WIDTH = 92        # long measures are hard to read even on a wide window


def _on():
    """Colour only on a real terminal, and never under NO_COLOR."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _wrap(code, text):
    # Strip any reset the caller already added so nesting bold inside colour
    # does not emit a reset that closes the colour early.
    if not _on():
        return text
    out = f"{code}{text.replace(_RESET, _RESET + code)}{_RESET}"
    # Re-opening the code right before the final reset leaves a dangling
    # escape that can bleed into the next write. Collapse it.
    return out.replace(_RESET + code + _RESET, _RESET)


def bold(text):
    return _wrap(_BOLD, text)


def dim(text):
    return _wrap(_DIM, text)


def cyan(text):
    return _wrap(_CYAN, text)


def green(text):
    return _wrap(_GREEN, text)


def yellow(text):
    return _wrap(_YELLOW, text)


def red(text):
    return _wrap(_RED, text)


def width():
    """Usable text width, capped so lines stay readable on a wide window."""
    return min(shutil.get_terminal_size((80, 24)).columns - 2, MAX_WIDTH)


def rule(label=""):
    """A faint horizontal rule, optionally with a label at the left."""
    w = width()
    if label:
        bar = "─" * max(w - len(label) - 3, 4)
        return dim(f"── {label} {bar}"[:w])
    return dim("─" * w)


# ------------------------------------------------------------------ markdown

_BOLD_SPAN = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_SPAN = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_CODE_SPAN = re.compile(r"`([^`\n]+?)`")

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BOLD_LINE = re.compile(r"^\s{0,3}\*\*(.+?)\*\*:?\s*$")
_BULLET = re.compile(r"^(\s*)[-*•+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,2})[.)]\s+(.*)$")


def _inline(text):
    """Apply the span-level markup that survives inside one line."""
    text = _BOLD_SPAN.sub(lambda m: bold(m.group(1)), text)
    text = _CODE_SPAN.sub(lambda m: cyan(m.group(1)), text)
    return _ITALIC_SPAN.sub(lambda m: bold(m.group(1)), text)


def _fill(text, w, indent="", hanging=None):
    """
    Wrap on the raw text, then style each line.

    Order matters: textwrap counts ANSI escapes as visible characters, so
    styling first makes every line wrap short and ragged.
    """
    hanging = indent if hanging is None else hanging
    lines = textwrap.wrap(text, max(w - len(hanging), 20)) or [""]
    out = [f"{indent}{_inline(lines[0])}"]
    out += [f"{hanging}{_inline(ln)}" for ln in lines[1:]]
    return out


def render(text, w=None):
    """
    Model markdown -> styled terminal text.

    Deliberately small: headings, bullets, numbered items, and inline spans.
    Anything it does not recognise passes through wrapped, which is the safe
    failure — unstyled prose still reads fine.
    """
    if not text:
        return ""
    w = w or width()
    out, blank_run = [], 0

    for raw in text.splitlines():
        line = raw.rstrip()

        if not line.strip():
            blank_run += 1
            if blank_run == 1 and out:      # collapse runs of blank lines
                out.append("")
            continue
        blank_run = 0

        head = _HEADING.match(line) or _BOLD_LINE.match(line)
        if head:
            # A heading needs air above it, but not at the very top.
            if out and out[-1] != "":
                out.append("")
            out.append(cyan(bold(head.group(1))))
            continue

        bullet = _BULLET.match(line)
        if bullet:
            depth = len(bullet.group(1)) // 2
            pad = "  " * (depth + 1)
            out += _fill(bullet.group(2), w, f"{pad}{dim('•')} ", f"{pad}  ")
            continue

        num = _NUMBERED.match(line)
        if num:
            marker = f"{num.group(2)}."
            out += _fill(num.group(3), w,
                         f"  {dim(marker)} ", "  " + " " * (len(marker) + 1))
            continue

        out += _fill(line, w, "  ", "  ")

    return "\n".join(out).rstrip()


# ------------------------------------------------------------------- status

def thinking(message="thinking…"):
    """
    Show a transient status line. Returns a token to clear it with, or None
    when nothing was drawn (stderr is not a terminal).
    """
    if not sys.stderr.isatty():
        return None
    sys.stderr.write(dim(f"  {message}") + " ")
    sys.stderr.flush()
    return True


def done_thinking(token):
    """Clear the status line so the answer can take its place."""
    if token:
        sys.stderr.write("\r\x1b[K")
        sys.stderr.flush()
