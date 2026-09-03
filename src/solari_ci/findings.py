"""Static findings for GitHub Actions workflows."""

from __future__ import annotations

import re
from typing import Any

from .models import Finding, Job, JobBaseline, Step, Workflow

_VERSION_REF = re.compile(r"^v?\d+(?:\.\d+){0,2}$")
_SHA_REF = re.compile(r"^[0-9a-fA-F]{40}$")
_BIG_RUNNER_LABEL = re.compile(r"(?:-cores|\d+vcpu)", re.IGNORECASE)
_SETUP_ACTIONS = {
    "actions/setup-node": "cache",
    "actions/setup-python": "cache",
    "astral-sh/setup-uv": "enable-cache",
}


def _action_prefix(uses: str | None) -> str | None:
    if not uses:
        return None
    return uses.split("@", 1)[0]


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "none", "null"}
    return bool(value)


def _new_finding(
    severity: str,
    code: str,
    message: str,
    job: Job,
    step: str | None,
    suggestion: str,
) -> Finding:
    return Finding(
        severity=severity,
        code=code,
        message=message,
        job=job.id,
        step=step,
        suggestion=suggestion,
    )


def _is_unpinned(uses: str) -> bool:
    if "@" not in uses:
        return True
    ref = uses.rsplit("@", 1)[1]
    return _VERSION_REF.fullmatch(ref) is None and _SHA_REF.fullmatch(ref) is None


def _pull_request_trigger(workflow: Workflow) -> bool:
    trigger = workflow.raw.get("on")
    if trigger is None and True in workflow.raw:
        trigger = workflow.raw[True]
    if isinstance(trigger, str):
        return trigger == "pull_request"
    if isinstance(trigger, list):
        return any(item == "pull_request" for item in trigger)
    if isinstance(trigger, dict):
        return "pull_request" in trigger
    return False


def _has_full_clone(step: Step) -> bool:
    if _action_prefix(step.uses) != "actions/checkout":
        return False
    value = step.with_.get("fetch-depth")
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return False


def _slow_install_kind(step: Step) -> str | None:
    if step.run is None:
        return None
    run = step.run.lower()
    pip_install = "pip install" in run and "uv pip" not in run and "uv run" not in run
    npm_install = "npm install" in run and "npm ci" not in run
    if pip_install and npm_install:
        return "both"
    if pip_install:
        return "pip"
    if npm_install:
        return "npm"
    return None


def _big_runner_labels(job: Job) -> list[str]:
    labels = [job.runs_on] if isinstance(job.runs_on, str) else job.runs_on
    return [label for label in labels if _BIG_RUNNER_LABEL.search(label)]


