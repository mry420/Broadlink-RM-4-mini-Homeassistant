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

import colors
import ha_sync
import irdb
import layout
import smartir

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_VERSION = "2.0.0"
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
DEFAULT_SETTINGS = {"remote_entity": "remote.rm4_mini", "ha_auto": True}

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
    if _ha is not None:
        _ha.request()  # push the change to Home Assistant (debounced, in the background)


def _load_store_locked() -> Dict[str, Any]:
    with _store_lock:
        return load_store()


def _patch_settings(patch: Dict[str, Any]) -> None:
    with _store_lock:
        data = load_store()
        data["settings"].update(patch)
        save_store(data)


_ha: Optional["ha_sync.HASync"] = None


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


class CommandUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    color: Optional[str] = Field(None, max_length=30)  # palette key, "" = no colour


class ColorAssignment(BaseModel):
    id: str
    color: str  # palette key, "" = other function, "none" = no reaction


class ColorCheck(BaseModel):
    assignments: List[ColorAssignment]
    delete_dead: bool = True


class LedTest(BaseModel):
    family: int = Field(..., ge=0)
    key: str = Field(..., max_length=10)


class SettingsUpdate(BaseModel):
    remote_entity: Optional[str] = Field(None, max_length=100)
    ha_auto: Optional[bool] = None


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


class IRDBSend(BaseModel):
    path: str = Field(..., max_length=400)
    index: int = Field(..., ge=0)


class IRDBImport(BaseModel):
    path: str = Field(..., max_length=400)
    name: str = Field(..., min_length=1, max_length=60)
    icon: str = Field("remote", max_length=30)
    indices: Optional[List[int]] = None


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
        data = load_store()
    for device in data["devices"]:
        for command in device.get("commands", []):
            command["display_color"] = colors.command_color(command)
    return data


@app.get("/api/colors")
def api_colors() -> Dict[str, Any]:
    return {"palette": colors.PALETTE}


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


def _unique_name(name: str, taken: List[str]) -> str:
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate, n = f"{name} {n}", n + 1
    return candidate


@app.put("/api/devices/{device_id}/commands/{command_id}")
def api_update_command(device_id: str, command_id: str, body: CommandUpdate) -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
        device = find_device(data, device_id)
        command = find_command(device, command_id)
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Name darf nicht leer sein.")
            if any(c["name"].lower() == name.lower() and c["id"] != command_id for c in device["commands"]):
                raise HTTPException(status_code=409, detail=f"Es gibt schon eine Taste „{name}“.")
            command["name"] = name
        if body.color is not None:
            if body.color and body.color not in colors.PALETTE:
                raise HTTPException(status_code=400, detail="Unbekannte Farbe.")
            command["color"] = body.color or ""
        command["updated"] = _now()
        _sync_smartir(device, data)
        save_store(data)
    return {**command, "display_color": colors.command_color(command)}


@app.post("/api/devices/{device_id}/colors")
def api_color_check(device_id: str, body: ColorCheck) -> Dict[str, Any]:
    """Store the colours the user actually saw and fix the button names."""
    with _store_lock:
        data = load_store()
        device = find_device(data, device_id)
        by_id = {c["id"]: c for c in device.get("commands", [])}
        dead = []
        renamed = 0
        for item in body.assignments:
            command = by_id.get(item.id)
            if not command:
                continue
            if item.color == "none":
                dead.append(item.id)
                continue
            if item.color and item.color not in colors.PALETTE:
                raise HTTPException(status_code=400, detail=f"Unbekannte Farbe {item.color}.")
            was_colour_button = bool(colors.command_color(command)) or command["name"].lower().startswith(("farbe", "color"))
            command["color"] = item.color
            # Colour buttons get the colour as name; other buttons keep theirs.
            if item.color and was_colour_button:
                label = colors.PALETTE[item.color]["label"]
                if command["name"] != label:
                    taken = [c["name"].lower() for c in device["commands"] if c["id"] != command["id"]]
                    command["name"] = _unique_name(label, taken)
                    renamed += 1
            command["updated"] = _now()
        if body.delete_dead and dead:
            device["commands"] = [c for c in device["commands"] if c["id"] not in dead]
        if device.get("smartir"):
            device["smartir"] = smartir.normalize_config(device, data["devices"], device["smartir"])
        _sync_smartir(device, data)
        save_store(data)
    log.info("Farbprüfung %s: %d umbenannt, %d ohne Reaktion.", device["name"], renamed, len(dead))
    return {"renamed": renamed, "removed": len(dead) if body.delete_dead else 0}


