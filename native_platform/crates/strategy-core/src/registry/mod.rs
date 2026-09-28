//! Strategy registry: TOML-declared strategy instances, each with its own id and parameters, and
//! a factory that builds each into a `Box<dyn Strategy>`.

pub mod config;
pub mod factory;

pub use config::{
    InstanceParams, RegistryError, StrategyInstanceCfg, StrategyKind, StrategyRegistryCfg,
};
pub use factory::{build, BuiltStrategy, Cadence};
