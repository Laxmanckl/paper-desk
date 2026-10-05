"""Telegram alerts: trade opened / closed, a daily summary, and trouble.

Turned on by two environment variables (the server keeps them in
/etc/paper-desk.env, written by server/telegram.sh):

    TELEGRAM_BOT_TOKEN   from @BotFather
    TELEGRAM_CHAT_ID     your chat with the bot

Alerts never stop the bot: if Telegram is unreachable, the message is
dropped and trading carries on.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from . import fx

ERROR_ALERT_AFTER_MIN = 30        # only alert if data problems last this long
DAILY_SUMMARY_UTC_HOUR = 21       # after the New York close (forex day ends ~21:00 UTC)


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    body = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    try:
        req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                      headers={"content-type": "application/json"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception as e:                           # never let an alert stop trading
        print(f"  telegram alert failed: {type(e).__name__}: {e}")
        return False


def _money(v: float, sign: bool = False) -> str:
    s = f"${abs(v):,.2f}"
    return ("-" if v < 0 else ("+" if sign and v > 0 else "")) + s


def _name(sym: str) -> str:
    return {"XAUUSD": "Gold (XAU/USD)", "EURUSD": "EUR/USD"}.get(sym, sym)


def trade_messages(acct, before_closed: int, before_open: set) -> list[str]:
    """Messages for trades opened or closed since the counts given."""
    out = []
    for t in acct.closed[before_closed:]:
        d = fx.INSTRUMENTS[t["symbol"]]["digits"]
        why = {"stop": "stop-loss hit", "target": "target hit", "time": "held 20 days",
               "reverse": "signal flipped", "end": "closed"}.get(t["reason"], t["reason"])
        out.append(f"{'✅' if t['pnl'] > 0 else '🔻'} CLOSED {t['side']} {_name(t['symbol'])}\n"
                   f"{t['entry']:,.{d}f} → {t['exit']:,.{d}f} ({why})\n"
                   f"P&L {_money(t['pnl'], True)} · {t['pips']:+.1f} pips\n"
                   f"Account {_money(acct.equity())} (paper)")
    for sym, p in acct.positions.items():
        if sym in before_open:
            continue
        d = fx.INSTRUMENTS[sym]["digits"]
        out.append(f"🟢 OPENED {p['side']} {_name(sym)} at {p['entry']:,.{d}f}\n"
                   f"Stop {p['stop']:,.{d}f} · Target {p['target']:,.{d}f}\n"
                   f"Signal p {p['signal_prob']:.0%}, risk 1% of account (paper)")
    return out


def trouble_message(acct, events: list, now: datetime) -> str | None:
    """Alert once if data errors / failed checks persist, and once when they clear."""
    s = acct.alerts_sent
    bad = [e for e in events if "data error" in e or e.startswith("BOT: check failed")]
    if bad:
        since = s.setdefault("trouble_since", now.isoformat(timespec="seconds"))
        mins = (now - datetime.fromisoformat(since)).total_seconds() / 60
        if mins >= ERROR_ALERT_AFTER_MIN and not s.get("trouble_alerted"):
            s["trouble_alerted"] = True
            return (f"⚠️ Paper bot: problem for {mins:.0f} min\n{bad[-1][:300]}\n"
                    "It keeps retrying every minute. Trades are held, nothing is lost.")
        return None
    if s.pop("trouble_since", None) and s.pop("trouble_alerted", None):
        return "👍 Paper bot: back to normal, prices are coming through again."
    return None


def daily_message(acct, now: datetime) -> str | None:
    """One summary per weekday, after the forex day ends."""
    day = now.strftime("%Y-%m-%d")
    if now.weekday() > 4 or now.hour < DAILY_SUMMARY_UTC_HOUR or acct.alerts_sent.get("daily") == day:
        return None
    acct.alerts_sent["daily"] = day
    eq = acct.equity()
    day_start = next((p[1] for p in reversed(acct.equity_log) if p[0][:10] < day), acct.start_equity)
    closed_today = [t for t in acct.closed if t.get("closed_at", "")[:10] == day]
    lines = [f"📊 Paper desk · {now:%a %d %b}",
             f"Account {_money(eq)} ({_money(eq - day_start, True)} today, "
             f"{(eq / acct.start_equity - 1) * 100:+.2f}% overall)"]
    if closed_today:
        lines.append(f"Closed today: {len(closed_today)} trade(s), "
                     f"{_money(sum(t['pnl'] for t in closed_today), True)}")
    for sym, p in acct.positions.items():
        lp = (acct.last_price.get(sym) or {}).get("price")
        if lp:
            half = fx.INSTRUMENTS[sym]["spread"] / 2
            fill = lp - half if p["side"] == "BUY" else lp + half
            pnl = (fill - p["entry"]) * (1 if p["side"] == "BUY" else -1) * p["units"]
            lines.append(f"Open {p['side']} {_name(sym)}: {_money(pnl, True)}")
    if not acct.positions:
        lines.append("No open trades.")
    lines.append("laxmanckl.github.io/paper-desk")
    return "\n".join(lines)


def notify(acct, events: list, before_closed: int, before_open: set,
           now: datetime | None = None, sender=send) -> list[str]:
    """Send whatever this check deserves. Returns the messages (for tests)."""
    now = now or datetime.now(timezone.utc)
    msgs = trade_messages(acct, before_closed, before_open)
    t = trouble_message(acct, events, now)
    if t:
        msgs.append(t)
    d = daily_message(acct, now)
    if d:
        msgs.append(d)
    for m in msgs:
        sender(m)
    return msgs
