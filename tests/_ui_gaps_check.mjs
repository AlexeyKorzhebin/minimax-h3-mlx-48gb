// Wave 1.5 DOM wiring scenarios; usage: node _ui_gaps_check.mjs <appUrl> <scenario>
import { htmlWrites, intervals, SGLANG, routes, calls, alerts, prompts, answers, getElementById, start, ok, err, PROJECT, sleep, queryAll, queryOne,
  fire, clickable, confirms } from "./_ui_harness.mjs";

const [, , appUrl, scenario] = process.argv;
const fail = (m) => { process.stderr.write(`${m}\n`); process.exit(1); };
const posts = () => calls.filter((c) => c.method === "POST").map((c) => [c.url, c.body]);

const DRAFT_PROJECT = PROJECT({ stages: { script: "awaiting_approval", scenes: "draft",
  upscale: "draft", assembly: "draft" }, scenes: [
  { idx: 0, prompt: "@a walks", duration: 8, status: "pending", job_id: null, clip_path: null,
    keyframe_path: null, start_image: "@arena" },
  { idx: 1, prompt: "@a runs", duration: 5, status: "pending", job_id: null, clip_path: null,
    keyframe_path: null, fresh_start: true }], references: [{ tag: "@a", version: 1 }] });
const FIELDS = "#project-body [data-scene-field]";
const field = (sceneField, idx, value) => ({ dataset: { sceneField, idx: String(idx) }, value });
// After every redraw the browser's fields hold the draft's own values and the editor carries the
// new epoch: the harness models that by pointing `.scene-editor` at the epoch it just drew and
// emptying the fields. Fields a scenario sets afterwards are what the person typed since.
const liveEditor = () => {
  const m = getElementById("project-body").innerHTML.match(/<div class="scene-editor" data-id="p1" data-epoch="(\d+)">/);
  if (m) queryOne["#project-body .scene-editor"] = { dataset: { epoch: m[1] } };
  queryAll[FIELDS] = [];
};
const open = async () => {
  fire("click", clickable({ dataset: { act: "open-project", id: "p1" }, match: (s) => s === "button[data-act]" }));
  await sleep(80);
  liveEditor();
};
// The redraw an action causes happens *inside* `act`, while the stale fields are still in
// `queryAll` -- exactly the moment Н1 is about; `liveEditor` runs only after it.
// The button a click lands on is the one the page really drew: its `data-*` come from the markup in
// `#project-body`, not from the scenario. (A scenario that handed the handler an `id` the markup does
// not carry once hid a button that called `/api/projects/undefined/...`.) Only a button the page
// draws into another place than `#project-body` (the tag-hint buttons are filled in by a mocked slot)
// may fall back to the scenario's own values.
const markupDataset = (tag) => {
  const dataset = {};
  for (const m of tag.matchAll(/ data-([a-z0-9-]+)="([^"]*)"/g)) {
    dataset[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = m[2]
      .replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  }
  return dataset;
};
const SYNTHETIC_OK = new Set(["tag-pick"]);
const realButton = (name, want = {}) => {
  const html = getElementById("project-body").innerHTML;
  for (const m of html.matchAll(/<button\b[^>]*>/g)) {
    const dataset = markupDataset(m[0]);
    if (dataset.act === name && Object.entries(want).every(([k, v]) => dataset[k] === v)) return dataset;
  }
  if (SYNTHETIC_OK.has(name)) return { act: name, ...want };
  throw new Error(`no <button data-act="${name}"> ${JSON.stringify(want)} in the drawn markup`);
};
const clickReal = (name, want = {}) =>
  fire("click", clickable({ dataset: realButton(name, want), match: (s) => s === "button[data-act]" }));
const act = async (name, extra = {}) => {
  clickReal(name, extra);
  await sleep(80);
  liveEditor();
};
const puts = () => calls.filter((c) => c.method === "PUT").map((c) => [c.url, c.body]);
const writes = () => calls.filter((c) => c.method !== "GET").map((c) => [c.method, c.url]);
const draftRoutes = (extra = {}) => ({ "GET /api/projects/p1": ok(DRAFT_PROJECT),
  "PUT /api/projects/p1/scenes": ok({ ok: true, project: DRAFT_PROJECT.project }), ...extra });

const DONE_PROJECT = PROJECT({ scenes: [
  { idx: 0, prompt: "@a walks", duration: 8, status: "done", job_id: null,
    clip_path: "/o/projects/p1/scenes/a.mp4", keyframe_path: null, seed: 305 },
  { idx: 1, prompt: "@a runs", duration: 5, status: "done", job_id: null,
    clip_path: "/o/projects/p1/scenes/b.mp4", keyframe_path: null }],
  references: [{ tag: "@a", version: 1 }], i2v_prefix: "Go.", seed: null });
const CARD_A = { tag: "@a", kind: "person", version: 1, latest_version: 1, description: "d",
                 assets: ["/o/library/a/v1/01-a.png"], versions: [{ version: 1 }] };
// The whole <div class="cls" …>…</div>, nested divs included: a regex up to the first </div> would
// silently cut a block that one day gets an inner wrapper and turn the comparison false.
const block = (cls) => {
  const html = getElementById("project-body").innerHTML;
  const at = html.indexOf(`<div class="${cls}"`);
  if (at < 0) return null;
  let depth = 0;
  for (const m of html.slice(at).matchAll(/<\/?div\b[^>]*>/g)) {
    depth += m[0].startsWith("</") ? -1 : 1;
    if (depth === 0) return html.slice(at, at + m.index + m[0].length);
  }
  return null;
};

const CHAT_SESSION = { ok: true, id: "c1", source: { kind: "project", id: "p1" }, mode: "t2va",
  image: "", end_image: "", duration: 10, messages: [], prompt: "", kind: "video", tags: ["@a"],
  project: { kind: "video", scenes: [{ prompt: "@a jumps", duration: 6 },
    { prompt: "@a lands", duration: 4 }, { prompt: "@a bows", duration: 3 }] } };
