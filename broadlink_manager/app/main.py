"""Broadlink IR Manager – FastAPI backend.

Learns, stores and sends IR codes with a Broadlink RM4 mini and exports
ready-to-use Home Assistant YAML.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import broadlink
from broadlink import exceptions as bl_errors
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import smartir

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_VERSION = "1.1.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DEVICES_FILE = DATA_DIR / "devices.json"
OPTIONS_FILE = DATA_DIR / "options.json"

# Fallback device type if direct discovery (hello) fails. 0x51DA = RM4 mini.
DEFAULT_DEVTYPE = 0x51DA
LEARN_TIMEOUT_DEFAULT = 30
LEARN_TIMEOUT_MAX = 120
LEARN_POLL_INTERVAL = 1.0
LEARNED_CODE_TTL = 15 * 60  # seconds a learned code stays in temporary storage
MULTI_CODE_DELAY = 0.5  # pause between codes of a multi-code button
DEFAULT_SETTINGS = {"remote_entity": "remote.rm4_mini"}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("broadlink_manager")


def _load_options() -> Dict[str, Any]:
    """Read the add-on options (written by the Supervisor) if present."""
    try:
        return json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _normalize_mac(value: str) -> str:
    """Return a MAC as 12 lowercase hex chars, or '' if invalid/empty."""
    cleaned = re.sub(r"[^0-9a-fA-F]", "", value or "").lower()
    return cleaned if len(cleaned) == 12 else ""


def _format_mac(mac_hex: str) -> str:
    return ":".join(mac_hex[i : i + 2] for i in range(0, 12, 2)).upper() if mac_hex else ""


_options = _load_options()
BROADLINK_IP = (os.environ.get("BROADLINK_IP") or _options.get("broadlink_ip") or "").strip()
BROADLINK_MAC = _normalize_mac(os.environ.get("BROADLINK_MAC") or _options.get("broadlink_mac") or "")
try:
    BROADLINK_DEVTYPE = int(os.environ.get("BROADLINK_DEVTYPE") or str(DEFAULT_DEVTYPE), 0)
except ValueError:
    BROADLINK_DEVTYPE = DEFAULT_DEVTYPE

if not BROADLINK_IP:
    log.warning("Keine broadlink_ip konfiguriert – bitte in den App-Optionen setzen.")
if (os.environ.get("BROADLINK_MAC") or _options.get("broadlink_mac")) and not BROADLINK_MAC:
    log.warning("broadlink_mac ist ungültig – erwartet z. B. AA:BB:CC:DD:EE:FF.")


# --------------------------------------------------------------------------
# Broadlink device handling
# --------------------------------------------------------------------------


class DeviceUnavailable(Exception):
    """Raised when the RM4 mini cannot be reached or authenticated."""


class BroadlinkClient:
    """Thread-safe wrapper around a single Broadlink RM device."""

    def __init__(self, ip: str, mac_hex: str, devtype: int) -> None:
        self.ip = ip
        self.mac_hex = mac_hex
        self.devtype = devtype
        self._device: Optional[broadlink.Device] = None
        self._lock = threading.RLock()
        self.last_error: Optional[str] = None

    # -- connection -------------------------------------------------------

    def _connect(self) -> broadlink.Device:
        if not self.ip:
            raise DeviceUnavailable("Keine IP-Adresse konfiguriert (Option 'broadlink_ip').")

        device: Optional[broadlink.Device] = None
        try:
            device = broadlink.hello(self.ip, timeout=5)
            found_mac = device.mac.hex()
            # Some firmwares report the MAC reversed in the hello response.
            if self.mac_hex and self.mac_hex not in (found_mac, bytes.fromhex(found_mac)[::-1].hex()):
                log.warning(
                    "MAC aus Konfiguration (%s) passt nicht zum Gerät unter %s (%s).",
                    _format_mac(self.mac_hex),
                    self.ip,
                    _format_mac(found_mac),
                )
            log.info(
                "Gerät gefunden: %s %s (Typ 0x%04X) unter %s",
                device.manufacturer,
                device.model,
                device.devtype,
                self.ip,
            )
        except bl_errors.BroadlinkException as err:
            if not self.mac_hex:
                raise DeviceUnavailable(
                    f"Gerät unter {self.ip} antwortet nicht ({err}). "
                    "Bitte IP prüfen oder zusätzlich die MAC-Adresse angeben."
                ) from err
            log.warning("Discovery fehlgeschlagen (%s) – verbinde direkt über IP/MAC.", err)
            device = broadlink.gendevice(
                self.devtype, (self.ip, 80), bytes.fromhex(self.mac_hex)
            )

        if getattr(device, "is_locked", False):
            raise DeviceUnavailable(
                "Das Gerät ist gesperrt. Bitte in der Broadlink-App unter "
                "Eigenschaften → 'Gerät sperren' deaktivieren."
            )
        if not hasattr(device, "send_data"):
            raise DeviceUnavailable(
                f"Das Gerät unter {self.ip} ist keine Broadlink-Fernbedienung (Typ 0x{device.devtype:04X})."
            )

        try:
            device.auth()
        except (bl_errors.BroadlinkException, OSError) as err:
            raise DeviceUnavailable(f"Authentifizierung fehlgeschlagen: {err}") from err
        return device

    def _get(self) -> broadlink.Device:
        if self._device is None:
            self._device = self._connect()
        return self._device

    def reset(self) -> None:
        with self._lock:
            self._device = None

    def call(self, func: Callable[[broadlink.Device], Any]) -> Any:
        """Run func(device); reconnect once on network/auth errors."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    result = func(self._get())
                    self.last_error = None
                    return result
                except DeviceUnavailable as err:
                    self._device = None
                    self.last_error = str(err)
                    raise
                except (bl_errors.BroadlinkException, OSError) as err:
                    self._device = None
                    self.last_error = str(err)
                    if attempt == 2:
                        raise DeviceUnavailable(f"Kommunikation mit dem RM4 mini fehlgeschlagen: {err}") from err
                    log.info("Fehler bei der Kommunikation (%s) – verbinde neu.", err)
        raise DeviceUnavailable("Unbekannter Fehler")  # pragma: no cover

    # -- high level -------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "ip": self.ip,
            "mac": _format_mac(self.mac_hex),
            "connected": False,
        }
        try:
            dev = self.call(lambda d: d)
            info.update(
                connected=True,
                model=f"{dev.manufacturer} {dev.model}".strip() or dev.type,
                devtype=f"0x{dev.devtype:04X}",
                name=dev.name,
            )
            if not info["mac"]:
                info["mac"] = _format_mac(dev.mac.hex())
        except DeviceUnavailable as err:
            info["error"] = str(err)
        return info

    def send(self, packets: List[bytes]) -> None:
        for index, packet in enumerate(packets):
            if index:
                time.sleep(MULTI_CODE_DELAY)
            self.call(lambda d, p=packet: d.send_data(p))

    def learn(self, timeout: float, cancel: threading.Event) -> Optional[bytes]:
        """Enter learning mode and poll check_data() until a code arrives."""
        self.call(lambda d: d.enter_learning())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel.wait(LEARN_POLL_INTERVAL):
                return None
            with self._lock:
                try:
                    data = self._get().check_data()
                except (bl_errors.ReadError, bl_errors.StorageError):
                    continue  # nothing captured yet
                except (bl_errors.BroadlinkException, OSError) as err:
                    self._device = None
                    raise DeviceUnavailable(f"Fehler beim Lernen: {err}") from err
            if data:
                return bytes(data)
        return None


