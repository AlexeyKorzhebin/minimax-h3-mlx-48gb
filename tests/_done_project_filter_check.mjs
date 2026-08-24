// Task 10 (волна ux-фиксов 2026-08-24): drives the REAL `h3_48gb/webui/app.js` -- not a
// reimplementation -- through its own startup poll (`startPage()` -> `poll()` -> `GET /api/state`
// -> `renderQueue()`) against a real, already-running server that has TWO finished jobs sitting in
// its queue: one an ordinary standalone `kind="generate"` run, one a project scene job whose
// `note` is shaped exactly like `assemble.scene_note` writes it (`project scene <id> #<idx>`).
//
// Before this fix, `renderQueue()` folded `queue.done`/`queue.failed` into one flat "закончилось"
// list with no notion of "this job belongs to a project" at all -- a project's scenes appeared
// twice: once as a card in the project's own modal, once more as an identical-looking tile in the
// general "Готово" section, mixed in with genuinely standalone runs. The fix
// (`isProjectPipelineNote`) filters the second copy out of `#finished`'s own `innerHTML` before it
// is ever set -- this script proves that from OUTSIDE the module: it reads back the real
// `document.getElementById("finished").innerHTML` a real click-free poll cycle produced, not a
// value this script already assumes.
//
// Same harness technique as `_scenario_busy_check.mjs`/`_scenario_prefix_check.mjs` (see either's
// own module docstring for why this needs `node`, not a `_node_eval` snippet): a minimal
// `document`/`window`/`localStorage` is wired up *before* `app.js` is imported, so `startPage()`
// actually runs with its own real closures, and `fetch` wraps the real global (Node 18+) against a
// real, already-running `h3_48gb.web` server.

const [, , appUrl, baseUrl] = process.argv;

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

if (!appUrl || !baseUrl) {
  fail("usage: node _done_project_filter_check.mjs <appUrl> <baseUrl>");
}

// -- a generic, self-mocking DOM node -- identical to the other `_*_check.mjs` scripts' own -------
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

  process.stdout.write(JSON.stringify({
    finishedHtml: getElementById("finished").innerHTML,
    finishedEmptyHidden: getElementById("finished-empty").hidden,
    finishedEmptyText: getElementById("finished-empty").textContent,
    doneSum: getElementById("done-sum").textContent,
  }));
  process.exit(0);
}

main().catch((err) => fail(`${err && err.stack || err}`));
