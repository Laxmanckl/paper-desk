"""Append-only JSONL log. Stage 1 logs snapshots; later stages add the Jev
decision, model version and outcome to the same record, which is what the
calibration check in guide Step 8 reads."""
from __future__ import annotations

import json
import os
import time


class JsonlLogger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._fh = open(path, "a", buffering=1, encoding="utf-8")

    def log(self, kind: str, **fields) -> None:
        rec = {"kind": kind, "logged_at": time.time(), **fields}
        self._fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def close(self) -> None:
        self._fh.close()
