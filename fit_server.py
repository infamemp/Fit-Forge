#!/usr/bin/env python3
"""
FIT Forge Server — local server that connects the web interface with the merge engine.
VERSION: beta-2026-06-10 — preserve developer_id (DeveloperDataId field 0) so Garmin
         Connect resolves CIQ developer fields; read _developer_id in parse_dev_data;
         propagate through combined_defs; corrected native_mesg_num/native_field_num
         variable labels in parse+write path.

Usage:
    python fit_server.py

Then open in your browser: http://localhost:7331
"""

import sys, os, json, tempfile, threading, webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ── Dependency check ──────────────────────────────────────────────────────────
def check_deps():
    missing = []
    try: from garmin_fit_sdk import Decoder
    except ImportError: missing.append("garmin-fit-sdk")
    try: from fit_tool.fit_file_builder import FitFileBuilder
    except ImportError: missing.append("fit-tool")
    if missing:
        print(f"\n❌ Missing dependencies: {', '.join(missing)}")
        print(f"\nInstall with:\n  pip install {' '.join(missing)}\n")
        sys.exit(1)

check_deps()

# Verify fit_writer version
try:
    import fit_writer as _fw_check
    _ver = [l for l in (_fw_check.__doc__ or '').splitlines() if 'VERSION' in l]
    print(f"  fit_writer: {_ver[0] if _ver else 'unknown version'}")
except Exception as _e:
    print(f"  ⚠️  fit_writer.py not found in the same folder: {_e}")
    import sys; sys.exit(1)

from garmin_fit_sdk import Decoder, Stream
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.hrv_message import HrvMessage
from fit_tool.profile.profile_type import (
    FileType, Manufacturer, Sport, SubSport,
    Event, EventType, Activity, LapTrigger, SessionTrigger
)

FIT_EPOCH = 631065600
PORT = 7331

# ── Field categories for the UI ───────────────────────────────────────────────
FIELD_META = {
    "position_lat":             {"icon":"📍","cat":"GPS",         "desc":"GPS Latitude"},
    "position_long":            {"icon":"📍","cat":"GPS",         "desc":"GPS Longitude"},
    "altitude":                 {"icon":"⛰️", "cat":"Elevation",  "desc":"Altitude (m)"},
    "enhanced_altitude":        {"icon":"⛰️", "cat":"Elevation",  "desc":"Enhanced Altitude (m)"},
    "grade":                    {"icon":"📐","cat":"Elevation",   "desc":"Grade (%)"},
    "heart_rate":               {"icon":"❤️", "cat":"Physiology", "desc":"Heart Rate (bpm)"},
    "temperature":              {"icon":"🌡️", "cat":"Physiology", "desc":"Temperature (°C)"},
    "enhanced_respiration_rate":{"icon":"💨","cat":"Physiology",  "desc":"Respiration Rate"},
    "cadence":                  {"icon":"🔄","cat":"Performance", "desc":"Cadence (rpm)"},
    "fractional_cadence":       {"icon":"🔄","cat":"Performance", "desc":"Fractional Cadence"},
    "power":                    {"icon":"💪","cat":"Performance", "desc":"Power (W)"},
    "accumulated_power":        {"icon":"💪","cat":"Performance", "desc":"Accumulated Power"},
    "speed":                    {"icon":"⚡","cat":"Performance", "desc":"Speed (m/s)"},
    "enhanced_speed":           {"icon":"⚡","cat":"Performance", "desc":"Enhanced Speed"},
    "distance":                 {"icon":"📏","cat":"Performance", "desc":"Distance (m)"},
    "left_right_balance":       {"icon":"⚖️", "cat":"Pedaling",  "desc":"L/R Balance"},
    "left_torque_effectiveness":{"icon":"🔧","cat":"Pedaling",   "desc":"Left Torque Effectiveness"},
    "right_torque_effectiveness":{"icon":"🔧","cat":"Pedaling",  "desc":"Right Torque Effectiveness"},
    "left_pedal_smoothness":    {"icon":"🔁","cat":"Pedaling",   "desc":"Left Pedal Smoothness"},
    "right_pedal_smoothness":   {"icon":"🔁","cat":"Pedaling",   "desc":"Right Pedal Smoothness"},
    "left_pco":                 {"icon":"📌","cat":"Pedaling",   "desc":"Left PCO"},
    "right_pco":                {"icon":"📌","cat":"Pedaling",   "desc":"Right PCO"},
    "left_power_phase":         {"icon":"〰️","cat":"Pedaling",   "desc":"Left Power Phase"},
    "left_power_phase_peak":    {"icon":"〰️","cat":"Pedaling",   "desc":"Left Power Phase Peak"},
    "right_power_phase":        {"icon":"〰️","cat":"Pedaling",   "desc":"Right Power Phase"},
    "right_power_phase_peak":   {"icon":"〰️","cat":"Pedaling",   "desc":"Right Power Phase Peak"},
    "compressed_accumulated_power": {"icon":"💪","cat":"Performance","desc":"Compressed Accumulated Power"},
    "cycles":                   {"icon":"🔄","cat":"Performance", "desc":"Cycles"},
    "vertical_oscillation":     {"icon":"↕️", "cat":"Running",   "desc":"Vertical Oscillation"},
    "stance_time":              {"icon":"🦶","cat":"Running",    "desc":"Ground Contact Time (ms)"},
    "stance_time_percent":      {"icon":"🦶","cat":"Running",    "desc":"Ground Contact Time %"},
    # Garmin undocumented record fields — identified by field number
    "performance_condition":    {"icon":"📈","cat":"Physiology", "desc":"Performance Condition"},
    "vo2max_estimate":          {"icon":"🫁","cat":"Physiology", "desc":"VO2max Estimate"},
    "aerobic_training_effect":  {"icon":"🫀","cat":"Physiology", "desc":"Aerobic Training Effect"},
    "anaerobic_training_effect":{"icon":"⚡","cat":"Physiology", "desc":"Anaerobic Training Effect"},
}

KNOWN_SDK_RECORD_FIELD_NUMS = {0,1,2,3,4,5,6,7,9,13,29,30,43,44,45,46,53,67,68,69,70,71,72,73,78,108}
EXTRA_RECORD_FIELD_META = {
    90:  {"name":"performance_condition",           "icon":"📈","cat":"Physiology","desc":"Performance Condition"},
    # Garmin Edge 5xx/8xx/1040 undocumented record fields (confirmed via FIT SDK field IDs)
    137: {"name":"vo2max_estimate",                 "icon":"🫁","cat":"Physiology","desc":"VO2max Estimate"},
    138: {"name":"aerobic_training_effect",         "icon":"🫀","cat":"Physiology","desc":"Aerobic Training Effect"},
    144: {"name":"anaerobic_training_effect",       "icon":"⚡","cat":"Physiology","desc":"Anaerobic Training Effect"},
}

# Fields that should default to device_source when both files have data.
# These are physical sensor measurements where the dedicated device (Garmin)
# is more reliable than a virtual estimate (Zwift).
# The user can still override any of these in the Conflict Resolution UI.
DEVICE_SOURCE_DEFAULT_FIELDS = {
    'left_right_balance',
    'left_torque_effectiveness', 'right_torque_effectiveness',
    'left_pedal_smoothness', 'right_pedal_smoothness',
    'left_pco', 'right_pco',
    'left_power_phase', 'left_power_phase_peak',
    'right_power_phase', 'right_power_phase_peak',
    'temperature',
}

def _extra_record_field_name(field_num):
    meta = EXTRA_RECORD_FIELD_META.get(field_num, {})
    return meta.get("name", f"std_field_{field_num}")

def _extra_record_field_meta(field_num):
    meta = dict(EXTRA_RECORD_FIELD_META.get(field_num, {}))
    if not meta:
        meta = {"icon":"🧬","cat":"Extra record","desc":f"Record field {field_num}"}
    meta.setdefault("name", _extra_record_field_name(field_num))
    return meta

def parse_standard_record_data(path):
    """Parse raw standard record fields directly from the FIT binary."""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    hs = data[0]
    ds = struct.unpack_from("<I", data, 4)[0]
    end = hs + ds
    pos = hs
    ldefs = {}
    defs = {}
    ts_vals = {}
    while pos < end and pos < len(data):
        rh = data[pos]; pos += 1
        if rh & 0x40 and not (rh & 0x80):
            is_dev = bool(rh & 0x20)
            ln = rh & 0x0F
            pos += 1
            arch = data[pos]; pos += 1
            le = arch == 0
            gmn = struct.unpack_from("<H" if le else ">H", data, pos)[0]; pos += 2
            nf = data[pos]; pos += 1
            fields = []
            for _ in range(nf):
                fields.append({"num": data[pos], "size": data[pos+1], "base": data[pos+2] & 0x1F})
                pos += 3
            dev_fields = []
            if is_dev:
                nd = data[pos]; pos += 1
                for _ in range(nd):
                    dev_fields.append({"num": data[pos], "size": data[pos+1], "dev_idx": data[pos+2]})
                    pos += 3
            total = sum(f["size"] for f in fields) + sum(f["size"] for f in dev_fields)
            ldefs[ln] = {"gmn": gmn, "fields": fields, "dev_fields": dev_fields, "total": total, "le": le}
        elif rh & 0x80:
            ln = (rh >> 5) & 0x03
            defd = ldefs.get(ln)
            if not defd:
                break
            pos += defd["total"]
        else:
            ln = rh & 0x0F
            defd = ldefs.get(ln)
            if not defd:
                break
            gmn = defd["gmn"]
            if gmn == 20:
                fpos = pos
                ts = None
                row = {}
                for f in defd["fields"]:
                    raw = data[fpos:fpos+f["size"]]
                    row[f["num"]] = raw
                    if f["num"] == 253 and len(raw) >= 4:
                        ts = struct.unpack_from("<I" if defd["le"] else ">I", raw, 0)[0]
                    defs[f["num"]] = {"size": f["size"], "base": f["base"]}
                    fpos += f["size"]
                if ts is not None:
                    ts_vals.setdefault(ts, {}).update(row)
            pos += defd["total"]
    return {"defs": defs, "ts_vals": ts_vals}

def _decode_std_field_sample(field_num, raw, base):
    import struct
    if raw is None:
        return None
    try:
        if field_num == 90 and len(raw) >= 1 and raw[0] != 0xFF:
            return int(raw[0]) - 100
        if base in (0x88, 8) and len(raw) >= 4:
            v = struct.unpack('<f', raw[:4])[0]
            return None if v != v else round(v, 4)
        if base in (0x85, 5) and len(raw) >= 4:
            return struct.unpack('<i', raw[:4])[0]
        if base in (0x86, 6) and len(raw) >= 4:
            return struct.unpack('<I', raw[:4])[0]
        if base in (0x83, 3) and len(raw) >= 2:
            return struct.unpack('<h', raw[:2])[0]
        if base in (0x84, 4) and len(raw) >= 2:
            return struct.unpack('<H', raw[:2])[0]
        if base in (0x01, 1) and len(raw) >= 1:
            return struct.unpack('<b', raw[:1])[0]
        if len(raw) >= 1:
            return raw[0]
    except Exception:
        return None
    return None

def get_extra_record_fields(path):
    info = parse_standard_record_data(path)
    defs = info.get('defs') or {}
    ts_vals = info.get('ts_vals') or {}
    # Sentinel (invalid) values by field size — Zwift fills all extra fields with these
    _SENTINELS = {
        1: {0xFF, 0x7F},
        2: {0xFFFF, 0x7FFF, 32767},
        4: {0xFFFFFFFF, 0x7FFFFFFF, 2147483647, 4294967295},
    }
    out = []
    for fn, meta in sorted(defs.items()):
        if fn in KNOWN_SDK_RECORD_FIELD_NUMS or fn == 253:
            continue
        sz = meta.get('size', 1)
        sentinels = _SENTINELS.get(sz, set())
        sample = None
        count = 0
        for ts, row in ts_vals.items():
            if fn not in row:
                continue
            raw = row[fn]
            # Skip records where the entire raw value is 0xFF bytes (invalid)
            if raw == b'\xff' * sz:
                continue
            v = _decode_std_field_sample(fn, raw, meta.get('base', 0))
            if v is None or v in sentinels:
                continue
            count += 1
            if sample is None:
                sample = v
        field_meta = _extra_record_field_meta(fn)
        out.append({
            "field_num": fn,
            "name": field_meta.get("name", _extra_record_field_name(fn)),
            "sample_value": sample,
            "count": count,
            "size": sz,
            "base": meta.get("base"),
        })
    return out

# ── Core merge logic (same as fit_merge.py) ───────────────────────────────────
def load_fit(path):
    s = Stream.from_file(path)
    d = Decoder(s)
    msgs, _ = d.read()
    return msgs

def get_fields(msgs):
    # SDK invalid sentinel values — fields where every record has one of these
    # are Zwift/legacy placeholder fields with no real data (e.g. compressed_accumulated_power, cycles)
    _SDK_INVALID = {255, 65535, 2147483647, 4294967295}
    fields = set()
    field_vals = {}  # field_name -> set of seen values (capped at 10 for performance)
    for r in msgs.get("record_mesgs", []):
        if not isinstance(r, dict):
            continue
        for k, v in r.items():
            if k == "timestamp" or not isinstance(k, str) or v is None:
                continue
            if k == "developer_fields":
                continue
            if k not in field_vals:
                field_vals[k] = set()
            if len(field_vals[k]) < 10:
                try:
                    field_vals[k].add(int(v))
                except (TypeError, ValueError):
                    field_vals[k].add(str(v))
    for k, vals in field_vals.items():
        # Only include the field if it has at least one non-invalid value
        if vals - _SDK_INVALID:
            fields.add(k)
    return sorted(fields)

