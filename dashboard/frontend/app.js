"use strict";

// Thin SPA over the dashboard REST + WebSocket API. No build step.
const api = {
  async versions() { return (await fetch("api/versions").then(r => r.json())).versions; },
  async listBots() { return fetch("api/bots").then(r => r.json()); },
  async createBot(body) {
    const r = await fetch("api/bots", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  async chat(id, message) {
    const r = await fetch(`api/bots/${id}/chat`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  },
  stop(id) { return fetch(`api/bots/${id}/stop`, { method: "POST" }); },
  connect(id) { return fetch(`api/bots/${id}/connect`, { method: "POST" }); },
  remove(id) { return fetch(`api/bots/${id}`, { method: "DELETE" }); },
};

const state = { bots: [], selected: null, ws: null, mapTimer: null, mapUrl: null, mapBusy: false };
const VISION_SMALL_REFRESH_MS = 30000;
const VISION_FULL_REFRESH_MS = 1800;

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

// -- bot list ---------------------------------------------------------------
async function refreshBots() {
  state.bots = await api.listBots();
  renderBotList();
  if (state.selected && !state.bots.find(b => b.id === state.selected)) {
    selectBot(null);
  }
}

function renderBotList() {
  const list = $("#bot-list");
  list.innerHTML = "";
  if (!state.bots.length) {
    list.append(el("li", "muted", "No bots yet."));
    return;
  }
  for (const bot of state.bots) {
    const li = el("li");
    if (bot.id === state.selected) li.classList.add("active");
    li.append(el("span", `dot ${bot.state}`));
    const info = el("div");
    info.append(el("div", "bot-name", bot.username));
    info.append(el("div", "bot-sub", `${bot.host}:${bot.port} · ${bot.version}`));
    li.append(info);
    li.append(el("div", "bot-meta muted", bot.state));
    li.onclick = () => selectBot(bot.id);
    list.append(li);
  }
}

// -- detail + websocket -----------------------------------------------------
function selectBot(id) {
  stopVisionControl();
  resetVisionTools();
  if (state.ws) { state.ws.close(); state.ws = null; }
  stopMap();
  Vision.detach();
  state.selected = id;
  renderBotList();
  $("#detail-empty").hidden = !!id;
  $("#detail-body").hidden = !id;
  $("#detail-error").textContent = "";
  resetPanels();
  if (!id) { closeVisionModal(); closeInventoryModal(); return; }
  $("#log").innerHTML = "";
  openSocket(id);
  startMap();
  Vision.setRefreshInterval(VISION_SMALL_REFRESH_MS);
  Vision.attach(id, Number($("#vision-range").value));
  loadState(id);
  loadMacroBar(id);
}

async function loadState(id) {
  try {
    const s = await fetch(`api/bots/${id}/state`).then(r => r.ok ? r.json() : null);
    if (!s || state.selected !== id) return;
    renderVitals(s.player);
    renderInventory(s.inventory);
  } catch { /* ignore */ }
}

function resetPanels() {
  $("#vitals").hidden = true;
  $("#inv-grid").innerHTML = '<div class="muted inv-empty">no inventory data yet</div>';
  $("#macro-bar").innerHTML = "";
}

function openSocket(id) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const base = location.pathname.replace(/\/[^/]*$/, "/");
  const ws = new WebSocket(`${proto}://${location.host}${base}api/bots/${id}/ws`);
  state.ws = ws;
  ws.onmessage = (ev) => {
    if (state.selected !== id) return;
    const msg = JSON.parse(ev.data);
    if (msg.type === "snapshot") {
      applyStatus(msg.data.status);
      msg.data.history.forEach(logEvent);
    } else {
      onLiveEvent(msg);
    }
  };
  ws.onclose = () => {
    stopVisionControl();
    if (state.selected === id) logSystem("— connection closed —");
  };
}

function onLiveEvent(ev) {
  logEvent(ev);
  const bot = state.bots.find(b => b.id === ev.bot_id);
  if (ev.type === "state" && bot) { bot.state = ev.data.state; renderBotList(); applyStatusPartial(ev.data.state, ev.data); }
  if ((ev.type === "spawn" || ev.type === "move") && bot) { applyPosition(ev.data); Vision.setPose(ev.data); }
  if (ev.type === "stats") { renderVitals(ev.data); if (ev.data.position) { applyPosition(ev.data.position); Vision.setPose(ev.data.position); } }
  if (ev.type === "inventory") renderInventory(ev.data);
  if (ev.type === "macro" && (ev.data.phase === "started" || ev.data.phase === "finished"
      || ev.data.phase === "cancelled")) loadMacroBar(ev.bot_id);
  if (ev.type === "error") { $("#detail-error").textContent = ev.data.message; applyStatusPartial("error"); }
}

function applyStatus(s) {
  $("#d-username").textContent = s.username;
  $("#d-target").textContent = `${s.host}:${s.port} · ${s.version}`;
  applyStatusPartial(s.state);
  if (s.position) applyPosition(s.position);
  if (s.last_error) $("#detail-error").textContent = s.last_error;
}

function applyStatusPartial(stateName, data) {
  const badge = $("#d-state");
  let label = stateName;
  if (stateName === "reconnecting" && data && data.attempt)
    label = `reconnecting (#${data.attempt})`;
  badge.textContent = label;
  badge.className = `badge ${stateName}`;
  // Show Connect only when stopped; Disconnect only when running.
  const running = ["connecting", "configuring", "play", "reconnecting"].includes(stateName);
  $("#btn-connect").hidden = running;
  $("#btn-stop").hidden = !running;
}

function applyPosition(p) {
  if (!p || p.x == null) return;
  $("#d-position").textContent =
    `x ${p.x.toFixed(1)}  y ${p.y.toFixed(1)}  z ${p.z.toFixed(1)}  ` +
    `(yaw ${Math.round(p.yaw)}°)`;
}

// -- logging ----------------------------------------------------------------
function logEvent(ev) {
  const type = ev.type;
  // too noisy for the log; surfaced in the header/vitals/inventory panels instead
  if (type === "move" || type === "stats" || type === "inventory") return;
  let text;
  if (type === "chat") text = describeChat(ev.data);
  else if (type === "state") {
    text = `→ ${ev.data.state}`;
    if (ev.data.state === "reconnecting")
      text += ` (attempt ${ev.data.attempt}, retry in ${ev.data.retry_in}s)`;
  }
  else if (type === "spawn") text = `spawned at x ${ev.data.x?.toFixed(1)} y ${ev.data.y?.toFixed(1)} z ${ev.data.z?.toFixed(1)}`;
  else if (type === "disconnect") text = `disconnected: ${fmt(ev.data.reason)}`;
  else if (type === "error") text = `${ev.data.kind}: ${ev.data.message}`;
  else if (type === "ready") text = "entered play state";
  else if (type === "macro") {
    const d = ev.data;
    text = `${d.macro} (${d.source}): ${d.phase}${d.detail ? " — " + d.detail : ""}`;
  }
  else text = fmt(ev.data);
  appendLine(type, tagFor(type), text, ev.ts);
}

function tagFor(type) {
  return { chat: "CHAT", state: "STATE", error: "ERR", spawn: "SPAWN",
           disconnect: "DISC", ready: "READY", macro: "MACRO" }[type] || type.toUpperCase();
}

function describeChat(data) {
  const p = data.params;
  if (p == null) return "(chat)";
  if (typeof p === "string") return p;
  // Common shapes: {message}, {content}, {plainMessage}, {senderName, message}
  const msg = p.message ?? p.content ?? p.plainMessage ?? p.unsignedContent;
  if (msg != null && typeof msg !== "object") {
    return p.senderName ? `<${fmt(p.senderName)}> ${fmt(msg)}` : fmt(msg);
  }
  return fmt(p);
}

function logSystem(text) { appendLine("system", "···", text, Date.now() / 1000); }

function appendLine(type, tag, msg, ts) {
  const log = $("#log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const line = el("div", `log-line ${type}`);
  line.append(el("span", "ts", new Date(ts * 1000).toLocaleTimeString()));
  line.append(el("span", "tag", tag));
  line.append(el("span", "msg", msg));
  log.append(line);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function fmt(v) {
  if (v == null) return "";
  if (typeof v === "string") return v;
  try { return JSON.stringify(v); } catch { return String(v); }
}

// -- forms ------------------------------------------------------------------
async function initVersions() {
  const sel = $("#version-select");
  try {
    for (const v of await api.versions()) {
      const o = el("option", null, v);
      o.value = v;
      if (v === "1.18.2") o.selected = true;
      sel.append(o);
    }
  } catch { sel.append(el("option", null, "1.18.2")); }
}

$("#new-bot").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("#new-bot-error").textContent = "";
  try {
    const bot = await api.createBot({
      host: f.host.value.trim(),
      port: Number(f.port.value),
      username: f.username.value.trim(),
      version: f.version.value,
      auto_reconnect: f.auto_reconnect.checked,
    });
    await refreshBots();
    selectBot(bot.id);
  } catch (err) {
    $("#new-bot-error").textContent = err.message;
  }
});

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message || !state.selected) return;
  try {
    await api.chat(state.selected, message);
    input.value = "";
  } catch (err) { $("#detail-error").textContent = err.message; }
});

