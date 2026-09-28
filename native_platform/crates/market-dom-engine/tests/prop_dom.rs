//! Characterization and property tests for the stateless DOM snapshot pipeline and the batch
//! tick-to-quote matching.
//!
//! The tests freeze current behaviour; `finding_*` tests record behaviour a caller may not expect.

use market_dom_engine::{
    build_dom_batch, build_dom_snapshot, build_dom_snapshot_from_raw_quote,
    build_dom_snapshot_from_source_payload, build_raw_quote_from_source_payload,
    match_ticks_to_quotes, DomError, DomSourceQuotePayload,
};
use market_types::{
    EventTimestamp, LastTrade, ParseError, Price, PriceLevel, QuoteSnapshot, RawQuoteSnapshot,
    TickSide, Volume,
};
use proptest::prelude::*;

/// `Price` units per 0.1 tick (the scale is 10_000); test prices are integer tenths
/// (12_514 is 1251.4), so every comparison is exact.
const TICK_UNITS: i64 = 1_000;

fn price(tenths: i64) -> Price {
    Price(tenths * TICK_UNITS)
}

fn level(tenths: i64, volume: i64) -> PriceLevel {
    PriceLevel {
        price: price(tenths),
        volume: Volume::new(volume).expect("test volume"),
    }
}

fn trade(tenths: i64, volume: i64, side: TickSide, timestamp: i64) -> LastTrade {
    LastTrade {
        price: price(tenths),
        volume: Volume::new(volume).expect("test volume"),
        side,
        timestamp: EventTimestamp(timestamp),
    }
}

fn quote(timestamp: i64, bids: Vec<PriceLevel>, asks: Vec<PriceLevel>) -> QuoteSnapshot {
    QuoteSnapshot {
        symbol: market_types::Symbol::new("VN30F1M").expect("symbol"),
        timestamp: EventTimestamp(timestamp),
        total_bid_qty: Volume::new(bids.iter().map(|l| l.volume.0).sum()).expect("bid total"),
        total_offer_qty: Volume::new(asks.iter().map(|l| l.volume.0).sum()).expect("ask total"),
        bid_levels: bids,
        offer_levels: asks,
        last_trade_price: None,
        last_trade: None,
    }
}

fn base_raw(timestamp: i64) -> RawQuoteSnapshot {
    RawQuoteSnapshot {
        symbol: "VN30F1M".to_string(),
        timestamp,
        datetime: None,
        total_bid_qty: 300,
        total_offer_qty: 330,
        bid_prices: vec![1251.4, 1251.3],
        bid_volumes: vec![100, 200],
        offer_prices: vec![1251.5, 1251.6],
        offer_volumes: vec![110, 220],
        last_trade_price: 1251.4,
        last_trade_volume: 5,
        last_trade_side: "B".to_string(),
        last_trade_timestamp: timestamp,
    }
}

/// Unordered `(price tenths, volume)` levels, so disordered and crossed books occur.
fn arb_levels() -> impl Strategy<Value = Vec<(i64, i64)>> {
    prop::collection::vec((9_000i64..14_000, 0i64..5_000), 0..12)
}

fn grid_price() -> impl Strategy<Value = f64> {
    (9_000i64..14_000).prop_map(|tenths| tenths as f64 / 10.0)
}

fn to_levels(specs: &[(i64, i64)]) -> Vec<PriceLevel> {
    specs.iter().map(|&(p, v)| level(p, v)).collect()
}

fn non_finite() -> impl Strategy<Value = f64> {
    prop_oneof![Just(f64::NAN), Just(f64::INFINITY), Just(f64::NEG_INFINITY)]
}