def _first_dict(seq, default=None):
    if default is None:
        default = {}
    if not seq:
        return default
    for item in seq:
        if isinstance(item, dict):
            return item
    return default

# Known developer app IDs → friendly names
_DEV_APP_NAMES = {
    "62696b6574657272612e636f6d": "Biketerra",
    "1a69b10a1d314afea32f6a579ae20d9f": "HRV4Training",
    "a49d1978f19b4a8eaff0e9423d173178": "W'Balance CIQ",
    "5d8f2e4a1c7b3f9e0d6a2b4c8e1f5a3d": "Stryd",
    "be2ee39529f849708e05c2bbb4b71050": "W'bal CIQ",  # wbal (kJ absolute) companion to W'Balance CIQ
}

SENSITIVE_DEV_APP_IDS = {
    "1a69b10a1d314afea32f6a579ae20d9f",  # HRV4Training / AlphaHRV-like CIQ fields
}


# Per-app field exclusion table.
# Fields listed here are OFF by default in the UI — the user must opt in consciously.
# They are NOT silently dropped: they appear in the dev fields list with an info badge.
#
# Format: { app_id_hex: { field_name_lowercase, ... } }
#
# Biketerra "power2" / "x_power2_0_0":
#   Second independent power source recorded by Biketerra (e.g. a separate power meter).
#   Included in the merge as a proper developer field — does NOT interfere with the
#   primary Garmin power channel. Off by default so the user chooses to include it.
_EXCLUDED_DEV_FIELDS_BY_APP = {
    "62696b6574657272612e636f6d": {"power2", "x_power2_0_0"},  # Biketerra
}

# Info badges shown in the UI alongside specific dev fields.
# key = (app_id_hex, field_name_lowercase)
_DEV_FIELD_WARNINGS = {
    ("62696b6574657272612e636f6d", "power2"):
        "Second independent power source — useful for comparing meters or as backup. Off by default.",
    ("62696b6574657272612e636f6d", "x_power2_0_0"):
        "Second independent power source — useful for comparing meters or as backup. Off by default.",
}

def _is_excluded_dev_field(app_id, name):
    """Return True if this field is excluded by default for this app.
    Uses exact name match (case-insensitive) — never a substring check.
    app_id comparison is case-insensitive and ignores trailing zero-bytes."""
    normalized_id = str(app_id or "").strip().lower().rstrip("0") or str(app_id or "").strip().lower()
    for table_id, excluded_names in _EXCLUDED_DEV_FIELDS_BY_APP.items():
        t = table_id.lower().rstrip("0") or table_id.lower()
        if normalized_id == t or str(app_id or "").strip().lower() == table_id.lower():
            return str(name or "").strip().lower() in excluded_names
    return False

def _dev_field_warning(app_id, name):
    """Return a warning string if this field has a known risk, else None."""
    normalized_id = str(app_id or "").strip().lower()
    normalized_name = str(name or "").strip().lower()
    # Check direct match first
    result = _DEV_FIELD_WARNINGS.get((normalized_id, normalized_name))
    if result:
        return result
    # Check with trailing zeros stripped from app_id
    stripped_id = normalized_id.rstrip("0")
    return _DEV_FIELD_WARNINGS.get((stripped_id, normalized_name))

def parse_dev_data(path):
    """Parse developer-field metadata and raw values directly from a FIT file."""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    hs = data[0]
    ds = struct.unpack_from("<I", data, 4)[0]
    end = hs + ds
    pos = hs
    ldefs = {}
    defs = {}
    ts_vals = {}
    base_sizes = {0:1,1:1,2:1,3:2,4:2,5:4,6:4,7:1,8:4,9:8,10:1,11:2,12:4,13:1,
                  0x83:2,0x84:2,0x85:4,0x86:4,0x88:4,0x89:8}
    while pos < end and pos < len(data):
        rh = data[pos]; pos += 1
        if rh & 0x40 and not (rh & 0x80):
            is_dev = bool(rh & 0x20)
            ln = rh & 0x0F
            pos += 1
            arch = data[pos]; pos += 1
            le = arch == 0
            gmn = struct.unpack_from("<H" if le else ">H", data, pos)[0]; pos += 2
            nf = data[pos]; pos += 1
            fields = []
            for _ in range(nf):
                fields.append({"num": data[pos], "size": data[pos+1], "base": data[pos+2] & 0x1F})
                pos += 3
            dvf = []
            if is_dev:
                nd = data[pos]; pos += 1
                for _ in range(nd):
                    dvf.append({"num": data[pos], "size": data[pos+1], "dev_idx": data[pos+2]})
                    pos += 3
            total = sum(f["size"] for f in fields) + sum(f["size"] for f in dvf)
            ldefs[ln] = {"gmn": gmn, "fields": fields, "dev_fields": dvf, "total": total, "le": le}
        elif rh & 0x80:
            ln = (rh >> 5) & 0x03
            defd = ldefs.get(ln)
            if not defd:
                break
            pos += defd["total"]
        else:
            ln = rh & 0x0F
            defd = ldefs.get(ln)
            if not defd:
                break
            gmn = defd["gmn"]
            if gmn == 207:
                fpos = pos; row = {}
                for f in defd["fields"]:
                    row[f["num"]] = data[fpos:fpos+f["size"]]
                    fpos += f["size"]
                di = row.get(3, b"\x00")[0] if row.get(3) else 0
                entry = defs.setdefault(di, {})
                # field 0 = developer_id (developer's Garmin account UUID, byte[16])
                # MUST be preserved — Garmin Connect (post-2025) uses it to resolve the
                # developer registration and display CIQ fields.  Writing all-0xFF causes
                # the fields to be silently dropped.
                entry['_developer_id'] = row.get(0, b'\xff'*16).hex()
                # field 1 = application_id (CIQ app UUID, byte[16])
                entry['_app_id'] = row.get(1, b'\xff'*16).hex()
                entry['_application_version'] = struct.unpack_from('<I' if defd['le'] else '>I', row.get(4, b'\xff\xff\xff\xff')[:4])[0] if row.get(4) and len(row.get(4, b'')) >= 4 else None
            elif gmn == 206:
                fpos = pos; row = {}
                for f in defd["fields"]:
                    row[f["num"]] = data[fpos:fpos+f["size"]]
                    fpos += f["size"]
                di = row.get(0, b"\x00")[0]
                fdn = row.get(1, b"\x00")[0]
                fname = _decode_fit_str(row.get(3, b''))
                units = _decode_fit_str(row.get(8, b''))
                base = row.get(2, b'\x00')[0]
                desc_size = (row.get(6, b'\xff')[0] if row.get(6) else 0xFF)
                sz = desc_size if desc_size != 0xFF else base_sizes.get(base, 4)
                _nfr = row.get(14)
                native_mesg_num = struct.unpack_from('<H' if defd['le'] else '>H', _nfr[:2])[0] if _nfr and len(_nfr) >= 2 else None
                if native_mesg_num == 0xFFFF: native_mesg_num = None
                native_field_num = row.get(15, b'\xff')[0] if row.get(15) else None
                if native_field_num == 0xFF:
                    native_field_num = None
                developer_id = row.get(13)
                developer_id_num = struct.unpack_from('<H' if defd['le'] else '>H', developer_id[:2])[0] if developer_id and len(developer_id) >= 2 else None
                if developer_id_num == 0xFFFF:
                    developer_id_num = None
                # Field 7 = scale (sint8). Some Garmin layouts omit explicit offset.
                scale_raw = row.get(7, b'\x7f')
                scale_val = None
                if scale_raw and len(scale_raw) >= 1:
                    sv = struct.unpack('<b', scale_raw[:1])[0]
                    if sv != 0x7F:
                        scale_val = sv
                offset_raw = row.get(9, b'\x7f')
                offset_val = None
                if offset_raw and len(offset_raw) >= 1:
                    ov = struct.unpack('<b', offset_raw[:1])[0]
                    if ov != 0x7F:
                        offset_val = ov
                defs.setdefault(di, {})[fdn] = {
                    "name": fname, "units": units, "size": sz, "record_size": sz, "desc_size": desc_size, "base": base,
                    "native_field_num": native_field_num, "native_mesg_num": native_mesg_num,
                    "developer_id": developer_id_num, "scale": scale_val, "offset": offset_val
                }
            elif gmn == 20 and defd.get("dev_fields"):
                fpos = pos
                ts = None
                src_le = defd["le"]
                for f in defd["fields"]:
                    if f["num"] == 253:
                        ts = struct.unpack_from("<I" if src_le else ">I", data, fpos)[0]
                    fpos += f["size"]
                devvals = {}
                for df in defd["dev_fields"]:
                    di = df["dev_idx"]; fn = df["num"]
                    defs.setdefault(di, {}).setdefault(fn, {"name": f"dev{di}_{fn}", "units": "", "base": 0x88, "size": df["size"]})
                    defs[di][fn]["record_size"] = df["size"]
                    if "size" not in defs[di][fn] or defs[di][fn].get("size") in (None, 0xFF):
                        defs[di][fn]["size"] = df["size"]
                    raw_bytes = data[fpos:fpos+df["size"]]
                    # If source is BE, swap bytes to LE for our LE output file
                    if not src_le and len(raw_bytes) > 1:
                        base_type = defs[di][fn].get("base", 0x88)
                        elem_size = {0x83:2, 0x84:2, 3:2, 4:2, 0x85:4, 0x86:4, 5:4, 6:4, 0x88:4, 8:4, 0x89:8, 9:8}.get(base_type, 0)
                        if elem_size >= 2 and len(raw_bytes) >= elem_size:
                            swapped = bytearray()
                            for offset in range(0, len(raw_bytes), elem_size):
                                chunk = raw_bytes[offset:offset+elem_size]
                                if len(chunk) == elem_size:
                                    swapped.extend(chunk[::-1])
                                else:
                                    swapped.extend(chunk)
                            raw_bytes = bytes(swapped)
                    devvals[(di, fn)] = raw_bytes
                    fpos += df["size"]
                if ts is not None:
                    ts_vals.setdefault(ts, {}).update(devvals)
            pos += defd["total"]
    return {"defs": defs, "ts_vals": ts_vals}


def _decode_dev_sample(raw, base):
    import struct
    if raw is None:
        return None
    try:
        if base in (0x88, 8):
            v = struct.unpack('<f', raw[:4])[0]
            return None if v != v else round(v, 4)
        if base in (0x85, 5):
            return struct.unpack('<i', raw[:4])[0]
        if base in (0x86, 6):
            return struct.unpack('<I', raw[:4])[0]
        if base in (0x83, 3):
            return struct.unpack('<h', raw[:2])[0]
        if base in (0x84, 4):
            return struct.unpack('<H', raw[:2])[0]
        if base in (0x01, 1):
            return struct.unpack('<b', raw[:1])[0]
        if base in (0x02, 2, 0x00, 0):
            return raw[0]
    except Exception:
        return None
    return None


def get_dev_fields(path, msgs=None):
    info = parse_dev_data(path)
    defs = info.get("defs") or {}
    ts_vals = info.get("ts_vals") or {}
    seen = []
    for di, fields in defs.items():
        if not isinstance(di, int) or not isinstance(fields, dict):
            continue
        app_id = fields.get("_app_id", "")
        if not isinstance(app_id, str):
            app_id = ""
        app_name = _DEV_APP_NAMES.get(app_id, _DEV_APP_NAMES.get(app_id.rstrip("0"), f"App dev{di}"))
        for fn, meta in fields.items():
            if (isinstance(fn, str) and fn.startswith('_')) or not isinstance(meta, dict):
                continue
            name = meta.get('name', f'dev{di}_{fn}')
            excluded_by_default = _is_excluded_dev_field(app_id, name)
            warning = _dev_field_warning(app_id, name)
            key = f"dev:{di}:{name}:fn{fn}"
            sample = None
            for _ts, ts_map in ts_vals.items():
                if not isinstance(ts_map, dict):
                    continue
                raw = ts_map.get((di, fn))
                if raw is not None:
                    sample = _decode_dev_sample(raw, meta.get("base", 0x88))
                    if sample is not None:
                        break
            count = sum(1 for _ts, ts_map in ts_vals.items() if isinstance(ts_map, dict) and (di, fn) in ts_map)
            seen.append({
                "key": key,
                "name": meta.get("name", f"dev{di}_{fn}"),
                "app_name": app_name,
                "app_id": app_id,
                "units": meta.get("units", ""),
                "sample_value": sample,
                "count": count,
                "developer_data_index": di,
                "field_def_num": fn,
                "excluded_by_default": excluded_by_default,
                "warning": warning,
            })
    return sorted(seen, key=lambda x: (x["app_name"], x["name"]))