client = BroadlinkClient(BROADLINK_IP, BROADLINK_MAC, BROADLINK_DEVTYPE)


# --------------------------------------------------------------------------
# Persistent storage (/data/devices.json)
# --------------------------------------------------------------------------

_store_lock = threading.Lock()


def _slugify(value: str) -> str:
    replacements = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"}
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    value = re.sub(r"\s*\+\s*$", " plus", value)
    value = re.sub(r"\s*[-−]\s*$", " minus", value)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return value or "item"


def _unique_id(base: str, existing: List[str]) -> str:
    candidate, counter = base, 2
    while candidate in existing:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def load_store() -> Dict[str, Any]:
    data: Dict[str, Any] = {"devices": []}
    try:
        loaded = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("devices"), list):
            data = loaded
        else:
            log.warning("%s hat ein unerwartetes Format – starte mit leerer Liste.", DEVICES_FILE)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as err:
        log.error("%s konnte nicht gelesen werden: %s", DEVICES_FILE, err)
    data["settings"] = {**DEFAULT_SETTINGS, **(data.get("settings") or {})}
    return data


def save_store(data: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DEVICES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, DEVICES_FILE)


def find_device(data: Dict[str, Any], device_id: str) -> Dict[str, Any]:
    for device in data["devices"]:
        if device["id"] == device_id:
            return device
    raise HTTPException(status_code=404, detail="Gerät nicht gefunden.")


