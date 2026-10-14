"""Stage 5 (fallback ladder, alerts, leverage) and the Step 8 tooling."""
import json
import time

import pytest

from jevsys import report as step8
from jevsys.alerts import Alerter
from jevsys.desk import Desk, market_open
from jevsys.market import MarketStore
from jevsys.paper import PaperBook
from jevsys.risk import RiskEngine
from jevsys.rules import decide_rules
from jevsys.settings import DEFAULTS, Settings
from jevsys.sim_fx import SimBridge
from jevsys.types import Quote
from tests.test_trading import ScriptedJev, answers

F = DEFAULTS["fallback"]
WED = 1_760_544_000.0  # Wednesday 16:00 UTC


# ---- code-only rules -----------------------------------------------------------
def snap(dist=0.8, slope=0.15, d100=1.0, rsi=60, svn=1.0):
    return {"ema20_dist_atr": dist, "ema20_slope_atr": slope, "ema100_dist_atr": d100, "rsi14": rsi, "spread_vs_norm": svn}


@pytest.mark.parametrize("kw,pos,kind,side", [
    ({}, None, "ENTER", "long"),
    ({"dist": -0.8, "slope": -0.15, "d100": -1, "rsi": 40}, None, "ENTER", "short"),
    ({"rsi": 75}, None, "STAND_ASIDE", None),            # stretched: no chase
    ({"d100": -0.2}, None, "STAND_ASIDE", None),         # against the bigger trend
    ({"dist": 0.3}, None, "STAND_ASIDE", None),          # not far enough from the EMA
    ({"svn": 1.8}, None, "STAND_ASIDE", None),           # spread too wide
    ({"dist": -0.1}, "long", "EXIT", None),              # back under EMA 20
    ({"rsi": 80}, "long", "EXIT", None),
    ({}, "long", "HOLD", None),
    ({"dist": 0.1, "rsi": 40}, "short", "EXIT", None),
])
def test_rules(kw, pos, kind, side):
    a = decide_rules(snap(**kw), pos, F)
    assert (a.kind, a.side) == (kind, side), a.why


def test_rules_need_history():
    assert decide_rules({"ema20_dist_atr": None}, None, F).kind == "STAND_ASIDE"


# ---- leverage ----------------------------------------------------------------------
def test_leverage_sizes_down_then_vetoes(tmp_path):
    s = Settings(str(tmp_path / "none.json"))
    r = RiskEngine(s)
    q = Quote("EURUSD", WED, 1.1, 1.1001)
    full = r.plan("EURUSD", "long", q, 3.0, 10_000)
    cut = r.plan("EURUSD", "long", q, 3.0, 10_000, notional_open=95_000)
    assert full["lots"] > cut["lots"] > 0 and cut["leverage_cut"] and cut["leverage_after"] <= 10
    none = r.plan("EURUSD", "long", q, 3.0, 10_000, notional_open=100_000)
    assert none["lots"] == 0
    v = r.entry_vetoes("EURUSD", "long", {}, q, WED, PaperBook(str(tmp_path / "b.json")), {}, 0, none)
    assert any("max leverage" in x for x in v)
    assert r.plan("EURUSD", "long", q, 3.0, 10_000, risk_mult=0.5)["lots"] == pytest.approx(full["lots"] / 2, abs=0.01)


