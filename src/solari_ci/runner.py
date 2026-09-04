"""Execute a GitHub Actions job inside Solari sandboxes."""

from __future__ import annotations

import asyncio
import base64
import inspect
import itertools
import math
import os
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from . import cloudbrowser, ledger, shims
from .client import SolariClient
from .models import Job, RepoSpec, RunResult, Step, StepResult

WORKSPACE = "/work/repo"
EXEC_TIMEOUT_MS = 24_000
CPU_ONLINE_TIMEOUT_S = 40.0
CPU_POLL_S = 1.0
BROWSER_COST_USD_PER_HOUR = 0.10

PLANS: dict[str, dict[str, float]] = {
    "free": {"vcpu_hour": 0.0525, "gb_hour": 0.0165},
    "starter": {"vcpu_hour": 0.035, "gb_hour": 0.011},
    "professional": {"vcpu_hour": 0.0245, "gb_hour": 0.0077},
    "enterprise": {"vcpu_hour": 0.0175, "gb_hour": 0.0055},
}

_SCRIPT_IDS = itertools.count(1)
_INTEGER_LINE = re.compile(r"^-?\d+$")
_BROWSER_RUN = re.compile(
    r"\b(?:playwright|pytest|npm\s+(?:run\s+)?test|npx|pnpm\s+(?:run\s+)?test|"
    r"yarn\s+(?:run\s+)?test|vitest|jest|browser-use|python(?:3)?)\b",
    re.IGNORECASE,
)
EventCallback = Callable[[dict[str, Any]], Any]
OutputCallback = Callable[[str], Any]


@dataclass
class StepScript:
    """The shell script and execution context for one workflow step."""

    name: str
    script: str
    cwd: str
    env: dict[str, str]
    shell: str = "bash"


def _shell_prefix(shell: str) -> str:
    return "sh -e" if shell == "sh" else "bash -eo pipefail"


def _render_script(script: StepScript) -> str:
    # Step env (e.g. build_env's static PATH baseline) is exported first, then
    # /tmp/solci/env.sh is sourced last so that shim-installed tool paths (e.g.
    # setup-node's /opt/node/bin) correctly prepend onto PATH instead of being
    # clobbered by a later static `export PATH=...` from a previous step's env.
    lines: list[str] = []
    if script.cwd:
        lines.append(f"cd {shlex.quote(script.cwd)} || exit 1")
    for key, value in script.env.items():
        if not key or "=" in key:
            continue
        lines.append(f"export {key}={shlex.quote(str(value))}")
    lines.append("[ -f /tmp/solci/env.sh ] && . /tmp/solci/env.sh || true")
    lines.append(script.script)
    return "\n".join(lines)


def _response_exit_code(response: Any) -> int | None:
    if not isinstance(response, dict):
        return None
    value = response.get("exitCode")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _poll_result(response: Any) -> tuple[int | None, str]:
    if not isinstance(response, dict):
        return None, ""
    value = response.get("stdout", "")
    output = value if isinstance(value, str) else str(value)
    lines = output.splitlines(keepends=True)
    if lines and _INTEGER_LINE.fullmatch(lines[0].strip()):
        return int(lines[0].strip()), "".join(lines[1:])[-4000:]
    return None, output[-4000:]


async def _notify(callback: EventCallback | None, event: dict[str, Any]) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


async def _notify_output(callback: OutputCallback | None, output: str) -> None:
    if callback is None:
        return
    result = callback(output)
    if inspect.isawaitable(result):
        await result


