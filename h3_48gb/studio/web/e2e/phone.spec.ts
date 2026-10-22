import { expect, test, type Locator, type Page } from "@playwright/test";

const H = 844;
type Reply = [number, unknown];
type Route = Reply | ((body: unknown) => Reply);

/** Exact "METHOD path?query" routes (the events stream included); anything else fails the
 *  test. Every request body is logged in order. */
async function api(page: Page, routes: Record<string, Route>) {
  const log: { key: string; body: unknown }[] = [];
  await page.route("**/api/studio/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${url.pathname}${url.search}`;
    const raw = request.postData();
    const body = raw ? JSON.parse(raw) : null;
    log.push({ key, body });
    const hit = routes[key];
    if (!hit) throw new Error(`unexpected request: ${key}`);
    const [status, payload] = typeof hit === "function" ? hit(body) : hit;
    if (status === 204) return route.fulfill({ status });
    await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });
  });
  return log;
}

/** Pinned = its bottom edge sits 12 px (the bar's padding) above the bottom of the window, and it
 *  stays there when the page is scrolled to the very end; nothing scrolls under it unreachable. */
async function expectPinned(page: Page, button: Locator) {
  await page.mouse.move(0, 0);              // a hovered primary button lifts by 1 px (AB hover)
  await expect(button).toBeVisible();
  const atTop = await button.boundingBox();
  expect(Math.round(atTop!.y + atTop!.height)).toBe(H - 12);
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const atEnd = await button.boundingBox();
  expect(Math.round(atEnd!.y + atEnd!.height)).toBe(H - 12);
  const last = await page.locator("main input, main textarea").last().boundingBox();
  const barTop = (await page.locator("form[aria-label='Новый проект'] > div.fixed").first().boundingBox())!.y;
  expect(last!.y + last!.height).toBeLessThanOrEqual(barTop);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.evaluate(() => window.scrollTo(0, 0));
}

test("step 1: «Создать проект» is pinned", async ({ page }) => {
  await api(page, { "GET /api/studio/projects": [200, []] });
  await page.goto("/studio/");
  await page.getByRole("button", { name: "Новый проект" }).click();
  await expectPinned(page, page.getByRole("button", { name: "Создать проект" }));
});
