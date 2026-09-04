"""Evidence gathering, proposal, and pull-request orchestration for solci agent mode."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from rich.markup import escape
from rich.syntax import Syntax

from . import cost, findings, history, report, runner, workflow
from .brain import Brain, CodexBrain, Proposal, get_brain
from .client import SolariClient
from .models import Curve, Job, JobBaseline, RepoSpec, RunResult, Workflow, _Model
from .theme import console

EventCallback = Callable[[dict[str, Any]], Any]


class NoMeasurementsError(RuntimeError):
    """Raised when every sandbox size in the sweep failed to produce a measurement."""


@dataclass
class AgentResult(_Model):
    proposal: Proposal | None
    pr_url: str | None
    evidence_md: str
    chart_path: Path | None


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git or gh through one testable subprocess seam."""
    return subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, text=True, check=False)


def _redact(text: str) -> str:
    detail = text
    for key_name in ("GITHUB_TOKEN", "GH_TOKEN", "SOLARI_API_KEY", "GOOGLE_API_KEY"):
        secret = os.environ.get(key_name)
        if secret:
            detail = detail.replace(secret, "[redacted]")
    return detail


async def _notify(callback: EventCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


def _sibling_names(workflows: list[Workflow], selected_workflow: Workflow, selected_job: Job) -> list[str]:
    names: list[str] = []
    for current_workflow in workflows:
        for current_job in current_workflow.jobs.values():
            if current_workflow is selected_workflow and current_job.id == selected_job.id:
                continue
            for value in (current_job.id, current_job.name):
                if value not in names:
                    names.append(value)
    return names


def _baseline(
    owner_repo: str,
    selected_workflow: Workflow,
    selected_job: Job,
    workflows: list[Workflow],
    runs: int,
) -> JobBaseline | None:
    return history.fetch_history(
        owner_repo,
        selected_workflow.path,
        selected_job.name,
        limit=runs,
        job_id=selected_job.id,
        sibling_names=_sibling_names(workflows, selected_workflow, selected_job),
    )


def build_curve(
    owner_repo: str,
    selected_workflow: Workflow,
    selected_job: Job,
    results: list[RunResult],
    baseline: JobBaseline | None,
    private: bool = True,
    cloud_browser: bool = False,
) -> Curve:
    """Build the shared measurement model used by run and agent commands."""
    github_cost = None
    if baseline is not None:
        github_cost = cost.github_job_cost(baseline.median_s, baseline.runner_label, private=private)
    return Curve(
        job_id=selected_job.id,
        owner_repo=owner_repo,
        runs=results,
        baseline=baseline,
        github_cost_usd=github_cost,
        recommendation=cost.recommend(results, baseline, private=private),
        findings=findings.analyze(
            selected_workflow,
            selected_job,
            baseline,
            cloud_browser=cloud_browser,
        ),
    )


async def run_sweep(
    job: Job,
    owner_repo: str,
    ref: str | None,
    sizes: list[int],
    mem_mb: int | None,
    plan: str,
    concurrency: int,
    keep: bool = False,
    on_event: EventCallback | None = None,
    *,
    cloud_browser: bool = False,
    expose_port: int | None = None,
) -> list[RunResult]:
    """Run one selected job at each requested CPU size."""
    repo = RepoSpec(owner_repo, ref, f"https://github.com/{owner_repo}", private=True)
    memory = lambda cpu: mem_mb if mem_mb is not None else max(2048, cpu * 1024)
    async with SolariClient() as client:
        run_kwargs: dict[str, Any] = {
            "mem_mb_for": memory,
            "concurrency": concurrency,
            "plan": plan,
            "keep": keep,
            "on_event": on_event,
        }
        if cloud_browser:
            run_kwargs["cloud_browser"] = True
        if expose_port is not None:
            run_kwargs["expose_port"] = expose_port
        return await runner.run_sizes(
            client,
            job,
            repo,
            sizes,
            **run_kwargs,
        )


def _clone_url(owner_repo: str) -> str:
    url = f"https://github.com/{owner_repo}"
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return url
    parsed = urlsplit(url)
    userinfo = f"x-access-token:{quote(token, safe='')}"
    return urlunsplit(
        (parsed.scheme, f"{userinfo}@{parsed.netloc}", parsed.path, parsed.query, parsed.fragment)
    )


def _clone(owner_repo: str, base_ref: str, repo_dir: Path) -> None:
    result = _run(
        ["git", "clone", "--depth", "1", "--branch", base_ref, _clone_url(owner_repo), str(repo_dir)]
    )
    if result.returncode != 0:
        detail = _redact(result.stderr.strip() or result.stdout.strip() or "no output")
        raise RuntimeError(f"git clone failed with exit code {result.returncode}: {detail}")


def _diff_paths(diff: str) -> set[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        match = re.match(r"^diff --git a/(.+) b/(.+)$", line)
        if match:
            paths.update((match.group(1), match.group(2)))
            continue
        match = re.match(r"^(?:---|\+\+\+) [ab]/(.+)$", line)
        if match and match.group(1) != "/dev/null":
            paths.add(match.group(1))
    return paths


def _check_workflow_only(diff: str, workflow_rel_path: str) -> None:
    paths = _diff_paths(diff)
    if paths and paths != {workflow_rel_path}:
        changed = ", ".join(sorted(paths))
        raise RuntimeError(f"proposal changes files outside {workflow_rel_path}: {changed}")


def _checked_git(
    command: list[str], cwd: Path, *, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    result = _run(command, cwd=cwd, input=input)
    if result.returncode != 0:
        detail = _redact(result.stderr.strip() or result.stdout.strip() or "no output")
        rendered = " ".join(command[:3])
        raise RuntimeError(f"{rendered} failed with exit code {result.returncode}: {detail}")
    return result


def _apply_if_needed(repo_dir: Path, workflow_rel_path: str, diff: str) -> None:
    state = _run(["git", "diff", "--quiet", "--", workflow_rel_path], cwd=repo_dir)
    if state.returncode == 0:
        _checked_git(["git", "apply", "--"], repo_dir, input=diff)
    elif state.returncode != 1:
        detail = _redact(state.stderr.strip() or state.stdout.strip() or "no output")
        raise RuntimeError(f"git diff check failed with exit code {state.returncode}: {detail}")


def _branch_name(job_id: str) -> str:
    safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "-", job_id).strip("-") or "job"
    stamp = datetime.now().astimezone().strftime("%Y%m%d")
    return f"solci/cpu-size-{safe_job}-{stamp}"


def _unique_branch_name(repo_dir: Path, job_id: str) -> str:
    """Pick a branch name that does not already exist on origin.

    A branch from an earlier attempt on the same day (e.g. one that pushed but
    failed before the pull request was opened) must never be force-pushed over,
    so fall back to a time-suffixed name when the day-scoped one is taken.
    """
    base = _branch_name(job_id)
    existing = _run(["git", "ls-remote", "--heads", "origin", base], cwd=repo_dir)
    if existing.returncode == 0 and existing.stdout.strip():
        suffix = datetime.now().astimezone().strftime("%H%M%S")
        return f"{base}-{suffix}"
    return base


def _pr_body(rationale: str, evidence_md: str) -> str:
    return "\n\n".join(
        (
            _redact(rationale).strip(),
            evidence_md,
            "![solci CPU curve](.github/solci/JOB-curve.png)",
        )
    ) + "\n"


def _open_pr(
    repo_dir: Path,
    job_id: str,
    workflow_rel_path: str,
    diff: str,
    evidence_md: str,
    rationale: str,
    chart_path: Path | None,
    body_path: Path,
) -> str:
    branch = _unique_branch_name(repo_dir, job_id)
    _checked_git(["git", "checkout", "-b", branch], repo_dir)
    _apply_if_needed(repo_dir, workflow_rel_path, diff)

    chart_target = repo_dir / ".github" / "solci" / f"{job_id}-curve.png"
    if chart_path is not None and chart_path.exists():
        chart_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chart_path, chart_target)

    body = _pr_body(rationale, evidence_md).replace("JOB-curve.png", f"{job_id}-curve.png")
    body_path.write_text(body, encoding="utf-8")
    files = [workflow_rel_path]
    if chart_target.exists():
        files.append(chart_target.relative_to(repo_dir).as_posix())
    _checked_git(["git", "add", *files], repo_dir)
    _checked_git(["git", "commit", "-m", f"ci: set {job_id} CPU size from solci evidence"], repo_dir)
    _checked_git(["git", "push", "-u", "origin", branch], repo_dir)
    result = _run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            f"ci: set {job_id} CPU size (solci)",
            "--body-file",
            str(body_path),
            "--head",
            branch,
        ],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        detail = _redact(result.stderr.strip() or result.stdout.strip() or "no output")
        raise RuntimeError(f"gh pr create failed with exit code {result.returncode}: {detail}")
    match = re.search(r"https?://github\.com/[^\s]+", result.stdout)
    if match is None:
        raise RuntimeError("gh pr create did not return a pull request URL")
    return match.group(0).rstrip(".,)")


