#!/usr/bin/env python3
"""Copy a Zeekr capture directory to a sanitised copy usable as a test fixture.

    python scripts/sanitise_zeekr_capture.py captures/2026-10-19-0930
    python scripts/sanitise_zeekr_capture.py IN_DIR --out OUT_DIR

What is replaced (consistently across all files in the capture, so the same
VIN/ID gets the same placeholder everywhere):
- VINs (by key name and any 17-char VIN-shaped string) -> ZEEKRVIN000000001...
- identifiers (keys ending in "id"/"Id"/"ID", "sn", iccid/imei/imsi, plate)
  -> stable placeholders of the same type (int stays int, str stays str)
- names, nicknames, user/account/owner fields, addresses -> "name-1", ...
- phone/mobile numbers, e-mail addresses -> placeholders
- tokens/secrets/passwords/keys/signatures and any JWT- or long opaque
  token-like string -> "<redacted-token-N>"
- coordinates (keys latitude/longitude/lat/lon/lng, incl. startLatitude ...)
  -> rounded to 2 decimal places (~1 km); type is kept

Other values (SoC, odometer, temperatures, status codes, timestamps) are left
as captured. The output still describes your car's behaviour; review it
before publishing. Only *.json files are copied; anything else is skipped.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")
OPAQUE_RE = re.compile(r"^(?:[Bb]earer\s+)?[A-Za-z0-9+/=_.-]{32,}$")

COORD_KEYS_EXACT = {"lat", "lon", "lng", "latitude", "longitude"}
TOKEN_WORDS = ("token", "secret", "password", "passwd", "signature", "cookie",
               "authorization", "accesskey", "apikey", "privatekey", "publickey", "session")
# matched as key suffix
NAME_WORDS = ("name", "nickname", "username", "user", "account", "owner", "address")
# matched exactly
NAME_EXACT = {"street", "city", "suburb", "postcode", "zipcode", "postalcode"}
# matched as key suffix (avoid substrings such as "satellite")
PHONE_WORDS = ("phone", "mobile", "msisdn", "tel", "telephone", "phonenumber", "mobileno")
EMAIL_WORDS = ("email", "mail")
ID_EXACT = {"sn", "iccid", "imei", "imsi", "plateno", "plate", "platenumber", "licenseplate",
            "license", "engineno", "vehicleno", "deviceno", "serialno", "serialnumber"}
# Fields that end in "id" but are not identifiers.
NOT_ID = {"paid", "valid", "invalid", "fluid", "humid", "hybrid", "android", "void"}
# Display/model names are useful in fixtures and not personal.
KEEP_NAME_KEYS = {"modelname", "seriesname", "brandname", "colorname", "colourname",
                  "configname", "vehiclemodelname", "name_en"}


def _norm(key) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


class Sanitiser:
    def __init__(self):
        self.maps: dict[str, dict] = {}

    def _placeholder(self, kind: str, original):
        table = self.maps.setdefault(kind, {})
        k = (type(original).__name__, original)
        if k not in table:
            n = len(table) + 1
            if kind == "vin":
                table[k] = f"ZEEKRVIN{n:09d}"
            elif isinstance(original, bool):
                table[k] = original
            elif isinstance(original, int):
                table[k] = 1000 + n
            elif isinstance(original, float):
                table[k] = float(1000 + n)
            elif kind == "email":
                table[k] = f"user{n}@example.invalid"
            elif kind == "phone":
                table[k] = f"+610000000{n:02d}"
            elif kind == "token":
                table[k] = f"<redacted-token-{n}>"
            else:
                table[k] = f"{kind}-{n}"
        return table[k]

    # -- classification ---------------------------------------------------
    @staticmethod
    def _key_kind(key) -> str | None:
        k = _norm(key)
        if not k:
            return None
        if k == "vin" or k.endswith("vin") or k.startswith("vin"):
            return "vin"
        if k in COORD_KEYS_EXACT or k.endswith("latitude") or k.endswith("longitude"):
            return "coord"
        if any(w in k for w in TOKEN_WORDS):
            return "token"
        if any(k.endswith(w) for w in EMAIL_WORDS):
            return "email"
        if any(k.endswith(w) for w in PHONE_WORDS):
            return "phone"
        if k in KEEP_NAME_KEYS:
            return None
        if k in NAME_EXACT or any(k.endswith(w) for w in NAME_WORDS):
            return "name"
        if k in ID_EXACT or (k.endswith("id") and k not in NOT_ID) or k.endswith("ids") \
                or k.endswith("uuid"):
            return "id"
        return None

    @staticmethod
    def _round_coord(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        if isinstance(value, str):
            try:
                return f"{round(float(value.strip()), 2):.2f}"
            except ValueError:
                return value
        return value

    def _scrub_key(self, key):
        if isinstance(key, str):
            return VIN_RE.sub(lambda m: self._placeholder("vin", m.group(0)), key)
        return key

    def _scrub_string(self, s: str) -> str:
        if OPAQUE_RE.match(s) or JWT_RE.fullmatch(s):
            return self._placeholder("token", s)
        s = JWT_RE.sub(lambda m: self._placeholder("token", m.group(0)), s)
        s = EMAIL_RE.sub(lambda m: self._placeholder("email", m.group(0)), s)
        s = VIN_RE.sub(lambda m: self._placeholder("vin", m.group(0)), s)
        return s

    def value(self, value, kind=None):
        if isinstance(value, dict):
            return {self._scrub_key(k): self.value(v, self._key_kind(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, kind) for v in value]
        if value is None or isinstance(value, bool):
            return value
        if kind == "coord":
            return self._round_coord(value)
        if kind in ("vin", "token", "email", "phone", "name", "id"):
            if isinstance(value, str) and not value.strip():
                return value
            return self._placeholder(kind, value)
        if isinstance(value, str):
            return self._scrub_string(value)
        return value


def sanitise_dir(src: Path, dst: Path, force: bool = False) -> list[Path]:
    src, dst = Path(src), Path(dst)
    if not src.is_dir():
        raise FileNotFoundError(f"capture directory not found: {src}")
    if dst.exists() and not force:
        raise FileExistsError(f"output already exists: {dst} (use --force)")
    dst.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(dst, 0o700)
    s = Sanitiser()
    # vehicles.json first so VIN placeholders are numbered in list order
    files = sorted(src.glob("*.json"), key=lambda p: (p.name != "vehicles.json", p.name))
    written = []
    for path in files:
        data = json.loads(path.read_text())
        clean = s.value(data)
        out = dst / path.name
        tmp = out.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(clean, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, out)
        os.chmod(out, 0o600)
        written.append(out)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Sanitise a Zeekr capture directory")
    ap.add_argument("capture_dir")
    ap.add_argument("--out", default=None, help="output dir (default: <capture_dir>-sanitised)")
    ap.add_argument("--force", action="store_true", help="overwrite files in an existing output dir")
    args = ap.parse_args(argv)
    src = Path(args.capture_dir.rstrip("/"))
    dst = Path(args.out) if args.out else src.with_name(src.name + "-sanitised")
    try:
        written = sanitise_dir(src, dst, args.force)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Wrote {len(written)} sanitised file(s) to {dst}")
    print("Review the files before sharing: values such as SoC, odometer and timestamps are kept.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
