// I2 (fix round 2, 2026-08-19 review): drives the REAL `h3_48gb/webui/app.js` -- not a
// reimplementation -- through the exact user gesture the live gate exposed: a scenario field's
// `focusout` (which fires `saveScenario` -> `PUT .../scenario`) immediately followed by a click on
// "Утвердить сюжет" (`approve-scenario` -> `POST .../approve/scenario`), the same order a browser
// delivers them in when a user clicks the button while a scenario field still has focus (`focusout`
// is part of the browser's own focus-change handling, which runs before the `click` event a
// moment later -- see `app.js`'s own comment above the delegated `focusout` listener).
//
// `app.js` guards its whole DOM half behind `typeof document !== "undefined"`
// (`tests/test_web.py`'s own `_node_eval` relies on this to import the file's pure functions
// outside a browser without wiring anything up) -- this script does the opposite on purpose: it
// defines a minimal `document`/`window`/`localStorage`/`fetch` *before* importing the module, so
// `startPage()` actually runs, with its real closures (`saveScenario`, `pendingScenarioSave`,
// `withProject`, the delegated `click`/`focusout` listeners) wired up exactly as a browser would.
//
// No DOM rendering is attempted -- `document.getElementById` returns a generic auto-stub (any
// property/method access is safe and a no-op) so `startPage()`'s own element wiring never throws,
// and the two DOM nodes this test's own synthetic events need (the edited scenario textarea, the
// "Утвердить сюжет" button) are hand-built plain objects with just enough real behaviour
// (`dataset`, `classList.contains`, `.closest(selector)`) for the delegated listeners' own
// selector checks to recognise them -- exactly the same contract a real `<textarea class=
// "scenario-prompt">`/`<button data-act="approve-scenario">` would satisfy.
//
// `fetch` is the one thing this script does not stub: it wraps the real global `fetch` (Node 18+)
// against a real `h3_48gb.web` server the caller already started, and logs every call's start/
// finish time -- the only way to prove, from outside, that the fixed code waits for one request
// to finish before sending the next, rather than firing both at once.

const [, , appUrl, baseUrl, pid, editedPromptB64] = process.argv;
const editedPrompt = Buffer.from(editedPromptB64, "base64").toString("utf-8");

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

if (!appUrl || !baseUrl || !pid || !editedPromptB64) {
  fail("usage: node _scenario_race_check.mjs <appUrl> <baseUrl> <pid> <editedPromptB64>");
}

// -- a generic, self-mocking DOM node -------------------------------------------------------------
//
// Any property read that is not one of the few explicitly modelled ones (`dataset`/`style`/
// `classList`/`value`/...) returns a *callable* stub, so both `el.foo()` and `el.foo.bar()` stay
// safe -- `startPage()`'s own element wiring touches dozens of ids this test does not care about
// (`$("submit").addEventListener(...)`, `$("prompt-file").value`, ...) and none of it may throw.
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
      // fallback: a callable that itself returns a fresh stub, so chained calls never throw
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

// Fields this test controls directly, standing in for "what the user currently sees typed into
// the rendered form" -- `collectScenarioScenes` (`app.js`) reads exactly these two selectors out
// of the real DOM under `#project-body`; there is no real render here, so this test supplies the
// query result by hand instead of a rendered tree, the same value a real render would have shown.
let promptFields = [];
let durationFields = [];

const listeners = {};
const fakeDocument = {
  body: makeStub("body"),
  documentElement: makeStub("html"),
  getElementById,
  querySelectorAll(sel) {
    if (sel === "#project-body .scenario-prompt") return promptFields;
    if (sel === "#project-body .scenario-duration") return durationFields;
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

// -- fetch: the real thing, against the real server, with a timestamped call log ------------------
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
  await import(appUrl);  // runs `startPage()` as a side effect -- see module docstring above

  // 1) let the page's own startup poll (`poll().then(...)`, the last line of `startPage()`) settle.
  await waitFor(() => callLog.some((c) => c.method === "GET" && c.url === "/api/state" && c.done),
                8000, "initial GET /api/state");
  await sleep(30);

  // 2) "open project" -- the same click a project-list row's own button sends.
  const openBtn = {
    dataset: { act: "open-project", id: pid },
    closest(sel) { return sel === "button[data-act]" ? this : null; },
  };
  (listeners.click || []).forEach((fn) => fn({ target: openBtn }));
  await waitFor(
    () => callLog.some((c) => c.method === "GET" && c.url === `/api/projects/${pid}` && c.done),
    8000, "GET /api/projects/<id> from openProjectModal");
  await sleep(30);

  // 3) "type" the edit into scene 0's prompt field -- the form now shows the edited text, nothing
  // is sent yet (that is `saveScenario`'s own job, triggered by `focusout` next).
  promptFields = [{ dataset: { idx: "0" }, value: editedPrompt }];
  durationFields = [];

  // 4) `focusout` on that field, immediately followed by a click on "Утвердить сюжет" -- no
  // `await` between them, matching a real browser's own event order for "click the button while
  // the field still has focus" exactly (`focusout` is part of the click's own focus-change
  // handling, dispatched before the `click` event itself).
  const promptField = {
    dataset: { idx: "0" },
    value: editedPrompt,
    classList: { contains: (c) => c === "scenario-prompt" },
    closest(sel) { return sel.includes("scenario-prompt") ? this : null; },
  };
  (listeners.focusout || []).forEach((fn) => fn({ target: promptField }));

  const approveBtn = {
    dataset: { act: "approve-scenario", id: pid },
    closest(sel) { return sel === "button[data-act]" ? this : null; },
  };
  (listeners.click || []).forEach((fn) => fn({ target: approveBtn }));

  // 5) wait for both requests the two events above must eventually send.
  await waitFor(
    () => callLog.some((c) => c.method === "PUT" && c.url === `/api/projects/${pid}/scenario`
      && c.done),
    15000, "PUT /api/projects/<id>/scenario");
  await waitFor(
    () => callLog.some((c) => c.method === "POST"
      && c.url === `/api/projects/${pid}/approve/scenario` && c.done),
    15000, "POST /api/projects/<id>/approve/scenario");

  const put = callLog.find((c) => c.method === "PUT" && c.url === `/api/projects/${pid}/scenario`);
  const post = callLog.find(
    (c) => c.method === "POST" && c.url === `/api/projects/${pid}/approve/scenario`);

  // 6) the real, final server state -- straight `fetch`, bypassing `app.js`'s own cache entirely,
  // so this reads exactly what a second browser tab would see.
  const finalRes = await realFetch(`${baseUrl}/api/projects/${pid}`);
  const finalJson = await finalRes.json();

  process.stdout.write(JSON.stringify({
    putStartedAt: put.startedAt, putFinishedAt: put.finishedAt, putStatus: put.status,
    postStartedAt: post.startedAt, postFinishedAt: post.finishedAt, postStatus: post.status,
    finalProject: finalJson.project,
  }));
  // `app.js`'s own `setInterval(poll, POLL_MS)`/`setInterval(renderConnection, 1000)` (its last
  // two lines) keep a real page's tab alive forever -- and keep node's own event loop alive with
  // it, past every `await` above resolving, unless told otherwise.
  process.exit(0);
}

main().catch((err) => fail(`${err && err.stack || err}`));