async def run_step(
    client: SolariClient,
    sandbox_id: str,
    script: StepScript,
    *,
    timeout_s: float = 1800,
    poll_s: float = 2.0,
    on_output: OutputCallback | None = None,
) -> StepResult:
    """Ship a script to a sandbox, run it detached, and poll for its result."""
    started = time.monotonic()
    script_id = next(_SCRIPT_IDS)
    base_path = f"/tmp/solci/step-{script_id}"
    script_path = f"{base_path}.sh"
    log_path = f"{base_path}.log"
    exit_path = f"{base_path}.exit"
    encoded = base64.b64encode(_render_script(script).encode("utf-8")).decode("ascii")

    try:
        write_command = (
            f"mkdir -p /tmp/solci && printf '%s' {shlex.quote(encoded)} "
            f"| base64 -d > {shlex.quote(script_path)} && chmod 700 {shlex.quote(script_path)}"
        )
        response = await client.exec(
            sandbox_id,
            "sh",
            ["-c", write_command],
            timeout_ms=EXEC_TIMEOUT_MS,
            cwd=None,
        )
        write_exit = _response_exit_code(response)
        if write_exit not in (None, 0):
            raise RuntimeError(f"script upload failed with exit code {write_exit}")

        shell = _shell_prefix(script.shell)
        launch_command = (
            f"( nohup {shell} {shlex.quote(script_path)} > {shlex.quote(log_path)} 2>&1; "
            f"printf '%s\\n' \"$?\" > {shlex.quote(exit_path)} ) >/dev/null 2>&1 &"
        )
        response = await client.exec(
            sandbox_id,
            "sh",
            ["-c", launch_command],
            timeout_ms=EXEC_TIMEOUT_MS,
            cwd=None,
        )
        launch_exit = _response_exit_code(response)
        if launch_exit not in (None, 0):
            raise RuntimeError(f"script launch failed with exit code {launch_exit}")

        poll_command = (
            f"if [ -f {shlex.quote(exit_path)} ]; then cat {shlex.quote(exit_path)}; fi; "
            f"tail -c 4000 {shlex.quote(log_path)} 2>/dev/null || true"
        )
        latest_output = ""
        while True:
            response = await client.exec(
                sandbox_id,
                "sh",
                ["-c", poll_command],
                timeout_ms=EXEC_TIMEOUT_MS,
                cwd=None,
            )
            exit_code, output = _poll_result(response)
            latest_output = output
            await _notify_output(on_output, output)
            elapsed = time.monotonic() - started
            if exit_code is not None:
                status = "ok" if exit_code == 0 else "failed"
                return StepResult(script.name, status, exit_code, elapsed, output[-4000:], None)
            if elapsed >= timeout_s:
                note = f"timed out after {timeout_s:.1f} seconds"
                return StepResult(script.name, "timeout", None, elapsed, latest_output[-4000:], note)
            if poll_s > 0:
                await asyncio.sleep(poll_s)
    except Exception as exc:  # noqa: BLE001 - step failures become a StepResult
        elapsed = max(0.0, time.monotonic() - started)
        return StepResult(script.name, "failed", None, elapsed, str(exc)[-4000:], f"runner error: {exc}")


def _parse_nproc(response: Any) -> int:
    if not isinstance(response, dict):
        return 0
    output = response.get("stdout", "")
    lines = str(output).splitlines()
    for line in reversed(lines):
        try:
            return int(line.strip())
        except ValueError:
            continue
    return 0


async def _wait_cpu_online(client: SolariClient, sandbox_id: str, cpu: int) -> tuple[float, str | None]:
    if cpu <= 1:
        return 0.0, None
    started = time.monotonic()
    observed = 0
    attempts = 0
    max_attempts = max(1, math.ceil(CPU_ONLINE_TIMEOUT_S / CPU_POLL_S) + 1)
    while attempts < max_attempts:
        attempts += 1
        try:
            response = await client.exec(sandbox_id, "nproc", [], timeout_ms=EXEC_TIMEOUT_MS)
            observed = max(observed, _parse_nproc(response))
        except Exception:  # noqa: BLE001 - keep polling until the bounded wait expires
            pass
        elapsed = time.monotonic() - started
        if observed >= cpu:
            return max(0.0, elapsed), None
        if elapsed >= CPU_ONLINE_TIMEOUT_S or attempts >= max_attempts:
            break
        await asyncio.sleep(CPU_POLL_S)
    elapsed = max(0.0, time.monotonic() - started)
    note = f"CPU online wait timed out: requested {cpu}, observed {observed}"
    return elapsed, note


