import { GitCompareArrows, GitFork, Search, Terminal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, type RunOut } from "../api/client";
import { Badge, Button, Empty, ErrorBanner, Kbd, Spinner, StatusDot, cx } from "../components/ui";
import { fmtAgo, fmtCost, fmtMs, fmtTime, fmtTokens, runDurationMs } from "../lib/format";
import { useKeys } from "../lib/keys";
import { linkProps, navigate, paths } from "../lib/router";

const PAGE = 50;

export function RunsPage() {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [showForks, setShowForks] = useState(true);
  const [runs, setRuns] = useState<RunOut[]>([]);
  const [total, setTotal] = useState(0);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [active, setActive] = useState(0);
  const [compare, setCompare] = useState<string[]>([]);
  const searchRef = useRef<HTMLInputElement>(null);
  const filterKey = `${query}|${status}|${showForks}`;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const timer = window.setTimeout(() => {
      api
        .listRuns({ limit: PAGE, q: query, status, include_forks: showForks })
        .then((page) => {
          if (cancelled) return;
          setRuns(page.items);
          setTotal(page.total);
          setCursor(page.next_cursor);
          setActive(0);
          setError(null);
        })
        .catch((e: Error) => !cancelled && setError(e))
        .finally(() => !cancelled && setLoading(false));
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [filterKey, query, status, showForks]);

  const loadMore = () => {
    if (!cursor) return;
    setLoading(true);
    api
      .listRuns({ limit: PAGE, cursor, q: query, status, include_forks: showForks })
      .then((page) => {
        setRuns((prev) => [...prev, ...page.items]);
        setCursor(page.next_cursor);
      })
      .catch((e: Error) => setError(e))
      .finally(() => setLoading(false));
  };

  const toggleCompare = (id: string) =>
    setCompare((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev.slice(-1), id]));

  useKeys({
    j: () => setActive((i) => Math.min(i + 1, runs.length - 1)),
    k: () => setActive((i) => Math.max(i - 1, 0)),
    Enter: () => runs[active] && navigate(paths.run(runs[active].id)),
    o: () => runs[active] && navigate(paths.run(runs[active].id)),
    x: () => runs[active] && toggleCompare(runs[active].id),
    "/": () => searchRef.current?.focus(),
  });

  useEffect(() => {
    document.querySelector(`[data-run-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  return (
    <div className="mx-auto flex h-full max-w-[1400px] flex-col px-6 py-5">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <h1 className="text-[20px] font-semibold tracking-tight">Runs</h1>
        <span className="tabular rounded-full bg-surface-3 px-2 py-0.5 text-[12px] text-muted">{total.toLocaleString()}</span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <label className="flex h-8 w-72 items-center gap-2 rounded-md border border-line bg-surface px-2.5 focus-within:border-llm">
            <Search className="size-3.5 text-faint" />
            <input
              ref={searchRef}
              data-testid="run-search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Escape" && e.currentTarget.blur()}
              placeholder="Search by name or id"
              className="min-w-0 flex-1 bg-transparent text-[13px] outline-none placeholder:text-faint"
            />
            <Kbd>/</Kbd>
          </label>
          <select value={status} onChange={(e) => setStatus(e.target.value)} className="h-8 rounded-md border border-line bg-surface px-2 text-[13px]" aria-label="Status filter">
            <option value="">All statuses</option>
            <option value="ok">OK</option>
            <option value="error">Error</option>
            <option value="running">Running</option>
          </select>
          <label className="flex h-8 items-center gap-1.5 rounded-md border border-line bg-surface px-2.5 text-[13px] text-muted">
            <input type="checkbox" checked={showForks} onChange={(e) => setShowForks(e.target.checked)} className="accent-[var(--llm)]" />
            Forks
          </label>
        </div>
      </div>

      {error ? <ErrorBanner error={error} /> : null}

      <div className="min-h-0 flex-1 overflow-y-auto rounded-xl border border-line bg-surface">
        <table className="w-full table-fixed text-left text-[13px]" data-testid="runs-table">
          <colgroup>
            <col className="w-10" />
            <col />
            <col className="w-28" />
            <col className="w-20" />
            <col className="w-40" />
            <col className="w-24" />
            <col className="w-24" />
            <col className="w-48" />
          </colgroup>
          <thead className="sticky top-0 z-10 bg-surface text-[10.5px] font-semibold uppercase tracking-wider text-faint">
            <tr className="border-b border-line">
              <th className="w-10 py-2.5 pl-4" aria-label="Compare" />
              <th className="py-2.5">Run</th>
              <th className="py-2.5">Started</th>
              <th className="py-2.5 text-right">Steps</th>
              <th className="py-2.5 text-right">Tokens in / out</th>
              <th className="py-2.5 text-right">Cost</th>
              <th className="py-2.5 text-right">Duration</th>
              <th className="py-2.5 pr-4 pl-6">Models</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run, i) => (
              <RunRow key={run.id} run={run} index={i} active={i === active} checked={compare.includes(run.id)} onToggle={() => toggleCompare(run.id)} />
            ))}
          </tbody>
        </table>
        {!loading && runs.length === 0 && !error ? (
          query || status ? (
            <Empty title="No runs match these filters" />
          ) : (
            <Empty title="No runs recorded yet">
              <p>Wrap your Anthropic client and run your agent:</p>
              <pre className="mt-2 rounded-lg border border-line bg-surface-2 px-4 py-3 text-left font-mono text-[12.5px] text-fg">
                {"import stepback\nclient = stepback.wrap(Anthropic())"}
              </pre>
            </Empty>
          )
        ) : null}
        <div className="flex items-center justify-center gap-3 py-4">
          {loading ? <Spinner label="Loading runs…" /> : null}
          {!loading && cursor ? (
            <Button onClick={loadMore}>Load more ({(total - runs.length).toLocaleString()} more)</Button>
          ) : null}
        </div>
      </div>

      <div className="mt-3 flex items-center gap-4 text-[11.5px] text-faint">
        <span><Kbd>j</Kbd> <Kbd>k</Kbd> move</span>
        <span><Kbd>Enter</Kbd> open</span>
        <span><Kbd>x</Kbd> select for compare</span>
        <span className="ml-auto inline-flex items-center gap-1.5">
          <Terminal className="size-3.5" /> also in your terminal: <code className="font-mono text-muted">stepback ls</code>
        </span>
      </div>

      {compare.length > 0 ? (
        <div className="fixed bottom-6 left-1/2 z-20 flex -translate-x-1/2 items-center gap-3 rounded-xl border border-line-strong bg-surface-2 px-4 py-2.5 shadow-pop">
          <GitCompareArrows className="size-4 text-llm" />
          <span className="text-[13px]">
            {compare.length === 1 ? "Select one more run to compare" : `Compare ${compare[0]} ↔ ${compare[1]}`}
          </span>
          <Button variant="ghost" onClick={() => setCompare([])}>Clear</Button>
          <Button variant="primary" data-testid="compare-button" disabled={compare.length !== 2} onClick={() => navigate(paths.diff(compare[0], compare[1]))}>
            Compare
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function RunRow({ run, index, active, checked, onToggle }: { run: RunOut; index: number; active: boolean; checked: boolean; onToggle: () => void }) {
  const link = linkProps(paths.run(run.id));
  return (
    <tr
      data-run-index={index}
      data-testid="run-row"
      className={cx("cursor-pointer border-b border-line transition-colors last:border-0", active ? "bg-selection" : "hover:bg-surface-2")}
      onClick={(e) => link.onClick(e)}
    >
      <td className="py-2.5 pl-4" onClick={(e) => e.stopPropagation()}>
        <input type="checkbox" checked={checked} onChange={onToggle} aria-label={`Select ${run.name} for comparison`} className="accent-[var(--llm)]" />
      </td>
      <td className="py-2.5 pr-4">
        <div className="flex min-w-0 items-center gap-2">
          <StatusDot status={run.status} />
          <a {...link} className="truncate font-medium text-fg hover:underline" onClick={(e) => e.stopPropagation() /* row handles it */}>
            {run.name}
          </a>
          {run.parent_run_id ? (
            <Badge tone="replay" title={`Forked from ${run.parent_run_id}`} className="shrink-0">
              <GitFork className="size-3" /> fork
            </Badge>
          ) : null}
          {run.fork_count ? (
            <Badge tone="replay" title="Forked runs" className="shrink-0">
              {run.fork_count} fork{run.fork_count > 1 ? "s" : ""}
            </Badge>
          ) : null}
          {run.error_count ? <Badge tone="error" className="shrink-0">{run.error_count} error{run.error_count > 1 ? "s" : ""}</Badge> : null}
        </div>
        <div className="mt-0.5 truncate pl-4 font-mono text-[11px] text-faint">{run.id}</div>
      </td>
      <td className="tabular py-2.5 whitespace-nowrap text-muted" title={fmtTime(run.created_at)}>{fmtAgo(run.created_at)}</td>
      <td className="tabular py-2.5 text-right">{run.step_count}</td>
      <td className="tabular py-2.5 text-right whitespace-nowrap">
        {fmtTokens(run.input_tokens)} <span className="text-faint">/</span> {fmtTokens(run.output_tokens)}
      </td>
      <td className="tabular py-2.5 text-right text-ok">{fmtCost(run.cost_usd)}</td>
      <td className="tabular py-2.5 text-right">{fmtMs(runDurationMs(run))}</td>
      <td className="truncate py-2.5 pr-4 pl-6 font-mono text-[12px] text-muted">{run.models.join(", ") || "—"}</td>
    </tr>
  );
}
