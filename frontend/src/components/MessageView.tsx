// Rendering of recorded messages and content blocks. Everything here is untrusted input:
// strings are rendered as React text, markdown only through SafeMarkdown.
import { AlertTriangle, Bot, Brain, CornerDownRight, FileText, Image, Settings2, User, Wrench } from "lucide-react";
import { type ReactNode, useState } from "react";
import { pretty } from "../lib/format";
import { Collapsible, PlainText } from "./Collapsible";
import { SafeMarkdown } from "./SafeMarkdown";
import { Badge, cx } from "./ui";

type Block = Record<string, unknown>;

export function asBlocks(content: unknown): Block[] {
  if (typeof content === "string") return [{ type: "text", text: content }];
  if (Array.isArray(content)) return content.map((b) => (typeof b === "object" && b !== null ? (b as Block) : { type: "text", text: String(b) }));
  if (content === null || content === undefined) return [];
  return [{ type: "unknown", value: content }];
}

export function textOf(content: unknown): string {
  return asBlocks(content)
    .map((b) => (b.type === "text" ? String(b.text ?? "") : b.type === "tool_result" ? textOf(b.content) : ""))
    .filter(Boolean)
    .join("\n");
}

const ROLE_META: Record<string, { icon: ReactNode; label: string; className: string }> = {
  user: { icon: <User className="size-3.5" />, label: "User", className: "text-info" },
  assistant: { icon: <Bot className="size-3.5" />, label: "Assistant", className: "text-llm" },
  system: { icon: <Settings2 className="size-3.5" />, label: "System", className: "text-replay" },
};

export function MessageCard({ role, content, isNew, index, toolNames }: { role: string; content: unknown; isNew?: boolean; index?: number; toolNames?: Map<string, string> }) {
  const meta = ROLE_META[role] ?? { icon: <FileText className="size-3.5" />, label: role, className: "text-muted" };
  const blocks = asBlocks(content);
  const onlyToolResults = blocks.length > 0 && blocks.every((b) => b.type === "tool_result");
  return (
    <div data-testid="message" className={cx("rounded-lg border bg-surface", isNew ? "border-line-strong" : "border-line")}>
      <div className="flex items-center gap-2 border-b border-line px-3 py-1.5 text-[12px]">
        <span className={cx("inline-flex items-center gap-1.5 font-semibold", meta.className)}>
          {meta.icon}
          {onlyToolResults ? "Tool results" : meta.label}
        </span>
        {index !== undefined ? <span className="font-mono text-[11px] text-faint">[{index}]</span> : null}
        {isNew ? <Badge tone="info">new in this step</Badge> : null}
      </div>
      <div className="space-y-2.5 px-3 py-2.5">
        {blocks.map((block, i) => (
          <BlockView key={i} block={block} role={role} toolNames={toolNames} />
        ))}
        {blocks.length === 0 ? <div className="text-faint italic">(empty)</div> : null}
      </div>
    </div>
  );
}

export function BlockView({ block, role, toolNames, result }: { block: Block; role: string; toolNames?: Map<string, string>; result?: Block }) {
  const type = String(block.type ?? "unknown");
  switch (type) {
    case "text": {
      const text = String(block.text ?? "");
      return role === "assistant" ? <MarkdownOrRaw text={text} /> : <PlainText text={text} />;
    }
    case "thinking":
    case "redacted_thinking":
      return <ThinkingBlock text={type === "thinking" ? String(block.thinking ?? "") : ""} />;
    case "tool_use":
    case "server_tool_use":
    case "mcp_tool_use":
      return <ToolCallCard name={String(block.name ?? "tool")} id={String(block.id ?? "")} input={block.input} result={result} />;
    case "tool_result":
      return (
        <ToolResultCard
          toolUseId={String(block.tool_use_id ?? "")}
          name={toolNames?.get(String(block.tool_use_id ?? ""))}
          content={block.content}
          isError={Boolean(block.is_error)}
        />
      );
    case "image":
    case "document":
      return <MediaPlaceholder block={block} />;
    default:
      return (
        <div>
          <div className="mb-1 text-[11px] font-medium uppercase tracking-wider text-faint">{type}</div>
          <PlainText text={pretty(type === "unknown" ? block.value : block)} mono />
        </div>
      );
  }
}