def read_utc_offset(path):
    import struct
    try:
        with open(path, "rb") as f: data = f.read()
        pos = 14; end = 14 + struct.unpack_from("<I", data, 4)[0]; ldefs = {}
        def rv(d, p, bt, le=True):
            try:
                if bt in (6,12): return struct.unpack_from("<I" if le else ">I", d, p)[0]
                if bt in (4,11): return struct.unpack_from("<H" if le else ">H", d, p)[0]
                if bt in (0,2,10,13): return d[p]
            except: return None
        while pos < end:
            rh = data[pos]; pos += 1
            if rh & 0x40:
                is_dev = bool(rh & 0x20); ln = rh & 0x0F; pos += 1
                arch = data[pos]; pos += 1; le = arch == 0
                gmn = struct.unpack_from("<H" if le else ">H", data, pos)[0]; pos += 2
                nf = data[pos]; pos += 1; flds = []
                for _ in range(nf): flds.append({"defNum":data[pos],"size":data[pos+1],"baseType":data[pos+2]&0x1F}); pos += 3
                df = []
                if is_dev:
                    nd = data[pos]; pos += 1
                    for _ in range(nd): df.append({"size":data[pos+1]}); pos += 3
                total = sum(f["size"] for f in flds) + sum(f["size"] for f in df)
                ldefs[ln] = {"gmn":gmn,"fields":flds,"le":le,"total_size":total}
            elif rh & 0x80:
                ln = (rh>>5)&0x03; defd = ldefs.get(ln)
                if not defd: break
                pos += defd["total_size"]
            else:
                ln = rh & 0x0F; defd = ldefs.get(ln)
                if not defd: break
                if defd["gmn"] == 34:
                    fpos = pos; act_ts = local_ts = None
                    for f in defd["fields"]:
                        v = rv(data, fpos, f["baseType"], defd["le"])
                        if f["defNum"] == 253: act_ts = v
                        if f["defNum"] == 5 and f["size"] == 4: local_ts = v
                        fpos += f["size"]
                    if act_ts and local_ts and local_ts != 0xFFFFFFFF:
                        return int(local_ts) - int(act_ts)
                fpos = pos
                for f in defd["fields"]:
                    if f["defNum"] == 253: break
                    fpos += f["size"]
                pos += defd["total_size"]
    except Exception: pass
    return None

def set_rec_field(rec_msg, name, value):
    if value is None: return
    try:
        if name == "position_lat":
            rec_msg.position_lat = int(value)
        elif name == "position_long":
            rec_msg.position_long = int(value)
        elif name in ("altitude","enhanced_altitude"):
            rec_msg.enhanced_altitude = value; rec_msg.altitude = value
        else:
            setattr(rec_msg, name, value)
    except Exception: pass



def _decode_fit_str(raw):
    if not raw:
        return ''
    return raw.split(b"\x00")[0].decode('utf-8', 'replace')

def _u16le(raw, default=None):
    import struct
    if not raw or len(raw) < 2:
        return default
    return struct.unpack('<H', raw[:2])[0]

def _u32le(raw, default=None):
    import struct
    if not raw or len(raw) < 4:
        return default
    return struct.unpack('<I', raw[:4])[0]

def parse_device_metadata(path):
    """Extract device/file identity metadata to preserve source device info."""
    import struct
    with open(path, 'rb') as f:
        data = f.read()
    hs = data[0]
    ds = struct.unpack_from('<I', data, 4)[0]
    end = hs + ds
    pos = hs
    ldefs = {}
    file_id = None; file_id_le = True
    device_info = None; device_info_le = True
    activity = None; activity_le = True
    while pos < end and pos < len(data):
        rh = data[pos]; pos += 1
        if rh & 0x40 and not (rh & 0x80):
            is_dev = bool(rh & 0x20)
            ln = rh & 0x0F
            pos += 1
            arch = data[pos]; pos += 1
            le = arch == 0
            gmn = struct.unpack_from('<H' if le else '>H', data, pos)[0]; pos += 2
            nf = data[pos]; pos += 1
            fields = []
            for _ in range(nf):
                fields.append((data[pos], data[pos+1], data[pos+2] & 0x1F))
                pos += 3
            dev_fields = []
            if is_dev:
                nd = data[pos]; pos += 1
                for _ in range(nd):
                    dev_fields.append((data[pos], data[pos+1], data[pos+2]))
                    pos += 3
            total = sum(sz for _, sz, _ in fields) + sum(sz for _, sz, _ in dev_fields)
            ldefs[ln] = {'gmn': gmn, 'fields': fields, 'total': total, 'le': le}
        elif rh & 0x80:
            ln = (rh >> 5) & 0x03
            defd = ldefs.get(ln)
            if not defd:
                break
            pos += defd['total']
        else:
            ln = rh & 0x0F
            defd = ldefs.get(ln)
            if not defd:
                break
            row = {}
            for fn, sz, bt in defd['fields']:
                row[fn] = data[pos:pos+sz]
                pos += sz
            if defd['gmn'] == 0 and file_id is None:
                file_id = row; file_id_le = defd['le']
            elif defd['gmn'] == 23 and device_info is None:
                device_info = row; device_info_le = defd['le']
            elif defd['gmn'] == 34 and activity is None:
                activity = row; activity_le = defd['le']

    def _u16(raw, default=None, le=True):
        if not raw or len(raw) < 2: return default
        return struct.unpack('<H' if le else '>H', raw[:2])[0]
    def _u32(raw, default=None, le=True):
        if not raw or len(raw) < 4: return default
        return struct.unpack('<I' if le else '>I', raw[:4])[0]

    file_id = file_id or {}
    device_info = device_info or {}
    product_name = _decode_fit_str(file_id.get(8) or device_info.get(27) or b'')
    descriptor = _decode_fit_str(device_info.get(19) or b'Primary Device') or 'Primary Device'
    software_raw = _u16(device_info.get(5), None, device_info_le)
    software_version = None if software_raw in (None, 0xFFFF) else round(float(software_raw) / 100.0, 2)
    meta = {
        'manufacturer': _u16(file_id.get(1), None, file_id_le) or _u16(device_info.get(2), 1, device_info_le) or 1,
        'product': _u16(file_id.get(2), None, file_id_le) or _u16(device_info.get(4), 0, device_info_le) or 0,
        'serial_number': _u32(file_id.get(3), None, file_id_le) or _u32(device_info.get(3), None, device_info_le),
        'file_number': _u16(file_id.get(5), 0, file_id_le),
        'product_name': product_name,
        'device_index': (device_info.get(0) or b'\x00')[0] if device_info.get(0) else 0,
        'software_version': software_version,
        'descriptor': descriptor,
        'source_type': (device_info.get(25) or b'\x05')[0] if device_info.get(25) else 5,
        'local_timestamp_offset': None,
    }
    if activity and 253 in activity and 5 in activity and len(activity[253]) >= 4 and len(activity[5]) >= 4:
        act_ts = _u32(activity[253], None, activity_le)
        local_ts = _u32(activity[5], None, activity_le)
        if act_ts is not None and local_ts not in (None, 0xFFFFFFFF):
            meta['local_timestamp_offset'] = int(local_ts) - int(act_ts)
    return meta

def parse_all_device_info(path):
    """Extract ALL device_info messages from source FIT file for pass-through."""
    import struct
    with open(path, 'rb') as f:
        data = f.read()
    hs = data[0]
    ds = struct.unpack_from('<I', data, 4)[0]
    end = hs + ds
    pos = hs
    ldefs = {}
    devices = []
    while pos < end and pos < len(data):
        rh = data[pos]; pos += 1
        if rh & 0x40 and not (rh & 0x80):
            is_dev = bool(rh & 0x20)
            ln = rh & 0x0F; pos += 1
            arch = data[pos]; pos += 1; le = arch == 0
            gmn = struct.unpack_from('<H' if le else '>H', data, pos)[0]; pos += 2
            nf = data[pos]; pos += 1; fields = []
            for _ in range(nf):
                fields.append((data[pos], data[pos+1], data[pos+2] & 0x1F)); pos += 3
            dev_fields = []
            if is_dev:
                nd = data[pos]; pos += 1
                for _ in range(nd): dev_fields.append((data[pos], data[pos+1], data[pos+2])); pos += 3
            total = sum(sz for _, sz, _ in fields) + sum(sz for _, sz, _ in dev_fields)
            ldefs[ln] = {'gmn': gmn, 'fields': fields, 'total': total, 'le': le}
        elif rh & 0x80:
            ln = (rh >> 5) & 0x03
            defd = ldefs.get(ln)
            if not defd: break
            pos += defd['total']
        else:
            ln = rh & 0x0F
            defd = ldefs.get(ln)
            if not defd: break
            row = {}
            for fn, sz, bt in defd['fields']:
                row[fn] = data[pos:pos+sz]; pos += sz
            if defd['gmn'] == 23:  # device_info
                le = defd['le']
                def _u16d(raw, default=None):
                    if not raw or len(raw) < 2: return default
                    return struct.unpack('<H' if le else '>H', raw[:2])[0]
                def _u32d(raw, default=None):
                    if not raw or len(raw) < 4: return default
                    return struct.unpack('<I' if le else '>I', raw[:4])[0]
                dev = {
                    'device_index': row.get(0, b'\x00')[0] if row.get(0) else 0,
                    'device_type': row.get(1, b'\xff')[0] if row.get(1) else None,
                    'manufacturer': _u16d(row.get(2), 1),
                    'serial_number': _u32d(row.get(3), None),
                    'product': _u16d(row.get(4), 0),
                    'software_version': None,
                    'descriptor': _decode_fit_str(row.get(19, b'')),
                    'product_name': _decode_fit_str(row.get(27, b'')),
                    'source_type': row.get(25, b'\x05')[0] if row.get(25) else 5,
                    'battery_voltage': None,
                    'battery_status': None,
                    'ant_device_number': _u16d(row.get(21), None),
                    'ant_transmission_type': row.get(22, b'\xff')[0] if row.get(22) else None,
                    'ant_network': row.get(20, b'\xff')[0] if row.get(20) else None,
                }
                sw_raw = _u16d(row.get(5), None)
                if sw_raw not in (None, 0xFFFF):
                    dev['software_version'] = round(float(sw_raw) / 100.0, 2)
                # battery voltage: field 10, uint16, scale 256
                bv_raw = _u16d(row.get(10), None)
                if bv_raw not in (None, 0xFFFF):
                    dev['battery_voltage'] = round(float(bv_raw) / 256.0, 2)
                # battery status: field 11, uint8
                bs_raw = row.get(11, b'\xff')[0] if row.get(11) else None
                if bs_raw not in (None, 0xFF):
                    dev['battery_status'] = bs_raw
                devices.append(dev)
            else:
                # skip non-device_info, already advanced pos
                pass
    return devices

def parse_user_profile_and_zones(msgs):
    """Extract user_profile and zones_target from already-decoded SDK messages."""
    user = {}
    zones = {}
    up = _first_dict(msgs.get('user_profile_mesgs', []), {})
    if up:
        # gender: SDK returns string 'male'/'female', we need enum 0=female, 1=male
        gender_str = up.get('gender')
        gender_val = None
        if gender_str == 'female': gender_val = 0
        elif gender_str == 'male': gender_val = 1
        user = {
            'friendly_name': str(up.get('friendly_name') or up.get(67) or ''),
            'gender': gender_val,
            'age': _safe_int(up.get('age')),
            'height_m': _safe_float(up.get('height')),
            'weight_kg': _safe_float(up.get('weight')),
            'resting_hr': _safe_int(up.get('resting_heart_rate')),
            'max_hr': _safe_int(up.get('default_max_heart_rate')),
            'activity_class': _safe_int(up.get('activity_class')),
        }
    zt = _first_dict(msgs.get('zones_target_mesgs', []), {})
    if zt:
        zones = {
            'ftp': _safe_int(zt.get('functional_threshold_power')),
            'max_hr': _safe_int(zt.get('max_heart_rate')),
            'threshold_hr': _safe_int(zt.get('threshold_heart_rate')),
        }
    return user, zones

def _safe_int(v):
    if v is None: return None
    try: return int(v)
    except: return None

def _safe_float(v):
    if v is None: return None
    try: return float(v)
    except: return None

