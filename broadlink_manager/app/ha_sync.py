"""Keep Home Assistant in sync with the learned devices.

Created through the HA APIs (no YAML, no restart):
- one script per button `script.ir_<device>_<button>` (sends the code and
  updates the remembered state),
- one script per scene `script.ir_szene_<scene>`,
- helpers for the remembered state: `input_boolean` (device/output on) and
  `input_select` (active input of an output),
- the dashboard "Fernbedienungen" with a scene view and one remote per device.

Inside the app container the Supervisor proxy is used (homeassistant_api: true).
For local development HA_URL + HA_TOKEN can point to any HA instance.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import websocket  # websocket-client

import colors
import layout

log = logging.getLogger("broadlink_manager.ha")

DASHBOARD_URL = "ir-fernbedienungen"
DASHBOARD_TITLE = "Fernbedienungen"
SCRIPT_PREFIX = "ir_"
SCENE_PREFIX = "ir_szene_"
DEBOUNCE = 1.5

# Kept for other modules that imported them from here
button_role = layout.button_role
button_icon = layout.button_icon


def script_id(device_id: str, command_id: str) -> str:
    raw = f"{SCRIPT_PREFIX}{device_id}_{command_id}"
    return re.sub(r"[^a-z0-9_]", "_", raw.lower())[:200]


def scene_script_id(scene_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", f"{SCENE_PREFIX}{scene_id}".lower())[:200]


def view_path(device: Dict[str, Any]) -> str:
    return re.sub(r"[^a-z0-9-]", "-", device["id"].lower())


# --------------------------------------------------------------------------
# Helpers for the remembered state
# --------------------------------------------------------------------------


def desired_helpers(devices: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """key → {domain, name, icon, options?}. Keys: power|dev, gpower|dev|group, select|dev|group."""
    helpers: Dict[str, Dict[str, Any]] = {}
    for device in devices:
        model = layout.state_model(device)
        icon = layout.DEVICE_ICONS.get(device.get("icon", ""), "mdi:remote")
        if model["power"]:
            helpers[f"power|{device['id']}"] = {"domain": "input_boolean", "name": f"IR {device['name']} an", "icon": icon}
        for group, entry in model["groups"].items():
            if entry["power"]:
                helpers[f"gpower|{device['id']}|{group}"] = {
                    "domain": "input_boolean", "name": f"IR {device['name']} {group} an", "icon": "mdi:power"}
            if entry["select"]:
                helpers[f"select|{device['id']}|{group}"] = {
                    "domain": "input_select", "name": f"IR {device['name']} {group}", "icon": "mdi:import",
                    "options": entry["options"]}
    return helpers


def _effect_actions(device_id: str, effects: List[Dict[str, Any]], helper_entities: Dict[str, str]) -> List[Dict[str, Any]]:
    actions = []
    for effect in effects:
        if effect["kind"] == "power":
            key = f"power|{device_id}" if effect["group"] is None else f"gpower|{device_id}|{effect['group']}"
            entity = helper_entities.get(key)
            if entity:
                service = {"toggle": "toggle", True: "turn_on", False: "turn_off"}[effect["value"]]
                actions.append({"action": f"input_boolean.{service}", "target": {"entity_id": entity}})
        elif effect["kind"] == "select":
            entity = helper_entities.get(f"select|{device_id}|{effect['group']}")
            if entity:
                actions.append({"action": "input_select.select_option", "target": {"entity_id": entity},
                                "data": {"option": effect["value"]}})
    return actions


# --------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------


def build_scripts(devices: List[Dict[str, Any]], remote_entity: str, scenes: List[Dict[str, Any]],
                  helper_entities: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    scripts: Dict[str, Dict[str, Any]] = {}
    known = {}
    for device in devices:
        for command in device.get("commands", []):
            code = command["code"]
            payload: Any = [f"b64:{c}" for c in code] if isinstance(code, list) else f"b64:{code}"
            _, label = layout.split_group(command["name"])
            role = layout.button_role(label)
            sid = script_id(device["id"], command["id"])
            known[(device["id"], command["id"])] = sid
            scripts[sid] = {
                "alias": f"{device['name']} – {command['name']}",
                "description": "Automatisch erstellt vom Broadlink IR Manager – Änderungen bitte in der App vornehmen.",
                "icon": layout.button_icon(label, role),
                "mode": "queued",
                "max": 10,
                "sequence": [
                    {"action": "remote.send_command", "target": {"entity_id": remote_entity}, "data": {"command": payload}},
                    *_effect_actions(device["id"], layout.command_effects(device, command), helper_entities),
                ],
            }
    for scene in scenes:
        sequence: List[Dict[str, Any]] = []
        for step in scene.get("steps", []):
            sid = known.get((step["device_id"], step["command_id"]))
            if not sid:
                continue
            call = {"action": f"script.{sid}"}
            if step.get("repeat", 1) > 1:
                sequence.append({"repeat": {"count": step["repeat"], "sequence": [call, {"delay": {"milliseconds": 350}}]}})
            else:
                sequence.append(call)
            if step.get("delay", 0) > 0:
                sequence.append({"delay": {"milliseconds": int(step["delay"] * 1000)}})
        if sequence:
            scripts[scene_script_id(scene["id"])] = {
                "alias": f"Szene: {scene['name']}",
                "description": "Szene aus dem Broadlink IR Manager.",
                "icon": "mdi:movie-open-play",
                "mode": "single",
                "sequence": sequence,
            }
    return scripts


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


def _button(entity: str, name: str, icon: str) -> Dict[str, Any]:
    return {
        "type": "button",
        "entity": entity,
        "name": name,
        "icon": icon,
        "show_state": False,
        "show_name": True,
        "icon_height": "32px",
        "tap_action": {"action": "toggle"},
        "hold_action": {"action": "none"},
    }


_BLANK = {
    "type": "button",
    "show_name": False,
    "show_icon": False,
    "show_state": False,
    "tap_action": {"action": "none"},
    "hold_action": {"action": "none"},
}
_FULL = {"columns": "full"}


def _grid(cards: List[Dict[str, Any]], columns: int) -> Dict[str, Any]:
    return {"type": "grid", "columns": max(1, columns), "square": False, "cards": cards, "grid_options": _FULL}


def _heading(text: str, style: str = "subtitle", icon: Optional[str] = None) -> Dict[str, Any]:
    card = {"type": "heading", "heading": text, "heading_style": style, "grid_options": _FULL}
    if icon:
        card["icon"] = icon
    return card


def _status_tile(entity: str, name: str, icon: str) -> Dict[str, Any]:
    return {"type": "tile", "entity": entity, "name": name, "icon": icon, "tap_action": {"action": "more-info"},
            "grid_options": _FULL}


def _color_palette_card(items: List[Tuple[str, str, str]], columns: int = 4) -> Dict[str, Any]:
    """Coloured circles (always in colour, unlike tiles) as one picture-elements card."""
    rows = (len(items) + columns - 1) // columns
    svg = f"<svg xmlns='http://www.w3.org/2000/svg' width='400' height='{rows * 100}'/>"
    image = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
    elements = []
    for i, (entity, name, color_key) in enumerate(items):
        row, col = divmod(i, columns)
        elements.append({
            "type": "icon",
            "icon": "mdi:circle",
            "title": name,
            "tap_action": {"action": "perform-action", "perform_action": "script.turn_on", "target": {"entity_id": entity}},
            "hold_action": {"action": "more-info", "entity": entity},
            "style": {
                "top": f"{(row + 0.5) * 100 / rows:.2f}%",
                "left": f"{(col + 0.5) * 100 / columns:.2f}%",
                "color": colors.PALETTE[color_key]["hex"],
                "--mdc-icon-size": "56px",
                "filter": "drop-shadow(0 0 6px rgba(0,0,0,.5))",
            },
        })
    return {"type": "picture-elements", "image": image, "elements": elements, "grid_options": _FULL}


def build_remote_view(device: Dict[str, Any], helper_entities: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    helper_entities = helper_entities or {}
    spec = layout.device_layout(device)

    def card(item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not item:
            return _BLANK
        return _button(f"script.{script_id(device['id'], item['command_id'])}", item["label"], item["icon"])

    cards: List[Dict[str, Any]] = [_heading(device["name"], "title", spec["icon"])]
    power_entity = helper_entities.get(f"power|{device['id']}")
    if power_entity:
        cards.append(_status_tile(power_entity, "Status", spec["icon"]))

    for section in spec["sections"]:
        kind = section["kind"]
        if kind == "palette":
            cards.append(_heading(section["title"]))
            cards.append(_color_palette_card([
                (f"script.{script_id(device['id'], i['command_id'])}", i["label"], i["color"]) for i in section["items"]
            ]))
            continue
        if kind == "group":
            cards.append(_heading(section["title"]))
            tiles = []
            select_entity = helper_entities.get(f"select|{device['id']}|{section['group']}")
            if select_entity:
                tiles.append(_status_tile(select_entity, "Aktiv", "mdi:import"))
            gpower_entity = helper_entities.get(f"gpower|{device['id']}|{section['group']}")
            if gpower_entity:
                tiles.append(_status_tile(gpower_entity, "Ausgang", "mdi:power"))
            if tiles:
                cards.append(_grid(tiles, len(tiles)))
            if section["switch"]:
                cards.append(_grid([card(i) for i in section["switch"]], len(section["switch"])))
            if section["items"]:
                cards.append(_grid([card(i) for i in section["items"]], section["columns"]))
            continue
        if section.get("title"):
            cards.append(_heading(section["title"]))
        cards.append(_grid([card(i) for i in section["items"]], section["columns"]))

    return {
        "title": device["name"],
        "path": view_path(device),
        "icon": spec["icon"],
        "type": "sections",
        "max_columns": 1,
        "sections": [{"type": "grid", "cards": cards}],
    }


def build_dashboard(devices: List[Dict[str, Any]], scenes: Optional[List[Dict[str, Any]]] = None,
                    helper_entities: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    views = []
    usable_scenes = [s for s in (scenes or []) if s.get("steps")]
    if usable_scenes:
        views.append({
            "title": "Szenen",
            "path": "szenen",
            "icon": "mdi:movie-open-play",
            "type": "sections",
            "max_columns": 1,
            "sections": [{"type": "grid", "cards": [
                _heading("Szenen", "title", "mdi:movie-open-play"),
                _grid([
                    {**_button(f"script.{scene_script_id(s['id'])}", s["name"], "mdi:play-circle"), "icon_height": "40px"}
                    for s in usable_scenes
                ], 2),
            ]}],
        })
    views += [build_remote_view(d, helper_entities) for d in devices if d.get("commands")]
    if not views:
        views = [{
            "title": "Fernbedienungen",
            "path": "start",
            "cards": [{"type": "markdown", "content": "Noch keine Geräte mit Tasten. Lege im **Broadlink IR Manager** ein Gerät an."}],
        }]
    return {"title": DASHBOARD_TITLE, "views": views}


# --------------------------------------------------------------------------
# HA API client
# --------------------------------------------------------------------------


class HAError(Exception):
    pass


class HAClient:
    def __init__(self) -> None:
        token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN") or ""
        base = os.environ.get("HA_URL") or ("http://supervisor/core" if os.environ.get("SUPERVISOR_TOKEN") else "")
        self.token = token
        self.base = base.rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self.token and self.base)

    def rest(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base}/api/{path.lstrip('/')}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 (local HA)
                raw = response.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:300]
            raise HAError(f"HA-API {method} {path}: {err.code} {detail}") from err
        except (urllib.error.URLError, OSError) as err:
            raise HAError(f"Home Assistant nicht erreichbar: {err}") from err
        return json.loads(raw) if raw else None

    def ws(self, messages: List[Dict[str, Any]]) -> List[Any]:
        # Supervisor proxy: ws://supervisor/core/websocket – direct HA: ws://host:8123/api/websocket
        ws_base = self.base.replace("https://", "wss://").replace("http://", "ws://")
        url = ws_base + ("/websocket" if ws_base.endswith("/core") else "/api/websocket")
        try:
            conn = websocket.create_connection(url, timeout=20)
        except (OSError, websocket.WebSocketException) as err:
            raise HAError(f"HA-WebSocket nicht erreichbar: {err}") from err
        try:
            hello = json.loads(conn.recv())
            if hello.get("type") == "auth_required":
                conn.send(json.dumps({"type": "auth", "access_token": self.token}))
                auth = json.loads(conn.recv())
                if auth.get("type") != "auth_ok":
                    raise HAError("HA-WebSocket: Anmeldung fehlgeschlagen")
            results = []
            for i, message in enumerate(messages, start=1):
                conn.send(json.dumps({**message, "id": i}))
                while True:
                    reply = json.loads(conn.recv())
                    if reply.get("id") == i and reply.get("type") == "result":
                        break
                if not reply.get("success"):
                    error = reply.get("error") or {}
                    results.append(HAError(f"{message['type']}: {error.get('message', error)}"))
                else:
                    results.append(reply.get("result"))
            return results
        except (OSError, websocket.WebSocketException, ValueError) as err:
            raise HAError(f"HA-WebSocket Fehler: {err}") from err
        finally:
            conn.close()


# --------------------------------------------------------------------------
# Background synchroniser
# --------------------------------------------------------------------------


def _hash(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class HASync:
    def __init__(self, load_store: Callable[[], Dict[str, Any]], save_settings: Callable[[Dict[str, Any]], None],
                 state_file: Path) -> None:
        self._load_store = load_store
        self._save_settings = save_settings
        self._state_file = state_file
        self._client = HAClient()
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._read_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
        self.status: Dict[str, Any] = {
            "available": self._client.available,
            "running": False,
            "last_sync": None,
            "error": None,
            "scripts": 0,
            "helpers": 0,
            "remote_entity": None,
            "remote_ok": None,
            "broadlink_remotes": [],
            "dashboard_url": f"/{DASHBOARD_URL}",
        }
        if self._client.available:
            log.info("Home-Assistant-API verfügbar (%s) – Geräte werden automatisch angelegt.", self._client.base)
            threading.Thread(target=self._loop, name="ha-sync", daemon=True).start()
        else:
            log.warning("Keine Home-Assistant-API (SUPERVISOR_TOKEN fehlt) – automatisches Anlegen ist aus.")

    # -- state file -------------------------------------------------------

    def _read_state(self) -> Dict[str, Any]:
        with self._file_lock:
            try:
                state = json.loads(self._state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
        state.setdefault("scripts", {})
        state.setdefault("helpers", {})
        state.setdefault("dashboard", None)
        return state

    def _write_state(self, state: Dict[str, Any]) -> None:
        with self._file_lock:
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_file)

    def _helper_entities(self) -> Dict[str, str]:
        return {k: v["entity_id"] for k, v in self._read_state()["helpers"].items()}

    # -- public -----------------------------------------------------------

    def request(self) -> None:
        if self._client.available:
            self._event.set()

    def sync_now(self) -> Dict[str, Any]:
        if not self._client.available:
            raise HAError("Kein Zugriff auf die Home-Assistant-API.")
        with self._lock:
            self._sync()
        return self.status

    def push_effects(self, device_id: str, effects: List[Dict[str, Any]]) -> None:
        """Mirror a button press from the app into the HA helpers."""
        if not self._client.available or not effects:
            return
        for action in _effect_actions(device_id, effects, self._helper_entities()):
            domain, service = action["action"].split(".", 1)
            try:
                self._client.rest("POST", f"services/{domain}/{service}", {**action["target"], **action.get("data", {})})
            except HAError as err:
                log.debug("Status nicht an HA übertragen: %s", err)
        self._read_cache = (0.0, {})

    def read_states(self, devices: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Current helper values: {device_id: {'power': bool, 'value:<group>': str, 'power:<group>': bool}}."""
        if not self._client.available:
            return {}
        stamp, cached = self._read_cache
        if time.time() - stamp < 2:
            return cached
        result: Dict[str, Dict[str, Any]] = {}
        for key, entity in self._helper_entities().items():
            try:
                value = self._client.rest("GET", f"states/{entity}")["state"]
            except (HAError, KeyError, TypeError):
                continue
            if value in ("unknown", "unavailable"):
                continue
            parts = key.split("|")
            entry = result.setdefault(parts[1], {})
            if parts[0] == "power":
                entry["power"] = value == "on"
            elif parts[0] == "gpower":
                entry[f"power:{parts[2]}"] = value == "on"
            elif parts[0] == "select":
                entry[f"value:{parts[2]}"] = value
        self._read_cache = (time.time(), result)
        return result

    # -- internals --------------------------------------------------------

    def _loop(self) -> None:
        time.sleep(3)
        self._event.set()  # initial sync after start
        while True:
            self._event.wait()
            time.sleep(DEBOUNCE)
            self._event.clear()
            with self._lock:
                try:
                    self._sync()
                except Exception as err:  # noqa: BLE001 – keep the worker alive
                    log.exception("HA-Synchronisierung fehlgeschlagen")
                    self.status["error"] = str(err)
                    self.status["running"] = False

    def _detect_remotes(self) -> List[str]:
        result = self._client.ws([{"type": "config/entity_registry/list"}])[0]
        if isinstance(result, Exception):
            raise result
        return sorted(
            e["entity_id"] for e in result
            if e.get("platform") == "broadlink" and e["entity_id"].startswith("remote.") and not e.get("disabled_by")
        )

    def _sync_helpers(self, state: Dict[str, Any], devices: List[Dict[str, Any]]) -> int:
        """Create/update/delete the input_boolean/input_select helpers. Returns number of changes."""
        known: Dict[str, Dict[str, Any]] = state["helpers"]
        desired = desired_helpers(devices)
        calls: List[Tuple[str, Optional[Dict[str, Any]], Dict[str, Any]]] = []  # (key, spec, message)
        for key, info in list(known.items()):
            if key not in desired or desired[key]["domain"] != info["domain"]:
                domain = info["domain"]
                calls.append((key, None, {"type": f"{domain}/delete", f"{domain}_id": info["id"]}))
        for key, spec in desired.items():
            digest = _hash(spec)
            info = known.get(key)
            if info and info["domain"] == spec["domain"]:
                if info.get("hash") != digest:
                    message = {"type": f"{spec['domain']}/update", f"{spec['domain']}_id": info["id"],
                               "name": spec["name"], "icon": spec["icon"]}
                    if "options" in spec:
                        message["options"] = spec["options"]
                    calls.append((key, spec, message))
                continue
            message = {"type": f"{spec['domain']}/create", "name": spec["name"], "icon": spec["icon"]}
            if "options" in spec:
                message["options"] = spec["options"]
            calls.append((key, spec, message))
        if not calls:
            return 0
        results = self._client.ws([c[2] for c in calls])
        errors = []
        for (key, spec, message), result in zip(calls, results):
            if isinstance(result, Exception):
                if message["type"].endswith("/delete"):
                    known.pop(key, None)  # already gone
                else:
                    errors.append(str(result))
                continue
            if spec is None:
                known.pop(key, None)
            else:
                helper_id = result.get("id") if isinstance(result, dict) else known.get(key, {}).get("id")
                known[key] = {"domain": spec["domain"], "id": helper_id,
                              "entity_id": f"{spec['domain']}.{helper_id}", "hash": _hash(spec)}
        self._write_state(state)
        if errors:
            raise HAError("Status-Helfer: " + "; ".join(errors[:3]))
        return len(calls)

    def _sync(self) -> None:
        self.status["running"] = True
        self.status["error"] = None
        data = self._load_store()
        settings = data["settings"]
        if not settings.get("ha_auto", True):
            self.status["running"] = False
            return

        problems: List[str] = []
        try:
            remotes = self._detect_remotes()
        except HAError as err:
            remotes = []
            problems.append(f"Broadlink-Erkennung: {err}")
        self.status["broadlink_remotes"] = remotes
        remote = settings.get("remote_entity") or ""
        if remotes and remote not in remotes:
            remote = remotes[0]
            log.info("Broadlink-Fernbedienung in HA erkannt: %s", remote)
            self._save_settings({"remote_entity": remote})
        self.status["remote_entity"] = remote
        self.status["remote_ok"] = remote in remotes

        state = self._read_state()
        try:
            helper_changes = self._sync_helpers(state, data["devices"])
        except HAError as err:
            helper_changes = 0
            problems.append(str(err))
        helpers = {k: v["entity_id"] for k, v in state["helpers"].items()}

        known: Dict[str, str] = state["scripts"]
        desired = build_scripts(data["devices"], remote, data.get("scenes", []), helpers)
        changed = 0
        for object_id in [k for k in known if k not in desired]:
            try:
                self._client.rest("DELETE", f"config/script/config/{object_id}")
            except HAError as err:
                if "404" not in str(err):
                    raise
            known.pop(object_id, None)
            changed += 1
            self._write_state(state)
        for object_id, config in desired.items():
            digest = _hash(config)
            if known.get(object_id) == digest:
                continue
            self._client.rest("POST", f"config/script/config/{object_id}", config)
            known[object_id] = digest
            changed += 1
            self._write_state(state)

        # Check that HA really loaded the scripts (needs `script: !include scripts.yaml`).
        if desired:
            probe = next(iter(desired))
            try:
                self._client.rest("GET", f"states/script.{probe}")
            except HAError as err:
                if "404" in str(err):
                    problems.append(
                        "Die Skripte wurden in scripts.yaml geschrieben, aber Home Assistant lädt sie nicht. "
                        "In configuration.yaml fehlt vermutlich die Zeile „script: !include scripts.yaml“."
                    )
                else:
                    problems.append(str(err))

        dashboard = build_dashboard(data["devices"], data.get("scenes", []), helpers)
        dash_hash = _hash(dashboard)
        try:
            self._save_dashboard(state, dashboard, dash_hash, changed + helper_changes)
        except HAError as err:
            problems.append(f"Dashboard: {err}")

        self._write_state(state)
        self.status.update(
            running=False,
            last_sync=time.strftime("%Y-%m-%dT%H:%M:%S"),
            scripts=len(known),
            helpers=len(state["helpers"]),
            error=" · ".join(problems) or None,
        )
        self._read_cache = (0.0, {})
        if problems:
            log.warning("Home-Assistant-Abgleich unvollständig: %s", " · ".join(problems))
        if changed or helper_changes:
            log.info("Home Assistant aktualisiert: %d Skript- und %d Helfer-Änderungen (%d Skripte, %d Helfer).",
                     changed, helper_changes, len(known), len(state["helpers"]))

    def _save_dashboard(self, state: Dict[str, Any], dashboard: Dict[str, Any], dash_hash: str, changed: int) -> None:
        if state.get("dashboard") != dash_hash or changed:
            existing = self._client.ws([{"type": "lovelace/dashboards/list"}])[0]
            if isinstance(existing, Exception):
                raise existing
            calls: List[Dict[str, Any]] = []
            if not any(d.get("url_path") == DASHBOARD_URL for d in existing):
                calls.append({
                    "type": "lovelace/dashboards/create",
                    "url_path": DASHBOARD_URL,
                    "title": DASHBOARD_TITLE,
                    "icon": "mdi:remote-tv",
                    "show_in_sidebar": True,
                    "require_admin": False,
                    "mode": "storage",
                })
            calls.append({"type": "lovelace/config/save", "url_path": DASHBOARD_URL, "config": dashboard})
            for result in self._client.ws(calls):
                if isinstance(result, Exception):
                    raise result
            state["dashboard"] = dash_hash
