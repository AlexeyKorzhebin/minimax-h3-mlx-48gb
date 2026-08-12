/* ===========================================================================
   h3 — панель прогонов. Поведение страницы.

   Разметка и оформление взяты из утверждённого макета `h3-panel-light.*`
   целиком; изменён только источник данных. В макете было подставное `STATE`,
   здесь — опрос `/api/state` раз в двадцать секунд и запросы к API на кнопки.

   Everything above the "СТРАНИЦА" divider is a pure function of its arguments:
   no `document`, no `fetch`, no clock of its own. That is not tidiness -- it is
   the only way the eleven acceptance requirements get checked without a
   browser, and `tests/test_web.py` imports this file into `node` to do exactly
   that. The DOM half at the bottom is guarded by a `typeof document` check so
   that importing the module outside a browser stays a no-op.

   Comments in English, interface text in Russian: the machine this runs on is
   Russian-speaking and the code is read by whoever maintains it.
   =========================================================================== */

/* Раз в двадцать секунд: шаг длится десять-восемнадцать минут, обслуживать
   реальное время нечего. */
export const POLL_MS = 20000;

/* Физическая память машины и две риски на шкале. Порог — предупреждение,
   а не запрет: модель подогнана на одном канвасе, и запрещать по
   экстраполяции неправильно. Молчать тоже нельзя — 15 с на 1248×832
   просят 49 ГБ. */
export const PHYSICAL_GB = 48;
export const WARN_GB = 40;
export const BLOCK_GB = 46;

/* Сколько назад смотрит список «закончилось». */
export const FINISHED_WINDOW_HOURS = 24;

/* Кадр превью пишется раз в N проходов; N — значение `--preview-every`,
   у CLI по умолчанию 5. */
export const DEFAULT_PREVIEW_EVERY = 5;

/* ===========================================================================
   ФОРМАТИРОВАНИЕ
   =========================================================================== */

/** Часы и минуты. Точность модели около десяти процентов — секунды здесь
 *  были бы обещанием, которого никто не давал. */
export function formatDuration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  // Rounded to whole minutes *first*, then split: rounding the remainder on its own printed
  // 3599 seconds as "60 мин" and 7199 as "1 ч 60 мин".
  const total = Math.round(s / 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  if (h && m) return `${h} ч ${String(m).padStart(2, "0")} мин`;
  if (h) return `${h} ч`;
  if (m) return `${m} мин`;
  return `${s} с`;
}

/** Минуты и секунды — для одного прохода, который длится минуты. */
export function formatFine(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m ? `${m} мин ${String(r).padStart(2, "0")} с` : `${r} с`;
}

export function formatClock(date) {
  const d = date instanceof Date ? date : new Date(date);
  if (Number.isNaN(d.getTime())) return "--:--";
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

export function formatGb(x) {
  return (Number(x) || 0).toFixed(1).replace(".", ",") + " ГБ";
}

export function plural(n, one, few, many) {
  const a = Math.abs(n) % 100;
  const b = a % 10;
  if (a > 10 && a < 20) return many;
  if (b > 1 && b < 5) return few;
  if (b === 1) return one;
  return many;
}

/** Всё, что попадает в разметку, проходит через это. Тег, заметка и текст
 *  промпта набраны человеком, а `log_tail` и `error` пришли из чужого
 *  процесса — ни одна из этих строк не наша. */
export function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ===========================================================================
   АРГУМЕНТЫ ЗАДАЧИ
   Задача хранит буквальный список аргументов, и это единственное место, где
   он разбирается обратно. Обе записи флага — `--tag x` и `--tag=x` — сервер
   принимает, поэтому обе понимаются и здесь.
   =========================================================================== */

export function argValue(args, flag) {
  const list = Array.isArray(args) ? args : [];
  let found = null;
  for (let i = 0; i < list.length; i++) {
    const item = String(list[i]);
    if (item === flag) {
      // argparse takes the last spelling of a repeated flag; so does this.
      found = i + 1 < list.length ? String(list[i + 1]) : null;
    } else if (item.startsWith(flag + "=")) {
      found = item.slice(flag.length + 1);
    }
  }
  return found;
}

/** Имя задачи для человека: тег, а если его нет — имя файла вывода. */
export function jobTag(job) {
  const tag = argValue(job.args, "--tag");
  if (tag) return tag;
  const stem = String(job.output_stem || "");
  return stem.slice(stem.lastIndexOf("/") + 1) || job.id;
}

/** `/media/<прогон>/<файл>` строится из `output_stem`, который сервер хранит
 *  абсолютным: предпоследний сегмент — каталог прогона, последний — основа
 *  имени. Если задача пишет прямо в корень выхода, каталога прогона нет и
 *  ссылка не строится: `/media` требует ровно `<прогон>/<файл>`. */
export function mediaParts(outputStem) {
  const parts = String(outputStem || "").split("/").filter((x) => x !== "");
  if (parts.length < 2) return null;
  return { run: parts[parts.length - 2], stem: parts[parts.length - 1] };
}

export function clipUrl(job) {
  const parts = mediaParts(job.output_stem);
  if (!parts) return null;
  return `/media/${encodeURIComponent(parts.run)}/${encodeURIComponent(parts.stem + ".mp4")}`;
}

/** Последний записанный кадр превью, или `null`, пока ни одного нет.
 *  Кадр пишется каждые `--preview-every` проходов, поэтому по числу
 *  завершённых проходов имя выводится, а не угадывается. */
export function previewStep(job, completedForwards) {
  const every = Number(argValue(job.args, "--preview-every") ?? DEFAULT_PREVIEW_EVERY);
  if (!Number.isFinite(every) || every <= 0) return 0;
  const done = Number(completedForwards) || 0;
  return Math.floor(done / every) * every;
}

export function previewUrl(job, completedForwards) {
  const step = previewStep(job, completedForwards);
  if (step <= 0) return null;
  const explicit = argValue(job.args, "--preview-stem");
  const parts = mediaParts(explicit || job.output_stem);
  if (!parts) return null;
  const name = `${parts.stem}-preview-step${String(step).padStart(2, "0")}.jpg`;
  return `/media/${encodeURIComponent(parts.run)}/${encodeURIComponent(name)}`;
}

/* ===========================================================================
   ПАМЯТЬ
   =========================================================================== */

/** Три исхода, а не два: до 40 ГБ молчим, выше 40 предупреждаем, выше 46
 *  требуем галочку. Порог считается по предсказанному пику против физических
 *  сорока восьми. */
export function memoryVerdict(peakGb) {
  const peak = Number(peakGb);
  if (!Number.isFinite(peak)) return { level: "ok", needsConfirm: false, text: "" };
  if (peak > BLOCK_GB) {
    return {
      level: "block",
      needsConfirm: true,
      text: `Предсказанный пик ${formatGb(peak)} против физических ${PHYSICAL_GB} — `
          + `за риской ${BLOCK_GB}. Прогон может упасть по памяти на середине.`,
    };
  }
  if (peak > WARN_GB) {
    return {
      level: "warn",
      needsConfirm: false,
      text: `Предсказанный пик ${formatGb(peak)} против физических ${PHYSICAL_GB} — `
          + `модель подогнана на одном канвасе и здесь может ошибаться.`,
    };
  }
  return { level: "ok", needsConfirm: false, text: "" };
}

/** Кнопка нажимается, только когда все три условия сошлись. Галочка — не
 *  украшение: без неё выше 46 ГБ постановки не будет. */
export function submitAllowed({ verdict, forced, canvasOk, busy }) {
  if (busy) return false;
  if (!canvasOk) return false;
  if (verdict && verdict.needsConfirm && !forced) return false;
  return true;
}

export function canvasIsPacked(width, height) {
  return Number.isInteger(width) && Number.isInteger(height)
      && width > 0 && height > 0 && width % 32 === 0 && height % 32 === 0;
}

/** Три готовых канваса, за черновик/предпросмотр/финал. Ровно эти три —
 *  форма не растёт списком пресетов, у неё есть ручной ввод для всего
 *  остального. */
export const CANVAS_PRESETS = [
  { key: "draft", label: "черновик", w: 448, h: 288 },
  { key: "small", label: "малое", w: 896, h: 576 },
  { key: "large", label: "большое", w: 1344, h: 768 },
];

/**
 * `{width, height}` для пресета `key`, или `null`, если такого пресета нет.
 *
 * Чистая функция — ни одного обращения к DOM, поэтому её проверяет узел без браузера, как и
 * весь остальной пул чистых функций этого модуля. Заполнение `#width`/`#height` и пересчёт
 * оценки после клика делает обработчик в `startPage()`, тем же путём, что и ручной ввод (см.
 * подписку `FIELDS` на `input`).
 */
export function applyCanvasPreset(key) {
  const preset = CANVAS_PRESETS.find((p) => p.key === key);
  return preset ? { width: preset.w, height: preset.h } : null;
}

/* ===========================================================================
   РАЗБОР ПРОМПТА

   The format is MiniMax's own, written down in `docs/upstream-guides/`; what
   follows is a reading of that document and nothing else. Everything this
   project wrote before those guides were found -- `[0.0-2.5s]` blocks, a
   `Characters:` section, a `[10s, ...]` header -- is a format we invented, and
   the model has no such fields; leftovers of it are reported, not corrected.

   Nothing here rewrites the prompt: it only shows what it sees. The format is
   the model's, not ours, and a rule we do not know must not become a ban.
   =========================================================================== */

/** The eleven languages the model speaks, spelled as they go inside `<d>[...]`. */
export const LANGUAGES = ["Arabic", "Chinese", "English", "French", "German", "Italian",
                          "Japanese", "Korean", "Portuguese", "Russian", "Spanish"];

/** The three top-level fields the documented format is made of. */
export const PROMPT_FIELDS = ["integrated_multimodal_description", "overall_soundscape",
                              "non_diegetic_music"];

const FIELD_HEAD = "(?:" + PROMPT_FIELDS.join("|") + ")";

const RX = {
  fld: new RegExp("^" + FIELD_HEAD + "\\s*:", "gm"),
  // `[Shot 1]` carries no timestamp; every later one opens with `At MM:SS.mmm,`.
  shot: /\[Shot\s+(\d+)\](?:\s+At\s+(\d{1,3}):(\d{2}(?:\.\d{1,3})?)\s*,?)?/g,
  // The opening tag is highlighted together with its language tag, the way it is written.
  dlg: /<d>(?:[ \t]*\[[^\]\n]*\])?|<\/d>/g,
  spk: /\(\s*S\d+(?:\s*,\s*S\d+)*\s*\)/g,
  // Markup of the format this project used before the guides were found.
  old: /\[\s*\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*s\s*\]|\[\s*\d+(?:\.\d+)?\s*s\s*,[^\]\n]*\]|^Characters\s*:/gm,
  // Abstract mood words the guide bans from `non_diegetic_music`. Physical descriptors that name
  // an audible property (soft, low, warm, gentle) are welcome there and deliberately absent here.
  mood: /\b(?:epic|tense|dramatic|emotional|melanchol(?:y|ic)|somber|joyful|uplifting|haunting|eerie|ominous|romantic|mysterious|suspenseful|triumphant|heroic|nostalgic|hopeful|wistful|sentimental|majestic|moody|sad|happy|fast-paced)\b/gi,
};