def find_command(device: Dict[str, Any], command_id: str) -> Dict[str, Any]:
    for command in device.get("commands", []):
        if command["id"] == command_id:
            return command
    raise HTTPException(status_code=404, detail="Taste nicht gefunden.")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Temporary storage for learned codes
# --------------------------------------------------------------------------

_learned: Dict[str, Dict[str, Any]] = {}
_learned_lock = threading.Lock()
_learn_running = threading.Lock()
_learn_cancel = threading.Event()


def _remember_code(code_b64: str) -> str:
    learn_id = uuid.uuid4().hex
    now = time.time()
    with _learned_lock:
        for key in [k for k, v in _learned.items() if now - v["ts"] > LEARNED_CODE_TTL]:
            del _learned[key]
        _learned[learn_id] = {"code": code_b64, "ts": now}
    return learn_id


def _pop_code(learn_id: str) -> Optional[str]:
    with _learned_lock:
        entry = _learned.pop(learn_id, None)
    if entry and time.time() - entry["ts"] <= LEARNED_CODE_TTL:
        return entry["code"]
    return None


def _packets(code: Any) -> List[bytes]:
    """Stored codes are a Base64 string or a list of them (multi-code button)."""
    return [_decode_code(c) for c in code] if isinstance(code, list) else [_decode_code(code)]


def _sync_smartir(device: Dict[str, Any], data: Dict[str, Any]) -> None:
    """Rewrite an installed SmartIR file after the device's buttons changed."""
    raw = device.get("smartir")
    if not raw or not raw.get("installed"):
        return
    config = smartir.normalize_config(device, data["devices"], raw)
    if smartir.missing_required(config):
        return
    try:
        smartir.install(device, config)
    except (OSError, RuntimeError) as err:
        log.warning("SmartIR-Datei für %s nicht aktualisiert: %s", device["name"], err)


def _decode_code(code: str) -> bytes:
    """Accept base64 (optionally 'b64:' prefixed) or hex IR codes."""
    raw = code.strip()
    if raw.lower().startswith("b64:"):
        raw = raw[4:]
    compact = re.sub(r"\s+", "", raw)
    if compact and len(compact) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", compact):
        return bytes.fromhex(compact)
    try:
        return base64.b64decode(compact, validate=True)
    except ValueError as err:  # binascii.Error is a ValueError
        raise HTTPException(status_code=400, detail="Ungültiger IR-Code (erwartet Base64 oder Hex).") from err


# --------------------------------------------------------------------------
# API models
# --------------------------------------------------------------------------


class DeviceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    icon: str = Field("remote", max_length=30)


class LearnRequest(BaseModel):
    timeout: int = Field(LEARN_TIMEOUT_DEFAULT, ge=5, le=LEARN_TIMEOUT_MAX)


class SaveCommand(BaseModel):
    device_id: str
    name: str = Field(..., min_length=1, max_length=60)
    learn_id: Optional[str] = None
    code: Optional[str] = None
    smartir_slot: Optional[str] = None
    smartir_source: bool = False


class SettingsUpdate(BaseModel):
    remote_entity: str = Field(..., max_length=100)


class SmartIRConfig(BaseModel):
    type: str
    code: Optional[int] = None
    slots: Dict[str, str] = {}
    sources: List[str] = []
    brightness_steps: Optional[int] = None


class SmartIRTest(BaseModel):
    type: str
    code: int
    slot: str = "on"


class SmartIRImport(BaseModel):
    type: str
    code: int
    name: str = Field(..., min_length=1, max_length=60)
    icon: str = Field("remote", max_length=30)


class SendRequest(BaseModel):
    device_id: Optional[str] = None
    command_id: Optional[str] = None
    code: Optional[str] = None


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------

app = FastAPI(title="Broadlink IR Manager", version=APP_VERSION, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.exception_handler(DeviceUnavailable)
def _device_unavailable_handler(_: Request, exc: DeviceUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"version": APP_VERSION, "broadlink_ip": BROADLINK_IP or "–"},
    )


@app.get("/api/status")
def api_status() -> Dict[str, Any]:
    return {"version": APP_VERSION, **client.status()}