$("#btn-connect").addEventListener("click", async () => {
  if (state.selected) { await api.connect(state.selected); refreshBots(); }
});
$("#btn-stop").addEventListener("click", async () => {
  if (state.selected) { await api.stop(state.selected); refreshBots(); }
});
$("#btn-remove").addEventListener("click", async () => {
  if (state.selected) { await api.remove(state.selected); selectBot(null); refreshBots(); }
});

// -- vitals + effects -------------------------------------------------------
const EFFECT_NAMES = {
  1: "Speed", 2: "Slowness", 3: "Haste", 4: "Mining Fatigue", 5: "Strength",
  6: "Instant Health", 7: "Instant Damage", 8: "Jump Boost", 9: "Nausea",
  10: "Regeneration", 11: "Resistance", 12: "Fire Resistance", 13: "Water Breathing",
  14: "Invisibility", 15: "Blindness", 16: "Night Vision", 17: "Hunger",
  18: "Weakness", 19: "Poison", 20: "Wither", 21: "Health Boost", 22: "Absorption",
  23: "Saturation", 24: "Glowing", 25: "Levitation", 26: "Luck", 27: "Bad Luck",
  28: "Slow Falling", 29: "Conduit Power", 30: "Dolphin's Grace", 31: "Bad Omen",
  32: "Hero of the Village", 33: "Darkness",
};
const ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"];

