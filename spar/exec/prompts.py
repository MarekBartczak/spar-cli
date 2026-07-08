"""Prompt builders for the execution phase (implement and review turns).

Produces two pure functions that build prompts for:
- Implementer: edit files in scope, address remarks, emit verdict with resolutions
- Reviewer: read diff, raise remarks or emit DONE, no code editing allowed
"""

from __future__ import annotations

from pathlib import Path
from spar.exec.tasklist import Task
from spar.state import StateRemark


def build_impl_prompt(
    task: Task,
    artifact_plan_path: Path,
    open_remarks: list[StateRemark],
    warning: str | None = None,
) -> str:
    """Build a prompt for the implementer (edit phase).

    Instructs the implementer to:
    - Edit ONLY files in the task's file scope
    - Read the plan at artifact_plan_path
    - Address each open remark
    - End with a verdict that only resolves remark ids (no DONE emission)
    - Include optional warning if provided

    Args:
        task: The Task to implement
        artifact_plan_path: Path to the artifact plan
        open_remarks: List of open StateRemark to address
        warning: Optional warning text to include

    Returns:
        A formatted prompt string
    """
    files_list = "\n".join(f"  - {f}" for f in task.files)

    if open_remarks:
        remarks_lines = ["Open remarks to address:"]
        for r in open_remarks:
            remarks_lines.append(f"  #{r.remark_id} [{r.severity.name}] ({r.author}): {r.text}")
        remarks_section = "\n" + "\n".join(remarks_lines)
        instruction = (
            "Your task is to implement the changes according to the plan and address the "
            "remarks below. For each remark you accept, you MUST make the corresponding real "
            "code change to the file(s) this turn — a remark marked `accepted` requires an "
            "actual edit on disk, not a prose acknowledgment. Use your file-editing tools. "
            "Reject (with a reason) any remark you will not act on."
        )
    else:
        remarks_section = ""
        instruction = (
            "Your task is to implement the task according to the plan. Create/edit the "
            "file(s) in your scope now, on disk, with real content per the plan. Use your "
            "file-editing tools. Do not merely describe the change."
        )

    warning_section = ""
    if warning:
        warning_section = f"\n\nWarning: {warning}"

    protocol = _IMPL_PROTOCOL_BLOCK

    return f"""\
Task ID: {task.id}
Description: {task.description}

Files in scope (edit ONLY these files):
{files_list}

The implementation plan is located at: {artifact_plan_path}

{instruction}{remarks_section}{warning_section}

{protocol}\
"""


def build_review_prompt(
    task: Task,
    diff_text: str,
    open_remarks: list[StateRemark],
) -> str:
    """Build a prompt for the reviewer (review phase).

    Instructs the reviewer to:
    - Read the provided diff (read-only, must NOT edit code)
    - Raise MUST/NICE remarks or emit DONE
    - NOT emit DONE if there are open MUST-level remarks

    Args:
        task: The Task being reviewed
        diff_text: The diff showing changes made
        open_remarks: List of open StateRemark to consider

    Returns:
        A formatted prompt string
    """
    remarks_section = ""
    if open_remarks:
        remarks_lines = ["Open remarks:"]
        for r in open_remarks:
            remarks_lines.append(f"  #{r.remark_id} [{r.severity.name}] ({r.author}): {r.text}")
        remarks_section = "\n" + "\n".join(remarks_lines)

    protocol = _REVIEW_PROTOCOL_BLOCK

    return f"""\
Task ID: {task.id}
Description: {task.description}

Review the following diff (read-only — you must NOT edit code):

{diff_text}{remarks_section}

{protocol}\
"""


_IMPL_PROTOCOL_BLOCK = """\
End your reply with EXACTLY ONE verdict block, using this syntax verbatim:

<verdict>
status: CONTINUE
resolved:
- #7 accepted
- #9 rejected: <one-line reason you disagree>
</verdict>

Protocol for implementation:
- Make the change by creating/editing files ON DISK with your file-editing tools — real
  content per the plan, not a description of it. The verdict only RECORDS what you did on
  disk; the actual edit is what counts.
- Edit ONLY the files listed in the file scope above.
- A remark you mark `accepted` REQUIRES a matching real edit on disk this turn. Never mark a
  remark accepted without making the corresponding code change; if you will not make the
  edit, mark it `#<id> rejected: <reason>` instead.
- In `resolved:` you MUST address EVERY open remark id listed above, each as either
  `#<id> accepted` (with the edit made) or `#<id> rejected: <why>`.
- Your verdict status must be CONTINUE — do not emit DONE (only the reviewer emits DONE).
- Do not raise new remarks and do not judge your own work — only the reviewer raises remarks and decides DONE/CONTINUE.
"""

_REVIEW_PROTOCOL_BLOCK = """\
End your reply with EXACTLY ONE verdict block, using this syntax verbatim:

<verdict>
status: CONTINUE
remarks:
- [MUST] <a blocking concern that must be fixed>
- [NICE] <an optional, non-blocking suggestion>
</verdict>

Protocol for review:
- Do not edit the code — this is read-only. You are reviewing only, not implementing.
- In `remarks:` raise new concerns, tagged `[MUST]` (blocking) or `[NICE]` (optional).
- Use `status: DONE` only if you have NO blocking `[MUST]` remarks remaining.
- Use `status: CONTINUE` if you have open `[MUST]`/`[NICE]` remarks to raise.
"""
