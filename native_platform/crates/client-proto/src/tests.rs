use super::*;
use market_dom_viewmodel::{DomLevelView, DomSnapshotView};
use market_profile_viewmodel::{
    CompositeProfileView, CompositeRangeView, ProfileSummaryView, TpoProfileRowView,
    VolumeProfileRowView,
};

#[test]
fn envelope_header_round_trips() {
    let header =
        EnvelopeHeader::new(MessageKind::DomBatch, 7, 42, 512).with_flags(FrameFlags::StreamEnd);

    let decoded = EnvelopeHeader::decode(header.encode(), MAX_PAYLOAD_LEN).expect("decode");

    assert_eq!(decoded.version, PROTOCOL_VERSION);
    assert_eq!(decoded.kind, MessageKind::DomBatch);
    assert_eq!(decoded.stream_id, 7);
    assert_eq!(decoded.sequence, 42);
    assert_eq!(decoded.payload_len, 512);
    assert!(is_stream_end(decoded.flags));
}

#[test]
fn decode_rejects_a_payload_len_above_the_limit() {
    let at_limit = EnvelopeHeader::new(MessageKind::LiveTick, 1, 1, MAX_PAYLOAD_LEN).encode();
    assert_eq!(
        EnvelopeHeader::decode(at_limit, MAX_PAYLOAD_LEN).map(|h| h.payload_len),
        Ok(MAX_PAYLOAD_LEN)
    );
    for payload_len in [MAX_PAYLOAD_LEN + 1, u32::MAX] {
        let bytes = EnvelopeHeader::new(MessageKind::LiveTick, 1, 1, payload_len).encode();
        assert_eq!(
            EnvelopeHeader::decode(bytes, MAX_PAYLOAD_LEN),
            Err(HeaderError::PayloadTooLarge {
                payload_len,
                max_payload_len: MAX_PAYLOAD_LEN,
            })
        );
    }
    // a server reading requests passes a smaller limit
    let request = EnvelopeHeader::new(MessageKind::PlaceOrderRequest, 3, 1, 4_097).encode();
    assert!(matches!(
        EnvelopeHeader::decode(request, 4_096),
        Err(HeaderError::PayloadTooLarge { .. })
    ));
}

#[test]
fn decode_reports_an_unknown_kind() {
    let mut bytes = EnvelopeHeader::new(MessageKind::LiveTick, 1, 1, 8).encode();
    bytes[2..4].copy_from_slice(&999_u16.to_be_bytes());
    assert_eq!(
        EnvelopeHeader::decode(bytes, MAX_PAYLOAD_LEN),
        Err(HeaderError::UnknownKind(999))
    );
}

#[test]
fn message_kind_rejects_unknown_values() {
    assert!(MessageKind::from_u16(999_u16).is_none());
}

#[test]
fn signal_kinds_decode_from_u16() {
    assert_eq!(MessageKind::from_u16(92), Some(MessageKind::SignalSnapshot));
    assert_eq!(MessageKind::from_u16(93), Some(MessageKind::SignalEvent));
}

#[test]
fn oms_kinds_decode_from_u16() {
    assert_eq!(MessageKind::from_u16(94), Some(MessageKind::OmsOrderEvent));
    assert_eq!(
        MessageKind::from_u16(95),
        Some(MessageKind::OrderHistoryRequest)
    );
    assert_eq!(
        MessageKind::from_u16(96),
        Some(MessageKind::OrderHistoryResponse)
    );
}

#[test]
fn exec_stats_kinds_decode_from_u16() {
    assert_eq!(
        MessageKind::from_u16(97),
        Some(MessageKind::OrderStatsRequest)
    );
    assert_eq!(
        MessageKind::from_u16(98),
        Some(MessageKind::OrderStatsResponse)
    );
    assert_eq!(
        MessageKind::from_u16(99),
        Some(MessageKind::ClosedTradesRequest)
    );
    assert_eq!(
        MessageKind::from_u16(100),
        Some(MessageKind::ClosedTradesResponse)
    );
    assert_eq!(
        MessageKind::from_u16(101),
        Some(MessageKind::EquityCurveRequest)
    );
    assert_eq!(
        MessageKind::from_u16(102),
        Some(MessageKind::EquityCurveResponse)
    );
}

