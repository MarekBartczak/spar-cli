from spar.config import SideConfig
from spar.exec.tasklist import parse_task_list, Task, TaskListError
import pytest

SIDES = {
    "claude": SideConfig(adapter="claude", command="claude", models=("opus","sonnet")),
    "codex": SideConfig(adapter="codex", command="codex", models=("gpt-5.5","gpt-5.4")),
}
ORDER = ["claude", "codex"]

PLAN = """# Plan
blah
## Tasks
- [t1] config bits | side=claude | model=sonnet | review=gpt-5.4 | deps=- | files=spar/config.py,tests/test_config.py
- [t2] parser | side=codex | model=gpt-5.5 | review=opus | deps=t1 | files=spar/exec/tasklist.py | test=pytest tests/test_exec_tasklist.py -q
## Next
"""

def test_parses_tasks():
    tasks = parse_task_list(PLAN, sides=SIDES, order=ORDER)
    assert [t.id for t in tasks] == ["t1", "t2"]
    assert tasks[0] == Task("t1","config bits","claude","sonnet","gpt-5.4",(),("spar/config.py","tests/test_config.py"),None)
    assert tasks[1].deps == ("t1",)
    assert tasks[1].test == "pytest tests/test_exec_tasklist.py -q"

def test_missing_tasks_section_errors():
    with pytest.raises(TaskListError):
        parse_task_list("# Plan\nno tasks here\n", sides=SIDES, order=ORDER)

def test_unknown_side_errors():
    p = "## Tasks\n- [t1] x | side=ghost | model=opus | review=gpt-5.4 | deps=- | files=a.py\n"
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_model_not_in_catalog_errors():
    p = "## Tasks\n- [t1] x | side=claude | model=gpt-5.5 | review=gpt-5.4 | deps=- | files=a.py\n"
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_review_model_must_be_other_side_catalog():
    p = "## Tasks\n- [t1] x | side=claude | model=opus | review=opus | deps=- | files=a.py\n"
    with pytest.raises(TaskListError):  # review must be in codex catalog
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_unknown_dep_errors():
    p = "## Tasks\n- [t1] x | side=claude | model=opus | review=gpt-5.4 | deps=t9 | files=a.py\n"
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_dependency_cycle_errors():
    p = ("## Tasks\n"
         "- [t1] a | side=claude | model=opus | review=gpt-5.4 | deps=t2 | files=a.py\n"
         "- [t2] b | side=codex | model=gpt-5.5 | review=opus | deps=t1 | files=b.py\n")
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_bad_id_format_errors():
    p = "## Tasks\n- [x1] q | side=claude | model=opus | review=gpt-5.4 | deps=- | files=a.py\n"
    with pytest.raises(TaskListError):
        parse_task_list(p, sides=SIDES, order=ORDER)

def test_concurrent_file_overlap_is_warning_not_error(caplog):
    # sequential slice: overlap between independent tasks warns, does not raise
    p = ("## Tasks\n"
         "- [t1] a | side=claude | model=opus | review=gpt-5.4 | deps=- | files=shared.py\n"
         "- [t2] b | side=codex | model=gpt-5.5 | review=opus | deps=- | files=shared.py\n")
    tasks = parse_task_list(p, sides=SIDES, order=ORDER)  # no raise
    assert len(tasks) == 2


def test_model_outside_impl_models_rejected():
    from spar.config import SideConfig

    sides = {
        "claude": SideConfig(
            adapter="claude", command="claude",
            models=("opus", "sonnet", "haiku"), default_model="sonnet",
            impl_models=("opus", "sonnet"),
        ),
        "codex": SideConfig(
            adapter="codex", command="codex",
            models=("gpt-5.5",), default_model="gpt-5.5",
        ),
    }
    plan = """## Tasks
- [t1] do it | side=claude | model=haiku | review=gpt-5.5 | deps=- | files=a.py
"""
    with pytest.raises(TaskListError) as excinfo:
        parse_task_list(plan, sides=sides, order=["claude", "codex"])
    assert "impl_models" in str(excinfo.value)


def test_empty_impl_models_allows_any_catalog_model():
    from spar.config import SideConfig

    sides = {
        "claude": SideConfig(
            adapter="claude", command="claude",
            models=("haiku",), default_model="haiku",
        ),
        "codex": SideConfig(
            adapter="codex", command="codex",
            models=("gpt-5.5",), default_model="gpt-5.5",
        ),
    }
    plan = """## Tasks
- [t1] do it | side=claude | model=haiku | review=gpt-5.5 | deps=- | files=a.py
"""
    tasks = parse_task_list(plan, sides=sides, order=["claude", "codex"])
    assert tasks[0].model == "haiku"
