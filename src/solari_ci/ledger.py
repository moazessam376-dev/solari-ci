"""Optional local ledger of solari-ci runs, shared with `solari-lab`.

solci appends one JSONL line per sandbox run to `~/.solari-lab/ledger.jsonl`,
shared with the other `solari-*` tools, so spend can be estimated client-side.
Nothing is written unless the ledger is enabled.

Enable it with the `SOLARI_LAB_LEDGER` env var (a file path), or by creating
the default directory `~/.solari-lab/`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path.home() / ".solari-lab"
DEFAULT_FILE = DEFAULT_DIR / "ledger.jsonl"


def ledger_path() -> Path | None:
    env = os.environ.get("SOLARI_LAB_LEDGER")
    if env:
        return Path(env).expanduser()
    if DEFAULT_DIR.is_dir():
        return DEFAULT_FILE
    return None


def record(event: str, **fields: Any) -> None:
    """Append one event. Never raises: a ledger failure must not fail a run."""
    path = ledger_path()
    if path is None:
        return
    line = {"ts": time.time(), "event": event, "source": "solari-ci", **fields}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, separators=(",", ":")) + "\n")
    except OSError:
        pass
