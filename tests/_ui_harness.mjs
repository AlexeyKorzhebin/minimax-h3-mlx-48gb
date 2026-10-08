// Shared fake DOM + scripted fetch for the node UI checks (tests/_panel_ui_check.mjs, _ui_gaps_check.mjs).
export const SGLANG = { ok: true, engine: "sglang", platform: "linux", worker: { state: "stopped" },
                 paused: true, outdir: "/o", queue: { pending: [], running: [], done: [], failed: [],
                 broken: [] }, runs: [], projects: [] };

// every innerHTML assignment per element label, so a scenario can tell a redraw from a skipped one
export const htmlWrites = {};

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
    set(_t, prop, value) {
      if (prop === "innerHTML") (htmlWrites[label] ||= []).push(value);
      store[prop] = value; return true;
    },
  });
}
const els = new Map();
export const getElementById = (id) => { if (!els.has(id)) els.set(id, makeEl(id)); return els.get(id); };
const docListeners = {};
export const queryAll = {};
export const queryOne = {};
globalThis.document = {
  body: makeEl("body"), documentElement: makeEl("html"), getElementById,
  querySelectorAll: (sel) => (Object.hasOwn(queryAll, sel) ? queryAll[sel] : []),
  querySelector: (sel) => (Object.hasOwn(queryOne, sel) ? queryOne[sel] : null),
  createElement: () => makeEl("new"),
  addEventListener(type, fn) { (docListeners[type] ||= []).push(fn); }, removeEventListener() {},
};
console.error = () => {};
export const alerts = [];
export const confirms = [];
export const prompts = [];
export const answers = { confirm: true, prompt: null };
globalThis.alert = (m) => alerts.push(m);
globalThis.confirm = (m) => { confirms.push(m); return answers.confirm; };
globalThis.prompt = (m) => { prompts.push(m); return answers.prompt; };
globalThis.window = { confirm: globalThis.confirm, alert: globalThis.alert, prompt: globalThis.prompt,
  location: { hash: "", host: "x" }, addEventListener() {}, removeEventListener() {}, scrollTo() {} };
const storage = new Map();
globalThis.localStorage = { getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)), removeItem: (k) => storage.delete(k) };
export const notifications = [];
globalThis.Notification = class { constructor(title, opts) { notifications.push({ title, body: opts.body }); } };
globalThis.Notification.permission = "granted";
export const intervals = [];
globalThis.setInterval = (fn) => { intervals.push(fn); return intervals.length; };

// -- scripted fetch: routes["METHOD url"] is a response or a list consumed one per call ----------
export const routes = {};
export const calls = [];
globalThis.fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  const key = `${method} ${url}`;
  const raw = opts && opts.body;
  calls.push({ method, url, headers: (opts && opts.headers) || null,
               body: typeof raw === "string" ? JSON.parse(raw) : (raw ? { raw: raw.name || "blob" } : null) });
  let entry = routes[key];
  if (Array.isArray(entry)) entry = entry.length > 1 ? entry.shift() : entry[0];
  if (typeof entry === "function") entry = await entry();
  if (!entry) entry = { status: 404, body: { error: { code: "no_route", message: key } } };
  return { ok: entry.status >= 200 && entry.status < 300, status: entry.status, json: async () => entry.body };
};
export const ok = (body) => ({ status: 200, body });
export const err = (status, code, message) => ({ status, body: { ok: false, error: { code, message } } });
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const clickable = (props) => ({ ...props, closest(sel) { return props.match(sel) ? this : null; } });
export const fire = (type, target) => (docListeners[type] || []).forEach((fn) => fn({ target }));
export const countCalls = (m, u) => calls.filter((c) => c.method === m && c.url === u).length;

export const PROJECT = (over = {}) => ({ ok: true, active_job: null, project: {
  id: "p1", kind: "video", title: "P", stages: { script: "approved", scenes: "done", upscale: "draft",
  assembly: "draft", scenario: "approved", track: "approved" }, scenes: [], scenario_scenes: [],
  references: [], route: [{ stage: "upscale", enabled: true }], i2v_prefix: "", track: {}, assembly: {},
  ...over } });

// defaults are set only for keys the scenario has not already put in `routes` (so it can pre-set a
// delayed `GET /api/state`); `extra` then overrides. Returns the imported app module.
export async function start(appUrl, extra = {}) {
  const defaults = { "GET /api/state": ok(SGLANG), "GET /api/llm": ok({ status: "" }),
    "GET /api/providers": ok({ active: "", providers: [] }), "GET /api/library": ok({ cards: [] }),
    "GET /api/gpu": ok({ ok: true, dispatcher: null, dispatcher_error: "x", idle_release_at: null,
                         queue: { pending: 0, paused: true, running: null } }) };
  for (const [key, value] of Object.entries(defaults)) if (!Object.hasOwn(routes, key)) routes[key] = value;
  Object.assign(routes, extra);
  const app = await import(appUrl);
  await sleep(80);
  return app;
}


