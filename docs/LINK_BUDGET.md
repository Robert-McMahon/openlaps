# Link budget: vehicle → pit

## 1. What this answers

Does full-rate telemetry — every CAN signal, 50 Hz GPS, and **100 Hz IMU**
with no decimation — fit over a HaLow radio link, and with how much margin?
And if it ever doesn't, what are the levers to claw margin back without
redesigning the wire format?

This is a static analysis, not a live model: it runs
[`tools/size_batch.py`](../tools/size_batch.py) against a measured signal mix
and reports the resulting bitrate, then checks that against the HaLow PHY
rate table. It replaces the predecessor project's marimo notebook
(`docs/mqtt_bandwidth.py` in the old repo) with real serialized protobuf
messages instead of interactive what-if sliders — the openlaps wire format
(`ChannelRegistry` / `SampleBatch`, see [`WIRE_FORMAT.md`](WIRE_FORMAT.md)
and [`proto/telemetry.proto`](../proto/telemetry.proto)) is simple and small
enough that one worked example, re-run when the signal mix changes, is more
useful than a slider panel.

**Headline result:** the full-rate stream, including 100 Hz IMU that never
crossed the old link at all, offers **~0.57 Mbit/s** — about **4.3× smaller**
than the predecessor's measured **~2.4 Mbit/s** JSON-over-MQTT baseline —
and clears the smallest HaLow channel/MCS combination worth considering
(2 MHz MCS4) with **~3.4× headroom**, leaving room to share the radio with a
video stream.

## 2. Method

`tools/size_batch.py` builds one real `SampleBatch` protobuf message (using
the generated Python bindings from `proto/telemetry.proto`) sized to a full
batching tick, and measures `len(batch.SerializeToString())` directly — no
hand-rolled tag/varint arithmetic. The signal mix modelled:

