"""Contract tests for real AI CLI flag drift.

These tests verify that real claude and codex CLIs still honor their documented
flags when invoked in non-interactive mode. They never run by default (gated by
SPAR_CONTRACT_TESTS env var) and are only used to detect CLI flag changes between
releases.

Each test has a 120s timeout and one-line diagnostics on failure telling the
maintainer which CLI contract drifted.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from spar.adapters.codex import CodexAdapter


# Module-level skip: entire module is skipped unless SPAR_CONTRACT_TESTS is set.
pytestmark = pytest.mark.skipif(
    not os.environ.get("SPAR_CONTRACT_TESTS"),
    reason="opt-in: set SPAR_CONTRACT_TESTS=1",
)


@pytest.mark.contract
@pytest.mark.timeout(120)
class TestClaudeContract:
    """Contract tests for claude CLI."""

    @pytest.fixture(autouse=True)
    def skip_if_no_claude(self):
        """Skip this test class if claude binary is not available."""
        if not shutil.which("claude"):
            pytest.skip("claude CLI not found in PATH")

    @pytest.mark.timeout(120)
    def test_claude_new_session(self, tmp_path):
        """Test: claude -p --output-format json 'Say OK' → exit 0, valid JSON."""
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

        # Stash the session_id for the resume test
        self.session_id = payload["session_id"]

    @pytest.mark.timeout(120)
    def test_claude_resume(self):
        """Test: claude resume with previous session ID."""
        # Ensure we have a session_id from the new_session test
        # (pytest runs test_claude_new_session first due to alphabetical order,
        # but this test uses instance state, so we run both to be safe)
        if not hasattr(self, "session_id"):
            pytest.skip("No session_id from new_session test")

        result = subprocess.run(
            [
                "claude",
                "-p",
                "--resume",
                self.session_id,
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
@pytest.mark.timeout(120)
class TestCodexContract:
    """Contract tests for codex CLI."""

    @pytest.fixture(autouse=True)
    def skip_if_no_codex(self):
        """Skip this test class if codex binary is not available."""
        if not shutil.which("codex"):
            pytest.skip("codex CLI not found in PATH")

    @pytest.mark.timeout(120)
    def test_codex_new_session(self, tmp_path):
        """Test: codex exec --json --sandbox read-only ... 'Say OK' → valid JSONL + last-msg."""
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
        parsed_lines = 0
        session_id = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    parsed_lines += 1
                    # Try to extract session_id using CodexAdapter logic
                    if "session_id" in obj:
                        session_id = obj["session_id"]
                    elif isinstance(obj.get("msg"), dict) and "session_id" in obj["msg"]:
                        session_id = obj["msg"]["session_id"]
            except json.JSONDecodeError:
                # Tolerate non-JSON lines as per CodexAdapter
                pass

        assert (
            parsed_lines > 0
        ), f"codex new session: no parseable JSONL lines in stdout: {result.stdout[:200]}"

        # Check last-message file exists and is non-empty
        assert (
            last_msg_path.exists()
        ), "codex new session: --output-last-message file not created"
        last_msg_content = last_msg_path.read_text()
        assert (
            last_msg_content
        ), "codex new session: --output-last-message file is empty"

        # Stash the session_id for the resume test
        self.session_id = session_id

    @pytest.mark.timeout(120)
    def test_codex_resume(self, tmp_path):
        """Test: codex exec resume with previous session ID."""
        if not hasattr(self, "session_id") or self.session_id is None:
            pytest.skip("No session_id from new_session test")

        last_msg_path = tmp_path / "last-message-resume.txt"

        result = subprocess.run(
            [
                "codex",
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--output-last-message",
                str(last_msg_path),
                "resume",
                self.session_id,
                "Say OK again",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert (
            result.returncode == 0
        ), f"codex resume: exit {result.returncode}; stderr: {result.stderr[:200]}"

        # Check last-message file exists and is non-empty
        assert (
            last_msg_path.exists()
        ), "codex resume: --output-last-message file not created"
        last_msg_content = last_msg_path.read_text()
        assert (
            last_msg_content
        ), "codex resume: --output-last-message file is empty"