def analyze(wf: Workflow, job: Job, baseline: JobBaseline | None) -> list[Finding]:
    """Generate deterministic static findings for one workflow job."""
    findings: list[Finding] = []
    has_cache_step = any(_action_prefix(step.uses) == "actions/cache" for step in job.steps)

    for step in job.steps:
        action = _action_prefix(step.uses)
        cache_key = _SETUP_ACTIONS.get(action or "")
        if cache_key is not None and not has_cache_step and not _truthy(step.with_.get(cache_key)):
            if action == "actions/setup-node":
                suggestion = (
                    "Add `with: {cache: 'npm'}` to this setup-node step to cache Node dependencies."
                )
            elif action == "actions/setup-python":
                suggestion = (
                    "Add `with: {cache: 'pip'}` to this setup-python step to cache Python dependencies."
                )
            else:
                suggestion = "Add `with: {enable-cache: true}` to this setup-uv step to cache dependencies."
            findings.append(
                _new_finding(
                    "medium",
                    "NO_CACHE_SETUP",
                    f"{action} does not enable dependency caching in this job.",
                    job,
                    step.name,
                    suggestion,
                )
            )

    for step in job.steps:
        if step.uses is not None and _is_unpinned(step.uses):
            findings.append(
                _new_finding(
                    "low",
                    "UNPINNED_ACTION",
                    f"Action `{step.uses}` uses a mutable or missing ref.",
                    job,
                    step.name,
                    (
                        "Pin this action to a full 40-character commit SHA or an exact version tag "
                        "such as `v4.0.0`."
                    ),
                )
            )

    if job.timeout_minutes is None:
        findings.append(
            _new_finding(
                "low",
                "NO_TIMEOUT",
                f"Job `{job.id}` has no job-level timeout.",
                job,
                None,
                "Add `timeout-minutes: 15` under this job to bound its runtime.",
            )
        )

    if "concurrency" not in wf.raw and _pull_request_trigger(wf):
        findings.append(
            _new_finding(
                "low",
                "NO_CONCURRENCY",
                "This pull-request workflow has no concurrency group to cancel superseded runs.",
                job,
                None,
                (
                    "Add `concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}` at the "
                    "workflow top level."
                ),
            )
        )

    for step in job.steps:
        if _has_full_clone(step):
            findings.append(
                _new_finding(
                    "low",
                    "FULL_CLONE",
                    "The checkout step requests the complete repository history.",
                    job,
                    step.name,
                    "Remove `fetch-depth: 0` or set `with: {fetch-depth: 1}` for a shallow checkout.",
                )
            )

    for step in job.steps:
        install_kind = _slow_install_kind(step)
        if install_kind == "pip":
            message = "This step uses `pip install`; consider `uv pip install` for faster installs."
            suggestion = "Replace `pip install` with `uv pip install` in this step for faster installs."
        elif install_kind == "npm":
            message = (
                "This step uses `npm install`; consider `npm ci` for reproducible, faster installs "
                "when a lockfile is present."
            )
            suggestion = "Replace `npm install` with `npm ci` in this step when the lockfile is present."
        elif install_kind == "both":
            message = (
                "This step uses `pip install` and `npm install`; consider `uv pip install` and `npm ci` "
                "for faster installs."
            )
            suggestion = (
                "Replace `pip install` with `uv pip install` and `npm install` with `npm ci` in this step."
            )
        else:
            continue
        findings.append(_new_finding("info", "SLOW_INSTALL_HINT", message, job, step.name, suggestion))

    if baseline is not None and baseline.failure_rate > 0.2:
        percentage = baseline.failure_rate * 100
        findings.append(
            _new_finding(
                "high",
                "HIGH_FAILURE_RATE",
                f"The historical failure rate for `{baseline.job_name}` is {percentage:.0f}%.",
                job,
                None,
                (
                    "Review the flaky step and test logs, then fix or isolate the failures before scaling "
                    "this job."
                ),
            )
        )

    big_runner_labels = _big_runner_labels(job)
    if big_runner_labels:
        label_text = ", ".join(big_runner_labels)
        vendor = (
            " (Blacksmith)"
            if any(label.lower().startswith("blacksmith") for label in big_runner_labels)
            else ""
        )
        findings.append(
            _new_finding(
                "medium",
                "BIG_RUNNER",
                f"Job `{job.id}` requests the large{vendor} runner label(s) `{label_text}`.",
                job,
                None,
                (
                    "Verify the workload needs these cores and consider a smaller `runs-on` label or "
                    "Solari's cheaper per-vCPU pricing."
                ),
            )
        )

    if job.services or job.container:
        findings.append(
            _new_finding(
                "info",
                "SERVICES_UNSUPPORTED",
                (
                    "solci cannot run this job natively; service containers/Docker are not available in "
                    "Solari sandboxes"
                ),
                job,
                None,
                "Remove services/container or run this job in an environment with Docker support.",
            )
        )

    if job.matrix:
        findings.append(
            _new_finding(
                "info",
                "MATRIX_NOTE",
                "Solari only measured and analyzed the first matrix cell for this job.",
                job,
                None,
                "Re-run with the other matrix values explicitly if you need results for every cell.",
            )
        )

    return findings