function renderVitals(p) {
  if (!p) return;
  $("#vitals").hidden = false;
  $("#v-gm").textContent = p.gamemode || "—";
  setVital("health", p.health, 20);
  setVital("food", p.food, 20);
  const xp = p.experience || {};
  $("#v-xp").textContent = `Lvl ${xp.level ?? 0}`;
  renderEffects(p.effects || []);
}

function setVital(kind, value, max) {
  const bar = $(`#v-${kind}-bar`);
  const txt = $(`#v-${kind}-txt`);
  if (value == null) { bar.style.width = "0%"; txt.textContent = "—"; return; }
  const v = Math.max(0, Math.min(max, value));
  bar.style.width = `${(v / max) * 100}%`;
  txt.textContent = `${Math.round(v)}/${max}`;
}

function renderEffects(effects) {
  const box = $("#effects");
  box.innerHTML = "";
  for (const e of effects) {
    const name = EFFECT_NAMES[e.effect_id] || `Effect ${e.effect_id}`;
    const lvl = e.amplifier != null && e.amplifier > 0 ? ` ${ROMAN[e.amplifier + 1] || e.amplifier + 1}` : "";
    const chip = el("span", "effect-chip");
    chip.append(el("span", null, name + lvl));
    chip.append(el("span", "dur", fmtDuration(e.duration)));
    box.append(chip);
  }
}

