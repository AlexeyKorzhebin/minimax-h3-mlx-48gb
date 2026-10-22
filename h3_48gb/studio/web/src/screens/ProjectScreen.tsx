import { ChevronLeft } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { Snapshot } from "@/api/types";
import { useEvents } from "@/useEvents";

const RELOAD_ON = ["project_updated"];

export function ProjectScreen({ id }: { id: string }) {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api<Snapshot>("GET", `/projects/${id}`).then(setSnap, (err) =>
      setError(err instanceof ApiError ? err.message : "Сервер не ответил"),
    );
  }, [id]);
  useEffect(load, [load]);
  useEvents(id, snap?.last_seq ?? null, RELOAD_ON, load);
  if (error) return <main className="mx-auto max-w-[1360px] px-4 pt-8 md:px-8"><p role="alert" className="text-danger">{error}</p></main>;
  if (!snap) return <main className="mx-auto max-w-[1360px] px-4 pt-8 md:px-8"><p className="text-ink3">Загрузка…</p></main>;
  const p = snap.project;
  return (
    <main className="mx-auto max-w-[1360px] px-4 pb-28 pt-6 md:px-8 md:pb-16 md:pt-8">
      <a href="#/" className="flex items-center gap-1 text-[13px] text-ink2 no-underline hover:text-ink">
        <ChevronLeft className="h-3.5 w-3.5" />
        Проекты
      </a>
      <div className="mt-4">
        <div className="eyebrow">Проект · {p.frame_format}</div>
        <h1 className="h-display mt-2 text-[30px] leading-[1.05] md:text-[44px]">{p.title}</h1>
        {p.idea_text && <p className="mt-3 max-w-[60ch] text-[15px] text-ink2">{p.idea_text}</p>}
      </div>
    </main>
  );
}
