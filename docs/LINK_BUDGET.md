# Link budget: vehicle to pit

## 1. What this answers

Does the full-rate telemetry stream (every CAN signal, 50 Hz GPS and 100 Hz
IMU, with no decimation) fit over a HaLow radio link, and with how much
margin? If it stops fitting, what can be changed without redesigning the
wire format?

This is a static analysis. It runs
[`tools/size_batch.py`](https://github.com/Robert-McMahon/openlaps/blob/main/tools/size_batch.py)
against a measured signal mix, reports the resulting bit rate, and compares
it with the HaLow PHY rate table. The tool builds real serialized protobuf
messages in the openlaps wire format (`ChannelRegistry` and `SampleBatch`,
see [`WIRE_FORMAT.md`](WIRE_FORMAT.md) and
[`proto/telemetry.proto`](https://github.com/Robert-McMahon/openlaps/blob/main/proto/telemetry.proto)).
Re-run it when the signal mix changes.

**Result:** the modelled full-rate stream is about **0.57 Mbit/s** of
protobuf payload with a small framing allowance. On that basis it fits the
smallest HaLow channel and MCS combination worth considering (2 MHz MCS4)
with about **3.4×** headroom.

**Read section 7 before relying on this figure.** A bench measurement on
2026-11-22 found that the bytes actually sent per batch are about three
times the payload, because of protocol overhead the model does not
include.

## 2. Method

`tools/size_batch.py` builds one `SampleBatch` protobuf message, using the
generated Python bindings from `proto/telemetry.proto`, sized to a full
batching tick. It measures `len(batch.SerializeToString())` directly. The
signal mix modelled:

- **CAN**, from measured per-message frame rates in
  [`tests/fixtures/mqtt_payload_stats.json`](https://github.com/Robert-McMahon/openlaps/blob/main/tests/fixtures/mqtt_payload_stats.json):
  **36 messages, ~853 frames/s, ~2,962 signal updates/s**. Each signal
  update becomes one `DOUBLE` `Sample`. This is the measured signal mix of
  the car the example profile describes.
- **GPS**, modelled at 50 Hz, 6 doubles per fix (lat, lon, speed, heading,
  and two status values).
- **IMU**, modelled at 100 Hz, 10 doubles per frame.

Every sample in the tool is a `DOUBLE` (`Sample.d`), the largest of the
value encodings the proto supports, so the figures are conservative for
channels that could be `INT64` or `BOOL`.

On top of the protobuf bytes the tool adds an estimated **~20 B per
message** for NATS framing (the subject `tele.<vehicle>.<source-class>`
plus protocol framing). This is a planning estimate, not a measurement.

**Not modelled by the tool:** TCP/IP and 802.11 MAC framing under NATS.
Assuming 52 B of IP and TCP headers and 36 B of 802.11 MAC, LLC and FCS
(88 B per segment), and one segment per batch (every batch here is well
under the 1,460 B MSS), this adds about **6 to 13%** to the figures below:
about 13% at 10 ms batches and 6% at 20 ms. The overhead is fixed per
segment, so it is a smaller fraction of a larger batch. JetStream headers,
acknowledgements and TLS are also not modelled; see section 7.

Reproduce with:

```
uv run python tools/size_batch.py --tick-ms 10
uv run python tools/size_batch.py --tick-ms 20
```

Both use the default stats file, `tests/fixtures/mqtt_payload_stats.json`.

## 3. Results: 10 ms and 20 ms tick

Tool output for both ticks with the default stats file:

| Tick | Samples/s | Batches/s | Mean batch bytes (protobuf only) | Framed batch bytes (+20 B NATS) | Offered load |
| --- | ---: | ---: | ---: | ---: | ---: |
| 10 ms | 4,262 | 100 | 703 B | 723 B | **578.4 kbit/s** |
| 20 ms | 4,262 | 50 | 1,390 B | 1,410 B | **564.0 kbit/s** |

The number of samples per second is the same for both ticks. What changes
is how many times per second the fixed per-batch costs are paid: about
20 B of protobuf batch header (`registry_seq` and two `fixed64` epoch
fields) and the 20 B NATS framing estimate. Section 6 explains why tick
length has little effect.

## 4. RBE in the shipped profile

The channel catalog supports per-channel report-by-exception (deadband,
`min_interval`, `max_interval`; see `WIRE_FORMAT.md` and the
catalog schema). `profiles/example-club-racer/catalog.yaml` ships policies on
76 of its 145 channels; its header comment explains each one. They fall in
two kinds:

- **Lossless**: `{deadband: 0, max_interval: 5s}` on every bool, enum,
  status word, keypad button and monotonic counter (fuel used, trip
  distance). Report-on-change drops only a value identical to the last one
  sent, so every edge and counter step still goes out. The PDM brake switch
  uses the same rule with a 200 ms heartbeat. `car.trigger_counter`, which
  steps on every frame, is rate-limited to 5 Hz instead; a 16-bit count
  cannot wrap in 200 ms, so a reader can still recover every increment.
- **Lossy, below the noise floor**: the ~100 Hz PD16 input voltages
  (0.05 V, at most 10 Hz) and the slow pressures, angles, duties and pump
  current (2 kPa, 0.5°, 0.5 %, 0.2 A, at most 20 Hz). Each deadband sits
  well under every alarm and watch threshold on the same channel.

rpm, throttle, pedal, MAP, speeds, lambda, knock and the IMU stay at full rate.

**Measured on track** (2026-11-19), by replaying two on-track CAN captures
from the Collie race of 2026-11-14 through the agent's own decoder, catalog
mapping and `RbeFilter` (laps 32–34 under a safety car, 390 s; laps 42–44
green, 280 s). CAN-sourced rows only; GNSS and fused channels are extra:

| CAN rows/s                                  | laps 32–34 | laps 42–44 |
|---------------------------------------------|-----------:|-----------:|
| offered (no RBE)                            |      2,852 |      2,819 |
| 2026-11-08 policies (34 channels)           |      1,502 |      1,511 |
| + lossless discrete/counter tier (issue #40)|      1,392 |      1,405 |

So RBE now removes about half of all CAN samples. The lossless tier cuts
about 110 rows/s more, with no information lost. Each discrete channel drops
from 3.6–19.5 rows/s to its 0.2 rows/s heartbeat. `car.fuel_total_used` drops
from 7.5 to 2.5–4 rows/s at race pace, because the counter does step that
often. Most of what remains is data that changes on every sample: the IMU is ~750 rows/s (six
100 Hz axes and three 50 Hz Euler angles), the 36–38 Hz engine channels
another ~550, and the unpoliced PD16 output currents and loads ~100. More
RBE there would mean deadbanding data the timing, fusion and watch layers
actually use, which is the trade-off described in section 6.

At the ~16 B marginal cost of a `DOUBLE` sample (section 2, and the check below),
the 1,450 rows/s RBE removes are worth roughly 23 kB/s ≈ **185 kbit/s** of
offered load on track. A bench at idle does better because fewer channels
move, so a bench figure overstates the saving on track.

To rerun it: load the profile with `tools/_bench.py:load_catalog`, decode the
capture with `CanCollector.replay`, and count `RbeFilter.accept` per channel.

*(Marginal per-sample cost check: a 43-sample batch header-only vs.
header-plus-samples measures 703 B − 20 B header ≈ 683 B for 43 samples =
15.9 B/sample, computed directly from the generated bindings the same way
`size_batch.py` does.)*

## 5. Comparison with the HaLow PHY table

The required PHY rate is the offered load divided by an airtime efficiency
of **0.5**, a common planning figure for 802.11ah that allows for ACKs,
backoff, retries and beacons. Using the heavier 10 ms figure
(578.4 kbit/s):

```
required_phy = offered / airtime_efficiency = 0.578 Mbit/s / 0.5 ≈ 1.2 Mbit/s
```

Against the HaLow PHY rate table (802.11ah, 1 spatial stream, long guard
interval):

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

**Result:** the modelled stream fits **2 MHz MCS4** (3.90 Mbps PHY) with
about **3.4×** headroom, and fits **1 MHz MCS4** (1.80 Mbps) with about
1.6×. Adding the TCP/IP and 802.11 framing from section 2 (13% at worst)
raises the required PHY rate to about 1.3 Mbit/s and reduces the 2 MHz
MCS4 headroom to about 3.0×.

At 2 MHz MCS4 the usable rate (PHY rate × airtime efficiency) is
3.90 × 0.5 = 1.95 Mbit/s. Against 0.57 Mbit/s of modelled telemetry that
leaves about 1.4 Mbit/s. The car camera does not use this capacity: video
is restricted to the Starlink path (`deploy/video-tunnel/README.md`).

## 6. Ways to reduce the load

None of these is needed to fit 2 MHz MCS4 on the modelled figures. They
are listed roughly in order of effort.

- **`float32` and scaled `UINT` values (available now, per channel).**
  `Sample.value` offers `float` (4 B, `ValueType.FLOAT`) and a varint
  `uint64` (`ValueType.UINT`, 1 to 2 B for small values) as well as
  `double` (8 B). [`WIRE_FORMAT.md`](WIRE_FORMAT.md), "Value encoding",
  describes the encoding and the `scale`/`offset` convention that lets a
  `UINT` channel carry a fractional physical value (eg coolant temperature
  in kelvin at `scale: 0.1`). A catalog author selects these per channel
  with the `encode:` setting ([`CATALOG.md`](CATALOG.md)). The default for
  a channel with no setting is `double`.

  Measured with `tools/size_batch.py --wire-profile {double,float32,mixed}`
  (`mixed` is float32 for analog channels and `UINT` for the integer
  channels, about 10% of samples; see the tool's docstring). NATS-framed
  offered load for this document's signal mix:

  | Tick | `double` (baseline) | `float32` | `mixed` |
  | --- | ---: | ---: | ---: |
  | 10 ms | 578.4 kbit/s | 440.8 kbit/s (**−23.8%**) | 430.4 kbit/s (**−25.6%**) |
  | 20 ms | 564.0 kbit/s | 428.0 kbit/s (**−24.1%**) | 419.6 kbit/s (**−25.6%**) |

  `mixed` saves only about two points more than `float32` for this signal
  mix, because most of the catalog is analog.
- **Columnar payload (reserved as `format_version 2`, not implemented).**
  At present every sample has its own submessage tag and length,
  `channel_id` and `t_offset_us`. `SampleBatch` reserves field numbers
  10 to 15 (see `proto/telemetry.proto` and `WIRE_FORMAT.md`, "Future:
  columnar payload (format_version 2)") for a payload with one packed
  array of values and one of offsets per channel, with `channel_id`
  written once. It helps only at longer ticks (100 ms and above), where a
  channel contributes many samples to one batch. At 10 to 20 ms most
  channels contribute one sample or none per batch. The estimated saving
  at the longer ticks is about 40% on top of the figures above. It is a
  breaking change to the wire format.
- **Per-channel rate caps and RBE in the catalog.** This needs only a
  configuration change. Section 4 describes what the example profile does
  and what was measured. Every lossless policy is already applied, so any
  further policy is lossy and reduces live fidelity for that channel. The
  cheapest candidates are the PD16 output currents and loads and the slow
  temperatures.
- **Longer ticks (little effect).** Section 3 shows that going from 10 ms
  to 20 ms changes the offered load from 578.4 to 564.0 kbit/s, or
  **2.5%**. The only cost that depends on the tick is the fixed per-batch
  header and framing, about 2,000 B/s of a 72,000 B/s stream. A tick
  longer than 20 ms saves almost nothing more and adds directly to the
  delay of the stream, because the tick is the minimum batching delay
  (`WIRE_FORMAT.md`).
- **Leafnode compression.** The NATS leafnode compresses with S2
  (`s2_auto`, on by default at both ends). It is off below about 10 ms
  round-trip time and at its strongest setting on a slow link. On the
  bench (2026-11-22) a backlogged link that stayed connected carried
  about eight times more messages per bit once the strongest setting was
  in use.

## 7. Caveats

- **Measured bytes on the wire are about three times the payload.** On
  the bench on 2026-11-22 (vehicle board to pit host over a LAN, GNSS and
  derived channels only, 110 messages/s, 13 kB/s of payload), each batch
  of about 120 B cost about 390 B on the wire: 106 kbit/s of payload
  became 350 kbit/s. The difference is the JetStream acknowledgement
  subject, the `Nats-Msg-Id` header, TLS records and TCP/IP headers,
  which the model's 20 B allowance does not cover. The overhead is per
  message, so its share is smaller for the larger batches of a full CAN
  load, but that has not been measured. With
  `OPENLAPS_LIVE_SOURCE=vehicle` every batch crosses the link twice
  (684 kbit/s in the same test).
- **Live frames are small by comparison.** With the `direct` live source,
  97 selected channels of a replayed race CAN capture cost about
  20 kbit/s, and about 0.6 kbit/s when no dashboard is open
  (`WIRE_FORMAT.md`, "Live link").
- **The rates come from a 30 s garage capture that includes an engine
  start**, not a track session (`tests/fixtures/mqtt_payload_stats.json`,
  from a 30.0 s CAN candump and a 4.99 s IMU candump). The Haltech ECU's
  broadcast rates are set in the device and were measured the same with
  the engine off and running. The PD16A's status messages are not:
  `PD16A_OUTPUT_STATUS` and `PD16A_DIAGNOSTICS` about double their rate
  once the engine runs (38 to 67 Hz and 10 to 20 Hz). Section 4 has
  on-track row rates, but the bit rate of a full track session has not
  been measured.
- **The airtime efficiency of 0.5 is the least certain input.** It is a
  planning figure for a clean link. Range, interference and retries at a
  track reduce it, and the required PHY rate rises in inverse proportion.
  The headroom in section 5 is headroom on a clean link.
- **Traffic from pit to vehicle is not modelled.** The pit sources the
  vehicle's `TELE` stream (`WIRE_FORMAT.md`, "JetStream stream
  definitions"), which produces consumer acknowledgements and flow
  control messages in the reverse direction. The figures here are the
  vehicle-to-pit load only. The full exchange has not been measured over
  a HaLow radio.
