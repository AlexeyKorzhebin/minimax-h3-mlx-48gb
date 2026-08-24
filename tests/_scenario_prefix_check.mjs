// Находка 2 (волна ux-фиксов 2026-08-24): drives the REAL `h3_48gb/webui/app.js` -- not a
// reimplementation -- through the exact gesture the "common prefix" fix introduces: the page now
// shows only the TAIL of a scene's prompt in `.scenario-prompt` (`projectScenarioSceneHtml`'s own
// docstring -- the common visual-bible prefix, shared by every scene, is sliced off and shown once,
// read-only, above the list instead), and `collectScenarioScenes` is the one place that has to
// glue that prefix back on before `PUT` ever leaves the page. Get that reconstruction wrong --
// forget the prefix, or add it twice -- and the edit that reaches disk is silently wrong,
// permanently: `PUT /api/projects/<id>/scenario` replaces the whole scene list at once (`web.
// _edit_project_scenario`'s own docstring), so a bad reconstruction here does not 400, it just
// writes the wrong prompt.
//
// Same technique as `_scenario_race_check.mjs`/`_scenario_busy_check.mjs` (see the first one's own
// module docstring for why this needs `node`, not a `_node_eval` snippet): a minimal `document`/
// `window`/`localStorage` is wired up *before* `app.js` is imported, so `startPage()` actually
// runs with its real closures (`collectScenarioScenes`, `saveScenario`, `scenarioCommonPrefix`, the
// delegated `focusout` listener) exactly as a browser would build them -- and `fetch` wraps the
// real global (Node 18+) against a real, already-running `h3_48gb.web` server, logging every
// request's URL *and body*, which `_scenario_race_check.mjs` does not need but this test does: the
// only way to see what `collectScenarioScenes` actually sent is to read the `PUT`'s own JSON body.
//
// This script edits only ONE scene's tail field (scene index `editIdx`) and leaves every other
// scene's `.scenario-prompt` untouched in the DOM (`promptFields` only ever contains that one
// entry) -- `collectScenarioScenes`'s own fallback ("a field not in the DOM falls back to the
// value already on disk", its own docstring) is what every OTHER scene has to go through, so this
// also proves the fix does not corrupt scenes nobody touched.

const [, , appUrl, baseUrl, pid, editIdxArg, editedTailB64] = process.argv;
const editIdx = Number(editIdxArg);
const editedTail = Buffer.from(editedTailB64, "base64").toString("utf-8");

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

if (!appUrl || !baseUrl || !pid || !editIdxArg || !editedTailB64 || !Number.isFinite(editIdx)) {
  fail("usage: node _scenario_prefix_check.mjs <appUrl> <baseUrl> <pid> <editIdx> <editedTailB64>");
}

// -- a generic, self-mocking DOM node -- identical to `_scenario_race_check.mjs`'s own -----------
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

// Only the edited scene's field exists in this "DOM" -- see module docstring: every other scene
// must come from `collectScenarioScenes`'s own on-disk fallback, not from here.
let promptFields = [];

const listeners = {};
const fakeDocument = {
  body: makeStub("body"),
  documentElement: makeStub("html"),
  getElementById,
  querySelectorAll(sel) {
    if (sel === "#project-body .scenario-prompt") return promptFields;
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

  // "type" the edited tail into the one scene this script touches -- ONLY the tail, exactly what
  // the rendered `.scenario-prompt` textarea now holds (`projectScenarioSceneHtml`'s own doc).
  promptFields = [{ dataset: { idx: String(editIdx) }, value: editedTail }];

  const field = {
    dataset: { idx: String(editIdx) },
    value: editedTail,
    classList: { contains: (c) => c === "scenario-prompt" },
    closest(sel) { return sel.includes("scenario-prompt") ? this : null; },
  };
  (listeners.focusout || []).forEach((fn) => fn({ target: field }));

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
