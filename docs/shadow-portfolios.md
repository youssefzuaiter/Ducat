# Long-term core: shadow portfolios

**Status:** registered 2026-11-05 · **Code:** `backtest/shadow.py` · **Tests:** `tests/test_core_shadow.py`

> Not financial advice. This compares rules on published prices; it does not rank, recommend or judge returns.

## Why it exists

The paper account tests the machinery: does the router build, rebalance and journal correctly. It cannot also
test the strategy against alternatives, because Alpaca allows only a few paper accounts and the news agent and
the core already use two. A shadow portfolio needs no account. It keeps a ledger, decides at each published close
with `core_paper.decide` (the function the live Allocator and router call), and fills at the next official open.

Four portfolios start together from the session the real account was built (2026-10-12) with $10,000 each:

| Name | Holds | Rule |
|---|---|---|
| `twin` | The owner's policy: 19% each of VTI, VXUS, BND, IAU, VNQ and 5% BIL | quarterly |
| `m3` | 20% each of the same five ETFs, no buffer | quarterly |
| `m2` | 60% VTI / 40% BND | quarterly |
| `m1` | 100% VTI, the benchmark | none (buy and hold) |

The reading rules (S1 to S7) are in `backtest/shadow.py` and printed at the top of every report. The key ones: the
set is closed, numbers are descriptive and labelled *not evidence* until 252 sessions, and the twin is a check on
the machinery (its value should track the real account's equity).

## Running it

```bash
./.venv/bin/python -m backtest.shadow --with-account               # update shadow-state/ and print the report
./.venv/bin/python -m backtest.shadow --out reports/shadow.md      # also keep the report
```

It recomputes the whole series from freshly fetched, dividend-adjusted bars every time, so there is no state to
corrupt. Daily values go to `shadow-state/shadow-days.csv` (not committed). Run it after 16:20 New York.

## What it does not do

- It charges no spread or fee (about 1 bp a year in the registered backtest).
- It does not tell you which mix is better before 252 sessions; over a short window the ordering is noise.
- M4 (inverse volatility) is not in the set. Adding it needs a new version and its own start date.
