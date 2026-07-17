"use strict";

// Thin SPA over the dashboard REST + WebSocket API. No build step.
const api = {
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
  async setServerAuth(id, settings) {
    const r = await fetch(`api/bots/${id}/server-auth`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        password: settings.password,
        auto_register: settings.autoRegister === true,
      }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
  },
  async clearServerAuth(id) {
    const r = await fetch(`api/bots/${id}/server-auth`, { method: "DELETE" });
    if (!r.ok && r.status !== 404)
      throw new Error((await r.json()).detail || r.statusText);
  },
  async navigate(id, x, z) {
    const r = await fetch(`api/bots/${id}/navigate`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, z }),
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    return r.json();
  },
  stop(id) { return fetch(`api/bots/${id}/stop`, { method: "POST" }); },
  connect(id) { return fetch(`api/bots/${id}/connect`, { method: "POST" }); },
  remove(id) { return fetch(`api/bots/${id}`, { method: "DELETE" }); },
};

const state = {
  bots: [], selected: null, ws: null,
  inventory: null,
  mapTimer: null, mapBusy: false, mapGeneration: 0,
  mapCenter: null, mapFollow: true, mapPosition: null, mapScale: 4,
  mapTarget: null, mapTiles: new Map(), mapManifest: new Map(),
  mapQueue: [], mapQueued: new Set(), mapActiveFetches: 0,
};
const VISION_SMALL_REFRESH_MS = 30000;
const VISION_FULL_REFRESH_MS = 1800;
const VISION_MAP_PIP_REFRESH_MS = 6000;
const ACTION_BAR_TTL_MS = 5000;
const OFFLINE_AUTH_STORAGE_KEY = "minebotOfflineAuth:v1";
let actionBarTimer = null;
let lastActionBarText = null;
const syncedOfflineAuth = new Map();

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
  syncOfflineAuthBots();
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
  closeServerAuthModal();
  closeMapModal();
  if (state.ws) { state.ws.close(); state.ws = null; }
  stopMap();
  Vision.detach();
  state.selected = id;
  resetVisionChat();
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
  clearActionBar();
  $("#vitals").hidden = true;
  renderInventory(null);
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
  const bot = state.bots.find(b => b.id === ev.bot_id);
  logEvent(ev);
  if (ev.type === "state" && bot) { bot.state = ev.data.state; renderBotList(); applyStatusPartial(ev.data.state, ev.data); }
  if (ev.type === "protocol" && bot) {
    bot.version = ev.data.version;
    renderBotList();
    $("#d-target").textContent = `${bot.host}:${bot.port} · ${bot.version}`;
  }
  if ((ev.type === "spawn" || ev.type === "move") && bot) { applyPosition(ev.data); Vision.setPose(ev.data); }
  if (ev.type === "stats") { renderVitals(ev.data); if (ev.data.position) { applyPosition(ev.data.position); Vision.setPose(ev.data.position); } }
  if (ev.type === "inventory") renderInventory(ev.data);
  if (ev.type === "navigation") applyNavigation(ev.data);
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
  state.mapPosition = { x: p.x, z: p.z, yaw: p.yaw || 0 };
  if (state.mapFollow) state.mapCenter = { x: p.x, z: p.z };
  drawMap();
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
  if (type === "action_bar") {
    showActionBar(describeChat(ev.data));
    return;
  }
  if (type === "chat") {
    const actionBar = describeActionBar(ev.data);
    if (actionBar !== null) {
      showActionBar(actionBar);
      return;
    }
    text = describeChat(ev.data);
    appendVisionChat(text, ev.ts);
  }
  else if (type === "state") {
    text = `→ ${ev.data.state}`;
    if (ev.data.state === "reconnecting")
      text += ` (attempt ${ev.data.attempt}, retry in ${ev.data.retry_in}s)`;
  }
  else if (type === "spawn") text = `spawned at x ${ev.data.x?.toFixed(1)} y ${ev.data.y?.toFixed(1)} z ${ev.data.z?.toFixed(1)}`;
  else if (type === "disconnect") text = `disconnected: ${fmt(ev.data.reason)}`;
  else if (type === "error") text = `${ev.data.kind}: ${ev.data.message}`;
  else if (type === "auth") text = ev.data.online_mode
    ? "premium / online-mode server (Microsoft account required)"
    : `offline / cracked-mode server${ev.data.encrypted ? " (encrypted connection)" : ""}`;
  else if (type === "protocol") text = `detected ${ev.data.server_name || ev.data.version} · protocol ${ev.data.protocol} · ${ev.data.version} schema`;
  else if (type === "server_auth") text = {
    probe_sent: "checking for an offline server login system",
    detected: "offline server login system detected",
    register_sent: "automatic server registration sent",
    login_sent: "automatic server login sent",
  }[ev.data.action] || fmt(ev.data);
  else if (type === "navigation") {
    const target = ev.data.target;
    text = `${ev.data.phase} map navigation` +
      (target ? ` to x ${target.x.toFixed(1)} z ${target.z.toFixed(1)}` : "");
  }
  else if (type === "ready") text = "entered play state";
  else if (type === "macro") {
    const d = ev.data;
    text = `${d.macro} (${d.source}): ${d.phase}${d.detail ? " — " + d.detail : ""}`;
  }
  else text = fmt(ev.data);
  appendLine(type, tagFor(type), text, ev.ts);
}

function tagFor(type) {
  return { chat: "CHAT", state: "STATE", error: "ERR", auth: "AUTH", protocol: "PROTO", server_auth: "LOGIN", navigation: "NAV", spawn: "SPAWN",
           disconnect: "DISC", ready: "READY", macro: "MACRO" }[type] || type.toUpperCase();
}

function describeChat(data) {
  const p = data.params;
  if (p == null) return "(chat)";
  if (typeof p === "string") return p;
  // Common shapes: {message}, {content}, {plainMessage}, {senderName, message}
  const msg = p.message ?? p.content ?? p.plainMessage ?? p.unsignedContent;
  const senderValue = p.senderName
    ?? (data.packet === "player_chat" ? p.networkName : null)
    ?? (data.packet === "profileless_chat" ? p.name : null);
  const sender = plainMinecraftText(senderValue).replace(/\s+/g, " ").trim();
  const withSender = message => {
    if (!sender) return message;
    const prefixes = [`<${sender}>`, `${sender}:`, `${sender} »`];
    return prefixes.some(prefix => message.startsWith(prefix))
      ? message
      : `<${sender}> ${message}`;
  };
  if (msg != null && typeof msg !== "object") {
    return withSender(fmt(msg));
  }
  if (msg != null) return withSender(plainMinecraftText(msg));
  return fmt(p);
}