function fmtDuration(ticks) {
  if (ticks == null || ticks < 0) return "∞";
  const s = Math.round(ticks / 20);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// -- inventory --------------------------------------------------------------
function renderInventory(inv) {
  const grid = $("#inv-grid");
  if (!inv || !inv.slots) {
    grid.innerHTML = '<div class="muted inv-empty">no inventory data yet</div>';
    return;
  }
  const slots = inv.slots;
  const held = inv.held_index;
  grid.innerHTML = "";

  // armor (5-8) + offhand (45)
  const top = el("div", "inv-row spaced");
  [5, 6, 7, 8].forEach(i => top.append(cell(slots[i], i, held)));
  top.append(el("div", "inv-gap"));
  top.append(cell(slots[45], 45, held));
  grid.append(top);

  // main inventory (9-35), three rows of nine
  for (let r = 0; r < 3; r++) {
    const row = el("div", "inv-row");
    for (let c = 0; c < 9; c++) { const i = 9 + r * 9 + c; row.append(cell(slots[i], i, held)); }
    grid.append(row);
  }

  // hotbar (36-44)
  const hot = el("div", "inv-row");
  hot.style.marginTop = "4px";
  for (let i = 36; i <= 44; i++) hot.append(cell(slots[i], i, held));
  grid.append(hot);
}

function cell(item, index, heldIndex) {
  const c = el("div", "inv-cell");
  if (index === heldIndex) c.classList.add("held");
  if (item) {
    c.classList.add("filled");
    const name = (item.name || "item").replace(/^minecraft:/, "");
    c.title = `${name} ×${item.count}`;
    c.append(el("span", "icon", abbrev(name)));
    if (item.count > 1) c.append(el("span", "count", item.count));
  }
  return c;
}

function abbrev(name) {
  // No textures available; show a short readable token per item.
  const parts = name.split("_");
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  return name.slice(0, 2).toUpperCase();
}

// -- minimap ----------------------------------------------------------------
const MAP_INTERVAL = 1500;

function startMap() {
  setMapStatus("waiting for chunks…");
  tickMap();
  state.mapTimer = setInterval(tickMap, MAP_INTERVAL);
}

function stopMap() {
  if (state.mapTimer) { clearInterval(state.mapTimer); state.mapTimer = null; }
  if (state.mapUrl) { URL.revokeObjectURL(state.mapUrl); state.mapUrl = null; }
  const img = $("#map-img");
  img.classList.remove("ready");
  img.removeAttribute("src");
}

async function tickMap() {
  const id = state.selected;
  if (!id || state.mapBusy) return;
  if (!$("#map-live").checked) return;
  state.mapBusy = true;
  try {
    const radius = $("#map-radius").value;
    const r = await fetch(`api/bots/${id}/map.png?radius=${radius}`, { cache: "no-store" });
    if (state.selected !== id) return;
    if (r.status === 200) {
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const img = $("#map-img");
      img.src = url;
      img.classList.add("ready");
      if (state.mapUrl) URL.revokeObjectURL(state.mapUrl);
      state.mapUrl = url;
      setMapStatus(null);
    } else {
      // 503 while the world isn't ready — keep the last frame if we have one.
      if (!$("#map-img").classList.contains("ready")) {
        const msg = (await r.json().catch(() => ({}))).detail || "map not ready";
        setMapStatus(msg);
      }
    }
  } catch { /* transient; next tick retries */ }
  finally { state.mapBusy = false; }
}

function setMapStatus(text) {
  const el = $("#map-status");
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

$("#map-radius").addEventListener("change", () => { if (state.selected) tickMap(); });
$("#map-live").addEventListener("change", (e) => {
  if (e.target.checked && state.selected) tickMap();
});

$("#vision-range").addEventListener("change", (e) => {
  if (state.selected) Vision.setRange(Number(e.target.value));
});
$("#vision-renderer").value = Vision.renderer();
$("#vision-renderer").addEventListener("change", (e) => {
  Vision.setRenderer(e.target.value);
});

// Full-screen vision modal: reparent the (same) canvas into the modal and back,
// so the WebGL context is reused rather than recreated.
function openVisionModal() {
  if (!state.selected) return;
  Vision.setRefreshInterval(VISION_FULL_REFRESH_MS);
  $("#vision-stage").appendChild(Vision.element());
  $("#vision-modal").hidden = false;
  requestAnimationFrame(() => Vision.resize());
}
function closeVisionModal() {
  if ($("#vision-modal").hidden) return;
  stopVisionControl();
  stopFreecam();
  $("#vision-tools").hidden = true;
  $("#vision-modal").hidden = true;
  $(".vision-view").insertBefore(Vision.element(), $("#vision-status"));
  Vision.setRefreshInterval(VISION_SMALL_REFRESH_MS);
  requestAnimationFrame(() => Vision.resize());
}
$("#vision-expand").addEventListener("click", openVisionModal);
$("#vision-modal-close").addEventListener("click", closeVisionModal);
$("#vision-modal").addEventListener("click", (e) => {
  if (e.target.id === "vision-modal") closeVisionModal();   // click backdrop
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeVisionModal();
});

// Pointer-lock first-person controls. The socket carries state snapshots at
// Minecraft's 20 Hz tick rate; the host times out stale input independently.
const visionControl = { active: false, keys: new Set(), timer: null };
const CONTROL_KEYS = new Set([
  "KeyW", "KeyA", "KeyS", "KeyD", "Space", "ShiftLeft", "ShiftRight",
]);

function controlAxis(positive, negative) {
  return (visionControl.keys.has(positive) ? 1 : 0)
    - (visionControl.keys.has(negative) ? 1 : 0);
}

function sendVisionControl(active = visionControl.active) {
  const look = Vision.look();
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN || (active && !look)) return;
  state.ws.send(JSON.stringify({
    type: "control",
    data: {
      active,
      forward: active ? controlAxis("KeyW", "KeyS") : 0,
      strafe: active ? controlAxis("KeyD", "KeyA") : 0,
      jump: active && visionControl.keys.has("Space"),
      sneak: active && (visionControl.keys.has("ShiftLeft") || visionControl.keys.has("ShiftRight")),
      yaw: look?.yaw ?? 0,
      pitch: look?.pitch ?? 0,
    },
  }));
}

function setControlUi(active) {
  const button = $("#vision-control");
  button.classList.toggle("active", active);
  button.textContent = active ? "Stop" : "Control";
  $("#vision-crosshair").hidden = !active;
}

function startVisionControl() {
  if (!state.selected || visionControl.active) return;
  stopFreecam();
  if (Vision.renderer() !== "custom") {
    Vision.setRenderer("custom");
    $("#vision-renderer").value = "custom";
  }
  openVisionModal();
  const target = Vision.element();
  visionControl.active = true;
  Vision.setControlActive(true);
  visionControl.keys.clear();
  setControlUi(true);
  sendVisionControl();
  visionControl.timer = setInterval(sendVisionControl, 50);
  if (!target.requestPointerLock) {
    stopVisionControl();
    return;
  }
  try {
    const lock = target.requestPointerLock();
    if (lock && typeof lock.catch === "function") lock.catch(stopVisionControl);
  } catch {
    stopVisionControl();
  }
}

function stopVisionControl() {
  if (!visionControl.active) return;
  sendVisionControl(false);
  visionControl.active = false;
  Vision.setControlActive(false);
  visionControl.keys.clear();
  clearInterval(visionControl.timer);
  visionControl.timer = null;
  setControlUi(false);
  if (document.pointerLockElement) document.exitPointerLock();
}

$("#vision-control").addEventListener("click", () => {
  if (visionControl.active) stopVisionControl();
  else startVisionControl();
});
document.addEventListener("pointerlockchange", () => {
  if (visionControl.active && document.pointerLockElement !== Vision.element())
    stopVisionControl();
});
document.addEventListener("mousemove", (e) => {
  if (!visionControl.active || document.pointerLockElement !== Vision.element()) return;
  Vision.adjustLook(e.movementX, e.movementY);
});
document.addEventListener("keydown", (e) => {
  if (!visionControl.active) return;
  if (e.key === "Escape") {
    stopVisionControl();
    return;
  }
  if (CONTROL_KEYS.has(e.code)) {
    e.preventDefault();
    visionControl.keys.add(e.code);
    sendVisionControl();
  }
});
document.addEventListener("keyup", (e) => {
  if (!visionControl.active || !CONTROL_KEYS.has(e.code)) return;
  e.preventDefault();
  visionControl.keys.delete(e.code);
  sendVisionControl();
});
window.addEventListener("blur", stopVisionControl);

// -- vision tools (freecam / xray / chest highlight) ------------------------
// Ctrl+Space toggles a small overlay of rendering tools inside the expanded
// vision view. Freecam is a browser-only fly camera (never drives the bot).
const freecam = { active: false };
const FREECAM_KEYS = new Set([
  "KeyW", "KeyA", "KeyS", "KeyD", "Space", "ShiftLeft", "ShiftRight",
]);

function toggleVisionTools() {
  if (!state.selected) return;
  if (Vision.renderer() !== "custom") {
    Vision.setRenderer("custom");
    $("#vision-renderer").value = "custom";
  }
  const panel = $("#vision-tools");
  if (panel.hidden) {
    openVisionModal();
    panel.hidden = false;
  } else {
    panel.hidden = true;
  }
}

function startFreecam() {
  if (freecam.active) return;
  if (visionControl.active) stopVisionControl();
  freecam.active = true;
  Vision.setTool("freecam", true);
  $("#tool-freecam").checked = true;
  $("#vision-crosshair").hidden = false;
  const target = Vision.element();
  try {
    if (target.requestPointerLock) target.requestPointerLock();
  } catch { /* pointer lock is optional; drag-look still works */ }
}

function stopFreecam() {
  if (!freecam.active) return;
  freecam.active = false;
  Vision.setTool("freecam", false);
  FREECAM_KEYS.forEach(k => Vision.setFreecamKey(k, false));
  $("#tool-freecam").checked = false;
  if (!visionControl.active) $("#vision-crosshair").hidden = true;
  if (document.pointerLockElement === Vision.element()) document.exitPointerLock();
}

function resetVisionTools() {
  stopFreecam();
  $("#vision-tools").hidden = true;
  for (const id of ["tool-xray", "tool-chests"]) $("#" + id).checked = false;
  Vision.setTool("xray", false);
  Vision.setTool("chests", false);
}

$("#tool-xray").addEventListener("change", (e) => Vision.setTool("xray", e.target.checked));
$("#tool-chests").addEventListener("change", (e) => Vision.setTool("chests", e.target.checked));
$("#tool-freecam").addEventListener("change", (e) => {
  // Drop focus so Space (freecam "up") doesn't re-toggle the checkbox.
  e.target.blur();
  if (e.target.checked) startFreecam(); else stopFreecam();
});

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.code === "Space") {
    e.preventDefault();
    toggleVisionTools();
    return;
  }
  if (freecam.active && FREECAM_KEYS.has(e.code)) {
    e.preventDefault();
    Vision.setFreecamKey(e.code, true);
  }
});
document.addEventListener("keyup", (e) => {
  if (freecam.active && FREECAM_KEYS.has(e.code)) {
    e.preventDefault();
    Vision.setFreecamKey(e.code, false);
  }
});
document.addEventListener("mousemove", (e) => {
  if (!freecam.active) return;
  // Freecam looks on pointer-lock movement, or click-drag when not locked.
  if (document.pointerLockElement === Vision.element()) {
    Vision.adjustLook(e.movementX, e.movementY);
  } else if (e.buttons & 1) {
    Vision.adjustLook(e.movementX, e.movementY);
  }
});
window.addEventListener("blur", () => {
  if (freecam.active) FREECAM_KEYS.forEach(k => Vision.setFreecamKey(k, false));
});

