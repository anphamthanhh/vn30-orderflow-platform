# Knowledge map

Each page follows the same shape — desk relevance, problem, mechanism with its constants, where the
code and tests are, the demo, and the limits. Numbers quoted from the private research record are
labelled as such; numbers produced in this repository come from the demos and are reproducible.

| # | Unit | The one thing to take away | Demo |
|---|---|---|---|
| 01 | [Contract, sessions, calendar](01-contract-sessions-calendar.md) | the calendar is derived from the tape, and the session has two closes | 03 |
| 02 | [Transaction cost and the PIT correction](02-transaction-cost-and-pit-tax.md) | the tax base is notional × IM/2: the tax term was ~5.9× too high, the total fee ~4–4.7× | 01 |
| 03 | [Execution realism and fill models](03-execution-realism-and-fill-models.md) | taker slippage measured on the recorded tape at 0.2 pt (twice the assumption); maker economics estimated from recorded books | 01, 06 |
| 04 | [Basis and index arbitrage](04-basis-and-index-arbitrage.md) | classic cash–futures arbitrage is a prop-desk trade; a one-year daily screen cannot validate a few-point edge | 03 |
| 05 | [Order-flow features and parity](05-order-flow-features-and-parity.md) | one name = one formula, proven bit-identical in Python and Rust | 02 |
| 06 | [Market profile](06-market-profile.md) | TPO/POC/value-area tie rules written down and pinned by a shared golden fixture | 02 |
| 07 | [Research integrity](07-research-integrity.md) | pre-registration, a holdout lock enforced in code, a ledger of every trial — and of the times the lock was broken | 03 |
| 08 | [Validation, three generations](08-validation-evolution.md) | a promotion gate retired because measurement showed no card could ever pass it | 05 |
| 09 | [Data platform and provenance](09-data-platform-and-provenance.md) | second-resolution timestamps need an ingest sequence, or candle closes are wrong | 04 |
| 10 | [Rust native platform](10-rust-native-platform.md) | deterministic engines, bounded backpressure, and a latency number re-measured correctly | 07 |
| 11 | [Execution safety](11-execution-safety.md) | one writer, netted exposure, kill first — and the guard weaknesses stated | — |
| 12 | [Engineering method](12-engineering-method.md) | AI-assisted development, controlled by records, generated state and parity tests | — |

Demos: `python demos/run-all.py` (outputs in `samples/outputs/`).
