"""Daily JSONL files of every snapshot (and, from Stage 2, every decision).

logs/snapshots-YYYY-MM-DD.jsonl, one line per block. When the day rolls over,
yesterday's file is gzipped; files older than `keep_days` are deleted. These
stay on the server (too big for git) and feed the Step 8 calibration check.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import shutil
import time


class SnapshotLog:
    def __init__(self, log_dir: str = "logs", keep_days: int = 14):
        self.dir, self.keep = log_dir, keep_days
        os.makedirs(log_dir, exist_ok=True)
        self._day = None
        self._fh = None

    def write(self, ts: float, rec: dict) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        if day != self._day:
            self._roll(day)
        self._fh.write(json.dumps({"ts": round(ts, 3), **rec}, separators=(",", ":")) + "\n")

    def _roll(self, day: str) -> None:
        if self._fh:
            self._fh.close()
        self._day = day
        self._fh = open(os.path.join(self.dir, f"snapshots-{day}.jsonl"), "a", buffering=1 << 16, encoding="utf-8")
        for path in glob.glob(os.path.join(self.dir, "snapshots-*.jsonl")):
            if not path.endswith(f"{day}.jsonl"):
                with open(path, "rb") as src, gzip.open(path + ".gz", "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.remove(path)
        cutoff = time.time() - self.keep * 86_400
        for path in glob.glob(os.path.join(self.dir, "snapshots-*.jsonl.gz")):
            if os.path.getmtime(path) < cutoff:
                os.remove(path)

    def flush(self) -> None:
        if self._fh:
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
