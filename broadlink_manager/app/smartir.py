"""SmartIR support: map learned buttons to SmartIR functions, build the
device code JSON, install it into Home Assistant and import codes from the
SmartIR community database."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("broadlink_manager.smartir")

SMARTIR_RAW = "https://raw.githubusercontent.com/smartHomeHub/SmartIR/master"
CUSTOM_CODE_START = 90001  # far above the community range (1000–7999)
CATALOG_TTL = 12 * 3600
DEFAULT_BRIGHTNESS_STEPS = 10

# Home Assistant config folder, mounted via `map: homeassistant_config`.
HA_CONFIG_CANDIDATES = [os.environ.get("HA_CONFIG_DIR", ""), "/homeassistant", "/config"]

# --------------------------------------------------------------------------
# Slot definitions (SmartIR command keys) and name matching
# --------------------------------------------------------------------------

DIGIT_SLOTS = [(f"digit{i}", f"Ziffer {i}") for i in range(10)]

TYPES: Dict[str, Dict[str, Any]] = {
    "media_player": {
        "label": "Media Player",
        "hint": "TV, Soundbar, Beamer, Receiver",
        "slots": [
            ("on", "Einschalten"),
            ("off", "Ausschalten"),
            ("volumeUp", "Lauter"),
            ("volumeDown", "Leiser"),
            ("mute", "Stumm"),
            ("nextChannel", "Kanal / Titel +"),
            ("previousChannel", "Kanal / Titel −"),
        ],
        "required": [["on", "off"]],  # at least one of these
        "has_sources": True,
        "has_digits": True,
    },
    "light": {
        "label": "Licht",
        "hint": "LED-Strip, Deckenlampe",
        "slots": [
            ("on", "Einschalten"),
            ("off", "Ausschalten"),
            ("brighten", "Heller"),
            ("dim", "Dunkler"),
            ("warmer", "Wärmer"),
            ("colder", "Kälter"),
            ("night", "Nachtlicht"),
        ],
        "required": [["on"], ["off"]],  # both needed
        "has_sources": False,
        "has_digits": False,
    },
}

_NAME_HINTS: Dict[str, List[str]] = {
    "on": ["an", "ein", "on", "power on", "power an", "einschalten", "anschalten"],
    "off": ["aus", "off", "power off", "power aus", "ausschalten"],
    "volumeUp": ["lauter", "volume up", "vol up", "vol +", "vol+", "volume +", "volume+", "laut +", "lautstaerke +", "lautstärke +"],
    "volumeDown": ["leiser", "volume down", "vol down", "vol -", "vol-", "volume -", "volume-", "laut -", "lautstaerke -", "lautstärke -"],
    "mute": ["mute", "stumm", "ton aus", "stummschalten"],
    "nextChannel": ["kanal +", "kanal+", "kanal hoch", "channel up", "ch +", "ch+", "p +", "p+", "programm +", "naechster", "nächster"],
    "previousChannel": ["kanal -", "kanal−", "kanal −", "kanal-", "kanal runter", "channel down", "ch -", "ch-", "p -", "p-", "programm -", "vorheriger"],
    "brighten": ["heller", "hell", "brighter", "brighten", "hell +", "helligkeit +"],
    "dim": ["dunkler", "dunkel", "dim", "dimmer", "darker", "hell -", "helligkeit -"],
    "warmer": ["waermer", "wärmer", "warm", "warmer", "warmweiss", "warmweiß"],
    "colder": ["kaelter", "kälter", "kalt", "cold", "colder", "cooler", "kaltweiss", "kaltweiß"],
    "night": ["nacht", "nachtlicht", "night", "nightlight", "night light"],
}
_TOGGLE_HINTS = ["power", "power toggle", "toggle", "standby", "on/off", "an/aus", "ein/aus", "an aus", "ein aus"]
_SOURCE_PREFIXES = ("hdmi", "av", "input", "eingang", "quelle", "source", "usb", "scart", "optical",
                    "optisch", "bluetooth", "bt", "aux", "component", "vga", "pc", "line", "coax", "tv ", "arc")


def _norm(name: str) -> str:
    value = name.strip().lower().replace("−", "-").replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*([+-])$", r" \1", value)  # "vol+" -> "vol +"
    return value


def _matches(name: str, hints: List[str]) -> bool:
    norm = _norm(name)
    return norm in {_norm(h) for h in hints}


def suggest_config(device: Dict[str, Any], type_key: str) -> Dict[str, Any]:
    """Guess a slot mapping from the button names of a device."""
    commands = device.get("commands", [])
    slots: Dict[str, str] = {}
    used: set = set()
    for slot, _label in TYPES[type_key]["slots"]:
        for command in commands:
            if command["id"] not in used and _matches(command["name"], _NAME_HINTS.get(slot, [])):
                slots[slot] = command["id"]
                used.add(command["id"])
                break
    toggle = next((c for c in commands if _matches(c["name"], _TOGGLE_HINTS)), None)
    if toggle:
        slots.setdefault("on", toggle["id"])
        slots.setdefault("off", toggle["id"])
        used.add(toggle["id"])

    sources: List[str] = []
    if TYPES[type_key]["has_digits"]:
        for command in commands:
            match = re.fullmatch(r"(?:taste |ziffer |zahl |kanal |channel |nr\.? ?)?([0-9])", _norm(command["name"]))
            if match and f"digit{match.group(1)}" not in slots:
                slots[f"digit{match.group(1)}"] = command["id"]
                used.add(command["id"])
    if TYPES[type_key]["has_sources"]:
        for command in commands:
            if command["id"] not in used and _norm(command["name"]).startswith(_SOURCE_PREFIXES):
                sources.append(command["id"])
    return {"slots": slots, "sources": sources}


def default_type_for_icon(icon: str) -> str:
    return "light" if icon in ("light", "led") else "media_player"


def device_class_for_icon(icon: str) -> str:
    return {"soundbar": "speaker", "receiver": "receiver"}.get(icon, "tv")


def next_free_code(devices: List[Dict[str, Any]]) -> int:
    used = {int((d.get("smartir") or {}).get("code") or 0) for d in devices}
    code = CUSTOM_CODE_START
    while code in used:
        code += 1
    return code


def normalize_config(device: Dict[str, Any], devices: List[Dict[str, Any]], raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a complete, validated SmartIR config for a device."""
    raw = raw or {}
    type_key = raw.get("type") if raw.get("type") in TYPES else default_type_for_icon(device.get("icon", ""))
    ids = {c["id"] for c in device.get("commands", [])}
    valid_slots = {s for s, _ in TYPES[type_key]["slots"]}
    if TYPES[type_key]["has_digits"]:
        valid_slots |= {s for s, _ in DIGIT_SLOTS}
    slots = {k: v for k, v in (raw.get("slots") or {}).items() if k in valid_slots and v in ids}
    sources = [s for s in (raw.get("sources") or []) if s in ids] if TYPES[type_key]["has_sources"] else []
    try:
        code = int(raw.get("code") or 0)
    except (TypeError, ValueError):
        code = 0
    if code <= 0:
        others = [d for d in devices if d["id"] != device["id"]]
        code = next_free_code(others)
    try:
        steps = max(2, min(30, int(raw.get("brightness_steps") or DEFAULT_BRIGHTNESS_STEPS)))
    except (TypeError, ValueError):
        steps = DEFAULT_BRIGHTNESS_STEPS
    return {
        "type": type_key,
        "code": code,
        "slots": slots,
        "sources": list(dict.fromkeys(sources)),
        "brightness_steps": steps,
        "installed": bool(raw.get("installed")),
    }


