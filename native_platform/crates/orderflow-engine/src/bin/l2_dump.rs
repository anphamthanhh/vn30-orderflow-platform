//! `l2_dump`: dump an L2 feature series computed by the engine crates, for Python↔Rust parity.
//!
//! Signing and accumulation use [`orderflow_engine::delta::DeltaCalculator::on_tick`] and
//! [`orderflow_engine::cvd::CvdTracker`]; the TPO profile and value area use
//! [`market_profile_engine::build_tpo_profile_from_ticks`] (ADR 0037). Only tape parsing and the
//! session watermarks (ICT day reset, 13:00 lunch rollover) are implemented here.
//!
//! ```text
//! cargo run -p orderflow-engine --bin l2_dump -- \
//!     --feature tape_cvd_lot_v1 --tape <tape.csv> [--out <path>]
//! cargo run -p orderflow-engine --bin l2_dump -- \
//!     --feature prof_tpo_poc_pts_v1 --tape <tape.csv> [--emit observation|profile]
//! ```
//!
//! Input: CSV with header `event_timestamp,price,volume,aggressor`, timestamps in ICT (UTC+7).
//! CVD features read `volume,aggressor`; `prof_tpo_poc_pts_v1` reads `price`.
//! Output: TSV with CVD as an integer and prices as integer ticks, so both stacks compare exactly.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use std::collections::BTreeMap;
use std::io::{BufWriter, Write};
use std::process::ExitCode;

use chrono::{FixedOffset, TimeZone};
use market_profile_engine::{build_tpo_profile_from_ticks, EngineTick};
use market_types::{EventTimestamp, Price, Volume};
use orderflow_engine::cvd::CvdTracker;
use orderflow_engine::delta::DeltaCalculator;

/// ICT offset from UTC in seconds (UTC+7, no DST).
const ICT_OFFSET_SECONDS: i64 = 7 * 3600;
/// Start of the afternoon session, 13:00 ICT, in seconds after midnight.
const LUNCH_RETURN_SECONDS: i64 = 13 * 3600;
/// Bar width for `DeltaCalculator`; the per-print `net_delta` read here does not depend on it.
const DELTA_BAR_SECONDS: i64 = 60;

/// VN30F1M tick size (0.1 pt) in `market_types::PRICE_SCALE` units.
const TICK_SIZE_SCALED: i64 = 1_000;
/// `params.price_bucket_tick` = 1 (`price_grouping` in the engine).
const PRICE_BUCKET_TICK: i32 = 1;
/// `params.tpo_period_min` = 30 (`market_profile_engine::DEFAULT_TPO_BUCKET_MINUTES`).
const TPO_PERIOD_MIN: i64 = 30;
/// `params.va_pct` = 0.70 (`market_profile_engine::DEFAULT_VALUE_AREA_PCT`).
const VA_PCT: f64 = 0.70;

/// L2 features this binary can dump; each name is a distinct feature definition.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Feature {
    /// `tape_cvd_lot_v1`: resets at the ICT day boundary; at 13:00 keeps the level, clears history.
    SessionRollover,
    /// `tape_cvd_norst_lot_v1`: never resets; a cumulative sum over the whole tape.
    NoReset,
    /// `prof_tpo_poc_pts_v1`: TPO POC and value area per ICT session (ADR 0037), with
    /// `price_bucket_tick = 1`, `tpo_period_min = 30`, `va_pct = 0.70`.
    ProfTpo,
}

impl Feature {
    fn parse(name: &str) -> Option<Self> {
        match name {
            "tape_cvd_lot_v1" => Some(Self::SessionRollover),
            "tape_cvd_norst_lot_v1" => Some(Self::NoReset),
            "prof_tpo_poc_pts_v1" => Some(Self::ProfTpo),
            _ => None,
        }
    }

    fn is_cvd(self) -> bool {
        matches!(self, Self::SessionRollover | Self::NoReset)
    }

    /// Tape columns this feature reads; a missing one is an error.
    fn required_columns(self) -> &'static [&'static str] {
        match self {
            Self::SessionRollover | Self::NoReset => &["volume", "aggressor"],
            Self::ProfTpo => &["price"],
        }
    }
}

