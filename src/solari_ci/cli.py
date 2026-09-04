"""Typer command line interface for solari-ci."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from . import agent as agent_layer
from . import brain, cost, findings, history, report, workflow
from .client import SolariClient, SolariError, load_env
from .models import Finding, Job, JobBaseline, RunResult, Workflow
from .theme import console, err_console, header, mark, table

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    help="Measure GitHub Actions jobs on Solari microVMs and size them from the curve.",
)

_SOLARI_KEY = re.compile(r"^slr_live_[A-Za-z0-9_-]{4,}$")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _mask_api_key(value: str) -> str:
    return f"slr_live_...{value[-4:]}" if len(value) >= 4 else "slr_live_...----"


def _safe_error(error: BaseException) -> str:
    detail = str(error)
    for key_name in ("SOLARI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_API_KEY"):
        secret = os.environ.get(key_name)
        if secret:
            detail = detail.replace(secret, "[redacted]")
    return detail or type(error).__name__


def _print_error(error: BaseException | str) -> None:
    detail = _safe_error(error) if isinstance(error, BaseException) else error
    for key_name in ("SOLARI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_API_KEY"):
        secret = os.environ.get(key_name)
        if secret:
            detail = detail.replace(secret, "[redacted]")
    err_console.print(f"[fail]error[/fail] {escape(detail)}")


def _is_owner_repo(target: str) -> bool:
    parts = target.split("/")
    return len(parts) == 2 and all(parts) and not target.startswith((".", "/"))


def _load_workflows(target: str, ref: str | None = None) -> tuple[list[Workflow], bool]:
    local = Path(target)
    if local.is_dir():
        return workflow.discover_local(local), True
    if not _is_owner_repo(target):
        raise workflow.WorkflowError(
            f"Target must be an owner/repo or a local repository path: {target}"
        )
    return workflow.fetch_remote(target, ref=ref), False


def _repo_private(owner_repo: str) -> bool | None:
    if not _is_owner_repo(owner_repo):
        return None
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}", "-q", ".private"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


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


def _runs_on(job: Job) -> str:
    return ", ".join(job.runs_on) if isinstance(job.runs_on, list) else job.runs_on


def _job_rows(workflows: list[Workflow]) -> list[list[str]]:
    rows: list[list[str]] = []
    for current_workflow in workflows:
        for job in current_workflow.jobs.values():
            rows.append(
                [
                    current_workflow.path,
                    job.id,
                    _runs_on(job),
                    str(len(job.steps)),
                    "yes" if job.matrix else "-",
                    "yes" if job.services or job.container else "-",
                ]
            )
    return rows


def _finding_rows(items: list[Finding]) -> list[list[str]]:
    return [
        [item.severity, item.code, item.step or "job", item.message, item.suggestion]
        for item in items
    ]


def _print_findings(items: list[Finding]) -> None:
    console.print("[hdr]FINDINGS[/hdr]")
    if not items:
        console.print("  [pass]No findings.[/pass]")
        return
    finding_table = table(
        ("SEVERITY", "left"),
        ("CODE", "left"),
        ("STEP", "left"),
        ("MESSAGE", "left"),
        ("SUGGESTION", "left"),
    )
    for row in _finding_rows(items):
        finding_table.add_row(*[escape(value) for value in row])
    console.print(finding_table)


def _print_inspect_human(
    target: str,
    workflows: list[Workflow],
    selected_workflow: Workflow,
    selected_job: Job,
    baseline: JobBaseline | None,
    private: bool | None,
) -> None:
    header("inspect")
    console.print(f"[accent]{mark('inspect')}[/accent] [muted]{escape(target)}[/muted]")
    jobs_table = table(
        ("WORKFLOW", "left"),
        ("JOB ID", "left"),
        ("RUNS-ON", "left"),
        ("STEPS", "right"),
        ("MATRIX", "left"),
        ("SERVICES", "left"),
    )
    for row in _job_rows(workflows):
        jobs_table.add_row(*[escape(value) for value in row])
    console.print("[hdr]WORKFLOWS / JOBS[/hdr]")
    console.print(jobs_table)

    console.print(
        f"[accent]{mark('inspect')} SELECTED[/accent] [muted]"
        f"{escape(selected_workflow.path)}:{escape(selected_job.id)}[/muted]"
    )
    steps_table = table(("#", "right"), ("NAME", "left"), ("USES / RUN", "left"), ("IF", "left"))
    for index, step in enumerate(selected_job.steps, 1):
        steps_table.add_row(
            str(index),
            escape(step.name),
            escape(step.uses or step.run or "-"),
            escape(step.if_ or "-")
        )
    console.print("[hdr]STEPS[/hdr]")
    console.print(steps_table)

    baseline_findings = findings.analyze(selected_workflow, selected_job, baseline)
    _print_findings(baseline_findings)
    if baseline is None:
        if history.last_error:
            console.print(f"[muted]History unavailable: {escape(history.last_error)}[/muted]")
        return

    github_cost = cost.github_job_cost(baseline.median_s, baseline.runner_label, private=private is not False)
    monthly_cost = cost.monthly(github_cost, baseline.monthly_runs_est)
    console.print("[hdr]HISTORY BASELINE[/hdr]")
    baseline_table = table(("METRIC", "left"), ("VALUE", "right"))
    baseline_table.add_row("runs", str(baseline.runs))
    baseline_table.add_row("median", f"{baseline.median_s:,.1f} s")
    baseline_table.add_row("p90", f"{baseline.p90_s:,.1f} s")
    baseline_table.add_row("failure rate", f"{baseline.failure_rate:.0%}")
    baseline_table.add_row("est. runs/month", f"{baseline.monthly_runs_est:,.1f}")
    baseline_table.add_row("GitHub $/run", f"${github_cost:.4f}")
    baseline_table.add_row("GitHub $/month", f"${monthly_cost:.2f}")
    console.print(baseline_table)
    if private is False:
        reference = cost.github_job_cost(baseline.median_s, baseline.runner_label, private=True)
        console.print(
            f"[muted]public repo: GitHub-hosted minutes are free; private-rate reference "
            f"${reference:.4f}/run[/muted]"
        )


def _inspect_payload(
    target: str,
    workflows: list[Workflow],
    selected_workflow: Workflow,
    selected_job: Job,
    baseline: JobBaseline | None,
    private: bool | None,
) -> dict[str, Any]:
    github_cost = None
    if baseline is not None:
        github_cost = cost.github_job_cost(
            baseline.median_s,
            baseline.runner_label,
            private=private is not False,
        )
    return {
        "target": target,
        "workflows": [
            {
                "path": current.path,
                "name": current.name,
                "jobs": [
                    {
                        "id": job.id,
                        "name": job.name,
                        "runs_on": job.runs_on,
                        "steps": len(job.steps),
                        "matrix": bool(job.matrix),
                        "services": bool(job.services or job.container),
                    }
                    for job in current.jobs.values()
                ],
            }
            for current in workflows
        ],
        "selected": {
            "workflow": selected_workflow.path,
            "job": selected_job.as_dict(),
            "findings": [
                item.as_dict()
                for item in findings.analyze(selected_workflow, selected_job, baseline)
            ],
            "baseline": baseline.as_dict() if baseline is not None else None,
            "github_cost_usd": github_cost,
            "github_monthly_cost_usd": (
                cost.monthly(github_cost, baseline.monthly_runs_est)
                if github_cost is not None and baseline is not None
                else None
            ),
            "visibility": "private" if private is True else "public" if private is False else "unknown",
        },
    }


def _check_gh_auth() -> Check:
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return Check("gh auth", False, "gh CLI is not installed or could not be started")
    if result.returncode == 0:
        return Check("gh auth", True, "authenticated")
    return Check("gh auth", False, f"gh auth status exited {result.returncode}")


async def _sandbox_check(api_key: str) -> Check:
    started = time.monotonic()
    try:
        async with SolariClient(api_key=api_key) as client:
            created = await client.create_sandbox(cpu=1, mem_mb=512)
            sandbox_id = str(created["sandboxId"])
            try:
                response = await client.exec(sandbox_id, "nproc", [])
                output = str(response.get("stdout", "")).strip().splitlines()
                nproc = output[-1] if output else "unknown"
                check = Check("Solari sandbox", True, f"create -> nproc={nproc}")
            except Exception as error:  # noqa: BLE001 - doctor turns all diagnostics into a check
                check = Check("Solari sandbox", False, f"request failed: {type(error).__name__}")
            try:
                await client.delete_sandbox(sandbox_id)
            except Exception as error:  # noqa: BLE001 - cleanup is part of the roundtrip check
                check = Check("Solari sandbox", False, f"cleanup failed: {type(error).__name__}")
            elapsed = time.monotonic() - started
            return Check(check.name, check.ok, f"{check.detail} -> delete in {elapsed:.2f}s")
    except Exception as error:  # noqa: BLE001 - doctor turns all diagnostics into a check
        return Check("Solari sandbox", False, f"request failed: {type(error).__name__}")


@app.command()
def doctor() -> None:
    """Check credentials, gh authentication, and a Solari sandbox roundtrip."""
    load_env()
    header("doctor")
    api_key = os.environ.get("SOLARI_API_KEY", "")
    key_ok = bool(_SOLARI_KEY.fullmatch(api_key))
    key_detail = _mask_api_key(api_key) if api_key else "missing"
    if api_key and not key_ok:
        key_detail += " (expected slr_live_...xxxx format)"
    checks = [Check("SOLARI_API_KEY", key_ok, key_detail)]
    checks.append(_check_gh_auth())
    if key_ok:
        checks.append(asyncio.run(_sandbox_check(api_key)))
    else:
        checks.append(
            Check("Solari sandbox", False, "skipped because SOLARI_API_KEY is missing or malformed")
        )

    checks_table = table(("CHECK", "left"), ("STATUS", "left"), ("DETAIL", "left"))
    for item in checks:
        status = "[pass]PASS[/pass]" if item.ok else "[fail]FAIL[/fail]"
        checks_table.add_row(item.name, status, escape(item.detail))
    console.print(checks_table)
    raise typer.Exit(0 if all(item.ok for item in checks) else 1)


@app.command()
def inspect(
    target: str = typer.Argument(..., help="owner/repo or a local repository path."),
    workflow_name: str | None = typer.Option(None, "--workflow", "-w", help="Workflow path or name filter."),
    job: str | None = typer.Option(None, "--job", "-j", help="Job id or display name."),
    runs: int = typer.Option(20, "--runs", min=1, help="Completed runs to use for the baseline."),
    as_json: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Inspect workflows, one selected job, history, and static findings."""
    load_env()
    try:
        workflows, local = _load_workflows(target)
        if not workflows:
            raise workflow.WorkflowError(f"No workflow files found for {target}")
        selected_workflow, selected_job = workflow.select_job(workflows, job, workflow_name)
        private = None if local else _repo_private(target)
        baseline = None if local else _baseline(target, selected_workflow, selected_job, workflows, runs)
        if as_json:
            payload = _inspect_payload(
                target,
                workflows,
                selected_workflow,
                selected_job,
                baseline,
                private,
            )
            typer.echo(json.dumps(payload, indent=2))
        else:
            _print_inspect_human(target, workflows, selected_workflow, selected_job, baseline, private)
    except SolariError as error:
        _print_error(error)
        raise typer.Exit(2) from error
    except (OSError, ValueError, workflow.WorkflowError) as error:
        _print_error(error)
        raise typer.Exit(1) from error


