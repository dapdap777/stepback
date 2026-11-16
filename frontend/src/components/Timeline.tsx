import { useVirtualizer } from "@tanstack/react-virtual";
import { AlertOctagon, History, RotateCcw, Sparkles, Wrench } from "lucide-react";
import { forwardRef, useCallback, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { api, type StepSummary } from "../api/client";
import { fmtCost, fmtMs, fmtTokens } from "../lib/format";
import { cx } from "./ui";

export const PAGE_SIZE = 200;
const ROW_HEIGHT = 58;

/** Step summaries for a run, fetched page by page as they scroll into view. */
export function useStepPages(runId: string) {
  const [pages, setPages] = useState<Map<number, StepSummary[]>>(() => new Map());
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const inflight = useRef(new Set<number>());
  const generation = useRef(0);

  const ensurePage = useCallback(
    (page: number) => {
      if (page < 0 || inflight.current.has(page)) return;
      inflight.current.add(page);
      const gen = generation.current;
      api
        .listSteps(runId, page * PAGE_SIZE, PAGE_SIZE)
        .then((result) => {
          if (gen !== generation.current) return;
          setTotal(result.total);
          setPages((prev) => new Map(prev).set(page, result.items));
        })
        .catch((e: Error) => {
          inflight.current.delete(page);
          if (gen === generation.current) setError(e);
        });
    },
    [runId],
  );

  useEffect(() => {
    generation.current += 1;
    inflight.current = new Set();
    setPages(new Map());
    setTotal(null);
    setError(null);
    ensurePage(0);
  }, [ensurePage]);

  const get = useCallback((index: number) => pages.get(Math.floor(index / PAGE_SIZE))?.[index % PAGE_SIZE], [pages]);
  return { total, get, ensurePage, error, loadedPages: pages };
}

export interface TimelineHandle {
  scrollTo: (index: number) => void;
}

interface TimelineProps {
  total: number;
  get: (index: number) => StepSummary | undefined;
  ensurePage: (page: number) => void;
  selected: number | null;
  onSelect: (index: number) => void;
  span: { start: number; end: number };
  runId: string;
  /** Rendered after the last row (e.g. the end-of-fork marker). */
  footer?: React.ReactNode;
}

export const Timeline = forwardRef<TimelineHandle, TimelineProps>(function Timeline({ total, get, ensurePage, selected, onSelect, span, runId, footer }, ref) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: total,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
  });
  useImperativeHandle(ref, () => ({ scrollTo: (index) => virtualizer.scrollToIndex(index, { align: "auto" }) }), [virtualizer]);

  const items = virtualizer.getVirtualItems();
  const first = items[0]?.index ?? 0;
  const last = items[items.length - 1]?.index ?? 0;
  useEffect(() => {
    for (let page = Math.floor(first / PAGE_SIZE); page <= Math.floor(last / PAGE_SIZE); page++) ensurePage(page);
  }, [first, last, ensurePage]);

  const duration = Math.max(span.end - span.start, 0.001);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="grid shrink-0 grid-cols-[52px_minmax(0,1fr)_118px_78px_66px_minmax(120px,22%)] items-center gap-3 border-b border-line bg-surface px-3 py-2 text-[10.5px] font-semibold uppercase tracking-wider text-faint">
        <span>Step</span>
        <span>Call</span>
        <span className="text-right">Tokens in → out</span>
        <span className="text-right">Cost</span>
        <span className="text-right">Latency</span>
        <span className="flex justify-between">
          <span>Timeline</span>
          <span className="tabular normal-case">{fmtMs(duration * 1000)}</span>
        </span>
      </div>
      <div ref={scrollRef} data-testid="timeline" className="min-h-0 flex-1 overflow-y-auto" role="listbox" aria-label={`Steps of ${runId}`}>
        <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
          {items.map((item) => {
            const step = get(item.index);
            return (
              <div key={item.key} className="absolute inset-x-0 top-0" style={{ height: item.size, transform: `translateY(${item.start}px)` }}>
                {step ? (
                  <StepRow step={step} index={item.index} selected={selected === item.index} onSelect={onSelect} span={span} duration={duration} />
                ) : (
                  <SkeletonRow />
                )}
              </div>
            );
          })}
        </div>
        {footer}
      </div>
    </div>
  );
});

function SkeletonRow() {
  return (
    <div className="flex h-full items-center gap-3 border-b border-line px-3">
      <div className="h-3 w-8 animate-pulse rounded bg-surface-3" />
      <div className="h-3 w-1/3 animate-pulse rounded bg-surface-3" />
    </div>
  );
}

