/* Map tab: pick suburbs, draw no-go zones (areas, or one side of a road), see listings.
   Leaflet (vendored) + OpenStreetMap tiles. Suburb lookups go through our server, which
   caches them and keeps to OpenStreetMap's 1 request/second limit.
   Zone format and the "which side" rule match scraper/geo.py. */
(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  const App = () => window.App;
  const clone = o => JSON.parse(JSON.stringify(o));
  const cssVar = n => getComputedStyle(document.body).getPropertyValue(n).trim();

  let map = null, layers = {}, mode = "pan", draft = null;
  let zones = [], inc = [], exc = [], dirty = false;
  const outlines = {};           // suburb name (lower) -> place (only when found)
  const tried = {};              // suburb name (lower) -> [L.LatLngBounds searched without finding it]
  const covered = (n, b) => (tried[n] || []).some(t => t.contains(b));
  const pending = new Set();     // names queued for lookup
  let outlineQueue = Promise.resolve();

  // ------------------------------------------------------------------ geometry (mirrors geo.py)
  const M_LAT = 110540, M_LON = 111320;
  const proj = lat0 => { const kx = M_LON * Math.cos(lat0 * Math.PI / 180);
    return { xy: p => [p[1] * kx, p[0] * M_LAT], ll: q => [q[1] / M_LAT, q[0] / kx] }; };

  function sideOfRoad(p, road) {
    const pr = proj(p[0]), P = pr.xy(p), xy = road.map(pr.xy), last = xy.length - 2;
    let best = null;
    for (let i = 0; i <= last; i++) {
      const A = xy[i], B = xy[i + 1], dx = B[0] - A[0], dy = B[1] - A[1], L2 = dx * dx + dy * dy;
      if (L2 < 0.25) continue;
      let t = ((P[0] - A[0]) * dx + (P[1] - A[1]) * dy) / L2;
      t = Math.max(i === 0 ? -Infinity : 0, Math.min(i === last ? Infinity : 1, t));
      const d = Math.hypot(P[0] - (A[0] + t * dx), P[1] - (A[1] + t * dy));
      const cross = dx * (P[1] - A[1]) - dy * (P[0] - A[0]);
      if (!best || d < best.d - 1e-9) best = { d, cross };
    }
    return !best || best.cross > 0 ? "left" : "right";
  }

  /* A shaded polygon showing roughly what a road_side zone covers: the road extended past both
     ends, offset sideways. The real test (sideOfRoad) has no distance limit; this is a guide. */
  function sideShade(road, side, reach = 12000) {
    const pr = proj(road[0][0]);
    let xy = road.map(pr.xy);
    const unit = (a, b) => { const dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy) || 1; return [dx / l, dy / l]; };
    const u0 = unit(xy[0], xy[1]), u1 = unit(xy[xy.length - 2], xy[xy.length - 1]);
    xy = [[xy[0][0] - u0[0] * reach, xy[0][1] - u0[1] * reach], ...xy,
          [xy[xy.length - 1][0] + u1[0] * reach, xy[xy.length - 1][1] + u1[1] * reach]];
    const sgn = side === "left" ? 1 : -1;
    const normals = xy.map((_, i) => {
      const a = unit(xy[Math.max(0, i - 1)], xy[Math.max(1, i)]), b = unit(xy[Math.min(i, xy.length - 2)], xy[Math.min(i + 1, xy.length - 1)]);
      let nx = -(a[1] + b[1]), ny = a[0] + b[0]; const l = Math.hypot(nx, ny) || 1;
      return [sgn * nx / l, sgn * ny / l];
    });
    const far = xy.map((p, i) => [p[0] + normals[i][0] * reach, p[1] + normals[i][1] * reach]);
    return [...xy, ...far.reverse()].map(pr.ll);
  }

  // ------------------------------------------------------------------ setup
  function init() {
    map = L.map("map", { doubleClickZoom: false, zoomControl: true });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',
    }).addTo(map);
    layers.suburbs = L.layerGroup().addTo(map);
    layers.zones = L.layerGroup().addTo(map);
    layers.pins = L.layerGroup().addTo(map);
    layers.draft = L.layerGroup().addTo(map);
    map.on("click", onMapClick);
    map.on("moveend", () => loadOutlines());
  }

  function loadFromConfig() {
    const f = (App().getConfig() || {}).filters || {};
    zones = clone(f.no_go_zones || []);
    inc = [...(f.include_suburbs || [])];
    exc = [...(f.exclude_suburbs || [])];
    setDirty(false);
  }

  let fitted = false;
  async function show() {
    const first = !map;
    if (first) { init(); loadFromConfig(); }
    else if (!dirty) loadFromConfig();
    map.invalidateSize();
    renderZones(); renderChips(); renderOutlines();
    await renderPins();
    if (!fitted) {
      fitted = fitToContent();
      if (!fitted && !map._loaded) map.setView([-29.0, 24.5], 5);   // South Africa until there's something to show
      if (!fitted && inc.length) {      // no pins or zones yet: start at the first area you include
        try {
          const p = (await App().api(`/api/geo/suburb?anywhere=1&name=${encodeURIComponent(inc[0])}`)).place;
          if (p && !fitted) { outlines[key(inc[0])] = p; map.setView([p.lat, p.lon], 13); fitted = true; renderOutlines(); }
        } catch { /* stay on the country view */ }
      }
    }
    setMode(mode);
    loadOutlines();
  }

  function fitToContent() {
    const b = L.latLngBounds([]);
    zones.forEach(z => z.points.forEach(p => b.extend(p)));
    layers.pins.eachLayer(m => b.extend(m.getLatLng()));
    Object.values(outlines).forEach(p => p && b.extend([p.lat, p.lon]));
    if (!b.isValid()) return false;
    map.fitBounds(b.pad(0.15), { maxZoom: 14 });
    return true;
  }

  // ------------------------------------------------------------------ dirty / save
  function setDirty(v) {
    dirty = v;
    $("#mapSave").disabled = !v;
    $("#mapRevert").hidden = !v;
    if (v) { $("#mapMsg").className = "small muted"; $("#mapMsg").textContent = "Unsaved changes"; }
  }
  window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });

  async function save() {
    const cfg = clone(App().getConfig() || {});
    cfg.filters = Object.assign(cfg.filters || {}, { no_go_zones: zones, include_suburbs: inc, exclude_suburbs: exc });
    const msg = $("#mapMsg");
    try {
      await App().api("/api/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cfg) });
      await App().loadSettings();
      setDirty(false);
      msg.className = "small ok";
      msg.textContent = "Saved. Press Re-score to apply it to this run; the next scrape uses it too." +
        (App().sessionGoal() ? " (Your session goal's areas still apply until you save or discard it.)" : "");
    } catch (err) {
      msg.className = "small err";
      msg.textContent = ((err.body && err.body.errors) || [err.message]).join("; ");
    }
  }

  // ------------------------------------------------------------------ modes
  const HINTS = {
    pan: "Drag to move, scroll to zoom. Click a pin for details.",
    suburb: "Click anywhere in a suburb to include or avoid it.",
    area: "Click to place the corners of the area you never want to live in.",
    road: "Click along the road, start to end, to trace it (a few points is enough; follow bends).",
  };
  function setMode(m) {
    mode = m; draft = null; layers.draft.clearLayers();
    $$(".modes .chip").forEach(b => { const on = b.dataset.mode === m; b.classList.toggle("on", on); b.setAttribute("aria-checked", on); });
    $("#map").classList.toggle("drawing", m !== "pan");
    hint();
  }
  $$(".modes .chip").forEach(b => b.addEventListener("click", () => setMode(b.dataset.mode)));
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && map && draft && $("#tab-map").classList.contains("active")) { draft = null; layers.draft.clearLayers(); hint(); }
  });

  function hint(extraHtml) {
    const el = $("#mapHint");
    if (extraHtml != null) { el.innerHTML = extraHtml; return; }
    if (!draft) { el.innerHTML = HINTS[mode]; return; }
    const n = draft.points.length, need = mode === "area" ? 3 : 2;
    if (draft.stage === "side") {
      el.innerHTML = `<b>Now click on the side of the road you don't want.</b><div class="hintbtns"><button type="button" class="btn small ghost" data-act="back">Back</button><button type="button" class="btn small ghost" data-act="cancel">Cancel</button></div>`;
    } else if (draft.stage === "name") {
      const kind = mode === "area" ? "area" : `${draft.side} side of the road`;
      el.innerHTML = `<label class="small">Name this no-go zone (${kind})<input type="text" id="zoneName" value="${App().esc(draft.name)}"></label>
        <div class="hintbtns"><button type="button" class="btn small" data-act="add">Add zone</button>${mode === "road" ? '<button type="button" class="btn small ghost" data-act="flip">Other side</button>' : ""}<button type="button" class="btn small ghost" data-act="cancel">Cancel</button></div>`;
      const inp = $("#zoneName"); inp.focus(); inp.select();
      inp.addEventListener("keydown", e => { if (e.key === "Enter") finishZone(); });
    } else {
      el.innerHTML = `${HINTS[mode]}<div class="muted small">${n} point${n === 1 ? "" : "s"}${n < need ? ` (need ${need})` : ""}</div>
        <div class="hintbtns"><button type="button" class="btn small" data-act="done" ${n < need ? "disabled" : ""}>Done</button><button type="button" class="btn small ghost" data-act="undo" ${n ? "" : "disabled"}>Undo point</button><button type="button" class="btn small ghost" data-act="cancel">Cancel</button></div>`;
    }
  }
  $("#mapHint").addEventListener("click", e => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act || !draft) return;
    if (act === "cancel") { draft = null; layers.draft.clearLayers(); hint(); return; }
    if (act === "undo") draft.points.pop();
    if (act === "back") { draft.stage = "points"; delete draft.side; }
    if (act === "done") {
      if (mode === "area") { draft.stage = "name"; draft.name = `No-go area ${zones.filter(z => z.type === "area").length + 1}`; }
      else draft.stage = "side";
    }
    if (act === "flip") { draft.side = draft.side === "left" ? "right" : "left"; }
    if (act === "add") { finishZone(); return; }
    drawDraft(); hint();
  });

  function onMapClick(e) {
    const p = [+e.latlng.lat.toFixed(6), +e.latlng.lng.toFixed(6)];
    if (mode === "suburb") return pickSuburb(e.latlng);
    if (mode !== "area" && mode !== "road") return;
    draft = draft || { points: [], stage: "points" };
    if (draft.stage === "points") draft.points.push(p);
    else if (draft.stage === "side") {
      draft.side = sideOfRoad(p, draft.points);
      draft.stage = "name";
      draft.name = `Not the ${draft.side} side of road ${zones.filter(z => z.type === "road_side").length + 1}`;
    } else return;
    drawDraft(); hint();
  }

  function drawDraft() {
    layers.draft.clearLayers();
    if (!draft) return;
    const c = cssVar("--critical");
    if (mode === "area" && draft.points.length) {
      (draft.points.length >= 3 ? L.polygon : L.polyline)(draft.points, { color: c, weight: 2, dashArray: "6 4", fillOpacity: 0.15 }).addTo(layers.draft);
    }
    if (mode === "road" && draft.points.length) {
      if (draft.side && draft.points.length >= 2) L.polygon(sideShade(draft.points, draft.side), { stroke: false, fillColor: c, fillOpacity: 0.22, interactive: false }).addTo(layers.draft);
      L.polyline(draft.points, { color: c, weight: 4, dashArray: draft.side ? null : "6 4" }).addTo(layers.draft);
    }
    draft.points.forEach(pt => L.circleMarker(pt, { radius: 4, color: c, weight: 2, fillColor: "#fff", fillOpacity: 1, interactive: false }).addTo(layers.draft));
  }

  function finishZone() {
    const name = ($("#zoneName")?.value || draft.name || "").trim() || "No-go zone";
    const z = mode === "area" ? { name, type: "area", points: draft.points }
                              : { name, type: "road_side", points: draft.points, side: draft.side };
    zones.push(z);
    draft = null; layers.draft.clearLayers();
    setDirty(true); renderZones(); hint();
    renderPins();
  }

  // ------------------------------------------------------------------ zones
  function renderZones() {
    layers.zones.clearLayers();
    const c = cssVar("--critical");
    zones.forEach((z, i) => {
      const off = z.enabled === false;
      const style = { color: c, opacity: off ? 0.35 : 1, fillOpacity: off ? 0.05 : 0.22, weight: 2 };
      if (z.type === "area") L.polygon(z.points, style).bindTooltip(z.name).addTo(layers.zones);
      else {
        L.polygon(sideShade(z.points, z.side), { ...style, stroke: false, interactive: false }).addTo(layers.zones);
        L.polyline(z.points, { ...style, weight: 4 }).bindTooltip(`${z.name} (${z.side} side blocked)`).addTo(layers.zones);
      }
    });
    $("#zoneList").innerHTML = zones.length ? zones.map((z, i) => `
      <li data-i="${i}">
        <input type="checkbox" class="z-on" ${z.enabled === false ? "" : "checked"} title="Use this zone">
        <input type="text" class="z-name" value="${App().esc(z.name)}" aria-label="Zone name">
        <span class="muted small">${z.type === "area" ? "area" : z.side + " of road"}</span>
        <button type="button" class="btn ghost small z-zoom" title="Show on map">Show</button>
        <button type="button" class="btn ghost small z-del" title="Delete">✕</button>
      </li>`).join("") : `<li class="muted small">None yet. Use <i>Draw no-go area</i> or <i>Block one side of a road</i>.</li>`;
  }
  $("#zoneList").addEventListener("input", e => {
    const li = e.target.closest("li[data-i]"); if (!li) return;
    const z = zones[+li.dataset.i];
    if (e.target.classList.contains("z-name")) z.name = e.target.value;
    if (e.target.classList.contains("z-on")) { if (e.target.checked) delete z.enabled; else z.enabled = false; renderZones(); }
    setDirty(true);
  });
  $("#zoneList").addEventListener("click", e => {
    const li = e.target.closest("li[data-i]"); if (!li) return;
    const i = +li.dataset.i;
    if (e.target.classList.contains("z-del")) { zones.splice(i, 1); setDirty(true); renderZones(); }
    if (e.target.classList.contains("z-zoom")) map.fitBounds(L.latLngBounds(zones[i].points).pad(0.4), { maxZoom: 16 });
  });

  // ------------------------------------------------------------------ suburbs
  const key = n => n.trim().toLowerCase();
  async function pickSuburb(latlng) {
    hint("Looking up the suburb here on OpenStreetMap…");
    let place = null;
    try {
      place = (await App().api(`/api/geo/at?lat=${latlng.lat.toFixed(5)}&lon=${latlng.lng.toFixed(5)}`)).place;
    } catch (err) { hint(`Lookup failed: ${App().esc(err.message)}`); return; }
    hint();
    if (!place || !place.name) { L.popup().setLatLng(latlng).setContent("No suburb found here.").openOn(map); return; }
    outlines[key(place.name)] = place;
    const name = place.name, inInc = inc.some(n => key(n) === key(name)), inExc = exc.some(n => key(n) === key(name));
    const div = document.createElement("div");
    div.className = "suburbpop";
    div.innerHTML = `<b>${App().esc(name)}</b>${place.geometry ? "" : '<div class="muted small">OpenStreetMap has no outline for this suburb, only its centre.</div>'}
      <div class="hintbtns">
        <button type="button" class="btn small" data-to="inc" ${inInc ? "disabled" : ""}>Include</button>
        <button type="button" class="btn small ghost" data-to="exc" ${inExc ? "disabled" : ""}>Avoid</button>
        ${inInc || inExc ? '<button type="button" class="btn small ghost" data-to="none">Remove</button>' : ""}
      </div>`;
    div.addEventListener("click", e => {
      const to = e.target.closest("[data-to]")?.dataset.to; if (!to) return;
      inc = inc.filter(n => key(n) !== key(name)); exc = exc.filter(n => key(n) !== key(name));
      if (to === "inc") inc.push(name);
      if (to === "exc") exc.push(name);
      setDirty(true); renderChips(); renderOutlines(); map.closePopup();
    });
    L.popup().setLatLng(latlng).setContent(div).openOn(map);
    renderOutlines(place);
  }

  function renderChips() {
    const chip = (n, list) => {
      const p = outlines[key(n)];
      const miss = !p && map && map._loaded && covered(key(n), map.getBounds());
      return `<span class="chip on-${list}" data-name="${App().esc(n)}" data-list="${list}" title="${miss ? "Not found on OpenStreetMap near this map view: it still matches by name" : "Show on map"}">${App().esc(n)}${miss ? " ?" : ""}<button type="button" class="x" aria-label="Remove ${App().esc(n)}">✕</button></span>`;
    };
    $("#incChips").innerHTML = inc.map(n => chip(n, "inc")).join("") || '<span class="muted small">Anywhere</span>';
    $("#excChips").innerHTML = exc.map(n => chip(n, "exc")).join("") || '<span class="muted small">None</span>';
  }
  ["#incChips", "#excChips"].forEach(sel => $(sel).addEventListener("click", e => {
    const c = e.target.closest(".chip"); if (!c) return;
    const n = c.dataset.name;
    if (e.target.classList.contains("x")) {
      if (c.dataset.list === "inc") inc = inc.filter(x => x !== n); else exc = exc.filter(x => x !== n);
      setDirty(true); renderChips(); renderOutlines(); return;
    }
    const p = outlines[key(n)];
    if (p && p.geometry) map.fitBounds(L.geoJSON(p.geometry).getBounds().pad(0.3));
    else if (p) map.setView([p.lat, p.lon], 14);
  }));

  function renderOutlines(temp) {
    layers.suburbs.clearLayers();
    const drawn = new Set();          // two names can resolve to the same OSM area (Burgundy / Burgundy Estate)
    const draw = (p, color, dash, label) => {
      if (drawn.has(p.osm)) return;
      drawn.add(p.osm);
      if (p.geometry) L.geoJSON(p.geometry, { style: { color, weight: 2, dashArray: dash, fillOpacity: 0.08 }, interactive: false }).addTo(layers.suburbs);
      else L.circleMarker([p.lat, p.lon], { radius: 8, color, weight: 2, dashArray: dash, fillOpacity: 0.1, interactive: false }).addTo(layers.suburbs);
      if (label) L.tooltip({ permanent: true, direction: "center", className: "sublabel" }).setLatLng([p.lat, p.lon]).setContent(App().esc(label)).addTo(layers.suburbs);
    };
    inc.forEach(n => outlines[key(n)] && draw(outlines[key(n)], cssVar("--good"), null, n));
    exc.forEach(n => outlines[key(n)] && draw(outlines[key(n)], cssVar("--series-2"), "4 4", n));
    if (temp && !inc.concat(exc).some(n => key(n) === key(temp.name))) draw(temp, cssVar("--accent"), "2 4", null);
  }

  /* Outlines for the chips. Looked up near the current view (the server refuses huge views),
     one name at a time; each answer is cached on the server for next time. */
  function loadOutlines() {
    if (!map) return;
    const b = map.getBounds();
    if (b.getNorth() - b.getSouth() > 3 || b.getEast() - b.getWest() > 3) return;
    const names = [...new Set(inc.concat(exc))].filter(n => !outlines[key(n)] && !covered(key(n), b) && !pending.has(key(n)));
    if (!names.length) { renderChips(); return; }
    names.forEach(n => pending.add(key(n)));
    outlineQueue = outlineQueue.then(async () => {
      for (const [i, n] of names.entries()) {
        pending.delete(key(n));
        if (outlines[key(n)]) continue;
        $("#mapStats").dataset.loading = `Finding area outlines on OpenStreetMap ${i + 1}/${names.length}…`;
        renderStats();
        const q = `name=${encodeURIComponent(n)}&s=${b.getSouth()}&w=${b.getWest()}&n=${b.getNorth()}&e=${b.getEast()}`;
        try {
          const place = (await App().api(`/api/geo/suburb?${q}`)).place;
          if (place) outlines[key(n)] = place; else (tried[key(n)] = tried[key(n)] || []).push(b);
        } catch { /* network trouble: try again on the next move */ }
        renderOutlines(); renderChips();
      }
      delete $("#mapStats").dataset.loading;
      renderStats();
    });
  }

  // ------------------------------------------------------------------ listings
  let pinCounts = null;
  async function renderPins() {
    if (!map) return;
    layers.pins.clearLayers();
    const exp = App().getRun();
    if (!exp) { pinCounts = null; renderStats(); return; }
    const A = App(), rows = [];
    for (const l of exp.listings || []) {
      rows.push({ site: l.id.split(":")[0], title: l.title, url: l.url, price: l.price_zar, beds: l.beds, size: l.size_m2,
                  suburb: l.suburb, address: l.address, score: l.score, lat: l.lat, lon: l.lon, prec: l.location,
                  flags: l.flags || [], rejected: null });
    }
    const matching = rows.length;
    if ($("#mapShowRejected").checked) {
      for (const r of await A.ensureAll()) {
        if (r.matches_filters === "yes") continue;
        rows.push({ site: r.site, title: r.title, url: r.url, price: r.price, beds: r.bedrooms, size: r.size_m2,
                    suburb: r.suburb, address: r.address, score: r.score, lat: r.lat ? +r.lat : null, lon: r.lon ? +r.lon : null,
                    prec: r.location_precision, flags: r.flags ? r.flags.split("; ") : [], rejected: r.filter_reason || "filtered" });
      }
    }
    let shown = 0, exact = 0, matchPinned = 0;
    const other = cssVar("--other");
    // filtered-out first so matches draw on top
    rows.sort((a, b) => (b.rejected ? 1 : 0) - (a.rejected ? 1 : 0));
    for (const r of rows) {
      if (r.lat == null || r.lon == null) continue;
      shown++;
      if (!r.rejected) { matchPinned++; if (r.prec === "exact") exact++; }
      const color = r.rejected ? other : cssVar(`--series-${A.SITE_ORDER.indexOf(r.site) + 1 || 1}`);
      const approx = r.prec !== "exact";
      const m = L.circleMarker([r.lat, r.lon], {
        radius: r.rejected ? 5 : 7, color: approx ? color : cssVar("--surface-1"), weight: 2,
        dashArray: approx ? "3 3" : null, fillColor: color, fillOpacity: approx ? 0.25 : 0.95,
      });
      const zoneFlag = r.flags.find(f => f.startsWith("may be in no-go zone"));
      m.bindPopup(`<b><a href="${A.esc(r.url)}" target="_blank" rel="noopener">${A.esc(r.title || "(untitled)")}</a></b>
        <div>${A.rand(r.price)} · ${r.beds ?? "?"} bed${r.size ? ` · ${r.size} m²` : ""}${r.score != null ? ` · score ${r.score}` : ""}</div>
        <div class="muted">${A.esc(r.address || r.suburb || "")} · ${A.esc(A.SITE_LABEL[r.site] || r.site)}</div>
        <div class="muted small">${approx ? "Approximate location" : "Exact location (from the site)"}</div>
        ${r.rejected ? `<div class="flag warn">filtered: ${A.esc(r.rejected)}</div>` : ""}
        ${zoneFlag ? `<div class="flag warn">${A.esc(zoneFlag)}</div>` : ""}`);
      m.addTo(layers.pins);
    }
    pinCounts = { matching, matchPinned, exact, shown };
    renderStats();
  }

  function renderStats() {
    const el = $("#mapStats");
    const c = pinCounts;
    const base = !App().getRun() ? "No run selected." :
      !c ? "" :
      `${c.matchPinned} of ${c.matching} matching listings have a map pin (${c.exact} exact). The rest are judged by suburb.` +
      (App().getRun().summary && App().getRun().summary.with_map_location == null ? " This run is older than map locations: run a new scrape to get pins." : "");
    el.textContent = [base, el.dataset.loading].filter(Boolean).join(" ");
  }

  $("#mapShowRejected").addEventListener("change", renderPins);
  $("#mapSave").addEventListener("click", save);
  $("#mapRevert").addEventListener("click", () => { loadFromConfig(); renderZones(); renderChips(); renderOutlines(); loadOutlines(); $("#mapMsg").textContent = ""; });
  document.addEventListener("run-loaded", () => { if (map) renderPins(); });
  document.addEventListener("click", e => {
    const g = e.target.closest("[data-goto]");
    if (g) { e.preventDefault(); App().showTab(g.dataset.goto); }
  });

  window.MapView = { show };
})();
