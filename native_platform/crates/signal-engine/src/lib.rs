//! SIG-001 signal engine: a synchronous three-tier decision state machine.
//!
//! ```text
//! TIER 1 — FILTER  VpinCalculator          per tick (volume buckets)
//! TIER 2 — BIAS    80% Rule (cold path)     once after 09:30 ICT (set_bias)
//! TIER 3 — ENTRY   CVD exhaustion           per completed 15m bar
//! EXITS  — adaptive trailing stop (chandelier ATR)
//! ```
//!
//! `SignalEvaluator::on_tick` maps `(price, volume, side, ts)` to signals and a decision without
//! async or network code; `backtest-engine` runs the same evaluator.

pub mod evaluator;
pub mod registry;
pub mod trailing;
pub mod triggers;

pub use evaluator::SignalConfig;