/// Output shape of a profile feature (ADR 0037).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Emit {
    /// Scalar reduction: one row per session, for `l2_observation`.
    Observation,
    /// Distribution: one row per price level keyed by integer tick, for `l2_profile`.
    Profile,
}

impl Emit {
    fn parse(name: &str) -> Option<Self> {
        match name {
            "observation" => Some(Self::Observation),
            "profile" => Some(Self::Profile),
            _ => None,
        }
    }
}

/// One print from the tape; `epoch_s` is UTC epoch seconds.
///
/// Columns absent from the tape are `None`, NaN or empty; each feature checks the ones it reads.
struct Print {
    epoch_s: i64,
    price: Option<f64>,
    volume: f64,
    aggressor: String,
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("l2_dump: {e}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), String> {
    let (feature, emit, tape, out) = parse_args()?;
    let prints = read_tape(&tape, feature)?;

    let stdout = std::io::stdout();
    let mut sink: Box<dyn Write> = match out {
        Some(path) => Box::new(BufWriter::new(
            std::fs::File::create(&path).map_err(|e| format!("cannot write {path}: {e}"))?,
        )),
        None => Box::new(BufWriter::new(stdout.lock())),
    };
    match feature {
        Feature::SessionRollover | Feature::NoReset => {
            let rows = compute(feature, &prints)?;
            write_tsv(&mut sink, &rows).map_err(|e| format!("write failed: {e}"))?;
        }
        Feature::ProfTpo => {
            let (observations, levels) = compute_profile(&prints)?;
            match emit {
                Emit::Observation => write_observations(&mut sink, &observations),
                Emit::Profile => write_profile(&mut sink, &levels),
            }
            .map_err(|e| format!("write failed: {e}"))?;
        }
    }
    sink.flush().map_err(|e| format!("flush failed: {e}"))
}

fn parse_args() -> Result<(Feature, Emit, String, Option<String>), String> {
    let mut feature = None;
    let mut emit = None;
    let mut tape = None;
    let mut out = None;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--feature" => {
                let v = args.next().ok_or("--feature needs a value")?;
                feature = Some(Feature::parse(&v).ok_or_else(|| {
                    format!(
                        "unknown feature {v:?}; known: tape_cvd_lot_v1, tape_cvd_norst_lot_v1, \
                         prof_tpo_poc_pts_v1"
                    )
                })?);
            }
            "--emit" => {
                let v = args.next().ok_or("--emit needs a value")?;
                emit =
                    Some(Emit::parse(&v).ok_or_else(|| {
                        format!("unknown emit {v:?}; known: observation, profile")
                    })?);
            }
            "--tape" => tape = Some(args.next().ok_or("--tape needs a value")?),
            "--out" => out = Some(args.next().ok_or("--out needs a value")?),
            other => return Err(format!("unexpected argument {other:?}")),
        }
    }
    let feature = feature.ok_or("--feature is required")?;
    // `--emit` would be a no-op for a CVD feature, so it is rejected rather than ignored.
    if emit.is_some() && feature.is_cvd() {
        return Err(
            "--emit applies only to features with a distribution; a CVD series has none"
                .to_string(),
        );
    }
    Ok((
        feature,
        emit.unwrap_or(Emit::Observation),
        tape.ok_or("--tape is required")?,
        out,
    ))
}

// --------------------------------------------------------------------------- tape

