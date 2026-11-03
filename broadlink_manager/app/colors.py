"""Colour model for LED/RGB remotes: palette, name → colour guess and roles."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# key: (German label, hex for the UI, Home Assistant tile colour)
PALETTE: Dict[str, Dict[str, str]] = {
    "red": {"label": "Rot", "hex": "#f44336", "ha": "red"},
    "dark_orange": {"label": "Rotorange", "hex": "#ff5722", "ha": "deep-orange"},
    "orange": {"label": "Orange", "hex": "#ff9800", "ha": "orange"},
    "amber": {"label": "Bernstein", "hex": "#ffc107", "ha": "amber"},
    "yellow": {"label": "Gelb", "hex": "#ffeb3b", "ha": "yellow"},
    "lime": {"label": "Hellgrün", "hex": "#8bc34a", "ha": "light-green"},
    "green": {"label": "Grün", "hex": "#4caf50", "ha": "green"},
    "teal": {"label": "Türkis", "hex": "#009688", "ha": "teal"},
    "cyan": {"label": "Cyan", "hex": "#00bcd4", "ha": "cyan"},
    "light_blue": {"label": "Hellblau", "hex": "#03a9f4", "ha": "light-blue"},
    "blue": {"label": "Blau", "hex": "#2196f3", "ha": "blue"},
    "indigo": {"label": "Dunkelblau", "hex": "#3f51b5", "ha": "indigo"},
    "purple": {"label": "Lila", "hex": "#9c27b0", "ha": "purple"},
    "magenta": {"label": "Magenta", "hex": "#d81b60", "ha": "deep-purple"},
    "pink": {"label": "Pink", "hex": "#e91e63", "ha": "pink"},
    "white": {"label": "Weiß", "hex": "#ffffff", "ha": "white"},
    "warm_white": {"label": "Warmweiß", "hex": "#ffd59a", "ha": "amber"},
}

_NAME_COLORS: Dict[str, str] = {
    "red": "red", "rot": "red", "r": "red",
    "green": "green", "gruen": "green", "grün": "green", "g": "green",
    "blue": "blue", "blau": "blue", "b": "blue",
    "white": "white", "weiss": "white", "weiß": "white", "w": "white", "cool white": "white", "kaltweiss": "white", "kaltweiß": "white",
    "warm": "warm_white", "warm white": "warm_white", "warmweiss": "warm_white", "warmweiß": "warm_white",
    "yellow": "yellow", "gelb": "yellow",
    "orange": "orange", "dark orange": "dark_orange", "dunkelorange": "dark_orange",
    "amber": "amber", "bernstein": "amber", "light orange": "amber",
    "pink": "pink", "rosa": "pink",
    "purple": "purple", "lila": "purple", "violet": "purple", "violett": "purple",
    "magenta": "magenta",
    "cyan": "cyan", "tuerkis": "teal", "türkis": "teal", "turquoise": "teal", "teal": "teal", "aqua": "cyan",
    "light blue": "light_blue", "hellblau": "light_blue", "sky blue": "light_blue",
    "dark blue": "indigo", "dunkelblau": "indigo", "indigo": "indigo",
    "light green": "lime", "hellgruen": "lime", "hellgrün": "lime", "lime": "lime",
}

# Usual colours of the shade buttons on 24/44-key RGB remotes (R1–R4, G1–G4, B1–B4).
_SHADES: Dict[str, List[str]] = {
    "r": ["dark_orange", "orange", "amber", "yellow"],
    "g": ["lime", "teal", "cyan", "light_blue"],
    "b": ["indigo", "purple", "magenta", "pink"],
}


def _norm(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", name.strip().lower())


def guess_color(name: str) -> Optional[str]:
    norm = _norm(name)
    if norm in _NAME_COLORS:
        return _NAME_COLORS[norm]
    shade = re.fullmatch(r"([rgb])\s?([1-4])", norm)
    if shade:
        return _SHADES[shade.group(1)][int(shade.group(2)) - 1]
    for key, value in _NAME_COLORS.items():
        if len(key) > 2 and re.fullmatch(rf"{re.escape(key)}\s?[0-9]?", norm):
            return value
    # Compound names such as "Light Red", "Dark Blue", "Hell Grün"
    words = norm.split()
    light = any(w in ("light", "hell", "pale") for w in words)
    dark = any(w in ("dark", "dunkel", "deep") for w in words)
    for word in words:
        base = _NAME_COLORS.get(word) if len(word) > 2 else None
        if not base:
            continue
        if light:
            return {"blue": "light_blue", "green": "lime", "red": "dark_orange", "orange": "amber"}.get(base, base)
        if dark:
            return {"blue": "indigo", "orange": "dark_orange", "green": "teal", "purple": "magenta"}.get(base, base)
        return base
    return None


def command_color(command: Dict) -> Optional[str]:
    """Explicit colour from the colour check, else a guess from the name."""
    if "color" in command:
        return command["color"] if command["color"] in PALETTE else None
    return guess_color(command["name"])


LED_MODE_NAMES = {"flash", "strobe", "fade", "smooth", "jump", "jump3", "jump7", "fade3", "fade7", "auto", "mode",
                  "quick", "slow", "speed", "speed up", "speed down", "music", "sound", "diy1", "diy2", "diy3",
                  "blinken", "blitz", "überblenden", "ueberblenden", "sanft", "modus", "schneller", "langsamer"}


def is_mode(name: str) -> bool:
    return _norm(name) in LED_MODE_NAMES
