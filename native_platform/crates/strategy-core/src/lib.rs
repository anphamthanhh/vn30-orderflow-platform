//! The [`Strategy`] trait and the concrete strategies, shared by the backtester and live trading.
//!
//! A strategy is a pure decision function returning a [`BtDecision`]; fills, fees and the
//! portfolio belong to the caller. The crate does no I/O.

pub mod registry;
pub mod strategies;
pub mod strategy;

pub use registry::{
    build, BuiltStrategy, Cadence, StrategyInstanceCfg, StrategyKind, StrategyRegistryCfg,
};
pub use strategies::{
    AtrBreakout, AtrBreakoutConfig, Atrx, AtrxConfig, Avcd, AvcdConfig, CvdConfig, CvdExhaustion,
    DualRegime, DualRegimeConfig, IbFade, IbFadeConfig, Mphf, MphfConfig, Sig001Strategy, Srlcv,
    SrlcvConfig, SupertrendConfig, SupertrendM30, VaEdgeConfig, VaEdgeRotation, VwapRevCvd,
    VwapRevCvdConfig,
};
pub use strategy::{
    dir_str, Action, Bar, BtDecision, Dir, Fill, MarketCtx, PositionView, Reason, Strategy,
};
