import { expect, openRun, REPLAY_BASE_URL, test } from "./fixtures";

// Replays hit e2e/fake_anthropic.py through the real SDK (ANTHROPIC_BASE_URL): no real calls.
test.describe("replay", () => {
  test.use({ baseURL: REPLAY_BASE_URL });

  test("edit the prompt, replay, land on the fork, see the diff", async ({ page, guard }) => {
    await openRun(page, "repo-triage-agent");
    const sourceUrl = page.url();
    await page.getByTestId("step-row").nth(9).click();
    const detail = page.getByTestId("step-detail");
    await expect(detail).toContainText("Step #9");

    // Open the editor.
    await page.getByTestId("replay-button").click();
    const editor = page.getByTestId("replay-editor");
    await expect(editor.getByTestId("replay-limits")).toContainText("tool code is not re-run");
    await expect(editor.getByTestId("replay-limits")).toContainText("the fork ends at this step");
    await expect(editor.getByTestId("replay-credentials")).toContainText("ANTHROPIC_API_KEY");
    await expect(editor.getByTestId("replay-model")).toHaveValue("claude-opus-5-5");

    // Edit the system prompt.
    await editor.getByTestId("replay-system").fill("You are a pirate. Answer in one sentence.");
    await expect(editor).toContainText("edited: system");

    // Review: model, tokens and cost are shown before anything is sent.
    await editor.getByTestId("replay-review").click();
    const confirm = editor.getByTestId("replay-confirm");
    await expect(confirm).toContainText("claude-opus-5-5");
    await expect(confirm.getByTestId("replay-input-tokens")).toContainText("counted by the API");
    await expect(confirm.getByTestId("replay-cost")).toContainText("$");
    expect(guard.requests.filter((u) => u.endsWith("/replay"))).toEqual([]); // not sent yet

    // Send, and land on the new fork at the replayed step.
    await confirm.getByTestId("replay-send").click();
    await expect(page.getByTestId("toast")).toContainText("Replay saved as a new fork");
    await expect(page).toHaveURL(/\/runs\/run_[0-9a-f]+\?step=8$/);
    expect(page.url()).not.toEqual(sourceUrl);
    await expect(page.getByTestId("run-title")).toHaveText("repo-triage-agent (replay #9)");
    await expect(detail.getByTestId("replay-banner")).toContainText("edited: system");
    await expect(detail).toContainText("Replayed reply (system: You are a pirate.");
    await expect(page.getByTestId("fork-end")).toContainText("tool code isn't re-run");
    expect(guard.requests.filter((u) => u.endsWith("/replay"))).toHaveLength(1);

    // Diff against the parent.
    await detail.getByTestId("replay-banner").getByRole("link", { name: /Compare with parent/ }).click();
    const diff = page.getByTestId("diff-view");
    await expect(diff.getByRole("heading", { level: 1 })).toContainText("repo-triage-agent");
    await expect(diff.getByRole("heading", { level: 1 })).toContainText("repo-triage-agent (replay #9)");
    const changed = page.locator('[data-testid=diff-pair][data-status="changed"]');
    await expect(changed).toHaveCount(1);
    await expect(changed).toContainText("#9");
    const pairDetail = page.getByTestId("pair-detail");
    await expect(pairDetail).toContainText("System prompt");
    await expect(pairDetail.locator("ins").first()).toContainText("pirate");
    await expect(pairDetail).not.toContainText('"stream"'); // transport detail, not an edit
  });

  test("invalid edits are flagged inline and nothing is sent", async ({ page, guard }) => {
    guard.allow(/status of 422/);
    await openRun(page, "repo-triage-agent");
    await page.getByTestId("step-row").nth(1).click();
    await page.getByTestId("replay-button").click();
    const editor = page.getByTestId("replay-editor");
    const review = editor.getByTestId("replay-review");

    // Client-side: broken JSON
    const messages = editor.getByTestId("replay-messages");
    const original = await messages.inputValue();
    await messages.fill(original.slice(0, -5));
    await expect(editor.getByTestId("field-error")).toContainText("Invalid JSON");
    await expect(review).toBeDisabled();

    // Client-side: bad max_tokens
    await messages.fill(original);
    await editor.getByTestId("replay-params").fill('{"max_tokens": 0}');
    await expect(editor.getByTestId("field-error")).toContainText("max_tokens");
    await editor.getByTestId("replay-params").fill('{"max_tokens": 512}');

    // Server-side: structural validation, shown on the right field
    await messages.fill(original.replace('"role": "user"', '"role": "robot"'));
    await review.click();
    await expect(editor.getByTestId("field-error")).toContainText("messages[0].role");
    await expect(editor.getByTestId("replay-confirm")).toHaveCount(0);
    expect(guard.requests.filter((u) => u.endsWith("/replay"))).toEqual([]);
  });

  test("an API error during replay is saved as a failed step, in the API's words", async ({ page, guard: _guard }) => {
    await openRun(page, "summarize-ticket");
    await page.getByTestId("step-row").nth(0).click();
    await page.getByTestId("replay-button").click();
    const editor = page.getByTestId("replay-editor");
    await editor.getByTestId("replay-model").fill("claude-retired-1");
    await editor.getByTestId("replay-review").click();
    await expect(editor.getByTestId("replay-cost")).toContainText("unknown model price");
    await editor.getByTestId("replay-send").click();

    await expect(page.getByTestId("toast")).toContainText("saved as a failed step");
    await expect(page.getByTestId("toast")).toContainText("not_found_error: model: claude-retired-1");
    await expect(page.getByTestId("run-title")).toHaveText("summarize-ticket (replay #0)");
    const detail = page.getByTestId("step-detail");
    await expect(detail.getByTestId("error-card")).toContainText("NotFoundError");
    await expect(detail.getByTestId("error-card")).toContainText("model: claude-retired-1");
    await expect(page.getByTestId("step-row").first()).toContainText("NotFoundError 404");
  });
});

test("without an API key, replay explains how to set one", async ({ page, guard: _guard }) => {
  // The default server runs with no credentials in its environment.
  await openRun(page, "summarize-ticket");
  await page.getByTestId("step-row").nth(0).click();
  await page.getByTestId("replay-button").click();
  const editor = page.getByTestId("replay-editor");
  const notice = editor.getByTestId("replay-credentials-missing");
  await expect(notice).toContainText("No Anthropic API key found");
  await expect(notice).toContainText("export ANTHROPIC_API_KEY=sk-ant-...");
  await editor.getByTestId("replay-review").click();
  const confirm = editor.getByTestId("replay-confirm");
  await expect(confirm.getByTestId("replay-input-tokens")).toContainText("No API key: estimated locally.");
  await expect(confirm.getByTestId("replay-send")).toBeDisabled();
  // Escape closes the editor without closing the step.
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await expect(editor).toHaveCount(0);
  await expect(page.getByTestId("step-detail")).toBeVisible();
});
