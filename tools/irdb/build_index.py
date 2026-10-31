"""Build the compact Flipper-IRDB index shipped with the app.

Usage (from the repository root):
    git clone --depth 1 --filter=blob:none --no-checkout https://github.com/Lucaslhm/Flipper-IRDB.git /tmp/irdb
    python3 tools/irdb/build_index.py /tmp/irdb broadlink_manager/app/data/irdb_index.json.gz

Only the file list is read (git ls-tree), no file contents are needed.
"""

import gzip
import json
import subprocess
import sys

CATEGORIES = [
    ("tv", "Fernseher", ["TVs", "Monitors", "Touchscreen_Displays", "Digital_Signs", "Universal_TV_Remotes"], ["TV", "Monitor"]),
    ("projector", "Beamer", ["Projectors", "Whiteboards"], ["Video_Projector", "Projector"]),
    ("audio", "Soundbar & Audio", ["SoundBars", "Speakers", "Audio_and_Video_Receivers", "CD_Players", "Multimedia", "Head_Units", "Car_Multimedia"],
     ["Receiver", "Pre-Amplifier", "Surround_Processor", "Amplifier", "CD_Player", "Tuner", "Lifestyle", "Audio", "Speaker", "Soundbar"]),
    ("video", "Player & Receiver", ["Blu-Ray", "DVD_Players", "VCR", "Cable_Boxes", "Streaming_Devices", "DVB-T", "TV_Tuner", "Laserdisc", "Consoles"],
     ["DVD_Player", "VCR", "Satellite", "Cable_Box", "DSS", "Blu-Ray", "Media_Player", "Laserdisc"]),
    ("light", "LED & Licht", ["LED_Lighting"], ["Lighting", "Light"]),
    ("ac", "Klimaanlage", ["ACs"], ["Air_Conditioner", "AC"]),
    ("fan", "Ventilator", ["Fans"], ["Fan"]),
    ("climate", "Heizen & Luft", ["Heaters", "Air_Purifiers", "Humidifiers", "Fireplaces"], ["Heater", "Air_Purifier"]),
    ("other", "Sonstiges", [], []),
]


def main(repo: str, out: str) -> None:
    paths = subprocess.run(
        ["git", "-C", repo, "ls-tree", "-r", "--name-only", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    by_dir = {d: i for i, c in enumerate(CATEGORIES) for d in c[2]}
    by_type = {t.lower(): i for i, c in enumerate(CATEGORIES) for t in c[3]}
    other = len(CATEGORIES) - 1
    entries = []
    for path in paths:
        if not path.endswith(".ir"):
            continue
        parts = path[:-3].split("/")
        if parts[0] == "_Converted_":
            source = parts[1] if len(parts) > 1 else ""
            if source == "CSV" and len(parts) == 6:
                brand, model = parts[3], f"{parts[4].replace('_', ' ')} {parts[5]}"
                category = by_type.get(parts[4].lower(), other)
            elif source in ("IR_Plus", "Pronto") and len(parts) == 5:
                brand, model, category = parts[3], parts[4], other
            else:
                continue
            curated = 0
        else:
            if len(parts) < 3:
                continue
            category = by_dir.get(parts[0], other)
            brand, model = parts[1], " / ".join(parts[2:])
            curated = 1
        brand = brand.replace("_", " ").strip()
        model = model.replace("_", " ").strip()
        entries.append([category, brand, model, path, curated])
    entries.sort(key=lambda e: (-e[4], e[1].lower(), e[2].lower()))
    data = {
        "source": "https://github.com/Lucaslhm/Flipper-IRDB (CC0 1.0)",
        "categories": [{"key": c[0], "label": c[1]} for c in CATEGORIES],
        "entries": entries,
    }
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(entries)} Einträge → {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
