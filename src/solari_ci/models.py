"""Shared data models for solari-ci."""

from __future__ import annotations

import dataclasses
from typing import Any


class _Model:
    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Step(_Model):
    name: str
    run: str | None
    uses: str | None
    with_: dict[str, Any] = dataclasses.field(default_factory=dict)
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    working_directory: str | None = None
    shell: str | None = None
    if_: str | None = None
    continue_on_error: bool = False
    timeout_minutes: int | None = None


@dataclasses.dataclass
class Job(_Model):
    id: str
    name: str
    runs_on: str | list[str]
    steps: list[Step] = dataclasses.field(default_factory=list)
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    needs: list[str] = dataclasses.field(default_factory=list)
    matrix: dict[str, Any] | None = None
    timeout_minutes: int | None = None
    container: str | None = None
    services: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Workflow(_Model):
    path: str
    name: str
    jobs: dict[str, Job] = dataclasses.field(default_factory=dict)
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class RepoSpec(_Model):
    owner_repo: str
    ref: str | None
    url: str
    private: bool = False


@dataclasses.dataclass
class JobBaseline(_Model):
    job_name: str
    runs: int
    median_s: float
    p90_s: float
    failure_rate: float
    runner_label: str
    monthly_runs_est: float


@dataclasses.dataclass
class Finding(_Model):
    severity: str
    code: str
    message: str
    job: str | None
    step: str | None
    suggestion: str


@dataclasses.dataclass
class StepResult(_Model):
    name: str
    status: str
    exit_code: int | None
    seconds: float
    log_tail: str
    note: str | None


@dataclasses.dataclass
class RunResult(_Model):
    job_id: str
    cpu: int
    mem_mb: int
    sandbox_id: str
    boot_s: float
    cpu_online_s: float
    steps: list[StepResult] = dataclasses.field(default_factory=list)
    total_s: float = 0.0
    ok: bool = False
    solari_cost_usd: float = 0.0
    error: str | None = None


@dataclasses.dataclass
class Curve(_Model):
    job_id: str
    owner_repo: str
    runs: list[RunResult] = dataclasses.field(default_factory=list)
    baseline: JobBaseline | None = None
    github_cost_usd: float | None = None
    recommendation: str = ""
    findings: list[Finding] = dataclasses.field(default_factory=list)