def do_merge(path1, path2, selected_fields, conflicts, utc_offset, include_hrv, selected_dev_keys=None, device_source="f1", time_source="f2", force_virtual=False, trim_to_shorter=False, f1_start_offset=0):
    import bisect
    import datetime as _dt
    import statistics as _stats
    import struct as _struct
    import math

    f1 = load_fit(path1) or {}; f2 = load_fit(path2) or {}
    r1 = f1.get("record_mesgs",[]) or []
    r2 = f2.get("record_mesgs",[]) or []
    if not r1 or not r2:
        raise ValueError("One of the files does not contain record_mesgs")

    def _ts_sec(ts):
        if isinstance(ts, (int, float)):
            v = int(ts)
            return v if v < 1_000_000_000 else v - FIT_EPOCH
        if hasattr(ts, 'tzinfo'):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            else:
                ts = ts.astimezone(_dt.timezone.utc)
            _fit_epoch = _dt.datetime(1989, 12, 31, 0, 0, 0, tzinfo=_dt.timezone.utc)
            return int((ts - _fit_epoch).total_seconds())
        raise TypeError(f"Unsupported timestamp type: {type(ts)}")

    def _shift_record(rec, shift_s):
        out = dict(rec)
        ts = rec.get('timestamp')
        if isinstance(ts, (int, float)):
            out['timestamp'] = int(ts) + shift_s
        else:
            out['timestamp'] = ts + _dt.timedelta(seconds=shift_s)
        return out

    def _corr(xs, ys):
        if len(xs) < 8:
            return None
        try:
            mx = _stats.mean(xs); my = _stats.mean(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            syy = sum((y - my) ** 2 for y in ys)
            if sxx <= 0 or syy <= 0:
                return None
            num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            return num / ((sxx * syy) ** 0.5)
        except Exception:
            return None

    def _first_diffs(vals):
        return [vals[i] - vals[i - 1] for i in range(1, len(vals))]

    def _score_shift(shift_s):
        candidate_fields = [
            'power', 'heart_rate', 'cadence', 'enhanced_speed', 'speed',
            'enhanced_altitude', 'altitude', 'temperature'
        ]
        score = 0.0
        used = 0
        for field in candidate_fields:
            seq1 = []
            seq2 = []
            for rec in r1:
                v1 = rec.get(field)
                if v1 is None:
                    continue
                t2 = _ts_sec(rec['timestamp']) - shift_s
                # file2 shifted by shift_s means original file2 second must be t2
                # exact second match first; nearest second is handled later during merge
                # alignment search stays strict to avoid smearing the score
                v2 = map2_raw.get(t2, {}).get(field)
                if v2 is None:
                    continue
                try:
                    seq1.append(float(v1))
                    seq2.append(float(v2))
                except Exception:
                    continue
            if len(seq1) < 30:
                continue
            c_val = _corr(seq1, seq2)
            d1 = _first_diffs(seq1)
            d2 = _first_diffs(seq2)
            c_diff = _corr(d1, d2) if len(d1) >= 8 else None
            field_score = None
            if c_val is not None and c_diff is not None:
                field_score = 0.35 * c_val + 0.65 * c_diff
            elif c_diff is not None:
                field_score = c_diff
            elif c_val is not None:
                field_score = c_val
            if field_score is None:
                continue
            # prioritize fields with clearer dynamics and more overlap
            weight = min(3.0, len(seq1) / 400.0)
            if field == 'power':
                weight *= 2.5
            elif field in ('heart_rate', 'cadence'):
                weight *= 1.5
            score += field_score * weight
            used += 1
        if not used:
            return None
        return score / used

    # Build raw timestamp maps first
    map1 = {_ts_sec(r['timestamp']): r for r in r1 if 'timestamp' in r}
    map2_raw = {_ts_sec(r['timestamp']): r for r in r2 if 'timestamp' in r}

    # ── Auto-orient: GPS file is always the base timeline ─────────────────────
    # The base timeline must come from whichever file has GPS (Zwift), so the
    # merged route, distance and session start_time are always correct regardless
    # of whether the user uploaded Zwift as F1 or F2.
    # This swap happens FIRST — before alignment, ts2_sorted, and all downstream
    # logic — so everything operates on correctly-oriented data.
    def _file_has_gps(records_map):
        for r in records_map.values():
            lat = r.get('position_lat')
            if lat is not None and lat not in (0, 2147483647, -2147483648):
                return True
        return False

    _f1_has_gps = _file_has_gps(map1)
    _f2_has_gps = _file_has_gps(map2_raw)
    _swapped = False

    if not _f1_has_gps and _f2_has_gps:
        # F1=Garmin (no GPS), F2=Zwift (GPS) — swap so GPS file drives the timeline
        map1, map2_raw = map2_raw, map1
        r1,  r2        = r2,  r1
        f1,  f2        = f2,  f1
        path1, path2   = path2, path1
        _swapped = True
        # Invert device_source, time_source, and conflict slot references
        def _invert(src):
            if   str(src).lower() == 'f1': return 'f2'
            elif str(src).lower() == 'f2': return 'f1'
            return src
        device_source = _invert(device_source)
        time_source   = _invert(time_source)
        conflicts = {k: (_invert(v) if v in ('f1','f2') else v)
                     for k, v in conflicts.items()}

    # GPS start offset: skip the first N seconds of the GPS-bearing file so the track
    # starts at the correct position on a loop course when merging a partial session
    # that began mid-way through a virtual route (e.g. Zwift loop).
    # After the swap above, map1 is always the GPS file — apply offset directly to map1.
    # offset=0 = default behavior unchanged.
    if f1_start_offset and f1_start_offset > 0:
        f1_min_ts = min(map1.keys())
        map1 = {ts: r for ts, r in map1.items() if ts >= f1_min_ts + f1_start_offset}

    start_delta = min(map1.keys()) - min(map2_raw.keys())
    search_radius = 30 if abs(start_delta) <= 60 else 90
    lo = start_delta - search_radius
    hi = start_delta + search_radius
    best_shift = start_delta
    best_score = None
    for shift_s in range(lo, hi + 1):
        s = _score_shift(shift_s)
        if s is None:
            continue
        if best_score is None or s > best_score:
            best_score = s
            best_shift = shift_s

    # Shift complementary file to align to base timeline
    r2_aligned = [_shift_record(r, best_shift) for r in r2]
    map2 = {_ts_sec(r['timestamp']): r for r in r2_aligned if 'timestamp' in r}
    ts2_sorted = sorted(map2.keys())

    def _nearest_rec(ts, tol=1):
        if not ts2_sorted:
            return {}
        idx = bisect.bisect_left(ts2_sorted, ts)
        candidates = []
        if idx < len(ts2_sorted):
            candidates.append(ts2_sorted[idx])
        if idx > 0:
            candidates.append(ts2_sorted[idx - 1])
        if not candidates:
            return {}
        best_ts = min(candidates, key=lambda x: (abs(x - ts), x))
        if abs(best_ts - ts) > tol:
            return {}
        return map2.get(best_ts, {})

    # Detect sport from source sessions — prefer device_source
    _ses1 = _first_dict(f1.get("session_mesgs", []), {})
    _ses2 = _first_dict(f2.get("session_mesgs", []), {})
    if str(device_source).lower() == 'f2':
        _sport = _ses2.get("sport") or _ses1.get("sport") or 2
        _sub_sport = _ses2.get("sub_sport") or _ses1.get("sub_sport") or 0
    else:
        _sport = _ses1.get("sport") or _ses2.get("sport") or 2
        _sub_sport = _ses1.get("sub_sport") or _ses2.get("sub_sport") or 0
    _SPORT_MAP = {"running":1,"cycling":2,"swimming":5,"walking":11,"hiking":17,
                  "generic":0,"transition":3,"fitness_equipment":4,"training":10}
    _SUB_SPORT_MAP = {
        "generic":0,"treadmill":1,"street":2,"trail":3,"track":4,"spin":5,
        "indoor_cycling":6,"road":7,"mountain":8,"downhill":9,"recumbent":10,
        "cyclocross":11,"hand_cycling":12,"track_cycling":13,"indoor_rowing":14,
        "elliptical":15,"stair_climbing":16,"lap_swimming":17,"open_water":18,
        "flexibility_training":19,"strength_training":20,"warm_up":21,"match":22,
        "exercise":23,"challenge":24,"indoor_skiing":25,"cardio_training":26,
        "indoor_walking":27,"e_bike_fitness":28,"bmx":29,"casual_walking":30,
        "speed_walking":31,"bike_to_run_transition":32,"run_to_bike_transition":33,
        "swim_to_bike_transition":34,"atv":35,"motocross":36,"backcountry":37,
        "resort":38,"rc_drone":39,"wingsuit":40,"whitewater":41,"skate_skiing":42,
        "yoga":43,"pilates":44,"indoor_running":45,"gravel_cycling":46,
        "e_bike_mountain":47,"commuting":48,"mixed_surface":49,"navigate":50,
        "track_me":51,"map":52,"single_gas_diving":53,"multi_gas_diving":54,
        "gauge_diving":55,"apnea_diving":56,"apnea_hunting":57,"virtual_activity":58,
        "obstacle":59,"breathing":62,"sail_race":65,"ultra":67,"indoor_climbing":68,
        "bouldering":69,"hiit":70,"amrap":73,"emom":74,"tabata":75,
    }
    if isinstance(_sport, str): _sport = _SPORT_MAP.get(_sport.lower(), 2)
    if isinstance(_sub_sport, str): _sub_sport = _SUB_SPORT_MAP.get(_sub_sport.lower(), 0)
    try: _sport = int(_sport)
    except: _sport = 2
    try: _sub_sport = int(_sub_sport)
    except: _sub_sport = 0

    # Optional override: mark merged workout as Garmin Virtual Activity
    # This avoids Connect classifying it only as indoor and mapping location from GPS.
    if force_virtual:
        _sub_sport = 58  # virtual_activity

    # Preserve standing/seated time from source sessions (prefer device_source)
    def _float_or_none(v):
        try: return float(v) if v is not None else None
        except: return None
    def _int_or_none(v):
        try: return int(v) if v is not None else None
        except: return None
    # Resolve time_standing / stand_count.
    # Use `is not None` checks instead of `or` to preserve valid zero values.
    # Default to 0 when neither source provides the field so the merged file
    # always reports seated/standing time (Garmin Connect expects this).
    def _pick_float(primary, fallback, key):
        v = primary.get(key)
        if v is not None: return _float_or_none(v)
        v = fallback.get(key)
        if v is not None: return _float_or_none(v)
        return 0.0   # default: zero standing time

    def _pick_int(primary, fallback, key):
        v = primary.get(key)
        if v is not None: return _int_or_none(v)
        v = fallback.get(key)
        if v is not None: return _int_or_none(v)
        return 0      # default: zero stand count

    if str(device_source).lower() == 'f2':
        _time_standing = _pick_float(_ses2, _ses1, "time_standing")
        _stand_count   = _pick_int(_ses2, _ses1, "stand_count")
    else:
        _time_standing = _pick_float(_ses1, _ses2, "time_standing")
        _stand_count   = _pick_int(_ses1, _ses2, "stand_count")

    # Base file timeline is authoritative. File2 is matched by nearest aligned second.
    all_ts = sorted(map1.keys())

    # Trim to shorter file: if enabled, cut F1's timeline at F2's last timestamp.
    # This ensures every record in the merge has complete data from both sources,
    # avoiding a tail of records where F2 has no data at all.
    if trim_to_shorter and ts2_sorted:
        f2_end = max(ts2_sorted)
        all_ts = [ts for ts in all_ts if ts <= f2_end]

    merged = []
    for ts in all_ts:
        a = map1.get(ts, {})
        b = _nearest_rec(ts, tol=1)
        rec = {"timestamp": a.get('timestamp', ts)}
        for k, v in b.items():
            if k != "timestamp" and v is not None and k in selected_fields:
                rec[k] = v
        for k, v in a.items():
            if k != "timestamp" and v is not None and k in selected_fields:
                if k in rec:
                    # Fields in DEVICE_SOURCE_DEFAULT_FIELDS default to device_source
                    # so physical sensor data wins over virtual estimates.
                    # The user can still override via the Conflict Resolution UI.
                    _default = device_source if k in DEVICE_SOURCE_DEFAULT_FIELDS else "f1"
                    strat = conflicts.get(k, _default)
                    if strat == "f1":
                        rec[k] = v
                    elif strat == "avg":
                        try:
                            rec[k] = round((float(rec[k]) + float(v)) / 2)
                        except Exception:
                            rec[k] = v
                else:
                    rec[k] = v
        merged.append(rec)

    # Keep merged record/GPS timeline anchored to File 1 (Biketerra/GPS).
    # Do NOT shift record timestamps: this was advancing valid GPS ~18 s too early
    # and breaking Garmin Connect route rendering.
    record_time_shift = 0

    # Normalize cumulative distance for Garmin Connect web summary.
    def _sc_to_deg(v):
        try:
            return float(v) * 180.0 / 2147483648.0
        except Exception:
            return None

    def _haversine_m(lat1, lon1, lat2, lon2):
        R = 6371000.0
        p1 = math.radians(lat1); p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
        a = math.sin(dp/2.0)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2.0)**2
        return 2.0 * R * math.asin(min(1.0, math.sqrt(a)))

    def _ts_float(ts):
        try:
            return float(_ts_sec(ts))
        except Exception:
            return None

    raw_distances = []
    for r in merged:
        dv = r.get("distance")
        if dv is None:
            continue
        try:
            raw_distances.append(float(dv))
        except Exception:
            pass

    # Distance offset normalization: when f1_start_offset > 0, the GPS file
    # (Zwift) has accumulated distance from its own start, not from the merge start.
    # Subtract the first distance value so the merged file always starts at 0.
    if f1_start_offset and f1_start_offset > 0 and raw_distances:
        _dist_offset = raw_distances[0]
        if _dist_offset > 1.0:  # only normalize if there's a meaningful offset
            for r in merged:
                dv = r.get("distance")
                if dv is not None:
                    try:
                        r["distance"] = max(0.0, float(dv) - _dist_offset)
                    except Exception:
                        pass
            raw_distances = [max(0.0, d - _dist_offset) for d in raw_distances]

    need_distance_rebuild = False
    if not raw_distances:
        need_distance_rebuild = True
    else:
        if max(raw_distances) <= 1.0:
            need_distance_rebuild = True
        else:
            monotonic_hits = sum(1 for i in range(1, len(raw_distances)) if raw_distances[i] >= raw_distances[i-1])
            if len(raw_distances) > 4 and monotonic_hits < int(0.80 * (len(raw_distances)-1)):
                need_distance_rebuild = True

    if need_distance_rebuild:
        cum_dist = 0.0
        prev = None
        for r in merged:
            lat = r.get("position_lat")
            lon = r.get("position_long")
            tsf = _ts_float(r.get("timestamp"))
            if lat is None or lon is None or tsf is None:
                r["distance"] = cum_dist
                continue
            latd = _sc_to_deg(lat); lond = _sc_to_deg(lon)
            if latd is None or lond is None:
                r["distance"] = cum_dist
                continue
            if prev is not None:
                plat, plon, pts = prev
                step = _haversine_m(plat, plon, latd, lond)
                dt_s = max(0.0, tsf - pts)
                if dt_s <= 0 or (step / max(dt_s, 1e-6) > 35.0):
                    step = 0.0
                cum_dist += step
            r["distance"] = cum_dist
            prev = (latd, lond, tsf)

    start_dt = merged[0]["timestamp"]; end_dt = merged[-1]["timestamp"]
    if isinstance(start_dt, int):
        elapsed = float(end_dt - start_dt)
    else:
        elapsed = (end_dt - start_dt).total_seconds()

    norm_distances = []
    for r in merged:
        dv = r.get("distance")
        if dv is None:
            continue
        try:
            norm_distances.append(float(dv))
        except Exception:
            pass
    dist = max(norm_distances) if norm_distances else 0.0

    _FIT_EPOCH_UNIX = 631065600
    _FIT_EPOCH_DT = _dt.datetime(1989, 12, 31, 0, 0, 0, tzinfo=_dt.timezone.utc)

    def ts_fit(dt):
        if isinstance(dt, (int, float)):
            v = int(dt)
            if v > 1_000_000_000:
                return v - _FIT_EPOCH_UNIX
            if 0 <= v <= 2_000_000_000:
                return v
            print(f"[WARN] Unexpected timestamp int {v}, using abs value")
            return abs(v)
        if hasattr(dt, 'tzinfo'):
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            else:
                dt = dt.astimezone(_dt.timezone.utc)
            return int((dt - _FIT_EPOCH_DT).total_seconds())
        print(f"[WARN] Unknown timestamp type {type(dt)}, returning 0")
        return 0

    start_ms=ts_fit(start_dt); end_ms=ts_fit(end_dt)
    def to_unix_ms(fit_s): return (fit_s + _FIT_EPOCH_UNIX) * 1000
    start_unix_ms = to_unix_ms(start_ms)
    end_unix_ms   = to_unix_ms(end_ms)

    sum_hr=cnt_hr=max_hr=sum_pow=cnt_pow=max_pow=sum_cad=cnt_cad=max_cad=0
    total_asc=total_desc=0; prev_alt=None
    sum_lte=cnt_lte=sum_rte=cnt_rte=0
    sum_lps=cnt_lps=sum_rps=cnt_rps=sum_cps=cnt_cps=0
    sum_lpco=cnt_lpco=sum_rpco=cnt_rpco=0
    sum_lpp0=sum_lpp1=cnt_lpp=0
    sum_lppk0=sum_lppk1=cnt_lppk=0
    sum_rpp0=sum_rpp1=cnt_rpp=0
    sum_rppk0=sum_rppk1=cnt_rppk=0
    sum_temp=cnt_temp=0
    total_cycles=0
    # For NP calculation (30s rolling average of power^4)
    power_list = []
    # Balance summary
    sum_bal_left=cnt_bal=0
    for r in merged:
        hr=r.get("heart_rate"); pw=r.get("power"); cd=r.get("cadence")
        alt=r.get("enhanced_altitude") or r.get("altitude")
        temp=r.get("temperature")
        bal=r.get("left_right_balance")
        if hr and 0<hr<250: sum_hr+=hr; cnt_hr+=1; max_hr=max(max_hr,hr)
        if pw and 0<pw<3000:
            sum_pow+=pw; cnt_pow+=1; max_pow=max(max_pow,pw)
            power_list.append(float(pw))
        else:
            power_list.append(0.0)
        if cd and 0<cd<250: sum_cad+=cd; cnt_cad+=1; max_cad=max(max_cad,cd)
        if cd and 0<cd<250: total_cycles+=cd  # each second = 1 revolution count at that rpm/60... actually total_cycles = sum of cadence * (time_interval / 60)
        if alt is not None and prev_alt is not None:
            d=alt-prev_alt
            if 0<d<100: total_asc+=d
            elif -100<d<0: total_desc+=abs(d)
        if alt is not None: prev_alt=alt
        if temp is not None:
            try: sum_temp+=float(temp); cnt_temp+=1
            except: pass
        if bal is not None:
            try:
                left_pct = int(bal) & 0x7F
                if 0 < left_pct <= 100:
                    sum_bal_left += left_pct; cnt_bal += 1
            except: pass
        # cycling dynamics
        lte=r.get("left_torque_effectiveness")
        rte=r.get("right_torque_effectiveness")
        lps=r.get("left_pedal_smoothness")
        rps=r.get("right_pedal_smoothness")
        lpco=r.get("left_pco"); rpco=r.get("right_pco")
        lpp=r.get("left_power_phase"); lppk=r.get("left_power_phase_peak")
        rpp=r.get("right_power_phase"); rppk=r.get("right_power_phase_peak")
        try:
            if lte is not None and 0<=float(lte)<=100: sum_lte+=float(lte); cnt_lte+=1
            if rte is not None and 0<=float(rte)<=100: sum_rte+=float(rte); cnt_rte+=1
            if lps is not None and 0<=float(lps)<=100: sum_lps+=float(lps); cnt_lps+=1
            if rps is not None and 0<=float(rps)<=100: sum_rps+=float(rps); cnt_rps+=1
            if lps is not None and rps is not None and 0<=float(lps)<=100 and 0<=float(rps)<=100:
                sum_cps+=(float(lps)+float(rps))/2; cnt_cps+=1
            if lpco is not None: sum_lpco+=float(lpco); cnt_lpco+=1
            if rpco is not None: sum_rpco+=float(rpco); cnt_rpco+=1
            if isinstance(lpp,(list,tuple)) and len(lpp)>=2:
                sum_lpp0+=float(lpp[0]); sum_lpp1+=float(lpp[1]); cnt_lpp+=1
            if isinstance(lppk,(list,tuple)) and len(lppk)>=2:
                sum_lppk0+=float(lppk[0]); sum_lppk1+=float(lppk[1]); cnt_lppk+=1
            if isinstance(rpp,(list,tuple)) and len(rpp)>=2:
                sum_rpp0+=float(rpp[0]); sum_rpp1+=float(rpp[1]); cnt_rpp+=1
            if isinstance(rppk,(list,tuple)) and len(rppk)>=2:
                sum_rppk0+=float(rppk[0]); sum_rppk1+=float(rppk[1]); cnt_rppk+=1
        except Exception:
            pass

    avg_hr=sum_hr//cnt_hr if cnt_hr else None
    avg_pow=sum_pow//cnt_pow if cnt_pow else None
    avg_cad=sum_cad//cnt_cad if cnt_cad else None
    asc_m=int(total_asc) if total_asc else None
    desc_m=int(total_desc) if total_desc else None
    avg_left_torque_eff  = round(sum_lte/cnt_lte, 1) if cnt_lte else None
    avg_right_torque_eff = round(sum_rte/cnt_rte, 1) if cnt_rte else None
    avg_left_smoothness  = round(sum_lps/cnt_lps, 1) if cnt_lps else None
    avg_right_smoothness = round(sum_rps/cnt_rps, 1) if cnt_rps else None
    avg_combined_smoothness = round(sum_cps/cnt_cps, 1) if cnt_cps else None
    avg_left_pco  = round(sum_lpco/cnt_lpco, 1) if cnt_lpco else None
    avg_right_pco = round(sum_rpco/cnt_rpco, 1) if cnt_rpco else None
    # Power phase angles CANNOT be averaged arithmetically (circular data wraps at 0°/360°).
    # Always use the source session/device values which are computed correctly by the device.
    avg_left_pp = None
    avg_left_pp_peak = None
    avg_right_pp = None
    avg_right_pp_peak = None

    # Additional session-level metrics
    max_cad_v = max_cad if max_cad > 0 else None
    avg_temp = round(sum_temp / cnt_temp, 1) if cnt_temp else None
    # Total cycles: each record is ~1 second, cadence is rpm → revolutions per record ≈ cadence/60
    total_cycles_v = int(round(total_cycles / 60.0)) if total_cycles > 0 else None

    # Normalized Power (NP): 30s rolling average of power, then 4th root of mean of 4th powers
    normalized_power = None
    if len(power_list) >= 30 and cnt_pow > 0:
        window = 30
        rolling = []
        running_sum = sum(power_list[:window])
        for i in range(window, len(power_list)):
            avg_30 = running_sum / window
            rolling.append(avg_30 ** 4)
            running_sum += power_list[i] - power_list[i - window]
        # last window
        avg_30 = running_sum / window
        rolling.append(avg_30 ** 4)
        if rolling:
            normalized_power = int(round((sum(rolling) / len(rolling)) ** 0.25))

    # Total work (joules) = sum of power per second (each record ≈ 1s)
    total_work = int(sum(power_list)) if cnt_pow > 0 else None
    # Total calories (rough estimate: work in kJ / 4.184 / ~0.25 efficiency → kcal)
    # Better: use source session value if available
    total_calories_calc = int(round(total_work / 4184.0 / 0.25)) if total_work else None

    # FTP/threshold_power from source sessions
    _ftp = _float_or_none(_ses1.get("threshold_power") or _ses2.get("threshold_power"))
    # Intensity Factor and TSS
    intensity_factor = None
    training_stress_score = None
    if normalized_power and _ftp and _ftp > 0:
        intensity_factor = round(normalized_power / _ftp, 3)
        training_stress_score = round((elapsed * normalized_power * intensity_factor) / (_ftp * 3600) * 100, 1)

    # Use source session calories if available, else calculated
    _src_calories = _int_or_none(_ses1.get("total_calories") or _ses2.get("total_calories"))
    total_calories = _src_calories if _src_calories else total_calories_calc

    # Left/right balance summary for session (encoded: bit7=right_dominant, bits0-6 = left%)
    balance_summary = None
    if cnt_bal > 0:
        avg_left = int(round(sum_bal_left / cnt_bal))
        right_dom = 1 if avg_left < 50 else 0
        balance_summary = (right_dom << 7) | (avg_left & 0x7F)

    # Source NP / work / cycles if available (prefer source over computed for work/cycles)
    _src_np = _int_or_none(_ses1.get("normalized_power") or _ses2.get("normalized_power"))
    _src_work = _int_or_none(_ses1.get("total_work") or _ses2.get("total_work"))
    _src_cycles = _int_or_none(_ses1.get("total_cycles") or _ses2.get("total_cycles"))
    if _src_np: normalized_power = _src_np
    if _src_work: total_work = _src_work
    if _src_cycles: total_cycles_v = _src_cycles

    # ── Prefer source session power_phase (4-value: seated+standing) over recomputed 2-value ──
    _dev_ses = _ses2 if str(device_source).lower() == 'f2' else _ses1
    _alt_ses = _ses1 if str(device_source).lower() == 'f2' else _ses2
    def _get_list(ses, key):
        v = ses.get(key)
        if v is None: return None
        if isinstance(v, (list, tuple)): return list(v)
        try:
            s = str(v).replace('[','').replace(']','').replace('(','').replace(')','')
            parts = [x.strip() for x in s.split(',') if x.strip()]
            return [float(x) for x in parts] if parts else None
        except: return None

    for _attr, _sdk_key in [
        ('avg_left_pp', 'avg_left_power_phase'),
        ('avg_left_pp_peak', 'avg_left_power_phase_peak'),
        ('avg_right_pp', 'avg_right_power_phase'),
        ('avg_right_pp_peak', 'avg_right_power_phase_peak'),
    ]:
        src_val = _get_list(_dev_ses, _sdk_key) or _get_list(_alt_ses, _sdk_key)
        if src_val and len(src_val) >= 2:
            if _attr == 'avg_left_pp': avg_left_pp = src_val
            elif _attr == 'avg_left_pp_peak': avg_left_pp_peak = src_val
            elif _attr == 'avg_right_pp': avg_right_pp = src_val
            elif _attr == 'avg_right_pp_peak': avg_right_pp_peak = src_val

    # ── Extract position fields from source session (not per-record data) ──
    _ses_avg_power_position = _get_list(_dev_ses, 'avg_power_position') or _get_list(_alt_ses, 'avg_power_position')
    _ses_max_power_position = _get_list(_dev_ses, 'max_power_position') or _get_list(_alt_ses, 'max_power_position')
    _ses_avg_cadence_position = _get_list(_dev_ses, 'avg_cadence_position') or _get_list(_alt_ses, 'avg_cadence_position')
    _ses_max_cadence_position = _get_list(_dev_ses, 'max_cadence_position') or _get_list(_alt_ses, 'max_cadence_position')

    # ── Extract training effect, load and respiration from source session ──
    # Always prefer the device source (Garmin) as it computes these metrics.
    def _pick_float_ses(key):
        v = _dev_ses.get(key)
        if v is not None: return _float_or_none(v)
        return _float_or_none(_alt_ses.get(key))

    _total_training_effect           = _pick_float_ses('total_training_effect')
    _total_anaerobic_training_effect = _pick_float_ses('total_anaerobic_training_effect')
    _training_load_peak              = _pick_float_ses('training_load_peak')
    _enhanced_avg_respiration_rate   = _pick_float_ses('enhanced_avg_respiration_rate')
    _enhanced_max_respiration_rate   = _pick_float_ses('enhanced_max_respiration_rate')
    _enhanced_min_respiration_rate   = _pick_float_ses('enhanced_min_respiration_rate')

    hrv_msgs = []
    if include_hrv:
        hrv_msgs = f2.get("hrv_mesgs",[]) or f1.get("hrv_mesgs",[])

    # ── Collect developer-field metadata/raw values from both source files ──
    import datetime as _dt2
    combined_defs = {}
    combined_ts = {}
    global_dev_idx = {}
    next_dev_idx = 0
    dev_source_rank = {path1: 0, path2: 1}
    dev_pair_source_rank = {}
    dev_meta_source_rank = {}
    for src_i, src in enumerate([path1, path2]):
        try:
            info = parse_dev_data(src)
            local_remap = {}
            for old_di, flds in info['defs'].items():
                app_id = flds.get('_app_id', f'{src}:{old_di}') if isinstance(flds, dict) else f'{src}:{old_di}'
                if app_id not in global_dev_idx:
                    global_dev_idx[app_id] = next_dev_idx
                    next_dev_idx += 1
                new_di = global_dev_idx[app_id]
                local_remap[old_di] = new_di
                combined_defs.setdefault(new_di, {})['_app_id'] = app_id
                # Preserve the original developer_id from the source file.
                # Use setdefault so the first-seen value wins (it's consistent for
                # each unique app_id, so any source is fine).
                combined_defs[new_di].setdefault(
                    '_developer_id',
                    flds.get('_developer_id', 'ff'*16) if isinstance(flds, dict) else 'ff'*16
                )
                src_rank = dev_source_rank.get(src, src_i)
                prev_rank = dev_meta_source_rank.get(new_di, -1)
                prefer_this_meta = (app_id in SENSITIVE_DEV_APP_IDS and src_rank >= prev_rank) or (app_id not in SENSITIVE_DEV_APP_IDS)
                if isinstance(flds, dict) and '_application_version' in flds and prefer_this_meta:
                    combined_defs[new_di]['_application_version'] = flds.get('_application_version')
                    dev_meta_source_rank[new_di] = src_rank
                for fn, meta in flds.items():
                    if isinstance(fn, str) and fn.startswith('_'):
                        continue
                    if not isinstance(meta, dict):
                        continue
                    # No early exclusion here — excluded-by-default fields are still
                    # collected so the user can opt-in via the UI.
                    # The selection filter below (selected_dev_keys / allowed) handles
                    # whether they end up in the output.
                    pair_key = (new_di, fn)
                    prev_pair_rank = dev_pair_source_rank.get(pair_key, -1)
                    prefer_this_pair = (app_id in SENSITIVE_DEV_APP_IDS and src_rank >= prev_pair_rank) or (app_id not in SENSITIVE_DEV_APP_IDS or pair_key not in combined_defs.get(new_di, {}))
                    if prefer_this_pair:
                        combined_defs[new_di][fn] = dict(meta)
                        dev_pair_source_rank[pair_key] = src_rank
            shift_for_src = 0 if src_i == 0 else best_shift
            for ts, vals in info['ts_vals'].items():
                target_ts = int(ts) + shift_for_src
                slot = combined_ts.setdefault(target_ts, {})
                for (old_di, fn), raw in vals.items():
                    if old_di not in local_remap:
                        continue
                    ndi = local_remap[old_di]
                    meta_di = combined_defs.get(ndi, {})
                    meta = meta_di.get(fn, {})
                    app_id = meta_di.get('_app_id', '')
                    pair_key = (ndi, fn)
                    if app_id in SENSITIVE_DEV_APP_IDS and pair_key in slot:
                        # Preserve the preferred source sample for sensitive apps instead of last-write-wins.
                        continue
                    slot[pair_key] = raw
        except Exception as e:
            print(f"[WARN] dev parse {src}: {e}")

    if combined_ts and 'record_time_shift' in locals() and record_time_shift:
        combined_ts = {int(ts) + int(record_time_shift): vals for ts, vals in combined_ts.items()}

    has_dev = bool(combined_defs and combined_ts)
    all_pairs = sorted(set(k for v in combined_ts.values() for k in v.keys())) if has_dev else []
    selected_dev_map = {}
    if selected_dev_keys:
        for src in [path1, path2]:
            try:
                info = parse_dev_data(src)
                for old_di, flds in info['defs'].items():
                    app_id = flds.get('_app_id', f'{src}:{old_di}') if isinstance(flds, dict) else f'{src}:{old_di}'
                    new_di = global_dev_idx.get(app_id, old_di)
                    for fn, meta in flds.items():
                        if isinstance(fn, str) and fn.startswith('_'):
                            continue
                        if not isinstance(meta, dict):
                            continue
                        name = meta.get('name', f'dev{old_di}_{fn}')
                        # Include ALL fields in the map — including excluded-by-default ones.
                        # The user may have explicitly opted in from the UI.
                        old_key = f"dev:{old_di}:{name}:fn{fn}"
                        selected_dev_map[old_key] = (new_di, fn)
            except Exception:
                pass
        allowed = {selected_dev_map[k] for k in selected_dev_keys if k in selected_dev_map}
        all_pairs = [pair for pair in all_pairs if pair in allowed]
        has_dev = bool(all_pairs)
    else:
        # No explicit selection: exclude fields that are off-by-default
        filtered_pairs = []
        for (di, fn) in all_pairs:
            meta_di = combined_defs.get(di, {})
            app_id = meta_di.get('_app_id', '')
            meta = meta_di.get(fn, {})
            name = meta.get('name', f'dev{di}_{fn}') if isinstance(meta, dict) else f'dev{di}_{fn}'
            if not _is_excluded_dev_field(app_id, name):
                filtered_pairs.append((di, fn))
        all_pairs = filtered_pairs
        has_dev = bool(all_pairs)

    # ── Collect extra/undocumented standard record fields (raw passthrough) ──
    extra_name_to_num = {name: fn for fn, name in ((fn, _extra_record_field_name(fn)) for fn in range(0, 256))}
    selected_extra_nums = sorted({extra_name_to_num[name] for name in selected_fields if name in extra_name_to_num})
    combined_extra_defs = {}
    combined_extra_ts = {}
    if selected_extra_nums:
        selected_extra_set = set(selected_extra_nums)
        for src_i, src in enumerate([path1, path2]):
            try:
                info = parse_standard_record_data(src)
                shift_for_src = 0 if src_i == 0 else best_shift
                for fn, meta in (info.get('defs') or {}).items():
                    if fn in selected_extra_set and fn not in KNOWN_SDK_RECORD_FIELD_NUMS and fn != 253:
                        combined_extra_defs[fn] = dict(meta)
                for ts, vals in (info.get('ts_vals') or {}).items():
                    filtered = {fn: raw for fn, raw in vals.items() if fn in selected_extra_set and fn not in KNOWN_SDK_RECORD_FIELD_NUMS and fn != 253}
                    if filtered:
                        combined_extra_ts.setdefault(int(ts) + shift_for_src, {}).update(filtered)
            except Exception as e:
                print(f"[WARN] std parse {src}: {e}")

    # ── Build FIT in pure binary ───────────────────────────────────────────
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(__file__))
    from fit_writer import (FitWriter, _ts, _u8, _u16, _u32, _s32, _f32,
                             INVALID_U8, INVALID_U16, INVALID_U32)

    _FIT_EPOCH_DT = _dt2.datetime(1989,12,31,0,0,0,tzinfo=_dt2.timezone.utc)
    def _ts_from_merged(dt):
        if isinstance(dt,(int,float)):
            v=int(dt)
            return v if v<1_000_000_000 else v-631065600
        if dt.tzinfo is None: dt=dt.replace(tzinfo=_dt2.timezone.utc)
        else: dt=dt.astimezone(_dt2.timezone.utc)
        return int((dt-_FIT_EPOCH_DT).total_seconds())

    start_ts_fit = _ts_from_merged(merged[0]["timestamp"])
    end_ts_fit   = _ts_from_merged(merged[-1]["timestamp"])

    # Use the session start_time from the time_source file so the activity
    # shows the correct time-of-day.  The user picks which device's clock
    # to trust via the "time_source" option in the UI (defaults to f2/Garmin).
    _time_ses = _ses2 if str(time_source).lower() == 'f2' else _ses1
    _time_start_raw = _time_ses.get("start_time")
    if _time_start_raw is not None:
        meta_start_ts_fit = _ts_from_merged(_time_start_raw)
    else:
        meta_start_ts_fit = start_ts_fit
    meta_end_ts_fit = end_ts_fit

    # Recalculate elapsed from the chosen session start to the last record
    elapsed = float(meta_end_ts_fit - meta_start_ts_fit)

    # UTC offset: prefer f1 (Garmin, always has correct timezone configured by user).
    # Fall back to f2 (Biketerra), then to 0 if neither has it.
    selected_offset = read_utc_offset(path1)
    if selected_offset is None:
        selected_offset = (parse_device_metadata(path1) or {}).get('local_timestamp_offset')
    if selected_offset is None:
        selected_offset = read_utc_offset(path2)
    if selected_offset is None:
        selected_offset = (parse_device_metadata(path2) or {}).get('local_timestamp_offset')
    utc_offset = int(selected_offset or 0)
    local_ts_fit = meta_end_ts_fit + utc_offset

    # Keep selected_time_path for stats reporting only
    selected_time_path = path2 if str(time_source).lower() == 'f2' else path1

    device_path = path2 if str(device_source).lower() == 'f2' else path1
    device_meta = parse_device_metadata(device_path)

    # ── Extract user profile, zones, all device_info, and laps from source ──
    # Prefer Garmin device (device_path) for metadata, fallback to the other
    user_profile1, zones1 = parse_user_profile_and_zones(f1)
    user_profile2, zones2 = parse_user_profile_and_zones(f2)
    # Prefer the device source for user profile/zones
    if str(device_source).lower() == 'f2':
        user_profile = user_profile2 if user_profile2 else user_profile1
        zones = zones2 if zones2.get('ftp') else zones1
    else:
        user_profile = user_profile1 if user_profile1 else user_profile2
        zones = zones1 if zones1.get('ftp') else zones2

    # All device_info messages from the device source
    all_devices = parse_all_device_info(device_path)

    # Source laps — prefer device source for laps, as it has richer lap data
    _LAP_TRIGGER_MAP = {"manual":0,"distance":1,"position_marked":2,"time":3,
                        "fitness_equipment":4,"session_end":7}
    if str(device_source).lower() == 'f2':
        src_laps_raw = f2.get("lap_mesgs", []) or []
        if not src_laps_raw or len(src_laps_raw) < 2:
            src_laps_raw = f1.get("lap_mesgs", []) or []
    else:
        src_laps_raw = f1.get("lap_mesgs", []) or []
        if not src_laps_raw or len(src_laps_raw) < 2:
            src_laps_raw = f2.get("lap_mesgs", []) or []
    # If still only 1 lap or no laps, we'll fall back to single synthetic lap later

    fw = FitWriter()
    fw.write_file_id(
        meta_start_ts_fit,
        manufacturer=device_meta.get('manufacturer', 1),
        product=device_meta.get('product', 0),
        serial_number=device_meta.get('serial_number'),
        number=device_meta.get('file_number', 0),
        product_name=device_meta.get('product_name', ''),
    )

    # Write all device_info messages (sensors, head unit, etc.)
    if all_devices:
        for dev in all_devices:
            fw.write_device_info(
                meta_start_ts_fit,
                device_index=dev.get('device_index', 0),
                manufacturer=dev.get('manufacturer', 1),
                product=dev.get('product', 0),
                serial_number=dev.get('serial_number'),
                software_version=dev.get('software_version'),
                descriptor=dev.get('descriptor', ''),
                product_name=dev.get('product_name', ''),
                source_type=dev.get('source_type', 5),
            )
    else:
        fw.write_device_info(
            meta_start_ts_fit,
            device_index=device_meta.get('device_index', 0),
            manufacturer=device_meta.get('manufacturer', 1),
            product=device_meta.get('product', 0),
            serial_number=device_meta.get('serial_number'),
            software_version=device_meta.get('software_version'),
            descriptor=device_meta.get('descriptor', 'Primary Device'),
            product_name=device_meta.get('product_name', ''),
            source_type=device_meta.get('source_type', 5),
        )

    # Write user profile if available
    if user_profile:
        fw.write_user_profile(
            weight_kg=user_profile.get('weight_kg'),
            height_m=user_profile.get('height_m'),
            gender=user_profile.get('gender'),
            age=user_profile.get('age'),
            resting_hr=user_profile.get('resting_hr'),
            max_hr=user_profile.get('max_hr'),
            activity_class=user_profile.get('activity_class'),
            friendly_name=user_profile.get('friendly_name', ''),
        )

    # Write zones target if available
    if zones and (zones.get('ftp') or zones.get('max_hr')):
        fw.write_zones_target(
            ftp=zones.get('ftp'),
            max_hr=zones.get('max_hr'),
            threshold_hr=zones.get('threshold_hr'),
        )

    fw.write_event(meta_start_ts_fit, event=0, event_type=0)

    if has_dev:
        written_dev_ids = set()
        for (di, fn) in all_pairs:
            if di not in written_dev_ids:
                meta_di = combined_defs.get(di, {})
                app_id_hex = meta_di.get('_app_id', 'ff' * 16)
                fw.write_developer_data_id(
                    di, app_id_hex,
                    application_version=meta_di.get('_application_version'),
                    developer_id_hex=meta_di.get('_developer_id'),
                )
                written_dev_ids.add(di)
            info = combined_defs.get(di, {}).get(fn, {})
            fw.write_field_description(
                dev_idx=di,
                field_def_num=fn,
                base_type=info.get('base', 0x84),
                field_name=info.get('name', f'dev{di}_{fn}'),
                units=info.get('units', ''),
                size=info.get('desc_size', info.get('size', 4)),
                native_field_num=info.get('native_field_num'),
                native_mesg_num=info.get('native_mesg_num'),
                scale=info.get('scale'),
                offset=info.get('offset'),
            )

    BASE_STD_FIELDS = [
        (253,4,0x86),(0,4,0x85),(1,4,0x85),(2,2,0x84),(78,4,0x86),
        (3,1,0x02),(4,1,0x02),(53,1,0x02),(5,4,0x86),(6,2,0x84),(73,4,0x86),
        (7,2,0x84),(9,2,0x83),(13,1,0x01),(29,4,0x86),(30,1,0x02),
        (43,1,0x02),(44,1,0x02),(45,1,0x02),(46,1,0x02),
        (67,1,0x01),(68,1,0x01),(69,2,0x02),(70,2,0x02),(71,2,0x02),(72,2,0x02),
        (108,2,0x84),
    ]
    extra_std_fields = [(fn, combined_extra_defs.get(fn, {}).get('size', 1), combined_extra_defs.get(fn, {}).get('base', 0x02))
                        for fn in selected_extra_nums if fn in combined_extra_defs]
    STD_FIELDS = BASE_STD_FIELDS + extra_std_fields

    dev_field_entries = [(fn,combined_defs.get(di,{}).get(fn,{}).get('record_size', combined_defs.get(di,{}).get(fn,{}).get('size',4)),di)
                          for (di,fn) in all_pairs] if has_dev else None
    fw.define_record(STD_FIELDS, dev_field_entries)

    _FIELD_SIZES = {253:4, 0:4, 1:4, 2:2, 78:4, 3:1, 4:1, 53:1, 5:4, 6:2, 73:4, 7:2, 9:2, 13:1, 29:4, 30:1, 43:1, 44:1, 45:1, 46:1, 67:1, 68:1, 69:2, 70:2, 71:2, 72:2, 108:2}
    for fn, sz, _bt in extra_std_fields:
        _FIELD_SIZES[fn] = sz

    def _encode_std(field_num, val):
        if val is None:
            if field_num in (0, 1):
                return _s32(2147483647)
            if field_num == 9:
                return _struct.pack('<h', 32767)
            if field_num == 13:
                return _struct.pack('<b', 127)
            return b'\xff' * _FIELD_SIZES.get(field_num, 1)
        try:
            if field_num == 0:   return _s32(int(val))
            if field_num == 1:   return _s32(int(val))
            if field_num == 2:   return _u16(max(0, min(65534, int(float(val)*5 + 2500))))
            if field_num == 78:  return _u32(max(0, min(0xFFFFFFFE, int(float(val)*5 + 2500))))
            if field_num == 3:   return _u8(max(0, min(254, int(val))))
            if field_num == 4:   return _u8(max(0, min(254, int(val))))
            if field_num == 53:  return _u8(max(0, min(254, int(round(float(val)*2)))))  # fractional_cadence scale 0.5
            if field_num == 5:   return _u32(max(0, int(float(val)*100)))
            if field_num == 6:   return _u16(max(0, min(65534, int(float(val)*1000))))
            if field_num == 73:  return _u32(max(0, int(float(val)*1000)))
            if field_num == 7:   return _u16(max(0, min(65534, int(val))))
            if field_num == 9:   return _struct.pack('<h', max(-32767, min(32767, int(float(val)*100))))
            if field_num == 13:  return _struct.pack('<b', max(-127, min(127, int(val))))
            if field_num == 29:  return _u32(max(0, int(val)))
            if field_num == 30:
                # left_right_balance: bit7=right_dominant, bits0-6 = pct×2
                # SDK value arrives as raw uint8 (already encoded per FIT profile)
                raw_bal = int(val) & 0xFF
                return _u8(min(254, raw_bal))
            if field_num in (43,44,45,46):
                # torque_effectiveness / pedal_smoothness: scale 0.5 → stored = pct×2
                return _u8(max(0, min(254, int(float(val)*2))))
            if field_num in (67,68):
                # left_pco / right_pco: sint8, scale 1 mm, no conversion needed
                return _struct.pack('<b', max(-127, min(127, int(round(float(val))))))
            if field_num in (69,70,71,72):
                # power_phase / power_phase_peak: two uint8 angles
                # FIT profile: scale = 360/256 = 1.40625 deg/unit
                # encode: raw = round(degrees / 1.40625) = round(degrees * 256/360)
                def _enc_phase_pair(v):
                    if isinstance(v, (list, tuple)) and len(v) >= 2:
                        vals = v[:2]
                    else:
                        s = str(v).replace('[','').replace(']','').replace('(','').replace(')','')
                        parts = [p.strip() for p in s.split(',') if p.strip()]
                        if len(parts) >= 2:
                            vals = parts[:2]
                        else:
                            return b'\xff\xff'
                    raw = []
                    for x in vals:
                        deg = float(x)
                        raw.append(max(0, min(254, int(round(deg * 256.0 / 360.0)))))
                    return bytes(raw[:2])
                return _enc_phase_pair(val)
            if field_num == 108: return _u16(max(0, min(65534, int(round(float(val)*100)))))  # enhanced_respiration_rate scale 0.01
        except Exception:
            pass
        return b'\xff' * _FIELD_SIZES.get(field_num, 1)

    SDK_TO_FN = {
        'position_lat':0,'position_long':1,'altitude':2,'enhanced_altitude':78,
        'heart_rate':3,'cadence':4,'fractional_cadence':53,'distance':5,'speed':6,'enhanced_speed':73,
        'power':7,'grade':9,'temperature':13,'accumulated_power':29,
        'left_right_balance':30,'left_torque_effectiveness':43,
        'right_torque_effectiveness':44,'left_pedal_smoothness':45,'right_pedal_smoothness':46,
        'left_pco':67,'right_pco':68,'left_power_phase':69,'left_power_phase_peak':70,
        'right_power_phase':71,'right_power_phase_peak':72,
        'enhanced_respiration_rate':108,
    }
    fn_to_sdk = {v:k for k,v in SDK_TO_FN.items()}
    base_std_nums = {fn for fn, _, _ in BASE_STD_FIELDS}

    for r in merged:
        ts_fit_val = _ts_from_merged(r["timestamp"])
        std_vals = []
        for fn,sz,bt in STD_FIELDS[1:]:
            sdk_name = fn_to_sdk.get(fn)
            if sdk_name:
                val = r.get(sdk_name)
                if sdk_name=='altitude' and val is None: val=r.get('enhanced_altitude')
                if sdk_name=='enhanced_altitude' and val is None: val=r.get('altitude')
                if sdk_name=='speed' and val is None: val=r.get('enhanced_speed')
                if sdk_name=='enhanced_speed' and val is None: val=r.get('speed')
                std_vals.append(_encode_std(fn, val))
            else:
                raw = (combined_extra_ts.get(ts_fit_val, {}) or {}).get(fn)
                std_vals.append(raw if (raw is not None and len(raw) == sz) else b'\xff' * sz)

        dev_vals = None
        if has_dev and all_pairs:
            devdata = combined_ts.get(ts_fit_val, {})
            dev_vals = []
            for (di,fn) in all_pairs:
                raw = devdata.get((di,fn))
                sz = combined_defs.get(di,{}).get(fn,{}).get('record_size', combined_defs.get(di,{}).get(fn,{}).get('size',4))
                dev_vals.append(raw if (raw and len(raw)==sz) else b'\xff'*sz)

        fw.write_record(ts_fit_val, std_vals, dev_vals)

    for h in hrv_msgs:
        raw_time = h.get("time")
        if raw_time is None: continue
        if isinstance(raw_time,(list,tuple)):
            rr = []
            for v in raw_time:
                if v is None: rr.append(65535)
                else:
                    try: rr.append(min(65534, max(0, int(round(float(v)*1000)))))
                    except: rr.append(65535)
        else:
            try: rr = [min(65534, max(0, int(round(float(raw_time)*1000))))]
            except: rr = [65535]
        fw.write_hrv(rr)

    fw.write_event(meta_end_ts_fit, event=0, event_type=4)

    # ── Write laps ──────────────────────────────────────────────────────
    # Try to use original laps from the source Garmin file
    num_laps_written = 0
    if len(src_laps_raw) >= 2:
        # Multiple laps from source — re-emit them
        for li, lap_dict in enumerate(src_laps_raw):
            if not isinstance(lap_dict, dict):
                continue
            lap_ts = lap_dict.get('timestamp')
            lap_start = lap_dict.get('start_time')
            lap_elapsed_raw = lap_dict.get('total_elapsed_time') or lap_dict.get('total_timer_time')
            lap_dist_raw = lap_dict.get('total_distance')
            if lap_ts is None or lap_start is None:
                continue
            lap_ts_fit = _ts_from_merged(lap_ts)
            lap_start_fit = _ts_from_merged(lap_start)
            lap_elapsed = float(lap_elapsed_raw) if lap_elapsed_raw is not None else 0
            lap_dist = float(lap_dist_raw) if lap_dist_raw is not None else 0

            # Lap trigger
            lt = lap_dict.get('lap_trigger', 0)
            if isinstance(lt, str):
                lt = _LAP_TRIGGER_MAP.get(lt.lower(), 0)
            try: lt = int(lt)
            except: lt = 0

            fw.write_lap(lap_ts_fit, lap_start_fit, lap_elapsed, lap_dist,
                         sport=_sport, sub_sport=_sub_sport, lap_trigger=lt,
                         avg_hr=_int_or_none(lap_dict.get('avg_heart_rate')),
                         max_hr=_int_or_none(lap_dict.get('max_heart_rate')),
                         avg_cad=_int_or_none(lap_dict.get('avg_cadence')),
                         max_cad=_int_or_none(lap_dict.get('max_cadence')),
                         avg_pow=_int_or_none(lap_dict.get('avg_power')),
                         max_pow=_int_or_none(lap_dict.get('max_power')),
                         asc=_int_or_none(lap_dict.get('total_ascent')),
                         desc=_int_or_none(lap_dict.get('total_descent')),
                         total_calories=_int_or_none(lap_dict.get('total_calories')),
                         normalized_power=_int_or_none(lap_dict.get('normalized_power')),
                         time_standing=_float_or_none(lap_dict.get('time_standing')) if lap_dict.get('time_standing') is not None else 0.0,
                         stand_count=_int_or_none(lap_dict.get('stand_count')) if lap_dict.get('stand_count') is not None else 0,
                         avg_left_torque_eff=_float_or_none(lap_dict.get('avg_left_torque_effectiveness')),
                         avg_right_torque_eff=_float_or_none(lap_dict.get('avg_right_torque_effectiveness')),
                         avg_left_smoothness=_float_or_none(lap_dict.get('avg_left_pedal_smoothness')),
                         avg_right_smoothness=_float_or_none(lap_dict.get('avg_right_pedal_smoothness')),
                         avg_combined_smoothness=_float_or_none(lap_dict.get('avg_combined_pedal_smoothness')),
                         avg_left_pco=_float_or_none(lap_dict.get('avg_left_pco')),
                         avg_right_pco=_float_or_none(lap_dict.get('avg_right_pco')),
                         avg_left_pp=lap_dict.get('avg_left_power_phase'),
                         avg_left_pp_peak=lap_dict.get('avg_left_power_phase_peak'),
                         avg_right_pp=lap_dict.get('avg_right_power_phase'),
                         avg_right_pp_peak=lap_dict.get('avg_right_power_phase_peak'),
                         left_right_balance=_int_or_none(lap_dict.get('left_right_balance')),
                         avg_power_position=lap_dict.get('avg_power_position'),
                         max_power_position=lap_dict.get('max_power_position'),
                         avg_cadence_position=lap_dict.get('avg_cadence_position'),
                         max_cadence_position=lap_dict.get('max_cadence_position'),
                         message_index=li)
            num_laps_written += 1

    if num_laps_written == 0:
        # Fallback: single synthetic lap covering the whole activity
        fw.write_lap(meta_end_ts_fit, meta_start_ts_fit, elapsed, dist,
                     sport=_sport, sub_sport=_sub_sport, lap_trigger=7,  # session_end
                     avg_hr=avg_hr, max_hr=max_hr,
                     avg_cad=avg_cad, max_cad=max_cad_v, avg_pow=avg_pow, max_pow=max_pow,
                     asc=asc_m, desc=desc_m,
                     total_calories=total_calories, normalized_power=normalized_power,
                     time_standing=_time_standing, stand_count=_stand_count,
                     avg_left_torque_eff=avg_left_torque_eff, avg_right_torque_eff=avg_right_torque_eff,
                     avg_left_smoothness=avg_left_smoothness, avg_right_smoothness=avg_right_smoothness,
                     avg_combined_smoothness=avg_combined_smoothness,
                     avg_left_pco=avg_left_pco, avg_right_pco=avg_right_pco,
                     avg_left_pp=avg_left_pp, avg_left_pp_peak=avg_left_pp_peak,
                     avg_right_pp=avg_right_pp, avg_right_pp_peak=avg_right_pp_peak,
                     left_right_balance=balance_summary,
                     avg_power_position=_ses_avg_power_position,
                     max_power_position=_ses_max_power_position,
                     avg_cadence_position=_ses_avg_cadence_position,
                     max_cadence_position=_ses_max_cadence_position)
        num_laps_written = 1

    fw.write_session(meta_end_ts_fit, meta_start_ts_fit, elapsed, dist,
                     sport=_sport, sub_sport=_sub_sport,
                     avg_hr=avg_hr, max_hr=max_hr,
                     avg_cad=avg_cad, max_cad=max_cad_v, avg_pow=avg_pow, max_pow=max_pow,
                     asc=asc_m, desc=desc_m,
                     time_standing=_time_standing, stand_count=_stand_count,
                     avg_left_torque_eff=avg_left_torque_eff, avg_right_torque_eff=avg_right_torque_eff,
                     avg_left_smoothness=avg_left_smoothness, avg_right_smoothness=avg_right_smoothness,
                     avg_combined_smoothness=avg_combined_smoothness,
                     avg_left_pco=avg_left_pco, avg_right_pco=avg_right_pco,
                     avg_left_pp=avg_left_pp, avg_left_pp_peak=avg_left_pp_peak,
                     avg_right_pp=avg_right_pp, avg_right_pp_peak=avg_right_pp_peak,
                     normalized_power=normalized_power,
                     threshold_power=int(_ftp) if _ftp else None,
                     intensity_factor=intensity_factor,
                     training_stress_score=training_stress_score,
                     total_calories=total_calories,
                     total_work=total_work,
                     total_cycles=total_cycles_v,
                     avg_temperature=avg_temp,
                     left_right_balance=balance_summary,
                     avg_power_position=_ses_avg_power_position,
                     max_power_position=_ses_max_power_position,
                     avg_cadence_position=_ses_avg_cadence_position,
                     max_cadence_position=_ses_max_cadence_position,
                     total_training_effect=_total_training_effect,
                     total_anaerobic_training_effect=_total_anaerobic_training_effect,
                     training_load_peak=_training_load_peak,
                     enhanced_avg_respiration_rate=_enhanced_avg_respiration_rate,
                     enhanced_max_respiration_rate=_enhanced_max_respiration_rate,
                     enhanced_min_respiration_rate=_enhanced_min_respiration_rate,
                     first_lap_index=0,
                     num_laps=num_laps_written)
    fw.write_activity(meta_end_ts_fit, local_ts_fit, elapsed)

    fit_bytes = fw.build()
    out_path = tempfile.mktemp(suffix=".fit")
    with open(out_path,'wb') as f: f.write(fit_bytes)

    print(f"[INFO] ALIGN shift file2→file1: {best_shift:+d}s score={best_score if best_score is not None else 'n/a'}")
    print(f"[INFO] FIT OK: {len(merged)} records, {len(all_pairs)} dev fields, {len(fit_bytes)//1024}KB")

    stats = {
        "records": len(merged), "hrv": len(hrv_msgs),
        "dev_fields": len(all_pairs),
        "laps": num_laps_written,
        "alignment_offset_s": int(best_shift),
        "utc_offset_s": int(utc_offset),
        "record_time_shift_s": int(record_time_shift),
        "time_source": 'File 2 / Garmin' if str(time_source).lower() == 'f2' else 'File 1 / Biketerra/GPS',
        "device_source": 'File 2 / Garmin' if str(device_source).lower() == 'f2' else 'File 1 / Biketerra/GPS',
        "force_virtual": bool(force_virtual),
        "trim_to_shorter": bool(trim_to_shorter),
        "f1_start_offset_s": int(f1_start_offset or 0),
        "files_auto_swapped": bool(_swapped),
        "device_name": device_meta.get('product_name') or f"manufacturer {device_meta.get('manufacturer', 1)} / product {device_meta.get('product', 0)}",
        "duration": f"{int(elapsed//3600):02d}:{int((elapsed%3600)//60):02d}:{int(elapsed%60):02d}",
        "distance_km": round(dist/1000, 2),
        "avg_hr": avg_hr, "max_hr": max_hr,
        "avg_power": avg_pow, "max_power": max_pow,
        "normalized_power": normalized_power,
        "avg_cadence": avg_cad, "max_cadence": max_cad_v,
        "ascent_m": asc_m or 0,
        "total_calories": total_calories,
        "avg_temperature": avg_temp,
        "num_devices": len(all_devices) if all_devices else 1,
        "has_user_profile": bool(user_profile),
        "has_zones": bool(zones and zones.get('ftp')),
        "size_kb": round(os.path.getsize(out_path)/1024, 1),
    }
    return out_path, stats

# ── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    # Store uploaded files in memory between requests
    uploads = {}
    last_result = None

    def log_message(self, format, *args):
        pass  # suppress default logs

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",len(body))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(body)

    def send_cors(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Methods","POST,GET,OPTIONS")
        self.send_header("Access-Control-Allow-Headers","Content-Type")
        self.end_headers()

    def do_OPTIONS(self):
        self.send_cors()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/index.html":
            self.serve_html()
        elif path == "/download" and Handler.last_result:
            self.serve_file(Handler.last_result)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length",0))
        body = self.rfile.read(length)

        if path == "/upload":
            self.handle_upload(body)
        elif path == "/merge":
            self.handle_merge(body)
        else:
            self.send_response(404); self.end_headers()

    def handle_upload(self, body):
        # Expects multipart — we parse manually for simplicity
        # Frontend sends: file index (0/1) + raw FIT bytes as multipart
        ct = self.headers.get("Content-Type","")
        if "boundary=" not in ct:
            self.send_json({"error":"Expected multipart"},400); return
        boundary = ct.split("boundary=")[1].strip().encode()
        parts = body.split(b"--"+boundary)
        idx = None; fname = None; fdata = None
        for part in parts:
            if b"Content-Disposition" not in part: continue
            header, _, content = part.partition(b"\r\n\r\n")
            header = header.decode(errors="replace")
            content = content.rstrip(b"\r\n--")
            if 'name="index"' in header:
                idx = int(content.strip())
            elif 'name="file"' in header:
                fdata = content
                for h in header.split("\r\n"):
                    if "filename=" in h:
                        fname = h.split('filename="')[1].rstrip('"')

        if idx is None or fdata is None:
            self.send_json({"error":"Missing index or file"},400); return

        # Save to temp file
        tmp = tempfile.mktemp(suffix=".fit")
        with open(tmp,"wb") as f: f.write(fdata)
        Handler.uploads[idx] = {"path":tmp,"name":fname,"size":len(fdata)}

        # Parse and return field info
        try:
            msgs = load_fit(tmp)
            recs = msgs.get("record_mesgs",[])
            hrv  = msgs.get("hrv_mesgs",[])
            fields = get_fields(msgs)
            extra_record_fields = [rf for rf in get_extra_record_fields(tmp) if rf["count"] > 0]
            for rf in extra_record_fields:
                if rf["name"] not in fields:
                    fields.append(rf["name"])
            fields = sorted(fields)
            utc_off = read_utc_offset(tmp)
            start_ts0 = recs[0]["timestamp"] if recs else None
            end_ts0   = recs[-1]["timestamp"] if recs else None
            # Support both int (FIT seconds) and datetime from SDK
            if isinstance(start_ts0, int):
                import datetime as _dt2
                _FE = 631065600
                start_ts = _dt2.datetime.utcfromtimestamp(start_ts0 + _FE).isoformat() if start_ts0 else None
                end_ts   = _dt2.datetime.utcfromtimestamp(end_ts0 + _FE).isoformat() if end_ts0 else None
                dur_s = float(end_ts0 - start_ts0) if (start_ts0 and end_ts0) else 0
            else:
                start_ts = start_ts0.isoformat() if start_ts0 else None
                end_ts   = end_ts0.isoformat() if end_ts0 else None
                dur_s = (end_ts0 - start_ts0).total_seconds() if (start_ts0 and end_ts0 and len(recs)>1) else 0
            dur = f"{int(dur_s//3600):02d}:{int((dur_s%3600)//60):02d}:{int(dur_s%60):02d}"
            ses  = _first_dict(msgs.get("session_mesgs", []), {})
            dev_fields = get_dev_fields(tmp, msgs)
            device_meta = parse_device_metadata(tmp) or {}
            if not isinstance(ses, dict):
                ses = {}
            # Build a display name from file_id / device_info via SDK
            fid = _first_dict(msgs.get("file_id_mesgs", []), {})
            _mfr_raw = fid.get("manufacturer", "")
            _prod_name = fid.get("product_name") or ""
            _prod_id = fid.get("product", "")
            # Manufacturer name map (common ones)
            _MFR_NAMES = {"garmin":"Garmin","wahoo_fitness":"Wahoo","stages_cycling":"Stages",
                          "favero_electronics":"Favero","development":"Development","sram":"SRAM",
                          "bryton":"Bryton","hammerhead":"Hammerhead","zwift":"Zwift"}
            if isinstance(_mfr_raw, str):
                _mfr_display = _MFR_NAMES.get(_mfr_raw.lower(), _mfr_raw.title())
            elif isinstance(_mfr_raw, int) and _mfr_raw == 1:
                _mfr_display = "Garmin"
            else:
                _mfr_display = str(_mfr_raw)
            # Garmin product IDs
            _GARMIN_PRODUCTS = {4061:"Edge 540",4062:"Edge 540 Solar",4063:"Edge 840",
                                4064:"Edge 840 Solar",3943:"Edge 1040",3944:"Edge 1040 Solar",
                                3570:"Edge 530",3121:"Edge 830",3112:"Edge 1030 Plus",
                                3589:"Edge 130 Plus",3837:"Forerunner 955",3990:"Forerunner 265"}
            if _prod_name:
                display_name = _prod_name
            elif _mfr_display == "Garmin" and isinstance(_prod_id, int) and _prod_id in _GARMIN_PRODUCTS:
                display_name = f"Garmin {_GARMIN_PRODUCTS[_prod_id]}"
            elif _mfr_display and _mfr_display not in ("0","1","256"):
                display_name = _mfr_display
            else:
                display_name = fname.replace('.fit','').replace('.FIT','')
            self.send_json({
                "ok": True, "idx": idx,
                "name": fname, "size_kb": round(len(fdata)/1024,1),
                "records": len(recs), "hrv": len(hrv),
                "start": start_ts, "end": end_ts, "duration": dur,
                "sport": str(ses.get("sport","?")),
                "sub_sport": str(ses.get("sub_sport","")),
                "fields": fields,
                "utc_offset_s": utc_off,
                "field_meta": {f: (FIELD_META.get(f) or {k:v for k,v in _extra_record_field_meta(next((rf["field_num"] for rf in extra_record_fields if rf["name"] == f), -1)).items() if k in ("icon","cat","desc")} or {"icon":"·","cat":"Other","desc":f}) for f in fields},
                "dev_fields": dev_fields,
                "extra_std_fields": extra_record_fields,
                "device": {"product_name": device_meta.get("product_name", ""), "manufacturer": device_meta.get("manufacturer", 1), "product": device_meta.get("product", 0)},
                "display_name": display_name,
            })
        except Exception as e:
            self.send_json({"error":str(e)},500)

    def handle_merge(self, body):
        try:
            req = json.loads(body)
        except Exception:
            self.send_json({"error":"Invalid JSON"},400); return
        if not isinstance(req, dict):
            self.send_json({"error":"JSON body must be an object"},400); return

        if 0 not in Handler.uploads or 1 not in Handler.uploads:
            self.send_json({"error":"Upload both files first"},400); return

        selected = set(req.get("fields",[]))
        conflicts = req.get("conflicts",{})
        utc_offset = int(req.get("utc_offset_s",0))
        include_hrv = req.get("include_hrv",True)
        selected_dev = set(req.get("dev_fields", []))
        device_source = req.get("device_source", "f1")
        time_source = req.get("time_source", "f2")
        force_virtual = bool(req.get("force_virtual", False))
        trim_to_shorter = bool(req.get("trim_to_shorter", False))
        f1_start_offset = int(req.get("f1_start_offset_s", 0) or 0)

        try:
            out_path, stats = do_merge(
                Handler.uploads[0]["path"],
                Handler.uploads[1]["path"],
                selected, conflicts, utc_offset, include_hrv, selected_dev, device_source, time_source, force_virtual, trim_to_shorter, f1_start_offset
            )
            Handler.last_result = out_path
            self.send_json({"ok":True,"stats":stats})
        except Exception as e:
            import traceback; traceback.print_exc()
            self.send_json({"error":str(e)},500)

    def serve_file(self, path):
        with open(path,"rb") as f: data = f.read()
        self.send_response(200)
        self.send_header("Content-Type","application/octet-stream")
        self.send_header("Content-Disposition",'attachment; filename="merged_activity.fit"')
        self.send_header("Content-Length",len(data))
        self.send_header("Access-Control-Allow-Origin","*")
        self.end_headers()
        self.wfile.write(data)

    def serve_html(self):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            "fit_forge_reviewed_fixed.html",
            "fit_forge_fixed.html",
            "fit_forge.html",
        ]
        html_path = None
        for name in candidates:
            candidate = os.path.join(base_dir, name)
            if os.path.exists(candidate):
                html_path = candidate
                break
        if not html_path:
            msg = (
                "No HTML interface file found.\n\n"
                f"Searched folder: {base_dir}\n"
                "Tried:\n - fit_forge_reviewed_fixed.html\n - fit_forge_fixed.html\n - fit_forge.html\n"
            ).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(msg))
            self.end_headers()
            self.wfile.write(msg)
            return
        with open(html_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.send_header("X-FIT-Forge-HTML", os.path.basename(html_path))
        self.end_headers()
        self.wfile.write(data)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    server = HTTPServer(("localhost", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"\n{'─'*50}")
    print(f"  🚴 FIT Forge Server")
    print(f"{'─'*50}")
    print(f"  Open in your browser: {url}")
    print(f"  Ctrl+C to stop")
    print(f"{'─'*50}\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
