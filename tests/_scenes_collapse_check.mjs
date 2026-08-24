// Task 10 (волна ux-фиксов 2026-08-24): drives the REAL `h3_48gb/webui/app.js` -- same harness
// technique as `_scenario_busy_check.mjs`/`_done_project_filter_check.mjs` (see either's own
// module docstring) -- through a click on "open-project" for a project whose `scenes` are already
// seeded (one done, one running, one pending) against a real, already-running server, and reports
// the raw `#project-body` markup `renderProjectModal()` actually produced.
//
// Before this fix, `projectScenesStageHtml` always rendered every scene card open, unconditionally
// -- a project with nine scenes filled the whole panel with nine cards before a human asked to see
// even one of them. The fix wraps the block in a collapsed-by-default `<details class="adv
// proj-scenes-block">`, the same native-`<details>` idiom `projectScenarioScenesHtml`'s own common-
// prefix block already uses (`scenario-common`) -- collapsing it is then plain browser behaviour,
// not something `app.js` has to implement itself, so what this script actually has to prove is
// narrower: (1) the counter/status text visible in the closed `<summary>` is correct without
// opening anything, and (2) the block really is a `<details>...<summary>...</summary>...</details>`
// with no `open` attribute -- get either wrong (missing `<details>`, an `open` attribute left on,
// a mis-nested `</details>`) and either the counter lies or the "expand on click" native behaviour
// this relies on silently stops working.

const [, , appUrl, baseUrl, pid] = process.argv;

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

if (!appUrl || !baseUrl || !pid) {
  fail("usage: node _scenes_collapse_check.mjs <appUrl> <baseUrl> <pid>");
}

function makeStub(label) {
  const store = { value: "", textContent: "", innerHTML: "", checked: false, hidden: false,
                  title: "" };
  const target = function stub() {};
  const handler = {
    get(_t, prop) {
      if (prop === "then" || prop === "catch" || prop === "finally" || typeof prop === "symbol") {
        return undefined;
      }
      if (prop === "dataset" || prop === "style") {
        if (!store[prop]) store[prop] = {};
        return store[prop];
      }
      if (prop === "classList") {
        if (!store.classList) {
          store.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
        }
        return store.classList;
      }
      if (prop in store) return store[prop];
      if (prop === "addEventListener" || prop === "removeEventListener") return () => {};
      if (prop === "querySelector" || prop === "closest") return () => null;
      if (prop === "querySelectorAll") return () => [];
      if (prop === "children" || prop === "childNodes") return [];
      return () => makeStub(`${label}.${String(prop)}`);
    },
    set(_t, prop, value) { store[prop] = value; return true; },
    deleteProperty(_t, prop) { delete store[prop]; return true; },
  };
  return new Proxy(target, handler);
}

const elementCache = new Map();
function getElementById(id) {
  if (!elementCache.has(id)) elementCache.set(id, makeStub(`#${id}`));
  return elementCache.get(id);
}

const listeners = {};
const fakeDocument = {
  body: makeStub("body"),
  documentElement: makeStub("html"),
  getElementById,
  querySelectorAll() { return []; },
  querySelector() { return null; },
  createElement() { return makeStub("created"); },
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  removeEventListener() {},
};

const fakeWindow = {
  confirm: () => true,
  prompt: () => null,
  location: { hash: "", host: new URL(baseUrl).host },
  addEventListener() {},
  removeEventListener() {},
  scrollTo() {},
};

const storage = new Map();
const fakeLocalStorage = {
  getItem: (k) => (storage.has(k) ? storage.get(k) : null),
  setItem: (k, v) => storage.set(k, String(v)),
  removeItem: (k) => storage.delete(k),
};

globalThis.document = fakeDocument;
globalThis.window = fakeWindow;
globalThis.localStorage = fakeLocalStorage;

const realFetch = globalThis.fetch;
const callLog = [];
globalThis.fetch = async (url, opts) => {
  const entry = { method: (opts && opts.method) || "GET", url, startedAt: Date.now(), done: false };
  callLog.push(entry);
  try {
    const res = await realFetch(baseUrl + url, opts);
    entry.finishedAt = Date.now();
    entry.status = res.status;
    entry.done = true;
    return res;
  } catch (err) {
    entry.finishedAt = Date.now();
    entry.done = true;
    entry.error = String(err);
    throw err;
  }
};

async function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function waitFor(pred, timeoutMs, what) {
  const t0 = Date.now();
  while (!pred()) {
    if (Date.now() - t0 > timeoutMs) fail(`timeout waiting for: ${what}`);
    await sleep(10);
  }
}

async function main() {
  await import(appUrl);

  await waitFor(() => callLog.some((c) => c.method === "GET" && c.url === "/api/state" && c.done),
                8000, "initial GET /api/state");
  await sleep(30);

  const openBtn = {
    dataset: { act: "open-project", id: pid },
    closest(sel) { return sel === "button[data-act]" ? this : null; },
  };
  (listeners.click || []).forEach((fn) => fn({ target: openBtn }));
  await waitFor(
    () => callLog.some((c) => c.method === "GET" && c.url === `/api/projects/${pid}` && c.done),
    8000, "GET /api/projects/<id> from openProjectModal");
  await sleep(30);

  process.stdout.write(JSON.stringify({
    projectBodyHtml: getElementById("project-body").innerHTML,
  }));
  process.exit(0);
}

main().catch((err) => fail(`${err && err.stack || err}`));
