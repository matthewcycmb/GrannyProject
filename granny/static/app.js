"use strict";
const byId = (id) => document.getElementById(id);
const buttons = [...document.querySelectorAll("[data-action]")];
let current = null;
let connected = false;
let busy = false;
let lastEvent = "";
let frameUrl = null;
let hasFrame = false;
let lastDeliveries = "";
let lastSteps = "";
let readiness = createReadiness();

function renderSteps() {
  const steps = summarizeSteps(current, connected);
  const signature = JSON.stringify(steps);
  if (signature === lastSteps) return;
  lastSteps = signature;
  const marks = {done: "✓", active: "◌", waiting: "○", off: "—", error: "×", warning: "!", offline: "?"};
  for (const name of ["voice", "telegram", "calls"]) {
    const row = byId(`step-${name}`);
    const value = steps[name];
    row.className = `step ${value.state}`;
    row.querySelector(".step-mark").textContent = marks[value.state];
    row.querySelector(".step-status").textContent = value.text;
  }
  byId("family-confirmation").textContent = steps.confirmation;
  byId("family-confirmation").hidden = !steps.confirmation;
}

function renderDeliveries() {
  const rows = Array.isArray(current?.deliveries) ? current.deliveries : [];
  const signature = JSON.stringify([rows, current?.telegram, current?.calls, current?.confirm_calls, current?.listen_calls, connected]);
  if (signature === lastDeliveries) return;
  lastDeliveries = signature;
  byId("call-mode-note").textContent = current?.listen_calls ? "Listen-only call audio is on. Your Mac microphone stays off during the call."
    : current?.confirm_calls ? "Family confirmation is on. Mac call playback requires an upgraded Twilio account."
    : "A connected call does not mean someone is coming. Their confirmation appears separately when enabled.";
  const labels = {queued: "Queued", sending: "Sending", accepted: "Accepted", initiated: "Dialing",
    ringing: "Ringing", "in-progress": "Connected", completed: "Ended", busy: "Busy",
    "no-answer": "No answer", canceled: "Cancelled", failed: "Failed", unconfirmed: "Unconfirmed"};
  const amounts = {queued: 12, sending: 30, initiated: 35, ringing: 60, "in-progress": 85};
  for (const channel of ["telegram", "calls"]) {
    const list = byId(`${channel}-progress`);
    const entries = rows.filter((row) => row.channel === channel);
    byId(`${channel}-state`).textContent = !connected ? "Offline" : !current?.[channel] ? "Off" : entries.length ? "Live status" : "Ready";
    list.replaceChildren();
    if (!entries.length) {
      const waiting = document.createElement("p");
      waiting.className = "delivery-empty";
      waiting.textContent = current?.[channel] ? "Waiting for an alert" : "Not enabled for this session";
      list.append(waiting);
    }
    for (const row of entries) {
      const failed = ["failed", "busy", "no-answer", "canceled", "unconfirmed"].includes(row.state);
      const pending = ["queued", "sending", "initiated", "ringing", "in-progress"].includes(row.state);
      const item = document.createElement("article");
      item.className = `recipient ${failed ? "delivery-failed" : pending ? "delivery-pending" : "delivery-complete"}`;
      const heading = document.createElement("div");
      heading.className = "recipient-heading";
      const name = document.createElement("strong");
      name.textContent = row.label;
      const badge = document.createElement("span");
      badge.textContent = channel === "calls" && row.state === "sending" ? "Dialing" : labels[row.state] || "Checking";
      heading.append(name, badge);
      const track = document.createElement("div");
      track.className = "delivery-track";
      const bar = document.createElement("span");
      bar.style.width = `${amounts[row.state] || 100}%`;
      track.append(bar);
      const detail = document.createElement("p");
      detail.textContent = !connected ? "Monitor disconnected; last known status shown" : row.detail;
      item.append(heading, track, detail);
      if (channel === "calls") {
        const reply = document.createElement("p");
        reply.className = row.confirmation === "coming" ? "family-confirmed" : "family-response";
        reply.textContent = ({coming: "✓ Family member confirmed they can come", unavailable: "Family member said they cannot come",
          "no-response": "No commitment to come was received", pending: "Waiting for family confirmation",
          "not-requested": "Confirmation requires listen-only call mode"})[row.confirmation] || "No family confirmation received";
        item.append(reply);
        if (row.audio !== "off") {
          const audio = document.createElement("p");
          audio.className = "call-audio";
          audio.textContent = ({live: "◉ Listening to the call on this Mac", ended: "Call audio finished",
            unavailable: "Live call audio unavailable", waiting: "Waiting for call audio",
            muted: "Call audio muted on this Mac", "other-call": "Another family call is playing on this Mac",
            "upgrade-required": "Mac playback unavailable on Twilio trial"})[row.audio] || "Call audio unavailable";
          item.append(audio);
        }
      }
      list.append(item);
    }
  }
}