const SCENARIOS = {
  async new_video() {
    answers.prompt = "Бой";
    await start(appUrl, { "POST /api/projects": ok({ ok: true, id: "p9", project: {} }),
      "GET /api/projects/p9": ok(PROJECT({ id: "p9", title: "Бой", stages: { script: "draft",
        scenes: "draft", upscale: "draft", assembly: "draft" } })) });
    getElementById("project-new-video").__listeners.click[0]();
    await sleep(80);
    return { prompts, posts: posts(), opened: getElementById("project-modal").hidden === false,
             title: getElementById("project-title").textContent };
  },
  async new_video_cancel() {
    answers.prompt = null;
    await start(appUrl, {});
    getElementById("project-new-video").__listeners.click[0]();
    await sleep(80);
    // cancelling must be silent: no POST, no alert, and showError (which fills #err) never ran
    return { posts: posts(), alerts, errHtml: getElementById("err").innerHTML };
  },
  async new_chat() {
    await start(appUrl, { "POST /api/chat": ok({ ok: true, id: "c1" }) });
    getElementById("project-new-chat").__listeners.click[0]();
    await sleep(80);
    return { posts: posts() };
  },
  async editor_dom_edits_survive_add() {
    await start(appUrl, draftRoutes());
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps"), field("seed", 1, "305")];
    await act("scene-add");
    queryAll[FIELDS] = [field("prompt", 2, "@a rests")];
    await act("scenes-save");
    return { puts: puts() };
  },
  async editor_edits_survive_ref_pin() {
    await start(appUrl, draftRoutes({ "PUT /api/projects/p1/references": ok({ ok: true, references: [] }) }));
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
    // the real handler (app.js:5334, the document `change` listener): target.closest(".project-refs") -> box.querySelectorAll(".ref-pin")
    const pin = { checked: true, dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-pin" } };
    const box = { dataset: { id: "p1" }, querySelectorAll: (sel) => (sel === ".ref-pin" ? [pin] : []) };
    pin.closest = (sel) => (sel === ".project-refs" ? box : null);
    fire("change", pin);
    await sleep(120);
    liveEditor();                   // the modal was redrawn: the fields now hold whatever the draft had
    await act("scenes-save");
    return { puts: puts().filter(([url]) => url === "/api/projects/p1/scenes") };
  },
  async editor_approve_saves_first() {
    await start(appUrl, draftRoutes({ "POST /api/projects/p1/approve/script": ok({ ok: true }) }));
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
    await act("approve-script");
    return { writes: writes() };
  },
  async editor_approve_stops_on_save_error() {
    await start(appUrl, draftRoutes({ "PUT /api/projects/p1/scenes": err(400, "args_invalid", "плохая сцена"),
      "POST /api/projects/p1/approve/script": ok({ ok: true }) }));
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
    await act("approve-script");
    return { writes: writes(), errorHidden: getElementById("project-err").hidden };
  },
  async editor_client_error() {
    await start(appUrl, draftRoutes());
    await open();
    await act("scene-add");
    await act("scenes-save");
    return { puts: puts(), error: getElementById("project-err").innerHTML };
  },
  async editor_grid_hint_live() {
    await start(appUrl, draftRoutes());
    await open();
    const hint = { textContent: "на сетке: 8 с" };
    queryOne['#project-body .grid-hint[data-idx="0"]'] = hint;
    const input = { value: "4", dataset: { sceneField: "duration", idx: "0" },
      closest(sel) { return sel === "[data-scene-field]" ? this : null; } };
    fire("input", input);
    return { hint: hint.textContent };
  },
  async editor_grid_hint_follows_fresh_start() {
    await start(appUrl, draftRoutes());
    await open();
    const hint = { textContent: "на сетке: 5,17 с" };
    queryOne['#project-body .grid-hint[data-idx="1"]'] = hint;
    // the person un-ticks «начать с чистого листа» on scene 1: it becomes chained, one frame shorter
    const box = { checked: false, dataset: { sceneField: "fresh_start", idx: "1" },
      classList: { contains: () => false },
      closest(sel) { return sel === '[data-scene-field="fresh_start"]' ? this : null; } };
    queryAll[FIELDS] = [box];
    fire("change", box);
    return { hint: hint.textContent };
  },
  async editor_save_resets_from_answer() {
    const server = PROJECT({ ...DRAFT_PROJECT.project, scenes: DRAFT_PROJECT.project.scenes.map(
      (s) => (s.idx === 0 ? { ...s, prompt: "@a server text" } : s)) });
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(DRAFT_PROJECT),
      "PUT /api/projects/p1/scenes": ok({ ok: true, project: server.project }) }));
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
    await act("scenes-save");           // the stale fields stay in queryAll through the redraw
    await act("scenes-save");
    return { puts: puts().map(([url, body]) => [url, body.scenes.map((s) => s.prompt)]) };
  },
  async editor_save_resets_when_reread_fails() {
    const server = PROJECT({ ...DRAFT_PROJECT.project, scenes: DRAFT_PROJECT.project.scenes.map(
      (s) => (s.idx === 0 ? { ...s, prompt: "@a server text" } : s)) });
    await start(appUrl, draftRoutes({ "PUT /api/projects/p1/scenes": ok({ ok: true, project: server.project }) }));
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
    routes["GET /api/projects/p1"] = err(500, "boom", "down");   // the re-read after the save fails
    await act("scenes-save");
    // an unrelated redraw (the person pins a reference) must not bring the typed text back
    routes["GET /api/projects/p1"] = ok(DRAFT_PROJECT);
    routes["PUT /api/projects/p1/references"] = ok({ ok: true, references: [] });
    const pin = { checked: true, dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-pin" } };
    const box = { dataset: { id: "p1" }, querySelectorAll: (sel) => (sel === ".ref-pin" ? [pin] : []) };
    pin.closest = (sel) => (sel === ".project-refs" ? box : null);
    fire("change", pin);
    await sleep(120);
    liveEditor();
    await act("scenes-save");
    return { puts: puts().filter(([url]) => url === "/api/projects/p1/scenes")
      .map(([url, body]) => [url, body.scenes.map((s) => s.prompt)]) };
  },
  async editor_client_error_seed() {
    await start(appUrl, draftRoutes());
    await open();
    queryAll[FIELDS] = [field("seed", 1, "-1")];
    await act("scenes-save");
    const body = getElementById("project-body").innerHTML;
    return { puts: puts(), inline: body.match(/<span class="hint bad scene-edit-error"[^>]*>[^<]*<\/span>/)[0] };
  },
  async editor_dirty_note_on_input() {
    await start(appUrl, draftRoutes());
    await open();
    const note = { hidden: true };
    queryOne["#project-body .dirty-note"] = note;
    const hiddenBefore = note.hidden;
    const input = { value: "@a jumps", selectionStart: 8, title: "", classList: { toggle() {}, contains: () => false },
      dataset: { sceneField: "prompt", idx: "0" },
      closest(sel) { return sel === "[data-scene-field]" ? this : null; } };
    queryAll[FIELDS] = [input];
    fire("input", input);
    return { hiddenBefore, hiddenAfter: note.hidden };
  },
  async editor_close_unsaved() {
    await start(appUrl, draftRoutes());
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
    answers.confirm = false;
    getElementById("project-close").__listeners.click[0]();
    return { confirms, hidden: getElementById("project-modal").hidden };
  },
  async editor_move_keeps_scenes() {
    await start(appUrl, draftRoutes());
    await open();
    const typed = [field("prompt", 0, "@a walks far"), field("prompt", 1, "@a runs fast")];
    queryAll[FIELDS] = typed;
    // no `act` here on purpose: the stale fields stay in queryAll through the redraw and are still
    // there at the save -- a DOM drawn from an older draft must never win over the moved draft
    clickReal("scene-down", { idx: "0" });
    await sleep(80);
    await act("scenes-save");
    return { puts: puts() };
  },
  async delete_project_no_draft_question() {
    await start(appUrl, draftRoutes({ "DELETE /api/projects/p1": ok({ ok: true }) }));
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
    answers.confirm = true;
    getElementById("project-delete").__listeners.click[0]();
    await sleep(80);
    return { confirms };
  },
  async tag_hint_on_input() {
    const proj = PROJECT({ ...DRAFT_PROJECT.project, references: [{ tag: "@amazon", version: 1 }, { tag: "@arena", version: 1 }] });
    const card = (tag) => ({ tag, kind: "person", version: 1, latest_version: 1, description: "d",
                            assets: [`/o/library/${tag.slice(1)}/v1/01-x.png`], versions: [{ version: 1 }] });
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(proj),
      "GET /api/library": ok({ ok: true, cards: [card("@amazon"), card("@arena"), card("@bob")] }) }));
    await open();
    const slot = { innerHTML: "" };
    queryOne['#project-body .tag-hint-slot[data-idx="0"]'] = slot;
    fire("input", { value: "fight @a", selectionStart: 8, title: "",
      dataset: { sceneField: "prompt", idx: "0" }, classList: { toggle() {}, contains: () => false },
      closest(sel) { return sel === "[data-scene-field]" ? this : null; } });
    return { slot: slot.innerHTML };
  },
  async tag_pick() {
    await start(appUrl, draftRoutes());
    await open();
    const prompt = { value: "fight @a", selectionStart: 8, dataset: { sceneField: "prompt", idx: "0" },
                     focus() {}, setSelectionRange() {} };
    queryOne['#project-body [data-scene-field="prompt"][data-idx="0"]'] = prompt;
    await act("tag-pick", { tag: "@amazon", idx: "0" });
    queryAll[FIELDS] = [prompt];
    await act("scenes-save");
    return { value: prompt.value, prompt0: puts()[0][1].scenes[0].prompt };
  },
  async scene0_upload() {
    await start(appUrl, draftRoutes({ "POST /api/uploads": ok({ ok: true, path: "/o/uploads/open.png" }) }));
    await open();
    const file = getElementById("scene0-file");
    file.files = [{ name: "open.png" }];
    file.__listeners.change[0]();
    await sleep(120);
    await act("scenes-save");
    const upload = calls.find((c) => c.url === "/api/uploads");
    return { upload: { url: upload.url, headers: upload.headers, body: upload.body },
             start_image: puts().at(-1)[1].scenes[0].start_image };
  },
  async json_load() {
    await start(appUrl, draftRoutes());
    await open();
    queryOne["#project-body .scenario-json-text"] = { value: '{"scenes": [{"prompt": "@a", "duration": 5}]}' };
    answers.confirm = true;
    await act("scenario-json-load");
    return { confirms, puts: puts() };
  },
  async json_load_not_clobbered() {
    const loaded = PROJECT({ ...DRAFT_PROJECT.project, scenes: [{ idx: 0, prompt: "@a", duration: 5,
      status: "pending", job_id: null, clip_path: null, keyframe_path: null }] });
    await start(appUrl, draftRoutes({ "PUT /api/projects/p1/scenes": ok({ ok: true, project: loaded.project }) }));
    await open();
    // fields of the two old scenes, still in the DOM when the JSON answer redraws the modal
    queryAll[FIELDS] = [field("prompt", 0, "@a walks"), field("prompt", 1, "@a runs")];
    queryOne["#project-body .scenario-json-text"] = { value: '{"scenes": [{"prompt": "@a", "duration": 5}]}' };
    answers.confirm = true;
    clickReal("scenario-json-load");
    await sleep(120);
    await act("scenes-save");
    return { puts: puts() };
  },
  async json_load_bad() {
    await start(appUrl, draftRoutes());
    await open();
    queryOne["#project-body .scenario-json-text"] = { value: "42" };
    await act("scenario-json-load");
    return { puts: puts(), error: getElementById("project-err").innerHTML };
  },
  async start_image_select_redraws() {
    const proj = PROJECT({ ...DRAFT_PROJECT.project, references: [{ tag: "@arena", version: 1 }] });
    const card = { tag: "@arena", kind: "environment", version: 1, latest_version: 1, description: "d",
      assets: ["/o/library/arena/v1/01-o.png"], versions: [{ version: 1, kind: "environment",
      assets: ["/o/library/arena/v1/01-o.png"] }] };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(proj),
      "GET /api/library": ok({ ok: true, cards: [card] }) }));
    await open();
    // the person picks no frame; the redraw must show the pick (and its thumbnail) at once
    const select = { value: "", dataset: { sceneField: "start_image", idx: "0" },
      classList: { contains: () => false },
      closest(sel) { return sel === '[data-scene-field="start_image"]' ? this : null; } };
    queryAll[FIELDS] = [select];
    fire("change", select);
    await sleep(40);
    const html = getElementById("project-body").innerHTML;
    return { selected: html.match(/<option value="[^"]*" selected>[^<]*<\/option>/g).slice(0, 1),
             thumb: html.includes("start-thumb") };
  },
  async json_text_survives_redraw() {
    await start(appUrl, draftRoutes());
    await open();
    const text = '{"scenes": [{"prompt": "@a", "duration": 5}]}';
    const box = { value: text, closest(sel) { return sel === ".scenario-json-text" ? this : null; } };
    fire("input", box);
    await act("scene-add");               // an unrelated redraw of the whole modal
    const html = getElementById("project-body").innerHTML;
    const m = html.match(/<details class="adv scenario-json"( open)?>.*?<textarea[^>]*>([^<]*)<\/textarea>/);
    return { text: m[2].replace(/&quot;/g, '"'), open: m[1] === " open" };
  },
  async stray_ref_kept_and_refused() {
    const proj = PROJECT({ ...DRAFT_PROJECT.project, references: [{ tag: "@arena", version: 1 }],
      scenes: DRAFT_PROJECT.project.scenes.map((s) => (s.idx === 1 ? { ...s, refs: ["@gone"] } : s)) });
    const card = { tag: "@arena", kind: "environment", version: 1, latest_version: 1, description: "d",
      assets: ["/o/library/arena/v1/01-o.png"], versions: [{ version: 1, kind: "environment",
      assets: ["/o/library/arena/v1/01-o.png"] }] };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(proj),
      "GET /api/library": ok({ ok: true, cards: [card] }) }));
    await open();
    await act("scenes-save");
    const html = getElementById("project-body").innerHTML;
    return { puts: puts(), kept: html.match(/<label><input type="checkbox"[^>]*data-tag="@gone"[^>]*>.*?<\/label>/)[0],
             inline: html.match(/<span class="hint bad scene-edit-error"[^>]*>[^<]*<\/span>/)[0] };
  },
  async refs_order_kept() {
    const refsOf = (tags) => tags.map((tag) => ({ tag, version: 1 }));
    const proj = PROJECT({ ...DRAFT_PROJECT.project, references: refsOf(["@a", "@b", "@c"]),
      scenes: DRAFT_PROJECT.project.scenes.map((s) => (s.idx === 0 ? { ...s, refs: ["@b", "@a"] } : s)) });
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(proj) }));
    await open();
    // the DOM lists the project's tags in its own order; @c was just ticked
    const box = (tag) => ({ checked: true, dataset: { sceneField: "refs", idx: "0", tag } });
    queryAll[FIELDS] = [box("@a"), box("@b"), box("@c")];
    await act("scenes-save");
    return { refs: puts()[0][1].scenes[0].refs };
  },
  async json_load_asks_for_unsaved_draft() {
    await start(appUrl, draftRoutes());
    await open();
    await act("scene-add");                       // the draft now has 3 scenes, the saved project 2
    answers.confirm = false;
    queryOne["#project-body .scenario-json-text"] = { value: '{"scenes": [{"prompt": "@a", "duration": 5}]}' };
    await act("scenario-json-load");
    return { confirms, puts: puts() };
  },
  async json_load_asks_for_unsaved_in_empty_project() {
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(PROJECT({ ...DRAFT_PROJECT.project, scenes: [] })) }));
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a typed")];
    answers.confirm = false;
    queryOne["#project-body .scenario-json-text"] = { value: '{"scenes": [{"prompt": "@a", "duration": 5}]}' };
    await act("scenario-json-load");
    return { confirms, puts: puts() };
  },
  async prompt_tag_issues_highlight() {
    const proj = PROJECT({ ...DRAFT_PROJECT.project, references: [{ tag: "@amazon", version: 1 }] });
    const card = { tag: "@amazon", kind: "person", version: 1, latest_version: 1, description: "d",
      assets: ["/o/library/amazon/v1/01-x.png"], versions: [{ version: 1 }] };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(proj),
      "GET /api/library": ok({ ok: true, cards: [card] }) }));
    await open();
    const typeInto = (value) => {
      const toggled = [];
      const el = { value, selectionStart: value.length, title: "x", dataset: { sceneField: "prompt", idx: "0" },
        classList: { toggle: (cls, on) => toggled.push([cls, on]), contains: () => false },
        closest(sel) { return sel === "[data-scene-field]" ? this : null; } };
      fire("input", el);
      return { toggled, title: el.title };
    };
    const unknown = typeInto("@gone walks");
    const clean = typeInto("@amazon walks");
    return { unknown: unknown.toggled, unknownTitle: unknown.title, clean: clean.toggled, cleanTitle: clean.title };
  },
  async refs_known_when_library_down() {
    const proj = PROJECT({ ...DRAFT_PROJECT.project, references: [{ tag: "@a", version: 1 }],
      scenes: DRAFT_PROJECT.project.scenes.map((s) => (s.idx === 0 ? { ...s, refs: ["@a"] } : s)) });
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(proj),
      "GET /api/library": err(500, "boom", "down") }));
    await open();
    const html = getElementById("project-body").innerHTML;
    return { box: html.match(/<label><input type="checkbox" data-scene-field="refs"[^>]*data-tag="@a"[^>]*>[^<]*(?:<span[^>]*>[^<]*<\/span>)?<\/label>/)[0] };
  },
  async scene0_wrong_extension() {
    await start(appUrl, draftRoutes({ "POST /api/uploads": ok({ ok: true, path: "/o/uploads/x.mp3" }) }));
    await open();
    const file = getElementById("scene0-file");
    file.files = [{ name: "x.mp3" }];
    file.__listeners.change[0]();
    await sleep(80);
    return { uploads: calls.filter((c) => c.url === "/api/uploads").map((c) => c.url),
             error: getElementById("project-err").innerHTML };
  },
  async scene0_upload_after_close() {
    await start(appUrl, draftRoutes({ "POST /api/uploads": () => sleep(60).then(() => ({ status: 500,
      body: { error: { code: "boom", message: "late" } } })) }));
    await open();
    const file = getElementById("scene0-file");
    file.files = [{ name: "open.png" }];
    file.__listeners.change[0]();
    getElementById("project-close").__listeners.click[0]();   // closed while the file is still going up
    await sleep(120);
    return { errHidden: getElementById("project-err").hidden };
  },
  async refs_ride_along() {
    const proj = PROJECT({ ...DRAFT_PROJECT.project, references: [{ tag: "@arena", version: 1 }] });
    const card = { tag: "@arena", kind: "environment", version: 1, latest_version: 1, description: "d",
      assets: ["/o/library/arena/v1/01-o.png"], versions: [{ version: 1, kind: "environment",
      assets: ["/o/library/arena/v1/01-o.png"] }] };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1": ok(proj),
      "GET /api/library": ok({ ok: true, cards: [card] }) }));
    await open();
    const box = (idx, checked) => ({ checked, value: "on", dataset: { sceneField: "refs", idx: String(idx), tag: "@arena" } });
    queryAll[FIELDS] = [box(0, false), box(1, true)];
    await act("scenes-save");
    return { refs: puts()[0][1].scenes.map((s) => s.refs ?? null) };
  },
  async library_preview_after_late_state() {
    routes["GET /api/state"] = () => sleep(40).then(() => ok(SGLANG));
    await start(appUrl, { "GET /api/library": ok({ ok: true, cards: [{ tag: "@alice", kind: "person",
      version: 1, latest_version: 1, description: "a woman", assets: ["/o/library/alice/v1/01-a.png"],
      versions: [{ version: 1 }] }] }) });
    await sleep(120);
    const m = getElementById("library-cards").innerHTML.match(/<img [^>]*>/);
    return { img: m ? m[0] : null };
  },
  async library_delete_in_use() {
    answers.confirm = true;
    const message = "@alice подключена к проектам: «Бой» (p1) — отключите её там или удалите проекты";
    await start(appUrl, { "DELETE /api/library/alice": err(409, "library_card_in_use", message) });
    const cardError = { hidden: true, textContent: "" };
    const card = { querySelector: (sel) => (sel === ".lib-card-error" ? cardError : null) };
    fire("click", { dataset: { act: "lib-delete", tag: "@alice" },
      closest(sel) { return sel === "button[data-act]" ? this : sel === ".lib-card" ? card : null; } });
    await sleep(80);
    return { confirms, cardError,
             deletes: calls.filter((c) => c.method === "DELETE").map((c) => c.url) };
  },
  async library_delete_declined() {
    answers.confirm = false;
    await start(appUrl, { "DELETE /api/library/alice": ok({ ok: true }) });
    const card = { querySelector: () => null };
    fire("click", { dataset: { act: "lib-delete", tag: "@alice" },
      closest(sel) { return sel === "button[data-act]" ? this : sel === ".lib-card" ? card : null; } });
    await sleep(80);
    return { confirms, deletes: calls.filter((c) => c.method === "DELETE").map((c) => c.url) };
  },
  async library_new_version() {
    await start(appUrl, { "POST /api/uploads": ok({ ok: true, path: "/o/uploads/b.png" }),
      "PUT /api/library/alice": ok({ ok: true, card: {} }) });
    const cardError = { hidden: true, textContent: "" };
    const parts = { ".lib-new-files": { files: [{ name: "b.png" }] },
                    ".lib-edit-desc": { value: "a woman" }, ".lib-card-error": cardError };
    const card = { querySelector: (sel) => (Object.hasOwn(parts, sel) ? parts[sel] : null) };
    fire("click", { dataset: { act: "lib-new-version", tag: "@alice" },
      closest(sel) { return sel === "button[data-act]" ? this : sel === ".lib-card" ? card : null; } });
    await sleep(120);
    return { uploads: calls.filter((c) => c.url === "/api/uploads").map((c) => c.headers["X-Filename"]),
             puts: puts(), cardError };
  },
  async refs_version_change() {
    const proj = PROJECT({ references: [{ tag: "@a", version: 2 }] });
    await start(appUrl, { "GET /api/projects/p1": ok(proj),
      "PUT /api/projects/p1/references": ok({ ok: true, references: [] }) });
    await open();
    const pin = { checked: true, dataset: { tag: "@a" } };
    const select = { value: "1", dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-version" } };
    const box = { dataset: { id: "p1" },
      querySelectorAll: (sel) => (sel === ".ref-pin" ? [pin] : sel === ".ref-version" ? [select] : []) };
    select.closest = (sel) => (sel === ".project-refs" ? box : null);
    fire("change", select);
    await sleep(120);
    return { puts: puts() };
  },
  async retry_with_new_seed() {
    await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
      "POST /api/projects/p1/scenes/0/retry": ok({ ok: true, project: DONE_PROJECT.project }) });
    await open();
    await act("retry-scene", { idx: "0" });
    const opened = /<div class="retry-panel" data-idx="0">/.test(getElementById("project-body").innerHTML);
    const fields = { ".retry-prompt": { value: "@a walks" }, ".retry-seed": { value: "7" },
                     ".retry-steps": { value: "" } };
    queryOne['#project-body .retry-panel[data-idx="0"]'] = {
      querySelector: (sel) => (Object.hasOwn(fields, sel) ? fields[sel] : null) };
    await act("retry-scene-go", { idx: "0" });
    return { opened, confirms, posts: posts() };
  },
  async retry_panel_keeps_typing() {
    await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
      "PUT /api/projects/p1/references": ok({ ok: true, references: [] }) });
    await open();
    await act("retry-scene", { idx: "0" });
    // typed into the open panel, then an unrelated redraw (a reference is ticked)
    const fields = { ".retry-prompt": { value: "@a walks far" }, ".retry-seed": { value: "9" },
                     ".retry-steps": { value: "" } };
    queryOne['#project-body .retry-panel[data-idx="0"]'] = {
      querySelector: (sel) => (Object.hasOwn(fields, sel) ? fields[sel] : null) };
    const pin = { checked: true, dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-pin" } };
    const box = { dataset: { id: "p1" }, querySelectorAll: (sel) => (sel === ".ref-pin" ? [pin] : []) };
    pin.closest = (sel) => (sel === ".project-refs" ? box : null);
    fire("change", pin);
    await sleep(120);
    const html = getElementById("project-body").innerHTML;
    const prompt = html.match(/<textarea class="inp retry-prompt"[^>]*>([^<]*)<\/textarea>/)[1];
    const seed = html.match(/<input class="inp num retry-seed"[^>]* value="([^"]*)"/)[1];
    return { prompt, seed };
  },
  async retry_refused_keeps_panel() {
    await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
      "POST /api/projects/p1/scenes/0/retry": err(409, "project_running",
        "Проект считается — референсы меняются после конца прогона.") });
    await open();
    await act("retry-scene", { idx: "0" });
    const fields = { ".retry-prompt": { value: "@a walks far" }, ".retry-seed": { value: "9" },
                     ".retry-steps": { value: "" } };
    queryOne['#project-body .retry-panel[data-idx="0"]'] = {
      querySelector: (sel) => (Object.hasOwn(fields, sel) ? fields[sel] : null) };
    await act("retry-scene-go", { idx: "0" });
    const html = getElementById("project-body").innerHTML;
    const prompt = html.match(/<textarea class="inp retry-prompt"[^>]*>([^<]*)<\/textarea>/);
    const seed = html.match(/<input class="inp num retry-seed"[^>]* value="([^"]*)"/);
    const error = html.match(/<p class="why retry-error">([^<]*)<\/p>/);
    return { open: Boolean(prompt), prompt: prompt && prompt[1], seed: seed && seed[1], error: error && error[1] };
  },
  async retry_clears_dead_clip_marks() {
    await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
      "POST /api/projects/p1/scenes/0/retry": ok({ ok: true, project: DONE_PROJECT.project }) });
    await open();
    // the browser reports scene 0's clip as a 404: the page remembers it and draws a placeholder
    fire("error", { dataset: { mediaUrl: "/media/projects/p1/scenes/a.mp4" }, tagName: "VIDEO" });
    await act("retry-scene", { idx: "0" });
    const deadBefore = getElementById("project-body").innerHTML.includes("клип удалён");
    const fields = { ".retry-prompt": { value: "@a walks" }, ".retry-seed": { value: "7" },
                     ".retry-steps": { value: "" } };
    queryOne['#project-body .retry-panel[data-idx="0"]'] = {
      querySelector: (sel) => (Object.hasOwn(fields, sel) ? fields[sel] : null) };
    await act("retry-scene-go", { idx: "0" });
    const html = getElementById("project-body").innerHTML;
    return { deadBefore, videoAfter: html.includes('<video src="/media/projects/p1/scenes/a.mp4"'),
             deadAfter: html.includes("клип удалён") };
  },
  async saved_mark_fades_on_edit() {
    const app = await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
      "PUT /api/projects/p1/settings": ok({ ok: true, project: DONE_PROJECT.project }),
      "PUT /api/projects/p1/references": ok({ ok: true, references: [] }) });
    await open();
    const prefix = { value: "Go on.", dataset: { id: "p1" }, classList: { contains: (c) => c === "i2v-prefix" } };
    prefix.closest = (sel) => (sel === ".i2v-prefix" ? prefix : null);
    fire("focusout", prefix);
    await sleep(120);
    // typing again: the mark goes out at once ...
    const mark = { removed: false, remove() { this.removed = true; } };
    queryOne["#project-body .saved-mark"] = mark;
    const typing = { value: "Go on, now", dataset: { id: "p1" },
      closest(sel) { return sel === ".i2v-prefix, .project-seed" ? this : null; } };
    fire("input", typing);
    // ... and a later redraw does not bring it back
    const pin = { checked: true, dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-pin" } };
    const box = { dataset: { id: "p1" }, querySelectorAll: (sel) => (sel === ".ref-pin" ? [pin] : []) };
    pin.closest = (sel) => (sel === ".project-refs" ? box : null);
    fire("change", pin);
    await sleep(120);
    return { markRemoved: mark.removed,
             after: block("project-settings") === app.projectSettingsHtml(DONE_PROJECT.project, "sglang", null, null) };
  },
  async locked_while_a_scene_runs() {
    const running = { ...DONE_PROJECT, active_job: { kind: "scene", idx: 1, job: { id: "j1" } } };
    const app = await start(appUrl, { "GET /api/projects/p1": ok(running),
      "GET /api/library": ok({ ok: true, cards: [CARD_A] }) });
    await open();
    const p = running.project;
    return {
      settings: block("project-settings") === app.projectSettingsHtml(p, "sglang", app.PROJECT_LOCK_TEXT.settings, null),
      refs: block("project-refs") === app.projectReferencesHtml(p, [CARD_A], p.references, app.PROJECT_LOCK_TEXT.references),
      route: getElementById("project-body").innerHTML.includes(app.projectRouteHtml(p, "sglang", null)),
    };
  },
  async locked_route_during_upscale() {
    const upscaling = { ...DONE_PROJECT, active_job: { kind: "upscale", job: { id: "j2" } } };
    const app = await start(appUrl, { "GET /api/projects/p1": ok(upscaling) });
    await open();
    return { route: getElementById("project-body").innerHTML.includes(
      app.projectRouteHtml(upscaling.project, "sglang", app.PROJECT_LOCK_TEXT.route)) };
  },
  async settings_saved_mark() {
    const app = await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
      "PUT /api/projects/p1/settings": ok({ ok: true, project: DONE_PROJECT.project }) });
    await open();
    // the real handler (document `focusout`) finds the field with event.target.closest(".i2v-prefix")
    const prefix = { value: "Go on.", dataset: { id: "p1" }, classList: { contains: (c) => c === "i2v-prefix" } };
    prefix.closest = (sel) => (sel === ".i2v-prefix" ? prefix : null);
    fire("focusout", prefix);
    await sleep(120);
    return { puts: puts(),
             mark: block("project-settings") === app.projectSettingsHtml(DONE_PROJECT.project, "sglang", null, "i2v_prefix") };
  },
  async version_chosen_before_the_tick() {
    const card = { tag: "@a", kind: "person", version: 2, latest_version: 2, description: "d",
      assets: ["/o/library/a/v2/01-a.png"], versions: [{ version: 1, assets: ["/o/library/a/v1/01-a.png"] },
                                                       { version: 2 }] };
    const proj = PROJECT({ ...DONE_PROJECT.project, references: [] });
    await start(appUrl, { "GET /api/projects/p1": ok(proj), "GET /api/library": ok({ ok: true, cards: [card] }),
      "PUT /api/projects/p1/references": ok({ ok: true, references: [] }) });
    await open();
    const pin = { checked: false, dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-pin" } };
    const select = { value: "1", dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-version" } };
    const box = { dataset: { id: "p1" },
      querySelectorAll: (sel) => (sel === ".ref-pin" ? [pin] : sel === ".ref-version" ? [select] : []) };
    select.closest = (sel) => (sel === ".project-refs" ? box : null);
    pin.closest = (sel) => (sel === ".project-refs" ? box : null);
    fire("change", select);                      // the version is picked first ...
    await sleep(120);
    const putsAfterPick = puts();
    await act("retry-scene", { idx: "0" });      // ... an unrelated redraw does not reset it ...
    const selectedAfterRedraw = /<option value="1" selected>v1<\/option>/.test(getElementById("project-body").innerHTML);
    pin.checked = true;                          // ... and the tick sends it
    fire("change", pin);
    await sleep(120);
    return { putsAfterPick, selectedAfterRedraw, putsAfterTick: puts() };
  },
  async seed_saved() {
    const app = await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
      "PUT /api/projects/p1/settings": ok({ ok: true, project: DONE_PROJECT.project }) });
    await open();
    queryOne["#project-body .project-seed"] = { value: "305" };
    await act("project-seed-save");
    return { puts: puts(),
             mark: block("project-settings") === app.projectSettingsHtml(DONE_PROJECT.project, "sglang", null, "seed") };
  },
  async seed_cleared() {
    await start(appUrl, { "GET /api/projects/p1": ok(DONE_PROJECT),
      "PUT /api/projects/p1/settings": ok({ ok: true, project: DONE_PROJECT.project }) });
    await open();
    queryOne["#project-body .project-seed"] = { value: "" };
    await act("project-seed-save");
    return { puts: puts() };
  },
  async project_chat_opens() {
    await start(appUrl, draftRoutes({ "POST /api/chat": ok({ ok: true, id: "c1" }),
                                      "GET /api/chat/c1": ok(CHAT_SESSION) }));
    await open();
    await act("project-chat");
    return { posts: posts() };
  },
  async chat_button_in_the_script_stage() {
    await start(appUrl, draftRoutes());
    await open();
    const m = getElementById("project-body").innerHTML.match(/<button type="button" class="ghost" data-act="project-chat"[^>]*>[^<]*<\/button>/);
    return { button: m && m[0] };
  },
  async chat_apply_to_project() {
    globalThis.window.location.hash = "#chat/c1";
    await start(appUrl, draftRoutes({ "GET /api/chat/c1": ok(CHAT_SESSION) }));
    await sleep(120);
    const label = getElementById("chat-make-project").textContent;
    answers.confirm = true;
    getElementById("chat-make-project").__listeners.click[0]();
    await sleep(160);
    return { label, confirms, puts: puts(), posts: posts() };
  },
  async chat_apply_declined() {
    globalThis.window.location.hash = "#chat/c1";
    await start(appUrl, draftRoutes({ "GET /api/chat/c1": ok(CHAT_SESSION) }));
    await sleep(120);
    answers.confirm = false;
    getElementById("chat-make-project").__listeners.click[0]();
    await sleep(160);
    return { puts: puts() };
  },
  async chat_apply_refused() {
    globalThis.window.location.hash = "#chat/c1";
    await start(appUrl, draftRoutes({ "GET /api/chat/c1": ok(CHAT_SESSION),
      "PUT /api/projects/p1/scenes": { status: 400, body: { error: { message: "плохая сцена" } } } }));
    await sleep(120);
    answers.confirm = true;
    getElementById("chat-make-project").__listeners.click[0]();
    await sleep(160);
    return { error: getElementById("chat-project-err").innerHTML,
             panelHidden: getElementById("chat-project-panel").hidden };
  },
  async chat_plain_keeps_its_label() {
    globalThis.window.location.hash = "#chat/c2";
    await start(appUrl, draftRoutes({ "GET /api/chat/c2": ok({ ...CHAT_SESSION, id: "c2",
      source: { kind: "new" } }) }));
    await sleep(120);
    return { label: getElementById("chat-make-project").textContent };
  },
  async h3_prompt_saves_first() {
    const answer = { ok: true, idx: 0, prompt: "P", pictures: [], audios: [],
                     keyframe: { kind: null, path: null }, duration: 8, seed: 42, steps: 50 };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1/scenes/0/h3-prompt": ok(answer) }));
    await open();
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps")];
    await act("scene-h3-prompt", { idx: "0" });
    const watched = ["PUT /api/projects/p1/scenes", "GET /api/projects/p1/scenes/0/h3-prompt"];
    return { order: calls.map((c) => `${c.method} ${c.url}`).filter((k) => watched.includes(k)),
             shown: /<div class="h3-prompt" data-idx="0">/.test(getElementById("project-body").innerHTML) };
  },
  async h3_prompt_clean_skips_save() {
    const answer = { ok: true, idx: 0, prompt: "P", pictures: [], audios: [],
                     keyframe: { kind: null, path: null }, duration: 8, seed: 42, steps: 50 };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1/scenes/0/h3-prompt": ok(answer) }));
    await open();
    await act("scene-h3-prompt", { idx: "0" });
    const watched = ["PUT /api/projects/p1/scenes", "GET /api/projects/p1/scenes/0/h3-prompt"];
    return { order: calls.map((c) => `${c.method} ${c.url}`).filter((k) => watched.includes(k)),
             shown: /<div class="h3-prompt" data-idx="0">/.test(getElementById("project-body").innerHTML) };
  },
  async h3_prompt_refused() {
    await start(appUrl, draftRoutes({ "GET /api/projects/p1/scenes/0/h3-prompt": { status: 400,
      body: { ok: false, error: { message: "scene 0: 190 frames is off sglang's grid" } } } }));
    await open();
    await act("scene-h3-prompt", { idx: "0" });
    return { shown: /<div class="h3-prompt"/.test(getElementById("project-body").innerHTML),
             errorHidden: getElementById("project-err").hidden, error: getElementById("project-err").innerHTML };
  },
  async h3_prompt_late_answer_dropped() {
    const answer = { ok: true, idx: 0, prompt: "P", pictures: [], audios: [],
                     keyframe: { kind: null, path: null }, duration: 8, seed: 42, steps: 50 };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1/scenes/0/h3-prompt":
      () => sleep(80).then(() => ok(answer)) }));
    await open();
    clickReal("scene-h3-prompt", { idx: "0" });
    await sleep(30);                       // the GET is in flight: the person types
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps far")];
    fire("input", { value: "@a jumps far", selectionStart: 0, title: "", dataset: { sceneField: "prompt", idx: "0" },
      classList: { toggle() {}, contains: () => false },
      closest(sel) { return sel === "[data-scene-field]" ? this : null; } });
    await sleep(200);
    return { shown: /<div class="h3-prompt"/.test(getElementById("project-body").innerHTML) };
  },
  async h3_prompt_dropped_by_reference_change() {
    const answer = { ok: true, idx: 0, prompt: "P", pictures: [], audios: [],
                     keyframe: { kind: null, path: null }, duration: 8, seed: 42, steps: 50 };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1/scenes/0/h3-prompt":
      () => sleep(80).then(() => ok(answer)), "PUT /api/projects/p1/references": ok({ ok: true, references: [] }) }));
    await open();
    clickReal("scene-h3-prompt", { idx: "0" });
    await sleep(30);                       // the GET is in flight: a reference is ticked
    const pin = { checked: true, dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-pin" } };
    const box = { dataset: { id: "p1" }, querySelectorAll: (sel) => (sel === ".ref-pin" ? [pin] : []) };
    pin.closest = (sel) => (sel === ".project-refs" ? box : null);
    fire("change", pin);
    await sleep(250);
    return { shown: /<div class="h3-prompt"/.test(getElementById("project-body").innerHTML) };
  },
  async h3_prompt_answer_kept_when_nothing_moved() {
    const answer = { ok: true, idx: 0, prompt: "P", pictures: [], audios: [],
                     keyframe: { kind: null, path: null }, duration: 8, seed: 42, steps: 50 };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1/scenes/0/h3-prompt":
      () => sleep(80).then(() => ok(answer)) }));
    await open();
    await act("scene-h3-prompt", { idx: "0" });
    return { shown: /<div class="h3-prompt"/.test(getElementById("project-body").innerHTML) };
  },
  async h3_prompt_refused_off_grid() {
    await start(appUrl, draftRoutes({ "GET /api/projects/p1/scenes/0/h3-prompt": { status: 400,
      body: { ok: false, error: { code: "duration_off_grid", message: "scene 0: 190 frames is off sglang's 17n+5 grid" } } } }));
    await open();
    await act("scene-h3-prompt", { idx: "0" });
    return { error: getElementById("project-err").innerHTML };
  },
  async h3_prompt_goes_out_on_edit() {
    const answer = { ok: true, idx: 0, prompt: "P", pictures: [], audios: [],
                     keyframe: { kind: null, path: null }, duration: 8, seed: 42, steps: 50 };
    await start(appUrl, draftRoutes({ "GET /api/projects/p1/scenes/0/h3-prompt": ok(answer),
      "PUT /api/projects/p1/references": ok({ ok: true, references: [] }) }));
    await open();
    await act("scene-h3-prompt", { idx: "0" });
    // typing in a field: the shown prompt is stale at once, in the page and in the state
    const gone = { removed: 0, remove() { this.removed += 1; } };
    queryAll["#project-body .h3-prompt"] = [gone];
    queryAll[FIELDS] = [field("prompt", 0, "@a jumps far")];
    fire("input", { value: "@a jumps far", selectionStart: 0, title: "", dataset: { sceneField: "prompt", idx: "0" },
      classList: { toggle() {}, contains: () => false },
      closest(sel) { return sel === "[data-scene-field]" ? this : null; } });
    const pin = { checked: true, dataset: { tag: "@a" }, classList: { contains: (c) => c === "ref-pin" } };
    const box = { dataset: { id: "p1" }, querySelectorAll: (sel) => (sel === ".ref-pin" ? [pin] : []) };
    pin.closest = (sel) => (sel === ".project-refs" ? box : null);
    fire("change", pin);
    await sleep(120);
    return { removed: gone.removed,
             shownAfterRedraw: /<div class="h3-prompt"/.test(getElementById("project-body").innerHTML) };
  },
};

