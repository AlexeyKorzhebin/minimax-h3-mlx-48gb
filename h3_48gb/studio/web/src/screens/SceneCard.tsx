import { Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { Finding, Scene, SceneResult } from "@/api/types";
import { Field } from "@/components/Field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useIsPhone, useMobileAction } from "@/mobileAction";
import {
  conflictPairs, init, input, isDirty, isStale, received, save, saveMine, sceneSeen, takeServer,
  type Field as DraftField,
} from "./sceneDraft";

const TEXTS = [
  ["action_text", "Действие", "что происходит в кадре"],
  ["performance_text", "Эмоция и игра", "обязательно"],
  ["sound_text", "Звук действий", "материал и контакт"],
  ["music_text", "Музыка", "необязательно"],
] as const;

// «Текст» is not offered: no ComfyUI template of part 1 renders it (spec §8.2).
const MODES = [["from_frame", "От кадра"], ["refs", "Референсы"]] as const;
const MODE_LABEL = { from_frame: "От кадра", refs: "Референсы", text: "Текст" } as const;
const VARIANT = { error: "destructive", warning: "warn", info: "outline" } as const;
export const CONFLICT_MESSAGE = "Сцену изменили в другом окне. Сравните и выберите, что оставить.";
export const STALE_NOTICE = "Сцену изменили в другом окне — при сохранении покажу, что различается.";

const LABEL: Record<DraftField, string> = {
  action_text: "Действие", performance_text: "Эмоция и игра", sound_text: "Звук действий",
  music_text: "Музыка", requested_s: "Длительность, с", seed: "Сид", mode: "Режим", refs: "Кто в кадре",
};

export function FindingList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) return null;
  return (
    <ul className="mt-1.5 grid gap-1">
      {findings.map((f, i) => (
        <li key={i} className="flex items-start gap-2 text-[12px] text-ink2">
          <Badge variant={VARIANT[f.severity]}>{f.rule}</Badge>
          <span>{f.message}</span>
        </li>
      ))}
    </ul>
  );
}

