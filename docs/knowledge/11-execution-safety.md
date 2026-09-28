# 11 · Execution safety: one writer, netted exposure, kill first

> **Desk relevance.** Before any strategy logic, a desk asks: can two decisions race? What wins
> when the operator hits kill? What happens when the broker call fails half-way? This unit records
> how the project's auto-trade path answers those questions — and where it does not yet.

**Status in this repository.** The reconciler lives in the private gateway application, which
depends on the broker adapter and Redis; it is described here with code references to the private
tree (`apps/gateway-server/src/auto_trade/`) and is not shipped. It was built for, and run only
against, the broker's paper-trading (demo) account: in the research record, "live" means real time
on that account, never real money (the protocol's Tier L for real orders was never reached).

## Design

| Property | How |
|---|---|
| Netting | each strategy holds a signed virtual target (0 or ±q); `desired_net = clamp(Σ targets, ±MAX_NET)`; one market order sends `desired − actual`, with a deterministic client order id |
| Single writer | one async loop owns all state: `tokio::select! { biased; control, decisions, marks }` — an operator `Kill`/`KillAll` is always selected before queued decisions |
| Opt-in and demo-only | disabled unless `AUTO_TRADE_ENABLED` is set; refuses to start, and refuses every decision, on an account flagged as real |
| Lifecycle | Registered → Armed (dry run) → Running ⇄ Paused → Halted; leaving Halted needs an operator acknowledgement |
| Session legality | market-to-limit only in continuous phases plus a 30-second guard after each open; no new entries after 14:43 |
| Fail-closed checks | daily-loss preflight blocks on error and halts the strategy; if positions cannot be read at start-up, assume the worst so the first reconcile flattens |
| Orphan handling | after an order, poll until a terminal state, cancel, re-check, and if still unresolved re-sync the net from the broker |
| Dedupe | per-strategy client order ids with a 5-minute window |

The research-side resting-limit probe (`historical/research_lab/probes/resting_limit_probe.py`) —
implemented and tested against a fake broker, **not yet run** against the venue — follows the same
philosophy with five interlocks: the account guard runs in the constructor; resting orders only (a
crossing limit raises instead of being clamped); dry run by default, and sending to the broker's
paper-trading endpoint needs both a flag and an environment variable; per-session budgets with a
hard stop on any unexpected marketable fill; and cancel-all before the closing auction. Every row
it writes is hash-chained.

## Weaknesses found while distilling (stated, not hidden)

1. The real-account guard is a **deny-list of one entry**: an unknown real account would pass. The
   correct form is an **allow-list** of demo accounts plus an explicit paper execution mode.
2. `biased` orders *selection*, not pre-emption: a kill that arrives during an in-flight reconcile
   waits for that iteration (bounded by the ~3 s settle budget).
3. The hard kill assumed the book was flat even when the flatten call failed; it should re-sync
   the net from the broker.
