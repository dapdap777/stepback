import { expect, openRun, test } from "./fixtures";

test("run list → run → step → diff", async ({ page, guard: _guard }) => {
  // 1. Run list
  await page.goto("/");
  const rows = page.getByTestId("run-row");
  await expect(rows).toHaveCount(6);
  await expect(rows.first()).toContainText("repo-triage-agent (replay #9)");
  await expect(page.getByTestId("runs-table")).toContainText("$0.1340");

  // 2. Open a run: the timeline is the main view
  await openRun(page, "repo-triage-agent");
  const steps = page.getByTestId("step-row");
  await expect(steps).toHaveCount(10);
  await expect(steps.nth(6)).toContainText("OverloadedError 529");
  await expect(steps.nth(0)).toContainText("search_issues");

  // 3. Select a step: its body loads on demand
  await steps.nth(1).click();
  const detail = page.getByTestId("step-detail");
  await expect(detail).toContainText("Step #1");
  await expect(detail.getByTestId("tool-call")).toContainText("read_file");
  await expect(detail.getByTestId("tool-call")).toContainText("def export_report"); // matched result
  await expect(page).toHaveURL(/step=1$/);

  // Keyboard: j / k move between steps
  await page.keyboard.press("j");
  await expect(detail).toContainText("Step #2");
  await expect(steps.nth(2)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("k");
  await expect(detail).toContainText("Step #1");

  // Errors are distinct
  await steps.nth(6).click();
  await expect(detail.getByTestId("error-card")).toContainText("Overloaded");

  // 4. Diff: open the fork, compare with its parent
  await page.getByRole("link", { name: "repo-triage-agent (replay #9)" }).click();
  await expect(page.getByTestId("run-title")).toHaveText("repo-triage-agent (replay #9)");
  await page.getByTestId("compare-parent").click();
  const diff = page.getByTestId("diff-view");
  await expect(diff).toContainText("first divergence at step #6");
  const pairs = page.getByTestId("diff-pair");
  await expect(pairs).toHaveCount(10);
  await expect(page.locator('[data-testid=diff-pair][data-status="shared"]')).toHaveCount(8);
  await expect(page.locator('[data-testid=diff-pair][data-status="removed"]')).toHaveCount(1);
  await expect(page.getByTestId("pair-detail")).toContainText("System prompt");
  await expect(page.getByTestId("pair-detail").locator("ins").first()).toBeVisible();
});

test("keyboard navigation on the run list", async ({ page, guard: _guard }) => {
  await page.goto("/");
  await expect(page.getByTestId("run-row")).toHaveCount(6);
  await page.keyboard.press("j");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("run-title")).toHaveText("repo-triage-agent");
  await expect(page.getByTestId("step-row")).toHaveCount(10); // steps loaded
  await page.keyboard.press("j");
  await expect(page.getByTestId("step-detail")).toContainText("Step #0");
  await page.keyboard.press("G");
  await expect(page.getByTestId("step-detail")).toContainText("Step #9");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("step-detail")).toHaveCount(0);
});

test("dark by default, light theme persists", async ({ page, guard: _guard }) => {
  await page.goto("/");
  const html = page.locator("html");
  await expect(html).not.toHaveClass(/light/);
  const darkBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  await page.getByTestId("theme-toggle").click();
  await expect(html).toHaveClass(/light/);
  const lightBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  expect(lightBg).not.toEqual(darkBg);
  await page.reload();
  await expect(html).toHaveClass(/light/);
});
