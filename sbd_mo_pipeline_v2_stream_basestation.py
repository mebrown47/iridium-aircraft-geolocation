#!/usr/bin/env python3
"""
sbd_mo_pipeline_v2.py
---------------------
SBD MO detection + content extraction pipeline for iridium-toolkit UL frame output.

Builds on v1 (session reassembly + MOMSN) and adds:
  - Parity-stripped ASCII decoding for all payloads
  - SBD protocol header parsing (0x76 message types)
  - ACARS message extraction (registration, label, route/waypoints)
  - NMEA sentence detection (raw and parity-encoded)
  - Geographic coordinate extraction (DDMMSS, decimal, NOTAM format)
  - METAR/TAF/NOTAM keyword extraction
  - Position request (REQPOS) detection
  - Aircraft registration catalog
  - Waypoint route reconstruction

Input:  output_parsed.ul  (iridium-parser.py output, UL frames only)
Output: decoded session table + content extraction + position summary

Usage:
    python3 sbd_mo_pipeline_v2.py output_parsed.ul
    python3 sbd_mo_pipeline_v2.py output_parsed.ul --positions-only
    python3 sbd_mo_pipeline_v2.py output_parsed.ul --aircraft-summary
    python3 sbd_mo_pipeline_v2.py output_parsed.ul --basestation-out
    python3 sbd_mo_pipeline_v2.py output_parsed.ul --stream-json
"""

import re
import sys
import struct
import math
import json
import os
import zlib
from datetime import datetime, timezone
from collections import defaultdict, Counter

# ---------------------------------------------------------------------------
# NAVAID / FIX DATABASE
# ---------------------------------------------------------------------------
# Load from faa_navaid_database.json (73K+ entries: VOR/VORTAC/NDB + named fixes)
# Format: { "FIX_ID": [lat, lon] } or { "FIX_ID": [[lat1,lon1],[lat2,lon2]] } for ambiguous

NAVAID_DB = {}

def load_navaid_db(path=None):
    """Load the NAVAID/fix database from JSON. Searches common locations."""
    global NAVAID_DB
    if path and os.path.exists(path):
        db_path = path
    else:
        # Search relative to script location, then CWD
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'faa_navaid_database.json'),
            os.path.join(os.getcwd(), 'faa_navaid_database.json'),
        ]
        db_path = None
        for c in candidates:
            if os.path.exists(c):
                db_path = c
                break

    if db_path:
        with open(db_path, 'r') as f:
            NAVAID_DB.update(json.load(f))
        return len(NAVAID_DB)
    return 0


def resolve_fix_position(fix_id, bearing_deg, distance_nm, ref_lat=None, ref_lon=None):
    """
    Resolve a fix/bearing/distance triple to lat/lon.

    Args:
        fix_id: NAVAID or waypoint identifier (e.g. 'ABQ', 'PYRIT')
        bearing_deg: bearing from fix in degrees
        distance_nm: distance from fix in nautical miles
        ref_lat, ref_lon: reference position for disambiguating collisions
                          (typically satellite sub-point or receiver location)

    Returns:
        dict with 'lat', 'lon', 'fix_lat', 'fix_lon' or None if fix not found
    """
    if fix_id not in NAVAID_DB:
        return None

    entry = NAVAID_DB[fix_id]

    # Handle ambiguous entries (multiple fixes with same ID)
    if isinstance(entry[0], list):
        if ref_lat is not None and ref_lon is not None:
            # Pick nearest to reference point
            best = min(entry, key=lambda e: (e[0]-ref_lat)**2 + (e[1]-ref_lon)**2)
            fix_lat, fix_lon = best
        else:
            # Default to first entry (usually US for aviation context)
            fix_lat, fix_lon = entry[0]
    else:
        fix_lat, fix_lon = entry

    # Great-circle forward projection
    R = 3440.065  # Earth radius in nautical miles
    lat1 = math.radians(fix_lat)
    lon1 = math.radians(fix_lon)
    brg = math.radians(bearing_deg)
    d = distance_nm / R

    lat2 = math.asin(
        math.sin(lat1) * math.cos(d) +
        math.cos(lat1) * math.sin(d) * math.cos(brg)
    )
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2)
    )

    return {
        'lat': math.degrees(lat2),
        'lon': math.degrees(lon2),
        'fix_lat': fix_lat,
        'fix_lon': fix_lon,
    }


# ---------------------------------------------------------------------------
# STEP 1 — Parse raw frame lines into structured dicts
# ---------------------------------------------------------------------------

