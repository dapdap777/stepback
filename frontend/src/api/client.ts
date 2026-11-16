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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
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

async function request<T>(method: string, path: string, query?: Query): Promise<T> {
  const response = await fetch(url(path, query), {
    method,
    credentials: "same-origin",
    // Required by the server for state-changing requests; harmless on reads.
    headers: { "X-Stepback-Client": "1", Accept: "application/json" },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
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
};
