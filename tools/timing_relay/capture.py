#!/usr/bin/env python3
"""Record every WebSocket frame the Natsoft live timing page exchanges.

P7.10 source 5, and a one-off: the Natsoft web page loads an obfuscated
client that opens a binary WebSocket and renders the standings itself.
The TCP feed behind it is documented and is what ``openlaps-timing-feed``
speaks; this script exists to confirm, at one live session, that the
WebSocket carries the same framed documents -- after which it is a
curiosity. Nothing in the pit depends on it.

It drives a real browser with Playwright, which is deliberately *not* a
project dependency (phase ground rule: no new dependency without a
sentence, and this one is used once). Run it from a throwaway
environment::

    uv tool run --from playwright playwright install chromium
    uv run --with playwright tools/timing_relay/capture.py \\
        https://www.natsoft.com.au/live/ --out frames.jsonl

Every frame in both directions is written as one JSON line with its
direction, arrival time and payload (text as-is, binary base64). If the
payloads start with ``!@#`` the framing is the TCP protocol's and
``pit.timing_feed.framing`` will read the concatenated payloads.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", help="the live timing page")
    parser.add_argument("--out", default="natsoft-frames.jsonl", help="where to write frames")
    parser.add_argument("--seconds", type=float, default=600.0, help="how long to record")
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "capture: playwright is not installed; run with `uv run --with playwright` "
            "after `playwright install chromium` (see the module docstring)",
            file=sys.stderr,
        )
        return 2

    out = Path(args.out)
    frames = 0
    with out.open("a", encoding="utf-8") as handle, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        page = browser.new_page()

        def record(direction: str, url: str, payload: str | bytes) -> None:
            nonlocal frames
            record_out: dict[str, object] = {"t": time.time(), "dir": direction, "url": url}
            if isinstance(payload, bytes):
                record_out["b64"] = base64.b64encode(payload).decode("ascii")
                record_out["magic"] = payload[:3] == b"!@#"
            else:
                record_out["text"] = payload
                record_out["magic"] = payload.startswith("!@#")
            handle.write(json.dumps(record_out, separators=(",", ":")) + "\n")
            handle.flush()
            frames += 1

        def on_websocket(ws) -> None:
            print(f"capture: websocket opened {ws.url}", file=sys.stderr)
            ws.on("framereceived", lambda payload: record("in", ws.url, payload))
            ws.on("framesent", lambda payload: record("out", ws.url, payload))
            ws.on("close", lambda _ws: print("capture: websocket closed", file=sys.stderr))

        page.on("websocket", on_websocket)
        page.goto(args.url, wait_until="domcontentloaded")
        deadline = time.monotonic() + args.seconds
        try:
            while time.monotonic() < deadline:
                page.wait_for_timeout(1000)
        except KeyboardInterrupt:
            pass
        browser.close()
    print(f"capture: {frames} frame(s) written to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
