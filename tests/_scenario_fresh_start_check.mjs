// P0 fix (keyframe-chain defect, 2026-08-25 nightly run): drives the REAL `h3_48gb/webui/app.js`
// -- not a reimplementation -- through the exact gesture the `fresh_start` toggle introduces:
// checking `.scenario-fresh-start` on one scene card must fire `PUT /api/projects/<id>/scenario`
// with that scene's own `fresh_start: true`, on `change` (a checkbox has no meaningful `focusout`,
// see `app.js`'s own delegated `change` listener for `.scenario-fresh-start`) -- while every OTHER
// scene not present in this test's own "DOM" must still be sent with whatever `fresh_start` it
// already had on disk, unchanged (`collectScenarioScenes`'s own on-disk fallback for a field not
// found in the DOM, the same contract `_scenario_prefix_check.mjs` already proves for `.scenario-
// prompt`).
//
// Same technique as `_scenario_prefix_check.mjs`/`_scenario_race_check.mjs` (see the first one's
// own module docstring for why this needs `node`, not a `_node_eval` snippet): a minimal
// `document`/`window`/`localStorage` is wired up *before* `app.js` is imported, so `startPage()`
// actually runs with its real closures (`collectScenarioScenes`, `saveScenario`, the delegated
// `change` listener) exactly as a browser would build them, and `fetch` wraps the real global
// (Node 18+) against a real, already-running `h3_48gb.web` server, logging every request's URL
// and body.

const [, , appUrl, baseUrl, pid, toggleIdxArg] = process.argv;
const toggleIdx = Number(toggleIdxArg);

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

if (!appUrl || !baseUrl || !pid || !toggleIdxArg || !Number.isFinite(toggleIdx)) {
  fail("usage: node _scenario_fresh_start_check.mjs <appUrl> <baseUrl> <pid> <toggleIdx>");
}

// -- a generic, self-mocking DOM node -- identical to `_scenario_prefix_check.mjs`'s own -----------
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

// Only the toggled scene's own checkbox exists in this "DOM" -- `.scenario-prompt`/`.scenario-
// duration` stay empty, so `collectScenarioScenes` reads every OTHER field (prompt, duration, and
// every other scene's own `fresh_start`) straight off the on-disk `scenario_scenes` it already
// holds in `project.project`, exactly the fallback path a real render never populates for a scene
// the user has not touched.
let freshStartFields = [];

const listeners = {};
const fakeDocument = {
  body: makeStub("body"),
  documentElement: makeStub("html"),
  getElementById,
  querySelectorAll(sel) {
    if (sel === "#project-body .scenario-fresh-start") return freshStartFields;
    if (sel === "#project-body .scenario-prompt") return [];
    if (sel === "#project-body .scenario-duration") return [];
    return [];
  },
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

// -- fetch: the real thing, against the real server, logging URL AND body ------------------------
const realFetch = globalThis.fetch;
const callLog = [];
globalThis.fetch = async (url, opts) => {
  const entry = { method: (opts && opts.method) || "GET", url, body: opts && opts.body,
                  startedAt: Date.now(), done: false };
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

  // "check" the toggled scene's own checkbox -- ONLY this one field exists in the DOM, matching
  // exactly what a real render would show for a card the user just clicked.
  const checkbox = {
    dataset: { idx: String(toggleIdx) },
    checked: true,
    classList: { contains: (c) => c === "scenario-fresh-start" },
    // Exact match, not `.includes()` -- the real listener's own selector is a single class,
    // `.scenario-fresh-start`, with no compound sibling the way `.scenario-prompt, .scenario-
    // duration` has one for `focusout` -- a loose substring match would still "match" a mutated
    // selector like `.scenario-fresh-start-typo` and silently defeat this test's own mutation
    // check.
    closest(sel) { return sel === ".scenario-fresh-start" ? this : null; },
  };
  freshStartFields = [checkbox];

  (listeners.change || []).forEach((fn) => fn({ target: checkbox }));

  await waitFor(
    () => callLog.some((c) => c.method === "PUT" && c.url === `/api/projects/${pid}/scenario`
      && c.done),
    15000, "PUT /api/projects/<id>/scenario");

  const put = callLog.find((c) => c.method === "PUT" && c.url === `/api/projects/${pid}/scenario`);

  const finalRes = await realFetch(`${baseUrl}/api/projects/${pid}`);
  const finalJson = await finalRes.json();

  process.stdout.write(JSON.stringify({
    putStatus: put.status,
    putBody: JSON.parse(put.body),
    finalProject: finalJson.project,
  }));
  process.exit(0);
}

main().catch((err) => fail(`${err && err.stack || err}`));