def _clone_url(repo: RepoSpec) -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not repo.private or not token:
        return repo.url
    parsed = urlsplit(repo.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return repo.url
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    userinfo = f"x-access-token:{quote(token, safe='')}"
    return urlunsplit((parsed.scheme, f"{userinfo}@{host}", parsed.path, parsed.query, parsed.fragment))


def _clone_script(repo: RepoSpec) -> str:
    branch = f" --branch {shlex.quote(repo.ref)}" if repo.ref else ""
    url = shlex.quote(_clone_url(repo))
    return "\n".join(
        (
            "set -e",
            "set -o pipefail",
            "mkdir -p /work",
            (
                f"git clone --depth 1{branch} {url} {WORKSPACE} 2>&1 | "
                "sed 's#x-access-token:[^@]*@#***@#g'"
            ),
        )
    )


def _step_cwd(step: Step) -> str:
    directory = step.working_directory
    if not directory:
        return WORKSPACE
    if directory.startswith("/"):
        return directory
    return f"{WORKSPACE}/{directory.lstrip('./')}"


def _condition_note(step: Step) -> str | None:
    condition = (step.if_ or "").strip()
    if not condition or condition.lower() == "true":
        return None
    if condition.lower() == "false":
        return "condition evaluated false"
    return "expression not evaluated"


def build_env(job: Job, step: Step, repo: RepoSpec) -> dict[str, str]:
    """Build the stable GitHub-like environment available to each step."""
    existing_path = os.environ.get("PATH", "")
    path_parts = ["/root/.local/bin", "/root/.cargo/bin"]
    if existing_path:
        path_parts.append(existing_path)
    ref = repo.ref or os.environ.get("GITHUB_REF", "")
    if ref and not ref.startswith("refs/"):
        ref = f"refs/heads/{ref}"
    env: dict[str, str] = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_WORKSPACE": WORKSPACE,
        "GITHUB_REPOSITORY": repo.owner_repo,
        "GITHUB_REF": ref,
        "GITHUB_SHA": os.environ.get("GITHUB_SHA", repo.ref if repo.ref and len(repo.ref) == 40 else ""),
        "RUNNER_OS": "Linux",
        "HOME": "/root",
        "PATH": ":".join(path_parts),
    }
    for values in (job.env, step.env):
        for key, value in values.items():
            string_value = str(value)
            if "${{" in string_value:
                continue
            env[str(key)] = string_value
    return env


def _translate_step(job: Job, step: Step, repo: RepoSpec, cpu: int) -> tuple[StepScript | None, str | None]:
    if step.uses:
        translated = shims.apply(step, {"repo": repo, "workspace": WORKSPACE, "cpu": cpu})
        if isinstance(translated, shims.SkipNote):
            return None, translated.reason
        return replace(
            translated,
            cwd=_step_cwd(step),
            env={**translated.env, **build_env(job, step, repo)},
            shell=step.shell or translated.shell,
        ), None
    if step.run is not None:
        return (
            StepScript(
                name=step.name,
                script=step.run,
                cwd=_step_cwd(step),
                env=build_env(job, step, repo),
                shell=step.shell or "bash",
            ),
            None,
        )
    return None, "step has no run or uses"


def _result_error(result: StepResult) -> str:
    return result.note or result.log_tail or f"step {result.name} failed"


def _browser_install_step(script: str | None) -> bool:
    if not script:
        return False
    return re.search(r"\b(?:npx\s+)?playwright\s+install(?:\s|$)", script, re.IGNORECASE) is not None


def _needs_cloud_browser(step: Step) -> bool:
    return step.run is not None and not _browser_install_step(step.run) and _BROWSER_RUN.search(step.run) is not None


async def _detect_repo_browser_tools(client: SolariClient, sandbox_id: str) -> set[str]:
    """Read dependency manifests inside the sandbox without changing them."""
    manifest_command = "if [ -f /work/repo/package.json ]; then cat /work/repo/package.json; fi"
    try:
        response = await client.exec(
            sandbox_id,
            "sh",
            ["-c", manifest_command],
            timeout_ms=EXEC_TIMEOUT_MS,
            cwd=None,
        )
    except Exception:  # noqa: BLE001 - dependency inspection is advisory
        return set()
    output = response.get("stdout", "") if isinstance(response, dict) else ""
    package_text = str(output)
    return cloudbrowser.detect_browser_tools({"package.json": package_text})


async def _write_browser_preloads(client: SolariClient, sandbox_id: str) -> None:
    """Upload browser hooks into /tmp without changing the checked-out repo."""
    payloads = {
        "/tmp/solci/pw-preload.cjs": cloudbrowser.JS_PRELOAD,
        "/tmp/solci/py-preload/sitecustomize.py": cloudbrowser.PYTHON_SITECUSTOMIZE,
    }
    commands = ["mkdir -p /tmp/solci/py-preload"]
    for path, contents in payloads.items():
        encoded = base64.b64encode(contents.encode("utf-8")).decode("ascii")
        commands.append(
            f"printf '%s' {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}"
        )
    response = await client.exec(
        sandbox_id,
        "sh",
        ["-c", " && ".join(commands)],
        timeout_ms=EXEC_TIMEOUT_MS,
        cwd=None,
    )
    exit_code = _response_exit_code(response)
    if exit_code not in (None, 0):
        raise RuntimeError(f"browser preload upload failed with exit code {exit_code}")