_led_families: Optional[List[Dict[str, Any]]] = None


def _families() -> List[Dict[str, Any]]:
    global _led_families
    if _led_families is None:
        _led_families = json.loads((BASE_DIR / "data" / "led_families.json").read_text(encoding="utf-8"))
    return _led_families


@app.get("/api/led/families")
def api_led_families() -> Dict[str, Any]:
    return {"families": [
        {"index": i, "path": f["path"], "buttons": f["buttons"], "remotes": f["remotes"],
         "brands": f["brands"], "keys": sorted(f["codes"])}
        for i, f in enumerate(_families())
    ]}


@app.post("/api/led/test")
def api_led_test(body: LedTest) -> Dict[str, Any]:
    families = _families()
    if body.family >= len(families):
        raise HTTPException(status_code=404, detail="Unbekannte Familie.")
    code = families[body.family]["codes"].get(body.key)
    if not code:
        raise HTTPException(status_code=404, detail="Diese Fernbedienung hat diese Taste nicht.")
    client.send(_packets(code))
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
    state = None
    if not body.code:
        state = _apply_effects(device, layout.command_effects(device, command))
    return {"ok": True, "sent": label, "state": state}


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
    entity = body.remote_entity.strip() if body.remote_entity is not None else None
    if entity is not None and not _ENTITY_RE.fullmatch(entity):
        raise HTTPException(status_code=400, detail="Ungültige Entität – erwartet z. B. remote.rm4_mini.")
    with _store_lock:
        data = load_store()
        if entity is not None:
            data["settings"]["remote_entity"] = entity
        if body.ha_auto is not None:
            data["settings"]["ha_auto"] = body.ha_auto
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


# --------------------------------------------------------------------------
# Open IR database (Flipper-IRDB)
# --------------------------------------------------------------------------

_BUTTON_NAMES = {
    "power": "Power", "on": "An", "off": "Aus", "power on": "An", "power off": "Aus",
    "vol up": "Lauter", "vol dn": "Leiser", "vol down": "Leiser", "volume up": "Lauter", "volume down": "Leiser",
    "mute": "Stumm", "ch next": "Kanal +", "ch prev": "Kanal −", "ch up": "Kanal +", "ch down": "Kanal −",
    "up": "Hoch", "down": "Runter", "left": "Links", "right": "Rechts", "ok": "OK", "enter": "OK",
    "menu": "Menü", "back": "Zurück", "return": "Zurück", "setup": "Einstellungen", "exit": "Exit", "source": "Quelle", "input": "Quelle", "info": "Info",
    "home": "Home", "play": "Play", "pause": "Pause", "stop": "Stop", "next": "Nächster", "prev": "Vorheriger",
    "brightness up": "Heller", "brightness down": "Dunkler", "bright up": "Heller", "bright dn": "Dunkler",
    "speed up": "Stufe +", "speed dn": "Stufe −", "timer": "Timer", "swing": "Swing", "oscillate": "Schwenken",
}
_CATEGORY_ICONS = {"tv": "tv", "projector": "projector", "audio": "soundbar", "video": "receiver",
                   "light": "led", "ac": "ac", "fan": "fan", "climate": "fan"}


def _german_button_name(name: str) -> str:
    key = re.sub(r"[_\s]+", " ", name.strip()).lower()
    if key in _BUTTON_NAMES:
        return _BUTTON_NAMES[key]
    color = colors.guess_color(name)
    if color and len(key) > 2 and not re.search(r"\d", key):  # "Red" → "Rot", but keep "R1"
        return colors.PALETTE[color]["label"]
    return re.sub(r"_+", " ", name).strip()


def _irdb_remote(path: str) -> List[Dict[str, Any]]:
    if not irdb.is_known_path(path):
        raise HTTPException(status_code=404, detail="Fernbedienung nicht in der Datenbank.")
    try:
        return irdb.load_remote(path)
    except (urllib.error.URLError, OSError) as err:
        raise HTTPException(status_code=502, detail=f"IR-Datenbank nicht erreichbar: {err}") from err


@app.get("/api/irdb/categories")
def api_irdb_categories() -> Dict[str, Any]:
    return {"categories": irdb.categories(), "icons": _CATEGORY_ICONS}


@app.get("/api/irdb/search")
def api_irdb_search(q: str = Query("", max_length=100), category: Optional[int] = None) -> Dict[str, Any]:
    entries, total = irdb.search(q, category)
    return {"entries": entries, "total": total}