SCENARIOS.finished_tiles_stable = async function finished_tiles_stable() {
  // Acceptance D3: every poll rebuilt the "Готово" tiles, so each <video> was requested again
  // ("?×? / ? с" and a black frame in between). Same data twice -> the tiles are not reassigned.
  const done = { id: "j1", exit_code: 0, kind: "generate", output_stem: "/o/clip", note: "",
    args: [], started_at: "2026-10-08T01:00:00Z", finished_at: "2026-10-08T01:05:00Z",
    estimate: { width: 896, height: 512, duration_seconds: 4 } };
  const state = { ...SGLANG, queue: { ...SGLANG.queue, done: [done] } };
  await start(appUrl, { "GET /api/state": ok(state) });
  const afterFirst = (htmlWrites.finished || []).length;
  await intervals[0]();
  await intervals[0]();
  const same = (htmlWrites.finished || []).length;
  // a new finished job is news: the list is drawn again
  const second = { ...done, id: "j2", output_stem: "/o/clip2" };
  routes["GET /api/state"] = ok({ ...state, queue: { ...state.queue, done: [second, done] } });
  await intervals[0]();
  const html = getElementById("finished").innerHTML;
  return { afterFirst, writesAfterTwoMorePolls: same,
           writesAfterNewJob: (htmlWrites.finished || []).length, tiles: (html.match(/<article /g) || []).length };
};

const run = SCENARIOS[scenario];
if (!run) fail(`unknown scenario ${scenario}`);
run().then((out) => { process.stdout.write(JSON.stringify(out)); process.exit(0); },
           (e) => fail(`${e && e.stack || e}`));