/** `2.5` as the format writes it: `00:02.500`. */
export function formatStamp(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  return String(Math.floor(s / 60)).padStart(2, "0") + ":"
       + (s % 60).toFixed(3).padStart(6, "0");
}

/** Непересекающиеся куски текста, которые надо подсветить. */
export function collectSpans(text) {
  const found = [];
  const push = (rx, cls) => {
    rx.lastIndex = 0;
    let m;
    while ((m = rx.exec(text))) found.push({ a: m.index, b: m.index + m[0].length, cls });
  };
  push(RX.fld, "fld");
  push(RX.shot, "shot");
  push(RX.dlg, "dlg");
  push(RX.spk, "spk");
  push(RX.old, "old");
  found.sort((x, y) => x.a - y.a || (y.b - y.a) - (x.b - x.a));
  const out = [];
  let last = -1;
  for (const f of found) if (f.a >= last) { out.push(f); last = f.b; }
  return out;
}

/** One top-level field: where its header starts, and the text that follows it up to the next
 *  header. `null` when the prompt does not carry the field at all. */
export function fieldValue(text, name) {
  const body = String(text == null ? "" : text);
  const head = new RegExp("^" + name + "\\s*:", "m").exec(body);
  if (!head) return null;
  const from = head.index + head[0].length;
  const rest = body.slice(from);
  const next = new RegExp("^" + FIELD_HEAD + "\\s*:", "m").exec(rest);
  return { at: head.index, from, text: next ? rest.slice(0, next.index) : rest };
}

/** Sentences, counted the way a reader counts them: by the stops between them. The lookahead is
 *  what keeps `00:02.500` and `0.00 seconds` from being read as two sentences each. */
export function countSentences(text) {
  return String(text == null ? "" : text)
    .split(/[.!?]+(?=\s|$)/)
    .map((piece) => piece.trim())
    .filter((piece) => piece !== "").length;
}

/** Every `[Shot N]` of the description, with its cut time in seconds or `null` for the first.
 *
 *  The search starts where `integrated_multimodal_description:` does, because the keyframe
 *  instruction above the fields says `(from [Shot 1])` -- a reference to a shot, not a shot.
 */
export function collectShots(text) {
  const body = String(text == null ? "" : text);
  const description = fieldValue(body, "integrated_multimodal_description");
  const from = description ? description.from : 0;
  const shots = [];
  RX.shot.lastIndex = 0;
  let m;
  while ((m = RX.shot.exec(body))) {
    if (m.index < from) continue;
    shots.push({
      n: Number(m[1]),
      at: m[2] === undefined ? null : Number(m[2]) * 60 + Number(m[3]),
      index: m.index,
    });
  }
  return shots;
}

/** Which speaker IDs the prompt names, and which of them ever open a `<d>`.
 *
 *  A character who never vocalises gets no ID, so an ID with no line behind it is either a
 *  silent character who was numbered by mistake or a line that was dropped. The stretch a mark
 *  owns runs to the next mark or to the next `[Shot`, whichever comes first: `(S1,S2) shout
 *  together, <d>...` gives the line to both, and `(S1) stands still. [Shot 2] ... (S2) says
 *  <d>...` gives it to neither but the second.
 */
export function collectSpeakers(text, shots) {
  const body = String(text == null ? "" : text);
  const cuts = (Array.isArray(shots) ? shots : []).map((shot) => shot.index);
  const marks = [];
  RX.spk.lastIndex = 0;
  let m;
  while ((m = RX.spk.exec(body))) {
    marks.push({ ids: m[0].match(/S\d+/g) || [], a: m.index, b: m.index + m[0].length });
  }
  const all = [];
  const speaking = new Set();
  marks.forEach((mark, i) => {
    for (const id of mark.ids) if (!all.includes(id)) all.push(id);
    const nextMark = i + 1 < marks.length ? marks[i + 1].a : body.length;
    const nextCut = cuts.find((at) => at >= mark.b);
    const until = Math.min(nextMark, nextCut === undefined ? body.length : nextCut);
    if (body.slice(mark.b, until).includes("<d>")) for (const id of mark.ids) speaking.add(id);
  });
  return { all, speaking: all.filter((id) => speaking.has(id)),
           silent: all.filter((id) => !speaking.has(id)) };
}

/** One of the two sound fields, checked against its sentence budget. */
function soundNote(body, name, most, { audio, extra = () => [] }) {
  const tail = audio ? " — у режима t2va без него пропадает звук" : "";
  const field = fieldValue(body, name);
  if (!field) return { k: audio ? "bad" : "warn", t: `Нет <span class="mono">${name}</span>` + tail };
  const value = field.text.trim();
  const sentences = countSentences(value);
  const trouble = [];
  if (value === "") trouble.push("поле пустое");
  else if (value !== "N/A" && (sentences < 1 || sentences > most)) {
    trouble.push(`${sentences} ${plural(sentences, "предложение", "предложения", "предложений")}, `
               + `а формат просит ${most === 4 ? "от одного до четырёх" : "от одного до трёх"}`);
  }
  trouble.push(...extra(value));
  return trouble.length
    ? { k: "warn", t: `<span class="mono">${name}</span>: ${trouble.join("; ")}` }
    : { k: "ok", t: `<span class="mono">${name}</span> на месте`
                  + (value === "N/A" ? " — <span class=\"mono\">N/A</span>"
                                     : `, <span class="num">${sentences}</span> `
                                       + plural(sentences, "предложение", "предложения",
                                                "предложений")) };
}

/**
 * Разбор промпта по документированному формату.
 *
 * `audio` — правда ли, что режим озвучен (`t2va`): без звуковых секций у
 * него пропадает звук, а у `t2v` их отсутствие ничего не ломает.
 */
export function analysePrompt(text, declaredSeconds, { audio = true } = {}) {
  const body = String(text == null ? "" : text);
  const declared = Number(declaredSeconds) || 0;
  const spans = collectSpans(body);
  const shots = collectShots(body);
  const notes = [];
  const bad = new Set();

  // Пустое поле — не промпт без звука, а промпт, которого ещё нет. Пять красных
  // замечаний на чистой странице учат не читать этот список вовсе.
  if (!body.trim()) {
    return { spans, shots, timeline: [],
             notes: [{ k: "warn", t: "Промпт пуст" }], bad };
  }

  // -- the field the whole format hangs off
  notes.push(fieldValue(body, "integrated_multimodal_description")
    ? { k: "ok", t: `Поле <span class="mono">integrated_multimodal_description</span> на месте` }
    : { k: "bad", t: `Нет поля <span class="mono">integrated_multimodal_description:</span> — `
                   + `описание планов должно идти под ним` });

  // -- shots and their cut times
  const timeline = [];
  if (!shots.length) {
    notes.push({ k: "warn", t: `Планов <span class="mono">[Shot N]</span> не найдено — `
                              + `вся сцена одним куском` });
  } else {
    const trouble = [];
    let previous = 0;
    if (shots[0].at !== null) {
      trouble.push("у первого плана есть метка времени, а её быть не должно");
      bad.add(shots[0].index);
      previous = shots[0].at;
    }
    for (let i = 1; i < shots.length; i++) {
      const shot = shots[i];
      if (shot.at === null) {
        trouble.push(`у плана ${shot.n} нет метки <span class="mono">At MM:SS.mmm,</span>`);
        bad.add(shot.index);
        continue;
      }
      if (!(shot.at > previous)) {
        trouble.push(`метка ${formatStamp(shot.at)} у плана ${shot.n} не позже предыдущей`);
        bad.add(shot.index);
      }
      if (declared > 0 && shot.at >= declared) {
        trouble.push(`метка ${formatStamp(shot.at)} у плана ${shot.n} за пределами `
                   + `заявленных ${declared.toFixed(1)} с`);
        bad.add(shot.index);
      }
      previous = shot.at;
    }
    if (shots.some((shot, i) => shot.n !== i + 1)) {
      trouble.push("планы пронумерованы не подряд с единицы");
    }
    const cuts = shots.slice(1).filter((shot) => shot.at !== null).map((shot) => shot.at);
    notes.push(trouble.length
      ? { k: "bad", t: `Планов ${shots.length}, но ${trouble.join("; ")}` }
      : { k: "ok", t: `Планов ${shots.length}`
                    + (cuts.length
                        ? `, склейки в <span class="num">`
                          + `${cuts.map(formatStamp).join(", ")}</span> — возрастают`
                          + (declared > 0
                              ? ` и укладываются в <span class="num">`
                                + `${declared.toFixed(1)}</span> с`
                              : "")
                        : " — один план, склеек нет") });
    // The bar under the editor is drawn only from a timeline that holds together; when it does
    // not, the note above already says why, and a drawing of nonsense would say it worse.
    if (!trouble.length && declared > 0 && cuts.every((at) => at < declared)) {
      const starts = shots.map((shot, i) => (i === 0 ? 0 : shot.at));
      starts.forEach((a, i) => timeline.push({
        n: shots[i].n, a, b: i + 1 < starts.length ? starts[i + 1] : declared,
      }));
    }
  }

  // -- <d> ... </d>, and the language inside
  const opens = (body.match(/<d>/g) || []).length;
  const closes = (body.match(/<\/d>/g) || []).length;
  let depth = 0;
  let tangled = false;
  const pairs = /<\/?d>/g;
  let tag;
  while ((tag = pairs.exec(body))) {
    depth += tag[0] === "<d>" ? 1 : -1;
    if (depth > 1 || depth < 0) { tangled = true; depth = Math.max(0, depth); }
  }
  const languages = [];
  const openings = /<d>/g;
  let open;
  while ((open = openings.exec(body))) {
    const named = /^[ \t]*\[([^\]\n]*)\]/.exec(body.slice(open.index + 3));
    languages.push(named ? named[1].trim() : null);
  }
  const unknown = languages.filter((name) => name === null || !LANGUAGES.includes(name));
  if (!opens && !closes) {
    notes.push({ k: "ok", t: `Реплик нет: тегов <span class="mono">&lt;d&gt;</span> в промпте `
                            + `не встречается` });
  } else if (opens !== closes) {
    notes.push({ k: "bad", t: `Теги речи не парные: <span class="mono">&lt;d&gt;</span> `
                            + `<span class="num">${opens}</span>, `
                            + `<span class="mono">&lt;/d&gt;</span> `
                            + `<span class="num">${closes}</span>` });
  } else if (tangled) {
    notes.push({ k: "bad", t: `Теги речи стоят не по порядку: закрывающий раньше открывающего `
                            + `или вложенные <span class="mono">&lt;d&gt;</span>` });
  } else if (unknown.length) {
    const names = unknown.map((name) => name === null ? "язык не назван"
                                                      : `«${escapeHtml(name)}»`);
    notes.push({ k: "bad", t: `Реплик <span class="num">${opens}</span>, но ${names.join(", ")}: `
                            + `внутри <span class="mono">&lt;d&gt;</span> язык из одиннадцати — `
                            + `${LANGUAGES.join(", ")}` });
  } else {
    const seen = [...new Set(languages)];
    notes.push({ k: "ok", t: `Реплик <span class="num">${opens}</span>, язык${seen.length > 1
                              ? "и" : ""} ${seen.join(", ")}` });
  }

  // -- (S1), (S2): only for those who actually vocalise
  const speakers = collectSpeakers(body, shots);
  if (speakers.silent.length) {
    notes.push({ k: "warn", t: `Идентификаторы без единой реплики: `
                              + `${speakers.silent.map((id) => `(${id})`).join(", ")} — `
                              + `по формату ID даётся только говорящему` });
  } else if (speakers.all.length) {
    notes.push({ k: "ok", t: `Говорящих <span class="num">${speakers.all.length}</span>: `
                            + `${speakers.all.map((id) => `(${id})`).join(", ")}` });
  }

  // -- the two sound fields
  notes.push(soundNote(body, "overall_soundscape", 4, {
    audio,
    // Speech belongs to the description; repeating it here is the mistake the guide names.
    extra: (value) => value.includes("<d>")
      ? [`внутри стоит <span class="mono">&lt;d&gt;</span>, а речь описывается `
         + `в <span class="mono">integrated_multimodal_description</span>`]
      : [],
  }));
  notes.push(soundNote(body, "non_diegetic_music", 3, {
    audio,
    // The guide bans abstract mood words outright: the field is instruments, tempo, dynamics.
    extra: (value) => {
      RX.mood.lastIndex = 0;
      const seen = [...new Set((value.match(RX.mood) || []).map((w) => w.toLowerCase()))];
      return seen.length
        ? [`слова о настроении (${seen.map((w) => `<span class="mono">${w}</span>`).join(", ")}) `
           + `— формат просит инструменты, темп и динамику`]
        : [];
    },
  }));

  // -- leftovers of the format this project invented
  RX.old.lastIndex = 0;
  const leftovers = [];
  let stale;
  while ((stale = RX.old.exec(body))) {
    const seen = stale[0].trim();
    if (!leftovers.includes(seen)) leftovers.push(seen);
  }
  if (leftovers.length) {
    const shown = leftovers.slice(0, 3)
      .map((seen) => `<span class="mono">${escapeHtml(seen)}</span>`).join(", ");
    notes.push({ k: "warn", t: `Разметка старого формата: ${shown}`
                              + (leftovers.length > 3 ? ` и ещё ${leftovers.length - 3}` : "")
                              + ` — документированный формат таких блоков не знает` });
  }

  return { spans, shots, timeline, notes, bad };
}

