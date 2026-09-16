// ==UserScript==
// @name         openlaps timing relay
// @namespace    https://github.com/Robert-McMahon/openlaps
// @version      0.1.0
// @description  Reads the rendered standings table on a live-timing page and posts a snapshot to the pit's timing-feed service about once a second.
// @match        https://www.timing71.org/*
// @match        https://timing71.org/*
// @match        https://*.natsoft.com.au/*
// @match        http://*.natsoft.com.au/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      *
// @run-at       document-idle
// ==/UserScript==

// The browser relay (P7.10 source 3): for events timed by a provider the
// feed client does not speak. Dependency-free on purpose -- it runs inside
// Tampermonkey or Violentmonkey on the laptop that already has the timing
// page open in a tab -- and reads nothing but the table the page renders.
//
// It finds the largest table on the page, maps its header text onto the
// field_* column names, and POSTs {"source", "session", "cars"} to
// POST /ingest/snapshot on the timing-feed service. A small badge in the
// corner says when the last post succeeded, and turns red when it stops.
//
// Set the endpoint once: click the badge and paste the URL, e.g.
// http://192.168.12.10:8089/ingest/snapshot. It is stored per browser.

(function () {
  "use strict";

  const DEFAULT_ENDPOINT = "http://127.0.0.1:8089/ingest/snapshot";
  const INTERVAL_MS = 1000;

  // Header text (lower-cased, punctuation stripped) -> field_* column.
  const COLUMNS = {
    num: "car_number", no: "car_number", "#": "car_number", car: "car_number", number: "car_number",
    pos: "position", p: "position", position: "position",
    pic: "class_position", "cls pos": "class_position",
    class: "class", cls: "class", cat: "class",
    driver: "driver", drivers: "driver", name: "driver",
    laps: "laps", lap: "laps",
    last: "last_lap_s", "last lap": "last_lap_s", "last time": "last_lap_s",
    best: "best_lap_s", "best lap": "best_lap_s", fastest: "best_lap_s",
    gap: "gap_lead_s", "gap to leader": "gap_lead_s", lead: "gap_lead_s",
    int: "gap_next_s", interval: "gap_next_s", diff: "gap_next_s", next: "gap_next_s",
    s1: "sec1_s", s2: "sec2_s", s3: "sec3_s", sec1: "sec1_s", sec2: "sec2_s", sec3: "sec3_s",
    pits: "pit_count", pit: "pit_count", stops: "pit_count",
    state: "state", status: "state",
  };

  let endpoint = DEFAULT_ENDPOINT;
  try { endpoint = GM_getValue("openlaps_endpoint", DEFAULT_ENDPOINT); } catch (e) { /* no GM */ }

  const badge = document.createElement("div");
  badge.style.cssText =
    "position:fixed;right:8px;bottom:8px;z-index:2147483647;font:12px/1.4 monospace;" +
    "padding:4px 8px;border-radius:4px;background:#444;color:#fff;cursor:pointer;opacity:0.85";
  badge.textContent = "openlaps relay: idle";
  badge.title = "Click to set the timing-feed endpoint";
  badge.addEventListener("click", () => {
    const value = window.prompt("timing-feed snapshot endpoint", endpoint);
    if (value) {
      endpoint = value.trim();
      try { GM_setValue("openlaps_endpoint", endpoint); } catch (e) { /* no GM */ }
    }
  });
  document.documentElement.appendChild(badge);

  function normaliseHeader(text) {
    return text.replace(/[. ]/g, " ").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function largestTable() {
    let best = null;
    for (const table of document.querySelectorAll("table")) {
      const rows = table.querySelectorAll("tr").length;
      if (!best || rows > best.rows) best = { table, rows };
    }
    return best && best.rows >= 2 ? best.table : null;
  }

  function headerCells(table) {
    const headerRow =
      table.querySelector("thead tr") ||
      Array.from(table.querySelectorAll("tr")).find((row) => row.querySelector("th"));
    if (!headerRow) return null;
    return Array.from(headerRow.children).map((cell) => COLUMNS[normaliseHeader(cell.textContent)] || null);
  }

  function readSnapshot() {
    const table = largestTable();
    if (!table) return null;
    const columns = headerCells(table);
    if (!columns || !columns.includes("car_number")) return null;
    const cars = [];
    const bodyRows = table.querySelectorAll("tbody tr, tr");
    for (const row of bodyRows) {
      if (row.querySelector("th")) continue;
      const cells = Array.from(row.children);
      if (cells.length < 2) continue;
      const car = {};
      columns.forEach((column, index) => {
        if (!column || !cells[index]) return;
        const text = cells[index].textContent.replace(/ /g, " ").trim();
        if (text) car[column] = text;
      });
      if (car.car_number) cars.push(car);
    }
    if (!cars.length) return null;
    const session = {};
    const flag = document.querySelector("[class*='flag'], [data-flag]");
    if (flag) {
      const word = (flag.getAttribute("data-flag") || flag.className || flag.textContent || "")
        .toLowerCase()
        .match(/green|yellow|red|chequered|checkered|sc|fcy|none/);
      if (word) session.flag_state = word[0];
    }
    const clock = document.querySelector("[class*='remain'], [class*='clock']");
    if (clock) session.time_remaining_s = clock.textContent.trim();
    return { source: "relay", session, cars };
  }

  let lastOk = 0;
  let inFlight = false;

  function report(ok, detail) {
    const age = lastOk ? Math.round((Date.now() - lastOk) / 1000) : null;
    badge.style.background = ok ? "#2a6" : "#a33";
    badge.textContent = ok
      ? "openlaps relay: ok " + new Date(lastOk).toLocaleTimeString()
      : "openlaps relay: " + detail + (age === null ? "" : " (last ok " + age + "s ago)");
  }

  function post(body) {
    const payload = JSON.stringify(body);
    if (typeof GM_xmlhttpRequest === "function") {
      GM_xmlhttpRequest({
        method: "POST",
        url: endpoint,
        data: payload,
        headers: { "Content-Type": "application/json" },
        timeout: 4000,
        onload: (r) => { inFlight = false; if (r.status === 200) { lastOk = Date.now(); report(true); } else report(false, "HTTP " + r.status); },
        onerror: () => { inFlight = false; report(false, "unreachable"); },
        ontimeout: () => { inFlight = false; report(false, "timeout"); },
      });
      return;
    }
    // Without a userscript manager: a no-cors fetch can carry text/plain
    // and the service parses it as JSON. The response is opaque, so
    // "sent" is all this path can report.
    fetch(endpoint, { method: "POST", mode: "no-cors", body: payload, headers: { "Content-Type": "text/plain" } })
      .then(() => { inFlight = false; lastOk = Date.now(); report(true); })
      .catch(() => { inFlight = false; report(false, "unreachable"); });
  }

  function tick() {
    if (inFlight) return;
    const snapshot = readSnapshot();
    if (!snapshot) { report(false, "no standings table"); return; }
    inFlight = true;
    post(snapshot);
  }

  setInterval(tick, INTERVAL_MS);
  tick();
})();