@app.get("/api/devices")
def api_devices() -> Dict[str, Any]:
    with _store_lock:
        return load_store()


@app.post("/api/devices")
def api_create_device(body: DeviceCreate) -> Dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name darf nicht leer sein.")
    with _store_lock:
        data = load_store()
        device_id = _unique_id(_slugify(name), [d["id"] for d in data["devices"]])
        device = {"id": device_id, "name": name, "icon": body.icon, "created": _now(), "commands": []}
        data["devices"].append(device)
        save_store(data)
    return device


@app.delete("/api/devices/{device_id}")
def api_delete_device(device_id: str) -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
        find_device(data, device_id)
        data["devices"] = [d for d in data["devices"] if d["id"] != device_id]
        save_store(data)
    return {"ok": True}


@app.delete("/api/devices/{device_id}/commands/{command_id}")
def api_delete_command(device_id: str, command_id: str) -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
        device = find_device(data, device_id)
        find_command(device, command_id)
        device["commands"] = [c for c in device["commands"] if c["id"] != command_id]
        if device.get("smartir"):
            device["smartir"] = smartir.normalize_config(device, data["devices"], device["smartir"])
        _sync_smartir(device, data)
        save_store(data)
    return {"ok": True}


@app.post("/api/learn")
def api_learn(body: Optional[LearnRequest] = None) -> Dict[str, Any]:
    timeout = (body or LearnRequest()).timeout
    if not _learn_running.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Es läuft bereits ein Lernvorgang.")
    try:
        _learn_cancel.clear()
        log.info("Lernmodus gestartet (%ss).", timeout)
        packet = client.learn(timeout, _learn_cancel)
    finally:
        _learn_running.release()

    if packet is None:
        if _learn_cancel.is_set():
            return {"cancelled": True}
        raise HTTPException(status_code=408, detail="Kein Signal empfangen. Bitte erneut versuchen.")

    code_b64 = base64.b64encode(packet).decode()
    learn_id = _remember_code(code_b64)
    log.info("IR-Code empfangen (%d Bytes).", len(packet))
    return {"learn_id": learn_id, "code": code_b64, "hex": packet.hex(), "length": len(packet)}


@app.post("/api/learn/cancel")
def api_learn_cancel() -> Dict[str, Any]:
    _learn_cancel.set()
    return {"ok": True}


@app.post("/api/save-command")
def api_save_command(body: SaveCommand) -> Dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name darf nicht leer sein.")

    if body.learn_id:
        code = _pop_code(body.learn_id)
        if code is None:
            raise HTTPException(status_code=410, detail="Angelernter Code ist abgelaufen. Bitte erneut anlernen.")
    elif body.code:
        code = base64.b64encode(_decode_code(body.code)).decode()
    else:
        raise HTTPException(status_code=400, detail="learn_id oder code wird benötigt.")

    with _store_lock:
        data = load_store()
        device = find_device(data, body.device_id)
        commands = device.setdefault("commands", [])
        existing = next((c for c in commands if c["name"].lower() == name.lower()), None)
        if existing:
            existing.update(code=code, updated=_now())
            command = existing
        else:
            command = {
                "id": _unique_id(_slugify(name), [c["id"] for c in commands]),
                "name": name,
                "code": code,
                "created": _now(),
            }
            commands.append(command)
        if body.smartir_slot or body.smartir_source:
            base = device.get("smartir")
            if not base:  # first use: start from the automatic suggestion
                type_key = smartir.default_type_for_icon(device.get("icon", ""))
                base = {"type": type_key, **smartir.suggest_config(device, type_key)}
            config = smartir.normalize_config(device, data["devices"], base)
            if body.smartir_slot:
                config["slots"][body.smartir_slot] = command["id"]
            if body.smartir_source and command["id"] not in config["sources"]:
                config["sources"].append(command["id"])
            device["smartir"] = smartir.normalize_config(device, data["devices"], config)
        _sync_smartir(device, data)
        save_store(data)
    return {"device_id": device["id"], "command": command}


@app.post("/api/send")
def api_send(body: SendRequest) -> Dict[str, Any]:
    if body.code:
        packets = [_decode_code(body.code)]
        label = "manueller Code"
    elif body.device_id and body.command_id:
        with _store_lock:
            device = find_device(load_store(), body.device_id)
            command = find_command(device, body.command_id)
        packets = _packets(command["code"])
        label = f"{device['name']} / {command['name']}"
    else:
        raise HTTPException(status_code=400, detail="device_id und command_id (oder code) werden benötigt.")

    client.send(packets)
    log.info("Gesendet: %s", label)
    return {"ok": True, "sent": label}


