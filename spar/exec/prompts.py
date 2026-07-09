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
    files_inline = ", ".join(task.files)

    lead = (
        f"You are IMPLEMENTING task {task.id}. This is a coding task, NOT a review. Using "
        f"your file-editing tools, CREATE/EDIT the following file(s) on disk NOW with real, "
        f"working content per the plan: {files_inline}. Do not comment on other tasks, do "
        "not review the plan, do not just describe changes — write the code."
    )

    if open_remarks:
        remarks_lines = ["Open remarks to address:"]
        for r in open_remarks:
            remarks_lines.append(f"  #{r.remark_id} [{r.severity.name}] ({r.author}): {r.text}")
        remarks_section = "\n" + "\n".join(remarks_lines)
        instruction = (
            "Implement the task according to the plan and address the remarks below. For "
            "each remark you accept, you MUST make the corresponding real code change on "
            "disk this turn — a remark marked `accepted` requires an actual edit, not a "
            "prose acknowledgment. Reject (with a reason) any remark you will not act on."
        )
    else:
        remarks_section = ""
        instruction = "Implement the task according to the plan. Do not merely describe the change."

    warning_section = ""
    if warning:
        warning_section = f"\n\nWarning: {warning}"

    protocol = _IMPL_PROTOCOL_BLOCK

    return f"""\
{lead}

Task ID: {task.id}
Description: {task.description}

Files in scope (edit ONLY these files):
{files_list}

Read the plan at {artifact_plan_path} for context.

{instruction}{remarks_section}{warning_section}

{protocol}\
"""


def build_review_prompt(
    task: Task,
    diff_text: str,
    open_remarks: list[StateRemark],
    foreign_files: tuple[tuple[str, tuple[str, ...]], ...] = (),
    merged_files: tuple[str, ...] = (),
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
        foreign_files: ``(task_id, file_globs)`` pairs for other, not-yet-merged
            tasks' file scopes — legitimately absent on this branch.
        merged_files: Actual paths already merged into integration (and still
            present there) — invisible in ``diff_text`` but present on disk.

    Returns:
        A formatted prompt string
    """
    remarks_section = ""
    if open_remarks:
        remarks_lines = ["Open remarks:"]
        for r in open_remarks:
            remarks_lines.append(f"  #{r.remark_id} [{r.severity.name}] ({r.author}): {r.text}")
        remarks_section = "\n" + "\n".join(remarks_lines)

    context_section = ""
    context_lines: list[str] = []
    if foreign_files:
        context_lines.append(
            "Files owned by other, not-yet-merged tasks (they arrive with "
            "later merges and may be ABSENT on this branch): do NOT require "
            "this task to create them — their absence is NOT a defect by "
            "itself; check that references to them match these planned "
            "names/paths. HOWEVER, if this task's own files HARD-reference a "
            "foreign file (import/include/link/build-source) so that this "
            "task cannot build or pass its test on THIS branch, raise a "
            "[MUST] naming the missing dependency — that is a plan-ordering "
            "defect (the task should have depended on the owner)."
        )
        for task_id, globs in foreign_files:
            context_lines.append(f"  {task_id}: {', '.join(globs)}")
    if merged_files:
        context_lines.append(
            "Files already merged from earlier tasks (present on this branch "
            "even though they do not appear in the diff above):"
        )
        for path in merged_files:
            context_lines.append(f"  {path}")
    if context_lines:
        context_section = "\n" + "\n".join(context_lines) + "\n"

    protocol = _REVIEW_PROTOCOL_BLOCK

    return f"""\
Task ID: {task.id}
Description: {task.description}

Review the following diff (read-only — you must NOT edit code):

{diff_text}
{context_section}{remarks_section}

{protocol}\
"""


_IMPL_PROTOCOL_BLOCK = """\
After you have written the files, end your reply with a verdict block. This is a trailing
formality that records what you already did on disk — it does not substitute for the edit
itself:

<verdict>
status: CONTINUE
resolved:
- #7 accepted
- #9 rejected: <one-line reason you disagree>
</verdict>

Protocol for the verdict:
- status is always CONTINUE — you are the implementer, you never end the review; only the
  reviewer emits DONE. Do not emit DONE.
- Include a `resolved:` section ONLY if there are open remarks listed above, resolving
  EVERY one of them as `#<id> accepted` (with the edit made on disk) or
  `#<id> rejected: <why>`.
- If there are NO open remarks, OMIT the `resolved:` section entirely — do not write any
  placeholder line (e.g. do NOT write "(no open remarks listed this turn)").
- Do NOT include a `remarks:` section — you do not raise remarks; only the reviewer does.
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
- Treat a reference to a MISSING file as a defect only if it matches none of
  the diff, the context lists above (if any), and a file already present in the repository.
  This does NOT override the hard-reference rule of the foreign-files section
  (a plan-ordering defect stays a [MUST] even though the file matches the
  foreign list).
- Use `status: DONE` only if you have NO blocking `[MUST]` remarks remaining.
- Use `status: CONTINUE` if you have open `[MUST]`/`[NICE]` remarks to raise.
"""
