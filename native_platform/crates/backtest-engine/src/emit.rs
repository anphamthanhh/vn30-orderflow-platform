//! Trade and equity records and their JSONL writer, read as-is by the Python metrics layer.
//!
//! P&L is already net of all fees; consumers must not charge them again.

use serde::Serialize;
use std::io::{self, Write};
use std::path::Path;

use crate::strategy::Reason;

/// One fill (enter or exit). `pnl_*` are populated only on exits.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TradeRecord {
    pub ts: i64,
    pub side: &'static str,
    pub action: &'static str,
    pub qty: u32,
    pub price: f64,
    /// Fee charged on this fill (VND).
    pub fee_vnd: f64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pnl_points: Option<f64>,
    /// Round-trip net P&L (VND) after entry+exit+position fees. Exit rows only.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pnl_vnd: Option<f64>,
    pub reason: Reason,
}

/// Account equity sample (bucket-last downsampled by the driver).
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
pub struct EquityPoint {
    pub ts: i64,
    pub equity: f64,
    /// Signed position at this sample (+long, -short, 0 flat).
    pub position: i32,
    pub unrealized: f64,
    pub realized: f64,
}

/// Write records as JSON Lines (one compact object per line). Overwrites.
pub fn write_jsonl<T: Serialize>(path: impl AsRef<Path>, records: &[T]) -> io::Result<()> {
    let file = std::fs::File::create(path)?;
    let mut w = io::BufWriter::new(file);
    for r in records {
        let line = serde_json::to_string(r).map_err(io::Error::other)?;
        w.write_all(line.as_bytes())?;
        w.write_all(b"\n")?;
    }
    w.flush()
}
