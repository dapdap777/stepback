import { Loader2 } from "lucide-react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(" ");
}

export type Tone = "llm" | "tool" | "error" | "ok" | "replay" | "info" | "neutral";

const TONES: Record<Tone, string> = {
  llm: "bg-llm-soft text-llm",
  tool: "bg-tool-soft text-tool",
  error: "bg-error-soft text-error",
  ok: "bg-ok-soft text-ok",
  replay: "bg-replay-soft text-replay",
  info: "bg-info-soft text-info",
  neutral: "bg-surface-3 text-muted",
};

export function Badge({ tone = "neutral", children, title, className }: { tone?: Tone; children: ReactNode; title?: string; className?: string }) {
  return (
    <span title={title} className={cx("inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-px text-[11px] font-medium", TONES[tone], className)}>
      {children}
    </span>
  );
}

export function Button({ variant = "default", className, children, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "primary" | "ghost" | "danger" }) {
  const styles = {
    default: "border border-line bg-surface-2 hover:bg-surface-3 text-fg",
    primary: "bg-llm text-white hover:brightness-110",
    ghost: "text-muted hover:text-fg hover:bg-surface-3",
    danger: "border border-line text-error hover:bg-error-soft",
  }[variant];
  return (
    <button type="button" className={cx("inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[12.5px] font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40", styles, className)} {...rest}>
      {children}
    </button>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="inline-flex min-w-[1.4em] items-center justify-center rounded border border-line-strong bg-surface-2 px-1 font-mono text-[10.5px] text-muted">{children}</kbd>;
}

export function StatusDot({ status }: { status: string }) {
  const color = status === "ok" ? "bg-ok" : status === "error" ? "bg-error" : "bg-tool animate-pulse";
  return <span className={cx("inline-block size-2 shrink-0 rounded-full", color)} title={status} />;
}

export function Stat({ label, value, tone, sub }: { label: string; value: ReactNode; tone?: "ok" | "info" | "error"; sub?: ReactNode }) {
  const color = tone === "ok" ? "text-ok" : tone === "info" ? "text-info" : tone === "error" ? "text-error" : "text-fg";
  return (
    <div className="min-w-0">
      <div className="text-[11px] font-medium uppercase tracking-wider text-faint">{label}</div>
      <div className={cx("tabular mt-0.5 truncate text-[15px] font-semibold", color)}>{value}</div>
      {sub ? <div className="tabular truncate text-[11.5px] text-muted">{sub}</div> : null}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-muted">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  );
}

export function ErrorBanner({ error }: { error: Error }) {
  return <div className="rounded-lg border border-error/40 bg-error-soft px-3 py-2 text-error">{error.message}</div>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16 text-center text-muted">
      <div className="text-[15px] font-semibold text-fg">{title}</div>
      {children}
    </div>
  );
}