def missing_required(config: Dict[str, Any]) -> List[str]:
    labels = dict(TYPES[config["type"]]["slots"])
    missing = []
    for group in TYPES[config["type"]]["required"]:
        if not any(slot in config["slots"] for slot in group):
            missing.append(" oder ".join(labels[s] for s in group))
    return missing


# --------------------------------------------------------------------------
# Build SmartIR JSON + YAML
# --------------------------------------------------------------------------


def _code_value(command: Dict[str, Any]) -> Any:
    return command["code"]  # str or list of str (Base64)


def build_device_json(device: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    by_id = {c["id"]: c for c in device.get("commands", [])}
    commands: Dict[str, Any] = {}
    for slot, _label in TYPES[config["type"]]["slots"]:
        if slot in config["slots"]:
            commands[slot] = _code_value(by_id[config["slots"][slot]])

    data: Dict[str, Any] = {
        "manufacturer": device.get("manufacturer") or "Broadlink IR Manager",
        "supportedModels": [device["name"]],
        "supportedController": "Broadlink",
        "commandsEncoding": "Base64",
    }
    if config["type"] == "media_player":
        sources: Dict[str, Any] = {}
        for command_id in config["sources"]:
            command = by_id[command_id]
            sources[command["name"]] = _code_value(command)
        for i in range(10):
            command_id = config["slots"].get(f"digit{i}")
            if command_id:
                sources[f"Channel {i}"] = _code_value(by_id[command_id])
        if sources:
            commands["sources"] = sources
    else:
        steps = config["brightness_steps"]
        # SmartIR uses both lists when stepping brightness, so always provide both.
        data["brightness"] = [round(255 * (i + 1) / steps) for i in range(steps)]
        data["colorTemperature"] = [round(2700 + (6500 - 2700) * i / (steps - 1)) for i in range(steps)]
    data["commands"] = commands
    return data


def build_yaml(device: Dict[str, Any], config: Dict[str, Any], remote_entity: str) -> str:
    platform = config["type"]
    lines = [
        "# In configuration.yaml einfügen (die Zeile 'smartir:' nur einmal),",
        "# danach Home Assistant neu starten.",
        "smartir:",
        "",
        f"{platform}:",
        "  - platform: smartir",
        f"    name: {json.dumps(device['name'], ensure_ascii=False)}",
        f"    unique_id: broadlink_manager_{device['id']}",
        f"    device_code: {config['code']}",
        f"    controller_data: {remote_entity}",
    ]
    if platform == "media_player":
        lines.append(f"    device_class: {device_class_for_icon(device.get('icon', ''))}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Installation into the HA config folder
# --------------------------------------------------------------------------


def ha_config_dir() -> Optional[Path]:
    for candidate in HA_CONFIG_CANDIDATES:
        if candidate and (Path(candidate) / "configuration.yaml").exists():
            return Path(candidate)
    return None


def smartir_dir() -> Optional[Path]:
    config_dir = ha_config_dir()
    if config_dir and (config_dir / "custom_components" / "smartir").is_dir():
        return config_dir / "custom_components" / "smartir"
    return None


def install_state() -> Dict[str, Any]:
    config_dir = ha_config_dir()
    sdir = smartir_dir()
    if not config_dir:
        message = "Kein Zugriff auf den HA-Konfigurationsordner."
    elif not sdir:
        message = "SmartIR ist nicht installiert (in HACS als benutzerdefiniertes Repository hinzufügen)."
    else:
        message = "SmartIR gefunden."
    return {"ha_config": bool(config_dir), "smartir": bool(sdir), "message": message}


def code_file_path(config: Dict[str, Any]) -> Optional[Path]:
    sdir = smartir_dir()
    if not sdir:
        return None
    return sdir / "codes" / config["type"] / f"{config['code']}.json"


def install(device: Dict[str, Any], config: Dict[str, Any]) -> Path:
    path = code_file_path(config)
    if path is None:
        raise RuntimeError(install_state()["message"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(build_device_json(device, config), indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    log.info("SmartIR-Datei geschrieben: %s", path)
    return path


def is_installed(config: Dict[str, Any]) -> bool:
    path = code_file_path(config)
    return bool(path and path.exists())


# --------------------------------------------------------------------------
# Community database (catalog + import)
# --------------------------------------------------------------------------

_catalog_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_catalog_lock = threading.Lock()

_DOC_FILES = {"media_player": "MEDIA_PLAYER.md", "light": "LIGHT.md"}
_ROW_RE = re.compile(r"\[(\d+)\]\(\.\./codes/(\w+)/\d+\.json\)\s*\|\s*([^|]*?)\s*\|\s*([^|\n]*)")


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "broadlink-ir-manager"})
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 (fixed https host)
        return response.read()


def parse_catalog(markdown: str, type_key: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    manufacturer = ""
    for line in markdown.splitlines():
        heading = re.match(r"^#{3,5}\s+(.+?)\s*$", line)
        if heading:
            manufacturer = heading.group(1).strip()
            continue
        row = _ROW_RE.search(line)
        if not row or row.group(2) != type_key:
            continue
        controller = row.group(4).strip()
        if "broadlink" not in controller.lower():
            continue
        models = [m.strip() for m in re.split(r"<br\s*/?>|,", row.group(3)) if m.strip()]
        entries.append({
            "code": int(row.group(1)),
            "manufacturer": manufacturer,
            "models": models,
        })
    return entries


def catalog(type_key: str) -> List[Dict[str, Any]]:
    if type_key not in _DOC_FILES:
        raise ValueError("Unbekannter Typ")
    with _catalog_lock:
        cached = _catalog_cache.get(type_key)
        if cached and time.time() - cached[0] < CATALOG_TTL:
            return cached[1]
    markdown = _http_get(f"{SMARTIR_RAW}/docs/{_DOC_FILES[type_key]}").decode("utf-8", "replace")
    entries = parse_catalog(markdown, type_key)
    with _catalog_lock:
        _catalog_cache[type_key] = (time.time(), entries)
    return entries


def _to_base64(value: str, encoding: str) -> str:
    if encoding == "Hex":
        return base64.b64encode(bytes.fromhex(value.replace(" ", ""))).decode()
    base64.b64decode(value, validate=True)  # validate
    return value


def _convert(value: Any, encoding: str) -> Any:
    if isinstance(value, list):
        return [_to_base64(v, encoding) for v in value]
    return _to_base64(value, encoding)


def fetch_code_file(type_key: str, code: int) -> Dict[str, Any]:
    if type_key not in TYPES:
        raise ValueError("Unbekannter Typ")
    data = json.loads(_http_get(f"{SMARTIR_RAW}/codes/{type_key}/{int(code)}.json"))
    if str(data.get("supportedController", "")).lower() != "broadlink":
        raise ValueError("Dieser Code ist nicht für Broadlink.")
    encoding = data.get("commandsEncoding", "Base64")
    if encoding not in ("Base64", "Hex"):
        raise ValueError(f"Kodierung {encoding} wird nicht unterstützt.")
    return data


_IMPORT_NAMES = {
    "on": "An", "off": "Aus", "volumeUp": "Lauter", "volumeDown": "Leiser", "mute": "Stumm",
    "nextChannel": "Kanal +", "previousChannel": "Kanal −", "brighten": "Heller", "dim": "Dunkler",
    "warmer": "Wärmer", "colder": "Kälter", "night": "Nachtlicht",
}


def commands_from_code_file(type_key: str, data: Dict[str, Any]) -> Tuple[List[Tuple[str, Any, Optional[str], bool]], int]:
    """Return [(name, code, slot, is_source)] and the brightness step count."""
    encoding = data.get("commandsEncoding", "Base64")
    result: List[Tuple[str, Any, Optional[str], bool]] = []
    commands = data.get("commands") or {}
    for slot, _label in TYPES[type_key]["slots"]:
        value = commands.get(slot)
        if value:
            result.append((_IMPORT_NAMES[slot], _convert(value, encoding), slot, False))
    if type_key == "media_player":
        for source, value in (commands.get("sources") or {}).items():
            if not value:
                continue
            digit = re.fullmatch(r"Channel ([0-9])", source)
            if digit:
                result.append((digit.group(1), _convert(value, encoding), f"digit{digit.group(1)}", False))
            else:
                result.append((source, _convert(value, encoding), None, True))
    steps = len(data.get("brightness") or []) or DEFAULT_BRIGHTNESS_STEPS
    return result, steps
