import { useEffect, useState } from "react";

export interface Toast {
  id: number;
  tone: "ok" | "error";
  title: string;
  body?: string;
}

let toasts: Toast[] = [];
let nextId = 1;
const listeners = new Set<(t: Toast[]) => void>();

function emit() {
  listeners.forEach((fn) => fn(toasts));
}

export function toast(t: Omit<Toast, "id">, ms = 7000): void {
  const id = nextId++;
  toasts = [...toasts, { ...t, id }];
  emit();
  window.setTimeout(() => dismiss(id), ms);
}

export function dismiss(id: number): void {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

export function useToasts(): Toast[] {
  const [state, setState] = useState(toasts);
  useEffect(() => {
    listeners.add(setState);
    return () => {
      listeners.delete(setState);
    };
  }, []);
  return state;
}
