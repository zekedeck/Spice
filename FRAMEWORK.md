# GEO2.0 — Master Engineering Framework

**Version:** 1.1 (MVP Pivot 2026-10-05) | **Original Date:** 2026-09-10 | **Authors:** 4 Data Scientists + 5 Senior Engineers | **Pivot decided by:** Engineering Lead

This document is the authoritative framework for building GEO2.0 into a live, public, free-hosted geolocation map of Bluesky posts. It synthesizes architectural decisions, data contracts, security controls, and implementation priorities across all nine specialist domains. Read this first. Follow links to domain specs for deep implementation detail.

---

## System Vision

A live map showing geolocated Bluesky posts, updated in real time, hosted entirely for free, fully open source. Posts flow from the Bluesky firehose → NLP entity extraction → LLM validation → geocoding → a public FastAPI + WebSocket backend → a MapLibre frontend on GitHub Pages.

**Security and safety are non-negotiable prerequisites**, not post-launch work.

---

## MVP Pivot — GitHub-Only, NYC-Only (decided 2026-10-05)

**This section supersedes the architecture below for the current build.** Everything from "Pipeline Architecture" onward describes the original global-scale, always-on-server design. Nothing below was deleted — it's kept intact as the **Phase 2+ scale-up reference** for when/if this grows past NYC.

**Why:** ship something real instead of building the global-scale version first. Login/account sprawl (8 separate free-tier services) was also a real maintenance cost on a solo project.

**What changes for MVP:**

| Area | Original plan | MVP |
|---|---|---|
| Geographic scope | Global firehose | **New York City only** (bounding box ~lat 40.50–40.92, lng -74.26–-73.70 — approximate, confirm/tighten during collector implementation) |
| Server | Fly.io, always-on, supervisord | **None.** GitHub Actions scheduled workflow runs for a few minutes per cycle, then exits |
| Backend API | FastAPI + WebSocket | **None.** Frontend fetches a static GeoJSON file straight off GitHub Pages |
| Update model | Live WebSocket push | **Polling refresh, every 10 minutes** (GitHub Actions cron floor is 5 min; 10 min gives safety margin against scheduling jitter and keeps the LLM queue drained between runs — 6 min is viable later if a tighter cadence is wanted, just leaner on margin) |
| Geocoder | Self-hosted Photon (US-wide, ~2.5GB index) | **Public Nominatim, NYC-bbox biased.** Correction (2026-10-05): self-hosted Photon needs a persistent server to answer queries, which the no-server MVP doesn't have — an ephemeral Actions job can't host it. Public Nominatim needs zero account/API key, and the bbox bias + hard-reject validation (Null Island, importance floor < 0.02, range check) already planned below fixes the original wrong-country problem without Photon. Rate-limited to ~1 req/sec, which is fine at NYC MVP volume. |
| LLM | Groq → Groq 70b → Gemini Flash fallback chain | **Groq only** (llama-3.1-8b). Considered dropping the LLM entirely for pure rule-based NYC-bbox geocoding, and considered Hugging Face as an alternative — rejected: self-hosting an HF model on GitHub Actions' CPU-only ephemeral runners is too slow (model load time alone eats the 10-min run budget), and HF's hosted inference API would add a login rather than remove one. Groq kept as-is: free, fast, built for exactly this "ephemeral job calls an API" shape. |
| Hosting accounts needed | 8: Fly.io, Oracle ARM, Cloudflare R2, Groq, Gemini, OpenCage, GitHub, Bluesky | **3: GitHub, Groq, Bluesky** |
| Data retention | N/A (live DB, no retention policy defined) | See "MVP Data Retention" below |

**MVP Data Retention (30-day window):**

- `data/live.geojson` — rolling 24–48h window of NYC pins, **overwritten** every run (fast load for the live map view)
- `data/archive/YYYY-MM-DD.geojson` — one file per day, appended throughout that day, **auto-deleted once older than 30 days** by the same workflow run
- Net effect: repo storage stays bounded — at most ~30 daily archive files plus one live file, never unbounded growth, regardless of how long the project runs
- Rough size check: even generous NYC-only volume keeps a 30-day archive in the tens-of-MB range — well under GitHub's ~1GB soft repo-size guidance

**MVP accounts needed (down from 8 to 3):**
1. **GitHub** — repo, Actions (scheduled workflow), Pages (frontend hosting) — already have the account, `gh` CLI not yet installed locally
2. **Groq** — free LLM API; user to create account at console.groq.com and generate a key (key goes into GitHub Actions repo secrets, never into chat or committed code)
3. **Bluesky** — app password already present locally in `.env` (`BSKY_HANDLE`, `BSKY_APP_PASSWORD`)

**Dropped for MVP** (not needed at NYC-only volume; all were backup/overflow capacity for global scale, or — for Photon — incompatible with having no persistent server): Fly.io, Oracle Always Free ARM, Cloudflare R2 + Litestream, OpenCage, Gemini Flash overflow, self-hosted Photon.

