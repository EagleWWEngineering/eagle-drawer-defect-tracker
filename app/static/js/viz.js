/* viz.js - small, dependency-free charts for the 2026-10 redesign (Reports and
 * the Quality Today dashboard). Plain SVG/HTML so they render offline, scale to
 * any width and read the theme from app.css tokens (var(--accent) etc.).
 *
 *   Viz.barList(el, rows, opts)      horizontal bars: [{label, value, cum?, delta?}]
 *   Viz.line(el, points, opts)       one line + optional dashed target, hover tooltip
 *   Viz.stacked(el, points, opts)    stacked columns (two series), hover tooltip
 *
 * Text always wears text tokens (never a series color), and every chart has a
 * text equivalent in its card (counts beside each bar, tooltips) - identity is
 * never color alone.
 */
(function () {
  const NS = "http://www.w3.org/2000/svg";

  function el(tag, attrs, parent) {
    const node = document.createElementNS(NS, tag);
    for (const key in attrs) node.setAttribute(key, attrs[key]);
    if (parent) parent.appendChild(node);
    return node;
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  function niceMax(value) {
    if (value <= 0) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(value)));
    const steps = [1, 2, 2.5, 5, 10];
    for (const s of steps) if (value <= s * pow) return s * pow;
    return 10 * pow;
  }

  function ensureTip(container) {
    let tip = container.querySelector(".chart-tip");
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "chart-tip";
      container.appendChild(tip);
    }
    return tip;
  }

  /** rows: [{label, value, cum (0-100, optional), delta (optional, + = worse),
   *  vital (bold), key (passed to onClick)}]. opts: {head: [name, value, cum],
   *  valueFormat, onClick(row), empty}. */
  function barList(container, rows, opts = {}) {
    if (!rows.length) {
      container.innerHTML = `<p class="hint">${esc(opts.empty || "Nothing to show for these filters.")}</p>`;
      return;
    }
    const max = Math.max(...rows.map((r) => r.value), 0) || 1;
    const fmt = opts.valueFormat || ((v) => v);
    const showCum = rows.some((r) => r.cum !== undefined && r.cum !== null);
    const head = opts.head
      ? `<div class="bar-row head"><span>${esc(opts.head[0])}</span><span></span><span style="text-align:right">${esc(opts.head[1] || "")}</span><span style="text-align:right">${showCum ? esc(opts.head[2] || "Cum.") : ""}</span></div>`
      : "";
    container.innerHTML =
      `<div class="bar-list">${head}` +
      rows
        .map((r, i) => {
          const delta =
            r.delta === undefined || r.delta === null
              ? ""
              : r.delta > 0
                ? `<span class="up" title="vs the period before">▲${r.delta}</span>`
                : r.delta < 0
                  ? `<span class="down" title="vs the period before">▼${-r.delta}</span>`
                  : "";
          const color = r.color ? `background:${r.color};` : "";
          return `<div class="bar-row${r.vital ? " vital" : ""}${opts.onClick ? " clickable" : ""}" data-i="${i}" title="${esc(r.label)}: ${esc(fmt(r.value))}">
            <span class="name">${esc(r.label)}</span>
            <span class="track"><span style="width:${((r.value / max) * 100).toFixed(1)}%;${color}"></span></span>
            <span class="num">${esc(fmt(r.value))}${delta}</span>
            <span class="cum">${showCum && r.cum !== undefined && r.cum !== null ? Math.round(r.cum) + "%" : ""}</span>
          </div>`;
        })
        .join("") +
      "</div>";
    if (opts.onClick) {
      container.querySelectorAll(".bar-row[data-i]").forEach((row) =>
        row.addEventListener("click", () => opts.onClick(rows[Number(row.dataset.i)]))
      );
    }
  }

  /** points: [{label, value (number|null), tip (html, optional)}].
   *  opts: {target (number|null), targetLabel, height, valueFormat, empty, ariaLabel, hover (default true)} */
  function line(container, points, opts = {}) {
    container.classList.add("svg-chart");
    container.innerHTML = "";
    const pts = points.filter((p) => p.value !== null && p.value !== undefined);
    if (!pts.length) {
      container.innerHTML = `<p class="hint">${esc(opts.empty || "No data for these filters.")}</p>`;
      return;
    }
    const W = 640, H = opts.height || 240, L = 40, R = 16, T = 16, B = 30;
    const fmt = opts.valueFormat || ((v) => v.toFixed(1));
    const top = niceMax(Math.max(...pts.map((p) => p.value), opts.target || 0) * 1.1);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.ariaLabel || "Trend chart" });
    container.appendChild(svg);
    const n = points.length;
    const x = (i) => (n === 1 ? (L + W - R) / 2 : L + (i * (W - L - R)) / (n - 1));
    const y = (v) => T + (1 - v / top) * (H - T - B);
    for (let k = 0; k <= 4; k++) {
      const v = (top * k) / 4;
      el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }, svg);
      el("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = Number.isInteger(v) ? v : v.toFixed(1);
    }
    const every = Math.max(1, Math.ceil(n / 7));
    points.forEach((p, i) => {
      if (i % every === 0 || i === n - 1) {
        const anchor = n > 1 && i === 0 ? "start" : n > 1 && i === n - 1 ? "end" : "middle";
        el("text", { x: x(i), y: H - 8, "text-anchor": anchor }, svg).textContent = p.label;
      }
    });
    if (opts.target !== null && opts.target !== undefined) {
      el("line", { x1: L, x2: W - R, y1: y(opts.target), y2: y(opts.target), stroke: "var(--good)", "stroke-width": 1.5, "stroke-dasharray": "6 5" }, svg);
      el("text", { x: W - R, y: y(opts.target) - 6, "text-anchor": "end", style: "fill:var(--good);font-weight:600" }, svg).textContent =
        opts.targetLabel || `Target ${opts.target}`;
    }
    // Gaps (null days) break the line instead of drawing through them.
    let segment = [];
    const segments = [];
    points.forEach((p, i) => {
      if (p.value === null || p.value === undefined) {
        if (segment.length) segments.push(segment);
        segment = [];
      } else segment.push([x(i), y(p.value)]);
    });
    if (segment.length) segments.push(segment);
    segments.forEach((seg) => {
      const d = seg.map((pt) => pt.join(",")).join(" ");
      if (seg.length > 1) {
        el("polygon", { points: `${seg[0][0]},${y(0)} ${d} ${seg[seg.length - 1][0]},${y(0)}`, fill: "var(--accent)", opacity: 0.08 }, svg);
        el("polyline", { points: d, fill: "none", stroke: "var(--accent)", "stroke-width": 2, "stroke-linejoin": "round" }, svg);
      }
    });
    if (n <= 12) {
      points.forEach((p, i) => {
        if (p.value !== null && p.value !== undefined)
          el("circle", { cx: x(i), cy: y(p.value), r: 4, fill: "var(--accent)", stroke: "var(--surface)", "stroke-width": 2 }, svg);
      });
    }
    const lastIndex = points.map((p) => p.value !== null && p.value !== undefined).lastIndexOf(true);
    if (lastIndex >= 0) {
      const lv = points[lastIndex].value;
      el("circle", { cx: x(lastIndex), cy: y(lv), r: 5, fill: "var(--accent)", stroke: "var(--surface)", "stroke-width": 2 }, svg);
      el("text", { x: x(lastIndex) - 8, y: y(lv) - 10, "text-anchor": "end", style: "fill:var(--fg);font-weight:600" }, svg).textContent = fmt(lv);
    }
    if (opts.hover === false) return;
    const tip = ensureTip(container);
    const cross = el("line", { y1: T, y2: H - B, stroke: "var(--muted)", "stroke-width": 1, opacity: 0 }, svg);
    const dot = el("circle", { r: 5, fill: "var(--accent)", stroke: "var(--surface)", "stroke-width": 2, opacity: 0 }, svg);
    const hit = el("rect", { x: L - 10, y: T, width: W - L - R + 20, height: H - T - B, fill: "transparent" }, svg);
    hit.addEventListener("pointermove", (ev) => {
      const r = svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) * W) / r.width;
      const i = n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round(((px - L) * (n - 1)) / (W - L - R))));
      const p = points[i];
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      cross.setAttribute("opacity", 0.5);
      if (p.value !== null && p.value !== undefined) {
        dot.setAttribute("cx", x(i));
        dot.setAttribute("cy", y(p.value));
        dot.setAttribute("opacity", 1);
      } else dot.setAttribute("opacity", 0);
      tip.innerHTML = p.tip || `<b>${esc(p.label)}</b> · ${p.value === null || p.value === undefined ? "no data" : esc(fmt(p.value))}`;
      tip.style.left = (x(i) * r.width) / W + "px";
      tip.style.top = (y(p.value ?? 0) * r.height) / H + "px";
      tip.classList.add("on");
    });
    hit.addEventListener("pointerleave", () => {
      tip.classList.remove("on");
      cross.setAttribute("opacity", 0);
      dot.setAttribute("opacity", 0);
    });
  }

  /** points: [{label, a, b}] stacked a (bottom) + b (top). opts: {colors:[a,b], names:[a,b], height, hover} */
  function stacked(container, points, opts = {}) {
    container.classList.add("svg-chart");
    container.innerHTML = "";
    if (!points.length) {
      container.innerHTML = `<p class="hint">${esc(opts.empty || "Nothing to show.")}</p>`;
      return;
    }
    const W = 640, H = opts.height || 220, L = 32, R = 8, T = 10, B = 30;
    const colors = opts.colors || ["var(--s-asm)", "var(--s-qc)"];
    const names = opts.names || ["A", "B"];
    const top = niceMax(Math.max(...points.map((p) => p.a + p.b), 1));
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opts.ariaLabel || "Stacked chart" });
    container.appendChild(svg);
    const y = (v) => T + (1 - v / top) * (H - T - B);
    const bw = (W - L - R) / points.length;
    const barW = Math.min(bw * 0.56, 46);
    for (let k = 0; k <= 4; k++) {
      const v = (top * k) / 4;
      el("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }, svg);
      el("text", { x: L - 8, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = Number.isInteger(v) ? v : v.toFixed(1);
    }
    const tip = opts.hover === false ? null : ensureTip(container);
    const every = Math.max(1, Math.ceil(points.length / 10));
    points.forEach((p, i) => {
      const cx = L + bw * i + bw / 2;
      const x0 = cx - barW / 2;
      const aTop = y(p.a);
      const bTop = y(p.a + p.b);
      if (p.a > 0) el("rect", { x: x0, y: aTop, width: barW, height: y(0) - aTop, fill: colors[0] }, svg);
      if (p.b > 0) {
        // 2px surface gap between the segments; rounded data end on top.
        const r = Math.min(4, (aTop - 2 - bTop) / 2);
        el("path", {
          d: `M${x0},${aTop - 2} V${bTop + r} Q${x0},${bTop} ${x0 + r},${bTop} H${x0 + barW - r} Q${x0 + barW},${bTop} ${x0 + barW},${bTop + r} V${aTop - 2} Z`,
          fill: colors[1],
        }, svg);
      }
      if (i % every === 0 || i === points.length - 1)
        el("text", { x: cx, y: H - 8, "text-anchor": "middle" }, svg).textContent = p.label;
      if (tip) {
        const hit = el("rect", { x: L + bw * i, y: T, width: bw, height: H - T - B, fill: "transparent" }, svg);
        hit.addEventListener("pointerenter", () => {
          const r = svg.getBoundingClientRect();
          tip.innerHTML = `<b>${esc(p.label)}</b> · ${esc(names[0])} ${p.a} · ${esc(names[1])} ${p.b}`;
          tip.style.left = (cx * r.width) / W + "px";
          tip.style.top = (bTop * r.height) / H + "px";
          tip.classList.add("on");
        });
        hit.addEventListener("pointerleave", () => tip.classList.remove("on"));
      }
    });
  }

  window.Viz = { barList, line, stacked };
})();
