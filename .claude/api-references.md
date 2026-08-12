# API References — Quick Lookup

## Celestrak GP API
- **Endpoint:** https://celestrak.org/NORAD/elements/gp.php
- **Query params:** ?GROUP=active|CATNR=25544|INTDES=2020-025&FORMAT=csv|json|xml
- **Rate limit:** 1 download per 2h cycle (Policy!)
- **Stop on:** HTTP 301/403/404/50x (M2M clients must halt)
- **Max volume:** 100 MB/day before IP block
- **Docs:** https://celestrak.org/NORAD/documentation/gp-data-formats.php
- **Usage Policy:** https://celestrak.org/usage-policy.php

## OMM/TLE Format Standards (spacedatastandards.org)
- **OMM Standard (CSV/JSON/XML):** https://www.spacedatastandards.org/standards/recommended-standard-definitions
  - Modern format for orbital elements (replaces TLE for 6-digit NORAD numbers)
  - Includes all Mean-Motion data fields
- **TLE Format (Legacy):** https://www.spacedatastandards.org/standards/sgp4-sdp4-propagation
  - Two-line element format (5-digit NORAD numbers, phased out July 2026)
  - Still used for historical data
- **CDM (Conjunction Data Messages):** https://www.spacedatastandards.org/standards/conjunction-data-message
  - Contains TCA, miss distance, collision probability
  - Used for conjunction/collision screening
- **CCSDS Standards Index:** https://www.spacedatastandards.org/standards
  - All official space-data standards (RINEX, ephemeris, etc.)

## Space-Track API
- **URL:** https://space-track.org/
- **Auth:** Login required (free, US Data-Use Agreement)
- **Rate limit:** 30 req/min, 300 req/hour
- **Classes:** gp_history, cdm_public, satcat
- **Python client:** `pip install spacetrack`
- **Docs:** https://space-track.org/documentation
- **Example:** ST.query(class_='gp_history', NORAD_CAT_ID=25544, orderby='epoch desc', limit=100)