/** Слой подсветки: тот же текст, что в поле, с обёрнутыми кусками. */
export function highlightHtml(text, analysis) {
  const body = String(text == null ? "" : text);
  let html = "";
  let i = 0;
  for (const s of analysis.spans) {
    html += escapeHtml(body.slice(i, s.a));
    const cls = s.cls === "shot" && analysis.bad.has(s.a) ? "shot bad" : s.cls;
    html += `<mark class="${cls}">${escapeHtml(body.slice(s.a, s.b))}</mark>`;
    i = s.b;
  }
  return html + escapeHtml(body.slice(i)) + "\n";
}

/** Полоска планов под полем: сколько секунд держится каждый до следующей склейки.
 *  Рисуется только по сходящейся раскадровке — на разъезжающейся её нет вовсе,
 *  а замечание над ней уже сказало почему. */
export function scaleHtml(analysis) {
  const timeline = (analysis && analysis.timeline) || [];
  if (!timeline.length) return "";
  const end = timeline[timeline.length - 1].b || 1;
  const pc = (x) => (x / end) * 100 + "%";
  return timeline.map((seg) =>
    `<div class="seg" style="left:${pc(seg.a)};width:${pc(Math.max(0, seg.b - seg.a))}"`
    + ` title="план ${seg.n}: ${formatStamp(seg.a)}–${formatStamp(seg.b)}">`
    + `${(seg.b - seg.a).toFixed(1)}</div>`).join("");
}

/* ===========================================================================
   ОЧЕРЕДЬ: СВОДКИ И СТРОКИ
   =========================================================================== */

export function jobSeconds(job) {
  const value = job && job.estimate ? Number(job.estimate.seconds) : NaN;
  return Number.isFinite(value) ? value : 0;
}

export function jobPeak(job) {
  const value = job && job.estimate ? Number(job.estimate.peak_gb) : NaN;
  return Number.isFinite(value) ? value : 0;
}

/**
 * «Четыре задачи, ≈8 ч 04 мин, до 08:48» — ответ на вопрос, ради которого
 * ночная очередь и набирается.
 *
 * `runningSeconds` — сколько осталось идущей задаче: время окончания очереди
 * считается от конца того, что уже считается, а не от «сейчас».
 * Работника нет — времени окончания не существует, и назвать его значило бы
 * соврать: очередь стоит.
 */
export function pendingSummary(jobs, { now, runningSeconds = 0, workerState = "alive" } = {}) {
  const list = Array.isArray(jobs) ? jobs : [];
  const seconds = list.reduce((sum, job) => sum + jobSeconds(job), 0);
  const at = now instanceof Date ? now : new Date(now || Date.now());
  const count = list.length;
  if (!count) return { count: 0, seconds: 0, endsAt: null, text: "" };
  const word = plural(count, "задача", "задачи", "задач");
  if (workerState !== "alive") {
    return {
      count, seconds, endsAt: null,
      text: `${count} ${word}, ≈${formatDuration(seconds)} работы — но очередь стоит`,
    };
  }
  const ahead = Number(runningSeconds) || 0;
  const endsAt = new Date(at.getTime() + (seconds + ahead) * 1000);
  const withRunning = ahead > 0 ? " с учётом идущей" : "";
  return {
    count, seconds, endsAt,
    text: `${count} ${word}, ≈${formatDuration(seconds)}, `
        + `до ${formatClock(endsAt)}${withRunning}`,
  };
}

/** Завершённые за сутки, свежие сверху.
 *
 *  Задача без разбираемого `finished_at` не отбрасывается сразу: она закончилась,
 *  момент просто не записан. Но и держать её вечно нельзя — список «за сутки»,
 *  который никогда ничего не забывает, растёт без предела и хоронит сегодняшнее.
 *  Поэтому дата берётся по первой разобравшейся из трёх, и только задача, у которой
 *  не читается ни одна, остаётся в списке насовсем: такой в очереди не бывает —
 *  `created_at` пишется при постановке. */
export function finishedWithin(jobs, now, hours = FINISHED_WINDOW_HOURS) {
  const at = (now instanceof Date ? now : new Date(now || Date.now())).getTime();
  const window = hours * 3600 * 1000;
  return (Array.isArray(jobs) ? jobs : [])
    .filter((job) => {
      const stamps = [job.finished_at, job.started_at, job.created_at]
        .map((value) => Date.parse(value || ""))
        .filter((stamp) => !Number.isNaN(stamp));
      return stamps.length === 0 ? true : at - Math.max(...stamps) <= window;
    })
    .sort((a, b) => String(b.finished_at || "").localeCompare(String(a.finished_at || "")));
}

/** Деления по числу проходов: «три из семи» считывается быстрее, чем «43 %». */
export function stepsHtml(completed, total) {
  const forwards = Number(total) || 0;
  if (forwards <= 0) return "";
  const done = Math.max(0, Math.min(forwards, Number(completed) || 0));
  let html = "";
  for (let i = 1; i <= forwards; i++) {
    const cls = i <= done ? "done" : (i === done + 1 ? "now" : "");
    html += cls ? `<i class="${cls}"></i>` : "<i></i>";
  }
  return html;
}

const SPEC_CELLS = (job) => {
  const e = job.estimate || {};
  const mode = argValue(job.args, "--mode") || "auto";
  const w = e.width ?? "?";
  const h = e.height ?? "?";
  const sec = e.duration_seconds ?? "?";
  const steps = e.steps ?? "?";
  return `<span class="c l">${escapeHtml(mode)}</span>`
       + `<span class="c">${escapeHtml(w)}×${escapeHtml(h)}</span>`
       + `<span class="c">${escapeHtml(sec)} с</span>`
       + `<span class="c">${escapeHtml(steps)}</span>`;
};

/**
 * Ждущая задача: пять действий — обсудить, править, наверх, копия, удалить.
 *
 * Править/наверх/удалить есть только здесь: у идущей и завершённой их нет вовсе — не серые
 * кнопки, а их отсутствие, серая кнопка обещает, что когда-нибудь нажмётся. Обсудить — из той
 * же породы: разговор кончается `PUT /api/jobs/<id>`, а он бывает только у ждущей. Копия —
 * исключение: она ничего не меняет в этой задаче, только читает её `args`/`note`, поэтому
 * уместна и у завершённой тоже (см. `finishedRowHtml`).
 */
