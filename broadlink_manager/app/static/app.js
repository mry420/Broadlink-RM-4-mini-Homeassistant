/* Broadlink IR Manager – frontend (vanilla JS).
 * All URLs are relative so the UI works behind Home Assistant ingress. */
(() => {
  "use strict";

  const LEARN_TIMEOUT = 30;
  const COLLAPSED_COUNT = 8;

  const ICONS = {
    tv: { label: "TV", svg: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8 21h8M12 17v4"/>' },
    soundbar: { label: "Soundbar", svg: '<rect x="2" y="9" width="20" height="6" rx="3"/><circle cx="7" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="17" cy="12" r="1"/>' },
    ac: { label: "Klima", svg: '<path d="M12 2v20M4.9 6l14.2 12M19.1 6 4.9 18"/><path d="m9 3 3 2 3-2M9 21l3-2 3 2"/>' },
    fan: { label: "Ventilator", svg: '<circle cx="12" cy="12" r="2"/><path d="M12 10c0-4 1-7 4-7s3 4-2 7M14 12c4 0 7 1 7 4s-4 3-7-2M12 14c0 4-1 7-4 7s-3-4 2-7M10 12c-4 0-7-1-7-4s4-3 7 2"/>' },
    light: { label: "Licht", svg: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.6 10.8c.7.5 1.1 1.3 1.1 2.2h5c0-.9.4-1.7 1.1-2.2A6 6 0 0 0 12 3Z"/>' },
    led: { label: "LED-Strip", svg: '<path d="M3 15c3-6 6 6 9 0s6 6 9 0"/><circle cx="6" cy="12.5" r="1"/><circle cx="12" cy="15" r="1"/><circle cx="18" cy="12.5" r="1"/>' },
    projector: { label: "Beamer", svg: '<rect x="2" y="8" width="20" height="9" rx="2"/><circle cx="16" cy="12.5" r="2.5"/><path d="M6 12h4M5 17v2M19 17v2"/>' },
    receiver: { label: "Receiver", svg: '<rect x="2" y="7" width="20" height="10" rx="2"/><circle cx="17" cy="12" r="2.5"/><path d="M5.5 11h6M5.5 13.5h4"/>' },
    remote: { label: "Sonstiges", svg: '<rect x="7" y="2" width="10" height="20" rx="3"/><circle cx="12" cy="7" r="1.6"/><path d="M10 12h4M10 15.5h4M10 19h4"/>' },
  };

  const SUGGESTIONS = ["Power", "An", "Aus", "Volume Up", "Volume Down", "Mute", "Kanal +", "Kanal −", "Input", "OK", "Heller", "Dunkler"];

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    devices: [],
    expanded: new Set(),
    settings: { remote_entity: "remote.rm4_mini" },
    learn: { deviceId: null, learnId: null, code: null, controller: null, timer: null, slot: null, source: false, presetName: "" },
    confirmAction: null,
    types: null,
    smartir: { deviceId: null, view: null, saving: null },
    db: { type: "media_player", cache: {}, selected: null },
  };

  // ------------------------------------------------------------------ API

  async function api(path, options = {}) {
    const opts = { headers: {}, ...options };
    if (opts.body && typeof opts.body !== "string") {
      opts.body = JSON.stringify(opts.body);
      opts.headers["Content-Type"] = "application/json";
    }
    // Relative path (no leading slash) keeps the ingress prefix.
    const res = await fetch(path.replace(/^\//, ""), opts);
    const type = res.headers.get("content-type") || "";
    const payload = type.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = payload && payload.detail;
      const message = typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((d) => d.msg).join(", ")
        : `Fehler ${res.status}`;
      const err = new Error(message);
      err.status = res.status;
      throw err;
    }
    return payload;
  }

  // ------------------------------------------------------------------ helpers

  function iconSvg(key, cls = "h-6 w-6") {
    const icon = ICONS[key] || ICONS.remote;
    return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icon.svg}</svg>`;
  }

  function toast(message, type = "info") {
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.textContent = message;
    $("#toasts").appendChild(el);
    requestAnimationFrame(() => el.classList.add("toast-visible"));
    setTimeout(() => {
      el.classList.remove("toast-visible");
      setTimeout(() => el.remove(), 250);
    }, type === "error" ? 5000 : 2600);
  }

  let lastFocus = null;

  function openModal(id) {
    lastFocus = document.activeElement;
    const modal = $(id);
    modal.classList.remove("hidden");
    requestAnimationFrame(() => modal.classList.add("modal-open"));
    const panel = $(".modal-panel", modal);
    panel.scrollTop = 0;
    const target = $("[data-autofocus]", modal) || panel;
    setTimeout(() => target.focus({ preventScroll: target === panel }), 50);
  }

  function closeModal(id) {
    const modal = $(id);
    if (modal.classList.contains("hidden")) return;
    modal.classList.remove("modal-open");
    setTimeout(() => modal.classList.add("hidden"), 150);
    if (id === "#modal-learn") stopLearning(true);
    if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) { /* fall through to legacy copy */ }
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    area.remove();
    return ok;
  }

  // ------------------------------------------------------------------ status

  async function refreshStatus() {
    const pill = $("#status-pill");
    pill.dataset.state = "pending";
    $("#status-text").textContent = "Verbinde …";
    try {
      const s = await api("api/status");
      if (s.ip) $("#status-ip").textContent = s.ip;
      if (s.connected) {
        pill.dataset.state = "ok";
        $("#status-text").textContent = "Verbunden";
        pill.title = [s.model, s.mac].filter(Boolean).join(" · ");
      } else {
        pill.dataset.state = "error";
        $("#status-text").textContent = "Offline";
        pill.title = s.error || "Nicht verbunden";
      }
    } catch (err) {
      pill.dataset.state = "error";
      $("#status-text").textContent = "Offline";
      pill.title = err.message;
    }
  }

  // ------------------------------------------------------------------ devices

  async function loadDevices() {
    try {
      const data = await api("api/devices");
      state.devices = data.devices || [];
      if (data.settings) state.settings = data.settings;
      render();
    } catch (err) {
      $("#loading").textContent = `Geräte konnten nicht geladen werden: ${err.message}`;
    }
  }

  function render() {
    $("#loading").classList.add("hidden");
    const grid = $("#device-grid");
    grid.replaceChildren();
    $("#empty-state").classList.toggle("hidden", state.devices.length > 0);

    for (const device of state.devices) {
      const card = $("#tpl-device").content.firstElementChild.cloneNode(true);
      card.dataset.deviceId = device.id;
      $(".device-icon", card).innerHTML = iconSvg(device.icon);
      $(".device-name", card).textContent = device.name;
      const commands = device.commands || [];
      $(".device-count", card).textContent = commands.length === 1 ? "1 Taste" : `${commands.length} Tasten`;
      $(".no-commands", card).classList.toggle("hidden", commands.length > 0);
      const sir = device.smartir;
      const sirBtn = $(".smartir-btn", card);
      sirBtn.dataset.state = sir ? (sir.installed ? "installed" : "configured") : "none";
      sirBtn.title = sir ? (sir.installed ? `SmartIR installiert (Code ${sir.code})` : `SmartIR eingerichtet (Code ${sir.code}), noch nicht installiert`) : "Als SmartIR-Gerät in Home Assistant nutzen";

      const list = $(".command-list", card);
      const expanded = state.expanded.has(device.id);
      const visible = expanded ? commands : commands.slice(0, COLLAPSED_COUNT);
      if (commands.length > COLLAPSED_COUNT) {
        const more = $(".show-more", card);
        more.classList.remove("hidden");
        more.textContent = expanded ? "Weniger anzeigen" : `Alle ${commands.length} Tasten anzeigen`;
      }
      for (const command of visible) {
        const row = $("#tpl-command").content.firstElementChild.cloneNode(true);
        row.dataset.commandId = command.id;
        $(".command-name", row).textContent = command.name;
        $(".command-name", row).title = command.name;
        list.appendChild(row);
      }
      grid.appendChild(card);
    }
  }

  function deviceById(id) {
    return state.devices.find((d) => d.id === id);
  }

  function buildIconPicker() {
    const picker = $("#icon-picker");
    picker.replaceChildren();
    Object.entries(ICONS).forEach(([key, icon], index) => {
      const label = document.createElement("label");
      label.className = "icon-option";
      label.innerHTML = `<input type="radio" name="icon" value="${key}" class="sr-only" ${index === 0 ? "checked" : ""}>${iconSvg(key, "h-5 w-5")}<span>${icon.label}</span>`;
      picker.appendChild(label);
    });
  }

  async function createDevice(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const name = form.name.value.trim();
    if (!name) return;
    const icon = (form.querySelector("input[name=icon]:checked") || {}).value || "remote";
    try {
      const device = await api("api/devices", { method: "POST", body: { name, icon } });
      state.devices.push(device);
      render();
      closeModal("#modal-device");
      toast(`„${device.name}“ angelegt`, "success");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function confirmDelete(title, text, action) {
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    state.confirmAction = action;
    openModal("#modal-confirm");
  }

  async function sendCommand(button, deviceId, commandId) {
    button.disabled = true;
    button.classList.add("is-busy");
    try {
      await api("api/send", { method: "POST", body: { device_id: deviceId, command_id: commandId } });
      button.classList.add("is-sent");
      setTimeout(() => button.classList.remove("is-sent"), 900);
    } catch (err) {
      toast(err.message, "error");
      refreshStatus();
    } finally {
      button.disabled = false;
      button.classList.remove("is-busy");
    }
  }

  // ------------------------------------------------------------------ learning

  function showLearnStep(step) {
    for (const id of ["learn-waiting", "learn-success", "learn-error"]) {
      $(`#${id}`).classList.toggle("hidden", id !== step);
    }
  }

  function stopLearning(cancelOnServer) {
    const l = state.learn;
    if (l.timer) clearInterval(l.timer);
    l.timer = null;
    if (l.controller) {
      if (cancelOnServer) api("api/learn/cancel", { method: "POST" }).catch(() => {});
      l.controller.abort();
      l.controller = null;
    }
  }

  function startCountdown(seconds) {
    const ring = $("#countdown-ring");
    const value = $("#countdown-value");
    const circumference = 2 * Math.PI * 44;
    ring.style.strokeDasharray = `${circumference}`;
    const end = Date.now() + seconds * 1000;
    const tick = () => {
      const left = Math.max(0, end - Date.now());
      value.textContent = String(Math.ceil(left / 1000));
      ring.style.strokeDashoffset = `${circumference * (1 - left / (seconds * 1000))}`;
    };
    tick();
    state.learn.timer = setInterval(tick, 200);
  }

  async function startLearning() {
    stopLearning(false);
    const l = state.learn;
    l.learnId = null;
    l.code = null;
    showLearnStep("learn-waiting");
    startCountdown(LEARN_TIMEOUT);
    const controller = new AbortController();
    l.controller = controller;
    try {
      const result = await api("api/learn", {
        method: "POST",
        body: { timeout: LEARN_TIMEOUT },
        signal: controller.signal,
      });
      if (controller.signal.aborted || result.cancelled) return;
      l.learnId = result.learn_id;
      l.code = result.code;
      $("#learn-length").textContent = result.length;
      $("#learn-code").textContent = result.code;
      $("#command-name").value = l.presetName || "";
      showLearnStep("learn-success");
      setTimeout(() => $("#command-name").focus(), 50);
    } catch (err) {
      if (err.name === "AbortError" || controller.signal.aborted) return;
      $("#learn-error-text").textContent = err.message;
      showLearnStep("learn-error");
      if (err.status === 503) refreshStatus();
    } finally {
      if (l.controller === controller) {
        l.controller = null;
        clearInterval(l.timer);
        l.timer = null;
      }
    }
  }

  function openLearnModal(deviceId, preset = {}) {
    const device = deviceById(deviceId);
    if (!device) return;
    state.learn.deviceId = deviceId;
    state.learn.slot = preset.slot || null;
    state.learn.source = Boolean(preset.source);
    state.learn.presetName = preset.name || "";
    $("#learn-device-name").textContent = preset.label ? `${device.name} · ${preset.label}` : device.name;
    const existing = new Set((device.commands || []).map((c) => c.name.toLowerCase()));
    const box = $("#name-suggestions");
    box.replaceChildren();
    for (const name of SUGGESTIONS.filter((n) => !existing.has(n.toLowerCase()))) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = name;
      chip.addEventListener("click", () => { $("#command-name").value = name; $("#command-name").focus(); });
      box.appendChild(chip);
    }
    openModal("#modal-learn");
    startLearning();
  }

  async function saveLearned(event) {
    event.preventDefault();
    const name = $("#command-name").value.trim();
    const l = state.learn;
    if (!name || !l.learnId) return;
    const device = deviceById(l.deviceId);
    const overwrite = device && (device.commands || []).some((c) => c.name.toLowerCase() === name.toLowerCase());
    try {
      await api("api/save-command", {
        method: "POST",
        body: { device_id: l.deviceId, name, learn_id: l.learnId, smartir_slot: l.slot, smartir_source: l.source },
      });
      l.learnId = null;
      closeModal("#modal-learn");
      toast(overwrite ? `„${name}“ aktualisiert` : `„${name}“ gespeichert`, "success");
      await loadDevices();
      if (isOpen("#modal-smartir") && state.smartir.deviceId === l.deviceId) await loadSmartir();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function testLearned() {
    const button = $("#learn-test");
    if (!state.learn.code) return;
    button.disabled = true;
    try {
      await api("api/send", { method: "POST", body: { code: state.learn.code } });
      toast("Testsignal gesendet", "success");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------------------------ YAML export

  async function loadYaml() {
    const out = $("#yaml-output");
    const entity = $("#remote-entity").value.trim() || "remote.rm4_mini";
    out.textContent = "Generiere …";
    try {
      if (entity !== state.settings.remote_entity) {
        state.settings = await api("api/settings", { method: "PUT", body: { remote_entity: entity } });
      }
      out.textContent = await api(`api/export/yaml?remote_entity=${encodeURIComponent(entity)}`);
    } catch (err) {
      out.textContent = `# Fehler: ${err.message}`;
    }
  }

  async function copyYaml() {
    const label = $("#yaml-copy span");
    const ok = await copyText($("#yaml-output").textContent);
    if (ok) {
      label.textContent = "Kopiert!";
      setTimeout(() => { label.textContent = "In Zwischenablage kopieren"; }, 1600);
    } else {
      toast("Kopieren nicht möglich – bitte Text manuell markieren.", "error");
    }
  }

  // ------------------------------------------------------------------ SmartIR database

  function isOpen(id) {
    return !$(id).classList.contains("hidden");
  }

  function showDeviceTab(tab) {
    $$("#modal-device [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
    $$("#modal-device .tab-pane").forEach((pane) => pane.classList.toggle("hidden", pane.dataset.pane !== tab));
    if (tab === "db") {
      loadCatalog();
      setTimeout(() => $("#db-search").focus(), 50);
    } else {
      setTimeout(() => $("#device-name").focus(), 50);
    }
  }

  async function loadCatalog() {
    const type = state.db.type;
    $$("#db-type [data-type]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.type === type)));
    selectDbEntry(null);
    if (!state.db.cache[type]) {
      $("#db-results").innerHTML = '<li class="db-empty">Lade SmartIR-Datenbank …</li>';
      try {
        state.db.cache[type] = (await api(`api/smartir/catalog?type=${type}`)).entries;
      } catch (err) {
        $("#db-results").innerHTML = "";
        const li = document.createElement("li");
        li.className = "db-empty";
        li.textContent = err.message;
        $("#db-results").appendChild(li);
        return;
      }
    }
    renderCatalog();
  }

  function renderCatalog() {
    const entries = state.db.cache[state.db.type];
    if (!entries) return;
    const terms = $("#db-search").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const matches = entries.filter((e) => {
      const hay = `${e.manufacturer} ${e.models.join(" ")} ${e.code}`.toLowerCase();
      return terms.every((t) => hay.includes(t));
    });
    const list = $("#db-results");
    list.replaceChildren();
    if (!matches.length) {
      list.innerHTML = '<li class="db-empty">Nichts gefunden – dann einfach selbst anlernen.</li>';
      return;
    }
    for (const entry of matches.slice(0, 150)) {
      const li = document.createElement("li");
      li.className = "db-row";
      li.setAttribute("role", "option");
      li.tabIndex = 0;
      li.dataset.code = entry.code;
      li.innerHTML = '<span class="db-maker"></span><span class="db-models"></span><span class="db-code"></span>';
      $(".db-maker", li).textContent = entry.manufacturer;
      $(".db-models", li).textContent = entry.models.join(", ");
      $(".db-code", li).textContent = `#${entry.code}`;
      const choose = () => selectDbEntry(entry);
      li.addEventListener("click", choose);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(); } });
      if (state.db.selected && state.db.selected.code === entry.code) li.setAttribute("aria-selected", "true");
      list.appendChild(li);
    }
  }

  function selectDbEntry(entry) {
    state.db.selected = entry;
    $("#db-selected").classList.toggle("hidden", !entry);
    $("#db-import").disabled = !entry;
    $$("#db-results .db-row").forEach((r) => r.setAttribute("aria-selected", String(!!entry && Number(r.dataset.code) === entry.code)));
    if (entry) {
      const kind = state.db.type === "light" ? "Licht" : "TV";
      $("#db-name").value = `${entry.manufacturer} ${kind}`.slice(0, 60);
    }
  }

  async function testDbCode(button) {
    const entry = state.db.selected;
    if (!entry) return;
    button.disabled = true;
    try {
      await api("api/smartir/test", { method: "POST", body: { type: state.db.type, code: entry.code, slot: button.dataset.test } });
      button.classList.add("is-sent");
      setTimeout(() => button.classList.remove("is-sent"), 900);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function importFromDb(event) {
    event.preventDefault();
    const entry = state.db.selected;
    const name = $("#db-name").value.trim();
    if (!entry || !name) return;
    const button = $("#db-import");
    button.disabled = true;
    try {
      const device = await api("api/smartir/import", {
        method: "POST",
        body: { type: state.db.type, code: entry.code, name, icon: state.db.type === "light" ? "light" : "tv" },
      });
      closeModal("#modal-device");
      toast(`„${device.name}“ mit ${device.commands.length} Tasten übernommen`, "success");
      await loadDevices();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      button.disabled = !state.db.selected;
    }
  }

  // ------------------------------------------------------------------ SmartIR device

  async function openSmartir(deviceId) {
    const device = deviceById(deviceId);
    if (!device) return;
    state.smartir.deviceId = deviceId;
    state.smartir.view = null;
    $("#smartir-device-name").textContent = device.name;
    $("#smartir-slots").innerHTML = '<li class="text-sm text-ha-muted">Lade …</li>';
    openModal("#modal-smartir");
    try {
      if (!state.types) state.types = await api("api/smartir/types");
      await loadSmartir();
      if (!device.smartir) await saveSmartir(); // keep the automatic suggestion
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function loadSmartir(type) {
    const id = state.smartir.deviceId;
    const query = type ? `?type=${type}` : "";
    const view = await api(`api/devices/${encodeURIComponent(id)}/smartir${query}`);
    state.smartir.view = view;
    renderSmartir();
  }

  async function saveSmartir(patch = {}) {
    const view = state.smartir.view;
    if (!view) return;
    const config = { ...view.config, ...patch };
    try {
      const updated = await api(`api/devices/${encodeURIComponent(state.smartir.deviceId)}/smartir`, {
        method: "PUT",
        body: { type: config.type, code: Number(config.code) || null, slots: config.slots, sources: config.sources, brightness_steps: Number(config.brightness_steps) || null },
      });
      state.smartir.view = updated;
      renderSmartir();
      const device = deviceById(state.smartir.deviceId);
      if (device) { device.smartir = updated.config; render(); }
    } catch (err) {
      toast(err.message, "error");
      renderSmartir();
    }
  }

  function commandOptions(select, device, selectedId) {
    select.replaceChildren(new Option("– nicht belegt –", ""));
    for (const command of device.commands || []) {
      select.appendChild(new Option(command.name, command.id, false, command.id === selectedId));
    }
  }

  function slotRow(slot, device, config) {
    const li = document.createElement("li");
    li.className = "slot-row";
    const assigned = config.slots[slot.key];
    li.dataset.state = assigned ? "ok" : "empty";
    li.innerHTML = '<span class="slot-check" aria-hidden="true"></span><span class="slot-label"></span><select class="input select"></select><button type="button" class="btn btn-ghost btn-xs">Anlernen</button>';
    $(".slot-label", li).textContent = slot.label;
    const select = $("select", li);
    select.setAttribute("aria-label", slot.label);
    commandOptions(select, device, assigned);
    select.addEventListener("change", () => {
      const slots = { ...config.slots };
      if (select.value) slots[slot.key] = select.value; else delete slots[slot.key];
      saveSmartir({ slots });
    });
    $("button", li).addEventListener("click", () => openLearnModal(device.id, { name: slot.learnName || slot.label, label: slot.label, slot: slot.key }));
    return li;
  }

  function renderSmartir() {
    const view = state.smartir.view;
    const device = deviceById(state.smartir.deviceId);
    if (!view || !device || !state.types) return;
    const config = view.config;
    const type = state.types[config.type];

    const typeBox = $("#smartir-type");
    typeBox.replaceChildren();
    for (const [key, t] of Object.entries(state.types)) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "segment";
      b.setAttribute("aria-pressed", String(key === config.type));
      b.innerHTML = `<span></span><small></small>`;
      b.firstChild.textContent = t.label;
      b.lastChild.textContent = t.hint;
      b.addEventListener("click", async () => {
        if (key === config.type) return;
        try { await loadSmartir(key); await saveSmartir(); } catch (err) { toast(err.message, "error"); }
      });
      typeBox.appendChild(b);
    }
    if (document.activeElement !== $("#smartir-code")) $("#smartir-code").value = config.code;

    const slots = $("#smartir-slots");
    slots.replaceChildren(...type.slots.map((slot) => slotRow(slot, device, config)));

    $("#smartir-sources-wrap").classList.toggle("hidden", !type.has_sources);
    $("#smartir-light-wrap").classList.toggle("hidden", config.type !== "light");
    if (type.has_sources) {
      const box = $("#smartir-sources");
      box.replaceChildren();
      const slotIds = new Set(Object.values(config.slots));
      for (const command of device.commands || []) {
        if (slotIds.has(command.id) && !config.sources.includes(command.id)) continue;
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip chip-toggle";
        chip.textContent = command.name;
        chip.setAttribute("aria-pressed", String(config.sources.includes(command.id)));
        chip.addEventListener("click", () => {
          const sources = config.sources.includes(command.id)
            ? config.sources.filter((s) => s !== command.id)
            : [...config.sources, command.id];
          saveSmartir({ sources });
        });
        box.appendChild(chip);
      }
      const add = document.createElement("button");
      add.type = "button";
      add.className = "chip chip-add";
      add.textContent = "+ Quelle anlernen";
      add.addEventListener("click", () => openLearnModal(device.id, { name: "HDMI 1", label: "Neue Quelle", source: true }));
      box.appendChild(add);
      $("#smartir-digits").replaceChildren(...type.digits.map((slot) => slotRow({ ...slot, learnName: slot.label.replace("Ziffer ", "") }, device, config)));
    }
    if (document.activeElement !== $("#smartir-steps")) $("#smartir-steps").value = config.brightness_steps;

    // Status
    const status = $("#smartir-status");
    const install = view.install;
    let tone = "info";
    let html = "";
    if (view.missing.length) {
      tone = "warn";
      html = `<strong>Es fehlt noch:</strong> ${escapeHtml(view.missing.join(", "))}`;
    } else if (!install.ha_config) {
      tone = "warn";
      html = "Kein Zugriff auf den HA-Konfigurationsordner. Lade die JSON-Datei herunter und lege sie selbst ab.";
    } else if (!install.smartir) {
      tone = "warn";
      html = 'SmartIR ist in Home Assistant noch nicht installiert. Installiere es über <strong>HACS</strong> (Suche „SmartIR“) und starte HA neu.';
    } else if (install.installed && config.installed) {
      tone = "ok";
      html = `<strong>Installiert</strong> als Code ${config.code}. Änderungen werden automatisch übernommen – nach dem ersten Mal YAML einfügen und HA neu starten.`;
    } else {
      html = "Bereit. „In Home Assistant installieren“ schreibt die Code-Datei direkt in deinen SmartIR-Ordner.";
    }
    status.dataset.tone = tone;
    status.innerHTML = html;
    $("#smartir-install").disabled = Boolean(view.missing.length) || !install.smartir;
    $("#smartir-install").textContent = install.installed && config.installed ? "Erneut installieren" : "In Home Assistant installieren";
    $("#smartir-yaml").textContent = view.yaml;
    if (config.installed) $("#smartir-yaml-wrap").open = true;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function bindSmartirEvents() {
    let codeTimer = null;
    $("#smartir-code").addEventListener("input", () => {
      clearTimeout(codeTimer);
      codeTimer = setTimeout(() => {
        const value = Number($("#smartir-code").value);
        if (value > 0) saveSmartir({ code: value });
      }, 600);
    });
    $("#smartir-steps").addEventListener("change", () => saveSmartir({ brightness_steps: Number($("#smartir-steps").value) }));
    $("#smartir-install").addEventListener("click", async () => {
      const button = $("#smartir-install");
      button.disabled = true;
      try {
        const view = await api(`api/devices/${encodeURIComponent(state.smartir.deviceId)}/smartir/install`, { method: "POST" });
        state.smartir.view = view;
        const device = deviceById(state.smartir.deviceId);
        if (device) { device.smartir = view.config; render(); }
        renderSmartir();
        toast("Code-Datei in SmartIR installiert", "success");
      } catch (err) {
        toast(err.message, "error");
        renderSmartir();
      }
    });
    $("#smartir-download").addEventListener("click", () => {
      const view = state.smartir.view;
      if (!view) return;
      const blob = new Blob([JSON.stringify(view.json, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `${view.config.code}.json`;
      document.body.appendChild(link);
      link.click();
      setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 1000);
    });
    $("#smartir-yaml-copy").addEventListener("click", async () => {
      const label = $("#smartir-yaml-copy span");
      if (await copyText($("#smartir-yaml").textContent)) {
        label.textContent = "Kopiert!";
        setTimeout(() => { label.textContent = "YAML kopieren"; }, 1600);
      } else {
        toast("Kopieren nicht möglich – bitte Text manuell markieren.", "error");
      }
    });
  }

  // ------------------------------------------------------------------ events

  function bindEvents() {
    const openNewDevice = () => {
      $("#form-device").reset();
      buildIconPicker();
      showDeviceTab("manual");
      openModal("#modal-device");
    };
    $$("#modal-device [data-tab]").forEach((tab) => tab.addEventListener("click", () => showDeviceTab(tab.dataset.tab)));
    $$("#db-type [data-type]").forEach((b) => b.addEventListener("click", () => { state.db.type = b.dataset.type; loadCatalog(); }));
    $("#db-search").addEventListener("input", renderCatalog);
    $("#form-db").addEventListener("submit", importFromDb);
    $$("#db-selected [data-test]").forEach((b) => b.addEventListener("click", () => testDbCode(b)));
    bindSmartirEvents();
    $("#btn-new-device").addEventListener("click", openNewDevice);
    $$("[data-action=new-device]").forEach((b) => b.addEventListener("click", openNewDevice));
    $("#form-device").addEventListener("submit", createDevice);
    $("#status-pill").addEventListener("click", refreshStatus);

    $("#btn-export").addEventListener("click", () => {
      $("#remote-entity").value = state.settings.remote_entity;
      openModal("#modal-yaml");
      loadYaml();
    });
    $("#yaml-refresh").addEventListener("click", loadYaml);
    $("#remote-entity").addEventListener("keydown", (e) => { if (e.key === "Enter") loadYaml(); });
    $("#yaml-copy").addEventListener("click", copyYaml);

    $("#learn-cancel").addEventListener("click", () => closeModal("#modal-learn"));
    $("#learn-retry").addEventListener("click", startLearning);
    $("#learn-again").addEventListener("click", startLearning);
    $("#learn-test").addEventListener("click", testLearned);
    $("#learn-success").addEventListener("submit", saveLearned);

    $("#confirm-ok").addEventListener("click", async () => {
      const action = state.confirmAction;
      state.confirmAction = null;
      closeModal("#modal-confirm");
      if (action) await action();
    });

    // Close buttons / backdrops
    $$(".modal").forEach((modal) => {
      $$("[data-close]", modal).forEach((el) => el.addEventListener("click", () => closeModal(`#${modal.id}`)));
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const open = $$(".modal").filter((m) => !m.classList.contains("hidden"));
      if (open.length) closeModal(`#${open[open.length - 1].id}`);
    });

    // Card actions (event delegation)
    $("#device-grid").addEventListener("click", (e) => {
      const button = e.target.closest("[data-action]");
      if (!button) return;
      const card = button.closest("[data-device-id]");
      const deviceId = card && card.dataset.deviceId;
      const device = deviceById(deviceId);
      const row = button.closest("[data-command-id]");
      const commandId = row && row.dataset.commandId;

      switch (button.dataset.action) {
        case "learn":
          openLearnModal(deviceId);
          break;
        case "smartir":
          openSmartir(deviceId);
          break;
        case "toggle-more":
          if (state.expanded.has(deviceId)) state.expanded.delete(deviceId); else state.expanded.add(deviceId);
          render();
          break;
        case "send":
          sendCommand(button, deviceId, commandId);
          break;
        case "delete-device":
          confirmDelete("Gerät löschen?", `„${device.name}“ und alle ${(device.commands || []).length} Tasten werden gelöscht.`, async () => {
            try {
              await api(`api/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" });
              toast(`„${device.name}“ gelöscht`);
              await loadDevices();
            } catch (err) { toast(err.message, "error"); }
          });
          break;
        case "delete-command": {
          const command = (device.commands || []).find((c) => c.id === commandId);
          confirmDelete("Taste löschen?", `„${command.name}“ von „${device.name}“ wird gelöscht.`, async () => {
            try {
              await api(`api/devices/${encodeURIComponent(deviceId)}/commands/${encodeURIComponent(commandId)}`, { method: "DELETE" });
              toast(`„${command.name}“ gelöscht`);
              await loadDevices();
            } catch (err) { toast(err.message, "error"); }
          });
          break;
        }
        default:
          break;
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    loadDevices();
    refreshStatus();
  });
})();