function StepRow({ step, index, selected, onSelect, span, duration }: { step: StepSummary; index: number; selected: boolean; onSelect: (i: number) => void; span: { start: number }; duration: number }) {
  const kind = step.error_type ? "error" : step.is_replay ? "replay" : "llm";
  const stripe = { error: "bg-error", replay: "bg-replay", llm: "bg-llm" }[kind];
  const left = ((step.started_at - span.start) / duration) * 100;
  const width = (((step.latency_ms ?? 0) / 1000) / duration) * 100;
  const ttftPct = step.ttft_ms && step.latency_ms ? (step.ttft_ms / step.latency_ms) * 100 : null;
  const userText = step.input_preview && !step.input_preview.startsWith("[") ? step.input_preview : null;
  return (
    <div
      role="option"
      aria-selected={selected}
      data-testid="step-row"
      data-step-index={index}
      onClick={() => onSelect(index)}
      className={cx(
        "group relative grid h-full cursor-pointer grid-cols-[52px_minmax(0,1fr)_118px_78px_66px_minmax(120px,22%)] items-center gap-3 border-b border-line px-3 transition-colors",
        selected ? "bg-selection" : "hover:bg-surface-2",
        step.inherited && "opacity-55",
      )}
    >
      <span className={cx("absolute inset-y-0 left-0 w-[3px]", stripe, !selected && "opacity-70")} />
      <span className="tabular font-mono text-[12px] text-muted">#{step.idx}</span>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-1.5">
          <KindIcon kind={kind} />
          <span className="shrink-0 font-mono text-[12px] text-fg" title={step.model ?? undefined}>{shortModel(step.model ?? step.provider)}</span>
          {step.inherited ? (
            <span className="inline-flex items-center gap-0.5 text-[11px] text-faint" title="Inherited from the parent run">
              <History className="size-3" />
            </span>
          ) : null}
          {step.error_type ? (
            <span className="truncate rounded bg-error-soft px-1.5 text-[11px] font-medium text-error">
              {step.error_type}
              {step.error_status ? ` ${step.error_status}` : ""}
            </span>
          ) : null}
          <span className="flex min-w-0 items-center gap-1.5 overflow-hidden">
          {step.tool_calls.map((call, i) => (
            <span
              key={call.id ?? i}
              title={call.input_preview}
              className={cx(
                "inline-flex max-w-[11rem] shrink-0 items-center gap-1 truncate rounded px-1.5 font-mono text-[11px]",
                call.is_error ? "bg-error-soft text-error" : "bg-tool-soft text-tool",
              )}
            >
              <Wrench className="size-3 shrink-0" />
              <span className="truncate">{call.name}</span>
            </span>
          ))}
          </span>
          {step.stop_reason === "max_tokens" ? <span className="rounded bg-error-soft px-1.5 text-[11px] text-error">max_tokens</span> : null}
        </div>
        <div className="mt-0.5 truncate text-[12px] text-muted">
          {userText ? <span className="mr-1.5 font-medium text-info">user:</span> : null}
          {userText ?? step.output_preview ?? <span className="text-faint italic">no output</span>}
        </div>
      </div>
      <span className="tabular text-right text-[12px]">
        {fmtTokens(step.input_tokens)} <span className="text-faint">→</span> {fmtTokens(step.output_tokens)}
      </span>
      <span className="tabular text-right text-[12px] text-ok">{fmtCost(step.cost_usd)}</span>
      <span className="tabular text-right text-[12px] font-semibold">{fmtMs(step.latency_ms)}</span>
      <div className="relative h-4 rounded-sm bg-surface-2" title={`started +${fmtMs((step.started_at - span.start) * 1000)}, took ${fmtMs(step.latency_ms)}`}>
        <div
          className={cx("absolute inset-y-0.5 overflow-hidden rounded-sm", kind === "error" ? "bg-error/80" : kind === "replay" ? "bg-replay/80" : "bg-llm/80")}
          style={{ left: `${Math.min(Math.max(left, 0), 99.5)}%`, width: `max(3px, ${Math.min(width, 100 - Math.max(left, 0))}%)` }}
        >
          {ttftPct !== null ? <div className="h-full bg-white/25" style={{ width: `${ttftPct}%` }} /> : null}
        </div>
      </div>
    </div>
  );
}

/** "claude-opus-5-5" -> "opus-5-5": the provider prefix adds nothing in a dense row. */
export function shortModel(model: string): string {
  return model.replace(/^claude-/, "");
}

function KindIcon({ kind }: { kind: "llm" | "error" | "replay" }) {
  if (kind === "error") return <AlertOctagon className="size-3.5 shrink-0 text-error" />;
  if (kind === "replay") return <RotateCcw className="size-3.5 shrink-0 text-replay" />;
  return <Sparkles className="size-3.5 shrink-0 text-llm" />;
}

/** Wall-clock span of the run for the waterfall column. */
export function useSpan(run: { created_at: number; ended_at: number | null }, loadedPages: Map<number, StepSummary[]>) {
  return useMemo(() => {
    let start = run.created_at;
    let end = run.ended_at ?? run.created_at;
    for (const page of loadedPages.values()) {
      for (const s of page) {
        start = Math.min(start, s.started_at);
        end = Math.max(end, s.started_at + (s.latency_ms ?? 0) / 1000);
      }
    }
    return { start, end };
  }, [run.created_at, run.ended_at, loadedPages]);
}