function describeActionBar(data) {
  const p = data?.params;
  if (p == null || typeof p !== "object") return null;
  const position = p.position;
  const isActionBar = p.isActionBar === true || p.overlay === true ||
    position === 2 || position === "action_bar" || position === "game_info";
  if (!isActionBar) return null;
  const content = p.content ?? p.message ?? p.text ?? p.unsignedContent ?? "";
  return plainMinecraftText(content).replace(/\s+/g, " ").trim();
}

function plainMinecraftText(value) {
  if (value == null) return "";
  if (typeof value === "string") return value.replace(/§[0-9a-fk-or]/gi, "");
  if (Array.isArray(value)) return value.map(plainMinecraftText).join("");
  if (typeof value !== "object") return String(value);
  const own = typeof value.text === "string" ? value.text : "";
  const translated = typeof value.translate === "string" && !own ? value.translate : "";
  return own + translated + plainMinecraftText(value.extra);
}

function showActionBar(text) {
  clearTimeout(actionBarTimer);
  if (!text) {
    clearActionBar();
    return;
  }
  if (text !== lastActionBarText) {
    $("#action-bar-text").textContent = text;
    lastActionBarText = text;
  }
  $("#action-bar").hidden = false;
  actionBarTimer = setTimeout(clearActionBar, ACTION_BAR_TTL_MS);
}

function clearActionBar() {
  clearTimeout(actionBarTimer);
  actionBarTimer = null;
  lastActionBarText = null;
  const bar = $("#action-bar");
  if (bar) bar.hidden = true;
}

function offlineAuthKey(bot) {
  return `${bot.host.trim().toLowerCase().replace(/\.$/, "")}:${bot.port}/${bot.username.toLowerCase()}`;
}

function readOfflineAuthStore() {
  try {
    const value = JSON.parse(localStorage.getItem(OFFLINE_AUTH_STORAGE_KEY) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch { return {}; }
}

function offlineAuthSettings(bot) {
  if (!bot) return null;
  const value = readOfflineAuthStore()[offlineAuthKey(bot)];
  return value && typeof value.password === "string" ? value : null;
}

function writeOfflineAuthSettings(bot, settings) {
  const store = readOfflineAuthStore();
  const key = offlineAuthKey(bot);
  if (settings) store[key] = settings;
  else delete store[key];
  try {
    localStorage.setItem(OFFLINE_AUTH_STORAGE_KEY, JSON.stringify(store));
    return true;
  } catch { return false; }
}

function offlineAuthFingerprint(bot, settings) {
  return `${bot.created_at}:${settings.password}\u0000${settings.autoRegister === true}`;
}

function syncOfflineAuthBots() {
  const liveIds = new Set(state.bots.map(bot => bot.id));
  for (const id of syncedOfflineAuth.keys()) {
    if (!liveIds.has(id)) syncedOfflineAuth.delete(id);
  }
  for (const bot of state.bots) {
    const settings = offlineAuthSettings(bot);
    if (!settings) continue;
    const fingerprint = offlineAuthFingerprint(bot, settings);
    if (syncedOfflineAuth.get(bot.id) === fingerprint) continue;
    syncedOfflineAuth.set(bot.id, fingerprint);
    api.setServerAuth(bot.id, settings).catch(() => {
      if (syncedOfflineAuth.get(bot.id) === fingerprint)
        syncedOfflineAuth.delete(bot.id);
    });
  }
}

function openServerAuthModal() {
  const bot = state.bots.find(b => b.id === state.selected);
  if (!bot) return;
  const settings = offlineAuthSettings(bot);
  $("#server-auth-password").value = settings?.password || "";
  $("#server-auth-register").checked = settings?.autoRegister === true;
  $("#server-auth-error").textContent = "";
  $("#server-auth-modal").hidden = false;
  $("#server-auth-password").focus();
}

function closeServerAuthModal() {
  const modal = $("#server-auth-modal");
  if (modal) modal.hidden = true;
}

async function saveServerAuthSettings(event) {
  event.preventDefault();
  const bot = state.bots.find(b => b.id === state.selected);
  const password = $("#server-auth-password").value;
  if (!bot) return;
  if (!password || /\s/.test(password)) {
    $("#server-auth-error").textContent = "Password must be non-empty and contain no spaces.";
    return;
  }
  const settings = {
    password,
    autoRegister: $("#server-auth-register").checked,
  };
  if (!writeOfflineAuthSettings(bot, settings)) {
    $("#server-auth-error").textContent = "Browser storage is unavailable.";
    return;
  }
  try {
    await api.setServerAuth(bot.id, settings);
    syncedOfflineAuth.set(bot.id, offlineAuthFingerprint(bot, settings));
    closeServerAuthModal();
  } catch (err) {
    $("#server-auth-error").textContent = err.message;
  }
}

async function clearServerAuthSettings() {
  const bot = state.bots.find(b => b.id === state.selected);
  if (!bot) return;
  writeOfflineAuthSettings(bot, null);
  try {
    await api.clearServerAuth(bot.id);
    syncedOfflineAuth.delete(bot.id);
    closeServerAuthModal();
  } catch (err) {
    $("#server-auth-error").textContent = err.message;
  }
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
$("#new-bot").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("#new-bot-error").textContent = "";
  try {
    const bot = await api.createBot({
      host: f.host.value.trim(),
      port: Number(f.port.value),
      username: f.username.value.trim(),
      auto_reconnect: f.auto_reconnect.checked,
    });
    await refreshBots();
    selectBot(bot.id);
  } catch (err) {
    $("#new-bot-error").textContent = err.message;
  }
});

async function sendChat(message) {
  if (!message || !state.selected) return;
  await api.chat(state.selected, message);
}

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message || !state.selected) return;
  try {
    await sendChat(message);
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
$("#btn-server-auth").addEventListener("click", openServerAuthModal);
$("#server-auth-modal-close").addEventListener("click", closeServerAuthModal);
$("#server-auth-modal").addEventListener("click", (e) => {
  if (e.target.id === "server-auth-modal") closeServerAuthModal();
});
$("#server-auth-form").addEventListener("submit", saveServerAuthSettings);
$("#server-auth-clear").addEventListener("click", clearServerAuthSettings);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeServerAuthModal();
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
  state.inventory = inv || null;
  renderInventoryGrid($("#inv-grid"), inv);
  renderInventoryGrid($("#vision-inventory-grid"), inv);
  renderVisionHotbar(inv);
}

