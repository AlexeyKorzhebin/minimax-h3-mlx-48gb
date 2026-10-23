import { api, ApiError } from "@/api/client";
import type { Task } from "@/api/types";
import { Button } from "@/components/ui/button";
import { useState } from "react";

export const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

/** What the owner reads about a task (spec §4.4 step 5): the wait with its reason, the step
 *  counter, the error — never a bare status code. */
export function taskText(t: Task): string {
  switch (t.status) {
    case "queued": return "В очереди";
    case "blocked": return "Ждёт кадр предыдущей сцены";
    case "waiting_gpu": return t.wait_reason ?? "Ждём карту";
    case "waiting_ready": return t.wait_reason ?? "Готовим ComfyUI";
    case "submitting": return "Отправляем в ComfyUI";
    case "submit_unknown": return t.wait_reason ?? "Не знаю, принял ли ComfyUI задачу";
    case "running":
      return t.progress_step !== null && t.progress_total !== null
        ? `Шаг ${t.progress_step} из ${t.progress_total}` : "Идёт";
    case "cancelling": return "Останавливаем";
    case "downloading":
    case "verifying": return "Проверяем результат";
    case "succeeded": return "Готово";
    case "failed": return `Ошибка: ${t.error ?? "без текста"}`;
    case "cancelled": return "Отменено";
  }
}

/** One task: its text, «Освободить» when Qwen holds the card (O23), «Отменить» (O14). */
export function TaskLine({ task, onChanged }: { task: Task; onChanged: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const run = async (method: string, path: string, body?: unknown) => {
    setError(null);
    try {
      await api(method, path, body);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Сервер не ответил");
    }
  };
  const qwen = task.status === "waiting_gpu" && (task.wait_reason ?? "").includes("Qwen");
  return (
    <div className="flex flex-wrap items-center gap-2 text-[13px]" aria-label="Задача">
      <span role="status" className={task.status === "failed" ? "text-danger" : "text-ink2"}>{taskText(task)}</span>
      {qwen && <Button variant="outline" onClick={() => run("POST", "/gpu/qwen/unload")}>Освободить</Button>}
      {!TERMINAL.has(task.status) && !task.cancel_requested && (
        <Button variant="ghost" onClick={() => run("POST", `/tasks/${task.id}/cancel`, { reason: "владелец" })}>Отменить</Button>
      )}
      {error && <span role="alert" className="text-danger">{error}</span>}
    </div>
  );
}
