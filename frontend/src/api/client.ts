// Typed client for the local Stepback API. Types are generated from the server's OpenAPI
// schema (`npm run gen:api`); CI fails if they drift from the backend.
import type { components } from "./schema";

export type Schemas = components["schemas"];
export type RunOut = Schemas["RunOut"];
export type RunPage = Schemas["RunPage"];
export type RunDetail = Schemas["RunDetail"];
export type StepSummary = Schemas["StepSummaryOut"];
export type StepPage = Schemas["StepPage"];
export type StepDetail = Schemas["StepDetail"];
export type RunDiff = Schemas["RunDiff"];
export type DiffPair = Schemas["DiffPair"];
export type Health = Schemas["Health"];
export type ReplayStatus = Schemas["ReplayStatus"];
export type ReplayEstimate = Schemas["ReplayEstimate"];
export type ReplayOut = Schemas["ReplayOut"];

/** Server-side validation error for one field of an edited replay request. */
export interface FieldError {
  path: string;
  message: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code: string | null = null,
    readonly fieldErrors: FieldError[] = [],
  ) {
    super(message);
  }
}

type Query = Record<string, string | number | boolean | null | undefined>;

function url(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

async function request<T>(method: string, path: string, query?: Query, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-Stepback-Client": "1", Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(url(path, query), {
    method,
    credentials: "same-origin",
    // X-Stepback-Client is required by the server for state-changing requests.
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = response.statusText;
    let code: string | null = null;
    let fieldErrors: FieldError[] = [];
    try {
      const payload = (await response.json()) as { detail?: unknown };
      const detail = payload.detail;
      if (typeof detail === "string") message = detail;
      else if (detail && typeof detail === "object" && !Array.isArray(detail)) {
        const d = detail as { message?: string; code?: string; errors?: FieldError[] };
        message = d.message ?? message;
        code = d.code ?? null;
        fieldErrors = d.errors ?? [];
      } else if (Array.isArray(detail)) {
        // FastAPI's own request validation errors
        fieldErrors = detail.map((e: { loc?: unknown[]; msg?: string }) => ({ path: (e.loc ?? []).join("."), message: e.msg ?? "invalid" }));
        message = "Invalid request";
      }
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, message, code, fieldErrors);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>("GET", "/api/health"),
  listRuns: (q: { limit?: number; cursor?: string | null; q?: string; status?: string; include_forks?: boolean }) =>
    request<RunPage>("GET", "/api/runs", q),
  getRun: (runId: string) => request<RunDetail>("GET", `/api/runs/${encodeURIComponent(runId)}`),
  deleteRun: (runId: string, cascade = false) =>
    request<void>("DELETE", `/api/runs/${encodeURIComponent(runId)}`, { cascade }),
  listSteps: (runId: string, offset: number, limit: number) =>
    request<StepPage>("GET", `/api/runs/${encodeURIComponent(runId)}/steps`, { offset, limit }),
  getStep: (stepId: string) => request<StepDetail>("GET", `/api/steps/${encodeURIComponent(stepId)}`),
  diff: (a: string, b: string, offset = 0, limit = 500) =>
    request<RunDiff>("GET", "/api/diff", { a, b, offset, limit }),
  replayStatus: () => request<ReplayStatus>("GET", "/api/replay/status"),
  estimateReplay: (stepId: string, req: Record<string, unknown>) =>
    request<ReplayEstimate>("POST", `/api/steps/${encodeURIComponent(stepId)}/replay/estimate`, undefined, { request: req }),
  /** Paid: sends one request to the Anthropic API. Only call from an explicit user action. */
  replay: (stepId: string, req: Record<string, unknown>) =>
    request<ReplayOut>("POST", `/api/steps/${encodeURIComponent(stepId)}/replay`, undefined, { request: req, confirm: true }),
};
