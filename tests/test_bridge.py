"""The Windows bridge, tested with a fake MetaTrader5 module: a broker with
odd symbol names (EURUSD.m, GOLD) and a clock at UTC+3."""
import importlib
import json
import os
import sys
import time
import types

import pytest

BROKER_OFFSET = 3 * 3600


class Obj(types.SimpleNamespace):
    pass


def fake_mt5(now):
    names = ["EURUSD.m", "GBPUSD.m", "AUDUSD.m", "USDJPY.m", "USDCAD.m", "USDCHF.m", "NZDUSD.m", "GOLD", "EURUSD.m.x"]
    m = types.ModuleType("MetaTrader5")
    m.TIMEFRAME_M5 = 5
    m.ACCOUNT_TRADE_MODE_DEMO = 0
    m.initialize = lambda *a: True
    m.last_error = lambda: (0, "ok")
    m.terminal_info = lambda: Obj()
    m.symbols_get = lambda: [Obj(name=n) for n in names]
    m.symbol_select = lambda n, on: n in names
    m.account_info = lambda: Obj(company="Octa", server="Octa-Demo", trade_mode=0)
    m.symbol_info = lambda n: Obj(point=0.01 if n == "GOLD" else (0.001 if "JPY" in n else 0.00001))
    broker_now = now + BROKER_OFFSET
    m.symbol_info_tick = lambda n: Obj(time_msc=int((broker_now - 1) * 1000), bid=1.1, ask=1.1002)

    def rates(n, tf, pos, count):
        last_open = (broker_now - broker_now % 300) - 300  # last closed bar (pos=1)
        return [{"time": last_open - 300 * i, "open": 1.1, "high": 1.11, "low": 1.09, "close": 1.1,
                 "tick_volume": 50, "spread": 8} for i in reversed(range(count))]
    m.copy_rates_from_pos = rates
    return m


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake_mt5(now))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
    mod = importlib.import_module("mt5_bridge")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "OFFSET_FILE", str(tmp_path / "off.json"))
    yield mod, now
    sys.path.pop(0)


def test_symbol_names_resolved(bridge):
    mod, _ = bridge
    got = mod.resolve_symbols({})
    assert got["EURUSD"] == "EURUSD.m" and got["XAUUSD"] == "GOLD" and len(got) == 8
    assert mod.resolve_symbols({"symbols": {"EURUSD": "EURUSD.m.x"}})["EURUSD"] == "EURUSD.m.x"


def test_broker_clock_converted_to_utc(bridge, tmp_path):
    mod, now = bridge
    clock = mod.Clock()
    p = mod.build_payload(mod.resolve_symbols({}), clock, need={"EURUSD"}, history=50)
    assert clock.offset_s == BROKER_OFFSET
    eur = p["symbols"]["EURUSD"]
    assert abs(eur["quote"]["t"] - now) < 5
    assert len(eur["bars"]) == 50 and len(p["symbols"]["GBPUSD"]["bars"]) == 3
    last_close = eur["bars"][-1][0] + 300
    assert last_close <= now and now - last_close < 300   # newest bar is closed, and recent
    assert p["account"] == "demo" and p["broker"] == "Octa"
    assert json.load(open(tmp_path / "off.json"))["offset_s"] == BROKER_OFFSET


def test_bridge_payload_accepted_by_server(bridge):
    from jevsys.market import MarketStore
    mod, now = bridge
    p = mod.build_payload(mod.resolve_symbols({}), mod.Clock(), need=set(), history=3)
    m = MarketStore()
    res = m.ingest(json.loads(json.dumps(p)), time.time())
    assert res["rejected"] == []
    assert len(m.data["XAUUSD"].bars) == 3 and m.data["XAUUSD"].broker_symbol == "GOLD"
