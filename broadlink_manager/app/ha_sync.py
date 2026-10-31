"""Keep Home Assistant in sync with the learned devices.

For every button a script `script.ir_<device>_<button>` is created through the
HA config API (same as the script editor does). Additionally a dashboard
"Fernbedienungen" is maintained with one remote-control view per device.

Inside the app container the Supervisor proxy is used (homeassistant_api: true).
For local development HA_URL + HA_TOKEN can point to any HA instance.
"""

from __future__ import annotations

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

from smartir import _NAME_HINTS, _SOURCE_PREFIXES, _TOGGLE_HINTS, _norm

log = logging.getLogger("broadlink_manager.ha")

DASHBOARD_URL = "ir-fernbedienungen"
DASHBOARD_TITLE = "Fernbedienungen"
SCRIPT_PREFIX = "ir_"
DEBOUNCE = 1.5

# --------------------------------------------------------------------------
# Button roles (layout + icons)
# --------------------------------------------------------------------------

_EXTRA_HINTS: Dict[str, List[str]] = {
    "up": ["hoch", "up", "oben", "pfeil hoch", "arrow up", "cursor up"],
    "down": ["runter", "down", "unten", "pfeil runter", "arrow down", "cursor down"],
    "left": ["links", "left", "pfeil links", "arrow left", "cursor left"],
    "right": ["rechts", "right", "pfeil rechts", "arrow right", "cursor right"],
    "ok": ["ok", "enter", "select", "auswahl", "bestaetigen", "bestätigen"],
    "menu": ["menue", "menü", "menu", "main menu", "hauptmenü"],
    "settings": ["setup", "settings", "einstellungen", "options", "optionen", "tools"],
    "back": ["zurueck", "zurück", "back", "return"],
    "home": ["home", "start"],
    "exit": ["exit", "beenden", "verlassen"],
    "info": ["info", "guide", "epg", "display"],
    "play": ["play", "wiedergabe", "play/pause", "play pause"],
    "pause": ["pause"],
    "stop": ["stop"],
    "rewind": ["rewind", "zurückspulen", "zurueckspulen", "rew", "<<"],
    "forward": ["forward", "vorspulen", "ff", "fwd", ">>"],
    "next": ["next", "skip", "nächster titel"],
    "prev": ["prev", "previous", "vorheriger titel"],
    "record": ["record", "rec", "aufnahme"],
}

_ICONS = {
    "power": "mdi:power", "on": "mdi:power-on", "off": "mdi:power-off",
    "volumeUp": "mdi:volume-plus", "volumeDown": "mdi:volume-minus", "mute": "mdi:volume-mute",
    "nextChannel": "mdi:chevron-double-up", "previousChannel": "mdi:chevron-double-down",
    "up": "mdi:chevron-up", "down": "mdi:chevron-down", "left": "mdi:chevron-left", "right": "mdi:chevron-right",
    "ok": "mdi:checkbox-blank-circle", "menu": "mdi:menu", "settings": "mdi:cog", "back": "mdi:arrow-u-left-top", "home": "mdi:home",
    "exit": "mdi:exit-to-app", "info": "mdi:information-outline", "source": "mdi:import",
    "play": "mdi:play", "pause": "mdi:pause", "stop": "mdi:stop", "rewind": "mdi:rewind",
    "forward": "mdi:fast-forward", "next": "mdi:skip-next", "prev": "mdi:skip-previous", "record": "mdi:record",
    "brighten": "mdi:brightness-7", "dim": "mdi:brightness-5", "warmer": "mdi:thermometer-plus",
    "colder": "mdi:snowflake", "night": "mdi:weather-night", "digit": "mdi:numeric",
}
_DEVICE_ICONS = {
    "tv": "mdi:television", "soundbar": "mdi:soundbar", "ac": "mdi:air-conditioner", "fan": "mdi:fan",
    "light": "mdi:lightbulb", "led": "mdi:led-strip-variant", "projector": "mdi:projector",
    "receiver": "mdi:audio-video", "remote": "mdi:remote",
}
_DIGIT_ICONS = {str(i): f"mdi:numeric-{i}" for i in range(10)}


