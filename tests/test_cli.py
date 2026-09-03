from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from solari_ci.cli import app

FIXTURE = (Path(__file__).parent / "fixtures" / "basic.yml").read_text(encoding="utf-8")


def test_inspect_local_json_reports_selected_job(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(FIXTURE, encoding="utf-8")

    result = CliRunner().invoke(app, ["inspect", str(tmp_path), "--job", "test", "--json"])

    assert result.exit_code == 0
    assert '"target"' in result.stdout
    assert '"SERVICES_UNSUPPORTED"' in result.stdout
    assert '"steps": 3' in result.stdout


def test_run_refuses_local_paths() -> None:
    result = CliRunner().invoke(app, ["run", ".", "--job", "test"])

    assert result.exit_code == 1
    assert "local paths are not supported" in result.output
