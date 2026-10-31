/* Broadlink IR Manager – frontend (vanilla JS).
 * All URLs are relative so the UI works behind Home Assistant ingress. */
(() => {
  "use strict";

  const LEARN_TIMEOUT = 30;

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
    learn: { deviceId: null, learnId: null, code: null, controller: null, timer: null },
    confirmAction: null,
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
    const focusable = $("input:not([type=hidden]), button.btn-primary", modal);
    if (focusable) setTimeout(() => focusable.focus(), 50);
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

      const list = $(".command-list", card);
      for (const command of commands) {
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
      $("#command-name").value = "";
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

  function openLearnModal(deviceId) {
    const device = deviceById(deviceId);
    if (!device) return;
    state.learn.deviceId = deviceId;
    $("#learn-device-name").textContent = device.name;
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
      await api("api/save-command", { method: "POST", body: { device_id: l.deviceId, name, learn_id: l.learnId } });
      l.learnId = null;
      closeModal("#modal-learn");
      toast(overwrite ? `„${name}“ aktualisiert` : `„${name}“ gespeichert`, "success");
      await loadDevices();
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
      out.textContent = await api(`api/export/yaml?remote_entity=${encodeURIComponent(entity)}`);
      try { localStorage.setItem("blm.remoteEntity", entity); } catch (_) { /* storage blocked */ }
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

  // ------------------------------------------------------------------ events

  function bindEvents() {
    const openNewDevice = () => {
      $("#form-device").reset();
      buildIconPicker();
      openModal("#modal-device");
    };
    $("#btn-new-device").addEventListener("click", openNewDevice);
    $$("[data-action=new-device]").forEach((b) => b.addEventListener("click", openNewDevice));
    $("#form-device").addEventListener("submit", createDevice);
    $("#status-pill").addEventListener("click", refreshStatus);

    $("#btn-export").addEventListener("click", () => {
      try {
        const saved = localStorage.getItem("blm.remoteEntity");
        if (saved) $("#remote-entity").value = saved;
      } catch (_) { /* storage blocked */ }
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
