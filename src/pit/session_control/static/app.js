// Session-control operator UI (P5.6). Vanilla JS against the service's own
// HTTP API: GET /session, GET /roster, GET /health, POST /session/*. The
// API key is asked for once and held in this browser's localStorage, sent
// as the bearer header on every gated call.
"use strict";

const KEY_STORAGE = "openlaps-session-api-key";

const $ = (id) => document.getElementById(id);
const show = (el, visible) => el.classList.toggle("hidden", !visible);

let apiKey = localStorage.getItem(KEY_STORAGE) || "";
let session = null; // last GET /session payload
let rosterData = null; // last GET /roster payload
// Server-minus-browser clock offset, from the payload's own timestamp, so
// the elapsed readouts survive a pit laptop with a drifted clock.
let clockOffsetMs = 0;

// ---------------------------------------------------------------------------
// API plumbing
// ---------------------------------------------------------------------------

async function api(path, body) {
  const options = { method: body === undefined ? "GET" : "POST" };
  options.headers = { Authorization: "Bearer " + apiKey };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 401) {
    forgetKey();
    throw new Error("API key required");
  }
  const decoded = await response.json();
  if (!response.ok) {
    throw new Error(decoded.error || "request failed (" + response.status + ")");
  }
  return decoded;
}

function forgetKey() {
  apiKey = "";
  localStorage.removeItem(KEY_STORAGE);
  show($("key-section"), true);
  for (const id of ["status-section", "start-section", "driver-section", "end-section"]) {
    show($(id), false);
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function setNotice(kind, text) {
  const notice = $("notice");
  notice.className = "notice " + kind;
  notice.textContent = text;
}

function fmtClock(ms) {
  return new Date(ms).toLocaleTimeString([], { hour12: false });
}

function fmtDuration(ms) {
  if (ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  return h > 0 ? h + ":" + rest : rest;
}

function serverNow() {
  return Date.now() + clockOffsetMs;
}

function renderSession() {
  const badge = $("status-badge");
  const detail = $("status-detail");
  const active = session !== null && session.status === "active";
  const ended = session !== null && session.status === "ended";

  if (session === null || session.status === "none") {
    badge.className = "status-badge none";
    badge.textContent = "NO SESSION";
    detail.innerHTML = "";
    show($("stint-table"), false);
  } else {
    badge.className = "status-badge " + session.status;
    badge.textContent = active
      ? "ACTIVE — " + session.driver + " in the car"
      : "ENDED — " + session.session_type;
    const rows = [
      ["Type", session.session_type],
      ["Track", session.track_name || "—"],
      ["Car", session.car || "—"],
      ["Stint", "#" + session.stint_number + " — " + session.driver],
      ["Session time", fmtDuration(serverNow() - session.session_start)],
    ];
    if (active) {
      rows.push(["Stint time", fmtDuration(serverNow() - session.stint_start)]);
    }
    detail.innerHTML = "";
    for (const [term, value] of rows) {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = term;
      dd.textContent = value;
      detail.append(dt, dd);
    }
    renderStints(active);
  }

  show($("start-section"), Boolean(apiKey) && !active);
  show($("driver-section"), Boolean(apiKey) && active);
  show($("end-section"), Boolean(apiKey) && active);
  if (ended) resetEndButton();
  updateDriverSelect();
  renderPlan();
}

// The driver select marks whoever is in the car and preselects the first
// driver who is not: the common action is a change, and any number of
// changes (including back to an earlier driver) is supported. Rebuilt only
// when the in-car driver changes, so it never fights an operator who has
// the list open.
let markedDriver = null;

function updateDriverSelect() {
  if (rosterData === null) return;
  const current =
    session !== null && session.status === "active" ? session.driver : null;
  if (current === markedDriver) return;
  markedDriver = current;
  const select = $("driver-select");
  select.innerHTML = "";
  for (const driver of rosterData.drivers) {
    const option = document.createElement("option");
    option.value = driver;
    option.textContent = driver === current ? driver + " (in car)" : driver;
    select.append(option);
  }
  const next = rosterData.drivers.find((driver) => driver !== current);
  if (next !== undefined) select.value = next;
}

function renderStints(active) {
  const rows = $("stint-rows");
  rows.innerHTML = "";
  const stints = [...(session.stints || [])];
  if (active) {
    stints.push({
      driver: session.driver,
      stint_number: session.stint_number,
      start_ms: session.stint_start,
      end_ms: null,
    });
  }
  for (const stint of stints) {
    const tr = document.createElement("tr");
    if (stint.end_ms === null) tr.className = "current";
    const end = stint.end_ms === null ? serverNow() : stint.end_ms;
    for (const text of [
      "#" + stint.stint_number,
      stint.driver,
      fmtClock(stint.start_ms),
      stint.end_ms === null ? "in car" : fmtClock(stint.end_ms),
      fmtDuration(end - stint.start_ms),
    ]) {
      const td = document.createElement("td");
      td.textContent = text;
      tr.append(td);
    }
    rows.append(tr);
  }
  show($("stint-table"), stints.length > 0);
}

function renderHealth(payload) {
  const health = $("health");
  const banner = $("banner");
  if (payload === null) {
    health.className = "health down";
    health.textContent = "unreachable";
    banner.className = "banner down";
    banner.textContent = "session-control is unreachable — nothing below can work";
    show(banner, true);
    return;
  }
  const problems = [];
  if (!payload.database.connected) problems.push("database disconnected");
  if (!payload.nats.connected) problems.push("NATS disconnected");
  if (payload.database.pending > 0) {
    problems.push(payload.database.pending + " DB write(s) queued");
  }
  if (problems.length > 0) {
    health.className = "health degraded";
    health.textContent = problems.join(", ");
    banner.className = "banner warn";
    banner.textContent =
      problems.join("; ") + " — actions are accepted and queued, not lost";
    show(banner, true);
  } else {
    health.className = "health";
    health.textContent = "DB ✓  NATS ✓";
    show(banner, false);
  }
}

// ---------------------------------------------------------------------------
// Data refresh
// ---------------------------------------------------------------------------

async function refreshSession() {
  if (!apiKey) return;
  try {
    session = await api("/session");
    if (typeof session.timestamp === "number" && session.timestamp > 0) {
      clockOffsetMs = session.timestamp - Date.now();
    }
    renderSession();
  } catch (error) {
    // The health banner is the reachability story; a failed poll here is
    // either that or a cleared key, both already visible.
  }
}

async function refreshHealth() {
  try {
    const response = await fetch("/health");
    renderHealth(await response.json());
  } catch (error) {
    renderHealth(null);
  }
}

async function loadRoster() {
  rosterData = await api("/roster");
  fillSelect($("start-type"), rosterData.session_types);
  fillSelect($("start-driver"), rosterData.drivers);
  markedDriver = undefined; // force a rebuild against the fresh roster
  updateDriverSelect();
  const tracks = $("track-list");
  tracks.innerHTML = "";
  for (const name of rosterData.tracks || []) {
    const option = document.createElement("option");
    option.value = name;
    tracks.append(option);
  }
}

function fillSelect(select, values) {
  select.innerHTML = "";
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  }
}

// The optional backdate: a datetime-local value is interpreted in this
// device's timezone, which is the operator's, which is the track's.
function parseAt(input) {
  if (!input.value) return null;
  const ms = new Date(input.value).getTime();
  return Number.isFinite(ms) ? ms : null;
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function perform(label, path, body) {
  try {
    session = await api(path, body);
    setNotice("ok", label + " — done");
    renderSession();
    return true;
  } catch (error) {
    setNotice("error", label + " failed: " + error.message);
    return false;
  }
}

$("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  apiKey = $("key-input").value.trim();
  localStorage.setItem(KEY_STORAGE, apiKey);
  $("key-input").value = "";
  show($("key-section"), false);
  show($("status-section"), true);
  try {
    await loadRoster();
    await refreshSession();
    await refreshPlan();
    setNotice("ok", "Unlocked");
  } catch (error) {
    setNotice("error", error.message);
  }
});

$("start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await perform("Start session", "/session/start", {
    session_type: $("start-type").value,
    driver: $("start-driver").value,
    track_name: $("start-track").value.trim(),
    car: $("start-car").value.trim(),
  });
});

