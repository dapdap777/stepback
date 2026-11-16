import { AlertOctagon, ArrowDown, ArrowUp, History, Radio, RotateCcw, Sparkles, X } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import type { ReplayOut, StepDetail as StepDetailT, StepSummary } from "../api/client";
import { useStep } from "../api/hooks";
import { useKeys } from "../lib/keys";
import { linkProps, paths } from "../lib/router";
import { ReplayEditor } from "./ReplayEditor";
import { fmtCost, fmtMs, fmtTokens, pretty } from "../lib/format";
import { PlainText } from "./Collapsible";
import { BlockView, MessageCard, asBlocks } from "./MessageView";
import { Badge, Button, ErrorBanner, Kbd, Spinner, cx } from "./ui";

type Tab = "overview" | "conversation" | "request" | "json";
type Message = { role: string; content: unknown };

export function StepDetailPanel({
  summary,
  position,
  total,
  onPrev,
  onNext,
  onClose,
  onReplayed,
  parentRunId,
}: {
  summary: StepSummary;
  position: number;
  total: number;
  onPrev: () => void;
  onNext: () => void;
  onClose: () => void;
  onReplayed: (result: ReplayOut) => void;
  parentRunId: string | null;
}) {
  const { data, error, loading } = useStep(summary.id);
  const [tab, setTab] = useState<Tab>("overview");
  const [replaying, setReplaying] = useState(false);
  useKeys({ r: () => data && setReplaying(true) }, !replaying);

  return (
    <aside data-testid="step-detail" className="flex h-full min-w-0 flex-col border-l border-line bg-surface">
      <header className="shrink-0 border-b border-line px-4 pt-3">
        <div className="flex items-center gap-2">
          <StepKindIcon summary={summary} />
          <h2 className="text-[15px] font-semibold">Step #{summary.idx}</h2>
          <span className="truncate font-mono text-[12px] text-muted">{summary.model ?? summary.provider}</span>
          <div className="ml-auto flex items-center gap-1">
            <Button
              data-testid="replay-button"
              className="mr-1 border-replay/40 text-replay hover:bg-replay-soft"
              onClick={() => setReplaying(true)}
              disabled={!data || data.id !== summary.id}
              title="Edit this call and re-send it as a new fork (r)"
            >
              <RotateCcw className="size-3.5" /> Replay
            </Button>
            <Button variant="ghost" onClick={onPrev} disabled={position <= 0} title="Previous step (k)" aria-label="Previous step">
              <ArrowUp className="size-3.5" />
            </Button>
            <Button variant="ghost" onClick={onNext} disabled={position >= total - 1} title="Next step (j)" aria-label="Next step">
              <ArrowDown className="size-3.5" />
            </Button>
            <Button variant="ghost" onClick={onClose} title="Close (Esc)" aria-label="Close step">
              <X className="size-3.5" />
            </Button>
          </div>
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {summary.error_type ? (
            <Badge tone="error">
              <AlertOctagon className="size-3" />
              {summary.error_type}
              {summary.error_status ? ` ${summary.error_status}` : ""}
            </Badge>
          ) : summary.stop_reason ? (
            <Badge tone={summary.stop_reason === "tool_use" ? "tool" : summary.stop_reason === "end_turn" ? "ok" : "neutral"}>{summary.stop_reason}</Badge>
          ) : null}
          {summary.streamed ? (
            <Badge tone="info">
              <Radio className="size-3" /> streamed
            </Badge>
          ) : null}
          {summary.incomplete ? <Badge tone="error">incomplete</Badge> : null}
          {summary.is_replay ? (
            <Badge tone="replay">
              <RotateCcw className="size-3" /> replay
            </Badge>
          ) : null}
          {summary.inherited ? (
            <Badge>
              <History className="size-3" /> inherited
            </Badge>
          ) : null}
          <span className="font-mono text-[11px] text-faint">{summary.operation}</span>
        </div>
        <div className="tabular mt-3 grid grid-cols-5 gap-3 text-[12px]">
          <Metric label="Latency" value={fmtMs(summary.latency_ms)} />
          <Metric label="TTFT" value={fmtMs(summary.ttft_ms)} />
          <Metric label="Tokens" value={`${fmtTokens(summary.input_tokens)} → ${fmtTokens(summary.output_tokens)}`} />
          <Metric label="Cache r/w" value={`${fmtTokens(summary.cache_read_tokens)} / ${fmtTokens(summary.cache_write_tokens)}`} />
          <Metric label="Cost" value={fmtCost(summary.cost_usd)} className="text-ok" />
        </div>
        <nav className="mt-3 flex gap-4 text-[12.5px]" role="tablist">
          {(["overview", "conversation", "request", "json"] as Tab[]).map((t) => (
            <button
              key={t}
              type="button"
              role="tab"
              aria-selected={tab === t}
              onClick={() => setTab(t)}
              className={cx("-mb-px border-b-2 pb-2 font-medium capitalize transition-colors", tab === t ? "border-llm text-fg" : "border-transparent text-muted hover:text-fg")}
            >
              {t === "json" ? "Raw JSON" : t}
            </button>
          ))}
        </nav>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {error ? <ErrorBanner error={error} /> : null}
        {loading && !data ? <Spinner label="Loading step…" /> : null}
        {data && data.id === summary.id && data.is_replay ? <ReplayBanner step={data} parentRunId={parentRunId} /> : null}
        {data && data.id === summary.id ? <TabBody tab={tab} step={data} /> : null}
      </div>
      {replaying && data && data.id === summary.id ? (
        <ReplayEditor
          step={data}
          onClose={() => setReplaying(false)}
          onDone={(result) => {
            setReplaying(false);
            onReplayed(result);
          }}
        />
      ) : null}
      <footer className="flex shrink-0 items-center gap-3 border-t border-line px-4 py-1.5 text-[11px] text-faint">
        <span>
          <Kbd>j</Kbd> <Kbd>k</Kbd> navigate
        </span>
        <span>
          <Kbd>Esc</Kbd> close
        </span>
        <span className="ml-auto font-mono">{summary.id}</span>
      </footer>
    </aside>
  );
}