function renderInventoryGrid(grid, inv) {
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

function renderVisionHotbar(inv) {
  const hotbar = $("#vision-hotbar");
  hotbar.replaceChildren();
  const slots = inv?.slots || {};
  const held = inv?.held_index;
  for (let index = 36; index <= 44; index++) hotbar.append(cell(slots[index], index, held));
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
const MAP_INTERVAL = 2000;
const MAP_MIN_SCALE = 0.5;
const MAP_MAX_SCALE = 32;
const MAP_TILE_CONCURRENCY = 4;
const MAP_TEXTURE_SCALE = 8;
const mapDrag = { active: false, moved: false, x: 0, y: 0, center: null };
const mapWheelZoom = { delta: 0, x: 0, y: 0, frame: null };

function startMap() {
  state.mapGeneration += 1;
  clearMapTiles();
  state.mapCenter = null;
  state.mapFollow = true;
  const bot = state.bots.find(item => item.id === state.selected);
  state.mapPosition = bot?.position ? {
    x: bot.position.x, z: bot.position.z, yaw: bot.position.yaw || 0,
  } : null;
  if (state.mapPosition)
    state.mapCenter = { x: state.mapPosition.x, z: state.mapPosition.z };
  state.mapScale = 4;
  state.mapTarget = null;
  updateMapScale();
  resizeMapCanvas();
  updateMapTarget();
  setMapStatus("waiting for chunks…");
  tickMap(true);
  state.mapTimer = setInterval(tickMap, MAP_INTERVAL);
}

function stopMap() {
  if (state.mapTimer) { clearInterval(state.mapTimer); state.mapTimer = null; }
  state.mapGeneration += 1;
  clearMapTiles();
  drawMap();
}

async function tickMap(force = false) {
  const id = state.selected;
  if (!id || state.mapBusy) return;
  if (!force && !$("#map-live").checked) return;
  state.mapBusy = true;
  const generation = state.mapGeneration;
  try {
    const response = await fetch(`api/bots/${id}/map/chunks`, { cache: "no-store" });
    if (state.selected !== id || state.mapGeneration !== generation) return;
    if (!response.ok) {
      if (!state.mapTiles.size)
        setMapStatus((await response.json().catch(() => ({}))).detail || "map not ready");
      return;
    }
    const payload = await response.json();
    state.mapManifest = new Map(payload.chunks.map(
      ([cx, cz, revision]) => [`${cx},${cz}`, { cx, cz, revision }]));
    queueVisibleTiles();
    if (!payload.chunks.length && !state.mapTiles.size)
      setMapStatus("waiting for chunks…");
  } catch { /* transient; next tick retries */ }
  finally { state.mapBusy = false; }
}

function setMapStatus(text) {
  const el = $("#map-status");
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

function clearMapTiles() {
  for (const tile of state.mapTiles.values()) {
    tile.image?.close?.();
    tile.textureImage?.close?.();
  }
  state.mapTiles.clear();
  state.mapManifest.clear();
  state.mapQueue = [];
  state.mapQueued.clear();
}

function resizeMapCanvas() {
  const canvas = $("#map-canvas");
  const rect = $("#map-view").getBoundingClientRect();
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, Math.round(rect.width * dpr));
  const height = Math.max(1, Math.round(rect.height * dpr));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  drawMap();
  queueVisibleTiles();
}

function visibleChunkBounds(margin = 1) {
  const rect = $("#map-view").getBoundingClientRect();
  if (!state.mapCenter || !rect.width || !rect.height) return null;
  const halfX = rect.width / state.mapScale / 2;
  const halfZ = rect.height / state.mapScale / 2;
  return {
    minX: Math.floor((state.mapCenter.x - halfX) / 16) - margin,
    maxX: Math.floor((state.mapCenter.x + halfX) / 16) + margin,
    minZ: Math.floor((state.mapCenter.z - halfZ) / 16) - margin,
    maxZ: Math.floor((state.mapCenter.z + halfZ) / 16) + margin,
  };
}

function queueVisibleTiles() {
  const bounds = visibleChunkBounds();
  if (!bounds || !state.selected) return;
  const textured = state.mapScale >= MAP_TEXTURE_SCALE;
  state.mapQueue = state.mapQueue.filter(tile => {
    const visible = tile.generation === state.mapGeneration &&
      tile.cx >= bounds.minX && tile.cx <= bounds.maxX &&
      tile.cz >= bounds.minZ && tile.cz <= bounds.maxZ &&
      (!tile.textured || textured);
    if (!visible) state.mapQueued.delete(tile.queueKey);
    return visible;
  });
  if (textured) pruneDetailedMapTiles(bounds);
  const additions = [];
  for (const [key, chunk] of state.mapManifest) {
    if (chunk.cx < bounds.minX || chunk.cx > bounds.maxX ||
        chunk.cz < bounds.minZ || chunk.cz > bounds.maxZ) continue;
    const cached = state.mapTiles.get(key);
    const revision = textured ? cached?.textureRevision : cached?.revision;
    const queueKey = `${state.mapGeneration}:${key}:${textured ? "texture" : "flat"}`;
    if (revision === chunk.revision || state.mapQueued.has(queueKey)) continue;
    state.mapQueued.add(queueKey);
    additions.push({
      ...chunk, key, queueKey, textured, generation: state.mapGeneration,
    });
  }
  state.mapQueue.push(...additions);
  state.mapQueue.sort((a, b) => {
    const ac = Math.hypot(a.cx * 16 + 8 - state.mapCenter.x,
                          a.cz * 16 + 8 - state.mapCenter.z);
    const bc = Math.hypot(b.cx * 16 + 8 - state.mapCenter.x,
                          b.cz * 16 + 8 - state.mapCenter.z);
    return ac - bc;
  });
  pumpMapTiles();
}

function pruneDetailedMapTiles(bounds) {
  for (const tile of state.mapTiles.values()) {
    if (!tile.textureImage || (tile.cx >= bounds.minX && tile.cx <= bounds.maxX &&
        tile.cz >= bounds.minZ && tile.cz <= bounds.maxZ)) continue;
    tile.textureImage.close?.();
    tile.textureImage = null;
    tile.textureRevision = null;
    tile.textureEtag = null;
  }
}

function pumpMapTiles() {
  while (state.mapActiveFetches < MAP_TILE_CONCURRENCY && state.mapQueue.length) {
    const tile = state.mapQueue.shift();
    state.mapActiveFetches += 1;
    fetchMapTile(tile).finally(() => {
      state.mapActiveFetches -= 1;
      state.mapQueued.delete(tile.queueKey);
      pumpMapTiles();
    });
  }
}

async function fetchMapTile(tile) {
  const id = state.selected;
  const cached = state.mapTiles.get(tile.key);
  const etag = tile.textured ? cached?.textureEtag : cached?.etag;
  const headers = etag ? { "If-None-Match": etag } : {};
  const query = tile.textured ? "?textured=1" : "";
  try {
    const response = await fetch(
      `api/bots/${id}/map/tiles/${tile.cx}/${tile.cz}.png${query}`, { headers });
    if (state.selected !== id || state.mapGeneration !== tile.generation) return;
    if (response.status === 304 && cached) {
      if (tile.textured) cached.textureRevision = tile.revision;
      else cached.revision = tile.revision;
      return;
    }
    if (!response.ok) return;
    const image = await createImageBitmap(await response.blob());
    if (state.selected !== id || state.mapGeneration !== tile.generation) {
      image.close?.();
      return;
    }
    // A flat and a textured request may overlap while crossing the zoom
    // threshold. Re-read the shared entry so whichever finishes last keeps
    // both images instead of replacing the other request's result.
    const entry = state.mapTiles.get(tile.key) || { cx: tile.cx, cz: tile.cz };
    if (tile.textured) {
      entry.textureImage?.close?.();
      entry.textureImage = image;
      entry.textureRevision = tile.revision;
      entry.textureEtag = response.headers.get("ETag");
    } else {
      entry.image?.close?.();
      entry.image = image;
      entry.revision = tile.revision;
      entry.etag = response.headers.get("ETag");
    }
    state.mapTiles.set(tile.key, entry);
    setMapStatus(null);
    drawMap();
  } catch { /* retry after the next manifest update */ }
}

function drawMap() {
  const canvas = $("#map-canvas");
  if (!canvas) return;
  const rect = $("#map-view").getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const dpr = canvas.width / rect.width;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.imageSmoothingEnabled = false;
  ctx.fillStyle = "#080b0e";
  ctx.fillRect(0, 0, rect.width, rect.height);
  if (!state.mapCenter) return;

  const tileSize = 16 * state.mapScale;
  for (const tile of state.mapTiles.values()) {
    const image = state.mapScale >= MAP_TEXTURE_SCALE && tile.textureImage
      ? tile.textureImage : tile.image;
    if (!image) continue;
    const x = rect.width / 2 + (tile.cx * 16 - state.mapCenter.x) * state.mapScale;
    const y = rect.height / 2 + (tile.cz * 16 - state.mapCenter.z) * state.mapScale;
    if (x + tileSize < 0 || y + tileSize < 0 || x > rect.width || y > rect.height)
      continue;
    ctx.drawImage(image, Math.round(x), Math.round(y),
                  Math.ceil(tileSize), Math.ceil(tileSize));
  }

  if (state.mapScale >= 12) drawBlockGrid(ctx, rect);
  drawMapBot(ctx, rect);
  updateMapTarget();
}

function drawBlockGrid(ctx, rect) {
  const scale = state.mapScale;
  const startX = rect.width / 2 +
    (Math.floor((state.mapCenter.x - rect.width / scale / 2)) - state.mapCenter.x) * scale;
  const startZ = rect.height / 2 +
    (Math.floor((state.mapCenter.z - rect.height / scale / 2)) - state.mapCenter.z) * scale;
  ctx.beginPath();
  for (let x = startX; x < rect.width; x += scale) { ctx.moveTo(x, 0); ctx.lineTo(x, rect.height); }
  for (let y = startZ; y < rect.height; y += scale) { ctx.moveTo(0, y); ctx.lineTo(rect.width, y); }
  ctx.strokeStyle = "rgba(0, 0, 0, .18)";
  ctx.lineWidth = 1;
  ctx.stroke();
}

function drawMapBot(ctx, rect) {
  const position = state.mapPosition;
  if (!position) return;
  const x = rect.width / 2 + (position.x - state.mapCenter.x) * state.mapScale;
  const y = rect.height / 2 + (position.z - state.mapCenter.z) * state.mapScale;
  if (x < -12 || y < -12 || x > rect.width + 12 || y > rect.height + 12) return;
  ctx.save();
  ctx.translate(x, y);
  // Minecraft yaw 0 faces south (+Z); this arrow's unrotated tip faces north.
  ctx.rotate(((position.yaw || 0) + 180) * Math.PI / 180);
  ctx.beginPath();
  ctx.moveTo(0, -9); ctx.lineTo(6, 7); ctx.lineTo(0, 4); ctx.lineTo(-6, 7);
  ctx.closePath();
  ctx.fillStyle = "#ef4f4f";
  ctx.strokeStyle = "#fff";
  ctx.lineWidth = 1.5;
  ctx.fill(); ctx.stroke();
  ctx.restore();
}

function mapPoint(clientX, clientY) {
  const rect = $("#map-view").getBoundingClientRect();
  if (!state.mapCenter || !rect.width || clientX < rect.left || clientX > rect.right ||
      clientY < rect.top || clientY > rect.bottom) return null;
  return {
    x: state.mapCenter.x + (clientX - rect.left - rect.width / 2) / state.mapScale,
    z: state.mapCenter.z + (clientY - rect.top - rect.height / 2) / state.mapScale,
    rect,
  };
}

function setMapScale(scale, anchorX = null, anchorY = null) {
  const oldPoint = anchorX == null ? null : mapPoint(anchorX, anchorY);
  const next = Math.max(MAP_MIN_SCALE, Math.min(MAP_MAX_SCALE, scale));
  if (oldPoint) {
    const rect = oldPoint.rect;
    state.mapCenter = {
      x: oldPoint.x - (anchorX - rect.left - rect.width / 2) / next,
      z: oldPoint.z - (anchorY - rect.top - rect.height / 2) / next,
    };
    state.mapFollow = false;
  }
  state.mapScale = next;
  updateMapScale();
  drawMap();
  queueVisibleTiles();
}

function zoomMap(direction, clientX = null, clientY = null) {
  const factor = direction > 0 ? 0.75 : 4 / 3;
  setMapScale(state.mapScale * factor, clientX, clientY);
}

function queueMapWheelZoom(event) {
  const rect = mapView.getBoundingClientRect();
  const unit = event.deltaMode === WheelEvent.DOM_DELTA_LINE
    ? 16
    : (event.deltaMode === WheelEvent.DOM_DELTA_PAGE ? rect.height : 1);
  mapWheelZoom.delta = Math.max(
    -240, Math.min(240, mapWheelZoom.delta + event.deltaY * unit));
  mapWheelZoom.x = event.clientX;
  mapWheelZoom.y = event.clientY;
  if (mapWheelZoom.frame !== null) return;
  mapWheelZoom.frame = requestAnimationFrame(() => {
    mapWheelZoom.frame = null;
    const delta = mapWheelZoom.delta;
    mapWheelZoom.delta = 0;
    if (!delta) return;
    const factor = Math.exp(-delta * 0.003);
    setMapScale(
      state.mapScale * factor, mapWheelZoom.x, mapWheelZoom.y);
  });
}

function updateMapScale() {
  const value = state.mapScale >= 1
    ? `${state.mapScale.toFixed(state.mapScale < 10 ? 1 : 0)} px/block`
    : `1 px/${Math.round(1 / state.mapScale)} blocks`;
  $("#map-scale").textContent = value;
}

function recenterMap() {
  state.mapFollow = true;
  state.mapCenter = state.mapPosition
    ? { x: state.mapPosition.x, z: state.mapPosition.z } : null;
  drawMap();
  queueVisibleTiles();
}

function updateMapTarget() {
  const marker = $("#map-target");
  const target = state.mapTarget;
  const view = $("#map-view");
  if (!target || !state.mapCenter) {
    marker.hidden = true;
    return;
  }
  const rect = view.getBoundingClientRect();
  const x = rect.width / 2 + (target.x - state.mapCenter.x) * state.mapScale;
  const y = rect.height / 2 + (target.z - state.mapCenter.z) * state.mapScale;
  if (x < 0 || x > rect.width || y < 0 || y > rect.height) {
    marker.hidden = true;
    return;
  }
  marker.style.left = `${x}px`;
  marker.style.top = `${y}px`;
  marker.className = `map-target ${target.phase || "started"}`;
  marker.hidden = false;
}

function applyNavigation(data) {
  if (!data?.target) return;
  if ((state.mapTarget?.navigationId || 0) > (data.navigation_id || 0)) return;
  state.mapTarget = {
    ...data.target, phase: data.phase, navigationId: data.navigation_id || 0,
  };
  updateMapTarget();
}

async function navigateFromMap(point) {
  if (!state.selected) return;
  const target = { x: Math.floor(point.x) + 0.5, z: Math.floor(point.z) + 0.5 };
  state.mapTarget = { ...target, phase: "started" };
  updateMapTarget();
  try {
    await api.navigate(state.selected, target.x, target.z);
  } catch (error) {
    state.mapTarget.phase = "stuck";
    updateMapTarget();
    logSystem(`map navigation failed: ${error.message}`);
  }
}

const mapView = $("#map-view");
mapView.addEventListener("pointerdown", (event) => {
  if (!mapPoint(event.clientX, event.clientY)) return;
  mapDrag.active = true;
  mapDrag.moved = false;
  mapDrag.x = event.clientX;
  mapDrag.y = event.clientY;
  mapDrag.center = { ...state.mapCenter };
  mapView.setPointerCapture(event.pointerId);
});
mapView.addEventListener("pointermove", (event) => {
  const point = mapPoint(event.clientX, event.clientY);
  const readout = $("#map-readout");
  if (point) {
    readout.textContent = `x ${Math.floor(point.x)}  z ${Math.floor(point.z)}`;
    readout.hidden = false;
  } else if (!mapDrag.active) readout.hidden = true;
  if (!mapDrag.active) return;
  const dx = event.clientX - mapDrag.x;
  const dy = event.clientY - mapDrag.y;
  if (Math.hypot(dx, dy) > 5) mapDrag.moved = true;
  if (mapDrag.moved) {
    mapView.classList.add("dragging");
    state.mapFollow = false;
    state.mapCenter = {
      x: mapDrag.center.x - dx / state.mapScale,
      z: mapDrag.center.z - dy / state.mapScale,
    };
    drawMap();
  }
});
mapView.addEventListener("pointerup", (event) => {
  if (!mapDrag.active) return;
  const dx = event.clientX - mapDrag.x;
  const dy = event.clientY - mapDrag.y;
  mapDrag.active = false;
  mapView.classList.remove("dragging");
  if (mapDrag.moved) {
    queueVisibleTiles();
  } else {
    const point = mapPoint(event.clientX, event.clientY);
    if (point) navigateFromMap(point);
  }
});
mapView.addEventListener("pointercancel", () => {
  mapDrag.active = false;
  mapView.classList.remove("dragging");
});
mapView.addEventListener("pointerleave", () => {
  if (!mapDrag.active) $("#map-readout").hidden = true;
});
mapView.addEventListener("wheel", (event) => {
  event.preventDefault();
  queueMapWheelZoom(event);
}, { passive: false });

$("#map-zoom-out").addEventListener("click", () => zoomMap(1));
$("#map-zoom-in").addEventListener("click", () => zoomMap(-1));
$("#map-recenter").addEventListener("click", recenterMap);
$("#map-live").addEventListener("change", (e) => {
  if (e.target.checked && state.selected) tickMap(true);
});

new ResizeObserver(resizeMapCanvas).observe(mapView);

function openMapModal() {
  if (!state.selected) return;
  closeVisionModal();
  $("#map-stage").appendChild($(".map-panel"));
  $("#map-pip-stage").appendChild(Vision.element());
  // Camera poses still animate every frame; only the expensive voxel geometry
  // refresh is throttled so map + PiP cannot starve Minecraft keepalives.
  Vision.setRefreshInterval(VISION_MAP_PIP_REFRESH_MS);
  $("#map-modal").hidden = false;
  requestAnimationFrame(() => { resizeMapCanvas(); Vision.resize(); });
}
function closeMapModal() {
  if ($("#map-modal").hidden) return;
  $("#map-modal").hidden = true;
  $(".right-col").insertBefore($(".map-panel"), $(".vision-panel"));
  $(".vision-view").insertBefore(Vision.element(), $("#vision-status"));
  Vision.setRefreshInterval(VISION_SMALL_REFRESH_MS);
  requestAnimationFrame(() => { resizeMapCanvas(); Vision.resize(); });
}
$("#map-expand").addEventListener("click", openMapModal);
$("#map-modal-close").addEventListener("click", closeMapModal);
$("#map-modal").addEventListener("click", (event) => {
  if (event.target.id === "map-modal") closeMapModal();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeMapModal();
});

$("#vision-range").addEventListener("change", (e) => {
  $("#control-render-distance").value = e.target.value;
  if (state.selected) Vision.setRange(Number(e.target.value));
});
$("#control-render-distance").value = $("#vision-range").value;
$("#control-render-distance").addEventListener("change", (e) => {
  $("#vision-range").value = e.target.value;
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
  closeVisionChat(false);
  closeVisionInventory(false);
  stopVisionLookaround();
  stopVisionControl();
  stopFreecam();
  $("#control-menu").hidden = true;
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
  if (e.key === "Escape" && !visionControl.active && !freecam.active
      && !visionLookaround.active) closeVisionModal();
});

// Pointer-lock first-person controls. The socket carries state snapshots at
// Minecraft's 20 Hz tick rate; the host times out stale input independently.
const visionControl = {
  active: false, paused: false, keys: new Set(), timer: null,
  lockPending: false, lockPendingTimer: null,
};
const visionLookaround = {
  active: false, lockPending: false, lockPendingTimer: null,
};
const controlMods = { doubleJump: false, superSpeed: false };
const visionChat = {
  open: false, resumeMode: null, returnMenu: false, history: [],
  ignoreUnlock: false, ignoreUnlockTimer: null,
  escapeClosing: false, escapeResumeMode: null,
};
const visionInventory = { open: false, resumeMode: null };
const VISION_CHAT_VISIBLE_MS = 12000;
const VISION_CHAT_MAX_HISTORY = 50;
const CONTROL_KEYS = new Set([
  "KeyW", "KeyA", "KeyS", "KeyD", "Space", "ShiftLeft", "ShiftRight",
]);

function clearPendingPointerLock(mode) {
  mode.lockPending = false;
  clearTimeout(mode.lockPendingTimer);
  mode.lockPendingTimer = null;
}

function requestVisionPointerLock(mode, onFailure) {
  const target = Vision.element();
  if (!target.requestPointerLock) {
    onFailure();
    return;
  }
  clearPendingPointerLock(mode);
  mode.lockPending = true;
  const fail = () => {
    if (!mode.lockPending) return;
    clearPendingPointerLock(mode);
    onFailure();
  };
  mode.lockPendingTimer = setTimeout(() => {
    if (document.pointerLockElement === target) clearPendingPointerLock(mode);
    else fail();
  }, 1500);
  try {
    const lock = target.requestPointerLock();
    if (lock && typeof lock.catch === "function") lock.catch(fail);
  } catch {
    fail();
  }
}

function controlAxis(positive, negative) {
  return (visionControl.keys.has(positive) ? 1 : 0)
    - (visionControl.keys.has(negative) ? 1 : 0);
}

function sendVisionControl(active = visionControl.active) {
  const look = Vision.look();
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN || (active && !look)) return;
  const moving = active && !visionControl.paused;
  state.ws.send(JSON.stringify({
    type: "control",
    data: {
      active,
      forward: moving ? controlAxis("KeyW", "KeyS") : 0,
      strafe: moving ? controlAxis("KeyD", "KeyA") : 0,
      jump: moving && visionControl.keys.has("Space"),
      sneak: moving && (visionControl.keys.has("ShiftLeft") || visionControl.keys.has("ShiftRight")),
      double_jump: controlMods.doubleJump,
      super_speed: controlMods.superSpeed,
      yaw: look?.yaw ?? 0,
      pitch: look?.pitch ?? 0,
    },
  }));
}

function setControlUi(active) {
  const button = $("#vision-control");
  button.classList.toggle("active", active);
  button.textContent = active ? "Stop" : "Control";
  $("#vision-crosshair").hidden = !active || visionControl.paused;
}

function startVisionControl() {
  if (!state.selected || visionControl.active) return;
  stopVisionLookaround();
  stopFreecam();
  if (Vision.renderer() !== "custom") {
    Vision.setRenderer("custom");
    $("#vision-renderer").value = "custom";
  }
  openVisionModal();
  visionControl.active = true;
  visionControl.paused = false;
  Vision.setControlActive(true);
  visionControl.keys.clear();
  $("#control-menu").hidden = true;
  setControlUi(true);
  sendVisionControl();
  visionControl.timer = setInterval(sendVisionControl, 50);
  requestVisionPointerLock(visionControl, pauseVisionControl);
}

function stopVisionControl() {
  if (!visionControl.active) return;
  clearPendingPointerLock(visionControl);
  sendVisionControl(false);
  visionControl.active = false;
  visionControl.paused = false;
  Vision.setControlActive(false);
  visionControl.keys.clear();
  clearInterval(visionControl.timer);
  visionControl.timer = null;
  $("#control-menu").hidden = true;
  setControlUi(false);
  if (document.pointerLockElement) document.exitPointerLock();
}

function pauseVisionControl() {
  if (!visionControl.active || visionControl.paused) return;
  clearPendingPointerLock(visionControl);
  visionControl.paused = true;
  visionControl.keys.clear();
  sendVisionControl();
  showVisionMenu("control");
  setControlUi(true);
  if (document.pointerLockElement === Vision.element()) document.exitPointerLock();
}

function showVisionMenu(mode) {
  closeVisionChat(false);
  const tools = Vision.getTools();
  $("#tool-xray").checked = tools.xray;
  $("#tool-chests").checked = tools.chests;
  $("#tool-freecam").checked = mode === "freecam";
  $("#tool-double-jump").checked = controlMods.doubleJump;
  $("#tool-super-speed").checked = controlMods.superSpeed;
  $("#control-render-distance").value = $("#vision-range").value;
  $("#control-exit").textContent = mode === "freecam"
    ? (freecam.returnToControl ? "Return to bot control" : "Exit freecam")
    : "Exit control mode";
  showControlMenuPanel("main");
  $("#control-menu").hidden = false;
}

function showControlMenuPanel(panel) {
  $("#control-menu-main").hidden = panel !== "main";
  $("#control-mods-menu").hidden = panel !== "mods";
}

function keepVisionControlPaused() {
  if (!visionControl.active) return;
  visionControl.paused = true;
  visionControl.keys.clear();
  sendVisionControl();
  $("#control-menu").hidden = true;
  setControlUi(true);
}

function resumeVisionControl(showMenuOnFailure = true) {
  if (!visionControl.active || !visionControl.paused) return;
  const onFailure = showMenuOnFailure ? pauseVisionControl : keepVisionControlPaused;
  visionControl.paused = false;
  $("#control-menu").hidden = true;
  setControlUi(true);
  requestVisionPointerLock(visionControl, onFailure);
}

$("#vision-control").addEventListener("click", () => {
  if (visionControl.active) stopVisionControl();
  else startVisionControl();
});
document.addEventListener("pointerlockchange", () => {
  const locked = document.pointerLockElement === Vision.element();
  if (locked) {
    clearPendingPointerLock(visionControl);
    clearPendingPointerLock(freecam);
    clearPendingPointerLock(visionLookaround);
  }
  if (!locked && visionChat.ignoreUnlock) {
    visionChat.ignoreUnlock = false;
    clearTimeout(visionChat.ignoreUnlockTimer);
    visionChat.ignoreUnlockTimer = null;
    return;
  }
  if (visionChat.open && !locked) return;
  if (visionInventory.open && !locked) return;
  if (!locked && (visionControl.lockPending || freecam.lockPending
      || visionLookaround.lockPending)) return;
  if (visionControl.active) {
    if (locked) {
      visionControl.paused = false;
      $("#control-menu").hidden = true;
      setControlUi(true);
    } else {
      pauseVisionControl();
    }
  } else if (freecam.active) {
    if (locked) {
      freecam.paused = false;
      $("#control-menu").hidden = true;
      $("#vision-crosshair").hidden = false;
    } else {
      pauseFreecam();
    }
  } else if (visionLookaround.active && !locked) {
    stopVisionLookaround(false);
  }
});
document.addEventListener("mousemove", (e) => {
  if ((!visionControl.active && !visionLookaround.active)
      || document.pointerLockElement !== Vision.element()) return;
  Vision.adjustLook(e.movementX, e.movementY);
});

function startVisionLookaround() {
  if (visionLookaround.active || $("#vision-modal").hidden) return;
  stopVisionControl();
  stopFreecam();
  if (Vision.renderer() !== "custom") {
    Vision.setRenderer("custom");
    $("#vision-renderer").value = "custom";
    $("#vision-stage").appendChild(Vision.element());
    requestAnimationFrame(() => Vision.resize());
  }
  visionLookaround.active = true;
  Vision.setLookaround(true);
  $("#vision-lookaround").checked = true;
  requestVisionPointerLock(visionLookaround, () => stopVisionLookaround(false));
}

function stopVisionLookaround(releasePointer = true) {
  if (!visionLookaround.active && !visionLookaround.lockPending) return;
  clearPendingPointerLock(visionLookaround);
  visionLookaround.active = false;
  Vision.setLookaround(false);
  $("#vision-lookaround").checked = false;
  if (releasePointer && document.pointerLockElement === Vision.element()) {
    document.exitPointerLock();
  }
}

$("#vision-lookaround").addEventListener("change", (e) => {
  if (e.target.checked) startVisionLookaround();
  else stopVisionLookaround();
});
$("#vision-inventory-close").addEventListener("click", () => closeVisionInventory(true));
document.addEventListener("keydown", (e) => {
  if (!visionControl.active) return;
  if (e.key === "Escape") {
    e.preventDefault();
    pauseVisionControl();
    return;
  }
  if (visionControl.paused) return;
  if (CONTROL_KEYS.has(e.code)) {
    e.preventDefault();
    visionControl.keys.add(e.code);
    sendVisionControl();
  }
});
document.addEventListener("keyup", (e) => {
  if (!visionControl.active || visionControl.paused || !CONTROL_KEYS.has(e.code)) return;
  e.preventDefault();
  visionControl.keys.delete(e.code);
  sendVisionControl();
});
window.addEventListener("blur", pauseVisionControl);

function renderVisionChat() {
  const messages = $("#vision-chat-messages");
  if (!messages) return;
  const now = Date.now();
  const entries = visionChat.open
    ? visionChat.history.slice(-14)
    : visionChat.history.slice(-8);
  messages.replaceChildren(...entries.map(entry => {
    const line = el("div", "vision-chat-message", entry.text);
    if (now - entry.receivedAt > VISION_CHAT_VISIBLE_MS) line.classList.add("stale");
    return line;
  }));
  if (visionChat.open) messages.scrollTop = messages.scrollHeight;
}

function appendVisionChat(text, ts) {
  if (!text) return;
  visionChat.history.push({
    text,
    receivedAt: Number.isFinite(ts) ? ts * 1000 : Date.now(),
  });
  if (visionChat.history.length > VISION_CHAT_MAX_HISTORY) visionChat.history.shift();
  renderVisionChat();
}

function resetVisionChat() {
  visionChat.history.length = 0;
  renderVisionChat();
}

function openVisionChat() {
  if (visionChat.open || $("#vision-modal").hidden || !state.selected) return;
  visionChat.open = true;
  visionChat.resumeMode = visionControl.active && !visionControl.paused
    ? "control"
    : (freecam.active && !freecam.paused ? "freecam" : null);
  visionChat.returnMenu = !$("#control-menu").hidden;
  if (visionControl.active) {
    visionControl.paused = true;
    visionControl.keys.clear();
    sendVisionControl();
    setControlUi(true);
  }
  if (freecam.active) {
    freecam.paused = true;
    FREECAM_KEYS.forEach(key => Vision.setFreecamKey(key, false));
    $("#vision-crosshair").hidden = true;
  }
  $("#control-menu").hidden = true;
  $("#vision-chat").classList.add("open");
  $("#vision-chat-form").hidden = false;
  renderVisionChat();
  if (document.pointerLockElement === Vision.element()) {
    visionChat.ignoreUnlock = true;
    clearTimeout(visionChat.ignoreUnlockTimer);
    visionChat.ignoreUnlockTimer = setTimeout(() => {
      visionChat.ignoreUnlock = false;
      visionChat.ignoreUnlockTimer = null;
    }, 1000);
    document.exitPointerLock();
  }
  requestAnimationFrame(() => $("#vision-chat-input").focus());
}

function closeVisionChat(resume = true) {
  if (!visionChat.open) return;
  const resumeMode = visionChat.resumeMode;
  const returnMenu = visionChat.returnMenu;
  visionChat.open = false;
  visionChat.resumeMode = null;
  visionChat.returnMenu = false;
  $("#vision-chat-input").value = "";
  $("#vision-chat-form").hidden = true;
  $("#vision-chat").classList.remove("open");
  renderVisionChat();
  if (!resume) return;
  if (resumeMode === "control") resumeVisionControl();
  else if (resumeMode === "freecam") resumeFreecam();
  else if (returnMenu && visionControl.active) showVisionMenu("control");
  else if (returnMenu && freecam.active) showVisionMenu("freecam");
}

function openVisionInventory() {
  if (visionInventory.open || $("#vision-modal").hidden
      || !$("#control-menu").hidden) return;
  visionInventory.open = true;
  visionInventory.resumeMode = visionControl.active && !visionControl.paused
    ? "control"
    : (freecam.active && !freecam.paused ? "freecam" : null);
  if (visionControl.active) {
    visionControl.paused = true;
    visionControl.keys.clear();
    sendVisionControl();
    setControlUi(true);
  }
  if (freecam.active) {
    freecam.paused = true;
    FREECAM_KEYS.forEach(key => Vision.setFreecamKey(key, false));
    $("#vision-crosshair").hidden = true;
  }
  if (visionLookaround.active) stopVisionLookaround();
  $("#vision-inventory").hidden = false;
  if (document.pointerLockElement === Vision.element()) document.exitPointerLock();
}

function closeVisionInventory(resume = true) {
  if (!visionInventory.open) return;
  const resumeMode = visionInventory.resumeMode;
  visionInventory.open = false;
  visionInventory.resumeMode = null;
  $("#vision-inventory").hidden = true;
  if (!resume) return;
  if (resumeMode === "control") resumeVisionControl(false);
  else if (resumeMode === "freecam") resumeFreecam(false);
}

$("#vision-chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#vision-chat-input");
  const message = input.value.trim();
  if (!message || !state.selected) return;
  const request = sendChat(message);
  closeVisionChat(true);
  try {
    await request;
  } catch (err) {
    $("#detail-error").textContent = err.message;
  }
});

