"""Terminal output. A LIVE DECISIONS table and a single decision, unpacked.

Every block carries a label for what is real and what is not: the data, the
decision engine, the execution mode. That labelling is the point. A reader
should never have to guess whether a number was read, computed, or made up.
"""

from __future__ import annotations

from .execution.paper import Book
from .fx import fmt_price, INSTRUMENTS

_W = 66
_ARROW = {"BUY": "up", "SELL": "dn", "HOLD": "--", "AVOID": "x "}


def rule(ch="-"):
    return ch * _W


def header(engine: str, n: int, data: str = "") -> str:
    dec = "JEV (live)" if engine == "jev" else "offline (deterministic)"
    data = data or f"sim ({n} assets)"
    return "\n".join([
        rule("="),
        "  JEV-BOT  ·  market decisions by JEV",
        rule("-"),
        f"  DATA        {data}",
        f"  DECISION    {dec}",
        f"  EXECUTION   paper",
        f"  PERFORMANCE simulated",
        rule("="),
    ])


def decisions_table(records) -> str:
    lines = ["  ASSET   CLASS    JEV      PROB   CONF   VERDICT", rule("-")]
    for st, d, gate in records:
        tag = "EXEC" if gate.verdict == "EXECUTE" else "skip"
        lines.append(
            f"  {st.symbol:<6}  {st.asset_class[:6]:<6}  {d.action:<5} "
            f"{_ARROW[d.action]:>2}  {d.probability:>4.0%}  {d.confidence:>4.0%}   "
            + (tag if gate.verdict == "EXECUTE" else f"{tag} · {gate.reason}")
        )
    return "\n".join(lines)


def decision_card(st, d, gate) -> str:
    lines = [
        rule("="),
        f"  {st.symbol}  ·  {st.asset_class}  ·  {fmt_price(st.symbol, st.price)}",
        rule("-"),
        f"  MARKET STATE",
        f"    24h            {st.change_24h:>+7.2%}",
        f"    volume         {st.volume_delta:>+7.0%}",
        f"    momentum       {st.momentum:>+7.2f}",
        f"    news           {st.news:>+7.2f}",
        f"    regime         {st.regime:>7}",
        rule("-"),
        f"  JEV OUTPUT  ({d.source})",
        f"    decision       {d.action:>7}",
        f"    probability    {d.probability:>7.2f}",
        f"    confidence     {d.confidence:>7.2f}",
        rule("-"),
        f"  GATE           {gate.verdict}" + (f"  ·  {gate.reason}" if gate.reason else ""),
        rule("="),
    ]
    return "\n".join(lines)


def book_summary(book: Book) -> str:
    ret = (book.equity() / 10_000 - 1) if book.equity() else 0
    lines = [
        rule("="),
        "  PAPER BOOK",
        rule("-"),
        f"  equity     ${book.equity():>10,.2f}   ({ret:+.2%})",
        f"  open P&L   ${book.open_pnl():>+10,.2f}",
        f"  positions  {book.open_positions():>10}",
    ]
    if book.fills:
        lines.append(rule("-"))
        for f in book.fills:
            lines.append(f"    {f.action:<4} {f.symbol:<5}  ${f.size:,.0f}  "
                         f"{f.pnl():>+8,.2f}")
    lines.append(rule("="))
    lines.append("  paper only · JEV decides, execution is simulated · not advice")
    return "\n".join(lines)


# --- backtest ------------------------------------------------------------------

def backtest_report(res, settings, engine: str) -> str:
    spec = INSTRUMENTS[res.symbol]
    pf = res.profit_factor
    pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
    reasons = {}
    for t in res.trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1
    dec = ", ".join(f"{k} {v}" for k, v in sorted(res.decisions.items()))
    avg_w = sum(t.pnl for t in res.wins) / len(res.wins) if res.wins else 0
    avg_l = sum(t.pnl for t in res.losses) / len(res.losses) if res.losses else 0
    lines = [
        rule("="),
        f"  BACKTEST  ·  {res.symbol}  ·  {spec['name']}",
        rule("-"),
        f"  DATA        {res.source}  ·  {res.bars} daily bars  ·  {res.first_date} -> {res.last_date}",
        f"  DECISION    {'JEV (live)' if engine == 'jev' else 'offline (deterministic)'}",
        f"  EXECUTION   paper  ·  risk {settings.risk_pct:.1%}/trade  ·  "
        f"SL {settings.sl_atr} ATR  ·  TP {settings.tp_atr} ATR  ·  max {settings.max_hold} bars",
        f"  COSTS       spread {spec['spread']} ({spec['spread'] / spec['pip']:.0f} pip) per round trip",
        rule("-"),
        f"  start equity     ${res.start_equity:>12,.2f}",
        f"  final equity     ${res.final_equity:>12,.2f}   ({res.total_return:+.2%})",
        f"  buy & hold                       ({res.buy_hold:+.2%})",
        f"  max drawdown     {res.max_drawdown:>13.2%}",
        rule("-"),
        f"  trades           {len(res.trades):>13}   (exits: "
        + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())) + ")",
        f"  win rate         {res.win_rate:>13.1%}",
        f"  avg win / loss   ${avg_w:>+10,.2f} / ${avg_l:>+,.2f}",
        f"  profit factor    {pf_s:>13}",
        f"  total pips       {res.total_pips:>+13,.1f}",
        f"  daily decisions  {dec}",
    ]
    if res.trades:
        lines += [rule("-"), "  LAST TRADES"]
        d = spec["digits"]
        for t in res.trades[-6:]:
            lines.append(f"    {t.side:<4} {t.entry_date:>10}  {t.entry:>{d+6},.{d}f} -> "
                         f"{t.exit:>{d+6},.{d}f}  {t.reason:<7} {t.pips:>+8.1f} pips  ${t.pnl:>+9,.2f}")
    lines += [rule("="), "  paper only · simulated fills · past results do not predict future ones · not advice"]
    return "\n".join(lines)


