"""Cost estimates for GitHub Actions and Solari.

GITHUB_PLATFORM_FEE_PER_MIN reflects GitHub's Actions platform fee of $0.002/minute
effective 2026-03-01, applying to all Actions minutes including self-hosted runners.
These rates were fetched/recorded on 2026-09-03.
"""

from __future__ import annotations

import math
import re

from .models import JobBaseline, RunResult

GITHUB_PER_MIN: dict[str, float] = {
    "ubuntu-latest": 0.008,
    "ubuntu-24.04": 0.008,
    "ubuntu-22.04": 0.008,
    "ubuntu-latest-4-cores": 0.016,
    "ubuntu-latest-8-cores": 0.032,
    "ubuntu-latest-16-cores": 0.064,
    "windows-latest": 0.016,
    "macos-latest": 0.08,
    # Blacksmith list price is half of GitHub's per-vCPU; fetched 2026-09-03 from blacksmith.sh/pricing.
    "blacksmith-2vcpu-ubuntu-2404": 0.004,
    "blacksmith-4vcpu-ubuntu-2404": 0.008,
    "blacksmith-8vcpu-ubuntu-2404": 0.016,
    "blacksmith-16vcpu-ubuntu-2404": 0.032,
}
GITHUB_PLATFORM_FEE_PER_MIN: float = 0.002
_RUNNER_SIZE = re.compile(r"(?:(\d+)vcpu|(\d+)-cores)", re.IGNORECASE)

PLANS: dict[str, dict[str, float]] = {
    "free": {"vcpu_hour": 0.0525, "gb_hour": 0.0165},
    "starter": {"vcpu_hour": 0.035, "gb_hour": 0.011},
    "professional": {"vcpu_hour": 0.0245, "gb_hour": 0.0077},
    "enterprise": {"vcpu_hour": 0.0175, "gb_hour": 0.0055},
}


def github_job_cost(seconds: float, runner_label: str, private: bool = True) -> float:
    """Estimate a GitHub Actions job cost, including the platform fee for private repos.

    Public repositories do not pay GitHub's per-minute runner charge, so this returns
    ``0.0`` when ``private`` is false.
    """
    if not private:
        return 0.0
    minutes = math.ceil(seconds / 60)
    rate = GITHUB_PER_MIN.get(runner_label)
    if rate is None and "${{" not in runner_label:
        match = _RUNNER_SIZE.search(runner_label)
        if match is not None:
            cores = int(match.group(1) or match.group(2))
            per_core = 0.004 if runner_label.lower().startswith("blacksmith") else 0.008
            rate = cores / 2 * per_core
    if rate is None:
        rate = GITHUB_PER_MIN["ubuntu-latest"]
    return float(minutes * (rate + GITHUB_PLATFORM_FEE_PER_MIN))


def solari_job_cost(seconds: float, cpu: int, mem_mb: int, plan: str = "starter") -> float:
    """Estimate continuously billed Solari compute and memory cost."""
    rates = PLANS.get(plan, PLANS["starter"])
    hours = seconds / 3600
    return hours * cpu * rates["vcpu_hour"] + hours * (mem_mb / 1024) * rates["gb_hour"]


def monthly(cost_per_run: float, runs_per_month: float) -> float:
    """Project a monthly cost from per-run cost and run volume."""
    return cost_per_run * runs_per_month


def recommend(runs: list[RunResult], baseline: JobBaseline | None, private: bool = True) -> str:
    """Recommend the lowest-CPU successful run within ten percent of the fastest."""
    successful = [run for run in runs if run.ok is True]
    if not successful:
        return "No successful runs were available to recommend from."

    fastest = min(successful, key=lambda run: run.total_s)
    cheapest = min(successful, key=lambda run: run.solari_cost_usd)
    eligible = [run for run in successful if run.total_s <= 1.10 * fastest.total_s]
    knee = min(eligible, key=lambda run: (run.cpu, run.solari_cost_usd)) if eligible else cheapest
    if fastest.solari_cost_usd == 0:
        cost_percentage = 0.0 if knee.solari_cost_usd == 0 else float("inf")
    else:
        cost_percentage = 100 * knee.solari_cost_usd / fastest.solari_cost_usd
    recommendation = (
        f"Use {knee.cpu} vCPU: {knee.total_s:.0f} s for ${knee.solari_cost_usd:.4f} per run, "
        f"within 10% of the {fastest.cpu} vCPU time ({fastest.total_s:.0f} s) at "
        f"{cost_percentage:.0f}% of its cost."
    )
    if baseline is not None:
        github_cost = github_job_cost(baseline.median_s, baseline.runner_label, private=private)
        if private:
            recommendation += (
                f" GitHub {baseline.runner_label} median is {baseline.median_s:.0f} s "
                f"(${github_cost:.3f}/run)."
            )
        else:
            private_cost = github_job_cost(baseline.median_s, baseline.runner_label, private=True)
            recommendation += (
                f" GitHub {baseline.runner_label} median is {baseline.median_s:.0f} s "
                f"(free on public repos; private-rate reference ${private_cost:.3f}/run)."
            )
    return recommendation
