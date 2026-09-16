# Natsoft feed captures

`hand-built.jsonl` is a replay fixture in the timing feed's capture format
(`src/pit/timing_feed/capture.py`: one JSON object per line, `t` the arrival
time in Unix seconds, `doc` the raw XML document). It was built by
`tests/natsoft_docs.py` from the documented Natsoft document types, with
invented values: four cars, a six-hour race, a safety car, one stop, and
our car (27) winning.

It is a stand-in. The first time `openlaps-timing-feed` runs against a live
meeting it writes a real capture to `OPENLAPS_TIMING_FEED_CAPTURE_DIR`;
copy a session of that here, trim it, and replace this file. The tests
that read it assert on shape (rows land in every table, laps derive, the
flag intervals close), not on these invented numbers.
