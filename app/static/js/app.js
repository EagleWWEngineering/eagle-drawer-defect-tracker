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

document.addEventListener("DOMContentLoaded", () => {
  initRoleSelector();
  highlightActiveNavLink();
});