async def _browser_base_url_map(
    client: SolariClient,
    sandbox_id: str,
    expose_port: int | None,
) -> dict[str, str]:
    """Map sandbox localhost URLs to public preview URLs when possible."""
    ports: set[int] = set()
    if expose_port is not None:
        ports.add(expose_port)
    config_command = (
        "for path in /work/repo/playwright.config.ts /work/repo/playwright.config.js "
        "/work/repo/playwright.config.mjs; do "
        "if [ -f \"$path\" ]; then printf '\\n'; sed -n '1,240p' \"$path\"; fi; "
        "done"
    )
    try:
        response = await client.exec(
            sandbox_id,
            "sh",
            ["-c", config_command],
            timeout_ms=EXEC_TIMEOUT_MS,
            cwd=None,
        )
        output = response.get("stdout", "") if isinstance(response, dict) else ""
        ports.update(cloudbrowser.detect_playwright_ports(str(output)))
    except Exception:  # noqa: BLE001 - localhost mapping is best effort
        pass

    mapping: dict[str, str] = {}
    for port in sorted(ports):
        try:
            preview_url = await client.sandbox_port_url(sandbox_id, port)
        except Exception:  # noqa: BLE001 - keep cloud-browser runs usable without mapping
            continue
        preview_url = preview_url.rstrip("/")
        for host in ("localhost", "127.0.0.1"):
            mapping[f"http://{host}:{port}"] = preview_url
    return mapping


def _browser_note(seconds: float, session_count: int = 1) -> str:
    cost = seconds / 3600 * BROWSER_COST_USD_PER_HOUR
    noun = "session" if session_count == 1 else "sessions"
    return f"cloud browser: {session_count} {noun}, {seconds:.1f}s, ${cost:.4f}"


async def _run_browser_step(
    client: SolariClient,
    sandbox_id: str,
    script: StepScript,
    base_url_map: dict[str, str],
    *,
    timeout_s: float,
) -> tuple[StepResult, float, float, list[str]]:
    """Run one step with one browser session and release it in all cases."""
    session: cloudbrowser.BrowserSession | None = None
    session_started = time.monotonic()
    session_seconds = 0.0
    session_cost = 0.0
    session_ids: list[str] = []
    try:
        session = await cloudbrowser.open_session(client)
        session_ids.append(session.session_id)
        browser_script = replace(
            script,
            env={**script.env, **cloudbrowser.browser_env(session.cdp_url, base_url_map)},
        )
        result = await run_step(
            client,
            sandbox_id,
            browser_script,
            timeout_s=timeout_s,
            on_output=None,
        )
    except Exception as exc:  # noqa: BLE001 - turn provisioning failures into step results
        elapsed = max(0.0, time.monotonic() - session_started)
        result = StepResult(
            script.name,
            "failed",
            None,
            elapsed,
            str(exc)[-4000:],
            f"cloud browser failed: {exc}",
        )
    finally:
        if session is not None:
            try:
                await cloudbrowser.close_session(client, session.session_id)
            except Exception as exc:  # noqa: BLE001 - surface a release failure in the step
                detail = f"cloud browser release failed: {exc}"
                result.note = f"{result.note}; {detail}" if result.note else detail
                if result.status == "ok":
                    result.status = "failed"
                    result.exit_code = None
            session_seconds = max(0.0, time.monotonic() - session_started)
            session_cost = session_seconds / 3600 * BROWSER_COST_USD_PER_HOUR
            browser_note = _browser_note(session_seconds)
            result.note = f"{result.note}; {browser_note}" if result.note else browser_note
    return result, session_seconds, session_cost, session_ids


async def _clone_repository(
    client: SolariClient,
    sandbox_id: str,
    job: Job,
    repo: RepoSpec,
) -> StepResult:
    checkout_step = Step(name="checkout", run=None, uses=None)
    script = StepScript(
        name="checkout",
        script=_clone_script(repo),
        cwd="/",
        env=build_env(job, checkout_step, repo),
        shell="bash",
    )
    return await run_step(client, sandbox_id, script)


