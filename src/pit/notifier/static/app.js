// The annunciator (P7.2). Vanilla JS against the notifier's own API:
// GET /events (server-sent events: a snapshot on connect, then every
// change), POST /ack, POST /test. No framework, no build step.
"use strict";

const NAME_STORAGE = "openlaps-ack-name";
const SOUND_STORAGE = "openlaps-alert-sound";
const REPEAT_TONE_MS = 10000;

const $ = (id) => document.getElementById(id);
const show = (el, visible) => el.classList.toggle("hidden", !visible);

let ackName = localStorage.getItem(NAME_STORAGE) || "";
let soundOn = localStorage.getItem(SOUND_STORAGE) === "on";
let snapshot = null;
let audio = null;
let lastToneAt = 0;
let knownFingerprints = new Set();

// ---------------------------------------------------------------------------
// Sound. Browsers only allow audio after a user gesture, so the button both
// asks permission and remembers the answer.
// ---------------------------------------------------------------------------

function ensureAudio() {
  if (!audio) {
    audio = new (window.AudioContext || window.webkitAudioContext)();
  }
  if (audio.state === "suspended") {
    audio.resume();
  }
}

function tone(pattern) {
  if (!soundOn) {
    return;
  }
  ensureAudio();
  let at = audio.currentTime;
  pattern.forEach(([freq, len]) => {
    const osc = audio.createOscillator();
    const gain = audio.createGain();
    osc.type = "square";
    osc.frequency.value = freq;
    gain.gain.value = 0.15;
    osc.connect(gain).connect(audio.destination);
    osc.start(at);
    osc.stop(at + len);
    at += len + 0.08;
  });
}

const CRITICAL_TONE = [[880, 0.25], [660, 0.25], [880, 0.25], [660, 0.25]];
const WARNING_TONE = [[520, 0.2], [520, 0.2]];

function updateSoundButton() {
  $("sound-button").textContent = soundOn ? "Sound on" : "Enable sound";
  $("sound-button").classList.toggle("on", soundOn);
}

$("sound-button").addEventListener("click", () => {
  soundOn = !soundOn;
  localStorage.setItem(SOUND_STORAGE, soundOn ? "on" : "off");
  if (soundOn) {
    ensureAudio();
    tone([[660, 0.15]]);
  }
  updateSoundButton();
});

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function fmtTime(iso) {
  if (!iso) {
    return "";
  }
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtAge(seconds) {
  if (seconds == null) {
    return "";
  }
  const s = Math.round(seconds);
  if (s < 60) {
    return s + " s";
  }
  const m = Math.floor(s / 60);
  if (m < 60) {
    return m + " min " + (s % 60) + " s";
  }
  return Math.floor(m / 60) + " h " + (m % 60) + " min";
}

function renderActive() {
  const list = $("active-list");
  list.textContent = "";
  const active = snapshot.active || [];
  show($("all-clear"), active.length === 0);
  document.body.classList.toggle("critical", snapshot.unacknowledged_critical > 0);
  active.forEach((alert) => {
    const li = document.createElement("li");
    li.className = "alert " + alert.severity + (alert.acked_at ? " acked" : "");
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = alert.summary;
    const detail = document.createElement("div");
    detail.className = "detail";
    detail.textContent =
      alert.severity.toUpperCase() + " · since " + fmtTime(alert.started_at) +
      " (" + fmtAge(alert.age_s) + ")" +
      (alert.acked_at ? " · acknowledged by " + alert.acked_by + " at " + fmtTime(alert.acked_at) : "");
    li.appendChild(title);
    li.appendChild(detail);
    if (alert.runbook_url) {
      const link = document.createElement("a");
      link.href = "http://" + location.hostname + ":3000" + alert.runbook_url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "open panel";
      link.className = "panel-link";
      li.appendChild(link);
    }
    if (!alert.acked_at) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ack";
      button.textContent = "Acknowledge";
      button.addEventListener("click", () => acknowledge(alert.fingerprint));
      li.appendChild(button);
    }
    list.appendChild(li);
  });
}

function renderHistory() {
  const list = $("history-list");
  list.textContent = "";
  const history = (snapshot.history || []).slice().reverse();
  history.forEach((event) => {
    const li = document.createElement("li");
    li.className = "event " + event.kind + " " + event.severity;
    let text = fmtTime(event.at) + "  " + event.kind + "  " + event.summary;
    if (event.by) {
      text += " — " + event.by;
    }
    if (event.note) {
      text += ": " + event.note;
    }
    li.textContent = text;
    list.appendChild(li);
  });
}

