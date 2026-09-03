from __future__ import annotations

import pytest

from solari_ci.cost import (
    GITHUB_PER_MIN,
    GITHUB_PLATFORM_FEE_PER_MIN,
    github_job_cost,
    monthly,
    recommend,
    solari_job_cost,
)
from solari_ci.models import JobBaseline, RunResult


def result(job_id: str, cpu: int, total_s: float, cost: float, ok: bool = True) -> RunResult:
    return RunResult(
        job_id=job_id,
        cpu=cpu,
        mem_mb=1024,
        sandbox_id=f"sb-{cpu}",
        boot_s=1.0,
        cpu_online_s=total_s,
        total_s=total_s,
        ok=ok,
        solari_cost_usd=cost,
    )


def test_github_job_cost_is_free_for_public_and_rounds_private_minutes() -> None:
    ubuntu_rate = GITHUB_PER_MIN["ubuntu-latest"] + GITHUB_PLATFORM_FEE_PER_MIN
    assert github_job_cost(61, "ubuntu-latest") == pytest.approx(2 * ubuntu_rate)

    unknown_runner_rate = GITHUB_PER_MIN["ubuntu-latest"] + GITHUB_PLATFORM_FEE_PER_MIN
    assert github_job_cost(60, "unknown-runner") == pytest.approx(unknown_runner_rate)
    assert github_job_cost(600, "macos-latest", private=False) == 0.0


def test_github_job_cost_supports_blacksmith_and_size_fallbacks() -> None:
    assert github_job_cost(60, "blacksmith-4vcpu-ubuntu-2404") == pytest.approx(0.010)
    assert github_job_cost(60, "BLACKSMITH-12vcpu-ubuntu-2404") == pytest.approx(0.026)
    assert github_job_cost(60, "custom-8-cores") == pytest.approx(0.034)
    assert github_job_cost(60, "ubuntu-${{ 8vcpu }}") == pytest.approx(0.010)


def test_solari_job_cost_uses_continuous_cpu_and_memory_billing() -> None:
    expected = (7200 / 3600) * 2 * 0.035 + (7200 / 3600) * (2048 / 1024) * 0.011
    assert solari_job_cost(7200, 2, 2048) == pytest.approx(expected)
    professional_expected = (1800 / 3600) * 4 * 0.0245 + (1800 / 3600) * (512 / 1024) * 0.0077
    assert solari_job_cost(1800, 4, 512, plan="professional") == pytest.approx(professional_expected)

    starter_expected = (3600 / 3600) * 1 * 0.035 + (3600 / 3600) * (1024 / 1024) * 0.011
    assert solari_job_cost(3600, 1, 1024, plan="missing") == pytest.approx(starter_expected)
    assert monthly(0.25, 30) == pytest.approx(7.5)


def test_recommend_chooses_lowest_cpu_within_ten_percent_and_mentions_baseline() -> None:
    runs = [
        result("job", 1, 200, 0.01),
        result("job", 2, 105, 0.04),
        result("job", 4, 100, 0.10),
        result("job", 8, 90, 0.20, ok=False),
    ]
    baseline = JobBaseline("job", 10, 70, 100, 0.1, "ubuntu-latest", 20)

    recommendation = recommend(runs, baseline)

    assert recommendation.startswith("Use 2 vCPU: 105 s for $0.0400 per run")
    assert "within 10% of the 4 vCPU time (100 s)" in recommendation
    assert "at 40% of its cost" in recommendation
    assert "GitHub ubuntu-latest median is 70 s ($0.020/run)." in recommendation

    public_recommendation = recommend(runs, baseline, private=False)
    assert "GitHub ubuntu-latest median is 70 s (free on public repos; private-rate reference $0.020/run)." in (
        public_recommendation
    )
    assert "GitHub ubuntu-latest median is 70 s ($0.020/run)." not in public_recommendation

    without_baseline = recommend(runs, None)
    assert without_baseline.startswith("Use 2 vCPU: 105 s for $0.0400 per run")
    assert "GitHub" not in without_baseline


def test_recommend_explains_when_no_successful_runs_exist() -> None:
    assert recommend([result("job", 2, 100, 0.04, ok=False)], None) == (
        "No successful runs were available to recommend from."
    )
