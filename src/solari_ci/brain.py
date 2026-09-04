"""LLM proposal backends for solci agent mode."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .models import _Model


class BrainError(RuntimeError):
    """Raised when a proposal brain cannot produce a safe proposal."""


@dataclass(frozen=True)
class Proposal(_Model):
    diff: str
    rationale: str
    summary: str
    brain: str
    model: str


class Brain(Protocol):
    def propose(self, evidence_md: str, repo_dir: Path, workflow_rel_path: str) -> Proposal:
        """Propose a workflow-only change from measured evidence."""


def _run(
    cmd: list[str],
    cwd: Path | None = None,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a brain or git subprocess through one testable seam."""
    return subprocess.run(cmd, cwd=cwd, input=input, capture_output=True, text=True, check=False)


def _redact(text: str) -> str:
    detail = text
    for key_name in ("GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_API_KEY"):
        secret = os.environ.get(key_name)
        if secret:
            detail = detail.replace(secret, "[redacted]")
    return detail


def _findings_from_evidence(evidence_md: str) -> str:
    marker = "## Findings"
    if marker not in evidence_md:
        return "No findings section was included in the evidence."
    findings_md = evidence_md.split(marker, 1)[1]
    if "## How measured" in findings_md:
        findings_md = findings_md.split("## How measured", 1)[0]
    return findings_md.strip() or "No findings."


def _build_prompt(
    evidence_md: str,
    findings_md: str,
    workflow_yaml: str,
    workflow_rel_path: str,
) -> str:
    """Build the one shared instruction prompt used by both brains."""
    return f"""You are the proposal stage of solci agent mode.

Use the measured evidence and static findings below to propose the smallest concrete change that
acts on the evidence. Candidate changes include a runner-size hint when the repository uses sized
or self-hosted runners, caching for setup actions, timeout-minutes, a concurrency group, `npm ci`
over `npm install`, or pinning third-party actions to a full SHA.

Rules:
- Edit only {workflow_rel_path}, which is the selected file under .github/workflows.
- Keep the YAML valid.
- Justify every change with the specific measured number from the evidence that motivates it.
- If the evidence does not justify any change, say so explicitly and make no edit. An empty diff is
  a valid and good outcome.
- If you are editing a local checkout, edit the workflow file directly on disk rather than pasting a
  unified diff into your response. Only include a fenced `diff` code block if you are not able to edit
  the file directly, and explain the rationale for the change before the final summary line.
- End your final response with exactly one line in the form `SUMMARY: ...`.

MEASURED EVIDENCE
-----------------
{evidence_md}

STATIC FINDINGS
---------------
{findings_md}

CURRENT WORKFLOW YAML: {workflow_rel_path}
---------------------------------------------
{workflow_yaml}
"""


def _workflow_prompt(evidence_md: str, repo_dir: Path, workflow_rel_path: str) -> str:
    workflow_path = repo_dir / workflow_rel_path
    try:
        workflow_yaml = workflow_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BrainError(f"Could not read workflow {workflow_rel_path}: {exc}") from exc
    return _build_prompt(
        evidence_md,
        _findings_from_evidence(evidence_md),
        workflow_yaml,
        workflow_rel_path,
    )


def _parse_summary(rationale: str) -> tuple[str, str]:
    match = re.search(r"^SUMMARY:\s*(.*?)\s*$", rationale, re.MULTILINE)
    if match:
        cleaned = (rationale[: match.start()] + rationale[match.end() :]).strip()
        summary = match.group(1).strip() or "No summary was provided."
        return cleaned, summary
    cleaned = rationale.strip()
    first_line = next(
        (line.strip() for line in cleaned.splitlines() if line.strip()),
        "No summary was provided.",
    )
    return cleaned, first_line