- **CAN**, from measured per-message frame rates in
  [`tests/fixtures/mqtt_payload_stats.json`](../tests/fixtures/mqtt_payload_stats.json)
  (the same stats file WP4's round-trip tests use): **36 messages, ~853
  frames/s, ~2,962 signal updates/s**. Each signal update becomes one
  `DOUBLE` `Sample`. This is the measured donor car's real signal mix —
  the example profile — not a hypothetical.
- **GPS**, modelled at 50 Hz, 6 doubles per fix (lat, lon, speed, heading,
  plus two status doubles).
- **IMU**, modelled at 100 Hz, 10 doubles per frame — full rate, not the old
  stack's 10 Hz decimated `imu/live` copy.

Every sample in the tool is a `DOUBLE` (`Sample.d`), which is the
worst-case (largest) value encoding of the four the proto supports
(`DOUBLE`/`INT64`/`BOOL`/`STRING`), so these figures are conservative for
channels that could instead be `INT64` or `BOOL`.

On top of the raw protobuf bytes, the tool adds a **~20 B/msg** NATS framing
estimate (subject string `tele.<vehicle>.<source-class>` plus protocol
framing) — a conservative planning figure, not a byte-exact measurement,
per the tool's own comment.

**Not modelled by the tool, noted here as a caveat:** TCP/IP + 802.11 MAC
framing underneath NATS. Using the predecessor notebook's own header
assumptions (52 B IP+TCP, 36 B 802.11 MAC+LLC+FCS = 88 B/segment, and one
segment per batch since every batch here is well under the 1,460 B MSS),
this adds roughly **6–13%** on top of the NATS-framed figures below (~13%
at the smaller 10 ms batches, ~6% at 20 ms — the overhead is fixed per
segment, so it shrinks as a fraction as batches get bigger). This is the
inverse of the old per-signal JSON stream, where ~150 B messages meant
framing was often a *third or more* of the wire bytes: openlaps batches are
large enough (0.6–1.3 kB) that this framing genuinely is "a few %", not the
dominant cost the old design had to fight.

Reproduce with:

```
uv run python tools/size_batch.py --tick-ms 10
uv run python tools/size_batch.py --tick-ms 20
```

(both against the default stats file, `tests/fixtures/mqtt_payload_stats.json`).

## 3. Results: 10 ms vs 20 ms tick

Actual tool output, both ticks, default stats file:

| Tick | Samples/s | Batches/s | Mean batch bytes (protobuf only) | Framed batch bytes (+20 B NATS) | Offered load |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10 ms | 4,262 | 100 | 703 B | 723 B | **578.4 kbit/s** |
| 20 ms | 4,262 | 50 | 1,390 B | 1,410 B | **564.0 kbit/s** |

Samples/s is identical between ticks (same signal mix, same 1-second
window); what changes is how many times per second the fixed ~20 B
per-batch header (protobuf batch header: `registry_seq` + two `fixed64`
epoch fields, ~20 B on its own) and the ~20 B NATS framing estimate get
paid. Both ticks land in the same ballpark — see §7 for why tick length is
a weak lever here.

## 4. RBE scenario: PD16 voltage channels

The channel catalog supports per-channel report-by-exception (deadband +
`max_interval` heartbeat — see `WIRE_FORMAT.md` and the catalog schema).
`tools/size_batch.py` does not currently support excluding or decimating
individual streams, so this is computed **analytically**, not from a tool
run.

Worked example: six 100 Hz PD16 analog-input voltage channels (the
catalog's `pd16.*` namespace), each carrying a `{deadband: 0.05, max_interval:
5s}` RBE policy. At full rate these are 6 channels × 100 Hz = **600
samples/s**. Using this document's own measured per-sample cost (~15.9 B
marginal per `DOUBLE` sample, in line with the ~16 B planning figure — see
§2 and the marginal-cost check in the footnote below):

```
600 samples/s × ~16 B/sample ≈ 9,600 B/s ≈ 77 kbit/s  (worst case, values changing every tick)
```

Under RBE, once a voltage channel is steady within its 0.05 (V) deadband,
it collapses to its `max_interval` heartbeat: 1 sample per 5 s per channel,
6 channels → 1.2 samples/s ≈ 19 B/s — effectively free. So the **~77 kbit/s
figure is the worst-case saving**, realised in full only while those
channels are genuinely steady (car stationary, no switched loads toggling);
on track, vibration and switching activity will trigger more deadband
crossings than the idle case, so treat 77 kbit/s as an upper bound on the
RBE benefit for this stream, not a number to subtract unconditionally from
the headline offered load.

*(Marginal per-sample cost check: a 43-sample batch header-only vs.
header-plus-samples measures 703 B − 20 B header ≈ 683 B for 43 samples =
15.9 B/sample, computed directly from the generated bindings the same way
`size_batch.py` does.)*

## 5. Verdict vs. the HaLow PHY table

Required PHY rate at the standard **0.5 airtime-efficiency** planning
figure (802.11ah ACKs, backoff, retries, beacons — reused from the
predecessor notebook), using the slightly heavier 10 ms figure
(578.4 kbit/s) as the conservative case:

```
required_phy = offered / airtime_efficiency = 0.578 Mbit/s / 0.5 ≈ 1.2 Mbit/s
```

Against the HaLow PHY rate table (802.11ah, 1 spatial stream, long guard
interval — reused from the predecessor notebook's `HALOW_PHY_MBPS` table):

| Channel · MCS | PHY rate | Required (1.2 Mbit/s) | Fits? | Headroom |
| --- | ---: | ---: | :--: | ---: |
| 1 MHz · MCS0 | 0.30 Mbps | 1.2 Mbit/s | ✗ | — |
| 1 MHz · MCS4 | 1.80 Mbps | 1.2 Mbit/s | ✓ | 1.6× |
| 1 MHz · MCS7 | 3.00 Mbps | 1.2 Mbit/s | ✓ | 2.6× |
| 2 MHz · MCS0 | 0.65 Mbps | 1.2 Mbit/s | ✗ | — |
| **2 MHz · MCS4** | **3.90 Mbps** | 1.2 Mbit/s | **✓** | **~3.4×** |
| 2 MHz · MCS7 | 6.50 Mbps | 1.2 Mbit/s | ✓ | 5.6× |
| 4 MHz · MCS4 | 8.10 Mbps | 1.2 Mbit/s | ✓ | 7.0× |
| 4 MHz · MCS7 | 13.50 Mbps | 1.2 Mbit/s | ✓ | 11.7× |
| 8 MHz · MCS4 | 17.55 Mbps | 1.2 Mbit/s | ✓ | 15.2× |
| 8 MHz · MCS7 | 29.25 Mbps | 1.2 Mbit/s | ✓ | 25.3× |

**Verdict:** the full-rate stream fits **2 MHz MCS4** (3.90 Mbps PHY) with
**~3.4× headroom**, and even the narrower **1 MHz MCS4** (1.80 Mbps) works,
with ~1.6× headroom. Folding in the TCP/IP+802.11 framing caveat from §2
(worst case ~13% at 10 ms batches) nudges required PHY to ~1.3 Mbit/s,
which does not change either verdict — 2 MHz MCS4 headroom drops to ~3.0×,
still comfortable.

At 2 MHz MCS4, usable goodput (PHY × airtime efficiency) is 3.90 × 0.5 =
1.95 Mbit/s; against ~0.57 Mbit/s of telemetry that leaves roughly
**1.4 Mbit/s** free — enough headroom for a video stream in the ~1–2 Mbit/s
class sharing the same link. On 4 MHz MCS4 (usable 4.05 Mbit/s) the
telemetry+video budget is even more comfortable.

## 6. Comparison vs. the predecessor

| | Offered load | Fits 2 MHz MCS4 (3.90 Mbps)? |
| --- | ---: | :--: |
| Predecessor (JSON-over-MQTT, per-signal topics, `docs/mqtt_bandwidth.py` "as configured today") | ~2.4 Mbit/s | ✗ — required PHY ~4.8 Mbit/s |
| openlaps (protobuf `SampleBatch` over NATS, this document, 20 ms tick) | ~0.564 Mbit/s | ✓ — ~3.4× headroom |

That is a **~4.3×** reduction (2.4 / 0.564 ≈ 4.3) at **full data rate with
zero decimation** — and the new figure *includes* 100 Hz IMU, which the old
bridge topic list excluded entirely (`telemetry/imu/data` never crossed the
link; only a 10 Hz decimated `imu/live` copy did). The old design's problem
was never the signal values themselves — the predecessor notebook measured
actual payload bytes at only ~5% of the wire for the per-signal JSON
stream — it was one MQTT+TCP+MAC framing cost paid per signal, per
message. Batching many samples into one `SampleBatch` per tick (protobuf,
integer channel IDs instead of repeated name strings) amortizes that fixed
cost across dozens of samples instead of one.

## 7. Levers if we ever need more margin

None of these are needed to fit 2 MHz MCS4 today (§5); listed in rough
order of effort, for if a future signal mix (more CAN buses, higher rates)
erodes the margin.

- **`float32`/`UINT`-with-`scale` values — shipped, per-channel catalog
  levers.** As of the `format_version`/compact-encoding schema update,
  `Sample.value` offers `float` (4 B, `ValueType.FLOAT`) and a varint
  `uint64` (`ValueType.UINT`, as low as 1-2 B for small-magnitude values)
  alongside the original `double` (8 B) — see
  [`WIRE_FORMAT.md`](WIRE_FORMAT.md) "Value encoding" for the wire
  mechanics and the `scale`/`offset` fixed-point convention that lets a
  `UINT` channel carry a fractional physical value (e.g. coolant temp in
  Kelvin at `scale: 0.1`). These aren't hypothetical any more — they're
  levers a catalog author reaches for per-channel today via the catalog's
  `encode:` setting (see [`docs/CATALOG.md`](CATALOG.md)); the default for
  an unconfigured channel is still `double`, unchanged.

  Measured with `tools/size_batch.py --wire-profile {double,float32,mixed}`
  (`mixed` = float32 for analog channels, `UINT` for the naturally-integer
  slice, ~10% of samples — see the tool's docstring), NATS-framed offered
  load against this document's default signal mix:

  | Tick | `double` (baseline) | `float32` | `mixed` |
  | --- | ---: | ---: | ---: |
  | 10 ms | 578.4 kbit/s | 440.8 kbit/s (**−23.8%**) | 430.4 kbit/s (**−25.6%**) |
  | 20 ms | 564.0 kbit/s | 428.0 kbit/s (**−24.1%**) | 419.6 kbit/s (**−25.6%**) |

  In line with the earlier estimate (~25% from halving the value field on
  channels that don't need double's range), and confirms `mixed` buys only
  a couple more points over plain `float32` for this signal mix, since most
  of today's catalog is analog/real-valued rather than naturally-integer.
- **Columnar payload — reserved as `format_version 2`, not yet
  implemented.** Right now every sample pays a submessage tag+length plus
  its own `channel_id` and `t_offset_us` fields. `SampleBatch` reserves
  field numbers 10-15 (see `proto/telemetry.proto` and `WIRE_FORMAT.md`'s
  "Future: columnar payload (format_version 2)" section) for a future
  payload shape — one packed array of values and one packed array of
  offsets per channel, `channel_id` written once — that removes this
  per-sample framing entirely. It only pays off at the longer, degraded-
  link tick lengths (100+ ms) where a channel contributes many samples to
  one batch; at the 10-20 ms ticks modelled in this document, most
  channels contribute ≤1 sample per batch, so there's nothing to amortize
  yet. Estimated **~40% further** reduction on top of the float32/mixed
  numbers above, at those longer ticks. This is a wire-format-breaking
  change (a new payload shape gated behind `format_version`), so it
  remains a "if we actually need it" lever, not a first-choice one — the
  schema is future-proofed for it, nothing more.
- **Per-channel rate caps / RBE in the catalog.** Config-only, no code or
  wire-format change — this is what §4's PD16 example already models.
  Applying deadband + `max_interval` policies more broadly across the
  catalog (not just PD16 voltages) trades live fidelity for bandwidth on a
  per-channel basis, exactly where it matters least (steady-state analog
  monitoring channels, not fast-changing engine/chassis signals).
- **Longer ticks — marginal, and here's why.** §3 shows 10 ms → 20 ms only
  moves offered load from 578.4 to 564.0 kbit/s, a **2.5%** change, because
  the only tick-dependent cost is the fixed ~20 B/batch header (protobuf) +
  ~20 B/batch (NATS framing) being paid 100×/s instead of 50×/s — at most
  ~2,000 B/s of a ~72,000 B/s stream. Going longer than 20 ms buys almost
  nothing further on bandwidth while directly adding to live-feed latency
  (tick length is the batching-latency floor per `WIRE_FORMAT.md`), so it's
  not a lever worth reaching for here.

## 8. Caveats

- **Rates come from a 30 s garage capture spanning a real engine start**,
  not a track session (`tests/fixtures/mqtt_payload_stats.json`, sourced
  from the ~30.0 s CAN candump fixture and a ~4.99 s IMU candump). The
  Haltech ECU broadcast rates are device-configured and measured identical
  engine-off vs. running, but the PD16A's status messages are not:
  `PD16A_OUTPUT_STATUS` and `PD16A_DIAGNOSTICS` roughly **double** their
  rate once the engine runs (38→67 Hz and 10→20 Hz vs. the earlier key-on
  capture), which is why these figures moved when the fixture gained an
  engine start. A full-session track capture has still not been measured —
  re-measure before treating any of this as commissioned.
- **Airtime efficiency (0.5) is the softest input.** It's a standard
  planning figure for a clean link; range, interference and retries at a
  real track push it lower, and required PHY rate scales inversely with
  it. Section 5's headroom numbers should be read as "headroom at a clean
  link", not a guarantee under worse RF conditions.
- **QoS/acknowledgement pattern is materially different from the old
  stack, and unverified on real hardware.** The predecessor's MQTT bridge
  used per-message QoS 1 PUBACKs — a small but real per-message reverse-
  channel cost, explicitly modelled in `docs/mqtt_bandwidth.py`. openlaps'
  pit `nats-server` instead **sources** the vehicle's `TELE` stream as a
  durable, resumable pull-based batch replication (see `WIRE_FORMAT.md`'s
  "JetStream stream definitions") — the reverse-channel chatter pattern
  (consumer acks, sourcing flow control) is structurally different, not
  just smaller, and has not been bench-measured over real HaLow. Treat the
  numbers in this document as the *forward* (vehicle → pit) offered load
  only, and bench-measure the full round-trip behaviour on real hardware
  before treating any of this as commissioned — this is Phase 4's garage
  bench test, not a paper exercise.
