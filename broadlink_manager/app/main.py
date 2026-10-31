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

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_VERSION = "1.0.0"
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

    def send(self, packet: bytes) -> None:
        self.call(lambda d: d.send_data(packet))

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
    try:
        data = json.loads(DEVICES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("devices"), list):
            return data
        log.warning("%s hat ein unerwartetes Format – starte mit leerer Liste.", DEVICES_FILE)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as err:
        log.error("%s konnte nicht gelesen werden: %s", DEVICES_FILE, err)
    return {"devices": []}


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
        save_store(data)
    return {"device_id": device["id"], "command": command}


@app.post("/api/send")
def api_send(body: SendRequest) -> Dict[str, Any]:
    if body.code:
        packet = _decode_code(body.code)
        label = "manueller Code"
    elif body.device_id and body.command_id:
        with _store_lock:
            device = find_device(load_store(), body.device_id)
            command = find_command(device, body.command_id)
        packet = _decode_code(command["code"])
        label = f"{device['name']} / {command['name']}"
    else:
        raise HTTPException(status_code=400, detail="device_id und command_id (oder code) werden benötigt.")

    client.send(packet)
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
                f"          command: {_q('b64:' + command['code'])}",
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
            f"        command_on: {_q(on['code'])}",
            f"        command_off: {_q(off['code'])}",
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
    remote_entity: str = Query("remote.rm4_mini", max_length=100),
) -> PlainTextResponse:
    entity = remote_entity.strip()
    if not re.fullmatch(r"remote\.[a-z0-9_]+", entity):
        raise HTTPException(status_code=400, detail="Ungültige Entität – erwartet z. B. remote.rm4_mini.")
    with _store_lock:
        data = load_store()
    return PlainTextResponse(build_yaml(data, entity, _format_mac(BROADLINK_MAC)))
