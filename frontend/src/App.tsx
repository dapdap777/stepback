import { GitCompareArrows, Keyboard, List, Moon, Sun } from "lucide-react";
import { useState } from "react";
import { api } from "./api/client";
import { useAsync } from "./api/hooks";
import { Empty, Kbd, cx } from "./components/ui";
import { useKeys } from "./lib/keys";
import { linkProps, paths, useRoute } from "./lib/router";
import { useTheme } from "./lib/theme";
import { DiffPage } from "./pages/DiffPage";
import { RunPage } from "./pages/RunPage";
import { RunsPage } from "./pages/RunsPage";

export function App() {
  const route = useRoute();
  const [theme, toggleTheme] = useTheme();
  const [help, setHelp] = useState(false);
  const health = useAsync("health", () => api.health());
  useKeys({ "?": () => setHelp((h) => !h), t: toggleTheme });
  useKeys({ Escape: () => setHelp(false) }, help);

  return (
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center gap-5 border-b border-line bg-surface px-5">
        <a {...linkProps(paths.runs())} className="flex items-center gap-2 font-semibold tracking-tight">
          <Logo />
          stepback
        </a>
        <nav className="flex items-center gap-1 text-[13px]">
          <NavLink to={paths.runs()} active={route.page === "runs" || route.page === "run"} icon={<List className="size-3.5" />}>
            Runs
          </NavLink>
          <NavLink to={paths.diff()} active={route.page === "diff"} icon={<GitCompareArrows className="size-3.5" />}>
            Compare
          </NavLink>
        </nav>
        <div className="ml-auto flex items-center gap-3">
          {health.data ? (
            <span className="hidden max-w-[28rem] truncate font-mono text-[11px] text-faint lg:inline" title="Trace database">
              {health.data.db_path}
            </span>
          ) : null}
          <button type="button" onClick={() => setHelp(true)} className="grid size-7 place-items-center rounded-md text-muted hover:bg-surface-3 hover:text-fg" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts">
            <Keyboard className="size-4" />
          </button>
          <button
            type="button"
            data-testid="theme-toggle"
            onClick={toggleTheme}
            className="grid size-7 place-items-center rounded-md text-muted hover:bg-surface-3 hover:text-fg"
            title={`Switch to ${theme === "dark" ? "light" : "dark"} theme (t)`}
            aria-label="Toggle theme"
          >
            {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </button>
        </div>
      </header>
      <main className="min-h-0 flex-1">
        {route.page === "runs" ? <RunsPage /> : null}
        {route.page === "run" ? <RunPage runId={route.runId} step={route.step} /> : null}
        {route.page === "diff" ? <DiffPage a={route.a} b={route.b} /> : null}
        {route.page === "notFound" ? <Empty title="Page not found" /> : null}
      </main>
      {help ? <ShortcutsDialog onClose={() => setHelp(false)} /> : null}
    </div>
  );
}

function NavLink({ to, active, icon, children }: { to: string; active: boolean; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <a {...linkProps(to)} className={cx("inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 font-medium", active ? "bg-surface-3 text-fg" : "text-muted hover:text-fg")}>
      {icon}
      {children}
    </a>
  );
}

function Logo() {
  return (
    <svg viewBox="0 0 32 32" className="size-6" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="var(--llm)" />
      <path d="M19 9l-7 7 7 7" fill="none" stroke="#fff" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="23" cy="16" r="2" fill="#fff" />
    </svg>
  );
}

const SHORTCUTS: [string, string[], string][] = [
  ["Everywhere", ["?"], "Show this help"],
  ["Everywhere", ["t"], "Toggle dark / light theme"],
  ["Runs", ["j", "k"], "Move down / up"],
  ["Runs", ["Enter"], "Open run"],
  ["Runs", ["x"], "Select for comparison"],
  ["Runs", ["/"], "Search"],
  ["Run", ["j", "k"], "Next / previous step"],
  ["Run", ["g", "G"], "First / last step"],
  ["Run", ["Esc"], "Close step"],
  ["Run", ["d"], "Compare fork with its parent"],
  ["Compare", ["j", "k"], "Next / previous step"],
  ["Compare", ["n"], "Next difference"],
];

function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4" onClick={onClose} role="dialog" aria-modal="true" aria-label="Keyboard shortcuts">
      <div className="w-full max-w-md rounded-xl border border-line-strong bg-surface p-5 shadow-pop" onClick={(e) => e.stopPropagation()}>
        <h2 className="mb-3 text-[15px] font-semibold">Keyboard shortcuts</h2>
        <table className="w-full text-[13px]">
          <tbody>
            {SHORTCUTS.map(([scope, keys, label], i) => (
              <tr key={i} className="border-b border-line last:border-0">
                <td className="py-1.5 text-[11px] uppercase tracking-wider text-faint">{scope}</td>
                <td className="py-1.5">
                  <span className="inline-flex gap-1">
                    {keys.map((k) => (
                      <Kbd key={k}>{k}</Kbd>
                    ))}
                  </span>
                </td>
                <td className="py-1.5 text-muted">{label}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