# --------------------------------------------------------------------------
# YAML export
# --------------------------------------------------------------------------

_ON_NAMES = {"on", "an", "ein", "power on", "power an", "einschalten", "anschalten"}
_OFF_NAMES = {"off", "aus", "power off", "power aus", "ausschalten"}
_TOGGLE_NAMES = {"power", "power toggle", "toggle", "standby", "on/off", "an/aus", "ein/aus", "ein aus", "an aus"}


def _q(value: str) -> str:
    """Quote a string for YAML (double-quoted, escaped)."""
    return json.dumps(value, ensure_ascii=False)


def _yaml_command(code: Any) -> str:
    if isinstance(code, list):
        return "[" + ", ".join(_q("b64:" + c) for c in code) + "]"
    return _q("b64:" + code)


def _first(code: Any) -> str:
    return code[0] if isinstance(code, list) else code


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", name.strip().lower())


def _switch_codes(device: Dict[str, Any]):
    by_name = {_norm(c["name"]): c for c in device.get("commands", [])}
    on = next((by_name[n] for n in by_name if n in _ON_NAMES), None)
    off = next((by_name[n] for n in by_name if n in _OFF_NAMES), None)
    if on and off:
        return on, off
    toggle = next((by_name[n] for n in by_name if n in _TOGGLE_NAMES), None)
    if toggle:
        return toggle, toggle
    return None


def build_yaml(data: Dict[str, Any], remote_entity: str, mac: str) -> str:
    devices = [d for d in data["devices"] if d.get("commands")]
    lines: List[str] = [
        "# ------------------------------------------------------------------",
        f"# Broadlink IR Manager {APP_VERSION} – Export vom {datetime.now():%d.%m.%Y %H:%M}",
        f"# Remote-Entität: {remote_entity}",
        "# Voraussetzung: Der RM4 mini ist in Home Assistant über die",
        "# Broadlink-Integration eingerichtet (Einstellungen → Geräte & Dienste).",
        "# ------------------------------------------------------------------",
        "",
    ]
    if not devices:
        lines.append("# Noch keine Tasten angelernt.")
        return "\n".join(lines) + "\n"

    lines += [
        "# 1) Skripte – eines pro Taste, nutzt remote.send_command",
        "#    Inhalt in scripts.yaml einfügen (ohne die Zeile 'script:')",
        "#    oder unter 'script:' in configuration.yaml.",
        "script:",
    ]
    used_script_ids: List[str] = []
    for device in devices:
        for command in device["commands"]:
            script_id = _unique_id(f"{_slugify(device['name'])}_{_slugify(command['name'])}", used_script_ids)
            used_script_ids.append(script_id)
            lines += [
                f"  {script_id}:",
                f"    alias: {_q(device['name'] + ' – ' + command['name'])}",
                "    icon: mdi:remote",
                "    mode: queued",
                "    sequence:",
                "      - action: remote.send_command",
                "        target:",
                f"          entity_id: {remote_entity}",
                "        data:",
                f"          command: {_yaml_command(command['code'])}",
            ]
    lines.append("")

    switch_lines: List[str] = []
    skipped: List[str] = []
    for device in devices:
        codes = _switch_codes(device)
        if not codes:
            skipped.append(device["name"])
            continue
        on, off = codes
        switch_lines += [
            f"      - name: {_q(device['name'])}",
            f"        command_on: {_q(_first(on['code']))}",
            f"        command_off: {_q(_first(off['code']))}",
        ]

    lines += [
        "# 2) Schalter-Entitäten (Broadlink-Switch-Plattform)",
        "#    Nutzt die Tasten 'An'/'Aus' bzw. 'On'/'Off' eines Geräts,",
        "#    sonst eine Taste 'Power' für beide Richtungen.",
        "#    In configuration.yaml einfügen und Home Assistant neu starten.",
    ]
    if switch_lines:
        lines += ["switch:", "  - platform: broadlink", f"    mac: {_q(mac or 'AA:BB:CC:DD:EE:FF')}", "    switches:"]
        lines += switch_lines
    else:
        lines.append("#    Keine passenden Tasten gefunden (benötigt 'Power' oder 'An' + 'Aus').")
    if skipped:
        lines.append(f"#    Ohne Schalter (keine Power/An/Aus-Taste): {', '.join(skipped)}")
    return "\n".join(lines) + "\n"