document.addEventListener("keydown", (e) => {
  if (visionChat.open) {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopImmediatePropagation();
      visionChat.escapeClosing = true;
      visionChat.escapeResumeMode = visionControl.active
        ? "control"
        : (freecam.active ? "freecam" : null);
      closeVisionChat(false);
      $("#control-menu").hidden = true;
    }
    return;
  }
  if (visionChat.escapeClosing && e.key === "Escape") {
    e.preventDefault();
    e.stopImmediatePropagation();
    return;
  }
  const target = e.target;
  const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement;
  if (e.code !== "KeyT" || typing || $("#vision-modal").hidden) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  openVisionChat();
}, true);

document.addEventListener("keyup", (e) => {
  if (!visionChat.escapeClosing || e.key !== "Escape") return;
  e.preventDefault();
  e.stopImmediatePropagation();
  const resumeMode = visionChat.escapeResumeMode;
  visionChat.escapeClosing = false;
  visionChat.escapeResumeMode = null;
  if (visionChat.open || !$("#control-menu").hidden) return;
  if (resumeMode === "control") resumeVisionControl(false);
  else if (resumeMode === "freecam") resumeFreecam(false);
}, true);

document.addEventListener("keydown", (e) => {
  if (!visionInventory.open || e.key !== "Escape") return;
  e.preventDefault();
  e.stopImmediatePropagation();
  closeVisionInventory(true);
}, true);

