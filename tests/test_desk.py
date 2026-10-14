import json
import time

import pytest

from jevsys import dashboard
from jevsys.__main__ import main
from jevsys.desk import Desk
from jevsys.feeds.sim import SimFeed


def make_desk(tmp_path, n=400, start=None):
    feed = SimFeed(n_blocks=n, seed=5, start_ts=start or time.time() - n * 0.3)
    return Desk(feed, state_path=str(tmp_path / "state" / "jev_desk.json"), log_dir=str(tmp_path / "logs"))


def test_desk_runs_and_reports(tmp_path):
    d = make_desk(tmp_path)
    d.run(realtime=False)
    st = d.state(full=True)
    assert st["totals"]["blocks"] == 400 and st["totals"]["errors"] == 0
    assert 0 < st["engine"]["tokens"] <= 400
    assert st["snapshot"]["blk"] == 400
    assert st["jev"]["model"] == "jev-1.13.0"
    assert [s["status"] for s in st["stages"]][:2] == ["live", "pending"]
    assert len(st["series"]) >= 3  # one point per 30 s over 120 s
    logs = list((tmp_path / "logs").glob("snapshots-*.jsonl"))
    assert logs and sum(1 for _ in open(logs[0])) == 400


def test_state_survives_restart(tmp_path):
    d = make_desk(tmp_path)
    d.run(realtime=False)
    d.save()
    d2 = make_desk(tmp_path, n=10, start=time.time() + 1)
    st = d2.state(full=True)
    assert st["totals"]["blocks"] == 400  # carried over
    assert len(st["series"]) >= 3 and any("Desk started" in e[1] for e in st["log"])


def test_rejected_block_is_counted_not_fatal(tmp_path):
    d = make_desk(tmp_path, n=5)
    blocks = list(d.feed.blocks())
    d.step(blocks[1])
    assert d.step(blocks[0]) is None  # older block after newer one
    assert d.state()["totals"]["rejected"] == 1


def test_dashboard_embeds_state_and_escapes(tmp_path):
    d = make_desk(tmp_path, n=50)
    d.run(realtime=False)
    d.event("</script><b>x</b>")
    html = dashboard.build(d.state(full=True))
    assert html.startswith("<!doctype html>") and "Jev Desk" in html
    assert "</script><b>" not in html  # cannot break out of the data block
    blob = html.split('id="desk-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(blob)["state"]["totals"]["blocks"] == 50


def test_heartbeat(tmp_path):
    d = make_desk(tmp_path, n=20)
    d.run(realtime=False)
    path = d.save()
    main(["heartbeat", "--state", path])  # fresh: passes
    st = json.load(open(path))
    st["server_time"] -= 3600
    json.dump(st, open(path, "w"))
    with pytest.raises(SystemExit):
        main(["heartbeat", "--state", path, "--max-age", "45"])
