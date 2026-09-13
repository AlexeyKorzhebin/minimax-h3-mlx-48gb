// Passport wave (2026-08-27, SPEC-scene-prompt-structure.md §5): drives the REAL
// `h3_48gb/webui/app.js` -- not a reimplementation -- through an ordinary scenario edit and reads
// what `collectScenarioScenes` actually put in the `PUT` body.
//
// `state_in`/`state_out` have **no UI control at all** on this page (deliberate -- this wave adds
// no widget), which is exactly what makes them fragile here: every scene's passport reaches the
// server only through `collectScenarioScenes`'s own on-disk fallback ("a field not in the DOM
// falls back to the value already on disk"). Drop the two lines that carry them and nothing on
// the page changes, nothing 400s, and no other test in the suite notices -- `PUT /api/projects/
// <id>/scenario` replaces the whole scene list at once (`web._edit_project_scenario`'s own
// docstring) and the server folds an absent passport to `""`, so the first time anyone fixes a
// typo at the gate, every passport in the scenario is silently erased from disk.
//
// Same technique as `_scenario_prefix_check.mjs`/`_scenario_fresh_start_check.mjs` (see the
// first one's own module docstring for why this needs `node` and not a `_node_eval` snippet): a
// minimal `document`/`window`/`localStorage` is wired up *before* `app.js` is imported, so
// `startPage()` runs with its real closures (`collectScenarioScenes`, `saveScenario`, the
// delegated `focusout` listener) exactly as a browser would build them, and `fetch` wraps the real
// global (Node 18+) against a real, already-running `h3_48gb.web` server, logging every request's
// URL *and body* -- reading that body is the only way to see what the page actually sent.
//
// The gesture is a prompt edit on ONE scene (`editIdx`); every other scene's `.scenario-prompt`
// stays out of this "DOM" entirely, so the untouched scenes go through the same on-disk fallback
// their passports do.

const [, , appUrl, baseUrl, pid, editIdxArg, editedTailB64] = process.argv;
const editIdx = Number(editIdxArg);

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

if (!appUrl || !baseUrl || !pid || !editIdxArg || !editedTailB64 || !Number.isFinite(editIdx)) {
  fail("usage: node _scenario_passport_check.mjs <appUrl> <baseUrl> <pid> <editIdx> "
     + "<editedTailB64>");
}

const editedTail = Buffer.from(editedTailB64, "base64").toString("utf-8");

// -- a generic, self-mocking DOM node -- identical to `_scenario_prefix_check.mjs`'s own ---------
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

let promptFields = [];

const listeners = {};
const fakeDocument = {
  body: makeStub("body"),
  documentElement: makeStub("html"),
  getElementById,
  querySelectorAll(sel) {
    if (sel === "#project-body .scenario-prompt") return promptFields;
    if (sel === "#project-body .scenario-duration") return [];
    if (sel === "#project-body .scenario-fresh-start") return [];
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

// -- fetch: the real thing, against the real server, logging URL AND body -----------------------
const realFetch = globalThis.fetch;
const callLog = [];
globalThis.fetch = async (url, opts) => {
  const entry = { method: (opts && opts.method) || "GET", url, body: opts && opts.body,
                  done: false };
  callLog.push(entry);
  try {
    const res = await realFetch(baseUrl + url, opts);
    entry.status = res.status;
    entry.done = true;
    return res;
  } catch (err) {
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

  const renderedHtml = getElementById("project-body").innerHTML;

  promptFields = [{ dataset: { idx: String(editIdx) }, value: editedTail }];

  const field = {
    dataset: { idx: String(editIdx) },
    value: editedTail,
    classList: { contains: (c) => c === "scenario-prompt" },
    // Exact match, never `.includes()` -- the real listener's own selector is the exact compound
    // string `.scenario-prompt, .scenario-duration` (`app.js`'s own `focusout` listener), and a
    // substring match would happily "match" a mutated selector like `.scenario-prompt-typo, .
    // scenario-duration`, silently defeating every mutation check aimed at that listener (found
    // live three times in two days -- see the memory note `js-driver-mocks-need-exact-match`).
    closest(sel) { return sel === ".scenario-prompt, .scenario-duration" ? this : null; },
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
    renderedHtml,
  }));
  process.exit(0);
}

main().catch((err) => fail(`${err && err.stack || err}`));
