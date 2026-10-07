# Spice — Staff Brief
**From:** Engineering Lead
**Date:** 2026-09-10
**Purpose:** Working standards and task assignments for all implementation workers on this project.

---

## How We Work

Read this before touching a single line of code.

**You are not the decision-maker.** You implement exactly what is scoped for you — no more. Do not refactor adjacent code, do not add features that weren't asked for, do not improve things you weren't assigned. If you spot a problem outside your scope, report it in your output and leave it alone.

**Security is non-negotiable.** This is a public-facing product. If your task touches user data, API output, or input handling, you treat it like production from day one. No "we'll harden it later." The framework spec (FRAMEWORK.md) defines what safe looks like — follow it exactly.

**Read before you write.** You must read every file you are about to edit. You must read FRAMEWORK.md sections relevant to your task. If you don't understand why a change is required, read the framework again before asking.

**Report your work clearly.** When you're done, give a precise summary: what files you changed, what lines changed, what was the before/after. If you couldn't complete something, say exactly why and what's blocking you — don't paper over it.

**Do not break existing behavior unless the framework says to.** The pipeline currently runs. P0 fixes patch bugs and add safety — they should not change the output format or pipeline flow unless explicitly called out.

---

## Active Assignments

---

### Worker A — `pipeline/database.py` (WAL Mode)

**Your task:** Add WAL mode to `init_db()`.

**Exactly what to add** (immediately after `conn = sqlite3.connect(db_path)`, before any table creation):
```python
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA busy_timeout=5000")
```

**Why this matters:** Collector, pipeline worker, and API server all hit SQLite concurrently. Without WAL, writes block reads and concurrent writes corrupt the database. This is a deployment blocker.

**Scope:** One function, two lines. Nothing else.

**Done when:** `init_db()` executes both pragmas before any `CREATE TABLE` statement.

---

### Worker B — `pipeline/collector.py` (Three fixes)

**Your task:** Three independent fixes in the collector. Do all three.

**Fix 1 — LRU cache for `get_profile()`**
The `on_message_handler` calls `client.get_profile(did)` on every single post. A single user can post hundreds of times per day — this hammers the Bluesky API and will hit rate limits fast.