@app.get("/api/irdb/remote")
def api_irdb_remote(path: str = Query(..., max_length=400)) -> Dict[str, Any]:
    signals = _irdb_remote(path)
    buttons = [
        {
            "index": i,
            "name": s["name"],
            "label": _german_button_name(s["name"]),
            "supported": s["supported"],
            "protocol": s.get("protocol") or ("Raw" if s.get("type") == "raw" else ""),
            "error": s.get("error"),
        }
        for i, s in enumerate(signals)
    ]
    return {"path": path, "buttons": buttons}


@app.post("/api/irdb/send")
def api_irdb_send(body: IRDBSend) -> Dict[str, Any]:
    signals = _irdb_remote(body.path)
    if body.index >= len(signals):
        raise HTTPException(status_code=404, detail="Taste nicht gefunden.")
    signal = signals[body.index]
    if not signal["supported"]:
        raise HTTPException(status_code=400, detail=signal.get("error") or "Signal nicht unterstützt.")
    client.send(_packets(signal["code"]))
    log.info("IR-Datenbank: gesendet %s / %s", body.path, signal["name"])
    return {"ok": True}


@app.post("/api/irdb/import")
def api_irdb_import(body: IRDBImport) -> Dict[str, Any]:
    signals = _irdb_remote(body.path)
    wanted = set(body.indices) if body.indices is not None else None
    name = body.name.strip()
    with _store_lock:
        data = load_store()
        device: Dict[str, Any] = {
            "id": _unique_id(_slugify(name), [d["id"] for d in data["devices"]]),
            "name": name,
            "icon": body.icon,
            "created": _now(),
            "source": f"IR-Datenbank: {body.path}",
            "commands": [],
        }
        used_names: List[str] = []
        for i, signal in enumerate(signals):
            if not signal["supported"] or (wanted is not None and i not in wanted):
                continue
            label = _german_button_name(signal["name"])
            unique_label, n = label, 2
            while unique_label.lower() in used_names:
                unique_label, n = f"{label} {n}", n + 1
            used_names.append(unique_label.lower())
            device["commands"].append({
                "id": _unique_id(_slugify(unique_label), [c["id"] for c in device["commands"]]),
                "name": unique_label,
                "code": signal["code"],
                "created": _now(),
            })
        if not device["commands"]:
            raise HTTPException(status_code=400, detail="Keine Tasten ausgewählt.")
        data["devices"].append(device)
        save_store(data)
    log.info("IR-Datenbank: „%s“ mit %d Tasten übernommen (%s).", name, len(device["commands"]), body.path)
    return device


# --------------------------------------------------------------------------
# Home Assistant sync (scripts + remote dashboard)
# --------------------------------------------------------------------------

_ha = ha_sync.HASync(_load_store_locked, _patch_settings, DATA_DIR / "ha_state.json")


@app.get("/api/ha/status")
def api_ha_status() -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
    views = {d["id"]: f"/{ha_sync.DASHBOARD_URL}/{ha_sync.view_path(d)}" for d in data["devices"] if d.get("commands")}
    return {**_ha.status, "auto": data["settings"].get("ha_auto", True), "views": views}


@app.post("/api/ha/sync")
def api_ha_sync() -> Dict[str, Any]:
    try:
        _ha.sync_now()
    except ha_sync.HAError as err:
        _ha.status["error"] = str(err)
        _ha.status["running"] = False
        raise HTTPException(status_code=502, detail=str(err)) from err
    return api_ha_status()


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


# --------------------------------------------------------------------------
# Remembered device state (power, active input per output)
# --------------------------------------------------------------------------

STATE_FILE = DATA_DIR / "state.json"
_state_lock = threading.Lock()


def _read_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _device_state(state: Dict[str, Any], device_id: str) -> Dict[str, Any]:
    return state.setdefault(device_id, {"power": None, "groups": {}})


def _apply_effects(device: Dict[str, Any], effects: List[Dict[str, Any]], push: bool = True) -> Dict[str, Any]:
    with _state_lock:
        state = _read_state()
        entry = _device_state(state, device["id"])
        for effect in effects:
            target = entry if effect["group"] is None else entry["groups"].setdefault(effect["group"], {"value": None, "power": None})
            if effect["kind"] == "power":
                target["power"] = (not target.get("power")) if effect["value"] == "toggle" else bool(effect["value"])
            elif effect["kind"] == "select":
                target["value"] = effect["value"]
        if effects:
            _write_state(state)
    if effects and push:
        threading.Thread(target=_ha.push_effects, args=(device["id"], effects), daemon=True).start()
    return entry


