import { expect, test, type Locator, type Page } from "@playwright/test";

const H = 844;
const PROJECT = { id: "p_1", revision: 3, title: "Лес", frame_format: "16:9", idea_text: "девушка у озера", style_block: "", created_at: "2026-10-21T10:00:00.000+00:00" };
const scene = (id: string, idx: number, over: object = {}) => ({
  id, idx, revision: 1, state_version: 1, edit_version: 1, mode: "from_frame", action_text: "She walks to the lake.",
  performance_text: "Her gaze drops, then she smiles.", sound_text: "Bare heels on wet planks.", music_text: "none",
  state_in_text: "", state_out_text: "", requested_s: 8, frames: 192, duration_s: 8, seed: 11,
  source_frame: { kind: "none", scene_id: null, asset_id: null }, refs: [], review_state: "none",
  approved_sketch_take_id: null, approved_final_take_id: null, ...over,
});

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
  const barTop = (await page.locator("[data-testid=mobile-action-bar], form[aria-label='Новый проект'] > div.fixed").first().boundingBox())!.y;
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

test("steps 2–3: the bar carries — and performs — the main action of the step in hand", async ({ page }) => {
  // a server with state for two scenes; every snapshot keeps last_seq 5, so the stream is
  // always opened at exactly ?after=5
  const scenes: Record<string, ReturnType<typeof scene>> = { s_1: scene("s_1", 0), s_2: scene("s_2", 1) };
  let conflictOnce = false;
  const patch = (id: string) => (body: unknown): Reply => {
    const { expected_edit_version, ...fields } = body as { expected_edit_version: number };
    if (conflictOnce && id === "s_1") {
      conflictOnce = false;
      scenes.s_1 = { ...scenes.s_1, music_text: "other", edit_version: scenes.s_1.edit_version + 1 };
      return [409, { error: { code: "revision_conflict", message: "x", detail: { current: scenes.s_1 } } }];
    }
    if (expected_edit_version !== scenes[id].edit_version) return [409, { error: { code: "revision_conflict", message: "x", detail: { current: scenes[id] } } }];
    scenes[id] = { ...scenes[id], ...fields, edit_version: scenes[id].edit_version + 1 };
    return [200, { scene: scenes[id], cascade: [], lint: [] }];
  };
  const snapshot = (): Reply => [200, { project: PROJECT, scenes: [scenes.s_1, scenes.s_2], cast: [], takes: [], tasks: [], last_seq: 5 }];
  const log = await api(page, {
    "GET /api/studio/projects/p_1": snapshot,
    "GET /api/studio/events?after=5": [204, null],
    "POST /api/studio/projects/p_1/lint": [200, { findings: {} }],
    "POST /api/studio/projects/p_1/cast": [201, {}],
    "PATCH /api/studio/scenes/s_1": patch("s_1"),
    "PATCH /api/studio/scenes/s_2": patch("s_2"),
    "POST /api/studio/projects/p_1/director/propose": [200, {
      proposal_id: "r_1", dropped_tags: [], durations: [{ frames: 192, duration_s: 8 }],
      scenes: [{ ...scene("x", 0), scene_id: null, seed: null, source_frame: { kind: "none" } }] }],
    "POST /api/studio/projects/p_1/director/apply": [200, { project: PROJECT, scenes: [], diff: {}, lint: {} }],
  });
  const sent = (key: string) => log.filter((r) => r.key === key).map((r) => r.body);
  const bar = page.getByTestId("mobile-action-bar");
  const music = (n: number) => page.getByRole("article", { name: `Сцена ${n}` }).getByLabel("Музыка");
  await page.goto("/studio/#/p/p_1");
  await expectPinned(page, bar.getByRole("button", { name: "Предложить сцены" }));

  // step 2 from the bar
  await page.getByLabel("Тег").fill("anna");
  await page.getByLabel("Кто это").fill("женщина");
  await expectPinned(page, bar.getByRole("button", { name: "Сохранить героя" }));
  await bar.getByRole("button", { name: "Сохранить героя" }).click();
  await expect(bar.getByRole("button", { name: "Предложить сцены" })).toBeVisible();
  expect(sent("POST /api/studio/projects/p_1/cast")).toEqual([
    { expected_revision: 3, tag: "anna", description_ru: "женщина", assets: [] }]);

  // two edited cards: the bar saves the one edited last, then the other
  await music(1).fill("cello");
  await music(2).fill("flute");
  await expectPinned(page, bar.getByRole("button", { name: "Сохранить сцену 2" }));
  await bar.getByRole("button", { name: "Сохранить сцену 2" }).click();
  await expect(bar.getByRole("button", { name: "Сохранить сцену 1" })).toBeVisible();
  await bar.getByRole("button", { name: "Сохранить сцену 1" }).click();
  await expect(bar.getByRole("button", { name: "Предложить сцены" })).toBeVisible();
  expect([sent("PATCH /api/studio/scenes/s_2"), sent("PATCH /api/studio/scenes/s_1")]).toEqual([
    [{ expected_edit_version: 1, music_text: "flute" }], [{ expected_edit_version: 1, music_text: "cello" }]]);

  // a conflict: the bar offers both choices and performs «Сохранить моё»
  conflictOnce = true;
  await music(1).fill("mine");
  await bar.getByRole("button", { name: "Сохранить сцену 1" }).click();
  await expectPinned(page, bar.getByRole("button", { name: "Сохранить моё" }));
  await expectPinned(page, bar.getByRole("button", { name: "Взять с сервера" }));
  await bar.getByRole("button", { name: "Сохранить моё" }).click();
  await expect(bar.getByRole("button", { name: "Предложить сцены" })).toBeVisible();
  expect(sent("PATCH /api/studio/scenes/s_1").slice(1)).toEqual([
    { expected_edit_version: 2, music_text: "mine" }, { expected_edit_version: 3, music_text: "mine" }]);
  expect(scenes.s_1.music_text).toBe("mine");

  // «Взять с сервера» from the bar
  conflictOnce = true;
  await music(1).fill("mine again");
  await bar.getByRole("button", { name: "Сохранить сцену 1" }).click();
  await bar.getByRole("button", { name: "Взять с сервера" }).click();
  await expect(music(1)).toHaveValue("other");

  // step 3 from the bar
  await bar.getByRole("button", { name: "Предложить сцены" }).click();
  await expectPinned(page, bar.getByRole("button", { name: "Применить" }));
  await bar.getByRole("button", { name: "Применить" }).click();
  await expect(page.getByRole("region", { name: "Предложение сценариста" })).toHaveCount(0);   // applied: the proposal is gone
  expect(sent("POST /api/studio/projects/p_1/director/apply")).toEqual([{
    proposal_id: "r_1", expected_revision: 3,
    deleted: [{ scene_id: "s_1", expected_edit_version: 5 }, { scene_id: "s_2", expected_edit_version: 2 }],
  }]);
});

