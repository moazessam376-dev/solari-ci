from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from rich.console import Console

from solari_ci import report
from solari_ci.models import Curve, Finding, JobBaseline, RunResult, StepResult


def make_curve() -> Curve:
    return Curve(
        job_id="test",
        owner_repo="acme/demo",
        runs=[
            RunResult(
                "test",
                1,
                2048,
                "sb-1",
                1.0,
                0.0,
                [StepResult("pytest", "ok", 0, 10.0, "", None)],
                12.0,
                True,
                0.001,
            ),
            RunResult(
                "test",
                2,
                2048,
                "sb-2",
                1.2,
                0.5,
                [StepResult("pytest", "ok", 0, 6.0, "", None)],
                8.0,
                True,
                0.002,
            ),
        ],
        baseline=JobBaseline("test", 10, 20.0, 24.0, 0.1, "ubuntu-latest", 30.0),
        github_cost_usd=0.01,
        recommendation="Use 2 vCPU for the best speed-cost balance.",
        findings=[Finding("info", "MATRIX_NOTE", "first cell only", "test", None, "measure other cells")],
    )


def test_render_terminal_contains_results_chart_steps_and_findings() -> None:
    output = StringIO()
    render_console = Console(file=output, color_system=None, width=160)

    report.render_terminal(make_curve(), render_console)

    text = output.getvalue()
    assert "RESULTS" in text
    assert "TOTAL TIME" in text
    assert "GitHub ubuntu-latest" in text
    assert "PER-STEP TIMING" in text
    assert "MATRIX_NOTE" in text
    assert "Use 2 vCPU" in text


def test_markdown_and_json_outputs_are_complete(tmp_path: Path) -> None:
    curve = make_curve()
    markdown = report.to_markdown(curve)
    json_path = tmp_path / "curve.json"
    report.write_json(curve, str(json_path))

    assert "## How measured" in markdown
    assert "Actions shimmed" in markdown
    assert "| CPU | Mem MB |" in markdown
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["job_id"] == "test"
    assert payload["runs"][1]["cpu"] == 2