function openInventoryModal() {
  $("#inventory-modal").hidden = false;
}
function closeInventoryModal() {
  $("#inventory-modal").hidden = true;
}
$("#inventory-open").addEventListener("click", openInventoryModal);
$("#inventory-modal-close").addEventListener("click", closeInventoryModal);
$("#inventory-modal").addEventListener("click", (e) => {
  if (e.target.id === "inventory-modal") closeInventoryModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeInventoryModal();
});

// -- macros -----------------------------------------------------------------
const STEP_SCHEMA = {
  chat: [{ k: "message", t: "text", ph: "message", wide: true }],
  wait: [{ k: "seconds", t: "num", ph: "seconds" }],
  look: [{ k: "yaw", t: "num", ph: "yaw" }, { k: "pitch", t: "num", ph: "pitch" }],
  move_to: [{ k: "x", t: "num", ph: "x" }, { k: "y", t: "num", ph: "y" }, { k: "z", t: "num", ph: "z" }],
  select_hotbar: [{ k: "slot", t: "int", ph: "0-8" }],
  creative_give: [{ k: "slot", t: "int", ph: "slot" }, { k: "item", t: "text", ph: "item name", wide: true }, { k: "count", t: "int", ph: "count" }],
};
const macroState = { defs: [], editing: null };

