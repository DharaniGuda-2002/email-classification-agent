#!/usr/bin/env python3
"""
Generate a signed Siri shortcut you can double-click to install.

    python shortcuts/make_shortcut.py

Builds a two-action shortcut — Run Shell Script, Show Result — and signs it
with macOS's own `shortcuts` tool so Shortcuts will accept it. No manual
building in the Shortcuts app; you click "Add Shortcut" once and you are done.

Saying "Hey Siri, Check My Emails" runs the agent and shows the brief on
screen. The Run Shell Script action blocks until the model replies, so Siri
waits for the real answer rather than a placeholder.

macOS only — the Run Shell Script action does not exist in Shortcuts on
iPhone, and the script needs this Mac's Python, LM Studio and credentials.
"""

import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT / "shortcuts" / "check-emails.sh"
NAME = "Check My Emails"
OUT = PROJECT / f"{NAME}.shortcut"


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


def build():
    # Invoking the runner as a command lets its own shebang pick bash, so the
    # BASH_SOURCE path detection inside it works even though this action's
    # shell is zsh. The runner handles PATH, the model server, and a spoken
    # fallback if anything fails.
    # Guard the embedded path. If the project moves, the old path goes stale
    # and the action produces nothing — which reads as Siri silently ignoring
    # you. This turns that silence into an instruction instead.
    script = (
        f'S={_quote(RUNNER)}\n'
        f'if [ -x "$S" ]; then "$S"; else\n'
        f'  echo "Email agent not found at $S."\n'
        f'  echo "It probably moved. Reinstall: run ./run.sh --shortcut there."\n'
        f'fi\n'
    )
    shell_uid = str(uuid.uuid4()).upper()

    return {
        "WFWorkflowClientVersion": "2038.1.3",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 946986751,
                           "WFWorkflowIconGlyphNumber": 61440},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": ["NCWidget"],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowActions": [
            action("is.workflow.actions.runshellscript",
                   {"Script": script, "Shell": "/bin/zsh",
                    "InputMode": "to stdin", "RunAsAdministrator": False},
                   uid=shell_uid),
            # Show Result puts the text on screen — as a Siri card by voice,
            # or a dialog when run from the app.
            action("is.workflow.actions.showresult",
                   {"Text": output_of(shell_uid, "Shell Script Result")}),
        ],
    }


def _quote(path):
    return "'" + str(path).replace("'", "'\\''") + "'"


def main():
    if not RUNNER.exists():
        print(f"Missing {RUNNER}. Run from the project, with shortcuts/ intact.",
              file=sys.stderr)
        return 1

    # The signing tool identifies the input by extension: a .plist suffix is
    # rejected with "isn't in the correct format" no matter the contents. It
    # must end in .shortcut.
    unsigned = PROJECT / ".unsigned.shortcut"
    unsigned.write_bytes(plistlib.dumps(build()))

    result = subprocess.run(
        ["shortcuts", "sign", "--mode", "anyone",
         "-i", str(unsigned), "-o", str(OUT)],
        capture_output=True, text=True, check=False,
    )
    unsigned.unlink(missing_ok=True)

    if result.returncode != 0:
        print("Could not sign the shortcut.", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        print("\nBuild it by hand instead — see shortcuts/README.md.",
              file=sys.stderr)
        return 1

    print(f"Created: {OUT.name}\n")
    print("Install it (opens Shortcuts, then click Add Shortcut):")
    print(f"  open {_quote(OUT)}\n")
    print(f'Then say:  "Hey Siri, {NAME}"')
    print("\nThe shortcut embeds this folder's path, so regenerate it if you "
          "move the project.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
