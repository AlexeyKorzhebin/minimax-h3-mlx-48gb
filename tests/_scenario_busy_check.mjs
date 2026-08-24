// Находка 1 (волна ux-фиксов 2026-08-24): drives the REAL `h3_48gb/webui/app.js` -- same
// technique as `_scenario_race_check.mjs` (see its own module docstring for why this needs
// `node`, not a reimplementation) -- through two clicks on "Сюжет без LLM"
// (`data-act="scenario-procedural"`) fired back to back, no `await` between them, the way a fast
// real double-click delivers two `click` events a few milliseconds apart.
//
// `_scenario_race_check.mjs`'s own `makeStub` treats `disabled` as an inert stored property --
// good enough for the race it checks (nothing there ever reads it), useless for this one: the
// actual guard this test exists to prove is `app.js`'s `setScenarioBusyUi` setting `.disabled =
// true` on the scenario buttons the instant the first click's handler runs (synchronously, before
// its own `await`), and a REAL browser never delivers a `click` event to a disabled form control
// at all -- that suppression is the whole protection against a second paid request going out
// on a second click while the first is still in flight (`gpu_busy`, `web.py`, only guards the
// local model -- an external provider has no server-side twin of this guard, see the fix report).
//
// So this script does two things `_scenario_race_check.mjs` does not:
//  1) `document.querySelectorAll` recognises the *exact* compound selector `setScenarioBusyUi`
//     (`app.js`) queries with, and returns hand-built button objects instead of `[]` -- the same
//     objects this script itself dispatches clicks at, so `.disabled = true` written by the
//     app's own code is the same property this script reads back afterwards.
//  2) its own `dispatchClick` helper checks `.disabled` before invoking any listener at all --
//     the one piece of real browser behaviour `makeStub` does not model -- so a click on an
//     already-disabled button is silently swallowed here exactly as it would be in a browser,
//     never reaching `app.js`'s delegated `document` click listener.
//
// `fetch` wraps the real global (Node 18+) against a real, already-running `h3_48gb.web` server
// the caller started -- the only way to prove from outside that the second click never sent its
// own `POST .../scenario/generate` at all, not merely that it "would have been safe".

const [, , appUrl, baseUrl, pid] = process.argv;

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

if (!appUrl || !baseUrl || !pid) {
  fail("usage: node _scenario_busy_check.mjs <appUrl> <baseUrl> <pid>");
}

// -- the exact compound selector `setScenarioBusyUi` (app.js) queries with -----------------------
// Copied verbatim (not reconstructed) so a typo in either place makes this test fail loudly
// rather than silently matching nothing.
const _SCENARIO_BUSY_SELECTOR = '#project-body [data-act="scenario-generate"], '
  + '#project-body [data-act="scenario-procedural"], '
  + '#project-body [data-act="scenario-save"], '
  + '#project-body [data-act="scenario-test-provider"], '
  + '#project-body [data-act="approve-scenario"], '
  + '#project-body #scenario-provider';

// -- a generic, self-mocking DOM node (identical to `_scenario_race_check.mjs`'s own) ------------
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

// -- the scenario-stage buttons `setScenarioBusyUi` is expected to disable -----------------------
// Real, plain objects (not proxies) -- this script both reads and writes `.disabled` on them
// directly, and dispatches clicks at the same objects, so there is exactly one source of truth
// for "is this button currently clickable" on both sides of the test.
function makeScenarioButton(act) {
  return {
    dataset: { act, id: pid },
    disabled: false,
    closest(sel) { return sel === "button[data-act]" ? this : null; },
  };
}
const scenarioButtons = {
  "scenario-generate": makeScenarioButton("scenario-generate"),
  "scenario-procedural": makeScenarioButton("scenario-procedural"),
  "scenario-save": makeScenarioButton("scenario-save"),
  "scenario-test-provider": makeScenarioButton("scenario-test-provider"),
  "approve-scenario": makeScenarioButton("approve-scenario"),
};
const scenarioButtonList = Object.values(scenarioButtons);

const listeners = {};
const fakeDocument = {
  body: makeStub("body"),
  documentElement: makeStub("html"),
  getElementById,
  querySelectorAll(sel) {
    if (sel === _SCENARIO_BUSY_SELECTOR) return scenarioButtonList;
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

// -- fetch: the real thing, against the real server, with a call log -----------------------------
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

// The one piece of real-browser behaviour `makeStub` does not model: a disabled form control
// never receives a `click` event at all (see this file's own module docstring).
function dispatchClick(target) {
  if (target.disabled) return false;
  (listeners.click || []).forEach((fn) => fn({ target }));
  return true;
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
  dispatchClick(openBtn);
  await waitFor(
    () => callLog.some((c) => c.method === "GET" && c.url === `/api/projects/${pid}` && c.done),
    8000, "GET /api/projects/<id> from openProjectModal");
  await sleep(30);

  // Two clicks on "Сюжет без LLM", back to back, no `await` between them -- the exact gesture a
  // fast real double-click produces (`scenario-procedural`: no `window.confirm` gate to interfere,
  // since the project's `scenario_scenes` is still empty at this point in the fixture).
  const button = scenarioButtons["scenario-procedural"];
  const firstDelivered = dispatchClick(button);
  const disabledRightAfterFirstClick = button.disabled;
  const secondDelivered = dispatchClick(button);

  await waitFor(
    () => callLog.some((c) => c.method === "POST"
      && c.url === `/api/projects/${pid}/scenario/generate` && c.done),
    15000, "POST /api/projects/<id>/scenario/generate");

  const generateCalls = callLog.filter(
    (c) => c.method === "POST" && c.url === `/api/projects/${pid}/scenario/generate`);

  process.stdout.write(JSON.stringify({
    firstDelivered, secondDelivered, disabledRightAfterFirstClick,
    generateCallCount: generateCalls.length,
  }));
  process.exit(0);
}

main().catch((err) => fail(`${err && err.stack || err}`));