async function loadDefs() {
  macroState.defs = await fetch("api/macros").then(r => r.json());
  renderDefs();
}

function renderDefs() {
  const ul = $("#macro-defs");
  ul.innerHTML = "";
  for (const m of macroState.defs) {
    const li = el("li");
    if (macroState.editing && macroState.editing.id === m.id) li.classList.add("active");
    li.append(el("div", "md-name", m.name));
    li.append(el("div", "md-trig", triggerLabel(m.trigger)));
    li.onclick = () => editMacro(m);
    ul.append(li);
  }
}

function triggerLabel(t) {
  if (!t || t.type === "manual") return "manual";
  if (t.type === "interval") return `every ${t.interval_seconds}s`;
  return `on ${t.event}${t.pattern ? ` /${t.pattern}/` : ""}`;
}

function openMacroModal() { $("#macro-modal").hidden = false; loadDefs(); }
function closeMacroModal() { $("#macro-modal").hidden = true; macroState.editing = null; }

function newMacro() {
  macroState.editing = null;
  fillEditor({ name: "", loop: 1, trigger: { type: "manual" }, steps: [{ action: "chat", message: "" }] });
  $("#me-delete").hidden = true;
  renderDefs();
}

function editMacro(m) {
  macroState.editing = m;
  fillEditor(m);
  $("#me-delete").hidden = false;
  renderDefs();
}

