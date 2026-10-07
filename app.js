/* Spice — frontend app
 * Static site, no build step. Loads data/live.geojson by default, or a
 * merged range of data/archive/YYYY-MM-DD.geojson files when a lookback
 * range is applied. Boolean keyword search filters whatever is currently
 * loaded. All displayed times are America/New_York (ET).
 */

(function () {
  "use strict";

  var ET_ZONE = "America/New_York";
  var LIVE_REFRESH_MS = 15 * 60 * 1000; // matches the pipeline's 15-min native cron cadence

  var map = null;
  var mode = "live"; // "live" | "archive"
  var allFeatures = []; // currently loaded map-pin feature set (post-lookback, pre-search)
  var allStateMentions = []; // currently loaded state/country-precision features (post-lookback, pre-search)
  var selectedUri = null;
  var liveRefreshTimer = null;

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
  var statePanelList = document.getElementById("state-panel-list");
  var statePanelCount = document.getElementById("state-panel-count");
  var statePanelEmpty = document.getElementById("state-panel-empty");

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
  // Boolean keyword search: AND / OR / NOT / "quoted phrases"
  // Grammar (left-to-right, AND/NOT bind tighter than OR):
  //   expr   := andExpr (OR andExpr)*
  //   andExpr:= notTerm (AND? notTerm)*   -- AND is implicit if omitted
  //   notTerm:= NOT? primary
  //   primary:= WORD | PHRASE
  // ---------------------------------------------------------------------

  function tokenizeQuery(raw) {
    var tokens = [];
    var re = /"([^"]*)"|(\S+)/g;
    var m;
    while ((m = re.exec(raw)) !== null) {
      if (m[1] !== undefined) {
        tokens.push({ type: "phrase", value: m[1] });
      } else {
        var word = m[2];
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
      if (t.type === "word" || t.type === "phrase") {
        next();
        return { kind: "term", value: t.value.toLowerCase() };
      }
      // Stray operator with nothing to bind to: skip it.
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

  function evalBooleanNode(node, lowerText) {
    if (!node) return true;
    switch (node.kind) {
      case "term":
        return lowerText.indexOf(node.value) !== -1;
      case "not":
        return !evalBooleanNode(node.child, lowerText);
      case "and":
        return evalBooleanNode(node.left, lowerText) && evalBooleanNode(node.right, lowerText);
      case "or":
        return evalBooleanNode(node.left, lowerText) || evalBooleanNode(node.right, lowerText);
      default:
        return true;
    }
  }

  function matchesSearch(feature, ast) {
    if (!ast) return true;
    var text = (feature.properties && feature.properties.text) || "";
    return evalBooleanNode(ast, text.toLowerCase());
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

  function currentAst() {
    var raw = searchInput.value.trim();
    if (!raw) return null;
    return parseBooleanQuery(raw);
  }

  function applyFilters() {
    var ast = currentAst();
    var filteredPins = ast ? allFeatures.filter(function (f) { return matchesSearch(f, ast); }) : allFeatures.slice();
    var filteredStateMentions = ast ? allStateMentions.filter(function (f) { return matchesSearch(f, ast); }) : allStateMentions.slice();
    clearSelection();
    var source = map.getSource("pins");
    if (source) {
      source.setData({ type: "FeatureCollection", features: filteredPins });
    }
    renderStateMentions(filteredStateMentions);
    setStatus(filteredPins.length + " pin" + (filteredPins.length === 1 ? "" : "s") + " shown");
  }

  // ---------------------------------------------------------------------
  // Data loading
  // ---------------------------------------------------------------------

  function loadLive() {
    mode = "live";
    lookbackSummaryText.textContent = "Live (last 48h)";
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

  function initLookbackBounds() {
    var now = new Date();
    var thirtyDaysAgo = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
    // date input min/max expect "YYYY-MM-DD" in the field's own (unzoned)
    // terms; we treat those terms as ET throughout the app.
    function toLocalDateValue(date) {
      var dtf = new Intl.DateTimeFormat("en-US", {
        timeZone: ET_ZONE,
        year: "numeric",
        month: "2-digit",
        day: "2-digit"
      });
      var obj = partsToObject(dtf.formatToParts(date));
      return obj.year + "-" + obj.month + "-" + obj.day;
    }
    var minVal = toLocalDateValue(thirtyDaysAgo);
    var maxVal = toLocalDateValue(now);
    lookbackFromDate.min = minVal;
    lookbackFromDate.max = maxVal;
    lookbackToDate.min = minVal;
    lookbackToDate.max = maxVal;
  }

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

    detailContent.innerHTML =
      '<h2 id="detail-panel-title" class="detail-handle">' + escapeHtml(p.handle || "unknown") + "</h2>" +
      '<p class="detail-timeago">' + escapeHtml(timeAgo(p.created_at)) + " · " + escapeHtml(formatEt(new Date(p.created_at))) + "</p>" +
      '<p class="detail-text">' + escapeHtml(p.text || "") + "</p>" +
      '<dl class="detail-meta">' +
      "<dt>Location</dt><dd>" + escapeHtml(p.mapped_location || p.location_text || "Unknown") + "</dd>" +
      '<dt>Precision</dt><dd><span class="detail-badge ' + badgeClass + '">' + escapeHtml(categoryLabel(p.category)) + " · " + escapeHtml(p.precision_level || "") + "</span></dd>" +
      "<dt>Confidence</dt><dd>" + pct + "%</dd>" +
      "</dl>" +
      (typeof p.source === "string" && /^https:\/\//i.test(p.source)
        ? '<a class="detail-link" href="' + escapeHtml(p.source) + '" target="_blank" rel="noopener noreferrer">View on Bluesky</a>'
        : "");

    detailPanel.setAttribute("aria-hidden", "false");
  }

  function truncateSnippet(text, maxLen) {
    var t = text || "";
    return t.length > maxLen ? t.slice(0, maxLen).trim() + "…" : t;
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

  function initMap() {
    map = new maplibregl.Map({
      container: "map",
      style: "https://tiles.openfreemap.org/styles/dark",
      center: [-74.00, 40.71],
      zoom: 11
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

    map.on("load", function () {
      map.addSource("pins", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] }
      });

      map.addSource("selected-pin", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] }
      });

      // Subtle always-on glow for unselected individual pins.
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

      map.on("click", "pin-point", function (e) {
        if (e.features && e.features[0]) {
          var feature = e.features[0];
          map.easeTo({ center: feature.geometry.coordinates, zoom: Math.max(map.getZoom(), 15) });
          openPanel(feature);
        }
      });

      map.on("click", function (e) {
        var features = map.queryRenderedFeatures(e.point, { layers: ["pin-point"] });
        if (features.length === 0) clearSelection();
      });

      ["pin-point"].forEach(function (layerId) {
        map.on("mouseenter", layerId, function () { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layerId, function () { map.getCanvas().style.cursor = ""; });
      });

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

  detailClose.addEventListener("click", clearSelection);

  lookbackApply.addEventListener("click", handleLookbackApply);
  lookbackReset.addEventListener("click", handleLookbackReset);

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

  initLookbackBounds();
  initMap();
})();