class CodexBrain:
    """Use the local codex CLI to edit the checked-out workflow."""

    def __init__(self, effort: str = "medium") -> None:
        self.effort = effort
        self.model = "gpt-5.6-luna"

    def propose(self, evidence_md: str, repo_dir: Path, workflow_rel_path: str) -> Proposal:
        prompt = _workflow_prompt(evidence_md, repo_dir, workflow_rel_path)
        descriptor, temporary_name = tempfile.mkstemp(prefix="solci-codex-")
        os.close(descriptor)
        temp_path = Path(temporary_name)
        try:
            command = [
                "codex",
                "exec",
                "--model",
                self.model,
                "-c",
                f"model_reasoning_effort={self.effort}",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "--cd",
                str(repo_dir),
                "--output-last-message",
                str(temp_path),
                "-",
            ]
            result = _run(command, input=prompt)
            try:
                rationale_text = temp_path.read_text(encoding="utf-8")
            except OSError:
                rationale_text = result.stdout or result.stderr
            if result.returncode != 0:
                detail = _redact(result.stderr.strip() or result.stdout.strip() or "no output")
                raise BrainError(f"codex failed with exit code {result.returncode}: {detail}")

            diff_result = _run(["git", "diff"], cwd=repo_dir)
            if diff_result.returncode != 0:
                detail = _redact(diff_result.stderr.strip() or diff_result.stdout.strip() or "no output")
                raise BrainError(f"git diff failed with exit code {diff_result.returncode}: {detail}")
            rationale, summary = _parse_summary(_redact(rationale_text))
            return Proposal(
                diff=diff_result.stdout,
                rationale=rationale,
                summary=summary,
                brain="codex",
                model=self.model,
            )
        except OSError as exc:
            raise BrainError(f"codex could not be started: {_redact(str(exc))}") from exc
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass


class GeminiBrain:
    """Use the Gemini REST API to return and validate a unified diff."""

    def __init__(self, model: str = "gemini-2.5-pro") -> None:
        self.model = model

    def propose(self, evidence_md: str, repo_dir: Path, workflow_rel_path: str) -> Proposal:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise BrainError("GOOGLE_API_KEY is required for the gemini brain")
        prompt = _workflow_prompt(evidence_md, repo_dir, workflow_rel_path)
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        try:
            response = httpx.post(
                endpoint,
                params={"key": api_key},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            raise BrainError(f"gemini request failed: {_redact(str(exc))}") from exc
        if response.status_code >= 400:
            raise BrainError(f"gemini request failed with status {response.status_code}")

        try:
            body = response.json()
            response_text = body["candidates"][0]["content"]["parts"][0]["text"]
            if not isinstance(response_text, str):
                raise TypeError("Gemini response text was not a string")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise BrainError(f"Could not parse Gemini response: {exc}") from exc

        fenced = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", response_text, re.IGNORECASE | re.DOTALL)
        diff = fenced.group(1) if fenced else ""
        rationale = _redact(response_text)
        if not diff:
            rationale = f"{rationale}\nNo unified diff was returned."
            cleaned, summary = _parse_summary(rationale)
            return Proposal(diff="", rationale=cleaned, summary=summary, brain="gemini", model=self.model)

        validation = _run(["git", "apply", "--check"], cwd=repo_dir, input=diff)
        if validation.returncode != 0:
            detail = _redact(validation.stderr.strip() or validation.stdout.strip() or "no output")
            rationale = f"{rationale}\nThe proposed diff failed git apply --check: {detail}"
            cleaned, summary = _parse_summary(rationale)
            return Proposal(diff="", rationale=cleaned, summary=summary, brain="gemini", model=self.model)

        cleaned, summary = _parse_summary(rationale)
        return Proposal(diff=diff, rationale=cleaned, summary=summary, brain="gemini", model=self.model)


def get_brain(name: str | None) -> Brain:
    """Select an explicitly requested brain or the first locally available backend."""
    if name == "codex":
        return CodexBrain()
    if name == "gemini":
        return GeminiBrain()
    if name is not None:
        raise BrainError(f"Unknown brain {name!r}; choose codex or gemini")
    if shutil.which("codex"):
        return CodexBrain()
    if os.environ.get("GOOGLE_API_KEY"):
        return GeminiBrain()
    raise BrainError("No brain is available: install codex or set GOOGLE_API_KEY for Gemini")
