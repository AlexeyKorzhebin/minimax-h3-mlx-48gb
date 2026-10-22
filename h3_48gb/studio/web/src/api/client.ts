import type { ApiErrorBody } from "./types";

export const API = "/api/studio";

export class ApiError extends Error {
  status: number;
  code: string;
  detail: Record<string, unknown>;
  constructor(status: number, code: string, message: string, detail: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function parse<T>(res: Response): Promise<T> {
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (res.ok) return body as T;
  const err = (body as ApiErrorBody | null)?.error;
  if (err) throw new ApiError(res.status, err.code, err.message, err.detail ?? {});
  throw new ApiError(res.status, "http_error", `Сервер ответил ${res.status}`, {});
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  return parse<T>(await fetch(`${API}${path}`, init));
}

export async function upload<T>(path: string, file: File, fields: Record<string, string> = {}) {
  const form = new FormData();
  form.append("file", file);
  for (const [k, v] of Object.entries(fields)) form.append(k, v);
  return parse<T>(await fetch(`${API}${path}`, { method: "POST", body: form }));
}