class StateUpdate(BaseModel):
    group: Optional[str] = Field(None, max_length=60)
    power: Optional[bool] = None
    value: Optional[str] = Field(None, max_length=60)


@app.get("/api/state")
def api_state() -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
    with _state_lock:
        state = _read_state()
    remote = _ha.read_states(data["devices"])  # values from HA helpers win (also changed by HA scripts)
    result: Dict[str, Any] = {}
    for device in data["devices"]:
        model = layout.state_model(device)
        if not model["power"] and not model["groups"]:
            continue
        local = state.get(device["id"], {"power": None, "groups": {}})
        merged = {"power": local.get("power") if model["power"] else None, "groups": {}, "model": model}
        for name, group in model["groups"].items():
            g = local.get("groups", {}).get(name, {})
            merged["groups"][name] = {"value": g.get("value") if group["select"] else None,
                                      "power": g.get("power") if group["power"] else None}
        for key, value in remote.get(device["id"], {}).items():
            if key == "power" and model["power"]:
                merged["power"] = value
            elif key.startswith("value:") and key[6:] in merged["groups"]:
                merged["groups"][key[6:]]["value"] = value
            elif key.startswith("power:") and key[6:] in merged["groups"]:
                merged["groups"][key[6:]]["power"] = value
        result[device["id"]] = merged
    return {"devices": result}


@app.put("/api/state/{device_id}")
def api_put_state(device_id: str, body: StateUpdate) -> Dict[str, Any]:
    """Correct the remembered state without sending anything (e.g. TV was switched by hand)."""
    with _store_lock:
        device = find_device(load_store(), device_id)
    effects = []
    if body.power is not None:
        effects.append({"kind": "power", "group": body.group, "value": body.power})
    if body.value is not None and body.group:
        effects.append({"kind": "select", "group": body.group, "value": body.value})
    return _apply_effects(device, effects)


# --------------------------------------------------------------------------
# Devices: edit, layout
# --------------------------------------------------------------------------


class DeviceUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    icon: Optional[str] = Field(None, max_length=30)


@app.put("/api/devices/{device_id}")
def api_update_device(device_id: str, body: DeviceUpdate) -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
        device = find_device(data, device_id)
        if body.name is not None and body.name.strip():
            device["name"] = body.name.strip()
        if body.icon is not None:
            device["icon"] = body.icon
        device["updated"] = _now()
        _sync_smartir(device, data)
        save_store(data)
    return device


@app.get("/api/devices/{device_id}/layout")
def api_device_layout(device_id: str) -> Dict[str, Any]:
    with _store_lock:
        device = find_device(load_store(), device_id)
    return layout.device_layout(device)


# --------------------------------------------------------------------------
# Scenes (macros across devices)
# --------------------------------------------------------------------------


class SceneStep(BaseModel):
    device_id: str
    command_id: str
    repeat: int = Field(1, ge=1, le=20)
    delay: float = Field(0.5, ge=0, le=120)  # seconds after this step


class SceneBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    icon: str = Field("movie", max_length=30)
    steps: List[SceneStep] = []


_scene_running: Dict[str, Any] = {"id": None, "step": 0, "error": None}


def _validate_steps(data: Dict[str, Any], steps: List[SceneStep]) -> List[Dict[str, Any]]:
    result = []
    for step in steps:
        device = find_device(data, step.device_id)
        find_command(device, step.command_id)
        result.append({"device_id": step.device_id, "command_id": step.command_id,
                       "repeat": step.repeat, "delay": round(step.delay, 2)})
    return result


@app.get("/api/scenes")
def api_scenes() -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
    return {"scenes": data.get("scenes", []), "running": _scene_running}


@app.post("/api/scenes")
def api_create_scene(body: SceneBody) -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
        scenes = data.setdefault("scenes", [])
        scene = {
            "id": _unique_id(_slugify(body.name), [s["id"] for s in scenes]),
            "name": body.name.strip(),
            "icon": body.icon,
            "steps": _validate_steps(data, body.steps),
            "created": _now(),
        }
        scenes.append(scene)
        save_store(data)
    return scene


@app.put("/api/scenes/{scene_id}")
def api_update_scene(scene_id: str, body: SceneBody) -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
        scene = next((s for s in data.get("scenes", []) if s["id"] == scene_id), None)
        if not scene:
            raise HTTPException(status_code=404, detail="Szene nicht gefunden.")
        scene.update(name=body.name.strip(), icon=body.icon, steps=_validate_steps(data, body.steps), updated=_now())
        save_store(data)
    return scene


