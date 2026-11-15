#!/usr/bin/env python3
"""Log once a second whether the car's Starlink has internet.

Run on the car board. Each second it opens a TCP connection to a public
address over IPv4 and over IPv6 (the tunnel prefers IPv6) and appends
`epoch,iso_time,v4_ms,v6_ms` to a CSV; an empty field means that check
failed within one second. Line the file up against GPS afterwards.

    nohup python3 starlink_watch.py >/dev/null 2>&1 &
"""

import socket
import sys
import time

OUT = sys.argv[1] if len(sys.argv) > 1 else "/home/luckfox/starlink-watch.csv"
TARGETS = ((socket.AF_INET, ("1.1.1.1", 443)), (socket.AF_INET6, ("2606:4700:4700::1111", 443)))


def probe(family, addr):
    s = socket.socket(family, socket.SOCK_STREAM)
    s.settimeout(1.0)
    t0 = time.monotonic()
    try:
        s.connect(addr)
        return f"{(time.monotonic() - t0) * 1000:.0f}"
    except OSError:
        return ""
    finally:
        s.close()


with open(OUT, "a", buffering=1) as f:
    if f.tell() == 0:
        f.write("epoch,time_utc,v4_ms,v6_ms\n")
    while True:
        start = time.time()
        row = [probe(fam, addr) for fam, addr in TARGETS]
        f.write(f"{start:.1f},{time.strftime('%H:%M:%S', time.gmtime(start))},{row[0]},{row[1]}\n")
        time.sleep(max(0.0, 1.0 - (time.time() - start)))
