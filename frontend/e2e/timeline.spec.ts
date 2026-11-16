import { expect, openRun, test } from "./fixtures";

test("3,000-step run is virtualized and loads bodies on demand", async ({ page, guard }) => {
  await openRun(page, "long-run");
  const timeline = page.getByTestId("timeline");
  await expect(page.getByTestId("step-row").first()).toContainText("#0");

  // Only the visible window is in the DOM.
  const rendered = await page.getByTestId("step-row").count();
  expect(rendered).toBeGreaterThan(5);
  expect(rendered).toBeLessThan(60);

  // No step bodies fetched until a step is selected.
  expect(guard.requests.filter((u) => u.includes("/api/steps/"))).toEqual([]);

  // Scroll through the whole run.
  for (const fraction of [0.25, 0.5, 0.75, 1]) {
    await timeline.evaluate((el, f) => (el.scrollTop = el.scrollHeight * f), fraction);
    await page.waitForTimeout(80);
  }
  await expect(page.locator('[data-testid=step-row][data-step-index="2999"]')).toBeVisible();
  expect(await page.getByTestId("step-row").count()).toBeLessThan(60);

  // Step summaries came in a handful of pages, not 3,000 requests.
  const pageRequests = guard.requests.filter((u) => /\/api\/runs\/[^/]+\/steps/.test(u));
  expect(pageRequests.length).toBeGreaterThanOrEqual(2);
  expect(pageRequests.length).toBeLessThanOrEqual(15);

  // Jump to the last step with the keyboard; its body loads on selection.
  await page.keyboard.press("G");
  await expect(page.getByTestId("step-detail")).toContainText("Step #2999");
  expect(guard.requests.filter((u) => u.includes("/api/steps/")).length).toBe(1);

  // Smoothness proxy: a scroll frame stays well under a frame budget.
  const frameMs = await timeline.evaluate(async (el) => {
    const times: number[] = [];
    for (let i = 0; i < 30; i++) {
      const t0 = performance.now();
      el.scrollTop = (el.scrollHeight * i) / 30;
      await new Promise(requestAnimationFrame);
      times.push(performance.now() - t0);
    }
    times.sort((a, b) => a - b);
    return times[Math.floor(times.length * 0.9)] ?? 0;
  });
  expect(frameMs).toBeLessThan(50);
});