def parse_frame(line):
    """Extract fields from a single IDA: UL frame line."""
    if not line.startswith("IDA:"):
        return None

    name_match    = re.search(r'IDA: p-(\d+)-(\S+)', line)
    mstime_match  = re.search(r'IDA: \S+ (\d+\.\d+)', line)
    ctr_match     = re.search(r'ctr=(\d+)', line)
    cont_match    = re.search(r'cont=(\d)', line)
    len_match     = re.search(r'len=(\d+)', line)
    payload_match = re.search(r'\[([0-9a-f]{2}(?:\.[0-9a-f]{2})*)\]', line)
    pos_match     = re.search(r'([-\d.]+)\|([-\d.]+)\|([\d.]+)', line)
    freq_match    = re.search(r'IDA: \S+ \S+ (\d{10})', line)

    if not name_match or not mstime_match:
        return None

    capture_start = int(name_match.group(1))
    antenna_id    = name_match.group(2)
    mstime        = float(mstime_match.group(1))
    wall_clock    = capture_start + mstime / 1000

    payload = b""
    if payload_match:
        payload = bytes.fromhex(payload_match.group(1).replace('.', ''))

    return {
        "src":           f"p-{capture_start}-{antenna_id}",
        "antenna_id":    antenna_id,
        "capture_start": capture_start,
        "mstime":        mstime,
        "wall_clock":    wall_clock,
        "freq_hz":       int(freq_match.group(1)) if freq_match else None,
        "ctr":           int(ctr_match.group(1)) if ctr_match else -1,
        "cont":          int(cont_match.group(1)) if cont_match else -1,
        "len":           int(len_match.group(1)) if len_match else 0,
        "payload":       payload,
        "sat_lat":       float(pos_match.group(1)) if pos_match else None,
        "sat_lon":       float(pos_match.group(2)) if pos_match else None,
    }


# ---------------------------------------------------------------------------
# STEP 2 — Reassemble multi-fragment bursts
# ---------------------------------------------------------------------------

def reassemble(frames):
    sessions = []
    current  = []

    for f in sorted(frames, key=lambda x: x['wall_clock']):
        if f['ctr'] == 0 and f['cont'] == 1:
            if current:
                sessions.append(current)
            current = [f]
        elif current and f['ctr'] > 0:
            current.append(f)
            if f['cont'] == 0:
                sessions.append(current)
                current = []
        else:
            sessions.append([f])

    if current:
        sessions.append(current)

    return sessions


# ---------------------------------------------------------------------------
# STEP 3 — MOMSN extraction (from v1)
# ---------------------------------------------------------------------------

def extract_momsn(payload):
    if len(payload) < 19:
        return None, None
    if payload[2] == 0x20:
        return payload[16], "TypeB"
    if payload[2] == 0x10:
        return payload[18], "TypeA"
    return None, None


# ---------------------------------------------------------------------------
# STEP 4 — Content extraction engine
# ---------------------------------------------------------------------------

def strip_parity(data):
    """Strip bit-7 parity from Iridium SBD ACARS encoding."""
    return bytes([b & 0x7F for b in data])


def to_printable(data, strip_par=True):
    """Convert bytes to printable ASCII, stripping parity if requested."""
    if strip_par:
        data = strip_parity(data)
    return ''.join(chr(b) if 32 <= b < 127 else '.' for b in data)