proptest! {
    /// Disordered or crossed books are kept as given; only the `malformed` flag is raised.
    #[test]
    fn malformed_flag_matches_disorder_or_cross_oracle(
        bids in arb_levels(),
        asks in arb_levels(),
    ) {
        let bid_levels = to_levels(&bids);
        let ask_levels = to_levels(&asks);
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(1_000, bid_levels.clone(), ask_levels.clone()),
            None,
        )
        .expect("snapshot");

        // strict comparisons: equal adjacent prices are not flagged
        let bids_disordered = bid_levels.windows(2).any(|w| w[0].price < w[1].price);
        let asks_disordered = ask_levels.windows(2).any(|w| w[0].price > w[1].price);
        // crossed compares the first levels with a strict `>`, so a locked book is not malformed
        let crossed = matches!(
            (bid_levels.first(), ask_levels.first()),
            (Some(b), Some(a)) if b.price > a.price
        );

        prop_assert_eq!(
            snapshot.malformed.0,
            bids_disordered || asks_disordered || crossed
        );
    }

    /// Levels pass through unchanged (no dedup, sorting or zero-volume filter); the best bid and
    /// ask are the first level of each side.
    #[test]
    fn levels_pass_through_verbatim_and_best_is_first_level(
        bids in arb_levels(),
        asks in arb_levels(),
    ) {
        let bid_levels = to_levels(&bids);
        let ask_levels = to_levels(&asks);
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(1_000, bid_levels.clone(), ask_levels.clone()),
            None,
        )
        .expect("snapshot");

        prop_assert_eq!(&snapshot.bids, &bid_levels);
        prop_assert_eq!(&snapshot.asks, &ask_levels);
        prop_assert_eq!(snapshot.best_bid, bid_levels.first().map(|l| l.price));
        prop_assert_eq!(snapshot.best_ask, ask_levels.first().map(|l| l.price));
    }

    /// Spread is the exact `best_ask - best_bid` when both exist, negative if crossed; else `None`.
    #[test]
    fn spread_is_exact_ask_minus_bid_or_none(
        bids in arb_levels(),
        asks in arb_levels(),
    ) {
        let snapshot = build_dom_snapshot(
            "VN30F1M",
            &quote(1_000, to_levels(&bids), to_levels(&asks)),
            None,
        )
        .expect("snapshot");

        match (snapshot.best_bid, snapshot.best_ask) {
            (Some(bid), Some(ask)) => {
                prop_assert_eq!(snapshot.spread, Some(Price(ask.0 - bid.0)));
            }
            _ => prop_assert_eq!(snapshot.spread, None),
        }
    }

    #[test]
    fn snapshot_building_is_deterministic(
        bids in arb_levels(),
        asks in arb_levels(),
        tick_tenths in 9_000i64..14_000,
        tick_ts in 0i64..100_000,
    ) {
        let q = quote(1_000, to_levels(&bids), to_levels(&asks));
        let tick = Some(trade(tick_tenths, 7, TickSide::Buy, tick_ts));
        let first = build_dom_snapshot("VN30F1M", &q, tick.clone()).expect("snapshot");
        let second = build_dom_snapshot("VN30F1M", &q, tick).expect("snapshot");
        prop_assert_eq!(first, second);
    }

    /// Ingress zips prices with volumes (shorter wins), caps each side at 20, keeps values exact.
    #[test]
    fn ingress_depth_capped_at_20_and_zipped_to_shortest(
        bid_prices in prop::collection::vec(grid_price(), 0..30),
        bid_volumes in prop::collection::vec(0i64..5_000, 0..30),
        offer_prices in prop::collection::vec(grid_price(), 0..30),
        offer_volumes in prop::collection::vec(0i64..5_000, 0..30),
    ) {
        let expected_bids = bid_prices.len().min(bid_volumes.len()).min(20);
        let expected_asks = offer_prices.len().min(offer_volumes.len()).min(20);

        let mut raw = base_raw(1_000);
        raw.bid_prices = bid_prices.clone();
        raw.bid_volumes = bid_volumes.clone();
        raw.offer_prices = offer_prices.clone();
        raw.offer_volumes = offer_volumes.clone();

        let snapshot =
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None).expect("snapshot");

        prop_assert_eq!(snapshot.bids.len(), expected_bids);
        prop_assert_eq!(snapshot.asks.len(), expected_asks);
        for (lvl, (p, v)) in snapshot.bids.iter().zip(bid_prices.iter().zip(&bid_volumes)) {
            prop_assert_eq!(lvl.price, Price::from_f64(*p).expect("grid price"));
            prop_assert_eq!(lvl.volume.0, *v);
        }
        for (lvl, (p, v)) in snapshot.asks.iter().zip(offer_prices.iter().zip(&offer_volumes)) {
            prop_assert_eq!(lvl.price, Price::from_f64(*p).expect("grid price"));
            prop_assert_eq!(lvl.volume.0, *v);
        }
    }

    /// A non-finite level price fails the whole snapshot; a non-finite trade price is only dropped.
    #[test]
    fn nonfinite_level_price_is_rejected(
        (len, idx) in (1usize..8).prop_flat_map(|len| (Just(len), 0..len)),
        bad in non_finite(),
        poison_bids in any::<bool>(),
    ) {
        let mut raw = base_raw(1_000);
        let prices = vec![1251.0; len];
        let volumes = vec![10i64; len];
        if poison_bids {
            raw.bid_prices = prices;
            raw.bid_volumes = volumes;
            raw.bid_prices[idx] = bad;
        } else {
            raw.offer_prices = prices;
            raw.offer_volumes = volumes;
            raw.offer_prices[idx] = bad;
        }

        prop_assert_eq!(
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None),
            Err(DomError::Parse(ParseError::NonFinitePrice))
        );
    }

    #[test]
    fn negative_volume_or_timestamp_is_rejected(neg in i64::MIN..0) {
        let mut raw = base_raw(1_000);
        raw.timestamp = neg;
        prop_assert_eq!(
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None),
            Err(DomError::Parse(ParseError::NegativeTimestamp))
        );

        let mut raw = base_raw(1_000);
        raw.bid_volumes[0] = neg;
        prop_assert_eq!(
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None),
            Err(DomError::Parse(ParseError::NegativeVolume))
        );

        let mut raw = base_raw(1_000);
        raw.total_offer_qty = neg;
        prop_assert_eq!(
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None),
            Err(DomError::Parse(ParseError::NegativeVolume))
        );
    }

    /// Invalid last-trade fields drop the trade but never fail or flag the snapshot.
    #[test]
    fn invalid_trade_fields_drop_trade_silently(
        bad_price in non_finite(),
        // no B or S, so it never normalises to a valid side
        bad_side in "[ACDEFGHIJKLMNOPQRTUVWXYZ]{1,3}",
        neg in i64::MIN..0,
    ) {
        // a non-finite trade price drops both the price and the trade, unlike a level price
        let mut raw = base_raw(1_000);
        raw.last_trade_price = bad_price;
        let snapshot =
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None).expect("snapshot");
        prop_assert_eq!(snapshot.last_trade_price, None);
        prop_assert_eq!(&snapshot.last_trade, &None);
        prop_assert!(!snapshot.malformed.0);

        // unrecognised side: trade dropped, price kept
        let mut raw = base_raw(1_000);
        raw.last_trade_side = bad_side;
        let snapshot =
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None).expect("snapshot");
        prop_assert_eq!(&snapshot.last_trade, &None);
        prop_assert_eq!(snapshot.last_trade_price, Some(price(12_514)));
        prop_assert!(!snapshot.malformed.0);

        // a negative trade volume is dropped silently, unlike a negative level volume
        let mut raw = base_raw(1_000);
        raw.last_trade_volume = neg;
        let snapshot =
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None).expect("snapshot");
        prop_assert_eq!(&snapshot.last_trade, &None);
        prop_assert_eq!(snapshot.last_trade_price, Some(price(12_514)));
        prop_assert!(!snapshot.malformed.0);

        // a negative trade timestamp is also dropped silently
        let mut raw = base_raw(1_000);
        raw.last_trade_timestamp = neg;
        let snapshot =
            build_dom_snapshot_from_raw_quote("VN30F1M", raw, None).expect("snapshot");
        prop_assert_eq!(&snapshot.last_trade, &None);
        prop_assert!(!snapshot.malformed.0);
    }

    /// `latest_tick` always replaces the quote's trade and price, without a timestamp check.
    #[test]
    fn latest_tick_always_overrides_quote_trade(
        tick_tenths in 9_000i64..14_000,
        tick_vol in 0i64..5_000,
        tick_ts in 0i64..100_000,
        buy in any::<bool>(),
    ) {
        let mut q = quote(1_000, vec![level(12_514, 100)], vec![level(12_515, 110)]);
        q.last_trade_price = Some(price(12_513));
        q.last_trade = Some(trade(12_513, 5, TickSide::Buy, 999));

        let side = if buy { TickSide::Buy } else { TickSide::Sell };
        let tick = trade(tick_tenths, tick_vol, side, tick_ts);
        let snapshot =
            build_dom_snapshot("VN30F1M", &q, Some(tick.clone())).expect("snapshot");

        prop_assert_eq!(snapshot.last_trade_price, Some(tick.price));
        prop_assert_eq!(snapshot.last_trade, Some(tick));
    }

    /// Each quote gets the latest tick at or before it (the last one on ties, as the sort is
    /// stable), else the `latest_tick` fallback whatever its timestamp.
    #[test]
    fn tick_matching_matches_last_at_or_before_oracle(
        quote_ts in prop::collection::vec(0i64..50, 1..10),
        tick_specs in prop::collection::vec((0i64..50, 12_000i64..12_100), 0..10),
        latest_ts in prop::option::of(0i64..50),
    ) {
        let quotes: Vec<_> = quote_ts
            .iter()
            .map(|&ts| quote(ts, vec![level(12_514, 10)], vec![level(12_515, 10)]))
            .collect();
        // unique volumes tell equal-timestamp ticks apart
        let ticks: Vec<_> = tick_specs
            .iter()
            .enumerate()
            .map(|(i, &(ts, p))| trade(p, i as i64 + 1, TickSide::Buy, ts))
            .collect();
        let latest = latest_ts.map(|ts| trade(12_500, 999, TickSide::Sell, ts));

        let matched = match_ticks_to_quotes(&quotes, &ticks, latest.clone());
        prop_assert_eq!(matched.len(), quotes.len());

        for (q, actual) in quotes.iter().zip(&matched) {
            let mut expected: Option<&LastTrade> = None;
            for t in &ticks {
                if t.timestamp.0 <= q.timestamp.0 {
                    let replace = match expected {
                        Some(e) => t.timestamp.0 >= e.timestamp.0,
                        None => true,
                    };
                    if replace {
                        expected = Some(t);
                    }
                }
            }
            let expected = expected.cloned().or_else(|| latest.clone());
            prop_assert_eq!(actual, &expected);
        }
    }

    /// The batch equals `match_ticks_to_quotes` then `build_dom_snapshot` per quote, in order.
    #[test]
    fn batch_equals_matching_plus_individual_snapshots(
        quote_ts in prop::collection::vec(0i64..50, 1..8),
        tick_specs in prop::collection::vec((0i64..50, 12_000i64..12_100), 0..8),
        latest_ts in prop::option::of(0i64..50),
    ) {
        let quotes: Vec<_> = quote_ts
            .iter()
            .map(|&ts| quote(ts, vec![level(12_514, 10)], vec![level(12_515, 10)]))
            .collect();
        let ticks: Vec<_> = tick_specs
            .iter()
            .enumerate()
            .map(|(i, &(ts, p))| trade(p, i as i64 + 1, TickSide::Sell, ts))
            .collect();
        let latest = latest_ts.map(|ts| trade(12_500, 999, TickSide::Buy, ts));

        let matched = match_ticks_to_quotes(&quotes, &ticks, latest.clone());
        let manual: Vec<_> = quotes
            .iter()
            .zip(matched)
            .map(|(q, tick)| build_dom_snapshot("VN30F1M", q, tick).expect("snapshot"))
            .collect();
        let batch = build_dom_batch("VN30F1M", &quotes, &ticks, latest).expect("batch");

        prop_assert_eq!(&batch, &manual);
        for (snapshot, q) in batch.iter().zip(&quotes) {
            prop_assert_eq!(snapshot.timestamp, Some(q.timestamp));
        }
    }

    /// A `DomSourceQuotePayload` builds the same snapshot as its `RawQuoteSnapshot`; a missing
    /// `last_trade_timestamp` defaults to the quote timestamp.
    #[test]
    fn source_payload_route_equals_raw_quote_route(
        timestamp in 0i64..100_000,
        bid_prices in prop::collection::vec(grid_price(), 0..25),
        bid_volumes in prop::collection::vec(0i64..5_000, 0..25),
        offer_prices in prop::collection::vec(grid_price(), 0..25),
        offer_volumes in prop::collection::vec(0i64..5_000, 0..25),
        last_trade_price in grid_price(),
        last_trade_volume in 0i64..1_000,
        last_trade_timestamp in prop::option::of(0i64..100_000),
    ) {
        let payload = DomSourceQuotePayload {
            symbol: "VN30F1M".to_string(),
            timestamp,
            datetime: None,
            total_bid_qty: bid_volumes.iter().sum(),
            total_offer_qty: offer_volumes.iter().sum(),
            bid_prices,
            bid_volumes,
            offer_prices,
            offer_volumes,
            last_trade_price,
            last_trade_volume,
            last_trade_side: "B".to_string(),
            last_trade_timestamp,
        };

        let raw = build_raw_quote_from_source_payload(payload.clone());
        prop_assert_eq!(
            raw.last_trade_timestamp,
            payload.last_trade_timestamp.unwrap_or(payload.timestamp)
        );

        let via_raw = build_dom_snapshot_from_raw_quote("VN30F1M", raw, None);
        let via_source = build_dom_snapshot_from_source_payload("VN30F1M", payload, None);
        prop_assert_eq!(via_source, via_raw);
    }

    #[test]
    fn symbol_argument_is_validated_but_not_trimmed(
        symbol in "[ ]{0,2}[A-Z0-9]{0,5}[ ]{0,2}",
    ) {
        let result = build_dom_snapshot(
            &symbol,
            &quote(1_000, vec![level(12_514, 100)], vec![level(12_515, 110)]),
            None,
        );
        if symbol.trim().is_empty() {
            prop_assert_eq!(result, Err(DomError::EmptySymbol));
        } else {
            // unlike `Symbol::new`, the snapshot keeps the symbol untrimmed
            prop_assert_eq!(result.expect("snapshot").symbol, symbol);
        }
    }
}

