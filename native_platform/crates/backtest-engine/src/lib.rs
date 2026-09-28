//! Deterministic, look-ahead-free backtest engine for VN30F1M futures (ADR 0008).
//!
//! Decisions fill at the next tick's price ([`run_ticks`], [`run_bars`]); [`run_prebuilt_bars`]
//! fills at bar opens, for signal research only. [`FillSim`] charges all fees, including PIT.

pub mod driver;
pub mod emit;
pub mod fill;
pub mod portfolio;

// strategies live in the shared `strategy-core` crate; re-exported to keep `crate::strategy` paths
pub use strategy_core::{strategies, strategy};

pub use driver::{run_bars, run_prebuilt_bars, run_ticks, run_ticks_with_book, BacktestResult};
pub use emit::{write_jsonl, EquityPoint, TradeRecord};
pub use fill::{e1, in_session, FillConfig, FillModel, FillSim};
pub use market_types::{BookSnapshot, RawBookSnapshot};
pub use portfolio::{Portfolio, Position};
pub use strategy_core::{
    Action, AtrBreakout, AtrBreakoutConfig, Atrx, AtrxConfig, Avcd, AvcdConfig, Bar, BtDecision,
    CvdConfig, CvdExhaustion, Dir, DualRegime, DualRegimeConfig, Fill, IbFade, IbFadeConfig,
    MarketCtx, Mphf, MphfConfig, PositionView, Sig001Strategy, Srlcv, SrlcvConfig, Strategy,
    SupertrendConfig, SupertrendM30, VaEdgeConfig, VaEdgeRotation, VwapRevCvd, VwapRevCvdConfig,
};
