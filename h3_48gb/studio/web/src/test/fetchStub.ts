import { vi } from "vitest";

/** Replies keyed by the exact "METHOD URL" string. Anything else fails the test: a stub that
 *  matched loosely (startsWith/includes) has let mutations survive in this repo before.
 *  A route is one reply, a queue of replies (the last one repeats), or a handler that computes
 *  the reply from the request body — a tiny server with state, for tests with two clients; a
 *  handler may return a Promise to hold the answer while the test types on. */
export type Reply = { status?: number; body: unknown };
export type Handler = (body: unknown) => Reply | Promise<Reply>;
export type Call = { key: string; body: unknown };

export function stubFetch(routes: Record<string, Reply | Reply[] | Handler>) {
  const calls: Call[] = [];
  const queues = new Map(Object.entries(routes).map(([k, v]) => [k, Array.isArray(v) ? [...v] : [v]]));
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${url}`;
    const raw = init?.body;
    const body = typeof raw === "string" ? JSON.parse(raw) : raw ?? null;
    calls.push({ key, body });
    const queue = queues.get(key);
    if (!queue || queue.length === 0) throw new Error(`unexpected fetch: ${key}`);
    const next = queue.length > 1 ? queue.shift()! : queue[0];
    const reply = await (typeof next === "function" ? next(body) : next);
    return new Response(JSON.stringify(reply.body), {
      status: reply.status ?? 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  return calls;
}

/** EventSource that records its URL and lets the test push events by name. */
export class FakeEventSource {
  static last: FakeEventSource | null = null;
  url: string;
  listeners = new Map<string, ((e: MessageEvent) => void)[]>();
  closed = false;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.last = this;
  }
  addEventListener(kind: string, fn: (e: MessageEvent) => void) {
    this.listeners.set(kind, [...(this.listeners.get(kind) ?? []), fn]);
  }
  emit(kind: string, data: unknown, id: number) {
    for (const fn of this.listeners.get(kind) ?? []) {
      fn(new MessageEvent(kind, { data: JSON.stringify(data), lastEventId: String(id) }));
    }
  }
  close() {
    this.closed = true;
  }
}