export function pendingRowHtml(job, { editingId = null } = {}) {
  const peak = jobPeak(job);
  const over = peak > WARN_GB;
  const priority = Number(job.priority) || 0;
  const id = escapeHtml(job.id);
  return `<div class="r wait${editingId === job.id ? " editing" : ""}">`
    + `<span class="m wait"></span>`
    + `<span class="name">${escapeHtml(jobTag(job))}`
    + (priority > 0 ? ` <span class="prio">↑${priority}</span>` : "")
    + `<span class="note">${escapeHtml(job.note)}</span></span>`
    + SPEC_CELLS(job)
    + `<span class="c">${formatDuration(jobSeconds(job))}</span>`
    + `<span class="c mem${over ? " over" : ""}">${formatGb(peak)}`
    + `<i class="mg" title="из ${PHYSICAL_GB} ГБ, риска на ${WARN_GB}">`
    + `<b style="width:${Math.min(100, peak / PHYSICAL_GB * 100)}%"></b></i></span>`
    + `<span class="acts">`
    + `<button data-act="chat" data-id="${id}">Обсудить</button>`
    + `<button data-act="edit" data-id="${id}">Править</button>`
    + `<button data-act="top" data-id="${id}">Наверх</button>`
    + `<button data-act="dup" data-id="${id}">Копия</button>`
    + `<button data-act="del" data-id="${id}">Удалить</button>`
    + `</span>`
    + `</div>`;
}

/**
 * Завершённая задача: код возврата виден всегда, причина — у упавших.
 * Единственное действие — копия (см. `pendingRowHtml`): прогон уже случился,
 * отменять и поднимать нечего, а вот повторить теми же параметрами — обычное дело.
 */
export function finishedRowHtml(job) {
  const code = job.exit_code;
  const ok = code === 0;
  const clip = ok ? clipUrl(job) : null;
  const stem = String(job.output_stem || "");
  const name = stem.slice(stem.lastIndexOf("/") + 1);
  const id = escapeHtml(job.id);
  const note = ok
    ? (clip ? `<a class="clip" href="${clip}">${escapeHtml(name)}.mp4</a>`
            : escapeHtml(name))
    : `код возврата ${escapeHtml(code == null ? "неизвестен" : code)}`;
  const took = job.started_at && job.finished_at
    ? (Date.parse(job.finished_at) - Date.parse(job.started_at)) / 1000
    : NaN;
  return `<div class="r ${ok ? "done" : "fail"}">`
    + `<span class="m ${ok ? "done" : "fail"}"></span>`
    + `<span class="name">${escapeHtml(jobTag(job))}<span class="note">${note}</span></span>`
    + SPEC_CELLS(job)
    + `<span class="c">${Number.isFinite(took) ? formatDuration(took) : "—"}</span>`
    + `<span class="c">${job.finished_at ? formatClock(new Date(job.finished_at)) : "—"}</span>`
    + `<span class="c l">код ${escapeHtml(code == null ? "?" : code)}</span>`
    + (ok ? "" : `<span class="why">${escapeHtml(job.log_tail || "причина не записана")}</span>`)
    + `<span class="acts">`
    + `<button data-act="dup" data-id="${id}">Копия</button>`
    + `</span>`
    + `</div>`;
}

/** Нечитаемые файлы очереди. Их нет ни в одном списке, а человек считает
 *  ночную очередь по списку — молчать о них нельзя. */
export function brokenHtml(broken) {
  const list = Array.isArray(broken) ? broken : [];
  if (!list.length) return "";
  const n = list.length;
  const word = plural(n, "файл", "файла", "файлов");
  const verb = plural(n, "не прочитался", "не прочитались", "не прочитались");
  const names = list
    .map((item) => `<span class="mono" title="${escapeHtml(item.error)}">`
                 + `${escapeHtml(item.path)}</span>`)
    .join(", ");
  return `<span>${n} ${word} в <span class="mono">queue/</span> ${verb} `
       + `и в очередь не попали: ${names}</span>`;
}

/** Прогон из `runs.scan`, отвечающий этой задаче: у обоих один каталог. */
export function runForJob(job, runs) {
  const outdir = argValue(job.args, "--outdir");
  if (!outdir) return null;
  return (Array.isArray(runs) ? runs : []).find((run) => run.outdir === outdir) || null;
}

/* ===========================================================================
   СВЯЗЬ С СЕРВЕРОМ
   =========================================================================== */

/** Строка «сервер не отвечает» или `null`, если последний опрос прошёл.
 *  Замершие цифры выглядят точно так же, как живые, — это и есть причина,
 *  по которой строка обязана появиться с первой же неудачи. */
export function offlineNotice(failures, lastOkAt, now) {
  if (!failures) return null;
  const at = (now instanceof Date ? now : new Date(now || Date.now())).getTime();
  if (!lastOkAt) return "Сервер не отвечает: страница ещё ни разу не получила состояние.";
  const ago = Math.max(0, Math.round((at - new Date(lastOkAt).getTime()) / 1000));
  return `Сервер не отвечает — данные ниже устарели, последний ответ `
       + `${formatDuration(ago)} назад (${formatClock(new Date(lastOkAt))}).`;
}

/** Русский текст отказа. Коды приходят из общего контракта `ERROR_CODES`;
 *  чего в этом списке нет, показывается как есть — сообщение сервера точнее
 *  любой заглушки. */
export function errorText(payload) {
  const error = (payload && payload.error) || {};
  const detail = error.detail || {};
  switch (error.code) {
    case "args_invalid":
      return { title: "Командная строка не годится", pre: detail.stderr || error.message };
    case "command_not_allowed":
      return { title: "Через очередь ставится только `generate` с чекпойнтом",
               pre: error.message };
    case "output_stem_conflict":
      return { title: `Имя вывода уже занято: ${detail.output_stem || "?"} — поменяйте тег`,
               pre: null };
    case "path_outside_root":
      return { title: "Путь вне корней, с которыми работает сервер",
               pre: `${detail.path || ""}\nкорни: `
                  + Object.values(detail.roots || {}).join(", ") };
    case "job_not_pending":
      return { title: "Задачу уже забрал работник — список перечитан", pre: null };
    case "prompt_name_invalid":
      return { title: "Имя промпта должно быть вида имя.txt без каталогов", pre: null };
    case "queue_unwritable":
      return { title: "Каталог очереди недоступен на запись", pre: detail.path || error.message };
    // Two codes, one sentence: `host_not_allowed` is a `Host:` that names another machine, and
    // `origin_not_allowed` is a write from another site. The second is the only one a browser
    // can actually produce, and before it was listed here it fell through to `default:` and
    // showed the server's English text on a Russian page.
    case "host_not_allowed":
    case "origin_not_allowed":
      return { title: "Запрос пришёл не с этой страницы", pre: error.message };
    case "internal_error":
      return { title: "Сервер споткнулся — смотрите его вывод в терминале",
               pre: detail.type || error.message };
    default:
      return { title: error.code ? `Отказ: ${error.code}` : "Запрос не прошёл",
               pre: error.message || null };
  }
}

/* ===========================================================================
   ФОРМА
   =========================================================================== */

/**
 * Список аргументов для `POST /api/jobs` (или `/api/estimate`, если
 * `withPrompt` снят: оценке промпт не нужен, а слать килобайты текста на
 * каждое нажатие клавиши незачем).
 */
export function buildArgs(form, { withPrompt = true } = {}) {
  const args = ["generate"];
  if (withPrompt) {
    if (form.promptFile) args.push("--prompt-file", form.promptFile);
    else if (form.prompt) args.push(form.prompt);
  }
  args.push("--width", String(form.width), "--height", String(form.height),
            "--duration", String(form.duration), "--steps", String(form.steps),
            "--seed", String(form.seed), "--tag", form.tag,
            "--mode", form.mode,
            "--checkpoint", form.checkpoint, "--outdir", form.outdir);
  if (form.lora) args.push("--turbo-lora", form.lora, "--turbo-strength", String(form.loraStrength));
  if (form.image) args.push("--image", form.image);
  if (form.endImage) args.push("--end-image", form.endImage);
  return args;
}

/** Тег с сидом в хвосте. Без него три сида одной сцены упрутся в
 *  `output_stem_conflict`: имя вывода строится из тега. */
export function tagWithSeed(tag, seed) {
  const base = String(tag || "run").replace(/-s\d+$/, "") || "run";
  return `${base}-s${seed}`;
}

/**
 * Что становится с формой после успешной постановки: ничего, кроме сида и
 * тега. Главный сценарий — накидать несколько задач на ночь, меняя по одному
 * полю; очищенная форма заставляла бы вводить всё заново.
 */
export function advanceAfterSubmit({ seed, tag }) {
  const current = Number.parseInt(seed, 10);
  const next = Number.isFinite(current) ? current + 1 : 1;
  return { seed: next, tag: tagWithSeed(tag, next) };
}

/** Каталог вывода по умолчанию: тот, куда шли последние задачи. Первый
 *  запуск — подкаталог по дате, а не корень выхода: `/media` отдаёт файлы
 *  только из каталога прогона, и задача, пишущая в корень, осталась бы без
 *  превью и без ссылки на ролик. */
export function defaultOutdir(state, today = new Date()) {
  const queue = (state && state.queue) || {};
  const jobs = ["running", "pending", "done", "failed"]
    .flatMap((name) => Array.isArray(queue[name]) ? queue[name] : [])
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  for (const job of jobs) {
    const outdir = argValue(job.args, "--outdir");
    if (outdir) return outdir;
  }
  const runs = (state && state.runs) || [];
  if (runs.length && runs[0].outdir) return runs[0].outdir;
  const stamp = [today.getFullYear(),
                 String(today.getMonth() + 1).padStart(2, "0"),
                 String(today.getDate()).padStart(2, "0")].join("-");
  return `~/video-out/${stamp}`;
}

/* ===========================================================================
   ДИАЛОГ

   The modal's own logic, kept here rather than inside `startPage()` for the
   reason the whole top half of this file exists: a conversation that rewrites
   the prompt window is checkable by `node` only while the rewriting is a
   function of its arguments.
   =========================================================================== */

/**
 * Ответ модели, собранный в текст промпта.
 *
 * The three fields and their order are fixed by the JSON schema the model answers under
 * (`provider.PROMPT_SCHEMA`), so the model cannot lose a field or reorder them and this
 * function does not have to guess at either. `instruction` carries no header of its own --
 * it is the keyframe sentence an `i2v` prompt opens with -- and is separated from the fields
 * by a blank line, the way the format document writes it. `null` there means `t2v`/`t2va`,
 * where such a sentence must not appear at all.
 */
export function buildPromptText(prompt) {
  const turn = prompt || {};
  const blocks = [];
  const instruction = turn.instruction == null ? "" : String(turn.instruction).trim();
  if (instruction) blocks.push(instruction);
  for (const name of PROMPT_FIELDS) {
    blocks.push(`${name}: ${turn[name] == null ? "" : String(turn[name])}`);
  }
  // A trailing newline: the text goes into a file and into `--prompt-file`, and every other
  // prompt in `prompts/` ends with one.
  return blocks.join("\n\n") + "\n";
}

