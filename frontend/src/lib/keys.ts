import { useEffect, useRef } from "react";

type Handler = (event: KeyboardEvent) => void;

// While a modal (dialog, editor) is open, only its own shortcuts fire.
let openModals = 0;

function typingInto(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/**
 * Global single-key shortcuts, ignored while typing in a field (except Escape) or with
 * modifiers held. Pass `modal` for shortcuts that belong to an open dialog.
 */
export function useKeys(bindings: Record<string, Handler>, enabled = true, modal = false): void {
  const ref = useRef(bindings);
  ref.current = bindings;
  useEffect(() => {
    if (!enabled) return;
    if (modal) openModals += 1;
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (!modal && openModals > 0) return;
      if (typingInto(event.target) && event.key !== "Escape") return;
      const handler = ref.current[event.key];
      if (handler) {
        event.preventDefault();
        handler(event);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (modal) openModals -= 1;
    };
  }, [enabled, modal]);
}