Add a module-level dict cache (max 10,000 entries). Before calling `get_profile(did)`, check the cache. After a successful call, write to the cache. If the cache exceeds 10,000 entries, drop the oldest half (simple eviction is fine — don't overcomplicate this).

**Fix 2 — Tombstone handler**
Currently the collector only handles `op.action == "create"`. When a user deletes a post on Bluesky, a delete op comes through the firehose and the post stays on our map forever. That's a user rights violation.

Add a handler for `op.action == "delete"` inside the `for op in commit.ops` loop. When a delete op comes in for a `app.bsky.feed.post/` path, construct the URI (`at://{commit.repo}/{op.path}`) and call a `delete_post(conn, uri)` function.

You must also add `delete_post()` to `pipeline/database.py`:
```python
def delete_post(conn, post_uri: str) -> None:
    conn.execute("DELETE FROM posts WHERE post_uri = ?", (post_uri,))
    conn.commit()
```

**Fix 3 — Text hard cap**
Before calling `insert_post()`, enforce a 4096-byte cap on post text:
```python
text = text[:4096]
```
Do this after the empty text check, before the profile lookup. Bluesky's max post is 300 graphemes but the firehose can carry malformed or embedded-content records. Unbounded text is a storage and potential injection risk.

**Done when:** All three fixes are present. Collector correctly caches DID→handle, deletes tombstoned posts, and caps text before DB write.

---

### Worker C — `pipeline/semantic_filter.py` (EMOJI context check gap)

**Your task:** EMOJI-labeled entities currently bypass MEDIA_SIGNALS and SUBJECT_SIGNALS checks. The music-bot check was correctly moved before the EMOJI early return, but the media/game/subject checks still get skipped.

A post like `"📍 Kyiv — war in Ukraine continues"` has an EMOJI entity that should be suppressed by SUBJECT_SIGNALS (`"war in"`), but currently passes straight through.

**The fix:** Move the EMOJI early return to AFTER the window-based MEDIA_SIGNALS and SUBJECT_SIGNALS checks. The EMOJI return should only fire if none of the suppression signals matched.

The music-bot check (`MUSIC_BOT_SIGNALS`) stays where it is — at the very top, before EMOJI. That behavior is correct.

The final flow should be:
1. Music-bot → suppress all (including EMOJI)
2. Window setup
3. MUSIC_CONTEXT_SIGNALS in window → suppress
4. MEDIA_SIGNALS in window → suppress
5. VENUE_SIGNALS in window → accept (return True regardless of label)
6. SUBJECT_SIGNALS in sentence → suppress
7. **EMOJI early return → True** (if we got here, EMOJI is clean)
8. GPE spatial prep check
9. Final label check

**Do not** change any signal lists, any thresholds, or any logic outside of the ordering of the EMOJI early return.

**Done when:** An EMOJI entity in a media/subject context is correctly suppressed.

---

### Worker D — `pipeline/geocoder.py` (Coordinate validation + ranking)

**Your task:** Two fixes to `geocode_query()`.

**Fix 1 — Hard reject invalid results**
After `best = max(results, key=_rank)`, add these hard rejects before returning the dict:

```python
lat = float(best.latitude)
lng = float(best.longitude)

# Null Island
if lat == 0.0 and lng == 0.0:
    logger.warning("Null Island result rejected for query %r", query)
    return None

# Basic range sanity
if not (-90 <= lat <= 90 and -180 <= lng <= 180):
    logger.warning("Out-of-range coordinates rejected for query %r: %s, %s", query, lat, lng)
    return None

# Importance floor
if float(best.raw.get("importance", 0.0)) < 0.02:
    logger.warning("Importance below floor rejected for query %r", query)
    return None
```

**Fix 2 — US country bonus in ranking**
The current `_rank()` function only uses `type_bonus` and `importance`. This is why "Red Rocks" returns an Australian result — the Australian node can have a higher importance score.

Update `_rank()` to add a country bonus:
```python
def _rank(r):
    atype = r.raw.get("type", r.raw.get("addresstype", ""))
    type_bonus = 1 if atype in _ADDRESS_TYPES else 0
    address = r.raw.get("address", {})
    country_code = address.get("country_code", "").lower()
    us_bonus = 1 if country_code == "us" else 0
    importance = float(r.raw.get("importance", 0.0))
    return (type_bonus + us_bonus, importance)
```

This makes any US result beat any non-US result of the same address type, before importance is considered.

**Do not** change `assign_precision()`, `resolve_post_locations()`, or any other function. Scope is `geocode_query()` only.

**Done when:** Null Island, out-of-range, and low-importance results are rejected. US results rank above non-US results of the same type.

---

### Worker E — `pipeline/writer.py` (Security: remove DID, fuzz coordinates)

**Your task:** Two security fixes to `post_to_features()`.

**Fix 1 — Remove `did` from output**
The `did` field is a permanent Bluesky user identifier. It must not appear in the public GeoJSON output. Remove it from the `properties` dict. That's it — one line deleted.

**Fix 2 — Fuzz address-precision coordinates**
For pins where `candidate.precision` starts with `"address"`, round coordinates to 3 decimal places (~110m grid) before writing to the feature geometry. For city/state/country precision, write full precision.

```python
if candidate.precision.startswith("address"):
    out_lat = round(candidate.lat, 3)
    out_lng = round(candidate.lng, 3)
else:
    out_lat = candidate.lat
    out_lng = candidate.lng
```

Use `out_lat`/`out_lng` in the coordinates array, not the raw values.

**Do not** change the feature schema, the `build_feature_collection()` function, or anything else in the file.

**Done when:** `did` is absent from all output properties. Address-precision coordinates are rounded to 3 decimal places.

---

## Quality Bar

Every worker's output gets reviewed before it's accepted. You will be sent back if:

- You changed anything outside your assigned scope
- You introduced a syntax error or broke an import
- Your fix doesn't actually address the bug described
- You added comments explaining things that are self-evident from the code
- You wrote any new abstractions, helper functions, or classes that weren't asked for

When in doubt, do less. A minimal correct fix beats an elegant over-engineered one.

---

## What's NOT Your Job (Phase 2 — don't touch yet)

- GLiNER integration
- ~~LLM geotagging layer (`pipeline/llm_geotagging.py`)~~ — **built 2026-10-06**, see FRAMEWORK.md's Build Gameplan
- FastAPI server (`api/`)
- Photon geocoder swap
- 4-pillar confidence scorer
- `fly.toml` / deployment config
- Frontend (MapLibre, GitHub Pages)

Those come later. Stay in your lane.

---

## Addendum — MVP Architecture Pivot (2026-10-05)

Read `FRAMEWORK.md` → "MVP Pivot — GitHub-Only, NYC-Only" before planning any Phase 2 work. Short version: we are not building the Fly.io/FastAPI/WebSocket backend next. The MVP is NYC-only, runs entirely inside GitHub (Actions on a 6-minute schedule + Pages for the static frontend), and needs only 3 external accounts (GitHub, Groq, Bluesky) instead of 8.

**Workers A–E: your current assignments (WAL mode, collector fixes, EMOJI ordering, geocoder validation, writer security fixes) are unchanged — fix those first regardless of architecture.**

One addition to "What's NOT Your Job": the FastAPI server, Fly.io deployment, and WebSocket layer are no longer the Phase 2 target. They're deferred indefinitely pending the GitHub-only MVP proving out. Do not start that work without explicit new assignment.

---

*Questions go up the chain. Don't guess on security decisions.*