async def run_agent(
    owner_repo: str,
    job: str,
    cpu: list[int],
    brain_name: str | None,
    effort: str,
    open_pr: bool,
    dry_run: bool,
    base_ref: str,
    on_event: EventCallback | None = None,
    *,
    plan: str = "starter",
    concurrency: int = 2,
    runs: int = 20,
    mem_mb: int | None = None,
    allow_history_only: bool = False,
    cloud_browser: bool = False,
    expose_port: int | None = None,
) -> AgentResult:
    """Measure a job, ask a brain for a workflow-only proposal, and optionally open a PR."""
    workflows = workflow.fetch_remote(owner_repo, ref=base_ref)
    if not workflows:
        raise workflow.WorkflowError(f"No workflow files found for {owner_repo}")
    selected_workflow, selected_job = workflow.select_job(workflows, job, None)
    expanded_job = workflow.expand_matrix_first(selected_job)
    baseline = _baseline(owner_repo, selected_workflow, expanded_job, workflows, runs)
    if expanded_job.services or expanded_job.container:
        raise RuntimeError(
            "solci cannot run this job because service containers/Docker are not available; "
            "solci runs steps natively in a microVM"
        )

    sweep_kwargs: dict[str, Any] = {"on_event": on_event}
    if cloud_browser:
        sweep_kwargs["cloud_browser"] = True
    if expose_port is not None:
        sweep_kwargs["expose_port"] = expose_port
    results = await run_sweep(
        expanded_job,
        owner_repo,
        base_ref,
        cpu,
        mem_mb,
        plan,
        concurrency,
        **sweep_kwargs,
    )
    if not any(result.ok for result in results) and not allow_history_only:
        reasons = "; ".join(
            f"{result.cpu} vCPU: {result.error}" for result in results if result.error
        ) or "no sandbox measurement succeeded"
        raise NoMeasurementsError(
            f"solci agent refuses to propose a change from history alone: {reasons}. "
            "Pass --allow-history-only to proceed without a fresh CPU sweep."
        )

    curve = build_curve(
        owner_repo,
        selected_workflow,
        expanded_job,
        results,
        baseline,
        cloud_browser=cloud_browser,
    )
    evidence_md = report.to_markdown(curve)
    root = Path(tempfile.mkdtemp(prefix="solci-agent-"))
    try:
        chart_file = root / f"{expanded_job.id}-curve.png"
        chart_path = chart_file if report.write_chart(curve, str(chart_file)) else None
        repo_dir = root / "repo"
        body_path = root / "pr-body.md"
        await _notify(on_event, {"event": "agent", "message": "cloning repository"})
        _clone(owner_repo, base_ref, repo_dir)
        selected_brain: Brain = get_brain(brain_name)
        if isinstance(selected_brain, CodexBrain):
            selected_brain.effort = effort
        proposal = await asyncio.to_thread(
            selected_brain.propose,
            evidence_md,
            repo_dir,
            selected_workflow.path,
        )
        _check_workflow_only(proposal.diff, selected_workflow.path)

        if not proposal.diff.strip():
            console.print("  [muted]no change proposed[/muted]")
            await _notify(on_event, {"event": "agent", "message": "no change proposed"})
            return AgentResult(proposal=proposal, pr_url=None, evidence_md=evidence_md, chart_path=chart_path)

        console.print("[hdr]PROPOSED DIFF[/hdr]")
        console.print(Syntax(_redact(proposal.diff), "diff", line_numbers=False))
        console.print("[hdr]RATIONALE[/hdr]")
        console.print(escape(_redact(proposal.rationale)))
        console.print(f"[hdr]SUMMARY[/hdr] {escape(_redact(proposal.summary))}")

        pr_url = None
        if open_pr and not dry_run:
            pr_url = _open_pr(
                repo_dir,
                expanded_job.id,
                selected_workflow.path,
                proposal.diff,
                evidence_md,
                proposal.rationale,
                chart_path,
                body_path,
            )
            console.print(f"[pass]PR opened:[/pass] {escape(pr_url)}")
            await _notify(on_event, {"event": "agent", "message": f"PR opened: {pr_url}"})
        elif dry_run:
            console.print("  [muted]dry run: no branch, commit, push, or PR created[/muted]")
        return AgentResult(proposal=proposal, pr_url=pr_url, evidence_md=evidence_md, chart_path=chart_path)
    finally:
        shutil.rmtree(root, ignore_errors=True)
