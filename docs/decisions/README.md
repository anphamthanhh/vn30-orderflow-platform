# Decision records (selected)

Thirteen of the project's architecture/research decision records, copied from the private
repository with four kinds of edit: internal hostnames and paths replaced with placeholders;
host-specific operational commands (service restarts, container commands) removed from ADR 0031;
links to records that are not reproduced here turned into plain text; and a few references to
records or notes that are not reproduced here marked "(not distributed)". File paths inside them
refer to the private repository's layout; not every path exists in this distilled edition
(`docs/provenance.md` maps what does). Some records mix Vietnamese and English, as written.

| ADR | Decision | Why it is here |
|---|---|---|
| 0005 | Python for research, Rust for the native platform, no FFI between them | the two-stack architecture |
| 0008 | The Rust backtester owns realised P&L; Python mirrors, never re-charges | single source of truth for cost |
| 0020 | Derivatives personal-income-tax base corrected (notional × IM/2) | the cost model (`knowledge/02`) |
| 0021 | ClickHouse becomes the canonical store; SQL Server retired | data platform (`knowledge/09`) |
| 0023 | One fill authority; the legacy microstructure simulator quarantined | execution realism (`knowledge/03`) |
| 0029 | The first-generation Python backtest stack retired | validation history (`knowledge/08`) |
| 0031 | Call-auction depth: the closing auction was captured, the opening one never was | market structure |
| 0032 | Four-layer market-data standard and the two columns that make it possible | provenance (`knowledge/09`) |
| 0037 | A feature name is a contract, not a label | one name, one formula (`knowledge/05`) |
| 0038 | Non-adoptions: meta-labeling, MDI and MDA | saying no, with reasons |
| 0040 | One definition of MAE/MFE, and what may be concluded from it | trade forensics (`knowledge/08`) |
| 0043 | Sub-session horizons closed as alpha; an execution-context lane opened | a negative result, recorded |
| native-0010 | Trade-side semantics: binary side, unclassified prints count as zero | CVD definition (`knowledge/05`) |

Numbering has gaps because the full series (40 records in the research repository, 11 in the Rust
workspace, some of them upstream harness templates) is not reproduced here.
