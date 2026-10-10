# The research loop

Both dashboards now carry a **Research loop** panel. It shows where each strategy stands in the cycle a quant desk runs, and the hard limits that guard it:

```
research agents → ledger → strategy code → written prediction → honest backtest → sealed test
      ↑                                                                               ↓
   lessons  ←  weekly critic  ←  decision journal  ←  paper trading (kill switch)  ←  promote
```

Paper only, as before. Nothing here can place a real order.

## What is where

| Piece | Where it lives | What it does |
| --- | --- | --- |
| Kill switch | `config/risk.json`, enforced in `jev_bot/killswitch.py` | Checked in code before every new trade on every desk. Daily loss, drawdown from peak, open positions, order size, a per-desk pause and a `kill_all`. Open trades keep their stops. |
| Hypothesis ledger | `state/ledger.json` | Every idea ever tested, its attempts, regime, results and lessons. Failed ideas are kept. |
| Written prediction | inside each ledger entry | Written before testing, then locked with a sha256 hash. It can never be edited. |
| Honest backtest | `jev_bot/research/honest.py` | Newest 25% of history sealed; walk-forward windows; 2x costs; delayed fills; worst stretches; Deflated Sharpe using every attempt counted. |
| Weekly critic | `jev_bot/research/review.py`, Sundays via `.github/workflows/research.yml` | Paper vs backtest vs prediction, what the week's losing trades had in common (including their entry conditions), signals that were not traded, repeated mistakes, keep / watch / pause. |
| Decision journal | `journal/<desk>/<month>.jsonl`, written by the bots | Every decision the moment it is made: strategy and version, market conditions, what it did (open, skip, blocked, close) and why. Published with the accounts; read by the critic; latest entries on the dashboards. |
| Strategy template | `jev_bot/strategies/` (copy `_template.py`) | One shape for every daily strategy: entry rules, exits in ATR, sizing, plus unit tests. The backtester and the live desk run any of them. |
| Promotion gate | `research promote H4` | Only a strategy that passed the backtest AND its sealed test goes to paper. The server then trades it on its own account (`state/strategies/H4.json`) with its own journal and kill switch section (`daily:H4`, or the daily limits). |
| Research agents | `jev_bot/research/agents.py`, Saturdays | Price Analyst, Macro Watcher and Central-Bank Reader each propose up to 3 ideas from one angle, alone, after reading the ledger; then the Skeptic attacks them. Memos in `research/`, ideas in the ledger as untested. |

## From your phone

