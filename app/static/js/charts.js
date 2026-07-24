/* charts.js - minimal dependency-free <canvas> chart rendering.
 *
 * The app must work fully offline (PROJECT_SPEC.md section 8), so instead of
 * vendoring a third-party charting library, this file draws the two charts the
 * app needs (Pareto bars + cumulative line, and a day/week trend line) directly
 * with the Canvas 2D API. Every chart is paired with a plain HTML table in the
 * template so the same numbers are always readable as text, not just pixels.
 */

const CHART_COLORS = ["#1d4ed8", "#0f766e", "#b45309", "#7c3aed", "#c0392b", "#0891b2"];

function _setupCanvas(canvas, cssWidth, cssHeight) {
  const dpr = window.devicePixelRatio || 1;
  canvas.style.width = cssWidth + "px";
  canvas.style.height = cssHeight + "px";
  canvas.width = cssWidth * dpr;
  canvas.height = cssHeight * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cssWidth, cssHeight);
  return ctx;
}

/** rows: [{label, defect_events, cumulative_pct}] sorted desc by defect_events. */
function renderParetoChart(canvas, rows) {
  const width = Math.max(canvas.parentElement.clientWidth, rows.length * 90);
  const height = 320;
  const ctx = _setupCanvas(canvas, width, height);

  const padding = { top: 24, right: 50, bottom: 70, left: 50 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  if (rows.length === 0) {
    ctx.fillStyle = "#5b6472";
    ctx.font = "14px sans-serif";
    ctx.fillText("No defect events in the selected filters.", padding.left, padding.top + 20);
    return;
  }

  const maxCount = Math.max(...rows.map((r) => r.defect_events), 1);
  const barWidth = plotW / rows.length;

  ctx.strokeStyle = "#d4d8de";
  ctx.beginPath();
  ctx.moveTo(padding.left, padding.top);
  ctx.lineTo(padding.left, padding.top + plotH);
  ctx.lineTo(padding.left + plotW, padding.top + plotH);
  ctx.stroke();

  rows.forEach((row, i) => {
    const barH = (row.defect_events / maxCount) * plotH;
    const x = padding.left + i * barWidth + barWidth * 0.15;
    const y = padding.top + plotH - barH;
    const w = barWidth * 0.7;

    ctx.fillStyle = CHART_COLORS[0];
    ctx.fillRect(x, y, w, barH);

    ctx.fillStyle = "#1a1d21";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(String(row.defect_events), x + w / 2, y - 6);

    ctx.save();
    ctx.translate(x + w / 2, padding.top + plotH + 10);
    ctx.rotate(-Math.PI / 5);
    ctx.textAlign = "right";
    ctx.font = "11px sans-serif";
    const label = row.label.length > 22 ? row.label.slice(0, 21) + "…" : row.label;
    ctx.fillText(label, 0, 0);
    ctx.restore();
  });

  // Cumulative % line (secondary axis 0-100) with its own labeled points.
  ctx.strokeStyle = "#c0392b";
  ctx.lineWidth = 2;
  ctx.beginPath();
  rows.forEach((row, i) => {
    const x = padding.left + i * barWidth + barWidth / 2;
    const y = padding.top + plotH - (row.cumulative_pct / 100) * plotH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  ctx.fillStyle = "#c0392b";
  rows.forEach((row, i) => {
    const x = padding.left + i * barWidth + barWidth / 2;
    const y = padding.top + plotH - (row.cumulative_pct / 100) * plotH;
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
  });

  ctx.fillStyle = "#1a1d21";
  ctx.font = "12px sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("Bars = defect events (left axis)", padding.left, 14);
  ctx.fillStyle = "#c0392b";
  ctx.fillText("Line = cumulative % (right axis, 0-100)", padding.left + 230, 14);
}

/** points: [{period, defect_events, drawers_inspected, unique_drawers_rejected}] */
function renderTrendChart(canvas, points) {
  const width = Math.max(canvas.parentElement.clientWidth, points.length * 60);
  const height = 300;
  const ctx = _setupCanvas(canvas, width, height);

  const padding = { top: 30, right: 20, bottom: 60, left: 50 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  if (points.length === 0) {
    ctx.fillStyle = "#5b6472";
    ctx.font = "14px sans-serif";
    ctx.fillText("No data in the selected date range.", padding.left, padding.top + 20);
    return;
  }

  const series = [
    { key: "defect_events", label: "Defect events", color: CHART_COLORS[0] },
    { key: "drawers_inspected", label: "Drawers inspected", color: CHART_COLORS[1] },
    { key: "unique_drawers_rejected", label: "Unique drawers rejected", color: CHART_COLORS[4] },
  ];
  const maxVal = Math.max(1, ...points.flatMap((p) => series.map((s) => p[s.key])));

  ctx.strokeStyle = "#d4d8de";
  ctx.beginPath();
  ctx.moveTo(padding.left, padding.top);
  ctx.lineTo(padding.left, padding.top + plotH);
  ctx.lineTo(padding.left + plotW, padding.top + plotH);
  ctx.stroke();

  const stepX = points.length > 1 ? plotW / (points.length - 1) : 0;

  series.forEach((s) => {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = padding.left + i * stepX;
      const y = padding.top + plotH - (p[s.key] / maxVal) * plotH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });

  ctx.fillStyle = "#1a1d21";
  ctx.font = "11px sans-serif";
  ctx.textAlign = "center";
  points.forEach((p, i) => {
    const x = padding.left + i * stepX;
    ctx.save();
    ctx.translate(x, padding.top + plotH + 10);
    ctx.rotate(-Math.PI / 6);
    ctx.textAlign = "right";
    ctx.fillText(p.period, 0, 0);
    ctx.restore();
  });

  // Legend (text + color together - never color alone).
  let legendX = padding.left;
  series.forEach((s) => {
    ctx.fillStyle = s.color;
    ctx.fillRect(legendX, 4, 10, 10);
    ctx.fillStyle = "#1a1d21";
    ctx.textAlign = "left";
    ctx.font = "11px sans-serif";
    ctx.fillText(s.label, legendX + 14, 13);
    legendX += ctx.measureText(s.label).width + 40;
  });
}

window.renderParetoChart = renderParetoChart;
window.renderTrendChart = renderTrendChart;
