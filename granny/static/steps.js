"use strict";

// These summarize provider states, not whether a person saw a notification.
function summarizeSteps(status, online = true) {
  const step = (state, text) => ({state, text});
  if (!online) return {voice: step("offline", "Offline"), telegram: step("offline", "Offline"), calls: step("offline", "Offline"), confirmation: ""};
  const phase = status?.state_key;
  const voice = phase === "CHECKING" ? step("active", "Checking…")
    : phase === "COOLDOWN" ? step("done", "You’re okay")
    : phase === "ALERTED" ? step("done", /no clear response/i.test(status.reason || "") ? "No reply" : "Check finished")
    : step("waiting", "Waiting");
  const rows = Array.isArray(status?.deliveries) ? status.deliveries : [];
  const failedStates = ["failed", "busy", "no-answer", "canceled", "unconfirmed"];
  function channel(name) {
    if (!status?.[name]) return step("off", "Off");
    const entries = rows.filter(row => row.channel === name);
    if (!entries.length) return phase === "COOLDOWN" ? step("off", "Not needed")
      : phase === "ALERTED" ? step("active", "Starting…") : step("waiting", "Waiting");
    if (name === "calls" && entries.some(row => row.confirmation === "coming")) return step("done", "Family coming");
    if (name === "calls" && entries.some(row => row.state === "retry-wait")) return step("active", "Calling again soon");
    if (name === "calls" && entries.some(row => row.state === "retry-stopped")) return step("warning", "Retries stopped");
    const success = entries.filter(row => name === "telegram" ? row.state === "accepted"
      : ["in-progress", "completed"].includes(row.state)).length;
    const failures = entries.filter(row => failedStates.includes(row.state));
    if (success === entries.length) return step("done", name === "telegram" ? "Sent to Telegram"
      : entries.every(row => row.state === "completed") ? "Call finished" : "Connected");
    if (failures.length) {
      if (success) return step("warning", `${success}/${entries.length} ${name === "telegram" ? "sent" : "connected"}`);
      if (failures.some(row => row.state === "unconfirmed")) return step("warning", "Unconfirmed");
      if (failures.every(row => row.state === "no-answer" || row.state === "busy")) return step("error", "No answer");
      return step("error", "Failed");
    }
    if (success) return step("active", `${success}/${entries.length} ${name === "telegram" ? "sent" : "connected"}`);
    if (name === "telegram") return step("active", "Sending…");
    return step("active", entries.some(row => row.state === "ringing") ? "Ringing…" : "Dialing…");
  }
  const calls = rows.filter(row => row.channel === "calls");
  const confirmation = calls.some(row => row.confirmation === "coming") ? "Family confirmed they’re coming."
    : calls.length && calls.every(row => row.confirmation === "unavailable") ? "Family cannot come."
    : calls.some(row => row.state === "completed") ? "No one has confirmed they’re coming."
    : "";
  return {voice, telegram: channel("telegram"), calls: channel("calls"), confirmation};
}

// Presentation only: lost tracking is shown immediately. Require a steady
// recovery before showing Monitoring again; never delay the detector itself.
function createReadiness() {
  let stableSince = null;
  let interrupted = false;
  return function(status, now) {
    const live = Boolean(status?.frame_available);
    const tracked = live && Boolean(status?.tracking_available);
    if (!tracked) { stableSince = null; interrupted = true; }
    else if (stableSince === null) stableSince = now;
    const video = live ? "Video connected" : "Video interrupted · reconnecting";
    const base = {title:status?.state || "Starting", description:status?.reason || "", video,
      attention:["CHECKING","POSSIBLE_FALL"].includes(status?.state_key)};
    if (["CHECKING","ALERTED","COOLDOWN"].includes(status?.state_key)) return base;
    if (!live) return {...base,title:"Camera interrupted",attention:true,
      description:"Fresh video is unavailable. Reconnecting automatically; fall detection is paused."};
    if (!status?.calibrated) return {...base,title:"Calibrate standing",
      description:"Keep one person’s head, feet and floor visible. Hold the camera still, then click Calibrate."};
    if (!tracked) return {...base,title:"Body tracking paused",attention:true,
      description:"Keep one person fully in view, clear of furniture. Video is connected, but the body cannot be tracked reliably."};
    if (now - stableSince < 1000) return {...base,
      title:interrupted ? "Body tracking paused" : "Tracking stabilizing",attention:true,
      description:"Reacquiring a steady body view. Keep the camera still."};
    interrupted = false;
    return {...base,description:base.description || "Watching for a sustained posture near the floor."};
  };
}

if (typeof module !== "undefined") module.exports = {summarizeSteps, createReadiness};