#[test]
fn close_position_kinds_and_payload_roundtrip() {
    assert_eq!(
        MessageKind::from_u16(64),
        Some(MessageKind::ClosePositionRequest)
    );
    assert_eq!(
        MessageKind::from_u16(65),
        Some(MessageKind::ClosePositionResponse)
    );

    let req = ClosePositionRequest {
        symbol: "VN30F1M".to_owned(),
    };
    let decoded: ClosePositionRequest =
        serde_json::from_value(serde_json::to_value(&req).expect("ser")).expect("de");
    assert_eq!(decoded.symbol, "VN30F1M");

    let resp = OutboundPayload::ClosePosition(ClosePositionResponse {
        order: OrderView {
            order_id: "9".to_owned(),
            symbol: "VN30F1M".to_owned(),
            side: "S".to_owned(),
            order_type: "market".to_owned(),
            quantity: 2,
            filled_quantity: 2,
            price: None,
            avg_fill_price: Some(1_305.0),
            status: "filled".to_owned(),
            reject_reason: None,
            created_at: 0,
            updated_at: 0,
        },
    });
    assert_eq!(resp.message_kind(), MessageKind::ClosePositionResponse);
    let json = serde_json::to_value(&resp).expect("serialize");
    let decoded: ClosePositionResponse =
        serde_json::from_value(json["ClosePosition"].clone()).expect("decode");
    assert_eq!(decoded.order.side, "S");
    assert_eq!(decoded.order.filled_quantity, 2);
}

#[test]
fn bracket_kinds_and_payload_roundtrip() {
    for (raw, expected) in [
        (103_u16, MessageKind::AttachBracketRequest),
        (104, MessageKind::AttachBracketResponse),
        (105, MessageKind::CancelBracketRequest),
        (106, MessageKind::CancelBracketResponse),
        (107, MessageKind::ListBracketsRequest),
        (108, MessageKind::ListBracketsResponse),
    ] {
        assert_eq!(MessageKind::from_u16(raw), Some(expected));
    }

    let req = AttachBracketRequest {
        symbol: "VN30F1M".to_owned(),
        side: "long".to_owned(),
        take_profit: Some(1_260.0),
        stop_loss: None,
        trailing_delta: Some(10.0),
    };
    let decoded: AttachBracketRequest =
        serde_json::from_value(serde_json::to_value(&req).expect("ser")).expect("de");
    assert_eq!(decoded.side, "long");
    assert_eq!(decoded.trailing_delta, Some(10.0));

    let resp = OutboundPayload::AttachBracket(AttachBracketResponse {
        bracket: BracketView {
            symbol: "VN30F1M".to_owned(),
            side: "long".to_owned(),
            take_profit: Some(1_260.0),
            stop_loss: None,
            trailing_delta: Some(10.0),
            trailing_stop: Some(1_255.0),
            status: "armed".to_owned(),
            armed_at: 1_700_000_000,
        },
    });
    assert_eq!(resp.message_kind(), MessageKind::AttachBracketResponse);
    let json = serde_json::to_value(&resp).expect("serialize");
    let decoded: AttachBracketResponse =
        serde_json::from_value(json["AttachBracket"].clone()).expect("decode");
    assert_eq!(decoded.bracket.status, "armed");
    assert_eq!(decoded.bracket.trailing_stop, Some(1_255.0));

    // an empty request has no symbol filter
    let list_req: ListBracketsRequest = serde_json::from_str("{}").expect("default list request");
    assert!(list_req.symbol.is_none());
}