function updateButtons() {
  const checking = current?.state_key === "CHECKING";
  const alerted = current?.state_key === "ALERTED";
  for (const button of buttons) {
    const action = button.dataset.action;
    button.disabled = !connected || busy ||
      (action === "okay" && !checking) ||
      (action === "help" && alerted) ||
      (["calibrate", "simulate"].includes(action) && (checking || alerted)) ||
      (action === "stop_voice" && (!alerted || !current?.voice_repeating)) ||
      (action === "stop_call_audio" && !current?.call_listening) ||
      (action === "calibrate" && !current?.frame_available);
  }
}

function connection(okay) {
  connected = okay;
  byId("connection").classList.toggle("muted", !okay);
  byId("connection").replaceChildren(Object.assign(document.createElement("span"), {className: "dot"}), document.createTextNode(okay ? "Connected locally" : "Disconnected"));
  byId("connection-warning").hidden = okay;
  if (!okay) {
    byId("voice-backup").textContent = 'Voice backup status unavailable. Check the Terminal running Granny.';
    readiness = createReadiness();
    byId("state").textContent = "Monitor disconnected";
    byId("state-description").textContent = "Check the Terminal running Granny Project. Updates resume when it reconnects.";
    byId("state-block").className = "state-block attention";
    byId("countdown").hidden = true;
    hasFrame = false;
  }
  cameraVisibility();
  renderSteps();
  renderDeliveries();
  updateButtons();
}

function cameraVisibility() {
  const visible = connected && current?.frame_available && hasFrame;
  byId("camera-feed").hidden = !visible;
  byId("camera-placeholder").hidden = visible;
}

function event(message) {
  if (message === lastEvent) return;
  lastEvent = message;
  const list = byId("activity");
  list.querySelector(".empty-event")?.remove();
  const item = document.createElement("li");
  const time = document.createElement("time");
  time.textContent = new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
  const text = document.createElement("span");
  text.textContent = message;
  item.append(time, text);
  list.prepend(item);
  while (list.children.length > 12) list.lastElementChild.remove();
}

async function refresh() {
  try {
    const response = await fetch("/api/status", {cache: "no-store", signal: AbortSignal.timeout(2500)});
    if (!response.ok) throw new Error("Monitor unavailable");
    current = await response.json();
    if (!current.running) throw new Error("Monitor stopped responding");
    const presentation = readiness(current, performance.now());
    byId("state").textContent = presentation.title;
    byId("state-description").textContent = presentation.description;
    const stateClass = current.state_key === "ALERTED" ? "alert" : presentation.attention ? "attention" : "";
    byId("state-block").className = `state-block ${stateClass}`;
    byId("countdown").hidden = current.remaining_seconds === null;
    byId("seconds").textContent = current.remaining_seconds ?? "";
    byId("camera-status").textContent = presentation.video;
    byId("tracking-detail").textContent = current.camera;
    byId("audio-status").textContent = current.audio;
    byId("voice-backup").textContent = current.voice_backup_ready
      ? 'Voice backup ON · Say “help” to alert immediately, even without the camera.'
      : current.state_key === 'CHECKING'
        ? 'Say “help” to alert now, or “I am okay” in a quiet pause.'
        : current.state_key === 'ALERTED'
          ? 'Alerts started. Repeating “help” will not send them again.'
          : current.voice_enabled ? current.audio : 'Voice is off. The Help button alerts immediately.';
    byId("heard").textContent = current.heard;
    byId("alert-status").textContent = current.alerts;
    const enabled = [current.telegram && "Telegram", current.calls && "Phone calls"].filter(Boolean);
    byId("notification-mode").textContent = enabled.length ? `${enabled.join(" + ")} ON` : "Practice mode";
    byId("mode-note").textContent = enabled.length ? "Real test alerts to selected contacts" : "No messages or calls will be sent";
    byId("calibration-badge").textContent = current.calibrated ? "Standing position calibrated" : "Calibration needed";
    byId("fps").textContent = `${current.fps.toFixed(1)} pose frames / sec`;
    event(`${current.state} · ${current.alerts}`);
    connection(true);
  } catch (_) {
    if (connected) event("Connection to the monitor lost");
    connection(false);
  }
}

