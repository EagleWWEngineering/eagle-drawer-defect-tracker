/* app.js - shared helpers used by every page template.
 *
 * Page-specific behavior lives in a small <script> block at the bottom of each
 * template (dashboard.html, defect_entry.html, ...). Everything reusable across
 * pages - toasts, badges, form-error rendering, the role selector, double-submit
 * protection - lives here so it's written once.
 */

function showToast(message, kind = "info") {
  let region = document.getElementById("toast-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "toast-region";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.appendChild(region);
  }
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.textContent = message;
  region.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

/** Priority always renders as an icon + a text label, never color alone. */
function priorityBadge(priority) {
  const slug = (priority || "").toLowerCase().replace(/\s+/g, "-");
  return `<span class="badge badge-priority-${slug}">${escapeHtml(priority)}</span>`;
}

function statusBadge(status) {
  const slug = (status || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return `<span class="badge badge-status badge-status-${slug}">${escapeHtml(status)}</span>`;
}

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatRate(value) {
  return value === null || value === undefined ? "N/A" : `${value.toFixed(1)}%`;
}

/** "$1,234.56". Returns "N/A" for null/undefined (e.g. cost on a row that predates
 * cost tracking and was never re-saved) so it's never confused with a real $0. */
function formatCurrency(value) {
  if (value === null || value === undefined) return "N/A";
  return `$${value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** "5 minutes ago", "2 hours ago", "3 days ago" - for sync-status style displays. */
function timeAgo(isoString) {
  if (!isoString) return "never";
  const diffMs = Date.now() - new Date(isoString).getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** Clears previous field errors in a form, then shows a new one next to its field. */
function clearFormErrors(form) {
  form.querySelectorAll(".field-error").forEach((el) => el.remove());
  form.querySelectorAll(".has-error").forEach((el) => el.classList.remove("has-error"));
}

function showFieldError(form, fieldName, message) {
  const field = fieldName && form.querySelector(`[name="${fieldName}"]`);
  if (field) {
    const wrapper = field.closest(".field") || field.parentElement;
    wrapper.classList.add("has-error");
    const p = document.createElement("p");
    p.className = "field-error";
    p.textContent = message;
    wrapper.appendChild(p);
    field.focus();
  } else {
    showToast(message, "error");
  }
}

/** Prevents a double-click / double-tap from submitting the same form twice. */
function guardDoubleSubmit(button, action) {
  return async (...args) => {
    if (button.dataset.submitting === "1") return;
    button.dataset.submitting = "1";
    button.disabled = true;
    try {
      return await action(...args);
    } finally {
      button.dataset.submitting = "0";
      button.disabled = false;
    }
  };
}

/** Wires up bulk row-select + the shared floating action bar (#bulk-action-bar in
 * base.html) for one table. Call the returned refresh() after every re-render of
 * the table body, since its row checkboxes are regenerated each time.
 *
 * options.tableBodySelector - CSS selector for the <tbody> whose rows carry
 *   `<input class="row-check" data-id="...">` checkboxes in their first column.
 * options.selectAllId - id of that table's header "select all" checkbox.
 * options.entityLabel - singular noun for confirm/success messages (e.g. "case").
 * options.onDelete(ids) - must soft-delete the given ids via the API.
 * options.onDeleted() - called after a successful delete to reload the table. */
function initBulkSelect({ tableBodySelector, selectAllId, entityLabel = "item", onDelete, onDeleted }) {
  const bar = document.getElementById("bulk-action-bar");
  const countEl = document.getElementById("bulk-action-count");
  const deleteBtn = document.getElementById("bulk-delete-btn");
  const clearBtn = document.getElementById("bulk-clear-btn");
  const selectAll = selectAllId ? document.getElementById(selectAllId) : null;

  function rowCheckboxes() {
    return Array.from(document.querySelectorAll(`${tableBodySelector} input.row-check`));
  }

  function selectedIds() {
    return rowCheckboxes()
      .filter((cb) => cb.checked)
      .map((cb) => Number(cb.dataset.id));
  }

  function updateBar() {
    const ids = selectedIds();
    const boxes = rowCheckboxes();
    if (ids.length > 0) {
      bar.style.display = "flex";
      countEl.textContent = `${ids.length} selected`;
    } else {
      bar.style.display = "none";
    }
    if (selectAll) {
      selectAll.checked = boxes.length > 0 && ids.length === boxes.length;
      selectAll.indeterminate = ids.length > 0 && ids.length < boxes.length;
    }
  }

  function refresh() {
    rowCheckboxes().forEach((cb) => {
      cb.addEventListener("change", updateBar);
      cb.addEventListener("click", (e) => e.stopPropagation());
    });
    updateBar();
  }

  if (selectAll) {
    selectAll.addEventListener("change", () => {
      rowCheckboxes().forEach((cb) => {
        cb.checked = selectAll.checked;
      });
      updateBar();
    });
  }

  clearBtn.addEventListener("click", () => {
    rowCheckboxes().forEach((cb) => {
      cb.checked = false;
    });
    updateBar();
  });

  deleteBtn.addEventListener(
    "click",
    guardDoubleSubmit(deleteBtn, async () => {
      const ids = selectedIds();
      if (!ids.length) return;
      const label = ids.length === 1 ? entityLabel : `${entityLabel}s`;
      if (!confirm(`Delete ${ids.length} selected ${label}? This can be undone by an admin.`)) return;
      try {
        await onDelete(ids);
        showToast(`${ids.length} ${label} deleted.`, "success");
        if (onDeleted) await onDeleted();
      } catch (err) {
        showToast(err.message, "error");
      }
    })
  );

  return { refresh };
}

/** Wires every `<button data-range="...">` on the page (the shared preset row
 * rendered by app/templates/_date_presets.html) to resolve that preset via
 * GET /api/v1/reports/date-preset (Api.getDatePreset - see
 * app/timezone_utils.py resolve_date_preset / app/services/working_days_
 * service.py resolve_working_day_preset), write the result into the given
 * start/end date inputs, then call onApply() to re-run the page's own reload.
 *
 * Extracted from the Dashboard (its original, page-specific version) so the
 * Dashboard and Reports pages share exactly one button row and exactly one
 * click handler instead of two copies that could drift apart. Presets set
 * ONLY the two named date inputs - onApply() must be the page's existing
 * reload (reads its own filter-form fresh), so every other filter already set
 * on the page is picked up untouched, never reset. */
function initDatePresetButtons({ startInputId, endInputId, onApply }) {
  document.querySelectorAll("[data-range]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const range = await Api.getDatePreset(btn.dataset.range);
        document.getElementById(startInputId).value = range.start_date;
        document.getElementById(endInputId).value = range.end_date;
        await onApply();
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  });
}

function initRoleSelector() {
  const select = document.getElementById("actor-role-select");
  if (!select) return;
  select.value = getActorRole();
  select.addEventListener("change", () => setActorRole(select.value));
}

function highlightActiveNavLink() {
  const links = document.querySelectorAll(".app-nav a");
  const path = window.location.pathname;
  links.forEach((a) => {
    if (a.getAttribute("href") === path) a.classList.add("active");
  });
}

/* ---------------------------------------------------------------------- */
/* Case detail modal - shared across Reports, Rework Queue, and the        */
/* Dashboard rework preview so clicking a case number anywhere opens the   */
/* same view (full case info + photo upload) instead of dead-ending on a   */
/* "#" link. Markup lives once in base.html (#case-detail-modal).          */
/* ---------------------------------------------------------------------- */

function openCaseDetailModal() {
  const modal = document.getElementById("case-detail-modal");
  if (!modal) return;
  modal.style.display = "flex";
  document.body.style.overflow = "hidden";
}

function closeCaseDetailModal() {
  const modal = document.getElementById("case-detail-modal");
  if (!modal) return;
  modal.style.display = "none";
  document.body.style.overflow = "";
}

async function renderCaseDetail(caseId) {
  const c = await Api.getDefectCase(caseId);
  document.getElementById("case-detail-title").textContent = `Case ${c.case_number}`;

  const itemsHtml =
    c.items
      .map(
        (i) =>
          `<li>${escapeHtml(i.defect_category_name)} — ${i.affected_drawer_quantity} drawer${i.affected_drawer_quantity === 1 ? "" : "s"}${i.notes ? ` — ${escapeHtml(i.notes)}` : ""}</li>`
      )
      .join("") || "<li>No defect items.</li>";

  const historyHtml =
    c.status_history
      .map(
        (h) =>
          `<li>${h.from_status ? `${escapeHtml(h.from_status)} &rarr; ` : ""}${escapeHtml(h.to_status)} <span class="hint">(${escapeHtml(h.changed_at_local || "")})</span>${h.note ? ` — ${escapeHtml(h.note)}` : ""}</li>`
      )
      .join("") || "<li>No status changes yet.</li>";

  const photosHtml = c.photos.length
    ? `<div class="photo-grid">${c.photos
        .map(
          (p) =>
            `<a href="${p.url}" target="_blank" rel="noopener" class="photo-thumb"><img src="${p.url}" alt="${escapeHtml(p.original_filename)}" loading="lazy"></a>`
        )
        .join("")}</div>`
    : `<p class="hint">No photos uploaded yet.</p>`;

  document.getElementById("case-detail-body").innerHTML = `
    <div class="case-detail-meta">
      ${priorityBadge(c.priority)} ${statusBadge(c.status)}
      <p><strong>Work order:</strong> ${escapeHtml(c.work_order_number)}${c.drawer_part_reference ? ` — ${escapeHtml(c.drawer_part_reference)}` : ""}</p>
      <p><strong>Detected:</strong> ${escapeHtml(c.detected_at_local || "")} &nbsp; <strong>Production date:</strong> ${c.production_date}</p>
      <p><strong>Found station:</strong> ${escapeHtml(c.found_station_name)} &nbsp; <strong>Possible source station:</strong> ${escapeHtml(c.possible_source_station_name || "unknown")}</p>
      ${c.disposition ? `<p><strong>Disposition:</strong> ${escapeHtml(c.disposition)}</p>` : ""}
    </div>

    <h3>Defect items</h3>
    <ul>${itemsHtml}</ul>

    ${c.root_cause ? `<p><strong>Root cause:</strong> ${escapeHtml(c.root_cause)}</p>` : ""}
    ${c.corrective_action ? `<p><strong>Corrective action:</strong> ${escapeHtml(c.corrective_action)}</p>` : ""}
    ${c.repair_action ? `<p><strong>Repair action:</strong> ${escapeHtml(c.repair_action)}</p>` : ""}
    ${c.notes ? `<p><strong>Notes:</strong> ${escapeHtml(c.notes)}</p>` : ""}

    <h3>Status history</h3>
    <ul>${historyHtml}</ul>

    <h3>Photos</h3>
    ${photosHtml}
    <form id="case-photo-upload-form" class="case-photo-upload">
      <div class="field">
        <label for="case-photo-file">Add a photo</label>
        <input type="file" id="case-photo-file" name="file" accept="image/jpeg,image/png,image/webp" required>
      </div>
      <button type="submit" class="secondary">Upload photo</button>
    </form>
  `;

  const uploadForm = document.getElementById("case-photo-upload-form");
  const uploadBtn = uploadForm.querySelector("button");
  uploadForm.addEventListener(
    "submit",
    guardDoubleSubmit(uploadBtn, async (e) => {
      e.preventDefault();
      const file = document.getElementById("case-photo-file").files[0];
      if (!file) return;
      try {
        await Api.uploadPhoto(caseId, file);
        showToast("Photo uploaded.", "success");
        await renderCaseDetail(caseId);
      } catch (err) {
        showToast(err.message, "error");
      }
    })
  );
}

/** Open the case detail modal for `caseId`. This is the one function every
 * case link on every page should call - see reports.html / rework_queue.html /
 * dashboard.html - instead of linking to "#" or elsewhere. */
async function openCaseDetail(caseId) {
  openCaseDetailModal();
  document.getElementById("case-detail-title").textContent = "Loading case…";
  document.getElementById("case-detail-body").innerHTML = "<p>Loading...</p>";
  try {
    await renderCaseDetail(caseId);
  } catch (err) {
    document.getElementById("case-detail-body").innerHTML = `<p class="hint">Could not load this case.</p>`;
    showToast(err.message, "error");
  }
}

/** Wires every `<a data-case="123">` inside `container` to open the case detail
 * modal instead of following its href. Call after any innerHTML re-render that
 * adds case links (Reports records table, Rework Queue, Dashboard preview). */
function wireCaseLinks(container) {
  container.querySelectorAll("a[data-case]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      openCaseDetail(Number(a.dataset.case));
    });
  });
}

function initCaseDetailModal() {
  const modal = document.getElementById("case-detail-modal");
  if (!modal) return;
  document.getElementById("case-detail-close").addEventListener("click", closeCaseDetailModal);
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeCaseDetailModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal.style.display !== "none") closeCaseDetailModal();
  });
}

window.openCaseDetail = openCaseDetail;
window.wireCaseLinks = wireCaseLinks;

document.addEventListener("DOMContentLoaded", () => {
  initRoleSelector();
  highlightActiveNavLink();
  initCaseDetailModal();
});
