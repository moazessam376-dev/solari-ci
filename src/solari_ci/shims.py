"""Small, explicit shims for the Actions used by the runner."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .models import Step

if TYPE_CHECKING:
    from .runner import StepScript


@dataclass(frozen=True)
class SkipNote:
    reason: str


_ENV_FILE = "/tmp/solci/env.sh"
_PATH_FILE = "/tmp/solci/path"


def _make_script(name: str, script: str, ctx: dict[str, Any]) -> StepScript:
    # Import lazily to avoid a runner <-> shims import cycle at module load time.
    from .runner import StepScript

    return StepScript(
        name=name,
        script=script,
        cwd=str(ctx.get("workspace") or "/work/repo"),
        env={},
        shell="bash",
    )


def _persist_path(directory: str) -> str:
    quoted = shlex.quote(directory)
    line = f"export PATH={quoted}:$PATH"
    return "\n".join(
        (
            "mkdir -p /tmp/solci",
            f"printf '%s\\n' {shlex.quote(line)} >> {_ENV_FILE}",
            f"printf '%s\\n' {shlex.quote(line)} >> {_PATH_FILE}",
        )
    )


def _setup_uv_script(name: str, ctx: dict[str, Any]) -> StepScript:
    script = "\n".join(
        (
            "set -e",
            "export HOME=/root",
            "export PATH=/root/.local/bin:$PATH",
            "mkdir -p /tmp/solci",
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            _persist_path("/root/.local/bin"),
            "uv --version",
        )
    )
    return _make_script(name, script, ctx)


def _python_version(with_: dict[str, Any]) -> str:
    value = with_.get("python-version", "3.11")
    return str(value).strip() or "3.11"


def _major_minor(version: str) -> str:
    match = re.match(r"^(\d+\.\d+)", version)
    return match.group(1) if match else version


def _setup_python_script(step: Step, ctx: dict[str, Any]) -> StepScript:
    version = _python_version(step.with_)
    if _major_minor(version) == "3.11":
        script = "python3 --version\npython3 -m pip --version"
        return _make_script(step.name, script, ctx)

    quoted_version = shlex.quote(version)
    shim_dir = f"/tmp/solci/python-{_major_minor(version).replace('.', '-') }"
    script = "\n".join(
        (
            "set -e",
            "export HOME=/root",
            "export PATH=/root/.local/bin:$PATH",
            "mkdir -p /tmp/solci",
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "export PATH=/root/.local/bin:$PATH",
            f"uv python install {quoted_version}",
            f"shim_dir={shlex.quote(shim_dir)}",
            'mkdir -p "$shim_dir"',
            f"python_bin=\"$(uv python find {quoted_version})\"",
            'test -n "$python_bin"',
            'ln -sf "$python_bin" "$shim_dir/python"',
            'ln -sf "$python_bin" "$shim_dir/python3"',
            "cat > \"$shim_dir/pip\" <<'EOF'",
            "#!/bin/sh",
            'exec uv pip "$@"',
            "EOF",
            'chmod +x "$shim_dir/pip"',
            _persist_path(shim_dir),
            '"$shim_dir/python" --version',
        )
    )
    return _make_script(step.name, script, ctx)


def _node_major(value: Any) -> str:
    version = str(value if value is not None else "18").strip()
    match = re.match(r"^(\d+)", version)
    return match.group(1) if match else "18"


def _setup_node_script(step: Step, ctx: dict[str, Any]) -> StepScript:
    major = _node_major(step.with_.get("node-version", "18"))
    if major == "18":
        return _make_script(step.name, "node --version\nnpm --version", ctx)

    base_url = f"https://nodejs.org/dist/latest-v{major}.x"
    script = "\n".join(
        (
            "set -e",
            "export HOME=/root",
            "mkdir -p /tmp/solci",
            f"base_url={shlex.quote(base_url)}",
            'archive_name="$(curl -fsSL "$base_url/" | sed -n \'s/.*href="[^\"]*\\(node-v[^\"]*-linux-x64\\.tar\\.xz\\)".*/\\1/p\' | head -n 1)"',
            'test -n "$archive_name"',
            'curl -fsSL "$base_url/$archive_name" -o /tmp/solci/node.tar.xz',
            "rm -rf /opt/node",
            "mkdir -p /opt/node",
            "tar -xJf /tmp/solci/node.tar.xz -C /opt/node --strip-components=1",
            _persist_path("/opt/node/bin"),
            "/opt/node/bin/node --version",
            "/opt/node/bin/npm --version",
        )
    )
    return _make_script(step.name, script, ctx)


def _setup_pnpm_script(step: Step, ctx: dict[str, Any]) -> StepScript:
    version = str(step.with_.get("version") or "latest")
    package = shlex.quote(f"pnpm@{version}")
    return _make_script(step.name, f"set -e\nnpm i -g {package}\npnpm --version", ctx)


def _setup_bun_script(name: str, ctx: dict[str, Any]) -> StepScript:
    script = "\n".join(
        (
            "set -e",
            "export HOME=/root",
            "curl -fsSL https://bun.sh/install | bash",
            "export PATH=/root/.bun/bin:$PATH",
            _persist_path("/root/.bun/bin"),
            "bun --version",
        )
    )
    return _make_script(name, script, ctx)


def apply(step: Step, ctx: dict[str, Any]) -> StepScript | SkipNote:
    """Translate a supported action step into a shell script or a note."""
    uses = (step.uses or "").strip()
    action = uses.split("@", 1)[0]

    if action == "actions/checkout":
        return SkipNote("checkout done by runner")
    if action == "actions/setup-python":
        return _setup_python_script(step, ctx)
    if action == "astral-sh/setup-uv":
        return _setup_uv_script(step.name, ctx)
    if action == "actions/setup-node":
        return _setup_node_script(step, ctx)
    if action == "pnpm/action-setup":
        return _setup_pnpm_script(step, ctx)
    if action == "oven-sh/setup-bun":
        return _setup_bun_script(step.name, ctx)
    if action in {
        "actions/cache",
        "actions/upload-artifact",
        "actions/download-artifact",
    } or action.startswith("codecov/"):
        return SkipNote("no-op on solci")
    return SkipNote(f"unsupported action {uses}: skipped")
