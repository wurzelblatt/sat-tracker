# Capstone Project „Satellite Tracking Pipeline" — Architecture, Implementation Plan & Evaluation

## TL;DR
- **Build a batch-oriented Medallion pipeline (Airflow ingest every 2 hours → S3/Parquet or Postgres Bronze/Silver/Gold) and propagate positions NOT materialized, but on-demand in the Streamlit frontend via vectorized `SatrecArray` — a Speed/Streaming layer is over-engineering for this use case.** The reason: from a single OMM/TLE dataset, you can compute the position for any point in time locally via SGP4; the catalog update is rate-limited to a two-hour cycle by Celestrak anyway.
- **Scope for 2 weeks: Must-haves = Ingest + Storage + queryable (Days 1–3); Should-haves = Streamlit map with on-demand propagation (Days 4–8); Nice-to-haves = SNS collision alert via SOCRATES + lean ML classification Payload/Rocket-Body/Debris (Days 9–13); Docs/Deployment (Days 14–15).** First cut-line is the ML application, then the collision alert.
- **Adhere strictly to the Celestrak Usage Policy (max. 1 download per 2-hour update cycle, stop immediately on HTTP 4xx/5xx, IP firewall block above >100 MB/day) — this is the most important "hard" constraint and simultaneously a great story for recruiters, because it demonstrates that you master production-ready rate-limiting/retry/caching patterns.**

## Key Findings

### 1. The Central Misconception to Avoid: "Real-time ≠ Streaming Ingest"
Satellite positions are **not** continuously measured and streamed. What you receive is a *set of orbital elements* (OMM/TLE) per object, which the US Space Force typically recomputes several times daily. From **a single** such element set, you can compute the position of the object at **any** point in time locally via SGP4. "Real-time on the map" therefore means: take the latest element set and propagate it to `now()` — that's a pure computational operation in the frontend, **not** a data stream.

Consequence: A Kafka/kSQLDB streaming ingest for positions would be **artificial** and would immediately be recognized as a "tool zoo" by experienced Data Engineers. The data source (Celestrak) only allows a refresh every 2 hours anyway. Your pipeline is therefore a **batch problem** with a *compute-intensive read path*, not a streaming problem.