def extract_content(payload):
    """
    Extract structured content from a reassembled SBD MO payload.

    Returns a dict with whatever was found:
      - registration, label, acars_text, waypoints, coordinates,
        metar/taf/notam, reqpos, sbd_type, nmea, freetext
    """
    result = {}

    if not payload or len(payload) < 2:
        return result

    # --- SBD protocol header detection ---
    if payload[0] == 0x76:
        result['sbd_header'] = payload[:4].hex()
        sbd_subtype = payload[1] if len(payload) > 1 else 0

        if sbd_subtype in (0x08, 0x09, 0x0a):
            # Multi-part SBD with ACARS content
            # Header structure: 76 08/09 26 XX ... then ACARS payload
            result['sbd_type'] = 'acars_bearer'
        elif sbd_subtype == 0x05:
            result['sbd_type'] = 'sbd_ack_or_ctrl'
            return result  # Short control frame, skip deep parsing

    # --- Parity-stripped text for content analysis ---
    stripped = strip_parity(payload)
    text = stripped.decode('ascii', errors='replace')

    # --- Aircraft registration (N-number, C-registration) ---
    # Look in the SBD header region where toolkit embeds it
    reg_match = re.search(r'([NC]-?[A-Z]{4}|N\d{2,5}[A-Z]{0,2})', text)
    if reg_match:
        reg = reg_match.group(1)
        # Filter out false positives from numeric coincidences
        if len(reg) >= 4 and not reg.startswith('N07'):  # N07615 is a coordinate fragment
            result['registration'] = reg

    # --- ACARS label ---
    label_match = re.search(r'[.\x15]([A-Z0-9_]{2})\d?[.\x02]', text)
    if label_match:
        result['acars_label'] = label_match.group(1)

    # --- NMEA sentences (raw and parity-encoded) ---
    # Raw ASCII
    for marker in ['$GP', '$GN', '$GL', '$II', '$IN', '$HC', '$HE', '$BD']:
        if marker in text:
            idx = text.index(marker)
            nmea_line = text[idx:].split('\n')[0].split('\r')[0]
            result['nmea'] = nmea_line
            break
    # Parity-encoded NMEA: $=0xA4, G=0xC7, P=0xD0
    parity_markers = [
        (b'\xa4\xc7\xd0', '$GP'),
        (b'\xa4\xc7\xce', '$GN'),
        (b'\xa4\xc7\xcc', '$GL'),
    ]
    for marker_bytes, marker_name in parity_markers:
        if marker_bytes in payload:
            idx = payload.index(marker_bytes)
            raw_region = payload[idx:idx+82]
            nmea_stripped = strip_parity(raw_region).decode('ascii', errors='replace')
            # Find the end (CR, LF, or non-printable)
            end = len(nmea_stripped)
            for i, c in enumerate(nmea_stripped):
                if c in ('\r', '\n') or ord(c) < 32:
                    end = i
                    break
            result['nmea_parity'] = nmea_stripped[:end]
            break

    # --- Geographic coordinates ---
    # DDMMSS.SS format: 364940.40N0761506.5W (full)
    # Also handle partial: 364940.40N0761506.5 (W truncated at payload boundary)
    coord_match = re.search(
        r'(\d{2})(\d{2})(\d{2}\.?\d*)\s*(N|S)\s*(\d{2,3})(\d{2})(\d{2}\.?\d*)\s*(E|W)',
        text
    )
    # Fallback: lat+lon without hemisphere suffix (common in truncated NOTAM fragments)
    if not coord_match:
        coord_match = re.search(
            r'(\d{2})(\d{2})(\d{2}\.\d+)(N|S)(\d{3})(\d{2})(\d{2}\.\d*)',
            text
        )
    if coord_match:
        lat_d = int(coord_match.group(1))
        lat_m = int(coord_match.group(2))
        lat_s = float(coord_match.group(3))
        lat = lat_d + lat_m / 60 + lat_s / 3600
        if coord_match.group(4) == 'S':
            lat = -lat

        lon_d = int(coord_match.group(5))
        lon_m = int(coord_match.group(6))
        lon_s = float(coord_match.group(7))
        lon = lon_d + lon_m / 60 + lon_s / 3600
        # group(8) may not exist in fallback regex (no W/E suffix)
        try:
            if coord_match.group(8) == 'W':
                lon = -lon
        except IndexError:
            # Infer hemisphere from longitude magnitude (>100 = likely Western)
            if lon_d > 100:
                lon = -lon

        result['coordinates'] = {
            'lat': lat, 'lon': lon,
            'raw': coord_match.group(0),
            'format': 'DDMMSS'
        }

    # --- REQPOS (position request from ground to aircraft) ---
    if 'REQPOS' in text:
        result['reqpos'] = True
        reqpos_match = re.search(r'REQPOS(\d+)', text)
        if reqpos_match:
            result['reqpos_id'] = reqpos_match.group(1)

    # --- Waypoint routes (bearing/distance from fixes) ---
    # Pattern: WAYPOINT,BBBDDD (bearing 3 digits, distance 3 digits)
    waypoints = re.findall(r'([A-Z]{2,6}),\s*(\d{3})(\d{3})', text)
    if len(waypoints) >= 2:
        wp_list = []
        for wp in waypoints:
            entry = {'fix': wp[0], 'bearing': int(wp[1]), 'distance': int(wp[2])}
            # Resolve to lat/lon if NAVAID DB is loaded
            if NAVAID_DB:
                resolved = resolve_fix_position(
                    wp[0], int(wp[1]), int(wp[2]),
                    ref_lat=30.45, ref_lon=-91.19  # Baton Rouge default; overridden below if sat pos available
                )
                if resolved:
                    entry['lat'] = resolved['lat']
                    entry['lon'] = resolved['lon']
                    entry['fix_lat'] = resolved['fix_lat']
                    entry['fix_lon'] = resolved['fix_lon']
            wp_list.append(entry)
        result['waypoints'] = wp_list

    # --- Wind data (WI/WD or /WD or /DD format) ---
    wind_match = re.findall(r'(?:WI|/W|/D)D?\s*(\d{3})(\d{3})(\d{3})', text)
    if wind_match:
        result['wind_data'] = [
            {'dir': int(w[0]), 'speed': int(w[1]), 'alt_or_temp': int(w[2])}
            for w in wind_match
        ]

    # --- METAR/TAF/NOTAM/PIREP ---
    for keyword in ['METAR', 'TAF', 'NOTAM', 'PIREP']:
        if keyword in text:
            # Extract the airport code
            airport_match = re.search(r'K[A-Z]{3}', text)
            result['wx_type'] = keyword
            if airport_match:
                result['wx_airport'] = airport_match.group(0)
            # Extract the text content
            idx = text.index(keyword)
            wx_text = text[idx:idx+200].replace('\r', ' ').replace('\x00', '').strip()
            result['wx_text'] = wx_text
            break

    # --- Landing/performance data ---
    if 'LDG WT' in text or 'TOGW' in text or 'FLAP' in text:
        result['performance_data'] = True
        perf_text = text.replace('\r', '\n').strip()
        result['performance_text'] = perf_text[:200]

    # --- Freetext messages (human-readable SBD messages) ---
    readable_ratio = sum(1 for b in stripped if 32 <= b < 127) / max(len(stripped), 1)
    if readable_ratio > 0.7 and len(payload) > 25:
        clean = text.replace('\r', ' ').replace('\x00', '').strip()
        # Filter out binary-looking content
        if len(clean) > 10 and any(c.isalpha() for c in clean):
            result['freetext'] = clean[:300]

    return result


