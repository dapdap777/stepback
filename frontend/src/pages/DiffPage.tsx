import { diffLines, diffWordsWithSpace } from "diff";
import { ArrowLeftRight, Equal, GitCompareArrows, Minus, Plus } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { api, type DiffPair, type RunDiff, type StepDetail, type StepSummary } from "../api/client";
import { fetchStep, useAsync } from "../api/hooks";
import { textOf } from "../components/MessageView";
import { Badge, Button, ErrorBanner, Spinner, type Tone, cx } from "../components/ui";
import { fmtCost, fmtDelta, fmtMs, fmtTokens, pretty } from "../lib/format";
import { useKeys } from "../lib/keys";
import { linkProps, navigate, paths } from "../lib/router";

export function DiffPage({ a, b }: { a: string | null; b: string | null }) {
  if (!a || !b) return <RunPicker a={a} b={b} />;
  return <DiffView key={`${a}|${b}`} a={a} b={b} />;
}

// ---------------------------------------------------------------------- picker
function RunPicker({ a, b }: { a: string | null; b: string | null }) {
  const runs = useAsync("picker", () => api.listRuns({ limit: 200 }));
  const [left, setLeft] = useState(a ?? "");
  const [right, setRight] = useState(b ?? "");
  const options = runs.data?.items ?? [];
  return (
    <div className="mx-auto max-w-2xl px-6 py-10">
      <h1 className="flex items-center gap-2 text-[20px] font-semibold tracking-tight">
        <GitCompareArrows className="size-5 text-llm" /> Compare runs
      </h1>
      <p className="mt-1 text-muted">Pick two runs. Steps are aligned by position; forks share their inherited steps with the parent.</p>
      {runs.error ? <ErrorBanner error={runs.error} /> : null}
      <div className="mt-6 grid gap-3">
        {[
          ["A (before)", left, setLeft],
          ["B (after)", right, setRight],
        ].map(([label, value, set]) => (
          <label key={label as string} className="grid gap-1">
            <span className="text-[12px] font-medium text-muted">{label as string}</span>
            <select value={value as string} onChange={(e) => (set as (v: string) => void)(e.target.value)} className="h-9 rounded-md border border-line bg-surface px-2">
              <option value="">Select a run…</option>
              {options.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name} · {r.id} · {r.step_count} steps
                </option>
              ))}
            </select>
          </label>
        ))}
        <div>
          <Button variant="primary" disabled={!left || !right || left === right} onClick={() => navigate(paths.diff(left, right))}>
            Compare
          </Button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------- diff view
const STATUS: Record<DiffPair["status"], { label: string; tone: Tone; icon: ReactNode }> = {
  shared: { label: "shared", tone: "neutral", icon: <Equal className="size-3" /> },
  same: { label: "identical", tone: "ok", icon: <Equal className="size-3" /> },
  changed: { label: "changed", tone: "tool", icon: <ArrowLeftRight className="size-3" /> },
  added: { label: "only in B", tone: "ok", icon: <Plus className="size-3" /> },
  removed: { label: "only in A", tone: "error", icon: <Minus className="size-3" /> },
};

