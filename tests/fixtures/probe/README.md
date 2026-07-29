# `link_probe.py` parser fixtures

Inputs for `tests/test_link_probe.py`. `tools/link_probe.py` is the
instrument every Phase 4 number is read through, so its parsers are tested
against the shapes they will actually meet rather than against shapes
invented to match the parser.

**Provenance is recorded per fixture below, including where it is not yet a
bench capture.** `docs/plan/PHASE4.md` → "Ground rules" requires a number to
cite where it came from; the same standard applies to the inputs the tool
learns to read.

## Captured from a running system

| File | Captured from | How |
| --- | --- | --- |
| `varz-vehicle.json`, `varz-pit.json` | `nats-server` 2.12.12 | `GET /varz` on each end of a live vehicle/pit leafnode pair, configured as `deploy/nats/vehicle.conf` and `deploy/nats/pit.conf` configure it (domains `veh`/`pit`, the pit dialling the vehicle). |
| `leafz-vehicle.json`, `leafz-pit.json` | same pair | `GET /leafz`, with 300 real `SampleBatch` messages already across the link — so `in_msgs`/`out_msgs`/`in_bytes`/`out_bytes` and `rtt` are non-zero and the forward/reverse asymmetry is the real one. |
| `jsz-vehicle.json`, `jsz-pit.json` | same pair | `GET /jsz?streams=1&consumers=1`. The vehicle has `TELE` and `CMD` created with `src/agent/publisher.py::_ensure_streams`'s config; the pit has `TELE_VEHICLE` sourcing it with `deploy/provision_pit_streams.py`'s config, and a durable pull consumer named `ingest-writer` shaped like `src/pit/ingest_writer/writer.py::_subscribe`'s. The consumer was deliberately left mid-flight, so `num_pending`, `num_ack_pending` and `num_redelivered` are all non-zero — a fixture with a fully-drained consumer would not exercise the columns a dropout run reads. |
| `nft-counters.json` | `nftables` 1.0.9 | `nft -j list counters` against a real ruleset carrying two named counters on TCP `:7422`, after ~4 MB of traffic. Both the metainfo entry and the asymmetric byte counts are as nft emitted them. |
| `proc-net-dev.txt` | Linux 6.8 | `/proc/net/dev` on the bench SBC, verbatim, including the docker `veth*` interfaces — the parser has to find one named interface among many. |
| `health-ingest.json`, `health-live.json`, `health-ntrip.json` | this repo | `HealthState.snapshot()` from `src/pit/{ingest_writer,live_decoder,ntrip_client}/health.py` — the exact dict `serve_health()` serialises — with counters advanced to healthy 20 ms-tick values and the rate windows aged by a real second before `roll()`. |
| `health-session.json` | this repo | `_health_payload()` from `src/pit/session_control/service.py`. |

## Not yet captured — replace during P4.2

| File | Status |
| --- | --- |
| `iw-station-dump.txt` | **Transcribed to `iw`'s output format, not captured.** |
| `iw-station-dump-two.txt` | As above, two associated stations, for peer selection. |
| `iw-station-dump-sparse.txt` | As above, the vendor-driver case: no `tx bitrate`, no `tx retries`, no `expected throughput`. This is the shape that makes the `ubus` fallback exist, so it is tested explicitly. |
| `ubus-assoclist.json` | **Transcribed to `iwinfo`'s `assoclist` format, not captured.** Rates are in kbit/s, as `iwinfo` reports them. |

The machine these fixtures were built on has no `iw`, no wireless station
association and no access to the HaLowLink routers, so the four radio
fixtures are format-accurate transcriptions rather than captures. They are
enough to pin the parsers' behaviour, and they are **not** enough to prove
the parsers match what the deployed chipset actually prints — which is
exactly what varies between HaLow vendor drivers, and the reason
`--radio-adapter` is configuration in the first place.

**Re-capture them when the bench is stood up (P4.2), from the local router:**

```bash
ssh <halow-router> iw dev <iface> station dump > iw-station-dump.txt
```

```bash
ssh <halow-router> ubus call iwinfo assoclist '{"device":"<iface>"}' > ubus-assoclist.json
```

If the real output differs from these, the fixture is what changes — and the
diff is itself worth recording in the run's operator notes, because it is a
fact about the deployed radio.