/// Read the tape; fails if `event_timestamp` or a column `feature` reads is missing.
fn read_tape(path: &str, feature: Feature) -> Result<Vec<Print>, String> {
    let text = std::fs::read_to_string(path).map_err(|e| format!("cannot read {path}: {e}"))?;
    let mut lines = text.lines();
    let header = lines.next().ok_or_else(|| format!("{path} is empty"))?;
    let cols: Vec<String> = split_csv(header)
        .into_iter()
        .map(|c| c.to_lowercase())
        .collect();
    let find = |name: &str| -> Option<usize> { cols.iter().position(|c| c == name) };
    let i_ts = find("event_timestamp")
        .ok_or_else(|| format!("{path} has no \"event_timestamp\" column; got {cols:?}"))?;
    let (i_price, i_vol, i_side) = (find("price"), find("volume"), find("aggressor"));
    for name in feature.required_columns() {
        if find(name).is_none() {
            return Err(format!(
                "{path} has no {name:?} column, which {feature:?} reads; got {cols:?}"
            ));
        }
    }

    let mut prints = Vec::new();
    for (n, line) in lines.enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let f = split_csv(line);
        let need = [Some(i_ts), i_price, i_vol, i_side]
            .into_iter()
            .flatten()
            .max()
            .unwrap_or(i_ts);
        if f.len() <= need {
            return Err(format!(
                "{path}:{} has {} fields, need {}",
                n + 2,
                f.len(),
                need + 1
            ));
        }
        let epoch_s = ict_string_to_epoch(&f[i_ts])
            .map_err(|e| format!("{path}:{}: bad event_timestamp: {e}", n + 2))?;
        let price = match i_price {
            Some(i) => Some(
                f[i].parse::<f64>()
                    .map_err(|_| format!("{path}:{}: bad price {:?}", n + 2, f[i]))?,
            ),
            None => None,
        };
        let volume = match i_vol {
            Some(i) => f[i]
                .parse::<f64>()
                .map_err(|_| format!("{path}:{}: bad volume {:?}", n + 2, f[i]))?,
            None => f64::NAN,
        };
        prints.push(Print {
            epoch_s,
            price,
            volume,
            aggressor: i_side.map(|i| f[i].clone()).unwrap_or_default(),
        });
    }
    Ok(prints)
}

/// Split a ClickHouse `CSVWithNames` line: comma-separated, optional double quotes, no escapes.
fn split_csv(line: &str) -> Vec<String> {
    line.trim_end_matches('\r')
        .split(',')
        .map(|f| f.trim().trim_matches('"').to_string())
        .collect()
}

/// Parse `YYYY-MM-DD HH:MM:SS[.fff]` as ICT wall-clock time into UTC epoch seconds.
///
/// Sub-second digits are truncated, matching `market_types::EventTimestamp` (seconds).
/// ICT is a fixed UTC+7 with no DST, so no timezone database is needed.
fn ict_string_to_epoch(s: &str) -> Result<i64, String> {
    let s = s.trim();
    let (date, time) = s
        .split_once(['T', ' '])
        .ok_or_else(|| format!("{s:?} is not `YYYY-MM-DD HH:MM:SS`"))?;
    let d: Vec<&str> = date.split('-').collect();
    let time = time.split('.').next().unwrap_or(time);
    let t: Vec<&str> = time.split(':').collect();
    if d.len() != 3 || t.len() != 3 {
        return Err(format!("{s:?} is not `YYYY-MM-DD HH:MM:SS`"));
    }
    let num = |v: &str| {
        v.parse::<i64>()
            .map_err(|_| format!("{s:?} has a non-numeric field"))
    };
    let (y, m, day) = (num(d[0])?, num(d[1])?, num(d[2])?);
    let (h, mi, sec) = (num(t[0])?, num(t[1])?, num(t[2])?);
    if !(1..=12).contains(&m) || !(1..=31).contains(&day) {
        return Err(format!("{s:?} has an out-of-range date"));
    }
    if !(0..24).contains(&h) || !(0..60).contains(&mi) || !(0..=60).contains(&sec) {
        return Err(format!("{s:?} has an out-of-range time"));
    }
    Ok(days_from_civil(y, m, day) * 86_400 + h * 3600 + mi * 60 + sec - ICT_OFFSET_SECONDS)
}

/// Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's `days_from_civil`).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let mp = (m + 9) % 12; // Mar = 0
    let doy = (153 * mp + 2) / 5 + d - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146_097 + doe - 719_468
}

// --------------------------------------------------------------------------- compute

/// One output row: the CVD after applying the print at `idx`.
struct Step {
    idx: usize,
    epoch_s: i64,
    cvd: i64,
}

