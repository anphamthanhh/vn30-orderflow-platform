//! One-way loopback latency probe for client-proto frames.
//!
//! A writer thread stamps each tick before JSON serialisation; a reader thread stamps it again
//! after decoding a `LiveTickView`. One monotonic clock, 127.0.0.1, `TCP_NODELAY`, one write per
//! frame. `--mode flood` sends back to back and also prints the per-frame read + decode time.
//!
//! ```text
//! cargo run --release -p client-proto --example loopback_latency -- \
//!     [--mode paced|flood] [--frames 5000] [--warmup 500] [--interval-us 1000]
//! ```

use std::error::Error;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread;
use std::time::{Duration, Instant};

use client_proto::{
    EnvelopeHeader, LiveTickView, MessageKind, OutboundPayload, ENVELOPE_HEADER_LEN,
    MAX_PAYLOAD_LEN,
};
use hdrhistogram::Histogram;

type BoxError = Box<dyn Error + Send + Sync>;

#[derive(Debug, Clone, Copy)]
struct Config {
    paced: bool,
    frames: u64,
    warmup: u64,
    interval: Duration,
}

fn parse_args() -> Result<Config, BoxError> {
    let mut cfg = Config {
        paced: true,
        frames: 5_000,
        warmup: 500,
        interval: Duration::from_micros(1_000),
    };
    let mut args = std::env::args().skip(1);
    while let Some(flag) = args.next() {
        let value = args.next().ok_or_else(|| format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--mode" => {
                cfg.paced = match value.as_str() {
                    "paced" => true,
                    "flood" => false,
                    other => return Err(format!("unknown mode {other:?}; use paced|flood").into()),
                }
            }
            "--frames" => cfg.frames = value.parse()?,
            "--warmup" => cfg.warmup = value.parse()?,
            "--interval-us" => cfg.interval = Duration::from_micros(value.parse()?),
            other => return Err(format!("unexpected argument {other:?}").into()),
        }
    }
    if cfg.frames == 0 {
        return Err("--frames must be at least 1".into());
    }
    Ok(cfg)
}

/// Nanoseconds since `base`, the clock both threads read.
fn nanos_since(base: Instant) -> u64 {
    u64::try_from(base.elapsed().as_nanos()).unwrap_or(u64::MAX)
}

/// Writer thread: one frame per tick, stamped just before serialisation.
fn serve(listener: TcpListener, base: Instant, cfg: Config) -> Result<Duration, BoxError> {
    let (mut sock, _) = listener.accept()?;
    sock.set_nodelay(true)?;
    let mut frame = Vec::with_capacity(512);
    let started = Instant::now();
    for seq in 1..=cfg.warmup + cfg.frames {
        if cfg.paced {
            thread::sleep(cfg.interval);
        }
        let tick = LiveTickView {
            symbol: "VN30F1M".to_owned(),
            timestamp: 1_600_000_000 + i64::try_from(seq)?,
            price: 1250.0,
            volume: 1,
            side: Some("buy".to_owned()),
            event_sequence: Some(seq),
            // send stamp relative to `base`; a live feed puts a Unix-epoch pickup time here
            source_pickup_unix_ns: Some(nanos_since(base)),
        };
        let payload = serde_json::to_vec(&OutboundPayload::LiveTick(tick))?;
        let header =
            EnvelopeHeader::new(MessageKind::LiveTick, 1, seq, u32::try_from(payload.len())?);
        frame.clear();
        frame.extend_from_slice(&header.encode());
        frame.extend_from_slice(&payload);
        sock.write_all(&frame)?;
    }
    Ok(started.elapsed())
}

fn us(nanos: u64) -> f64 {
    // display only; the histogram holds integer nanoseconds
    nanos as f64 / 1_000.0
}

fn print_row(name: &str, h: &Histogram<u64>) {
    println!(
        "{name:<12} p50={:.1} p90={:.1} p99={:.1} p99.9={:.1} max={:.1} mean={:.1}",
        us(h.value_at_quantile(0.50)),
        us(h.value_at_quantile(0.90)),
        us(h.value_at_quantile(0.99)),
        us(h.value_at_quantile(0.999)),
        us(h.max()),
        h.mean() / 1_000.0,
    );
}

fn main() -> Result<(), BoxError> {
    let cfg = parse_args()?;
    let base = Instant::now();
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let addr = listener.local_addr()?;
    let server = thread::spawn(move || serve(listener, base, cfg));

    let mut sock = TcpStream::connect(addr)?;
    sock.set_nodelay(true)?;
    let mut one_way = Histogram::<u64>::new_with_bounds(1, 60_000_000_000, 3)?;
    let mut read_decode = Histogram::<u64>::new_with_bounds(1, 60_000_000_000, 3)?;
    let mut header = [0_u8; ENVELOPE_HEADER_LEN];
    let mut payload = Vec::with_capacity(512);
    for i in 0..cfg.warmup + cfg.frames {
        let read_started = nanos_since(base);
        sock.read_exact(&mut header)?;
        let h = EnvelopeHeader::decode(header, MAX_PAYLOAD_LEN)?;
        payload.resize(usize::try_from(h.payload_len)?, 0);
        sock.read_exact(&mut payload)?;
        let value: serde_json::Value = serde_json::from_slice(&payload)?;
        let inner = value
            .get("LiveTick")
            .cloned()
            .ok_or("frame is not a LiveTick")?;
        let tick: LiveTickView = serde_json::from_value(inner)?;
        let decoded = nanos_since(base);
        if i >= cfg.warmup {
            let sent = tick.source_pickup_unix_ns.ok_or("tick has no send stamp")?;
            one_way.record(decoded.saturating_sub(sent).max(1))?;
            read_decode.record(decoded.saturating_sub(read_started).max(1))?;
        }
    }
    let send_elapsed = server.join().map_err(|_| "server thread panicked")??;

    let mode = if cfg.paced { "paced" } else { "flood" };
    let frames_sent = cfg.warmup + cfg.frames;
    let gap_us = send_elapsed.as_secs_f64() * 1e6 / frames_sent as f64;
    println!(
        "loopback_latency mode={mode} frames={} warmup={} interval_us={} achieved_gap_us={gap_us:.1} \
         os={} threads={}",
        cfg.frames,
        cfg.warmup,
        cfg.interval.as_micros(),
        std::env::consts::OS,
        thread::available_parallelism().map_or(0, |n| n.get()),
    );
    println!("LOOPBACK (127.0.0.1, one process, one clock), microseconds:");
    print_row("one_way", &one_way);
    if !cfg.paced {
        // on an unpaced stream this is the backlog-drain rate, not a latency
        print_row("read_decode", &read_decode);
    }
    Ok(())
}
