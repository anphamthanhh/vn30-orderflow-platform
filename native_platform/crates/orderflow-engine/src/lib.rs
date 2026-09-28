//! Incremental order-flow and bar indicators for VN30F1M.
//!
//! CVD, VPIN, OFI, OBI, volume profile, VWAP, ATR, ADX, RSI and session references; the
//! `l2_dump` binary serves the Python parity tests.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

pub mod adx;
pub mod anchored_vwap;
pub mod atr;
pub mod cvd;
pub mod delta;
pub mod imbalance;
pub mod initial_balance;
pub mod obi;
pub mod ofi;
pub mod rsi;
pub mod spread;
pub mod volume_ma;
pub mod volume_profile;
pub mod vpin;
pub mod vwap;
