"""Async client for the Solari sandbox API."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx


class SolariError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


class SolariClient:
    def __init__(self, api_key: str | None = None, *, base_url: str = "https://api.getsolari.com", timeout_s: float = 30.0):
        self.api_key = api_key or os.environ.get("SOLARI_API_KEY")
        if not self.api_key:
            raise SolariError("SOLARI_API_KEY is required")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._http: httpx.AsyncClient | None = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}", "User-Agent": "solari-ci/0.1"},
                timeout=self.timeout_s,
            )
        return self._http

    @staticmethod
    def _parse_body(text: str) -> Any:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _code(body: Any) -> str | None:
        return body.get("code") if isinstance(body, dict) and isinstance(body.get("code"), str) else None

    def _error(self, method: str, path: str, response: httpx.Response) -> SolariError:
        body = self._parse_body(response.text)
        text = response.text[:500]
        return SolariError(f"{method} {path} failed with status {response.status_code}: {text}", response.status_code, self._code(body), body)

    async def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
                       retries: int = 1, retry_statuses: tuple[int, ...] = ()) -> Any:
        client = await self._client()
        for attempt in range(retries):
            try:
                response = await client.request(method, path, json=json_body)
            except httpx.HTTPError as exc:
                if attempt + 1 == retries:
                    raise SolariError(f"{method} {path} failed: {exc}") from exc
                await self._sleep(5 * (attempt + 1) if method == "POST" and path == "/sandboxes" else 1.5 * (attempt + 1))
                continue
            if response.status_code in retry_statuses and attempt + 1 < retries:
                await self._sleep(5 * (attempt + 1) if method == "POST" and path == "/sandboxes" else 1.5 * (attempt + 1))
                continue
            if response.is_error:
                raise self._error(method, path, response)
            return self._parse_body(response.text)
        raise AssertionError("unreachable")

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio
        await asyncio.sleep(seconds)

    async def create_sandbox(self, cpu: int, mem_mb: int, timeout_ms: int = 600_000, template: str = "base") -> dict:
        body = await self._request("POST", "/sandboxes", json_body={"template": template, "kind": "sandbox", "cpu": cpu, "memMb": mem_mb, "timeoutMs": timeout_ms}, retries=6, retry_statuses=(502, 503, 504, 429))
        if not isinstance(body, dict) or not body.get("sandboxId"):
            raise SolariError("POST /sandboxes returned a response without sandboxId", body=body)
        return body

    async def get_sandbox(self, sandbox_id: str) -> dict:
        body = await self._request("GET", f"/sandboxes/{sandbox_id}")
        if not isinstance(body, dict):
            raise SolariError("GET sandbox returned a non-object response", body=body)
        return body

    async def exec(self, sandbox_id: str, cmd: str, args: list[str], timeout_ms: int = 24_000, cwd: str | None = None) -> dict:
        body = await self._request("POST", f"/sandboxes/{sandbox_id}/exec", json_body={"cmd": cmd, "args": args, "cwd": cwd, "timeoutMs": min(timeout_ms, 24_000)}, retries=5, retry_statuses=(502, 503, 504))
        if not isinstance(body, dict):
            raise SolariError("POST sandbox exec returned a non-object response", body=body)
        return {**body, "exitCode": body.get("exitCode"), "stdout": body.get("stdout", ""), "stderr": body.get("stderr", "")}

    async def delete_sandbox(self, sandbox_id: str) -> None:
        client = await self._client()
        response = await client.delete(f"/sandboxes/{sandbox_id}")
        if response.status_code != 404 and response.is_error:
            raise self._error("DELETE", f"/sandboxes/{sandbox_id}", response)

    async def list_sandboxes(self) -> list[dict]:
        body = await self._request("GET", "/sandboxes")
        items = body.get("sandboxes", []) if isinstance(body, dict) else body
        return items if isinstance(items, list) else []

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> SolariClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        await self.close()


def load_env(path: str | Path = ".env") -> None:
    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
