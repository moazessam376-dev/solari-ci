"""Terminal, Markdown, JSON, and optional chart output for solci runs."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import cost
from .models import Curve, Finding, RunResult, StepResult
from .theme import BG, FG, GREY, ORANGE
from .theme import console as default_console

SHIMMED_ACTIONS = (
    "actions/setup-python",
    "astral-sh/setup-uv",
    "actions/setup-node",
    "pnpm/action-setup",
    "oven-sh/setup-bun",
)
SKIPPED_ACTIONS = (
    "actions/checkout (runner clones the repository)",
    "actions/cache",
    "actions/upload-artifact",
    "actions/download-artifact",
    "codecov/*",
    "unsupported actions",
)


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:,.1f} s"


def _table(*columns: tuple[str, str]) -> Table:
    result = Table(
        box=None,
        show_edge=False,
        pad_edge=True,
        padding=(0, 2),
        header_style=GREY,
        expand=False,
    )
    for name, justify in columns:
        result.add_column(name.upper(), justify=justify)  # type: ignore[arg-type]
    return result


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.4f}"


def _successful_reference(curve: Curve) -> RunResult | None:
    successful = [run for run in curve.runs if run.ok and run.total_s > 0]
    return next((run for run in successful if run.cpu == 1), None)


def _speedup(run: RunResult, reference: RunResult | None) -> str:
    if reference is None or not run.ok or run.total_s <= 0:
        return "-"
    return f"{reference.total_s / run.total_s:.2f}x"


def _monthly_cost(run: RunResult, curve: Curve) -> float | None:
    if curve.baseline is None:
        return None
    return cost.monthly(run.solari_cost_usd, curve.baseline.monthly_runs_est)


def _result_rows(curve: Curve) -> list[list[str]]:
    reference = _successful_reference(curve)
    return [
        [
            str(run.cpu),
            f"{run.mem_mb:,}",
            _seconds(run.boot_s),
            _seconds(run.cpu_online_s),
            _seconds(run.total_s),
            _money(run.solari_cost_usd),
            _money(_monthly_cost(run, curve)),
            _speedup(run, reference),
        ]
        for run in sorted(curve.runs, key=lambda item: item.cpu)
    ]


def _baseline_label(curve: Curve) -> str | None:
    if curve.baseline is None:
        return None
    return f"GitHub {curve.baseline.runner_label}"


def _chart_rows(curve: Curve) -> list[tuple[str, float, str]]:
    rows = [
        (f"{run.cpu} vCPU", run.total_s, _money(run.solari_cost_usd))
        for run in sorted(curve.runs, key=lambda item: item.cpu)
        if run.total_s > 0
    ]
    if curve.baseline is not None:
        rows.append(
            (
                _baseline_label(curve) or "GitHub baseline",
                curve.baseline.median_s,
                _money(curve.github_cost_usd),
            )
        )
    return rows


def _render_ascii_chart(curve: Curve, console: Console) -> None:
    rows = _chart_rows(curve)
    console.print("[hdr]TOTAL TIME[/hdr]")
    if not rows:
        console.print("  [muted]No timing data available.[/muted]")
        return
    maximum = max(value for _, value, _ in rows)
    label_width = max(len(label) for label, _, _ in rows)
    for label, value, _ in rows:
        width = max(1, round(34 * value / maximum)) if maximum else 1
        console.print(f"  {label:<{label_width}}  {'#' * width}  {value:,.1f} s")


def _step_names(curve: Curve) -> list[str]:
    names: list[str] = []
    for run in curve.runs:
        for step in run.steps:
            if step.name not in names:
                names.append(step.name)
    return names


def _step_value(step: StepResult | None) -> str:
    if step is None:
        return "-"
    if step.status != "ok":
        return f"{step.status} ({step.seconds:,.1f}s)"
    if step.note:
        return f"{step.seconds:,.1f}s ({step.note})"
    return f"{step.seconds:,.1f}s"


def _step_rows(curve: Curve) -> list[list[str]]:
    names = _step_names(curve)
    runs = sorted(curve.runs, key=lambda item: item.cpu)
    by_run = [{step.name: step for step in run.steps} for run in runs]
    return [[name, *[_step_value(items.get(name)) for items in by_run]] for name in names]


def _findings_rows(findings: list[Finding]) -> list[list[str]]:
    return [
        [finding.severity, finding.code, finding.step or "job", finding.message, finding.suggestion]
        for finding in findings
    ]


def _measurement_note() -> str:
    shimmed = ", ".join(SHIMMED_ACTIONS)
    skipped = ", ".join(SKIPPED_ACTIONS)
    return (
        "Measured on Solari microVMs; workflow steps run natively. "
        f"Actions shimmed: {shimmed}. Actions skipped/no-op: {skipped}."
    )


def render_terminal(curve: Curve, console: Console = default_console) -> None:
    """Render the complete evidence report to a Rich console."""
    console.print(
        f"[accent]▤ RESULTS[/accent] [muted]"
        f"{escape(curve.owner_repo)} / {escape(curve.job_id)}[/muted]"
    )
    results = _table(
        ("CPU", "right"),
        ("MEM MB", "right"),
        ("BOOT", "right"),
        ("CPU ONLINE", "right"),
        ("TOTAL", "right"),
        ("SOLARI/RUN", "right"),
        ("SOLARI/MONTH", "right"),
        ("SPEEDUP VS 1", "right"),
    )
    for row in _result_rows(curve):
        results.add_row(*row)
    console.print(results)

    if curve.baseline is not None:
        baseline_cost = _money(curve.github_cost_usd)
        console.print(
            f"  [muted]GitHub baseline: median {_seconds(curve.baseline.median_s)}, "
            f"p90 {_seconds(curve.baseline.p90_s)}, {curve.baseline.runs} runs, "
            f"{baseline_cost}/run, {curve.baseline.monthly_runs_est:,.1f} runs/month.[/muted]"
        )
    _render_ascii_chart(curve, console)

    console.print("[hdr]PER-STEP TIMING[/hdr]")
    runs = sorted(curve.runs, key=lambda item: item.cpu)
    step_table = _table(("STEP", "left"), *[(f"{run.cpu} VCPU", "right") for run in runs])
    for row in _step_rows(curve):
        step_table.add_row(*[escape(value) for value in row])
    if _step_names(curve):
        console.print(step_table)
    else:
        console.print("  [muted]No steps were recorded.[/muted]")

    recommendation = curve.recommendation or "No recommendation was produced."
    console.print(Panel(escape(recommendation), title="RECOMMENDATION", border_style=ORANGE))

    console.print("[hdr]FINDINGS[/hdr]")
    findings_table = _table(
        ("SEVERITY", "left"),
        ("CODE", "left"),
        ("STEP", "left"),
        ("MESSAGE", "left"),
        ("SUGGESTION", "left"),
    )
    for row in _findings_rows(curve.findings):
        findings_table.add_row(*[escape(value) for value in row])
    if curve.findings:
        console.print(findings_table)
    else:
        console.print("  [pass]No findings.[/pass]")


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [f"| {' | '.join(headers)} |", f"| {' | '.join('---' for _ in headers)} |"]
    lines.extend(f"| {' | '.join(row)} |" for row in rows)
    return "\n".join(lines)


def to_markdown(curve: Curve) -> str:
    """Return the report in GitHub-flavoured Markdown."""
    headers = ["CPU", "Mem MB", "Boot", "CPU online", "Total", "Solari/run", "Solari/month", "Speedup vs 1"]
    lines = [
        f"# solci report: `{curve.owner_repo}` / `{curve.job_id}`",
        "",
        "## Results",
        "",
        _markdown_table(headers, _result_rows(curve)),
    ]
    if curve.baseline is not None:
        lines.extend(
            [
                "",
                (
                    f"GitHub baseline: median {_seconds(curve.baseline.median_s)}, "
                    f"p90 {_seconds(curve.baseline.p90_s)}, {curve.baseline.runs} runs, "
                    f"{_money(curve.github_cost_usd)}/run, "
                    f"{curve.baseline.monthly_runs_est:,.1f} runs/month."
                ),
            ]
        )

    chart_rows = [[label, _seconds(seconds), price] for label, seconds, price in _chart_rows(curve)]
    lines.extend(["", "## Total time", "", _markdown_table(["Size", "Total", "Cost/run"], chart_rows)])
    step_headers = ["Step", *[f"{run.cpu} vCPU" for run in sorted(curve.runs, key=lambda item: item.cpu)]]
    lines.extend(["", "## Per-step timing", "", _markdown_table(step_headers, _step_rows(curve))])
    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            f"> {curve.recommendation or 'No recommendation was produced.'}",
        ]
    )
    finding_rows = _findings_rows(curve.findings)
    lines.extend(["", "## Findings", ""])
    if finding_rows:
        lines.append(_markdown_table(["Severity", "Code", "Step", "Message", "Suggestion"], finding_rows))
    else:
        lines.append("No findings.")
    lines.extend(["", "## How measured", "", f"_{_measurement_note()}_"])
    return "\n".join(lines) + "\n"


def write_chart(curve: Curve, path: str) -> bool:
    """Write an optional 1600x900 PNG chart, returning false without matplotlib."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        default_console.print("[warn]matplotlib is not installed; chart was not written.[/warn]")
        return False

    rows = _chart_rows(curve)
    labels = [label for label, _, _ in rows]
    values = [value for _, value, _ in rows]
    prices = [price for _, _, price in rows]
    figure, axis = plt.subplots(figsize=(16, 9), dpi=100)
    try:
        figure.patch.set_facecolor(BG)
        axis.set_facecolor(BG)
        bars = axis.bar(labels, values, color=ORANGE, width=0.62)
        axis.set_title(f"solci: {curve.owner_repo} / {curve.job_id}", color=FG, fontsize=18, pad=18)
        axis.set_ylabel("Total seconds", color=FG)
        axis.tick_params(colors=GREY, labelrotation=0)
        for spine in axis.spines.values():
            spine.set_color(GREY)
        axis.grid(axis="y", color="#252a32", alpha=0.65)
        axis.set_axisbelow(True)
        for bar, price in zip(bars, prices):
            axis.annotate(
                price,
                (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                ha="center",
                va="bottom",
                color=FG,
                xytext=(0, 6),
                textcoords="offset points",
            )
        figure.tight_layout()
        figure.savefig(Path(path), dpi=100, facecolor=figure.get_facecolor())
    finally:
        plt.close(figure)
    return True


def write_json(curve: Curve, path: str) -> None:
    """Write a deterministic JSON representation of a curve."""
    Path(path).write_text(json.dumps(curve.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
