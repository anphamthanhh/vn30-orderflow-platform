# 0005 — Python Backtest + Rust Native Split

<!-- deployment-status: v1 — see docs/templates/decision.md -->
**Deployment:** N/A (code-only — deployment is the merge; proof is the test row in docs/TEST_MATRIX.md)


## Date

2025-04-30

## Status

accepted

## Context

The backtest system was mature in Python (12 strategies, WFA, optimization). But real-time visualization needed high-throughput rendering (DOM ladder, heatmaps, TPO charts) that Python UIs (Dash/Plotly) could not handle at sub-second update rates.

## Decision

Keep the Python backtest pipeline intact. Build a new Rust workspace (`native_platform/`) for:
- GPU-accelerated desktop visualization (egui/wgpu)
- TCP gateway for data distribution (tokio)
- DNSE broker integration with MQTT streaming

The two stacks communicate through shared data formats (CSV, SQL Server, Redis) rather than direct FFI.

## Consequences

- **Pro**: Each stack uses the best tool for its job — Python for research velocity, Rust for rendering performance
- **Pro**: No risky FFI boundary; data coupling is loose
- **Con**: Two stacks to maintain, deploy, and debug
- **Con**: Domain types duplicated between Python (pydantic) and Rust (market-types crate)