export function SceneCard({ scene, castTags, findings, onSaved, onConflict, onDelete }: {
  scene: Scene;
  castTags: string[];
  findings: Finding[];
  onSaved: (r: SceneResult) => void;
  onConflict: () => void;
  onDelete: () => void;
}) {
  // All sync logic lives in sceneDraft.ts (pure, model-tested); the card renders the state and
  // runs the one request the state names.
  const [state, setState] = useState(() => init(scene));
  useEffect(() => setState((s) => sceneSeen(s, scene)), [scene]);

  const callbacks = useRef({ onSaved, onConflict });
  callbacks.current = { onSaved, onConflict };
  const sent = useRef(new Set<number>());
  const request = state.inflight;
  useEffect(() => {
    if (!request || sent.current.has(request.id)) return;
    sent.current.add(request.id);
    api<SceneResult>("PATCH", `/scenes/${scene.id}`, request.body).then(
      (result) => {
        setState((s) => received(s, request, { kind: "ok", scene: result.scene }));
        callbacks.current.onSaved(result);
      },
      (err) => {
        if (err instanceof ApiError && err.code === "revision_conflict") {
          setState((s) => received(s, request, { kind: "conflict", current: err.detail.current as Scene }));
          callbacks.current.onConflict();
        } else {
          setState((s) => received(s, request, { kind: "error", message: err instanceof ApiError ? err.message : "Сервер не ответил" }));
        }
      },
    );
  }, [request, scene.id]);

  const edit = (field: DraftField, value: string) => setState((s) => input(s, field, value));
  const { draft, conflict, error } = state;
  const dirty = isDirty(state);
  const refs = draft.refs ? draft.refs.split(",") : [];

  const phone = useIsPhone();
  useMobileAction(`scene-${scene.id}`, conflict
    ? { label: "Сохранить моё", onClick: () => setState(saveMine), priority: 4, touched: state.inputs,
        secondary: { label: "Взять с сервера", onClick: () => setState(takeServer) } }
    : dirty
      ? { label: `Сохранить сцену ${scene.idx + 1}`, onClick: () => setState(save), priority: 3, touched: state.inputs }
      : null);

  const byField = (name: string) => findings.filter((f) => f.field === name);
  return (
    <article aria-label={`Сцена ${scene.idx + 1}`} className="rounded-lg border border-line p-5">
      <header className="flex flex-wrap items-center gap-2">
        <h3 className="h-display text-[20px]">Сцена {scene.idx + 1}</h3>
        <Badge variant="outline">{scene.frames} кадров · {scene.duration_s.toFixed(3).replace(".", ",")} с</Badge>
        {scene.source_frame.kind === "prev_last_frame" && scene.idx > 0 && <Badge variant="outline">от кадра сцены {scene.idx}</Badge>}
        <Button variant="ghost" size="icon" aria-label={`Удалить сцену ${scene.idx + 1}`} className="ml-auto" onClick={onDelete}>
          <Trash2 />
        </Button>
      </header>
      <div className="mt-4 grid gap-4">
        <div className="flex flex-wrap items-center gap-3">
          <div role="group" aria-label="Режим" className="inline-flex rounded-full border border-line2 bg-surface p-0.5">
            {MODES.map(([mode, label]) => (
              <button
                key={mode}
                type="button"
                aria-pressed={draft.mode === mode}
                onClick={() => edit("mode", mode)}
                className="h-[30px] cursor-pointer rounded-full px-3.5 text-[13px] font-medium text-ink2 aria-pressed:bg-bg aria-pressed:text-ink aria-pressed:shadow-[0_2px_10px_-3px_rgba(23,71,230,.28),0_0_0_1px_var(--line)]"
              >
                {label}
              </button>
            ))}
          </div>
          {draft.mode === "text" && <Badge variant="warn">{MODE_LABEL.text}: нет шаблона в части 1</Badge>}
        </div>
        {castTags.length > 0 && (
          <div role="group" aria-label="Кто в кадре" className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold">Кто в кадре</span>
            {castTags.map((tag) => (
              <button
                key={tag}
                type="button"
                aria-pressed={refs.includes(tag)}
                onClick={() => edit("refs", (refs.includes(tag) ? refs.filter((t) => t !== tag) : [...refs, tag]).join(","))}
                className="h-[22px] cursor-pointer rounded-full border border-line px-2 font-mono text-[11px] text-ink2 aria-pressed:border-cobalt-line aria-pressed:bg-cobalt-soft aria-pressed:text-cobalt"
              >
                @{tag}
              </button>
            ))}
          </div>
        )}
        {TEXTS.map(([f, label, hint]) => (
          <div key={f}>
            <Field text={label} hint={hint}>
              <Textarea value={draft[f]} onChange={(e) => edit(f, e.target.value)} />
            </Field>
            <FindingList findings={byField(f)} />
          </div>
        ))}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <Field text="Длительность, с">
              <Input type="number" min={3} max={15} step={0.5} value={draft.requested_s} onChange={(e) => edit("requested_s", e.target.value)} />
            </Field>
            <FindingList findings={byField("requested_s")} />
          </div>
          <Field text="Сид">
            <Input type="number" min={1} value={draft.seed} onChange={(e) => edit("seed", e.target.value)} />
          </Field>
        </div>
        <FindingList findings={findings.filter((f) => f.field === "source_frame" || f.field === null)} />
        {isStale(state) && <p className="text-[13px] text-warn">{STALE_NOTICE}</p>}
        {conflict && (
          <div role="group" aria-label="Конфликт правок" className="grid gap-2 rounded-md border border-warn p-3 text-[13px]">
            <p role="alert" className="font-medium">{CONFLICT_MESSAGE}</p>
            <dl className="grid gap-1">
              {conflictPairs(state).map((pair) => (
                <div key={pair.field} data-testid={`conflict-${pair.field}`}>
                  <dt className="text-xs font-semibold">{LABEL[pair.field]}</dt>
                  <dd>{`Сервер: ${pair.server} · Моё: ${pair.mine}`}</dd>
                </div>
              ))}
            </dl>
            {!phone && (
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => setState(saveMine)}>Сохранить моё</Button>
                <Button variant="outline" onClick={() => setState(takeServer)}>Взять с сервера</Button>
              </div>
            )}
          </div>
        )}
        {error && <p role="alert" className="text-[13px] text-danger">{error}</p>}
        {!phone && (
          <Button disabled={!dirty || conflict} onClick={() => setState(save)} className="justify-self-start">
            Сохранить сцену
          </Button>
        )}
      </div>
    </article>
  );
}
