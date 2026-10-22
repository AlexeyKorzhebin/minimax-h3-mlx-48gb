import { Film, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { Project, ProjectCreate, ProjectListItem } from "@/api/types";
import { Field } from "@/components/Field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export const STEPS = ["Идея", "Герой", "Сценарий", "Эскизы", "Финал", "Сборка"];

export function plural(n: number) {
  const d = n % 10, h = n % 100;
  if (d === 1 && h !== 11) return "сцена";
  if (d >= 2 && d <= 4 && (h < 12 || h > 14)) return "сцены";
  return "сцен";
}

function ProjectCard({ p, n }: { p: ProjectListItem; n: number }) {
  return (
    <a href={`#/p/${p.id}`} className="block overflow-hidden rounded-lg border border-line bg-bg text-ink no-underline transition-colors hover:border-ink">
      <div className="relative aspect-video bg-surface2">
        <span className="absolute left-3 top-3 rounded-full bg-black/60 px-2 py-0.5 font-mono text-[11px] text-white">
          № {String(n).padStart(2, "0")}
        </span>
        <span className="absolute bottom-3 left-3 flex items-center gap-1.5 font-mono text-[12px] text-ink2">
          <Film className="h-3.5 w-3.5" />
          <span data-testid="card-meta">
            {`${p.scenes_total === 0 ? "сцен пока нет" : `${p.scenes_total} ${plural(p.scenes_total)}`} · ${p.frame_format}`}
          </span>
        </span>
      </div>
      <div className="p-5">
        <h2 className="h-display text-[24px] leading-tight">{p.title}</h2>
        <div className="route mt-5">
          {STEPS.map((s, k) => <i key={s} className={k === 0 ? "cur" : ""} />)}
        </div>
      </div>
    </a>
  );
}

function NewProjectForm({ onCreated }: { onCreated: (p: Project) => void }) {
  const [title, setTitle] = useState("");
  const [format, setFormat] = useState<"16:9" | "9:16">("16:9");
  const [idea, setIdea] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body: ProjectCreate = { title: title.trim(), frame_format: format, idea_text: idea };
      onCreated(await api<Project>("POST", "/projects", body));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Сервер не ответил");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="mt-6 grid gap-4 rounded-lg border border-line p-5" aria-label="Новый проект">
      <Field text="Название">
        <Input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={200} />
      </Field>
      <div>
        <span className="mb-1.5 block text-xs font-semibold">Формат кадра</span>
        <div role="group" aria-label="Формат кадра" className="inline-flex rounded-full border border-line2 bg-surface p-0.5">
          {(["16:9", "9:16"] as const).map((f) => (
            <button
              key={f}
              type="button"
              aria-pressed={format === f}
              onClick={() => setFormat(f)}
              className="h-[30px] cursor-pointer rounded-full px-3.5 text-[13px] font-medium text-ink2 aria-pressed:bg-bg aria-pressed:text-ink aria-pressed:shadow-[0_2px_10px_-3px_rgba(23,71,230,.28),0_0_0_1px_var(--line)]"
            >
              {f}
            </button>
          ))}
        </div>
      </div>
      <Field text="Идея" hint="можно позже">
        <Textarea value={idea} onChange={(e) => setIdea(e.target.value)} />
      </Field>
      {error && <p role="alert" className="text-[13px] text-danger">{error}</p>}
      <div className="fixed inset-x-0 bottom-0 z-20 border-t border-line bg-[var(--scrim)] p-3 backdrop-blur-md md:static md:border-0 md:bg-transparent md:p-0">
        <Button type="submit" size="lg" disabled={busy || !title.trim()} className="w-full md:w-auto">
          Создать проект
        </Button>
      </div>
    </form>
  );
}

export function ProjectsScreen() {
  const [items, setItems] = useState<ProjectListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    api<ProjectListItem[]>("GET", "/projects").then(setItems, (err) =>
      setError(err instanceof ApiError ? err.message : "Сервер не ответил"),
    );
  }, []);
  return (
    <main className="mx-auto max-w-[1360px] px-4 pb-28 pt-8 md:px-8 md:pb-16 md:pt-12">
      <div className="flex flex-col gap-6 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="eyebrow">Проекты · {items?.length ?? "…"}</div>
          <h1 className="h-display mt-2 text-[34px] leading-[1.05] md:text-[52px]">
            Что снимаем<span className="text-cobalt">.</span>
          </h1>
        </div>
        {!creating && (
          <Button size="lg" onClick={() => setCreating(true)}>
            <Plus />
            Новый проект
          </Button>
        )}
      </div>
      {creating && <NewProjectForm onCreated={(p) => (location.hash = `#/p/${p.id}`)} />}
      {error && <p role="alert" className="mt-6 text-danger">{error}</p>}
      {items && items.length === 0 && !creating && (
        <p className="mt-8 text-ink2">Проектов пока нет — начните с «Новый проект».</p>
      )}
      <div className="mt-8 grid gap-6 md:grid-cols-2 md:gap-8">
        {items?.map((p, i) => <ProjectCard key={p.id} p={p} n={i + 1} />)}
      </div>
    </main>
  );
}