fn compute(feature: Feature, prints: &[Print]) -> Result<Vec<Step>, String> {
    let mut delta = DeltaCalculator::new(DELTA_BAR_SECONDS);
    let mut cvd = CvdTracker::default();
    let mut last_day_key: Option<i64> = None;
    let mut last_sec_in_day: i64 = 0;
    let mut out = Vec::with_capacity(prints.len());

    for (idx, p) in prints.iter().enumerate() {
        if feature == Feature::SessionRollover {
            // no reset on the first day; a day change never also fires the lunch rollover
            let ict = p.epoch_s + ICT_OFFSET_SECONDS;
            let day_key = ict.div_euclid(86_400);
            let sec_in_day = ict.rem_euclid(86_400);
            if last_day_key != Some(day_key) {
                if last_day_key.is_some() {
                    cvd.reset();
                }
                last_day_key = Some(day_key);
            } else if last_sec_in_day < LUNCH_RETURN_SECONDS && sec_in_day >= LUNCH_RETURN_SECONDS {
                cvd.rollover();
            }
            last_sec_in_day = sec_in_day;
        }

        let tick = delta.on_tick(p.volume, &p.aggressor, p.epoch_s);
        let value = cvd.update(tick.net_delta, p.epoch_s);
        out.push(Step {
            idx,
            epoch_s: p.epoch_s,
            cvd: as_exact_integer(value)
                .ok_or_else(|| format!("row {idx}: CVD {value} is not an exact integer"))?,
        });
    }
    Ok(out)
}

/// Return `v` as `i64` if it is an integer exactly representable in `f64` (|v| ≤ 2^53).
///
/// CVD sums integer lot counts, so any other value is an input error and is not rounded.
fn as_exact_integer(v: f64) -> Option<i64> {
    if v.is_finite() && v.fract() == 0.0 && v.abs() <= 9.007_199_254_740_992e15 {
        Some(v as i64)
    } else {
        None
    }
}

fn write_tsv(sink: &mut dyn Write, rows: &[Step]) -> std::io::Result<()> {
    writeln!(sink, "idx\tepoch_s\tcvd")?;
    for r in rows {
        writeln!(sink, "{}\t{}\t{}", r.idx, r.epoch_s, r.cvd)?;
    }
    Ok(())
}

// ------------------------------------------------------------------- profile

/// Scalar reduction of one session's TPO profile (an `l2_observation` row).
struct ProfileObservation {
    session: String,
    prints: usize,
    levels: usize,
    tpo_total: i64,
    poc_tick: i64,
    val_tick: i64,
    vah_tick: i64,
    va_width_ticks: i64,
}

/// One price level of a session's TPO histogram (an `l2_profile` row).
struct ProfileLevel {
    session: String,
    price_tick: i64,
    tpo_count: i64,
}

/// Convert a bucketed `Price` to whole ticks; an off-grid price is an error, not rounded.
fn to_tick(price: Price) -> Result<i64, String> {
    if price.0 % TICK_SIZE_SCALED != 0 {
        return Err(format!(
            "price {} is not on the {TICK_SIZE_SCALED}-unit tick grid; the profile was not bucketed",
            price.0
        ));
    }
    Ok(price.0 / TICK_SIZE_SCALED)
}

/// Group prints into engine ticks per ICT calendar day, in ascending order.
///
/// The lunch break does not split a session (`params.seam_13h: none`). `volume` is zero
/// because a TPO count does not read it.
fn build_sessions(prints: &[Print]) -> Result<BTreeMap<i64, Vec<EngineTick>>, String> {
    let offset = FixedOffset::east_opt(
        i32::try_from(ICT_OFFSET_SECONDS).map_err(|_| "ICT offset does not fit in i32")?,
    )
    .ok_or("ICT offset is not a valid fixed offset")?;
    let mut by_day: BTreeMap<i64, Vec<EngineTick>> = BTreeMap::new();
    for (idx, p) in prints.iter().enumerate() {
        let price = p
            .price
            .ok_or_else(|| format!("row {idx}: prof_tpo_poc_pts_v1 needs a `price` column"))?;
        let datetime = offset
            .timestamp_opt(p.epoch_s, 0)
            .single()
            .ok_or_else(|| format!("row {idx}: epoch {} is not a valid instant", p.epoch_s))?;
        by_day
            .entry((p.epoch_s + ICT_OFFSET_SECONDS).div_euclid(86_400))
            .or_default()
            .push(EngineTick {
                timestamp: EventTimestamp(p.epoch_s),
                datetime,
                price: Price::from_f64(price)
                    .map_err(|e| format!("row {idx}: bad price {price}: {e}"))?,
                volume: Volume(0),
                side: None,
                last_trade_side: None,
            });
    }
    Ok(by_day)
}