#[test]
fn zero_volume_levels_are_kept_not_removed() {
    let snapshot = build_dom_snapshot(
        "VN30F1M",
        &quote(
            1_000,
            vec![level(12_514, 0), level(12_513, 200)],
            vec![level(12_515, 0)],
        ),
        None,
    )
    .expect("snapshot");

    assert_eq!(snapshot.bids.len(), 2);
    assert_eq!(snapshot.bids[0].volume.0, 0);
    assert_eq!(snapshot.asks.len(), 1);
    assert_eq!(snapshot.asks[0].volume.0, 0);
    assert!(!snapshot.malformed.0);
}

#[test]
fn locked_and_duplicate_price_books_are_not_malformed() {
    let locked = build_dom_snapshot(
        "VN30F1M",
        &quote(1_000, vec![level(12_515, 100)], vec![level(12_515, 90)]),
        None,
    )
    .expect("snapshot");
    assert_eq!(locked.spread, Some(Price(0)));
    assert!(!locked.malformed.0);

    let duplicated = build_dom_snapshot(
        "VN30F1M",
        &quote(
            1_000,
            vec![level(12_514, 100), level(12_514, 50), level(12_513, 10)],
            vec![level(12_515, 1), level(12_515, 2)],
        ),
        None,
    )
    .expect("snapshot");
    assert!(!duplicated.malformed.0);
}