document.addEventListener("keydown", (e) => {
  if (e.code !== "KeyE" || $("#vision-modal").hidden || visionChat.open) return;
  const target = e.target;
  const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement
    || target instanceof HTMLSelectElement;
  if (typing) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  if (visionInventory.open) closeVisionInventory(true);
  else openVisionInventory();
}, true);

Vision.element().addEventListener("click", () => {
  if (visionChat.open || visionInventory.open || !$("#control-menu").hidden) return;
  if (visionControl.active && visionControl.paused) resumeVisionControl();
  else if (freecam.active && freecam.paused) resumeFreecam();
});

setInterval(() => {
  if (!visionChat.open && !$("#vision-modal").hidden) renderVisionChat();
}, 1000);

$("#control-resume").addEventListener("click", () => {
  if (visionControl.active) resumeVisionControl();
  else if (freecam.active) resumeFreecam();
});
$("#control-mods-open").addEventListener("click", () => showControlMenuPanel("mods"));
$("#control-mods-back").addEventListener("click", () => showControlMenuPanel("main"));
$("#control-exit").addEventListener("click", () => {
  if (visionControl.active) stopVisionControl();
  else if (freecam.active) stopFreecam(true);
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || $("#control-menu").hidden) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  if (!$("#control-mods-menu").hidden) {
    showControlMenuPanel("main");
  } else if (visionControl.active) {
    resumeVisionControl();
  } else if (freecam.active) {
    resumeFreecam();
  }
}, true);

