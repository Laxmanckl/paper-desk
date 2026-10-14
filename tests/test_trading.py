"""Stages 3-4: policy, risk engine, paper book."""
import json
import os
import time

import pytest

from jevsys.desk import Desk
from jevsys.jev import JevLayer
from jevsys.market import MarketStore
from jevsys.paper import PaperBook
from jevsys.policy import decide, tightened_stop
from jevsys.risk import RiskEngine, size_lots, usd_bet
from jevsys.settings import DEFAULTS, Settings
from jevsys.sim_fx import SimBridge
from jevsys.types import Quote

P = DEFAULTS["policy"]
WED = 1_760_544_000.0  # Wednesday 2025-10-15 16:00 UTC (not Friday, market open)


def answers(direction="up", dconf=0.8, regime="trending", rconf=0.7, quality=2, qconf=0.6,
            cont=0.7, costs=0.2, urgency=0, uconf=0.6):
    labels = ["Do not enter", "Marginal", "Good", "Excellent"]
    ulabels = ["No urgency", "Mild", "Tighten the stop", "Close now"]
    return {"regime": {"value": regime, "p": rconf}, "direction": {"value": direction, "p": dconf},
            "move_continues": {"p": cont}, "costs_abnormal": {"p": costs},
            "entry_quality": {"level": quality, "label": labels[quality], "p": qconf, "score": quality},
            "exit_urgency": {"level": urgency, "label": ulabels[urgency], "p": uconf, "score": urgency}}


# ---- sizing ------------------------------------------------------------------
def test_size_lots():
    assert size_lots(10_000, 0.5, 20, 10.0, 5) == 0.25          # $50 risk / (20 pips x $10)
    assert size_lots(10_000, 0.5, 20, 10.0, 0.1) == 0.1          # capped
    assert size_lots(10_000, 0.5, 260, 10.0, 5) == 0.01          # $50 / $2,600 = 0.019 -> floor 0.01
    assert size_lots(10_000, 0.5, 600, 10.0, 5) == 0.0           # too wide even at 0.01


def test_usd_direction():
    assert usd_bet("EURUSD", "short") == usd_bet("USDJPY", "long") == 1   # both win if USD strengthens
    assert usd_bet("XAUUSD", "long") == -1


# ---- policy ------------------------------------------------------------------
@pytest.mark.parametrize("kw,kind,side", [
    ({}, "ENTER", "long"),
    ({"direction": "down"}, "ENTER", "short"),
    ({"costs": 0.7}, "STAND_ASIDE", None),
    ({"regime": "volatile", "rconf": 0.65}, "STAND_ASIDE", None),
    ({"regime": "volatile", "rconf": 0.5}, "ENTER", "long"),          # not sure it's volatile: allowed
    ({"regime": "quiet"}, "STAND_ASIDE", None),
    ({"direction": "neutral"}, "STAND_ASIDE", None),
    ({"dconf": 0.6}, "STAND_ASIDE", None),                             # below the 0.65 entry bar
    ({"quality": 1}, "STAND_ASIDE", None),
    ({"qconf": 0.3}, "STAND_ASIDE", None),
    ({"cont": 0.4}, "STAND_ASIDE", None),
])
def test_policy_flat(kw, kind, side):
    a = decide(answers(**kw), None, P)
    assert (a.kind, a.side) == (kind, side), a.why


@pytest.mark.parametrize("pos,kw,kind", [
    ("long", {"urgency": 3}, "EXIT"),
    ("long", {"urgency": 3, "uconf": 0.3}, "HOLD"),                  # not confident enough
    ("long", {"direction": "down", "dconf": 0.62}, "EXIT"),          # exit bar (0.6) is lower than entry bar (0.65)
    ("short", {"direction": "up", "dconf": 0.62}, "EXIT"),
    ("long", {"urgency": 2}, "TIGHTEN"),
    ("long", {}, "HOLD"),
])
def test_policy_in_position(pos, kw, kind):
    assert decide(answers(**kw), pos, P).kind == kind


def test_tightened_stop():
    assert tightened_stop("long", 1.1000, 1.0980, 1.0990, 5) == 1.0985        # losing: halfway
    assert tightened_stop("long", 1.1000, 1.0980, 1.1010, 5) == 1.1000        # winning: at least breakeven
    assert tightened_stop("short", 1.1000, 1.1020, 1.0990, 5) == 1.1000