def button_role(name: str) -> str:
    norm = _norm(name)
    if re.fullmatch(r"(?:taste |ziffer |zahl |nr\.? ?)?[0-9]", norm):
        return "digit"
    if norm in {_norm(h) for h in _TOGGLE_HINTS}:
        return "power"
    for role, hints in list(_NAME_HINTS.items()) + list(_EXTRA_HINTS.items()):
        if norm in {_norm(h) for h in hints}:
            return role
    if norm.startswith(_SOURCE_PREFIXES):
        return "source"
    return "other"


def button_icon(name: str, role: str) -> str:
    if role == "digit":
        return _DIGIT_ICONS.get(re.sub(r"\D", "", name)[-1:], "mdi:numeric")
    return _ICONS.get(role, "mdi:remote")


def script_id(device_id: str, command_id: str) -> str:
    raw = f"{SCRIPT_PREFIX}{device_id}_{command_id}"
    return re.sub(r"[^a-z0-9_]", "_", raw.lower())[:200]


# --------------------------------------------------------------------------
# Script + dashboard builders
# --------------------------------------------------------------------------


def build_scripts(devices: List[Dict[str, Any]], remote_entity: str) -> Dict[str, Dict[str, Any]]:
    scripts: Dict[str, Dict[str, Any]] = {}
    for device in devices:
        for command in device.get("commands", []):
            code = command["code"]
            payload: Any = [f"b64:{c}" for c in code] if isinstance(code, list) else f"b64:{code}"
            role = button_role(command["name"])
            scripts[script_id(device["id"], command["id"])] = {
                "alias": f"{device['name']} – {command['name']}",
                "description": "Automatisch erstellt vom Broadlink IR Manager – Änderungen bitte in der App vornehmen.",
                "icon": button_icon(command["name"], role),
                "mode": "queued",
                "max": 10,
                "sequence": [
                    {
                        "action": "remote.send_command",
                        "target": {"entity_id": remote_entity},
                        "data": {"command": payload},
                    }
                ],
            }
    return scripts


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


def _grid(cards: List[Dict[str, Any]], columns: int) -> Dict[str, Any]:
    return {"type": "grid", "columns": columns, "square": False, "cards": cards, "grid_options": {"columns": "full"}}


def build_remote_view(device: Dict[str, Any]) -> Dict[str, Any]:
    buttons: Dict[str, List[Dict[str, Any]]] = {}
    for command in device.get("commands", []):
        role = button_role(command["name"])
        card = _button(f"script.{script_id(device['id'], command['id'])}", command["name"], button_icon(command["name"], role))
        buttons.setdefault(role, []).append(card)

    def take(role: str) -> Optional[Dict[str, Any]]:
        items = buttons.get(role)
        return items.pop(0) if items else None

    cards: List[Dict[str, Any]] = [{
        "type": "heading",
        "heading": device["name"],
        "icon": _DEVICE_ICONS.get(device.get("icon", ""), "mdi:remote"),
        "heading_style": "title",
        "grid_options": {"columns": "full"},
    }]

    top = [c for c in (take("power"), take("on"), take("off"), take("source"), take("mute")) if c]
    top += buttons.pop("power", [])
    if top:
        cards.append(_grid(top, min(3, len(top))))

    nav = {r: take(r) for r in ("up", "down", "left", "right", "ok")}
    if any(nav.values()):
        corners = [c for c in (take("menu"), take("back"), take("home"), take("exit"), take("info"), take("settings")) if c]
        corners += [_BLANK] * 4
        cells = [corners[0], nav["up"], corners[1], nav["left"], nav["ok"], nav["right"], corners[2], nav["down"], corners[3]]
        cards.append(_grid([c or _BLANK for c in cells], 3))

    rocker = [take("volumeUp"), take("nextChannel"), take("volumeDown"), take("previousChannel")]
    if any(rocker):
        if not rocker[1] and not rocker[3]:
            cards.append(_grid([c for c in (rocker[0], rocker[2]) if c], 2))
        elif not rocker[0] and not rocker[2]:
            cards.append(_grid([c for c in (rocker[1], rocker[3]) if c], 2))
        else:
            cards.append(_grid([c or _BLANK for c in rocker], 2))

    light = [c for r in ("brighten", "dim", "warmer", "colder", "night") for c in buttons.pop(r, [])]
    if light:
        cards.append(_grid(light, 3 if len(light) != 4 else 2))

    media = [c for r in ("rewind", "prev", "play", "pause", "stop", "next", "forward", "record") for c in buttons.pop(r, [])]
    if media:
        cards.append(_grid(media, 4 if len(media) >= 4 else len(media)))

    digits = buttons.pop("digit", [])
    if digits:
        by_digit = {re.sub(r"\D", "", c["name"])[-1:]: c for c in digits}
        cells = [by_digit.get(str(i)) or _BLANK for i in range(1, 10)] + [_BLANK, by_digit.get("0") or _BLANK, _BLANK]
        cards.append(_grid(cells, 3))

    sources = buttons.pop("source", [])
    if sources:
        cards.append({"type": "heading", "heading": "Quellen", "heading_style": "subtitle", "grid_options": {"columns": "full"}})
        cards.append(_grid(sources, 3))

    rest = [c for items in buttons.values() for c in items]
    if rest:
        cards.append({"type": "heading", "heading": "Weitere Tasten", "heading_style": "subtitle", "grid_options": {"columns": "full"}})
        cards.append(_grid(rest, 3))

    return {
        "title": device["name"],
        "path": re.sub(r"[^a-z0-9-]", "-", device["id"].lower()),
        "icon": _DEVICE_ICONS.get(device.get("icon", ""), "mdi:remote"),
        "type": "sections",
        "max_columns": 1,
        "sections": [{"type": "grid", "cards": cards}],
    }


