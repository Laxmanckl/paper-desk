"""Stage 2, tested with the real typesafe-sdk talking to a simulated TypeSafe API."""
import json
import time

import httpx2
import pytest
from typesafe_sdk import RetryPolicy, TypeSafeClient

from jevsys.desk import Desk
from jevsys.jev import BATTERY, MODEL_VERSION, JevLayer
from jevsys.market import MarketStore
from jevsys.sim_fx import SimBridge


def fake_api(model=MODEL_VERSION, status=200, delay=0.0, seen=None):
    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        if seen is not None:
            seen.append({"body": body, "auth": req.headers.get("authorization"), "url": str(req.url)})
        if delay:
            time.sleep(delay)
        if status != 200:
            return httpx2.Response(status, json={"detail": "nope"})
        answers = {}
        for name, q in body["questions"].items():
            if q["type"] == "choice":
                opts = list(q["criteria"])
                probs = {o: (0.7 if i == 0 else 0.3 / (len(opts) - 1)) for i, o in enumerate(opts)}
                answers[name] = {"type": "choice", "choice": opts[0], "confidence": 0.7, "probabilities": probs}
            elif q["type"] == "score":
                n = len(q["criteria"])
                probs = {str(i): (0.6 if i == 2 else 0.4 / (n - 1)) for i in range(n)}
                answers[name] = {"type": "score", "score": 1.9, "confidence": 0.6,
                                 "legend": {str(i): c for i, c in enumerate(q["criteria"])}, "probabilities": probs}
            else:
                answers[name] = {"type": "noul", "noul": 0.83}
        return httpx2.Response(200, json={"model": model, "usage": {"input_tokens": 420, "output_tokens": 6},
                                          "answers": answers})
    return handler


def layer(**kw):
    client = TypeSafeClient(api_key="test-key", model=MODEL_VERSION, timeout=5,
                            retry=RetryPolicy(max_retries=0), transport=httpx2.MockTransport(fake_api(**kw)))
    return JevLayer(client=client, deadline_s=kw.pop("deadline", 5) if "deadline" in kw else 5)


def snaps():
    now = time.time()
    m = MarketStore()
    m.ingest(SimBridge(seed=2, start_ts=now - 3 * 86_400).step_to(now), now)
    from jevsys.fx_state import FxStateEngine
    eng = FxStateEngine(m)
    return {s: eng.snapshot(s, now) for s in m.data}, m, now


def test_request_follows_the_guide():
    seen = []
    client = TypeSafeClient(api_key="test-key", model=MODEL_VERSION, retry=RetryPolicy(max_retries=0),
                            transport=httpx2.MockTransport(fake_api(seen=seen)))
    j = JevLayer(client=client)
    s, _, _ = snaps()
    j.ask_all({"XAUUSD": s["XAUUSD"]})
    req = seen[0]
    assert req["url"] == "https://api.typesafe.ai/v1/systemone"          # direct API, no gateway
    assert req["body"]["model"] == "jev-1.13.0"                            # pinned
    assert req["body"]["state"] == s["XAUUSD"]                             # the snapshot, numbers only
    assert req["auth"] == "Bearer test-key"
    qs = req["body"]["questions"]
    assert {k: q["type"] for k, q in qs.items()} == {k: t.lower() for k, t, _, _ in BATTERY}
    assert all(v for v in qs["regime"]["criteria"].values())               # every Choice option described


def test_answers_parsed_for_all_instruments_in_parallel():
    s, _, _ = snaps()
    j = layer(delay=0.3)
    t0 = time.perf_counter()
    out = j.ask_all(s)
    assert time.perf_counter() - t0 < 1.5                                  # 8 x 0.3 s ran in parallel
    a = out["EURUSD"]
    assert a["model"] == MODEL_VERSION and a["pinned_ok"] and a["tokens_in"] == 420
    ans = a["answers"]
    assert ans["regime"] == {"value": "trending", "p": 0.7, "probs": ans["regime"]["probs"]}
    assert ans["move_continues"] == {"p": 0.83}
    assert ans["entry_quality"]["level"] == 2 and ans["entry_quality"]["label"] == "Good"
    st = j.status()
    assert st["status"] == "live" and st["calls"] == 8 and st["tokens_in"] == 8 * 420