/**
 * Состояние модалки после одного ответа модели.
 *
 * `turn.prompt === null` — реплика без правки (уточняющий вопрос, обсуждение): в ленту она
 * попадает, окна промпта не касается. Иначе окно переписывается целиком — модель отвечает
 * промптом полностью, а не куском, и склеивать её ответ с прошлым текстом было бы догадкой.
 *
 * Правка руками не теряется по построению: следующий ход уходит с текстом окна, а не с тем,
 * что модель прислала в прошлый раз (см. `sendChatMessage`).
 */
export function applyTurn(state, turn) {
  const answer = turn || {};
  if (!Array.isArray(state.log)) state.log = [];
  state.log.push({ role: "assistant", text: String(answer.reply == null ? "" : answer.reply) });
  if (answer.prompt) state.promptText = buildPromptText(answer.prompt);
  return state;
}

/**
 * Русская строка отказа хода — по коду, как и везде на этой странице.
 *
 * `runningSeconds` — остаток идущего прогона, тот самый, что приборная строка печатает наверху
 * (`renderRunning`). Без него `gpu_busy` — факт без совета: человек не знает, ждать минуту или
 * три часа. Прогон без оценки молчит, а не обещает «~0 мин».
 *
 * Сообщение при любом отказе остаётся в поле ввода (см. `sendChatMessage`) — об этом сказано
 * словами, потому что поле выглядит одинаково и когда текст цел, и когда его стёрли.
 */
export function chatFailureText(payload, runningSeconds = 0) {
  const error = (payload && payload.error) || {};
  const message = error.message || "";
  switch (error.code) {
    case "gpu_busy": {
      const left = Number(runningSeconds) || 0;
      return "Идёт прогон — модель поднимется после него"
           + (left > 0 ? ` (~${formatDuration(left)})` : "")
           + ". Сообщение осталось в поле ввода.";
    }
    case "chat_busy":
      return "Ход уже идёт — дождитесь ответа модели.";
    case "provider_unavailable":
      return message || "Активный LLM-провайдер недоступен.";
    case "llama_did_not_start":
      return `Модель не поднялась: ${message}`;
    case "chat_unreachable":
      return `Провайдер не ответил: ${message}`;
    case "bad_model_json":
      return `Модель не удержала формат: ${message}`;
    default: {
      const { title, pre } = errorText(payload);
      return pre ? `${title}: ${pre}` : title;
    }
  }
}

/** Позиционный промпт списка аргументов, или `null`, если промпт уходит файлом. */
export function promptOfArgs(args) {
  const list = (Array.isArray(args) ? args : []).map(String);
  return list.length > 1 && !list[1].startsWith("-") ? list[1] : null;
}

/**
 * Те же аргументы задачи, но с промптом `text`.
 *
 * `--prompt-file` **убирается**, а не переписывается: у поставленной задачи он показывает на
 * снимок `queue/prompts/<id>.txt`, который делает сама очередь, и оставить его значило бы
 * получить 200 на правку и прогон по старому тексту — снимок читает работник, а не страница.
 * Новый текст уходит позиционно, ровно как его кладёт `buildArgs`, а очередь снимет с него
 * собственную копию при `PUT /api/jobs/<id>`.
 */
export function argsWithPrompt(args, text) {
  const list = (Array.isArray(args) ? args : []).map(String);
  const head = list.length ? list[0] : "generate";
  const rest = [];
  for (let i = 1; i < list.length; i++) {
    const item = list[i];
    if (item === "--prompt-file") { i += 1; continue; }      // флаг вместе со своим значением
    if (item.startsWith("--prompt-file=")) continue;
    // Позиционный промпт ставит эта же страница и всегда сразу за подкомандой (`buildArgs`);
    // ничто другое на этом месте без ведущего дефиса не стоит.
    if (i === 1 && !item.startsWith("-")) continue;
    rest.push(item);
  }
  return [head, String(text), ...rest];
}

/* ===========================================================================
   СТРАНИЦА
   Ниже — единственная половина файла, которая знает про DOM и про сеть.
   Вне браузера модуль импортируется ради функций выше и не делает ничего.
   =========================================================================== */

if (typeof document !== "undefined") {
  startPage();
}