#[test]
fn metrics_history_kinds_and_payload_roundtrip() {
    assert_eq!(
        MessageKind::from_u16(109),
        Some(MessageKind::FetchMetricsHistoryRequest)
    );
    assert_eq!(
        MessageKind::from_u16(110),
        Some(MessageKind::FetchMetricsHistoryResponse)
    );

    let req = FetchMetricsHistoryRequest {
        symbol: "VN30F1M".to_owned(),
        from_timestamp: 1_700_000_000,
        to_timestamp: 1_700_050_000,
        max_points: Some(4096),
    };
    let decoded: FetchMetricsHistoryRequest =
        serde_json::from_value(serde_json::to_value(&req).expect("ser")).expect("de");
    assert_eq!(decoded.from_timestamp, 1_700_000_000);
    assert_eq!(decoded.max_points, Some(4096));

    let resp = OutboundPayload::MetricsHistory(FetchMetricsHistoryResponse {
        points: vec![MetricsHistoryPoint {
            ts: 1_700_000_005,
            cvd: 120.0,
            vpin: 0.42,
            vpin_regime: "normal".to_owned(),
            ofi_cumulative: -3.5,
            vwap_cumulative_pv: 15_600.0,
            vwap_cumulative_vol: 12.0,
            vwap_cumulative_pv_sq: 20_280_000.0,
            vwap_last_trading_day_utc: 1_699_995_600,
        }],
        total_count: 1,
    });
    assert_eq!(
        resp.message_kind(),
        MessageKind::FetchMetricsHistoryResponse
    );
    let json = serde_json::to_value(&resp).expect("serialize");
    let decoded: FetchMetricsHistoryResponse =
        serde_json::from_value(json["MetricsHistory"].clone()).expect("decode");
    assert_eq!(decoded.points.len(), 1);
    assert_eq!(decoded.points[0].vpin_regime, "normal");
    assert_eq!(decoded.points[0].vwap_cumulative_pv, 15_600.0);
    assert_eq!(decoded.points[0].vwap_cumulative_vol, 12.0);
    assert_eq!(decoded.points[0].vwap_cumulative_pv_sq, 20_280_000.0);
    assert_eq!(decoded.points[0].vwap_last_trading_day_utc, 1_699_995_600);
}

#[test]
fn metrics_history_point_decodes_without_vwap_fields() {
    // payload from a peer that predates the `vwap_*` fields
    let legacy = serde_json::json!({
        "ts": 1_700_000_005_i64,
        "cvd": 120.0,
        "vpin": 0.42,
        "vpin_regime": "normal",
        "ofi_cumulative": -3.5
    });
    let decoded: MetricsHistoryPoint = serde_json::from_value(legacy).expect("decode legacy");
    assert_eq!(decoded.vwap_cumulative_pv, 0.0);
    assert_eq!(decoded.vwap_cumulative_vol, 0.0);
    assert_eq!(decoded.vwap_cumulative_pv_sq, 0.0);
    assert_eq!(decoded.vwap_last_trading_day_utc, 0);
}

#[test]
fn order_fills_kinds_and_payload_roundtrip() {
    assert_eq!(
        MessageKind::from_u16(111),
        Some(MessageKind::FetchOrderFillsRequest)
    );
    assert_eq!(
        MessageKind::from_u16(112),
        Some(MessageKind::FetchOrderFillsResponse)
    );
    let resp = OutboundPayload::OrderFills(FetchOrderFillsResponse {
        rows: vec![OrderFillRow {
            fill_ts: 1_786_000_000_000,
            session_date: "2026-08-11".to_owned(),
            order_id: "7085730".to_owned(),
            client_order_id: "c1".to_owned(),
            symbol: "VN30F1M".to_owned(),
            side: "B".to_owned(),
            fill_seq: 1,
            fill_qty: 1,
            fill_price: 1934.3,
            remaining_qty: 0,
        }],
        total_count: 1,
    });
    assert_eq!(resp.message_kind(), MessageKind::FetchOrderFillsResponse);
    let json = serde_json::to_value(&resp).expect("serialize");
    let decoded: FetchOrderFillsResponse =
        serde_json::from_value(json["OrderFills"].clone()).expect("decode");
    assert_eq!(decoded.rows.len(), 1);
    assert_eq!(decoded.rows[0].fill_price, 1934.3);
    assert_eq!(decoded.rows[0].side, "B");
}