# ---- alerts --------------------------------------------------------------------------
def test_alerter_off_without_token_and_cooldown(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert not Alerter().enabled
    got = []
    a = Alerter(send_fn=got.append)
    assert a.send("x", key="k", cooldown_s=60) and not a.send("x again", key="k", cooldown_s=60)
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert got == ["x"]


# ---- the desk under failure ------------------------------------------------------------
class FlakyJev(ScriptedJev):
    """Answers until `down` is set, then fails like a network outage (or answers late)."""

    def __init__(self, script):
        super().__init__(script)
        self.down = None

    def ask_all(self, snaps):
        if not self.down:
            self.consec_errors = 0
            return super().ask_all(snaps)
        self.consec_errors += 1
        return {s: {"error": "connection refused" if self.down == "network" else "no answer within 20s",
                    "kind": self.down, "ms": 20_000} for s in snaps}


def desk(tmp_path, jev, sent=None, **cfg_over):
    now = time.time()
    sim = SimBridge(seed=21, start_ts=now - 3 * 86_400)
    m = MarketStore()
    m.ingest(sim.step_to(now), now)
    cfg = json.loads(open("config/desk.json").read())
    cfg["risk"].update({"friday_no_entries_utc": "23:59", "friday_flat_utc": "23:59"})
    for sec, kv in cfg_over.items():
        cfg[sec].update(kv)
    p = tmp_path / "desk.json"
    p.write_text(json.dumps(cfg))
    al = Alerter(send_fn=(sent.append if sent is not None else (lambda t: None)))
    d = Desk(m, state_path=str(tmp_path / "state" / "jev_desk.json"), log_dir=str(tmp_path / "logs"),
             jev=jev, settings_path=str(p), alerter=al)
    return d, m, sim, now


def weekday_or_skip(now):
    if time.gmtime(now).tm_wday in (5, 6):
        pytest.skip("weekend: entries blocked by design")


def test_jev_down_switches_to_rules_then_back(tmp_path):
    jev = FlakyJev(lambda s: answers("neutral"))
    d, m, sim, now = desk(tmp_path, jev, fallback={"min_ema20_dist_atr": 0.0, "min_ema20_slope_atr": 0.0,
                                                    "rsi_long_min": 0, "rsi_long_max": 100, "rsi_short_min": 0,
                                                    "rsi_short_max": 100, "max_spread_vs_normal": 10})
    weekday_or_skip(now)
    jev.down = "network"
    out1 = d.decide_round(now)                     # 1 failed round: hold (one blip isn't an outage)
    assert all(r["action"]["kind"] == "WAIT" for r in out1.values()) and not d.fallback_on
    out2 = d.decide_round(now + 1)                 # 2nd failure: code-only rules take over
    assert d.fallback_on and d.state()["ladder"]["current"] == "fallback"
    assert any(r["action"].get("source") == "rules" for r in out2.values())
    rule_pos = [p for p in d.book.positions.values()]
    assert rule_pos and all("[code-only rules]" in p.reason for p in rule_pos)
    full = RiskEngine(d.settings).plan(rule_pos[0].sym, rule_pos[0].side,
                                       d.quotes(now + 1)[rule_pos[0].sym], out2[rule_pos[0].sym]["snap"]["atr_pips"], 10_000)
    assert rule_pos[0].lots <= full["lots"] / 2 + 0.01      # half size
    assert any("Switched to code-only rules" in t for _, t in d.alerter.history)
    jev.down = None
    d.decide_round(now + 2)
    assert not d.fallback_on and any("answering again" in t for _, t in d.alerter.history)


def test_late_answers_hold_not_fallback(tmp_path):
    jev = FlakyJev(lambda s: answers("up", dconf=0.9))
    d, m, sim, now = desk(tmp_path, jev)
    jev.down = "late"
    for i in range(3):
        out = d.decide_round(now + i)
    assert all(r["action"]["kind"] == "WAIT" and "late" in r["action"]["why"] for r in out.values())
    assert not d.book.positions and d.state()["ladder"]["current"] == "late"


def test_kill_switch_sends_alert(tmp_path):
    jev = ScriptedJev(lambda s: answers("up", dconf=0.9) if s == "EURUSD" else answers("neutral"))
    d, m, sim, now = desk(tmp_path, jev, risk={"max_daily_loss_pct": 0.1, "stop_atr_mult": 20})
    weekday_or_skip(now)
    d.decide_round(now)
    p = d.book.positions["EURUSD"]
    t = now + 10
    m.ingest({"symbols": {"EURUSD": {"quote": {"t": t, "bid": p.entry - 0.0050, "ask": p.entry - 0.0049}}}}, t)
    d.monitor_tick(t)
    assert d.book.halted and any("KILL SWITCH" in txt for _, txt in d.alerter.history)


def test_bridge_watchdog_alerts_when_market_open(tmp_path):
    d, m, sim, now = desk(tmp_path, ScriptedJev(lambda s: answers("neutral")))
    m.last_ingest = WED - 600                     # 10 min of silence on a Wednesday afternoon
    d._watch_bridge(WED, d.settings.alerts)
    assert any("no prices from the MT5 bridge" in t for _, t in d.alerter.history)
    m.last_ingest = WED + 5
    d._watch_bridge(WED + 10, d.settings.alerts)
    assert any("sending prices again" in t for _, t in d.alerter.history)
    sat = WED + 3 * 86_400                        # Saturday: silence is normal
    (tmp_path / "b").mkdir()
    d2, m2, _, _ = desk(tmp_path / "b", ScriptedJev(lambda s: answers("neutral")))
    m2.last_ingest = sat - 3600
    d2._watch_bridge(sat, d2.settings.alerts)
    assert not any("MT5 bridge" in t for _, t in d2.alerter.history)


def test_market_hours():
    assert market_open(WED) and not market_open(WED + 3 * 86_400)            # Wed yes, Sat no
    fri_2059, fri_2101 = WED + 2 * 86_400 + 4 * 3600 + 59 * 60, WED + 2 * 86_400 + 5 * 3600 + 60
    assert market_open(fri_2059) and not market_open(fri_2101)
    sun_2101 = WED + 4 * 86_400 + 5 * 3600 + 60
    assert market_open(sun_2101)


def test_daily_summary_once_per_weekday(tmp_path):
    d, m, sim, now = desk(tmp_path, ScriptedJev(lambda s: answers("neutral")))
    at = WED - 16 * 3600 + 21 * 3600 + 6 * 60     # Wednesday 21:06 UTC
    d._daily_summary(at - 120, d.settings.alerts)  # 21:04: too early
    d._daily_summary(at, d.settings.alerts)
    d._daily_summary(at + 600, d.settings.alerts)  # already sent today
    assert sum("Jev Desk daily" in t for _, t in d.alerter.history) == 1


def test_shadow_accounts_trade_the_same_data(tmp_path):
    # Jev keeps saying "up" but only at 55%: the gated live account stands aside, the no-gate shadow trades
    jev = ScriptedJev(lambda s: answers("up", dconf=0.55, qconf=0.3) if s == "GBPUSD" else answers("neutral"))
    d, m, sim, now = desk(tmp_path, jev)
    weekday_or_skip(now)
    out = d.decide_round(now)
    assert "GBPUSD" not in d.book.positions and out["GBPUSD"]["action"]["kind"] == "STAND_ASIDE"
    assert "GBPUSD" in d.shadows["nogate"].book.positions and out["GBPUSD"]["shadow"]["nogate"] == "ENTER"
    assert "rules" in out["GBPUSD"]["shadow"]
    st = d.state()
    assert set(st["shadows"]) == {"rules", "nogate"} and st["shadows"]["nogate"]["open"] >= 1
    line = json.loads(open(next((tmp_path / "logs").glob("snapshots-*.jsonl"))).readline())
    assert "shadow" in line


# ---- Step 8 report ----------------------------------------------------------------------
def test_calibration_math():
    rows = []
    t0 = WED
    # EURUSD rises 1 pip every hour. Jev says "up" at 80% on 4 of 5 calls and "down" at 80% once:
    # 80% predicted, 80% happened -> calibrated.
    for i in range(10):
        j = {"direction": {"value": "down" if i == 3 else "up", "p": 0.8}, "move_continues": {"p": 0.6},
             "costs_abnormal": {"p": 0.2}}
        rows.append({"ts": t0 + i * 3600, "sym": "EURUSD", "fresh": True, "jev": j,
                     "snap": {"mid": 1.1 + i * 0.0001, "ret_1h_bps": 1.0, "spread_vs_norm": 1.0}})
    cal = step8.calibration(rows)
    d = cal["direction"]
    assert d["n"] == 9 and d["bins"][0]["predicted"] == 0.8 and d["bins"][0]["actual"] == pytest.approx(8 / 9, abs=0.001)
    assert cal["move_continues"]["bins"][0]["actual"] == 1.0           # every hour continued up
    assert cal["costs_abnormal"]["bins"][0]["actual"] == 0.0


def test_report_waits_for_enough_data(tmp_path):
    d, m, sim, now = desk(tmp_path, ScriptedJev(lambda s: answers("neutral")))
    d.decide_round(now)
    r = d.refresh_report(now)
    assert not r["ready_for_real_money"] and {c["status"] for c in r["checks"]} == {"wait"}
    assert set(r["accounts"]) == {"jev", "rules", "nogate"}
    text = step8.to_text(r)
    assert "NOT ready for real money" in text and "Code-only rules" in text


def test_drill_command_passes():
    from jevsys.drill import run_drill
    results = run_drill(verbose=False)
    assert results and all(ok for _, ok, _ in results), results