# ---- paper book ----------------------------------------------------------------
def q(sym, bid, ask, t=WED):
    return Quote(sym, t, bid, ask)


def test_fills_pay_the_spread(tmp_path):
    b = PaperBook(str(tmp_path / "b.json"))
    p = b.open("EURUSD", "long", 0.10, q("EURUSD", 1.10000, 1.10010), 1.0980, 1.1030, WED, "test")
    assert p.entry == 1.10010                                      # bought at the ask
    tr = b.close("EURUSD", q("EURUSD", 1.10110, 1.10120), WED + 600, "test")
    assert tr["exit"] == 1.10110 and tr["pips"] == 10.0            # sold at the bid
    assert tr["pnl"] == pytest.approx(10.0)                        # 10 pips x $10 x 0.1 lot
    assert b.equity({}) == pytest.approx(10_010.0)


def test_usdjpy_pnl_converted(tmp_path):
    b = PaperBook(str(tmp_path / "b.json"))
    b.open("USDJPY", "short", 1.0, q("USDJPY", 150.00, 150.02), 150.5, 149.0, WED, "t")
    tr = b.close("USDJPY", q("USDJPY", 149.48, 149.50), WED, "t")
    assert tr["pips"] == 50.0
    assert tr["pnl"] == pytest.approx(50 * 1000 / 149.50, abs=0.01)  # 50 pips x (1000 JPY / price) per lot


def test_stop_only_tightens_and_book_survives_restart(tmp_path):
    path = str(tmp_path / "b.json")
    b = PaperBook(path)
    b.open("XAUUSD", "long", 0.05, q("XAUUSD", 4000.0, 4000.4), 3990.0, 4015.0, WED, "t")
    assert not b.move_stop("XAUUSD", 3985.0) and b.move_stop("XAUUSD", 3995.0)
    b2 = PaperBook(path)
    assert b2.positions["XAUUSD"].stop == 3995.0 and b2.positions["XAUUSD"].initial_stop == 3990.0


# ---- risk engine ----------------------------------------------------------------
def settings(tmp_path, **risk):
    p = tmp_path / "desk.json"
    p.write_text(json.dumps({"policy": {}, "risk": risk}))
    return Settings(str(p))


def test_plan_sets_stop_target_size(tmp_path):
    r = RiskEngine(settings(tmp_path))
    plan = r.plan("EURUSD", "long", q("EURUSD", 1.1000, 1.1001), atr_pips=8.0, equity=10_000)
    assert plan["stop_pips"] == 20.0 and plan["stop"] == 1.0981 and plan["target"] == 1.1031
    assert plan["lots"] == 0.25
    short = r.plan("EURUSD", "short", q("EURUSD", 1.1000, 1.1001), 8.0, 10_000)
    assert short["stop"] == 1.1020 and short["target"] == 1.0970


def test_entry_vetoes(tmp_path):
    r = RiskEngine(settings(tmp_path, max_open_positions=2, max_same_usd_direction=1))
    b = PaperBook(str(tmp_path / "b.json"))
    snap = {"spread_vs_norm": 1.0}
    good_q = q("GBPUSD", 1.3, 1.3002)
    plan = r.plan("GBPUSD", "short", good_q, 8, 10_000)
    assert r.entry_vetoes("GBPUSD", "short", snap, good_q, WED, b, {"ms": 300}, 0, plan) == []
    b.open("EURUSD", "short", 0.1, q("EURUSD", 1.1, 1.1001), 1.102, 1.097, WED, "t")   # already short EUR = long USD
    v = r.entry_vetoes("GBPUSD", "short", snap, good_q, WED, b, {"ms": 300}, 0, plan)
    assert any("same way on the US dollar" in x for x in v)
    v = r.entry_vetoes("GBPUSD", "long", {"spread_vs_norm": 2.5}, Quote("GBPUSD", WED - 120, 1.3, 1.3002), WED, b,
                       {"ms": 9000}, 3, plan)
    text = " | ".join(v)
    for part in ["2.5x normal", "too old", "Jev failed 3 rounds", "9000 ms"]:
        assert part in text, part
    fri = WED + 2 * 86_400 + 3 * 3600            # Friday 19:00 UTC
    assert any("weekend" in x for x in r.entry_vetoes("GBPUSD", "long", snap, Quote("GBPUSD", fri, 1.3, 1.3002), fri, b, {}, 0, plan))
    wide = r.plan("GBPUSD", "long", good_q, atr_pips=500, equity=10_000)
    assert any("too wide" in x for x in r.entry_vetoes("GBPUSD", "long", snap, good_q, WED, PaperBook(str(tmp_path / "c.json")), {}, 0, wide))


