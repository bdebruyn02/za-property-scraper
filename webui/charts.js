/* Minimal SVG scatter + spline chart (no dependencies).
   Marks: 9px dots with a 2px surface ring, 2px spline, recessive grid, nearest-point hover. */
(function () {
  const NS = "http://www.w3.org/2000/svg";

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  function niceStep(span, target) {
    const raw = span / target, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
  }
  function ticks(lo, hi, target) {
    const step = niceStep(hi - lo || 1, target);
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
    return out;
  }
  function pct(sorted, q) {
    if (!sorted.length) return 0;
    const i = (sorted.length - 1) * q, lo = Math.floor(i), hi = Math.min(lo + 1, sorted.length - 1);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
  }

  /* Monotone cubic (Fritsch–Carlson) path through points sorted by x: smooth, no overshoot. */
  function splinePath(pts) {
    const n = pts.length;
    if (n < 2) return "";
    const dx = [], dy = [], m = [];
    for (let i = 0; i < n - 1; i++) { dx[i] = pts[i + 1][0] - pts[i][0]; dy[i] = pts[i + 1][1] - pts[i][1]; m[i] = dy[i] / dx[i]; }
    const t = [m[0]];
    for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
    t[n - 1] = m[n - 2];
    for (let i = 0; i < n - 1; i++) {
      if (m[i] === 0) { t[i] = 0; t[i + 1] = 0; continue; }
      const a = t[i] / m[i], b = t[i + 1] / m[i], h = a * a + b * b;
      if (h > 9) { const s = 3 / Math.sqrt(h); t[i] = s * a * m[i]; t[i + 1] = s * b * m[i]; }
    }
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < n - 1; i++) {
      const h = dx[i] / 3;
      d += ` C${pts[i][0] + h},${pts[i][1] + h * t[i]} ${pts[i + 1][0] - h},${pts[i + 1][1] - h * t[i + 1]} ${pts[i + 1][0]},${pts[i + 1][1]}`;
    }
    return d;
  }

  /**
   * opts: { points:[{x,y,cls,color,data}], curve:[[x,y]], band:{from,to,label},
   *         hline:{y,label}, xLabel, yFmt, xFmt, height, tooltip(data)->html, onClick(data) }
   */
  function scatter(container, opts) {
    container.innerHTML = "";
    const W = Math.max(360, container.clientWidth), H = opts.height || 380;
    const M = { l: 70, r: 18, t: 24, b: 42 };
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.ariaLabel || "chart" }, container);

    const xsAll = opts.points.map(p => p.x).concat(opts.curve.map(c => c[0])).sort((a, b) => a - b);
    const ysAll = opts.points.map(p => p.y).concat(opts.curve.map(c => c[1])).sort((a, b) => a - b);
    if (!xsAll.length) { el("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, svg).textContent = "No data with floor size yet"; return { hidden: 0 }; }
    // Trim extreme outliers so the interesting region isn't squashed; report how many are off-chart.
    let x0 = Math.min(pct(xsAll, 0.01), opts.curve.length ? opts.curve[0][0] : Infinity);
    let x1 = Math.max(pct(xsAll, 0.97), opts.curve.length ? opts.curve[opts.curve.length - 1][0] : -Infinity);
    let y0 = 0, y1 = Math.max(pct(ysAll, 0.97), opts.hline ? opts.hline.y * 1.08 : 0);
    const xpad = (x1 - x0) * 0.04; x0 = Math.max(0, x0 - xpad); x1 += xpad; y1 *= 1.05;
    const sx = v => M.l + (v - x0) / (x1 - x0) * (W - M.l - M.r);
    const sy = v => H - M.b - (v - y0) / (y1 - y0) * (H - M.t - M.b);

    // grid + axes
    for (const v of ticks(y0, y1, 6)) {
      el("line", { x1: M.l, x2: W - M.r, y1: sy(v), y2: sy(v), class: "gridline" }, svg);
      el("text", { x: M.l - 8, y: sy(v) + 4, "text-anchor": "end" }, svg).textContent = opts.yFmt(v);
    }
    for (const v of ticks(x0, x1, Math.min(10, Math.floor(W / 90)))) {
      el("text", { x: sx(v), y: H - M.b + 16, "text-anchor": "middle" }, svg).textContent = opts.xFmt ? opts.xFmt(v) : v;
    }
    el("line", { x1: M.l, x2: W - M.r, y1: sy(0), y2: sy(0), class: "axis" }, svg);
    el("text", { x: (M.l + W - M.r) / 2, y: H - 6, "text-anchor": "middle" }, svg).textContent = opts.xLabel;

    // sweet-spot band
    if (opts.band) {
      const bx0 = sx(Math.max(opts.band.from, x0)), bx1 = sx(Math.min(opts.band.to, x1));
      if (bx1 > bx0) {
        el("rect", { x: bx0, y: M.t, width: bx1 - bx0, height: H - M.t - M.b, class: "bandrect" }, svg);
        el("text", { x: (bx0 + bx1) / 2, y: M.t - 8, "text-anchor": "middle", class: "lbl-strong" }, svg).textContent = opts.band.label;
      }
    }
    // budget line
    if (opts.hline && opts.hline.y < y1) {
      el("line", { x1: M.l, x2: W - M.r, y1: sy(opts.hline.y), y2: sy(opts.hline.y), class: "budget" }, svg);
      el("text", { x: W - M.r - 4, y: sy(opts.hline.y) - 6, "text-anchor": "end" }, svg).textContent = opts.hline.label;
    }

    // points: filtered-out (grey) underneath, matches on top
    const visible = opts.points.filter(p => p.x >= x0 && p.x <= x1 && p.y >= y0 && p.y <= y1);
    const hidden = opts.points.length - visible.length;
    const ordered = visible.slice().sort((a, b) => (a.cls === "other") - (b.cls === "other")).reverse();
    const nodes = [];
    for (const p of ordered) {
      const c = el("circle", { cx: sx(p.x), cy: sy(p.y), r: p.cls === "other" ? 3.5 : 4.5,
                               class: "pt " + (p.cls || ""), fill: p.color || "var(--other)" }, svg);
      nodes.push({ p, c, X: sx(p.x), Y: sy(p.y) });
    }
    // spline on top of dots so it stays readable
    if (opts.curve.length > 1) {
      const pts = opts.curve.filter(c => c[0] >= x0 && c[0] <= x1).map(c => [sx(c[0]), sy(c[1])]);
      el("path", { d: splinePath(pts), class: "curve" }, svg);
      const last = pts[pts.length - 1];
      if (last && opts.curveLabel) {
        el("text", { x: last[0] - 4, y: last[1] - 8, "text-anchor": "end", class: "lbl-strong" }, svg).textContent = opts.curveLabel;
      }
    }

    // hover: nearest point within 24px (bigger hit target than the mark)
    const tip = document.getElementById("tooltip");
    let active = null;
    const overlay = el("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" }, svg);
    function nearest(evt) {
      const r = svg.getBoundingClientRect(), k = W / r.width;
      const mx = (evt.clientX - r.left) * k, my = (evt.clientY - r.top) * k;
      let best = null, bd = 24 * k;
      for (const n of nodes) { const d = Math.hypot(n.X - mx, n.Y - my); if (d < bd) { bd = d; best = n; } }
      return best;
    }
    overlay.addEventListener("mousemove", evt => {
      const n = nearest(evt);
      if (active && active !== n) active.c.classList.remove("hl");
      active = n;
      if (!n) { tip.hidden = true; overlay.style.cursor = "default"; return; }
      n.c.classList.add("hl");
      n.c.parentNode.appendChild(n.c); svg.appendChild(overlay);
      tip.innerHTML = opts.tooltip(n.p.data);
      tip.hidden = false;
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      let left = evt.clientX + 14, top = evt.clientY + 14;
      if (left + tw > window.innerWidth - 8) left = evt.clientX - tw - 14;
      if (top + th > window.innerHeight - 8) top = evt.clientY - th - 14;
      tip.style.left = left + "px"; tip.style.top = top + "px";
      overlay.style.cursor = n.p.data && n.p.data.url ? "pointer" : "default";
    });
    overlay.addEventListener("mouseleave", () => { tip.hidden = true; if (active) active.c.classList.remove("hl"); active = null; });
    overlay.addEventListener("click", evt => { const n = nearest(evt); if (n && opts.onClick) opts.onClick(n.p.data); });
    return { hidden };
  }

  window.Charts = { scatter };
})();