#[test]
fn exchange_tp_sl_kinds_and_payload_roundtrip() {
    for k in 113..=116u16 {
        assert!(MessageKind::from_u16(k).is_some());
    }
    let resp = OutboundPayload::ExchangeTpSlGet(ExchangeTpSlGetResponse {
        supported: true,
        configs: vec![ExchangeTpSlView {
            account_no: "DEMO-0001".to_owned(),
            take_profit_enabled: true,
            stop_loss_enabled: true,
            trailing_enabled: true,
            take_profit_price: 1300.0,
            stop_loss_price: 1290.0,
            stop_loss_delta_price: 3.0,
            status: "ACTIVE".to_owned(),
        }],
    });
    assert_eq!(resp.message_kind(), MessageKind::ExchangeTpSlGetResponse);
    let json = serde_json::to_value(&resp).expect("serialize");
    let decoded: ExchangeTpSlGetResponse =
        serde_json::from_value(json["ExchangeTpSlGet"].clone()).expect("decode");
    assert!(decoded.supported && decoded.configs[0].trailing_enabled);
    assert_eq!(decoded.configs[0].account_no, "DEMO-0001");
}

#[test]
fn exec_stats_payloads_roundtrip() {
    let stats = OutboundPayload::OrderStats(OrderStatsResponse {
        days: vec![OrderStatsDay {
            session_date: "2026-08-07".to_owned(),
            order_count: 12,
            fill_count: 9,
            reject_count: 1,
            fill_rate: 0.75,
            latency_p50_ms: 180.0,
            latency_p95_ms: 640.0,
        }],
        total_count: 1,
    });
    assert_eq!(stats.message_kind(), MessageKind::OrderStatsResponse);
    let json = serde_json::to_value(&stats).expect("serialize");
    let decoded: OrderStatsResponse =
        serde_json::from_value(json["OrderStats"].clone()).expect("decode");
    assert_eq!(decoded.days[0].order_count, 12);
    assert_eq!(decoded.days[0].fill_rate, 0.75);

    let trades = OutboundPayload::ClosedTrades(ClosedTradesResponse {
        rows: vec![ClosedTradeRow {
            exit_ts: 1_783_563_988_000,
            entry_ts: 1_783_563_900_000,
            session_date: "2026-08-07".to_owned(),
            symbol: "VN30F1M".to_owned(),
            side: "B".to_owned(),
            qty: 2,
            entry_price: 1_300.0,
            exit_price: 1_305.5,
            realized_pnl_points: 5.5,
            realized_pnl_money: 1_100_000.0,
            hold_secs: 88.0,
        }],
        total_count: 1,
    });
    assert_eq!(trades.message_kind(), MessageKind::ClosedTradesResponse);
    let json = serde_json::to_value(&trades).expect("serialize");
    let decoded: ClosedTradesResponse =
        serde_json::from_value(json["ClosedTrades"].clone()).expect("decode");
    assert_eq!(decoded.rows[0].realized_pnl_points, 5.5);

    let equity = OutboundPayload::EquityCurve(EquityCurveResponse {
        points: vec![EquityCurvePoint {
            snapshot_ts: 1_783_563_988_000,
            session_date: "2026-08-07".to_owned(),
            nav: 100_000_000.0,
            equity: 100_500_000.0,
            unrealized_pnl: 500_000.0,
            realized_pnl: 0.0,
            margin_used: 20_000_000.0,
            buying_power: 80_000_000.0,
            source: "intraday".to_owned(),
        }],
        total_count: 1,
    });
    assert_eq!(equity.message_kind(), MessageKind::EquityCurveResponse);
    let json = serde_json::to_value(&equity).expect("serialize");
    let decoded: EquityCurveResponse =
        serde_json::from_value(json["EquityCurve"].clone()).expect("decode");
    assert_eq!(decoded.points[0].source, "intraday");
}