function DiffView({ a, b }: { a: string; b: string }) {
  const diff = useAsync(`diff:${a}|${b}`, () => api.diff(a, b));
  const [selected, setSelected] = useState<number | null>(null);
  const data = diff.data;
  useEffect(() => {
    if (data && selected === null) {
      // Open on the first step whose content actually changed; fall back to the first divergence.
      const changed = data.pairs.find((p) => p.status === "changed");
      setSelected(changed?.position ?? data.first_divergence ?? 0);
    }
  }, [data, selected]);
  useKeys({
    j: () => data && setSelected((s) => Math.min((s ?? -1) + 1, data.pairs.length - 1)),
    k: () => data && setSelected((s) => Math.max((s ?? 1) - 1, 0)),
    n: () => {
      if (!data) return;
      const next = data.pairs.find((p) => p.position > (selected ?? -1) && p.status !== "shared" && p.status !== "same");
      if (next) setSelected(next.position);
    },
  });
  useEffect(() => {
    document.querySelector(`[data-pair="${selected}"]`)?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  if (diff.error) return <div className="p-6"><ErrorBanner error={diff.error} /></div>;
  if (!data) return <div className="p-6"><Spinner label="Comparing runs…" /></div>;
  const pair = data.pairs.find((p) => p.position === selected) ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="diff-view">
      <DiffHeader diff={data} />
      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1 overflow-y-auto">
          <div className="sticky top-0 z-10 grid grid-cols-[100px_minmax(0,1fr)_minmax(0,1fr)] gap-3 border-b border-line bg-surface px-4 py-2 text-[10.5px] font-semibold uppercase tracking-wider text-faint">
            <span>Status</span>
            <span>A · {data.a.name}</span>
            <span>B · {data.b.name}</span>
          </div>
          {data.pairs.map((p) => (
            <PairRow key={p.position} pair={p} selected={p.position === selected} onSelect={() => setSelected(p.position)} />
          ))}
          {data.total_pairs > data.pairs.length ? (
            <div className="p-4 text-center text-[12px] text-muted">Showing the first {data.pairs.length} of {data.total_pairs} steps.</div>
          ) : null}
        </div>
        <div className="w-[48%] max-w-[820px] min-w-[420px] shrink-0 overflow-y-auto border-l border-line bg-surface">
          {pair ? <PairDetail key={pair.position} pair={pair} /> : null}
        </div>
      </div>
    </div>
  );
}

function DiffHeader({ diff }: { diff: RunDiff }) {
  const ta = diff.totals_a;
  const tb = diff.totals_b;
  const delta = (x: number | null | undefined, y: number | null | undefined, format: (v: number) => string, lowerIsBetter = true) => {
    if (x === null || x === undefined || y === null || y === undefined) return null;
    const d = y - x;
    const good = lowerIsBetter ? d < 0 : d > 0;
    return <span className={cx("tabular text-[12px] font-medium", d === 0 ? "text-faint" : good ? "text-ok" : "text-error")}>{fmtDelta(d, format)}</span>;
  };
  const rows: [string, ReactNode, ReactNode, ReactNode][] = [
    ["Steps", ta.steps, tb.steps, delta(ta.steps, tb.steps, String, false)],
    ["Input tokens", fmtTokens(ta.input_tokens), fmtTokens(tb.input_tokens), delta(ta.input_tokens, tb.input_tokens, fmtTokens)],
    ["Output tokens", fmtTokens(ta.output_tokens), fmtTokens(tb.output_tokens), delta(ta.output_tokens, tb.output_tokens, fmtTokens)],
    ["Cost", fmtCost(ta.cost_usd), fmtCost(tb.cost_usd), delta(ta.cost_usd, tb.cost_usd, fmtCost)],
    ["LLM time", fmtMs(ta.latency_ms), fmtMs(tb.latency_ms), delta(ta.latency_ms, tb.latency_ms, fmtMs)],
  ];
  return (
    <header className="shrink-0 border-b border-line bg-surface px-6 py-4">
      <div className="flex flex-wrap items-center gap-3">
        <GitCompareArrows className="size-5 text-llm" />
        <h1 className="text-[18px] font-semibold tracking-tight">
          <a {...linkProps(paths.run(diff.a.id))} className="hover:underline">{diff.a.name}</a>
          <span className="mx-2 text-faint">↔</span>
          <a {...linkProps(paths.run(diff.b.id))} className="hover:underline">{diff.b.name}</a>
        </h1>
        {diff.first_divergence === null ? (
          <Badge tone="ok">identical</Badge>
        ) : (
          <Badge tone="tool">first divergence at {divergenceLabel(diff)}</Badge>
        )}
        <Button className="ml-auto" variant="ghost" onClick={() => navigate(paths.diff(diff.b.id, diff.a.id))}>
          <ArrowLeftRight className="size-3.5" /> Swap
        </Button>
      </div>
      <div className="mt-3 grid grid-cols-5 gap-4">
        {rows.map(([label, va, vb, d]) => (
          <div key={label}>
            <div className="text-[10.5px] font-semibold uppercase tracking-wider text-faint">{label}</div>
            <div className="tabular mt-0.5 flex items-baseline gap-2 text-[14px] font-semibold">
              <span className="text-muted">{va}</span>
              <span className="text-faint">→</span>
              <span>{vb}</span>
              {d}
            </div>
          </div>
        ))}
      </div>
    </header>
  );
}

function divergenceLabel(diff: RunDiff): string {
  const pair = diff.pairs.find((p) => p.position === diff.first_divergence);
  const idx = pair?.a?.idx ?? pair?.b?.idx;
  return idx === undefined ? `row ${diff.first_divergence}` : `step #${idx}`;
}

function PairRow({ pair, selected, onSelect }: { pair: DiffPair; selected: boolean; onSelect: () => void }) {
  const status = STATUS[pair.status];
  const quiet = pair.status === "shared" || pair.status === "same";
  return (
    <div
      data-pair={pair.position}
      data-testid="diff-pair"
      data-status={pair.status}
      onClick={onSelect}
      className={cx("grid cursor-pointer grid-cols-[100px_minmax(0,1fr)_minmax(0,1fr)] items-center gap-3 border-b border-line px-4 py-2", selected ? "bg-selection" : "hover:bg-surface-2", quiet && !selected && "opacity-60")}
    >
      <span>
        <Badge tone={status.tone}>
          {status.icon} {status.label}
        </Badge>
      </span>
      <StepCell step={pair.a} highlight={!quiet} />
      <StepCell step={pair.b} highlight={!quiet} />
    </div>
  );
}

function StepCell({ step, highlight }: { step: StepSummary | null | undefined; highlight: boolean }) {
  if (!step) return <span className="px-2 text-[12px] text-faint italic">no matching step</span>;
  return (
    <div className={cx("min-w-0 rounded-md px-2 py-1", highlight && "bg-surface-2")} title={step.model ?? undefined}>
      <div className="flex min-w-0 items-center gap-1.5 text-[12px]">
        <span className="tabular shrink-0 font-mono text-muted">#{step.idx}</span>
        {step.error_type ? <Badge tone="error" className="shrink-0">{step.error_type}</Badge> : null}
        {step.is_replay ? <Badge tone="replay" className="shrink-0">replay</Badge> : null}
        <span className="flex min-w-0 gap-1 overflow-hidden">
          {step.tool_calls.map((c, i) => (
            <Badge key={i} tone="tool">{c.name}</Badge>
          ))}
        </span>
        <span className="tabular ml-auto shrink-0 pl-2 text-faint">
          {fmtTokens(step.input_tokens)}→{fmtTokens(step.output_tokens)} · <span className="text-ok">{fmtCost(step.cost_usd)}</span> · {fmtMs(step.latency_ms)}
        </span>
      </div>
      <div className="mt-0.5 truncate text-[12px]">{step.output_preview ?? <span className="text-faint italic">no output</span>}</div>
    </div>
  );
}

// ---------------------------------------------------------------------- content diff
function PairDetail({ pair }: { pair: DiffPair }) {
  const [steps, setSteps] = useState<[StepDetail | null, StepDetail | null] | null>(null);
  const [error, setError] = useState<Error | null>(null);
  useEffect(() => {
    let cancelled = false;
    Promise.all([pair.a ? fetchStep(pair.a.id) : null, pair.b ? fetchStep(pair.b.id) : null])
      .then((r) => !cancelled && setSteps(r as [StepDetail | null, StepDetail | null]))
      .catch((e: Error) => !cancelled && setError(e));
    return () => {
      cancelled = true;
    };
  }, [pair.a, pair.b]);

  return (
    <div className="px-5 py-4" data-testid="pair-detail">
      <div className="mb-4 flex items-center gap-2">
        <h2 className="text-[15px] font-semibold">
          {pair.a ? `A #${pair.a.idx}` : "—"} <span className="text-faint">↔</span> {pair.b ? `B #${pair.b.idx}` : "—"}
        </h2>
        <Badge tone={STATUS[pair.status].tone}>{STATUS[pair.status].label}</Badge>
        {pair.request_changed && pair.a && pair.b ? <Badge tone="tool">request changed</Badge> : null}
        {pair.response_changed && pair.a && pair.b ? <Badge tone="tool">response changed</Badge> : null}
      </div>
      {error ? <ErrorBanner error={error} /> : null}
      {!steps && !error ? <Spinner label="Loading both steps…" /> : null}
      {steps ? <ContentDiff a={steps[0]} b={steps[1]} pair={pair} /> : null}
    </div>
  );
}

type Msg = { role?: string; content?: unknown };

function ContentDiff({ a, b, pair }: { a: StepDetail | null; b: StepDetail | null; pair: DiffPair }) {
  const sections = useMemo(() => buildSections(a, b), [a, b]);
  if (pair.status === "shared") {
    return <p className="text-muted">This step is shared: run B inherited it from run A, so it's the same recorded call.</p>;
  }
  if (!a || !b) {
    const only = (a ?? b) as StepDetail;
    const side = a ? "A" : "B";
    return (
      <div className="space-y-3">
        <p className="text-muted">
          Step #{only.idx} exists only in run {side}
          {only.error ? " — it failed, and failed calls never become part of a fork's history." : "."}
        </p>
        <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5">
          <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">{only.error ? "Error" : "Response"}</div>
          <div className="whitespace-pre-wrap break-words">{only.error ? errorText(only.error) : respText(only) || "—"}</div>
        </div>
      </div>
    );
  }
  return (
    <div className="space-y-5">
      {sections.length === 0 ? <p className="text-muted">No differences in request or response.</p> : null}
      {sections.map((s) => (
        <section key={s.title}>
          <h3 className="mb-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">{s.title}</h3>
          <TextDiff before={s.before} after={s.after} />
        </section>
      ))}
      <Legend />
    </div>
  );
}

function buildSections(a: StepDetail | null, b: StepDetail | null) {
  const sections: { title: string; before: string; after: string }[] = [];
  const add = (title: string, before: string, after: string) => {
    if (before !== after) sections.push({ title, before, after });
  };
  const ra = (a?.request ?? {}) as Record<string, unknown>;
  const rb = (b?.request ?? {}) as Record<string, unknown>;
  add("System prompt", sysText(ra.system), sysText(rb.system));
  const params = (r: Record<string, unknown>) => {
    const { messages: _m, system: _s, tools: _t, ...rest } = r;
    return pretty(rest);
  };
  add("Parameters", a ? params(ra) : "", b ? params(rb) : "");
  const toolNames = (r: Record<string, unknown>) => (Array.isArray(r.tools) ? r.tools.map((t) => String((t as Record<string, unknown>).name)).join("\n") : "");
  add("Tools", toolNames(ra), toolNames(rb));
  const ma = (Array.isArray(ra.messages) ? ra.messages : []) as Msg[];
  const mb = (Array.isArray(rb.messages) ? rb.messages : []) as Msg[];
  for (let i = 0; i < Math.max(ma.length, mb.length); i++) {
    add(`Message [${i}] · ${mb[i]?.role ?? ma[i]?.role ?? ""}`, msgText(ma[i]), msgText(mb[i]));
  }
  add("Response", respText(a), respText(b));
  if (a?.error || b?.error) add("Error", a?.error ? pretty(a.error) : "", b?.error ? pretty(b.error) : "");
  return sections;
}

function errorText(error: Record<string, unknown>): string {
  const body = error.body as { error?: { type?: string; message?: string } } | undefined;
  const detail = body?.error?.message ? `${body.error.type ?? "error"}: ${body.error.message}` : String(error.message ?? "");
  return `${String(error.type ?? "Error")}${error.status_code ? ` ${String(error.status_code)}` : ""} — ${detail}`;
}

function sysText(system: unknown): string {
  if (system === undefined || system === null) return "";
  return typeof system === "string" ? system : textOf(system) || pretty(system);
}

function msgText(m: Msg | undefined): string {
  if (!m) return "";
  return blocksText(m.content);
}

function blocksText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return content === undefined ? "" : pretty(content);
  return content
    .map((block: Record<string, unknown>) => {
      switch (block.type) {
        case "text":
          return String(block.text ?? "");
        case "tool_use":
          return `[tool_use ${String(block.name)}] ${JSON.stringify(block.input)}`;
        case "tool_result":
          return `[tool_result] ${textOf(block.content) || pretty(block.content)}`;
        case "thinking":
          return "[thinking]";
        default:
          return `[${String(block.type)}]`;
      }
    })
    .join("\n");
}

