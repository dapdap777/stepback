// A ~40-line router: three pages don't need a routing library.
import { useEffect, useState } from "react";

export type Route =
  | { page: "runs" }
  | { page: "run"; runId: string; step: number | null }
  | { page: "diff"; a: string | null; b: string | null }
  | { page: "notFound" };

export function parseRoute(location: Location = window.location): Route {
  const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  const params = new URLSearchParams(location.search);
  if (parts.length === 0) return { page: "runs" };
  if (parts[0] === "runs" && parts[1]) {
    const step = params.get("step");
    return { page: "run", runId: parts[1], step: step !== null && /^\d+$/.test(step) ? Number(step) : null };
  }
  if (parts[0] === "diff") return { page: "diff", a: params.get("a"), b: params.get("b") };
  return { page: "notFound" };
}

const listeners = new Set<() => void>();

export function navigate(to: string, { replace = false } = {}): void {
  if (to === window.location.pathname + window.location.search) return;
  if (replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  listeners.forEach((fn) => fn());
}

export function useRoute(): Route {
  const [route, setRoute] = useState(parseRoute);
  useEffect(() => {
    const update = () => setRoute(parseRoute());
    listeners.add(update);
    window.addEventListener("popstate", update);
    return () => {
      listeners.delete(update);
      window.removeEventListener("popstate", update);
    };
  }, []);
  return route;
}

export const paths = {
  runs: () => "/",
  run: (runId: string, step?: number | null) =>
    `/runs/${encodeURIComponent(runId)}${step !== undefined && step !== null ? `?step=${step}` : ""}`,
  diff: (a?: string | null, b?: string | null) => {
    const params = new URLSearchParams();
    if (a) params.set("a", a);
    if (b) params.set("b", b);
    const qs = params.toString();
    return qs ? `/diff?${qs}` : "/diff";
  },
};

/** Props for an <a> that navigates client-side but still supports open-in-new-tab. */
export function linkProps(to: string) {
  return {
    href: to,
    onClick: (event: React.MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey) return;
      event.preventDefault();
      navigate(to);
    },
  };
}