@app.get("/api/export/yaml", response_class=PlainTextResponse)
def api_export_yaml(
    remote_entity: Optional[str] = Query(None, max_length=100),
) -> PlainTextResponse:
    with _store_lock:
        settings = load_store()["settings"]
    entity = (remote_entity or settings["remote_entity"]).strip()
    if not re.fullmatch(r"remote\.[a-z0-9_]+", entity):
        raise HTTPException(status_code=400, detail="Ungültige Entität – erwartet z. B. remote.rm4_mini.")
    with _store_lock:
        data = load_store()
    return PlainTextResponse(build_yaml(data, entity, _format_mac(BROADLINK_MAC)))


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

_ENTITY_RE = re.compile(r"remote\.[a-z0-9_]+")


@app.get("/api/settings")
def api_get_settings() -> Dict[str, Any]:
    with _store_lock:
        return load_store()["settings"]


@app.put("/api/settings")
def api_put_settings(body: SettingsUpdate) -> Dict[str, Any]:
    entity = body.remote_entity.strip()
    if not _ENTITY_RE.fullmatch(entity):
        raise HTTPException(status_code=400, detail="Ungültige Entität – erwartet z. B. remote.rm4_mini.")
    with _store_lock:
        data = load_store()
        data["settings"]["remote_entity"] = entity
        for device in data["devices"]:
            _sync_smartir(device, data)
        save_store(data)
        return data["settings"]


# --------------------------------------------------------------------------
# SmartIR
# --------------------------------------------------------------------------


