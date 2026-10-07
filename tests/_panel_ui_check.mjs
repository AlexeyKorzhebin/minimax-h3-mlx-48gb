// Task 12 fix round 1: drives the REAL `h3_48gb/webui/app.js` DOM half (no server, a scripted
// `fetch`) through the wave-1 UI actions and prints what happened as one JSON document, which
// `tests/test_webui_panel.py` compares for exact equality.
//
// usage: node _panel_ui_check.mjs <appUrl> <scenario>
// The fake DOM is a Proxy per element id that records `addEventListener` and lets `.hidden`,
// `.value`, `.innerHTML`, ... be read back; document-level listeners are collected so a scenario
// can fire the same delegated `click`/`change` events a browser would.

const [, , appUrl, scenario] = process.argv;
const fail = (m) => { process.stderr.write(`${m}\n`); process.exit(1); };
if (!appUrl || !scenario) fail("usage: node _panel_ui_check.mjs <appUrl> <scenario>");

const SGLANG = { ok: true, engine: "sglang", platform: "linux", worker: { state: "stopped" },
                 paused: true, outdir: "/o", queue: { pending: [], running: [], done: [], failed: [],
                 broken: [] }, runs: [], projects: [] };

function makeEl(label) {
  const store = { value: "", textContent: "", innerHTML: "", checked: false, hidden: false, title: "" };
  const own = {};
  return new Proxy(function stub() {}, {
    get(_t, prop) {
      if (prop === "then" || typeof prop === "symbol") return undefined;
      if (prop === "dataset" || prop === "style") return (store[prop] ||= {});
      if (prop === "classList") {
        return (store.classList ||= { add() {}, remove() {}, toggle() {}, contains: () => false });
      }
      if (prop === "__listeners") return own;
      if (prop in store) return store[prop];
      if (prop === "addEventListener") return (type, fn) => { (own[type] ||= []).push(fn); };
      if (prop === "removeEventListener") return () => {};
      if (prop === "querySelector" || prop === "closest") return () => null;
      if (prop === "querySelectorAll") return () => [];
      if (prop === "children" || prop === "childNodes") return [];
      return () => makeEl(`${label}.${String(prop)}`);
    },
    set(_t, prop, value) { store[prop] = value; return true; },
  });
}
const els = new Map();
const getElementById = (id) => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); };
const docListeners = {};
globalThis.document = {
  body: makeEl("body"), documentElement: makeEl("html"), getElementById,
  querySelectorAll: () => [], querySelector: () => null, createElement: () => makeEl("new"),
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); }, removeEventListener() {},
};
console.error = () => {};
const alerts = [];
const confirms = [];
let confirmAnswer = true;
globalThis.alert = (m) => alerts.push(m);
globalThis.confirm = (m) => { confirms.push(m); return confirmAnswer; };
globalThis.window = { confirm: globalThis.confirm, alert: globalThis.alert, prompt: () => null,
  location: { hash: "", host: "x" }, addEventListener() {}, removeEventListener() {}, scrollTo() {} };
const storage = new Map();
globalThis.localStorage = { getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)), removeItem: (k) => storage.delete(k) };
const notifications = [];
globalThis.Notification = class { constructor(title, opts) { notifications.push({ title, body: opts.body }); } };
globalThis.Notification.permission = "granted";
const intervals = [];
globalThis.setInterval = (fn) => { intervals.push(fn); return intervals.length; };

