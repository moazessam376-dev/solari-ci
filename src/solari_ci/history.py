"""Retrieve GitHub Actions job history through the gh command-line client."""

from __future__ import annotations

import json
import math
import statistics
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import JobBaseline

last_error: str | None = None
_API_CACHE: dict[tuple[str, ...], Any] = {}
_CACHE_GH: object | None = None


def _gh(args: list[str]) -> str:
    """Run gh and return stdout."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"gh could not be started: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        detail = stderr or result.stdout.strip() or "no stderr output"
        raise RuntimeError(f"gh failed with exit code {result.returncode}: {detail}")
    return result.stdout


def gh_available() -> bool:
    """Return whether gh can run successfully."""
    try:
        _gh(["--version"])
    except Exception:  # noqa: BLE001 - availability means any gh failure is false
        return False
    return True


def _repo_parts(owner_repo: str) -> tuple[str, str]:
    parts = owner_repo.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"owner_repo must be in owner/repo form: {owner_repo}")
    return parts[0], parts[1]


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid ISO8601 timestamp: {value!r}")
    timestamp = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _cached_json(args: list[str], description: str) -> Any:
    global _CACHE_GH
    if _CACHE_GH is not _gh:
        _API_CACHE.clear()
        _CACHE_GH = _gh
    key = tuple(args)
    if key not in _API_CACHE:
        response = _gh(args)
        try:
            _API_CACHE[key] = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not parse {description} JSON: {exc}") from exc
    return _API_CACHE[key]


def _job_label(job: dict[str, Any]) -> str:
    labels = job.get("labels")
    if isinstance(labels, list) and labels and labels[0]:
        return str(labels[0])
    return "ubuntu-latest"


def _job_name_matches(
    candidate_name: str,
    job_name: str,
    job_id: str | None,
    sibling_names: list[str] | None,
) -> bool:
    """Match a GitHub Actions job name against the requested job name.

    Handles plain names, matrix-suffixed names ("test (3.12)" matching "test"),
    and job names that are themselves unresolved GitHub Actions expressions
    (e.g. "${{ matrix.name || matrix.python }}") which the static YAML parser
    cannot evaluate. In the expression case, fall back to matching the prefix
    before the expression, then to elimination against sibling job names/ids,
    then to the job id.
    """
    def same_job_variant(candidate: str, base: str) -> bool:
        return candidate == base or candidate.startswith(f"{base} (")

    if same_job_variant(candidate_name, job_name):
        return True
    if "${{" not in job_name:
        return False

    prefix = job_name.split("${{", 1)[0].strip()
    if prefix:
        return candidate_name.startswith(prefix)

    others = set(sibling_names or [])
    if job_id:
        others.add(job_id)
    if others and not any(same_job_variant(candidate_name, other) for other in others):
        return True

    if job_id:
        return same_job_variant(candidate_name, job_id)

    return False


def fetch_history(
    owner_repo: str,
    workflow_path: str,
    job_name: str,
    limit: int = 20,
    *,
    job_id: str | None = None,
    sibling_names: list[str] | None = None,
) -> JobBaseline | None:
    """Fetch completed runs and summarize the matching job's timing and failures."""
    global last_error
    last_error = None
    try:
        owner, repo = _repo_parts(owner_repo)
        workflow_name = Path(workflow_path).name
        runs_endpoint = (
            f"repos/{owner}/{repo}/actions/workflows/{workflow_name}/runs"
            f"?per_page={limit}&status=completed"
        )
        runs_response = _cached_json(["api", runs_endpoint], "workflow runs")
        if not isinstance(runs_response, dict):
            raise TypeError("workflow runs response was not a JSON object")
        workflow_runs = runs_response.get("workflow_runs", [])
        if not isinstance(workflow_runs, list):
            raise TypeError("workflow_runs was not a JSON list")
        if not workflow_runs:
            last_error = f"No completed workflow runs found for {workflow_path} in {owner_repo}"
            return None

        run_ids: list[str] = []
        created_at: list[datetime] = []
        for run in workflow_runs:
            if not isinstance(run, dict) or run.get("id") is None:
                raise ValueError("a workflow run was missing its id")
            run_ids.append(str(run["id"]))
            created_at.append(_parse_timestamp(run.get("created_at")))

        matched: list[tuple[float, str, str]] = []
        for run_id in run_ids:
            jobs_endpoint = f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
            jobs_response = _cached_json(["api", jobs_endpoint], f"jobs for run {run_id}")
            if not isinstance(jobs_response, dict):
                raise TypeError(f"jobs response for run {run_id} was not a JSON object")
            jobs = jobs_response.get("jobs", [])
            if not isinstance(jobs, list):
                raise TypeError(f"jobs for run {run_id} was not a JSON list")
            # A single workflow run can contain several matrix-variant jobs that all match
            # an unresolved expression job name (e.g. one per OS). Collect every match for
            # this run, then prefer a Linux-labelled one, since solci only runs jobs on
            # Linux microVMs and comparing against a Windows/macOS baseline would mislead.
            run_candidates: list[tuple[float, str, str]] = []
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                candidate_name = job.get("name")
                if not isinstance(candidate_name, str):
                    continue
                if not _job_name_matches(candidate_name, job_name, job_id, sibling_names):
                    continue
                started = _parse_timestamp(job.get("started_at"))
                completed = _parse_timestamp(job.get("completed_at"))
                duration_s = (completed - started).total_seconds()
                run_candidates.append((duration_s, str(job.get("conclusion")), _job_label(job)))
            if not run_candidates:
                continue
            linux_candidate = next((c for c in run_candidates if "ubuntu" in c[2].lower()), None)
            matched.append(linux_candidate if linux_candidate is not None else run_candidates[0])

        if not matched:
            last_error = (
                f"No completed job named {job_name!r} was found in {len(workflow_runs)} runs "
                f"for {owner_repo}"
            )
            return None

        durations = sorted(item[0] for item in matched)
        p90_index = min(len(durations) - 1, max(0, math.ceil(0.9 * len(durations)) - 1))
        span_days = max(1, (max(created_at) - min(created_at)).days)
        # When a job name is an unresolved matrix expression, several OS variants can all
        # match the same job. Prefer a Linux label for the reported baseline since solci only
        # runs jobs on Linux microVMs; fall back to whichever label matched first otherwise.
        linux_label = next((item[2] for item in matched if "ubuntu" in item[2].lower()), None)
        runner_label = linux_label if linux_label is not None else matched[0][2]
        return JobBaseline(
            job_name=job_name,
            runs=len(matched),
            median_s=float(statistics.median(durations)),
            p90_s=float(durations[p90_index]),
            failure_rate=sum(item[1] != "success" for item in matched) / len(matched),
            runner_label=runner_label,
            monthly_runs_est=len(workflow_runs) / span_days * 30,
        )
    except Exception as exc:  # noqa: BLE001 - failures are reported through last_error
        last_error = f"Failed to fetch history for {owner_repo} job {job_name!r}: {exc}"
        return None