$("driver-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = { driver: $("driver-select").value };
  const at = parseAt($("driver-at"));
  if (at !== null) body.at = at;
  if (await perform("Driver change", "/session/driver", body)) {
    $("driver-at").value = "";
  }
});

// Two-step end: first press arms, second press acts, and six idle seconds
// disarm. A misclicked end during a race is a real cost.
let endArmTimer = null;

function resetEndButton() {
  const button = $("end-button");
  button.classList.remove("armed");
  button.textContent = "End session";
  if (endArmTimer !== null) {
    clearTimeout(endArmTimer);
    endArmTimer = null;
  }
}

$("end-button").addEventListener("click", async () => {
  const button = $("end-button");
  if (!button.classList.contains("armed")) {
    button.classList.add("armed");
    button.textContent = "Press again to end the session";
    endArmTimer = setTimeout(resetEndButton, 6000);
    return;
  }
  resetEndButton();
  const body = {};
  const at = parseAt($("end-at"));
  if (at !== null) body.at = at;
  if (await perform("End session", "/session/end", body)) {
    $("end-at").value = "";
  }
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Race plan (P7.8). One revision per save; the form always shows the latest
// and says who saved it and when. The stops textarea is a tiny grammar so a
// tired operator types "lap 42 refuel Driver B", not JSON.
// ---------------------------------------------------------------------------

let plan = null; // last GET /session/plan payload

function toLocalInput(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
    "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
}

function stopsToText(stops) {
  return (stops || []).map((s) => {
    const when = s.at_lap != null ? "lap " + s.at_lap
      : new Date(s.at_ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    return when + " " + s.type + (s.driver_in ? " " + s.driver_in : "");
  }).join("\n");
}

function parseStops(text) {
  const stops = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    let m = line.match(/^lap\s+(\d+)\s+(refuel|service)(?:\s+(.+))?$/i);
    if (m) {
      stops.push({ at_lap: Number(m[1]), type: m[2].toLowerCase(), driver_in: m[3] ? m[3].trim() : null });
      continue;
    }
    m = line.match(/^(\d{1,2}):(\d{2})\s+(refuel|service)(?:\s+(.+))?$/i);
    if (m) {
      const at = new Date();
      at.setHours(Number(m[1]), Number(m[2]), 0, 0);
      stops.push({ at_ms: at.getTime(), type: m[3].toLowerCase(), driver_in: m[4] ? m[4].trim() : null });
      continue;
    }
    throw new Error("cannot read stop line: " + line);
  }
  return stops;
}

function renderPlan() {
  const hasSession = session !== null && session.status !== "none";
  show($("plan-section"), Boolean(apiKey) && hasSession);
  if (!hasSession) return;
  const current = plan && plan.plan;
  if (!current) {
    $("plan-status").textContent = "No plan entered for this session yet.";
    return;
  }
  $("plan-status").textContent =
    "Revision " + current.revision + ", saved " + fmtClock(current.updated_at_ms) +
    (current.updated_by ? " by " + current.updated_by : "") + ".";
}

function fillPlanForm() {
  const current = plan && plan.plan;
  if (!current) return;
  $("plan-end-at").value = current.race_end_at_ms ? toLocalInput(current.race_end_at_ms) : "";
  $("plan-end-laps").value = current.race_end_laps == null ? "" : current.race_end_laps;
  $("plan-authority").value = current.end_authority;
  $("plan-tank").value = current.tank_l;
  $("plan-usable").value = current.usable_fuel_l;
  $("plan-refuel-min").value = current.refuel_min_s / 60;
  $("plan-service-min").value = current.service_typical_s / 60;
  const limits = current.driver_limits || {};
  $("plan-max-continuous").value = limits.max_continuous_min == null ? "" : limits.max_continuous_min;
  $("plan-max-total").value = limits.max_total_min == null ? "" : limits.max_total_min;
  $("plan-min-rest").value = limits.min_rest_min == null ? "" : limits.min_rest_min;
  $("plan-stops").value = stopsToText(current.planned_stops);
  $("plan-car-number").value = current.car_number || "";
  if (current.updated_by && !$("plan-by").value) $("plan-by").value = current.updated_by;
}

async function refreshPlan() {
  if (!apiKey || session === null || session.status === "none") {
    plan = null;
    renderPlan();
    return;
  }
  try {
    const fresh = await api("/session/plan");
    const changed = JSON.stringify(fresh) !== JSON.stringify(plan);
    plan = fresh;
    renderPlan();
    if (changed) fillPlanForm();
  } catch (error) {
    $("plan-status").textContent = "Plan unavailable: " + error.message;
  }
}

function numberOrNull(id) {
  const value = $(id).value.trim();
  return value === "" ? null : Number(value);
}

$("plan-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  let body;
  try {
    body = {
      race_end_at_ms: parseAt($("plan-end-at")),
      race_end_laps: numberOrNull("plan-end-laps"),
      end_authority: $("plan-authority").value,
      tank_l: numberOrNull("plan-tank"),
      usable_fuel_l: numberOrNull("plan-usable"),
      refuel_min_s: Math.round((numberOrNull("plan-refuel-min") || 0) * 60),
      service_typical_s: Math.round((numberOrNull("plan-service-min") || 0) * 60),
      driver_limits: {
        max_continuous_min: numberOrNull("plan-max-continuous"),
        max_total_min: numberOrNull("plan-max-total"),
        min_rest_min: numberOrNull("plan-min-rest"),
      },
      planned_stops: parseStops($("plan-stops").value),
      car_number: $("plan-car-number").value.trim() || null,
      updated_by: $("plan-by").value.trim() || null,
    };
  } catch (error) {
    setNotice("error", error.message);
    return;
  }
  try {
    plan = await api("/session/plan", body);
    setNotice("ok", "Race plan saved as revision " + plan.plan.revision);
    renderPlan();
  } catch (error) {
    setNotice("error", "Race plan not saved: " + error.message);
  }
});

async function boot() {
  await refreshHealth();
  if (!apiKey) {
    show($("key-section"), true);
    show($("status-section"), false);
    return;
  }
  try {
    await loadRoster();
    await refreshSession();
    await refreshPlan();
  } catch (error) {
    setNotice("error", error.message);
  }
}

setInterval(refreshHealth, 5000);
setInterval(refreshSession, 2000);
setInterval(refreshPlan, 5000);
setInterval(() => {
  if (session !== null && session.status === "active") renderSession();
}, 1000);
boot();