function ReplayBanner({ step, parentRunId }: { step: StepDetailT; parentRunId: string | null }) {
  const overrides = (step.replay_overrides ?? {}) as { changed?: string[] };
  const changed = overrides.changed ?? [];
  return (
    <div data-testid="replay-banner" className="mb-4 rounded-lg border border-replay/30 bg-replay-soft px-3 py-2.5 text-[12.5px]">
      <div className="flex items-center gap-2">
        <RotateCcw className="size-3.5 text-replay" />
        <span className="font-semibold text-replay">Replayed call</span>
        <span className="text-muted">{changed.length ? `edited: ${changed.join(", ")}` : "re-sent unchanged"}</span>
        {parentRunId ? (
          <a {...linkProps(paths.diff(parentRunId, step.run_id))} className="ml-auto font-medium text-replay hover:underline">
            Compare with parent →
          </a>
        ) : null}
      </div>
      <div className="mt-1 text-muted">The fork ends here: replay re-sends one LLM call and doesn't re-run your tool code.</div>
    </div>
  );
}

function Metric({ label, value, className }: { label: string; value: string; className?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10.5px] font-medium uppercase tracking-wider text-faint">{label}</div>
      <div className={cx("truncate font-semibold", className)}>{value}</div>
    </div>
  );
}

export function StepKindIcon({ summary }: { summary: Pick<StepSummary, "error_type" | "is_replay"> }) {
  if (summary.error_type) return <span className="grid size-6 place-items-center rounded-md bg-error-soft text-error"><AlertOctagon className="size-3.5" /></span>;
  if (summary.is_replay) return <span className="grid size-6 place-items-center rounded-md bg-replay-soft text-replay"><RotateCcw className="size-3.5" /></span>;
  return <span className="grid size-6 place-items-center rounded-md bg-llm-soft text-llm"><Sparkles className="size-3.5" /></span>;
}

function TabBody({ tab, step }: { tab: Tab; step: StepDetailT }) {
  const messages = (Array.isArray(step.request.messages) ? step.request.messages : []) as Message[];
  const newFrom = step.parent_message_count === null ? 0 : Math.min(messages.length, step.parent_message_count + 1);
  const toolNames = useMemo(() => toolNameIndex(messages), [messages]);
  switch (tab) {
    case "overview":
      return <Overview step={step} messages={messages} newFrom={newFrom} toolNames={toolNames} />;
    case "conversation":
      return <Conversation step={step} messages={messages} newFrom={newFrom} toolNames={toolNames} />;
    case "request":
      return <RequestView step={step} />;
    case "json":
      return <PlainText text={pretty(step)} mono maxHeight={2000} />;
  }
}

function toolNameIndex(messages: Message[]): Map<string, string> {
  const names = new Map<string, string>();
  for (const m of messages) for (const b of asBlocks(m.content)) if (b.type === "tool_use" && typeof b.id === "string") names.set(b.id, String(b.name));
  return names;
}

function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="mb-5">
      <div className="mb-2 flex items-center gap-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-faint">{title}</h3>
        {aside}
      </div>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