def build_dashboard(devices: List[Dict[str, Any]]) -> Dict[str, Any]:
    views = [build_remote_view(d) for d in devices if d.get("commands")]
    if not views:
        views = [{
            "title": "Fernbedienungen",
            "path": "start",
            "cards": [{"type": "markdown", "content": "Noch keine Geräte mit Tasten. Lege im **Broadlink IR Manager** ein Gerät an."}],
        }]
    return {"title": DASHBOARD_TITLE, "views": views}


def view_path(device: Dict[str, Any]) -> str:
    return re.sub(r"[^a-z0-9-]", "-", device["id"].lower())


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
        self.status: Dict[str, Any] = {
            "available": self._client.available,
            "running": False,
            "last_sync": None,
            "error": None,
            "scripts": 0,
            "remote_entity": None,
            "remote_ok": None,
            "broadlink_remotes": [],
            "dashboard_url": f"/{DASHBOARD_URL}",
        }
        if self._client.available:
            threading.Thread(target=self._loop, name="ha-sync", daemon=True).start()

    # -- state file -------------------------------------------------------

    def _read_state(self) -> Dict[str, Any]:
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"scripts": {}, "dashboard": None}

    def _write_state(self, state: Dict[str, Any]) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self._state_file)

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

    def _sync(self) -> None:
        self.status["running"] = True
        self.status["error"] = None
        data = self._load_store()
        settings = data["settings"]
        if not settings.get("ha_auto", True):
            self.status["running"] = False
            return

        remotes = self._detect_remotes()
        self.status["broadlink_remotes"] = remotes
        remote = settings.get("remote_entity") or ""
        if remotes and remote not in remotes:
            remote = remotes[0]
            log.info("Broadlink-Fernbedienung in HA erkannt: %s", remote)
            self._save_settings({"remote_entity": remote})
        self.status["remote_entity"] = remote
        self.status["remote_ok"] = remote in remotes

        state = self._read_state()
        known: Dict[str, str] = state.get("scripts", {})
        desired = build_scripts(data["devices"], remote)
        changed = 0
        for object_id in [k for k in known if k not in desired]:
            try:
                self._client.rest("DELETE", f"config/script/config/{object_id}")
            except HAError as err:
                if "404" not in str(err):
                    raise
            known.pop(object_id, None)
            changed += 1
            self._write_state({**state, "scripts": known})
        for object_id, config in desired.items():
            digest = _hash(config)
            if known.get(object_id) == digest:
                continue
            self._client.rest("POST", f"config/script/config/{object_id}", config)
            known[object_id] = digest
            changed += 1
            self._write_state({**state, "scripts": known})

        dashboard = build_dashboard(data["devices"])
        dash_hash = _hash(dashboard)
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

        state["scripts"] = known
        self._write_state(state)
        self.status.update(
            running=False,
            last_sync=time.strftime("%Y-%m-%dT%H:%M:%S"),
            scripts=len(known),
        )
        if changed:
            log.info("Home Assistant aktualisiert: %d Skript-Änderungen, %d Skripte gesamt.", changed, len(known))
