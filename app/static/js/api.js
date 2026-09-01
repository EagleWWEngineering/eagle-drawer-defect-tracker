/* api.js - thin wrapper around fetch() for the /api/v1/* JSON API.
 *
 * Every page's JavaScript talks to the backend ONLY through the functions in this
 * file. That keeps "how do I call the API" in one place, matching the same rule
 * the backend follows (UI -> API -> service layer -> database).
 */

/* PROTOTYPE role selector (see app/dependencies.py get_actor_role): this header is
 * only used to label who did what in the audit log for this single-user pilot.
 * It is NOT a security boundary - anyone can change it. */
const ACTOR_ROLE_KEY = "eagle_defect_tracker_actor_role";

function getActorRole() {
  return localStorage.getItem(ACTOR_ROLE_KEY) || "QC";
}

function setActorRole(role) {
  localStorage.setItem(ACTOR_ROLE_KEY, role);
}

class ApiError extends Error {
  constructor(message, field, status) {
    super(message);
    this.field = field || null;
    this.status = status;
  }
}

function buildQueryString(params) {
  if (!params) return "";
  const usp = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    usp.set(key, value);
  });
  const qs = usp.toString();
  return qs ? `?${qs}` : "";
}

async function request(method, path, { params, body, isForm } = {}) {
  const url = path + buildQueryString(params);
  const headers = { "X-Actor-Role": getActorRole() };
  let payload = body;
  if (body !== undefined && !isForm) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(url, { method, headers, body: payload });
  } catch (networkErr) {
    throw new ApiError(
      "Could not reach the Eagle Drawer Defect Tracker server. Is it running?",
      null,
      0
    );
  }

  if (response.status === 204) return null;

  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();

  if (!response.ok) {
    if (data && typeof data === "object" && data.error) {
      throw new ApiError(data.error.message, data.error.field, response.status);
    }
    if (data && typeof data === "object" && data.detail) {
      throw new ApiError(
        typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail),
        null,
        response.status
      );
    }
    throw new ApiError(`Request failed (HTTP ${response.status}).`, null, response.status);
  }
  return data;
}

const Api = {
  get: (path, params) => request("GET", path, { params }),
  post: (path, body, params) => request("POST", path, { body, params }),
  patch: (path, body, params) => request("PATCH", path, { body, params }),
  put: (path, body, params) => request("PUT", path, { body, params }),
  delete: (path, params) => request("DELETE", path, { params }),

  uploadPhoto: async (caseId, file) => {
    const formData = new FormData();
    formData.append("file", file);
    return request("POST", `/api/v1/defect-cases/${caseId}/photos`, {
      body: formData,
      isForm: true,
    });
  },

  // Master data
  getMasterData: () => request("GET", "/api/v1/master-data"),
  createStation: (payload) => request("POST", "/api/v1/master-data/stations", { body: payload }),
  updateStation: (id, payload) =>
    request("PATCH", `/api/v1/master-data/stations/${id}`, { body: payload }),
  createCategory: (payload) =>
    request("POST", "/api/v1/master-data/defect-categories", { body: payload }),
  updateCategory: (id, payload) =>
    request("PATCH", `/api/v1/master-data/defect-categories/${id}`, { body: payload }),

  // Cost settings (Phase 4)
  getCostPerDrawer: () => request("GET", "/api/v1/settings/cost-per-drawer"),
  updateCostPerDrawer: (costPerDrawer) =>
    request("PUT", "/api/v1/settings/cost-per-drawer", { body: { cost_per_drawer: costPerDrawer } }),

  // Defect cases
  getRecentWorkOrders: (limit) =>
    request("GET", "/api/v1/defect-cases/work-orders/recent", { params: { limit } }),
  getLastStationForWorkOrder: (workOrderNumber) =>
    request("GET", `/api/v1/defect-cases/work-orders/${encodeURIComponent(workOrderNumber)}/last-station`),
  createDefectCase: (payload) => request("POST", "/api/v1/defect-cases", { body: payload }),
  listDefectCases: (params) => request("GET", "/api/v1/defect-cases", { params }),
  getDefectCase: (id) => request("GET", `/api/v1/defect-cases/${id}`),
  updateDefectCase: (id, payload) =>
    request("PATCH", `/api/v1/defect-cases/${id}`, { body: payload }),
  changeStatus: (id, payload) =>
    request("POST", `/api/v1/defect-cases/${id}/status`, { body: payload }),
  softDeleteCase: (id) => request("DELETE", `/api/v1/defect-cases/${id}`),
  bulkDeleteDefectCases: (ids) => request("POST", "/api/v1/defect-cases/bulk-delete", { body: { ids } }),
  bulkRestoreDefectCases: (ids) => request("POST", "/api/v1/defect-cases/bulk-restore", { body: { ids } }),

  // Daily production
  upsertDailySummary: (date, payload) =>
    request("PUT", `/api/v1/daily-production/${date}`, { body: payload }),
  listDailySummaries: (params) => request("GET", "/api/v1/daily-production", { params }),

  // Daily schedule (Phase 6)
  getSchedule: (params) => request("GET", "/api/v1/daily-production/schedule", { params }),
  putSchedule: (payload) => request("PUT", "/api/v1/daily-production/schedule", { body: payload }),
  getScheduleAttainment: (params) =>
    request("GET", "/api/v1/daily-production/schedule-attainment", { params }),

  // Reports
  // Phase 6: resolves a Dashboard date-preset button (Today/Yesterday/Last 7
  // days/Last 30 days/Month to date) to {start_date, end_date} server-side, in
  // DISPLAY_TIMEZONE - see app/timezone_utils.py resolve_date_preset(). Never
  // recompute this boundary logic in JS - that's exactly the kind of drift this
  // call avoids.
  getDatePreset: (preset) => request("GET", "/api/v1/reports/date-preset", { params: { preset } }),
  getReworkQueue: (params) => request("GET", "/api/v1/rework-queue", { params }),
  getSummary: (params) => request("GET", "/api/v1/reports/summary", { params }),
  getPareto: (params) => request("GET", "/api/v1/reports/pareto", { params }),
  getTrend: (params) => request("GET", "/api/v1/reports/trend", { params }),
  getWorkOrderHistory: (wo) => request("GET", `/api/v1/reports/work-orders/${encodeURIComponent(wo)}`),

  exportCsvUrl: (params) => "/api/v1/exports/defects.csv" + buildQueryString(params),

  health: () => request("GET", "/api/v1/health"),

  // Label-scan OCR (Phase 9 Part 3) - see app/static/js/label-scan.js.
  // getScanConfig tells the New Defect form's scan button which engine is
  // active: "tesseract" (default) runs client-side and calls scanParseLabel;
  // any other provider captures a photo and calls scanDiagnose instead.
  getScanConfig: () => request("GET", "/api/v1/scan/config"),
  // The default path: Tesseract.js already recognised `lines` client-side -
  // this posts the raw text for parsing/validation only (no image, no
  // provider call - see app/routers/scan.py parse_label).
  scanParseLabel: (payload) => request("POST", "/api/v1/scan/parse-label", { body: payload }),
  // The optional cloud-provider path - `formData` contains the captured image
  // plus any qr_* fields decoded client-side. Writes nothing server-side.
  scanDiagnose: (formData) =>
    request("POST", "/api/v1/scan/diagnose", { body: formData, isForm: true }),
};

window.Api = Api;
window.ApiError = ApiError;
window.getActorRole = getActorRole;
window.setActorRole = setActorRole;
