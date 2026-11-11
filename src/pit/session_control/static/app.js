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
  closeTimeline();
  show($("timeline-edit"), false);
  show($("reopen-button"), false);
  for (const id of ["status-section", "start-section", "driver-section", "end-section",
    "flag-section", "fuel-section"]) {
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

// A time of day, with the date in front whenever it is not today: a session
// left open overnight, or looked at days later, must not read as today's.
function fmtClock(ms) {
  const when = new Date(ms);
  const time = when.toLocaleTimeString([], { hour12: false });
  if (when.toDateString() === new Date(serverNow()).toDateString()) return time;
  return when.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) +
    " " + time;
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
    closeTimeline();
    show($("timeline-edit"), false);
    show($("reopen-button"), false);
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
    const red = openRed();
    if (red !== null) {
      rows.push(["Flag", "RED for " + fmtDuration(serverNow() - red.start_ms)]);
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
    if (timelineSessionId !== null && timelineSessionId !== session.session_id) closeTimeline();
    show($("timeline-edit"), Boolean(apiKey) && timelineSessionId === null);
    show($("reopen-button"), Boolean(apiKey) && ended && timelineSessionId === null);
    if (!ended) resetReopenButton();
  }

  show($("start-section"), Boolean(apiKey) && !active);
  show($("driver-section"), Boolean(apiKey) && active);
  show($("end-section"), Boolean(apiKey) && active);
  show($("flag-section"), Boolean(apiKey) && active);
  show($("fuel-section"), Boolean(apiKey) && active);
  renderFlag();
  if (ended) resetEndButton();
  updateDriverSelect();
  renderPlan();
  renderStrategy();
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
// A field filled by setExact() — from a stored time or a picked line
// crossing — keeps that exact millisecond for as long as its text is
// untouched; the field itself only shows whole seconds.
function parseAt(input) {
  if (!input.value) return null;
  if (input.dataset.exactText === input.value) return Number(input.dataset.exactMs);
  const ms = new Date(input.value).getTime();
  return Number.isFinite(ms) ? ms : null;
}

function setExact(input, ms) {
  input.value = toLocalInputSeconds(ms);
  input.dataset.exactMs = String(ms);
  input.dataset.exactText = input.value;
}

function clearAt(input) {
  input.value = "";
  delete input.dataset.exactMs;
  delete input.dataset.exactText;
}

function openRed() {
  if (session === null || session.status !== "active") return null;
  const reds = session.reds || [];
  const last = reds[reds.length - 1];
  return last !== undefined && last.end_ms === null ? last : null;
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function perform(label, path, body, hint) {
  try {
    // An action returns the wire payload, which has no stint list: read
    // the full session back rather than render half of it.
    await api(path, body);
    setNotice("ok", label + " — done");
    await refreshSession();
    return true;
  } catch (error) {
    setNotice("error", label + " failed: " + error.message + (hint || ""));
    return false;
  }
}

// A refused backdate is nearly always a mistyped date or AM for PM. Say
// what the field was read as and what it was compared against.
function backdateHint(at) {
  if (at === null || session === null) return "";
  return " (the time entered reads as " + fmtDateTime(at) +
    "; the stint started " + fmtDateTime(session.stint_start) + ")";
}

function fmtDateTime(ms) {
  return new Date(ms).toLocaleString([], {
    weekday: "short", day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
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
  const outAt = parseAt($("driver-out-at"));
  if (at !== null) body.at = at;
  if (outAt !== null) body.out_at = outAt;
  const earliest = outAt !== null ? outAt : at;
  if (await perform("Driver change", "/session/driver", body, backdateHint(earliest))) {
    clearAt($("driver-at"));
    clearAt($("driver-out-at"));
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
  if (await perform("End session", "/session/end", body, backdateHint(at))) {
    clearAt($("end-at"));
  }
});

// Reopen: the way back from an end pressed by mistake. Same two-step arm
// as the end button, because reopening the wrong session is also a cost.
let reopenArmTimer = null;

function resetReopenButton() {
  const button = $("reopen-button");
  button.classList.remove("armed");
  button.textContent = "Reopen session";
  if (reopenArmTimer !== null) {
    clearTimeout(reopenArmTimer);
    reopenArmTimer = null;
  }
}

$("reopen-button").addEventListener("click", async () => {
  const button = $("reopen-button");
  if (!button.classList.contains("armed")) {
    button.classList.add("armed");
    button.textContent = "Press again to reopen: the last stint carries on";
    reopenArmTimer = setTimeout(resetReopenButton, 6000);
    return;
  }
  resetReopenButton();
  await perform("Reopen session", "/session/reopen", {});
});

// Red flag: one button that says what pressing it will do.
function renderFlag() {
  const red = openRed();
  const button = $("flag-button");
  if (red === null) {
    $("flag-status").textContent = "The race is running.";
    button.textContent = "Red flag: race stopped";
    button.className = "danger";
  } else {
    $("flag-status").textContent =
      "RED FLAG since " + fmtClock(red.start_ms) + ". Driver clocks are paused.";
    button.textContent = "Green: racing again";
    button.className = "primary";
  }
}

$("flag-button").addEventListener("click", async () => {
  const going = openRed() === null ? "red" : "green";
  const body = {};
  const at = parseAt($("flag-at"));
  if (at !== null) body.at = at;
  const label = going === "red" ? "Red flag" : "Green flag";
  if (await perform(label, "/session/" + going, body)) clearAt($("flag-at"));
});

$("fuel-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = { level_l: Number($("fuel-level").value) };
  const at = parseAt($("fuel-at"));
  if (at !== null) body.at = at;
  if (await perform("Fuel mark", "/session/fuel", body)) clearAt($("fuel-at"));
});

// ---------------------------------------------------------------------------
// Line crossings. GET /session/events lists the car's completed laps and pit
// entries and exits; a small select above a time field offers them, and
// picking one fills the field with the exact crossing time. The field stays
// editable for the cases a crossing cannot cover: no GPS, a practice day.
// ---------------------------------------------------------------------------

let crossings = []; // newest first: { at_ms, type, line, lap_number }
const RECENT_LAPS = 60;

async function refreshCrossings() {
  if (!apiKey || session === null || session.status === "none") {
    crossings = [];
    return;
  }
  try {
    crossings = (await api("/session/events")).events || [];
  } catch (error) {
    // The pickers simply offer nothing; typing a time still works.
  }
}

function crossingLabel(crossing) {
  const lap = crossing.lap_number === null ? "" : "lap " + crossing.lap_number + " ";
  const what = crossing.type === "pit_entry" ? "pit entry"
    : crossing.type === "pit_exit" ? "pit exit" : "finished";
  const line = crossing.type === "lap_completed" || !crossing.line ? "" : " (" + crossing.line + ")";
  return lap + what + line + " — " + fmtClock(crossing.at_ms);
}

// Build the select for one time field. `kinds` orders what is offered: the
// first kind is the one this field usually wants, so it is listed first.
function attachPicker(holder, input, kinds) {
  const select = document.createElement("select");
  select.className = "pick-select";
  const fill = () => {
    select.innerHTML = "";
    const first = document.createElement("option");
    first.value = "";
    first.textContent = crossings.length > 0
      ? "Pick a line crossing…" : "No line crossings recorded — type the time";
    select.append(first);
    for (const kind of kinds) {
      let offered = crossings.filter((crossing) => crossing.type === kind);
      if (kind === "lap_completed") offered = offered.slice(0, RECENT_LAPS);
      for (const crossing of offered) {
        const option = document.createElement("option");
        option.value = String(crossing.at_ms);
        option.textContent = crossingLabel(crossing);
        select.append(option);
      }
    }
  };
  fill();
  // Rebuilt when opened rather than on a timer, so the list is current and
  // never changes under a finger.
  select.addEventListener("focus", fill);
  select.addEventListener("change", () => {
    if (select.value) setExact(input, Number(select.value));
    select.value = "";
  });
  holder.append(select);
}

for (const holder of document.querySelectorAll("span.pick")) {
  attachPicker(holder, $(holder.dataset.for), holder.dataset.kinds.split(" "));
}

// ---------------------------------------------------------------------------
// Edit timeline. The current session — active or ended — restated in one
// form and saved in one POST /session/timeline: every stint with its driver,
// start and end, the red-flag periods and the fuel marks. The form is built
// once when opened and left alone by the polling refresh, so it never
// fights an operator who is typing in it.
// ---------------------------------------------------------------------------

let timelineSessionId = null; // the session the open form belongs to, if any

function toLocalInputSeconds(ms) {
  const pad = (n) => String(n).padStart(2, "0");
  return toLocalInput(ms) + ":" + pad(new Date(ms).getSeconds());
}

function timeField(labelText, ms, kinds, required) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const holder = document.createElement("span");
  const input = document.createElement("input");
  input.type = "datetime-local";
  input.step = "1";
  input.required = required;
  if (ms !== null && ms !== undefined) setExact(input, ms);
  attachPicker(holder, input, kinds);
  label.append(holder, input);
  return { label, input };
}

function rowShell(container, title, onRemove) {
  const row = document.createElement("div");
  row.className = "row";
  const heading = document.createElement("div");
  heading.className = "row-title";
  heading.textContent = title;
  row.append(heading);
  const buttons = document.createElement("div");
  buttons.className = "row-buttons";
  if (onRemove) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "Remove";
    remove.addEventListener("click", onRemove);
    buttons.append(remove);
  }
  container.append(row);
  return { row, heading, buttons };
}

// The stint rows are rebuilt from this list on every structural change
// (split, remove); each entry holds what has been typed so far.
let stintDrafts = []; // { driver, start, end } as ms or null

function readStintDrafts() {
  for (const draft of stintDrafts) {
    if (!draft.inputs) continue;
    draft.driver = draft.inputs.driver.value;
    draft.start = parseAt(draft.inputs.start);
    draft.end = draft.inputs.end ? parseAt(draft.inputs.end) : null;
  }
}

function driverChoices(current) {
  const names = [...((rosterData && rosterData.drivers) || [])];
  for (const draft of stintDrafts) {
    if (draft.driver && !names.includes(draft.driver)) names.push(draft.driver);
  }
  if (current && !names.includes(current)) names.push(current);
  return names;
}

function renderStintDrafts() {
  const container = $("timeline-stints");
  container.innerHTML = "";
  const active = session !== null && session.status === "active";
  stintDrafts.forEach((draft, index) => {
    const last = index === stintDrafts.length - 1;
    const open = last && active;
    const shell = rowShell(
      container,
      "Stint #" + (index + 1) + (open ? " (in the car now)" : ""),
      stintDrafts.length > 1 ? () => {
        readStintDrafts();
        stintDrafts.splice(index, 1);
        renderStintDrafts();
      } : null,
    );
    const driverLabel = document.createElement("label");
    driverLabel.textContent = "Driver";
    const driver = document.createElement("select");
    fillSelect(driver, driverChoices(draft.driver));
    if (draft.driver) driver.value = draft.driver;
    driverLabel.append(driver);
    const start = timeField(
      index === 0 ? "Started (also the session start)" : "Started (pit exit)",
      draft.start, ["pit_exit", "lap_completed"], true);
    shell.row.append(driverLabel, start.label);
    let end = null;
    if (!open) {
      end = timeField(
        last ? "Ended (also the session end)" : "Ended (pit entry)",
        draft.end, ["pit_entry", "lap_completed"], true);
      shell.row.append(end.label);
    }
    const split = document.createElement("button");
    split.type = "button";
    split.className = "secondary";
    split.textContent = "Add a driver change in this stint";
    split.addEventListener("click", () => {
      readStintDrafts();
      const original = stintDrafts[index];
      const others = driverChoices(null).filter((name) => name !== original.driver);
      stintDrafts.splice(index + 1, 0, {
        driver: others[0] || original.driver,
        start: null,
        end: original.end,
      });
      original.end = null;
      renderStintDrafts();
    });
    shell.buttons.prepend(split);
    shell.row.append(shell.buttons);
    draft.inputs = { driver, start: start.input, end: end ? end.input : null };
  });
}

function addRedRow(startMs, endMs) {
  const container = $("timeline-reds");
  const shell = rowShell(container, "Red flag", () => shell.row.remove());
  const start = timeField("Race stopped at", startMs, ["lap_completed", "pit_entry"], true);
  const end = timeField("Went green at (empty if still stopped)", endMs,
    ["lap_completed", "pit_exit"], false);
  shell.row.append(start.label, end.label, shell.buttons);
  shell.row.fields = { start: start.input, end: end.input };
}

function addMarkRow(atMs, levelL) {
  const container = $("timeline-marks");
  const shell = rowShell(container, "Fuel mark", () => shell.row.remove());
  const at = timeField("As of", atMs, ["pit_exit", "pit_entry"], true);
  const levelLabel = document.createElement("label");
  levelLabel.textContent = "Litres in the tank";
  const level = document.createElement("input");
  level.type = "number";
  level.min = "0";
  level.step = "0.5";
  level.required = true;
  if (levelL !== null && levelL !== undefined) level.value = levelL;
  levelLabel.append(level);
  shell.row.append(at.label, levelLabel, shell.buttons);
  shell.row.fields = { at: at.input, level };
}

function closeTimeline() {
  timelineSessionId = null;
  stintDrafts = [];
  for (const id of ["timeline-stints", "timeline-reds", "timeline-marks"]) {
    $(id).innerHTML = "";
  }
  show($("timeline-form"), false);
}

$("timeline-edit").addEventListener("click", async () => {
  if (session === null || session.status === "none") return;
  closeTimeline();
  await refreshCrossings();
  timelineSessionId = session.session_id;
  stintDrafts = (session.stints || []).map((stint) => ({
    driver: stint.driver, start: stint.start_ms, end: stint.end_ms,
  }));
  if (session.status === "active") {
    stintDrafts.push({ driver: session.driver, start: session.stint_start, end: null });
  }
  renderStintDrafts();
  for (const red of session.reds || []) addRedRow(red.start_ms, red.end_ms);
  for (const mark of session.fuel_marks || []) addMarkRow(mark.at_ms, mark.level_l);
  show($("timeline-form"), true);
  show($("timeline-edit"), false);
  show($("reopen-button"), false);
});

$("timeline-add-red").addEventListener("click", () => addRedRow(null, null));
$("timeline-add-mark").addEventListener("click", () => {
  const tank = plan && plan.plan ? plan.plan.tank_l : null;
  addMarkRow(null, tank);
});

$("timeline-cancel").addEventListener("click", () => {
  closeTimeline();
  renderSession();
});

$("timeline-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  readStintDrafts();
  const body = {
    stints: stintDrafts.map((draft) => ({
      driver: draft.driver, start: draft.start, end: draft.end,
    })),
    reds: [...$("timeline-reds").children].map((row) => ({
      start: parseAt(row.fields.start), end: parseAt(row.fields.end),
    })),
    fuel_marks: [...$("timeline-marks").children].map((row) => ({
      at: parseAt(row.fields.at), level_l: Number(row.fields.level.value),
    })),
  };
  if (await perform("Edit timeline", "/session/timeline", body)) {
    closeTimeline();
    renderSession();
  }
});

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
  $("plan-fuel-scale").value = current.fuel_used_scale == null ? 1 : current.fuel_used_scale;
  if (!$("fuel-level").value) $("fuel-level").value = current.tank_l;
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