def test_exit_reasons(tmp_path):
    r = RiskEngine(settings(tmp_path, max_hold_hours=6))
    b = PaperBook(str(tmp_path / "b.json"))
    p = b.open("EURUSD", "long", 0.1, q("EURUSD", 1.1, 1.1001), 1.0980, 1.1030, WED, "t")
    assert r.exit_reason(p, q("EURUSD", 1.0999, 1.1000), WED) is None
    assert r.exit_reason(p, q("EURUSD", 1.0975, 1.0976), WED) == "stop"           # gapped through: fills at 1.0975
    assert r.exit_reason(p, q("EURUSD", 1.1031, 1.1032), WED) == "target"
    assert r.exit_reason(p, q("EURUSD", 1.1, 1.1001), WED + 6 * 3600) == "max hold 6 h"
    fri_late = WED + 2 * 86_400 + 4.5 * 3600 + 60   # Friday 20:31 UTC
    p2 = b.open("GBPUSD", "long", 0.1, q("GBPUSD", 1.3, 1.3001, fri_late - 1800), 1.298, 1.303, fri_late - 1800, "t")
    assert r.exit_reason(p2, q("GBPUSD", 1.3, 1.3001, fri_late), fri_late) == "flat before the weekend"


# ---- desk end to end ------------------------------------------------------------
class ScriptedJev(JevLayer):
    """Stands in for TypeSafe: returns the answers a test scripts per instrument."""

    def __init__(self, script):
        self.script, self.model, self.reason = script, "jev-1.13.0", ""
        self.client = object()
        self.calls = self.errors = self.late = self.consec_errors = self.tokens_in = 0
        self.latencies, self.last_model_seen, self.last_error = [], None, None
        import threading
        self.lock = threading.Lock()

    def ask_all(self, snaps):
        out = {s: {"answers": self.script(s), "model": self.model, "ms": 210, "tokens_in": 660, "pinned_ok": True}
               for s in snaps}
        self.calls += len(out)
        return out


def live_desk(tmp_path, script, **risk):
    now = time.time()
    sim = SimBridge(seed=21, start_ts=now - 3 * 86_400)
    m = MarketStore()
    m.ingest(sim.step_to(now), now)
    cfg = tmp_path / "desk.json"
    cfg.write_text(json.dumps({"policy": {}, "risk": {"friday_no_entries_utc": "23:59", "friday_flat_utc": "23:59", **risk}}))
    d = Desk(m, state_path=str(tmp_path / "state" / "jev_desk.json"), log_dir=str(tmp_path / "logs"),
             jev=ScriptedJev(script), settings_path=str(cfg))
    return d, m, sim, now


def push_quote(m, sym, bid, ask, t):
    m.ingest({"symbols": {sym: {"quote": {"t": t, "bid": bid, "ask": ask}}}}, t)


def test_round_opens_trades_within_limits(tmp_path):
    d, m, sim, now = live_desk(tmp_path, lambda s: answers("up" if s.endswith("USD") else "down"))
    if time.gmtime(now).tm_wday in (5, 6):
        pytest.skip("weekend: entries are blocked by design")
    out = d.decide_round(now)
    opened = [s for s, r in out.items() if r["action"]["kind"] == "ENTER" or r["action"].get("position_id")]
    vetoed = [s for s, r in out.items() if r["action"]["kind"] == "VETOED"]
    # every scripted call is "dollar weaker" (long XXX/USD, short USD/XXX): only 2 may run at once
    assert len(d.book.positions) == 2 and len(vetoed) == 6
    for p in d.book.positions.values():
        assert p.stop != p.entry and p.target != p.entry and p.lots >= 0.01
        assert p.risk_usd <= 10_000 * 0.005 + 0.01                          # never more than 0.5% to the stop
        assert p.jev and p.settings == d.settings.version
    lines = [json.loads(x) for x in open(next((tmp_path / "logs").glob("snapshots-*.jsonl")))]
    assert {x["action"]["kind"] for x in lines} == {"ENTER", "VETOED"}
    assert all(x["settings"] == d.settings.version and x["model"] == "jev-1.13.0" for x in lines)
    st = d.state(full=True)
    assert st["ladder"]["current"] == "normal" and len(st["book"]["positions"]) == 2


