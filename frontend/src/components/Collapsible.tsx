import { ChevronDown, ChevronUp } from "lucide-react";
import { type ReactNode, useLayoutEffect, useRef, useState } from "react";

/** Clamp tall content to `maxHeight` px with a "show all" toggle. */
export function Collapsible({ children, maxHeight = 220, defaultOpen = false }: { children: ReactNode; maxHeight?: number; defaultOpen?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflowing, setOverflowing] = useState(false);
  const [open, setOpen] = useState(defaultOpen);
  const [fullHeight, setFullHeight] = useState(0);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      setFullHeight(el.scrollHeight);
      setOverflowing(el.scrollHeight > maxHeight + 40);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [maxHeight]);

  const clamped = overflowing && !open;
  return (
    <div>
      <div ref={ref} className="relative overflow-hidden" style={clamped ? { maxHeight } : undefined}>
        {children}
        {clamped ? <div className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-surface to-transparent" /> : null}
      </div>
      {overflowing ? (
        <button type="button" onClick={() => setOpen((o) => !o)} className="mt-1 inline-flex items-center gap-1 text-[12px] font-medium text-muted hover:text-fg">
          {open ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
          {open ? "Show less" : `Show all (${Math.max(1, Math.round(fullHeight / 20))} lines)`}
        </button>
      ) : null}
    </div>
  );
}

/** Plain (untrusted) text, preserving whitespace. */
export function PlainText({ text, mono = false, maxHeight }: { text: string; mono?: boolean; maxHeight?: number }) {
  return (
    <Collapsible maxHeight={maxHeight}>
      <div className={mono ? "whitespace-pre-wrap break-words font-mono text-[12px] leading-[1.6]" : "whitespace-pre-wrap break-words"}>{text}</div>
    </Collapsible>
  );
}
