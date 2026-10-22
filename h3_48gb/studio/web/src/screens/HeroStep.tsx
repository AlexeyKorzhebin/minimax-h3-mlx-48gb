import { ImagePlus } from "lucide-react";
import { useRef, useState } from "react";
import { api, ApiError, upload } from "@/api/client";
import type { Asset, CastCreate, Snapshot } from "@/api/types";
import { Field } from "@/components/Field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { useIsPhone, useMobileAction } from "@/mobileAction";

const LLM_DOWN = new Set(["llm_failed", "llm_not_configured", "gpu_busy"]);

type Key = "photo" | "tag" | "ru" | "en" | "phrase";

export function HeroStep({ snap, onChanged }: { snap: Snapshot; onChanged: () => void }) {
  const [photo, setPhoto] = useState<Asset | null>(null);
  const [tag, setTag] = useState("");
  const [ru, setRu] = useState("");
  const [manual, setManual] = useState(false);
  const [en, setEn] = useState("");
  const [phrase, setPhrase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const form = useRef<HTMLFormElement>(null);
  const phone = useIsPhone();
  // Edit counters, as in the scene draft: a save answer clears only the fields nobody touched
  // since they were sent. `pick` is the token of the latest photo choice: the last chosen wins.
  const [gen] = useState<Record<Key, number>>(() => ({ photo: 0, tag: 0, ru: 0, en: 0, phrase: 0 }));
  const [pick] = useState({ token: 0 });
  const bump = (k: Key) => { gen[k] += 1; };
  const touched = Boolean(photo || tag || ru);
  const blocked = busy || uploading || !tag || !ru;
  useMobileAction("hero", touched
    ? { label: "Сохранить героя", onClick: () => form.current?.requestSubmit(), disabled: blocked, priority: 2 }
    : null);

  async function choose(file: File | undefined) {
    if (!file) return;
    const mine = ++pick.token;
    bump("photo");
    setError(null);
    setUploading(true);
    try {
      const asset = await upload<Asset>("/assets", file, { kind: "image" });
      if (mine !== pick.token) return;
      bump("photo");
      setPhoto(asset);
    } catch (err) {
      if (mine === pick.token) setError(err instanceof ApiError ? err.message : "Файл не загрузился");
    } finally {
      if (mine === pick.token) setUploading(false);
    }
  }

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (blocked) return;
    setBusy(true);
    setError(null);
    const sent = { ...gen };
    const body: CastCreate = {
      expected_revision: snap.project.revision,
      tag,
      description_ru: ru,
      assets: photo ? [{ asset_id: photo.id, role: "image" }] : [],
      ...(manual ? { description_en: en, portrait_phrase: phrase } : {}),
    };
    try {
      await api("POST", `/projects/${snap.project.id}/cast`, body);
      const same = (k: Key) => gen[k] === sent[k];
      if (same("photo")) setPhoto(null);
      if (same("tag")) setTag("");
      if (same("ru")) setRu("");
      if (same("en")) setEn("");
      if (same("phrase")) setPhrase("");
      if (same("en") && same("phrase")) setManual(false);
      onChanged();
    } catch (err) {
      if (err instanceof ApiError && LLM_DOWN.has(err.code)) {
        setManual(true);
        setError(`${err.message}. Впишите английское описание сами.`);
      } else if (err instanceof ApiError && err.code === "revision_conflict") {
        setError("Проект изменился в другом окне — данные обновлены, сохраните ещё раз");
        onChanged();
      } else {
        setError(err instanceof ApiError ? err.message : "Сервер не ответил");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="hero-h" className="mt-10">
      <div className="eyebrow">Шаг 2</div>
      <h2 id="hero-h" className="h-display mt-1 text-[24px]">Герой</h2>
      <ul className="mt-4 grid gap-4 md:grid-cols-2">
        {snap.cast.map((c) => (
          <li key={c.id} className="flex gap-4 rounded-lg border border-line p-4">
            {c.assets[0] && <img src={c.assets[0].url} alt={`@${c.tag}`} className="h-20 w-20 rounded-md object-cover" />}
            <div className="min-w-0">
              <div className="font-mono text-[12px] text-cobalt">@{c.tag}</div>
              <p className="mt-1 text-[13px]">{c.description_en}</p>
              <p className="mt-1 text-[12px] text-ink3">«{c.portrait_phrase}»</p>
            </div>
          </li>
        ))}
      </ul>
      <form ref={form} onSubmit={save} aria-label="Новый герой" className="mt-4 grid gap-4 rounded-lg border border-line p-5 md:grid-cols-[160px_1fr]">
        <label className="grid aspect-square cursor-pointer place-items-center overflow-hidden rounded-md border border-dashed border-line2 bg-surface text-ink3">
          {photo ? <img src={photo.url} alt="Фото героя" className="h-full w-full object-cover" /> : <ImagePlus className="h-6 w-6" aria-hidden />}
          <input type="file" accept="image/png,image/jpeg,image/webp" className="sr-only" aria-label="Фото героя" onChange={(e) => choose(e.target.files?.[0])} />
        </label>
        <div className="grid gap-4">
          <Field text="Тег" hint="латиницей, для @ в сценах">
            <Input value={tag} onChange={(e) => { bump("tag"); setTag(e.target.value.toLowerCase()); }} pattern="[a-z][a-z0-9_-]{0,31}" required />
          </Field>
          <Field text="Кто это" hint="по-русски">
            <Textarea value={ru} onChange={(e) => { bump("ru"); setRu(e.target.value); }} required />
          </Field>
          {manual && (
            <>
              <Field text="Описание по-английски">
                <Textarea value={en} onChange={(e) => { bump("en"); setEn(e.target.value); }} required />
              </Field>
              <Field text="Портретная фраза" hint="до 30 слов, вставляется в каждую сцену">
                <Input value={phrase} onChange={(e) => { bump("phrase"); setPhrase(e.target.value); }} required />
              </Field>
            </>
          )}
          {uploading && <p className="text-[13px] text-ink3">Фото загружается…</p>}
          {error && <p role="alert" className="text-[13px] text-danger">{error}</p>}
          {!phone && (
            <Button type="submit" disabled={blocked} className="justify-self-start">
              Сохранить героя
            </Button>
          )}
        </div>
      </form>
    </section>
  );
}
