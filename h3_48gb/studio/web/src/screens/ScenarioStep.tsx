import { Plus, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/api/client";
import type { LintMap, Proposal, Scene, SceneIn, ScenesResult, Snapshot } from "@/api/types";
import { Field } from "@/components/Field";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useIsPhone, useMobileAction } from "@/mobileAction";
import { MODE_LABEL, REMOVED_NOTICE, REPLACED_NOTICE, SceneCard } from "./SceneCard";
import { toBody, type Draft, FIELDS } from "./sceneDraft";


/** The frame source as O7 takes it. A source derived from the order (`auto`) goes as
 *  `{kind: "prev_last_frame"}` WITHOUT a scene_id — the server derives it again, and a named
 *  scene would turn the derivation into the owner's explicit choice; `auto` itself is forbidden on
 *  input (400). An explicit source goes with everything it has. */
function sourceOut(s: Scene): SceneIn["source_frame"] {
  const f = s.source_frame;
  if (f.auto === true) return { kind: "prev_last_frame" };
  return { kind: f.kind, scene_id: f.scene_id, asset_id: f.asset_id };
}

/** The O7 copy of a saved scene — every field exactly as the server stored it, the frame source
 *  with its `scene_id` included. An operation on the set (add, delete, apply) changes nothing in
 *  a scene it is not about: dropping the explicit source here once re-chained S3 from S1 to S2 on
 *  «+ Сцена» with no 409 (review r8). Re-linking is the delete's own, explicit step below. */
export function toSceneIn(s: Scene): SceneIn {
  return {
    scene_id: s.id,
    expected_edit_version: s.edit_version,
    mode: s.mode,
    action_text: s.action_text,
    performance_text: s.performance_text,
    sound_text: s.sound_text,
    music_text: s.music_text,
    state_in_text: s.state_in_text,
    state_out_text: s.state_out_text,
    requested_s: s.requested_s,
    seed: s.seed,
    source_frame: sourceOut(s),
    refs: s.refs,
  };
}

/** «Удалить сцену»: the others as saved, except a scene whose frame came from the deleted one —
 *  it now continues from the scene before the deleted one (the first scene: no source, L12). */
export function afterDelete(scenes: Scene[], removed: Scene): SceneIn[] {
  const at = scenes.findIndex((x) => x.id === removed.id);
  const before = at > 0 ? scenes[at - 1].id : null;
  return scenes.filter((x) => x.id !== removed.id).map((x) => {
    const copy = toSceneIn(x);
    if (x.source_frame.auto === true) return copy;                    // derived: the server re-derives it
    if (x.source_frame.kind !== "prev_last_frame" || x.source_frame.scene_id !== removed.id) return copy;
    return { ...copy, source_frame: { kind: "prev_last_frame", scene_id: before, asset_id: null } };
  });
}

/** A scene this tab removes, at the edit_version it read: the server refuses a deletion over an
 *  edit another tab saved meanwhile (spec §7 O7/O26, review r7). */
export const gone = (s: Scene) => ({ scene_id: s.id, expected_edit_version: s.edit_version });

/** The number of the scene this one continues from — by the named scene, not by position. */
function sourceNo(scenes: Scene[], s: Scene): number | null {
  const at = s.source_frame.scene_id ? scenes.findIndex((x) => x.id === s.source_frame.scene_id) : -1;
  return at >= 0 ? at + 1 : null;
}

const seconds = (s: number) => s.toFixed(3).replace(".", ",");