function fillEditor(m) {
  $("#macro-editor").hidden = false;
  $("#me-error").textContent = "";
  $("#me-name").value = m.name || "";
  $("#me-loop").value = m.loop || 1;
  const t = m.trigger || { type: "manual" };
  $("#me-trigger").value = t.type;
  $("#me-event").value = t.event || "chat";
  $("#me-pattern").value = t.pattern || "";
  $("#me-interval").value = t.interval_seconds || 30;
  syncTriggerFields();
  const steps = $("#me-steps");
  steps.innerHTML = "";
  (m.steps || []).forEach(addStepRow);
  if (!m.steps || !m.steps.length) addStepRow({ action: "chat", message: "" });
}

function syncTriggerFields() {
  const type = $("#me-trigger").value;
  $("#me-event-wrap").hidden = type !== "event";
  $("#me-interval-wrap").hidden = type !== "interval";
  $("#me-pattern-wrap").style.display = $("#me-event").value === "chat" ? "" : "none";
}

function addStepRow(step) {
  const row = el("div", "step-row");
  const sel = el("select", "step-action");
  for (const action of Object.keys(STEP_SCHEMA)) {
    const o = el("option", null, action); o.value = action;
    if (step && step.action === action) o.selected = true;
    sel.append(o);
  }
  const params = el("div", "step-params");
  sel.onchange = () => renderParams(params, sel.value, {});
  renderParams(params, sel.value, step || {});
  const del = el("button", "step-del", "✕");
  del.type = "button";
  del.onclick = () => row.remove();
  row.append(el("span", "drag", "⠿"), sel, params, del);
  $("#me-steps").append(row);
}

function renderParams(container, action, values) {
  container.innerHTML = "";
  for (const f of STEP_SCHEMA[action]) {
    const inp = el("input");
    inp.dataset.key = f.k;
    inp.type = f.t === "text" ? "text" : "number";
    inp.placeholder = f.ph;
    if (f.wide) inp.classList.add("wide");
    if (values[f.k] != null) inp.value = values[f.k];
    container.append(inp);
  }
}

function collectMacro() {
  const type = $("#me-trigger").value;
  const trigger = { type };
  if (type === "event") {
    trigger.event = $("#me-event").value;
    if (trigger.event === "chat") trigger.pattern = $("#me-pattern").value.trim();
  } else if (type === "interval") {
    trigger.interval_seconds = Number($("#me-interval").value);
  }
  const steps = [...$("#me-steps").querySelectorAll(".step-row")].map(row => {
    const action = row.querySelector(".step-action").value;
    const step = { action };
    for (const inp of row.querySelectorAll(".step-params input")) {
      const f = STEP_SCHEMA[action].find(x => x.k === inp.dataset.key);
      const raw = inp.value;
      step[inp.dataset.key] = f.t === "text" ? raw
        : f.t === "int" ? parseInt(raw, 10) : Number(raw);
    }
    return step;
  });
  return { name: $("#me-name").value.trim(), loop: Number($("#me-loop").value), trigger, steps };
}