/// Build one session's TPO profile and return its observation and price levels.
///
/// POC/VAH/VAL are read from `ProfileSummary` by field, since implementations differ in tuple
/// order (`(poc, vah, val)` vs `(poc, val, vah)`); `val <= poc <= vah` is then checked.
fn session_observation(
    ticks: &[EngineTick],
) -> Result<(ProfileObservation, Vec<ProfileLevel>), String> {
    let first = ticks.first().ok_or("empty session")?;
    let session = first.datetime.format("%Y-%m-%d").to_string();
    let summary = build_tpo_profile_from_ticks(
        ticks,
        Price(TICK_SIZE_SCALED),
        PRICE_BUCKET_TICK,
        VA_PCT,
        TPO_PERIOD_MIN,
    )
    .map_err(|e| format!("{session}: build_tpo_profile_from_ticks: {e}"))?;

    let poc_tick = to_tick(summary.poc.ok_or_else(|| format!("{session}: no POC"))?)?;
    let vah_tick = to_tick(summary.vah.ok_or_else(|| format!("{session}: no VAH"))?)?;
    let val_tick = to_tick(summary.val.ok_or_else(|| format!("{session}: no VAL"))?)?;
    if !(val_tick <= poc_tick && poc_tick <= vah_tick) {
        return Err(format!(
            "{session}: value area is not val <= poc <= vah (val={val_tick} poc={poc_tick} \
             vah={vah_tick}). The most likely cause is a VAH/VAL swap: \
             market-profile-engine returns (poc, vah, val) while three other \
             implementations return (poc, val, vah)."
        ));
    }

    let mut levels = Vec::with_capacity(summary.rows.len());
    let mut tpo_total: i64 = 0;
    for row in &summary.rows {
        let count = i64::try_from(row.tpo_count).map_err(|_| "tpo_count does not fit in i64")?;
        tpo_total += count;
        levels.push(ProfileLevel {
            session: session.clone(),
            price_tick: to_tick(row.price)?,
            tpo_count: count,
        });
    }

    Ok((
        ProfileObservation {
            session,
            prints: ticks.len(),
            levels: summary.rows.len(),
            tpo_total,
            poc_tick,
            val_tick,
            vah_tick,
            va_width_ticks: vah_tick - val_tick,
        },
        levels,
    ))
}

fn compute_profile(
    prints: &[Print],
) -> Result<(Vec<ProfileObservation>, Vec<ProfileLevel>), String> {
    let mut observations = Vec::new();
    let mut levels = Vec::new();
    for ticks in build_sessions(prints)?.values() {
        let (observation, mut session_levels) = session_observation(ticks)?;
        observations.push(observation);
        levels.append(&mut session_levels);
    }
    Ok((observations, levels))
}

impl std::fmt::Debug for ProfileObservation {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{} poc={} val={} vah={} width={}",
            self.session, self.poc_tick, self.val_tick, self.vah_tick, self.va_width_ticks
        )
    }
}

fn write_observations(sink: &mut dyn Write, rows: &[ProfileObservation]) -> std::io::Result<()> {
    writeln!(
        sink,
        "session\tprints\tlevels\ttpo_total\tpoc_tick\tval_tick\tvah_tick\tva_width_ticks"
    )?;
    for r in rows {
        writeln!(
            sink,
            "{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
            r.session,
            r.prints,
            r.levels,
            r.tpo_total,
            r.poc_tick,
            r.val_tick,
            r.vah_tick,
            r.va_width_ticks
        )?;
    }
    Ok(())
}

fn write_profile(sink: &mut dyn Write, rows: &[ProfileLevel]) -> std::io::Result<()> {
    writeln!(sink, "session\tprice_tick\ttpo_count")?;
    for r in rows {
        writeln!(sink, "{}\t{}\t{}", r.session, r.price_tick, r.tpo_count)?;
    }
    Ok(())
}

// --------------------------------------------------------------------------- tests

#[cfg(test)]
mod tests {
    use super::*;

    fn p(ts: &str, volume: f64, side: &str) -> Print {
        Print {
            epoch_s: ict_string_to_epoch(ts).unwrap(),
            price: None,
            volume,
            aggressor: side.to_string(),
        }
    }

    fn q(ts: &str, price: f64) -> Print {
        Print {
            epoch_s: ict_string_to_epoch(ts).unwrap(),
            price: Some(price),
            volume: f64::NAN,
            aggressor: String::new(),
        }
    }

