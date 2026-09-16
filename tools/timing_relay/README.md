# Timing relay tools

The fallback sources for the timing feed (P7.10, `src/pit/timing_feed/`).
The service's first sources are a capture file and the Natsoft TCP feed;
these two exist for the cases where neither works.

## `relay.user.js` -- the browser relay

For events timed by a provider the feed client does not speak. A
dependency-free userscript for Tampermonkey or Violentmonkey: it reads the
rendered standings table on the Timing71 page (or the Natsoft page as a
fallback), maps the header text onto the `field_*` columns, and posts a
snapshot to `POST /ingest/snapshot` on the timing-feed service about once a
second. A badge in the corner of the page shows when the last post
succeeded and turns red when it stops.

1. Install a userscript manager in the browser on the pit laptop.
2. Open `relay.user.js` in the browser; the manager offers to install it.
3. Open the live timing page. Click the badge and paste the endpoint:
   `http://<pit-host>:8089/ingest/snapshot`.

The endpoint accepts posts from any origin on purpose: the caller is a
script on another site's page. The pit LAN is the boundary, as it is for
every other unauthenticated service in the stack (`deploy/README.md`).

What it reads is only as good as the table: a page that names its columns
oddly maps fewer of them, and a page that draws its standings in `div`s
rather than a `table` maps none. Add the header words to `COLUMNS` in the
script when a provider surprises you.

## `capture.py` -- the WebSocket frame capture

One run at one live session, to confirm the Natsoft web page's WebSocket
carries the same framed documents as the TCP feed. Drives Playwright,
which is not a project dependency; the docstring says how to run it from
a throwaway environment. After that it is a curiosity.

## What the service captures on its own

Every raw document the Natsoft client receives is appended to a capture
file in `OPENLAPS_TIMING_FEED_CAPTURE_DIR`, so a live session leaves a
replayable fixture behind without either of these tools. Replay it with
`OPENLAPS_TIMING_FEED_SOURCE=replay` and
`OPENLAPS_TIMING_FEED_REPLAY_FILE=<the file>`; see `example.env`.