def test_monitor_stops_out_and_records_trade(tmp_path):
    d, m, sim, now = live_desk(tmp_path, lambda s: answers("up", dconf=0.9) if s == "EURUSD" else answers("neutral"))
    if time.gmtime(now).tm_wday in (5, 6):
        pytest.skip("weekend")
    d.decide_round(now)
    p = d.book.positions["EURUSD"]
    t = now + 30
    push_quote(m, "EURUSD", p.stop - 0.0003, p.stop - 0.0002, t)    # price gaps through the stop
    d.monitor_tick(t)
    assert "EURUSD" not in d.book.positions
    tr = d.book.trades[-1]
    assert tr["why"] == "stop" and tr["exit"] == round(p.stop - 0.0003, 5) and tr["pnl"] < 0
    assert tr["r"] < -1                                              # slipped past the stop: worse than -1R
    assert any(e[1].startswith("CLOSE EURUSD") for e in d.state()["log"])


def test_exit_signal_closes_position(tmp_path):
    calls = {"n": 0}

    def script(s):
        if s != "GBPUSD":
            return answers("neutral")
        calls["n"] += 1
        return answers("up", dconf=0.9) if calls["n"] == 1 else answers("up", urgency=3, uconf=0.8)

    d, m, sim, now = live_desk(tmp_path, script)
    if time.gmtime(now).tm_wday in (5, 6):
        pytest.skip("weekend")
    d.decide_round(now)
    assert "GBPUSD" in d.book.positions
    out = d.decide_round(now + 1)
    assert out["GBPUSD"]["action"]["kind"] == "EXIT" and "GBPUSD" not in d.book.positions


def test_daily_kill_switch_closes_everything(tmp_path):
    d, m, sim, now = live_desk(tmp_path, lambda s: answers("up", dconf=0.9) if s in ("EURUSD", "USDJPY") else answers("neutral"),
                               max_daily_loss_pct=0.2, risk_per_trade_pct=1.0, stop_atr_mult=20)
    if time.gmtime(now).tm_wday in (5, 6):
        pytest.skip("weekend")
    d.decide_round(now)
    assert d.book.positions
    t = now + 20
    for sym, p in list(d.book.positions.items()):
        move = -40 * (0.01 if sym == "USDJPY" else 0.0001) * p.dir   # 40 pips against each position
        push_quote(m, sym, p.entry + move, p.entry + move + 0.0002, t)
    d.monitor_tick(t)
    assert not d.book.positions and d.book.halted["kind"] == "daily"
    out = d.decide_round(t + 1)
    assert all(r["action"]["kind"] in ("VETOED", "STAND_ASIDE", "WAIT") for r in out.values())
    assert d.state()["ladder"]["current"] == "kill"


def test_drawdown_kill_needs_manual_reset(tmp_path):
    d, m, sim, now = live_desk(tmp_path, lambda s: answers("neutral"))
    d.book.halt("drawdown", "test", now)
    d.decide_round(now)
    assert d.book.halted
    open(d.reset_flag, "w").close()
    d.monitor_tick(now + 2)
    assert d.book.halted is None and not os.path.exists(d.reset_flag)


def test_settings_edit_hot_reload_and_bad_edit_rejected(tmp_path):
    path = tmp_path / "desk.json"
    path.write_text(json.dumps({"policy": {"enter_direction_conf": 0.7}, "risk": {}}))
    s = Settings(str(path))
    v1 = s.version
    assert s.policy["enter_direction_conf"] == 0.7
    time.sleep(0.01)
    path.write_text(json.dumps({"policy": {"enter_direction_conf": 7}, "risk": {}}))       # out of range
    os.utime(path, (time.time() + 1, time.time() + 1))
    assert "rejected" in s.reload() and s.policy["enter_direction_conf"] == 0.7 and s.version == v1
    path.write_text(json.dumps({"policy": {"enter_direction_conf": 0.75}, "risk": {}}))
    os.utime(path, (time.time() + 2, time.time() + 2))
    assert "changed" in s.reload() and s.policy["enter_direction_conf"] == 0.75


def test_repo_config_is_valid():
    s = Settings("config/desk.json")
    assert s.error is None and s.version != "defaults"
