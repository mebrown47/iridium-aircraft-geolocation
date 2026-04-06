# Basestation output note

This pipeline now supports a Basestation/SBS-1 style output mode:

```bash
python3 sbd_mo_pipeline_v2_with_basestation.py output_parsed.ul --basestation-out
```

It emits `MSG,3` position lines for:

- direct coordinates extracted from message content
- resolved waypoint-derived positions

Example:

```text
MSG,3,1,1,ABC123,1,2026/04/05,23:36:07.000,2026/04/05,23:36:07.000,N710CK,,,,35.014800,-113.486200,,,0,0,0,0
```

Notes:

- `HexIdent` is a deterministic pseudo identifier because Iridium ACARS payloads do not provide true ICAO24 values.
- `Callsign` is populated from the extracted aircraft registration when available.
- This mode is intended as an interoperability bridge for tools that already ingest Basestation-style position feeds.