async def run_job(
    client: SolariClient,
    job: Job,
    repo: RepoSpec,
    *,
    cpu: int,
    mem_mb: int,
    plan: str = "starter",
    keep: bool = False,
    on_event: EventCallback | None = None,
    cloud_browser: bool = False,
    expose_port: int | None = None,
) -> RunResult:
    """Run one job, cleaning up its sandbox unless ``keep`` is true."""
    started = time.monotonic()
    sandbox_id = ""
    boot_s = 0.0
    cpu_online_s = 0.0
    steps: list[StepResult] = []
    error: str | None = None
    job_ok = False
    cpu_note: str | None = None
    total_s = 0.0
    browser_seconds = 0.0
    browser_cost_usd = 0.0
    browser_session_ids: list[str] = []

    try:
        boot_started = time.monotonic()
        created = await client.create_sandbox(cpu=cpu, mem_mb=mem_mb)
        boot_s = max(0.0, time.monotonic() - boot_started)
        if not isinstance(created, dict) or not created.get("sandboxId"):
            raise RuntimeError("sandbox creation returned no sandboxId")
        sandbox_id = str(created["sandboxId"])
        await _notify(on_event, {"event": "sandbox_created", "sandbox_id": sandbox_id, "cpu": cpu})

        cpu_online_s, cpu_note = await _wait_cpu_online(client, sandbox_id, cpu)
        await _notify(
            on_event, {"event": "cpu_online", "cpu": cpu, "cpu_online_s": cpu_online_s, "note": cpu_note}
        )

        clone_result = await _clone_repository(client, sandbox_id, job, repo)
        checkout_recorded = False
        if clone_result.status != "ok":
            error = _result_error(clone_result)

        base_url_map: dict[str, str] = {}
        cypress_detected = False
        if cloud_browser and clone_result.status == "ok":
            cypress_detected = "cypress" in await _detect_repo_browser_tools(client, sandbox_id)
            if cypress_detected:
                await _notify(
                    on_event,
                    {
                        "event": "browser_warning",
                        "message": (
                            "Cypress cannot use Solari cloud Chrome; running Cypress locally in the sandbox"
                        ),
                    },
                )
            await _write_browser_preloads(client, sandbox_id)
            base_url_map = await _browser_base_url_map(client, sandbox_id, expose_port)

        job_ok = error is None
        for step in job.steps:
            condition_note = _condition_note(step)
            if condition_note is not None:
                step_result = StepResult(step.name, "ok", None, 0.0, "", condition_note)
            elif not job_ok:
                break
            elif step.uses and step.uses.split("@", 1)[0] == "actions/checkout":
                checkout_recorded = True
                step_result = replace(clone_result, name=step.name, note="checkout done by runner")
            elif cloud_browser and _browser_install_step(step.run):
                step_result = StepResult(
                    step.name,
                    "ok",
                    None,
                    0.0,
                    "",
                    "playwright install skipped: Solari cloud Chrome is already provisioned",
                )
            else:
                translated, skip_note = _translate_step(job, step, repo, cpu)
                if translated is None:
                    step_result = StepResult(step.name, "ok", None, 0.0, "", skip_note)
                else:
                    if cloud_browser:
                        translated = replace(
                            translated,
                            env={
                                **translated.env,
                                "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
                                "PUPPETEER_SKIP_DOWNLOAD": "1",
                            },
                        )
                    await _notify(on_event, {"event": "step_started", "step": step.name, "cpu": cpu})
                    timeout_s = step.timeout_minutes * 60 if step.timeout_minutes is not None else 1800
                    if cloud_browser and _needs_cloud_browser(step) and not (
                        cypress_detected
                        and step.run is not None
                        and re.search(r"\bcypress\b", step.run, re.IGNORECASE)
                    ):
                        (
                            step_result,
                            step_browser_seconds,
                            step_browser_cost,
                            step_browser_session_ids,
                        ) = await _run_browser_step(
                            client,
                            sandbox_id,
                            translated,
                            base_url_map,
                            timeout_s=timeout_s,
                        )
                        browser_seconds += step_browser_seconds
                        browser_cost_usd += step_browser_cost
                        browser_session_ids.extend(step_browser_session_ids)
                    else:
                        step_result = await run_step(
                            client,
                            sandbox_id,
                            translated,
                            timeout_s=timeout_s,
                            on_output=None,
                        )
                    await _notify(
                        on_event,
                        {
                            "event": "step_finished",
                            "step": step.name,
                            "cpu": cpu,
                            "status": step_result.status,
                            "seconds": step_result.seconds,
                        },
                    )
            steps.append(step_result)
            if step_result.status in {"failed", "timeout"}:
                if step.continue_on_error:
                    step_result.note = step_result.note or "continue-on-error"
                else:
                    job_ok = False
                    error = _result_error(step_result)
                    break

        if not checkout_recorded and clone_result.status != "ok":
            error = error or _result_error(clone_result)
        if cpu_note is not None:
            error = f"{error}; {cpu_note}" if error else cpu_note
        job_ok = job_ok and cpu_note is None and error is None
    except Exception as exc:  # noqa: BLE001 - convert run failures to RunResult
        error = str(exc)
        job_ok = False
    finally:
        total_s = max(0.0, time.monotonic() - started) if sandbox_id else 0.0
        if sandbox_id and not keep:
            try:
                await client.delete_sandbox(sandbox_id)
            except Exception as exc:  # noqa: BLE001 - preserve cleanup failure in result
                cleanup_error = f"sandbox cleanup failed: {exc}"
                error = f"{error}; {cleanup_error}" if error else cleanup_error
                job_ok = False

    rates = PLANS.get(plan, PLANS["starter"])
    hours = total_s / 3600
    cost = hours * (cpu * rates["vcpu_hour"] + (mem_mb / 1024) * rates["gb_hour"])
    try:
        ledger.record(
            "run",
            job_id=job.id,
            cpu=cpu,
            mem_mb=mem_mb,
            sandbox_id=sandbox_id,
            total_s=total_s,
            ok=job_ok,
            cost_usd=cost,
            error=error,
        )
    except Exception:  # noqa: BLE001 - ledger failures must not hide the run result
        pass
    result = RunResult(
        job_id=job.id,
        cpu=cpu,
        mem_mb=mem_mb,
        sandbox_id=sandbox_id,
        boot_s=boot_s,
        cpu_online_s=cpu_online_s,
        steps=steps,
        total_s=total_s,
        ok=job_ok,
        solari_cost_usd=cost,
        error=error,
        browser_seconds=browser_seconds,
        browser_cost_usd=browser_cost_usd,
        browser_session_ids=browser_session_ids,
    )
    await _notify(
        on_event,
        {
            "event": "run_finished",
            "cpu": cpu,
            "status": "done" if result.ok else "failed",
            "seconds": result.total_s,
        },
    )
    return result


