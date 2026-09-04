from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from solari_ci import brain


def test_get_brain_prefers_codex_then_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setattr(brain.shutil, "which", lambda name: "/usr/local/bin/codex")
    assert isinstance(brain.get_brain(None), brain.CodexBrain)

    monkeypatch.setattr(brain.shutil, "which", lambda name: None)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    assert isinstance(brain.get_brain(None), brain.GeminiBrain)

    monkeypatch.delenv("GOOGLE_API_KEY")
    with pytest.raises(brain.BrainError, match="No brain is available"):
        brain.get_brain(None)


def test_codex_brain_reads_diff_and_parses_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workflow_path = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("name: CI\njobs: {}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str], cwd: Path | None = None, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[0] == "codex":
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text("Changed the timeout.\nSUMMARY: Add a timeout\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(
            cmd,
            0,
            "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n",
            "",
        )

    monkeypatch.setattr(brain, "_run", fake_run)
    proposal = brain.CodexBrain(effort="high").propose(
        "# evidence\n## Findings\nNo findings.",
        tmp_path,
        ".github/workflows/ci.yml",
    )

    assert proposal.brain == "codex"
    assert proposal.model == "gpt-5.6-luna"
    assert proposal.summary == "Add a timeout"
    assert proposal.rationale == "Changed the timeout."
    assert calls[-1] == ["git", "diff"]


def test_gemini_brain_validates_fenced_diff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    workflow_path = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("name: CI\njobs: {}\n", encoding="utf-8")
    diff = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "candidates": [
                    {"content": {"parts": [{"text": f"Reason\nSUMMARY: Cache it\n```diff\n{diff}```"}]}}
                ]
            }

    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str], cwd: Path | None = None, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(brain.httpx, "post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(brain, "_run", fake_run)

    proposal = brain.GeminiBrain().propose("evidence", tmp_path, ".github/workflows/ci.yml")

    assert proposal.diff == diff
    assert proposal.summary == "Cache it"
    assert calls == [["git", "apply", "--check"]]


def test_gemini_brain_returns_empty_diff_when_validation_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workflow_path = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("name: CI\njobs: {}\n", encoding="utf-8")

    class Response:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "Reason\n```diff\nnot a patch\n```"}]}}
                ]
            }

    def fake_run(
        cmd: list[str], cwd: Path | None = None, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, "", "bad patch")

    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(brain.httpx, "post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(brain, "_run", fake_run)

    proposal = brain.GeminiBrain().propose("evidence", tmp_path, ".github/workflows/ci.yml")

    assert proposal.diff == ""
    assert "bad patch" in proposal.rationale