const ITEM = (n: number) => ({
  id: `p_${n}`, revision: 1, title: `Проект ${n}`, frame_format: "16:9", idea_text: "", style_block: "",
  created_at: "2026-10-21T10:00:00.000+00:00", scenes_total: n, scenes_done: 0,
});

async function openFormWithThreeCards(page: Page) {
  await api(page, { "GET /api/studio/projects": [200, [ITEM(1), ITEM(2), ITEM(3)]] });
  await page.goto("/studio/");
  await page.getByRole("link", { name: /Проект 3/ }).waitFor();
  await page.getByRole("button", { name: "Новый проект" }).click();
  await page.mouse.move(0, 0);
}

test("step 1 with three cards: the last card ends above the pinned bar, and the bar stays on top of the cards", async ({ page }) => {
  await openFormWithThreeCards(page);
  const button = page.getByRole("button", { name: "Создать проект" });
  const bar = page.locator("form[aria-label='Новый проект'] > div.fixed").first();
  const barTop = (await bar.boundingBox())!.y;
  const height = await page.evaluate(() => document.documentElement.scrollHeight);
  // walk the page down in 150 px steps: wherever a card is behind the bar, the bar is
  // what a finger would hit (z-index), not the card. (The button itself is disabled while the
  // title is empty, so the hit is checked against the whole bar.)
  let covered = 0;
  for (let y = 0; y <= height; y += 150) {
    await page.evaluate((to) => window.scrollTo(0, to), y);
    const hit = await button.evaluate((el) => {
      const r = el.getBoundingClientRect();
      const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      const card = [...document.querySelectorAll("a[href^='#/p/']")].some((a) => {
        const c = a.getBoundingClientRect();
        return c.bottom > r.top && c.top < r.bottom;
      });
      return { onBar: !!top && el.closest("div.fixed")!.contains(top), card, top: top?.outerHTML.slice(0, 120), y: scrollY };
    });
    expect(hit.onBar, JSON.stringify(hit)).toBe(true);
    if (hit.card) covered++;
  }
  expect(covered).toBeGreaterThan(0);   // the walk really passed cards under the bar
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  const last = (await page.getByRole("link", { name: /Проект 3/ }).boundingBox())!;
  expect(Math.round(last.y + last.height)).toBeLessThanOrEqual(Math.round(barTop));
});

test("the primary button is a cobalt-gradient pill the width of the phone minus the 12 px gutters", async ({ page }) => {
  await openFormWithThreeCards(page);
  const button = page.getByRole("button", { name: "Создать проект" });
  const style = await button.evaluate((el) => {
    const s = getComputedStyle(el);
    return { radius: s.borderTopLeftRadius, image: s.backgroundImage, height: el.getBoundingClientRect().height };
  });
  expect(parseFloat(style.radius)).toBeGreaterThanOrEqual(style.height / 2);   // a pill, not rounded-md (6 px)
  expect(style.image).toContain("linear-gradient(");
  // the light cobalt #1747e6 (23, 71, 230) is the first stop; the orange #d97757 would not match
  expect(style.image).toMatch(/rgb\(23, 71, 230\)|color\(srgb 0\.0*9\d* 0\.27\d* 0\.90\d*\)/);
  expect((await button.boundingBox())!.width).toBe(390 - 2 * 12);
});

test("the dark theme tells the browser it is dark (native controls, scrollbars)", async ({ page }) => {
  await api(page, { "GET /api/studio/projects": [200, []] });
  await page.goto("/studio/");
  const scheme = () => page.evaluate(() => getComputedStyle(document.documentElement).colorScheme);
  expect(await scheme()).toBe("light");
  await page.getByRole("button", { name: "Тема: Светлая" }).click();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("dark");
  expect(await scheme()).toBe("dark");
});