- **Pause a desk:** edit `config/risk.json` on GitHub (pencil icon), set that desk's `"enabled": false`. The servers pick it up within ~15 minutes.
- **Stop everything:** set `"kill_all": true`.
- **After a drawdown halt:** review, then either raise `max_drawdown_pct` or set `peak_reset` to any new text (for example today's date) to measure the drawdown from now.
- **Run the critic now / backtest the daily bot / ask the agents:** Actions → research → Run workflow → type `review`, `backtest H1`, `sealed H1` or `agents`.

## Turning on the research agents

They use the Claude API, billed to your Anthropic account (four calls a week; the two news readers also run a few web searches).

1. Create a key at console.anthropic.com → API keys.
2. GitHub → your repo → Settings → Secrets and variables → Actions → New repository secret: `ANTHROPIC_API_KEY`.
3. Optional: a repository *variable* `RESEARCH_MODEL` to pick the model (default `claude-sonnet-5-5`). Web search must be enabled for your organization in the Claude Console.

Without the key the Saturday run simply skips. Ideas never trade by themselves: each one waits in the ledger until you turn it into a strategy and it passes the honest tests.

## From idea to paper trading

```bash
cp jev_bot/strategies/_template.py jev_bot/strategies/meanrev.py   # edit: name, hypothesis, signal(), tests()
python -m jev_bot research check meanrev                         # its unit tests must pass
python -m jev_bot research wire H4 daily --strategy meanrev       # attach it to the ledger idea
python -m jev_bot research predict H4 --return 5 --win-rate 45 --max-dd 8 --min-trades 30 --fail-return-below 0
# GitHub: Actions → research → Run workflow → backtest H4   (then: sealed H4, once)
python -m jev_bot research promote H4                             # only after a passed sealed test
```

Claude Code prompt for the "turn the idea into code" step:

> Take idea H4 from state/ledger.json and write it as a strategy: copy jev_bot/strategies/_template.py to jev_bot/strategies/<name>.py, keep the same shape (signal(history) entry rules, sl_atr/tp_atr/max_hold exits, risk_pct sizing), put the hypothesis on top and a version number, and write tests() cases that confirm the entry rules trigger exactly when they should. Run `python -m jev_bot research check <name>` and fix anything that fails. Do not run a backtest.

## Commands

```bash
python -m jev_bot research list
python -m jev_bot research search --market XAUUSD --status failed
python -m jev_bot research add "Gold mean-reverts after 3 down days in a choppy regime" \
    --market XAUUSD --source "Price Analyst" --falsify "loses money over 40+ trades"
python -m jev_bot research predict H4 --return 6 --win-rate 45 --max-dd 8 --min-trades 30 --fail-return-below 0
python -m jev_bot research backtest H4            # development period only (add --source sim for a dry run)
python -m jev_bot research revise H4 "stop 1.2 ATR instead of 1.5"   # counts as an attempt; cap 5
python -m jev_bot research sealed H4              # exactly once per idea
python -m jev_bot research review                 # the critic
python -m jev_bot research agents --dry-run       # what each research agent would be given
python -m jev_bot research check _template        # a strategy's unit tests
python -m jev_bot research promote H4             # after a passed sealed test
python -m jev_bot research export-mt5 EURUSD XAUUSD --days 60       # Windows PC: history for scalper tests
```

Scalper desks (H2, H3) are tested on 1-minute history from MT5. On the Windows PC:

```bash
python -m jev_bot research export-mt5 EURUSD GBPUSD XAUUSD --days 60
python -m jev_bot research backtest H2 --csv EURUSD=data/mt5/EURUSD_M1.csv --csv GBPUSD=data/mt5/GBPUSD_M1.csv --csv XAUUSD=data/mt5/XAUUSD_M1.csv
```

Then commit `state/ledger.json` (the scalper's next publish does it for you if you run it inside `C:\paper-scalper`).

## The three strategies already running

H1 (daily), H2 (scalper A) and H3 (scalper B) reached paper trading before this loop existed. They have no prediction written up front and earlier tuning was not counted. The honest thing to do now:

1. Write a **forward** prediction for each (`research predict H1 ...`). It is marked as covering paper trading from the day it was written.
2. Run the honest backtest (`backtest H1` on GitHub; H2 and H3 on the MT5 PC).
3. Let the Sunday critic compare paper results against both.

## Research agents on Claude Pro (no API key)

Open Claude Code (or a claude.ai session with this repo) and paste:

> Run this week's research agents for paper-desk. For each role in price, macro, central: run `python -m jev_bot research agents --prompt <role> > /tmp/<role>-prompt.md`, then start a SEPARATE subagent per role that reads only its own prompt file (macro and central may use web search; price may not), writes its full reply to /tmp/<role>-reply.md, and never sees the other roles' files. Then import each with `python -m jev_bot research agents --import <role> /tmp/<role>-reply.md`. Then do the same for the Skeptic (`--prompt skeptic`, `--import skeptic`). Commit research/ and state/ledger.json and push.

That is the same run the Saturday workflow does with an API key: same briefs, same ledger checks, same duplicate filter.

## Research agents by hand (Claude Code prompts)

The Saturday run does this automatically. To run one yourself, keep each agent separate so they do not anchor on each other; each one checks the ledger first.

> You are the [Price Analyst | Macro Watcher | Skeptic]. Look only at [price and volume | rates, data releases, USD and gold drivers | why each idea fails] for [XAUUSD and EUR/USD]. Before proposing anything, run `python -m jev_bot research search --market XAUUSD` and `--status failed`, and do not repeat any failed idea unless the market regime has clearly changed. Propose at most 3 testable ideas. For each: the idea in one sentence, why it might work, the market conditions it needs, and what result would prove it wrong. Add each with `python -m jev_bot research add "..." --market ... --source "[ROLE]" --falsify "..."`. Do not read other agents' notes.

An idea becomes testable once it is wired into a strategy (`research wire H4 daily --strategy meanrev`); its prediction must be written before the first backtest. New 1-minute (scalper) strategies still go into `jev_bot/scalp/` by hand; the template covers daily strategies.
