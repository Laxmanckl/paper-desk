import json
import time
import urllib.error
import urllib.request

import pytest

from jevsys import dashboard
from jevsys.__main__ import main
from jevsys.desk import Desk
from jevsys.market import MarketStore
from jevsys.sim_fx import SimBridge
from jevsys.web import serve


def make(tmp_path, days=3):
    now = time.time()
    sim = SimBridge(seed=4, start_ts=now - days * 86_400)
    m = MarketStore()
    m.ingest(sim.step_to(now), now)
    d = Desk(m, state_path=str(tmp_path / "state" / "jev_desk.json"), log_dir=str(tmp_path / "logs"))
    return d, sim, now


def test_round_logs_every_instrument(tmp_path):
    d, _, now = make(tmp_path)
    out = d.decide_round(now)
    assert len(out) == 8 and all(r["fresh"] for r in out.values())
    lines = [json.loads(x) for x in open(next((tmp_path / "logs").glob("snapshots-*.jsonl")))]
    assert {x["sym"] for x in lines} == set(out) and all(x["model"] is None for x in lines)
    st = d.state(full=True)
    assert st["totals"]["rounds"] == 1 and 0 < st["engine"]["tokens_max"] <= 400
    assert st["instruments"]["XAUUSD"]["series"] and st["instruments"]["XAUUSD"]["last"]["fresh"]


def test_stale_round_holds_and_says_why(tmp_path):
    d, _, now = make(tmp_path)
    out = d.decide_round(now + 4 * 3600)
    assert not any(r["fresh"] for r in out.values())
    assert all(r["decision"] is None for r in out.values())
    assert any("No fresh prices" in e[1] for e in d.state()["log"])


def test_schedule_aligns_to_bar_close(tmp_path):
    d, _, _ = make(tmp_path, days=1)
    t = d.next_round(1_760_000_123.0)
    assert (t - d.cfg.grace_s) % (15 * 60) == 0 and t > 1_760_000_123.0


def test_restart_keeps_history(tmp_path):
    d, _, now = make(tmp_path)
    d.decide_round(now)
    d.save()
    d2 = Desk(MarketStore(), state_path=d.state_path, log_dir=str(tmp_path / "logs"))
    assert d2.state()["totals"]["rounds"] == 1


def test_ingest_endpoint_needs_token(tmp_path):
    d, sim, now = make(tmp_path, days=2)
    srv = serve(d, 0, token="s3cret")
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    body = json.dumps(sim.step_to(time.time())).encode()

    def post(tok):
        req = urllib.request.Request(url + "/api/ingest", data=body, method="POST",
                                     headers={"X-Bridge-Token": tok, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=5)

    with pytest.raises(urllib.error.HTTPError) as e:
        post("wrong")
    assert e.value.code == 401
    res = json.loads(post("s3cret").read())
    assert res["ok"] and res["need_history"] == []
    st = json.loads(urllib.request.urlopen(url + "/api/state", timeout=5).read())
    assert st["bridge"]["status"] == "live"
    assert "<title>Jev Desk</title>" in urllib.request.urlopen(url + "/", timeout=5).read().decode()
    srv.shutdown()


def test_dashboard_escapes_and_embeds(tmp_path):
    d, _, now = make(tmp_path, days=1)
    d.decide_round(now)
    d.event("</script><b>x</b>")
    html = dashboard.build(d.state(full=True))
    assert "</script><b>" not in html
    blob = html.split('id="desk-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(blob)["state"]["totals"]["rounds"] == 1


def test_heartbeat(tmp_path):
    d, _, _ = make(tmp_path, days=1)
    path = d.save()
    main(["heartbeat", "--state", path])
    st = json.load(open(path))
    st["server_time"] -= 3600
    json.dump(st, open(path, "w"))
    with pytest.raises(SystemExit):
        main(["heartbeat", "--state", path])
