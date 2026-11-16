import { useEffect, useState } from "react";

export type Theme = "dark" | "light";
const KEY = "stepline.theme";

function stored(): Theme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

/** Dark by default; `.light` on <html> opts into the light palette. */
export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("light", theme === "light");
}

export function initTheme(): void {
  applyTheme(stored());
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(stored);
  useEffect(() => {
    applyTheme(theme);
    try {
      localStorage.setItem(KEY, theme);
    } catch {
      /* private mode */
    }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))];
}