---

## Build Gameplan (as of 2026-10-06)

Status: architecture decided, repo live (private) at github.com/zekedeck/Spice, all 3 accounts wired up (GitHub + Groq secret + Bluesky `.env`), UI design approved (dark NYC map, pin-click side panel, boolean keyword search, date+time range lookback, ET timezone — see "Finalized interaction design" under Phase 4 below). No pipeline code changed yet. Sequence from here to launch:

| Phase | What | Status |
|---|---|---|
| 0 | Architecture, logins, repo, UI design | **Done** |
| 1 | P0 bug fixes — 5 independent fixes across 5 files (STAFF_BRIEF.md Workers A–E), dispatched to parallel subagent workers, no interdependencies | **Done** (2026-10-06) |
| 2 | Collector batch-mode rework — convert from persistent firehose-stream listener to "connect for a short window, grab posts, exit" so it fits a GitHub Actions job; add the NYC bounding-box filter before DB write | Not started |
| 3 | Geocoder NYC-bbox wiring — confirm public Nominatim calls (not Photon, see correction above) use the NYC bbox bias + hard rejects from Worker D's fixes | Not started |
| 4 | Boolean keyword search backend — parse AND / OR / NOT / quoted-phrase expressions against post text; new requirement driven by the finalized UI | Not started |
| 5 | GitHub Actions workflow (`.github/workflows/`) — 10-min cron, runs collect→filter→NLP→Groq→geocode→write, commits `data/live.geojson` + `data/archive/YYYY-MM-DD.geojson`, prunes archive past 30 days | Not started |
| 6 | Frontend build — real MapLibre GL JS + OpenFreeMap tiles (dark style), matching the approved mockup's interaction design exactly | Not started |
| 7 | End-to-end test — manually trigger the Actions workflow a few times on the still-private repo, verify output before anything runs unattended | Not started |
| 8 | Flip repo to public, confirm the schedule fires on its own, monitor first live runs | Not started |

## Terms of Service / Compliance Review (2026-10-06)

Reviewed the actual policies of every external service this pipeline depends on, against what the code does. Nothing found is a launch blocker. One real gap found and fixed; rest is already compliant or deferred to a later phase.

