import { expect, openRun, test } from "./fixtures";

// The seeded "xss-probe" run carries <script>, <img onerror>, <iframe>, <svg onload>,
// javascript: links and remote images in its system prompt, user message, model output
// (rendered as markdown), tool input and tool result. None of it may execute or load.
const TITLE = "xss-probe <script>alert(1)</script>";

test("recorded content is inert: no script runs, nothing loads", async ({ page, guard }) => {
  await openRun(page, TITLE);
  const detail = page.getByTestId("step-detail");

  for (const index of [0, 1]) {
    await page.getByTestId("step-row").nth(index).click();
    await expect(detail).toContainText(`Step #${index}`);
    for (const tab of ["Overview", "Conversation", "Request", "Raw JSON"]) {
      await detail.getByRole("tab", { name: tab }).click();
      await page.waitForTimeout(50);
      // Payloads are shown as literal text...
      await expect(detail).toContainText("window.__stepline_pwned");
      // ...and never become live elements.
      expect(await detail.locator("script, iframe, img, object, embed, svg[onload]").count()).toBe(0);
      expect(await detail.locator('a[href^="javascript:" i]').count()).toBe(0);
      expect(await page.locator("[onerror], [onload]").count()).toBe(0);
    }
  }

  // Markdown with HTML shows the tags as text (and the raw toggle shows the source).
  await page.getByTestId("step-row").nth(0).click();
  await detail.getByRole("tab", { name: "Overview" }).click();
  const markdown = detail.locator(".prose-sb").first();
  await expect(markdown).toContainText("Rendering markdown with payloads");
  await expect(markdown).toContainText("<script>window.__stepline_pwned = 'script'</script>");
  await expect(markdown).toContainText("<svg onload=");
  await expect(markdown).toContainText("[image: tracking pixel]");
  await expect(markdown.locator("a")).toHaveCount(0); // the javascript: link is rendered as text

  expect(await page.evaluate(() => (window as unknown as { __stepline_pwned?: string }).__stepline_pwned)).toBeUndefined();
  expect(guard.requests.filter((u) => u.includes("tracker.invalid"))).toEqual([]);
});

test("strict CSP is served and Trusted Types block HTML injection", async ({ page, guard }) => {
  const response = await page.goto("/");
  const csp = response?.headers()["content-security-policy"] ?? "";
  expect(csp).toContain("script-src 'self'");
  expect(csp).toContain("require-trusted-types-for 'script'");
  expect(csp).not.toContain("unsafe-inline");
  // Even if a bug tried to inject recorded HTML via innerHTML, the browser would refuse.
  const blocked = await page.evaluate(() => {
    try {
      document.createElement("div").innerHTML = "<img src=x onerror=alert(1)>";
      return false;
    } catch {
      return true;
    }
  });
  expect(blocked).toBe(true);
  // That deliberate attempt is the only CSP report we expect; clear exactly it for the guard.
  await page.waitForTimeout(100);
  const expected = (p: string) => p.includes("TrustedHTML");
  expect(guard.problems.filter(expected)).toHaveLength(1);
  guard.problems.splice(0, guard.problems.length, ...guard.problems.filter((p) => !expected(p)));
  await page.evaluate(() => ((window as unknown as { __violations: string[] }).__violations = []));
});