function startPage() {
  const $ = (id) => document.getElementById(id);
  // `mode` подписан отдельно: у него сверх пересчёта есть своя работа — показать
  // или спрятать поля кадров.
  const FIELDS = ["width", "height", "duration", "steps", "seed", "tag",
                  "ckpt", "lora", "lora-str", "outdir", "image", "end-image"];

  let state = null;            // последний удавшийся ответ /api/state
  let failures = 0;            // подряд неудавшихся опросов
  let lastOkAt = null;         // когда состояние получено в последний раз
  let editing = null;          // id правящейся задачи, или null
  let busy = false;            // идёт запрос, меняющий очередь
  let lastEstimate = null;     // последняя удавшаяся оценка
  let promptFromFile = null;   // {name, text} — что было загружено из файла
  let estimateTimer = null;
  let chat = null;             // состояние открытой модалки диалога, или null
  let runningLeft = 0;         // сколько осталось идущему прогону — им объясняется gpu_busy

  const readForm = () => ({
    width: Math.round(Number($("width").value) || 0),
    height: Math.round(Number($("height").value) || 0),
    duration: Number(String($("duration").value).replace(",", ".")) || 0,
    steps: Math.round(Number($("steps").value) || 0),
    seed: Math.round(Number($("seed").value) || 0),
    tag: $("tag").value.trim() || "run",
    mode: $("mode").value,
    checkpoint: $("ckpt").value.trim(),
    lora: $("lora").value.trim(),
    loraStrength: Number(String($("lora-str").value).replace(",", ".")) || 1,
    outdir: $("outdir").value.trim(),
    note: $("note").value.trim(),
    prompt: $("prompt").value,
    image: $("image").value.trim(),
    endImage: $("end-image").value.trim(),
    promptFile: promptFileArg(),
  });

  /* Промпт уходит файлом, только если он в точности равен файлу: иначе на
     диске лежал бы один текст, а считался другой. Изменённый уходит
     позиционно, и об этом сказано под полем. */
  function promptFileArg() {
    if (!promptFromFile) return null;
    if ($("prompt").value !== promptFromFile.text) return null;
    return promptFromFile.path;
  }

  // -- сеть -------------------------------------------------------------------------------

  /* Никаких заголовков сверх Content-Type и никакого `mode`: браузер сам
     проставит Origin и Sec-Fetch-Site: same-origin, а сервер сверяет именно
     их. Подменять их нельзя, добавлять нечего. */
  async function api(method, url, body) {
    const options = { method };
    if (body !== undefined) {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify(body);
    }
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(url);
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  async function poll() {
    try {
      state = await api("GET", "/api/state");
      failures = 0;
      lastOkAt = new Date();
      if (!$("outdir").value) $("outdir").value = defaultOutdir(state);
    } catch {
      failures += 1;
    }
    // Outside the `try`: the prompt list has its own route and its own failure, and it is
    // refreshed on every poll rather than once at startup because prompts are written into
    // `prompts/` by hand while the page is open.
    await loadPromptList($("prompt-file").value);
    renderConnection();
    renderQueue();
  }

  // -- отрисовка --------------------------------------------------------------------------

  function renderConnection() {
    const notice = offlineNotice(failures, lastOkAt, new Date());
    $("conn").hidden = notice === null;
    $("conn").textContent = notice || "";
    if (notice) document.body.dataset.stale = "1";
    else delete document.body.dataset.stale;
    $("clock").textContent = formatClock(new Date());
    $("poll").textContent = lastOkAt
      ? `обновлено в ${formatClock(lastOkAt)}`
      : "опрос не проходил";
  }

  function renderQueue() {
    if (!state) return;
    const now = new Date();
    const queue = state.queue || {};
    const workerState = (state.worker || {}).state || "unknown";

    // -- приборная строка
    $("rail").dataset.worker = workerState;
    $("worker-state").textContent = {
      alive: "Работник запущен",
      stopped: "Работник не запущен",
      unknown: "Состояние работника неизвестно",
    }[workerState] || "Состояние работника неизвестно";
    $("worker-pid").textContent = {
      alive: "задачи берутся из очереди",
      stopped: "очередь стоит, задачи не берутся",
      unknown: "замок не удалось проверить",
    }[workerState] || "";

    // -- идёт сейчас
    const running = (queue.running || [])[0] || null;
    const progress = renderRunning(running, workerState, now);
    // Тот же остаток, что печатается в приборной строке, нужен модалке: `gpu_busy` без него —
    // отказ без совета, ждать минуту или три часа по нему не понять.
    runningLeft = progress.left;

    // -- ждут
    const pending = queue.pending || [];
    $("pending").innerHTML = pending
      .map((job) => pendingRowHtml(job, { editingId: editing })).join("");
    $("pending-empty").hidden = pending.length > 0;
    const summary = pendingSummary(pending, {
      now, runningSeconds: progress.left, workerState,
    });
    $("pending-sum").textContent = summary.text;

    const broken = brokenHtml(queue.broken);
    $("pending-bad").hidden = broken === "";
    $("pending-bad").innerHTML = broken;

    // -- закончилось за сутки
    const finished = finishedWithin([...(queue.done || []), ...(queue.failed || [])], now);
    $("finished").innerHTML = finished.map(finishedRowHtml).join("");
    $("finished-empty").hidden = finished.length > 0;
    const failed = finished.filter((job) => job.exit_code !== 0).length;
    $("done-sum").textContent = finished.length
      ? `${finished.length}, из них упало ${failed}`
      : "";
  }

  function renderRunning(job, workerState, now) {
    const box = $("running");
    const rail = $("rail-run");
    const steps = $("rail-steps");
    if (!job) {
      box.className = "running none";
      box.textContent = workerState === "alive"
        ? "Ничего не считается; работник ждёт задачу"
        : "Ничего не считается";
      rail.className = "rail-run idle";
      rail.textContent = workerState === "stopped"
        ? "Прогон не ведётся: некому его вести"
        : "Ничего не считается";
      steps.innerHTML = "";
      return { left: 0 };
    }

    // Работника нет, а задача лежит в running/ — это ровно тот случай, который
    // сверка при старте вернёт в очередь. Показывать её прогресс было бы враньём.
    if (workerState !== "alive") {
      box.className = "running stale";
      box.innerHTML = `<b class="mono">${escapeHtml(jobTag(job))}</b> осталась в `
        + `<span class="mono">running/</span>, но работник не запущен: прогресс неизвестен. `
        + `При следующем старте работника задача вернётся в очередь и подхватит `
        + `собственный чекпойнт.`;
      rail.className = "rail-run idle";
      rail.textContent = "Прогон не ведётся: некому его вести";
      steps.innerHTML = "";
      return { left: 0 };
    }

    const run = runForJob(job, state.runs);
    const forwards = Number((run && run.total) ?? (job.estimate || {}).forwards) || 0;
    const completed = Number((run && run.completed) || 0);
    const left = Number(run && run.eta_seconds);
    const remaining = Number.isFinite(left) ? left : jobSeconds(job);
    const till = new Date(now.getTime() + remaining * 1000);
    const share = forwards ? Math.min(1, completed / forwards) : 0;
    const peak = jobPeak(job);
    const shot = previewUrl(job, completed);

    const e = job.estimate || {};
    const cell = (k, v, cls = "") =>
      `<div><span class="k">${k}</span><span class="v${cls}">${v}</span></div>`;

    box.className = "running";
    box.innerHTML = [
      "<div>",
      shot
        // Кадр мог ещё не долететь на диск между записью чекпойнта и запросом:
        // пустое место честнее битой картинки.
        ? `<img src="${shot}" alt="превью-кадр" onerror="this.hidden = true">`
        : `<div class="noshot">Превью ещё не записано</div>`,
      `<div class="run-cap">`
        + (shot ? `Превью, проход ${previewStep(job, completed)} · TAE`
                : "Кадр появится после первых проходов")
        + `</div>`,
      "</div>",
      `<div class="run-body">`,
      `<div class="run-line1">`,
      `<span class="m run" aria-hidden="true"></span>`,
      `<span class="run-name">${escapeHtml(jobTag(job))}</span>`,
      `<span class="run-spec">${escapeHtml(argValue(job.args, "--mode") || "auto")}`
        + ` · ${escapeHtml(e.width)}×${escapeHtml(e.height)}`
        + ` · ${escapeHtml(e.duration_seconds)} с`
        + ` · сид ${escapeHtml(argValue(job.args, "--seed") || "0")}</span>`,
      `</div>`,
      `<div class="run-num">`,
      cell("Проход", `${completed}/${forwards}`),
      cell("Доля", `${Math.round(share * 100)} %`),
      cell("Осталось", formatDuration(remaining)),
      cell("Кончится", formatClock(till)),
      cell("Пик памяти", formatGb(peak), peak > WARN_GB ? " over" : ""),
      `</div>`,
      `<div class="bar"><i style="width:${share * 100}%"></i></div>`,
      `<div class="run-foot">`,
      `старт <span class="num">`
        + `${job.started_at ? formatClock(new Date(job.started_at)) : "—"}</span> · `,
      `оценка <span class="num">${formatDuration(jobSeconds(job))}</span> · `,
      `<span class="mono">${escapeHtml(job.id)}</span> · `,
      `<span class="run-note">${escapeHtml(job.note)}</span>`,
      `</div>`,
      `</div>`,
    ].join("");

    rail.className = "rail-run";
    rail.innerHTML = `<span class="run-tag">${escapeHtml(jobTag(job))}</span>`
      + `<span class="run-step">проход ${completed}/${forwards}</span>`
      + `<span class="run-share">${Math.round(share * 100)} %</span>`
      + `<span class="run-left">осталось <b>${formatDuration(remaining)}</b> `
      + `<span class="dim">до ${formatClock(till)}</span></span>`;
    steps.innerHTML = stepsHtml(completed, forwards);
    return { left: remaining };
  }

  /* Разбор, подсветка и полоска планов — одни и те же для формы и для модалки: `ids` называет
     тройку элементов, в которые рисовать (`hl`/`scale`/`parse` у формы, `chat-*` у модалки).
     Длительность берётся из формы в обоих случаях — сессия чата её не хранит, а разбору нужна
     одна цифра, чтобы сказать, укладываются ли склейки в ролик. */
  function paintPrompt(ids, text, { audio }) {
    const declared = Number(String($("duration").value).replace(",", ".")) || 0;
    const analysis = analysePrompt(text, declared, { audio });
    $(ids.hl).innerHTML = highlightHtml(text, analysis);
    const scale = scaleHtml(analysis);
    $(ids.scale).innerHTML = scale;
    $(ids.scale).hidden = scale === "";   // пустая полоска покрытия ничего не покрывает
    $(ids.parse).innerHTML = analysis.notes.map((n) => `<li class="${n.k}">${n.t}</li>`).join("");
  }

  function renderPrompt() {
    paintPrompt({ hl: "hl", scale: "scale", parse: "parse" }, $("prompt").value,
                { audio: $("mode").value === "t2va" });

    const box = $("prompt-src");
    const file = promptFileArg();
    if (file) {
      box.className = "src";
      box.textContent = `Уйдёт как --prompt-file ${file}; очередь снимет с него копию.`;
    } else if (promptFromFile) {
      box.className = "src dirty";
      box.textContent = `Текст разошёлся с ${promptFromFile.name} — уйдёт строкой в командной `
        + `строке, снимка промпта у задачи не будет. Сохраните в файл, чтобы был.`;
    } else {
      box.className = "src dirty";
      box.textContent = "Промпт не из файла — уйдёт строкой в командной строке, "
        + "снимка промпта у задачи не будет.";
    }
  }

  function showError(payload) {
    const { title, pre } = errorText(payload);
    $("err").hidden = false;
    $("err").innerHTML = `<b>${escapeHtml(title)}</b>`
      + (pre ? `<pre>${escapeHtml(pre)}</pre>` : "");
  }

  function clearError() {
    $("err").hidden = true;
    $("err").innerHTML = "";
  }

  function say(text) {
    $("est-said").textContent = text;
    setTimeout(() => { $("est-said").textContent = ""; }, 6000);
  }

  // -- оценка -----------------------------------------------------------------------------

  function applyEstimate(estimate) {
    lastEstimate = estimate;
    const forwards = Number(estimate.forwards) || 0;
    $("est-time").textContent = "≈" + formatDuration(estimate.seconds);
    $("est-sub").innerHTML = `${forwards} `
      + `${plural(forwards, "проход", "прохода", "проходов")} по `
      + `<span class="num">${formatFine(estimate.seconds_per_forward)}</span>, `
      + `DiT ${escapeHtml(estimate.bits)} бит`;
    $("mem-num").textContent = "~" + formatGb(estimate.peak_gb);

    const verdict = memoryVerdict(estimate.peak_gb);
    const fill = $("mem-fill");
    fill.style.width = Math.min(100, estimate.peak_gb / PHYSICAL_GB * 100) + "%";
    fill.className = "fill" + (verdict.level === "block" ? " bad"
                             : verdict.level === "warn" ? " warn" : "");
    $("mem-warn").hidden = verdict.level === "ok";
    $("mem-warn").className = "mem-warn" + (verdict.level === "block" ? " bad" : "");
    $("mem-warn").textContent = verdict.text;
    $("force-wrap").hidden = !verdict.needsConfirm;
    if (!verdict.needsConfirm) $("force").checked = false;
    refreshSubmitState();
  }

  function refreshSubmitState() {
    const form = readForm();
    const packed = canvasIsPacked(form.width, form.height);
    $("canvas-hint").textContent = packed ? "Кратно 32" : "Должно быть кратно 32";
    $("canvas-hint").className = "hint" + (packed ? "" : " bad");
    const verdict = lastEstimate ? memoryVerdict(lastEstimate.peak_gb)
                                 : { needsConfirm: false };
    $("submit").disabled = !submitAllowed({
      verdict, forced: $("force").checked, canvasOk: packed, busy,
    });
  }

  /* Оценка пересчитывается на каждое изменение поля, но не на каждое нажатие
     клавиши: подряд идущие правки схлопываются в один запрос. */
  function scheduleEstimate() {
    refreshSubmitState();
    clearTimeout(estimateTimer);
    estimateTimer = setTimeout(requestEstimate, 250);
  }

  async function requestEstimate() {
    const form = readForm();
    // Оценка проверяет пути, поэтому пустой каталог — не «ноль», а отказ
    // `path_outside_root`, показанный до того, как человек хоть что-то ввёл.
    if (!canvasIsPacked(form.width, form.height) || !form.outdir || !form.checkpoint) return;
    try {
      const answer = await api("POST", "/api/estimate",
                               { args: buildArgs(form, { withPrompt: false }) });
      applyEstimate(answer.estimate);
      clearError();
    } catch (error) {
      if (error.payload) showError(error.payload);
      $("est-time").textContent = "≈—";
      $("mem-num").textContent = "—";
    }
  }

  // -- промпты ----------------------------------------------------------------------------

  /* The sentinel of the "new file" entry has to be a value no prompt name can take, and it also
     has to survive the HTML parser: NUL does not. The standard makes the parser replace NUL in
     an attribute value with U+FFFD, so the entry reached the page with a different value than
     the one compared against, every comparison was false, and picking it wedged the select
     instead of asking for a name. `__new__` is printable, cannot be a prompt name (the server
     demands a `.txt` suffix), and keeps `app.js` a text file for `grep`. */
  const NEW_PROMPT = "__new__";

  /* Called on every poll, not once at startup: prompts are written by hand into `prompts/`
     while the page is open, and a list that never grows is a file that cannot be picked.
     The current choice is passed back in, or reloading would silently reset the select. */
  async function loadPromptList(selected) {
    let answer;
    try {
      answer = await api("GET", "/api/prompts");
    } catch {
      return;
    }
    const select = $("prompt-file");
    select.innerHTML = `<option value="">— промпт набран здесь —</option>`
      + answer.prompts.map((p) =>
          `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name)} · ${p.bytes} Б</option>`)
        .join("")
      + `<option value="${NEW_PROMPT}">— новый файл… —</option>`;
    select.value = selected || "";
  }

  async function loadPrompt(name) {
    if (!name) { promptFromFile = null; renderPrompt(); return; }
    try {
      const answer = await api("GET", "/api/prompts/" + encodeURIComponent(name));
      promptFromFile = { name, text: answer.text, path: answer.path };
      $("prompt").value = answer.text;
      clearError();
    } catch (error) {
      if (error.payload) showError(error.payload);
      promptFromFile = null;
    }
    renderPrompt();
  }

  async function savePrompt() {
    let name = $("prompt-file").value;
    if (!name || name === NEW_PROMPT) {
      name = window.prompt("Имя файла в prompts/ (латиница, цифры, дефис, .txt)", "scene.txt");
      if (!name) return;
    }
    try {
      const answer = await api("PUT", "/api/prompts/" + encodeURIComponent(name),
                               { text: $("prompt").value });
      promptFromFile = { name, text: $("prompt").value, path: answer.path };
      await loadPromptList(name);
      clearError();
      say(`Сохранено · ${answer.bytes} Б`);
    } catch (error) {
      if (error.payload) showError(error.payload);
    }
    renderPrompt();
  }

  // -- диалог -----------------------------------------------------------------------------

  /* Адрес — часть состояния модалки. Обрыв связи или перезагрузка страницы во время хода не
     теряет разговор: сессия лежит в `<outdir>/chat/<id>.json`, а `#chat/<id>` открывает её
     заново с историей с диска. Идентификатор — `secrets.token_hex(4)`, шестнадцатеричный. */
  const CHAT_HASH = /^#chat\/([0-9a-f]+)$/;

  /* Кнопка завершения зависит от источника: разговор о промпте библиотеки кончается файлом,
     о задаче — правкой задачи, а начатый из формы — текстом в редакторе. `clip` (ролик проекта)
     сервер уже хранит, а страница открывать пока не умеет — до спеки «проекты» он ведёт себя
     как новый промпт. */
  const FINISH_LABEL = { new: "в Редактор", clip: "в Редактор",
                         prompt: "сохранить промпт", job: "обновить задачу" };

  const LLM_TEXT = {
    up: "модель поднята",
    down: "модель не поднята — поднимется при первом сообщении",
  };

  /* Предупреждение хода — по коду, как и отказ: сервер шлёт `{code, message}` именно затем,
     чтобы страница не разбирала его предложение. Ход при этом состоялся — просто без кадра. */
  const WARNING_TEXT = {
    image_not_found: "Кадр не нашёлся — ход ушёл без картинки",
    image_unreadable: "Кадр не прочитался — ход ушёл без картинки",
    bad_image: "Кадр не годится в картинку — ход ушёл без него",
  };

  function chatSourceText(source) {
    const it = source || {};
    if (it.kind === "prompt") return `промпт ${it.name || "?"}`;
    if (it.kind === "job") return `задача ${it.id || "?"}`;
    if (it.kind === "clip") return `ролик ${it.id || "?"}`;
    return "новый промпт";
  }

  function chatWarningText(warning) {
    const it = warning || {};
    const head = WARNING_TEXT[it.code] || "Ход прошёл с оговоркой";
    return it.message ? `${head}: ${it.message}` : head;
  }

  /**
   * Плашка модели. `override` — строка на один случай («жду ответа…»), без него плашка
   * собирается из выбранного провайдера и последнего известного состояния.
   *
   * У внешнего провайдера подниматься нечему: тридцать гигабайт держит только локальная
   * модель, и `/api/llm` честно отвечает `down` — но «модель не поднята» на openrouter звучит
   * как «что-то не готово», хотя готово всё.
   */
  function renderLlmPlate(override) {
    if (override) { $("chat-llm").textContent = override; return; }
    const row = ((chat && chat.providers) || [])
      .find((item) => item.name === $("chat-provider").value);
    $("chat-llm").textContent = row && row.type !== "llama-local"
      ? "внешний провайдер — память этой машины не занимает"
      : (LLM_TEXT[(chat && chat.llmStatus) || ""] || "состояние модели неизвестно");
  }

  function renderChatPrompt() {
    if (!chat) return;
    // Присваивание только при расхождении: `value = value` во время набора сбрасывает каретку
    // в конец строки.
    if ($("chat-prompt-text").value !== chat.promptText) {
      $("chat-prompt-text").value = chat.promptText;
    }
    paintPrompt({ hl: "chat-hl", scale: "chat-scale", parse: "chat-parse" }, chat.promptText,
                { audio: (chat.mode || $("mode").value) === "t2va" });
  }

  function renderChatLog() {
    if (!chat) return;
    const box = $("chat-log");
    box.innerHTML = chat.log.length
      ? chat.log.map((entry) =>
          `<li class="turn ${entry.role}${entry.kind ? " " + entry.kind : ""}">`
          + `${escapeHtml(entry.text)}</li>`).join("")
      : `<li class="turn note">Опишите идею словами — модель соберёт промпт по формату. `
        + `Уже готовый текст можно вставить в окно слева и попросить привести к стандарту.</li>`;
    box.scrollTop = box.scrollHeight;   // свежая реплика видна без прокрутки
  }

  /* Роспись провайдеров и состояние локальной модели. Недоступный провайдер остаётся в списке
     серым со своей причиной («нет токена OPENROUTER_API_KEY»): исчезнувший из списка выглядит
     как не настроенный вовсе. */
  async function loadProviders() {
    let roster;
    try {
      roster = await api("GET", "/api/providers");
    } catch {
      renderLlmPlate("роспись провайдеров не прочиталась");
      return;
    }
    const rows = roster.providers || [];
    if (chat) chat.providers = rows;
    $("chat-provider").innerHTML = rows.map((row) =>
      `<option value="${escapeHtml(row.name)}"${row.available ? "" : " disabled"}>`
      + `${escapeHtml(row.name)}`
      + (row.available ? "" : ` · ${escapeHtml(row.reason || "недоступен")}`)
      + `</option>`).join("");
    $("chat-provider").value = roster.active || "";
    if (!rows.length) {
      renderLlmPlate("провайдеров нет — заполните providers.json");
      return;
    }
    try {
      if (chat) chat.llmStatus = (await api("GET", "/api/llm")).status;
    } catch {
      if (chat) chat.llmStatus = "";
    }
    renderLlmPlate();
  }

  /** Новая сессия от источника и переход в неё. `opened` — что видно только странице:
   *  текст промпта, режим и путь кадра (сервер сам их не знает). */
  async function openChatModal(source, opened = {}) {
    try {
      const answer = await api("POST", "/api/chat", {
        source,
        prompt: opened.prompt || "",
        mode: opened.mode || "",
        image: opened.image || "",
      });
      clearError();
      window.location.hash = `#chat/${answer.id}`;
      await syncChatFromHash();
    } catch (error) {
      if (error.payload) showError(error.payload);
    }
  }

  async function enterChat(id) {
    let session;
    try {
      session = await api("GET", "/api/chat/" + encodeURIComponent(id));
    } catch (error) {
      if (error.payload) showError(error.payload);
      closeChat();
      return;
    }
    chat = {
      id,
      source: session.source || { kind: "new" },
      mode: session.mode || "",
      /* Окно восстанавливается из последнего ответа модели, а не из `prompt` сессии: `prompt`
         — это текст, с которым сессию открыли, и ходы его не переписывают (так устроен
         сервер). Правки руками между ходами в сессии не живут вовсе — они уходят следующим
         ходом и возвращаются в ответе. */
      promptText: session.prompt_struct ? buildPromptText(session.prompt_struct)
                                        : (session.prompt || ""),
      log: (session.messages || []).map((m) => ({ role: m.role, text: m.content })),
      sending: false,
      providers: [],       // роспись из /api/providers — по ней собирается плашка модели
      llmStatus: "",
    };
    $("chat-modal").hidden = false;
    $("chat-finish").textContent = FINISH_LABEL[chat.source.kind] || FINISH_LABEL.new;
    $("chat-source").textContent = chatSourceText(chat.source);
    renderChatPrompt();
    renderChatLog();
    await loadProviders();
    $("chat-input").focus();
  }

  function closeChat() {
    chat = null;
    $("chat-modal").hidden = true;
    if (CHAT_HASH.test(window.location.hash || "")) window.location.hash = "";
  }

  async function syncChatFromHash() {
    const match = CHAT_HASH.exec(window.location.hash || "");
    if (!match) {
      if (chat) closeChat();
      return;
    }
    if (chat && chat.id === match[1]) return;
    await enterChat(match[1]);
  }

  async function sendChatMessage() {
    if (!chat || chat.sending) return;
    const text = $("chat-input").value.trim();
    if (!text) return;
    chat.sending = true;
    $("chat-send").disabled = true;
    renderLlmPlate("жду ответа — на холодной модели это до минуты");
    chat.log.push({ role: "user", text });
    renderChatLog();
    try {
      const answer = await api("POST", `/api/chat/${encodeURIComponent(chat.id)}/message`,
                               { text, prompt: chat.promptText,
                                 provider: $("chat-provider").value });
      applyTurn(chat, answer);
      if (answer.warning) {
        chat.log.push({ role: "note", kind: "warn", text: chatWarningText(answer.warning) });
      }
      $("chat-input").value = "";
      chat.llmStatus = (answer.llm || {}).status || chat.llmStatus;
      renderLlmPlate();
      clearError();
    } catch (error) {
      /* Ход не состоялся: сервер ничего не записал, поэтому реплика уходит из ленты обратно —
         и остаётся в поле ввода. Текст, который человек написал, теряться не должен ни при
         занятом GPU, ни при упавшем провайдере. */
      chat.log.pop();
      chat.log.push({ role: "note", kind: "bad",
                      text: chatFailureText(error.payload, runningLeft) });
      chat.llmStatus = "down";
      renderLlmPlate();
    } finally {
      chat.sending = false;
      $("chat-send").disabled = false;
      renderChatPrompt();
      renderChatLog();
    }
  }

  /** Промпт поставленной задачи, насколько его вообще видно странице.
   *
   *  Снимок `queue/prompts/<id>.txt` маршрутом не отдаётся, поэтому читаются два случая:
   *  промпт, ушедший позиционно (он лежит прямо в `args`), и задача из библиотеки — у неё
   *  `prompt_source` называет файл `prompts/<имя>.txt`. Иначе окно открывается пустым:
   *  пустое честнее чужого текста.
   */
  async function jobPromptText(job) {
    const positional = promptOfArgs(job.args);
    if (positional !== null) return positional;
    const match = /^prompts\/([^/]+\.txt)$/.exec(String(job.prompt_source || ""));
    if (!match) return "";
    try {
      return (await api("GET", "/api/prompts/" + encodeURIComponent(match[1]))).text;
    } catch {
      return "";
    }
  }

  async function openChatFromJob(id) {
    const job = ((state && state.queue && state.queue.pending) || [])
      .find((row) => row.id === id);
    if (!job) return;
    await openChatModal({ kind: "job", id }, {
      prompt: await jobPromptText(job),
      mode: argValue(job.args, "--mode") || "t2va",
      image: argValue(job.args, "--image") || "",
    });
  }

  /**
   * Кнопка завершения. Что она делает, решает вид источника — и только он.
   *
   * Неудача оставляет модалку открытой: текст в окне — единственная копия работы, и закрывать
   * его после отказа значит терять её.
   */
  async function finishChat() {
    if (!chat) return;
    const source = chat.source || { kind: "new" };
    const text = chat.promptText;

    if (source.kind === "prompt" && source.name) {
      try {
        const answer = await api("PUT", "/api/prompts/" + encodeURIComponent(source.name),
                                 { text });
        // Форма может смотреть на этот же файл: если да, её текст обязан совпасть с тем, что
        // теперь на диске, иначе следующая постановка уйдёт строкой вместо --prompt-file.
        if ($("prompt-file").value === source.name) {
          promptFromFile = { name: source.name, text, path: answer.path };
          $("prompt").value = text;
        }
        await loadPromptList($("prompt-file").value);
        renderPrompt();
        clearError();
        say(`Сохранено · ${answer.bytes} Б`);
      } catch (error) {
        if (error.payload) showError(error.payload);
        return;
      }
    } else if (source.kind === "job" && source.id) {
      const job = ((state && state.queue && state.queue.pending) || [])
        .find((row) => row.id === source.id);
      if (!job) {
        showError({ error: { code: "job_not_pending" } });
        return;
      }
      let failed = false;
      await withQueue(async () => {
        try {
          await api("PUT", "/api/jobs/" + encodeURIComponent(source.id),
                    { args: argsWithPrompt(job.args, text), note: job.note || "" });
          say("Задача обновлена");
        } catch (error) {
          failed = true;
          throw error;
        }
      });
      if (failed) return;
    } else {
      /* «в Редактор»: дальше обычная постановка. Промпт перестаёт быть файловым — текст
         разошёлся с файлом ровно в тот момент, когда его переписала модель, и уйти он должен
         строкой, а не как --prompt-file на старое содержимое. */
      $("prompt").value = text;
      promptFromFile = null;
      $("prompt-file").value = "";
      renderPrompt();
      scheduleEstimate();
    }
    closeChat();
  }

  // -- действия ---------------------------------------------------------------------------

  function fillFormFrom(job) {
    const e = job.estimate || {};
    $("width").value = e.width ?? 896;
    $("height").value = e.height ?? 512;
    $("duration").value = e.duration_seconds ?? 5;
    $("steps").value = e.steps ?? 31;
    $("seed").value = argValue(job.args, "--seed") || "0";
    $("tag").value = argValue(job.args, "--tag") || "run";
    $("mode").value = argValue(job.args, "--mode") || "t2va";
    $("ckpt").value = argValue(job.args, "--checkpoint") || "";
    $("lora").value = argValue(job.args, "--turbo-lora") || "";
    $("lora-str").value = argValue(job.args, "--turbo-strength") || "1.00";
    $("outdir").value = argValue(job.args, "--outdir") || "";
    $("image").value = argValue(job.args, "--image") || "";
    $("end-image").value = argValue(job.args, "--end-image") || "";
    $("note").value = job.note || "";
    syncModeRows();
  }

  function setEditing(id) {
    editing = id;
    $("form-mode-note").textContent = id ? `Правка ждущей задачи ${id}` : "Новая задача";
    $("submit").textContent = id ? "Сохранить правку" : "Поставить в очередь";
    $("cancel-edit").hidden = !id;
    if (id && state) {
      const job = (state.queue.pending || []).find((x) => x.id === id);
      if (job) {
        fillFormFrom(job);
        /* `prefers-reduced-motion` is honoured by the stylesheet for everything the stylesheet
           animates, but a smooth scroll asked for in script is not one of those things: the
           media query cannot reach it, so the query is asked here instead. */
        const still = typeof window.matchMedia === "function"
          && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        window.scrollTo({ top: 0, behavior: still ? "auto" : "smooth" });
      }
    }
    renderQueue();
    renderPrompt();
    scheduleEstimate();
  }

  async function withQueue(action) {
    busy = true;
    refreshSubmitState();
    try {
      await action();
      clearError();
    } catch (error) {
      if (error.payload) showError(error.payload);
      else showError({ error: { message: "сервер не ответил" } });
    } finally {
      busy = false;
      // Любой исход — перечитать состояние. У 409 это единственно верная
      // реакция: задачу забрал работник, и повторять запрос нечего.
      await poll();
      refreshSubmitState();
    }
  }

  async function submit() {
    const form = readForm();
    const body = { args: buildArgs(form), note: form.note };
    await withQueue(async () => {
      if (editing) {
        await api("PUT", "/api/jobs/" + encodeURIComponent(editing), body);
        say("Правка сохранена");
        setEditing(null);
      } else {
        const answer = await api("POST", "/api/jobs", body);
        // Форма намеренно не очищается: за вечер сюда кладут пять задач
        // подряд, меняя по одному полю. Сдвигаются только сид и тег.
        const next = advanceAfterSubmit({ seed: form.seed, tag: form.tag });
        $("seed").value = next.seed;
        $("tag").value = next.tag;
        say(`Поставлено · ${answer.job.id} · следующий сид ${next.seed}`);
        scheduleEstimate();
      }
    });
  }

  function syncModeRows() {
    const mode = $("mode").value;
    $("row-image").hidden = !(mode === "i2v" || mode === "flf");
    $("row-end-image").hidden = mode !== "flf";
  }

  // -- подписки ---------------------------------------------------------------------------

  document.addEventListener("click", (event) => {
    const presetButton = event.target.closest("button[data-preset]");
    if (presetButton) {
      const preset = applyCanvasPreset(presetButton.dataset.preset);
      if (preset) {
        $("width").value = preset.width;
        $("height").value = preset.height;
        // Тот же путь, что и ручной ввод в поля из FIELDS — см. подписку ниже.
        scheduleEstimate();
        renderPrompt();
      }
      return;
    }

    const button = event.target.closest("button[data-act]");
    if (!button) return;
    const id = button.dataset.id;
    if (button.dataset.act === "chat") { openChatFromJob(id); return; }
    if (button.dataset.act === "edit") { setEditing(id); return; }
    if (button.dataset.act === "top") {
      withQueue(() => api("POST", `/api/jobs/${encodeURIComponent(id)}/top`, {}));
      return;
    }
    if (button.dataset.act === "dup") {
      withQueue(() => api("POST", `/api/jobs/${encodeURIComponent(id)}/duplicate`, {}));
      return;
    }
    if (button.dataset.act === "del") {
      withQueue(async () => {
        await api("DELETE", "/api/jobs/" + encodeURIComponent(id));
        if (editing === id) setEditing(null);
      });
    }
  });

  $("submit").addEventListener("click", submit);
  $("cancel-edit").addEventListener("click", () => setEditing(null));
  $("force").addEventListener("change", refreshSubmitState);
  $("save-prompt").addEventListener("click", savePrompt);
  $("prompt-file").addEventListener("change", (event) => {
    const name = event.target.value;
    if (name === NEW_PROMPT) { event.target.value = ""; promptFromFile = null; renderPrompt(); }
    else loadPrompt(name);
  });
  $("prompt").addEventListener("input", renderPrompt);
  $("prompt").addEventListener("scroll", () => {
    $("hl").scrollTop = $("prompt").scrollTop;
    $("hl").scrollLeft = $("prompt").scrollLeft;
  });
  for (const id of FIELDS) {
    $(id).addEventListener("input", () => { scheduleEstimate(); renderPrompt(); });
  }
  $("mode").addEventListener("change", () => {
    syncModeRows();
    renderPrompt();     // t2va без звуковых секций — отказ, t2v без них — нет
    scheduleEstimate();
  });

  // -- подписки модалки -------------------------------------------------------------------

  $("chat-new").addEventListener("click", () => {
    const mode = $("mode").value;
    openChatModal({ kind: "new" }, {
      prompt: $("prompt").value,
      mode,
      // Кадр есть только у двух режимов, и только у них он уходит в сессию: модель смотрит на
      // него глазами, а t2v-разговору показывать нечего.
      image: (mode === "i2v" || mode === "flf") ? $("image").value.trim() : "",
    });
  });

  $("chat-prompt-open").addEventListener("click", async () => {
    const name = $("prompt-file").value;
    if (!name || name === NEW_PROMPT) {
      say("Сначала выберите промпт в списке");
      return;
    }
    let text;
    try {
      text = (await api("GET", "/api/prompts/" + encodeURIComponent(name))).text;
    } catch (error) {
      if (error.payload) showError(error.payload);
      return;
    }
    openChatModal({ kind: "prompt", name }, { prompt: text, mode: $("mode").value });
  });

  // Смена провайдера меняет и смысл плашки: у внешнего поднимать нечего.
  $("chat-provider").addEventListener("change", () => renderLlmPlate());
  $("chat-close").addEventListener("click", closeChat);
  $("chat-finish").addEventListener("click", finishChat);
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendChatMessage();
  });
  /* Enter отправляет, Shift+Enter переносит строку: поле на две строки — это реплика, а не
     редактор; сам промпт правится слева. */
  $("chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendChatMessage();
    }
  });
  /* Правка руками живёт в состоянии модалки и уходит следующим ходом: сервер собирает
     системное сообщение из тела запроса, а не из сессии, поэтому модель видит именно то, что
     сейчас в окне. */
  $("chat-prompt-text").addEventListener("input", () => {
    if (!chat) return;
    chat.promptText = $("chat-prompt-text").value;
    renderChatPrompt();
  });
  $("chat-prompt-text").addEventListener("scroll", () => {
    $("chat-hl").scrollTop = $("chat-prompt-text").scrollTop;
    $("chat-hl").scrollLeft = $("chat-prompt-text").scrollLeft;
  });
  // Клик по подложке и Esc закрывают модалку — привычные два жеста, оба ничего не теряют:
  // сессия остаётся на диске, и `#chat/<id>` открывает её обратно.
  $("chat-modal").addEventListener("click", (event) => {
    if (event.target === $("chat-modal")) closeChat();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && chat) closeChat();
  });
  window.addEventListener("hashchange", syncChatFromHash);

  // -- запуск -----------------------------------------------------------------------------

  syncModeRows();
  renderPrompt();
  renderConnection();
  poll().then(() => { requestEstimate(); syncChatFromHash(); });
  setInterval(poll, POLL_MS);
  setInterval(renderConnection, 1000);
}