| Service | Finding | Status |
|---|---|---|
| **Nominatim** (geocoder) | Must cache results, never re-query identical queries, max 1 req/sec, needs a real identifying User-Agent, requires "Data from OpenStreetMap" attribution on the map | **Fully compliant as of 2026-10-06.** Throttling already was (`time.sleep(1)`). User-Agent updated to `Spice/1.0 (NYC Bluesky geolocation map; contact: zeke.deck@gmail.com)`. Caching was missing entirely — added a `geocode_cache` SQLite table (30-day TTL) wired into `geocode_query()`/`resolve_post_locations()`. **Attribution** — still deferred to Phase 6 (frontend doesn't exist yet); MapLibre's default attribution control must stay enabled when built, don't strip it. |
| **Bluesky / AT Protocol** | No restriction found on storing/displaying public post content externally; Bluesky itself doesn't guarantee deletions propagate everywhere | **Compliant** — tombstone handling (Phase 1, Worker B) already exceeds what's required |
| **GitHub Actions** | Vague "no excessive automated bulk activity" clause, no explicit ban on small scheduled workflows | **Low risk** — keep each run lean; revisit cadence only if GitHub ever flags it |
| **Groq** | AUP bars illegal/harmful content and requires accuracy review for consequential public use; exact free-tier rate limit for the specific model couldn't be confirmed from docs | **Action for user**: verify the actual current rate limit for `llama-3.1-8b-instant` in the Groq console before relying on the "6k req/day" figure assumed elsewhere in this doc |
| **OpenFreeMap** (tiles) | MIT-licensed, no rate limit, no key; requires "OpenFreeMap © OpenMapTiles Data from OpenStreetMap" attribution | **Compliant by default** — just don't disable MapLibre's attribution control when Phase 6 builds the frontend |

Phase 1 is unblocked and dispatched now. Phases 2–4 are independent of each other and could run in parallel once Phase 1 clears the files they touch (collector.py, geocoder.py). Phase 5 depends on 1–4 being done since the workflow calls into that code. Phase 6 can start anytime in parallel with 1–5 (no shared files). Phase 7–8 are launch gating and must come last.

---

## Pipeline Architecture

```
[Bluesky Firehose]
       │  WebSocket (atproto)
       ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 0 — Collector (pipeline/collector.py)                │
│  • Ingests ALL public posts                                  │
│  • DID → handle LRU cache (fix: currently calls API per post)│
│  • Handles tombstones (delete ops) — CURRENTLY MISSING      │
│  • Hard limit: 4096 bytes/post text before DB write         │
│  • Writes: posts table (processed=0)                        │
└─────────────────────────────────────────────────────────────┘
       │  SQLite (WAL mode)
       ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 1 — NLP Entity Extraction                            │
│  PRIMARY: GLiNER gliner_mediumv2.1 (replace spaCy)         │
│  FALLBACK: spaCy en_core_web_sm (recall safety net)        │
│  • 12–15 semantic labels (versioned YAML config)           │
│  • Pre-filter: bot signatures, news URLs, game keywords     │
│  • Emoji 📍 pre-pass (bypasses NLP, highest confidence)    │
│  • Stance tagger: present_at / past_visit / news_subject   │
│  • Output: ExtractedEntity list (including suppressed ones) │
└─────────────────────────────────────────────────────────────┘
       │  (if no entities → discard, never reaches LLM)
       ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 2 — LLM Geotagging (NEW: pipeline/llm_geotagging.py)│
│  PRIMARY: Groq llama-3.1-8b (6k req/day, 30 RPM)          │
│  ESCALATION: Groq llama-3.3-70b (hard cases, 1k req/day)  │
│  OVERFLOW: Gemini 1.5 Flash (1.5k req/day, 15 RPM)        │
│  FALLBACK: Rule-based build_geocoding_query() (nlp.py)     │
│                                                              │
│  Per entity, LLM returns ONE of:                           │
│  • resolve → lat/lng from training knowledge (famous venues)│
│  • query   → enriched geocoding query string               │
│  • accept  → entity text is already geocodeable            │
│  • null    → suppress (music_bot / news_subject / fictional)│
│                                                              │
│  Token optimization:                                        │
│  • Pre-gate: skip LLM for already-qualified GPEs           │
│  • SQLite entity cache (TTL 30 days, confidence ≥ 0.85)   │
│  • Static system prompt = free token caching at Groq       │
│  • Adaptive text truncation: 400 chars (600 if entity late)│
│  • Hallucination validation: coord plausibility + reverse  │
└─────────────────────────────────────────────────────────────┘
       │  (resolved coords skip Stage 3 entirely)
       ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 3 — Geocoder (pipeline/geocoder.py)                  │
│  PRIMARY: Photon self-hosted (US-only OSM extract, ~2.5 GB)│
│  BACKUP: OpenCage (2,500/day free, cloud)                  │
│  TERTIARY: Public Nominatim (keep for anchor queries only) │
│                                                              │
│  Key fixes vs current:                                      │
│  • US bbox bias: bbox=-125,24,-65,50 on all queries        │
│  • Multi-factor ranking: type match + US country + pop     │
│  • osm_tag filters for FAC entities (venue/stadium/theatre)│
│  • SQLite geocode cache (90/60/30 day TTL by precision)    │
│  • Remove time.sleep(1) for Photon; keep for public Nominatim│
└─────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 4 — Validation & Confidence Scoring                  │
│  REPLACES: flat scorer.py additive formula                  │
│                                                              │
│  Coordinate validation (hard rejects):                      │
│  • Null Island (0,0), range sanity, importance < 0.02      │
│  • Name match ratio < 0.15, bbox mismatch on clarified     │
│                                                              │
│  4-Pillar confidence score [0.0–1.0]:                       │
│  • Pillar A (30%): linguistic evidence (stance, prepositions)│
│  • Pillar B (35%): geocoder quality (importance, precision) │
│  • Pillar C (20%): post context (author history, account age)│
│  • Pillar D (15%): entity source (EMOJI > FAC > GPE > ORG) │
│  • Catastrophic pillar cap: if A<0.15 or B<0.10 → max 0.40 │
│                                                              │
│  Thresholds: ≥0.80 full pin | 0.60–0.79 standard pin       │
│              0.45–0.59 flagged pin (dim + "?")              │
│              0.30–0.44 hold for review | <0.30 reject       │
│                                                              │
│  Deduplication: spatial cluster + 2-hour window + semantic  │
│  Event surge detection: >20 posts/30min → auto-flag         │
└─────────────────────────────────────────────────────────────┘
       │  Writes: pins table + WebSocket broadcast
       ▼
┌─────────────────────────────────────────────────────────────┐
│  FastAPI Server (api/)                                       │
│  • GET /api/v1/pins (bbox, since, precision, confidence)   │
│  • GET /api/v1/pins/{pin_id:path}                          │
│  • GET /api/v1/stats                                        │
│  • GET /health                                              │
│  • WS  /api/v1/ws/pins (live pin broadcast)                │
│  • Cursor-based pagination (time-ordered, not offset)       │
│  • Rate limiting: 60 req/min/IP (slowapi)                  │
└─────────────────────────────────────────────────────────────┘
       │  HTTPS (Fly.io TLS)
       ▼
┌─────────────────────────────────────────────────────────────┐
│  Frontend — GitHub Pages (static, free)                      │
│  • MapLibre GL JS (WebGL, handles 50k+ pins)               │
│  • Supercluster in Web Worker (non-blocking clustering)     │
│  • Alpine.js state management (no build step, CDN)         │
│  • OpenFreeMap tiles (free, no API key)                    │
│  • Two-phase load: REST historical pins → WS live feed     │
│  • Filters: time range, radius, precision tier toggle      │
└─────────────────────────────────────────────────────────────┘
```

---

## Infrastructure

### Hosting Stack (All Free)

| Component | Service | Rationale |
|---|---|---|
| Backend (API + workers) | **Fly.io** | Never sleeps (critical for firehose); 3 VMs free; managed TLS |
| Database | **SQLite on Fly.io volume** | Zero new deps; WAL mode handles concurrency |
| Frontend | **GitHub Pages** | Static, free, always on |
| Backup | **Oracle Always Free ARM** | 24GB RAM → fits self-hosted Photon; keep warm |
| DB replication | **Litestream → Cloudflare R2** | Free egress; continuous WAL streaming |
| Monitoring | **UptimeRobot** (free) | 3 monitors: /health, /health/collector, /api/v1/stats |

### Process Model (Single Fly.io Machine)

```
supervisord (PID 1)
├── collector     → python -m pipeline.collector
├── pipeline      → python worker.py  (headless; replaces interactive main.py)
├── api           → uvicorn api.main:app --port 8000
├── backup        → daily sqlite3 .backup + gzip → /data/backups/
└── litestream    → continuous WAL replication to R2
```

### Critical Infrastructure Fixes (Do These Before Deploying)

1. **`DB_PATH` must be `/data/raw/posts.db`** — current `data/raw/posts.db` is relative and points inside the ephemeral container layer. Every deploy wipes the database without this fix.
2. **WAL mode** — add `PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;` to `init_db()`. Without it, multi-process SQLite access corrupts under concurrent load.
3. **`auto_stop_machines = false`** in `fly.toml` — Fly.io's default sleeps idle machines. The firehose collector must never sleep.
4. **Refactor `main.py` to headless `worker.py`** — remove all `input()` calls; read config from env vars; run in a configurable loop.

---

## Security Controls (Implement Before Any Public Exposure)

Security is organized into priority tiers. P0 must be done before the first public deploy.

### P0 — Immediate (Data Protection)

- **Remove `did` from GeoJSON/API output** — permanent user identifier; not needed by the map
- **Fuzz coordinates to 3 decimal places** for address-precision pins (~110m grid) in `writer.py`
- **Verify no secrets in git history** — scan with `detect-secrets`; add pre-commit hook
- **`chmod 600 .env`** — current file is world-readable (644)
- **`force_https = true`** in `fly.toml`
- **Text length hard cap in collector**: 4096 bytes before writing to DB

### P1 — Before Launch

- **CORS lockdown** — `allow_origins=["https://yourusername.github.io"]` only; never wildcard
- **Rate limiting** — `slowapi` on all API endpoints; 60 req/min/IP for `/api/v1/pins`
- **Content Security Policy** on GitHub Pages frontend
- **Tombstone handling** — collector must process `op.action == "delete"` and remove rows from `posts`; deleted posts must not appear on the map
- **Dependency hash pinning** — `pip-compile --generate-hashes`; pin spaCy model wheel by sha256

### P3 — Before LLM Stage Goes Live (Critical)

- **Prompt injection defense**: sanitize post text (NFC normalize, strip control chars, hard truncate to 1000 chars) before LLM prompt construction; never f-string post text into the system prompt
- **LLM output schema validation**: validate every LLM response against the JSON schema before consuming; treat parse failure as rule-fallback, not crash
- **Structured delimiters**: use `<<<BEGIN_POST>>> ... <<<END_POST>>>` pattern; static system prompt only

### Known Bugs to Fix (Security-Relevant)

- **EMOJI label bypasses all context checks** in `semantic_filter.py` line 68-69 — `return True` fires before music/media/subject signals are checked
- **`get_profile()` called per post** in `collector.py` — will hit Bluesky rate limits; add LRU cache
- **No coordinate validation** in `geocoder.py` — Nominatim can return Null Island (0,0) or wrong country; add hard rejects

---

## Data Contracts Summary

### Public API (Frontend ↔ Backend)

The frontend consumes only these fields per pin feature. Nothing else crosses this boundary.

```json
{
  "type": "Feature",
  "id": "{post_uri}__{candidate_index}",
  "geometry": {"type": "Point", "coordinates": [lng, lat]},
  "properties": {
    "pin_id": "string",
    "post_uri": "string",
    "post_url": "string",
    "handle": "string",
    "text": "string (full post text)",
    "post_created_at": "ISO 8601 UTC",
    "location_text": "string",
    "mapped_location": "string",
    "precision": "address|address_clarified|city|state|country",
    "confidence": "float [0.0–1.0]",
    "candidate_index": "integer",
    "candidate_total": "integer"
  }
}
```

**Deliberately excluded from public API:** `did`, `query_sent`, `source_label` (GPE/FAC/etc.), `importance`, `geocoder_raw`, `score_components`, `collected_at`, `processed_at`.

### WebSocket Protocol

```
wss://api.geo2.example.com/api/v1/ws/pins

Server → Client message types:
  welcome       (on connect: session_id, heartbeat_interval_s)
  heartbeat     (every 30s)
  new_pin       (payload.feature = identical schema to REST GeoJSON Feature)
  backpressure_warn  (queue > 50 msgs)
  reconnect_hint     (queue > 200 msgs → server closes with code 4001)
  error

Client → Server: none (v1 is server-push only)
```

### Internal Stage Contracts

Each pipeline stage consumes a typed struct from the previous stage. Stages are decoupled — changing Stage 1 (NLP model) does not require changing Stage 3 (geocoder) provided the output schema is stable.

```
Stage 0 output: RawPost (TypedDict) → SQLite posts table
Stage 1 output: ExtractedEntity list (span_text, label, raw_score, stance_tag, suppressed)
Stage 2 output: LLMGeoResult list (action, lat, lng, query, confidence, suppress_reason)
Stage 3 output: LocationCandidate dataclass (text, lat, lng, precision, display_name, ...)
Stage 4 output: ScoredCandidate (+ confidence_breakdown, hard_reject_flags)
Database write: pins table (full provenance — see schema below)
```

### Database Schema (Key Tables)

```sql
-- Raw collection (existing, extended)
posts (id, did, handle, post_uri, post_url, text, created_at, collected_at,
       processed INTEGER DEFAULT 0,   -- 0=pending, 1=done, 2=skipped
       skip_reason TEXT,              -- NEW
       pipeline_version TEXT)         -- NEW

-- Geocoded output (new)
pins (pin_id TEXT PRIMARY KEY,        -- "{post_uri}__{candidate_index}"
      post_uri, handle, did, post_url, post_text, post_created_at,
      lat, lng, location_text, mapped_location, precision, confidence,
      query_sent, was_clarified, geocoder, address_type,
      nominatim_importance, geocoder_raw,
      score_components, scorer_version,
      candidate_index, candidate_total, sibling_locations,
      is_active INTEGER DEFAULT 1,    -- soft delete for moderation
      processed_at, pipeline_version)

-- LLM entity resolution cache (new)
llm_entity_cache (cache_key TEXT PRIMARY KEY, entity_text, action,
                  lat, lng, query, canonical_name, confidence,
                  suppress_reason, hit_count, created_at, expires_at)

-- Geocoding cache (new)
geocode_cache (query_key TEXT PRIMARY KEY, lat, lng, display_name,
               address_type, importance, source, cached_at, ttl_days)
```

---

## Implementation Phases

### Phase 1 — Fix Geotagger (Current Blocker)

*Goal: Eliminate the Nominatim wrong-country failures and build the LLM validation layer.*

**Week 1 — Geocoder fixes (no new dependencies)**
1. Add `PRAGMA journal_mode=WAL` + `busy_timeout` to `init_db()` (15 min)
2. Add `geocode_cache` SQLite table (2 hours)
3. Add US bbox bias + `countrycodes=us` to existing Nominatim calls (1 hour)
4. Replace 2-tuple geocoder ranking with multi-factor float scorer (2 hours)
5. Add coordinate hard rejects: Null Island, range check, importance floor < 0.02 (1 hour)
6. Fix EMOJI bypass bug in `semantic_filter.py` line 68 (30 min)

**Week 2 — GLiNER integration**
7. Replace spaCy primary extractor with GLiNER `gliner_mediumv2.1`
8. Implement versioned label YAML config
9. Add stance tagger (present_at / past_visit / news_subject)
10. Keep spaCy as fallback for zero-entity posts

**Week 3 — LLM geotagging layer**
11. Create `pipeline/llm_geotagging.py` with Groq primary + fallback chain
12. Add `llm_entity_cache` table
13. Add pre-gate (skip LLM for qualified GPEs)
14. Implement hallucination validation (plausibility check + reverse geocode for confidence < 0.92)
15. Add `LLM_ENABLED=false` kill switch to `config.py`

**Week 4 — Self-hosted Photon**
16. Download US-only OSM PBF from Geofabrik
17. Build Photon index locally (~2–4 hours)
18. Deploy Photon on Fly.io with US-only data
19. Wire as primary geocoder; OpenCage as cloud backup

---

### Phase 2 — Continuous Backend

*Goal: Replace interactive `main.py` with headless workers + FastAPI API.*

1. Fix `DB_PATH` → `/data/raw/posts.db` via env var (15 min, critical)
2. Create headless `worker.py` (remove all `input()` calls from `main.py`)
3. Add `pins` database table + `insert_pin()` function
4. Fix LRU cache for `did → handle` resolution in `collector.py`
5. Add tombstone handler in `collector.py` (`op.action == "delete"`)
6. Build `api/` package: FastAPI app, routers, deps
7. Implement REST endpoints: `/api/v1/pins`, `/api/v1/pins/{id}`, `/api/v1/stats`, `/health`
8. Implement WebSocket hub with `asyncio.run_coroutine_threadsafe` fan-out
9. Add CORS lockdown and rate limiting (`slowapi`)
10. Add security headers middleware

---

### Phase 3 — Infrastructure & Deploy

1. Write `Dockerfile` + `deploy/supervisord.conf`
2. Write `fly.toml` with `auto_stop_machines = false`, health check, persistent volume mount
3. Write `deploy/backup_db.sh` (daily SQLite backup + 7-backup rotation)
4. Set up Litestream → Cloudflare R2 replication
5. Set all secrets: `fly secrets set BSKY_HANDLE=... BSKY_APP_PASSWORD=... GROQ_API_KEY=...`
6. Write GitHub Actions deploy workflow (push to `main` → `flyctl deploy --remote-only`)
7. First deploy + verify with `fly logs` and `fly ssh console`
8. Set up UptimeRobot monitors: `/health`, `/health/collector`, `/api/v1/stats`
9. Provision Oracle Always Free ARM as warm standby

---

### Phase 4 — Frontend

1. MapLibre GL JS on GitHub Pages (OpenFreeMap tiles — no API key)
2. Two-phase load: `GET /api/v1/pins?since=24h` → WS sync handshake
3. Supercluster in Web Worker (non-blocking clustering)
4. Alpine.js state management (no build step)
5. Filter panel: time range slider (noUiSlider), radius filter, precision tier toggles
6. Pin popups: handle, text (truncated), timestamp, precision badge, confidence, Bluesky link
7. WebSocket client with exponential backoff reconnect + RAF-based message batching
8. Connection status indicator (green/yellow/red dot)
9. Accessibility: list view fallback, `aria-live` region for new pins, focus management

**For MVP:** items 2, 7, 8 above (two-phase REST→WS load, WebSocket client, connection status dot) don't apply — there's no backend to connect to. Replace with: fetch `data/live.geojson` on page load, re-fetch on the same 10-minute interval the Actions workflow runs on. See "MVP Pivot" section above.

**MVP Design Reference — Citizen app (screenshots in `Design_brief/`):**

Source: Citizen (crime/safety alert app) — strong reference for "live feed of geolocated events on a dark map," the same core UI problem GEO2.0 has.

- **Dark map theme** — navy/black base, muted street lines, neighborhood labels on by default. Needs a dark MapLibre/OpenFreeMap style, not the default light one.
- **Category icon pins** instead of generic dots — color/icon-coded by entity source label (EMOJI / FAC / GPE / ORG) rather than one uniform marker style.
- **Soft glow/radius circle** around an actively-selected or just-added pin, to draw the eye to new activity.
- **Density clustering as size-scaled dots** when zoomed out — confirms Supercluster (already planned) is the right approach; dot size should scale with cluster post count.
- **Bottom card list** under the map, not just hover popups — one card per pin: handle, text (truncated), time-ago, precision badge, confidence, Bluesky link. Maps directly onto the existing public API pin schema (see "Data Contracts Summary" above) — no new fields needed.
- **Top search bar + time-range toggle** ("Last 24 Hours") — natural fit for toggling between `data/live.geojson` (rolling 24-48h) and the 30-day `data/archive/` files from the MVP data retention design.

**Finalized interaction design (approved 2026-10-06, mocked up as a design-canvas artifact):**

- **No card feed.** Map is full-width by default showing no post data — just pins. Clicking a pin opens a **right-side detail panel** for that single post (handle, text, neighborhood, precision, confidence). An X closes it back to map-only. Replaces the earlier "bottom card list" idea.
- **Search bar is boolean keyword search**, not a neighborhood-name lookup — supports AND / OR / NOT and quoted exact phrases (e.g. `"subway" AND NOT "closed"`). Affects the backend query layer too: whatever serves the frontend needs to parse boolean keyword expressions against post text, not just substring match.
- **Lookback is a date+time range picker**, not a single date or a vague "30 days" bucket — a calendar with separate **From** and **To** selections (each with its own date and time), since the archive is literally one file per day and users should be able to bound a query to an arbitrary window within the last 30 days, not just jump to one day.
- **All times displayed in ET** (America/New_York) — correct since this is an NYC-only app; backend should store/convert accordingly rather than showing raw UTC.
- Pin colors stay category-coded (venue / neighborhood mention / org / flagged-low-confidence) with a soft glow halo, stronger when selected — carried over from the original Citizen-reference notes above.

---

## Monitoring & Drift Detection

### Metrics to Emit Every Pipeline Run

```
posts_ingested, posts_processed, posts_skipped_language
entity_extraction_rate, semantic_filter_pass_rate
geocode_success_rate, confidence_pass_rate
mean_confidence, stddev_confidence
mention_class_distribution: {V_HIGH, V_LOW, E_ANN, S_NEWS, BOT, ...}
precision_tier_distribution: {address: 0.xx, city: 0.xx, ...}
null_island_rate, bbox_mismatch_rate, ambiguity_flag_rate
llm_cache_hits, llm_api_calls, llm_hallucination_caught
llm_source_groq_8b, llm_source_groq_70b, llm_source_gemini, llm_rule_fallback
pins_displayed, pins_held, pins_rejected
```

### Alert Thresholds (log WARNING)

| Metric | Threshold |
|---|---|
| `geocode_success_rate` | Drop > 10% from 7-day avg |
| `confidence_pass_rate` | ±20% from 7-day avg |
| `mention_class_distribution[S_NEWS]` | +10 percentage points (conflict surge) |
| `bbox_mismatch_rate` | +10% |
| `llm_hallucination_caught / llm_resolved_direct` | > 5% |
| `llm_rule_fallback / total_posts_with_entities` | > 30% (quota issue) |
| `pins_displayed` | ±50% (pipeline failure or major content shift) |

### Geocoder Health Check (Run Before Each Pipeline Batch)

```python
HEALTH_CHECK_QUERIES = [
    ("Eiffel Tower, Paris", {"lat": 48.858, "lng": 2.294, "tolerance_km": 0.5}),
    ("Times Square, New York", {"lat": 40.758, "lng": -73.985, "tolerance_km": 0.5}),
    ("Red Rocks Amphitheatre, Colorado", {"lat": 39.665, "lng": -105.206, "tolerance_km": 1.0}),
]
# If ANY check fails → abort pipeline run, log alert
```

---

## Known Bugs in Current Codebase

Ordered by severity:

| Severity | File | Issue | Fix |
|---|---|---|---|
| CRITICAL | `config.py` | `DB_PATH = "data/raw/posts.db"` (relative → erased on deploy) | **Already fixed in the codebase** (verified 2026-10-06) — `config.py:19` already reads `os.getenv("DB_PATH", "/data/raw/posts.db")`, and both callers (`main.py`, `collector.py`) import `DB_PATH` from `config` rather than hardcoding a path. This FRAMEWORK.md entry was stale. |
| CRITICAL | `collector.py` | No tombstone handler — deleted posts stay on map forever | **Fixed 2026-10-06** (Worker B) |
| CRITICAL | `geocoder.py` | No coordinate validation — Null Island / wrong country accepted | **Fixed 2026-10-06** (Worker D) |
| HIGH | `semantic_filter.py` line 68 | `entity_label == "EMOJI"` → immediate `return True`, bypasses all context checks | **Fixed 2026-10-06** (Worker C) |
| HIGH | `collector.py` | `get_profile(did)` called per post → Bluesky rate limit hit | **Fixed 2026-10-06** (Worker B) |
| HIGH | `database.py` | No WAL mode | **Fixed 2026-10-06** (Worker A) |
| HIGH | `writer.py` | `did` and full-precision coordinates in public GeoJSON output | **Fixed 2026-10-06** (Worker E) |
| MEDIUM | `geocoder.py` | 2-tuple ranking picks wrong-country results | **Fixed 2026-10-06** (Worker D, added US-bonus ranking) |
| MEDIUM | `collector.py` | `time.sleep(5)` in reconnect loop — delays clean shutdown | Replace with `stop_event.wait(timeout=5)` |
| MEDIUM | `main.py` | `time.sleep(1)` geocoding sleep blocks pipeline thread | Acceptable for now; replace with token-bucket rate limiter when parallelizing |
| MEDIUM | `nlp.py` | Co-entity GPE enrichment uses motion-from entities (e.g., "Flying from Boston to Portland" adds Boston as context for Portland query) | Filter to static-at and motion-to entities only |
| LOW | `pipeline/github_push.py` | Any exception crashes the whole pipeline | Wrap in try/except; log and continue |

---

## Technology Decisions Summary

| Concern | Choice | Rationale |
|---|---|---|
| NLP entity extraction | GLiNER mediumv2.1 (primary), spaCy sm (fallback) | Semantic labels beat fixed taxonomy for venue detection |
| LLM geotagging | Groq llama-3.1-8b → 70b → Gemini Flash | Free tiers; fast; reliable JSON output; cascading fallback |
| Geocoder primary | Photon self-hosted (US-only OSM) | No rate limit; population-weighted ranking; bbox bias |
| Geocoder backup | OpenCage (2.5k/day free) | Better ranking than public Nominatim; clean API |
| Confidence scoring | 4-pillar weighted score with catastrophic floor | Prevents compensating bonuses rescuing bad inputs |
| Backend framework | FastAPI + uvicorn | Async WebSocket + REST; minimal overhead |
| Database | SQLite (WAL mode) | Zero new dependencies; fits single-server free tier |
| Process manager | supervisord | Battle-tested PID-1 for multi-process containers |
| Deployment | Fly.io (primary), Oracle Always Free (backup) | Never sleeps; 3 free VMs; managed TLS |
| DB backup | Litestream → Cloudflare R2 | Free egress; continuous WAL streaming |
| Map renderer | MapLibre GL JS | WebGL; handles 50k+ pins; native GeoJSON layers |
| Clustering | Supercluster in Web Worker | Non-blocking; 100k+ points; incremental add |
| Map tiles | OpenFreeMap | Free; no API key; vector quality |
| State management | Alpine.js | No build step; CDN; HTML-first; fits GitHub Pages |
| Frontend hosting | GitHub Pages | Free; static; always on |

---

## Open Questions for Engineering Review

1. **Bandwidth constraint:** Full Bluesky firehose ≈ 5 GB/day inbound on the Fly.io machine. At 160 GB/month free egress, only ~32 days of inbound before overage. A keyword pre-filter in the collector (drop posts with no location-indicative words before DB write) is likely required. What precision/recall tradeoff is acceptable? **Resolved by MVP pivot (2026-10-05):** NYC-only scope + no persistent Fly.io server makes this moot for MVP. Still relevant if/when scaling back to global scope (Phase 2+).

2. **LLM stage latency:** The async queue + 30 RPM Groq limit means pipeline throughput for LLM-validated posts is bounded at 1,800/hour. Is this acceptable for the live feed use case, or should LLM validation be sampled (e.g., only for ambiguous entities)?

3. **Annotation tooling:** Ground truth evaluation (DS3 Section 8) requires annotating ~800 posts/month. What tooling exists? Label Studio (free, self-hostable) is the recommendation if none exists.

4. **GLiNER GPU requirement:** `gliner_mediumv2.1` achieves adequate throughput only on GPU. Fly.io free tier has no GPU. Options: (a) CPU inference with batch size 8 (acceptable latency?), (b) Groq API for GLiNER inference (not currently available), (c) run NLP on separate Oracle ARM machine (4 OCPUs). Decision needed before Phase 1 Week 2. **Resolved by MVP pivot (2026-10-05):** NYC-only volume is low enough for CPU inference (option a) — no Oracle ARM machine needed for MVP. Revisit if volume/scope grows.

5. **Content policy:** Who reviews the hold queue (confidence 0.30–0.44) and handles content reports? An operator review workflow needs to be defined before public launch.

6. **Radius filter:** Current `main.py` has interactive radius/global mode. The headless worker should default to global mode. The radius filter should move entirely to the API layer (`lat`/`lng`/`radius_mi` query params). Confirm this matches the intended product behavior.

---

## Appendix: File Change Index

Files that must change before Phase 2 is complete:

| File | Change |
|---|---|
| `config.py` | DB_PATH env var; LLM config constants; startup validation |
| `pipeline/collector.py` | LRU handle cache; tombstone handler; stop_event param; text length cap |
| `pipeline/database.py` | WAL mode; pins table; geocode_cache table; llm_entity_cache table |
| `pipeline/geocoder.py` | Multi-factor ranking; US bbox; coord validation; Photon integration; cache layer |
| `pipeline/semantic_filter.py` | Fix EMOJI bypass bug; add stance classification taxonomy |
| `pipeline/nlp.py` | Replace spaCy with GLiNER primary; versioned label YAML |
| `pipeline/scorer.py` | Replace flat formula with 4-pillar scoring |
| `pipeline/writer.py` | Remove `did`; fuzz coords; write to `pins` table not GeoJSON file |
| `models.py` | Add `llm_action`, `llm_source`, `confidence_breakdown` to LocationCandidate |
| `main.py` | Rename to `backfill.py`; keep for offline/debug use |

New files to create:

| File | Purpose |
|---|---|
| `worker.py` | Headless pipeline worker (replaces interactive main.py) |
| `server.py` | Single entrypoint: starts collector thread, worker thread, uvicorn |
| `pipeline/llm_geotagging.py` | LLM Stage 2 implementation |
| `api/app.py` | FastAPI app factory |
| `api/routers/pins.py` | REST pin endpoints |
| `api/routers/ws.py` | WebSocket endpoint |
| `api/routers/health.py` | Health endpoint |
| `api/deps.py` | DB connection, hub injectors |
| `api/models.py` | Pydantic request/response schemas |
| `deploy/supervisord.conf` | Process management config |
| `deploy/backup_db.sh` | Daily SQLite backup script |
| `Dockerfile` | Container definition |
| `fly.toml` | Fly.io deployment config |
| `.github/workflows/deploy.yml` | CI/CD pipeline |
| `config/gliner_labels_v1.yaml` | Versioned GLiNER label set |
| `litestream.yml` | Continuous DB replication config |
| `frontend/index.html` | MapLibre map frontend |
| `frontend/app.js` | Alpine.js + WebSocket + MapLibre integration |
| `frontend/cluster-worker.js` | Supercluster Web Worker |