#[test]
fn oms_order_event_payload_roundtrip() {
    let event = OmsOrderEventView {
        event_ts: 1_783_563_368_869,
        event_kind: "place".to_owned(),
        order_id: "6984536".to_owned(),
        client_order_id: "cli_1".to_owned(),
        symbol: "VN30F1M".to_owned(),
        side: "B".to_owned(),
        order_type: "market".to_owned(),
        status: "pending".to_owned(),
        qty: 1,
        filled_qty: 0,
        price: 0.0,
        avg_price: 0.0,
        reject_reason: String::new(),
    };
    let payload = OutboundPayload::OmsOrderEvent(event.clone());
    assert_eq!(payload.message_kind(), MessageKind::OmsOrderEvent);
    let json = serde_json::to_value(&payload).expect("serialize");
    let decoded: OmsOrderEventView =
        serde_json::from_value(json["OmsOrderEvent"].clone()).expect("decode");
    assert_eq!(decoded, event);
    // optional string fields may be absent
    let minimal: OmsOrderEventView = serde_json::from_value(serde_json::json!({
        "event_ts": 1, "event_kind": "list", "order_id": "1", "symbol": "VN30F1M",
        "side": "B", "order_type": "limit", "status": "pending",
        "qty": 1, "filled_qty": 0, "price": 1300.0, "avg_price": 0.0
    }))
    .expect("minimal decode");
    assert!(minimal.client_order_id.is_empty());
    assert!(minimal.reject_reason.is_empty());
}

#[test]
fn order_history_payload_roundtrip() {
    let response = OrderHistoryResponse {
        rows: vec![OrderHistoryRow {
            event_ts: 1_783_563_988_888,
            session_date: "2026-07-09".to_owned(),
            order_id: "6984536".to_owned(),
            client_order_id: String::new(),
            symbol: "VN30F1M".to_owned(),
            side: "B".to_owned(),
            order_type: "market".to_owned(),
            status: "filled".to_owned(),
            qty: 1,
            filled_qty: 1,
            price: 0.0,
            avg_price: 1994.1,
            reject_reason: String::new(),
            event_kind: "list".to_owned(),
        }],
        total_count: 1,
    };
    let payload = OutboundPayload::OrderHistory(response.clone());
    assert_eq!(payload.message_kind(), MessageKind::OrderHistoryResponse);
    let json = serde_json::to_value(&payload).expect("serialize");
    let decoded: OrderHistoryResponse =
        serde_json::from_value(json["OrderHistory"].clone()).expect("decode");
    assert_eq!(decoded, response);
    let request = OrderHistoryRequest {
        symbol: Some("VN30F1M".to_owned()),
        from_timestamp: 1_783_468_800,
        to_timestamp: 1_783_555_200,
        max_rows: Some(500),
    };
    let bytes = serde_json::to_vec(&request).expect("request serialize");
    let back: OrderHistoryRequest = serde_json::from_slice(&bytes).expect("request decode");
    assert_eq!(back, request);
}

#[test]
fn signal_event_payload_roundtrip() {
    let event = SignalEventView {
        id: "trailing_stop_update".to_owned(),
        direction: "bearish".to_owned(),
        strength: 0.42,
        price: 1301.5,
        timestamp: 1_783_296_000,
        target: None,
        stop: Some(1303.2),
        note: "cvd=-120.0".to_owned(),
    };
    let payload = OutboundPayload::SignalEvent(event.clone());
    assert_eq!(payload.message_kind(), MessageKind::SignalEvent);
    let json = serde_json::to_value(&payload).expect("serialize");
    let decoded: SignalEventView =
        serde_json::from_value(json["SignalEvent"].clone()).expect("decode");
    assert_eq!(decoded, event);
}