#[test]
fn finding_empty_batch_skips_symbol_validation() {
    // with no quotes the symbol is never validated, so a blank one returns Ok(vec![])
    assert_eq!(build_dom_batch("", &[], &[], None), Ok(vec![]));
    assert_eq!(build_dom_batch("   ", &[], &[], None), Ok(vec![]));

    // with at least one quote a blank symbol is rejected
    let quotes = vec![quote(
        1_000,
        vec![level(12_514, 10)],
        vec![level(12_515, 10)],
    )];
    assert_eq!(
        build_dom_batch("", &quotes, &[], None).expect_err("blank symbol"),
        DomError::EmptySymbol
    );
    assert_eq!(
        build_dom_snapshot("", &quotes[0], None).expect_err("blank symbol"),
        DomError::EmptySymbol
    );
}

#[test]
fn finding_future_latest_tick_attaches_to_past_quote() {
    // the latest_tick fallback has no timestamp check: a future tick attaches to a past quote
    let quotes = vec![quote(
        1_000,
        vec![level(12_514, 10)],
        vec![level(12_515, 10)],
    )];
    let latest = Some(trade(12_510, 4, TickSide::Sell, 5_000));

    let matched = match_ticks_to_quotes(&quotes, &[], latest.clone());
    assert_eq!(matched, vec![latest]);
}

