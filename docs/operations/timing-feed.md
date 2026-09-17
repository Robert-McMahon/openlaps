# Timing feed fallbacks

`openlaps-timing-feed` normally reads the Natsoft TCP feed or replays a capture.
Two tools cover providers and sessions where that path is unavailable.

## Browser relay

`tools/timing_relay/relay.user.js` is a dependency-free userscript for
Tampermonkey or Violentmonkey. It reads a supported live standings table and
posts a snapshot to:

```text
http://<pit-host>:8089/ingest/snapshot
```

The endpoint permits cross-origin posts because the caller runs on the timing
provider's page. Restrict access at the pit network boundary.

## WebSocket investigation

`tools/timing_relay/capture.py` uses Playwright to capture browser WebSocket
frames for protocol investigation. Playwright is intentionally not a project
dependency; use the isolated environment documented in the script.

## Built-in capture and replay

The timing-feed service appends received live documents to
`OPENLAPS_TIMING_FEED_CAPTURE_DIR`. Replay a capture with:

```text
OPENLAPS_TIMING_FEED_SOURCE=replay
OPENLAPS_TIMING_FEED_REPLAY_FILE=<capture path>
```

Use `none` as the source when only the HTTP and WebSocket ingest endpoints are
required.
