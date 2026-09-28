//! VN30F1M session clock in ICT (UTC+7): trading hours 09:00-11:30 and 13:00-14:45, session
//! keys, the initial-balance window and tick-time parsing. There is no holiday calendar.

#![deny(clippy::correctness)]
#![warn(clippy::suspicious, clippy::style, clippy::complexity, clippy::perf)]

use chrono::{DateTime, FixedOffset, NaiveDate, NaiveTime, TimeZone, Utc};
use market_types::{EventTimestamp, ParseError, SessionKey};
use thiserror::Error;

pub const VN30F_UTC_OFFSET_HOURS: i32 = 7;
pub const TIMESTAMP_CONSISTENCY_TOLERANCE_SECONDS: i64 = 1;
pub const VN30F_MARKET_OPEN_HOUR: u32 = 9;
pub const VN30F_MARKET_OPEN_MINUTE: u32 = 0;
pub const VN30F_LUNCH_START_HOUR: u32 = 11;
pub const VN30F_LUNCH_START_MINUTE: u32 = 30;
pub const VN30F_LUNCH_END_HOUR: u32 = 13;
pub const VN30F_LUNCH_END_MINUTE: u32 = 0;
pub const VN30F_MARKET_CLOSE_HOUR: u32 = 14;
pub const VN30F_MARKET_CLOSE_MINUTE: u32 = 45;
/// End of the morning initial balance, exclusive: 10:00 ICT (Dalton's one-hour IB).
///
/// Single prints, excess and poor highs and lows are defined relative to the IB; take its length
/// from [`vn30f_ib_window_seconds`] rather than hardcoding it.
pub const VN30F_IB_END_HOUR: u32 = 10;
pub const VN30F_IB_END_MINUTE: u32 = 0;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum TimeError {
    #[error("timezone offset construction failed")]
    InvalidOffset,
    #[error("datetime value must include timezone information")]
    MissingTimezone,
    #[error("datetime and timestamp values are contradictory")]
    ContradictoryTickTime,
    #[error("tick must include either datetime or timestamp")]
    MissingTickTime,
    #[error("datetime string is invalid")]
    InvalidDateTime,
    #[error("date construction failed")]
    InvalidDate,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TickTimeInput {
    pub datetime_iso: Option<String>,
    pub timestamp: Option<EventTimestamp>,
}

pub fn vn30f_timezone() -> Result<FixedOffset, TimeError> {
    FixedOffset::east_opt(VN30F_UTC_OFFSET_HOURS * 3600).ok_or(TimeError::InvalidOffset)
}

pub fn parse_vn30f_datetime(value: &str) -> Result<DateTime<FixedOffset>, TimeError> {
    let parsed = DateTime::parse_from_rfc3339(value).map_err(|_| TimeError::InvalidDateTime)?;
    let tz = vn30f_timezone()?;
    Ok(parsed.with_timezone(&tz))
}

pub fn extract_tick_datetime(input: &TickTimeInput) -> Result<DateTime<FixedOffset>, TimeError> {
    let dt_from_datetime = input
        .datetime_iso
        .as_deref()
        .map(parse_vn30f_datetime)
        .transpose()?;

    let dt_from_timestamp = input
        .timestamp
        .map(|timestamp| {
            let utc = Utc
                .timestamp_opt(timestamp.0, 0)
                .single()
                .ok_or(TimeError::InvalidDateTime)?;
            let tz = vn30f_timezone()?;
            Ok::<DateTime<FixedOffset>, TimeError>(utc.with_timezone(&tz))
        })
        .transpose()?;

    match (dt_from_datetime, dt_from_timestamp) {
        (Some(dt), Some(ts_dt)) => {
            let diff = (dt - ts_dt).num_seconds().abs();
            if diff > TIMESTAMP_CONSISTENCY_TOLERANCE_SECONDS {
                return Err(TimeError::ContradictoryTickTime);
            }
            Ok(dt)
        }
        (Some(dt), None) => Ok(dt),
        (None, Some(ts_dt)) => Ok(ts_dt),
        (None, None) => Err(TimeError::MissingTickTime),
    }
}

pub fn intraday_session_key(value: DateTime<FixedOffset>) -> Result<SessionKey, ParseError> {
    SessionKey::new(value.date_naive().format("%Y-%m-%d").to_string())
}

pub fn intraday_session_bounds(
    value: DateTime<FixedOffset>,
) -> Result<(DateTime<FixedOffset>, DateTime<FixedOffset>), TimeError> {
    let date = value.date_naive();
    let tz = vn30f_timezone()?;
    let start = local_datetime(date, VN30F_MARKET_OPEN_HOUR, VN30F_MARKET_OPEN_MINUTE, &tz)?;
    let end = local_datetime(
        date,
        VN30F_MARKET_CLOSE_HOUR,
        VN30F_MARKET_CLOSE_MINUTE,
        &tz,
    )?;
    Ok((start, end))
}

pub fn is_vn30f_trading_time(value: DateTime<FixedOffset>) -> bool {
    let current = value.time();
    let morning_start = market_time(VN30F_MARKET_OPEN_HOUR, VN30F_MARKET_OPEN_MINUTE);
    let lunch_start = market_time(VN30F_LUNCH_START_HOUR, VN30F_LUNCH_START_MINUTE);
    let lunch_end = market_time(VN30F_LUNCH_END_HOUR, VN30F_LUNCH_END_MINUTE);
    let close = market_time(VN30F_MARKET_CLOSE_HOUR, VN30F_MARKET_CLOSE_MINUTE);
    (current >= morning_start && current < lunch_start)
        || (current >= lunch_end && current <= close)
}

/// Length of the morning initial balance in seconds, from the session open to the IB end
/// ([`VN30F_IB_END_HOUR`], [`VN30F_IB_END_MINUTE`]).
pub const fn vn30f_ib_window_seconds() -> i64 {
    let open = (VN30F_MARKET_OPEN_HOUR as i64) * 3600 + (VN30F_MARKET_OPEN_MINUTE as i64) * 60;
    let end = (VN30F_IB_END_HOUR as i64) * 3600 + (VN30F_IB_END_MINUTE as i64) * 60;
    end - open
}

pub fn is_morning_initial_balance_time(value: DateTime<FixedOffset>) -> bool {
    let current = value.time();
    let start = market_time(VN30F_MARKET_OPEN_HOUR, VN30F_MARKET_OPEN_MINUTE);
    let ib_end = market_time(VN30F_IB_END_HOUR, VN30F_IB_END_MINUTE);
    current >= start && current < ib_end && is_vn30f_trading_time(value)
}

fn local_datetime(
    date: NaiveDate,
    hour: u32,
    minute: u32,
    tz: &FixedOffset,
) -> Result<DateTime<FixedOffset>, TimeError> {
    let naive = date
        .and_hms_opt(hour, minute, 0)
        .ok_or(TimeError::InvalidDate)?;
    tz.from_local_datetime(&naive)
        .single()
        .ok_or(TimeError::InvalidDate)
}

fn market_time(hour: u32, minute: u32) -> NaiveTime {
    NaiveTime::from_hms_opt(hour, minute, 0).expect("market_time called with invalid hour/minute")
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Timelike;
    use market_types::EventTimestamp;

    #[test]
    fn extract_tick_datetime_accepts_consistent_datetime_and_timestamp() {
        let input = TickTimeInput {
            datetime_iso: Some("2026-04-17T09:00:00+07:00".to_string()),
            timestamp: Some(EventTimestamp::new(1_776_391_200).expect("valid timestamp")),
        };
        let dt = extract_tick_datetime(&input).expect("tick time should parse");
        assert_eq!(dt.hour(), 9);
        assert_eq!(dt.minute(), 0);
    }

    #[test]
    fn initial_balance_is_dalton_one_hour_0900_to_1000() {
        // Dalton's one-hour IB, the definition the research uses
        assert_eq!(vn30f_ib_window_seconds(), 3600);

        let inside = parse_vn30f_datetime("2026-04-17T09:59:59+07:00").expect("valid datetime");
        assert!(
            is_morning_initial_balance_time(inside),
            "09:59:59 must still be inside the initial balance"
        );

        let boundary = parse_vn30f_datetime("2026-04-17T10:00:00+07:00").expect("valid datetime");
        assert!(
            !is_morning_initial_balance_time(boundary),
            "the IB end is exclusive: 10:00:00 is outside"
        );

        let old_boundary = parse_vn30f_datetime("2026-04-17T09:30:00+07:00").expect("valid");
        assert!(
            is_morning_initial_balance_time(old_boundary),
            "09:30 is INSIDE the IB now; it was the end boundary before decision D"
        );
    }

    #[test]
    fn trading_time_respects_lunch_break() {
        let dt = parse_vn30f_datetime("2026-04-17T12:00:00+07:00").expect("valid datetime");
        assert!(!is_vn30f_trading_time(dt));
    }
}