function Overview({ step, messages, newFrom, toolNames }: { step: StepDetailT; messages: Message[]; newFrom: number; toolNames: Map<string, string> }) {
  const results = new Map(step.tool_results.map((r) => [r.tool_use_id ?? "", r as unknown as Record<string, unknown>]));
  const response = step.response as { content?: unknown } | null;
  const newMessages = messages.slice(newFrom);
  return (
    <>
      {step.error ? <ErrorCard error={step.error} /> : null}
      <Section title={newFrom > 0 ? "New input" : "Input"} aside={newFrom > 0 ? <span className="text-[11px] text-faint">+ {newFrom} earlier messages in Conversation</span> : null}>
        {newMessages.length === 0 ? <div className="text-faint italic">No new messages.</div> : null}
        {newMessages.map((m, i) => (
          <MessageCard key={i} role={m.role} content={m.content} index={newFrom + i} toolNames={toolNames} />
        ))}
      </Section>
      <Section title="Output">
        {response ? (
          <div className="rounded-lg border border-llm/30 bg-surface">
            <div className="flex items-center gap-2 border-b border-line px-3 py-1.5 text-[12px] font-semibold text-llm">
              <Sparkles className="size-3.5" /> Response
              {step.stop_reason ? <span className="font-normal text-muted">· {step.stop_reason}</span> : null}
            </div>
            <div className="space-y-2.5 px-3 py-2.5">
              {asBlocks(response.content).map((b, i) => (
                <BlockView key={i} block={b} role="assistant" result={typeof b.id === "string" ? results.get(b.id) : undefined} />
              ))}
            </div>
          </div>
        ) : (
          <div className="text-faint italic">{step.error ? "No response (the call failed)." : "No response recorded."}</div>
        )}
      </Section>
    </>
  );
}

function ErrorCard({ error }: { error: Record<string, unknown> }) {
  const body = error.body as { error?: { type?: string; message?: string } } | undefined;
  const message = body?.error?.message ?? String(error.message ?? "");
  return (
    <div data-testid="error-card" className="mb-5 rounded-lg border border-error/40 bg-error-soft px-3 py-2.5">
      <div className="flex items-center gap-2 font-semibold text-error">
        <AlertOctagon className="size-4" />
        {String(error.type ?? "Error")}
        {error.status_code ? <span className="font-mono text-[12px]">{String(error.status_code)}</span> : null}
        {body?.error?.type ? <span className="font-mono text-[12px] font-normal">{body.error.type}</span> : null}
      </div>
      <div className="mt-1 whitespace-pre-wrap break-words text-fg">{message}</div>
    </div>
  );
}

function Conversation({ step, messages, newFrom, toolNames }: { step: StepDetailT; messages: Message[]; newFrom: number; toolNames: Map<string, string> }) {
  const [showOld, setShowOld] = useState(false);
  const system = step.request.system;
  return (
    <>
      {system ? (
        <Section title="System prompt">
          <MessageCard role="system" content={system} />
        </Section>
      ) : null}
      <Section title={`Messages (${messages.length})`}>
        {newFrom > 0 && !showOld ? (
          <button type="button" onClick={() => setShowOld(true)} className="w-full rounded-lg border border-dashed border-line-strong py-2 text-[12px] font-medium text-muted hover:text-fg">
            Show {newFrom} earlier message{newFrom === 1 ? "" : "s"}
          </button>
        ) : null}
        {messages.map((m, i) =>
          i < newFrom && !showOld ? null : <MessageCard key={i} role={m.role} content={m.content} index={i} isNew={i >= newFrom && newFrom > 0} toolNames={toolNames} />,
        )}
      </Section>
    </>
  );
}

function RequestView({ step }: { step: StepDetailT }) {
  const { system, messages: _messages, tools, ...params } = step.request as Record<string, unknown>;
  const toolList = (Array.isArray(tools) ? tools : []) as Record<string, unknown>[];
  return (
    <>
      <Section title="Parameters">
        <div className="overflow-hidden rounded-lg border border-line">
          <table className="w-full text-[12.5px]">
            <tbody>
              {Object.entries(params).map(([key, value]) => (
                <tr key={key} className="border-b border-line last:border-0">
                  <td className="w-40 bg-surface-2 px-3 py-1.5 align-top font-mono text-muted">{key}</td>
                  <td className="px-3 py-1.5 font-mono break-all">{typeof value === "string" ? value : JSON.stringify(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
      {system ? (
        <Section title="System prompt">
          <div className="rounded-lg border border-line bg-surface px-3 py-2.5">
            <PlainText text={typeof system === "string" ? system : pretty(system)} />
          </div>
        </Section>
      ) : null}
      {toolList.length ? (
        <Section title={`Tools (${toolList.length})`}>
          {toolList.map((tool, i) => (
            <details key={i} className="rounded-lg border border-line bg-surface px-3 py-2">
              <summary className="cursor-pointer font-mono text-[12.5px] font-semibold text-tool">
                {String(tool.name ?? tool.type ?? "tool")}
                {tool.description ? <span className="ml-2 font-sans font-normal text-muted">{String(tool.description)}</span> : null}
              </summary>
              <div className="mt-2">
                <PlainText text={pretty(tool.input_schema ?? tool)} mono />
              </div>
            </details>
          ))}
        </Section>
      ) : null}
    </>
  );
}