async function statusLoop() {
  await refresh();
  setTimeout(statusLoop, 400);
}

async function frameLoop() {
  try {
    if (connected && current?.frame_available) {
      const response = await fetch("/api/frame.jpg", {cache: "no-store", signal: AbortSignal.timeout(2000)});
      if (!response.ok) throw new Error("Camera unavailable");
      const nextUrl = URL.createObjectURL(await response.blob());
      const oldUrl = frameUrl;
      byId("camera-feed").src = nextUrl;
      frameUrl = nextUrl;
      if (oldUrl) URL.revokeObjectURL(oldUrl);
    } else {
      hasFrame = false;
    }
  } catch (_) {
    hasFrame = false;
  }
  cameraVisibility();
  setTimeout(frameLoop, 100);
}
byId("camera-feed").addEventListener("load", () => {hasFrame = true; cameraVisibility();});
byId("camera-feed").addEventListener("error", () => {hasFrame = false; cameraVisibility();});

for (const button of buttons) {
  button.addEventListener("click", async () => {
    if (!connected || busy || !current) return;
    const action = button.dataset.action;
    const incident = current.incident_id;
    const token = current.control_token;
    busy = true;
    updateButtons();
    const message = byId("action-message");
    message.className = "";
    message.textContent = "Applying…";
    try {
      if (action === "calibrate") {
        const checkCalibrationState = () => {
          if (!connected || current.control_token !== token || current.incident_id !== incident ||
              ["CHECKING", "ALERTED"].includes(current.state_key)) {
            throw new Error("Calibration cancelled because the monitor changed. Review its status and try again.");
          }
        };
        for (let seconds = 5; seconds > 0; seconds--) {
          checkCalibrationState();
          message.textContent = `Calibrating in ${seconds}… Stand still with your head and feet visible for the final two seconds.`;
          await new Promise((resolve) => setTimeout(resolve, 1000));
        }
        checkCalibrationState();
        message.textContent = "Checking your standing position…";
      }
      const response = await fetch("/api/action", {
        method: "POST", headers: {"Content-Type": "application/json", "X-Granny-Token": token},
        body: JSON.stringify({action, incident_id: incident}),
        signal: AbortSignal.timeout(5000),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Action could not be applied");
      message.textContent = ({calibrate: "Calibrated. Monitoring is active.", simulate: "Voice check started. Say help now, or I am okay during a quiet pause.", okay: "Response submitted. Check the current incident status above.", help: "Help response submitted. Check notification status above.", stop_voice: "Voice stopped. Previously sent alerts remain active.", stop_call_audio: "Call audio muted on this Mac. The phone call continues.", reset: "Monitor reset. Previously sent messages cannot be recalled."})[button.dataset.action];
      await refresh();
    } catch (error) {
      message.textContent = error.name === "TimeoutError" || error.name === "TypeError" ? "Connection interrupted. Check the incident status before retrying." : error.message;
      message.className = "error";
    } finally {
      busy = false;
      updateButtons();
    }
  });
}
statusLoop();
frameLoop();