def _parse_cpu(value: str) -> list[int]:
    try:
        sizes = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise typer.BadParameter("CPU sizes must be comma-separated positive integers") from error
    if not sizes or any(size < 1 for size in sizes):
        raise typer.BadParameter("CPU sizes must be comma-separated positive integers")
    return list(dict.fromkeys(sizes))


def _run_progress(event: dict[str, Any]) -> None:
    event_name = event.get("event")
    cpu = event.get("cpu", "?")
    if event_name == "browser_warning":
        console.print(f"  [warn]browser[/warn] {escape(str(event.get('message', '')))}")
    elif event_name == "sandbox_created":
        console.print(f"  [accent]{mark('run')}[/accent] {cpu} vCPU  created")
    elif event_name == "cpu_online":
        elapsed = float(event.get("cpu_online_s", 0))
        console.print(f"  [accent]{mark('run')}[/accent] {cpu} vCPU  cpu online in {elapsed:.2f}s")
    elif event_name == "step_started":
        step_name = escape(str(event.get("step", "step")))
        console.print(f"  [accent]{mark('run')}[/accent] {cpu} vCPU  {step_name}  started")
    elif event_name == "step_finished":
        status = str(event.get("status", "unknown"))
        style = "pass" if status == "ok" else "fail"
        console.print(
            f"  [accent]{mark('run')}[/accent] {cpu} vCPU  {escape(str(event.get('step', 'step')))} "
            f"{float(event.get('seconds', 0)):.1f}s [{style}]{status}[/{style}]"
        )
    elif event_name == "run_finished":
        status = str(event.get("status", "failed"))
        style = "pass" if status == "done" else "fail"
        console.print(f"  [accent]{mark('run')}[/accent] {cpu} vCPU  [{style}]{status}[/{style}]")


