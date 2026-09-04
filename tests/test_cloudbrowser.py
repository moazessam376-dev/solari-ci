from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from solari_ci import cloudbrowser, runner
from solari_ci.models import Job, RepoSpec, Step, StepResult


def test_detect_browser_tools_from_package_and_python_dependency_files(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {"@playwright/test": "1.49.1", "puppeteer": "24.0.0"},
                "devDependencies": {"cypress": "13.0.0"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text(
        "pytest-playwright==0.5.2\n# browser-use is optional\nbrowser-use>=0.13\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['playwright>=1.40']\n",
        encoding="utf-8",
    )

    assert cloudbrowser.detect_browser_tools(tmp_path) == {
        "@playwright/test",
        "browser-use",
        "cypress",
        "playwright",
        "puppeteer",
        "pytest-playwright",
    }


def test_detect_browser_tools_accepts_inline_file_list() -> None:
    files = [
        ("package.json", '{"devDependencies": {"playwright": "1.49.1"}}'),
        ("requirements-dev.txt", "browser-use==0.13.0\n"),
    ]

    assert cloudbrowser.detect_browser_tools(files) == {"playwright", "browser-use"}


def test_browser_env_preserves_node_options_and_prepends_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NODE_OPTIONS", "--trace-warnings")
    monkeypatch.setenv("PYTHONPATH", "/repo/src")

    env = cloudbrowser.browser_env(
        "wss://api.getsolari.com/cdp/session-1",
        {"http://localhost:4173": "https://preview.example/session-1"},
    )

    assert env["SOLCI_CDP_URL"].endswith("/session-1")
    assert env["NODE_OPTIONS"] == "--trace-warnings --require /tmp/solci/pw-preload.cjs"
    assert env["PYTHONPATH"] == "/tmp/solci/py-preload:/repo/src"
    assert env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    assert env["PUPPETEER_SKIP_DOWNLOAD"] == "1"
    assert json.loads(env["SOLCI_BASE_URL_MAP"]) == {
        "http://localhost:4173": "https://preview.example/session-1"
    }


def test_preloads_contain_all_patch_targets() -> None:
    assert "require.resolve('playwright-core'" in cloudbrowser.JS_PRELOAD
    assert "require.resolve('playwright'" in cloudbrowser.JS_PRELOAD
    assert "chromium.launch" in cloudbrowser.JS_PRELOAD
    assert "chromium.connectOverCDP" in cloudbrowser.JS_PRELOAD
    assert "launchPersistentContext" in cloudbrowser.JS_PRELOAD
    assert "page.goto" in cloudbrowser.JS_PRELOAD
    assert "mapAbsolute" in cloudbrowser.JS_PRELOAD
    assert "URLSearchParams" in cloudbrowser.JS_PRELOAD
    assert "browser.newContext = async function" in cloudbrowser.JS_PRELOAD
    assert "_newContextForReuse" in cloudbrowser.JS_PRELOAD
    assert "if (!mapping) return value;" in cloudbrowser.JS_PRELOAD
    assert "browserWSEndpoint" in cloudbrowser.JS_PRELOAD
    assert "BrowserType" in cloudbrowser.PYTHON_SITECUSTOMIZE
    assert "connect_over_cdp" in cloudbrowser.PYTHON_SITECUSTOMIZE
    assert "BrowserSession" in cloudbrowser.PYTHON_SITECUSTOMIZE
    assert "browser_profile" in cloudbrowser.PYTHON_SITECUSTOMIZE
    assert "cdp_url" in cloudbrowser.PYTHON_SITECUSTOMIZE


class SessionClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def create_session(self) -> dict[str, str]:
        return {"sessionId": "session-1", "cdpUrl": "wss://api.getsolari.com/cdp/session-1"}

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


@pytest.mark.asyncio
async def test_open_and_close_session() -> None:
    client = SessionClient()

    session = await cloudbrowser.open_session(client)
    await cloudbrowser.close_session(client, session.session_id)

    assert session == cloudbrowser.BrowserSession(
        "session-1", "wss://api.getsolari.com/cdp/session-1"
    )
    assert client.deleted == ["session-1"]


@pytest.mark.asyncio
async def test_open_session_accepts_the_raw_api_websocket_shape() -> None:
    class RawClient:
        async def create_session(self) -> dict[str, str]:
            return {"sessionId": "session-2", "wsEndpoint": "wss://api.getsolari.com/ws/session-2"}

        async def delete_session(self, session_id: str) -> None:
            return None

    session = await cloudbrowser.open_session(RawClient())

    assert session == cloudbrowser.BrowserSession(
        "session-2", "wss://api.getsolari.com/cdp/session-2"
    )


def test_detect_playwright_ports_from_webserver_config() -> None:
    config = """
    export default defineConfig({
      use: { baseURL: 'http://localhost:4173' },
      webServer: { command: 'npm run dev', port: 4173 },
    });
    """

    assert cloudbrowser.detect_playwright_ports(config) == {4173}


def test_open_session_rejects_malformed_client_response() -> None:
    class BadClient:
        async def create_session(self) -> Any:
            return {"sessionId": "missing-cdp"}

    with pytest.raises(RuntimeError, match="no session id or CDP URL"):
        asyncio.run(cloudbrowser.open_session(BadClient()))


class RunnerClient:
    def __init__(self) -> None:
        self.deleted_sandboxes: list[str] = []
        self.deleted_sessions: list[str] = []

    async def create_sandbox(self, cpu: int, mem_mb: int, **kwargs: Any) -> dict[str, str]:
        return {"sandboxId": "sandbox-1"}

    async def delete_sandbox(self, sandbox_id: str) -> None:
        self.deleted_sandboxes.append(sandbox_id)

    async def create_session(self) -> dict[str, str]:
        return {"sessionId": "session-1", "cdpUrl": "wss://api.getsolari.com/cdp/session-1"}

    async def delete_session(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)

    async def sandbox_port_url(self, sandbox_id: str, port: int) -> str:
        return f"https://preview.example/{sandbox_id}/{port}"

    async def exec(
        self,
        sandbox_id: str,
        cmd: str,
        args: list[str],
        timeout_ms: int = 24_000,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        if cmd == "nproc":
            return {"exitCode": 0, "stdout": "2\n", "stderr": ""}
        if len(args) > 1 and "tail -c 4000" in args[1]:
            return {"exitCode": 0, "stdout": "0\nstep\n", "stderr": ""}
        return {"exitCode": 0, "stdout": "", "stderr": ""}


@pytest.mark.asyncio
async def test_runner_skips_install_and_scopes_one_session_to_browser_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RunnerClient()
    captured: list[runner.StepScript] = []

    async def fake_clone(*args: Any, **kwargs: Any) -> StepResult:
        return StepResult("checkout", "ok", 0, 0.1, "", None)

    async def fake_run_step(*args: Any, **kwargs: Any) -> StepResult:
        captured.append(args[2])
        return StepResult(args[2].name, "ok", 0, 0.1, "", None)

    monkeypatch.setattr(runner, "_clone_repository", fake_clone)
    monkeypatch.setattr(runner, "run_step", fake_run_step)
    job = Job(
        "e2e",
        "E2E",
        "ubuntu-latest",
        steps=[
            Step("checkout", None, "actions/checkout@v4"),
            Step("install", "npx playwright install --with-deps chromium", None),
            Step("test", "npx playwright test", None),
        ],
    )

    result = await runner.run_job(
        client,
        job,
        RepoSpec("acme/demo", "main", "https://github.com/acme/demo"),
        cpu=2,
        mem_mb=2048,
        cloud_browser=True,
    )

    assert result.ok
    assert result.browser_session_ids == ["session-1"]
    assert result.browser_seconds >= 0
    assert result.browser_cost_usd >= 0
    assert client.deleted_sessions == ["session-1"]
    assert [step.name for step in result.steps] == ["checkout", "install", "test"]
    assert "skipped" in (result.steps[1].note or "")
    assert captured[-1].env["SOLCI_CDP_URL"].endswith("/session-1")
