# GPS trace replay fixture

`wanneroo-trace.csv` is a ~110 s, ~20 Hz slice (2 200 fixes) of real
on-track driving at Wanneroo Raceway, extracted from the predecessor
logger's `gps.lp.gz` InfluxDB line-protocol dump
(`/mnt/data/logger/backups/backup_migration_tmp/gps.lp.gz`, June 2025
event). Speed in that dump is knots (`pyubx2`'s native RMC unit); this
fixture converts it to km/h so it can be dropped straight into an encoded
`$GPRMC` sentence without a units surprise downstream.

Columns: `t_s` (seconds from the start of the slice), `lat`, `lon`,
`speed_kmh`, `heading_deg`.

Used by `tools/replay.py`'s `--gps-trace` source: each row is encoded to a
synthetic RMC sentence and fed through the real serial collector's NMEA
decoder, exactly like a live receiver's own sentences.
