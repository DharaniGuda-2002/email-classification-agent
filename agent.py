import json, requests

BASE = "http://localhost:1234/v1/chat/completions"
MODEL = "google/gemma-4-e4b"   # must be a tool-capable model

# ---------------------------------------------------------------
# FAKE DATA — replace with real IMAP later
# ---------------------------------------------------------------
FAKE_INBOX = [
    {"from": "billing@aws.com",      "subject": "Your invoice is ready",        "unread": True},
    {"from": "mom@gmail.com",        "subject": "call me when you get a chance", "unread": True},
    {"from": "no-reply@github.com",  "subject": "[repo] PR #42 needs review",   "unread": True},
    {"from": "deals@spamco.biz",     "subject": "70% OFF EVERYTHING!!!",        "unread": True},
    {"from": "hr@work.com",          "subject": "Timesheet due Friday",         "unread": False},
{"from": "no-reply@github.com",  "subject": " urgent PR #42 needs review",   "unread": True}
]

def read_emails(unread_only=True):
    print("   [tool] read_emails called")          # so you can SEE it fire
    mails = [m for m in FAKE_INBOX if m["unread"]] if unread_only else FAKE_INBOX
    if not mails:
        return "No emails."
    return "\n".join(f"{i+1}. From: {m['from']} | Subject: {m['subject']}"
                     for i, m in enumerate(mails))

TOOL_IMPL = {"read_emails": read_emails}

TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_emails",
        "description": "Read emails from the user's inbox. Returns sender and subject.",
        "parameters": {
            "type": "object",
            "properties": {
                "unread_only": {
                    "type": "boolean",
                    "description": "If true, only unread emails. Default true.",
                },
            },
            "required": [],
        },
    },
}]

# ---------------------------------------------------------------
# AGENT
# ---------------------------------------------------------------
def call_model(messages, max_steps=8):
    """Run one turn: model -> maybe tools -> model -> ... -> text."""
    for _ in range(max_steps):
        try:
            resp = requests.post(BASE, json={
                "model": MODEL,
                "messages": messages,
                "tools": TOOLS,
            }, timeout=120)
            r = resp.json()
        except Exception as e:
            return f"[connection error] {e}"

        if "choices" not in r:
            return f"[api error] {r}"

        msg = r["choices"][0]["message"]
        messages.append(msg)

        calls = msg.get("tool_calls")
        if not calls:
            return msg.get("content") or "[empty response]"

        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            if name in TOOL_IMPL:
                try:
                    result = TOOL_IMPL[name](**args)
                except Exception as e:
                    result = f"Tool error: {e}"
            else:
                result = f"Unknown tool: {name}"

            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": str(result),
            })

    return "[hit step limit]"


def main():
    messages = [{
        "role": "system",
        "content": "You are an email assistant. Use the read_emails tool to look at "
                   "the user's inbox. Be concise.",
    }]

    # --- startup task: runs automatically, no input needed ---
    STARTUP_TASK = "Scan my inbox and give me a short summary of unread emails."
    print("=" * 55)
    print("Startup task:", STARTUP_TASK)
    print("=" * 55)
    messages.append({"role": "user", "content": STARTUP_TASK})
    print("\nAgent:", call_model(messages), "\n")

    # --- then hand control to the user ---
    print("-" * 55)
    print("Chat with the agent. Type 'quit' to exit, 'reset' to clear history.")
    print("-" * 55)

    while True:
        try:
            user = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user:
            continue
        if user.lower() in ("quit", "exit"):
            break
        if user.lower() == "reset":
            del messages[1:]
            print("(history cleared)")
            continue

        messages.append({"role": "user", "content": user})
        print("\nAgent:", call_model(messages))

    print("\nBye.")

if __name__ == "__main__":
    main()