export function ScenarioStep({ snap, onChanged }: { snap: Snapshot; onChanged: () => void }) {
  const pid = snap.project.id;
  const [idea, setIdea] = useState(snap.project.idea_text);
  const [count, setCount] = useState("2");
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [lint, setLint] = useState<LintMap>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const castTags = snap.cast.map((c) => c.tag);

  // A late answer of an older lint request must not replace a newer one: cleanup retires it.
  useEffect(() => {
    let current = true;
    api<{ findings: LintMap }>("POST", `/projects/${pid}/lint`, {}).then(
      (r) => { if (current) setLint(r.findings); },
      () => { if (current) setLint({}); },
    );
    return () => { current = false; };
  }, [pid, snap.last_seq]);

  // A scene deleted in another window while this card holds unsaved text stays as a card — the
  // owner decides what to do with the draft. Found during render (before the card unmounts).
  const dirtyIds = useRef(new Set<string>());
  const ownDeleted = useRef(new Set<string>());
  const ownReplaced = useRef(new Set<string>());   // replaced by our own «Применить»: a dirty draft stays, with its own notice
  const onDirty = (id: string, dirty: boolean) => { if (dirty) dirtyIds.current.add(id); else dirtyIds.current.delete(id); };
  const [seen, setSeen] = useState(snap.scenes);
  const [orphans, setOrphans] = useState<Scene[]>([]);
  if (seen !== snap.scenes) {
    setSeen(snap.scenes);
    const alive = new Set(snap.scenes.map((x) => x.id));
    for (const id of ownDeleted.current) if (alive.has(id)) ownDeleted.current.delete(id);   // the deletion did not happen
    const lost = seen.filter((x) => !alive.has(x.id) && dirtyIds.current.has(x.id) && !ownDeleted.current.has(x.id) && !orphans.some((o) => o.id === x.id));
    if (lost.length > 0 || orphans.some((o) => alive.has(o.id))) {
      setOrphans((all) => [...all.filter((o) => !alive.has(o.id)), ...lost]);
    }
  }
  const dismiss = (id: string) => {
    dirtyIds.current.delete(id);
    setOrphans((all) => all.filter((o) => o.id !== id));
  };

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      if (err instanceof ApiError && err.code === "revision_conflict") onChanged();
      setError(err instanceof ApiError ? err.message : "Сервер не ответил");
    } finally {
      setBusy(false);
    }
  }

  const propose = () => run(async () => {
    setProposal(await api<Proposal>("POST", `/projects/${pid}/director/propose`, { idea_text: idea, scenes_count: Number(count) }));
  });
  const apply = () => run(async () => {
    const ids = snap.scenes.map((x) => x.id);
    ids.forEach((id) => ownReplaced.current.add(id));
    try {
      await api<ScenesResult>("POST", `/projects/${pid}/director/apply`, {
        proposal_id: proposal!.proposal_id, expected_revision: snap.project.revision, deleted: snap.scenes.map(gone),
      });
    } catch (err) {
      ids.forEach((id) => ownReplaced.current.delete(id));   // nothing was replaced
      throw err;
    }
    setProposal(null);
    onChanged();
  });
  const replace = (scenes: SceneIn[], deleted: Scene[] = [], after?: () => void) => run(async () => {
    await api<ScenesResult>("PUT", `/projects/${pid}/scenes`, {
      expected_revision: snap.project.revision, scenes, deleted: deleted.map(gone),
    });
    after?.();
    onChanged();
  });
  /** «Вернуть как новую сцену»: the draft of a scene deleted elsewhere goes to the end, its frame derived from the order. */
  const restore = (orphan: Scene, draft: Draft) => {
    const { expected_edit_version: _v, ...fields } = toBody(draft, [...FIELDS], 0);
    void _v;
    return replace([
      ...snap.scenes.map(toSceneIn),
      { ...fields, source_frame: { kind: snap.scenes.length ? "prev_last_frame" : "none" } } as SceneIn,
    ], [], () => dismiss(orphan.id));
  };
  const remove = (s: Scene) => {
    if (dirtyIds.current.has(s.id) && !window.confirm(`В сцене ${s.idx + 1} есть несохранённые правки. Удалить сцену?`)) return;
    ownDeleted.current.add(s.id);
    return replace(afterDelete(snap.scenes, s), [s]);
  };
  const addScene = () => replace([
    ...snap.scenes.map(toSceneIn),
    { mode: "from_frame", requested_s: 8, source_frame: { kind: snap.scenes.length ? "prev_last_frame" : "none" }, refs: [] },
  ]);

  const phone = useIsPhone();
  useMobileAction("scenario", proposal
    ? { label: "Применить", onClick: apply, disabled: busy, priority: 1 }
    : { label: "Предложить сцены", onClick: propose, disabled: busy || !idea.trim(), priority: 0 });

  return (
    <section aria-labelledby="scenario-h" className="mt-10">
      <div className="eyebrow">Шаг 3</div>
      <h2 id="scenario-h" className="h-display mt-1 text-[24px]">Сценарий</h2>
      <div className="mt-4 grid gap-4 rounded-lg border border-line p-5">
        <Field text="Идея">
          <Textarea value={idea} onChange={(e) => setIdea(e.target.value)} />
        </Field>
        <div className="flex flex-wrap items-end gap-3">
          <Field text="Сцен">
            <Input type="number" min={1} value={count} onChange={(e) => setCount(e.target.value)} className="w-24" />
          </Field>
          {!phone && (
            <Button onClick={propose} disabled={busy || !idea.trim()} size="lg">
              <Sparkles />
              Предложить сцены
            </Button>
          )}
        </div>
        {error && <p role="alert" className="text-[13px] text-danger">{error}</p>}
      </div>

      {proposal && (
        <div aria-label="Предложение сценариста" role="region" className="mt-4 rounded-lg border border-cobalt-line bg-cobalt-soft p-5">
          <ol className="grid gap-3">
            {proposal.scenes.map((s, i) => (
              <li key={i} className="rounded-md bg-bg p-3 text-[13px]" aria-label={`Предложенная сцена ${i + 1}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-[11px] text-ink3">Сцена {i + 1}</span>
                  <Badge>{MODE_LABEL[s.mode]}</Badge>
                  <Badge variant="outline">{`${proposal.durations[i].frames} кадров · ${seconds(proposal.durations[i].duration_s)} с`}</Badge>
                </div>
                <p className="mt-1">{s.action_text}</p>
                <p className="mt-1 text-ink2">{s.performance_text}</p>
                <p className="mt-1 text-ink3">{s.sound_text}</p>
              </li>
            ))}
          </ol>
          {proposal.dropped_tags.length > 0 && (
            <p className="mt-3 text-[13px] text-ink2">Сценарист назвал героев вне кастинга: {proposal.dropped_tags.map((t) => `@${t}`).join(", ")} — они не попали в сцены.</p>
          )}
          {snap.scenes.length > 0 && <p className="mt-3 text-[13px] text-warn">Текущие сцены будут заменены.</p>}
          {!phone && <Button size="lg" onClick={apply} disabled={busy} className="mt-3">Применить</Button>}
        </div>
      )}

      <div className="mt-6 grid gap-4">
        {[...snap.scenes, ...orphans].map((s) => (
          <SceneCard
            key={s.id}
            scene={s}
            castTags={castTags}
            findings={lint[s.id] ?? []}
            sourceNo={sourceNo(snap.scenes, s)}
            deleteDisabled={busy}
            removed={orphans.some((o) => o.id === s.id)}
            removedNotice={ownReplaced.current.has(s.id) ? REPLACED_NOTICE : REMOVED_NOTICE}
            onDirty={onDirty}
            onRestore={(draft) => restore(s, draft)}
            onDismiss={() => dismiss(s.id)}
            onSaved={onChanged}
            onConflict={onChanged}
            onDelete={() => remove(s)}
          />
        ))}
        <Button variant="outline" onClick={addScene} disabled={busy} className="justify-self-start">
          <Plus />
          Сцена
        </Button>
      </div>
    </section>
  );
}
