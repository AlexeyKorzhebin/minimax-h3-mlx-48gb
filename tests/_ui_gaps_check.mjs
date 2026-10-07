// Wave 1.5 DOM wiring scenarios; usage: node _ui_gaps_check.mjs <appUrl> <scenario>
import { routes, calls, alerts, prompts, answers, getElementById, start, ok, err, PROJECT, sleep, queryAll, queryOne,
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
const act = async (name, extra = {}) => {
  fire("click", clickable({ dataset: { act: name, id: "p1", ...extra }, match: (s) => s === "button[data-act]" }));
  await sleep(80);
  liveEditor();
};
const puts = () => calls.filter((c) => c.method === "PUT").map((c) => [c.url, c.body]);
const writes = () => calls.filter((c) => c.method !== "GET").map((c) => [c.method, c.url]);
const draftRoutes = (extra = {}) => ({ "GET /api/projects/p1": ok(DRAFT_PROJECT),
  "PUT /api/projects/p1/scenes": ok({ ok: true, project: DRAFT_PROJECT.project }), ...extra });

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
    // the real handler (app.js:5064-5070): target.closest(".project-refs") -> box.querySelectorAll(".ref-pin")
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
      closest(sel) { return sel === '[data-scene-field="duration"]' ? this : null; } };
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
    fire("click", clickable({ dataset: { act: "scene-down", id: "p1", idx: "0" }, match: (s) => s === "button[data-act]" }));
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
};

const run = SCENARIOS[scenario];
if (!run) fail(`unknown scenario ${scenario}`);
run().then((out) => { process.stdout.write(JSON.stringify(out)); process.exit(0); },
           (e) => fail(`${e && e.stack || e}`));
