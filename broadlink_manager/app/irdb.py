"""Open IR code database (Flipper-IRDB, CC0) and IR protocol encoders.

The index of all remote files ships with the app (data/irdb_index.json.gz);
single remote files are downloaded from GitHub on demand and cached. Parsed
signals (NEC, Samsung32, SIRC, RC5, RC6, Kaseikyo, ...) are encoded to
microsecond pulse trains and then to the Broadlink packet format.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("broadlink_manager.irdb")

REPO_RAW = "https://raw.githubusercontent.com/Lucaslhm/Flipper-IRDB/main/"
INDEX_FILE = Path(__file__).resolve().parent / "data" / "irdb_index.json.gz"
BROADLINK_TICK = 32.84  # µs per Broadlink time unit
FINAL_GAP = 109460  # µs, encodes as the usual 0x00 0x0d 0x05 trailer

# --------------------------------------------------------------------------
# Index + search
# --------------------------------------------------------------------------

_index: Optional[Dict[str, Any]] = None
_index_lock = threading.Lock()


def index() -> Dict[str, Any]:
    """{"categories": [{key, label}], "entries": [[cat, brand, model, path, curated]]}"""
    global _index
    with _index_lock:
        if _index is None:
            with gzip.open(INDEX_FILE, "rt", encoding="utf-8") as handle:
                _index = json.load(handle)
        return _index


def categories() -> List[Dict[str, Any]]:
    data = index()
    counts: Dict[int, int] = {}
    for entry in data["entries"]:
        counts[entry[0]] = counts.get(entry[0], 0) + 1
    return [
        {"id": i, "key": c["key"], "label": c["label"], "count": counts.get(i, 0)}
        for i, c in enumerate(data["categories"])
        if counts.get(i)
    ]


def _fold(text: str) -> str:
    text = text.lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("_", " "), ("-", " ")):
        text = text.replace(src, dst)
    return text


def search(query: str, category: Optional[int], limit: int = 200) -> Tuple[List[Dict[str, Any]], int]:
    data = index()
    terms = [t for t in _fold(query).split() if t]
    hits = []
    for cat, brand, model, path, curated in data["entries"]:
        if category is not None and cat != category:
            continue
        if terms:
            hay = _fold(f"{brand} {model}")
            if not all(t in hay for t in terms):
                continue
        hits.append((0 if curated else 1, brand.lower(), model.lower(), cat, brand, model, path, curated))
    hits.sort()
    total = len(hits)
    result = [
        {"category": h[3], "brand": h[4], "model": h[5], "path": h[6], "curated": bool(h[7])}
        for h in hits[:limit]
    ]
    return result, total


def is_known_path(path: str) -> bool:
    return any(entry[3] == path for entry in index()["entries"])


# --------------------------------------------------------------------------
# Download + parse .ir files
# --------------------------------------------------------------------------

_file_cache: Dict[str, List[Dict[str, Any]]] = {}
_file_cache_lock = threading.Lock()
CACHE_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "irdb_cache"


def _cache_path(path: str) -> Path:
    return CACHE_DIR / (re.sub(r"[^A-Za-z0-9._-]+", "_", path)[-180:])


def fetch_text(path: str) -> str:
    cached = _cache_path(path)
    if cached.exists():
        return cached.read_text(encoding="utf-8", errors="replace")
    url = REPO_RAW + urllib.parse.quote(path)
    request = urllib.request.Request(url, headers={"User-Agent": "broadlink-ir-manager"})
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 (fixed https host)
        text = response.read().decode("utf-8", errors="replace")
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return text


def parse_ir(text: str) -> List[Dict[str, Any]]:
    """Parse a Flipper 'IR signals file' into a list of signal dicts."""
    signals: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "name":
            if current:
                signals.append(current)
            current = {"name": value}
        elif current and key in ("type", "protocol", "address", "command", "frequency", "duty_cycle", "data"):
            current[key] = value
    if current:
        signals.append(current)
    return [s for s in signals if s.get("name") and s.get("type")]


def load_remote(path: str) -> List[Dict[str, Any]]:
    with _file_cache_lock:
        if path in _file_cache:
            return _file_cache[path]
    signals = parse_ir(fetch_text(path))
    for signal in signals:
        try:
            signal["code"] = encode_signal(signal)
            signal["supported"] = True
        except (ValueError, KeyError) as err:
            signal["code"] = None
            signal["supported"] = False
            signal["error"] = str(err)
    with _file_cache_lock:
        if len(_file_cache) > 64:
            _file_cache.clear()
        _file_cache[path] = signals
    return signals


# --------------------------------------------------------------------------
# Protocol encoders → microsecond pulses (mark, space, mark, ...)
# --------------------------------------------------------------------------


def _hexint(value: str) -> int:
    """Flipper stores 'address: 08 13 00 00' as little-endian bytes."""
    parts = value.split()
    if not parts:
        raise ValueError("leerer Wert")
    return int.from_bytes(bytes(int(p, 16) for p in parts), "little")


def _bits_lsb(value: int, count: int) -> List[int]:
    return [(value >> i) & 1 for i in range(count)]


def _bits_msb(value: int, count: int) -> List[int]:
    return [(value >> (count - 1 - i)) & 1 for i in range(count)]


def _pulse_distance(bits: List[int], preamble: Tuple[int, int], mark: int, space0: int, space1: int) -> List[int]:
    pulses = [preamble[0], preamble[1]]
    for bit in bits:
        pulses += [mark, space1 if bit else space0]
    pulses.append(mark)  # stop bit
    return pulses


def _pulse_width(bits: List[int], preamble: Tuple[int, int], mark0: int, mark1: int, space: int) -> List[int]:
    pulses = [preamble[0], preamble[1]]
    for i, bit in enumerate(bits):
        pulses.append(mark1 if bit else mark0)
        if i < len(bits) - 1:
            pulses.append(space)
    return pulses


def _manchester(levels: List[Tuple[int, int]]) -> List[int]:
    """levels: [(level, duration)] → merged pulses starting with a mark."""
    merged: List[List[int]] = []
    for level, duration in levels:
        if merged and merged[-1][0] == level:
            merged[-1][1] += duration
        else:
            merged.append([level, duration])
    while merged and merged[0][0] == 0:
        merged.pop(0)
    while merged and merged[-1][0] == 0:
        merged.pop()
    return [d for _, d in merged]


def _repeat(frame: List[int], times: int, period: Optional[int] = None, gap: Optional[int] = None) -> List[int]:
    pulses: List[int] = []
    for i in range(times):
        pulses += frame
        if i < times - 1:
            pause = gap if gap is not None else max(5000, (period or 0) - sum(frame))
            pulses.append(pause)
    return pulses


def encode_parsed(protocol: str, address: int, command: int) -> List[int]:
    p = protocol.strip()
    if p in ("NEC", "NECext", "NEC42", "NEC42ext"):
        if p == "NEC":
            a, c = address & 0xFF, command & 0xFF
            value, count = a | ((~a & 0xFF) << 8) | (c << 16) | ((~c & 0xFF) << 24), 32
        elif p == "NECext":
            value, count = (address & 0xFFFF) | ((command & 0xFFFF) << 16), 32
        elif p == "NEC42":
            a, c = address & 0x1FFF, command & 0xFF
            value, count = a | ((~a & 0x1FFF) << 13) | (c << 26) | ((~c & 0xFF) << 34), 42
        else:
            value, count = (address & 0x3FFFFFF) | ((command & 0xFFFF) << 26), 42
        return _pulse_distance(_bits_lsb(value, count), (9000, 4500), 560, 560, 1690)
    if p == "Samsung32":
        a, c = address & 0xFF, command & 0xFF
        value = a | (a << 8) | (c << 16) | ((~c & 0xFF) << 24)
        return _pulse_distance(_bits_lsb(value, 32), (4500, 4500), 550, 550, 1650)
    if p in ("SIRC", "SIRC15", "SIRC20"):
        count, mask = {"SIRC": (12, 0x1F), "SIRC15": (15, 0xFF), "SIRC20": (20, 0x1FFF)}[p]
        value = (command & 0x7F) | ((address & mask) << 7)
        frame = _pulse_width(_bits_lsb(value, count), (2400, 600), 600, 1200, 600)
        return _repeat(frame, 3, period=45000)
    if p == "Kaseikyo":
        ident = (address >> 24) & 3
        vendor = (address >> 8) & 0xFFFF
        genre1, genre2 = (address >> 4) & 0xF, address & 0xF
        b = [vendor & 0xFF, vendor >> 8, 0, 0, 0, 0]
        parity = b[0] ^ b[1]
        parity = (parity & 0xF) ^ (parity >> 4)
        b[2] = (parity & 0xF) | (genre1 << 4)
        b[3] = (genre2 & 0xF) | ((command & 0xF) << 4)
        b[4] = ((ident << 6) | ((command >> 4) & 0x3F)) & 0xFF
        b[5] = b[2] ^ b[3] ^ b[4]
        value = int.from_bytes(bytes(b), "little")
        return _pulse_distance(_bits_lsb(value, 48), (3456, 1728), 432, 432, 1296)
    if p == "RCA":
        a, c = address & 0xF, command & 0xFF
        value = a | (c << 4) | ((~a & 0xF) << 12) | ((~c & 0xFF) << 16)
        return _pulse_distance(_bits_lsb(value, 24), (4000, 4000), 500, 1000, 2000)
    if p == "Pioneer":
        a, c = address & 0xFF, command & 0xFF
        value = a | ((~a & 0xFF) << 8) | (c << 16) | ((~c & 0xFF) << 24)
        frame = _pulse_distance(_bits_lsb(value, 33), (8500, 4225), 500, 500, 1500)
        return _repeat(frame, 2, gap=26000)
    if p in ("RC5", "RC5X"):
        half = 888
        bits = [1, 1 if p == "RC5" else 0, 0] + _bits_msb(address & 0x1F, 5) + _bits_msb(command & 0x3F, 6)
        levels: List[Tuple[int, int]] = []
        for bit in bits:  # RC5: 1 = space→mark, 0 = mark→space
            levels += [(0, half), (1, half)] if bit else [(1, half), (0, half)]
        return _manchester(levels)
    if p == "RC6":
        half = 444
        levels = [(1, 2666), (0, 889)]
        bits = [(1, half), (0, half), (0, half), (0, half), (0, 2 * half)]  # start, mode 000, toggle
        bits += [(b, half) for b in _bits_msb(address & 0xFF, 8) + _bits_msb(command & 0xFF, 8)]
        for bit, width in bits:  # RC6: 1 = mark→space, 0 = space→mark
            levels += [(1, width), (0, width)] if bit else [(0, width), (1, width)]
        return _manchester(levels)
    raise ValueError(f"Protokoll {protocol} wird nicht unterstützt")


def encode_signal(signal: Dict[str, Any]) -> str:
    """Return a Broadlink Base64 code for a parsed or raw Flipper signal."""
    if signal["type"] == "raw":
        pulses = [abs(int(float(x))) for x in signal["data"].split()]
        frequency = int(float(signal.get("frequency", "38000")))
        if frequency > 50000:
            raise ValueError(f"Trägerfrequenz {frequency // 1000} kHz wird vom RM4 mini nicht unterstützt")
    elif signal["type"] == "parsed":
        pulses = encode_parsed(signal["protocol"], _hexint(signal["address"]), _hexint(signal["command"]))
    else:
        raise ValueError("Unbekannter Signaltyp")
    pulses = [p for p in pulses if p > 0]
    if not pulses:
        raise ValueError("Leeres Signal")
    if len(pulses) % 2:  # ends with a mark → add trailing gap
        pulses.append(FINAL_GAP)
    else:
        pulses[-1] = max(pulses[-1], FINAL_GAP)
    return base64.b64encode(pulses_to_packet(pulses)).decode()


def pulses_to_packet(pulses: List[int]) -> bytes:
    body = bytearray()
    for pulse in pulses:
        ticks = max(1, int(round(pulse / BROADLINK_TICK)))
        if ticks > 255:
            body += bytes([0, (ticks >> 8) & 0xFF, ticks & 0xFF])
        else:
            body.append(ticks)
    return bytes([0x26, 0x00, len(body) & 0xFF, len(body) >> 8]) + bytes(body)


def packet_to_pulses(packet: bytes) -> List[int]:
    """Inverse of pulses_to_packet (used for tests and diagnostics)."""
    length = packet[2] | (packet[3] << 8)
    data, i, out = packet[4 : 4 + length], 0, []
    while i < len(data):
        value = data[i]
        i += 1
        if value == 0:
            value = (data[i] << 8) | data[i + 1]
            i += 2
        out.append(round(value * BROADLINK_TICK))
    return out