// -- scripted fetch: routes["METHOD url"] is a response or a list consumed one per call ----------
const routes = {};
const calls = [];
globalThis.fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  const key = `${method} ${url}`;
  calls.push({ method, url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  let entry = routes[key];
  if (Array.isArray(entry)) entry = entry.length > 1 ? entry.shift() : entry[0];
  if (typeof entry === "function") entry = entry();
  if (!entry) entry = { status: 404, body: { error: { code: "no_route", message: key } } };
  return { ok: entry.status >= 200 && entry.status < 300, status: entry.status, json: async () => entry.body };
};
const ok = (body) => ({ status: 200, body });
const err = (status, code, message) => ({ status, body: { ok: false, error: { code, message } } });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clickable = (props) => ({ ...props, closest(sel) { return props.match(sel) ? this : null; } });
const fire = (type, target) => (docListeners[type] || []).forEach((fn) => fn({ target }));
const countCalls = (m, u) => calls.filter((c) => c.method === m && c.url === u).length;

const PROJECT = (over = {}) => ({ ok: true, active_job: null, project: {
  id: "p1", kind: "video", title: "P", stages: { script: "approved", scenes: "done", upscale: "draft",
  assembly: "draft", scenario: "approved", track: "approved" }, scenes: [], scenario_scenes: [],
  references: [], route: [{ stage: "upscale", enabled: true }], i2v_prefix: "", track: {}, assembly: {},
  ...over } });

async function start(extra = {}) {
  Object.assign(routes, { "GET /api/state": ok(SGLANG), "GET /api/llm": ok({ status: "" }),
    "GET /api/providers": ok({ active: "", providers: [] }), "GET /api/library": ok({ cards: [] }),
    "GET /api/gpu": ok({ ok: true, dispatcher: null, dispatcher_error: "x", idle_release_at: null,
                         queue: { pending: 0, paused: true, running: null } }), ...extra });
  await import(appUrl);
  await sleep(80);
}

async function main() {
  let out;
  if (scenario === "release_confirmed" || scenario === "release_declined" || scenario === "release_second_fails") {
    confirmAnswer = scenario !== "release_declined";
    const second = scenario === "release_second_fails" ? err(500, "dispatcher_unavailable", "диспетчер лёг") : ok({ ok: true });
    await start({ "POST /api/gpu/release": [
      err(409, "release_needs_confirm", "H3 считает сцену — освободить карту? Сцена будет потеряна"), second] });
    const stateReads = countCalls("GET", "/api/state");
    getElementById("gpu-release").__listeners.click[0]();
    await sleep(80);
    out = { bodies: calls.filter((c) => c.url === "/api/gpu/release").map((c) => c.body),
            confirms, alerts, repolled: countCalls("GET", "/api/state") > stateReads };
  } else if (scenario === "notify" || scenario === "render_throws") {
    const soon = new Date(Date.now() - 11 * 60_000).toISOString();
    const running = { id: "j1", kind: "generate", note: "", started_at: soon, wait_reason: "ждём GPU: занято" };
    const gpuBody = ok({ ok: true, dispatcher: { own: {}, foreign: scenario === "render_throws" ? "boom" : [], qwen: { running: false, unloaded_by_us: false },
      gpu: null }, dispatcher_error: null, idle_release_at: null, queue: { pending: 1, paused: false, running } });
    const withProject = (id, assembly) => ok({ ...SGLANG, projects: [{ id, stages: { assembly } }] });
    await start({ "GET /api/state": withProject("p1", "done"), "GET /api/gpu": gpuBody });
    const afterFirst = notifications.splice(0);          // first snapshot after opening: silent
    routes["GET /api/state"] = withProject("p2", "done");
    await intervals[0]();
    // p1 vanished and p2 is new: one «проект готов»; the 11-minute wait was primed by the first
    // snapshot, so it must NOT fire again within the hour
    out = { afterFirst, afterSecond: notifications.splice(0) };
  } else if (scenario === "route_error") {
    // the server has the upscale ON, the user unticks it, the server refuses: only a redraw from
    // the server's state puts the tick back (the body is blanked first so a missing redraw shows)
    await start({ "GET /api/projects/p1": ok(PROJECT({ route: [{ stage: "upscale", enabled: true }] })),
      "PUT /api/projects/p1/route": err(409, "route_locked", "маршрут нельзя менять") });
    fire("click", clickable({ dataset: { act: "open-project", id: "p1" }, match: (s) => s === "button[data-act]" }));
    await sleep(80);
    const before = { project: countCalls("GET", "/api/projects/p1"), providers: countCalls("GET", "/api/providers") };
    getElementById("project-body").innerHTML = "STALE";
    const box = { checked: false, dataset: { id: "p1" }, classList: { contains: (c) => c === "route-upscale-box" },
                  closest: () => null };
    fire("change", box);
    await sleep(120);
    out = { puts: calls.filter((c) => c.method === "PUT").map((c) => [c.url, c.body]), alerts,
            projectRereads: countCalls("GET", "/api/projects/p1") - before.project,
            providerReloads: countCalls("GET", "/api/providers") - before.providers,
            boxChecked: /class="route-upscale-box"[^>]*checked/.test(getElementById("project-body").innerHTML),
            errorHidden: getElementById("project-err").hidden,
            errorHtml: getElementById("project-err").innerHTML };
  } else if (scenario === "upscale_retry") {
    await start({ "GET /api/projects/p1": ok(PROJECT({ stages: { script: "approved", scenes: "done",
      upscale: "failed", assembly: "draft", scenario: "approved", track: "approved" } })),
      "POST /api/projects/p1/upscale/retry": ok({ ok: true }) });
    fire("click", clickable({ dataset: { act: "open-project", id: "p1" }, match: (s) => s === "button[data-act]" }));
    await sleep(80);
    const html = getElementById("project-body").innerHTML;
    fire("click", clickable({ dataset: { id: "p1" }, match: (s) => s === ".upscale-retry" }));
    await sleep(80);
    out = { status: /<div class="upscale-status"[^>]*>Апскейл LTX: упал/.test(html),
            posts: calls.filter((c) => c.method === "POST").map((c) => [c.url, c.body]) };
  } else if (scenario === "scene_error") {
    // final review C2: the reason a chained scene could not be submitted is on its card
    const scene = (idx, status, extra = {}) => ({ idx, prompt: "@a walks", duration: 8, status,
      job_id: null, clip_path: null, keyframe_path: null, ...extra });
    await start({ "GET /api/projects/p1": ok(PROJECT({ scenes: [scene(0, "done"),
      scene(1, "failed", { error: "сцена не поставлена: AssembleError: кадр <залит>" })] })) });
    fire("click", clickable({ dataset: { act: "open-project", id: "p1" }, match: (s) => s === "button[data-act]" }));
    await sleep(80);
    out = { errors: getElementById("project-body").innerHTML.match(/<div class="scene-error why">[^<]*<\/div>/g) };
  } else if (scenario === "input_sglang" || scenario === "input_mlx") {
    const engine = scenario === "input_mlx" ? "mlx" : "sglang";
    await start({ "GET /api/state": ok({ ...SGLANG, engine }), "GET /api/projects/p1": ok(PROJECT()) });
    fire("click", clickable({ dataset: { act: "open-project", id: "p1" }, match: (s) => s === "button[data-act]" }));
    await sleep(80);
    const toggles = [];
    const area = { value: "a dog", title: "", selectionStart: 5, nextElementSibling: null, after() {},
                   classList: { toggle: (name, on) => toggles.push([name, on]), contains: () => false },
                   closest(sel) { return sel === ".scenario-prompt" ? this : null; } };
    fire("input", area);
    out = { title: area.title, toggles };
  } else if (scenario === "cancel_run") {
    await start({ "DELETE /api/jobs/j1": ok({ ok: true, cancelling: true,
      message: "H3 досчитает сцену впустую, следующая задача начнётся после" }) });
    fire("click", clickable({ dataset: { act: "cancel-run", id: "j1" }, match: (s) => s === "button[data-act]" }));
    await sleep(80);
    out = { deletes: calls.filter((c) => c.method === "DELETE").map((c) => c.url), alerts };
  } else if (scenario === "library_save") {
    await start({ "PUT /api/library/alice": ok({ ok: true, card: {} }) });
    const cardError = { hidden: true, textContent: "" };
    const card = { querySelector: (sel) => (sel === ".lib-card-error" ? cardError : { value: "  a woman in a green coat " }) };
    // the card is found through `closest(".lib-card")` on the button
    const button = { dataset: { tag: "@alice" }, closest(sel) { return sel === ".lib-save" ? this : sel === ".lib-card" ? card : null; } };
    fire("click", button);
    await sleep(80);
    out = { puts: calls.filter((c) => c.method === "PUT").map((c) => [c.url, c.body]),
            cardError, addFormError: getElementById("lib-error").hidden };
  } else if (scenario === "library_save_error") {
    await start({ "PUT /api/library/alice": err(409, "library_busy", "карточка занята") });
    const cardError = { hidden: true, textContent: "" };
    const card = { querySelector: (sel) => (sel === ".lib-card-error" ? cardError : { value: "x" }) };
    fire("click", { dataset: { tag: "@alice" }, closest(sel) { return sel === ".lib-save" ? this : sel === ".lib-card" ? card : null; } });
    await sleep(80);
    out = { cardError, addFormErrorHidden: getElementById("lib-error").hidden };
  } else {
    fail(`unknown scenario ${scenario}`);
  }
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}
main().catch((e) => fail(`${e && e.stack || e}`));
