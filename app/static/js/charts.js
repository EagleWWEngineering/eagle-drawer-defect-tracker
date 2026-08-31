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

/** Shared multi-series line chart renderer used by renderTrendChart and
 * renderCostTrendChart. series: [{key, label, color}]. valueFormatter formats
 * each point label under the x-axis stays the same ("period" field); values are
 * read from point[series.key]. */
function _renderLineChart(canvas, points, series, { emptyMessage = "No data in the selected date range." } = {}) {
  const width = Math.max(canvas.parentElement.clientWidth, points.length * 60);
  const height = 300;
  const ctx = _setupCanvas(canvas, width, height);

  const padding = { top: 30, right: 20, bottom: 60, left: 50 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  if (points.length === 0) {
    ctx.fillStyle = "#5b6472";
    ctx.font = "14px sans-serif";
    ctx.fillText(emptyMessage, padding.left, padding.top + 20);
    return;
  }

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

/** points: [{period, defect_events, drawers_inspected, unique_drawers_rejected}] */
function renderTrendChart(canvas, points) {
  _renderLineChart(canvas, points, [
    { key: "defect_events", label: "Defect events", color: CHART_COLORS[0] },
    { key: "drawers_inspected", label: "Drawers inspected", color: CHART_COLORS[1] },
    { key: "unique_drawers_rejected", label: "Unique drawers rejected", color: CHART_COLORS[4] },
  ]);
}

/** points: [{period, internal_rework_cost, cost_avoided}] (Phase 7 "Cost model" -
 * one cost unit per defect case). Scrap cost was dropped from this app entirely -
 * see docs/PROJECT_SPEC_PHASE4.md "Scrap removal". */
function renderCostTrendChart(canvas, points) {
  _renderLineChart(
    canvas,
    points,
    [
      { key: "internal_rework_cost", label: "Internal rework cost ($)", color: CHART_COLORS[2] },
      { key: "cost_avoided", label: "Cost avoided ($)", color: CHART_COLORS[1] },
    ],
    { emptyMessage: "No cost data in the selected date range." }
  );
}

/** Phase 6: Dashboard "Scheduled vs Completed" card - one pair of bars per day.
 * days: [{production_date, drawers_scheduled: int|null, drawers_inspected: int,
 * is_working_day: bool}]. Scheduled renders muted (grey), Completed/Inspected
 * renders primary (blue), per PROJECT_SPEC.md Phase 6 addendum 5b. A day with
 * drawers_scheduled === null (no daily_schedules row - unknown, never assumed
 * 0) draws no Scheduled bar at all and labels that slot "—" instead of "0" -
 * the accompanying HTML table (same rule as every other chart in this file) is
 * what actually distinguishes "unknown" from "a real zero" in text, since a
 * 0-height bar looks the same either way.
 *
 * Working Days Logic (Part C addendum): a weekend day never reaches this
 * function at all (the API drops it entirely). A weekday holiday/shutdown
 * arrives with is_working_day: false and draws as one flat grey "no
 * production" bar instead of the normal Scheduled/Completed pair - a blank gap
 * would otherwise read as "the tracker is broken" rather than "nothing was
 * scheduled that day". */
function renderScheduleVsCompletedChart(canvas, days) {
  const width = Math.max(canvas.parentElement.clientWidth, days.length * 90);
  const height = 300;
  const ctx = _setupCanvas(canvas, width, height);

  const padding = { top: 30, right: 20, bottom: 60, left: 50 };
  const plotW = width - padding.left - padding.right;
  const plotH = height - padding.top - padding.bottom;

  if (days.length === 0) {
    ctx.fillStyle = "#5b6472";
    ctx.font = "14px sans-serif";
    ctx.fillText("No dates in the selected range.", padding.left, padding.top + 20);
    return;
  }

  const maxVal = Math.max(
    1,
    ...days.map((d) => d.drawers_scheduled || 0),
    ...days.map((d) => d.drawers_inspected || 0)
  );

  ctx.strokeStyle = "#d4d8de";
  ctx.beginPath();
  ctx.moveTo(padding.left, padding.top);
  ctx.lineTo(padding.left, padding.top + plotH);
  ctx.lineTo(padding.left + plotW, padding.top + plotH);
  ctx.stroke();

  const slot = plotW / days.length;
  const barW = Math.min(28, slot * 0.32);
  const scheduledColor = "#9aa5b1";
  const completedColor = CHART_COLORS[0];
  const noProductionColor = "#e3e6ea";

  days.forEach((d, i) => {
    const slotX = padding.left + i * slot + slot / 2;

    if (d.is_working_day === false) {
      // Weekday holiday/shutdown: one flat grey bar, no Scheduled/Completed
      // split - a real absence of production, not missing data.
      const flagH = Math.max(6, plotH * 0.06);
      ctx.fillStyle = noProductionColor;
      ctx.fillRect(slotX - barW, padding.top + plotH - flagH, barW * 2, flagH);
      ctx.fillStyle = "#5b6472";
      ctx.font = "italic 10px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("no production", slotX, padding.top + plotH - flagH - 6);
    } else {
      if (d.drawers_scheduled !== null && d.drawers_scheduled !== undefined) {
        const h = (d.drawers_scheduled / maxVal) * plotH;
        ctx.fillStyle = scheduledColor;
        ctx.fillRect(slotX - barW - 2, padding.top + plotH - h, barW, h);
      }
      const inspectedH = (d.drawers_inspected / maxVal) * plotH;
      ctx.fillStyle = completedColor;
      ctx.fillRect(slotX + 2, padding.top + plotH - inspectedH, barW, inspectedH);

      ctx.fillStyle = "#1a1d21";
      ctx.font = "11px sans-serif";
      ctx.textAlign = "center";
      const scheduledLabel = d.drawers_scheduled === null || d.drawers_scheduled === undefined ? "—" : String(d.drawers_scheduled);
      ctx.fillText(scheduledLabel, slotX - barW / 2 - 2, padding.top + plotH - Math.max(0, (d.drawers_scheduled || 0) / maxVal * plotH) - 6);
      ctx.fillText(String(d.drawers_inspected), slotX + barW / 2 + 2, padding.top + plotH - inspectedH - 6);
    }

    ctx.save();
    ctx.translate(slotX, padding.top + plotH + 10);
    ctx.rotate(-Math.PI / 6);
    ctx.font = "11px sans-serif";
    ctx.textAlign = "right";
    ctx.fillStyle = d.is_working_day === false ? "#9aa5b1" : "#1a1d21";
    ctx.fillText(d.production_date, 0, 0);
    ctx.restore();
  });

  let legendX = padding.left;
  [
    { label: "Scheduled", color: scheduledColor },
    { label: "Completed (inspected)", color: completedColor },
  ].forEach((s) => {
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
window.renderCostTrendChart = renderCostTrendChart;
window.renderScheduleVsCompletedChart = renderScheduleVsCompletedChart;
