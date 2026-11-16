import { useEffect, useRef } from "react";

type Handler = (event: KeyboardEvent) => void;

function typingInto(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/** Global single-key shortcuts, ignored while typing in a field or with modifiers held. */
export function useKeys(bindings: Record<string, Handler>, enabled = true): void {
  const ref = useRef(bindings);
  ref.current = bindings;
  useEffect(() => {
    if (!enabled) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (typingInto(event.target) && event.key !== "Escape") return;
      const handler = ref.current[event.key];
      if (handler) {
        event.preventDefault();
        handler(event);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled]);
}