// -- vision tools (freecam / xray / chest highlight) ------------------------
// Freecam is a browser-only fly camera (never drives the bot).
const freecam = {
  active: false, paused: false, returnToControl: false,
  lockPending: false, lockPendingTimer: null,
};
const FREECAM_KEYS = new Set([
  "KeyW", "KeyA", "KeyS", "KeyD", "Space", "ShiftLeft", "ShiftRight",
]);

function startFreecam() {
  if (freecam.active) return;
  stopVisionLookaround();
  const returnToControl = visionControl.active;
  if (returnToControl) stopVisionControl();
  freecam.active = true;
  freecam.paused = false;
  freecam.returnToControl = returnToControl;
  Vision.setTool("freecam", true);
  $("#tool-freecam").checked = true;
  $("#control-menu").hidden = true;
  $("#vision-crosshair").hidden = false;
  requestVisionPointerLock(freecam, pauseFreecam);
}

function stopFreecam(resumeControl = false) {
  if (!freecam.active) return;
  clearPendingPointerLock(freecam);
  const shouldResumeControl = resumeControl && freecam.returnToControl;
  freecam.active = false;
  freecam.paused = false;
  freecam.returnToControl = false;
  Vision.setTool("freecam", false);
  FREECAM_KEYS.forEach(k => Vision.setFreecamKey(k, false));
  $("#tool-freecam").checked = false;
  $("#control-menu").hidden = true;
  if (!visionControl.active) $("#vision-crosshair").hidden = true;
  if (document.pointerLockElement === Vision.element()) document.exitPointerLock();
  if (shouldResumeControl) startVisionControl();
}

