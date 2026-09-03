from __future__ import annotations

import asyncio
import base64
from typing import Any

import pytest

from solari_ci import runner, shims
from solari_ci.models import Job, RepoSpec, RunResult, Step


class FakeClient:
    def __init__(self, *, nproc: str = "2\n", poll_outputs: list[str] | None = None) -> None:
        self.nproc = nproc
        self.poll_outputs = list(poll_outputs or ["0\noutput\n"])
        self.calls: list[tuple[str, str, list[str], int, str | None]] = []
        self.created: list[tuple[int, int]] = []
        self.deleted: list[str] = []

    async def create_sandbox(self, cpu: int, mem_mb: int, **kwargs: Any) -> dict[str, str]:
        self.created.append((cpu, mem_mb))
        return {"sandboxId": "sb-1"}

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)

    async def exec(
        self,
        sandbox_id: str,
        cmd: str,
        args: list[str],
        timeout_ms: int = 24_000,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((sandbox_id, cmd, args, timeout_ms, cwd))
        command = args[1] if len(args) > 1 else ""
        if cmd == "nproc":
            return {"exitCode": 0, "stdout": self.nproc, "stderr": ""}
        if "tail -c 4000" in command:
            output = self.poll_outputs.pop(0) if self.poll_outputs else "0\noutput\n"
            return {"exitCode": 0, "stdout": output, "stderr": ""}
        return {"exitCode": 0, "stdout": "", "stderr": ""}


def make_job(steps: list[Step] | None = None) -> Job:
    return Job(id="ci", name="CI", runs_on="ubuntu-latest", steps=steps or [])


def make_repo() -> RepoSpec:
    return RepoSpec("acme/demo", "main", "https://github.com/acme/demo", private=False)


@pytest.mark.asyncio
async def test_run_step_ships_base64_and_polls_for_exit() -> None:
    client = FakeClient()
    output: list[str] = []
    script = runner.StepScript("test", "echo '$HOME'", "/work/repo", {"VALUE": "a'b"}, "bash")

    result = await runner.run_step(client, "sb-1", script, poll_s=0, on_output=output.append)

    assert result.status == "ok"
    assert result.exit_code == 0
    assert result.log_tail == "output\n"
    assert output == ["output\n"]
    upload = client.calls[0][2][1]
    expected = base64.b64encode(runner._render_script(script).encode()).decode()
    assert expected in upload
    assert "base64 -d" in upload
    assert "nohup bash -eo pipefail" in client.calls[1][2][1]
    assert "cd /work/repo || exit 1" in runner._render_script(script)
    assert all(call[4] is None for call in client.calls)


def test_render_script_sources_env_after_step_exports() -> None:
    """env.sh (shim-installed PATH prefixes) must win over a step's static PATH export."""
    script = runner.StepScript(
        name="npm ci",
        script="npm ci",
        cwd="/work/repo",
        env={"PATH": "/root/.local/bin:/usr/bin"},
        shell="bash",
    )
    rendered = runner._render_script(script)
    export_index = rendered.index("export PATH=")
    source_index = rendered.index("/tmp/solci/env.sh")
    assert export_index < source_index, "step env exports must come before sourcing env.sh"


async def test_run_step_returns_timeout_when_exit_file_never_appears() -> None:
    client = FakeClient(poll_outputs=["still running\n"])

    result = await runner.run_step(
        client,
        "sb-1",
        runner.StepScript("long", "sleep 20", "/work/repo", {}, "sh"),
        timeout_s=0,
        poll_s=0,
    )

    assert result.status == "timeout"
    assert result.exit_code is None
    assert result.note is not None and "timed out" in result.note


@pytest.mark.asyncio
async def test_run_job_clones_runs_steps_and_deletes_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    client = FakeClient()
    target_job = make_job(
        [
            Step("checkout", None, "actions/checkout@v4"),
            Step("test", "echo ok", None),
        ]
    )

    result = await runner.run_job(client, target_job, make_repo(), cpu=1, mem_mb=2048)

    assert result.ok is True
    assert result.sandbox_id == "sb-1"
    assert [item.name for item in result.steps] == ["checkout", "test"]
    assert result.steps[0].note == "checkout done by runner"
    assert client.created == [(1, 2048)]
    assert client.deleted == ["sb-1"]


@pytest.mark.asyncio
async def test_run_job_records_cpu_online_timeout_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "CPU_ONLINE_TIMEOUT_S", 0.0)
    client = FakeClient(nproc="1\n")

    result = await runner.run_job(client, make_job(), make_repo(), cpu=2, mem_mb=2048)

    assert result.ok is False
    assert result.error is not None
    assert "requested 2, observed 1" in result.error
    assert client.deleted == ["sb-1"]


