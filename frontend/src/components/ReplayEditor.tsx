// Replay editor: edit one recorded request and re-send it as a new forked run.
//
// Nothing is sent without an explicit action: "Review cost" only validates and estimates
// (free token counting); the paid request goes out when the user clicks "Send replay".
import { AlertTriangle, ArrowLeft, Info, KeyRound, Loader2, RotateCcw, Send, Undo2, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { ApiError, api, type FieldError, type ReplayEstimate, type ReplayOut, type ReplayStatus, type StepDetail } from "../api/client";
import { fmtCost, fmtTokens } from "../lib/format";
import { useKeys } from "../lib/keys";
import { Badge, Button, cx } from "./ui";

type FieldKey = "model" | "system" | "messages" | "tools" | "params";
const CORE = new Set(["model", "system", "messages", "tools", "stream"]);

interface Draft {
  model: string;
  system: string;
  messages: string;
  tools: string;
  params: string;
}

function initialDraft(request: Record<string, unknown>): { draft: Draft; systemIsText: boolean } {
  const { system, messages, tools } = request;
  const params = Object.fromEntries(Object.entries(request).filter(([k]) => !CORE.has(k)));
  const systemIsText = system === undefined || system === null || typeof system === "string";
  return {
    systemIsText,
    draft: {
      model: String(request.model ?? ""),
      system: systemIsText ? String(system ?? "") : JSON.stringify(system, null, 2),
      messages: JSON.stringify(messages ?? [], null, 2),
      tools: tools === undefined ? "" : JSON.stringify(tools, null, 2),
      params: JSON.stringify(params, null, 2),
    },
  };
}

function parseJson(text: string): { value?: unknown; error?: string } {
  try {
    return { value: JSON.parse(text) };
  } catch (e) {
    return { error: `Invalid JSON: ${(e as Error).message}` };
  }
}

/** Build the request from the draft, collecting per-field errors (client-side checks). */
function buildRequest(draft: Draft, systemIsText: boolean): { request: Record<string, unknown> | null; errors: Partial<Record<FieldKey, string>> } {
  const errors: Partial<Record<FieldKey, string>> = {};
  const request: Record<string, unknown> = {};

  if (!draft.model.trim()) errors.model = "Choose a model.";
  else request.model = draft.model.trim();

  if (systemIsText) {
    if (draft.system.trim()) request.system = draft.system;
  } else if (draft.system.trim()) {
    const parsed = parseJson(draft.system);
    if (parsed.error) errors.system = parsed.error;
    else if (!Array.isArray(parsed.value) && typeof parsed.value !== "string") errors.system = "Must be a string or an array of text blocks.";
    else request.system = parsed.value;
  }

  const messages = parseJson(draft.messages);
  if (messages.error) errors.messages = messages.error;
  else if (!Array.isArray(messages.value) || messages.value.length === 0) errors.messages = "Must be a non-empty array of messages.";
  else request.messages = messages.value;

  if (draft.tools.trim()) {
    const tools = parseJson(draft.tools);
    if (tools.error) errors.tools = tools.error;
    else if (!Array.isArray(tools.value)) errors.tools = "Must be an array of tool definitions.";
    else request.tools = tools.value;
  }

  const params = parseJson(draft.params || "{}");
  if (params.error) errors.params = params.error;
  else if (typeof params.value !== "object" || params.value === null || Array.isArray(params.value)) errors.params = "Must be a JSON object.";
  else {
    const p = params.value as Record<string, unknown>;
    for (const key of ["model", "system", "messages", "tools"]) {
      if (key in p) errors.params = `Edit "${key}" in its own field, not in parameters.`;
    }
    const maxTokens = p.max_tokens;
    if (!(typeof maxTokens === "number" && Number.isInteger(maxTokens) && maxTokens > 0)) errors.params = errors.params ?? '"max_tokens" must be a positive integer.';
    Object.assign(request, p);
  }
  return { request: Object.keys(errors).length ? null : request, errors };
}

/** Map server error paths (messages[0].role, params.max_tokens, ...) to editor fields. */
function fieldOf(path: string): FieldKey {
  const head = path.split(/[.[]/)[0] ?? "";
  return head === "model" || head === "system" || head === "messages" || head === "tools" ? head : "params";
}

export function ReplayEditor({ step, onClose, onDone }: { step: StepDetail; onClose: () => void; onDone: (result: ReplayOut) => void }) {
  const original = useMemo(() => initialDraft(step.request as Record<string, unknown>), [step.request]);
  const [draft, setDraft] = useState<Draft>(original.draft);
  const [status, setStatus] = useState<ReplayStatus | null>(null);
  const [serverErrors, setServerErrors] = useState<FieldError[]>([]);
  const [estimate, setEstimate] = useState<ReplayEstimate | null>(null);
  const [busy, setBusy] = useState<"estimate" | "send" | null>(null);
  const [failure, setFailure] = useState<{ message: string; code: string | null } | null>(null);

  useEffect(() => {
    api.replayStatus().then(setStatus).catch(() => setStatus(null));
  }, []);
  useKeys({ Escape: () => (estimate && !busy ? setEstimate(null) : !busy && onClose()) }, true, true);

  const built = useMemo(() => buildRequest(draft, original.systemIsText), [draft, original.systemIsText]);
  const fieldErrors: Partial<Record<FieldKey, string[]>> = {};
  for (const [key, message] of Object.entries(built.errors)) fieldErrors[key as FieldKey] = [message as string];
  for (const e of serverErrors) (fieldErrors[fieldOf(e.path)] ??= []).push(`${e.path}: ${e.message}`);
  const edited = (Object.keys(draft) as FieldKey[]).filter((k) => draft[k] !== original.draft[k]);
  const credentialsMissing = status !== null && (!status.credentials_configured || !status.sdk_installed);

  const update = (key: FieldKey, value: string) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setServerErrors((errs) => errs.filter((e) => fieldOf(e.path) !== key));
    setEstimate(null);
    setFailure(null);
  };

  const review = async () => {
    if (!built.request) return;
    setBusy("estimate");
    setFailure(null);
    try {
      setEstimate(await api.estimateReplay(step.id, built.request));
      setServerErrors([]);
    } catch (e) {
      const err = e as ApiError;
      if (err.fieldErrors?.length) setServerErrors(err.fieldErrors);
      else setFailure({ message: err.message, code: err.code ?? null });
    } finally {
      setBusy(null);
    }
  };

  const send = async () => {
    if (!built.request || !estimate) return;
    setBusy("send");
    setFailure(null);
    try {
      onDone(await api.replay(step.id, built.request));
    } catch (e) {
      const err = e as ApiError;
      if (err.fieldErrors?.length) {
        setServerErrors(err.fieldErrors);
        setEstimate(null);
      } else setFailure({ message: err.message, code: err.code ?? null });
      setBusy(null);
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-black/50" role="dialog" aria-modal="true" aria-label={`Replay step ${step.idx}`} data-testid="replay-editor">
      <div className="flex h-full w-full max-w-[880px] flex-col border-l border-line-strong bg-surface shadow-pop">
        <header className="flex items-center gap-2 border-b border-line px-5 py-3">
          <span className="grid size-7 place-items-center rounded-md bg-replay-soft text-replay">
            <RotateCcw className="size-4" />
          </span>
          <h2 className="text-[15px] font-semibold">Replay step #{step.idx}</h2>
          {edited.length ? (
            <Badge tone="replay">edited: {edited.join(", ")}</Badge>
          ) : (
            <Badge>unchanged</Badge>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose} disabled={busy === "send"} aria-label="Close replay editor">
            <X className="size-4" />
          </Button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
          <div className="flex gap-2.5 rounded-lg border border-replay/30 bg-replay-soft px-3 py-2.5 text-[12.5px]" data-testid="replay-limits">
            <Info className="mt-0.5 size-4 shrink-0 text-replay" />
            <div>
              Replay re-sends <b>this one LLM call</b> with your edits and saves the result as a <b>new forked run</b> linked to step #{step.idx}.
              Your tool code is <b>not</b> re-run, so the fork ends at this step. The original run is never modified.
            </div>
          </div>

          <CredentialNotice status={status} />

          <Field label="Model" error={fieldErrors.model} edited={draft.model !== original.draft.model} onReset={() => update("model", original.draft.model)} hint={`recorded with ${original.draft.model}`}>
            <input
              data-testid="replay-model"
              list="replay-models"
              value={draft.model}
              onChange={(e) => update("model", e.target.value)}
              className="h-9 w-full rounded-md border border-line bg-surface-2 px-2.5 font-mono text-[13px] outline-none focus:border-llm"
              spellCheck={false}
            />
            <datalist id="replay-models">
              {(status?.known_models ?? []).map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </Field>

          <Field label={original.systemIsText ? "System prompt" : "System prompt (JSON blocks)"} error={fieldErrors.system} edited={draft.system !== original.draft.system} onReset={() => update("system", original.draft.system)}>
            <Editor testId="replay-system" value={draft.system} onChange={(v) => update("system", v)} rows={5} mono={!original.systemIsText} placeholder="(no system prompt)" />
          </Field>

          <Field label="Messages (JSON)" error={fieldErrors.messages} edited={draft.messages !== original.draft.messages} onReset={() => update("messages", original.draft.messages)} hint="role: user | assistant; content: string or content blocks">
            <Editor testId="replay-messages" value={draft.messages} onChange={(v) => update("messages", v)} rows={14} mono />
          </Field>

          <Field label="Tools (JSON)" error={fieldErrors.tools} edited={draft.tools !== original.draft.tools} onReset={() => update("tools", original.draft.tools)} hint="definitions only: replay doesn't run your tool code">
            <Editor testId="replay-tools" value={draft.tools} onChange={(v) => update("tools", v)} rows={6} mono placeholder="(no tools)" />
          </Field>

          <Field label="Parameters (JSON)" error={fieldErrors.params} edited={draft.params !== original.draft.params} onReset={() => update("params", original.draft.params)} hint="max_tokens, thinking, output_config, tool_choice, … (stream is ignored)">
            <Editor testId="replay-params" value={draft.params} onChange={(v) => update("params", v)} rows={6} mono />
          </Field>
        </div>

        <footer className="border-t border-line px-5 py-3">
          {failure ? (
            <div data-testid="replay-failure" className="mb-3 flex gap-2 rounded-lg border border-error/40 bg-error-soft px-3 py-2 text-[12.5px] text-error">
              {failure.code === "missing_credentials" ? <KeyRound className="mt-0.5 size-4 shrink-0" /> : <AlertTriangle className="mt-0.5 size-4 shrink-0" />}
              <div className="whitespace-pre-wrap text-fg">{failure.message}</div>
            </div>
          ) : null}
          {estimate ? (
            <ConfirmPanel estimate={estimate} sending={busy === "send"} disabled={credentialsMissing} mock={status?.mock ?? false} onBack={() => setEstimate(null)} onSend={send} />
          ) : (
            <div className="flex items-center gap-2">
              <span className="text-[12px] text-faint">Review shows the model, input tokens and estimated cost before anything is sent.</span>
              <Button className="ml-auto" onClick={onClose}>Cancel</Button>
              <Button data-testid="replay-review" variant="primary" className="bg-replay" onClick={review} disabled={!built.request || busy !== null}>
                {busy === "estimate" ? <Loader2 className="size-3.5 animate-spin" /> : null}
                Review cost
              </Button>
            </div>
          )}
        </footer>
      </div>
    </div>
  );
}

function CredentialNotice({ status }: { status: ReplayStatus | null }) {
  if (!status) return null;
  if (status.mock) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-muted" data-testid="replay-credentials">
        <KeyRound className="size-3.5 text-tool" />
        Offline mode (<code className="font-mono">stepback ui --mock</code>): replays go to the bundled mock API. Nothing is sent or billed.
      </div>
    );
  }
  if (status.sdk_installed && status.credentials_configured) {
    return (
      <div className="flex items-center gap-2 text-[12px] text-muted" data-testid="replay-credentials">
        <KeyRound className="size-3.5 text-ok" />
        Sends with the key from <code className="font-mono">{status.credential_source}</code> in the server's environment, read at send time and never stored.
      </div>
    );
  }
  return (
    <div data-testid="replay-credentials-missing" className="flex gap-2.5 rounded-lg border border-tool/40 bg-tool-soft px-3 py-2.5 text-[12.5px]">
      <KeyRound className="mt-0.5 size-4 shrink-0 text-tool" />
      <div className="min-w-0 whitespace-pre-wrap">{status.message}</div>
    </div>
  );
}

function ConfirmPanel({ estimate, sending, disabled, mock, onBack, onSend }: { estimate: ReplayEstimate; sending: boolean; disabled: boolean; mock: boolean; onBack: () => void; onSend: () => void }) {
  const approx = estimate.exact ? "" : "≈ ";
  return (
    <div data-testid="replay-confirm">
      <div className="mb-3 grid grid-cols-4 gap-4 rounded-lg border border-line bg-surface-2 px-4 py-3">
        <Metric label="Model" value={<span className="font-mono text-[13px]">{estimate.model}</span>} />
        <Metric
          label="Input tokens"
          value={`${approx}${fmtTokens(estimate.input_tokens)}`}
          sub={estimate.exact ? (mock ? "counted by the offline mock" : "counted by the API") : estimate.note ?? "estimated locally"}
          testId="replay-input-tokens"
        />
        <Metric
          label="Estimated cost"
          value={estimate.price_known ? `${approx}${fmtCost(estimate.input_cost_usd)} – ${fmtCost(estimate.max_total_cost_usd)}` : "unknown model price"}
          sub={
            estimate.price_known
              ? `${mock ? "if sent for real: " : ""}input, up to ${fmtTokens(estimate.max_tokens)} output tokens`
              : "add it to prices.toml"
          }
          testId="replay-cost"
        />
        <Metric label="Original call" value={fmtCost(estimate.original_cost_usd)} sub={estimate.original_input_tokens ? `${fmtTokens(estimate.original_input_tokens)} input tokens` : undefined} />
      </div>
      <div className="flex items-center gap-2">
        <span className="text-[12px] text-muted">
          {mock ? "Sends 1 request to the offline mock API. Nothing is billed." : "Sends 1 request to the Anthropic API, billed to your account."}
        </span>
        <Button className="ml-auto" onClick={onBack} disabled={sending}>
          <ArrowLeft className="size-3.5" /> Back to edit
        </Button>
        <Button data-testid="replay-send" variant="primary" className="bg-replay" onClick={onSend} disabled={sending || disabled}>
          {sending ? <Loader2 className="size-3.5 animate-spin" /> : <Send className="size-3.5" />}
          {sending ? "Sending…" : "Send replay"}
        </Button>
      </div>
    </div>
  );
}

function Metric({ label, value, sub, testId }: { label: string; value: React.ReactNode; sub?: string; testId?: string }) {
  return (
    <div className="min-w-0" data-testid={testId}>
      <div className="text-[10.5px] font-semibold uppercase tracking-wider text-faint">{label}</div>
      <div className="tabular mt-0.5 truncate text-[14px] font-semibold">{value}</div>
      {sub ? <div className="truncate text-[11.5px] text-muted">{sub}</div> : null}
    </div>
  );
}

function Field({ label, hint, error, edited, onReset, children }: { label: string; hint?: string; error?: string[]; edited: boolean; onReset: () => void; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="mb-1 flex items-center gap-2 text-[12px]">
        <span className="font-semibold text-fg">{label}</span>
        {edited ? <Badge tone="replay">edited</Badge> : null}
        {hint ? <span className="truncate text-faint">{hint}</span> : null}
        {edited ? (
          <button type="button" onClick={(e) => (e.preventDefault(), onReset())} className="ml-auto inline-flex items-center gap-1 text-muted hover:text-fg">
            <Undo2 className="size-3" /> reset
          </button>
        ) : null}
      </div>
      {children}
      {error?.length ? (
        <ul className="mt-1 space-y-0.5 text-[12px] text-error" data-testid="field-error">
          {error.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      ) : null}
    </label>
  );
}

function Editor({ value, onChange, rows, mono, placeholder, testId }: { value: string; onChange: (v: string) => void; rows: number; mono?: boolean; placeholder?: string; testId?: string }) {
  return (
    <textarea
      data-testid={testId}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      rows={rows}
      spellCheck={false}
      placeholder={placeholder}
      className={cx(
        "w-full resize-y rounded-md border border-line bg-surface-2 px-2.5 py-2 text-[12.5px] leading-[1.55] outline-none focus:border-llm",
        mono && "font-mono text-[12px]",
      )}
    />
  );
}