// ---------------------------------------------------------------------------
// Strategy (P7.9). Read-only: the strategy service's latest evaluation for
// this session, from GET /session/strategy. The lower bound is the number
// that gets radioed, so it is the one shown.
// ---------------------------------------------------------------------------

let strategy = null; // last GET /session/strategy payload

function fmtLapTime(seconds) {
  if (seconds == null) return "--";
  const m = Math.floor(seconds / 60);
  const s = (seconds - m * 60).toFixed(1).padStart(4, "0");
  return m + ":" + s;
}

function fmtSeconds(seconds) {
  return seconds == null ? "--" : fmtDuration(seconds * 1000);
}

function strategyRows(s) {
  const window = s.window_open_lap == null ? (s.stops_needed === 0 ? "no stop needed" : "--")
    : "laps " + s.window_open_lap + " to " + s.window_close_lap;
  return [
    ["Fuel remaining", s.fuel_remaining_l == null ? "--"
      : s.fuel_remaining_l.toFixed(1) + " L (" + s.rebase_confidence + ")", false],
    ["Burn per lap", s.burn_l_per_lap == null ? "--"
      : s.burn_l_per_lap.toFixed(2) + " L over " + s.burn_laps + " laps", false],
    ["Laps to dry (lower bound)", s.laps_to_dry_lo == null ? "--"
      : s.laps_to_dry_lo.toFixed(1) + " (up to " + s.laps_to_dry_hi.toFixed(1) + ")", false],
    ["Time to dry", fmtSeconds(s.time_to_dry_s_lo), false],
    ["Pit window", window, false],
    ["Target lap", fmtLapTime(s.target_lap_s), false],
    ["Driver time left", (s.driver || "--") + ": " + fmtSeconds(s.driver_time_remaining_s),
      s.driver_time_remaining_s != null && s.driver_time_remaining_s < 600],
    ["Refuel release", s.refuel_release_at_ms == null ? "--"
      : fmtClock(s.refuel_release_at_ms) + " (" + fmtSeconds(s.refuel_remaining_s) + " to go)", false],
  ];
}

