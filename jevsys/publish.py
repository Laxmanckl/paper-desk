"""Pushes the desk state to GitHub so Pages can rebuild the dashboard.

Same pattern as the old Paper Desk: the server commits state/jev_desk.json
every N minutes; the workflow sees the push and redeploys the page.
"""
from __future__ import annotations

import subprocess
import threading
import time


def _git(*args: str, cwd: str = ".") -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)


def push_state(path: str, cwd: str = ".") -> str:
    _git("add", path, cwd=cwd)
    if _git("diff", "--cached", "--quiet", cwd=cwd).returncode == 0:
        return "nothing to push"
    _git("commit", "-q", "-m", f"jev desk {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}", cwd=cwd)
    pull = _git("pull", "-q", "--rebase", "-X", "theirs", "--autostash", "origin", "main", cwd=cwd)
    if pull.returncode != 0:
        _git("rebase", "--abort", cwd=cwd)
        return "pull failed: " + (pull.stderr.strip().splitlines() or ["?"])[-1]
    push = _git("push", "-q", "origin", "HEAD:main", cwd=cwd)
    if push.returncode != 0:
        return "push failed: " + (push.stderr.strip().splitlines() or ["?"])[-1]
    return "pushed"


class Publisher(threading.Thread):
    def __init__(self, desk, every_min: float, stop: threading.Event):
        super().__init__(daemon=True, name="publisher")
        self.desk, self.every, self.stop = desk, every_min * 60, stop

    def run(self) -> None:
        # first push after one minute, so a fresh install shows up quickly
        if self.stop.wait(60):
            return
        while True:
            self.desk.save()
            res = push_state(self.desk.state_path)
            if res != "pushed":
                self.desk.event(f"GitHub publish: {res}")
            if self.stop.wait(self.every):
                return