#[test]
fn finding_stream_tick_overrides_newer_latest_tick() {
    // a qualifying stream tick (ts 1000) beats a newer latest_tick (ts 1005) for a quote at 1001
    let quotes = vec![quote(
        1_001,
        vec![level(12_514, 10)],
        vec![level(12_515, 10)],
    )];
    let ticks = vec![trade(12_512, 3, TickSide::Buy, 1_000)];
    let latest = Some(trade(12_513, 8, TickSide::Sell, 1_005));

    let matched = match_ticks_to_quotes(&quotes, &ticks, latest);
    assert_eq!(matched, vec![Some(ticks[0].clone())]);
}

#[test]
fn finding_best_bid_is_first_level_not_maximum() {
    // on a disordered book the best levels are the first ones, not the extremes
    let snapshot = build_dom_snapshot(
        "VN30F1M",
        &quote(
            1_000,
            vec![level(12_513, 100), level(12_515, 90)],
            vec![level(12_516, 80), level(12_514, 70)],
        ),
        None,
    )
    .expect("snapshot");

    assert_eq!(snapshot.best_bid, Some(price(12_513)));
    assert_eq!(snapshot.best_ask, Some(price(12_516)));
    assert!(snapshot.malformed.0);
}

#[test]
fn finding_snapshot_symbol_ignores_quote_symbol() {
    // the snapshot takes the symbol argument and ignores `quote.symbol`
    let snapshot = build_dom_snapshot(
        "OTHER",
        &quote(1_000, vec![level(12_514, 100)], vec![level(12_515, 110)]),
        None,
    )
    .expect("snapshot");

    assert_eq!(snapshot.symbol, "OTHER");
}
