//! Concrete strategies, built on the shared `orderflow-engine` and `signal-engine` primitives.

pub mod atr_breakout;
pub mod atrx;
pub mod avcd;
pub mod cvd_exhaustion;
pub mod dual_regime;
pub mod ib_fade;
pub mod mphf;
pub mod sig001;
pub mod srlcv;
pub mod supertrend_m30;
pub mod va_edge_rotation;
pub mod vwap_reversion_cvd;

pub use atr_breakout::{AtrBreakout, AtrBreakoutConfig};
pub use atrx::{Atrx, AtrxConfig};
pub use avcd::{Avcd, AvcdConfig};
pub use cvd_exhaustion::{CvdConfig, CvdExhaustion};
pub use dual_regime::{DualRegime, DualRegimeConfig};
pub use ib_fade::{IbFade, IbFadeConfig};
pub use mphf::{Mphf, MphfConfig};
pub use sig001::Sig001Strategy;
pub use srlcv::{Srlcv, SrlcvConfig};
pub use supertrend_m30::{SupertrendConfig, SupertrendM30};
pub use va_edge_rotation::{VaEdgeConfig, VaEdgeRotation};
pub use vwap_reversion_cvd::{VwapRevCvd, VwapRevCvdConfig};