function respText(step: StepDetail | null): string {
  const response = step?.response as { content?: unknown } | null | undefined;
  return response ? blocksText(response.content) : "";
}

function TextDiff({ before, after }: { before: string; after: string }) {
  const parts = useMemo(() => (before.length + after.length > 20000 ? diffLines(before, after) : diffWordsWithSpace(before, after)), [before, after]);
  return (
    <div className="max-h-[420px] overflow-y-auto rounded-lg border border-line bg-surface-2 px-3 py-2.5 font-mono text-[12px] leading-[1.65] break-words whitespace-pre-wrap">
      {parts.map((part, i) =>
        part.added ? (
          <ins key={i} className="rounded-sm bg-ok-soft text-ok no-underline">{part.value}</ins>
        ) : part.removed ? (
          <del key={i} className="rounded-sm bg-error-soft text-error line-through decoration-error/50">{part.value}</del>
        ) : (
          <span key={i} className="text-muted">{part.value}</span>
        ),
      )}
    </div>
  );
}

function Legend() {
  return (
    <div className="flex items-center gap-4 text-[11.5px] text-faint">
      <span><del className="rounded-sm bg-error-soft px-1 text-error">removed</del> only in A</span>
      <span><ins className="rounded-sm bg-ok-soft px-1 text-ok no-underline">added</ins> only in B</span>
      <span className="ml-auto"><kbd className="font-mono">n</kbd> next difference</span>
    </div>
  );
}