# ---------------------------------------------------------------------------
# STEP 5 — Display
# ---------------------------------------------------------------------------

def display_session(idx, session, content, verbose=True):
    """Print one decoded session with content extraction."""
    frms         = sorted(session, key=lambda x: x['ctr'])
    full_payload = b"".join(f['payload'] for f in frms)
    anchor       = frms[0]

    ts_str  = datetime.fromtimestamp(anchor['wall_clock'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    sat_pos = f"({anchor['sat_lat']:.2f}, {anchor['sat_lon']:.2f})" if anchor['sat_lat'] else "unknown"
    freq_str = f"{anchor['freq_hz']/1e6:.4f} MHz" if anchor['freq_hz'] else "unknown"

    momsn, mtype = extract_momsn(full_payload)
    stype = "multi" if len(session) > 1 else "single"

    # Only display sessions with interesting content unless verbose
    has_content = any(k in content for k in (
        'registration', 'nmea', 'nmea_parity', 'coordinates', 'waypoints',
        'reqpos', 'wx_type', 'performance_data', 'freetext'
    ))

    if not verbose and not has_content and momsn is None:
        return False  # Signal: not displayed

    print(f"{'─'*72}")
    print(f"Session {idx:05d}  [{stype}]  {ts_str}  sat={sat_pos}")
    print(f"  Source    : {anchor['src']}  antenna={anchor['antenna_id']}")
    print(f"  Frequency : {freq_str}")
    print(f"  Fragments : {len(session)}  |  Total bytes: {len(full_payload)}")

    if momsn is not None:
        print(f"  MOMSN     : {momsn}  (0x{momsn:02x})  [{mtype}]")

    if content.get('sbd_header'):
        print(f"  SBD Header: {content['sbd_header']}  type={content.get('sbd_type', 'unknown')}")

    if content.get('registration'):
        print(f"  Aircraft  : {content['registration']}")

    if content.get('acars_label'):
        print(f"  ACARS Lbl : {content['acars_label']}")

    if content.get('nmea'):
        print(f"  *** NMEA  : {content['nmea']}")

    if content.get('nmea_parity'):
        print(f"  *** NMEA(p): {content['nmea_parity']}")

    if content.get('coordinates'):
        c = content['coordinates']
        print(f"  *** POSITION: {c['lat']:.6f}°, {c['lon']:.6f}°  (raw: {c['raw']})")

    if content.get('reqpos'):
        print(f"  *** REQPOS: Position request (ID: {content.get('reqpos_id', '?')})")

    if content.get('waypoints'):
        wps = content['waypoints']
        wp_str = ' → '.join(f"{w['fix']}({w['bearing']:03d}/{w['distance']:03d})" for w in wps[:6])
        if len(wps) > 6:
            wp_str += f" ... +{len(wps)-6} more"
        print(f"  Route     : {wp_str}")

    if content.get('wx_type'):
        print(f"  Weather   : {content['wx_type']} {content.get('wx_airport', '')}")
        if content.get('wx_text'):
            print(f"              {content['wx_text'][:100]}")

    if content.get('performance_data') and verbose:
        lines = content.get('performance_text', '').split('\n')
        for line in lines[:4]:
            line = line.strip()
            if line:
                print(f"  Perf      : {line[:80]}")

    if content.get('freetext') and not any(k in content for k in ('waypoints', 'wx_type', 'performance_data')):
        print(f"  Text      : {content['freetext'][:120]}")

    print(f"  Payload   : {full_payload[:40].hex()}{'...' if len(full_payload)>40 else ''}")
    print()
    return True


# ---------------------------------------------------------------------------
# STEP 6 — Summary reports
# ---------------------------------------------------------------------------

def print_aircraft_summary(all_content):
    """Print a summary of all aircraft observed."""
    aircraft = defaultdict(lambda: {
        'sessions': [], 'labels': set(), 'waypoints': [],
        'positions': [], 'weather': [], 'has_perf': False
    })

    for idx, content in all_content:
        reg = content.get('registration')
        if not reg:
            continue
        aircraft[reg]['sessions'].append(idx)
        if content.get('acars_label'):
            aircraft[reg]['labels'].add(content['acars_label'])
        if content.get('waypoints'):
            aircraft[reg]['waypoints'].extend(content['waypoints'])
        if content.get('coordinates'):
            aircraft[reg]['positions'].append(content['coordinates'])
        if content.get('wx_type'):
            aircraft[reg]['weather'].append(content['wx_type'])
        if content.get('performance_data'):
            aircraft[reg]['has_perf'] = True

    print(f"\n{'═'*72}")
    print("AIRCRAFT CATALOG")
    print(f"{'─'*72}")
    print(f"  {'Registration':12s}  {'Sessions':>8s}  {'Labels':12s}  {'Waypoints':>5s}  {'Positions':>5s}  Notes")
    print(f"{'─'*72}")

    for reg in sorted(aircraft.keys()):
        info = aircraft[reg]
        labels = ','.join(sorted(info['labels'])) or '-'
        n_wp = len(info['waypoints'])
        n_pos = len(info['positions'])
        notes = []
        if info['has_perf']:
            notes.append('PERF')
        if info['weather']:
            notes.append(f"WX:{','.join(set(info['weather']))}")
        notes_str = ' '.join(notes)
        print(f"  {reg:12s}  {len(info['sessions']):>8d}  {labels:12s}  {n_wp:>5d}  {n_pos:>5d}  {notes_str}")

    print(f"{'─'*72}")
    print(f"  Total aircraft: {len(aircraft)}")
    print()


def print_position_summary(all_content, all_sessions):
    """Print all extracted positions."""
    print(f"\n{'═'*72}")
    print("POSITION FIXES")
    print(f"{'─'*72}")

    fixes = []
    for idx, content in all_content:
        if content.get('coordinates'):
            c = content['coordinates']
            reg = content.get('registration', '?')
            session = all_sessions[idx - 1] if idx <= len(all_sessions) else None
            anchor = sorted(session, key=lambda x: x['ctr'])[0] if session else None
            ts = datetime.fromtimestamp(anchor['wall_clock'], tz=timezone.utc).strftime('%H:%M:%S') if anchor else '?'
            sat = f"({anchor['sat_lat']:.1f}, {anchor['sat_lon']:.1f})" if anchor and anchor['sat_lat'] else "?"
            fixes.append((idx, ts, reg, c['lat'], c['lon'], c['raw'], sat))

    if fixes:
        for idx, ts, reg, lat, lon, raw, sat in fixes:
            print(f"  Sess {idx:5d}  {ts}  {reg:10s}  {lat:10.6f}° {lon:11.6f}°  sat={sat}")
            print(f"                              raw: {raw}")
    else:
        print("  No geographic coordinates extracted.")

    # Count resolved waypoint fixes
    resolved_wp_count = 0
    for idx, content in all_content:
        if content.get('waypoints'):
            for wp in content['waypoints']:
                if 'lat' in wp:
                    resolved_wp_count += 1
    if resolved_wp_count > 0:
        print(f"\n  Resolved waypoint positions: {resolved_wp_count}")

    # Also list REQPOS sessions
    reqpos = [(idx, c) for idx, c in all_content if c.get('reqpos')]
    if reqpos:
        print(f"\n  REQPOS (position requests):")
        for idx, c in reqpos:
            reg = c.get('registration', '?')
            print(f"    Sess {idx:5d}  {reg}")

    print(f"{'─'*72}")
    print()


def print_route_summary(all_content):
    """Print waypoint routes grouped by aircraft."""
    print(f"\n{'═'*72}")
    print("WAYPOINT ROUTES")
    print(f"{'─'*72}")

    routes_by_aircraft = defaultdict(list)
    for idx, content in all_content:
        if content.get('waypoints'):
            reg = content.get('registration', '?')
            routes_by_aircraft[reg].append((idx, content['waypoints']))

    for reg in sorted(routes_by_aircraft.keys()):
        routes = routes_by_aircraft[reg]
        print(f"\n  {reg}  ({len(routes)} route segments):")
        # Combine all waypoints to show the full route
        all_fixes = []
        seen = set()
        for idx, wps in routes:
            for wp in wps:
                key = wp['fix']
                if key not in seen:
                    seen.add(key)
                    all_fixes.append(wp)
        for wp in all_fixes:
            if 'lat' in wp and 'lon' in wp:
                print(f"    {wp['fix']:8s}  brg {wp['bearing']:03d}°  dist {wp['distance']:3d} nm  ->  {wp['lat']:8.4f}°, {wp['lon']:9.4f}°")
            else:
                print(f"    {wp['fix']:8s}  brg {wp['bearing']:03d}°  dist {wp['distance']:3d} nm  ->  (unresolved)")

    if not routes_by_aircraft:
        print("  No waypoint routes extracted.")
    print(f"{'─'*72}")
    print()


def print_payload_statistics(all_payloads):
    """Print payload type distribution."""
    print(f"\n{'═'*72}")
    print("PAYLOAD STATISTICS")
    print(f"{'─'*72}")

    len_counts = Counter(len(p) for p in all_payloads)
    print(f"  Total sessions: {len(all_payloads)}")
    print(f"\n  Length distribution:")
    for length, count in sorted(len_counts.items()):
        bar = '█' * min(count // 100, 50)
        print(f"    {length:3d} bytes: {count:6d}  {bar}")

    # 23-byte patterns (the dominant structured binary class)
    p23 = [p for p in all_payloads if len(p) == 23]
    unique_23 = len(set(p.hex() for p in p23))
    print(f"\n  23-byte payloads: {len(p23)} total, {unique_23} unique patterns")
    print(f"  (These are likely periodic status/registration frames)")

    # 2-byte payloads
    p2 = Counter(p.hex() for p in all_payloads if len(p) == 2)
    if p2:
        print(f"\n  2-byte payloads (likely ACKs/status):")
        for hexstr, count in p2.most_common(5):
            print(f"    {hexstr}: {count}")

    print(f"{'─'*72}")
    print()




# ---------------------------------------------------------------------------
# Basestation / SBS-1 output
# ---------------------------------------------------------------------------

def pseudo_hex_ident(registration, lat=None, lon=None):
    """Create a deterministic pseudo ICAO24-like hex ident for non-ADS-B sources."""
    key = registration or "IRIDIUM"
    if lat is not None and lon is not None:
        key = f"{key}:{round(lat, 3)}:{round(lon, 3)}"
    crc = zlib.crc32(key.encode("utf-8")) & 0xFFFFFF
    return f"{crc:06X}"


def format_basestation_timestamp(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y/%m/%d"), dt.strftime("%H:%M:%S.000")


def emit_basestation_line(registration, lat, lon, ts, altitude="", speed="", track="", vrate="", squawk="", onground="0"):
    """Emit a Basestation/SBS-1 MSG,3 position line."""
    hex_ident = pseudo_hex_ident(registration, lat, lon)
    callsign = registration or ""
    gen_date, gen_time = format_basestation_timestamp(ts)
    log_date, log_time = gen_date, gen_time

    fields = [
        "MSG", "3", "1", "1", hex_ident, "1",
        gen_date, gen_time, log_date, log_time,
        callsign, str(altitude), str(speed), str(track),
        f"{lat:.6f}", f"{lon:.6f}", str(vrate), str(squawk),
        "0", "0", "0", str(onground)
    ]
    print(",".join(fields))


def emit_basestation_output(all_content, sessions):
    """Emit Basestation-format lines for direct coordinates and resolved waypoint positions."""
    emitted = 0
    seen = set()

    for idx, session in enumerate(sessions, 1):
        _, content = all_content[idx - 1]
        frms = sorted(session, key=lambda x: x['ctr'])
        anchor = frms[0]
        ts = anchor['wall_clock']
        registration = content.get('registration') or f"IRDM{idx:05d}"

        if content.get('coordinates'):
            c = content['coordinates']
            lat = c.get('lat')
            lon = c.get('lon')
            if lat is not None and lon is not None:
                key = (registration, round(lat, 6), round(lon, 6), int(ts))
                if key not in seen:
                    emit_basestation_line(registration, lat, lon, ts)
                    seen.add(key)
                    emitted += 1

        for wp in content.get('waypoints', []):
            if 'lat' in wp and 'lon' in wp:
                lat = wp['lat']
                lon = wp['lon']
                key = (registration, round(lat, 6), round(lon, 6), int(ts), wp.get('fix', ''))
                if key not in seen:
                    emit_basestation_line(registration, lat, lon, ts)
                    seen.add(key)
                    emitted += 1

    return emitted




# ---------------------------------------------------------------------------
# JSON stream output for live visualization
# ---------------------------------------------------------------------------

def iso_utc_from_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit_stream_event(registration, lat, lon, ts, source="iridium_acars", fix=None, bearing_deg=None, distance_nm=None):
    event = {
        "type": "iridium_position",
        "reg": registration or "",
        "lat": float(lat),
        "lon": float(lon),
        "timestamp": iso_utc_from_ts(ts),
        "source": source,
    }
    if fix is not None:
        event["fix"] = fix
    if bearing_deg is not None:
        event["bearing_deg"] = int(bearing_deg)
    if distance_nm is not None:
        event["distance_nm"] = int(distance_nm)

    print(json.dumps(event, separators=(",", ":")))


def emit_stream_json_output(all_content, sessions):
    """Emit newline-delimited JSON events for direct coordinates and resolved waypoint positions."""
    emitted = 0
    seen = set()

    for idx, session in enumerate(sessions, 1):
        _, content = all_content[idx - 1]
        frms = sorted(session, key=lambda x: x['ctr'])
        anchor = frms[0]
        ts = anchor['wall_clock']
        registration = content.get('registration') or f"IRDM{idx:05d}"

        # Direct coordinate extraction
        if content.get('coordinates'):
            c = content['coordinates']
            lat = c.get('lat')
            lon = c.get('lon')
            if lat is not None and lon is not None:
                key = ("coord", registration, round(lat, 6), round(lon, 6), int(ts))
                if key not in seen:
                    emit_stream_event(registration, lat, lon, ts)
                    seen.add(key)
                    emitted += 1

        # Waypoint-derived positions
        for wp in content.get('waypoints', []):
            if 'lat' in wp and 'lon' in wp:
                lat = wp['lat']
                lon = wp['lon']
                key = ("wp", registration, round(lat, 6), round(lon, 6), int(ts), wp.get('fix', ''))
                if key not in seen:
                    emit_stream_event(
                        registration,
                        lat,
                        lon,
                        ts,
                        fix=wp.get("fix"),
                        bearing_deg=wp.get("bearing"),
                        distance_nm=wp.get("distance"),
                    )
                    seen.add(key)
                    emitted += 1

    return emitted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]

    positions_only = '--positions-only' in args
    aircraft_only = '--aircraft-summary' in args
    verbose = '--verbose' in args or '-v' in args
    basestation_out = '--basestation-out' in args
    stream_json = '--stream-json' in args

    # Remove flags from args
    path_args = [a for a in args if not a.startswith('-')]
    path = path_args[0] if path_args else "-"
    fh = open(path) if path != "-" else sys.stdin

    # Parse frames
    frames = []
    for line in fh:
        f = parse_frame(line.strip())
        if f:
            frames.append(f)

    if path != "-":
        fh.close()

    if not frames:
        print("No IDA UL frames found.")
        return

    # Header
    capture_start = datetime.fromtimestamp(frames[0]['capture_start'], tz=timezone.utc)

    # Load NAVAID database
    db_count = load_navaid_db()

    # Reassemble
    sessions = reassemble(frames)
    multi_sessions = [s for s in sessions if len(s) > 1]

    # Extract content from all sessions
    all_payloads = []
    all_content = []
    for idx, session in enumerate(sessions, 1):
        frms = sorted(session, key=lambda x: x['ctr'])
        full_payload = b"".join(f['payload'] for f in frms)
        all_payloads.append(full_payload)

        content = extract_content(full_payload)
        all_content.append((idx, content))

    if stream_json:
        emitted = emit_stream_json_output(all_content, sessions)
        if emitted == 0:
            print("No JSON position events emitted.", file=sys.stderr)
        return

    print(f"\nSBD MO Pipeline v2")
    print(f"  Capture start : {capture_start.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  UL IDA frames : {len(frames)}")

    if db_count:
        print(f"  NAVAID database: {db_count} fixes loaded")
    else:
        print(f"  NAVAID database: not found (waypoints will not be resolved to lat/lon)")

    # Count interesting sessions
    n_with_reg = sum(1 for _, c in all_content if c.get('registration'))
    n_with_pos = sum(1 for _, c in all_content if c.get('coordinates'))
    n_with_wp = sum(1 for _, c in all_content if c.get('waypoints'))
    n_with_nmea = sum(1 for _, c in all_content if c.get('nmea') or c.get('nmea_parity'))
    n_with_wx = sum(1 for _, c in all_content if c.get('wx_type'))
    n_with_reqpos = sum(1 for _, c in all_content if c.get('reqpos'))
    n_with_perf = sum(1 for _, c in all_content if c.get('performance_data'))
    n_with_text = sum(1 for _, c in all_content if c.get('freetext'))
    momsn_sessions = [s for s in multi_sessions
                      if extract_momsn(b"".join(f['payload']
                          for f in sorted(s, key=lambda x: x['ctr'])))[0] is not None]

    print(f"  Sessions      : {len(sessions)}  ({len(multi_sessions)} multi-fragment, {len(momsn_sessions)} with MOMSN)")
    print(f"\n  Content extraction:")
    print(f"    Aircraft registrations : {n_with_reg}")
    print(f"    Geographic coordinates : {n_with_pos}")
    print(f"    Waypoint routes        : {n_with_wp}")
    n_resolved = sum(1 for _, c in all_content
                     for wp in c.get('waypoints', []) if 'lat' in wp)
    n_unresolved = sum(1 for _, c in all_content
                       for wp in c.get('waypoints', []) if 'lat' not in wp)
    if n_resolved or n_unresolved:
        print(f"      Resolved positions   : {n_resolved}  ({n_unresolved} unresolved)")
    print(f"    NMEA sentences         : {n_with_nmea}")
    print(f"    METAR/TAF/NOTAM/PIREP  : {n_with_wx}")
    print(f"    REQPOS requests        : {n_with_reqpos}")
    print(f"    Performance data       : {n_with_perf}")
    print(f"    Freetext messages      : {n_with_text}")
    print()

    if basestation_out:
        emitted = emit_basestation_output(all_content, sessions)
        if emitted == 0:
            print("No basestation position lines emitted.", file=sys.stderr)
        return

    # --- Output based on mode ---
    if positions_only:
        print_position_summary(all_content, sessions)
        print_route_summary(all_content)
        return

    if aircraft_only:
        print_aircraft_summary(all_content)
        print_route_summary(all_content)
        return

    # Full output: display sessions with content
    displayed = 0
    for idx, session in enumerate(sessions, 1):
        _, content = all_content[idx - 1]
        if display_session(idx, session, content, verbose=verbose):
            displayed += 1

    if not verbose:
        print(f"\n  (Displayed {displayed} sessions with content out of {len(sessions)} total)")
        print(f"  (Use --verbose / -v to show all sessions)")

    # Summaries
    print_aircraft_summary(all_content)
    print_position_summary(all_content, sessions)
    print_route_summary(all_content)
    print_payload_statistics(all_payloads)

    # MOMSN table (from v1)
    print(f"{'═'*72}")
    print("MOMSN SEQUENCE TABLE")
    print(f"{'─'*72}")
    print(f"{'Sess':>5}  {'MOMSN':>6}  {'Type':>6}  {'Timestamp (UTC)':>22}  {'Sat position'}")
    print(f"{'─'*72}")

    prev_momsn = None
    for idx, session in enumerate(sessions, 1):
        frms         = sorted(session, key=lambda x: x['ctr'])
        full_payload = b"".join(f['payload'] for f in frms)
        momsn, mtype = extract_momsn(full_payload)
        if momsn is None:
            continue
        anchor = frms[0]
        ts_str = datetime.fromtimestamp(anchor['wall_clock'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        sat_pos = f"({anchor['sat_lat']:.2f}, {anchor['sat_lon']:.2f})" if anchor['sat_lat'] else "?"

        gap = ""
        if prev_momsn is not None and mtype == "TypeB":
            diff = momsn - prev_momsn
            if diff > 1:
                gap = f"  ← GAP: {diff-1} missed"

        print(f"{idx:>5}  {momsn:>6}  {mtype:>6}  {ts_str}  {sat_pos}{gap}")
        prev_momsn = momsn if mtype == "TypeB" else prev_momsn

    print(f"{'─'*72}")
    print()


if __name__ == "__main__":
    main()
