# 10 · Rust native platform: engines, transport, and an honest latency number

> **Desk relevance.** A trading system needs engines that are deterministic and a transport that
> degrades safely under load. This unit covers the Rust workspace published here and corrects the
> project's own earlier latency figure.

## Workspace (published subset)

The private workspace has 17 library crates and 2 applications (~107k lines, 1,366 tests; all
1,347 offline tests pass). This repository publishes the **15 crates that build and test without
any broker, database or GPU** (`native_platform/README.md` has the crate map and dependency graph):

| Layer | Crates |
|---|---|
| Domain | `market-types` (parse-don't-validate newtypes: fixed-point prices, non-negative volumes), `market-time` |
| Data contracts | `market-data-core` (provider and executor traits; `close_position` flattens server-side so a stale client cannot over-flatten) |
| Engines | `market-dom-engine`, `market-profile-engine`, `orderflow-engine` (+ `l2_dump`), `signal-engine`, `strategy-core`, `backtest-engine` (+ `backtest`) |
| View models | DOM, profile, order-flow |
| Transport | `client-proto`, `gateway-service` |
| Test support | `market-testkit` |

Left out: the broker adapter, the Redis/SQL adapters, the gateway application and the GPU desktop
client (see `docs/provenance.md`).

## Engineering properties

- `unsafe_code = "forbid"` and `unused_must_use = "deny"` at workspace level; there is no `unsafe`
  anywhere. Release profile: `opt-level 3`, fat LTO, one codegen unit, `panic = "abort"`, stripped.
- Engines are pure and deterministic; the backtester fills a decision on the **next** tick at a
  tick price and never reads the wall clock.
- Property tests (proptest) check invariants against simple oracles: incremental CVD equals batch,
  VPIN stays in [0, 1], POC lies inside the value area, the DOM as-of join matches a reference.
- `Cargo.lock` is committed; the toolchain is pinned in `rust-toolchain.toml`.

## Transport (`client-proto`, `gateway-service`)

- 20-byte big-endian frame header (version, flags, kind, stream id, sequence, payload length) with a
  JSON payload; 71 message kinds; an unknown kind closes the connection by design.
- Backpressure is end-to-end: each subscription feeds a capacity-1 channel, so a slow client slows
  its producer instead of growing an unbounded buffer. Execution requests have their own lane with
  a short queue and a timeout, failing fast with `lane_overloaded`.
- A circuit breaker opens for 30 s after 5 failures in 10 s and serves the last cached payload
  marked `degraded`.
- A client that starts mid-session back-fills the day before switching to the live stream,
  session-scoped and checkpoint-gated (a private decision record, ADR 0042); the message kinds for it
  live in `client-proto`.

## Latency — what the old number measured, and the corrected one

The project's earlier benchmark reported **p99 124.5 µs** "server write → client parse, loopback".
Re-reading the harness: server and client ran in one process, the server wrote frames
**unpaced**, and the client started its timer before each read — so it measured how fast a client
drains a backlog, not the one-way latency of a tick. Re-running it reproduces the figure
(p99 124–133 µs), which is exactly why the method matters more than the number.

The corrected probe shipped here (`native_platform/crates/client-proto/examples/loopback_latency.rs`)
uses one clock for both sides, `TCP_NODELAY` on both ends, one write per frame, paced sends, a
blocking reader that decodes exactly like the desktop client, and an HdrHistogram. On a Windows
workstation, paced: **one-way p50 ≈ 0.10 ms, p99 ≈ 0.19–0.21 ms across runs (loopback)** — dominated by waking the
blocked reader, and still well under the 1 ms design target. An earlier async (tokio) variant of the
probe measured p99 ≈ 0.35–0.37 ms because it adds a runtime wake-up on the read side. Under flood,
the per-frame read+decode series reproduces the old figure — which is exactly why it was not a
one-way latency. None of these is a network or exchange latency. `demos/07-loopback-latency.py` runs
the probe on your machine.

## Known issues found while distilling

- **Fixed here, with tests:** the frame header decoder trusted the declared payload length (a peer
  could request a 4 GiB allocation) — it now rejects anything above a 16 MiB limit before allocating;
  the anchored-VWAP calculator added the pre-anchor VWAP to the post-anchor one (a test that only
  asserted `after > before` hid it).
- **Documented:** the private gateway application did not set `TCP_NODELAY` on accepted sockets and
  wrote header and payload in two writes; the Rust session calendar has no holiday table (the Python
  one does).

Details and test names: `native_platform/README.md`.