function renderPipeline() {
  const hb = snapshot.heartbeat || {};
  const el = $("pipeline");
  el.classList.remove("ok", "down", "unknown");
  if (hb.ok === true) {
    el.classList.add("ok");
    el.textContent = "path alive · heartbeat " + fmtAge(hb.age_s) + " ago";
  } else if (hb.ok === false) {
    el.classList.add("down");
    el.textContent = "ALERT PATH SILENT · last heartbeat " + fmtAge(hb.age_s) + " ago";
  } else {
    el.classList.add("unknown");
    el.textContent = "path: no heartbeat yet";
  }
}

function render() {
  if (!snapshot) {
    return;
  }
  renderActive();
  renderHistory();
  renderPipeline();
}

// ---------------------------------------------------------------------------
// Noise policy: a tone on every new firing alert, then a repeat every ten
// seconds while any critical alert is unacknowledged.
// ---------------------------------------------------------------------------

function noteNewAlerts() {
  const current = new Set((snapshot.active || []).map((a) => a.fingerprint));
  let newest = null;
  (snapshot.active || []).forEach((a) => {
    if (!knownFingerprints.has(a.fingerprint) && !a.acked_at) {
      newest = a;
    }
  });
  knownFingerprints = current;
  if (newest) {
    tone(newest.severity === "critical" ? CRITICAL_TONE : WARNING_TONE);
    lastToneAt = Date.now();
  }
}

setInterval(() => {
  if (snapshot && snapshot.unacknowledged_critical > 0 && Date.now() - lastToneAt >= REPEAT_TONE_MS) {
    tone(CRITICAL_TONE);
    lastToneAt = Date.now();
  }
}, 1000);

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const decoded = await response.json();
  if (!response.ok) {
    throw new Error(decoded.error || "request failed (" + response.status + ")");
  }
  return decoded;
}

function notice(text, kind) {
  const el = $("notice");
  el.textContent = text;
  el.className = "notice " + (kind || "");
  show(el, Boolean(text));
  if (text) {
    setTimeout(() => show(el, false), 6000);
  }
}

async function acknowledge(fingerprint) {
  if (!ackName) {
    notice("Enter your name below before acknowledging.", "warn");
    $("name-input").focus();
    return;
  }
  try {
    await post("/ack", { fingerprint, by: ackName });
  } catch (err) {
    notice(err.message, "error");
  }
}

$("name-form").addEventListener("submit", (event) => {
  event.preventDefault();
  ackName = $("name-input").value.trim();
  localStorage.setItem(NAME_STORAGE, ackName);
  notice(ackName ? "Acknowledging as " + ackName : "Name cleared", "");
});

$("test-critical").addEventListener("click", () => post("/test", { severity: "critical" }).catch((e) => notice(e.message, "error")));
$("test-warning").addEventListener("click", () => post("/test", { severity: "warning" }).catch((e) => notice(e.message, "error")));

// ---------------------------------------------------------------------------
// The live feed. EventSource reconnects on its own; the first event after a
// (re)connect is always a full snapshot.
// ---------------------------------------------------------------------------

function connect() {
  const source = new EventSource("/events");
  source.addEventListener("snapshot", (event) => {
    snapshot = JSON.parse(event.data);
    $("queue").textContent = "queue: " + ((snapshot.queue && snapshot.queue.pending) || 0);
    noteNewAlerts();
    render();
  });
  source.addEventListener("message", () => {
    // Channel deliveries also arrive here; the snapshot that follows them
    // carries everything the page shows, so there is nothing to do.
  });
  source.onopen = () => {
    $("connection").textContent = "live";
    $("connection").className = "connection live";
  };
  source.onerror = () => {
    $("connection").textContent = "reconnecting…";
    $("connection").className = "connection down";
  };
}

// Health poll for the retry-queue depth, which is not part of the snapshot.
setInterval(async () => {
  try {
    const response = await fetch("/health");
    const health = await response.json();
    $("queue").textContent = "queue: " + health.queue.pending;
    $("queue").classList.toggle("busy", health.queue.pending > 0);
  } catch (err) {
    // The SSE connection indicator already says the service is unreachable.
  }
}, 5000);

$("name-input").value = ackName;
updateSoundButton();
connect();
// Ages tick without a new snapshot.
setInterval(() => {
  if (snapshot) {
    (snapshot.active || []).forEach((a) => { a.age_s += 1; });
    if (snapshot.heartbeat && snapshot.heartbeat.age_s != null) {
      snapshot.heartbeat.age_s += 1;
      const limit = snapshot.heartbeat.expected_s * 3;
      if (snapshot.heartbeat.age_s > limit) {
        snapshot.heartbeat.ok = false;
      }
    }
    render();
  }
}, 1000);
