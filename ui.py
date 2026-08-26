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
import threading
import time

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

# Colour carries meaning here, not decoration. Every heading in the same
# cyan wasted the strongest signal the terminal has: with these you can scan
# a triage without reading it — amber is the only thing wanting anything.
SECTION_COLORS = (
    ("needs a reply", "amber"),
    ("rejection", "red"),
    ("confirmation", "green"),
    ("worth knowing", "cyan"),
    ("noise", "dim"),
    ("moving forward", "green"),
    ("waiting", "amber"),
    ("draft", "cyan"),
)
_BOLD_LINE = re.compile(r"^\s{0,3}\*\*(.+?)\*\*:?\s*$")
_BULLET = re.compile(r"^(\s*)[-*•+]\s+(.*)$")
_NUMBERED = re.compile(r"^(\s*)(\d{1,2})[.)]\s+(.*)$")


def _paint(name):
    """The colour function for a name, defaulting to cyan."""
    return {"amber": yellow, "red": red, "green": green,
            "cyan": cyan, "dim": dim}.get(name, cyan)


def _section_color(heading):
    """Match a heading to its meaning. Falls back to cyan for anything new."""
    low = heading.lower()
    for needle, name in SECTION_COLORS:
        if needle in low:
            return name
    return "cyan"


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
    # Which section we are inside, so its bullets match its heading.
    last_color = ["cyan"]

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
            title = head.group(1)
            tint = _paint(_section_color(title))
            # The count is context, not the point — dimmed so the eye lands
            # on the words first.
            count = re.search(r"\s*\((\d+)\)\s*$", title)
            if count:
                out.append(tint(bold(title[:count.start()]))
                           + dim(f"  {count.group(1)}"))
            else:
                out.append(tint(bold(title)))
            last_color[0] = _section_color(title)
            continue

        bullet = _BULLET.match(line)
        if bullet:
            depth = len(bullet.group(1)) // 2
            pad = "  " * (depth + 1)
            marker = _paint(last_color[0])("•")
            out += _fill(bullet.group(2), w, f"{pad}{marker} ", f"{pad}  ")
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
    A live status line while the model works. Returns a token to clear with,
    or None when nothing was drawn (stderr is not a terminal).

    It counts up because the wait is genuinely long — twenty seconds when the
    model is cold — and a line that never changes reads as a hang. The
    counter is the difference between "it is working" and "it has died".
    """
    if not sys.stderr.isatty():
        return None

    stop = threading.Event()

    def tick():
        started = time.monotonic()
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        i = 0
        while not stop.wait(0.1):
            elapsed = time.monotonic() - started
            # Seconds only appear once the wait is worth remarking on, so a
            # fast answer does not flash a stopwatch at you.
            clock = f"  {elapsed:.0f}s" if elapsed >= 3 else ""
            sys.stderr.write(
                f"\r\x1b[K  {dim(frames[i % len(frames)])} "
                f"{dim(message)}{dim(clock)}")
            sys.stderr.flush()
            i += 1

    worker = threading.Thread(target=tick, daemon=True)
    worker.start()
    return (stop, worker)


def done_thinking(token):
    """Stop the status line and clear it, so the answer takes its place."""
    if not token:
        return
    stop, worker = token
    stop.set()
    worker.join(timeout=0.5)
    sys.stderr.write("\r\x1b[K")
    sys.stderr.flush()
