"""Group the LED remotes of Flipper-IRDB into code families for the LED finder.

Remotes whose "Red" button sends the same code belong to one family. The app
sends "On" + "Red" of each family in turn and asks the user what happens.

Usage (from the repository root, with a checkout of Flipper-IRDB that
contains the LED_Lighting folder):
    python3 tools/irdb/build_led_families.py /tmp/irdb broadlink_manager/app/data/led_families.json
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "broadlink_manager" / "app"))
import irdb  # noqa: E402

KEYS = {
    "on": {"on", "power on", "power"},
    "off": {"off", "power off"},
    "red": {"red", "r"},
    "green": {"green", "g"},
    "blue": {"blue", "b"},
}


def main(repo: str, out: str) -> None:
    families = defaultdict(list)
    for path in sorted(Path(repo, "LED_Lighting").rglob("*.ir")):
        signals = irdb.parse_ir(path.read_text(errors="replace"))
        found = {}
        for signal in signals:
            name = signal["name"].strip().lower().replace("_", " ")
            for key, names in KEYS.items():
                if key not in found and name in names:
                    try:
                        found[key] = {"code": irdb.encode_signal(signal), "sig": signal}
                    except (ValueError, KeyError):
                        pass
        if "red" not in found:
            continue
        family_key = found["red"]["code"]
        families[family_key].append((str(path.relative_to(repo)), len(signals), found))

    result = []
    for members in families.values():
        members.sort(key=lambda m: -m[1])
        path, buttons, found = members[0]
        brands = sorted({m[0].split("/")[1].replace("_", " ") for m in members})
        # Use On/Green/Blue from any member that has them
        codes = {}
        for key in KEYS:
            for _p, _n, f in members:
                if key in f:
                    codes[key] = f[key]["code"]
                    break
        result.append({
            "path": path,
            "buttons": buttons,
            "remotes": len(members),
            "brands": brands[:6],
            "codes": codes,
        })
    result.sort(key=lambda f: (-f["remotes"], -f["buttons"]))
    Path(out).write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(result)} LED-Familien → {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
