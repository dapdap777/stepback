import { ArrowLeft, GitCompareArrows, GitFork, MousePointerClick, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef } from "react";
import { api, type RunDetail } from "../api/client";
import { useAsync } from "../api/hooks";
import { StepDetailPanel } from "../components/StepDetail";
import { Timeline, type TimelineHandle, useSpan, useStepPages } from "../components/Timeline";
import { Badge, Button, Empty, ErrorBanner, Kbd, Spinner, Stat, StatusDot } from "../components/ui";
import { fmtCost, fmtMs, fmtTime, fmtTokens, runDurationMs } from "../lib/format";
import { useKeys } from "../lib/keys";
import { linkProps, navigate, paths } from "../lib/router";

export function RunPage({ runId, step }: { runId: string; step: number | null }) {
  const detail = useAsync(`run:${runId}`, () => api.getRun(runId));
  if (detail.error) {
    return (
      <div className="p-6">
        <ErrorBanner error={detail.error} />
      </div>
    );
  }
  if (!detail.data) {
    return (
      <div className="p-6">
        <Spinner label="Loading run…" />
      </div>
    );
  }
  return <RunView key={runId} detail={detail.data} selected={step} />;
}

function RunView({ detail, selected }: { detail: RunDetail; selected: number | null }) {
  const run = detail.run;
  const { total, get, ensurePage, error, loadedPages } = useStepPages(run.id);
  const span = useSpan(run, loadedPages);
  const timeline = useRef<TimelineHandle>(null);
  const count = total ?? 0;
  const current = selected !== null && selected < count ? selected : null;
  const currentStep = current !== null ? get(current) : undefined;

  const select = useCallback(
    (index: number | null) => {
      navigate(paths.run(run.id, index), { replace: true });
      if (index !== null) timeline.current?.scrollTo(index);
    },
    [run.id],
  );

  useEffect(() => {
    if (current !== null) {
      ensurePage(Math.floor(current / 200));
      timeline.current?.scrollTo(current);
    }
    // Scroll to a step selected via the URL once the first page has loaded.
  }, [count > 0]);

  const move = (delta: number) => {
    if (count === 0) return;
    select(current === null ? (delta > 0 ? 0 : count - 1) : Math.min(Math.max(current + delta, 0), count - 1));
  };
  useKeys({
    j: () => move(1),
    k: () => move(-1),
    ArrowDown: () => move(1),
    ArrowUp: () => move(-1),
    g: () => count && select(0),
    G: () => count && select(count - 1),
    Escape: () => select(null),
    d: () => detail.parent && navigate(paths.diff(detail.parent.id, run.id)),
  });

  const remove = async () => {
    const forks = run.fork_count;
    const message = forks
      ? `Delete "${run.name}" and its ${forks} fork(s)? This cannot be undone.`
      : `Delete "${run.name}"? This cannot be undone.`;
    if (!window.confirm(message)) return;
    await api.deleteRun(run.id, forks > 0);
    navigate(paths.runs());
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 border-b border-line bg-surface px-6 pt-4 pb-4">
        <div className="flex items-center gap-2 text-[12px] text-muted">
          <a {...linkProps(paths.runs())} className="inline-flex items-center gap-1 hover:text-fg">
            <ArrowLeft className="size-3.5" /> Runs
          </a>
          {detail.parent ? (
            <>
              <span className="text-faint">/</span>
              <a {...linkProps(paths.run(detail.parent.id, detail.forked_from_step?.idx))} className="inline-flex items-center gap-1 hover:text-fg">
                <GitFork className="size-3.5" /> {detail.parent.name}
              </a>
            </>
          ) : null}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <StatusDot status={run.status} />
          <h1 className="text-[20px] font-semibold tracking-tight" data-testid="run-title">{run.name}</h1>
          <span className="font-mono text-[12px] text-faint">{run.id}</span>
          {detail.parent ? (
            <Badge tone="replay">
              <GitFork className="size-3" /> forked at step #{detail.forked_from_step?.idx ?? "?"}
            </Badge>
          ) : null}
          <div className="ml-auto flex items-center gap-2">
            {detail.parent ? (
              <Button data-testid="compare-parent" onClick={() => navigate(paths.diff(detail.parent!.id, run.id))} title="Compare with the parent run (d)">
                <GitCompareArrows className="size-3.5" /> Compare with parent
              </Button>
            ) : (
              <Button onClick={() => navigate(paths.diff(run.id))}>
                <GitCompareArrows className="size-3.5" /> Compare…
              </Button>
            )}
            <Button variant="danger" onClick={remove} title="Delete run">
              <Trash2 className="size-3.5" />
            </Button>
          </div>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-6 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="Steps" value={(detail.inherited_steps + detail.own_steps).toLocaleString()} sub={detail.inherited_steps ? `${detail.inherited_steps} inherited` : run.error_count ? <span className="text-error">{run.error_count} failed</span> : "all succeeded"} />
          <Stat label="Tokens" value={`${fmtTokens(run.input_tokens)} → ${fmtTokens(run.output_tokens)}`} sub="input → output" />
          <Stat label="Cost" value={fmtCost(run.cost_usd)} tone="ok" sub="estimated" />
          <Stat label="Duration" value={fmtMs(runDurationMs(run))} sub={`${fmtMs(run.latency_ms)} in LLM calls`} />
          <Stat label="Started" value={fmtTime(run.created_at)} />
          <Stat label="Models" value={<span className="font-mono text-[13px]">{run.models.join(", ") || "—"}</span>} />
        </div>
        {detail.forks.length ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 text-[12px]">
            <span className="text-faint">Forks:</span>
            {detail.forks.map((fork) => (
              <a key={fork.id} {...linkProps(paths.run(fork.id))} className="inline-flex items-center gap-1 rounded-md bg-replay-soft px-2 py-0.5 text-replay hover:brightness-125">
                <GitFork className="size-3" /> {fork.name}
              </a>
            ))}
          </div>
        ) : null}
      </header>

      {error ? <ErrorBanner error={error} /> : null}
      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1">
          {total === null ? (
            <div className="p-6">
              <Spinner label="Loading steps…" />
            </div>
          ) : total === 0 ? (
            <Empty title="No steps recorded in this run" />
          ) : (
            <Timeline ref={timeline} total={count} get={get} ensurePage={ensurePage} selected={current} onSelect={select} span={span} runId={run.id} />
          )}
        </div>
        <div className="w-[46%] max-w-[760px] min-w-[420px] shrink-0">
          {currentStep ? (
            <StepDetailPanel summary={currentStep} position={current ?? 0} total={count} onPrev={() => move(-1)} onNext={() => move(1)} onClose={() => select(null)} />
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-3 border-l border-line bg-surface text-center text-muted">
              <MousePointerClick className="size-6 text-faint" />
              <div>Select a step to inspect its messages, tool calls and response.</div>
              <div className="text-[12px] text-faint">
                <Kbd>j</Kbd> / <Kbd>k</Kbd> to move through steps
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
