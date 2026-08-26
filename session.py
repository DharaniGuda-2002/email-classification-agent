"""
Conversation memory, so a follow-up means something.

Siri runs every turn as a fresh process. Without history on disk, "what about
the last three days?" arrives with no idea what came before, and the model
answers as if you had just walked in.

Sessions expire. A "today" from this morning should not still be in context
tonight, and a stale window silently answering the wrong question is worse
than starting over.
"""

import json
import re
import time
from pathlib import Path

SESSION_DIR = Path(__file__).resolve().parent / ".sessions"
TTL_MINUTES = 15      # a conversation goes cold fast
MAX_MESSAGES = 40     # a long chat must not run away with the context window


def _path(name):
    # The name arrives from a shortcut argument, so keep it to a safe charset
    # rather than trusting it as a filename.
    safe = re.sub(r"[^A-Za-z0-9_-]", "", name)[:40] or "default"
    return SESSION_DIR / f"{safe}.json"


def load(name):
    """Prior messages, or [] when there is no live session."""
    if not name:
        return []
    path = _path(name)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []      # missing or corrupt is not worth failing a turn over

    if (time.time() - data.get("updated_at", 0)) / 60 > TTL_MINUTES:
        return []

    messages = data.get("messages", [])
    return messages if isinstance(messages, list) else []


def save(name, messages):
    """Persist messages, trimmed to the retention cap."""
    if not name:
        return
    try:
        SESSION_DIR.mkdir(exist_ok=True)
        _path(name).write_text(json.dumps({
            "updated_at": time.time(),
            "messages": messages[-MAX_MESSAGES:],
        }))
    except OSError:
        pass           # memory is a convenience; losing it must not break a turn


def clear(name):
    """Forget this conversation. Returns True if there was one."""
    try:
        _path(name).unlink()
        return True
    except OSError:
        return False