function MarkdownOrRaw({ text }: { text: string }) {
  const [raw, setRaw] = useState(false);
  return (
    <div className="group relative">
      <button
        type="button"
        onClick={() => setRaw((r) => !r)}
        className="absolute -top-0.5 right-0 rounded px-1.5 text-[10.5px] font-medium text-faint opacity-0 transition-opacity group-hover:opacity-100 hover:text-fg"
        title="Toggle markdown rendering"
      >
        {raw ? "markdown" : "raw"}
      </button>
      <Collapsible maxHeight={320}>{raw ? <div className="whitespace-pre-wrap break-words">{text}</div> : <SafeMarkdown>{text}</SafeMarkdown>}</Collapsible>
    </div>
  );
}

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-dashed border-line-strong px-2.5 py-1.5">
      <button type="button" onClick={() => setOpen((o) => !o)} className="inline-flex items-center gap-1.5 text-[12px] font-medium text-muted hover:text-fg" disabled={!text}>
        <Brain className="size-3.5 text-llm" />
        Thinking {text ? `· ${text.length.toLocaleString()} chars` : "· not returned by the API"}
      </button>
      {open && text ? (
        <div className="mt-1.5 text-muted italic">
          <PlainText text={text} />
        </div>
      ) : null}
    </div>
  );
}

export function ToolCallCard({ name, id, input, result }: { name: string; id: string; input: unknown; result?: Block }) {
  return (
    <div data-testid="tool-call" className="overflow-hidden rounded-md border border-tool/35 bg-tool-soft">
      <div className="flex items-center gap-2 px-2.5 py-1.5 text-[12px]">
        <Wrench className="size-3.5 text-tool" />
        <span className="font-mono font-semibold text-tool">{name}</span>
        <span className="truncate font-mono text-[10.5px] text-faint">{id}</span>
      </div>
      <div className="border-t border-tool/25 bg-surface px-2.5 py-2">
        <PlainText text={pretty(input)} mono maxHeight={180} />
      </div>
      {result ? (
        <div className={cx("border-t px-2.5 py-2", result.is_error ? "border-error/30 bg-error-soft" : "border-tool/25 bg-surface-2")}>
          <div className={cx("mb-1 inline-flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wider", result.is_error ? "text-error" : "text-ok")}>
            <CornerDownRight className="size-3" /> {result.is_error ? "Error result" : "Result"}
          </div>
          <PlainText text={textOf(result.content) || pretty(result.content)} mono maxHeight={180} />
        </div>
      ) : null}
    </div>
  );
}

function ToolResultCard({ toolUseId, name, content, isError }: { toolUseId: string; name?: string; content: unknown; isError: boolean }) {
  return (
    <div data-testid="tool-result" className={cx("overflow-hidden rounded-md border", isError ? "border-error/40 bg-error-soft" : "border-tool/30 bg-surface-2")}>
      <div className="flex items-center gap-2 px-2.5 py-1.5 text-[12px]">
        {isError ? <AlertTriangle className="size-3.5 text-error" /> : <CornerDownRight className="size-3.5 text-tool" />}
        <span className={cx("font-semibold", isError ? "text-error" : "text-tool")}>{name ? `${name} result` : "Tool result"}</span>
        <span className="truncate font-mono text-[10.5px] text-faint">{toolUseId}</span>
      </div>
      <div className="border-t border-line bg-surface px-2.5 py-2">
        <PlainText text={textOf(content) || pretty(content)} mono maxHeight={180} />
      </div>
    </div>
  );
}

function MediaPlaceholder({ block }: { block: Block }) {
  const source = (block.source ?? {}) as Record<string, unknown>;
  const kind = String(block.type);
  const media = String(source.media_type ?? source.type ?? "");
  const size = typeof source.data === "string" ? ` · ${Math.round((source.data.length * 3) / 4 / 1024)} KB` : "";
  return (
    <div className="inline-flex items-center gap-2 rounded-md border border-line bg-surface-2 px-2.5 py-1.5 text-[12px] text-muted">
      {kind === "image" ? <Image className="size-3.5" /> : <FileText className="size-3.5" />}
      {kind} {media ? `(${media}${size})` : ""}
    </div>
  );
}