async def _run_curve(
    job: Job,
    owner_repo: str,
    ref: str | None,
    sizes: list[int],
    mem_mb: int | None,
    plan: str,
    concurrency: int,
    keep: bool,
    cloud_browser: bool,
    expose_port: int | None,
) -> list[RunResult]:
    return await agent_layer.run_sweep(
        job,
        owner_repo,
        ref,
        sizes,
        mem_mb,
        plan,
        concurrency,
        keep,
        on_event=_run_progress,
        cloud_browser=cloud_browser,
        expose_port=expose_port,
    )


@app.command()
def run(
    target: str = typer.Argument(..., help="owner/repo; local paths are not supported for runs."),
    job: str = typer.Option(..., "--job", "-j", help="Job id or display name."),
    cpu: str = typer.Option("1,2,4,8", "--cpu", help="Comma-separated CPU sizes."),
    mem: int | None = typer.Option(None, "--mem", min=128, help="Memory in MB for every size."),
    ref: str | None = typer.Option(None, "--ref", help="Branch, tag, or commit."),
    plan: str = typer.Option(
        "starter",
        "--plan",
        help="Solari plan: free, starter, professional, or enterprise.",
    ),
    concurrency: int = typer.Option(2, "--concurrency", min=1),
    runs: int = typer.Option(20, "--runs", min=1, help="Completed runs to use for the baseline."),
    json_path: Path | None = typer.Option(None, "--json", help="Write the full result to a JSON path."),
    md_path: Path | None = typer.Option(None, "--md", help="Write a Markdown report to a path."),
    chart_path: Path | None = typer.Option(None, "--chart", help="Write an optional PNG chart to a path."),
    keep: bool = typer.Option(False, "--keep", help="Keep the Solari sandboxes after the run."),
    no_history: bool = typer.Option(False, "--no-history", help="Skip the GitHub Actions history lookup."),
    cloud_browser: bool = typer.Option(
        False,
        "--cloud-browser",
        help="Use Solari cloud Chrome for Playwright, Puppeteer, and browser-use steps.",
    ),
    expose_port: int | None = typer.Option(
        None,
        "--expose-port",
        min=1,
        max=65535,
        help="Expose a sandbox localhost port to cloud Chrome.",
    ),
) -> None:
    """Run one GitHub Actions job at several Solari CPU sizes."""
    load_env()
    if not _is_owner_repo(target):
        _print_error("solci run requires an owner/repo target; local paths are not supported")
        raise typer.Exit(1)

    try:
        sizes = _parse_cpu(cpu)
        workflows, _ = _load_workflows(target, ref=ref)
        if not workflows:
            raise workflow.WorkflowError(f"No workflow files found for {target}")
        selected_workflow, selected_job = workflow.select_job(workflows, job, None)
        expanded_job = workflow.expand_matrix_first(selected_job)
        baseline = None if no_history else _baseline(target, selected_workflow, expanded_job, workflows, runs)
        if expanded_job.services or expanded_job.container:
            _print_error(
                "solci cannot run this job because service containers/Docker are not available; "
                "solci runs steps natively in a microVM"
            )
            raise typer.Exit(3)
        private = _repo_private(target)
        console.print()
        header("run")
        console.print(
            f"[accent]{mark('run')}[/accent] [muted]"
            f"{escape(target)}:{escape(expanded_job.id)}[/muted]"
        )
        results = asyncio.run(
            _run_curve(
                expanded_job,
                target,
                ref,
                sizes,
                mem,
                plan,
                concurrency,
                keep,
                cloud_browser,
                expose_port,
            )
        )
        curve = agent_layer.build_curve(
            target,
            selected_workflow,
            expanded_job,
            results,
            baseline,
            private=private is not False,
            cloud_browser=cloud_browser,
        )
        report.render_terminal(curve, console)
        if json_path is not None:
            report.write_json(curve, str(json_path))
        if md_path is not None:
            md_path.write_text(report.to_markdown(curve), encoding="utf-8")
        if chart_path is not None:
            report.write_chart(curve, str(chart_path))
        raise typer.Exit(0 if any(result.ok for result in results) else 1)
    except typer.Exit:
        raise
    except SolariError as error:
        _print_error(error)
        raise typer.Exit(2) from error
    except (OSError, ValueError, workflow.WorkflowError, typer.BadParameter) as error:
        _print_error(error)
        raise typer.Exit(1) from error