def _smartir_view(device: Dict[str, Any], data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    state = smartir.install_state()
    path = smartir.code_file_path(config)
    return {
        "device_id": device["id"],
        "config": config,
        "missing": smartir.missing_required(config),
        "json": smartir.build_device_json(device, config),
        "yaml": smartir.build_yaml(device, config, data["settings"]["remote_entity"]),
        "install": {**state, "path": str(path) if path else None, "installed": smartir.is_installed(config)},
    }


@app.get("/api/smartir/types")
def api_smartir_types() -> Dict[str, Any]:
    return {
        key: {
            "label": t["label"],
            "hint": t["hint"],
            "slots": [{"key": k, "label": label} for k, label in t["slots"]],
            "digits": [{"key": k, "label": label} for k, label in smartir.DIGIT_SLOTS] if t["has_digits"] else [],
            "has_sources": t["has_sources"],
        }
        for key, t in smartir.TYPES.items()
    }


@app.get("/api/devices/{device_id}/smartir")
def api_get_smartir(device_id: str, type: Optional[str] = None) -> Dict[str, Any]:  # noqa: A002
    with _store_lock:
        data = load_store()
        device = find_device(data, device_id)
        raw = device.get("smartir")
        if raw and (type is None or type == raw.get("type")):
            config = smartir.normalize_config(device, data["devices"], raw)
        else:
            type_key = type if type in smartir.TYPES else smartir.default_type_for_icon(device.get("icon", ""))
            suggestion = smartir.suggest_config(device, type_key)
            base = {**(raw or {}), "type": type_key, **suggestion}
            config = smartir.normalize_config(device, data["devices"], base)
        return _smartir_view(device, data, config)


@app.put("/api/devices/{device_id}/smartir")
def api_put_smartir(device_id: str, body: SmartIRConfig) -> Dict[str, Any]:
    if body.type not in smartir.TYPES:
        raise HTTPException(status_code=400, detail="Unbekannter SmartIR-Typ.")
    with _store_lock:
        data = load_store()
        device = find_device(data, device_id)
        if body.code is not None:
            clash = next((d for d in data["devices"] if d["id"] != device_id
                          and (d.get("smartir") or {}).get("code") == body.code), None)
            if clash:
                raise HTTPException(status_code=409, detail=f"Code {body.code} nutzt bereits „{clash['name']}“.")
        previous = device.get("smartir") or {}
        raw = {**body.dict(), "installed": previous.get("installed", False)}
        config = smartir.normalize_config(device, data["devices"], raw)
        device["smartir"] = config
        _sync_smartir(device, data)
        save_store(data)
        return _smartir_view(device, data, config)


@app.post("/api/devices/{device_id}/smartir/install")
def api_install_smartir(device_id: str) -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
        device = find_device(data, device_id)
        config = smartir.normalize_config(device, data["devices"], device.get("smartir"))
        missing = smartir.missing_required(config)
        if missing:
            raise HTTPException(status_code=400, detail="Es fehlt noch: " + ", ".join(missing))
        try:
            path = smartir.install(device, config)
        except RuntimeError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        except OSError as err:
            raise HTTPException(status_code=500, detail=f"Datei konnte nicht geschrieben werden: {err}") from err
        config["installed"] = True
        device["smartir"] = config
        save_store(data)
        view = _smartir_view(device, data, config)
    view["written"] = str(path)
    return view


@app.get("/api/smartir/catalog")
def api_smartir_catalog(type: str = "media_player") -> Dict[str, Any]:  # noqa: A002
    try:
        return {"type": type, "entries": smartir.catalog(type)}
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except (urllib.error.URLError, OSError) as err:
        raise HTTPException(status_code=502, detail=f"SmartIR-Datenbank nicht erreichbar: {err}") from err


def _fetch_code_file(type_key: str, code: int) -> Dict[str, Any]:
    try:
        return smartir.fetch_code_file(type_key, code)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    except (urllib.error.URLError, OSError) as err:
        raise HTTPException(status_code=502, detail=f"Code {code} konnte nicht geladen werden: {err}") from err


@app.post("/api/smartir/test")
def api_smartir_test(body: SmartIRTest) -> Dict[str, Any]:
    data = _fetch_code_file(body.type, body.code)
    commands, _steps = smartir.commands_from_code_file(body.type, data)
    match = next((c for c in commands if c[2] == body.slot), None)
    if not match:
        raise HTTPException(status_code=404, detail="Dieser Code hat keine passende Taste.")
    client.send(_packets(match[1]))
    return {"ok": True}


@app.post("/api/smartir/import")
def api_smartir_import(body: SmartIRImport) -> Dict[str, Any]:
    if body.type not in smartir.TYPES:
        raise HTTPException(status_code=400, detail="Unbekannter SmartIR-Typ.")
    file_data = _fetch_code_file(body.type, body.code)
    imported, steps = smartir.commands_from_code_file(body.type, file_data)
    if not imported:
        raise HTTPException(status_code=400, detail="Die Code-Datei enthält keine nutzbaren Tasten.")
    name = body.name.strip()
    with _store_lock:
        data = load_store()
        device_id = _unique_id(_slugify(name), [d["id"] for d in data["devices"]])
        device: Dict[str, Any] = {
            "id": device_id,
            "name": name,
            "icon": body.icon,
            "manufacturer": file_data.get("manufacturer") or "",
            "created": _now(),
            "source": f"SmartIR {body.type} {body.code}",
            "commands": [],
        }
        slots: Dict[str, str] = {}
        sources: List[str] = []
        for cmd_name, code, slot, is_source in imported:
            command_id = _unique_id(_slugify(cmd_name), [c["id"] for c in device["commands"]])
            device["commands"].append({"id": command_id, "name": cmd_name, "code": code, "created": _now()})
            if slot:
                slots[slot] = command_id
            if is_source:
                sources.append(command_id)
        data["devices"].append(device)
        device["smartir"] = smartir.normalize_config(
            device, data["devices"],
            {"type": body.type, "slots": slots, "sources": sources, "brightness_steps": steps},
        )
        save_store(data)
    log.info("SmartIR-Code %s (%s) als „%s“ importiert.", body.code, body.type, name)
    return device


def _resync_loop() -> None:
    """Restore installed SmartIR files, e.g. after a SmartIR update via HACS."""
    while True:
        try:
            with _store_lock:
                data = load_store()
                for device in data["devices"]:
                    raw = device.get("smartir")
                    if raw and raw.get("installed"):
                        config = smartir.normalize_config(device, data["devices"], raw)
                        path = smartir.code_file_path(config)
                        if path and not path.exists():
                            log.info("SmartIR-Datei fehlt (%s) – stelle sie wieder her.", path)
                            _sync_smartir(device, data)
        except Exception as err:  # noqa: BLE001 – never let the watcher die
            log.warning("SmartIR-Prüfung fehlgeschlagen: %s", err)
        time.sleep(600)


@app.on_event("startup")
def _start_background() -> None:
    threading.Thread(target=_resync_loop, name="smartir-resync", daemon=True).start()
