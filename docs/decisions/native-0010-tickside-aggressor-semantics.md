# ADR 0010 — TickSide vs Python Aggressor Semantics: giữ binary enum, neutral-drop 'U', counter dời phase-04

- Status: Accepted (2026-07-09)
- Lane: normal (audit-only, zero behavior change)
- Story/Plan: `plans/20260708-1529-backend-first-platform-strategy/phase-03-domain-model-audit.md`

## Context

Đánh giá kiến trúc 2026-07-08 nêu vấn đề "dual enum TickSide vs AggressorSide chưa unified".
Scout xác minh: **AggressorSide chưa từng tồn tại trong Rust workspace** — chỉ có
`TickSide { Buy, Sell }` (`crates/market-types/src/lib.rs:436-439`), 18+ consumers.
Câu hỏi thật còn lại: Python pipeline phân loại aggressor **B/S/U** (Lee-Ready,
`data_provider/orderflow/processors/lee_ready_classifier.py:14-75` — 'U' khi cả tick rule
lẫn quote rule fail), còn Rust chỉ có enum nhị phân — tick 'U' đi đâu?

## Audit findings (2026-07-09)

Chuỗi xử lý 'U' qua các boundary (chi tiết: phase-03 file + Explore audit):

| Boundary | file:line | 'U'/absent/garbage → behavior |
|---|---|---|
| Python → Redis | `data_provider/redis_stream/stream_writer.py:70-76` | 'U' propagate nguyên trạng trong field `side` |
| MQTT codec | `broker-dnse/src/mqtt/codec.rs:16-27` | first-char không phải B/S → `None` |
| Redis ingest | `market-data-adapters/src/lib.rs:2935` + `market-types/src/lib.rs:10-16` (`normalize_side`) | non-B/S → `None`, lưu `Tick.side = None` (tick KHÔNG bị drop — vẫn tới client) |
| Signal task | `gateway-server/src/signal/task.rs:188-193` | `side: None` → `""` vào `evaluator.on_tick` |
| DeltaCalculator | `orderflow-engine/src/delta.rs:48-52` | match chỉ B/BUY/S/SELL, còn lại → `(0.0, 0.0)` — **volume drop im lặng khỏi delta/CVD/VPIN, không counter** |
| ClickHouse write | `market-data-adapters/src/lib.rs:2114-2115` | TickSide→(1\|2); không có mã cho unknown (side None không ghi cột này) |

Tần suất 'U' production (`default.vn30f_trades`, 29 phiên 05-26 → 07-09):

- Median/đa số phiên: **0%** (pipeline sau ADR-0007 classify B/S đầy đủ).
- Burst days: 2026-06-04 **3.06% volume**, 07-02 **2.55%**, 07-06 **1.08%**, còn lại ≤0.02%.
- Mean ~0.23% volume — burst trùng các ngày pipeline sự cố/quote gap, không phải trạng thái thường trực.

## Decision

**Option (a)+(c)-lite** — giữ nguyên, document, đo lường:

1. `TickSide { Buy, Sell }` **giữ binary** — KHÔNG thêm variant `Unknown` (option b bị bác:
   đụng 18+ files/match arms, chi phí lớn cho median 0%).
2. Policy chính thức cho 'U' tại engine boundary = **neutral-drop**: tick unclassified đóng
   góp 0 vào signed delta/CVD và không vào VPIN buckets. Đây là hành vi ĐÚNG ngữ nghĩa,
   không phải bug: Lee-Ready 'U' nghĩa là "không thể biết hướng aggressor" — gán bừa
   Buy/Sell sẽ bias delta nhiều hơn là bỏ trung tính. Tick vẫn tới client đầy đủ
   (price/volume hiển thị, chỉ side rỗng).
3. **`ticks_unclassified_total` counter** tại gateway signal ingest — GIAO CHO PHASE-04
   (observability) implement cùng metrics stack, tránh sửa gateway src ngay sau deploy
   fd726831. Counter làm burst days (>0.5% volume) nhìn thấy được thay vì im lặng.
4. Rust = single source of truth (user directive 2026-07-09): wire/engine contract là
   B/S/None; Python 'U' được coi là "absent" ở phía Rust — không đổi Python.

## Consequences

- Zero behavior change; zero hot-path cost đến khi phase-04 thêm counter (1 atomic inc).
- CVD/VPIN trên burst day thiếu tối đa ~3% volume — chấp nhận được với advisory signals;
  counter phase-04 + alert sẽ phát hiện nếu 'U' thành mãn tính (>0.5% kéo dài).
- Trả lời scout question #1: AggressorSide không phải "deprecation dở" — chưa bao giờ
  tồn tại; không tạo enum mới.
- Follow-up có điều kiện: nếu counter cho thấy 'U' >0.5% volume kéo dài nhiều phiên →
  mở task riêng đánh giá option (b) (enum Unknown), được bảo vệ bởi lưới test PLAT-TEST-001.

## Rejected alternatives

- **(b) `TickSide::Unknown`**: đắt (18+ consumers, mọi match arm), không có lợi ích runtime
  khi engines vẫn phải neutral-drop; chỉ tái xét theo trigger ở trên.
- **Gán 'U' theo tick-rule fallback phía Rust**: duplicate logic classifier Python, vi phạm
  single source of truth pipeline (classifier sống ở data_provider).
- **Drop toàn bộ tick 'U' tại ingest**: mất price/volume cho client display — tệ hơn hiện trạng.
