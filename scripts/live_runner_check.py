"""Run the opt-in live Solari runner smoke test."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

from solari_ci.client import SolariClient
from solari_ci.models import Job, RepoSpec, Step
from solari_ci.runner import run_sizes

OWNER_REPO = "moazessam376-dev/solari-lab"
REPOSITORY_URL = "https://github.com/moazessam376-dev/solari-lab"


def _github_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        os.environ["GITHUB_TOKEN"] = token
        return token
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    token = result.stdout.strip()
    if token:
        os.environ["GITHUB_TOKEN"] = token
    return token or None


def _job() -> Job:
    return Job(
        id="ci",
        name="CI",
        runs_on="ubuntu-latest",
        timeout_minutes=30,
        steps=[
            Step("checkout", None, "actions/checkout@v4"),
            Step("setup uv", None, "astral-sh/setup-uv@v4"),
            Step("install", 'uv venv -p 3.12 && uv pip install -e ".[dev]"', None),
            Step("ruff", ".venv/bin/ruff check src tests", None),
            Step("pytest", ".venv/bin/pytest -q", None),
        ],
    )


async def main() -> int:
    if not os.environ.get("SOLARI_API_KEY"):
        print("Live smoke skipped: SOLARI_API_KEY is not set.")
        return 0
    if _github_token() is None:
        print("Live smoke skipped: no GitHub token is available.")
        return 0

    repo = RepoSpec(OWNER_REPO, None, REPOSITORY_URL, private=True)
    async with SolariClient(api_key=os.environ["SOLARI_API_KEY"]) as client:
        results = await run_sizes(client, _job(), repo, [1, 2, 4])
        print("cpu  boot_s  cpu_online_s  steps  total_s  ok  cost")
        for result in results:
            timings = ", ".join(f"{step.name}={step.seconds:.1f}s" for step in result.steps)
            print(
                f"{result.cpu}    {result.boot_s:.1f}       {result.cpu_online_s:.1f}       "
                f"{timings or '-'}  {result.total_s:.1f}  {result.ok}  ${result.solari_cost_usd:.4f}"
            )
        remaining = await client.list_sandboxes()
        print(f"remaining sandboxes: {len(remaining)}")
        return 0 if not remaining else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