function pauseFreecam() {
  if (!freecam.active || freecam.paused) return;
  clearPendingPointerLock(freecam);
  freecam.paused = true;
  FREECAM_KEYS.forEach(k => Vision.setFreecamKey(k, false));
  $("#vision-crosshair").hidden = true;
  showVisionMenu("freecam");
  if (document.pointerLockElement === Vision.element()) document.exitPointerLock();
}

function keepFreecamPaused() {
  if (!freecam.active) return;
  freecam.paused = true;
  FREECAM_KEYS.forEach(key => Vision.setFreecamKey(key, false));
  $("#control-menu").hidden = true;
  $("#vision-crosshair").hidden = true;
}

function resumeFreecam(showMenuOnFailure = true) {
  if (!freecam.active || !freecam.paused) return;
  const onFailure = showMenuOnFailure ? pauseFreecam : keepFreecamPaused;
  freecam.paused = false;
  $("#control-menu").hidden = true;
  $("#vision-crosshair").hidden = false;
  requestVisionPointerLock(freecam, onFailure);
}

function resetVisionTools() {
  stopFreecam();
  $("#control-menu").hidden = true;
  for (const id of ["tool-xray", "tool-chests", "tool-double-jump", "tool-super-speed"]) {
    $("#" + id).checked = false;
  }
  controlMods.doubleJump = false;
  controlMods.superSpeed = false;
  Vision.setTool("xray", false);
  Vision.setTool("chests", false);
}