    #[test]
    fn epoch_conversion_matches_known_instants() {
        // 1970-01-01 07:00:00 ICT is the Unix epoch.
        assert_eq!(ict_string_to_epoch("1970-01-01 07:00:00").unwrap(), 0);
        // Leap-year handling on a March boundary of a century year.
        assert_eq!(
            ict_string_to_epoch("2000-03-01 07:00:00").unwrap()
                - ict_string_to_epoch("2000-02-28 07:00:00").unwrap(),
            2 * 86_400
        );
        assert_eq!(
            ict_string_to_epoch("2026-08-06 09:00:00").unwrap(),
            1_785_981_600
        );
        // Milliseconds are truncated, not rejected.
        assert_eq!(
            ict_string_to_epoch("2026-08-06 09:00:00.000").unwrap(),
            ict_string_to_epoch("2026-08-06 09:00:00").unwrap()
        );
    }

    #[test]
    fn unknown_aggressor_contributes_zero() {
        let prints = vec![
            p("2026-08-06 09:00:00", 5.0, "B"),
            p("2026-08-06 09:00:01", 7.0, "U"),
            p("2026-08-06 09:00:02", 2.0, "S"),
        ];
        let rows = compute(Feature::SessionRollover, &prints).unwrap();
        assert_eq!(
            rows.iter().map(|r| r.cvd).collect::<Vec<_>>(),
            vec![5, 5, 3]
        );
    }

    #[test]
    fn lunch_rollover_preserves_the_level() {
        let prints = vec![
            p("2026-08-06 11:29:59", 4.0, "B"),
            p("2026-08-06 13:00:00", 1.0, "S"),
        ];
        let rows = compute(Feature::SessionRollover, &prints).unwrap();
        assert_eq!(rows.iter().map(|r| r.cvd).collect::<Vec<_>>(), vec![4, 3]);
    }

    #[test]
    fn day_boundary_resets_to_zero_but_no_reset_variant_does_not() {
        let prints = vec![
            p("2026-08-06 14:45:00", 9.0, "B"),
            p("2026-08-07 09:00:00", 2.0, "B"),
        ];
        assert_eq!(
            compute(Feature::SessionRollover, &prints)
                .unwrap()
                .iter()
                .map(|r| r.cvd)
                .collect::<Vec<_>>(),
            vec![9, 2]
        );
        assert_eq!(
            compute(Feature::NoReset, &prints)
                .unwrap()
                .iter()
                .map(|r| r.cvd)
                .collect::<Vec<_>>(),
            vec![9, 11]
        );
    }

    #[test]
    fn a_second_lunch_crossing_in_the_same_day_does_not_fire_twice() {
        // rollover keeps the level, so this pins the watermark logic rather than the value
        let prints = vec![
            p("2026-08-06 12:59:59", 4.0, "B"),
            p("2026-08-06 13:00:00", 1.0, "B"),
            p("2026-08-06 13:00:01", 1.0, "B"),
        ];
        let rows = compute(Feature::SessionRollover, &prints).unwrap();
        assert_eq!(
            rows.iter().map(|r| r.cvd).collect::<Vec<_>>(),
            vec![4, 5, 6]
        );
    }

    #[test]
    fn non_integral_cvd_is_refused_rather_than_rounded() {
        let prints = vec![p("2026-08-06 09:00:00", 1.5, "B")];
        assert!(compute(Feature::SessionRollover, &prints).is_err());
    }

    #[test]
    fn csv_splitter_handles_quotes_and_crlf() {
        assert_eq!(
            split_csv("\"2026-08-06 09:00:00\",1913.8,1,\"S\"\r"),
            vec!["2026-08-06 09:00:00", "1913.8", "1", "S"]
        );
    }

    #[test]
    fn feature_names_are_the_registry_names() {
        assert_eq!(
            Feature::parse("tape_cvd_lot_v1"),
            Some(Feature::SessionRollover)
        );
        assert_eq!(
            Feature::parse("tape_cvd_norst_lot_v1"),
            Some(Feature::NoReset)
        );
        assert_eq!(
            Feature::parse("prof_tpo_poc_pts_v1"),
            Some(Feature::ProfTpo)
        );
        assert_eq!(Feature::parse("cvd"), None);
        assert_eq!(Feature::parse("tpo"), None);
        assert_eq!(Feature::parse("prof_vpoc_pts_v1"), None);
    }

