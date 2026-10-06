import type {
  AnalysisResult,
  AnalysisSpec,
  CacheCleanupPreview,
  CacheCleanupResult,
  CacheStatus,
  ClassificationResponse,
  DatasetInfo,
  DiagnosticPreview,
  DiagnosticProgress,
  DiagnosticResult,
  DiagnosticSpec,
  DerivedSpec,
  JobProgress,
  Preview
} from "./types";

const tokenFromUrl = new URLSearchParams(window.location.search).get("token");
if (tokenFromUrl) {
  sessionStorage.setItem("local-app-token", tokenFromUrl);
  history.replaceState({}, "", window.location.pathname);
}

const token = sessionStorage.getItem("local-app-token") ?? "";
const apiBase = import.meta.env.VITE_API_BASE ?? "";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`${apiBase}${path}`, { ...options, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    const detail = payload.detail;
    const message = typeof detail === "string" ? detail : detail?.message ?? JSON.stringify(detail);
    const error = new Error(message || "Request failed") as Error & { position?: number; status?: number; code?: string };
    if (detail?.position !== undefined) error.position = detail.position;
    error.status = response.status;
    if (detail?.code) error.code = detail.code;
    throw error;
  }
  return response.json() as Promise<T>;
}

export const api = {
  async upload(file: File, replaceDatasetId?: string): Promise<DatasetInfo> {
    const form = new FormData();
    form.append("file", file);
    if (replaceDatasetId) form.append("replace_dataset_id", replaceDatasetId);
    return request("/api/datasets", { method: "POST", body: form });
  },
  createDerived(datasetId: string, spec: DerivedSpec) {
    return request<{ stats: Record<string, number>; dataset: DatasetInfo }>(`/api/datasets/${datasetId}/derived`, {
      method: "POST",
      body: JSON.stringify(spec)
    });
  },
  removeDerived(datasetId: string, name: string) {
    return request<DatasetInfo>(`/api/datasets/${datasetId}/derived/${encodeURIComponent(name)}`, { method: "DELETE" });
  },
  classifyControls(datasetId: string) {
    return request<ClassificationResponse>(`/api/datasets/${datasetId}/control-classifications`);
  },
  validateFilter(datasetId: string, expression: string) {
    return request<{ valid: boolean; before: number; after: number; removed: number; removed_ratio: number }>(
      `/api/datasets/${datasetId}/filter/validate`,
      { method: "POST", body: JSON.stringify({ expression }) }
    );
  },
  applyFilter(datasetId: string, expression: string) {
    return request<{ dataset: DatasetInfo; before: number; after: number; removed: number; removed_ratio: number }>(
      `/api/datasets/${datasetId}/filter/apply`,
      { method: "POST", body: JSON.stringify({ expression }) }
    );
  },
  preview(spec: AnalysisSpec) {
    return request<Preview>("/api/analysis/preview", { method: "POST", body: JSON.stringify(spec) });
  },
  start(spec: AnalysisSpec) {
    return request<JobProgress>("/api/jobs", { method: "POST", body: JSON.stringify(spec) });
  },
  cancel(jobId: string) {
    return request<{ cancel_requested: boolean }>(`/api/jobs/${jobId}/cancel`, { method: "POST" });
  },
  result(jobId: string) {
    return request<AnalysisResult>(`/api/jobs/${jobId}/result`);
  },
  socket(jobId: string): WebSocket {
    const configured = apiBase || window.location.origin;
    const url = new URL(configured, window.location.origin);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `/api/jobs/${jobId}/ws`;
    url.search = token ? `token=${encodeURIComponent(token)}` : "";
    return new WebSocket(url);
  },
  diagnosticPreview(jobId: string, spec: DiagnosticSpec) {
    return request<DiagnosticPreview>(`/api/jobs/${jobId}/diagnostics/preview`, {
      method: "POST",
      body: JSON.stringify(spec)
    });
  },
  startDiagnostic(jobId: string, kind: "event-study" | "placebo", spec: DiagnosticSpec) {
    return request<DiagnosticProgress>(`/api/jobs/${jobId}/diagnostics/${kind}`, {
      method: "POST",
      body: JSON.stringify(spec)
    });
  },
  diagnosticResult(jobId: string) {
    return request<DiagnosticResult>(`/api/diagnostic-jobs/${jobId}/result`);
  },
  updateDiagnosticFigureSettings(jobId: string, spec: DiagnosticSpec) {
    return request<{ updated: boolean }>(`/api/diagnostic-jobs/${jobId}/figure-settings`, {
      method: "PATCH",
      body: JSON.stringify({ language: spec.language, labels: spec.labels, show_density: spec.show_density })
    });
  },
  cancelDiagnostic(jobId: string) {
    return request<{ cancel_requested: boolean }>(`/api/diagnostic-jobs/${jobId}/cancel`, { method: "POST" });
  },
  diagnosticSocket(jobId: string): WebSocket {
    const configured = apiBase || window.location.origin;
    const url = new URL(configured, window.location.origin);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `/api/diagnostic-jobs/${jobId}/ws`;
    url.search = token ? `token=${encodeURIComponent(token)}` : "";
    return new WebSocket(url);
  },
  diagnosticAssetUrl(jobId: string, path: string): string {
    const separator = path.includes("?") ? "&" : "?";
    return `${apiBase}/api/diagnostic-jobs/${jobId}/${path}${token ? `${separator}token=${encodeURIComponent(token)}` : ""}`;
  },
  async downloadDiagnostic(jobId: string, path: string, filename: string): Promise<void> {
    const headers = new Headers();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(`${apiBase}/api/diagnostic-jobs/${jobId}/${path}`, { headers });
    if (!response.ok) throw new Error((await response.json()).detail ?? "Export failed");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
  },
  async download(jobId: string, kind: "xlsx" | "docx"): Promise<void> {
    const headers = new Headers();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(`${apiBase}/api/jobs/${jobId}/export.${kind}`, { headers });
    if (!response.ok) throw new Error((await response.json()).detail ?? "Export failed");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `control-search-${jobId.slice(0, 8)}.${kind}`;
    link.click();
    URL.revokeObjectURL(url);
  },
  cacheStatus() {
    return request<CacheStatus>("/api/cache/status");
  },
  renewCacheLease(payload: { dataset_id: string | null; analysis_job_ids: string[]; diagnostic_job_ids: string[] }) {
    return request<CacheStatus>("/api/cache/active", {
      method: "PUT",
      body: JSON.stringify(payload)
    });
  },
  previewCacheCleanup() {
    return request<CacheCleanupPreview>("/api/cache/cleanup/preview", { method: "POST" });
  },
  cleanupCache() {
    return request<CacheCleanupResult>("/api/cache/cleanup", { method: "POST" });
  },
  shutdown() {
    return request<{ shutting_down: boolean }>("/api/shutdown", { method: "POST" });
  }
};