def test_build_env_applies_job_then_step_and_drops_expressions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin")
    target_job = Job(
        id="ci",
        name="CI",
        runs_on="ubuntu-latest",
        env={"FROM_JOB": 123, "DROP_JOB": "${{ matrix.value }}"},
    )
    target_step = Step("test", "echo", None, env={"FROM_JOB": "step", "DROP_STEP": "${{ github.sha }}"})

    env = runner.build_env(target_job, target_step, make_repo())

    assert env["CI"] == "true"
    assert env["GITHUB_ACTIONS"] == "true"
    assert env["GITHUB_REPOSITORY"] == "acme/demo"
    assert env["GITHUB_REF"] == "refs/heads/main"
    assert env["RUNNER_OS"] == "Linux"
    assert env["HOME"] == "/root"
    assert "/root/.local/bin" in env["PATH"]
    assert "/root/.cargo/bin" in env["PATH"]
    assert env["FROM_JOB"] == "step"
    assert "DROP_JOB" not in env
    assert "DROP_STEP" not in env


def test_shims_map_supported_actions_and_skip_unsupported_actions() -> None:
    context = {"workspace": "/work/repo", "cpu": 2}
    cases = [
        (Step("checkout", None, "actions/checkout@v4"), shims.SkipNote, "checkout done by runner"),
        (Step("uv", None, "astral-sh/setup-uv@v4"), runner.StepScript, "uv"),
        (Step("python", None, "actions/setup-python@v5"), runner.StepScript, "python3"),
        (Step("node", None, "actions/setup-node@v4"), runner.StepScript, "node"),
        (Step("pnpm", None, "pnpm/action-setup@v4"), runner.StepScript, "pnpm"),
        (Step("bun", None, "oven-sh/setup-bun@v2"), runner.StepScript, "bun"),
        (Step("cache", None, "actions/cache@v4"), shims.SkipNote, "no-op on solci"),
        (Step("other", None, "owner/action@main"), shims.SkipNote, "unsupported action"),
    ]

    for action_step, expected_type, expected_text in cases:
        translated = shims.apply(action_step, context)
        assert isinstance(translated, expected_type)
        assert expected_text in (translated.reason if isinstance(translated, shims.SkipNote) else translated.script)


@pytest.mark.asyncio
async def test_run_sizes_isolates_failures_and_sorts_results(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_job(
        client: Any,
        job: Job,
        repo: RepoSpec,
        *,
        cpu: int,
        mem_mb: int,
        plan: str,
        on_event: Any,
    ) -> RunResult:
        await asyncio.sleep(0)
        if cpu == 2:
            raise RuntimeError("size failed")
        return RunResult(job.id, cpu, mem_mb, f"sb-{cpu}", 0, 0, total_s=float(cpu), ok=True)

    monkeypatch.setattr(runner, "run_job", fake_run_job)
    results = await runner.run_sizes(
        FakeClient(),
        make_job(),
        make_repo(),
        [4, 2, 1],
        mem_mb_for=lambda cpu: cpu * 1024,
        concurrency=2,
    )

    assert [item.cpu for item in results] == [1, 2, 4]
    assert results[0].mem_mb == 1024
    assert results[1].ok is False
    assert results[1].error == "size failed"
    assert results[2].ok is True