@app.delete("/api/scenes/{scene_id}")
def api_delete_scene(scene_id: str) -> Dict[str, Any]:
    with _store_lock:
        data = load_store()
        before = len(data.get("scenes", []))
        data["scenes"] = [s for s in data.get("scenes", []) if s["id"] != scene_id]
        if len(data["scenes"]) == before:
            raise HTTPException(status_code=404, detail="Szene nicht gefunden.")
        save_store(data)
    return {"ok": True}


def _run_scene(scene: Dict[str, Any], devices: Dict[str, Dict[str, Any]]) -> None:
    _scene_running.update(id=scene["id"], step=0, error=None)
    try:
        for index, step in enumerate(scene["steps"]):
            _scene_running["step"] = index + 1
            device = devices.get(step["device_id"])
            command = next((c for c in (device or {}).get("commands", []) if c["id"] == step["command_id"]), None)
            if not command:
                log.warning("Szene %s: Schritt %d übersprungen (Taste fehlt).", scene["name"], index + 1)
                continue
            for n in range(step.get("repeat", 1)):
                client.send(_packets(command["code"]))
                _apply_effects(device, layout.command_effects(device, command))
                if n < step.get("repeat", 1) - 1:
                    time.sleep(0.35)
            time.sleep(step.get("delay", 0.5))
        log.info("Szene „%s“ ausgeführt.", scene["name"])
    except (DeviceUnavailable, HTTPException) as err:
        _scene_running["error"] = str(getattr(err, "detail", err))
        log.warning("Szene „%s“ abgebrochen: %s", scene["name"], _scene_running["error"])
    finally:
        _scene_running["id"] = None


@app.post("/api/scenes/{scene_id}/run")
def api_run_scene(scene_id: str) -> Dict[str, Any]:
    if _scene_running["id"]:
        raise HTTPException(status_code=409, detail="Es läuft bereits eine Szene.")
    with _store_lock:
        data = load_store()
    scene = next((s for s in data.get("scenes", []) if s["id"] == scene_id), None)
    if not scene:
        raise HTTPException(status_code=404, detail="Szene nicht gefunden.")
    if not scene["steps"]:
        raise HTTPException(status_code=400, detail="Die Szene hat noch keine Schritte.")
    devices = {d["id"]: d for d in data["devices"]}
    threading.Thread(target=_run_scene, args=(scene, devices), name="scene", daemon=True).start()
    return {"started": True}


# --------------------------------------------------------------------------
# Backup / restore
# --------------------------------------------------------------------------


@app.get("/api/backup")
def api_backup() -> JSONResponse:
    with _store_lock:
        data = load_store()
    payload = {"app": "broadlink_manager", "version": APP_VERSION, "exported": _now(),
               "devices": data["devices"], "scenes": data.get("scenes", []), "settings": data["settings"]}
    filename = f"broadlink-ir-manager-{datetime.now():%Y%m%d-%H%M}.json"
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class RestoreBody(BaseModel):
    devices: List[Dict[str, Any]]
    scenes: List[Dict[str, Any]] = []
    settings: Optional[Dict[str, Any]] = None


@app.post("/api/restore")
def api_restore(body: RestoreBody) -> Dict[str, Any]:
    ids = set()
    for device in body.devices:
        if not isinstance(device.get("id"), str) or not isinstance(device.get("name"), str):
            raise HTTPException(status_code=400, detail="Ungültige Sicherung: Gerät ohne id/name.")
        if device["id"] in ids:
            raise HTTPException(status_code=400, detail=f"Ungültige Sicherung: doppelte Geräte-ID {device['id']}.")
        ids.add(device["id"])
        for command in device.get("commands", []):
            if not isinstance(command.get("id"), str) or not isinstance(command.get("name"), str):
                raise HTTPException(status_code=400, detail="Ungültige Sicherung: Taste ohne id/name.")
            try:
                _packets(command.get("code"))
            except (HTTPException, TypeError) as err:
                raise HTTPException(status_code=400, detail=f"Ungültiger Code bei „{command['name']}“.") from err
    with _store_lock:
        data = load_store()
        data["devices"] = body.devices
        data["scenes"] = [s for s in body.scenes if isinstance(s, dict) and s.get("id")]
        if body.settings:
            data["settings"].update({k: v for k, v in body.settings.items() if k in DEFAULT_SETTINGS})
        save_store(data)
    log.info("Sicherung wiederhergestellt: %d Geräte, %d Szenen.", len(body.devices), len(data["scenes"]))
    return {"devices": len(body.devices), "scenes": len(data["scenes"])}