def sweep_table(rows) -> str:
    """rows: (seed, Result)"""
    lines = ["  SEED   RETURN   BUY&HOLD   MAX DD   TRADES   WIN%    PF", rule("-")]
    for seed, r in rows:
        pf = "inf" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
        lines.append(f"  {seed:>4}  {r.total_return:>+7.1%}   {r.buy_hold:>+7.1%}   "
                     f"{r.max_drawdown:>6.1%}   {len(r.trades):>6}   {r.win_rate:>4.0%}  {pf:>5}")
    rets = sorted(r.total_return for _, r in rows)
    pos = sum(1 for x in rets if x > 0)
    lines += [rule("-"),
              f"  median return {rets[len(rets)//2]:+.1%}  ·  worst {rets[0]:+.1%}  ·  "
              f"best {rets[-1]:+.1%}  ·  profitable in {pos}/{len(rets)} runs"]
    return "\n".join(lines)


# --- live paper account ----------------------------------------------------------

def live_banner(source, symbols, account_path, watch) -> str:
    from .feeds import TICKERS
    tick = ", ".join(f"{s} ({TICKERS[source][s]})" for s in symbols)
    return "\n".join([
        rule("="),
        "  JEV-BOT LIVE  ·  real prices, FAKE money",
        rule("-"),
        f"  DATA        {source} · {tick}",
        f"  EXECUTION   paper only · no broker · no real orders",
        f"  ACCOUNT     {account_path}",
        f"  MODE        " + (f"re-check every {watch} min" if watch else "one check"),
        rule("="),
    ])


def live_events(events) -> str:
    from datetime import datetime
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not events:
        return f"  [{stamp}] no new daily bar yet; positions checked, nothing to do"
    return "\n".join(f"  [{stamp}] {e}" for e in events)


def account_status(acct, history: bool = False) -> str:
    eq = acct.equity()
    ret = eq / acct.start_equity - 1
    closed = acct.closed
    wins = [t for t in closed if t["pnl"] > 0]
    lines = [
        rule("="),
        "  PAPER ACCOUNT  ·  fake money",
        rule("-"),
        f"  equity      ${eq:>11,.2f}   ({ret:+.2%} since {acct.created[:10]})",
        f"  realised    ${acct.cash - acct.start_equity:>+11,.2f}   "
        f"open P&L ${acct.open_pnl():>+,.2f}",
        f"  trades      {len(closed):>11}   win rate "
        + (f"{len(wins) / len(closed):.0%}" if closed else "n/a"),
    ]
    for sym, lp in sorted(acct.last_price.items()):
        digits = INSTRUMENTS[sym]["digits"]
        dec = acct.last_decision.get(sym, {})
        lines.append(rule("-"))
        lines.append(f"  {sym:<7} {lp['price']:>12,.{digits}f}   ({lp['source']}, {lp['time'][:16]} UTC)")
        if dec:
            lines.append(f"          last signal {dec['action']} on {dec['bar']} "
                         f"(p {dec['probability']:.0%}) -> {dec['gate']}")
        p = acct.positions.get(sym)
        if p:
            px = lp["price"]
            direction = 1 if p["side"] == "BUY" else -1
            half = INSTRUMENTS[sym]["spread"] / 2
            fill = px - half if p["side"] == "BUY" else px + half
            pnl = (fill - p["entry"]) * direction * p["units"]
            lines.append(f"          OPEN {p['side']} @ {p['entry']:,.{digits}f}  "
                         f"stop {p['stop']:,.{digits}f}  target {p['target']:,.{digits}f}  "
                         f"P&L ${pnl:+,.2f}")
        else:
            lines.append("          no open position")
    if history and closed:
        lines += [rule("-"), "  CLOSED TRADES"]
        for t in closed[-15:]:
            digits = INSTRUMENTS[t["symbol"]]["digits"]
            lines.append(f"    {t['closed_at'][:10]}  {t['side']:<4} {t['symbol']:<6} "
                         f"{t['entry']:>11,.{digits}f} -> {t['exit']:>11,.{digits}f}  "
                         f"{t['reason']:<7} ${t['pnl']:>+9,.2f}")
    lines += [rule("="), "  paper only · real prices, simulated fills · not advice"]
    return "\n".join(lines)
