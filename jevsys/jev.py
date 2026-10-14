"""Stage 2: the Jev decision layer (guide Steps 2 and 4).

For every instrument with a fresh snapshot, one `system_one` call asks the
six-question battery. The eight instruments are asked in parallel, so a round
takes about as long as one call.

Rules from the guide, enforced here:
- Call TypeSafe directly (api.typesafe.ai), not through a gateway.
- Pin the model version and log it with every answer. If TypeSafe reports a
  different model than the pinned one, the answer is flagged.
- Questions are atomic, and every Choice option has a clear description.
- Jev only judges. It never decides to trade; that is the policy engine (Stage 3).
- A late answer is no answer: anything slower than the deadline is discarded.

The API key comes from the TYPESAFE_API_KEY environment variable, which the
service loads from /etc/jev-desk.env. It is never logged or shown.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

MODEL_VERSION = "jev-1.13.0"
DEADLINE_S = 20.0  # whole round; a 15-min cadence has plenty of room, but stale answers never count

# key, type, instructions, criteria (Choice: {name: description}; Score: [levels]; Noul: None)
BATTERY = [
    ("regime", "Choice", "What market regime does this state describe right now?", {
        "trending": "Price moving persistently in one direction",
        "ranging": "Price oscillating within a stable range",
        "volatile": "Large moves in both directions without clear structure",
        "quiet": "Very small moves and little activity",
    }),
    ("direction", "Choice", "What is the most likely price bias over the next hour?", {
        "up": "Price more likely to be higher in an hour",
        "down": "Price more likely to be lower in an hour",
        "neutral": "No clear bias either way",
    }),
    ("move_continues", "Noul",
     "The most recent price move is likely to continue over the next hour rather than reverse", None),
    ("costs_abnormal", "Noul",
     "The spread is abnormally wide compared with normal conditions for this instrument", None),
    ("entry_quality", "Score", "How favorable is this state for opening a new directional position?",
     ["Do not enter", "Marginal", "Good", "Excellent"]),
    ("exit_urgency", "Score", "How urgently should an open position in this instrument be reduced or closed?",
     ["No urgency", "Mild", "Tighten the stop", "Close now"]),
]


def questions():
    """The battery as SDK question objects (built lazily so the desk runs without the SDK)."""
    from typesafe_sdk import Choice, Noul, Score
    out = {}
    for key, kind, ask, crit in BATTERY:
        if kind == "Choice":
            out[key] = Choice(instructions=ask, criteria=crit)
        elif kind == "Score":
            out[key] = Score(instructions=ask, criteria=crit)
        else:
            out[key] = Noul(instructions=ask)
    return out


def dashboard_questions() -> list[dict]:
    return [{"key": k, "type": t, "ask": a,
             "options": list(c) if isinstance(c, dict) else c if c else None} for k, t, a, c in BATTERY]


def parse(resp) -> dict:
    """SDK response -> plain JSON-able dict, the same shape the log and dashboard use."""
    out = {}
    for key, kind, _, crit in BATTERY:
        a = resp.answers.get(key)
        if a is None:
            continue
        if kind == "Choice":
            out[key] = {"value": a.choice, "p": round(a.confidence, 4),
                        "probs": {k: round(v, 4) for k, v in a.probabilities.items()}}
        elif kind == "Score":
            level = max(a.probabilities, key=a.probabilities.get) if a.probabilities else round(a.score)
            out[key] = {"score": round(a.score, 3), "p": round(a.confidence, 4), "level": int(level),
                        "label": crit[int(level)] if 0 <= int(level) < len(crit) else str(level),
                        "probs": {str(k): round(v, 4) for k, v in a.probabilities.items()}}
        else:
            out[key] = {"p": round(a.noul, 4)}
    return out


def _error_kind(e: Exception) -> str:
    name = type(e).__name__
    return {"TypeSafeAuthenticationError": "auth", "TypeSafePermissionDeniedError": "auth",
            "TypeSafeRateLimitError": "rate_limit", "TypeSafeAPITimeoutError": "timeout",
            "TypeSafeAPIConnectionError": "network"}.get(name, "error")


class JevLayer:
    def __init__(self, model: str = MODEL_VERSION, client=None, deadline_s: float = DEADLINE_S):
        self.model = model
        self.deadline_s = deadline_s
        self.lock = threading.Lock()
        self.reason = ""
        self.client = client
        self.calls = self.errors = self.late = self.consec_errors = 0
        self.tokens_in = 0
        self.latencies: list[float] = []
        self.last_model_seen: str | None = None
        self.last_error: str | None = None
        if self.client is None:
            if not os.environ.get("TYPESAFE_API_KEY", "").strip():
                self.reason = "no API key on the server yet"
            else:
                try:
                    from typesafe_sdk import RetryPolicy, TypeSafeClient
                    # one quick retry; the round deadline still caps total time
                    self.client = TypeSafeClient(model=model, timeout=8.0,
                                                 retry=RetryPolicy(max_retries=1, backoff_initial=0.5, backoff_max=1.0))
                except ImportError:
                    self.reason = "typesafe-sdk not installed (run server/setup.sh)"
                except Exception as e:  # invalid key format etc.
                    self.reason = f"cannot start: {e}"
        self._questions = questions() if self.client is not None else None
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="jev") if self.client is not None else None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _ask_one(self, snap: dict) -> dict:
        t0 = time.perf_counter()
        try:
            resp = self.client.system_one(state=snap, questions=self._questions, model=self.model)
        except Exception as e:  # recorded per instrument; the round carries on
            return {"error": str(e)[:200], "kind": _error_kind(e), "ms": round((time.perf_counter() - t0) * 1000)}
        ms = round((time.perf_counter() - t0) * 1000)
        return {"answers": parse(resp), "model": resp.model, "ms": ms,
                "tokens_in": getattr(resp.usage, "input_tokens", None),
                "pinned_ok": resp.model == self.model}

    def ask_all(self, snaps: dict[str, dict]) -> dict[str, dict]:
        """Ask the battery for every snapshot in parallel. Late answers are discarded."""
        if not self.enabled or not snaps:
            return {}
        start = time.perf_counter()
        futs = {self._pool.submit(self._ask_one, s): sym for sym, s in snaps.items()}
        done, pending = wait(futs, timeout=self.deadline_s)
        out = {}
        for f, sym in futs.items():
            if f in done:
                out[sym] = f.result()
            else:
                f.cancel()
                out[sym] = {"error": f"no answer within {self.deadline_s:.0f}s", "kind": "late",
                            "ms": round((time.perf_counter() - start) * 1000)}
        with self.lock:
            for r in out.values():
                self.calls += 1
                if "error" in r:
                    self.errors += 1
                    self.late += r["kind"] == "late"
                    self.last_error = f"{r['kind']}: {r['error']}"
                else:
                    self.latencies = (self.latencies + [r["ms"]])[-200:]
                    self.tokens_in += r.get("tokens_in") or 0
                    self.last_model_seen = r["model"]
            ok = sum(1 for r in out.values() if "error" not in r)
            self.consec_errors = 0 if ok else self.consec_errors + 1
        return out

    def status(self) -> dict:
        with self.lock:
            lat = sorted(self.latencies)
            st = ("live" if self.enabled and self.consec_errors == 0 and self.calls
                  else "starting" if self.enabled and not self.calls
                  else "errors" if self.enabled else "off")
            return {"status": st, "reason": self.reason, "model": self.model, "model_seen": self.last_model_seen,
                    "calls": self.calls, "errors": self.errors, "late": self.late, "consec_errors": self.consec_errors,
                    "latency_p50_ms": lat[len(lat) // 2] if lat else None, "tokens_in": self.tokens_in,
                    "last_error": self.last_error}