#[test]
fn signal_snapshot_payload_roundtrip() {
    let snapshot = SignalStateView {
        vpin: 0.55,
        vpin_regime: "elevated".to_owned(),
        bias: Some(SessionBiasView {
            direction: "bullish".to_owned(),
            prior_vah: 1305.0,
            prior_val: 1295.0,
            target: 1305.0,
            confidence: 0.8,
            computed_at: 1_783_297_800,
        }),
        position: Some(AdvisoryPositionView {
            direction: "bullish".to_owned(),
            entry_price: 1296.0,
            entry_ts: 1_783_298_000,
            stop: 1294.5,
            extreme: 1298.0,
        }),
        recent_events: vec![],
    };
    let payload = OutboundPayload::SignalSnapshot(snapshot.clone());
    assert_eq!(payload.message_kind(), MessageKind::SignalSnapshot);
    let json = serde_json::to_value(&payload).expect("serialize");
    let decoded: SignalStateView =
        serde_json::from_value(json["SignalSnapshot"].clone()).expect("decode");
    assert_eq!(decoded, snapshot);
    // optional fields may be absent
    let minimal: SignalStateView =
        serde_json::from_value(serde_json::json!({"vpin": 0.0, "vpin_regime": "normal"}))
            .expect("minimal decode");
    assert!(minimal.bias.is_none());
    assert!(minimal.position.is_none());
    assert!(minimal.recent_events.is_empty());
}

#[test]
fn outbound_payload_wraps_dom_snapshot_view() {
    let payload = OutboundPayload::DomSnapshot(DomSnapshotView {
        r#type: "dom_snapshot".to_owned(),
        symbol: "VN30F1M".to_owned(),
        timestamp: Some(10),
        event_sequence: Some(42),
        datetime: Some("2026-04-19T09:15:00+07:00".to_owned()),
        last_trade_price: Some(1251.4),
        best_bid: Some(1251.3),
        best_ask: Some(1251.5),
        spread: Some(0.2),
        total_bid_qty: 100,
        total_offer_qty: 120,
        bids: vec![DomLevelView {
            price: 1251.3,
            volume: 50.0,
        }],
        asks: vec![DomLevelView {
            price: 1251.5,
            volume: 60.0,
        }],
        last_trade: None,
    });

    assert!(matches!(payload, OutboundPayload::DomSnapshot(_)));
}

#[test]
fn outbound_payload_wraps_composite_profile_view() {
    let payload = OutboundPayload::CompositeProfile(CompositeProfileView {
        symbol: "VN30F1M".to_owned(),
        range: CompositeRangeView {
            from: "2026-04-19T09:00:00+07:00".to_owned(),
            to: "2026-04-19T11:30:00+07:00".to_owned(),
        },
        applied_range: CompositeRangeView {
            from: "2026-04-19T09:00:00+07:00".to_owned(),
            to: "2026-04-19T11:30:00+07:00".to_owned(),
        },
        overview_interval: "M5".to_owned(),
        overview_bars: Vec::new(),
        market_profile: ProfileSummaryView {
            rows: vec![TpoProfileRowView {
                price: 1251.4,
                tpo_count: 2,
                bucket_ids: vec!["1".to_owned()],
            }],
            poc: Some(1251.4),
            vah: Some(1251.5),
            val: Some(1251.3),
        },
        volume_profile: ProfileSummaryView {
            rows: vec![VolumeProfileRowView {
                price: 1251.4,
                volume: 120.0,
                buy_volume: 0.0,
                sell_volume: 0.0,
            }],
            poc: Some(1251.4),
            vah: Some(1251.5),
            val: Some(1251.3),
        },
        auction_references: None,
        advanced_references: None,
        source: None,
    });

    assert!(matches!(payload, OutboundPayload::CompositeProfile(_)));
}