def test_model_drift_is_flagged(tmp_path):
    s, m, now = snaps()
    client = TypeSafeClient(api_key="k", model=MODEL_VERSION, retry=RetryPolicy(max_retries=0),
                            transport=httpx2.MockTransport(fake_api(model="jev-1.14.0")))
    d = Desk(m, state_path=str(tmp_path / "s.json"), log_dir=str(tmp_path / "logs"), jev=JevLayer(client=client))
    d.decide_round(now)
    assert any("not the pinned jev-1.13.0" in e[1] for e in d.state()["log"])


def test_late_answers_are_discarded():
    s, _, _ = snaps()
    client = TypeSafeClient(api_key="k", model=MODEL_VERSION, retry=RetryPolicy(max_retries=0),
                            transport=httpx2.MockTransport(fake_api(delay=1.5)))
    j = JevLayer(client=client, deadline_s=0.5)
    out = j.ask_all({"EURUSD": s["EURUSD"]})
    assert out["EURUSD"]["kind"] == "late" and "answers" not in out["EURUSD"]


@pytest.mark.parametrize("status,kind", [(401, "auth"), (429, "rate_limit"), (500, "error")])
def test_api_errors_dont_crash_the_round(tmp_path, status, kind):
    s, m, now = snaps()
    client = TypeSafeClient(api_key="k", model=MODEL_VERSION, retry=RetryPolicy(max_retries=0),
                            transport=httpx2.MockTransport(fake_api(status=status)))
    d = Desk(m, state_path=str(tmp_path / "s.json"), log_dir=str(tmp_path / "logs"), jev=JevLayer(client=client))
    out = d.decide_round(now)
    assert all(r["jev"]["kind"] == kind for r in out.values())
    assert all(r["decision"] is None for r in out.values())
    st = d.state()
    assert st["jev"]["status"] == "errors"
    if kind == "auth":
        assert any("rejected the API key" in e[1] for e in st["log"])


def test_round_logs_snapshot_answers_and_model_together(tmp_path):
    s, m, now = snaps()
    d = Desk(m, state_path=str(tmp_path / "s.json"), log_dir=str(tmp_path / "logs"), jev=layer())
    d.decide_round(now)
    lines = [json.loads(x) for x in open(next((tmp_path / "logs").glob("snapshots-*.jsonl")))]
    assert len(lines) == 8
    for x in lines:
        assert x["model"] == x["pinned"] == "jev-1.13.0"
        assert x["jev"]["regime"]["value"] == "trending" and x["snap"]["mid"] > 0
    st = d.state(full=True)
    assert st["desk"]["stage"] == 2 and st["stages"][1]["status"] == "live"
    assert st["instruments"]["XAUUSD"]["last"]["jev"]["answers"]["direction"]["value"] == "up"
    assert any("First Jev answers" in e[1] for e in st["log"])


def test_stale_instruments_are_not_sent_to_jev(tmp_path):
    seen = []
    s, m, now = snaps()
    client = TypeSafeClient(api_key="k", model=MODEL_VERSION, retry=RetryPolicy(max_retries=0),
                            transport=httpx2.MockTransport(fake_api(seen=seen)))
    d = Desk(m, state_path=str(tmp_path / "s.json"), log_dir=str(tmp_path / "logs"), jev=JevLayer(client=client))
    d.decide_round(now + 3600)  # an hour with no prices: everything stale
    assert seen == []


def test_no_key_means_jev_off(monkeypatch, tmp_path):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    j = JevLayer()
    assert not j.enabled and "no API key" in j.reason and j.ask_all({"X": {}}) == {}
    s, m, now = snaps()
    d = Desk(m, state_path=str(tmp_path / "s.json"), log_dir=str(tmp_path / "logs"), jev=j)
    out = d.decide_round(now)
    assert all(r["jev"] is None for r in out.values())
    st = d.state()
    assert st["stages"][1]["status"] == "needs key" and st["jev"]["status"] == "off"
