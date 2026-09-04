from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from solari_ci import agent
from solari_ci.brain import Proposal
from solari_ci.models import Job, RunResult, Workflow

WORKFLOW_PATH = ".github/workflows/ci.yml"


def make_workflow() -> Workflow:
    job = Job(id="test", name="Test", runs_on="ubuntu-latest")
    return Workflow(path=WORKFLOW_PATH, name="CI", jobs={"test": job})


def make_result() -> RunResult:
    return RunResult("test", 1, 2048, "sb-1", 0.1, 0.0, total_s=3.0, ok=True, solari_cost_usd=0.001)


class FakeBrain:
    def __init__(self, proposal: Proposal) -> None:
        self.proposal = proposal

    def propose(self, evidence_md: str, repo_dir: Path, workflow_rel_path: str) -> Proposal:
        return self.proposal


def setup_agent(monkeypatch: pytest.MonkeyPatch, proposal: Proposal) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(agent.workflow, "fetch_remote", lambda owner_repo, ref=None: [make_workflow()])
    monkeypatch.setattr(agent.history, "fetch_history", lambda *args, **kwargs: None)
    async def fake_sweep(*args: object, **kwargs: object) -> list[RunResult]:
        return _completed_sweep()

    monkeypatch.setattr(agent, "run_sweep", fake_sweep)
    monkeypatch.setattr(agent, "get_brain", lambda name, effort="medium": FakeBrain(proposal))

    def fake_run(
        cmd: list[str], cwd: Path | None = None, input: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            Path(cmd[-1]).mkdir(parents=True)
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/acme/demo/pull/42\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(agent, "_run", fake_run)
    monkeypatch.setattr(agent.report, "write_chart", _write_chart)
    return calls


def _completed_sweep() -> list[RunResult]:
    return [make_result()]


def _failed_result() -> RunResult:
    return RunResult(
        "test", 2, 2048, "sb-2", 0.1, 0.0, total_s=0.0, ok=False, solari_cost_usd=0.0, error="429 rate limited"
    )


def _write_chart(curve: object, path: str) -> bool:
    Path(path).write_bytes(b"png")
    return True


@pytest.mark.asyncio
async def test_run_agent_empty_diff_does_not_attempt_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = Proposal("", "Evidence does not justify a change.", "No change", "fake", "test-model")
    calls = setup_agent(monkeypatch, proposal)

    result = await agent.run_agent("acme/demo", "test", [1], None, "medium", True, False, "main")

    assert result.pr_url is None
    assert result.proposal == proposal
    assert not any(command[:2] == ["git", "push"] for command in calls)
    assert not any(command[:3] == ["gh", "pr", "create"] for command in calls)


@pytest.mark.asyncio
async def test_run_agent_dry_run_skips_publish_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = Proposal(
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n",
        "Use data",
        "Edit",
        "fake",
        "test-model",
    )
    calls = setup_agent(monkeypatch, proposal)

    result = await agent.run_agent("acme/demo", "test", [1], None, "medium", True, True, "main")

    assert result.pr_url is None
    assert not any(command[:2] == ["git", "push"] for command in calls)
    assert not any(command[:3] == ["gh", "pr", "create"] for command in calls)
    assert not any(command[:3] == ["git", "commit", "-m"] for command in calls)


@pytest.mark.asyncio
async def test_run_agent_refuses_when_no_size_succeeded(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = Proposal(
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n",
        "Use data",
        "Edit",
        "fake",
        "test-model",
    )
    calls = setup_agent(monkeypatch, proposal)

    async def failing_sweep(*args: object, **kwargs: object) -> list[RunResult]:
        return [_failed_result()]

    monkeypatch.setattr(agent, "run_sweep", failing_sweep)

    with pytest.raises(agent.NoMeasurementsError):
        await agent.run_agent("acme/demo", "test", [2], None, "medium", True, False, "main")

    assert not any(command[:3] == ["gh", "pr", "create"] for command in calls)


@pytest.mark.asyncio
async def test_run_agent_allow_history_only_bypasses_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = Proposal(
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n",
        "Use data",
        "Edit",
        "fake",
        "test-model",
    )
    setup_agent(monkeypatch, proposal)

    async def failing_sweep(*args: object, **kwargs: object) -> list[RunResult]:
        return [_failed_result()]

    monkeypatch.setattr(agent, "run_sweep", failing_sweep)

    result = await agent.run_agent(
        "acme/demo", "test", [2], None, "medium", True, False, "main", allow_history_only=True
    )

    assert result.pr_url == "https://github.com/acme/demo/pull/42"


@pytest.mark.asyncio
async def test_run_agent_pr_flow_parses_url_and_orders_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = Proposal(
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n",
        "Use data",
        "Edit",
        "fake",
        "test-model",
    )
    calls = setup_agent(monkeypatch, proposal)

    result = await agent.run_agent("acme/demo", "test", [1], None, "medium", True, False, "main")

    assert result.pr_url == "https://github.com/acme/demo/pull/42"
    names = [command[:2] for command in calls]
    assert names.index(["git", "checkout"]) < names.index(["git", "commit"])
    assert names.index(["git", "commit"]) < names.index(["git", "push"])
    assert names.index(["git", "push"]) < names.index(["gh", "pr"])
