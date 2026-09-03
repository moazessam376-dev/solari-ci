from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from solari_ci import workflow
from solari_ci.models import Job, Step

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_workflow_models_basic_job_and_skips_reusable_job() -> None:
    parsed = workflow.parse_workflow(fixture_text("basic.yml"), ".github/workflows/ci.yml")

    assert parsed.path == ".github/workflows/ci.yml"
    assert parsed.name == "CI"
    assert parsed.raw["on"] == {"push": None, "pull_request": None}
    assert True not in parsed.raw
    assert set(parsed.raw["jobs"]) == {"test", "reusable"}
    assert set(parsed.jobs) == {"test"}

    job = parsed.jobs["test"]
    assert job.id == "test"
    assert job.name == "Test suite"
    assert job.runs_on == "ubuntu-latest"
    assert job.needs == ["build"]
    assert job.matrix == {
        "python-version": ["3.12", "3.13"],
        "os": ["ubuntu-latest", "windows-latest"],
        "include": [{"python-version": "3.14", "os": "ubuntu-latest"}],
    }
    assert job.container == "python:3.12"
    assert job.services == {"postgres": {"image": "postgres:16"}}
    assert job.timeout_minutes == 20
    assert job.env == {"CI": "true"}
    assert job.steps[0].name == "actions/checkout@v4"
    assert job.steps[0].with_ == {"fetch-depth": 1}
    assert job.steps[1].name == "actions/setup-python@v5"
    assert job.steps[1].with_["python-version"] == "${{ matrix.python-version }}"
    assert job.steps[1].env["PYTHON_VERSION"] == "${{  matrix.python-version  }}"
    assert job.steps[2].name == "python -V"


def test_parse_workflow_restores_unquoted_on_boolean_key() -> None:
    parsed = workflow.parse_workflow(fixture_text("on_true.yml"), ".github/workflows/on.yml")

    assert parsed.raw["on"] is True
    assert True not in parsed.raw


def test_discover_local_reads_supported_files_in_filename_order(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "z-last.yml").write_text(fixture_text("on_true.yml"), encoding="utf-8")
    (workflow_dir / "a-first.yaml").write_text(fixture_text("basic.yml"), encoding="utf-8")
    (workflow_dir / "ignored.txt").write_text("not yaml", encoding="utf-8")

    discovered = workflow.discover_local(tmp_path)

    assert [item.path for item in discovered] == [
        ".github/workflows/a-first.yaml",
        ".github/workflows/z-last.yml",
    ]
    assert workflow.discover_local(tmp_path / "missing") == []


def test_fetch_remote_lists_and_decodes_workflow_content(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    remote_text = (
        "name: Remote\non: push\njobs:\n  lint:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: ruff check\n"
    )
    endpoints: dict[str, str] = {
        "repos/acme/demo/contents/.github/workflows?ref=main": json.dumps(
            [
                {"name": "README.md", "path": ".github/workflows/README.md", "type": "file"},
                {"name": "remote.yml", "path": ".github/workflows/remote.yml", "type": "file"},
            ]
        ),
        "repos/acme/demo/contents/.github/workflows/remote.yml?ref=main": json.dumps(
            {
                "content": base64.b64encode(remote_text.encode("utf-8")).decode("ascii"),
                "encoding": "base64",
            }
        ),
    }

    def fake_gh(args: list[str]) -> str:
        calls.append(tuple(args))
        return endpoints[args[1]]

    monkeypatch.setattr(workflow, "_gh", fake_gh)
    result = workflow.fetch_remote("acme/demo", ref="main")

    assert [(item.path, item.name) for item in result] == [
        (".github/workflows/remote.yml", "Remote")
    ]
    assert len(calls) == 2


def test_select_job_supports_workflow_narrowing_and_reports_ambiguity() -> None:
    first = workflow.parse_workflow(
        "name: First\non: push\njobs:\n  build:\n    name: Build\n"
        "    runs-on: ubuntu-latest\n    steps: []\n",
        ".github/workflows/first.yml",
    )
    second = workflow.parse_workflow(
        "name: Second\non: push\njobs:\n  deploy:\n    name: Build\n"
        "    runs-on: ubuntu-latest\n    steps: []\n",
        ".github/workflows/second.yml",
    )

    selected, selected_job = workflow.select_job([first, second], "build", None)
    assert selected is first
    assert selected_job.id == "build"

    with pytest.raises(workflow.WorkflowError, match="first.yml:build.*second.yml:deploy"):
        workflow.select_job([first, second], "Build", None)
    with pytest.raises(workflow.WorkflowError, match="first.yml:build.*second.yml:deploy"):
        workflow.select_job([first, second], None, None)

    narrowed, narrowed_job = workflow.select_job([first, second], None, "second.yml")
    assert narrowed is second
    assert narrowed_job.id == "deploy"


def test_expand_matrix_first_returns_new_job_and_substitutes_strings() -> None:
    original = workflow.parse_workflow(fixture_text("basic.yml"), ".github/workflows/ci.yml").jobs["test"]

    expanded = workflow.expand_matrix_first(original)

    assert isinstance(expanded, Job)
    assert expanded is not original
    assert expanded.name == "Test suite (matrix: python-version=3.12, os=ubuntu-latest)"
    assert expanded.steps[1].with_["python-version"] == "3.12"
    assert expanded.steps[1].env["PYTHON_VERSION"] == "3.12"
    assert original.steps[1].with_["python-version"] == "${{ matrix.python-version }}"
    assert workflow.expand_matrix_first(Job("plain", "plain", "ubuntu-latest")) is not None


def test_expand_matrix_first_skips_expression_axes() -> None:
    original = Job(
        "test",
        "Test",
        "${{ matrix.os }}",
        steps=[Step("run", "echo ${{ matrix.os }} ${{ matrix.python }}", None)],
        matrix={"os": "${{ fromJSON(inputs.os) }}", "python": ["3.12", "3.13"]},
    )

    expanded = workflow.expand_matrix_first(original)

    assert expanded.steps[0].run == "echo ${{ matrix.os }} 3.12"
    assert expanded.name == "Test (matrix: python=3.12)"