async function saveMacro() {
  const body = collectMacro();
  const editing = macroState.editing;
  const url = editing ? `api/macros/${editing.id}` : "api/macros";
  const r = await fetch(url, {
    method: editing ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) { $("#me-error").textContent = (await r.json()).detail || "save failed"; return; }
  const saved = await r.json();
  await loadDefs();
  editMacro(macroState.defs.find(m => m.id === saved.id) || saved);
  if (state.selected) loadMacroBar(state.selected);
}

async function deleteMacro() {
  if (!macroState.editing) return;
  await fetch(`api/macros/${macroState.editing.id}`, { method: "DELETE" });
  macroState.editing = null;
  $("#macro-editor").hidden = true;
  await loadDefs();
  if (state.selected) loadMacroBar(state.selected);
}

// -- per-bot macro run bar --------------------------------------------------
async function loadMacroBar(botId) {
  if (!botId) return;
  try {
    const [defs, status] = await Promise.all([
      macroState.defs.length ? Promise.resolve(macroState.defs) : fetch("api/macros").then(r => r.json()),
      fetch(`api/bots/${botId}/macros`).then(r => r.ok ? r.json() : { armed: [], running: [] }),
    ]);
    macroState.defs = defs;
    if (state.selected === botId) renderMacroBar(botId, defs, status);
  } catch { /* ignore */ }
}

function renderMacroBar(botId, defs, status) {
  const bar = $("#macro-bar");
  bar.innerHTML = "";
  const runningIds = new Set(status.running.map(r => r.macro_id));
  for (const m of defs) {
    const armed = status.armed.includes(m.id);
    const chip = el("div", "macro-chip");
    if (armed) chip.classList.add("armed");
    if (runningIds.has(m.id)) chip.append(el("span", "run-dot"));
    chip.append(el("span", "mname", m.name));
    chip.append(el("span", "trig", triggerLabel(m.trigger)));
    const run = el("button", null, "▶ Run");
    run.onclick = () => runMacro(botId, m.id);
    chip.append(run);
    if (m.trigger && m.trigger.type !== "manual") {
      const t = el("button", "secondary", armed ? "Disarm" : "Arm");
      t.onclick = () => (armed ? disarmMacro : armMacro)(botId, m.id);
      chip.append(t);
    }
    bar.append(chip);
  }
}

async function runMacro(botId, macroId) {
  const r = await fetch(`api/bots/${botId}/macros/${macroId}/run`, { method: "POST" });
  if (!r.ok) $("#detail-error").textContent = (await r.json()).detail || "run failed";
  loadMacroBar(botId);
}
async function armMacro(botId, macroId) {
  const r = await fetch(`api/bots/${botId}/macros/${macroId}/arm`, { method: "POST" });
  if (!r.ok) $("#detail-error").textContent = (await r.json()).detail || "arm failed";
  loadMacroBar(botId);
}
async function disarmMacro(botId, macroId) {
  await fetch(`api/bots/${botId}/macros/${macroId}/disarm`, { method: "POST" });
  loadMacroBar(botId);
}

// wire modal controls
$("#btn-macros").addEventListener("click", openMacroModal);
$("#macro-modal-close").addEventListener("click", closeMacroModal);
$("#macro-modal").addEventListener("click", (e) => { if (e.target.id === "macro-modal") closeMacroModal(); });
$("#macro-new").addEventListener("click", newMacro);
$("#me-add-step").addEventListener("click", () => addStepRow({ action: "chat", message: "" }));
$("#me-trigger").addEventListener("change", syncTriggerFields);
$("#me-event").addEventListener("change", syncTriggerFields);
$("#me-save").addEventListener("click", saveMacro);
$("#me-delete").addEventListener("click", deleteMacro);
$("#me-cancel").addEventListener("click", () => { $("#macro-editor").hidden = true; macroState.editing = null; renderDefs(); });

// -- auth -------------------------------------------------------------------
async function initUser() {
  try {
    const me = await fetch("api/me").then(r => r.ok ? r.json() : null);
    if (me) $("#user-name").textContent = me.name || me.email || me.sub || "signed in";
  } catch { /* ignore */ }
}
$("#btn-logout").addEventListener("click", async () => {
  await fetch("auth/logout", { method: "POST" });
  location.reload();
});

// -- boot -------------------------------------------------------------------
initUser();
initVersions();
refreshBots();
setInterval(refreshBots, 4000);  // keep the list/status dots fresh