function renderStrategy() {
  const hasSession = session !== null && session.status !== "none";
  show($("strategy-section"), Boolean(apiKey) && hasSession);
  if (!hasSession) return;
  const current = strategy && strategy.strategy;
  const grid = $("strategy-grid");
  grid.replaceChildren();
  if (!current) {
    $("strategy-status").textContent = "The strategy service has not evaluated this session yet.";
    $("strategy-stops").textContent = "";
    return;
  }
  $("strategy-status").textContent =
    "Evaluated " + fmtClock(current.time_ms) + " after lap " + (current.lap_number ?? "--") +
    (current.plan_revision ? ", plan revision " + current.plan_revision : ", no plan") + ".";
  for (const [label, value, warn] of strategyRows(current)) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value;
    if (warn) dd.classList.add("warn");
    grid.append(dt, dd);
  }
  const stops = (current.stop_plan || []).map((stop) =>
    "lap " + stop.lap + " " + stop.type + (stop.driver_in ? " " + stop.driver_in : "") +
    (stop.delta_laps ? " (" + (stop.delta_laps > 0 ? "+" : "") + stop.delta_laps + " vs plan)" : ""));
  $("strategy-stops").textContent = stops.length
    ? "Stops the numbers say: " + stops.join("; ") + "."
    : (current.stops_needed === 0 ? "No further stop needed on current fuel." : "");
}

async function refreshStrategy() {
  if (!apiKey || session === null || session.status === "none") {
    strategy = null;
    renderStrategy();
    return;
  }
  try {
    strategy = await api("/session/strategy");
    renderStrategy();
  } catch (error) {
    $("strategy-status").textContent = "Strategy unavailable: " + error.message;
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
      fuel_used_scale: numberOrNull("plan-fuel-scale"),
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
    await refreshStrategy();
    await refreshCrossings();
  } catch (error) {
    setNotice("error", error.message);
  }
}

setInterval(refreshHealth, 5000);
setInterval(refreshSession, 2000);
setInterval(refreshPlan, 5000);
setInterval(refreshStrategy, 5000);
setInterval(refreshCrossings, 10000);
setInterval(() => {
  if (session !== null && session.status === "active") renderSession();
}, 1000);
boot();