$("#tool-xray").addEventListener("change", (e) => Vision.setTool("xray", e.target.checked));
$("#tool-chests").addEventListener("change", (e) => Vision.setTool("chests", e.target.checked));
$("#tool-double-jump").addEventListener("change", (e) => {
  controlMods.doubleJump = e.target.checked;
  sendVisionControl();
});
$("#tool-super-speed").addEventListener("change", (e) => {
  controlMods.superSpeed = e.target.checked;
  sendVisionControl();
});
$("#tool-freecam").addEventListener("change", (e) => {
  if (e.target.checked) startFreecam(); else stopFreecam(true);
  // Drop focus after requesting pointer lock so the click's user activation is
  // still available to the browser for the mode transition.
  e.target.blur();
});

document.addEventListener("keydown", (e) => {
  if (!freecam.active) return;
  if (e.key === "Escape") {
    e.preventDefault();
    pauseFreecam();
    return;
  }
  if (!freecam.paused && FREECAM_KEYS.has(e.code)) {
    e.preventDefault();
    Vision.setFreecamKey(e.code, true);
  }
});
document.addEventListener("keyup", (e) => {
  if (freecam.active && !freecam.paused && FREECAM_KEYS.has(e.code)) {
    e.preventDefault();
    Vision.setFreecamKey(e.code, false);
  }
});
document.addEventListener("mousemove", (e) => {
  if (!freecam.active || freecam.paused) return;
  // Freecam looks on pointer-lock movement, or click-drag when not locked.
  if (document.pointerLockElement === Vision.element()) {
    Vision.adjustLook(e.movementX, e.movementY);
  } else if (e.buttons & 1) {
    Vision.adjustLook(e.movementX, e.movementY);
  }
});
window.addEventListener("blur", pauseFreecam);

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
refreshBots();
setInterval(refreshBots, 4000);  // keep the list/status dots fresh
