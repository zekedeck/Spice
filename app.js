/* Spice — frontend app
 * Static site, no build step. Loads data/live.geojson by default, or a
 * merged range of data/archive/YYYY-MM-DD.geojson files when a lookback
 * range is applied. Boolean keyword search filters whatever is currently
 * loaded. All displayed times are America/New_York (ET).
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // Password gate. Casual-visitor deterrent ONLY -- not real access
  // control. GitHub Pages serves everything as plain static files with no
  // server-side execution, so nothing in this file can actually stop a
  // motivated visitor from fetching data/live.geojson (or this very
  // script) directly by URL. The point is just to keep an idle/accidental
  // visitor from landing on the live map, not to withstand an attacker.
  // No rate-limiting/lockout/delay by design -- that would imply a
  // stronger guarantee than this is meant to make.
  //
  // The one thing that DOES matter here: the gate must stop the actual
  // data fetches, not just hide their rendered result behind an overlay.
  // See startApp() / the bottom of this file -- initMap() (which triggers
  // loadLive()'s fetch of data/live.geojson) and loadCameras() (which
  // fetches data/nyc_cameras.json) only ever run from inside startApp(),
  // which only runs once the gate is passed. Nothing above that point
  // touches the network.
  //
  // GATE_PASSWORD_HASH_HEX holds the SHA-256 hex digest of the real
  // password, compared against a hash of whatever's typed in (via
  // crypto.subtle.digest) -- never the plaintext itself. To generate the
  // real value once you have the real password, run ONE of:
  //
  //   Browser console (on this site, or any page):
  //     crypto.subtle.digest("SHA-256", new TextEncoder().encode("the real password"))
  //       .then(buf => console.log(
  //         [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("")
  //       ));
  //
  //   Node.js (18+, no deps):
  //     node -e "crypto.subtle.digest('SHA-256', new TextEncoder().encode('the real password')).then(b => console.log(Buffer.from(b).toString('hex')))"
  //
  // Paste the resulting 64-character hex string in as this constant's
  // value. Left as this exact placeholder, it is NOT a valid SHA-256 hex
  // digest (wrong length, wrong charset) -- it cannot accidentally match
  // any real password's hash, so the gate visibly fails every attempt
  // until replaced.
  var GATE_PASSWORD_HASH_HEX = "abe21e6c92f1793f0f40ba75b8137193f7bca632dfcbcc6492b221acf9edfd89";
  // localStorage (not sessionStorage): deliberately persists across tabs
  // and future visits, so the owner and anyone they've shared the
  // password with aren't re-prompted every single time -- this is a
  // casual deterrent, not a re-auth-every-session model.
  var GATE_UNLOCK_STORAGE_KEY = "spiceGateUnlocked";

  var gateOverlay = document.getElementById("gate-overlay");
  var gateForm = document.getElementById("gate-form");
  var gateInput = document.getElementById("gate-password-input");
  var gateError = document.getElementById("gate-error");

  function sha256Hex(text) {
    var data = new TextEncoder().encode(text);
    return crypto.subtle.digest("SHA-256", data).then(function (buf) {
      var bytes = new Uint8Array(buf);
      var hex = "";
      for (var i = 0; i < bytes.length; i++) {
        hex += bytes[i].toString(16).padStart(2, "0");
      }
      return hex;
    });
  }

  function isGateUnlocked() {
    try {
      return localStorage.getItem(GATE_UNLOCK_STORAGE_KEY) === "1";
    } catch (e) {
      return false; // localStorage unavailable (private mode, etc.): fail closed, re-show the gate
    }
  }

  function unlockGate() {
    try {
      localStorage.setItem(GATE_UNLOCK_STORAGE_KEY, "1");
    } catch (e) {
      // Can't persist -- gate will just re-show next visit. Not fatal.
    }
    document.body.classList.add("gate-unlocked");
    startApp();
  }

  function showGateError(msg) {
    if (!gateError) return;
    gateError.textContent = msg;
    gateError.hidden = !msg;
  }

  function handleGateSubmit(e) {
    e.preventDefault();
    var entered = gateInput ? gateInput.value : "";
    sha256Hex(entered).then(function (hex) {
      if (hex === GATE_PASSWORD_HASH_HEX) {
        showGateError("");
        unlockGate();
      } else {
        showGateError("Wrong password.");
      }
    });
  }

  if (gateForm) gateForm.addEventListener("submit", handleGateSubmit);

  var ET_ZONE = "America/New_York";
  var LIVE_REFRESH_MS = 15 * 60 * 1000; // matches the pipeline's 15-min native cron cadence
  // Live-window slider: purely client-side narrowing of the already-loaded
  // live dataset, capped at 24h. The server-side window (config.py's
  // LIVE_WINDOW_HOURS = 48, baked into data/live.geojson by worker.py) is
  // already wider than this cap, so every slider position just filters
  // what's already loaded -- no refetch, no backend change.
  var LIVE_WINDOW_MIN_HOURS = 1;
  var LIVE_WINDOW_MAX_HOURS = 24;
  var liveWindowHours = LIVE_WINDOW_MAX_HOURS; // default: the max, i.e. no extra narrowing
  var CAMERA_RADIUS_M = 400; // nearby-camera lookup radius
  var CAMERA_MAX_COUNT = 3; // cap shown per pin so dense areas (Midtown) don't clutter the panel
  var CAMERA_REFRESH_MS = 1000; // how often open camera thumbnails re-fetch their snapshot
  // Webcams filter is meant to show every camera on the map, not a
  // zoomed-in subset -- no minzoom gate (previously 14, which hid all
  // ~979 dots at the default citywide view; reported live as "shows
  // nothing").

  // Radius filter: draw-a-circle + synced 1-25mi slider, restricting visible
  // pins to within some distance of a chosen center. RADIUS_EARTH_MILES
  // matches pipeline/radius_filter.py's haversine_miles() constant exactly
  // (same formula, ported rather than reinvented) so "N miles" here means
  // the same thing it would mean server-side.
  var RADIUS_MIN_MILES = 1;
  var RADIUS_MAX_MILES = 25;
  var RADIUS_DEFAULT_MILES = 5;
  var RADIUS_EARTH_MILES = 3958.8;
  var MILES_TO_METERS = 1609.344;
  var RADIUS_CIRCLE_POINTS = 64;

  var map = null;
  var mode = "live"; // "live" | "archive"
  var allFeatures = []; // currently loaded map-pin feature set (post-lookback, pre-search)
  var allStateMentions = []; // currently loaded state/country-precision features (post-lookback, pre-search)
  var selectedUri = null;
  var liveRefreshTimer = null;
  var nycCameras = []; // static NYC DOT traffic-camera reference data; loaded once, no network per-lookup
  var cameraRefreshTimer = null; // re-fetches open camera thumbnails on a tick; see startCameraRefresh()

  // Radius-filter state: null center means the filter is inactive (passes
  // everything). radiusMiles still holds a value even when inactive, purely
  // so the slider has something sane to show/start from.
  var radiusCenter = null; // [lng, lat] or null
  var radiusMiles = RADIUS_DEFAULT_MILES;
  var drawModeActive = false;
  var isDrawingDrag = false;
  // Move-existing-circle state: separate from the draw-a-new-circle state
  // above. Active whenever a mousedown lands inside an already-drawn
  // circle's filled interior (and not on a pin/camera dot) while
  // drawModeActive is false -- see the map "mousedown" handler below.
  var isMovingCircle = false;
  var moveDragStartLngLat = null; // [lng, lat] where the move-drag began
  var moveDragStartCenter = null; // radiusCenter at the moment the drag began

  var statusLine = document.getElementById("status-line");
  var detailPanel = document.getElementById("detail-panel");
  var detailContent = document.getElementById("detail-content");
  var detailClose = document.getElementById("detail-close");
  var searchForm = document.getElementById("search-form");
  var searchInput = document.getElementById("search-input");
  var lookbackFromDate = document.getElementById("lookback-from-date");
  var lookbackFromTime = document.getElementById("lookback-from-time");
  var lookbackToDate = document.getElementById("lookback-to-date");
  var lookbackToTime = document.getElementById("lookback-to-time");
  var lookbackApply = document.getElementById("lookback-apply");
  var lookbackReset = document.getElementById("lookback-reset");
  var lookbackError = document.getElementById("lookback-error");
  var lookbackSummaryText = document.getElementById("lookback-summary-text");
  var lookbackDetails = document.getElementById("lookback-details");
  var liveWindowFieldset = document.getElementById("live-window-fieldset");
  var liveWindowSlider = document.getElementById("live-window-slider");
  var liveWindowValueEl = document.getElementById("live-window-value");
  var calendarGrid = document.getElementById("calendar-grid");
  var calendarMonthLabel = document.getElementById("calendar-month-label");
  var calendarPrevBtn = document.getElementById("calendar-prev");
  var calendarNextBtn = document.getElementById("calendar-next");
  var calendarSelectionText = document.getElementById("calendar-selection-text");
  var statePanelList = document.getElementById("state-panel-list");
  var statePanelCount = document.getElementById("state-panel-count");
  var statePanelEmpty = document.getElementById("state-panel-empty");
  var toggleProtests = document.getElementById("toggle-protests");
  var toggleCrime = document.getElementById("toggle-crime");
  var togglePolice = document.getElementById("toggle-police");
  var toggleWebcams = document.getElementById("toggle-webcams");
  // radiusPanel: the whole Geofence <details> element itself (not just its
  // inner panel body) -- is-filtering gets toggled on this so the topbar
  // tab shows an active state even while its panel is closed.
  var radiusPanel = document.getElementById("geofence-details");
  var geofenceDetails = radiusPanel;
  var geofenceSummaryText = document.getElementById("geofence-summary-text");
  var radiusDrawToggle = document.getElementById("radius-draw-toggle");
  var radiusSlider = document.getElementById("radius-slider");
  var radiusValueEl = document.getElementById("radius-value");
  var radiusClearBtn = document.getElementById("radius-clear");

  function setStatus(msg) {
    statusLine.textContent = msg || "";
  }

  // ---------------------------------------------------------------------
  // Timezone helpers (America/New_York), no external library.
  // ---------------------------------------------------------------------

  // Converts an Intl.DateTimeFormat#formatToParts() result into a plain
  // { year, month, day, hour, minute, second, ... } object keyed by part type.
  function partsToObject(parts) {
    var obj = {};
    for (var i = 0; i < parts.length; i++) {
      obj[parts[i].type] = parts[i].value;
    }
    return obj;
  }

  // Returns the UTC-minus-local offset, in ms, for `timeZone` at `date`.
  // i.e. localWallClockAsUTCNumber = date.getTime() + offsetMs
  function tzOffsetMs(timeZone, date) {
    var dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
    var obj = partsToObject(dtf.formatToParts(date));
    var asLocal = Date.UTC(
      parseInt(obj.year, 10),
      parseInt(obj.month, 10) - 1,
      parseInt(obj.day, 10),
      parseInt(obj.hour, 10),
      parseInt(obj.minute, 10),
      parseInt(obj.second, 10)
    );
    return asLocal - date.getTime();
  }

  // Converts wall-clock ET components (from a <input type="datetime-local">
  // value, already entered by the user as ET) into a real UTC Date instant.
  function etComponentsToUtcDate(year, month, day, hour, minute) {
    var guessUtcMs = Date.UTC(year, month - 1, day, hour, minute, 0);
    var offsetMs = tzOffsetMs(ET_ZONE, new Date(guessUtcMs));
    return new Date(guessUtcMs - offsetMs);
  }

  // Combines a <input type="date"> value ("YYYY-MM-DD") and a
  // <input type="time"> value ("HH:MM" or "HH:MM:SS") into a
  // { year, month, day, hour, minute } object.
  // An empty/missing time defaults to midnight.
  function combineDateAndTimeValue(dateValue, timeValue) {
    var dm = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateValue || "");
    if (!dm) return null;
    var tm = /^(\d{2}):(\d{2})/.exec(timeValue || "");
    return {
      year: parseInt(dm[1], 10),
      month: parseInt(dm[2], 10),
      day: parseInt(dm[3], 10),
      hour: tm ? parseInt(tm[1], 10) : 0,
      minute: tm ? parseInt(tm[2], 10) : 0
    };
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  // Formats a UTC Date as an ET date string "YYYY-MM-DD".
  function etDateString(date) {
    var dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: ET_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    });
    var obj = partsToObject(dtf.formatToParts(date));
    return obj.year + "-" + obj.month + "-" + obj.day;
  }

  function formatEt(date) {
    var dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: ET_ZONE,
      dateStyle: "medium",
      timeStyle: "short"
    });
    return dtf.format(date) + " ET";
  }

  function timeAgo(createdAtIso) {
    var created = new Date(createdAtIso).getTime();
    var diffMs = Date.now() - created;
    if (diffMs < 0) diffMs = 0;
    var sec = Math.floor(diffMs / 1000);
    if (sec < 60) return sec + "s ago";
    var min = Math.floor(sec / 60);
    if (min < 60) return min + "m ago";
    var hr = Math.floor(min / 60);
    if (hr < 24) return hr + "h ago";
    var day = Math.floor(hr / 24);
    return day + "d ago";
  }

  // Returns the list of ET calendar-day strings ("YYYY-MM-DD") spanned by
  // two UTC instants, inclusive.
  function etDaySpan(fromDate, toDate) {
    var days = [];
    var startStr = etDateString(fromDate);
    var cursor = new Date(Date.UTC(
      parseInt(startStr.slice(0, 4), 10),
      parseInt(startStr.slice(5, 7), 10) - 1,
      parseInt(startStr.slice(8, 10), 10)
    ));
    var endStr = etDateString(toDate);
    var guard = 0;
    while (guard < 31) {
      var curStr = cursor.getUTCFullYear() + "-" + pad2(cursor.getUTCMonth() + 1) + "-" + pad2(cursor.getUTCDate());
      days.push(curStr);
      if (curStr === endStr) break;
      cursor.setUTCDate(cursor.getUTCDate() + 1);
      guard++;
    }
    return days;
  }

  // ---------------------------------------------------------------------
  // Boolean keyword search: AND / OR / NOT / "quoted phrases" / (grouping)
  // Grammar (left-to-right, AND/NOT bind tighter than OR):
  //   expr   := andExpr (OR andExpr)*
  //   andExpr:= notTerm (AND? notTerm)*   -- AND is implicit if omitted
  //   notTerm:= NOT? primary
  //   primary:= WORD | PHRASE | "(" expr ")"
  //
  // Term matching, decided per-term at parse time (buildTermNode):
  //   - a term containing internal whitespace (a real multi-word phrase,
  //     including an intentional leading space like " rally") is always a
  //     literal, case-insensitive substring match.
  //   - a single "word" (no whitespace, whether it arrived quoted or bare)
  //     ending in "*" is a case-insensitive, word-prefix wildcard match
  //     (protest* -> \bprotest\w*): matches protest/protests/protester/...
  //   - a single word with no wildcard, <=5 chars, and ALL CAPS (ICE, NYPD)
  //     is a case-SENSITIVE, whole-word match (\bICE\b against the
  //     original-case text) -- this is what keeps "ICE" from colliding
  //     with "iced coffee" / "nice" / "SPICE", etc.
  //   - everything else is a case-insensitive substring match, as before.
  // ---------------------------------------------------------------------

  function tokenizeQuery(raw) {
    var tokens = [];
    var re = /"([^"]*)"|([()])|([^\s()"]+)/g;
    var m;
    while ((m = re.exec(raw)) !== null) {
      if (m[1] !== undefined) {
        tokens.push({ type: "phrase", value: m[1] });
      } else if (m[2] !== undefined) {
        tokens.push({ type: "paren", value: m[2] });
      } else {
        var word = m[3];
        var upper = word.toUpperCase();
        if (upper === "AND" || upper === "OR" || upper === "NOT") {
          tokens.push({ type: "op", value: upper });
        } else {
          tokens.push({ type: "word", value: word });
        }
      }
    }
    return tokens;
  }

  function escapeRegExp(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  var ALL_CAPS_RE = /^[A-Z]+$/;

  // Builds the term-matching node for a single parsed WORD/PHRASE value
  // (original case preserved in `raw`). See the matching rules above.
  function buildTermNode(raw) {
    if (/\s/.test(raw)) {
      return { kind: "term", mode: "substring", value: raw.toLowerCase() };
    }
    if (raw.length > 1 && raw.charAt(raw.length - 1) === "*") {
      var prefix = raw.slice(0, -1).toLowerCase();
      return { kind: "term", mode: "prefix", re: new RegExp("\\b" + escapeRegExp(prefix) + "\\w*") };
    }
    if (raw.length <= 5 && ALL_CAPS_RE.test(raw)) {
      return { kind: "term", mode: "exact-case", re: new RegExp("\\b" + escapeRegExp(raw) + "\\b") };
    }
    return { kind: "term", mode: "substring", value: raw.toLowerCase() };
  }

  function parseBooleanQuery(raw) {
    var tokens = tokenizeQuery(raw);
    var pos = 0;

    function peek() {
      return pos < tokens.length ? tokens[pos] : null;
    }
    function next() {
      return tokens[pos++];
    }

    function parsePrimary() {
      var t = peek();
      if (!t) return null;
      if (t.type === "paren" && t.value === "(") {
        next();
        var inner = parseOr();
        var close = peek();
        if (close && close.type === "paren" && close.value === ")") {
          next();
        }
        return inner;
      }
      if (t.type === "word" || t.type === "phrase") {
        next();
        return buildTermNode(t.value);
      }
      // Stray operator or unmatched paren with nothing to bind to: skip it.
      next();
      return null;
    }

    function parseNotTerm() {
      var negate = false;
      while (peek() && peek().type === "op" && peek().value === "NOT") {
        next();
        negate = !negate;
      }
      var primary = parsePrimary();
      if (!primary) return null;
      return negate ? { kind: "not", child: primary } : primary;
    }

    function parseAnd() {
      var left = parseNotTerm();
      if (!left) return null;
      while (true) {
        var t = peek();
        if (!t) break;
        if (t.type === "op" && t.value === "OR") break;
        if (t.type === "paren" && t.value === ")") break; // closes an enclosing group
        if (t.type === "op" && t.value === "AND") next(); // explicit AND
        // otherwise: implicit AND, don't consume
        var right = parseNotTerm();
        if (!right) break;
        left = { kind: "and", left: left, right: right };
      }
      return left;
    }

    function parseOr() {
      var left = parseAnd();
      if (!left) return null;
      while (true) {
        var t = peek();
        if (!t || t.type !== "op" || t.value !== "OR") break;
        next();
        var right = parseAnd();
        if (!right) break;
        left = { kind: "or", left: left, right: right };
      }
      return left;
    }

    return parseOr();
  }

  function evalBooleanNode(node, ctx) {
    if (!node) return true;
    switch (node.kind) {
      case "term":
        if (node.mode === "prefix") return node.re.test(ctx.lower);
        if (node.mode === "exact-case") return node.re.test(ctx.original);
        return ctx.lower.indexOf(node.value) !== -1;
      case "not":
        return !evalBooleanNode(node.child, ctx);
      case "and":
        return evalBooleanNode(node.left, ctx) && evalBooleanNode(node.right, ctx);
      case "or":
        return evalBooleanNode(node.left, ctx) || evalBooleanNode(node.right, ctx);
      default:
        return true;
    }
  }

  function matchesSearch(feature, ast) {
    if (!ast) return true;
    var text = (feature.properties && feature.properties.text) || "";
    return evalBooleanNode(ast, { lower: text.toLowerCase(), original: text });
  }

  // ---------------------------------------------------------------------
  // Category filters: four toggle switches reusing the boolean engine
  // above. Queries are corpus-validated finalized strings -- not meant to
  // be redesigned here. Parsed once at startup.
  // ---------------------------------------------------------------------

  // "rally"/"rallies" guarded against sports-comeback usage ("4th-quarter
  // rally", "rally from a deficit") via a NOT-exclusion rather than
  // requiring a co-occurring protest word -- that would also drop genuine
  // standalone rally posts with no other protest-shaped word nearby.
  // Corpus-validated: kills both a real NFL and a real NBA false positive,
  // adds zero new matches, every other query unchanged.
  var PROTEST_AST = parseBooleanQuery(
    '"protest*" OR ((" rally" OR "rallies") NOT ("quarter" OR " inning" OR "deficit" OR "losing skid" OR "comeback" OR "playoff" OR "halftime" OR "overtime" OR "touchdown")) OR "vigil" OR "no kings" OR ("march*" AND ("protest*" OR " rally" OR "rallies" OR "demonstrat*" OR "mobiliz*" OR "field call"))'
  );
  var CRIME_AST = parseBooleanQuery(
    '((" shot" OR "shooting") AND ("gun" OR "ICE" OR "police" OR "NYPD" OR "agent*" OR "wound*" OR "injur*" OR "victim" OR "kill*" OR "arrest*" OR "federal")) OR "stabbed" OR "stabbing" OR ("assault*" AND ("arrest*" OR "charge*" OR "convict*" OR "sentenc*" OR "court" OR "custody" OR "police" OR "NYPD" OR "victim" OR "probation")) OR "arrest*"'
  );
  var POLICE_AST = parseBooleanQuery(
    '("ICE" AND ("detain*" OR "arrest*" OR "grabbed" OR "shot" OR "shoot*" OR "wound*" OR "raid*" OR "deport*" OR "custody" OR "operation" OR "gunfire" OR "protest")) OR (("NYPD" OR "police") AND ("shot" OR "shooting" OR "arrest*" OR "wound*" OR "search*" OR "investigat*" OR "respond*" OR "pursuit" OR "custody" OR "raid*" OR "cyclist" OR "red light" OR "injur*" OR "gunmen" OR "shootout" OR "gang"))'
  );

  // Priority order when a post matches more than one category: Protests ->
  // Crime -> Police Activity. Picks the first match, consistently, for the
  // single icon a pin shows. Computed once per feature at annotate() time,
  // independent of which toggles are currently active -- the toggles only
  // control which pins are *visible*; a visible pin's icon always reflects
  // this classification.
  function classifyCategory(text) {
    var fakeFeature = { properties: { text: text } };
    if (matchesSearch(fakeFeature, PROTEST_AST)) return "protest";
    if (matchesSearch(fakeFeature, CRIME_AST)) return "crime";
    if (matchesSearch(fakeFeature, POLICE_AST)) return "police";
    return "";
  }

  // ---------------------------------------------------------------------
  // Feature annotation (color category) + filtering pipeline
  // ---------------------------------------------------------------------

  function categorize(props) {
    var confidence = typeof props.confidence === "number" ? props.confidence : 1;
    if (confidence < 0.6) return "flagged";
    var precision = (props.precision_level || "").toLowerCase();
    if (precision.indexOf("address") === 0) return "address";
    if (precision === "city") return "city";
    return "region";
  }

  function computeAgeMinutes(createdAtIso) {
    var created = new Date(createdAtIso).getTime();
    return (Date.now() - created) / 60000;
  }

  function annotate(features) {
    for (var i = 0; i < features.length; i++) {
      var props = features[i].properties || {};
      props.category = categorize(props);
      props.ageMinutes = computeAgeMinutes(props.created_at);
      props.matchCategory = classifyCategory(props.text || "");
      features[i].properties = props;
    }
    return features;
  }

  // Low-specificity mentions (state/country precision) are misleading as map
  // pins since they all collapse onto the same generic centroid. Split them
  // out so they never reach the "pins" source and instead render as a list.
  function isStateMention(feature) {
    var level = ((feature.properties && feature.properties.precision_level) || "").toLowerCase();
    return level === "state" || level === "country";
  }

  function splitFeatures(features) {
    var pins = [];
    var stateMentions = [];
    for (var i = 0; i < features.length; i++) {
      if (isStateMention(features[i])) {
        stateMentions.push(features[i]);
      } else {
        pins.push(features[i]);
      }
    }
    return { pins: pins, stateMentions: stateMentions };
  }

  // ---------------------------------------------------------------------
  // Same-coordinate stack splitting.
  //
  // Multiple posts routinely resolve to the exact same coordinate (shared
  // address, generic landmark point, etc.) -- handlePinClick/
  // openLocationPicker already handle that case by querying every feature
  // under the clicked pixel and offering a picker list, so no post is ever
  // silently hidden. But visually, a stack of 2 or 18 posts still renders
  // as a single dot, which flattens real signal: several independent posts
  // corroborating the same event at the same spot look identical to one
  // lone mention.
  //
  // Splitting it visually is only warranted when the stack is actually
  // reporting DIFFERENT things, i.e. 2+ distinct matchCategory values
  // (classifyCategory()'s "protest"/"crime"/"police"/"" -- "no category"
  // counts as one of the distinct values same as any other). A stack that's
  // entirely one category stays exactly as it renders today: one dot, full
  // list still reachable via the click picker.
  //
  // When a coordinate's stack DOES mix categories, each category gets its
  // own small offset point (not each individual post) -- so e.g. 3 "protest"
  // + 2 "crime" posts at one spot become two dots, one per category, each
  // still stacked internally and still resolved via the picker if that
  // category's subgroup has more than one member. This keeps
  // handlePinClick's queryRenderedFeatures-at-the-click-pixel dedup logic
  // correct unchanged: a same-category subgroup stays at one exact point
  // (picker still needed), a now-separated category pin is its own point
  // (no picker needed once it's down to one post there).
  //
  // Operates on whatever feature array is about to become the "pins"
  // source's GeoJSON (see applyFilters()) -- it does NOT mutate the
  // underlying objects in allFeatures, so re-filtering always starts from
  // the true, un-offset coordinates rather than compounding drift.
  // ---------------------------------------------------------------------

  var STACK_OFFSET_CATEGORY_ORDER = ["protest", "crime", "police", ""];
  var STACK_OFFSET_METERS = 50; // target minimum distance between adjacent offset points

  // Small-distance equirectangular approximation -- plenty accurate at the
  // tens-of-meters scale these offsets operate at.
  function metersToLngLatDelta(lat, bearingRad, meters) {
    var dLat = (meters * Math.cos(bearingRad)) / 111320;
    var dLng = (meters * Math.sin(bearingRad)) / (111320 * Math.cos((lat * Math.PI) / 180));
    return [dLng, dLat];
  }

  function withStackOffsets(features) {
    var groups = {};
    var groupOrder = [];
    for (var i = 0; i < features.length; i++) {
      var coords = features[i].geometry.coordinates;
      var key = coords[0] + "," + coords[1];
      if (!groups[key]) {
        groups[key] = [];
        groupOrder.push(key);
      }
      groups[key].push(features[i]);
    }

    var output = [];
    for (var g = 0; g < groupOrder.length; g++) {
      var members = groups[groupOrder[g]];

      var byCategory = {};
      var categoriesPresent = [];
      for (var j = 0; j < members.length; j++) {
        var cat = (members[j].properties && members[j].properties.matchCategory) || "";
        if (!byCategory[cat]) {
          byCategory[cat] = [];
          categoriesPresent.push(cat);
        }
        byCategory[cat].push(members[j]);
      }

      if (categoriesPresent.length <= 1) {
        // Entirely one category (or entirely uncategorized) -- leave
        // untouched, stays a single visual dot.
        for (var k = 0; k < members.length; k++) output.push(members[k]);
        continue;
      }

      // 2+ distinct categories at this coordinate: give each category its
      // own offset point on a small circle. Fixed ordering keeps the
      // arrangement deterministic regardless of array/object-key order.
      var orderedCats = STACK_OFFSET_CATEGORY_ORDER.filter(function (c) {
        return categoriesPresent.indexOf(c) !== -1;
      });
      var n = orderedCats.length;
      var baseCoords = members[0].geometry.coordinates;
      var radius = STACK_OFFSET_METERS / (2 * Math.sin(Math.PI / n));

      for (var c = 0; c < n; c++) {
        var bearing = (2 * Math.PI * c) / n;
        var delta = metersToLngLatDelta(baseCoords[1], bearing, radius);
        var catMembers = byCategory[orderedCats[c]];
        for (var m = 0; m < catMembers.length; m++) {
          var original = catMembers[m];
          output.push({
            type: "Feature",
            properties: original.properties,
            geometry: {
              type: "Point",
              coordinates: [baseCoords[0] + delta[0], baseCoords[1] + delta[1]]
            }
          });
        }
      }
    }
    return output;
  }

  // ---------------------------------------------------------------------
  // Radius filter
  // ---------------------------------------------------------------------

  // Ported directly from pipeline/radius_filter.py's haversine_miles() --
  // same standard haversine formula, same R=3958.8 Earth-radius constant --
  // so this filter's notion of "N miles" matches the already-validated
  // server-side one instead of inventing separate distance math client-side.
  function haversineMiles(lat1, lng1, lat2, lng2) {
    var dLat = ((lat2 - lat1) * Math.PI) / 180;
    var dLng = ((lng2 - lng1) * Math.PI) / 180;
    var a =
      Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos((lat1 * Math.PI) / 180) *
        Math.cos((lat2 * Math.PI) / 180) *
        Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return RADIUS_EARTH_MILES * 2 * Math.asin(Math.sqrt(a));
  }

  // Real geographic circle (constant real-world radius), not a flat
  // degree-offset ellipse: walks a full bearing sweep through the same
  // metersToLngLatDelta() small-distance conversion the pin-stacking offsets
  // above already use, converting miles -> meters at the boundary rather
  // than writing separate trig for this feature.
  function radiusCircleRing(center, miles) {
    var meters = miles * MILES_TO_METERS;
    var ring = [];
    for (var i = 0; i <= RADIUS_CIRCLE_POINTS; i++) {
      var bearing = (2 * Math.PI * i) / RADIUS_CIRCLE_POINTS;
      var delta = metersToLngLatDelta(center[1], bearing, meters);
      ring.push([center[0] + delta[0], center[1] + delta[1]]);
    }
    return ring;
  }

  function radiusCircleGeoJSON() {
    if (!radiusCenter) return { type: "FeatureCollection", features: [] };
    return {
      type: "FeatureCollection",
      features: [
        {
          type: "Feature",
          properties: {},
          geometry: { type: "Polygon", coordinates: [radiusCircleRing(radiusCenter, radiusMiles)] }
        }
      ]
    };
  }

  function updateRadiusCircleLayer() {
    var source = map && map.getSource("radius-circle");
    if (source) source.setData(radiusCircleGeoJSON());
  }

  // One decimal place, trailing ".0" stripped (so a whole number like 5
  // still reads "5 mi" rather than "5.0 mi") -- radiusMiles itself stays a
  // full-precision float; this is purely a display format.
  function formatMiles(miles) {
    var rounded = Math.round(miles * 10) / 10;
    return (rounded % 1 === 0 ? rounded.toFixed(0) : rounded.toFixed(1)) + " mi";
  }

  function updateRadiusValueLabel() {
    var label = formatMiles(radiusMiles);
    if (radiusValueEl) radiusValueEl.textContent = label;
    // Mirrors the label into the always-visible topbar summary too, since
    // the Geofence <details> panel itself gets closed while actively
    // drawing/resizing (see setDrawModeActive()) -- this is the only live
    // feedback visible at that point.
    if (geofenceSummaryText) geofenceSummaryText.textContent = radiusCenter ? label : "";
  }

  function updateRadiusActiveUi() {
    if (radiusClearBtn) radiusClearBtn.hidden = !radiusCenter;
    if (radiusPanel) radiusPanel.classList.toggle("is-filtering", !!radiusCenter);
  }

  // No radius filter active -> passes unconditionally, matching
  // passesLiveWindow(f)'s pattern just below.
  function passesRadius(f) {
    if (!radiusCenter) return true;
    var coords = f.geometry && f.geometry.coordinates;
    if (!coords) return true;
    return haversineMiles(radiusCenter[1], radiusCenter[0], coords[1], coords[0]) <= radiusMiles;
  }

  function setDrawModeActive(active) {
    drawModeActive = active;
    isDrawingDrag = false;
    if (radiusDrawToggle) {
      radiusDrawToggle.classList.toggle("is-active", active);
      radiusDrawToggle.setAttribute("aria-pressed", active ? "true" : "false");
      radiusDrawToggle.textContent = active ? "Click + drag on map…" : "Draw circle";
    }
    // Arming draw mode closes the Geofence topbar panel: the actual
    // drawing happens by click-and-dragging on the MAP, not inside the
    // panel, and closing it up front avoids the panel's own dropdown box
    // sitting on top of (and blocking clicks on) whatever part of the map
    // the user wants to draw on. The topbar summary itself (via
    // updateRadiusValueLabel()) keeps showing the live radius while
    // drawing, so nothing is lost by closing it.
    if (active && geofenceDetails) geofenceDetails.open = false;
    if (map) {
      // Disabling dragPan while drawing keeps mousedown-drag from panning
      // the map instead of sizing the circle.
      if (active) {
        map.dragPan.disable();
        map.getCanvas().style.cursor = "crosshair";
      } else {
        map.dragPan.enable();
        map.getCanvas().style.cursor = "";
      }
    }
  }

  function clearRadiusFilter() {
    radiusCenter = null;
    isDrawingDrag = false;
    radiusMiles = RADIUS_DEFAULT_MILES;
    if (radiusSlider) radiusSlider.value = String(RADIUS_DEFAULT_MILES);
    updateRadiusValueLabel();
    updateRadiusActiveUi();
    updateRadiusCircleLayer();
    applyFilters();
  }

  function currentAst() {
    var raw = searchInput.value.trim();
    if (!raw) return null;
    return parseBooleanQuery(raw);
  }

  // Active category-toggle ASTs, OR'd together: a pin must match at least
  // one active category to pass. With zero toggles active, there's no
  // category restriction at all (matches everything) -- so turning on
  // more toggles only ever adds pins back in, never removes any beyond
  // what the free-text search already excluded.
  function activeCategoryAsts() {
    var asts = [];
    if (toggleProtests && toggleProtests.checked) asts.push(PROTEST_AST);
    if (toggleCrime && toggleCrime.checked) asts.push(CRIME_AST);
    if (togglePolice && togglePolice.checked) asts.push(POLICE_AST);
    return asts;
  }

  function matchesAnyAst(feature, asts) {
    if (asts.length === 0) return true;
    for (var i = 0; i < asts.length; i++) {
      if (matchesSearch(feature, asts[i])) return true;
    }
    return false;
  }

  // Live-window slider only applies in "live" mode -- an archive lookback
  // range is a deliberate, already-bounded historical selection, so the
  // slider's hours-ago cap doesn't mean anything there (and its fieldset is
  // disabled in the UI while archive mode is active; see
  // setLiveWindowControlEnabled()).
  function passesLiveWindow(f) {
    if (mode !== "live") return true;
    var age = f.properties ? f.properties.ageMinutes : undefined;
    if (typeof age !== "number" || isNaN(age)) return true;
    return age <= liveWindowHours * 60;
  }

  function applyFilters() {
    var ast = currentAst();
    var categoryAsts = activeCategoryAsts();
    function passes(f) {
      return (
        (ast ? matchesSearch(f, ast) : true) &&
        matchesAnyAst(f, categoryAsts) &&
        passesLiveWindow(f) &&
        passesRadius(f)
      );
    }
    var filteredPins = allFeatures.filter(passes);
    var filteredStateMentions = allStateMentions.filter(passes);
    clearSelection();
    var source = map.getSource("pins");
    if (source) {
      source.setData({ type: "FeatureCollection", features: withStackOffsets(filteredPins) });
    }
    renderStateMentions(filteredStateMentions);
    setStatus(filteredPins.length + " pin" + (filteredPins.length === 1 ? "" : "s") + " shown");
  }

  // ---------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------

  function liveWindowLabel() {
    return "Live (last " + liveWindowHours + "h)";
  }

  function setLiveWindowControlEnabled(enabled) {
    if (liveWindowSlider) liveWindowSlider.disabled = !enabled;
    if (liveWindowFieldset) liveWindowFieldset.classList.toggle("is-disabled", !enabled);
  }

  function loadLive() {
    mode = "live";
    setLiveWindowControlEnabled(true);
    lookbackSummaryText.textContent = liveWindowLabel();
    return fetch("data/live.geojson", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("live.geojson HTTP " + res.status);
        return res.json();
      })
      .then(function (geojson) {
        var annotated = annotate((geojson && geojson.features) || []);
        var split = splitFeatures(annotated);
        allFeatures = split.pins;
        allStateMentions = split.stateMentions;
        applyFilters();
      })
      .catch(function (err) {
        setStatus("Could not load live data.");
        console.error(err);
      });
  }

  function fetchArchiveDay(dayStr) {
    return fetch("data/archive/" + dayStr + ".geojson", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) return []; // missing day file: skip gracefully
        return res.json();
      })
      .then(function (geojson) {
        return (geojson && geojson.features) || [];
      })
      .catch(function () {
        return []; // network error on one day: skip, don't break the query
      });
  }

  function loadArchiveRange(fromUtc, toUtc) {
    mode = "archive";
    setLiveWindowControlEnabled(false);
    var days = etDaySpan(fromUtc, toUtc);
    setStatus("Loading " + days.length + " day" + (days.length === 1 ? "" : "s") + "…");
    return Promise.all(days.map(fetchArchiveDay)).then(function (dayFeatureLists) {
      var merged = [];
      for (var i = 0; i < dayFeatureLists.length; i++) {
        merged = merged.concat(dayFeatureLists[i]);
      }
      var fromMs = fromUtc.getTime();
      var toMs = toUtc.getTime();
      var inRange = merged.filter(function (f) {
        var created = new Date(f.properties.created_at).getTime();
        return created >= fromMs && created <= toMs;
      });
      var annotated = annotate(inRange);
      var split = splitFeatures(annotated);
      allFeatures = split.pins;
      allStateMentions = split.stateMentions;
      applyFilters();
      lookbackSummaryText.textContent = formatEt(fromUtc) + " → " + formatEt(toUtc);
    });
  }

  // ---------------------------------------------------------------------
  // Lookback controls
  // ---------------------------------------------------------------------

  // ---------------------------------------------------------------------
  // Visible calendar (date-range picker). Click a start day, then an end
  // day; the selected range is written into the hidden lookback-from-date
  // / lookback-to-date inputs that the rest of the lookback logic reads.
  // ---------------------------------------------------------------------

  var calState = {
    minStr: null,   // earliest selectable date, "YYYY-MM-DD" (ET, 30 days back)
    maxStr: null,   // latest selectable date, "YYYY-MM-DD" (ET, today)
    viewYear: null,
    viewMonth: null, // 1-12
    startStr: null,
    endStr: null
  };

  var MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
  ];
  var WEEKDAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

  function etTodayParts() {
    var dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: ET_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    });
    var obj = partsToObject(dtf.formatToParts(new Date()));
    return { year: parseInt(obj.year, 10), month: parseInt(obj.month, 10), day: parseInt(obj.day, 10) };
  }

  function dateStrFromParts(y, m, d) {
    return y + "-" + pad2(m) + "-" + pad2(d);
  }

  function addDaysToDateStr(dateStr, deltaDays) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
    var d = new Date(Date.UTC(parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10)));
    d.setUTCDate(d.getUTCDate() + deltaDays);
    return dateStrFromParts(d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate());
  }

  function monthKey(year, month) {
    return year * 12 + (month - 1);
  }

  function initLookbackBounds() {
    var today = etTodayParts();
    calState.maxStr = dateStrFromParts(today.year, today.month, today.day);
    calState.minStr = addDaysToDateStr(calState.maxStr, -30);
    calState.viewYear = today.year;
    calState.viewMonth = today.month;
    renderCalendar();
  }

  function updateSelectionText() {
    if (!calState.startStr) {
      calendarSelectionText.textContent = "Pick a start date, then an end date.";
    } else if (!calState.endStr) {
      calendarSelectionText.textContent = "From " + calState.startStr + " — pick an end date.";
    } else {
      calendarSelectionText.textContent = calState.startStr + " → " + calState.endStr;
    }
  }

  function handleCalendarDayClick(dateStr) {
    if (!calState.startStr || calState.endStr) {
      calState.startStr = dateStr;
      calState.endStr = null;
    } else if (dateStr < calState.startStr) {
      calState.endStr = calState.startStr;
      calState.startStr = dateStr;
    } else {
      calState.endStr = dateStr;
    }
    lookbackFromDate.value = calState.startStr || "";
    lookbackToDate.value = calState.endStr || "";
    updateSelectionText();
    renderCalendar();
  }

  function renderCalendar() {
    var year = calState.viewYear;
    var month = calState.viewMonth; // 1-12
    calendarMonthLabel.textContent = MONTH_NAMES[month - 1] + " " + year;

    var firstWeekday = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
    var daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();

    calendarGrid.innerHTML = "";
    for (var i = 0; i < firstWeekday; i++) {
      var empty = document.createElement("span");
      empty.className = "calendar-day calendar-day--empty";
      calendarGrid.appendChild(empty);
    }

    for (var d = 1; d <= daysInMonth; d++) {
      var dStr = dateStrFromParts(year, month, d);
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "calendar-day";
      btn.textContent = String(d);

      var disabled = dStr < calState.minStr || dStr > calState.maxStr;
      if (disabled) btn.disabled = true;

      if (dStr === calState.maxStr) btn.classList.add("calendar-day--today");
      if (calState.startStr && calState.startStr <= dStr && calState.endStr && dStr <= calState.endStr && dStr !== calState.startStr && dStr !== calState.endStr) {
        btn.classList.add("calendar-day--in-range");
      }
      if (dStr === calState.startStr) btn.classList.add("calendar-day--start");
      if (dStr === calState.endStr) btn.classList.add("calendar-day--end");

      var dateObj = new Date(Date.UTC(year, month - 1, d));
      btn.setAttribute("aria-label", WEEKDAY_NAMES[dateObj.getUTCDay()] + ", " + MONTH_NAMES[month - 1] + " " + d + ", " + year);
      if (dStr === calState.startStr || dStr === calState.endStr) btn.setAttribute("aria-pressed", "true");

      if (!disabled) {
        btn.addEventListener("click", (function (clickedStr) {
          return function () { handleCalendarDayClick(clickedStr); };
        })(dStr));
      }

      calendarGrid.appendChild(btn);
    }

    calendarPrevBtn.disabled = monthKey(year, month) <= monthKey(
      parseInt(calState.minStr.slice(0, 4), 10), parseInt(calState.minStr.slice(5, 7), 10)
    );
    calendarNextBtn.disabled = monthKey(year, month) >= monthKey(
      parseInt(calState.maxStr.slice(0, 4), 10), parseInt(calState.maxStr.slice(5, 7), 10)
    );
  }

  function navigateCalendar(deltaMonths) {
    var m = calState.viewMonth - 1 + deltaMonths; // 0-based
    calState.viewYear += Math.floor(m / 12);
    calState.viewMonth = (((m % 12) + 12) % 12) + 1;
    renderCalendar();
  }

  function resetCalendarSelection() {
    calState.startStr = null;
    calState.endStr = null;
    var today = etTodayParts();
    calState.viewYear = today.year;
    calState.viewMonth = today.month;
    updateSelectionText();
    renderCalendar();
  }

  calendarPrevBtn.addEventListener("click", function () { navigateCalendar(-1); });
  calendarNextBtn.addEventListener("click", function () { navigateCalendar(1); });

  function showLookbackError(msg) {
    lookbackError.textContent = msg;
    lookbackError.hidden = !msg;
  }

  function handleLookbackApply() {
    var fromParts = combineDateAndTimeValue(lookbackFromDate.value, lookbackFromTime.value);
    var toParts = combineDateAndTimeValue(lookbackToDate.value, lookbackToTime.value);
    if (!fromParts || !toParts) {
      showLookbackError("Pick both a From and a To date/time.");
      return;
    }
    var fromUtc = etComponentsToUtcDate(fromParts.year, fromParts.month, fromParts.day, fromParts.hour, fromParts.minute);
    var toUtc = etComponentsToUtcDate(toParts.year, toParts.month, toParts.day, toParts.hour, toParts.minute);
    if (fromUtc.getTime() >= toUtc.getTime()) {
      showLookbackError("From must be earlier than To.");
      return;
    }
    var thirtyDaysAgoMs = Date.now() - 30 * 24 * 60 * 60 * 1000;
    if (fromUtc.getTime() < thirtyDaysAgoMs) {
      showLookbackError("Range must be within the last 30 days.");
      return;
    }
    showLookbackError("");
    stopLiveRefresh();
    loadArchiveRange(fromUtc, toUtc);
    lookbackDetails.open = false;
  }

  function handleLookbackReset() {
    lookbackFromDate.value = "";
    lookbackFromTime.value = "";
    lookbackToDate.value = "";
    lookbackToTime.value = "";
    resetCalendarSelection();
    showLookbackError("");
    loadLive();
    startLiveRefresh();
    lookbackDetails.open = false;
  }

  function startLiveRefresh() {
    stopLiveRefresh();
    liveRefreshTimer = setInterval(function () {
      if (mode === "live") loadLive();
    }, LIVE_REFRESH_MS);
  }

  function stopLiveRefresh() {
    if (liveRefreshTimer) {
      clearInterval(liveRefreshTimer);
      liveRefreshTimer = null;
    }
  }

  // ---------------------------------------------------------------------
  // Nearby traffic cameras (NYC DOT, static reference data — see
  // data/nyc_cameras.json). Loaded once at startup; the per-pin lookup below
  // is a local distance calc with no network call, so opening the detail
  // panel never blocks on a fetch. Only each camera's <img> thumbnail hits
  // the network, lazily, once rendered.
  // ---------------------------------------------------------------------

  function loadCameras() {
    return fetch("data/nyc_cameras.json", { cache: "force-cache" })
      .then(function (res) {
        if (!res.ok) throw new Error("nyc_cameras.json HTTP " + res.status);
        return res.json();
      })
      .then(function (cameras) {
        nycCameras = Array.isArray(cameras) ? cameras : [];
        renderCameraLayer();
      })
      .catch(function (err) {
        nycCameras = []; // feature degrades silently: no cameras shown
        console.error(err);
      });
  }

  // ---------------------------------------------------------------------
  // Webcams map layer (standalone toggle, separate from the detail-panel
  // nearby-camera feature above): plots every nyc_cameras.json point as a
  // muted dot layer, below post pins in z-order. Not tied to post
  // geocoding at all -- static reference data, shown or hidden purely by
  // the Webcams toggle, at any zoom.
  // ---------------------------------------------------------------------

  function camerasToGeoJSON(cameras) {
    var features = [];
    for (var i = 0; i < cameras.length; i++) {
      var c = cameras[i];
      features.push({
        type: "Feature",
        properties: { id: c.id, name: c.name || "Traffic camera", area: c.area || "" },
        geometry: { type: "Point", coordinates: [c.lng, c.lat] }
      });
    }
    return { type: "FeatureCollection", features: features };
  }

  // Populates the "cameras" source once both the map style has loaded and
  // the camera reference data has arrived -- whichever finishes second
  // calls this; it's a no-op if either isn't ready yet.
  function renderCameraLayer() {
    if (!map || !map.getSource("cameras")) return;
    map.getSource("cameras").setData(camerasToGeoJSON(nycCameras));
  }

  var cameraPopup = null;

  function openCameraPopup(feature) {
    var p = feature.properties;
    var imgUrl = cameraImageUrl(p.id);
    var html =
      '<div class="camera-popup">' +
      '<div class="camera-popup-name">' + escapeHtml(p.name) + (p.area ? " · " + escapeHtml(p.area) : "") + "</div>" +
      '<a href="' + escapeHtml(imgUrl) + '" target="_blank" rel="noopener noreferrer">' +
      '<img class="camera-popup-img" src="' + escapeHtml(imgUrl) + '" alt="' + escapeHtml(p.name) + '" />' +
      "</a>" +
      '<a class="camera-popup-link" href="' + escapeHtml(imgUrl) + '" target="_blank" rel="noopener noreferrer">Open full image</a>' +
      "</div>";
    if (cameraPopup) cameraPopup.remove();
    cameraPopup = new maplibregl.Popup({ className: "camera-popup", closeButton: true, closeOnClick: true, maxWidth: "200px", offset: 10 })
      .setLngLat(feature.geometry.coordinates)
      .setHTML(html)
      .addTo(map);
  }

  // Haversine distance in meters between two lat/lng points.
  function distanceMeters(lat1, lng1, lat2, lng2) {
    var R = 6371000;
    var toRad = Math.PI / 180;
    var dLat = (lat2 - lat1) * toRad;
    var dLng = (lng2 - lng1) * toRad;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1 * toRad) * Math.cos(lat2 * toRad) *
      Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  // Returns up to CAMERA_MAX_COUNT cameras within CAMERA_RADIUS_M of
  // (lat, lng), nearest first. Pure local computation against the static
  // camera list — no network access.
  function findNearbyCameras(lat, lng) {
    var withDistance = [];
    for (var i = 0; i < nycCameras.length; i++) {
      var cam = nycCameras[i];
      var d = distanceMeters(lat, lng, cam.lat, cam.lng);
      if (d <= CAMERA_RADIUS_M) {
        withDistance.push({ camera: cam, distance: d });
      }
    }
    withDistance.sort(function (a, b) { return a.distance - b.distance; });
    return withDistance.slice(0, CAMERA_MAX_COUNT);
  }

  function cameraImageUrl(id) {
    return "https://webcams.nyctmc.org/api/cameras/" + id + "/image";
  }

  // Renders the nearby-cameras block for the detail panel, or "" when none
  // are in range (matches the rest of the panel's convention of simply
  // omitting optional/absent fields rather than showing an empty state).
  function renderNearbyCameras(lat, lng) {
    var nearby = findNearbyCameras(lat, lng);
    if (nearby.length === 0) return "";

    var items = nearby.map(function (entry) {
      var cam = entry.camera;
      var imgUrl = cameraImageUrl(cam.id);
      var distLabel = "~" + (Math.round(entry.distance / 10) * 10) + "m away";
      var label = cam.name ? escapeHtml(cam.name) + " · " + distLabel : distLabel;
      return (
        '<a class="camera-thumb" href="' + escapeHtml(imgUrl) + '" target="_blank" rel="noopener noreferrer">' +
        '<img class="camera-thumb-img" src="' + escapeHtml(imgUrl) + '" data-camera-id="' + escapeHtml(String(cam.id)) + '" alt="' + escapeHtml(cam.name || "Traffic camera") + '" loading="lazy" onerror="this.classList.add(\'camera-thumb-broken\')" />' +
        '<span class="camera-thumb-label">' + label + "</span>" +
        "</a>"
      );
    }).join("");

    return (
      '<div class="detail-cameras">' +
      '<div class="detail-cameras-title">Nearby traffic cameras</div>' +
      '<div class="detail-cameras-list">' + items + "</div>" +
      "</div>"
    );
  }

  // Re-fetches each open camera thumbnail's snapshot on a fixed tick, so
  // the images stay live-ish while the detail panel is open. The DOT
  // endpoint returns the same URL for every frame, so a plain src
  // reassignment would hit cache and never actually refresh -- a
  // cache-busting query param forces a real re-fetch each tick. Only
  // touches the <img> src (not innerHTML), so it doesn't disturb layout,
  // scroll position, or any other part of the open panel.
  function startCameraRefresh() {
    stopCameraRefresh();
    var imgs = detailContent.querySelectorAll(".camera-thumb-img[data-camera-id]");
    if (imgs.length === 0) return;
    cameraRefreshTimer = setInterval(function () {
      imgs.forEach(function (img) {
        img.classList.remove("camera-thumb-broken");
        img.src = cameraImageUrl(img.getAttribute("data-camera-id")) + "?t=" + Date.now();
      });
    }, CAMERA_REFRESH_MS);
  }

  function stopCameraRefresh() {
    if (cameraRefreshTimer !== null) {
      clearInterval(cameraRefreshTimer);
      cameraRefreshTimer = null;
    }
  }

  // ---------------------------------------------------------------------
  // Detail panel
  // ---------------------------------------------------------------------

  function categoryLabel(category) {
    switch (category) {
      case "address": return "Address";
      case "city": return "City / neighborhood";
      case "flagged": return "Low confidence";
      default: return "State / other";
    }
  }

  // Precision badges show "<categoryLabel> · <precision_level>" (e.g.
  // "Address · address (clarified)"). For some category/precision_level
  // pairs the two halves say the same thing in different casing ("Address"
  // / "address", "City / neighborhood" / "city") and showing both reads as
  // a forgotten template variable. For others, precision_level carries real
  // information categoryLabel doesn't ("address (clarified)" vs "Address";
  // or for the catch-all "region" bucket — categoryLabel "State / other" —
  // precision_level pins down which of state / country / unclassified it
  // actually is). This decides, per pin, whether the precision_level half
  // is worth showing.
  function precisionAddsInfo(category, label, precisionLevel) {
    var lvl = (precisionLevel || "").trim();
    if (!lvl) return false;
    var lvlLower = lvl.toLowerCase();
    var labelLower = label.toLowerCase();

    // Split into a leading alphabetic core ("address") and any trailing
    // qualifier (" (clarified)"). A non-empty qualifier is always real,
    // additional information worth keeping.
    var m = lvlLower.match(/^([a-z]+)(.*)$/);
    var core = m ? m[1] : lvlLower;
    var qualifier = m ? m[2].trim() : "";
    if (qualifier) return true;

    // "address" and "city" categories map to precision_level 1:1 (the only
    // precision_level categorize() ever pairs with them is the bare word
    // itself), so once there's no qualifier, showing it adds nothing
    // beyond what categoryLabel already said. Every other category
    // ("flagged", or the state/country/unclassified catch-all) can pair
    // with several different precision_level values, so the specific word
    // is genuinely informative there.
    if (category !== "address" && category !== "city") return true;

    var wordBoundary = new RegExp("\\b" + core + "\\b");
    return !wordBoundary.test(labelLower);
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function openPanel(feature) {
    var p = feature.properties;
    selectedUri = p.post_uri;
    updateSelectedSource(feature);

    var pct = Math.round((typeof p.confidence === "number" ? p.confidence : 0) * 100);
    var badgeClass = "badge-" + p.category;
    var coords = feature.geometry && feature.geometry.coordinates;
    var camerasHtml = coords ? renderNearbyCameras(coords[1], coords[0]) : "";
    var precisionLabel = categoryLabel(p.category);
    var precisionBadge = escapeHtml(precisionLabel) +
      (precisionAddsInfo(p.category, precisionLabel, p.precision_level) ? " · " + escapeHtml(p.precision_level) : "");

    detailContent.innerHTML =
      '<h2 id="detail-panel-title" class="detail-handle">' + escapeHtml(p.handle || "unknown") + "</h2>" +
      '<p class="detail-timeago">' + escapeHtml(timeAgo(p.created_at)) + " · " + escapeHtml(formatEt(new Date(p.created_at))) + "</p>" +
      '<p class="detail-text">' + escapeHtml(p.text || "") + "</p>" +
      '<dl class="detail-meta">' +
      "<dt>Location</dt><dd>" + escapeHtml(p.mapped_location || p.location_text || "Unknown") + "</dd>" +
      '<dt>Precision</dt><dd><span class="detail-badge ' + badgeClass + '">' + precisionBadge + "</span></dd>" +
      "<dt>Confidence</dt><dd>" + pct + "%</dd>" +
      "</dl>" +
      (typeof p.source === "string" && /^https:\/\//i.test(p.source)
        ? '<a class="detail-link" href="' + escapeHtml(p.source) + '" target="_blank" rel="noopener noreferrer">View on Bluesky</a>'
        : "") +
      camerasHtml;

    detailPanel.setAttribute("aria-hidden", "false");
    startCameraRefresh();
  }

  function truncateSnippet(text, maxLen) {
    var t = text || "";
    return t.length > maxLen ? t.slice(0, maxLen).trim() + "…" : t;
  }

  // Shown instead of openPanel() when a click resolves more than one
  // distinct post at the same coordinate (see handlePinClick). Reuses the
  // State Mentions sidebar's list-item markup/classes as-is -- same shape
  // of data (handle, snippet, time), no new CSS needed. Picking an entry
  // opens the normal single-post detail view in its place.
  function openLocationPicker(features) {
    selectedUri = null;
    updateSelectedSource(null);
    stopCameraRefresh();

    var items = features.map(function (feature) {
      var p = feature.properties;
      return (
        '<button type="button" class="state-mention-item">' +
        '<span class="state-mention-handle">' + escapeHtml(p.handle || "unknown") + "</span>" +
        '<span class="state-mention-snippet">' + escapeHtml(truncateSnippet(p.text, 80)) + "</span>" +
        '<span class="state-mention-time">' + escapeHtml(timeAgo(p.created_at)) + "</span>" +
        "</button>"
      );
    }).join("");

    detailContent.innerHTML =
      '<h2 id="detail-panel-title" class="detail-handle">' + features.length + " posts at this location</h2>" +
      '<div class="state-panel-list">' + items + "</div>";

    var buttons = detailContent.querySelectorAll(".state-mention-item");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener("click", (function (feature) {
        return function () { openPanel(feature); };
      })(features[i]));
    }

    detailPanel.setAttribute("aria-hidden", "false");
  }

  // Renders the left-side "State mentions" sidebar list. Clicking an entry
  // opens the same right-side detail panel used for map pins.
  function renderStateMentions(features) {
    statePanelCount.textContent = "(" + features.length + ")";
    statePanelEmpty.hidden = features.length !== 0;
    statePanelList.innerHTML = "";
    for (var i = 0; i < features.length; i++) {
      var feature = features[i];
      var p = feature.properties;
      var item = document.createElement("button");
      item.type = "button";
      item.className = "state-mention-item";
      item.setAttribute("aria-label", (p.handle || "unknown") + ": " + truncateSnippet(p.text, 60));
      item.innerHTML =
        '<span class="state-mention-handle">' + escapeHtml(p.handle || "unknown") + "</span>" +
        '<span class="state-mention-snippet">' + escapeHtml(truncateSnippet(p.text, 80)) + "</span>" +
        '<span class="state-mention-time">' + escapeHtml(timeAgo(p.created_at)) + "</span>";
      item.addEventListener("click", (function (f) {
        return function () { openPanel(f); };
      })(feature));
      statePanelList.appendChild(item);
    }
  }

  function clearSelection() {
    selectedUri = null;
    detailPanel.setAttribute("aria-hidden", "true");
    updateSelectedSource(null);
    stopCameraRefresh();
  }

  function updateSelectedSource(feature) {
    var source = map.getSource("selected-pin");
    if (!source) return;
    source.setData({
      type: "FeatureCollection",
      features: feature ? [feature] : []
    });
  }

  // ---------------------------------------------------------------------
  // Map setup
  // ---------------------------------------------------------------------

  // Recency gradient: warm white (just posted) -> yellow -> orange -> red
  // (12h+), clamped at the red stop for anything older.
  var AGE_COLOR_EXPR = [
    "interpolate", ["linear"], ["get", "ageMinutes"],
    0, "#fdf6e3",
    15, "#f7d154",
    120, "#f2924a",
    720, "#d94f4f"
  ];

  // ---------------------------------------------------------------------
  // Category + camera icon glyphs. Drawn as small vector pictograms on an
  // offscreen canvas rather than relying on emoji/font glyph support (not
  // guaranteed across browsers/headless renderers), then registered with
  // MapLibre as images for symbol layers.
  //
  // The three category glyphs (protest/crime/police) ARE part of the
  // age-gradient color system: they're registered as SDF ("sdf: true")
  // images, which MapLibre recolors as a flat tint from the image's alpha
  // channel at paint time via icon-color/icon-halo-* -- the RGB baked in
  // here is discarded, only alpha (shape coverage) matters. So each glyph
  // is drawn as a single opaque silhouette (black) on a transparent
  // background, with no color or backing-circle logic baked into the
  // pixels; the pin-icons layer's paint block now renders the glyph itself
  // fixed white (icon-color) and drives AGE_COLOR_EXPR through
  // icon-halo-color instead, as a colored glow around the glyph.
  // ---------------------------------------------------------------------

  // A real SDF halo needs distance information to extend into (see
  // toSignedDistanceField below for the full story -- short version:
  // MapLibre's SDF shader reads a fixed ~6 source-bitmap-px band around the
  // glyph's edge as the halo's distance gradient). The draw*Icon functions
  // below were tuned to fill most of a 28px canvas with almost no margin,
  // which was fine for the old 1.1px contrast outline but leaves no room
  // for that band. Register glyphs on a larger canvas with the same glyph
  // drawn at its original 28px scale, centered with real padding on all
  // sides, so there's bitmap space for the halo to actually fade into.
  // icon-size (0.85, set where pin-icons is defined below) is unchanged
  // from before this padding was added -- see the comment there for why
  // that still renders the glyph at its original on-map pixel size.
  var ICON_GLYPH_PX = 28; // the scale draw*Icon's internal fractions (s * 0.x) are tuned at
  var ICON_CANVAS_PX = 48; // registered bitmap size: glyph + padding for the halo to bleed into
  var ICON_PAD_PX = (ICON_CANVAS_PX - ICON_GLYPH_PX) / 2;

  function withIconPadding(draw) {
    return function (ctx, canvasSize) {
      ctx.clearRect(0, 0, canvasSize, canvasSize);
      ctx.save();
      ctx.translate(ICON_PAD_PX, ICON_PAD_PX);
      draw(ctx, ICON_GLYPH_PX);
      ctx.restore();
    };
  }

  function roundRectPath(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function makeIconImageData(size, draw) {
    var canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    var ctx = canvas.getContext("2d");
    draw(ctx, size);
    return ctx.getImageData(0, 0, size, size);
  }

  // MapLibre's SDF icon/text shader (confirmed by reading the shipped
  // shader source, symbolSDF) does NOT generate a distance field from a
  // plain alpha mask on its own -- for `sdf: true` images it samples the
  // image's alpha channel directly as an already-computed signed distance,
  // calibrated to a fixed convention: the shape boundary sits at alpha
  // byte 192 (0.75 = (256-64)/256), and the full 0..255 alpha range spans
  // exactly SDF_PX (8) source-bitmap pixels of distance either side (a
  // `#define SDF_PX 8.0` constant baked into the shader, not configurable
  // per image). A plain binary 0/255 mask -- which is what
  // makeIconImageData above produces -- has none of that gradient, so any
  // icon-halo-width/-blur wide enough to be visible saturates instantly:
  // the whole "outside the glyph" region reads as the same flat distance
  // value, and the halo floods out to the bitmap's own padding edge rather
  // than fading into a soft glow (this is exactly what a hard-edged
  // clipped-square halo turned out to be, found while tuning icon-halo-*
  // below). This converts a drawn glyph's binary alpha mask into a real
  // distance field matching that exact calibration, via a brute-force
  // nearest-opposite-pixel search bounded to SDF_SEARCH_RADIUS (plenty,
  // since the shader clamps anything past 6px outside / 2px inside to
  // 0/255 anyway) -- icons are tiny (tens of px) so this is cheap and only
  // runs once at startup per glyph.
  var SDF_EDGE_BYTE = 192; // 0.75 * 256 -- MapLibre's fixed glyph-boundary alpha threshold
  var SDF_PX = 8; // MapLibre's fixed alpha-to-distance scale, in source bitmap px
  var SDF_SEARCH_RADIUS = 9; // bitmap px; true distances beyond this always clamp to 0/255 anyway

  function toSignedDistanceField(imageData, size) {
    var srcAlpha = imageData.data;
    var inside = new Uint8Array(size * size);
    for (var i = 0; i < size * size; i++) inside[i] = srcAlpha[i * 4 + 3] >= 128 ? 1 : 0;
    var out = new Uint8ClampedArray(size * size);
    for (var y = 0; y < size; y++) {
      var minY = Math.max(0, y - SDF_SEARCH_RADIUS);
      var maxY = Math.min(size - 1, y + SDF_SEARCH_RADIUS);
      for (var x = 0; x < size; x++) {
        var idx = y * size + x;
        var self = inside[idx];
        var minX = Math.max(0, x - SDF_SEARCH_RADIUS);
        var maxX = Math.min(size - 1, x + SDF_SEARCH_RADIUS);
        var bestSq = Infinity;
        for (var ny = minY; ny <= maxY; ny++) {
          var rowOffset = ny * size;
          for (var nx = minX; nx <= maxX; nx++) {
            if (inside[rowOffset + nx] !== self) {
              var dy = ny - y, dx = nx - x;
              var dSq = dx * dx + dy * dy;
              if (dSq < bestSq) bestSq = dSq;
            }
          }
        }
        var dist = bestSq === Infinity ? SDF_SEARCH_RADIUS : Math.sqrt(bestSq);
        var signedDist = self ? dist : -dist;
        out[idx] = Math.round(SDF_EDGE_BYTE + signedDist * (256 / SDF_PX));
      }
    }
    for (var j = 0; j < size * size; j++) {
      var o = j * 4;
      imageData.data[o] = 255;
      imageData.data[o + 1] = 255;
      imageData.data[o + 2] = 255;
      imageData.data[o + 3] = out[j];
    }
    return imageData;
  }

  function makeSdfIconImageData(size, draw) {
    return toSignedDistanceField(makeIconImageData(size, draw), size);
  }

  // The earlier vector fist drawing read as an unrecognizable white blob at
  // actual pin size (reported live) -- a bold letter glyph is legible at
  // that scale, so Protests uses a plain "P" instead. Drawn as a flat
  // black silhouette on transparent, per the SDF convention above.
  function drawLetterIcon(letter) {
    return function (ctx, s) {
      ctx.clearRect(0, 0, s, s);
      ctx.fillStyle = "#000000";
      ctx.font = "700 " + Math.round(s * 0.56) + "px -apple-system, BlinkMacSystemFont, Helvetica, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      // Baseline-based vertical centering reads slightly high for caps;
      // nudge down a touch so the glyph sits visually centered.
      ctx.fillText(letter, s / 2, s / 2 + s * 0.04);
    };
  }

  var drawFistIcon = drawLetterIcon("P");

  function drawExclamationIcon(ctx, s) {
    ctx.clearRect(0, 0, s, s);
    var cx = s / 2;
    ctx.fillStyle = "#000000";
    var barW = s * 0.14;
    roundRectPath(ctx, cx - barW / 2, s * 0.20, barW, s * 0.38, barW / 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(cx, s * 0.72, barW * 0.62, 0, Math.PI * 2);
    ctx.fill();
  }

  function drawBadgeIcon(ctx, s) {
    ctx.clearRect(0, 0, s, s);
    var cx = s / 2;
    ctx.fillStyle = "#000000";
    ctx.beginPath();
    ctx.moveTo(cx - s * 0.22, s * 0.24);
    ctx.lineTo(cx + s * 0.22, s * 0.24);
    ctx.quadraticCurveTo(cx + s * 0.26, s * 0.45, cx + s * 0.18, s * 0.62);
    ctx.quadraticCurveTo(cx + s * 0.08, s * 0.76, cx, s * 0.80);
    ctx.quadraticCurveTo(cx - s * 0.08, s * 0.76, cx - s * 0.18, s * 0.62);
    ctx.quadraticCurveTo(cx - s * 0.26, s * 0.45, cx - s * 0.22, s * 0.24);
    ctx.closePath();
    ctx.fill();
    // Punch a transparent hole for the badge's center emblem instead of a
    // baked-in dark circle -- an SDF image is recolored as one flat tint,
    // so a two-tone cutout has to be an actual hole in the alpha channel,
    // not a second fill color.
    ctx.globalCompositeOperation = "destination-out";
    ctx.beginPath();
    ctx.arc(cx, s * 0.47, s * 0.065, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalCompositeOperation = "source-over";
  }

  // Small, muted, plain camera glyph -- deliberately not part of the
  // age-gradient color system, so it reads as reference data, not a post.
  function drawCameraGlyph(ctx, s) {
    ctx.clearRect(0, 0, s, s);
    ctx.fillStyle = "rgba(154, 163, 181, 0.95)";
    roundRectPath(ctx, s * 0.16, s * 0.34, s * 0.68, s * 0.42, s * 0.06);
    ctx.fill();
    roundRectPath(ctx, s * 0.38, s * 0.22, s * 0.26, s * 0.14, s * 0.03);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(s * 0.5, s * 0.55, s * 0.17, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(11, 14, 20, 0.9)";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(s * 0.5, s * 0.55, s * 0.10, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(154, 163, 181, 0.95)";
    ctx.fill();
  }

  function initMap() {
    map = new maplibregl.Map({
      container: "map",
      style: "https://tiles.openfreemap.org/styles/dark",
      center: [-74.00, 40.71],
      zoom: 11
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

    map.on("load", function () {
      // Register category + camera icon images before anything references
      // them by name.
      map.addImage("icon-protest", makeSdfIconImageData(ICON_CANVAS_PX, withIconPadding(drawFistIcon)), { sdf: true });
      map.addImage("icon-crime", makeSdfIconImageData(ICON_CANVAS_PX, withIconPadding(drawExclamationIcon)), { sdf: true });
      map.addImage("icon-police", makeSdfIconImageData(ICON_CANVAS_PX, withIconPadding(drawBadgeIcon)), { sdf: true });
      map.addImage("camera-glyph", makeIconImageData(24, drawCameraGlyph));

      // --- Webcams (standalone layer, separate from the detail-panel
      // nearby-camera feature). Added first so it renders BELOW post pins
      // in z-order. No minzoom gate -- the Webcams toggle is meant to show
      // every camera on the map at once ("a map of every webcam").
      map.addSource("cameras", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] }
      });

      map.addLayer({
        id: "camera-dots",
        type: "symbol",
        source: "cameras",
        layout: {
          "icon-image": "camera-glyph",
          "icon-size": 0.55,
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "visibility": "none" // Webcams toggle starts off
        }
      });

      // Radius-filter circle. Added before "pins" so it renders below the
      // pin layers in z-order (translucent fill + visible border, pins
      // stay legible on top). Empty FeatureCollection until a circle
      // actually exists (see radiusCircleGeoJSON()).
      map.addSource("radius-circle", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] }
      });

      map.addLayer({
        id: "radius-circle-fill",
        type: "fill",
        source: "radius-circle",
        paint: {
          "fill-color": "#5b8cff",
          "fill-opacity": 0.12
        }
      });

      map.addLayer({
        id: "radius-circle-line",
        type: "line",
        source: "radius-circle",
        paint: {
          "line-color": "#5b8cff",
          "line-width": 2,
          "line-opacity": 0.85
        }
      });

      // Post pins. No clustering -- reported live as unwanted ("bird's eye
      // view bubbles"); removed entirely rather than just visually
      // suppressed, so there's no leftover cluster config to reintroduce
      // the bubbles by accident later.
      map.addSource("pins", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] }
      });

      map.addSource("selected-pin", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] }
      });

      // Subtle always-on glow for unselected pins.
      map.addLayer({
        id: "pin-halo",
        type: "circle",
        source: "pins",
        paint: {
          "circle-color": AGE_COLOR_EXPR,
          "circle-radius": 14,
          "circle-blur": 1,
          "circle-opacity": 0.22
        }
      });

      map.addLayer({
        id: "pin-point",
        type: "circle",
        source: "pins",
        paint: {
          "circle-color": AGE_COLOR_EXPR,
          "circle-radius": 6,
          "circle-stroke-width": 1.5,
          "circle-stroke-color": "#0b0e14"
        }
      });

      // Category icons, layered on top of the age-colored dot. Priority
      // order Protests -> Crime -> Police Activity is already baked into
      // matchCategory (see classifyCategory), so this is a simple lookup.
      map.addLayer({
        id: "pin-icons",
        type: "symbol",
        source: "pins",
        filter: ["any",
          ["==", ["get", "matchCategory"], "protest"],
          ["==", ["get", "matchCategory"], "crime"],
          ["==", ["get", "matchCategory"], "police"]
        ],
        layout: {
          "icon-image": ["match", ["get", "matchCategory"],
            "protest", "icon-protest",
            "crime", "icon-crime",
            "police", "icon-police",
            "icon-protest"
          ],
          // The glyph occupies the same ICON_GLYPH_PX/ICON_CANVAS_PX
          // fraction of the (now-padded) bitmap regardless of icon-size,
          // so 0.85 -- originally tuned against the old unpadded 28px
          // bitmap, where the glyph WAS the whole canvas -- still renders
          // the glyph itself at the same on-map pixel size as before. What
          // changed is only the canvas has real margin around it now.
          "icon-size": 0.85,
          "icon-allow-overlap": true,
          "icon-ignore-placement": true
        },
        paint: {
          // Reverted from a fully age-colored glyph (reported live as
          // reading poorly -- "just a color blob", shape no longer
          // legible): the glyph itself is fixed white so it stays crisp
          // against the dark map tiles at every age, and AGE_COLOR_EXPR
          // instead drives a wide colored halo/glow around it -- same
          // gradient the dot and its own halo already use, just applied
          // to the icon's edge instead of its fill.
          // icon-halo-width has a hard architectural ceiling here: MapLibre's
          // SDF shader (symbolSDF) encodes distance over a fixed 8-texture-px
          // span per the full alpha range, so the maximum halo distance this
          // image format can represent before it saturates/floods is
          // ~6 bitmap px, i.e. ~6 * icon-size CSS px (icon-size is 0.85
          // above) -- about 5.1px. 4 stays safely inside that.
          // icon-halo-blur deliberately kept small: the halo's softness
          // already comes from the generated SDF's own ~6px distance ramp
          // (see toSignedDistanceField), and the shader mixes halo-blur in
          // as an ADDITIVE smoothstep half-width around the halo edge --
          // past about 0.5-0.6 here (worked out against halo-width 4 and
          // fontScale 0.85) that smoothstep's lower bound dips below zero
          // and leaks a faint uniform tint across the icon's ENTIRE padded
          // bitmap, reading as a flat colored square behind the glow
          // instead of a clean fade (found while tuning this value).
          "icon-color": "#ffffff",
          "icon-halo-color": AGE_COLOR_EXPR,
          "icon-halo-width": 4,
          "icon-halo-blur": 0.4
        }
      });

      // Stronger glow for the selected pin, drawn above everything else.
      map.addLayer({
        id: "selected-halo",
        type: "circle",
        source: "selected-pin",
        paint: {
          "circle-color": AGE_COLOR_EXPR,
          "circle-radius": 22,
          "circle-blur": 0.8,
          "circle-opacity": 0.55
        }
      });

      var PIN_INTERACTIVE_LAYERS = ["pin-point", "pin-icons", "camera-dots"];

      // Multiple distinct posts can resolve to the identical coordinate
      // (shared address, generic city-center point, etc. -- confirmed live
      // with real data: up to 33 posts stacked on one point). A plain
      // click only ever sees whichever one happens to be topmost at that
      // pixel, silently hiding the rest with no way to reach them. Bound
      // to pin-point only (every post has one, categorized or not, so it's
      // the superset of pin-icons) and queried explicitly rather than
      // trusting e.features[0], which returns every feature under that
      // pixel across the whole layer, not just the first hit.
      function handlePinClick(e) {
        // Draw mode takes priority: setting the circle's center is the
        // whole point of clicking the map right now, even if a pin happens
        // to be under the cursor. Same for a just-finished circle-move drag
        // (isMovingCircle) -- its trailing click shouldn't also act on
        // whatever happens to be under the cursor at the drop point.
        if (drawModeActive || isMovingCircle) return;
        var features = map.queryRenderedFeatures(e.point, { layers: ["pin-point"] });
        if (!features.length) return;

        var seenUris = {};
        var uniqueFeatures = [];
        for (var i = 0; i < features.length; i++) {
          var uri = features[i].properties.post_uri;
          if (!seenUris[uri]) {
            seenUris[uri] = true;
            uniqueFeatures.push(features[i]);
          }
        }

        var target = uniqueFeatures[0];
        // Slow, gentle ease rather than MapLibre's default (fast enough to
        // feel jarring/nauseating when jumping several zoom levels at once,
        // e.g. from a city-wide view straight to street level).
        map.easeTo({ center: target.geometry.coordinates, zoom: Math.max(map.getZoom(), 15), duration: 1400 });
        if (uniqueFeatures.length === 1) {
          openPanel(target);
        } else {
          openLocationPicker(uniqueFeatures);
        }
      }
      map.on("click", "pin-point", handlePinClick);

      map.on("click", "camera-dots", function (e) {
        if (drawModeActive || isMovingCircle) return;
        if (e.features && e.features[0]) openCameraPopup(e.features[0]);
      });

      // Click elsewhere on the map (not on any interactive layer above)
      // clears the open detail panel.
      map.on("click", function (e) {
        if (drawModeActive || isMovingCircle) return;
        var features = map.queryRenderedFeatures(e.point, { layers: PIN_INTERACTIVE_LAYERS });
        if (features.length === 0) clearSelection();
      });

      PIN_INTERACTIVE_LAYERS.forEach(function (layerId) {
        map.on("mouseenter", layerId, function () { if (!drawModeActive && !isMovingCircle) map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layerId, function () { if (!drawModeActive && !isMovingCircle) map.getCanvas().style.cursor = ""; });
      });

      // --- Geofence draw/move-circle interaction ---------------------------
      // Two distinct drag modes share the same mousedown/mousemove/mouseup
      // plumbing, kept mutually exclusive (drawModeActive always wins):
      //
      //   1. Draw a NEW circle (drawModeActive): mousedown sets the fixed
      //      center; mousemove live-updates the radius to the cursor's
      //      current real-world (haversine) distance from that center;
      //      mouseup finalizes.
      //   2. MOVE an existing circle (isMovingCircle): mousedown on the
      //      filled interior of an already-drawn circle (and not on top of
      //      a pin/camera dot, which keep their own click-to-open behavior)
      //      grabs it; mousemove translates radiusCenter by the same
      //      lng/lat delta the cursor has moved since the grab, keeping the
      //      grabbed point under the cursor rather than re-centering on it;
      //      mouseup finalizes. Radius stays fixed throughout.
      //
      // Both bound on the map generally rather than a specific layer -- the
      // center/interior can legally overlap a pin, and this interaction
      // takes priority over the normal pin-click handling above (guarded
      // via the drawModeActive / isMovingCircle checks there).
      map.on("mousedown", function (e) {
        if (drawModeActive) {
          e.preventDefault();
          isDrawingDrag = true;
          radiusCenter = [e.lngLat.lng, e.lngLat.lat];
          radiusMiles = RADIUS_MIN_MILES;
          if (radiusSlider) radiusSlider.value = String(radiusMiles);
          updateRadiusValueLabel();
          updateRadiusActiveUi();
          updateRadiusCircleLayer();
          applyFilters();
          return;
        }

        if (!radiusCenter) return;
        // A pin/camera dot sitting inside the circle keeps its own
        // click-to-open behavior -- only an empty patch of the circle's
        // interior starts a move.
        if (map.queryRenderedFeatures(e.point, { layers: PIN_INTERACTIVE_LAYERS }).length) return;
        if (!map.queryRenderedFeatures(e.point, { layers: ["radius-circle-fill"] }).length) return;
        e.preventDefault();
        isMovingCircle = true;
        moveDragStartLngLat = [e.lngLat.lng, e.lngLat.lat];
        moveDragStartCenter = radiusCenter.slice();
        map.dragPan.disable();
        map.getCanvas().style.cursor = "grabbing";
      });

      map.on("mousemove", function (e) {
        if (drawModeActive) {
          if (!isDrawingDrag || !radiusCenter) return;
          var dist = haversineMiles(radiusCenter[1], radiusCenter[0], e.lngLat.lat, e.lngLat.lng);
          radiusMiles = Math.min(RADIUS_MAX_MILES, Math.max(RADIUS_MIN_MILES, dist));
          // The slider mirrors radiusMiles for display/sync purposes only --
          // rounded to one decimal here just to avoid stuffing a long
          // floating-point string into the input's value attribute on
          // every tick. radiusMiles itself (driving the circle polygon and
          // passesRadius()) stays the full-precision value; see
          // formatMiles() for the separate, rounded-for-display label.
          if (radiusSlider) radiusSlider.value = String(Math.round(radiusMiles * 10) / 10);
          updateRadiusValueLabel();
          updateRadiusCircleLayer();
          applyFilters();
          return;
        }

        if (isMovingCircle) {
          radiusCenter = [
            moveDragStartCenter[0] + (e.lngLat.lng - moveDragStartLngLat[0]),
            moveDragStartCenter[1] + (e.lngLat.lat - moveDragStartLngLat[1])
          ];
          updateRadiusCircleLayer();
          applyFilters();
          return;
        }

        // Idle hover: a "grab" affordance over the circle's interior, so
        // the move-circle interaction is discoverable without instructions.
        // Skipped over a pin/camera dot -- those already set their own
        // "pointer" cursor via the mouseenter/mouseleave bindings above,
        // and this runs on every mousemove so it must not fight that.
        if (!radiusCenter) return;
        if (map.queryRenderedFeatures(e.point, { layers: PIN_INTERACTIVE_LAYERS }).length) return;
        var overCircle = map.queryRenderedFeatures(e.point, { layers: ["radius-circle-fill"] }).length > 0;
        map.getCanvas().style.cursor = overCircle ? "grab" : "";
      });

      function finishPointerDrag() {
        if (isDrawingDrag) {
          isDrawingDrag = false;
          applyFilters();
        }
        if (isMovingCircle) {
          map.dragPan.enable();
          map.getCanvas().style.cursor = "";
          applyFilters();
          // Deferred rather than cleared immediately: the trailing "click"
          // event for this same mousedown-drag-mouseup interaction fires
          // synchronously right after this handler, and the pin/camera/
          // clear-on-click handlers above need to still see isMovingCircle
          // as true to know to ignore that click (see their guards).
          setTimeout(function () { isMovingCircle = false; }, 0);
        }
      }
      map.on("mouseup", finishPointerDrag);
      // MapLibre's own "mouseup" only fires for releases over the canvas --
      // catch releases that end outside the map too, so a drag can't get
      // stuck "active" with no way to finalize it.
      document.addEventListener("mouseup", finishPointerDrag);

      renderCameraLayer(); // in case loadCameras() already resolved

      loadLive().then(startLiveRefresh);
    });
  }

  // ---------------------------------------------------------------------
  // Wire up controls
  // ---------------------------------------------------------------------

  searchForm.addEventListener("submit", function (e) {
    e.preventDefault();
    applyFilters();
  });
  searchInput.addEventListener("input", function () {
    applyFilters();
  });

  [toggleProtests, toggleCrime, togglePolice].forEach(function (el) {
    if (el) el.addEventListener("change", applyFilters);
  });

  if (toggleWebcams) {
    toggleWebcams.addEventListener("change", function () {
      if (!map || !map.getLayer("camera-dots")) return;
      map.setLayoutProperty("camera-dots", "visibility", toggleWebcams.checked ? "visible" : "none");
    });
  }

  detailClose.addEventListener("click", clearSelection);

  lookbackApply.addEventListener("click", handleLookbackApply);
  lookbackReset.addEventListener("click", handleLookbackReset);

  if (radiusDrawToggle) {
    radiusDrawToggle.addEventListener("click", function () {
      setDrawModeActive(!drawModeActive);
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && drawModeActive) setDrawModeActive(false);
  });

  if (radiusSlider) {
    radiusSlider.addEventListener("input", function () {
      // parseFloat (not parseInt): the slider's step is 0.1, matching the
      // continuous, unrounded radius the draw-circle drag now produces
      // (see the mousemove handler above) -- both paths should feel
      // equally smooth, not have the slider snap to whole miles while
      // dragging is continuous.
      var parsed = parseFloat(radiusSlider.value);
      radiusMiles = isNaN(parsed)
        ? RADIUS_DEFAULT_MILES
        : Math.min(RADIUS_MAX_MILES, Math.max(RADIUS_MIN_MILES, parsed));
      // Slider used before any circle was drawn: establish a default
      // center (current map center) rather than requiring draw-first.
      if (!radiusCenter && map) {
        var c = map.getCenter();
        radiusCenter = [c.lng, c.lat];
      }
      updateRadiusValueLabel();
      updateRadiusActiveUi();
      updateRadiusCircleLayer();
      applyFilters();
    });
  }

  if (radiusClearBtn) {
    radiusClearBtn.addEventListener("click", clearRadiusFilter);
  }

  if (liveWindowSlider) {
    liveWindowSlider.addEventListener("input", function () {
      var parsed = parseInt(liveWindowSlider.value, 10);
      liveWindowHours = isNaN(parsed)
        ? LIVE_WINDOW_MAX_HOURS
        : Math.min(LIVE_WINDOW_MAX_HOURS, Math.max(LIVE_WINDOW_MIN_HOURS, parsed));
      if (liveWindowValueEl) liveWindowValueEl.textContent = liveWindowHours + "h";
      if (mode === "live") {
        lookbackSummaryText.textContent = liveWindowLabel();
        applyFilters();
      }
    });
  }

  // Browsers throttle setInterval timers on backgrounded tabs, which can
  // stall the live poll. When the tab becomes visible again after actually
  // having been hidden (not on initial load), refetch immediately instead
  // of waiting for the next scheduled interval tick.
  var tabWasHidden = false;
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      tabWasHidden = true;
      return;
    }
    if (document.visibilityState === "visible" && tabWasHidden) {
      tabWasHidden = false;
      if (mode === "live") loadLive();
    }
  });

  // Everything that actually touches the network or spins up the map
  // (initMap() -> loadLive()'s fetch of data/live.geojson, plus
  // loadCameras()'s fetch of data/nyc_cameras.json) lives behind this
  // function, which only ever runs after the gate above is passed --
  // either immediately, if localStorage already remembers an unlock from
  // a previous visit, or from unlockGate() once the right password is
  // entered. initLookbackBounds() has no network call of its own, but is
  // grouped in here too since it's part of the same startup sequence and
  // there's no reason for it to run while the map/data behind it can't.
  function startApp() {
    initLookbackBounds();
    initMap();
    loadCameras();
  }

  if (isGateUnlocked()) {
    document.body.classList.add("gate-unlocked");
    startApp();
  } else if (gateInput) {
    gateInput.focus();
  }
})();
