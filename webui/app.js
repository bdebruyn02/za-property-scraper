(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  const SITE_ORDER = ["property24", "privateproperty", "gumtree"];
  const SITE_LABEL = { property24: "Property24", privateproperty: "Private Property", gumtree: "Gumtree" };
  const siteColor = s => `var(--series-${SITE_ORDER.indexOf(s) + 1 || 1})`;
  const TYPES = ["Apartment", "Townhouse", "Simplex", "Duplex", "Cluster", "Penthouse", "Loft",
                 "Cottage", "Room", "House", "Commercial", "Farm", "Land"];
  const NON_RES = new Set(["Commercial", "Land", "Farm", "Room"]);

  const state = { runs: [], stamp: null, exp: null, all: null, rows: [], sortKey: "score", sortAsc: false,
                  config: null, pollTimer: null, logSeq: 0 };

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const rand = v => v == null || v === "" ? "–" : "R " + Math.round(+v).toLocaleString("en-ZA").replace(/,/g, " ");
  const num = v => v == null || v === "" ? null : +v;
  const fmtStamp = s => `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)} ${s.slice(9, 11)}:${s.slice(11, 13)}`;

  async function api(path, opts) {
    const r = await fetch(path, opts);
    const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw Object.assign(new Error(body.error || r.statusText), { body });
    return body;
  }

  // ------------------------------------------------------------------ tabs
  function showTab(name) {
    $$(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
    $$(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
    if (name === "chart") renderCharts();
    if (name === "map" && window.MapView) window.MapView.show();
    history.replaceState(null, "", "#" + name);
  }
  $$(".tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

  // ------------------------------------------------------------------ runs
  async function loadRuns(selectStamp) {
    state.runs = await api("/api/runs");
    const sel = $("#runSelect");
    sel.innerHTML = state.runs.map(r =>
      `<option value="${r.stamp}">${fmtStamp(r.stamp)} · ${r.matching} matches${r.new_only ? " · new only" : ""}${r.has_scores ? "" : " · no scores"}</option>`).join("");
    $("#emptyState").hidden = state.runs.length > 0;
    if (!state.runs.length) { $("#tiles").innerHTML = ""; return; }
    const stamp = selectStamp || state.stamp || state.runs[0].stamp;
    sel.value = state.runs.some(r => r.stamp === stamp) ? stamp : state.runs[0].stamp;
    await loadRun(sel.value);
  }
  $("#runSelect").addEventListener("change", e => loadRun(e.target.value));
  $("#btnRescore").addEventListener("click", async () => {
    if (!state.stamp) return;
    const btn = $("#btnRescore"); btn.disabled = true; btn.textContent = "Re-scoring…";
    try {
      const r = await api(`/api/runs/${state.stamp}/rescore`, { method: "POST", headers: { "Content-Type": "application/json" },
                                                                   body: JSON.stringify({ overrides: sessionGoal()?.overrides || null }) });
      await loadRuns(r.stamp);
    } catch (e) { alert("Re-score failed: " + e.message); }
    btn.disabled = false; btn.textContent = "Re-score";
  });

  async function loadRun(stamp) {
    state.stamp = stamp;
    state.exp = await api(`/api/runs/${stamp}`);
    state.all = null;
    $("#dlJson").href = `/files/claude_export_${stamp}.json`;
    $("#dlCsv").href = `/files/listings_${stamp}.csv`;
    $("#oldRunNote").hidden = !!state.exp.market;
    renderTiles();
    if ($("#showRejected").checked || $("#chartShowAll").checked) await ensureAll();
    renderSiteToggles();
    renderTable();
    if ($("#tab-chart").classList.contains("active")) renderCharts();
    document.dispatchEvent(new CustomEvent("run-loaded"));
  }

  async function ensureAll() {
    if (!state.all) state.all = await api(`/api/runs/${state.stamp}/all`);
    return state.all;
  }

  // normalise an export listing / CSV row into one shape
  function fromExport(l) {
    const [site, ...rest] = l.id.split(":");
    return { key: l.id, site, id: rest.join(":"), price: num(l.price_zar), score: num(l.score), value_pct: num(l.value_pct),
             fair: num(l.fair_price), beds: num(l.beds), baths: num(l.baths), size: num(l.size_m2), title: l.title,
             type: l.type, suburb: l.suburb, address: l.address, available: l.available, agency: l.agency,
             flags: l.flags || [], badges: l.badges || [], excerpt: l.excerpt, url: l.url, dupe_of: l.dupe_of,
             kind: l.kind, period: l.price_period || "month", rejected: null };
  }
  function fromCsv(r) {
    return { key: `${r.site}:${r.listing_id}`, site: r.site, id: r.listing_id, price: num(r.price), score: num(r.score),
             value_pct: num(r.value_pct), fair: num(r.fair_price), beds: num(r.bedrooms), baths: num(r.bathrooms),
             size: num(r.size_m2), title: r.title, type: r.property_type, suburb: r.suburb, address: r.address,
             available: r.available, agency: r.agency, flags: r.flags ? r.flags.split("; ") : [],
             badges: r.badges ? r.badges.split("; ") : [], excerpt: r.excerpt, url: r.url, dupe_of: r.dupe_of || null,
             kind: r.kind, period: r.price_period || (r.kind === "rent" ? "month" : null),
             rejected: r.matches_filters === "yes" ? null : (r.filter_reason || "filtered") };
  }

  // ------------------------------------------------------------------ tiles
  const isSale = () => ((state.exp && state.exp.filters_applied) || {}).kind === "sale";
  const priceWord = () => isSale() ? "Price" : "Rent";
  function applyKindLabels() {
    $("#thPrice").textContent = priceWord();
    $("#h2Chart1").textContent = `${priceWord()} vs floor size`;
    $("#h2Chart2").textContent = `${priceWord()} per m²`;
  }
  function renderTiles() {
    applyKindLabels();
    const e = state.exp, s = e.summary || {}, m = e.market || {}, sw = m.sweet_spot;
    const f = e.filters_applied || {};
    const tile = (k, v, sub) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub || ""}</div></div>`;
    $("#tiles").innerHTML = [
      tile("Matching listings", s.matching ?? "–", `of ${s.scraped_total ?? "?"} scraped${e.new_only ? " (new only)" : ""}`),
      tile(`Median ${priceWord().toLowerCase()} (matches)`, rand(s.price_median), `${rand(s.price_min)} – ${rand(s.price_max)}`),
      tile("Size sweet spot", sw ? `${sw.size_m2_from}–${sw.size_m2_to} m²` : "–",
           sw ? `fair rent ${rand(sw.fair_rent_from)}–${rand(sw.fair_rent_to)}` : "needs ≥10 listings with sizes"),
      tile("Your budget buys", sw && sw.budget_buys_up_to_m2 ? `~${sw.budget_buys_up_to_m2} m²` : "–",
           f.max_price ? `typically, at ${rand(f.max_price)}/month` : ""),
      tile("Likely duplicates", s.possible_cross_site_duplicates ?? "–", "same unit on two sites"),
      tile("Market sample", m.points_used ?? "–", "listings with size used for the curve"),
      tile("On the map", s.with_map_location ?? "–", s.with_map_location != null ? `of ${s.matching} matches have a map pin` : "older run: no locations"),
    ].join("");
  }

  // ------------------------------------------------------------------ table
  function renderSiteToggles() {
    const box = $("#siteToggles");
    if (box.childElementCount) return;
    box.innerHTML = SITE_ORDER.map(s =>
      `<label><input type="checkbox" data-site="${s}" checked><span class="swatch" style="background:${siteColor(s)}"></span>${SITE_LABEL[s]}</label>`).join("");
    box.addEventListener("change", renderTable);
  }

  function currentRows() {
    const base = $("#showRejected").checked && state.all ? state.all.map(fromCsv) : (state.exp.listings || []).map(fromExport);
    const q = $("#q").value.trim().toLowerCase();
    const sites = new Set($$("#siteToggles input:checked").map(i => i.dataset.site));
    return base.filter(r =>
      sites.has(r.site) &&
      !($("#hideDupes").checked && r.dupe_of) &&
      (!q || [r.title, r.suburb, r.agency, r.excerpt, r.address, r.type].join(" ").toLowerCase().includes(q)));
  }

  function sortRows(rows) {
    const k = state.sortKey, dir = state.sortAsc ? 1 : -1;
    const val = r => k === "flags" ? r.flags.length : r[k];
    return rows.sort((a, b) => {
      const va = val(a), vb = val(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      return (typeof va === "number" ? va - vb : String(va).localeCompare(String(vb))) * dir;
    });
  }

  function renderTable() {
    if (!state.exp) return;
    const rows = sortRows(currentRows());
    $("#rowCount").textContent = `${rows.length} shown`;
    $$("#listTable th").forEach(th => {
      th.classList.toggle("sorted", th.dataset.k === state.sortKey);
      th.classList.toggle("asc", th.dataset.k === state.sortKey && state.sortAsc);
    });
    $("#listTable tbody").innerHTML = rows.map(r => {
      const vp = r.value_pct == null ? "–" : `<span class="${r.value_pct >= 0 ? "pos" : "neg"}">${r.value_pct > 0 ? "+" : ""}${r.value_pct}%</span>`;
      const score = r.score == null ? "–" : `<span class="scorebar"><i style="width:${r.score}%"></i></span>${r.score}`;
      const flags = (r.rejected ? [`<span class="flag warn">filtered: ${esc(r.rejected)}</span>`] : [])
        .concat(r.flags.map(f => `<span class="flag ${/below market|scam|POA/.test(f) ? "warn" : ""}">${esc(f)}</span>`))
        .concat(r.badges.filter(b => b !== "New").map(b => `<span class="flag">${esc(b)}</span>`)).join("");
      return `<tr class="${r.rejected ? "rejected" : ""}">
        <td class="num">${score}</td>
        <td class="num">${rand(r.price)}${r.period && r.period !== "month" ? "/" + r.period : ""}</td>
        <td class="num" title="fair price ${rand(r.fair)}">${vp}</td>
        <td class="num">${r.beds ?? "–"}</td><td class="num">${r.baths ?? "–"}</td>
        <td class="num">${r.size ?? "–"}</td>
        <td class="title"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || "(untitled)")}</a>
            <div class="ex">${esc(r.type || "")}${r.address ? " · " + esc(r.address) : ""}${r.excerpt ? " · " + esc(r.excerpt.slice(0, 140)) + "…" : ""}</div></td>
        <td>${esc(r.suburb || "–")}</td>
        <td class="nowrap">${esc(r.available || "–")}</td>
        <td><span class="swatch" style="background:${siteColor(r.site)}"></span>${esc(SITE_LABEL[r.site] || r.site)}</td>
        <td>${esc(r.agency || "–")}</td>
        <td>${flags}</td></tr>`;
    }).join("");
  }
  $$("#listTable th").forEach(th => th.addEventListener("click", () => {
    if (state.sortKey === th.dataset.k) state.sortAsc = !state.sortAsc;
    else { state.sortKey = th.dataset.k; state.sortAsc = ["title", "suburb", "site", "agency", "available"].includes(th.dataset.k); }
    renderTable();
  }));
  $("#q").addEventListener("input", renderTable);
  $("#hideDupes").addEventListener("change", renderTable);
  $("#showRejected").addEventListener("change", async () => { if ($("#showRejected").checked) await ensureAll(); renderTable(); });

  // ------------------------------------------------------------------ charts
  function tooltipHtml(r) {
    const row = (k, v) => `<div class="row"><span>${k}</span><span>${v}</span></div>`;
    return `<b>${esc(r.title || "")}</b>` +
      row(priceWord(), rand(r.price)) + row("Fair price", rand(r.fair)) +
      (r.value_pct != null ? row("vs market", (r.value_pct > 0 ? "+" : "") + r.value_pct + "%") : "") +
      row("Size", r.size ? r.size + " m²" : "–") + row("Beds", r.beds ?? "–") +
      row("Suburb", esc(r.suburb || "–")) + row("Site", SITE_LABEL[r.site] || r.site) +
      (r.score != null ? row("Score", r.score) : "") +
      (r.rejected ? row("Filtered", esc(r.rejected)) : "");
  }

  function legend(id, withBudget) {
    $(id).innerHTML = SITE_ORDER.map(s => `<span><i class="dot" style="background:${siteColor(s)}"></i>${SITE_LABEL[s]}</span>`).join("") +
      `<span><i class="dot" style="background:var(--other)"></i>Filtered out</span>` +
      `<span><i class="line"></i>Fair price (spline)</span><span><i class="band"></i>Sweet spot</span>` +
      (withBudget ? `<span><i class="dash"></i>Your max rent</span>` : "");
  }

  async function renderCharts() {
    if (!state.exp) return;
    const m = state.exp.market || {}, curve = m.fair_price_curve || [], sw = m.sweet_spot;
    const showAll = $("#chartShowAll").checked;
    let rows = (state.exp.listings || []).map(fromExport);
    if (showAll) {
      const all = (await ensureAll()).map(fromCsv)
        .filter(r => r.rejected && r.kind === "rent" && r.period === "month" && !NON_RES.has(r.type) && r.price >= 2000);
      rows = rows.concat(all);
    }
    const plotted = rows.filter(r => r.size && r.price);
    const noSize = rows.filter(r => !r.rejected && !r.size).length;
    const mk = (r, y) => ({ x: r.size, y, cls: r.rejected ? "other" : "", color: r.rejected ? null : siteColor(r.site), data: r });
    const band = sw ? { from: sw.size_m2_from, to: sw.size_m2_to, label: `Sweet spot ${sw.size_m2_from}–${sw.size_m2_to} m²` } : null;
    const maxP = (state.exp.filters_applied || {}).max_price;
    const open = r => r.url && window.open(r.url, "_blank", "noopener");

    legend("#legend1", !!maxP);
    const r1 = Charts.scatter($("#chart1"), {
      points: plotted.map(r => mk(r, r.price)), curve, band,
      hline: maxP ? { y: maxP, label: `Your max ${rand(maxP)}` } : null,
      xLabel: "Floor size (m²)",
      yFmt: v => v >= 1e6 ? "R " + (v / 1e6).toLocaleString("en-ZA", { maximumFractionDigits: 2 }) + "m" : "R " + (v / 1000).toLocaleString("en-ZA") + "k",
      curveLabel: "fair price", tooltip: tooltipHtml, onClick: open, height: 400, ariaLabel: "Monthly rent against floor size",
    });
    $("#chartNote1").textContent = `${noSize} matching listings have no floor size and aren't plotted.` +
      (r1.hidden ? ` ${r1.hidden} outliers are outside the visible range.` : "");

    legend("#legend2", false);
    Charts.scatter($("#chart2"), {
      points: plotted.map(r => mk(r, r.price / r.size)), curve: curve.map(([x, y]) => [x, y / x]), band,
      xLabel: "Floor size (m²)", yFmt: v => "R " + Math.round(v), curveLabel: "fair R/m²",
      tooltip: r => tooltipHtml(r) + `<div class="row"><span>Rent per m²</span><span>R ${Math.round(r.price / r.size)}</span></div>`,
      onClick: open, height: 320, ariaLabel: "Rent per square metre against floor size",
    });

    const budgetLine = !sw ? "" :
      sw.within_budget === "yes" ? "The whole band is within your budget." :
      sw.within_budget === "partly" ? `Your max rent reaches the start of the band (the curve crosses it at ~${sw.budget_buys_up_to_m2} m²). Listings below the line in the band are the ones to chase.` :
      sw.within_budget === "no" ? `The band starts about <b>${rand(sw.extra_rent_to_reach)}</b> a month above your max rent. At your max rent the market typically gives you ~${sw.budget_buys_up_to_m2 ?? "?"} m²; stretching the budget would buy a lot more space.` : "";
    $("#sweetText").innerHTML = sw
      ? `<b>Sweet spot: ${sw.size_m2_from}–${sw.size_m2_to} m²</b>, fair rent ${rand(sw.fair_rent_from)}–${rand(sw.fair_rent_to)} a month.
         Here each extra m² adds about <b>R ${sw.marginal_rent_per_extra_m2}</b> to the rent, against a typical R ${sw.typical_marginal_rent_per_extra_m2}.
         ${budgetLine} The best value is a listing <i>inside</i> the band and <i>below</i> the line.
         <br><span class="muted small">${esc(m.method || "")}</span>`
      : "Not enough listings with a floor size to fit the curve yet (needs 10+). Run a scrape that includes the market-sample searches.";
  }
  $("#chartShowAll").addEventListener("change", renderCharts);
  let resizeT;
  window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if ($("#tab-chart").classList.contains("active")) renderCharts(); }, 150); });

  // ------------------------------------------------------------------ run
  function setRunState(s) {
    const pill = $("#runState");
    pill.textContent = s; pill.className = "pill " + s;
    $("#btnStart").disabled = s === "running";
    $("#btnStop").disabled = s !== "running";
  }
  function appendLog(lines) {
    if (!lines.length) return;
    const pre = $("#log"), atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent += lines.map(l => l.text).join("\n") + "\n";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }
  async function poll() {
    try {
      const st = await api(`/api/status?since=${state.logSeq}`);
      appendLog(st.lines); state.logSeq = st.next;
      setRunState(st.state);
      if (st.state === "running") { state.pollTimer = setTimeout(poll, 1000); return; }
      state.pollTimer = null;
      if (st.error) appendLog([{ text: "ERROR: " + st.error }]);
      if (st.result) { await loadRuns(st.result.stamp); }
    } catch (e) { appendLog([{ text: "Lost contact with the server: " + e.message }]); setRunState("error"); }
  }
  async function startRun() {
    showTab("run");
    $("#log").textContent = ""; state.logSeq = 0;
    const mp = parseInt($("#optMaxPages").value, 10);
    try {
      await api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        new_only: $("#optNewOnly").checked, debug: $("#optDebug").checked, no_browser: $("#optNoBrowser").checked,
        no_filters: $("#optNoFilters").checked, max_pages: mp > 0 ? mp : null,
        no_locations: !$("#optLocate").checked,
        overrides: sessionGoal()?.overrides || null }) });
      setRunState("running"); poll();
    } catch (e) { appendLog([{ text: "Could not start: " + e.message }]); }
  }
  $("#btnStart").addEventListener("click", startRun);
  $("#btnStop").addEventListener("click", () => api("/api/stop", { method: "POST" }));

  // ------------------------------------------------------------------ settings
  const list = v => (v || "").split(/[\n,]/).map(s => s.trim()).filter(Boolean);
  const numOrNull = v => v === "" || v == null ? null : +v;

  function searchRow(s = { enabled: true, name: "", url: "" }) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><input type="checkbox" class="s-en" ${s.enabled !== false ? "checked" : ""}></td>
      <td><input type="text" class="s-name" value="${esc(s.name)}" placeholder="e.g. P24 Table View"></td>
      <td><input type="text" class="s-url" value="${esc(s.url)}" placeholder="https://www.property24.com/to-rent/..."></td>
      <td><input type="number" class="s-pages" min="1" max="20" value="${s.max_pages ?? ""}" placeholder="def."></td>
      <td><button type="button" class="btn ghost small s-del" title="Remove">✕</button></td>`;
    tr.querySelector(".s-del").addEventListener("click", () => tr.remove());
    return tr;
  }

  async function loadSettings() {
    const { config, supported_domains } = await api("/api/config");
    state.config = config;
    const f = config.filters || {}, sc = config.scoring || {};
    $("#notes").value = config.notes_for_claude || "";
    $("#f_kind").value = f.kind ?? "";
    for (const k of ["min_price", "max_price", "min_beds", "max_beds", "min_size_m2"]) $("#f_" + k).value = f[k] ?? "";
    $("#f_monthly_price_only").checked = f.monthly_price_only !== false;
    const chosen = new Set(f.property_types || []);
    $("#f_types").innerHTML = TYPES.map(t => `<label><input type="checkbox" value="${t}" ${chosen.has(t) ? "checked" : ""}> ${t}</label>`).join("");
    $("#f_include_suburbs").value = (f.include_suburbs || []).join(", ");
    $("#f_exclude_suburbs").value = (f.exclude_suburbs || []).join(", ");
    $("#f_exclude_keywords").value = (f.exclude_keywords || []).join(", ");
    $("#s_bonus_keywords").value = (sc.bonus_keywords || []).join(", ");
    $("#s_red_flag_keywords").value = (sc.red_flag_keywords || []).join(", ");
    $("#maxPages").value = config.max_pages ?? 5;
    $("#domains").textContent = supported_domains.join(", ");
    const body = $("#searchRows"); body.innerHTML = "";
    (config.searches || []).forEach(s => body.appendChild(searchRow(s)));
    const nz = (f.no_go_zones || []).length;
    $("#zoneNote").textContent = nz ? `${nz} no-go zone${nz > 1 ? "s" : ""} saved.` : "No no-go zones yet.";
    $("#runSearchList").innerHTML = (config.searches || []).filter(s => s.enabled !== false)
      .map(s => `<li>${esc(s.name)} <span class="muted">(${s.max_pages || config.max_pages || 5} pages max)</span></li>`).join("");
  }
  $("#addSearch").addEventListener("click", () => $("#searchRows").appendChild(searchRow()));

  $("#settingsForm").addEventListener("submit", async e => {
    e.preventDefault();
    const cfg = JSON.parse(JSON.stringify(state.config || {}));   // keep unknown keys
    cfg.notes_for_claude = $("#notes").value.trim();
    cfg.max_pages = parseInt($("#maxPages").value, 10) || 5;
    cfg.filters = Object.assign(cfg.filters || {}, {
      kind: $("#f_kind").value || null,
      min_price: numOrNull($("#f_min_price").value), max_price: numOrNull($("#f_max_price").value),
      min_beds: numOrNull($("#f_min_beds").value), max_beds: numOrNull($("#f_max_beds").value),
      min_size_m2: numOrNull($("#f_min_size_m2").value),
      monthly_price_only: $("#f_monthly_price_only").checked,
      property_types: $$("#f_types input:checked").map(i => i.value),
      include_suburbs: list($("#f_include_suburbs").value),
      exclude_suburbs: list($("#f_exclude_suburbs").value),
      exclude_keywords: list($("#f_exclude_keywords").value),
    });
    cfg.scoring = Object.assign(cfg.scoring || {}, {
      bonus_keywords: list($("#s_bonus_keywords").value),
      red_flag_keywords: list($("#s_red_flag_keywords").value),
    });
    cfg.searches = $$("#searchRows tr").map(tr => {
      const s = { name: tr.querySelector(".s-name").value.trim(), url: tr.querySelector(".s-url").value.trim() };
      const p = parseInt(tr.querySelector(".s-pages").value, 10);
      if (p > 0) s.max_pages = p;
      if (!tr.querySelector(".s-en").checked) s.enabled = false;
      return s;
    }).filter(s => s.name || s.url);
    const msg = $("#saveMsg");
    try {
      await api("/api/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cfg) });
      msg.className = "ok"; msg.textContent = "Saved. The next scrape uses these settings.";
      await loadSettings();
    } catch (err) {
      msg.className = "err"; msg.textContent = (err.body && err.body.errors || [err.message]).join("\n");
    }
  });

  // ------------------------------------------------------------------ session goal (onboarding, not saved)
  function sessionGoal() {
    try { return JSON.parse(sessionStorage.getItem("sessionGoal") || "null"); } catch { return null; }
  }
  function setSessionGoal(g) {
    if (g) sessionStorage.setItem("sessionGoal", JSON.stringify(g)); else sessionStorage.removeItem("sessionGoal");
    $("#sessionBanner").hidden = !g;
  }
  $("#bnDiscard").addEventListener("click", () => setSessionGoal(null));
  $("#bnSave").addEventListener("click", async () => {
    const g = sessionGoal(); if (!g) return;
    try {
      await api("/api/onboarding/apply", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answers: g.answers, notes: g.notes, replace_searches: g.replace_searches, save: true }) });
      setSessionGoal(null); await loadSettings();
    } catch (e) { alert("Could not save: " + ((e.body && e.body.errors) || [e.message]).join("; ")); }
  });
  window.App = { api, showTab, startRun, loadSettings, sessionGoal, setSessionGoal, getConfig: () => state.config,
                 getRun: () => state.exp, ensureAll: () => state.stamp ? ensureAll() : Promise.resolve([]),
                 rand, esc, siteColor, SITE_LABEL, SITE_ORDER };

  // ------------------------------------------------------------------ init
  (async function init() {
    setSessionGoal(sessionGoal());
    await Promise.all([loadSettings(), loadRuns()]);
    $("#interp").textContent = (await api("/api/interpretation")).how_to_interpret;
    const st = await api("/api/status");
    setRunState(st.state);
    appendLog(st.lines); state.logSeq = st.next;
    if (st.state === "running") poll();
    const tab = location.hash.slice(1);
    if (tab && $("#tab-" + tab)) showTab(tab);
    if (window.Onboarding) window.Onboarding.maybeShow();
  })();
})();
