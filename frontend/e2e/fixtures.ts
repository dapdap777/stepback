import { test as base, expect, type Page } from "@playwright/test";

export interface Guard {
  problems: string[];
  requests: string[];
}

/**
 * Every test fails if the page logs an error, violates the CSP, or makes any request
 * that leaves the local server's origin.
 */
export const test = base.extend<{ guard: Guard }>({
  guard: async ({ page, baseURL }, use) => {
    const guard: Guard = { problems: [], requests: [] };
    await page.addInitScript(() => {
      (window as unknown as { __violations: string[] }).__violations = [];
      document.addEventListener("securitypolicyviolation", (e) => {
        (window as unknown as { __violations: string[] }).__violations.push(`${e.violatedDirective}: ${e.blockedURI}`);
      });
    });
    page.on("console", (msg) => {
      if (msg.type() === "error") guard.problems.push(`console: ${msg.text()}`);
    });
    page.on("pageerror", (err) => guard.problems.push(`pageerror: ${err.message}`));
    page.on("request", (req) => {
      guard.requests.push(req.url());
      if (!req.url().startsWith(baseURL ?? "") && !req.url().startsWith("data:")) guard.problems.push(`external request: ${req.url()}`);
    });
    await use(guard);
    const violations = await page.evaluate(() => (window as unknown as { __violations?: string[] }).__violations ?? []).catch(() => []);
    expect([...guard.problems, ...violations.map((v) => `csp: ${v}`)]).toEqual([]);
  },
});

export { expect };

export async function openRun(page: Page, name: string): Promise<void> {
  await page.goto("/");
  await page.getByTestId("runs-table").getByRole("link", { name, exact: true }).click();
  await expect(page.getByTestId("run-title")).toHaveText(name);
}
