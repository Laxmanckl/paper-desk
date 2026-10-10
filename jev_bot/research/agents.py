"""The research agents (Step 2): specialists that propose ideas, each from one angle.

    Price Analyst        price and volatility patterns only (gets the numbers)
    Macro Watcher        rates, inflation and jobs data, the dollar, risk mood (searches the web)
    Central-Bank Reader  Fed / ECB statements and speeches (searches the web)
    Skeptic              runs LAST, on the new ideas only: why each will fail

Rules the code enforces, not just the prompts:
  - each specialist runs in its own call and never sees the others' memos
  - each one gets the hypothesis ledger first (failed ideas included) and is
    told not to repeat them; near-duplicates of ledger ideas are dropped anyway
  - at most 3 ideas each, every idea with what would prove it wrong
  - ideas enter the ledger as "untested" ideas: nothing trades until a human
    wires one into a strategy and it passes the honest tests

Memos go to research/<role>-<date>.md. Uses the Claude API (ANTHROPIC_API_KEY);
the model is RESEARCH_MODEL, default below.

    python -m jev_bot research agents            all four
    python -m jev_bot research agents --dry-run  show what each agent would be given
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import ledger, stats

API = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("RESEARCH_MODEL", "claude-sonnet-5-5")
OUT = Path(__file__).resolve().parent.parent.parent / "research"
WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
MARKETS = ("XAUUSD", "EURUSD")

ROLES = {
    "price": {
        "name": "Price Analyst", "web": False,
        "angle": "price and volatility behaviour only: trends, ranges, breakouts, mean reversion, "
                 "day-of-week and session effects, volatility regimes. No news, no macro opinions.",
    },
    "macro": {
        "name": "Macro Watcher", "web": True,
        "angle": "macro drivers only: US and euro-area rates and yields, inflation, jobs data, the dollar "
                 "index, risk-on/risk-off mood, and the economic calendar for the next two weeks.",
    },
    "central": {
        "name": "Central-Bank Reader", "web": True,
        "angle": "central banks only: the Federal Reserve and the ECB (and central-bank gold buying): "
                 "recent decisions, statements, speeches and minutes, and what markets expect next.",
    },
}

DESK_FACTS = (
    "The desk paper-trades spot gold (XAUUSD) and EUR/USD. There are two kinds of strategy it can run: "
    "DAILY strategies that decide once per completed daily bar (entry rules on daily OHLC, stop and "
    "target in ATR multiples, hold up to a few weeks), and 1-MINUTE scalping strategies on forex majors and "
    "gold. Ideas must be testable on price history with fixed rules: something like 'when X, then Y within "
    "N days', not a one-off forecast."
)


class AgentError(Exception):
    pass


# --- what each agent is given ------------------------------------------------------

def ledger_brief(data: dict) -> str:
    rows = []
    for h in data.get("hypotheses", []):
        lessons = "; ".join(x["text"] for x in (h.get("lessons") or [])[-3:])
        rows.append(f"- {h['id']} [{ledger.STATUSES.get(h.get('status'), h.get('status'))}] "
                    f"({', '.join(h.get('markets', [])[:3])}; regime {h.get('regime') or 'n/a'}): {h['idea']}"
                    + (f" | lessons: {lessons}" if lessons else ""))
    return "\n".join(rows) or "(the ledger is empty)"


def market_brief(fetch=None) -> str:
    """Numbers only, computed here, so the Price Analyst works from real data."""
    from .. import feeds, fx, live
    lines = []
    for sym in MARKETS:
        try:
            bars, quote = (fetch or (lambda s: feeds.fetch(s, "yahoo", "2y", save_dir=None)))(sym)
            bars = live.completed_bars(bars, quote)
        except Exception as e:
            lines.append(f"{sym}: no data ({e})")
            continue
        c = [b.close for b in bars]
        if len(c) < 130:
            lines.append(f"{sym}: only {len(c)} days of data")
            continue
        atr = fx.atr(bars, len(bars) - 1)
        sma50 = sum(c[-50:]) / 50
        hi20, lo20 = max(b.high for b in bars[-20:]), min(b.low for b in bars[-20:])
        rets = [c[i] / c[i - 1] - 1 for i in range(len(c) - 120, len(c))]
        lines.append(
            f"{sym} ({bars[-1].date}): close {c[-1]:g}; change 5d {(c[-1] / c[-6] - 1) * 100:+.2f}%, "
            f"20d {(c[-1] / c[-21] - 1) * 100:+.2f}%, 60d {(c[-1] / c[-61] - 1) * 100:+.2f}%; "
            f"ATR14 {atr / c[-1] * 100:.2f}% of price; {(c[-1] / sma50 - 1) * 100:+.2f}% vs 50-day average; "
            f"20-day range {lo20:g}-{hi20:g}; regime {stats.regime(c)}; "
            f"up days in last 120: {sum(r > 0 for r in rets)}; last 10 closes {', '.join(f'{x:g}' for x in c[-10:])}")
    return "\n".join(lines)


def specialist_prompt(role: str, data: dict, market: str, today: str) -> tuple[str, str]:
    r = ROLES[role]
    system = (f"You are the {r['name']} on a small trading research desk. Your angle: {r['angle']}\n\n"
              f"{DESK_FACTS}\n\nYou work alone: you never see the other researchers' notes. Be concrete and "
              "sceptical of your own ideas. This is research for paper trading, not advice.")
    user = (f"Today is {today}.\n\nTHE HYPOTHESIS LEDGER (every idea already tested or queued, failures included). "
            "Do not propose anything already there, and never repeat a failed idea unless you can say exactly "
            f"which market condition has changed since it failed:\n{ledger_brief(data)}\n\n"
            f"MARKET NUMBERS:\n{market}\n\n"
            "Propose AT MOST 3 testable trading ideas for XAUUSD and/or EURUSD, from your angle only. For each: "
            "the idea in one sentence, why it might work, the exact market conditions it needs, and what result "
            "would prove it wrong. Fewer, better ideas beat three weak ones; zero is a valid answer.\n\n"
            "Reply with a short memo in markdown (your reasoning, sources if you searched), then end with ONE "
            "```json block exactly like:\n"
            '{"ideas": [{"idea": "...", "why": "...", "conditions": "...", "falsify": "...", '
            '"markets": ["XAUUSD"], "kind": "daily or scalp"}]}')
    return system, user


def skeptic_prompt(ideas: list[dict], data: dict, today: str) -> tuple[str, str]:
    system = ("You are the Skeptic on a small trading research desk. Your only job is to argue why each "
              "idea will fail: overfitting risk, costs and spreads, regime dependence, too few trades to "
              f"ever test, data that is not available, or a duplicate of a past failure.\n\n{DESK_FACTS}")
    listing = "\n".join(f"- {h['id']}: {h['idea']} (needs: {h.get('conditions', '')}; falsified by: "
                        f"{h.get('falsify', '')})" for h in ideas)
    user = (f"Today is {today}.\n\nNEW IDEAS:\n{listing}\n\nTHE LEDGER (past ideas and lessons):\n"
            f"{ledger_brief(data)}\n\nFor each new idea give the strongest reason it will fail, and a verdict: "
            "'test' (worth the effort despite that) or 'drop'. Be blunt. End with ONE ```json block:\n"
            '{"reviews": [{"id": "H7", "objection": "...", "verdict": "test"}]}')
    return system, user


# --- the API ----------------------------------------------------------------------------

def call(system: str, user: str, web: bool, key: str | None = None, model: str | None = None,
         post=None) -> str:
    """One agent call. Returns the text of the final answer. `post` is injectable for tests."""
    key = key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key and post is None:
        raise AgentError("ANTHROPIC_API_KEY is not set (add it as a GitHub secret to run the agents)")
    messages = [{"role": "user", "content": user}]
    body = {"model": model or MODEL, "max_tokens": 4000, "system": system, "messages": messages}
    if web:
        body["tools"] = [WEB_SEARCH]
    post = post or _post
    text = []
    for _ in range(4):                                # web search may pause a long turn
        resp = post(body, key)
        content = resp.get("content", [])
        text += [b.get("text", "") for b in content if b.get("type") == "text"]
        if resp.get("stop_reason") != "pause_turn":
            break
        messages.append({"role": "assistant", "content": content})
    return "".join(text)


def _post(body: dict, key: str) -> dict:
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AgentError(f"Claude API error {e.code}: {e.read().decode()[:300]}") from None
    except urllib.error.URLError as e:
        raise AgentError(f"could not reach the Claude API: {e.reason}") from None


def last_json(text: str) -> dict:
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for raw in reversed(blocks):
        try:
            return json.loads(raw)
        except ValueError:
            continue
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            pass
    raise AgentError("the agent's reply had no readable JSON block")


# --- dedupe against the ledger ------------------------------------------------------------

def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


def near_duplicate(idea: str, data: dict, threshold: float = 0.7) -> str | None:
    a = _words(idea)
    for h in data.get("hypotheses", []):
        b = _words(h.get("idea", ""))
        if not (a and b):
            continue
        common = len(a & b)
        # same idea reworded (Jaccard), or one a shortened copy of the other (overlap)
        if common / len(a | b) >= threshold or common / min(len(a), len(b)) >= 0.85:
            return h["id"]
    return None


# --- the weekly run ---------------------------------------------------------------------

def run(roles: list[str] | None = None, dry_run: bool = False, post=None, fetch=None,
        out_dir: str | os.PathLike | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    roles = roles or list(ROLES) + ["skeptic"]
    data = ledger.load()
    market = market_brief(fetch)
    out = Path(out_dir or OUT)
    report: dict = {"date": today, "added": [], "skipped": [], "errors": [], "memos": []}

    new: list[dict] = []
    # every specialist gets the ledger as it was BEFORE this run, so no agent sees
    # another agent's fresh ideas (that would anchor them)
    snapshot = json.loads(json.dumps(data))
    for role in [r for r in roles if r in ROLES]:
        system, user = specialist_prompt(role, snapshot, market, today)
        if dry_run:
            print(f"\n===== {ROLES[role]['name']} {'(with web search)' if ROLES[role]['web'] else ''}\n{user}")
            continue
        try:
            text = call(system, user, ROLES[role]["web"], post=post)
            ideas = (last_json(text).get("ideas") or [])[:3]
        except AgentError as e:
            report["errors"].append(f"{ROLES[role]['name']}: {e}")
            continue
        out.mkdir(parents=True, exist_ok=True)
        memo = out / f"{role}-{today}.md"
        memo.write_text(f"# {ROLES[role]['name']} · {today}\n\n{text.strip()}\n", encoding="utf-8")
        report["memos"].append(str(memo.name))
        for i in ideas:
            idea = str(i.get("idea", "")).strip()
            if not idea:
                continue
            dup = near_duplicate(idea, data)
            if dup:
                report["skipped"].append(f"{ROLES[role]['name']}: '{idea[:80]}' looks like {dup}")
                continue
            markets = [m.upper() for m in i.get("markets") or [] if m.upper() in MARKETS] or list(MARKETS)
            h = ledger.add(data, idea, "idea", markets, ROLES[role]["name"], str(i.get("falsify", "")),
                           note=f"why: {i.get('why', '')} | needs: {i.get('conditions', '')} | "
                                f"kind: {i.get('kind', '')} | memo: research/{memo.name}")
            h["conditions"] = str(i.get("conditions", ""))
            new.append(h)
            report["added"].append(h["id"])

    if "skeptic" in roles and new and not dry_run:
        system, user = skeptic_prompt(new, data, today)
        try:
            text = call(system, user, False, post=post)
            (out / f"skeptic-{today}.md").write_text(f"# Skeptic · {today}\n\n{text.strip()}\n", encoding="utf-8")
            report["memos"].append(f"skeptic-{today}.md")
            for rv in last_json(text).get("reviews") or []:
                try:
                    h = ledger.get(data, str(rv.get("id", "")))
                except ledger.LedgerError:
                    continue
                verdict = "drop" if str(rv.get("verdict", "")).lower().startswith("drop") else "test"
                h["skeptic"] = {"verdict": verdict, "objection": str(rv.get("objection", ""))}
                ledger.add_lesson(h, f"Skeptic ({verdict}): {rv.get('objection', '')}", "Skeptic")
        except AgentError as e:
            report["errors"].append(f"Skeptic: {e}")
    if not dry_run:
        ledger.save(data)
    return report
