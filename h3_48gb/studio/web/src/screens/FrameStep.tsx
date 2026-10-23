import { ImageIcon } from "lucide-react";
import { useState } from "react";
import { api, ApiError } from "@/api/client";
import type { Plan, SceneResult, Snapshot } from "@/api/types";
import { PlanSummary } from "@/components/PlanSummary";
import { TaskLine, TERMINAL } from "@/components/TaskLine";
import { Button } from "@/components/ui/button";
import { uuid4 } from "@/lib/uuid";
import { useIsPhone, useMobileAction } from "@/mobileAction";

/** Step 4 «Первый кадр» (spec §4.4): scene 1 in `from_frame` gets a Krea 2 keyframe —
 *  plan (O10) → «Подтвердить» (O12, a fresh Idempotency-Key per press) → progress (O13 via the
 *  snapshot and SSE) → «Взять в сцену 1» (O8 `source_frame{kind:"asset"}`). */
export function FrameStep({ snap, onChanged, newKey = uuid4 }: {
  snap: Snapshot;
  onChanged: () => void;
  newKey?: () => string;
}) {
  const pid = snap.project.id;
  const scene = snap.scenes[0];
  // The Idempotency-Key belongs to the plan, not to the press: a retry after a network failure
  // reuses it and gets the stored answer (200) instead of a second task. A plan built for a scene
  // that has changed since (revision) is dropped.
  const [built, setBuilt] = useState<{ plan: Plan; key: string; sceneId: string; revision: number } | null>(null);
  const plan = built && scene && built.sceneId === scene.id && built.revision === scene.revision ? built.plan : null;
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const phone = useIsPhone();
  const task = scene ? [...snap.tasks].reverse().find((t) => t.kind === "image") ?? null : null;
  const attached = scene?.source_frame.kind === "asset" ? scene.source_frame.asset_id : null;
  const result = task?.status === "succeeded" ? task.result_asset_id : null;
  const active = task !== null && !TERMINAL.has(task.status);
  const item = plan?.items[0] ?? null;
  const ready = item?.status === "ready";
  const canTake = result !== null && result !== attached;

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Сервер не ответил");
    } finally {
      setBusy(false);
    }
  }
  const makePlan = () => run(async () => {
    const made = await api<Plan>("POST", `/projects/${pid}/plan`, { kind: "image", scene_id: scene!.id });
    setBuilt({ plan: made, key: newKey(), sceneId: scene!.id, revision: scene!.revision });
  });
  const confirm = () => run(async () => {
    try {
      await api("POST", `/projects/${pid}/image`, { plan_id: plan!.plan_id }, { "Idempotency-Key": built!.key });
    } catch (err) {
      if (err instanceof ApiError && (err.code === "plan_used" || err.code === "plan_expired")) {
        setBuilt(null);      // the plan is spent: the owner builds a new one
        onChanged();
      }
      throw err;
    }
    setBuilt(null);
    onChanged();
  });
  const take = () => run(async () => {
    try {
      await api<SceneResult>("PATCH", `/scenes/${scene!.id}`, {
        expected_edit_version: scene!.edit_version, source_frame: { kind: "asset", asset_id: result },
      });
    } catch (err) {
      // 409: the scene moved under us — reload so the button retries with the new edit_version
      // (explicit retry by the owner, never automatic)
      if (err instanceof ApiError && err.status === 409) onChanged();
      throw err;
    }
    onChanged();
  });

  const needed = scene !== undefined && scene.mode === "from_frame";
  useMobileAction("frame", !needed ? null
    : plan ? { label: "Подтвердить", onClick: confirm, disabled: busy || !ready, priority: 1 }
    : canTake ? { label: "Взять в сцену 1", onClick: take, disabled: busy, priority: 1 }
    : null);

  if (!scene) return null;
  return (
    <section aria-labelledby="frame-h" className="mt-10">
      <div className="eyebrow">Шаг 4</div>
      <h2 id="frame-h" className="h-display mt-1 text-[24px]">Первый кадр</h2>
      <div className="mt-4 grid gap-4 rounded-lg border border-line p-5">
        {!needed ? (
          <p className="text-[13px] text-ink2">Сцена 1 снимается по фото героя (режим «Референсы») — кадр не нужен.</p>
        ) : (
          <>
            {attached && (
              <figure className="grid gap-1">
                <img src={`/api/studio/assets/${attached}/file`} alt="Кадр сцены 1" className="aspect-video w-full max-w-[640px] rounded-md border border-line object-cover" />
                <figcaption className="text-[12px] text-ink3">Кадр сцены 1</figcaption>
              </figure>
            )}
            {!plan && (
              <Button variant={attached || canTake ? "outline" : "default"} size="lg" onClick={makePlan} disabled={busy || active} className="justify-self-start">
                <ImageIcon />
                {attached || canTake ? "Сделать другой кадр" : "Сделать кадр"}
              </Button>
            )}
            {plan && (
              <div role="region" aria-label="План кадра" className="grid gap-2 rounded-md border border-cobalt-line bg-cobalt-soft p-4">
                <p className="text-[13px]">{item!.prompt}</p>
                <p className="font-mono text-[12px] text-ink3">{`${item!.width}×${item!.height} · Krea 2`}</p>
                <PlanSummary plan={plan} />
                {item && item.reasons.length > 0 && (
                  <ul role="alert" className="text-[13px] text-danger">
                    {item.reasons.map((r) => <li key={r}>{r}</li>)}
                  </ul>
                )}
                <div className="flex gap-2">
                  {!phone && <Button size="lg" onClick={confirm} disabled={busy || !ready}>Подтвердить</Button>}
                  <Button size="lg" variant="outline" onClick={() => setBuilt(null)} disabled={busy}>Отмена</Button>
                </div>
              </div>
            )}
            {task && !(task.status === "succeeded" && task.result_asset_id === attached) && (
              <TaskLine key={task.id} task={task} onChanged={onChanged} />
            )}
            {canTake && (
              <figure className="grid gap-2">
                <img src={task!.result_url!} alt="Новый кадр" className="aspect-video w-full max-w-[640px] rounded-md border border-line object-cover" />
                {!phone && <Button size="lg" onClick={take} disabled={busy} className="justify-self-start">Взять в сцену 1</Button>}
              </figure>
            )}
          </>
        )}
        {error && <p role="alert" className="text-[13px] text-danger">{error}</p>}
      </div>
    </section>
  );
}
