#!/usr/bin/env python3
"""
Generate a signed Siri shortcut you can double-click to install.

    python shortcuts/make_shortcut.py

Builds a two-action shortcut — Run Shell Script, Show Result — and signs it
with macOS's own `shortcuts` tool so Shortcuts will accept it. No manual
building in the Shortcuts app; you click "Add Shortcut" once and you are done.

Saying "Hey Siri, Mabel" runs the agent and shows the brief on
screen. The Run Shell Script action blocks until the model replies, so Siri
waits for the real answer rather than a placeholder.

macOS only — the Run Shell Script action does not exist in Shortcuts on
iPhone, and the script needs this Mac's Python, LM Studio and credentials.
"""

import base64
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT / "shortcuts" / "check-emails.sh"
ASK_RUNNER = PROJECT / "shortcuts" / "ask-email.sh"
ICON = PROJECT / "assets" / "Mabel.icns"

NAME = "Mabel"                    # fixed summary
ASK_NAME = "Ask Mabel"            # conversational
OUT = PROJECT / f"{NAME}.shortcut"
ASK_OUT = PROJECT / f"{ASK_NAME}.shortcut"


def action(identifier, params, uid=None):
    params = {**params, "UUID": uid or str(uuid.uuid4()).upper()}
    return {"WFWorkflowActionIdentifier": identifier,
            "WFWorkflowActionParameters": params}


def output_of(uid, name):
    """
    A reference to a previous action's output.

    Actions do not reliably chain implicitly — a Show Result with no explicit
    input displays nothing, which looks like Siri ignoring you. The hand-off
    is wired by UUID, the way the Shortcuts app does it internally.
    """
    return {"WFSerializationType": "WFTextTokenAttachment",
            "Value": {"OutputUUID": uid, "OutputName": name,
                      "Type": "ActionOutput"}}


def _guarded(runner):
    """
    The shell one-liner, wrapped in an existence check.

    If the project moves, the embedded path goes stale and Run Shell Script
    produces nothing — which reads as Siri silently ignoring you. This turns
    that silence into an instruction.
    """
    return (
        f'S={_quote(runner)}\n'
        f'if [ -x "$S" ]; then "$S"; else\n'
        f'  echo "Email agent not found at $S."\n'
        f'  echo "It probably moved. Reinstall: run ./mabel shortcut there."\n'
        f'fi\n'
    )


def _envelope(actions):
    # Embed the custom Mabel icon (ICNS -> base64 PNG data for shortcuts)
    icon_data = ""
    if ICON.exists():
        try:
            icon_data = base64.b64encode(ICON.read_bytes()).decode()
        except OSError:
            pass

    icon_dict = {}
    if icon_data:
        # shortcuts format: base64-encoded ICNS under WFWorkflowIconData
        icon_dict = {"WFWorkflowIconData": icon_data}
    else:
        # Fallback: system glyph
        icon_dict = {"WFWorkflowIconStartColor": 946986751,
                     "WFWorkflowIconGlyphNumber": 61440}

    return {
        "WFWorkflowClientVersion": "2038.1.3",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowIcon": icon_dict,
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": ["NCWidget"],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowActions": actions,
    }


def build():
    """The fixed summary: no question, just today's triage on screen."""
    shell_uid = str(uuid.uuid4()).upper()
    return _envelope([
        action("is.workflow.actions.runshellscript",
               {"Script": _guarded(RUNNER), "Shell": "/bin/zsh",
                "InputMode": "to stdin", "RunAsAdministrator": False},
               uid=shell_uid),
        # Show Result puts the text on screen — a Siri card by voice, or a
        # dialog when run from the app.
        action("is.workflow.actions.showresult",
               {"Text": output_of(shell_uid, "Shell Script Result")}),
    ])


def build_ask():
    """
    The conversational one: Siri asks what you want, the agent answers.

    Three actions, wired by UUID. The spoken text goes to the script on
    stdin, and the script passes --session, so a follow-up in the same
    minute still knows what you were talking about.
    """
    ask_uid = str(uuid.uuid4()).upper()
    shell_uid = str(uuid.uuid4()).upper()
    return _envelope([
        action("is.workflow.actions.ask",
               {"WFAskActionPrompt": "What do you want to know?",
                "WFInputType": "Text"},
               uid=ask_uid),
        action("is.workflow.actions.runshellscript",
               {"Script": _guarded(ASK_RUNNER), "Shell": "/bin/zsh",
                "InputMode": "to stdin", "RunAsAdministrator": False,
                "Input": output_of(ask_uid, "Provided Input")},
               uid=shell_uid),
        action("is.workflow.actions.showresult",
               {"Text": output_of(shell_uid, "Shell Script Result")}),
    ])


def _quote(path):
    return "'" + str(path).replace("'", "'\\''") + "'"


def _sign(workflow, out):
    """Write and sign one shortcut. Returns True on success."""
    # The signing tool identifies the input by extension: a .plist suffix is
    # rejected with "isn't in the correct format" no matter the contents. It
    # must end in .shortcut.
    unsigned = PROJECT / ".unsigned.shortcut"
    unsigned.write_bytes(plistlib.dumps(workflow))
    result = subprocess.run(
        ["shortcuts", "sign", "--mode", "anyone",
         "-i", str(unsigned), "-o", str(out)],
        capture_output=True, text=True, check=False,
    )
    unsigned.unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"Could not sign {out.name}.", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        return False
    return True


def main():
    for runner in (RUNNER, ASK_RUNNER):
        if not runner.exists():
            print(f"Missing {runner}. Run from the project, shortcuts/ intact.",
                  file=sys.stderr)
            return 1
        runner.chmod(0o755)   # Shortcuts will not run a non-executable script

    built = []
    for workflow, out, name in ((build(), OUT, NAME),
                                (build_ask(), ASK_OUT, ASK_NAME)):
        if not _sign(workflow, out):
            print("\nBuild it by hand instead — see shortcuts/README.md.",
                  file=sys.stderr)
            return 1
        built.append((out, name))

    print("Created two shortcuts:\n")
    print(f"  {NAME:16}  today's summary, no question asked")
    print(f"  {ASK_NAME:16}  asks what you want, then answers\n")
    print("Install both (each opens Shortcuts — click Add Shortcut):")
    for out, _ in built:
        print(f"  open {_quote(out)}")
    print(f'\nThen say:  "Hey Siri, {NAME}"')
    print(f'           "Hey Siri, {ASK_NAME}"  ->  "any interviews this week?"')
    print("\nThey embed this folder's path, so regenerate if you move it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
