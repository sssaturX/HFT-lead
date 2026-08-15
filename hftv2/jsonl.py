from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO


class JsonlWriter:
    def __init__(self, path: Path, flush_every: int = 200) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh: TextIO = path.open("a", encoding="utf-8", buffering=1 << 16)
        self._flush_every = flush_every
        self._n = 0

    def write(self, obj: dict[str, Any]) -> None:
        self._fh.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
        self._fh.write("\n")
        self._n += 1
        if self._n % self._flush_every == 0:
            self._fh.flush()

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()
