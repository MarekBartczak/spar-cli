"""Parser and validator for the ``## Tasks`` section of a consensus Plan.

Implements the §4.1 grammar:

    - [<id>] <description> | side=<side> | model=<impl-model> | review=<review-model>
      | deps=<id,id|-> | files=<glob,glob> [ | test=<cmd>]

``parse_task_list`` extracts the section, parses each task line, and validates
side/model/review-model/dependency references against the supplied ``sides``
catalog and ``order`` list, raising :class:`TaskListError` (with the offending
line) on any violation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SECTION_START_RE = re.compile(r"^##\s+Tasks\s*$", re.MULTILINE)
_SECTION_NEXT_RE = re.compile(r"^##\s", re.MULTILINE)
_LINE_RE = re.compile(r"^- \[(?P<id>t\d+)\]\s+(?P<desc>.*?)\s*\|\s*(?P<rest>.*)$")

_REQUIRED_ORDER = ("side", "model", "review", "deps")


class TaskListError(Exception):
    """Raised when the ``## Tasks`` section is missing or fails validation."""


@dataclass(frozen=True)
class Task:
    id: str
    description: str
    side: str
    model: str
    review_model: str
    deps: tuple[str, ...]
    files: tuple[str, ...]
    test: str | None = None


def parse_task_list(plan_text: str, *, sides: dict[str, "SideConfig"], order: list[str]) -> tuple[Task, ...]:
    """Parse and validate the ``## Tasks`` section of ``plan_text``.

    Raises ``TaskListError`` (including the offending line) on any grammar or
    validation violation. File-scope overlap between independently-runnable
    tasks (no transitive dependency either way) is logged as a warning, not
    raised, in this sequential-execution slice.
    """
    section = _extract_tasks_section(plan_text)

    raw_tasks: list[dict] = []
    ids_seen: set[str] = set()
    for line in section.splitlines():
        line = line.strip()
        if not line:
            continue
        raw_tasks.append(_parse_line(line, ids_seen))

    id_set = {rt["id"] for rt in raw_tasks}
    deps_map: dict[str, tuple[str, ...]] = {}

    for rt in raw_tasks:
        _validate_task(rt, sides=sides, order=order, id_set=id_set)
        deps_map[rt["id"]] = rt["deps"]

    _check_cycles(deps_map)

    tasks = tuple(
        Task(
            id=rt["id"],
            description=rt["desc"],
            side=rt["side"],
            model=rt["model"],
            review_model=rt["review"],
            deps=rt["deps"],
            files=rt["files"],
            test=rt["test"],
        )
        for rt in raw_tasks
    )

    _warn_file_overlaps(tasks, deps_map)

    return tasks


def _extract_tasks_section(plan_text: str) -> str:
    start_match = _SECTION_START_RE.search(plan_text)
    if not start_match:
        raise TaskListError("no '## Tasks' section found in plan")

    start = start_match.end()
    next_match = _SECTION_NEXT_RE.search(plan_text, start)
    end = next_match.start() if next_match else len(plan_text)
    return plan_text[start:end]


def _parse_line(line: str, ids_seen: set[str]) -> dict:
    m = _LINE_RE.match(line)
    if not m:
        raise TaskListError(f"malformed task line: {line!r}")

    task_id = m.group("id")
    if task_id in ids_seen:
        raise TaskListError(f"duplicate task id {task_id!r} in line: {line!r}")
    ids_seen.add(task_id)

    desc = m.group("desc")
    remaining = m.group("rest")

    values: dict[str, str] = {}
    for key in _REQUIRED_ORDER:
        if " | " not in remaining:
            raise TaskListError(f"missing required field {key!r} in line: {line!r}")
        field, remaining = remaining.split(" | ", 1)
        field = field.strip()
        parsed_key, sep, parsed_val = field.partition("=")
        if not sep or parsed_key != key:
            raise TaskListError(f"expected field {key + '=...'!r} but got {field!r} in line: {line!r}")
        values[key] = parsed_val

    remaining = remaining.strip()
    if " | " in remaining:
        files_field, test_field = remaining.split(" | ", 1)
    else:
        files_field, test_field = remaining, None

    files_field = files_field.strip()
    fkey, fsep, fval = files_field.partition("=")
    if not fsep or fkey != "files":
        raise TaskListError(f"expected field 'files=...' but got {files_field!r} in line: {line!r}")

    files = tuple(part.strip() for part in fval.split(","))
    if not files or any(not part for part in files):
        raise TaskListError(f"empty or malformed files list in line: {line!r}")

    test_value: str | None = None
    if test_field is not None:
        test_field = test_field.strip()
        if not test_field.startswith("test="):
            raise TaskListError(f"unexpected trailing field {test_field!r} in line: {line!r}")
        test_value = test_field[len("test=") :]

    deps_raw = values["deps"]
    deps = () if deps_raw == "-" else tuple(part.strip() for part in deps_raw.split(","))
    if any(not part for part in deps):
        raise TaskListError(f"empty or malformed deps list in line: {line!r}")

    return {
        "id": task_id,
        "desc": desc,
        "side": values["side"],
        "model": values["model"],
        "review": values["review"],
        "deps": deps,
        "files": files,
        "test": test_value,
        "line": line,
    }


