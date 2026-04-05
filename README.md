
# 📡 Passive Aircraft Geolocation from Iridium ACARS

**Extract aircraft positions from Iridium SBD messages**

---

## 🚀 What This Is

This project demonstrates a method for **passively geolocating aircraft** by analyzing the *semantic content* of ACARS messages transmitted over **Iridium Short Burst Data (SBD)**.

Instead of using Doppler, multilateration, or satellite ephemeris…

> **We read what the aircraft is already telling us.**

---

## 🔥 Key Result

From a **9-minute SDR capture**:

- ✈️ 33 aircraft position fixes (~1–5 NM accuracy)
- 🧾 48 aircraft identified
- 🧭 7 route segments reconstructed
- 📍 Direct lat/lon extracted from NOTAM
- 🌦️ Weather + operational data decoded  
- ⚡ **Zero physics-based computation**

---

## 🧠 Core Insight

Iridium + ACARS already carries **position-bearing information**:

- Waypoint + bearing + distance (`FIX,BBBDDD`)
- ADS-C position reports (planned)
- REQPOS responses (lat/lon)
- Airport + weather references

➡️ The standard tools decode the message  
➡️ **This pipeline interprets what it means**

---

## 🏗️ Pipeline Overview

![Pipeline Diagram](docs/pipeline.png)

```
RF Capture → Burst Decode → SBD Reassembly → Content Extraction → Position Resolution
```

---

## 🗺️ Navigation Database

- Full FAA navigation database included
- ~100,000+ fixes and NAVAIDs
- Enables near-complete waypoint resolution

---

## ⚙️ Requirements

### Hardware
- HackRF One / Airspy R2 (or similar)
- L-band antenna + LNA

### Software
- gr-iridium + iridium-toolkit  or
- iridium-sniffer
- Python 3

---

## ▶️ Quick Start

```bash
iridium-sniffer ... | python3 iridium-parser.py \
  | tee output.parsed \
  | python3 reassembler.py -m acars > output.acars

grep 'IDA:.*UL' output.parsed > output_parsed.ul

python3 sbd_mo_pipeline_v2.py output_parsed.ul
```

---

## 📊 What Gets Extracted

- Aircraft registrations
- Waypoint routes
- Geographic coordinates
- Weather products
- Performance data

---

## 🆚 Comparison

| Feature | iridium-toolkit | This Pipeline |
|--------|----------------|--------------|
| ACARS decode | ✅ | ✅ |
| Payload interpretation | ❌ | ✅ |
| Aircraft position | ❌ | ✅ |

---

## ⚠️ Limitations

- ADS-C extraction pending
- UL/DL direction not tracked
- Track stitching incomplete

---

## 🛣️ Roadmap

- ADS-C decoding
- Track stitching
- Toolkit plugin

---

## 📜 License

MIT License

---

## 📣 Author

Mike (@ElbaSatGuy)
