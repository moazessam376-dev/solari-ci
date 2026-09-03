from __future__ import annotations

from pathlib import Path

import pytest

from solari_ci import history

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_json(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_fetch_history_summarizes_runs_and_matches_matrix_names(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    responses = {
        "repos/acme/demo/actions/workflows/ci.yml/runs?per_page=20&status=completed": fixture_json(
            "workflow_runs.json"
        ),
        "repos/acme/demo/actions/runs/101/jobs": fixture_json("jobs_101.json"),
        "repos/acme/demo/actions/runs/102/jobs": fixture_json("jobs_102.json"),
        "repos/acme/demo/actions/runs/103/jobs": fixture_json("jobs_103.json"),
    }

    def fake_gh(args: list[str]) -> str:
        calls.append(tuple(args))
        return responses[args[1]]

    monkeypatch.setattr(history, "_gh", fake_gh)
    first = history.fetch_history("acme/demo", ".github/workflows/ci.yml", "test")

    assert first is not None
    assert first.job_name == "test"
    assert first.runs == 3
    assert first.median_s == 60.0
    assert first.p90_s == 120.0
    assert first.failure_rate == pytest.approx(1 / 3)
    assert first.runner_label == "ubuntu-24.04"
    assert first.monthly_runs_est == pytest.approx(9.0)
    assert history.last_error is None
    assert calls[0][1].endswith("?per_page=20&status=completed")

    second = history.fetch_history("acme/demo", ".github/workflows/ci.yml", "test")
    assert second == first
    assert len(calls) == 4


def test_fetch_history_handles_zero_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh(args: list[str]) -> str:
        return fixture_json("no_runs.json")

    monkeypatch.setattr(history, "_gh", fake_gh)
    result = history.fetch_history("acme/empty", ".github/workflows/ci.yml", "test")

    assert result is None
    assert history.last_error is not None
    assert "No completed workflow runs" in history.last_error


def test_fetch_history_reports_gh_failure_and_json_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_gh(args: list[str]) -> str:
        raise RuntimeError("permission denied")

    monkeypatch.setattr(history, "_gh", failing_gh)
    assert history.fetch_history("acme/failing", ".github/workflows/ci.yml", "test") is None
    assert history.last_error is not None
    assert "permission denied" in history.last_error

    def invalid_gh(args: list[str]) -> str:
        return "not JSON"

    monkeypatch.setattr(history, "_gh", invalid_gh)
    assert history.fetch_history("acme/invalid", ".github/workflows/ci.yml", "test") is None
    assert history.last_error is not None
    assert "could not parse" in history.last_error


def test_gh_available_reflects_gh_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "_gh", lambda args: "gh version 2")
    assert history.gh_available() is True

    def fail(args: list[str]) -> str:
        raise OSError("gh missing")

    monkeypatch.setattr(history, "_gh", fail)
    assert history.gh_available() is False


def test_fetch_history_matches_expression_job_name_via_sibling_elimination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        "repos/acme/demo/actions/workflows/ci.yml/runs?per_page=20&status=completed": fixture_json(
            "workflow_runs.json"
        ),
        "repos/acme/demo/actions/runs/101/jobs": fixture_json("jobs_expr.json"),
        "repos/acme/demo/actions/runs/102/jobs": fixture_json("jobs_expr.json"),
        "repos/acme/demo/actions/runs/103/jobs": fixture_json("jobs_expr.json"),
    }

    def fake_gh(args: list[str]) -> str:
        return responses[args[1]]

    monkeypatch.setattr(history, "_gh", fake_gh)
    result = history.fetch_history(
        "acme/demo",
        ".github/workflows/ci.yml",
        "${{ matrix.name || matrix.python }}",
        job_id="test",
        sibling_names=["lint"],
    )

    assert result is not None
    assert result.runs == 3
    assert result.job_name == "${{ matrix.name || matrix.python }}"
    assert history.last_error is None


def test_fetch_history_prefers_linux_label_among_matched_matrix_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        "repos/acme/demo/actions/workflows/ci.yml/runs?per_page=20&status=completed": fixture_json(
            "workflow_runs.json"
        ),
        "repos/acme/demo/actions/runs/101/jobs": fixture_json("jobs_mixed_os.json"),
        "repos/acme/demo/actions/runs/102/jobs": fixture_json("jobs_mixed_os.json"),
        "repos/acme/demo/actions/runs/103/jobs": fixture_json("jobs_mixed_os.json"),
    }

    def fake_gh(args: list[str]) -> str:
        return responses[args[1]]

    monkeypatch.setattr(history, "_gh", fake_gh)
    result = history.fetch_history(
        "acme/demo",
        ".github/workflows/ci.yml",
        "${{ matrix.os }} . node ${{ matrix.node }}",
        job_id="test",
        sibling_names=[],
    )

    assert result is not None
    assert result.runner_label == "ubuntu-latest"


def test_expression_job_matching_excludes_siblings_and_accepts_matrix_variant() -> None:
    expression = "${{ matrix.name || matrix.python }}"

    assert history._job_name_matches("lint", expression, "test", ["lint"]) is False
    assert history._job_name_matches("test (3.12)", expression, "test", ["lint"]) is True