def _default_mem_mb_for(cpu: int) -> int:
    return max(2048, 1024 * cpu)


async def run_sizes(
    client: SolariClient,
    job: Job,
    repo: RepoSpec,
    sizes: list[int],
    *,
    mem_mb_for: Callable[[int], int] = _default_mem_mb_for,
    concurrency: int = 2,
    plan: str = "starter",
    keep: bool = False,
    on_event: EventCallback | None = None,
    cloud_browser: bool = False,
    expose_port: int | None = None,
) -> list[RunResult]:
    """Run each requested CPU size with bounded concurrency and isolated failures."""
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    semaphore = asyncio.Semaphore(concurrency)

    async def one(size: int) -> RunResult:
        async with semaphore:
            mem_mb = 0
            try:
                mem_mb = mem_mb_for(size)
                run_kwargs: dict[str, Any] = {
                    "cpu": size,
                    "mem_mb": mem_mb,
                    "plan": plan,
                    "on_event": on_event,
                }
                if keep:
                    run_kwargs["keep"] = True
                if cloud_browser:
                    run_kwargs["cloud_browser"] = True
                if expose_port is not None:
                    run_kwargs["expose_port"] = expose_port
                return await run_job(client, job, repo, **run_kwargs)
            except Exception as exc:  # noqa: BLE001 - isolate one size from its siblings
                return RunResult(
                    job_id=job.id,
                    cpu=size,
                    mem_mb=mem_mb,
                    sandbox_id="",
                    boot_s=0.0,
                    cpu_online_s=0.0,
                    total_s=0.0,
                    ok=False,
                    solari_cost_usd=0.0,
                    error=str(exc),
                )

    results = await asyncio.gather(*(one(size) for size in sizes))
    return sorted(results, key=lambda result: result.cpu)