def _validate_task(rt: dict, *, sides: dict, order: list, id_set: set[str]) -> None:
    line = rt["line"]
    side = rt["side"]

    if side not in sides:
        raise TaskListError(f"unknown side {side!r} in line: {line!r}")

    if rt["model"] not in sides[side].models:
        raise TaskListError(f"model {rt['model']!r} not in catalog for side {side!r}, line: {line!r}")

    impl_allowed = getattr(sides[side], "impl_models", ()) or ()
    if impl_allowed and rt["model"] not in impl_allowed:
        raise TaskListError(
            f"model {rt['model']!r} not allowed for implementation on side {side!r} "
            f"(impl_models={list(impl_allowed)}), line: {line!r}"
        )

    if side not in order:
        raise TaskListError(f"side {side!r} not present in order {order!r}, line: {line!r}")

    others = [s for s in order if s != side]
    if len(others) != 1:
        raise TaskListError(
            f"cannot resolve reviewing side for {side!r} (order={order!r}): expected exactly one other "
            f"side, line: {line!r}"
        )
    other_side = others[0]
    if other_side not in sides:
        raise TaskListError(f"unknown side {other_side!r} referenced by order {order!r}")

    if rt["review"] not in sides[other_side].models:
        raise TaskListError(
            f"review model {rt['review']!r} not in catalog for side {other_side!r}, line: {line!r}"
        )

    review_allowed = getattr(sides[other_side], "review_models", ()) or ()
    if review_allowed and rt["review"] not in review_allowed:
        raise TaskListError(
            f"review model {rt['review']!r} not allowed for review on side {other_side!r} "
            f"(review_models={list(review_allowed)}), line: {line!r}"
        )

    for dep in rt["deps"]:
        if dep not in id_set:
            raise TaskListError(f"unknown dependency {dep!r} in line: {line!r}")


def _check_cycles(deps_map: dict[str, tuple[str, ...]]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {task_id: WHITE for task_id in deps_map}

    def visit(node: str) -> None:
        color[node] = GRAY
        for dep in deps_map[node]:
            if color[dep] == GRAY:
                raise TaskListError(f"dependency cycle detected: {node!r} -> {dep!r}")
            if color[dep] == WHITE:
                visit(dep)
        color[node] = BLACK

    for task_id in deps_map:
        if color[task_id] == WHITE:
            visit(task_id)


def _warn_file_overlaps(tasks: tuple[Task, ...], deps_map: dict[str, tuple[str, ...]]) -> None:
    reach_cache: dict[str, set[str]] = {}

    def reach(node: str) -> set[str]:
        if node in reach_cache:
            return reach_cache[node]
        result: set[str] = set()
        for dep in deps_map[node]:
            result.add(dep)
            result |= reach(dep)
        reach_cache[node] = result
        return result

    for t in tasks:
        reach(t.id)

    for i in range(len(tasks)):
        for j in range(i + 1, len(tasks)):
            a, b = tasks[i], tasks[j]
            if b.id in reach_cache[a.id] or a.id in reach_cache[b.id]:
                continue
            overlap = set(a.files) & set(b.files)
            if overlap:
                logger.warning(
                    "File-scope overlap between independent tasks %s and %s: %s",
                    a.id,
                    b.id,
                    sorted(overlap),
                )
