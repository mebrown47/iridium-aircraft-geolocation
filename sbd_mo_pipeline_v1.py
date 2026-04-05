#!/usr/bin/env python3
"""
sbd_mo_pipeline.py
------------------
Minimal SBD MO detection pipeline for iridium-toolkit UL frame output.

Input:  output_parsed.ul  (iridium-parser.py output, UL frames only)
Output: decoded session table with MOMSN, fragment count, payload

Usage:
    python3 sbd_mo_pipeline.py output_parsed.ul
    cat output_parsed.ul | python3 sbd_mo_pipeline.py -

Timestamp notes:
  - Frame name field: p-<unix_epoch>-e<id>
    The unix_epoch embedded in the name is the capture file start time.
  - mstime field (e.g. 000009199.5579): milliseconds elapsed since capture start.
  - Wall clock = capture_start + mstime/1000
  - Third numeric field (e.g. 1622383676): receive frequency in Hz (~1622 MHz),
    NOT a Unix timestamp. Do not interpret as time.
"""

import re
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# STEP 1 — Parse raw frame lines into structured dicts
# ---------------------------------------------------------------------------

def parse_frame(line):
    """Extract fields from a single IDA: UL frame line."""
    if not line.startswith("IDA:"):
        return None

    # Name encodes capture start epoch: p-<start_unix>-e<id>
    name_match    = re.search(r'IDA: p-(\d+)-(\S+)', line)
    mstime_match  = re.search(r'IDA: \S+ (\d+\.\d+)', line)
    ctr_match     = re.search(r'ctr=(\d+)', line)
    cont_match    = re.search(r'cont=(\d)', line)
    len_match     = re.search(r'len=(\d+)', line)
    payload_match = re.search(r'\[([0-9a-f]{2}(?:\.[0-9a-f]{2})*)\]', line)
    pos_match     = re.search(r'([-\d.]+)\|([-\d.]+)\|([\d.]+)', line)
    # Third numeric field is frequency in Hz, not time
    freq_match    = re.search(r'IDA: \S+ \S+ (\d{10})', line)

    if not name_match or not mstime_match:
        return None

    capture_start = int(name_match.group(1))
    antenna_id    = name_match.group(2)
    mstime        = float(mstime_match.group(1))
    wall_clock    = capture_start + mstime / 1000   # correct wall time

    payload = b""
    if payload_match:
        payload = bytes.fromhex(payload_match.group(1).replace('.', ''))

    return {
        "src":           f"p-{capture_start}-{antenna_id}",
        "antenna_id":    antenna_id,
        "capture_start": capture_start,
        "mstime":        mstime,
        "wall_clock":    wall_clock,            # seconds since unix epoch
        "freq_hz":       int(freq_match.group(1)) if freq_match else None,
        "ctr":           int(ctr_match.group(1)) if ctr_match else -1,
        "cont":          int(cont_match.group(1)) if cont_match else -1,
        "len":           int(len_match.group(1)) if len_match else 0,
        "payload":       payload,
        "sat_lat":       float(pos_match.group(1)) if pos_match else None,
        "sat_lon":       float(pos_match.group(2)) if pos_match else None,
    }


# ---------------------------------------------------------------------------
# STEP 2 — Reassemble multi-fragment bursts into complete MO messages
# ---------------------------------------------------------------------------

def reassemble(frames):
    """
    Group IDA frames into sessions.

    Rules:
      - ctr=000, cont=1  → start of a multi-fragment session
      - ctr>0            → continuation fragment, append to current session
      - cont=0 on ctr>0  → final fragment, close session
      - ctr=000, cont=0  → standalone single-frame message
    """
    sessions = []
    current  = []

    for f in sorted(frames, key=lambda x: x['wall_clock']):
        if f['ctr'] == 0 and f['cont'] == 1:
            if current:
                sessions.append(current)   # flush incomplete session
            current = [f]
        elif current and f['ctr'] > 0:
            current.append(f)
            if f['cont'] == 0:
                sessions.append(current)
                current = []
        else:
            sessions.append([f])           # standalone

    if current:
        sessions.append(current)

    return sessions


