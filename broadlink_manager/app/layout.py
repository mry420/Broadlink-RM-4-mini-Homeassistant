"""Remote-control layout and state model shared by the app UI and the HA dashboard.

`device_layout()` turns a flat button list into sections (power row, D-pad,
volume/channel rocker, colour palette, groups such as "Out A" …).
`command_effects()` describes how pressing a button changes the remembered
state (power on/off, which input is selected on an output …).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import colors
from smartir import _NAME_HINTS, _SOURCE_PREFIXES, _TOGGLE_HINTS, _norm

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

ICONS = {
    "power": "mdi:power", "on": "mdi:power-on", "off": "mdi:power-off",
    "volumeUp": "mdi:volume-plus", "volumeDown": "mdi:volume-minus", "mute": "mdi:volume-mute",
    "nextChannel": "mdi:chevron-double-up", "previousChannel": "mdi:chevron-double-down",
    "up": "mdi:chevron-up", "down": "mdi:chevron-down", "left": "mdi:chevron-left", "right": "mdi:chevron-right",
    "ok": "mdi:checkbox-blank-circle", "menu": "mdi:menu", "settings": "mdi:cog", "back": "mdi:arrow-u-left-top",
    "home": "mdi:home", "exit": "mdi:exit-to-app", "info": "mdi:information-outline", "source": "mdi:import",
    "play": "mdi:play", "pause": "mdi:pause", "stop": "mdi:stop", "rewind": "mdi:rewind",
    "forward": "mdi:fast-forward", "next": "mdi:skip-next", "prev": "mdi:skip-previous", "record": "mdi:record",
    "brighten": "mdi:brightness-7", "dim": "mdi:brightness-5", "warmer": "mdi:thermometer-plus",
    "colder": "mdi:snowflake", "night": "mdi:weather-night", "digit": "mdi:numeric", "mode": "mdi:auto-fix",
}
_NAME_ICONS = {
    "a↔b": "mdi:swap-horizontal", "a<->b": "mdi:swap-horizontal", "a-b": "mdi:swap-horizontal",
    "arc": "mdi:speaker", "earc": "mdi:speaker", "hdmi arc": "mdi:speaker",
    "pass": "mdi:surround-sound", "passthrough": "mdi:surround-sound",
    "2ch": "mdi:surround-sound-2-0", "5.1ch": "mdi:surround-sound-5-1", "7.1ch": "mdi:surround-sound-7-1",
    "bluetooth": "mdi:bluetooth", "optisch": "mdi:toslink", "optical": "mdi:toslink",
}
DEVICE_ICONS = {
    "tv": "mdi:television", "soundbar": "mdi:soundbar", "ac": "mdi:air-conditioner", "fan": "mdi:fan",
    "light": "mdi:lightbulb", "led": "mdi:led-strip-variant", "projector": "mdi:projector",
    "receiver": "mdi:audio-video", "remote": "mdi:remote",
}
_DIGIT_ICONS = {str(i): f"mdi:numeric-{i}" for i in range(10)}
_GENERIC_SOURCE = {"quelle", "source", "input", "eingang", "quellen", "sources"}
POWER_ROLES = ("power", "on", "off")


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
    special = _NAME_ICONS.get(name.strip().lower())
    if special:
        return special
    if role == "digit":
        return _DIGIT_ICONS.get(re.sub(r"\D", "", name)[-1:], "mdi:numeric")
    return ICONS.get(role, "mdi:remote")


def split_group(name: str) -> Tuple[Optional[str], str]:
    """'Out A: HDMI 1' → ('Out A', 'HDMI 1'); 'Power' → (None, 'Power')."""
    prefix, sep, label = name.partition(":")
    if sep and prefix.strip() and label.strip():
        return prefix.strip(), label.strip()
    return None, name.strip()


def is_led(device: Dict[str, Any]) -> bool:
    commands = device.get("commands", [])
    return device.get("icon") in ("led", "light") or sum(1 for c in commands if colors.command_color(c)) >= 3


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def _item(command: Dict[str, Any], label: Optional[str] = None, role: Optional[str] = None) -> Dict[str, Any]:
    label = label or command["name"]
    role = role or button_role(label)
    return {
        "command_id": command["id"],
        "label": label,
        "role": role,
        "icon": button_icon(label, role),
        "color": colors.command_color(command),
    }


def device_layout(device: Dict[str, Any]) -> Dict[str, Any]:
    """Sections: {kind, title, columns, items, group}. `None` items are blank cells."""
    commands = device.get("commands", [])
    led = is_led(device)
    by_role: Dict[str, List[Dict[str, Any]]] = {}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    palette: List[Dict[str, Any]] = []
    modes: List[Dict[str, Any]] = []

    for command in commands:
        group, label = split_group(command["name"])
        if group:
            groups.setdefault(group, []).append(_item(command, label))
            continue
        if led and colors.command_color(command):
            palette.append(_item(command))
            continue
        if led and colors.is_mode(command["name"]):
            modes.append(_item(command, role="mode"))
            continue
        item = _item(command)
        by_role.setdefault(item["role"], []).append(item)

    def take(role: str) -> Optional[Dict[str, Any]]:
        items = by_role.get(role)
        return items.pop(0) if items else None

    sections: List[Dict[str, Any]] = []

    top = [i for i in (take("power"), take("on"), take("off")) if i] + by_role.pop("power", [])
    generic_source = next((i for i in by_role.get("source", []) if _norm(i["label"]) in _GENERIC_SOURCE), None)
    if generic_source:
        by_role["source"].remove(generic_source)
        top.append(generic_source)
    mute = take("mute")
    if mute:
        top.append(mute)
    if top:
        sections.append({"kind": "row", "title": None, "columns": min(3, len(top)), "items": top})

    nav = {r: take(r) for r in ("up", "down", "left", "right", "ok")}
    if any(nav.values()):
        corners = [i for i in (take("menu"), take("back"), take("home"), take("exit"), take("info"), take("settings")) if i]
        corners += [None] * 4
        cells = [corners[0], nav["up"], corners[1], nav["left"], nav["ok"], nav["right"], corners[2], nav["down"], corners[3]]
        sections.append({"kind": "dpad", "title": None, "columns": 3, "items": cells})

    rocker = [take("volumeUp"), take("nextChannel"), take("volumeDown"), take("previousChannel")]
    if any(rocker):
        if not rocker[1] and not rocker[3]:
            items = [i for i in (rocker[0], rocker[2]) if i]
        elif not rocker[0] and not rocker[2]:
            items = [i for i in (rocker[1], rocker[3]) if i]
        else:
            items = rocker
        sections.append({"kind": "rocker", "title": None, "columns": 2, "items": items})

    light = [i for r in ("brighten", "dim", "warmer", "colder", "night") for i in by_role.pop(r, [])]
    if light:
        sections.append({"kind": "grid", "title": None, "columns": 2 if len(light) == 4 else min(3, len(light)), "items": light})
    if palette:
        sections.append({"kind": "palette", "title": "Farben", "columns": 4, "items": palette})
    if modes:
        sections.append({"kind": "grid", "title": "Effekte", "columns": min(4, len(modes)), "items": modes})

    media = [i for r in ("rewind", "prev", "play", "pause", "stop", "next", "forward", "record") for i in by_role.pop(r, [])]
    if media:
        sections.append({"kind": "grid", "title": None, "columns": min(4, len(media)), "items": media})

    digits = by_role.pop("digit", [])
    if digits:
        by_digit = {re.sub(r"\D", "", i["label"])[-1:]: i for i in digits}
        cells = [by_digit.get(str(n)) for n in range(1, 10)] + [None, by_digit.get("0"), None]
        sections.append({"kind": "digits", "title": None, "columns": 3, "items": cells})

    model = state_model(device)
    for title, items in groups.items():
        if model["groups"].get(title, {}).get("select"):
            for i in items:  # inputs of an output: show them as inputs
                if i["role"] not in POWER_ROLES and i["icon"] == "mdi:remote":
                    i["icon"], i["role"] = "mdi:import", "source"
        switch = [i for i in items if i["role"] in POWER_ROLES]
        others = [i for i in items if i["role"] not in POWER_ROLES]
        sections.append({
            "kind": "group", "title": title, "group": title,
            "switch": switch, "columns": min(4, len(others)) or 1, "items": others,
        })

    sources = by_role.pop("source", [])
    if sources:
        sections.append({"kind": "group", "title": "Eingang", "group": "Eingang", "switch": [],
                         "columns": min(3, len(sources)), "items": sources})

    rest = [i for items in by_role.values() for i in items]
    if rest:
        sections.append({"kind": "grid", "title": "Weitere Tasten", "columns": 3, "items": rest})

    return {"device_id": device["id"], "name": device["name"], "icon": DEVICE_ICONS.get(device.get("icon", ""), "mdi:remote"),
            "sections": sections, "state": model}


# --------------------------------------------------------------------------
# State model (what can be remembered for a device)
# --------------------------------------------------------------------------


# Groups whose buttons are mutually exclusive inputs (the state shows the active one)
_SELECT_GROUP = re.compile(r"\b(out|output|ausgang|eingang|input|quelle|source|zone|hdmi|tv)\b", re.I)


def state_model(device: Dict[str, Any]) -> Dict[str, Any]:
    """{'power': bool, 'groups': {name: {'options': [...], 'power': bool}}}"""
    power = False
    groups: Dict[str, Dict[str, Any]] = {}
    for command in device.get("commands", []):
        group, label = split_group(command["name"])
        role = button_role(label)
        if group:
            entry = groups.setdefault(group, {"options": [], "power": False})
            if role in POWER_ROLES:
                entry["power"] = True
            elif label not in entry["options"]:
                entry["options"].append(label)
        elif role in POWER_ROLES:
            power = True
        elif role == "source" and _norm(label) not in _GENERIC_SOURCE:
            entry = groups.setdefault("Eingang", {"options": [], "power": False})
            if label not in entry["options"]:
                entry["options"].append(label)
    for name, entry in groups.items():
        sources = sum(1 for o in entry["options"] if button_role(o) == "source")
        entry["select"] = len(entry["options"]) >= 2 and (
            bool(_SELECT_GROUP.search(name)) or sources * 2 > len(entry["options"])
        )
    groups = {k: v for k, v in groups.items() if v["select"] or v["power"]}
    return {"power": power, "groups": groups}


def command_effects(device: Dict[str, Any], command: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What pressing this button changes: [{'kind': 'power'|'select', 'group': str|None, 'value': ...}]."""
    model = state_model(device)
    group, label = split_group(command["name"])
    role = button_role(label)
    if not group and role == "source" and "Eingang" in model["groups"] and label in model["groups"]["Eingang"]["options"]:
        group = "Eingang"
    effects: List[Dict[str, Any]] = []
    if role in POWER_ROLES:
        value = {"power": "toggle", "on": True, "off": False}[role]
        if group and model["groups"].get(group, {}).get("power"):
            effects.append({"kind": "power", "group": group, "value": value})
        elif not group and model["power"]:
            effects.append({"kind": "power", "group": None, "value": value})
    elif group and model["groups"].get(group, {}).get("select") and label in model["groups"][group]["options"]:
        effects.append({"kind": "select", "group": group, "value": label})
    return effects
