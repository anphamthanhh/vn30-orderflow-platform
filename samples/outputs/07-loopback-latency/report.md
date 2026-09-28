# Demo 07 - loopback latency of the client frame

Machine: Windows 11, Intel64 Family 6 Model 79 Stepping 1, GenuineIntel. All figures are **loopback** (127.0.0.1, one process, one monotonic clock).

| mode | series | p50 µs | p90 µs | p99 µs | p99.9 µs | max µs | mean µs |
|---|---|---|---|---|---|---|---|
| paced | one_way | 101.4 | 177.0 | 209.0 | 282.1 | 390.7 | 112.5 |
| flood | one_way | 40.6 | 60.1 | 92.5 | 127.6 | 184.8 | 39.0 |
| flood | read_decode | 19.4 | 50.4 | 85.1 | 157.8 | 270.3 | 27.1 |

How to read it: the *paced* one-way series is the time a single frame takes from the writer's stamp to a typed decode on the reader. Under *flood*, frames queue behind each other; the per-frame read+decode series there is small because the next frame is usually already buffered — that is the number an older loopback harness reported as p99 ≈ 125 µs. The paced interval is bounded below by the OS timer resolution; either way it is realistic spacing for futures ticks.

```text
$ --mode paced
loopback_latency mode=paced frames=2000 warmup=200 interval_us=1000 achieved_gap_us=1637.8 os=windows threads=44
LOOPBACK (127.0.0.1, one process, one clock), microseconds:
one_way      p50=101.4 p90=177.0 p99=209.0 p99.9=282.1 max=390.7 mean=112.5

$ --mode flood
loopback_latency mode=flood frames=20000 warmup=2000 interval_us=1000 achieved_gap_us=28.0 os=windows threads=44
LOOPBACK (127.0.0.1, one process, one clock), microseconds:
one_way      p50=40.6 p90=60.1 p99=92.5 p99.9=127.6 max=184.8 mean=39.0
read_decode  p50=19.4 p90=50.4 p99=85.1 p99.9=157.8 max=270.3 mean=27.1
```