def _agent_progress(event: dict[str, Any]) -> None:
    if event.get("event") != "agent":
        _run_progress(event)
        return
    message = escape(str(event.get("message", "")))
    console.print(f"  [accent]{mark('agent')}[/accent] {message}")


@app.command()
def agent(
    target: str = typer.Argument(..., help="owner/repo; agent mode uses a remote repository."),
    job: str = typer.Option(..., "--job", "-j", help="Job id or display name."),
    cpu: str = typer.Option("1,2,4", "--cpu", help="Comma-separated CPU sizes."),
    mem: int | None = typer.Option(None, "--mem", min=128, help="Memory in MB for every size."),
    plan: str = typer.Option(
        "starter",
        "--plan",
        help="Solari plan: free, starter, professional, or enterprise.",
    ),
    concurrency: int = typer.Option(2, "--concurrency", min=1),
    runs: int = typer.Option(20, "--runs", min=1, help="Completed runs to use for the baseline."),
    brain_name: str | None = typer.Option(None, "--brain", help="Proposal brain: codex or gemini."),
    effort: str = typer.Option("medium", "--effort", help="Codex reasoning effort."),
    open_pr: bool = typer.Option(False, "--pr", help="Open a pull request for the proposal."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Clone and propose without publishing anything."),
    base: str = typer.Option("main", "--base", help="Base branch to clone and measure."),
    cloud_browser: bool = typer.Option(
        False,
        "--cloud-browser",
        help="Use Solari cloud Chrome for Playwright, Puppeteer, and browser-use steps.",
    ),
    expose_port: int | None = typer.Option(
        None,
        "--expose-port",
        min=1,
        max=65535,
        help="Expose a sandbox localhost port to cloud Chrome.",
    ),
    allow_history_only: bool = typer.Option(
        False,
        "--allow-history-only",
        help="Propose a change even if every sandbox size in the sweep failed, using history alone.",
    ),
) -> None:
    """Measure a job, propose a workflow edit, and optionally open a pull request."""
    load_env()
    if not _is_owner_repo(target):
        _print_error("solci agent requires an owner/repo target; local paths are not supported")
        raise typer.Exit(1)
    try:
        sizes = _parse_cpu(cpu)
        if brain_name not in {None, "codex", "gemini"}:
            raise typer.BadParameter("brain must be codex or gemini")
        if effort not in {"medium", "high", "xhigh", "max"}:
            raise typer.BadParameter("effort must be medium, high, xhigh, or max")
        console.print()
        header("agent", dry_run=dry_run)
        console.print(f"[accent]{mark('agent')}[/accent] [muted]{escape(target)}:{escape(job)}[/muted]")
        result = asyncio.run(
            agent_layer.run_agent(
                target,
                job,
                sizes,
                brain_name,
                effort,
                open_pr,
                dry_run,
                base,
                on_event=_agent_progress,
                plan=plan,
                concurrency=concurrency,
                runs=runs,
                mem_mb=mem,
                allow_history_only=allow_history_only,
                cloud_browser=cloud_browser,
                expose_port=expose_port,
            )
        )
        if result.pr_url:
            console.print(f"[pass]Agent complete:[/pass] {escape(result.pr_url)}")
        elif result.proposal is not None and not result.proposal.diff.strip():
            console.print("[pass]Agent complete:[/pass] no change proposed")
        else:
            console.print("[pass]Agent complete:[/pass] proposal ready; no PR opened")
        raise typer.Exit(0)
    except typer.Exit:
        raise
    except SolariError as error:
        _print_error(error)
        raise typer.Exit(2) from error
    except agent_layer.NoMeasurementsError as error:
        _print_error(error)
        raise typer.Exit(4) from error
    except (
        OSError,
        ValueError,
        RuntimeError,
        workflow.WorkflowError,
        brain.BrainError,
        typer.BadParameter,
    ) as error:
        _print_error(error)
        raise typer.Exit(1) from error


if __name__ == "__main__":  # pragma: no cover
    app()
