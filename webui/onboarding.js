/* "What's your goal?" onboarding wizard. Answers -> /api/onboarding/preview (server builds
   filters, scoring, brief for Claude and search URLs) -> /api/onboarding/apply (save, or
   session-only overrides kept in sessionStorage). */
(function () {
  "use strict";
  const $ = s => document.querySelector(s);
  const $$ = s => Array.from(document.querySelectorAll(s));
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const rand = n => "R " + Math.round(n).toLocaleString("en-ZA").replace(/,/g, " ");
  const STEPS = 6;
  const BEDS = [[0, "Studio"], [1, "1"], [2, "2"], [3, "3"], [4, "4+"]];
  const PRESETS = { rent: [8000, 10000, 13000, 15000, 20000], buy: [1000000, 1500000, 2000000, 3000000, 4500000] };

  let catalog = null, step = 0, preview = null;
  let a = blank();

  function blank() {
    return { goal: null, budget_max: null, budget_min: null, beds: [], min_size_m2: null, types: [],
             regions: [], include_suburbs: "", exclude_suburbs: "", priorities: ["value"], extra: "" };
  }

  // Seed answers from the saved config when there are no saved onboarding answers yet.
  function fromConfig(cfg) {
    const f = (cfg && cfg.filters) || {}, b = blank();
    b.goal = f.kind === "sale" ? "buy" : f.kind === "rent" ? "rent" : null;
    b.budget_max = f.max_price ?? null; b.budget_min = f.min_price ?? null;
    if (f.min_beds != null || f.max_beds != null) {
      const lo = f.min_beds ?? 0, hi = f.max_beds ?? 4;
      b.beds = BEDS.map(x => x[0]).filter(v => v >= lo && v <= hi);
    }
    b.types = (f.property_types || []).filter(t => catalog.property_types.includes(t));
    const inc = new Set((f.include_suburbs || []).map(s => s.toLowerCase()));
    b.regions = catalog.regions.filter(r => r.suburbs.some(s => inc.has(s.toLowerCase()))).map(r => r.id);
    const known = new Set(catalog.regions.flatMap(r => r.suburbs.map(s => s.toLowerCase())));
    b.include_suburbs = (f.include_suburbs || []).filter(s => !known.has(s.toLowerCase())).join(", ");
    b.exclude_suburbs = (f.exclude_suburbs || []).join(", ");
    b.min_size_m2 = f.min_size_m2 ?? null;
    return b;
  }

  // ---------------------------------------------------------------- rendering
  function chips(el, items, selected, onToggle, ordered) {
    el.innerHTML = items.map(([v, label]) => {
      const i = selected.indexOf(v);
      return `<button type="button" class="chip ${i >= 0 ? "on" : ""}" data-v="${esc(v)}" aria-pressed="${i >= 0}">` +
             `${ordered && i >= 0 ? `<em>${i + 1}</em>` : ""}${esc(label)}</button>`;
    }).join("");
    el.querySelectorAll(".chip").forEach(c => c.addEventListener("click", () => {
      const raw = c.dataset.v, v = items.find(x => String(x[0]) === raw)[0];
      readInputs(); onToggle(v); render();
    }));
  }
  const toggle = (arr, v) => { const i = arr.indexOf(v); if (i >= 0) arr.splice(i, 1); else arr.push(v); };

  function render() {
    $$(".ob-step").forEach(s => s.classList.toggle("active", +s.dataset.step === step));
    $("#obProgress").innerHTML = Array.from({ length: STEPS }, (_, i) => `<i class="${i < step ? "done" : i === step ? "cur" : ""}"></i>`).join("");
    $("#obBack").style.visibility = step === 0 ? "hidden" : "visible";
    $("#obNext").textContent = step === STEPS - 1 ? ($("#obSave").checked ? "Save" : "Use for this session") +
      ($("#obStart").checked ? " & start" : "") : "Next";
    $("#obError").hidden = true;

    $$(".choice[data-goal]").forEach(c => c.classList.toggle("on", c.dataset.goal === a.goal));
    const rent = a.goal !== "buy";
    $("#obPer").textContent = rent ? "rent per month (R)" : "price (R)";
    $("#obMax").value = a.budget_max ?? ""; $("#obMin").value = a.budget_min ?? ""; $("#obSize").value = a.min_size_m2 ?? "";
    chips($("#obBudgetPresets"), PRESETS[rent ? "rent" : "buy"].map(v => [v, (rent ? "≤ " : "≤ ") + rand(v)]),
          [a.budget_max], v => { a.budget_max = v; });
    chips($("#obBeds"), BEDS, a.beds, v => toggle(a.beds, v));
    chips($("#obTypes"), catalog.property_types.map(t => [t, t]), a.types, v => toggle(a.types, v));
    $("#obRegions").innerHTML = catalog.regions.map(r =>
      `<button type="button" class="choice ${a.regions.includes(r.id) ? "on" : ""}" data-region="${r.id}">
         <b>${esc(r.label)}</b><span>${esc(r.suburbs.join(", "))}</span></button>`).join("");
    $$("#obRegions .choice").forEach(c => c.addEventListener("click", () => { readInputs(); toggle(a.regions, c.dataset.region); render(); }));
    $("#obInclude").value = a.include_suburbs; $("#obExclude").value = a.exclude_suburbs;
    chips($("#obPrios"), catalog.priorities.map(p => [p.id, p.label]), a.priorities, v => toggle(a.priorities, v), true);
    $("#obExtra").value = a.extra;
  }

  function readInputs() {
    const n = v => v === "" ? null : +v;
    a.budget_max = n($("#obMax").value); a.budget_min = n($("#obMin").value); a.min_size_m2 = n($("#obSize").value);
    a.include_suburbs = $("#obInclude").value; a.exclude_suburbs = $("#obExclude").value; a.extra = $("#obExtra").value;
  }

  function stepError() {
    if (step === 0 && !a.goal) return "Choose rent or buy.";
    if (step === 1 && !a.budget_max) return "Set a maximum budget (or tap a preset).";
    if (step === 1 && a.budget_min && a.budget_min > a.budget_max) return "Minimum is above maximum.";
    if (step === 2 && !a.types.length) return "Pick at least one property type.";
    if (step === 3 && !a.regions.length && !a.include_suburbs.trim()) return "Pick an area or type one in.";
    return null;
  }

  async function buildPreview() {
    preview = await App.api("/api/onboarding/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(a) });
    const f = preview.filters, rent = f.kind === "rent";
    const beds = a.beds.length ? a.beds.slice().sort((x, y) => x - y).map(b => BEDS.find(x => x[0] === b)[1]).join(", ") : "any";
    $("#obSummary").innerHTML = [
      `<li><b>${rent ? "Rent" : "Buy"}</b>, ${rand(f.min_price)} – ${rand(f.max_price)}${rent ? " per month" : ""}; bedrooms: ${esc(beds)}</li>`,
      `<li>Types: ${esc(f.property_types.join(", "))}</li>`,
      `<li>Areas: ${esc(f.include_suburbs.join(", "))}${f.exclude_suburbs.length ? ` · avoid ${esc(f.exclude_suburbs.join(", "))}` : ""}</li>`,
      `<li>Score bonus keywords: ${preview.scoring.bonus_keywords.length ? esc(preview.scoring.bonus_keywords.slice(0, 6).join(", ")) + (preview.scoring.bonus_keywords.length > 6 ? "…" : "") : "none"}</li>`,
    ].join("");
    $("#obNotes").value = preview.notes_for_claude;
    $("#obSearchesSummary").textContent = `${preview.searches.length} searches for the chosen areas` +
      (preview.searches.length ? "" : " (none: typed-in areas need a search URL in Settings)");
    $("#obSearches").innerHTML = preview.searches.map(s => `<li>${esc(s.name)} <span class="muted">(${s.max_pages} pages)</span></li>`).join("");
    $("#obReplace").disabled = !preview.searches.length;
    if (!preview.searches.length) $("#obReplace").checked = false;
  }

  async function next() {
    readInputs();
    const err = stepError();
    if (err) { $("#obError").textContent = err; $("#obError").hidden = false; return; }
    if (step < STEPS - 1) {
      step++;
      if (step === STEPS - 1) {
        try { await buildPreview(); } catch (e) { step--; $("#obError").textContent = ((e.body && e.body.errors) || [e.message]).join(" "); $("#obError").hidden = false; return; }
      }
      render();
      return;
    }
    await finish();
  }

  async function finish() {
    const save = $("#obSave").checked, replace = $("#obReplace").checked, start = $("#obStart").checked;
    const notes = $("#obNotes").value;
    $("#obNext").disabled = true;
    try {
      const r = await App.api("/api/onboarding/apply", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answers: a, notes, save, replace_searches: replace }) });
      if (save) App.setSessionGoal(null);
      else App.setSessionGoal({ answers: a, notes, replace_searches: replace, overrides: r.overrides });
      localStorage.setItem("onboardingSeen", "1");
      await App.loadSettings();
      close();
      if (start) App.startRun(); else App.showTab(save ? "settings" : "run");
    } catch (e) {
      $("#obError").textContent = ((e.body && e.body.errors) || [e.message]).join(" "); $("#obError").hidden = false;
    } finally { $("#obNext").disabled = false; }
  }

  function close() { $("#onboarding").hidden = true; document.body.classList.remove("modal-open"); }

  async function open() {
    const st = await App.api("/api/onboarding");
    catalog = st.catalog;
    const sess = App.sessionGoal();
    a = Object.assign(blank(), (sess && sess.answers) || st.answers || fromConfig(App.getConfig()));
    if (!a.priorities || !a.priorities.length) a.priorities = ["value"];
    step = 0;
    $("#obSave").checked = true; $("#obStart").checked = true;
    $("#obReplace").checked = !st.done;            // first time: replace the sample searches
    $("#onboarding").hidden = false; document.body.classList.add("modal-open");
    render();
    $(".choice[data-goal]").focus();
  }

  // ---------------------------------------------------------------- events
  $$(".choice[data-goal]").forEach(c => c.addEventListener("click", () => {
    if (a.goal && a.goal !== c.dataset.goal) { a.budget_max = null; a.budget_min = null; }   // rent vs price scale
    a.goal = c.dataset.goal; render(); next();
  }));
  $("#obNext").addEventListener("click", next);
  $("#obBack").addEventListener("click", () => { readInputs(); if (step > 0) { step--; render(); } });
  $("#obSkip").addEventListener("click", () => { localStorage.setItem("onboardingSeen", "1"); close(); });
  ["#obSave", "#obStart"].forEach(id => $(id).addEventListener("change", render));
  $("#onboarding").addEventListener("keydown", e => {
    if (e.key === "Escape") $("#obSkip").click();
    if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); next(); }
  });
  $("#btnGoal").addEventListener("click", open);

  window.Onboarding = {
    open,
    async maybeShow() {
      const st = await App.api("/api/onboarding");
      if (!st.done && !localStorage.getItem("onboardingSeen")) open();
    },
  };
})();