### 2. Computational Load: On-demand Propagation is Easily Feasible in the Frontend
The standard library `sgp4` (Brandon Rhodes, wraps Vallado's official C++ code) achieves roughly **2.5–2.8 million propagations per second** on a single CPU core via the vectorized `SatrecArray.sgp4()` interface. This is empirically verified.

A full catalog of ~22,000–30,000 objects at **a single** timestamp is thus in the ballpark of **~8–11 milliseconds**. Even an orbit track per object (e.g., 90 timesteps over one orbital period) for thousands of objects remains in the seconds range.

Bottom line: You cache the element sets in memory and propagate on each refresh to `now()`. This is fast enough and drastically simpler than any materialized position table. **No "speed layer" needed.**

### 3. Materialize vs. On-demand — The Data Volume Calculation
If you were to materialize propagated positions, the volume explodes: 30,000 objects × one timestep every 60s × 24h = **43.2 million rows per day**. For a 2-week portfolio project, this is absurd and provides no added value, because the same information is losslessly contained in the ~30,000 element sets (a few MB).

**Recommendation: Propagate on-demand.** Materialize at most a **small Gold snapshot** (e.g., current Lat/Lon/Alt of all objects at the last ingest timestamp).

### 4. Celestrak Usage Policy — The Hard Rules (as of 2026-05-15)
From the official policy and GP documentation:
- **Download only once per update cycle.** Celestrak checks for new GP data only every 2 hours.
- **Take HTTP error codes seriously:** Machine clients **must stop querying immediately** on 301/403/404/50x.
- **Daily volume budget:** IP addresses exceeding 100 MB of downloads per day get firewall-blocked.
- **Query format (GP):** `https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=csv`
- **Important 2026:** Five-digit catalog numbers exhausted since July 2026; new objects use six-digit IDs (100000+). You **must** use OMM in CSV/JSON/XML, not legacy TLE.

## Architecture Diagram

```
Celestrak GP/OMM → Airflow (tenacity, ETag) → S3 Bronze (raw)
                                               ↓
                                          dbt/Pandas
                                               ↓
                                           S3 Silver (elset)
                                               ↓
                                      SGP4 Propagation
                                               ↓
                                           S3 Gold (snapshots)
                                               ↓
                                   Streamlit (SatrecArray, pydeck)
```

## Data Model Overview

- **Bronze:** Raw OMM/CSV files, partitioned by ingest_date/hour
- **Silver:** Normalized elset (NORAD_CAT_ID, EPOCH unique), SCD-2 historization
- **Gold:** dim_object (SATCAT metadata), fact_latest_elset, position_snapshot, conjunction_candidates

## Day-by-Day Roadmap (Days 1–15)

### Days 1–3 — MUST: Ingest + Storage + queryable
- Day 1: Repo setup, Docker Compose (Postgres + Airflow), Celestrak client with tenacity/ETag/UA
- Day 2: Silver model (dbt or Pandas), dedup, SCD-2, tests
- Day 3: Gold (dim_object, fact_latest_elset, position_snapshot via SGP4)

### Days 4–8 — SHOULD: Streamlit Map
- Days 4–5: Streamlit skeleton, cache_data(ttl="2h"), SatrecArray propagation
- Days 6–7: pydeck map, filters, @st.fragment(run_every="5s")
- Day 8: Performance tuning, deploy to Streamlit Community Cloud

### Days 9–13 — NICE-TO-HAVE: Alarm + ML
- Days 9–10: Conjunction screening (SOCRATES or APSIS filter) → SNS alert
- Days 11–13: ML — Payload/Rocket-Body/Debris classification

### Days 14–15 — Docs & Deployment
- README, architecture diagram, demo video, cleanup

## Cut-Lines (If Time Gets Tight)
1. ML application (pure nice-to-have)
2. Collision alert (reduce to SOCRATES ingest only)
3. Cloud variant (fall back to Docker Compose + Postgres)
4. Fancy visualization (simple 2D map)
5. **NEVER cut:** working ingest + queryable storage + running Streamlit map

## Deferred Decision: Historical Retention (Bronze/Silver SCD-2)

Bronze keeps every raw landing; Silver's `elset` is fully SCD-2 historized
(all past epochs kept, chained via `valid_from`/`valid_to`/`is_current`)
rather than latest-only. This costs nothing extra to build — it's the
natural output of computing SCD-2 as a pure function of epoch, not a
separate snapshot mechanism (see `reports/step2-silver-layer.md`) — so it
was kept by default.

**However, none of the three roadmap deliverables actually require it:**
- On-demand SGP4 propagation (Streamlit map) only needs the latest elset per satellite.
- Collision screening (SOCRATES/APSIS) only screens current orbits.
- ML classification (Payload/Rocket-Body/Debris) predicts a static label from a single current snapshot; historical epochs would only matter as *optional* engineered features (BSTAR/mean-motion drift as decay signals), which is not in current scope.

**Candidate cut, if time/disk pressure hits:** collapse bronze/silver to
latest-elset-only. Would not break the map, collision alert, or ML
classifier as currently scoped. Revisit only if this becomes a real
constraint — not acted on as of 2026-08-13.

## Caveats
- **SGP4 accuracy:** TLE/GP positions deviate by ~1–3 km/day (data accuracy, not algorithm)
- **Space-Track redistribution:** Raw data cannot be publicly committed (US Data-Use Agreement)
- **Streamlit Community Cloud:** 1 GB RAM limit (on-demand propagation fits fine)
- **`_GlobeView` is experimental:** no rotation, rendering issues at high zoom (2D map is safer)
- **Two-week realism:** Must + Should is very doable; Must + Should + both Nice-to-haves + Cloud docs are ambitious