    // --------------------------------------------------------------- profile

    /// TPO counts from 1900.0 up are 1, 1, 2, 1, so the POC is 1900.2. The 3.5-TPO target
    /// expands the band twice and equal-weight ties take the lower side (`va_tiebreak: lower`),
    /// giving VAL = 1900.0 and VAH = POC.
    #[test]
    fn a_hand_checkable_session_reduces_to_the_documented_value_area() {
        let prints = vec![
            q("2026-07-09 09:00:00", 1900.0),
            q("2026-07-09 09:10:00", 1900.1),
            q("2026-07-09 09:20:00", 1900.2),
            q("2026-07-09 09:40:00", 1900.2),
            q("2026-07-09 09:50:00", 1900.3),
        ];
        let (observations, levels) = compute_profile(&prints).unwrap();
        assert_eq!(observations.len(), 1);
        let o = &observations[0];
        assert_eq!(o.session, "2026-07-09");
        assert_eq!((o.levels, o.tpo_total), (4, 5));
        assert_eq!(o.poc_tick, 19002);
        assert_eq!(o.val_tick, 19000);
        assert_eq!(o.vah_tick, 19002);
        assert_eq!(o.va_width_ticks, 2);
        assert_eq!(
            levels
                .iter()
                .map(|l| (l.price_tick, l.tpo_count))
                .collect::<Vec<_>>(),
            vec![(19000, 1), (19001, 1), (19002, 2), (19003, 1)]
        );
    }

    /// A TPO counts periods, not prints: ten prints in one half-hour are one TPO.
    #[test]
    fn repeats_inside_one_period_count_once() {
        let many: Vec<Print> = (0..10)
            .map(|i| q(&format!("2026-07-09 09:{:02}:00", i), 1900.0))
            .collect();
        let (observations, levels) = compute_profile(&many).unwrap();
        assert_eq!(observations[0].tpo_total, 1);
        assert_eq!(levels[0].tpo_count, 1);
    }

    #[test]
    fn sessions_split_on_the_ict_day_and_not_on_lunch() {
        let prints = vec![
            q("2026-07-09 09:00:00", 1900.0),
            q("2026-07-09 13:30:00", 1900.0),
            q("2026-07-10 09:00:00", 1901.0),
        ];
        let (observations, _) = compute_profile(&prints).unwrap();
        assert_eq!(
            observations
                .iter()
                .map(|o| (o.session.as_str(), o.prints, o.tpo_total))
                .collect::<Vec<_>>(),
            vec![("2026-07-09", 2, 2), ("2026-07-10", 1, 1)]
        );
    }

    /// Guards against a VAH/VAL swap.
    #[test]
    fn every_emitted_session_satisfies_val_le_poc_le_vah() {
        let prints: Vec<Print> = (0..40)
            .map(|i| {
                q(
                    &format!("2026-07-09 {:02}:{:02}:00", 9 + i / 12, (i % 12) * 5),
                    1900.0 + f64::from(i % 7) * 0.1,
                )
            })
            .collect();
        let (observations, _) = compute_profile(&prints).unwrap();
        assert!(!observations.is_empty());
        for o in &observations {
            assert!(
                o.val_tick <= o.poc_tick && o.poc_tick <= o.vah_tick,
                "{o:?}"
            );
            assert_eq!(o.va_width_ticks, o.vah_tick - o.val_tick);
        }
    }

    /// A CVD tape (no `price` column) is refused by the profile feature.
    #[test]
    fn each_feature_declares_the_columns_it_reads() {
        assert_eq!(
            Feature::SessionRollover.required_columns(),
            &["volume", "aggressor"]
        );
        assert_eq!(Feature::ProfTpo.required_columns(), &["price"]);
        assert!(compute_profile(&[p("2026-07-09 09:00:00", 1.0, "B")]).is_err());
    }

    #[test]
    fn a_price_off_the_tick_grid_is_refused_rather_than_rounded() {
        assert!(to_tick(Price(19_000_500)).is_err());
        assert_eq!(to_tick(Price(19_000_000)).unwrap(), 19_000);
    }
}
