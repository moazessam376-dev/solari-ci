from __future__ import annotations

from typing import Any

from solari_ci.findings import analyze
from solari_ci.models import Finding, Job, JobBaseline, Step, Workflow


def make_step(
    *,
    name: str = "step",
    run: str | None = None,
    uses: str | None = None,
    with_: dict[str, Any] | None = None,
) -> Step:
    return Step(name=name, run=run, uses=uses, with_=with_ or {})


def make_job(
    *,
    steps: list[Step] | None = None,
    runs_on: str | list[str] = "ubuntu-latest",
    matrix: dict[str, Any] | None = None,
    timeout_minutes: int | None = 15,
) -> Job:
    return Job(
        id="test",
        name="Test suite",
        runs_on=runs_on,
        steps=steps or [],
        matrix=matrix,
        timeout_minutes=timeout_minutes,
    )


def make_workflow(*, raw: dict[str, Any] | None = None) -> Workflow:
    return Workflow(
        path=".github/workflows/ci.yml",
        name="CI",
        raw=raw or {"on": ["push"]},
    )


def assert_finding(
    findings: list[Finding],
    job: Job,
    code: str,
    severity: str,
    step: str | None = None,
) -> None:
    expected = Finding(
        severity=severity,
        code=code,
        message="",
        job=job.id,
        step=step,
        suggestion="",
    )
    matches = [finding for finding in findings if finding.code == expected.code]
    assert len(matches) == 1
    actual = matches[0]
    assert actual.severity == expected.severity
    assert actual.job == expected.job
    assert actual.step == expected.step


def assert_absent(findings: list[Finding], code: str) -> None:
    assert all(finding.code != code for finding in findings)


def test_no_cache_setup_positive_and_negative_cases() -> None:
    positive_job = make_job(
        steps=[
            make_step(
                name="Set up Python",
                uses="actions/setup-python@v5",
                with_={"python-version": "3.12"},
            )
        ]
    )
    positive = analyze(make_workflow(), positive_job, None)
    assert_finding(positive, positive_job, "NO_CACHE_SETUP", "medium", "Set up Python")

    negative_job = make_job(
        steps=[
            make_step(
                name="Set up Python",
                uses="actions/setup-python@v5",
                with_={"python-version": "3.12", "cache": "pip"},
            )
        ]
    )
    negative = analyze(make_workflow(), negative_job, None)
    assert_absent(negative, "NO_CACHE_SETUP")


def test_unpinned_action_positive_and_negative_cases() -> None:
    positive_job = make_job(steps=[make_step(name="Checkout", uses="actions/checkout@main")])
    positive = analyze(make_workflow(), positive_job, None)
    assert_finding(positive, positive_job, "UNPINNED_ACTION", "low", "Checkout")

    negative_job = make_job(steps=[make_step(name="Checkout", uses="actions/checkout@v4.0.0")])
    negative = analyze(make_workflow(), negative_job, None)
    assert_absent(negative, "UNPINNED_ACTION")


def test_no_timeout_positive_and_negative_cases() -> None:
    positive_job = make_job(timeout_minutes=None)
    positive = analyze(make_workflow(), positive_job, None)
    assert_finding(positive, positive_job, "NO_TIMEOUT", "low")

    negative_job = make_job(timeout_minutes=20)
    negative = analyze(make_workflow(), negative_job, None)
    assert_absent(negative, "NO_TIMEOUT")


def test_no_concurrency_positive_and_negative_cases() -> None:
    positive_job = make_job()
    positive_workflow = make_workflow(raw={"on": ["pull_request"]})
    positive = analyze(positive_workflow, positive_job, None)
    assert_finding(positive, positive_job, "NO_CONCURRENCY", "low")

    negative_workflow = make_workflow(
        raw={"on": ["pull_request"], "concurrency": {"group": "ci-${{ github.ref }}"}}
    )
    negative = analyze(negative_workflow, positive_job, None)
    assert_absent(negative, "NO_CONCURRENCY")


def test_full_clone_positive_and_negative_cases() -> None:
    positive_job = make_job(
        steps=[make_step(name="Checkout", uses="actions/checkout@v4", with_={"fetch-depth": 0})]
    )
    positive = analyze(make_workflow(), positive_job, None)
    assert_finding(positive, positive_job, "FULL_CLONE", "low", "Checkout")

    negative_job = make_job(
        steps=[make_step(name="Checkout", uses="actions/checkout@v4", with_={"fetch-depth": 1})]
    )
    negative = analyze(make_workflow(), negative_job, None)
    assert_absent(negative, "FULL_CLONE")


def test_slow_install_hint_positive_and_negative_cases() -> None:
    positive_job = make_job(steps=[make_step(name="Install", run="pip install -r requirements.txt")])
    positive = analyze(make_workflow(), positive_job, None)
    assert_finding(positive, positive_job, "SLOW_INSTALL_HINT", "info", "Install")

    negative_job = make_job(steps=[make_step(name="Install", run="uv pip install -r requirements.txt")])
    negative = analyze(make_workflow(), negative_job, None)
    assert_absent(negative, "SLOW_INSTALL_HINT")


def test_high_failure_rate_positive_and_negative_cases() -> None:
    job = make_job()
    positive_baseline = JobBaseline("test", 10, 60, 90, 0.21, "ubuntu-latest", 30)
    positive = analyze(make_workflow(), job, positive_baseline)
    assert_finding(positive, job, "HIGH_FAILURE_RATE", "high")

    negative_baseline = JobBaseline("test", 10, 60, 90, 0.20, "ubuntu-latest", 30)
    negative = analyze(make_workflow(), job, negative_baseline)
    assert_absent(negative, "HIGH_FAILURE_RATE")


def test_big_runner_positive_and_negative_cases() -> None:
    positive_job = make_job(runs_on="ubuntu-latest-4-cores")
    positive = analyze(make_workflow(), positive_job, None)
    assert_finding(positive, positive_job, "BIG_RUNNER", "medium")

    negative_job = make_job(runs_on="ubuntu-latest")
    negative = analyze(make_workflow(), negative_job, None)
    assert_absent(negative, "BIG_RUNNER")


def test_big_runner_and_services_findings_cover_blacksmith_and_docker() -> None:
    job = Job(
        id="test",
        name="Test suite",
        runs_on="blacksmith-4vcpu-ubuntu-2404",
        services={"postgres": {"image": "postgres:16"}},
        container="python:3.12",
    )

    result = analyze(make_workflow(), job, None)

    assert_finding(result, job, "BIG_RUNNER", "medium")
    assert "Blacksmith" in next(item.message for item in result if item.code == "BIG_RUNNER")
    finding = next(item for item in result if item.code == "SERVICES_UNSUPPORTED")
    assert finding.severity == "info"
    assert "service containers/Docker" in finding.message


def test_matrix_note_positive_and_negative_cases() -> None:
    positive_job = make_job(matrix={"python-version": ["3.12", "3.13"]})
    positive = analyze(make_workflow(), positive_job, None)
    assert_finding(positive, positive_job, "MATRIX_NOTE", "info")

    negative_job = make_job(matrix=None)
    negative = analyze(make_workflow(), negative_job, None)
    assert_absent(negative, "MATRIX_NOTE")
