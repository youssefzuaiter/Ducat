# Long-term core: the go / no-go page

**Status: DRAFT for the owner to edit and decide.** Drafted 2026-11-05. Nothing here is registered, and nothing here
authorizes anything, until the owner has edited it and committed it with a date. Items marked **OWNER** are the
owner's to set; the suggested value is a starting point, not a decision. Not financial advice.

## Why this page exists

The design says the paper period is the out-of-sample test, and the briefs say the system is meant to manage real
money one day, but nowhere is it written what would make "one day" arrive. Without that, the decision gets made
after the fact, by feel, on a good month. This page fixes the rule **before** the evidence exists, in the same way the
backtest's pass rules, the paper report's reading rules (P1-P7) and the shadow set's (S1-S7) were fixed first.

Three honest limits shape it:

1. **Paper cannot prove returns.** A few quarters of a policy that rebalances four times a year is a test of the
   machinery, not of the portfolio (paper-report rule P5). The return case rests on the registered backtest, which is
   in-sample, and on the owner's own risk tolerance. No paper result changes that.
2. **Paper cannot prove real fill costs.** Alpaca's paper fills come from its own simulator (P3). Paper shows that
   orders go in at the right time, at the right size, at prices that traded. The registered cost is about 1 bp a year
   (P4), so this matters little, but it is not measured against a real market.
3. **A "go" is not a flag to flip.** The paper-only lock stays in the code. Real money is a separate, reviewed project
   (see "What a go means"). This page decides only whether to *start* that project.

So the gates below are about **whether the machine reliably does what the policy says**. Whether the owner is willing
to hold this portfolio through its registered worst case is a separate decision (gate R) that no paper evidence can
make for them.

## The gates (all must hold)

Each gate is something the existing reports already print or log, so it can be read, not argued. Suggested values are
marked **OWNER**.

| # | Gate | How it is read | Suggested (OWNER) |
|---|---|---|---|
| G1 | **Long enough.** Enough rebalances have run on the real machinery, and the shadow set has its first registered reading. | Paper report section 2; shadow report status line. | At least 4 quarterly rebalances executed (Jan, Apr, Jul, Oct 2027) **and** 252 shadow sessions (S5). Earliest: about October 2027. |
| G2 | **Integrity.** The record is trustworthy. | Paper report P1, every monthly report. | The hash chain intact and exactly one policy hash in every monthly report for the whole period. |
| G3 | **No unexplained incidents.** | Paper report P2 (`plan_mismatch`, `order_rejected`, abandoned or expired plans). | Zero unexplained in the last 2 quarters; every earlier one has a written cause and a fix in the repo. |
| G4 | **Every rebalance ran on its intended session.** The router was there in its window, and the Allocator ran every session. | Journal (`plan_done` on `execute_on`); allocator and watchdog logs. | Each of the last 2 rebalances executed on the day planned, with no manual rescue. |
| G5 | **Fills are real prices.** | Paper report P3 (pre-open fills within 25 bp of the open), P6 and P7 (after-sells buys inside their minute's range, none left unchecked). | No P3 fault and no P7 fault or unchecked fill in the last 2 rebalances, or each one explained. |
| G6 | **The twin tracks the account.** The shadow twin and the real account agree, so the plumbing does what the simulation says. | Shadow report S6, read after the close. | Gap never above 0.25% of equity on an after-close reading without a written explanation (the first reading was 0.006%). |
| G7 | **The safety brakes work.** | A drill on paper: `core_ctl halt`, confirm open orders cancel, `/health` shows the kill switch on and the watchdog reports it, then resume as the runbook says (delete `core-router-state.json`, restart). The breaker path is already covered by tests. | Drill performed once, and again after any change to the router. A halt leaves no journal entry today (it lives in the router's state file and `/health`), so the owner notes the date and result by hand in the repo. |
| G8 | **No quiet changes.** Policy and limits did not change during the evaluated period. | P1's single policy hash; `git log -- policy/ tier0_core.py`. | No change. A change restarts the clock for G1. |

**Gate R: risk acceptance (OWNER, not a measurement).** The registered worst case at the pessimistic cost level is a
maximum drawdown inside -35% and a longest underwater spell inside 4 years (the tolerance given 2026-10-01, O5), for
the owner's $10,000 policy mix. M1 (100% VTI) sat at -34.98% against that line, so the margin is thin for a pure equity
mix; the chosen M3 mix is the diversified one. The owner must be able to read those two numbers, and a real fall
of that size, without selling. If that is not true, the answer is no, whatever G1 to G8 say.

## Stop conditions (any one means no-go until fixed, and a go already given is paused)

- the journal fails its integrity check, or a second policy hash appears;
- a `plan_mismatch` the owner cannot explain from a real change in the inputs;
- a plan that broke a hard limit, or an order sent outside 09:20-09:27 New York;
- a P7 fault that is not explained;
- the twin gap above the G6 line with no explanation.

## What a go means

A go means: **start the live-trading project.** It does not mean flipping `paper=True`. That project is its own,
reviewed piece of work, and at minimum it covers:

- **Brokerage and eligibility (OWNER to verify, not assumed here):** a live account the owner can open and fund from
  where they live, with automated trading allowed on it, and what the broker requires for that.
- **Tax and legal (OWNER to verify with a qualified person):** how a self-managed portfolio with quarterly rebalancing
  is taxed in the owner's jurisdiction, and whether anything is needed to run software on one's own account. This
  system is for the owner's own money only; managing anyone else's is a different regulatory matter.
- **A reviewed code change** that lifts the paper-only lock for exactly one live account, behind the same hard limits,
  with the lock's tests rewritten rather than deleted, and a second person or a fresh, careful review of that diff.
- **Keys:** live credentials never in the repository or a shared `.env`; a separate secret store; separate read-only
  keys for reporting.
- **Hosting:** an always-on host, not a laptop that sleeps (the runbook already says a laptop is "Fine for the first weeks, while you watch");
  the state directory on persistent storage and backed up.
- **Rollback:** the kill switch tested on the live account before funding, and a written plan for selling to cash.

## How a first live step would go (OWNER)

Suggested, to be replaced by the owner's own numbers:

- **Size:** a small fraction of the intended amount first (for example 10% of it), held through one full quarterly
  rebalance before any increase, then stepped up only after each step runs clean. The $10,000 funded cap in
  `policy/core.toml` is the paper account's cap and stays a separate, deliberate setting.
- **Same gates, again,** on the live account: G2 to G6 read for its first two rebalances before the next size step.
- **No discretionary changes** to the mix, the rule or the limits during a step.

## Reviews

- Monthly: `backtest.paper_report`, read against G2, G3 and G5.
- After each quarterly rebalance (Jan 5, Apr, Jul and Oct 2027): G4 to G6 and the stop conditions.
- About October 2027: the first full reading of G1 to G8, and gate R.

## Changing this page

The gates, once committed, are not edited after seeing the evidence. If one turns out to be wrong, the fix is a new
dated version that says what changed and why, and the clock for G1 restarts. Nothing here loosens the paper-only lock,
the hard limits or any registered threshold; those change only through their own reviewed changes.