# ---------------------------------------------------------------------------
# STEP 3 — Extract MOMSN from reassembled payload
# ---------------------------------------------------------------------------

def extract_momsn(payload):
    """
    Two header types observed in this terminal:

    Type A (byte[2] == 0x10):  20-byte session header
        byte[18] = MOMSN (values seen: 1, 2, 3, 11)

    Type B (byte[2] == 0x20):  20-byte session header
        byte[16] = MOMSN (values seen: 106–113, monotonically increasing)

    Returns (momsn, type_label) or (None, None) if not decodable.
    """
    if len(payload) < 19:
        return None, None
    if payload[2] == 0x20:
        return payload[16], "TypeB"
    if payload[2] == 0x10:
        return payload[18], "TypeA"
    return None, None


# ---------------------------------------------------------------------------
# STEP 4 — Display
# ---------------------------------------------------------------------------

def display_session(idx, session):
    """Print one decoded session to stdout."""
    frms         = sorted(session, key=lambda x: x['ctr'])
    full_payload = b"".join(f['payload'] for f in frms)
    anchor       = frms[0]

    ts_str  = datetime.fromtimestamp(anchor['wall_clock'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    sat_pos = f"({anchor['sat_lat']:.2f}, {anchor['sat_lon']:.2f})" if anchor['sat_lat'] else "unknown"
    freq_str = f"{anchor['freq_hz']/1e6:.4f} MHz" if anchor['freq_hz'] else "unknown"

    momsn, mtype = extract_momsn(full_payload)
    stype = "multi" if len(session) > 1 else "single"

    # Scan reassembled payload for NMEA
    text = full_payload.decode('ascii', errors='replace')
    nmea = None
    for marker in ['$GP', '$II', '$IN', '$GL']:
        if marker in text:
            nmea = text[text.find(marker):].split('\n')[0]
            break

    print(f"{'─'*72}")
    print(f"Session {idx:02d}  [{stype}]  {ts_str}  sat={sat_pos}")
    print(f"  Source    : {anchor['src']}  antenna={anchor['antenna_id']}")
    print(f"  Frequency : {freq_str}")
    print(f"  Fragments : {len(session)}  |  Total bytes: {len(full_payload)}")

    if momsn is not None:
        print(f"  MOMSN     : {momsn}  (0x{momsn:02x})  [{mtype}]")
    else:
        print(f"  MOMSN     : not decodable (single-frame or unknown header)")

    print(f"  Payload   : {full_payload.hex()}")

    if nmea:
        print(f"  *** NMEA  : {nmea}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "-"
    fh   = open(path) if path != "-" else sys.stdin

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

    capture_start = datetime.fromtimestamp(frames[0]['capture_start'], tz=timezone.utc)
    print(f"\nSBD MO Pipeline")
    print(f"  Capture start : {capture_start.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  UL IDA frames : {len(frames)}")

    sessions = reassemble(frames)

    multi_sessions = [s for s in sessions if len(s) > 1]
    momsn_sessions = [s for s in multi_sessions
                      if extract_momsn(b"".join(f['payload']
                          for f in sorted(s, key=lambda x: x['ctr'])))[0] is not None]

    print(f"  Sessions      : {len(sessions)}  ({len(multi_sessions)} multi-fragment, {len(momsn_sessions)} with MOMSN)")
    print()

    for idx, session in enumerate(sessions, 1):
        display_session(idx, session)

    # Summary: MOMSN sequence table
    print(f"{'═'*72}")
    print("MOMSN SEQUENCE TABLE")
    print(f"{'─'*72}")
    print(f"{'Sess':>4}  {'MOMSN':>6}  {'Type':>6}  {'Timestamp (UTC)':>22}  {'Sat position'}")
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

        print(f"{idx:>4}  {momsn:>6}  {mtype:>6}  {ts_str}  {sat_pos}{gap}")
        prev_momsn = momsn if mtype == "TypeB" else prev_momsn

    print(f"{'─'*72}")
    print()


if __name__ == "__main__":
    main()
