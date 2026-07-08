"""Contract tests for real AI CLI flag drift.

These tests verify that real claude and codex CLIs still honor their documented
flags when invoked in non-interactive mode. They never run by default (gated by
SPAR_CONTRACT_TESTS env var) and are only used to detect CLI flag changes between
releases.

Each subprocess call has a 120s timeout (enforced by subprocess.run's own
`timeout` argument) and one-line diagnostics on failure telling the maintainer
which CLI contract drifted.

Note: these tests invoke codex with `--sandbox read-only` (safety, since the
test runner's filesystem is not a disposable sandbox), whereas the production
CodexAdapter uses `--sandbox workspace-write` (spar/adapters/codex.py) so the
debate can actually edit the artifact file.
"""

import json
import os
import shutil
import subprocess

import pytest

from spar.adapters.codex import CodexAdapter


# Module-level skip: entire module is skipped unless SPAR_CONTRACT_TESTS is set.
pytestmark = pytest.mark.skipif(
    not os.environ.get("SPAR_CONTRACT_TESTS"),
    reason="opt-in: set SPAR_CONTRACT_TESTS=1",
)


@pytest.mark.contract
class TestClaudeContract:
    """Contract tests for claude CLI."""

    @pytest.fixture(autouse=True)
    def skip_if_no_claude(self):
        """Skip this test class if claude binary is not available."""
        if not shutil.which("claude"):
            pytest.skip("claude CLI not found in PATH")

    def test_claude_new_session_then_resume(self):
        """Test: new session via -p --output-format json, then --resume with its session_id.

        Both legs run in a single test function because the session_id obtained
        from the first `claude` invocation only exists for the lifetime of this
        test; splitting into separate test methods would lose it between runs.
        """
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json", "Say OK"],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert (
            result.returncode == 0
        ), f"claude new session: exit {result.returncode}; stderr: {result.stderr[:200]}"

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"claude new session: stdout not valid JSON: {exc}; stdout: {result.stdout[:200]}"
            )

        assert "session_id" in payload, (
            "claude new session: missing session_id in JSON; "
            f"keys: {list(payload.keys())}"
        )
        assert "result" in payload, (
            "claude new session: missing result in JSON; "
            f"keys: {list(payload.keys())}"
        )

        session_id = payload["session_id"]

        # --- resume leg ---
        result = subprocess.run(
            [
                "claude",
                "-p",
                "--resume",
                session_id,
                "--output-format",
                "json",
                "Say OK again",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert (
            result.returncode == 0
        ), f"claude resume: exit {result.returncode}; stderr: {result.stderr[:200]}"

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"claude resume: stdout not valid JSON: {exc}; stdout: {result.stdout[:200]}"
            )

        assert "result" in payload, (
            "claude resume: missing result in JSON; "
            f"keys: {list(payload.keys())}"
        )


@pytest.mark.contract
class TestCodexContract:
    """Contract tests for codex CLI."""

    @pytest.fixture(autouse=True)
    def skip_if_no_codex(self):
        """Skip this test class if codex binary is not available."""
        if not shutil.which("codex"):
            pytest.skip("codex CLI not found in PATH")

    def test_codex_new_session_then_resume(self, tmp_path):
        """Test: new session via codex exec --json, then `resume <session_id>`.

        Both legs run in a single test function so the session_id obtained
        from the first `codex exec` invocation is available for the resume
        leg without relying on cross-test instance state.
        """
        last_msg_path = tmp_path / "last-message.txt"

        result = subprocess.run(
            [
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--output-last-message",
                str(last_msg_path),
                "Say OK",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert (
            result.returncode == 0
        ), f"codex new session: exit {result.returncode}; stderr: {result.stderr[:200]}"

        # Check stdout has at least one parseable JSONL line
        parsed_lines = sum(
            1
            for line in result.stdout.splitlines()
            if line.strip() and _is_json_object(line)
        )

        assert (
            parsed_lines > 0
        ), f"codex new session: no parseable JSONL lines in stdout: {result.stdout[:200]}"

        session_id = CodexAdapter._extract_session_id(result.stdout)

        # Check last-message file exists and is non-empty
        assert (
            last_msg_path.exists()
        ), "codex new session: --output-last-message file not created"
        last_msg_content = last_msg_path.read_text()
        assert (
            last_msg_content
        ), "codex new session: --output-last-message file is empty"

        if session_id is None:
            pytest.skip("codex did not emit a session_id; cannot test resume")

        # --- resume leg ---
        last_msg_path_resume = tmp_path / "last-message-resume.txt"

        result = subprocess.run(
            [
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--output-last-message",
                str(last_msg_path_resume),
                "resume",
                session_id,
                "Say OK again",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert (
            result.returncode == 0
        ), f"codex resume: exit {result.returncode}; stderr: {result.stderr[:200]}"

        assert (
            last_msg_path_resume.exists()
        ), "codex resume: --output-last-message file not created"
        last_msg_resume_content = last_msg_path_resume.read_text()
        assert (
            last_msg_resume_content
        ), "codex resume: --output-last-message file is empty"


def _is_json_object(line: str) -> bool:
    """True if `line` parses as a JSON object (tolerates non-JSON lines)."""
    try:
        return isinstance(json.loads(line), dict)
    except json.JSONDecodeError:
        return False
