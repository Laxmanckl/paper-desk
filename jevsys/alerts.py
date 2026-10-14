"""Telegram alerts (guide Step 6, rung 5: "kill switch, close positions, send yourself an alert").

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in /etc/jev-desk.env (set up with
server/telegram.sh). Without them, alerts are simply off. Messages go out on a
background thread, so a slow network never delays a trading decision. Repeats
of the same alert are held back for `cooldown_s`.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request


class Alerter:
    def __init__(self, token: str | None = None, chat: str | None = None, send_fn=None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat = chat if chat is not None else os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self._custom = send_fn is not None
        self._send_fn = send_fn or self._telegram
        self._q: queue.Queue = queue.Queue(maxsize=200)
        self._last: dict[str, float] = {}
        self.sent = self.failed = 0
        self.last_error: str | None = None
        self.history: list[list] = []
        if self.enabled:
            threading.Thread(target=self._worker, daemon=True, name="alerts").start()

    @property
    def enabled(self) -> bool:
        return self._custom or bool(self.token and self.chat)

    def send(self, text: str, key: str | None = None, cooldown_s: float = 0.0) -> bool:
        """Queue a message. With a key, the same key won't be sent again within cooldown_s."""
        now = time.time()
        if key and cooldown_s and now - self._last.get(key, 0) < cooldown_s:
            return False
        if key:
            self._last[key] = now
        self.history = (self.history + [[round(now), text]])[-50:]
        if not self.enabled:
            return False
        try:
            self._q.put_nowait(text)
            return True
        except queue.Full:
            return False

    def send_now(self, text: str) -> None:
        """Synchronous send for the test command; raises on failure."""
        self._send_fn(text)

    def _worker(self) -> None:
        while True:
            text = self._q.get()
            for attempt in range(3):
                try:
                    self._send_fn(text)
                    self.sent += 1
                    break
                except Exception as e:  # network blip: retry, then give up on this message
                    self.last_error = str(e)[:200]
                    time.sleep(2 * (attempt + 1))
            else:
                self.failed += 1

    def _telegram(self, text: str) -> None:
        body = urllib.parse.urlencode({"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": "true"}).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{self.token}/sendMessage", data=body, timeout=15) as r:
            res = json.loads(r.read())
        if not res.get("ok"):
            raise RuntimeError(res.get("description", "Telegram refused the message"))

    def status(self) -> dict:
        return {"enabled": self.enabled, "sent": self.sent, "failed": self.failed, "last_error": self.last_error,
                "recent": self.history[-10:]}
