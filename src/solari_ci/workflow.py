"""Parse and retrieve GitHub Actions workflow definitions."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .models import Job, Step, Workflow


class WorkflowError(Exception):
    """Raised when a workflow cannot be loaded or selected."""


_MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([^}\s]+)\s*\}\}")


def _as_string(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _string_value_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _value_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _parse_step(value: Any) -> Step:
    data = value if isinstance(value, dict) else {}
    uses = _as_string(data.get("uses"))
    run = _as_string(data.get("run"))
    if "name" in data and data["name"] is not None:
        name = _as_string(data["name"]) or ""
    else:
        name = uses or run or ""
    return Step(
        name=name,
        run=run,
        uses=uses,
        with_=_value_dict(data.get("with")),
        env=_string_value_dict(data.get("env")),
        working_directory=_as_string(data.get("working-directory")),
        shell=_as_string(data.get("shell")),
        if_=_as_string(data.get("if")),
        continue_on_error=_as_bool(data.get("continue-on-error", False)),
        timeout_minutes=_as_int(data.get("timeout-minutes")),
    )


def _parse_runs_on(value: Any) -> str | list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return _as_string(value) or "ubuntu-latest"


def _parse_needs(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _parse_job(job_id: str, value: dict[str, Any]) -> Job:
    name = _as_string(value.get("name", job_id)) or job_id
    strategy = value.get("strategy")
    matrix: dict[str, Any] | None = None
    if isinstance(strategy, dict) and isinstance(strategy.get("matrix"), dict):
        matrix = strategy["matrix"]
    container_value = value.get("container")
    if isinstance(container_value, dict):
        container = _as_string(container_value.get("image"))
    else:
        container = _as_string(container_value)
    raw_steps = value.get("steps", [])
    steps = [_parse_step(step) for step in raw_steps] if isinstance(raw_steps, list) else []
    return Job(
        id=job_id,
        name=name,
        runs_on=_parse_runs_on(value.get("runs-on")),
        steps=steps,
        env=_string_value_dict(value.get("env")),
        needs=_parse_needs(value.get("needs")),
        matrix=matrix,
        timeout_minutes=_as_int(value.get("timeout-minutes")),
        container=container,
        services=_value_dict(value.get("services")),
    )


def parse_workflow(text: str, path: str) -> Workflow:
    """Parse YAML text into the shared workflow models."""
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError(f"Could not parse workflow {path}: {exc}") from exc

    if loaded is None:
        raw: dict[str, Any] = {}
    elif isinstance(loaded, dict):
        raw = loaded
    else:
        raise WorkflowError(f"Workflow {path} must contain a top-level mapping")

    # PyYAML uses YAML 1.1, where an unquoted top-level `on` is loaded as True.
    if True in raw and "on" not in raw:
        raw["on"] = raw.pop(True)

    raw_jobs = raw.get("jobs", {})
    if raw_jobs is None:
        raw_jobs = {}
    if not isinstance(raw_jobs, dict):
        raise WorkflowError(f"Workflow {path} has a non-mapping jobs section")

    jobs: dict[str, Job] = {}
    for raw_job_id, raw_job in raw_jobs.items():
        if not isinstance(raw_job, dict):
            continue
        job_id = str(raw_job_id)
        # A job-level `uses` is a reusable-workflow call, not a job with steps.
        if "uses" in raw_job:
            continue
        jobs[job_id] = _parse_job(job_id, raw_job)

    workflow_name = _as_string(raw.get("name")) or Path(path).name
    return Workflow(path=path, name=workflow_name, jobs=jobs, raw=raw)


def discover_local(repo_dir: Path) -> list[Workflow]:
    """Discover and parse workflow files in a local repository."""
    workflow_dir = repo_dir / ".github" / "workflows"
    if not workflow_dir.is_dir():
        return []
    paths = sorted(
        (
            path
            for path in workflow_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".yml", ".yaml"}
        ),
        key=lambda path: path.name,
    )
    workflows: list[Workflow] = []
    for path in paths:
        relative_path = path.relative_to(repo_dir).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"Could not read workflow {relative_path}: {exc}") from exc
        workflows.append(parse_workflow(text, relative_path))
    return workflows


def _gh(args: list[str]) -> str:
    """Run gh and return stdout, translating command failures to WorkflowError."""
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError as exc:
        raise WorkflowError(f"gh could not be started: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        detail = stderr or result.stdout.strip() or "no stderr output"
        raise WorkflowError(f"gh failed with exit code {result.returncode}: {detail}")
    return result.stdout


def _json_response(text: str, description: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"Could not parse {description} from gh response: {exc}") from exc


def _with_ref(endpoint: str, ref: str | None) -> str:
    return f"{endpoint}?ref={ref}" if ref is not None else endpoint


def _repo_parts(owner_repo: str) -> tuple[str, str]:
    parts = owner_repo.split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise WorkflowError(f"owner_repo must be in owner/repo form: {owner_repo}")
    return parts[0], parts[1]


def fetch_remote(owner_repo: str, ref: str | None = None) -> list[Workflow]:
    """Fetch and parse workflow files from a GitHub repository through gh."""
    try:
        return _fetch_remote(owner_repo, ref)
    except WorkflowError:
        raise
    except Exception as exc:
        raise WorkflowError(f"Could not fetch workflows for {owner_repo}: {exc}") from exc


def _fetch_remote(owner_repo: str, ref: str | None = None) -> list[Workflow]:
    owner, repo = _repo_parts(owner_repo)
    directory_endpoint = _with_ref(f"repos/{owner}/{repo}/contents/.github/workflows", ref)
    directory = _json_response(_gh(["api", directory_endpoint]), "workflow directory")
    if not isinstance(directory, list):
        raise WorkflowError("GitHub workflow directory response was not a list")

    entries: list[tuple[str, dict[str, Any]]] = []
    for entry in directory:
        if not isinstance(entry, dict):
            continue
        entry_path = entry.get("path")
        if not isinstance(entry_path, str) or Path(entry_path).suffix.lower() not in {".yml", ".yaml"}:
            continue
        if entry.get("type") == "dir":
            continue
        entries.append((entry_path, entry))

    workflows: list[Workflow] = []
    for workflow_path, entry in sorted(entries, key=lambda item: item[0]):
        content_endpoint = _with_ref(f"repos/{owner}/{repo}/contents/{workflow_path}", ref)
        content_response = _json_response(_gh(["api", content_endpoint]), workflow_path)
        if isinstance(content_response, dict) and isinstance(content_response.get("content"), str):
            encoded = content_response["content"].replace("\n", "")
            try:
                text = base64.b64decode(encoded).decode("utf-8")
            except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
                raise WorkflowError(f"Could not decode workflow {workflow_path}: {exc}") from exc
        elif isinstance(entry.get("download_url"), str):
            text = _gh(["api", entry["download_url"]])
        else:
            raise WorkflowError(f"GitHub response for {workflow_path} had no base64 content")
        workflows.append(parse_workflow(text, workflow_path))
    return workflows


def _workflow_matches(workflow: Workflow, pattern: str) -> bool:
    return (
        pattern == workflow.path
        or pattern == Path(workflow.path).name
        or pattern in workflow.path
        or workflow.path.endswith(pattern)
    )


def _candidate_label(workflow: Workflow, job: Job) -> str:
    return f"{workflow.path}:{job.id}"


def select_job(wfs: list[Workflow], job: str | None, workflow: str | None) -> tuple[Workflow, Job]:
    """Select one job by id or name, optionally narrowing by workflow path."""
    candidates_workflows = wfs
    if workflow is not None:
        candidates_workflows = [item for item in wfs if _workflow_matches(item, workflow)]
        if not candidates_workflows:
            raise WorkflowError(f"No workflow matched {workflow!r}")

    all_candidates = [(item, candidate) for item in candidates_workflows for candidate in item.jobs.values()]
    if job is None:
        matches = all_candidates
    else:
        matches = [
            (item, candidate)
            for item, candidate in all_candidates
            if candidate.id == job or candidate.name == job
        ]

    if not matches:
        requested = "any job" if job is None else f"job {job!r}"
        raise WorkflowError(f"No matching {requested} found")
    if len(matches) > 1:
        labels = ", ".join(_candidate_label(item, candidate) for item, candidate in matches)
        raise WorkflowError(f"Job selection is ambiguous; candidates: {labels}")
    return matches[0]


def _replace_matrix_strings(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _MATRIX_EXPRESSION.sub(
            lambda match: replacements.get(match.group(1), match.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {key: _replace_matrix_strings(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_matrix_strings(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_matrix_strings(item, replacements) for item in value)
    return value


def expand_matrix_first(job: Job) -> Job:
    """Return a copy of a matrix job with the first value of each axis substituted."""
    if not job.matrix:
        return job

    expanded = copy.deepcopy(job)
    replacements: dict[str, str] = {}
    for axis, values in job.matrix.items():
        if axis in {"include", "exclude"} or not isinstance(values, list) or not values:
            continue
        replacements[str(axis)] = str(values[0])

    for step in expanded.steps:
        if step.run is not None:
            step.run = _replace_matrix_strings(step.run, replacements)
        step.with_ = _replace_matrix_strings(step.with_, replacements)
        step.env = _replace_matrix_strings(step.env, replacements)

    if replacements:
        pairs = ", ".join(f"{axis}={value}" for axis, value in replacements.items())
        expanded.name = f"{expanded.name} (matrix: {pairs})"
    return expanded
