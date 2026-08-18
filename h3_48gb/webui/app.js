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

/** `/media/<прогон>/<файл>` строится из `output_stem` (абсолютный) и `outdir` —
 *  тот самый `--outdir`, с которым запущен `h3 web`, из `/api/state` (`build_state`
 *  в `web.py`). Fix round 1 (ревью A6, C1): раньше «прогон» брался по числу
 *  сегментов (предпоследний), что верно ровно на глубине 1 — а своя папка
 *  задачи (A6) плюс папка-дата, которую сама форма подставляет по умолчанию
 *  (`defaultOutdir()`), вместе дают глубину 2 и больше. Без `outdir` сервера
 *  границу между «корнем сервера» и «путём задачи» разобрать нечем — вся
 *  строка `output_stem` абсолютна и ничем не выдаёт, где она начинается, — и
 *  без него ссылка не строится. Остаток после `outdir/` целиком, до
 *  последнего `/`, идёт в `run` одним куском (сервер, `_media`, сам разбирает
 *  его до последнего `/`, а не по сегментам) — это и даёт произвольную
 *  глубину: `2026-08-12/20260813-1435-kot-italy` не хуже, чем `2026-08-12`. */
export function mediaParts(outputStem, outdir) {
  const stem = String(outputStem || "");
  const base = String(outdir || "");
  if (!base || !stem.startsWith(`${base}/`)) return null;
  const relative = stem.slice(base.length + 1);
  const lastSlash = relative.lastIndexOf("/");
  if (lastSlash <= 0) return null;
  return { run: relative.slice(0, lastSlash), stem: relative.slice(lastSlash + 1) };
}

/** Версия для `?v=` на ссылке `/media`, не трогая годовой `immutable`-кэш сервера (`_cache_control`
 *  в `web.py`, `MEDIA_MAX_AGE`): у сервера нет способа отличить «тот же файл» от «файл с тем же
 *  именем переписан», а редактирование/повтор упавшей задачи с тем же тегом пишет именно поверх
 *  старого имени. `finished_at` — как только он есть, он не меняется, и он единственное на клиенте,
 *  что различает два прогона *одной и той же* задачи (id у задачи один и тот же что до, что после
 *  повтора). Пока прогон не завершился, `finished_at` ещё `null` — тогда берём `started_at`: он уже
 *  проставлен и всё равно меняется на каждый новый прогон той же задачи, только на менее точный
 *  момент. Нет ни того, ни другого (задача только легла в очередь) — версии не будет: ссылка ещё не
 *  строится (`clipUrl`/`previewUrl` сами возвращают `null`, пока файла с большой вероятностью нет).
 */
function mediaVersion(job) {
  return job.finished_at || job.started_at || "";
}

/** `url`, если версии нет — так `previewUrl`/`clipUrl` не выдают лишний `?v=`, когда version
 *  ещё нечем наполнить, ровно как остальной модуль возвращает `null`, а не мусорную ссылку. */
function withVersion(url, version) {
  return version ? `${url}?v=${encodeURIComponent(version)}` : url;
}

export function clipUrl(job, outdir) {
  const parts = mediaParts(job.output_stem, outdir);
  if (!parts) return null;
  const url = `/media/${encodeURIComponent(parts.run)}/${encodeURIComponent(parts.stem + ".mp4")}`;
  return withVersion(url, mediaVersion(job));
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

export function previewUrl(job, completedForwards, outdir) {
  const step = previewStep(job, completedForwards);
  if (step <= 0) return null;
  const explicit = argValue(job.args, "--preview-stem");
  const parts = mediaParts(explicit || job.output_stem, outdir);
  if (!parts) return null;
  const name = `${parts.stem}-preview-step${String(step).padStart(2, "0")}.jpg`;
  const url = `/media/${encodeURIComponent(parts.run)}/${encodeURIComponent(name)}`;
  return withVersion(url, mediaVersion(job));
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

/** Три готовых канваса — черновик/предпросмотр/финал — и каждый в двух ориентациях (C2).
 *
 *  Вертикальные добавлены не ради полноты списка: вертикальный ролик снимается тем же
 *  черновиком, что и горизонтальный, а до этой задачи его размеры набирались руками в оба поля
 *  и набирались неправильно. Ровно шесть — форма не растёт списком пресетов дальше, у неё есть
 *  «своё…» с ручным вводом для всего остального.
 *
 *  Порядок — парами (гориз., верт.): выпадашка читается сверху вниз, и размер обязан стоять
 *  рядом со своим поворотом, а не в отдельном хвосте списка. `label` — подпись пункта без
 *  чисел; числа в подписи пишет разметка, и `test_every_canvas_preset_has_its_own_option_...`
 *  сверяет, что она пишет именно эти. */
export const CANVAS_PRESETS = [
  { key: "draft", label: "черновик", w: 448, h: 288 },
  { key: "draft-v", label: "черновик верт.", w: 288, h: 448 },
  { key: "small", label: "малое", w: 896, h: 576 },
  { key: "small-v", label: "малое верт.", w: 576, h: 896 },
  { key: "large", label: "большое", w: 1344, h: 768 },
  { key: "large-v", label: "большое верт.", w: 768, h: 1344 },
];

/**
 * `{width, height}` для пресета `key`, или `null`, если такого пресета нет.
 *
 * Чистая функция — ни одного обращения к DOM, поэтому её проверяет узел без браузера, как и
 * весь остальной пул чистых функций этого модуля. Заполнение `#width`/`#height` и пересчёт
 * оценки после выбора делает обработчик в `startPage()`, тем же путём, что и ручной ввод (см.
 * подписку `FIELDS` на `input`).
 */
export function applyCanvasPreset(key) {
  const preset = CANVAS_PRESETS.find((p) => p.key === key);
  return preset ? { width: preset.w, height: preset.h } : null;
}

/**
 * Пункт выпадашки, которому отвечает канвас `width`×`height`, или `"custom"`, если ни один.
 *
 * Обратная сторона `applyCanvasPreset`: выпадашка обязана показывать то, что в полях, а не то,
 * что в ней последний раз выбрали руками. С этой стороны приходят правка задачи
 * (`fillFormFrom` — у задачи свои числа, и они могут не совпасть ни с одним пресетом) и ручной
 * ввод в «своё…».
 */
export function canvasPresetKey(width, height) {
  const preset = CANVAS_PRESETS.find((p) => p.w === Number(width) && p.h === Number(height));
  return preset ? preset.key : "custom";
}

/** Последнее звено пути, как есть — тем же правилом, что и `pathBasename` в DOM-половине:
 *  путь мог прийти с сервера (всегда `/`) или быть вписан руками на другой ОС. */
function nameOfPath(value) {
  const trimmed = String(value == null ? "" : value).trim();
  if (!trimmed) return "";
  const parts = trimmed.split(/[\\/]/);
  return parts[parts.length - 1];
}

/** То же, но без расширения. Только для **файлов**: у каталога «расширения» не бывает, и
 *  отрезать у него хвост по последней точке значит назвать другой каталог (правка по ревью C2 —
 *  `h3-8bit-full.v2` превращался в `h3-8bit-full`, который у людей лежит рядом). */
function stemOfPath(value) {
  const name = nameOfPath(value);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(0, dot) : name;
}

/**
 * Одна строка вместо четырёх свёрнутых полей: «h3-8bit-full · 8 шагов · LoRA 1.00 · таблица l100».
 *
 * `<details>` с настройками модели закрыт по умолчанию (C2, требование 3) — и это правильно
 * ровно до тех пор, пока свёрнутый вид говорит, что под ним лежит. Прогон по чужому чекпойнту
 * или без LoRA стоит лишнего часа, а выглядит как обычный, и единственное, что стоит между
 * человеком и этим часом, — вот эта строка.
 *
 * Отсутствующее не молчит, а называется: пустая LoRA — «LoRA нет» (без неё прогон идёт вчетверо
 * дольше), пустая таблица AdaLN — «таблица чекпойнта» (сетку тогда задаёт сам чекпойнт, см.
 * подпись поля). Молчание об этом читалось бы как «на месте».
 *
 * Чистая функция от объекта того же вида, что отдаёт `readForm()`: `{checkpoint, steps, lora,
 * loraStrength, adaln}`. Ни одного обращения к DOM — её зовёт `refreshSubmitState`, а проверяет
 * узел без браузера.
 */
export function modelSummary(form) {
  const it = form || {};
  // Чекпойнт — каталог (`--checkpoint` указывает на папку с весами), поэтому имя целиком:
  // см. `stemOfPath` о том, чем это кончалось. Таблица AdaLN ниже — файл, и там стем уместен.
  const checkpoint = nameOfPath(it.checkpoint) || "чекпойнт не указан";
  const steps = Math.max(0, Math.round(Number(it.steps) || 0));
  const lora = String(it.lora || "").trim()
    ? `LoRA ${(Number(it.loraStrength) || 0).toFixed(2)}`
    : "LoRA нет";
  const adaln = stemOfPath(it.adaln);
  // `adaln_8_l100.safetensors` -> `l100`: имена таблиц строятся из числа шагов и длины сетки,
  // и в свёрнутой строке человеку нужен именно хвост, а не общий для всех префикс.
  const table = adaln ? `таблица ${adaln.split("_").pop() || adaln}` : "таблица чекпойнта";
  return [checkpoint,
          `${steps} ${plural(steps, "шаг", "шага", "шагов")}`,
          lora, table].join(" · ");
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

/**
 * A3: длительность, против которой модалка проверяет склейки — из состояния диалога
 * (`chat.duration`), не из формы. Форма и модалка — два разных ролика: форма ставит задачу
 * с одной длительностью, модалка тем временем может обсуждать совсем другую (например, чат
 * открыт от задачи с её собственным `--duration`), и `paintPrompt` модалки обязан спорить с
 * планами по числу, которое видел сервер в системном контексте (`duration: N s`,
 * `_locked_turn`), а не по тому, что сейчас в поле `#duration` где-то ещё на странице.
 *
 * Чистая функция ровно потому, что от неё это и нужно: она не имеет доступа к DOM (`chat`
 * само по себе — обычный объект), так что её нельзя случайно свести обратно к чтению формы.
 */
export function chatDuration(state) {
  return (state && Number(state.duration)) || 10;
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

/** Все завершённые, свежие сверху.
 *
 *  Окна тут больше нет. Список смотрел на сутки назад из догадки, что список, который
 *  ничего не забывает, «хоронит сегодняшнее»; догадка не подтвердилась, а цена
 *  оказалась настоящей — из тринадцати роликов за выходные на странице оставалось два,
 *  и это читается как «пропали», а не как «убраны с глаз». Сегодняшнее и так сверху,
 *  потому что список отсортирован, а вчерашнее внизу никому не мешает: карточки
 *  прокручиваются страницей.
 *
 *  Дата для сортировки — первая разобравшаяся из трёх, а не один `finished_at`:
 *  задача, у которой момент не записан, закончилась тогда же, когда всё остальное, и
 *  уезжать за прошлый год не должна. Та, у которой не читается ни одна, уходит вниз —
 *  такой в очереди не бывает, `created_at` пишется при постановке. */
export function finishedSorted(jobs) {
  const at = (job) => {
    const stamps = [job.finished_at, job.started_at, job.created_at]
      .map((value) => Date.parse(value || ""))
      .filter((stamp) => !Number.isNaN(stamp));
    return stamps.length ? Math.max(...stamps) : -Infinity;
  };
  return (Array.isArray(jobs) ? jobs : []).slice().sort((a, b) => at(b) - at(a));
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

/** Технические параметры задачи одной строкой: режим, канвас, длительность, шаги.
 *
 *  До C2 это были четыре ячейки общей девятиколоночной сетки `--cols`, одной на все три списка.
 *  Списков теперь два и оба карточные (макет: `.qitem` в очереди, `.rcard` в готовом), сравнивать
 *  числа по вертикали между ними больше нечем и незачем — параметры стали подписью под именем. */
const specText = (job) => {
  const e = job.estimate || {};
  const mode = argValue(job.args, "--mode") || "auto";
  const w = e.width ?? "?";
  const h = e.height ?? "?";
  const sec = e.duration_seconds ?? "?";
  const steps = e.steps ?? "?";
  return `${escapeHtml(mode)} · ${escapeHtml(w)}×${escapeHtml(h)}`
       + ` · ${escapeHtml(sec)} с · ${escapeHtml(steps)} шаг.`;
};

/**
 * Ждущая задача: пять действий — обсудить, править, наверх, копия, удалить.
 *
 * Править/наверх/удалить есть только здесь: у идущей и завершённой их нет вовсе — не серые
 * кнопки, а их отсутствие, серая кнопка обещает, что когда-нибудь нажмётся. Обсудить — из той
 * же породы: разговор кончается `PUT /api/jobs/<id>`, а он бывает только у ждущей. Копия —
 * исключение: она ничего не меняет в этой задаче, только читает её `args`/`note`, поэтому
 * уместна и у завершённой тоже (см. `finishedRowHtml`).
 *
 * C2: строка общей сетки стала карточкой `.qitem` из макета — номер в очереди слева, имя и
 * параметры столбиком, действия справа. Заметка (`job.note`) остаётся: поле ввода из формы ушло
 * (требование 4), но сама заметка приезжает с сервера у копий и у задач, поставленных не с этой
 * страницы, и прятать её было бы прятанием чужих данных.
 */
export function pendingRowHtml(job, { editingId = null, index = null } = {}) {
  const peak = jobPeak(job);
  const over = peak > WARN_GB;
  const priority = Number(job.priority) || 0;
  const id = escapeHtml(job.id);
  const note = String(job.note || "");
  return `<div class="qitem${editingId === job.id ? " editing" : ""}">`
    + `<span class="idx">${index === null ? "" : escapeHtml(index)}</span>`
    + `<span class="m wait" aria-hidden="true"></span>`
    + `<span class="body">`
    + `<span class="n">${escapeHtml(jobTag(job))}`
    + (priority > 0 ? ` <span class="prio">↑${priority}</span>` : "")
    + `</span>`
    + `<span class="meta">${specText(job)} · ≈${formatDuration(jobSeconds(job))}`
    + `<span class="mem${over ? " over" : ""}">${formatGb(peak)}`
    + `<i class="mg" title="из ${PHYSICAL_GB} ГБ, риска на ${WARN_GB}">`
    + `<b style="width:${Math.min(100, peak / PHYSICAL_GB * 100)}%"></b></i></span></span>`
    + (note ? `<span class="note">${escapeHtml(note)}</span>` : "")
    + `</span>`
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
 *
 * Четыре действия (было три до task 2): «Обсудить», «Показать в Finder» и «Копия» только читают
 * задачу, ничего в ней не меняя (у «Обсудить» есть и пишущий финал — «обновить задачу»,
 * `PUT /api/jobs/<id>` из модалки, — но он существует только для ждущей, и сервер сам откажет
 * `job_not_pending`, попроси его завершённая; это осмысленный отказ, а не дыра, так что кнопка
 * открытия разговора здесь всё равно уместна: посмотреть и обсудить прошлый прогон стоит и без
 * намерения его переписывать). «Удалить» — четвёртое и единственное пишущее: `DELETE
 * /api/jobs/<id>` теперь понимает и готовую/упавшую задачу (не только ждущую), стирая и её запись
 * из очереди, и файлы прогона с диска — см. `h3_48gb/web.py`'s `_delete_finished_job`. Свой
 * `data-act` (`delrun`, не `del`) и свой `confirm()`: у ждущей нет подтверждения вовсе (снять из
 * очереди — дешёвая, обратимая на практике операция, задачу можно переставить снова), а здесь
 * кнопка стирает файлы с диска, и обратно их не вернуть. Едит/наверх нет вовсе — прогон уже
 * случился, у готовой карточки нет ни своей строки в форме, чтобы её править, ни места в очереди,
 * откуда её поднимать.
 *
 * `outdir` — необязательный (fix round 1, A6): без него `clipUrl` просто не
 * построит ссылку (см. `mediaParts`), а не упадёт — карточка ещё покажет имя
 * файла текстом, как до этой задачи, если `outdir` почему-то не пришёл.
 *
 * C2: строка стала карточкой `.rcard` из макета, и главное в ней — кадр.
 *
 * Round 2 (карточки готового): кадр решает исход, и это не мелочь. У успешной задачи — её
 * собственный ролик (`<video>`, первый кадр браузер вытянет сам; клик по нему — пуск/пауза
 * инлайн, без открытия вкладки): TAE-снимок полусырого латента с середины диффузии для готовой
 * задачи ничего не говорит — есть mp4, и он один даёт честный кадр. У упавшей ролика нет никогда
 * (прогон не дошёл до записи), и там кадр остаётся тем же снимком TAE, что и раньше — единственное,
 * что от прогона осталось посмотреть, поэтому `<img>` с явной подписью, что это снимок с середины
 * оборвавшегося счёта, а не результат.
 *
 * Число проходов для этой подписи и для адреса кадра упавшей — `runs.scan`
 * (`run.completed`, посчитанный по чекпойнтам на диске), не `estimate.forwards`: до последнего
 * прохода упавшая не дожила, и адрес, построенный по обещанному числу, отвечал 404 на каждый
 * опрос, раз в двадцать секунд, на каждую упавшую карточку (ревью C2, опасение 1). У успешной
 * этот вопрос больше не встаёт вовсе — кадр её ролика не зависит от числа проходов.
 *
 * Кадра у упавшей может не быть вовсе (прогон короче одного интервала записи, упал раньше
 * первого, или его каталога уже нет в `runs`), а у успешной — без `outdir` (`clipUrl` тогда
 * возвращает `null`) — в обоих случаях рамка пустая: битая картинка хуже пустой.
 */
export function finishedRowHtml(job, outdir, runs) {
  const code = job.exit_code;
  const ok = code === 0;
  const clip = ok ? clipUrl(job, outdir) : null;
  const stem = String(job.output_stem || "");
  const name = stem.slice(stem.lastIndexOf("/") + 1);
  const id = escapeHtml(job.id);
  const e = job.estimate || {};
  const run = ok ? null : runForJob(job, runs);
  const completed = ok ? 0 : Number((run && run.completed) || 0);
  const step = ok ? 0 : previewStep(job, completed);
  const shot = ok ? null : previewUrl(job, completed, outdir);
  // Код возврата стоит в `.meta` и только там: до этой правки упавшая карточка называла его
  // дважды подряд — «код 1» строкой выше и «код возврата 1» строкой ниже.
  const link = ok && clip
    ? `<a class="clip" href="${clip}">${escapeHtml(name)}.mp4</a>`
    : escapeHtml(name);
  const took = job.started_at && job.finished_at
    ? (Date.parse(job.finished_at) - Date.parse(job.started_at)) / 1000
    : NaN;
  const frame = ok
    ? (clip
        ? `<video class="frame-video" preload="metadata" muted playsinline src="${clip}" `
          + `title="Кадр ролика — щёлкните для показа/паузы"></video>`
        : "")
    : (shot
        ? `<img src="${shot}" alt="снимок с середины диффузии" `
          + `title="снимок с шага ${step} — прогон упал" onerror="this.hidden = true">`
        : "");
  return `<article class="rcard ${ok ? "done" : "fail"}">`
    + `<div class="frame">`
    + frame
    + `<span class="tc">${escapeHtml(e.width ?? "?")}×${escapeHtml(e.height ?? "?")}</span>`
    + `<span class="dur">${escapeHtml(e.duration_seconds ?? "?")} с</span>`
    + `</div>`
    + `<div class="info">`
    + `<div class="n"><span class="m ${ok ? "done" : "fail"}" aria-hidden="true"></span>`
    + `${escapeHtml(jobTag(job))}</div>`
    + `<div class="meta">${Number.isFinite(took) ? formatDuration(took) : "—"}`
    + ` · ${job.finished_at ? formatClock(new Date(job.finished_at)) : "—"}`
    + ` · код ${escapeHtml(code == null ? "?" : code)}</div>`
    + `<div class="link">${link}</div>`
    + (ok ? "" : `<div class="why">${escapeHtml(job.log_tail || "причина не записана")}</div>`)
    + `<div class="acts">`
    + `<button data-act="chat" data-id="${id}">Обсудить</button>`
    + `<button data-act="reveal" data-id="${id}">Показать в Finder</button>`
    + `<button data-act="dup" data-id="${id}">Копия</button>`
    + `<button data-act="delrun" data-id="${id}">Удалить</button>`
    + `</div>`
    + `</div>`
    + `</article>`;
}

/**
 * Одно слово о том, что с очередью, для показания в чроме.
 *
 * Четыре исхода, а не три (правка по ревью C2): до неё «идёт» стояло на любой непаузной
 * очереди при живом работнике — в том числе на пустой, где не идёт ничего, — и спорило с
 * соседней строкой «Ничего не считается» в той же чроме.
 *
 * «Свободна» и «стоит» — разные вещи, и путать их дорого: свободная очередь возьмёт первую же
 * поставленную задачу, стоящая не возьмёт (некому — работник не запущен или его не проверить).
 * Пауза называется отдельно и первой: она снимается кнопкой, остальные два — нет.
 */
export function queueStateWord({ paused, workerState, running, pending } = {}) {
  if (paused) return "на паузе";
  if (workerState !== "alive") return "стоит";
  return running || Number(pending) > 0 ? "идёт" : "свободна";
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
   ПРОЕКТЫ (Task 7)

   Список рисуется из `/api/state`'s собственного `"projects"` (task 6's
   `project_summary` — название/kind/этап/прогресс сцен, без похода на
   отдельный эндпоинт), панель — из `GET /api/projects/<id>` по клику и
   после каждого действия. Функции здесь чистые (без DOM/сети) ради тех же
   node-тестов, что проверяют остальной пул этого файла.
   =========================================================================== */

/** Подпись kind русским словом — единственное, чем бейдж в списке/шапке панели отличается по
 *  типу продукта (design spec, "Суть": ролик / клип на песню / просто mp3). Без цвета по
 *  типу — палитра страницы бережёт единственный акцент для «GPU считает», а kind не о
 *  вычислении. */
export const PROJECT_KIND_LABEL = { video: "ролик", clip: "клип", song: "песня" };

export function projectKindLabel(kind) {
  return PROJECT_KIND_LABEL[kind] || String(kind || "?");
}

/** Слово этапа для строки списка/шапки панели — из `stages`+`kind` одних (обе формы, сводка
 *  `project_summary` и полный `project.as_dict()`, несут оба поля одинаково — task 6 report,
 *  сомнение 6, явно предупреждает не полагаться на единообразие остального; здесь читается
 *  ровно то общее, что есть в обеих). M2 (task 6 fix round 1): `kind="song"` завершён ровно
 *  когда `stages.track === "approved"` — `stages.scenes`/`stages.assembly` к этому моменту уже
 *  `"done"`, но проверка идёт по `track`, а не по ним, чтобы не завязываться на деталь,
 *  которая для video/clip значит совсем другое. */
export function projectStageWord(project) {
  const stages = (project && project.stages) || {};
  const kind = project && project.kind;
  if (kind === "song") {
    if (stages.track === "approved") return "готово";
    if (stages.track === "running") return "трек пересчитывается";
    if (stages.track === "awaiting_approval") return "трек: ждёт прослушивания";
    if (stages.script === "approved") return "трек";
    if (stages.script === "awaiting_approval") return "сценарий: ждёт утверждения";
    return "сценарий";
  }
  if (kind === "clip") {
    if (stages.assembly === "done") return "готово";
    if (stages.assembly === "failed") return "сборка упала";
    if (stages.assembly === "running") return "сборка";
    if (stages.scenes === "running") return "сцены";
    if (stages.track === "running") return "трек пересчитывается";
    if (stages.track === "awaiting_approval") return "трек: ждёт прослушивания";
    if (stages.script === "approved") return "трек";
    if (stages.script === "awaiting_approval") return "сценарий: ждёт утверждения";
    return "сценарий";
  }
  // video
  if (stages.assembly === "done") return "готово";
  if (stages.assembly === "failed") return "сборка упала";
  if (stages.assembly === "running") return "сборка";
  if (stages.scenes === "running") return "сцены";
  if (stages.script === "awaiting_approval") return "сценарий: ждёт утверждения";
  return "сценарий";
}

/** «n/N» сцен для список-строки; клип/видео без сцен ещё (сценарий не утверждён, или клип
 *  ждёт трек) читаются пустой строкой — ноль из нуля не сообщает ничего, что не сказано уже
 *  словом этапа. `kind="song"` не имеет сцен вовсе (design spec: "просто mp3"). */
export function projectSceneProgressText(row) {
  if (!row || row.kind === "song") return "";
  const total = Number(row.scenes_total) || 0;
  if (!total) return "";
  return `${Number(row.scenes_done) || 0}/${total}`;
}

/* -- оценки времени: «сцены — как у обычных задач, песня — с её собственным ×13» ---------------
   Не второй источник истины поверх сервера: у задачи, которая уже стоит в очереди (в любом из
   четырёх списков — сцена может быть done/failed, не только pending/running), `estimate.seconds`
   уже посчитан сервером (той же формулой, что и обычная generate-задача для сцены;
   `worker.song_job_wallclock_estimate_seconds`'s собственный ×13 для трека) — и переиспользуется
   как есть, тем же `jobSeconds`, что рисует очередь. Формула ниже нужна ровно для сцен, которые
   ещё НЕ дошли до очереди (design spec: "Оценка длительности проекта = сумма оценок кусков" —
   без неё сумма считала бы только уже поставленные куски, а не весь проект). */

/** `assemble.DEFAULT_SCENE_CANVAS`/`DEFAULT_SCENE_STEPS` — те же числа, которыми
 *  `_scene_generate_args` реально снаряжает сцену: без выбора у формы (сцены проекта не проходят
 *  через обычную форму постановки), других чисел для ещё не поставленной сцены взять неоткуда. */
export const PROJECT_SCENE_CANVAS = { width: 896, height: 512 };
export const PROJECT_SCENE_STEPS = 8;

/** Третье по счёту место с этой же формулой (`h3_48gb.web.estimate`, `assemble.
 *  _scene_wallclock_estimate_seconds`) — намеренный, а не случайный дубль: та же причина, что
 *  `assemble.py`'s собственный докстринг называет для своего дубля («не тянуть зависимость ради
 *  одной формулы»), только здесь зависимость была бы на Python-модуль из браузера, чего нет и
 *  быть не может. Значения синхронизируются вручную — если формула когда-нибудь изменится в
 *  Python, эта копия должна измениться тем же коммитом. */
export function sceneWallclockEstimateSeconds(width, height, duration, steps) {
  const rows = (5.53 + 1.641 * (duration - 2.4)) * (width / 16) * (height / 16) + 81 * duration + 820;
  const secondsPerForward = 5.699e-3 * rows + 2.671e-7 * (rows ** 2);
  const diffusion = secondsPerForward * (steps - 1);
  const overhead = 36 + 7.44e-5 * width * height * duration;
  return diffusion + overhead;
}

/** Сумма оценок всех кусков проекта — сцены (job-эстимейт, если сцена уже стоит в очереди,
 *  иначе формула выше по её `duration`) плюс трек (job-эстимейт последней song-задачи этого
 *  проекта, найденной по `note` — `assemble.scene_note`'s сосед `"project track <id>"`,
 *  `_submit_project_song_job`, не имеет отдельного парсера на сервере, поэтому сравнивается
 *  здесь буквально). Ничего не оценивается до того, как соответствующая задача хоть раз легла в
 *  очередь (трек до approve сценария, клип-сцены до approve трека) — это честная сумма того, что
 *  реально известно, не прогноз того, что ещё не решено (см. task 7 report, "Сомнения"). */
export function projectEstimateSeconds(project, jobs) {
  const list = Array.isArray(jobs) ? jobs : [];
  const byId = {};
  for (const job of list) byId[job.id] = job;

  let total = 0;
  for (const scene of (project && project.scenes) || []) {
    const job = scene.job_id ? byId[scene.job_id] : null;
    total += job ? jobSeconds(job)
                : sceneWallclockEstimateSeconds(PROJECT_SCENE_CANVAS.width,
                                                 PROJECT_SCENE_CANVAS.height,
                                                 Number(scene.duration) || 0,
                                                 PROJECT_SCENE_STEPS);
  }
  if (project && (project.kind === "clip" || project.kind === "song")) {
    const note = `project track ${project.id}`;
    const trackJobs = list.filter((job) => job.note === note);
    const trackJob = trackJobs[trackJobs.length - 1];
    if (trackJob) total += jobSeconds(trackJob);
  }
  return total;
}

/** `<путь абсолютный под outdir>` → `/media/<относительный, посегментно закодированный>` —
 *  та же идея, что `mediaParts`/`clipUrl`, только без деления на `{run, stem}`: проектные файлы
 *  (клип сцены, `track/song.mastered.mp3`, `assembly/final.mp4`) лежат на произвольной глубине
 *  под `<outdir>/projects/<id>/...`, а `/media/<relative>` (`_media` в web.py) читает путь любой
 *  глубины начиная с task A6 — незачем угадывать, какой сегмент тут «run». Версии (`?v=`) нет:
 *  сценa retry всегда пишет новое случайное имя файла (`_scene_generate_args`'s собственный
 *  `secrets.token_hex(2)` в теге), поэтому иммутабельный кэш `/media` никогда не видит одно и то
 *  же имя дважды с разным содержимым для сцен; `final.mp4`/трек — исключение, см. task 7 report. */
export function projectMediaUrl(path, outdir) {
  const p = String(path == null ? "" : path);
  const base = String(outdir || "");
  if (!p || !base || !p.startsWith(`${base}/`)) return null;
  const relative = p.slice(base.length + 1);
  if (!relative) return null;
  return "/media/" + relative.split("/").map(encodeURIComponent).join("/");
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
    // Один код на два непохожих отказа: командная строка задачи, которую разобрал argparse
    // (и тогда есть `detail.stderr` с его собственной фразой), и любое «тело запроса не той
    // формы» — например `mode`, которого не знает генератор. Про второе «командная строка не
    // годится» — неправда, а страница обязана называть вещи своими именами.
    case "args_invalid":
      return detail.stderr
        ? { title: "Командная строка не годится", pre: detail.stderr }
        : { title: "Запрос не той формы", pre: error.message };
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
    // Отказ формы, а не чата, но встречают его там же — красной плашкой под кнопкой. Заголовок
    // называет оба выхода, потому что их ровно два, и поле «Таблица AdaLN» названо так, как оно
    // подписано на странице: искать «--adaln-cache» в форме негде.
    case "checkpoint_without_adaln":
      return { title: "У чекпойнта нет читаемой таблицы AdaLN — укажите её в поле «Таблица "
                    + "AdaLN» или почините transformer/adaln_cache.safetensors",
               pre: detail.cache || error.message };
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

    /* -- чат: девять кодов, каждый со своей развязкой ------------------------------------
       До этого места доходил только `default:` — «Отказ: chat_unreachable» английским кодом
       на русской странице, одинаковый для «модель не подняли», «провайдер молчит» и «файл
       сессии сломан», хотя чинятся они тремя разными действиями. Тексты разные ровно там,
       где разное следующее действие человека. */
    case "chat_not_found":
      return { title: "Такой сессии чата больше нет — начните новую", pre: null };
    case "chat_busy":
      return { title: "Ход уже идёт — дождитесь ответа модели", pre: null };
    case "chat_corrupt":
      return { title: "Файл сессии повреждён — почините его или начните новый чат",
               pre: detail.path || error.message };
    case "bad_image":
      // Условия — в заголовке, а не только в сообщении сервера под ним: заголовок читают первым,
      // а иногда и единственным, и «кадр не годится» без единого условия оставляет человека
      // гадать. Длину имени файла тут не поминаем — она больше не причина отказа, имя просто
      // укорачивается (`sanitize_upload_name`).
      return { title: "Кадром может быть png, jpg или webp до 16 МБ (разрешение любое)",
               pre: error.message };
    case "gpu_busy":
      return { title: "Идёт прогон — локальная модель поднимется после него", pre: null };
    case "provider_unavailable":
      // `reason` из росписи («нет токена OPENROUTER_API_KEY») сервер уже положил в message.
      return { title: "Провайдер недоступен", pre: error.message };
    case "llama_did_not_start":
      return { title: "llama-server не поднялся — хвост его лога ниже", pre: error.message };
    case "chat_unreachable":
      return { title: "Провайдер не ответил — проверьте адрес и что он запущен",
               pre: error.message };
    case "bad_provider_reply":
      // Не то же, что `bad_model_json`: там модель не удержала схему, тут ходом не ответили
      // вовсе — так отвечает OpenRouter, когда падает уже его собственный апстрим.
      return { title: "Провайдер ответил не ходом — это его отказ, не модели",
               pre: error.message };
    case "bad_model_json":
      return { title: "Модель не удержала формат ответа — попробуйте ещё раз или смените "
                    + "провайдера", pre: error.message };

    default:
      return { title: error.code ? `Отказ: ${error.code}` : "Запрос не прошёл",
               pre: error.message || null };
  }
}

/* ===========================================================================
   ПЛАШКА ВЫГРУЗКИ

   Задача 6 учит работника не браться за следующую задачу, пока жив порт llama:
   поднятая модель и непустая очередь означают не «сейчас начнётся», а «стоит»,
   и разговор в модалке — не единственный способ снять модель с GPU, плашка
   в очереди — второй, для того, кто вообще не собирался открывать чат.
   =========================================================================== */

/**
 * Чистая функция: `{pending, llm, paused}` — сколько задач ждёт, в каком состоянии локальная
 * модель (`/api/llm`'s `status`) и стоит ли сама очередь (`/api/state`'s `paused`) — в решение,
 * показывать ли плашку.
 *
 * `llm !== "up"` включает и `"down"`, и `"busy"`, и внешнего провайдера, у
 * которого `/api/llm` тоже отвечает `down` (см. `_llm_state` в `web.py`):
 * ни то ни другое не держит память этой машины, снимать нечего.
 *
 * `paused` гасит плашку раньше, чем до неё доходит очередь: A5 (пауза/старт очереди) — работник
 * не берёт задачи, пока `is_paused`, так что «выгрузить и начать генерацию» на паузе — совет,
 * которому нечего начинать, и он лишь путает человека, у которого очередь стоит по его же
 * собственному решению, не по занятому GPU.
 */
export function unloadBanner(state) {
  const pending = Number(state && state.pending) || 0;
  const llm = state && state.llm;
  const paused = Boolean(state && state.paused);
  if (paused) return { show: false };
  if (pending > 0 && llm === "up") {
    return { show: true,
             text: "Модель в памяти держит GPU — выгрузить и начать генерацию?" };
  }
  return { show: false };
}

/** Ключ состояния, по которому «пусть ждёт» узнаёт, что ждать больше нечего:
 *  тот же `{pending, llm}`, что решает, показывать ли плашку. */
export function bannerKey(state) {
  return `${Number(state && state.pending) || 0}:${(state && state.llm) || ""}`;
}

/**
 * Показывать ли плашку прямо сейчас, при фиксированном «отклонено на ключе X».
 *
 * Не то, чем страница пользуется (см. `nextBannerState`): сравнение по одному застывшему
 * `dismissedKey` не замечает, что состояние успело уйти от отклонённого значения и вернуться —
 * оно смотрит только «совпадает ключ сейчас или нет», а не «был ли между двумя одинаковыми
 * ключами хоть один другой». Оставлена как более простой строительный блок и потому, что раунд
 * ревью нашёл дыру именно в этом различии — пусть тест на неё виден рядом с тем, что было не так.
 */
export function unloadBannerVisible(state, dismissedKey) {
  return unloadBanner(state).show && bannerKey(state) !== dismissedKey;
}

/**
 * Переход дисмисс-состояния плашки на один опрос — то, чем страница пользуется на самом деле.
 *
 * `prev` — `{dismissedKey}`, посчитанный на предыдущем опросе (или `{dismissedKey: null}` до
 * первого клика «пусть ждёт»); `state` — свежий `{pending, llm, paused}`. Отличие от
 * `unloadBannerVisible` ровно в том, что тут нашло ревью первого раунда: `dismissedKey` не
 * переживает уход состояния от себя. Без сгорания цикл «2 задачи, up → пусть ждёт → 3 задачи
 * (плашка снова видна, задачу добавили) → одну из трёх удалили, снова 2 задачи, up» молча гасил
 * предупреждение во второй раз — ключ `"2:up"` совпадал с тем, что отклонили в первый раз, хотя
 * именно это повторное появление никто не отклонял. Сгорание чинит это: как только ключ хоть раз
 * разошёлся с `dismissedKey`, старый дисмисс забывается, и попадание обратно в то же
 * `{pending, llm}` снова спрашивает. `paused` не входит в `bannerKey` — она гасит `show` через
 * `unloadBanner` напрямую (см. его докстринг), а ключ дисмисса остаётся про то же `{pending, llm}`
 * и после того, как пауза снята, помнит именно то, что было отклонено при её снятии.
 */
export function nextBannerState(prev, state) {
  const key = bannerKey(state);
  const previous = (prev && prev.dismissedKey) ?? null;
  const dismissedKey = previous !== null && previous !== key ? null : previous;
  return { dismissedKey, show: unloadBanner(state).show && dismissedKey !== key };
}

/* ===========================================================================
   ФОРМА
   =========================================================================== */

/**
 * Значения, к которым «Новая задача» возвращает форму: поле — значение.
 *
 * Форма намеренно не очищается сама после постановки (см. `advanceAfterSubmit`): за вечер сюда
 * кладут пять задач, меняя по одному полю. Ровно поэтому и нужен явный выход — накидав ночную
 * пачку, следующую задачу человек начинает с чужого промпта, чужого кадра и чужого тега и
 * вычищает их руками, по одному полю, ничего при этом не пропустив только по везению.
 *
 * **Здесь только сочинение, и это единственная граница, которую надо помнить.** Рецепт —
 * чекпойнт, LoRA с её силой, таблица AdaLN, число шагов, папка вывода — не в этом списке и не
 * должен в нём оказаться: он один и тот же месяцами, и его повторный набор был бы той самой
 * работой, ради отмены которой форма не чистится сама. По той же причине сброс не делается
 * перезагрузкой страницы: та унесла бы и рецепт, и открытый диалог.
 *
 * Чистая функция, а не запись прямо в поля: список значений, проверяемый только глазами, — это
 * список, который разъедется с разметкой (`test_the_reset_defaults_are_the_ones_the_page_...`
 * сверяет каждое значение с `value=` в `index.html`).
 */
export function resetFormState() {
  return {
    prompt: "",
    "prompt-file": "",      // «— промпт набран здесь —», см. `loadPromptList`
    image: "",
    "end-image": "",
    tag: "run",
    seed: "0",
    mode: "t2va",
    duration: "10",
    "canvas-preset": "small",
    // Числа под пресетом ставятся вместе с ним: `applyCanvasChoice` их перепишет, но форма
    // обязана быть согласованной и до того, как что-нибудь её пересчитает.
    width: "896",
    height: "576",
  };
}

/** Можно ли выводить канвас из кадра: есть режим с кадром и есть сам кадр.
 *
 *  Пункт «из кадра» без кадра — обещание, которое некому выполнить: CLI не найдёт, из чего
 *  выводить, подставит `DEFAULT_CANVAS` и посчитает молча не то. Отдельная чистая функция, потому
 *  что ответ нужен в трёх местах (доступность пункта, `buildArgs`, автопереключение после
 *  загрузки кадра), и разъехавшись они дадут ровно ту тихую ошибку, от которой пункт заведён. */
export function autoCanvasAllowed(mode, image) {
  return (mode === "i2v" || mode === "flf") && Boolean((image || "").trim());
}

/** Подпись оценки про выведенный канвас — пустая, если канвас выбран руками.
 *
 *  Пресет человек видит в выпадашке; выведенный из кадра он не выбирал и увидеть ему негде, а
 *  разрешение будущего ролика — не та вещь, которую узнают по файлу на диске. */
export function canvasNote(fromImage, estimate) {
  if (!fromImage || !estimate) return "";
  const { width, height } = estimate;
  if (!width || !height) return "";
  return `из кадра: ${width}×${height}`;
}

/**
 * Список аргументов для `POST /api/jobs` (или `/api/estimate`, если
 * `withPrompt` снят: оценке промпт не нужен, а слать килобайты текста на
 * каждое нажатие клавиши незачем).
 *
 * `--width`/`--height` уходят всегда, **кроме** «из кадра (авто)»: `resolve_canvas` в CLI
 * выводит канвас из кадра (аспект цел, кратность 32, аспект 1:4..4:1) ровно тогда, когда не
 * получил ни того, ни другого — с одним из двух он отказывается (`partial_canvas_with_image`),
 * с обоими берёт их как есть. Форма слала оба всегда, поэтому автовывод из веба был недостижим:
 * вертикальную картинку молча растягивало в горизонтальный канвас пресета.
 */
export function buildArgs(form, { withPrompt = true } = {}) {
  const args = ["generate"];
  if (withPrompt) {
    if (form.promptFile) args.push("--prompt-file", form.promptFile);
    else if (form.prompt) args.push(form.prompt);
  }
  // Условие смотрит и на кадр, а не только на выбранный пункт: выпадашка могла остаться на
  // «из кадра» после того, как кадр убрали, и молчаливый `DEFAULT_CANVAS` — худший исход из всех.
  if (!(form.canvasFromImage && autoCanvasAllowed(form.mode, form.image))) {
    args.push("--width", String(form.width), "--height", String(form.height));
  }
  args.push("--duration", String(form.duration), "--steps", String(form.steps),
            "--seed", String(form.seed), "--tag", form.tag,
            "--mode", form.mode,
            "--checkpoint", form.checkpoint, "--outdir", form.outdir);
  if (form.lora) args.push("--turbo-lora", form.lora, "--turbo-strength", String(form.loraStrength));
  if (form.adaln) args.push("--adaln-cache", form.adaln);
  if (form.image) args.push("--image", form.image);
  if (form.endImage) args.push("--end-image", form.endImage);
  return args;
}

/** Текст зоны загрузки кадра (A7): пусто — приглашение перетащить или выбрать файл, что-то
 *  загружено (или вписано вручную) — имя файла. Чистая функция состояния зоны, а не поля ввода
 *  напрямую: `state` — `{name}` c возможно отсутствующим или пустым `name`, ровно то, что
 *  `updateUploadZone` (ниже, в DOM-половине) строит из текущего пути в поле, а node-тест — из
 *  ничего вообще. */
export function uploadZoneLabel(state) {
  const name = (state && state.name) || "";
  // Пустое состояние называет условия целиком, а не приглашает молча. Отказ по этой зоне
  // приходит одной строкой `bad_image`, и человек, у которого кадр не взяли, иначе гадает между
  // форматом, размером и разрешением — при том, что разрешение тут не ограничено вовсе
  // (канвас выводится из кадра, аспект сохраняется).
  return name || "перетащи картинку или выбери файл — png, jpg, webp до 16 МБ, разрешение любое";
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
 *
 * `turn.slug` (A4) следует тому же правилу, что и `session["slug"]` на сервере
 * (`web._locked_turn`): ход, ничего не назвавший, не стирает то, что назвал предыдущий —
 * `state.slug` переписывается только непустой строкой.
 */
export function applyTurn(state, turn) {
  const answer = turn || {};
  if (!Array.isArray(state.log)) state.log = [];
  state.log.push({ role: "assistant", text: String(answer.reply == null ? "" : answer.reply) });
  if (answer.prompt) state.promptText = buildPromptText(answer.prompt);
  if (typeof answer.slug === "string" && answer.slug) state.slug = answer.slug;
  // Task 7 ("Проекты"): `project`, like `slug` above, is optional metadata a turn may or may not
  // answer with (task 5's `PROMPT_SCHEMA`, "project" outside the top-level `required`) -- present
  // only once the model has actually settled on a project idea, never cleared by a later turn
  // that simply did not repeat it (the server's own session keeps the last one the same way,
  // `_chat_message` in web.py: `session["project"]` is only ever overwritten by a *new* dict,
  // never reset to `null` by its absence).
  if (answer.project && typeof answer.project === "object") state.project = answer.project;
  return state;
}

/** Кириллица → латиница, буква в букву. Тег ограничен `[a-z0-9-]`
 *  (см. `heuristicSlug`), и это единственное место, где кириллица в него превращается. */
const SLUG_TRANSLIT = {
  а: "a", б: "b", в: "v", г: "g", д: "d", е: "e", ё: "e", ж: "zh", з: "z", и: "i",
  й: "y", к: "k", л: "l", м: "m", н: "n", о: "o", п: "p", р: "r", с: "s", т: "t",
  у: "u", ф: "f", х: "h", ц: "ts", ч: "ch", ш: "sh", щ: "sch", ъ: "", ы: "y",
  ь: "", э: "e", ю: "yu", я: "ya",
};

/** Слова, которые `heuristicSlug` пропускает: не суть сцены, а разметка и грамматика вокруг
 *  неё. `[Shot 1]` вырезается отдельным проходом (см. ниже) — здесь только стиль-слова, которыми
 *  формат открывает `[Shot 1]` (docs/h3-prompt-system.md, «Cinematic, live-action, ...»), и три
 *  английских артикля. */
const SLUG_SKIP_WORDS = new Set(["live-action", "cinematic", "a", "an", "the"]);

/**
 * Итоговая чистка любого кандидата в тег — общая точка для `heuristicSlug` (слова уже почти
 * чистые, собраны из латиницы/кириллицы промпта) и для `slug`, который прислала модель (A4):
 * `PROMPT_SCHEMA` не накладывает на него никакого ограничения по алфавиту, это произвольный
 * текст, и оба в итоге становятся `#tag`. Транслитерирует кириллицу, стягивает любой пробег
 * символов вне `[a-z0-9-]` в одиночный дефис, срезает висящие дефисы по краям и по 24-символьному
 * пределу (тому же, которым `heuristicSlug` режет собранные слова).
 */
export function normalizeSlug(text) {
  const lower = String(text == null ? "" : text).toLowerCase();
  const romanized = Array.from(lower)
    .map((ch) => (ch in SLUG_TRANSLIT ? SLUG_TRANSLIT[ch] : ch))
    .join("");
  const cleaned = romanized.replace(/[^a-z0-9-]+/g, "-").replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned.slice(0, 24).replace(/-+$/, "");
}

/**
 * Эвристический тег из промпта, когда модель не прислала свой `slug` (A4).
 *
 * Источник текста — поле `integrated_multimodal_description`, размеченное так же, как его читает
 * подсветка (`fieldValue`): если заголовок поля есть, берётся текст до следующего заголовка;
 * иначе (промпт ещё не разбит на поля) читается весь текст как есть. `[Shot N]` и стиль-слова, с
 * которых `[Shot 1]` начинается по документу, значимого о сцене не говорят и пропускаются вместе
 * с артиклями; первые три оставшихся слова склеиваются дефисом и уходят через `normalizeSlug` —
 * ту же чистку и тот же 24-символьный предел, что и у слага модели.
 */
export function heuristicSlug(promptText) {
  const text = String(promptText == null ? "" : promptText);
  const field = fieldValue(text, "integrated_multimodal_description");
  const body = (field ? field.text : text).replace(/\[Shot\s+\d+\]/gi, " ");
  const words = body.match(/[\p{L}\p{N}]+(?:-[\p{L}\p{N}]+)*/gu) || [];
  const picked = [];
  for (const raw of words) {
    if (SLUG_SKIP_WORDS.has(raw.toLowerCase())) continue;
    picked.push(raw);
    if (picked.length === 3) break;
  }
  return normalizeSlug(picked.join("-"));
}

/**
 * Тег поля `#tag` после «в Редактор» (A4, fix round 1 — до этого слаг сессии подменял тег
 * безусловно, стирая то, что человек вписал сам).
 *
 * «Слаг = имя по умолчанию, не диктат» — тот же принцип, по которому `submit()` трогает `#tag`
 * только при пустом/`"run"` значении (см. `heuristicSlug`'s own call site). Заменяется только
 * тег, который сейчас пуст, равен `"run"`, или равен `lastAutoTag` — тому автослагу, который эта
 * же функция сама подставила в прошлый раз и который человек с тех пор не трогал руками. Любой
 * другой текст в поле — чья-то правка, и остаётся как есть.
 */
export function tagFromSessionSlug(currentTag, slug, lastAutoTag) {
  const clean = normalizeSlug(slug);
  if (!clean) return currentTag;
  const current = String(currentTag == null ? "" : currentTag).trim();
  const overwritable = !current || current === "run" || current === lastAutoTag;
  return overwritable ? clean : currentTag;
}

/**
 * A4, fix round 2: где `lastAutoTag` (fix round 1) живёт между двумя открытиями модалки одной и
 * той же сессии.
 *
 * `chat` умирает на каждом `closeChat()` — и `finishChat` закрывает модалку безусловно, на всех
 * трёх путях. Сессию можно открыть заново (`#chat/<id>` тот же), и `enterChat` строит для неё
 * совсем новый объект `chat` — своего `lastAutoTag` у него по построению больше нет, даже если
 * предыдущее «в Редактор» только что что-то в `#tag` положило. Без памяти снаружи `chat` вторая
 * подстановка на той же сессии не отличила бы «это моя прошлая работа, можно двигать дальше» от
 * «человек вписал именно это руками» — и застряла бы на первом слаге навсегда, что и произошло
 * (review round 2). Ключ — id сессии, а не что-то более глобальное: тег одной сессии не имеет
 * права решать судьбу тега другой. Перезагрузка страницы обнуляет её вместе со всем остальным
 * состоянием страницы (`chat`, `state`, содержимое `#tag` в DOM) — тем же самым, чем эта память и
 * должна быть ограничена.
 */
const autoTagBySession = {};

/** Запомнить `tag` как последнюю автоподстановку сессии `sid` в `map`. Принимает карту
 *  параметром (а не читает `autoTagBySession` сама) — так тестируема без module-level состояния
 *  и без DOM, своей собственной картой. */
export function rememberAutoTag(map, sid, tag) {
  if (sid) map[sid] = tag;
  return map;
}

/** Что сессия `sid` получала автоподстановкой в прошлый раз, по данным `map` — или `""`, если
 *  ничего (сессия только что открыта впервые, или это вообще не сессия). */
export function recallAutoTag(map, sid) {
  return (sid && map[sid]) || "";
}

/** Предупреждение хода — по коду, как и отказ: сервер шлёт `{code, message}` именно затем,
 *  чтобы страница не разбирала его предложение. Ход при этом состоялся — просто без кадра. */
const WARNING_TEXT = {
  image_not_found: "Кадр не нашёлся — ход ушёл без картинки",
  image_unreadable: "Кадр не прочитался — ход ушёл без картинки",
  bad_image: "Кадр не годится в картинку — ход ушёл без него",
};

export function chatWarningText(warning) {
  const it = warning || {};
  const head = WARNING_TEXT[it.code] || "Ход прошёл с оговоркой";
  return it.message ? `${head}: ${it.message}` : head;
}

/* Адрес — часть состояния модалки. Обрыв связи или перезагрузка страницы во время хода не
   теряет разговор: сессия лежит в `<outdir>/chat/<id>.json`, а `#chat/<id>` открывает её
   заново с историей с диска. Идентификатор — `secrets.token_hex(4)`, шестнадцатеричный. */
export const CHAT_HASH = /^#chat\/([0-9a-f]+)$/;

/**
 * Что делать с адресом: открыть сессию — или ничего.
 *
 * Закрывать — никогда. Раньше адрес, переставший быть `#chat/<id>`, закрывал окно, и шаг «назад»
 * в браузере уносил несохранённую правку промпта так же тихо, как это делали Esc и подложка.
 * Теперь окно закрывает только кнопка, а `closeChat` сам обнуляет `chatWanted`, не дожидаясь
 * события об адресе, — иначе та же сессия после закрытия не открылась бы второй раз.
 *
 * `current` — сессия, которая уже открыта **или прямо сейчас открывается**, и второе слово тут
 * и есть весь смысл. Открытие модалки — это `location.hash = "#chat/<id>"`, а браузер ставит
 * `hashchange` в очередь, а не зовёт обработчик тут же; страница поэтому вызывала синхронизацию
 * руками сразу после присваивания. Сторож при этом смотрел на `chat`, который появляется только
 * *после* `await` за сессией, — то есть в окне ожидания был пуст, `hashchange` успевал прийти
 * ровно в него, и та же сессия читалась вторым GET и рисовалась второй раз (в ленте это видно
 * как мигание, а первый ход мог уйти в перетёртое состояние).
 *
 * Отдельная чистая функция, а не условие внутри обработчика: сторож от гонки, проверяемый только
 * глазами, — это тот же сторож, которого не было.
 */
export function chatHashAction(hash, current) {
  const match = CHAT_HASH.exec(hash || "");
  if (!match) return { act: "nothing" };
  if (match[1] === current) return { act: "nothing" };
  return { act: "enter", id: match[1] };
}

/** Реплика по умолчанию для хода, в который брошена картинка без единого слова (A8, требование
 *  2) — видна в ленте как обычная реплика `user`, а не выдумывается молча: человек должен знать,
 *  о чём в итоге спросили модель. Текст — та же формулировка, что и абзац для модели в
 *  `docs/h3-prompt-system.md` («опиши её и предложи промпт от неё»), только от первого лица
 *  просьбы, а не от лица инструкции. */
export const DEFAULT_IMAGE_TURN_TEXT = "Опиши кадр и предложи промпт от него";

/**
 * Тело хода, приложенного кадром (A8) — чистая функция, которую `sendChatMessage` зовёт перед
 * `api()`, чтобы собрать `image`/`set_mode`/(при нужде) `text` для тела запроса.
 *
 * `state` — `{text, pendingImage, mode}`: `text` уже обрезан (`.trim()`), `pendingImage` —
 * `{path, name}` только что загруженного через `/api/uploads` кадра или `null` (ничего не
 * приложено), `mode` — текущий режим сессии (`chat.mode`), из того же списка, что и
 * `web.CHAT_MODES` на сервере.
 *
 * Без кадра — пустой объект: обычный ход ничего не примешивает к телу, которое строит сам
 * `sendChatMessage`. С кадром: `image` — путь, который вернул аплоад, и он уходит всегда; `text`
 * подставляется, только когда поле ввода было пустым (см. `DEFAULT_IMAGE_TURN_TEXT`) — печатный
 * текст едет как есть, без правки. `set_mode: "i2v"` появляется, только если режим сессии ещё
 * не решил, каким кадром пользоваться (пустой или `t2va`); `i2v`/`flf` не трогаются — кадр там
 * уже учтён, и незачем переписывать то, что сервер и так знает.
 */
export function attachmentBody(state) {
  const pending = (state && state.pendingImage) || null;
  if (!pending || !pending.path) return {};
  const body = { image: pending.path };
  const mode = (state && state.mode) || "";
  if (!mode || mode === "t2va") body.set_mode = "i2v";
  const text = String((state && state.text) || "").trim();
  if (!text) body.text = DEFAULT_IMAGE_TURN_TEXT;
  return body;
}

/**
 * Плейсхолдер-запись хода, пока ответ модели не пришёл.
 *
 * Сервер синхронный (см. докстринг `_chat_message`) и до самого ответа не присылает ничего —
 * стадии на клиенте больше взять неоткуда, кроме последнего известного `llmStatus`. `"down"` —
 * единственный статус, за которым стоит настоящее ожидание (холодный старт может занять минуту),
 * остальное (`"up"`, `"busy"`, пусто) получает менее тревожный текст. `kind: "pending"` — метка,
 * по которой `landTurn`/`landFailure` находят и убирают эту запись, когда ответ (или отказ)
 * приземлился; `role: "note"` — чтобы `renderChatLog` собрал ей тот же CSS-класс, что и другим
 * служебным строкам ленты (`warn`/`bad`).
 */
export function pendingEntry(llmStatus) {
  return { role: "note", kind: "pending",
           text: llmStatus === "down" ? "поднимаю модель…" : "модель думает…" };
}

/**
 * Что должно оказаться в поле ввода после отказа хода — `typed` (несостоявшаяся реплика) или то,
 * что там уже есть.
 *
 * Кнопка «отправить» не блокируется на время хода (см. докстринг `sendChatMessage`), а холодная
 * модель может думать до минуты — этого достаточно, чтобы человек успел начать новый черновик,
 * пока старая реплика ещё летит и может отказать. Затирать этот черновик старым текстом молча
 * нельзя: старый текст и так виден в ленте строкой `user`, под которой легла запись об отказе, а
 * поле ввода принадлежит тому, что печатается *сейчас*. Поэтому `typed` возвращается только в
 * пустое поле — если там уже что-то есть, оно остаётся как есть.
 */
export function restoredInput(currentValue, typed) {
  return currentValue ? currentValue : typed;
}

/** Убирает плейсхолдер хода (см. `pendingEntry`) из лога состояния, если он там есть. По
 *  ссылке и безусловно — до сторожа "чужая сессия", а не после: ответ, приземлившийся не туда,
 *  всё равно обязан не оставить вечные точки на объекте состояния, который их получил. */
function dropPending(state) {
  if (state && Array.isArray(state.log)) {
    state.log = state.log.filter((entry) => entry.kind !== "pending");
  }
}

/**
 * Ответ хода — в ту сессию, которая его и просила, или никуда.
 *
 * Ход длится десятки секунд, и всё это время модалку можно закрыть (Esc, подложка, «закрыть»)
 * и открыть заново на другой сессии. Оба исхода были живыми ошибками. Ответ, пришедший на
 * закрытую модалку, ронял `TypeError` внутри `try` самого `await`, `catch` падал повторно на
 * `chat.log`, и `finally` не успевал вернуть кнопку «отправить» — она оставалась мёртвой до
 * перезагрузки. Ответ, пришедший на *другую* сессию, дописывал ей чужую реплику и переписывал
 * её окно промпта.
 *
 * Правило одно: ответ принадлежит тому объекту состояния, который его заказал. Сравнение по
 * ссылке, а не по `id`: закрыть и открыть ту же сессию заново — это тоже новое состояние, и
 * лента у него уже перечитана с диска.
 *
 * `savedText` двигается вместе с окном: ответ модели — не «несохранённая правка руками»
 * (см. `hasUnsavedEdits`).
 */
export function landTurn(current, expected, answer) {
  dropPending(expected);
  if (!current || current !== expected) return false;
  applyTurn(current, answer);
  const warning = answer && answer.warning;
  if (warning) current.log.push({ role: "note", kind: "warn", text: chatWarningText(warning) });
  current.savedText = current.promptText;
  return true;
}

/**
 * Отказ хода — туда же, куда лёг бы ответ, и по тому же правилу (см. `landTurn`).
 *
 * Реплика уходит из ленты обратно: сервер её не записал, а в поле ввода она осталась. И
 * состояние модели ставится по коду: `gpu_busy` — это `busy` («идёт прогон»), а не `down`
 * («поднимется при первом сообщении»); плашка со вторым текстом прямо над записью о прогоне
 * отвечает на тот же вопрос второй раз и неверно.
 */
export function landFailure(current, expected, payload, runningSeconds = 0) {
  dropPending(expected);
  if (!current || current !== expected) return false;
  if (!Array.isArray(current.log)) current.log = [];
  const last = current.log[current.log.length - 1];
  if (last && last.role === "user") current.log.pop();
  current.log.push({ role: "note", kind: "bad",
                     text: chatFailureText(payload, runningSeconds) });
  const code = ((payload && payload.error) || {}).code;
  current.llmStatus = code === "gpu_busy" ? "busy" : "down";
  return true;
}

const LLM_TEXT = {
  up: "модель поднята",
  down: "модель не поднята — поднимется при первом сообщении",
};

/**
 * Текст плашки модели.
 *
 * `external` — активен провайдер в интернете: тридцать гигабайт держит только локальная модель,
 * `/api/llm` для внешнего честно отвечает `down`, но «модель не поднята» на openrouter читается
 * как «что-то не готово», хотя готово всё.
 *
 * `busy` — GPU занят прогоном; остаток берётся из того же прогресса работника, что печатается
 * в приборной строке. Прогон без оценки молчит, а не обещает «~0 мин».
 */
export function llmPlateText(status, { external = false, runningSeconds = 0 } = {}) {
  if (external) return "внешний провайдер — память этой машины не занимает";
  if (status === "busy") {
    const left = Number(runningSeconds) || 0;
    return "идёт прогон — модель поднимется после него"
         + (left > 0 ? ` (~${formatDuration(left)})` : "");
  }
  return LLM_TEXT[status] || "состояние модели неизвестно";
}

/**
 * Есть ли в окне работа, которой нет больше нигде.
 *
 * Сессия на диске хранит ответы модели (`prompt_struct`) и текст, с которым её открыли, — и
 * ничего между ними: правка руками живёт только в браузере. Esc и клик по подложке — один
 * жест, поэтому перед ними этот вопрос задаётся, а после ответа модели (`landTurn` двигает
 * `savedText`) — нет: спрашивать о том, что и так сохранено, значит научить отвечать «да» не
 * читая.
 */
export function hasUnsavedEdits(state) {
  if (!state) return false;
  return String(state.promptText || "") !== String(state.savedText || "");
}

/**
 * Причина, по которой кнопку завершения нажимать нельзя, или `null`.
 *
 * Пустое окно одним кликом обнуляло файл в `prompts/` и ставило в очередь `["generate", ""]`.
 * Ни то, ни другое не отменяется, и ни то, ни другое не могло быть намерением: разговор,
 * который нечем закончить, — это разговор, который ещё не начался.
 */
export function finishRefusal(state) {
  if (!state) return null;
  return String(state.promptText || "").trim() === ""
    ? "Окно промпта пустое — сохранять нечего."
    : null;
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
   ТЕМА (task C1)
   Три состояния, в этом порядке по кругу: системная (нет атрибута — решает
   браузер через `prefers-color-scheme`), тёмная, светлая. "system" хранится
   как отсутствие `data-theme`, а не как строка "system" на `<html>` -- это
   то же самое состояние, в котором страница жила до появления переключателя,
   так что уже сохранённый (или вовсе отсутствующий) выбор не ломается.
   =========================================================================== */
const THEME_ORDER = ["system", "dark", "light"];
export const THEME_STORAGE_KEY = "h3-theme";

/** Следующее состояние по кругу. Значение, которого нет в списке (будущая
 *  версия переключателя, или подправленный вручную localStorage), не
 *  заклинивает цикл -- оно просто считается началом цикла, "system". */
export function nextTheme(current) {
  const i = THEME_ORDER.indexOf(current);
  return THEME_ORDER[(i + 1) % THEME_ORDER.length];
}

/** Русская подпись состояния для кнопки в шапке. Тот же откат к "системная",
 *  что и в `nextTheme` -- незнакомое значение не должно молчать пустой строкой. */
export function themeLabel(value) {
  if (value === "dark") return "Тёмная";
  if (value === "light") return "Светлая";
  return "Системная";
}

/** Ставит `data-theme` на `<html>` и запоминает выбор. DOM-эффект, поэтому не
 *  чистая функция -- но, как и остальная страница ниже, безопасна для импорта
 *  вне браузера: она не вызывается сама по себе, только по клику или на
 *  старте `startPage()`. */
export function applyTheme(value) {
  const html = document.documentElement;
  if (value === "dark" || value === "light") {
    html.setAttribute("data-theme", value);
  } else {
    html.removeAttribute("data-theme");
  }
  try {
    localStorage.setItem(THEME_STORAGE_KEY, value);
  } catch {
    /* приватный режим или заполненная квота -- тема всё равно применилась к этому показу */
  }
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

  // -- тема -----------------------------------------------------------------------------
  // Значение уже применено синхронно инлайновым скриптом в <head> (до отрисовки CSS, чтобы
  // не мигнуть системной темой при сохранённом ручном выборе) -- здесь только подпись кнопки
  // и подписка на клик. `applyTheme` безопасно вызвать второй раз: он идемпотентен.
  (function initTheme() {
    let saved = "system";
    try { saved = localStorage.getItem(THEME_STORAGE_KEY) || "system"; } catch { /* см. applyTheme */ }
    applyTheme(saved);
    const button = $("theme-toggle");
    const label = $("theme-toggle-label");
    // "system" на <html> нет атрибутом (см. applyTheme) -- getAttribute тогда вернёт null,
    // а nextTheme(null) не найдёт его в THEME_ORDER и не сдвинется с места.
    const current = () => document.documentElement.getAttribute("data-theme") || "system";
    const render = () => { label.textContent = themeLabel(current()); };
    render();
    button.addEventListener("click", () => {
      applyTheme(nextTheme(current()));
      render();
    });
  })();

  // `mode` подписан отдельно: у него сверх пересчёта есть своя работа — показать
  // или спрятать поля кадров.
  const FIELDS = ["width", "height", "duration", "steps", "seed", "tag",
                  "ckpt", "lora", "lora-str", "adaln", "outdir", "image", "end-image"];

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
  let llmStatus = "";          // последний известный `/api/llm`'s `status` — своя переменная,
                                // отдельная от `chat.llmStatus` модалки, чтобы не путать их опрос
  // Дисмисс плашки выгрузки — состояние `nextBannerState` переносит с опроса на опрос, а не
  // застывший ключ: без этого возврат к уже отклонённому `{pending, llm}` после промежуточного
  // изменения молча гасил бы предупреждение, которое в этот раз никто не отклонял.
  let bannerState = { dismissedKey: null };
  let unloadBannerInput = { pending: 0, llm: "" };  // вход последней отрисовки — им отвечает клик
  /* Заметка правящейся задачи (C2, требование 4). Поля ввода у неё больше нет: за всё время
     существования формы её никто не заполнял, а место она занимала в самом видном углу. Сама
     заметка при этом жива — сервер её хранит, копия наследует, и `pendingRowHtml` её рисует, —
     поэтому правка задачи обязана вернуть на `PUT /api/jobs/<id>` ту же строку, с которой
     пришла, а не пустую: иначе «Править» тихо стирала бы чужой текст. Новая задача уходит с
     пустой заметкой, как и раньше уходила с пустым полем. */
  let formNote = "";

  // -- проекты (Task 7) --------------------------------------------------------------------
  let project = null;          // {id, project: {...as_dict()}, active_job} панели, или null
  let projectBusy = false;     // идёт запрос, меняющий проект — та же роль, что `busy` у очереди
  let projectMp3 = null;       // {path, name} — mp3, загруженный для импорта трека клипа
  let projectMp3TrackSource = "generate";  // радио «Сделать проектом» для kind=clip

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
    adaln: $("adaln").value.trim(),
    outdir: $("outdir").value.trim(),
    note: formNote,
    prompt: $("prompt").value,
    image: $("image").value.trim(),
    endImage: $("end-image").value.trim(),
    promptFile: promptFileArg(),
    canvasFromImage: autoCanvasChosen(),
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
    // Опрашивается вместе с `/api/state`, а не только когда открыта модалка чата: очередь
    // стоит именно тогда, когда её никто не смотрит через диалог, и плашке нужно собственное
    // знание о модели, не завязанное на `chat` (см. `unloadBanner`).
    try {
      llmStatus = (await api("GET", "/api/llm")).status;
    } catch {
      llmStatus = "";
    }
    // Outside the `try`: the prompt list has its own route and its own failure, and it is
    // refreshed on every poll rather than once at startup because prompts are written into
    // `prompts/` by hand while the page is open.
    await loadPromptList($("prompt-file").value);
    renderConnection();
    renderQueue();
    renderProjects();
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

    const paused = Boolean(state.paused);
    const running = (queue.running || [])[0] || null;
    const pending = queue.pending || [];

    // -- чрома: три показания рядом, каждое в два слова (макет: `.readout`)
    $("rail").dataset.worker = workerState;
    $("worker-state").textContent = {
      alive: "запущен", stopped: "не запущен", unknown: "неизвестно",
    }[workerState] || "неизвестно";
    // Объяснение — в подсказке самого показания, а не отдельной строкой рядом: на узком окне
    // от показания остаётся одна лампа, и второму слову там места нет. До правки по ревью C2
    // этот текст пересчитывался на каждый опрос в элемент с `display: none`, то есть никуда.
    $("worker").title = {
      alive: "задачи берутся из очереди",
      stopped: "очередь стоит, задачи не берутся",
      unknown: "замок не удалось проверить",
    }[workerState] || "";
    // Четыре исхода, а не три (`queueStateWord`): пустая очередь при живом работнике —
    // «свободна», и говорить о ней «идёт» рядом с «Ничего не считается» значит спорить с
    // самим собой в одной чроме.
    $("queue-state").textContent = queueStateWord(
      { paused, workerState, running: Boolean(running), pending: pending.length });
    $("queue-state").className = "v" + (paused || workerState !== "alive" ? " warn" : "");
    $("llm-state").textContent = {
      up: "поднята", busy: "занята прогоном", down: "выгружена",
    }[llmStatus] || "неизвестно";
    $("llm-state").className = "v" + (llmStatus === "up" ? " hot" : "");

    // -- идёт сейчас
    const progress = renderRunning(running, workerState, now);
    // Тот же остаток, что печатается в приборной строке, нужен модалке: `gpu_busy` без него —
    // отказ без совета, ждать минуту или три часа по нему не понять.
    runningLeft = progress.left;

    // -- ждут
    renderUnloadBanner(pending.length);
    $("pending").innerHTML = pending
      .map((job, i) => pendingRowHtml(job, { editingId: editing, index: i + 1 })).join("");
    $("pending-empty").hidden = pending.length > 0;
    $("pending-count").textContent = pending.length || "";
    const summary = pendingSummary(pending, {
      now, runningSeconds: progress.left, workerState,
    });

    /* -- пауза/старт очереди (A5, место из C2): кнопка вынесена из `<h2>` в собственную полосу
       состояния над списком. `state.paused` решает подпись и то, какой маршрут бьёт нажатие
       (см. `toggleQueuePause`) — оба из одного значения. Атрибута нажатости тут больше нет
       (правка по ревью C2): у кнопки с меняющейся подписью он спорит с ней вслух — «Начать
       расчёт, нажата». Состояние называет сама подпись, и она же читается скринридером.
       Крупная строка рядом отвечает на единственный вопрос, ради которого на эту зону смотрят
       издалека: считается сейчас или нет. */
    $("queue-pause-toggle").textContent = paused ? "▶ Начать расчёт" : "⏸ Приостановить";
    $("queue-title").textContent = paused ? "Очередь приостановлена"
      : workerState !== "alive" ? "Работник не запущен"
      : running ? "Идёт расчёт"
      : pending.length ? "Очередь ждёт своей минуты" : "Очередь пуста";
    $("queue-sub").textContent = paused
      ? "работник не берёт задачи, пока не нажать «Начать расчёт»"
      : workerState !== "alive" ? "запустите h3 worker — без него очередь стоит"
      : summary.text || (running ? "в очереди больше ничего не ждёт" : "ставить нечего");

    const broken = brokenHtml(queue.broken);
    $("pending-bad").hidden = broken === "";
    $("pending-bad").innerHTML = broken;

    // -- закончилось: всё, что есть, свежее сверху
    const finished = finishedSorted([...(queue.done || []), ...(queue.failed || [])]);
    $("finished").innerHTML = finished
      .map((job) => finishedRowHtml(job, state.outdir, state.runs)).join("");
    $("finished-empty").hidden = finished.length > 0;
    const failed = finished.filter((job) => job.exit_code !== 0).length;
    // Счётчик теперь общий, а не «за сутки», — и это единственное место, где видно, сколько
    // всего насчитано: числу в заголовке верят больше, чем длине прокрученного списка.
    $("done-sum").textContent = finished.length
      ? `${finished.length} ${plural(finished.length, "ролик", "ролика", "роликов")}`
        + (failed ? `, из них упало ${failed}` : "")
      : "";
  }

  // -- проекты (Task 7) --------------------------------------------------------------------
  // Список — из `state.projects` (сводка `/api/state` уже несёт, task 6's `project_summary`),
  // панель — из `GET /api/projects/<id>` по клику и после каждого действия (`withProject`,
  // тот же принцип, что `withQueue` у очереди: любой исход — перечитать состояние).

  /** Все задачи из всех четырёх списков очереди, одним плоским списком — тем же путём, что
   *  `jobById` собирает их для поиска одной, только здесь нужен весь список: оценка времени
   *  проекта (`projectEstimateSeconds`) ищет job-эстимейт сцены/трека по `job_id`/`note`, а
   *  сцена может быть уже `done`/`failed`, не только pending/running. */
  function allQueueJobs() {
    const queue = (state && state.queue) || {};
    return [...(queue.pending || []), ...(queue.running || []),
            ...(queue.done || []), ...(queue.failed || [])];
  }

  function renderProjects() {
    if (!state) return;
    const rows = state.projects || [];
    $("projects-list").innerHTML = rows.map(projectRowHtml).join("");
    $("projects-empty").hidden = rows.length > 0;
    $("projects-sum").textContent = rows.length
      ? `${rows.length} ${plural(rows.length, "проект", "проекта", "проектов")}` : "";
  }

  function projectRowHtml(row) {
    const stage = projectStageWord(row);
    const scenes = projectSceneProgressText(row);
    return `<button class="proj-item" type="button" data-act="open-project" `
      + `data-id="${escapeHtml(row.id)}">`
      + `<span class="badge">${escapeHtml(projectKindLabel(row.kind))}</span>`
      + `<span class="body">`
      + `<span class="n">${escapeHtml(row.title || row.id)}</span>`
      + `<span class="stage">${escapeHtml(stage)}</span>`
      + `</span>`
      + (scenes ? `<span class="scenes${row.scenes_failed ? " bad" : ""}">`
                  + `${escapeHtml(scenes)}</span>` : "")
      + `</button>`;
  }

  function showProjectError(payload) {
    const { title, pre } = errorText(payload);
    $("project-err").hidden = false;
    $("project-err").innerHTML = `<b>${escapeHtml(title)}</b>` + (pre ? `<pre>${escapeHtml(pre)}</pre>` : "");
  }
  function clearProjectError() {
    $("project-err").hidden = true;
    $("project-err").innerHTML = "";
  }

  /** Открывает панель немедленно (заголовок = id, тело — «Загрузка…») и только потом идёт за
   *  данными: отказ (404 неизвестного id, 400 пути вне корня) тогда есть, где показать — внутри
   *  уже открытой модалки, а не молча никуда. */
  async function openProjectModal(id) {
    project = { id, project: null, active_job: null };
    projectMp3 = null;
    $("project-modal").hidden = false;
    $("project-title").textContent = id;
    $("project-kind-badge").textContent = "";
    $("project-estimate").textContent = "";
    $("project-body").innerHTML = '<p class="empty pad">Загрузка…</p>';
    clearProjectError();
    await refreshProjectDetail();
  }

  /** Перечитывает панель уже открытого проекта — после каждого действия (`withProject`) и на
   *  первом открытии (`openProjectModal`). Отказ на РЕфреше (сеть моргнула после действия,
   *  которое само уже прошло) не стирает то, что уже отрисовано, — только на самом первом
   *  открытии тело остаётся пустым под плашкой ошибки, потому что рисовать ещё нечего. */
  async function refreshProjectDetail() {
    if (!project) return;
    const hadData = Boolean(project.project);
    try {
      const answer = await api("GET", "/api/projects/" + encodeURIComponent(project.id));
      if (!project || project.id !== answer.project.id) return;  // окно закрыли/сменили, пока шёл запрос
      project = { id: project.id, project: answer.project, active_job: answer.active_job };
      clearProjectError();
    } catch (error) {
      if (error.payload) showProjectError(error.payload);
      else showProjectError({ error: { message: "сервер не ответил" } });
      if (!hadData) $("project-body").innerHTML = "";
      return;
    }
    renderProjectModal();
  }

  function closeProjectModal() {
    project = null;
    projectMp3 = null;
    $("project-modal").hidden = true;
  }

  function renderProjectModal() {
    if (!project || !project.project) return;
    const proj = project.project;
    $("project-title").textContent = proj.title || proj.id;
    $("project-kind-badge").textContent = projectKindLabel(proj.kind);
    const jobs = allQueueJobs();
    const seconds = projectEstimateSeconds(proj, jobs);
    $("project-estimate").textContent = seconds ? `≈${formatDuration(seconds)}` : "";
    const outdir = state && state.outdir;
    $("project-body").innerHTML = projectScriptStageHtml(proj)
      + projectTrackStageHtml(proj, project.active_job, outdir)
      + projectScenesStageHtml(proj, outdir)
      + projectAssemblyStageHtml(proj, outdir);
  }

  function projectScriptStageHtml(proj) {
    const status = proj.stages.script;
    const statusWord = { draft: "черновик", awaiting_approval: "ждёт утверждения",
                         approved: "утверждён" }[status] || status;
    const gate = status === "awaiting_approval"
      ? `<button class="inverse" type="button" data-act="approve-script" `
        + `data-id="${escapeHtml(proj.id)}">Утвердить сценарий</button>` : "";
    let body;
    if (proj.kind === "video") {
      const n = proj.scenes.length;
      body = n
        ? `<p class="proj-stage-note">${n} ${plural(n, "сцена", "сцены", "сцен")} в сценарии.</p>`
        : `<p class="proj-stage-note">Сценарий ещё пуст.</p>`;
    } else {
      const lyrics = (proj.track && proj.track.lyrics) || "";
      const caption = (proj.track && proj.track.caption) || "";
      body = lyrics
        ? `<div class="proj-lyrics">${escapeHtml(lyrics)}</div>`
          + (caption ? `<p class="proj-stage-note">${escapeHtml(caption)}</p>` : "")
        : `<p class="proj-stage-note">Лирика ещё не написана.</p>`;
    }
    return `<div class="proj-stage">`
      + `<div class="proj-stage-head">`
      + `<span class="t">Сценарий</span>`
      + `<span class="proj-stage-status">${escapeHtml(statusWord)}</span>`
      + `<div class="spacer"></div>${gate}</div>`
      + `<div class="proj-stage-body">${body}</div></div>`;
  }

  /** Только для kind в (clip, song), и только после того, как сценарий реально утверждён хотя
   *  бы раз (script — единственный гейт, который не откатывается назад, task 6's `approve_stage`)
   *  — до этого этапа «трек» ещё нечему быть, даже пустой полосой. */
  function projectTrackStageHtml(proj, activeJob, outdir) {
    if (proj.kind === "video" || proj.stages.script !== "approved") return "";
    const track = proj.track || {};
    const status = proj.stages.track;
    const statusWord = { draft: track.mp3 ? "нужно пересчитать — предыдущая попытка не удалась"
                                          : "не начат",
                         running: "пересчитывается", awaiting_approval: "ждёт прослушивания",
                         approved: "утверждён" }[status] || status;
    const mp3 = track.mastered_mp3 || track.mp3;
    const mp3Url = mp3 ? projectMediaUrl(mp3, outdir) : null;
    const player = mp3Url
      ? `<audio class="proj-audio" controls preload="metadata" src="${escapeHtml(mp3Url)}"></audio>`
      : "";
    const undersung = track.undersung
      ? `<p class="proj-stage-note">трек короче лирики — не всё спето, проверьте текст.</p>` : "";
    const doneNote = (proj.kind === "song" && status === "approved")
      ? `<p class="proj-stage-note"><b>Проект завершён</b> — mp3 готов.</p>` : "";
    const gate = status === "awaiting_approval"
      ? `<button class="inverse" type="button" data-act="approve-track" `
        + `data-id="${escapeHtml(proj.id)}">Утвердить трек</button>` : "";
    // «Пересчитать» доступен из draft/awaiting_approval (никогда из approved/done — трек уже
    // committed, см. `_retry_project_track` в web.py) и только пока ничего не считается прямо
    // сейчас (`active_job.kind === "track"` — тот же джойн с очередью, что и у списка/удаления,
    // M6: `stages.track` сама по себе не знает, что песня уже в полёте).
    const retryAllowed = (status === "draft" || status === "awaiting_approval")
      && !(activeJob && activeJob.kind === "track");
    const seedField = track.source === "import" ? "" : `<input class="inp mono" `
      + `id="project-track-seed" type="text" placeholder="сид (необязательно)">`;
    const seedRow = retryAllowed
      ? `<div class="proj-track-retry">${seedField}`
        + `<button class="ghost" type="button" data-act="retry-track" `
        + `data-id="${escapeHtml(proj.id)}">Пересчитать трек</button></div>` : "";
    const activeNote = activeJob && activeJob.kind === "track"
      ? `<p class="proj-stage-note">трек считается сейчас — `
        + `<span class="mono">${escapeHtml(activeJob.job.id)}</span></p>` : "";
    return `<div class="proj-stage">`
      + `<div class="proj-stage-head">`
      + `<span class="t">Трек</span>`
      + `<span class="proj-stage-status">${escapeHtml(statusWord)}</span>`
      + `<div class="spacer"></div>${gate}</div>`
      + `<div class="proj-stage-body">${player}${undersung}${doneNote}${activeNote}${seedRow}</div>`
      + `</div>`;
  }

  function projectSceneCardHtml(scene, projId, outdir) {
    const mark = { pending: "wait", running: "run", done: "done", failed: "fail" }[scene.status]
      || "wait";
    const clipUrl = scene.clip_path ? projectMediaUrl(scene.clip_path, outdir) : null;
    const frame = clipUrl
      ? `<div class="frame"><video src="${escapeHtml(clipUrl)}" preload="metadata" controls></video></div>`
      : `<div class="frame"></div>`;
    const promptText = String(scene.prompt || "").slice(0, 260);
    return `<div class="scene-card">${frame}`
      + `<div class="info">`
      + `<div class="row1">`
      + `<span class="m ${mark}" aria-hidden="true"></span>`
      + `<span class="idx">#${scene.idx}</span>`
      + `<span class="sdur">${formatFine(scene.duration)}</span>`
      + `</div>`
      + `<div class="prompt">${escapeHtml(promptText)}</div>`
      + `<div class="acts"><button type="button" data-act="retry-scene" `
      + `data-id="${escapeHtml(projId)}" data-idx="${scene.idx}">пересчитать</button></div>`
      + `</div></div>`;
  }

  function projectScenesStageHtml(proj, outdir) {
    if (proj.kind === "song" || !proj.scenes.length) return "";
    const done = proj.scenes.filter((s) => s.status === "done").length;
    const cards = proj.scenes.slice().sort((a, b) => a.idx - b.idx)
      .map((scene) => projectSceneCardHtml(scene, proj.id, outdir)).join("");
    return `<div class="proj-stage">`
      + `<div class="proj-stage-head">`
      + `<span class="t">Сцены</span>`
      + `<span class="proj-stage-status">${done}/${proj.scenes.length} готово`
      + `${proj.stages.scenes === "failed" ? " — есть упавшие" : ""}</span>`
      + `</div>`
      + `<div class="proj-stage-body"><div class="scene-grid">${cards}</div></div></div>`;
  }

  function projectAssemblyStageHtml(proj, outdir) {
    if (proj.kind === "song") return "";
    const status = proj.stages.assembly;
    if (status === "draft" && !proj.assembly.final_path) return "";  // сборка ещё не начиналась
    const statusWord = { draft: "не начата", running: "идёт", done: "готово", failed: "упала" }
      [status] || status;
    const retry = status === "failed"
      ? `<button class="ghost" type="button" data-act="retry-assembly" `
        + `data-id="${escapeHtml(proj.id)}">Пересчитать сборку</button>` : "";
    const finalUrl = proj.assembly.final_path ? projectMediaUrl(proj.assembly.final_path, outdir) : null;
    const link = finalUrl
      ? `<p class="proj-final"><a class="clip" href="${escapeHtml(finalUrl)}" target="_blank" `
        + `rel="noopener">final.mp4</a></p>`
      : `<p class="proj-stage-note">пока нечего собирать</p>`;
    return `<div class="proj-stage">`
      + `<div class="proj-stage-head">`
      + `<span class="t">Сборка</span>`
      + `<span class="proj-stage-status">${escapeHtml(statusWord)}</span>`
      + `<div class="spacer"></div>${retry}</div>`
      + `<div class="proj-stage-body">${link}</div></div>`;
  }

  /** Тот же принцип, что `withQueue`: флаг занятости, отказ красной плашкой, и в любом исходе —
   *  перечитать состояние (`poll` обновляет список из `/api/state`, `refreshProjectDetail` —
   *  открытую панель, если она есть). */
  async function withProject(action) {
    projectBusy = true;
    try {
      await action();
      clearProjectError();
    } catch (error) {
      if (error.payload) showProjectError(error.payload);
      else showProjectError({ error: { message: "сервер не ответил" } });
    } finally {
      projectBusy = false;
      await poll();
      if (project) await refreshProjectDetail();
    }
  }

  async function deleteProject() {
    if (!project) return;
    if (!window.confirm("Удалить проект целиком? Файлы (клипы, трек, сборка) удаляются с диска.")) {
      return;
    }
    const id = project.id;
    try {
      await api("DELETE", "/api/projects/" + encodeURIComponent(id));
    } catch (error) {
      if (error.payload) showProjectError(error.payload);
      else showProjectError({ error: { message: "сервер не ответил" } });
      return;
    }
    closeProjectModal();
    await poll();
  }

  /** Плашка выгрузки над списком ждущих: см. `nextBannerState`. `pendingCount` приходит от
   *  вызывающего — он уже разобрал `queue.pending`, второй раз разбирать незачем. `paused`
   *  берётся из последнего `/api/state` (A5): на паузе плашка не показывается — см. `unloadBanner`. */
  function renderUnloadBanner(pendingCount) {
    unloadBannerInput = { pending: pendingCount, llm: llmStatus, paused: Boolean(state && state.paused) };
    bannerState = nextBannerState(bannerState, unloadBannerInput);
    $("unload-banner").hidden = !bannerState.show;
    $("unload-banner-text").textContent = bannerState.show
      ? unloadBanner(unloadBannerInput).text : "";
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
    const shot = previewUrl(job, completed, state.outdir);

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
      /* Сегменты проходов прямо в карточке задачи (C2, макет: `.now .steps`): в приборной
         строке те же деления стоят на всю ширину экрана и отвечают «сколько осталось вообще»,
         а здесь — «сколько осталось вот этой задаче», и смотрят на них с разного расстояния.
         Та же `stepsHtml`, что и наверху: одно правило рисования, два места показа. */
      `<div class="steps run-steps">${stepsHtml(completed, forwards)}</div>`,
      `<div class="steps-legend">`,
      `<span>проход ${completed} / ${forwards}</span>`,
      `<span>${Math.round(share * 100)} %</span>`,
      `</div>`,
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
     Длительность приходит от вызывающего (A3): у формы это `#duration`, у модалки — состояние
     чата через `chatDuration` (`renderChatPrompt`), а не общее поле формы — сессия и форма могут
     говорить о ролике разной длины. */
  function paintPrompt(ids, text, { audio, declared }) {
    const analysis = analysePrompt(text, declared, { audio });
    $(ids.hl).innerHTML = highlightHtml(text, analysis);
    const scale = scaleHtml(analysis);
    $(ids.scale).innerHTML = scale;
    $(ids.scale).hidden = scale === "";   // пустая полоска покрытия ничего не покрывает
    $(ids.parse).innerHTML = analysis.notes.map((n) => `<li class="${n.k}">${n.t}</li>`).join("");
  }

  function renderPrompt() {
    paintPrompt({ hl: "hl", scale: "scale", parse: "parse" }, $("prompt").value,
                { audio: $("mode").value === "t2va",
                  declared: Number(String($("duration").value).replace(",", ".")) || 0 });

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
    // Выведенный канвас — единственное место, где человек его видит: пункт «из кадра» чисел не
    // показывает, а в `#width`/`#height` под ним лежит не он.
    const note = canvasNote(autoCanvasChosen(), estimate);
    $("est-sub").innerHTML = `${forwards} `
      + `${plural(forwards, "проход", "прохода", "проходов")} по `
      + `<span class="num">${formatFine(estimate.seconds_per_forward)}</span>, `
      + `DiT ${escapeHtml(estimate.bits)} бит`
      + (note ? `, ${escapeHtml(note)}` : "");
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
    // Свёрнутые «Настройки модели» обязаны сказать, что под ними лежит (C2, требование 3).
    $("model-summary").textContent = modelSummary(form);
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

  /* Сессия, которая открыта или открывается прямо сейчас, — вход сторожа `chatHashAction`.
     Отдельно от `chat` потому, что `chat` появляется только после ответа сервера, а гонка живёт
     ровно в этом окне ожидания (см. докстроку `chatHashAction`). */
  let chatWanted = null;

  /* Кнопка завершения зависит от источника: разговор о промпте библиотеки кончается файлом,
     о задаче — правкой задачи, а начатый из формы — текстом в редакторе. `clip` (ролик проекта)
     сервер уже хранит, а страница открывать пока не умеет — до спеки «проекты» он ведёт себя
     как новый промпт. */
  const FINISH_LABEL = { new: "в Редактор", clip: "в Редактор",
                         prompt: "сохранить промпт", job: "обновить задачу" };

  function chatSourceText(source) {
    const it = source || {};
    if (it.kind === "prompt") return `промпт ${it.name || "?"}`;
    if (it.kind === "job") return `задача ${it.id || "?"}`;
    if (it.kind === "clip") return `ролик ${it.id || "?"}`;
    return "новый промпт";
  }

  /**
   * Плашка модели. `override` — строка на один случай («жду ответа…»), без него плашка
   * собирается из выбранного провайдера и последнего известного состояния (`llmPlateText`).
   */
  function renderLlmPlate(override) {
    if (override) { $("chat-llm").textContent = override; $("chat-llm-dot").className = "dot"; return; }
    const row = ((chat && chat.providers) || [])
      .find((item) => item.name === $("chat-provider").value);
    const status = (chat && chat.llmStatus) || "";
    $("chat-llm").textContent = llmPlateText(status, {
      external: Boolean(row) && row.type !== "llama-local",
      runningSeconds: runningLeft,
    });
    /* Точка рядом со словом — форма макета. Янтарь достаётся только `busy`, то есть ровно
       тому состоянию, в котором GPU действительно занят прогоном; «поднята» — зелёная, всё
       остальное серое. Цвет здесь подтверждает слово, а не заменяет его. */
    $("chat-llm-dot").className = "dot"
      + (status === "busy" ? " hot" : status === "up" ? " ok" : "");
  }

  function renderChatPrompt() {
    if (!chat) return;
    // Присваивание только при расхождении: `value = value` во время набора сбрасывает каретку
    // в конец строки.
    if ($("chat-prompt-text").value !== chat.promptText) {
      $("chat-prompt-text").value = chat.promptText;
    }
    paintPrompt({ hl: "chat-hl", scale: "chat-scale", parse: "chat-parse" }, chat.promptText,
                { audio: (chat.mode || $("mode").value) === "t2va",
                  declared: chatDuration(chat) });
    // Приписка у заголовка окна — место макета под «черновик · не сохранён». Здесь она отвечает
    // на единственный вопрос, который у этого окна есть: переживёт ли набранное закрытие. Не
    // переживёт (`hasUnsavedEdits`) — об этом сказано до Esc, а не в диалоге после него.
    $("chat-prompt-note").textContent = hasUnsavedEdits(chat) ? "правки не сохранены" : "";
  }

  function renderChatLog() {
    if (!chat) return;
    const box = $("chat-log");
    box.innerHTML = chat.log.length
      ? chat.log.map((entry) => {
          // `role` и `kind` — свои, не с сервера, но в этом файле экранируется всё, что
          // попадает в разметку, и исключение «тут значение точно наше» — ровно то место, где
          // однажды окажется чужое (`role` приходит из `session.messages` с диска).
          // Три точки у `pending`-записи — разметка, не текст: `pendingEntry` остаётся чистой
          // функцией, а анимация (CSS, `@keyframes pending-dot`) идёт своим ходом и гасится
          // тем же `prefers-reduced-motion`, что и остальная страница.
          const dots = entry.kind === "pending"
            ? '<span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>' : "";
          // A8, требование 2: ход, к которому приложен кадр, несёт пометку с именем файла —
          // `sendChatMessage` кладёт его в `entry.attachment` в момент отправки, только для
          // хода, который его реально приложил (история с диска этого поля не знает вовсе).
          const attach = entry.attachment
            ? `<span class="turn-attach">📎 ${escapeHtml(entry.attachment)}</span>` : "";
          return `<li class="turn ${escapeHtml(entry.role)}`
            + `${entry.kind ? " " + escapeHtml(entry.kind) : ""}">`
            + `${attach}${escapeHtml(entry.text)}${dots}</li>`;
        }).join("")
      /* Пустая лента — не белое пятно, а короткая инструкция (C3, макет: пустое состояние).
         Разговор начинается с чистого листа каждый раз, и лист обязан сказать, чего от него
         ждут: три вещи, которые здесь можно сделать, в том порядке, в каком их делают. */
      : `<li class="feed-empty">`
        + `<span class="fe-t">Расскажите, что снимаем</span>`
        + `<span>Опишите идею словами — модель соберёт промпт по формату MiniMax H3.</span>`
        + `<span>Уже готовый текст вставьте в окно промпта и попросите привести к стандарту.</span>`
        + `<span>Картинку можно перетащить прямо в поле ввода — станет опорным кадром.</span>`
        + `</li>`;
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
   *  текст промпта, режим, путь кадра, длительность (сервер сам их не знает) и `notice` —
   *  строка, которую надо показать в ленте сразу (например, что промпт задачи прочитать не
   *  удалось). `opened.duration` — A3: `#duration` формы у нового диалога, `--duration` задачи
   *  у диалога от неё (`openChatFromJob`); без источника — дефолт того же поля сервера. */
  async function openChatModal(source, opened = {}) {
    try {
      const answer = await api("POST", "/api/chat", {
        source,
        prompt: opened.prompt || "",
        mode: opened.mode || "",
        image: opened.image || "",
        end_image: opened.endImage || "",
        duration: chatDuration(opened),
      });
      clearError();
      window.location.hash = `#chat/${answer.id}`;
      await syncChatFromHash();
      if (opened.notice && chat && chat.id === answer.id) {
        chat.log.push({ role: "note", kind: "warn", text: opened.notice });
        renderChatLog();
      }
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
    // Пока шёл GET, окно могли закрыть или увести на другую сессию: `chatWanted` — то же
    // «ответ принадлежит тому, кто его заказал», что у `landTurn`, только для открытия.
    if (chatWanted !== id) return;
    chat = {
      id,
      source: session.source || { kind: "new" },
      mode: session.mode || "",
      // A8: кадр сессии — тот, с которым её открыли, или тот, что подложил более ранний ход
      // этой же модалки (`sendChatMessage`'s own `image`, приземлённый после ответа). Читается
      // отсюда «в Редактор» (`finishChat`), той же дорогой, какой `slug` доезжает до `#tag`.
      image: session.image || "",
      // A8: кадр, который только что загрузился через `/api/uploads` (скрепка/dnd в поле
      // ввода), но ещё не ушёл ходом — `null`, пока ничего не приложено; `sendChatMessage`
      // сбрасывает его в начале хода, а `attachmentBody` читает как `state.pendingImage`.
      pendingImage: null,
      // A3: длительность сессии — редактируется прямо в шапке модалки (`chat-duration`) и
      // уходит каждым ходом (`sendChatMessage`); `chatDuration` отвечает за дефолт, если сессия
      // почему-то ничего не сказала.
      duration: chatDuration(session),
      /* Окно восстанавливается из последнего ответа модели, а не из `prompt` сессии: `prompt`
         — это текст, с которым сессию открыли, и ходы его не переписывают (так устроен
         сервер). Правки руками между ходами в сессии не живут вовсе — они уходят следующим
         ходом и возвращаются в ответе. */
      promptText: session.prompt_struct ? buildPromptText(session.prompt_struct)
                                        : (session.prompt || ""),
      /* Что из этого текста переживёт закрытие модалки: всё, что человек напечатает сверху,
         не живёт нигде, кроме браузера, — на этом стоит вопрос перед Esc (`hasUnsavedEdits`). */
      savedText: session.prompt_struct ? buildPromptText(session.prompt_struct)
                                       : (session.prompt || ""),
      log: (session.messages || []).map((m) => ({ role: m.role, text: m.content })),
      sending: false,
      providers: [],       // роспись из /api/providers — по ней собирается плашка модели
      llmStatus: "",
      // A4: последний известный слаг сессии — сервер хранит его тем же правилом, каким хранит
      // `prompt_struct` (только непустая строка переписывает), `applyTurn` держит его свежим на
      // каждом ходе, а «в Редактор» (`finishChat`) подставляет его в `#tag`.
      slug: session.slug || "",
      // A4, fix round 2: восстановлен из module-level `autoTagBySession`, не всегда с пустой
      // строки — `chat` умер вместе с прошлым закрытием модалки этой же сессии, но подстановку,
      // которую та модалка сделала в `#tag`, память переживает и здесь узнаётся обратно как
      // «своя», а не как чужая ручная правка (fix round 1 гарантию строил только внутри одной
      // открытой модалки; round 2 — на переоткрытие той же сессии).
      lastAutoTag: recallAutoTag(autoTagBySession, id),
      // Task 7 ("Проекты"): `session["project"]` — сервер отдаёт его целиком в `GET /api/chat/
      // <id>` (`_read_chat` разворачивает всю сессию), тем же полем, что несёт каждый ход
      // (`_chat_message`'s ответ, приземляется `applyTurn`). `null`, если ни один ход сессии
      // ещё не ответил проектом — кнопка «Сделать проектом» ниже остаётся disabled.
      project: (session.project && typeof session.project === "object") ? session.project : null,
    };
    $("chat-modal").hidden = false;
    $("chat-finish").textContent = FINISH_LABEL[chat.source.kind] || FINISH_LABEL.new;
    $("chat-source").textContent = chatSourceText(chat.source);
    $("chat-duration").value = chat.duration;
    $("chat-make-project").disabled = !chat.project;
    $("chat-project-panel").hidden = true;   // панель создания — не состояние прошлой сессии
    renderChatPrompt();
    renderChatLog();
    renderChatAttachment();   // A8: сбрасывает бейдж прошлой сессии -- новый `chat` начинается
                               // с пустого `pendingImage`, а DOM без этого мог остаться на её
                               // последнем состоянии (открытая/ошибочная загрузка).
    await loadProviders();
    $("chat-input").focus();
  }

  function closeChat() {
    chat = null;
    chatWanted = null;
    $("chat-project-panel").hidden = true;
    $("chat-modal").hidden = true;
    if (CHAT_HASH.test(window.location.hash || "")) window.location.hash = "";
  }

  /* Единственный жест, закрывающий окно, — кнопка «закрыть». Правка руками нигде, кроме этого
     окна, не живёт (сервер хранит ответы модели), поэтому даже она спрашивает.
     `finishChat` спрашивать не должен: он этот текст как раз и сохраняет. */
  function requestCloseChat() {
    if (hasUnsavedEdits(chat)
        && !window.confirm("Правки промпта не сохранены и пропадут. Закрыть?")) return;
    closeChat();
  }

  /* «очистить» в шапке — не «закрыть»: стирает сессию на диске, а не просто прячет окно.
     Подтверждение обязательно, потому что отменить это уже нельзя (в отличие от простого
     закрытия, после которого сессия остаётся и открывается снова тем же `#chat/<id>`).
     После удаления модалка не закрывается, а сразу открывает пустую новую сессию — тем же
     `openChatModal`, каким открывается кнопка «Новый диалог». */
  async function requestDeleteChat() {
    if (!chat) return;
    if (!window.confirm("Удалить этот диалог целиком?")) return;
    try {
      await api("DELETE", "/api/chat/" + encodeURIComponent(chat.id));
    } catch (error) {
      if (error.payload) showError(error.payload);
      return;
    }
    openChatModal({ kind: "new" });
  }

  async function syncChatFromHash() {
    const action = chatHashAction(window.location.hash, chatWanted);
    if (action.act === "nothing") return;
    // Помечаем намерение до `await`, а не после: `hashchange` от нашего же присваивания хеша
    // приходит именно в это окно, и сторож обязан застать его уже занятым.
    chatWanted = action.id;
    await enterChat(action.id);
  }

  /**
   * Один ход: реплика уходит на сервер, ответ приземляется туда, откуда ушёл.
   *
   * `session` — состояние на момент отправки. Пока идёт ход (десятки секунд), модалку можно
   * закрыть и открыть на другой сессии, поэтому приземление обоих исходов идёт через
   * `landTurn`/`landFailure`, а рисуется только то, что приземлилось (см. их докстроки).
   *
   * Кнопка отпускается до этой проверки и без всяких условий: она принадлежит модалке, а не
   * сессии, и оставить её мёртвой — значит потребовать перезагрузки страницы.
   *
   * Поле ввода чистится сразу, а не по ответу: сервер синхронный и до самого ответа ничего не
   * шлёт, так что "реплика ушла" и "поле свободно для следующей" должны стать правдой в один и
   * тот же клик — иначе от «отправить» ждали бы того же ответа, что и от самого хода. Исходный
   * текст (`typed`, не обрезанный) при этом сохраняется, но возвращается в поле не безусловно —
   * кнопка «отправить» весь ход остаётся живой, и человек мог успеть начать новый черновик, пока
   * старая реплика летела; затирать его старым текстом молча нельзя (см. `restoredInput`). Ход
   * при этом должен ещё и правда лечь в эту сессию, а не в чужую, на которую модалка успела
   * перескочить.
   *
   * A8: кадр, приложенный скрепкой/dnd (`chat.pendingImage`, уже загруженный на сервер
   * `/api/uploads` к этому моменту — см. `uploadChatImage`), уходит этим же ходом через
   * `attachmentBody`: `image`/`set_mode`, и, если поле ввода было пустым, дефолтный текст
   * реплики вместо неё. Кадр снимается с состояния модалки до отправки, той же логикой, что и
   * поле ввода строкой выше, — а не по ответу, чтобы новый кадр можно было прикладывать, пока
   * этот ход ещё летит.
   */
  async function sendChatMessage() {
    if (!chat || chat.sending) return;
    const typed = $("chat-input").value;
    const text = typed.trim();
    const attachment = chat.pendingImage;
    if (!text && !attachment) return;
    const extra = attachmentBody({ text, pendingImage: attachment, mode: chat.mode });
    const outgoingText = extra.text !== undefined ? extra.text : text;
    const session = chat;
    session.sending = true;
    $("chat-send").disabled = true;
    renderLlmPlate("жду ответа — на холодной модели это до минуты");
    session.log.push({ role: "user", text: outgoingText,
                       attachment: attachment ? attachment.name : "" });
    // Плейсхолдер хода — точки, что ход идёт, пока сервер ничего не прислал (см. `pendingEntry`).
    session.log.push(pendingEntry(session.llmStatus));
    $("chat-input").value = "";
    clearChatAttachment();
    renderChatLog();

    let answer = null;
    let failure = null;
    try {
      answer = await api("POST", `/api/chat/${encodeURIComponent(session.id)}/message`,
                         { text: outgoingText, prompt: session.promptText,
                           provider: $("chat-provider").value,
                           // A3: длительность из состояния модалки, не из формы — она может
                           // расходиться с сессией с самого открытия, а с этого хода и сама
                           // могла подвинуться в `chat-duration`.
                           duration: chatDuration(session),
                           image: extra.image, set_mode: extra.set_mode });
    } catch (error) {
      failure = error;
    }
    session.sending = false;
    $("chat-send").disabled = false;

    if (answer) {
      if (!landTurn(chat, session, answer)) return;
      // Сервер уже применил `image`/`set_mode` к сессии (`_locked_turn`) -- состояние модалки
      // догоняет тем же значением, которое сама и отправила, а не ждёт следующего перечитывания
      // сессии, которого может не случиться до самого «в Редактор».
      if (extra.image) chat.image = extra.image;
      if (extra.set_mode) chat.mode = extra.set_mode;
      chat.llmStatus = (answer.llm || {}).status || chat.llmStatus;
      clearError();
    } else if (!landFailure(chat, session, failure && failure.payload, runningLeft)) {
      return;
    } else {
      // Ход не удался: `landFailure` уже вернул реплику в ленту, и поле получает обратно тот же
      // текст, который в нём был до отправки — но только если человек не начал печатать что-то
      // новое, пока ход летел (см. `restoredInput`). Кадр — тем же правилом: отказ не должен
      // заставлять грузить файл заново.
      $("chat-input").value = restoredInput($("chat-input").value, typed);
      if (attachment && !chat.pendingImage) restoreChatAttachment(attachment);
    }
    renderLlmPlate();
    renderChatPrompt();
    renderChatLog();
    // Task 7: этот самый ход мог быть первым, ответившим полем `project` (`applyTurn` уже
    // положил его в `chat.project`, если да) — кнопка «Сделать проектом» обязана ожить тем же
    // кадром, без ожидания следующего действия.
    $("chat-make-project").disabled = !chat.project;
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

  /** Задача по `id`, в каком бы из четырёх списков `/api/state` она ни сидела.
   *
   *  `openChatFromJob` зовёт её для любой карточки с кнопкой «Обсудить» — а с C2 такая
   *  кнопка есть и у готовой (`finishedRowHtml`), не только у ждущей. Искать по одному
   *  `queue.pending`, как раньше, значит открывать разговор для ждущей и молча ничего не
   *  делать по клику для готовой — не отказ, а тишина, которую легко принять за то, что
   *  кнопка ничего и не делала. */
  function jobById(id) {
    const queue = (state && state.queue) || {};
    return [...(queue.pending || []), ...(queue.running || []),
            ...(queue.done || []), ...(queue.failed || [])]
      .find((row) => row.id === id) || null;
  }

  async function openChatFromJob(id) {
    const job = jobById(id);
    if (!job) return;
    const prompt = await jobPromptText(job);
    await openChatModal({ kind: "job", id }, {
      prompt,
      mode: argValue(job.args, "--mode") || "t2va",
      image: argValue(job.args, "--image") || "",
      // `flf`-задача несёт второй кадр отдельным флагом — без него чат от такой задачи не видит
      // последний кадр вовсе, и модель не может написать инструкцию `mode: flf` (T4b).
      endImage: argValue(job.args, "--end-image") || "",
      // A3: длительность задачи-источника, тем же путём, каким приходят режим и кадр выше —
      // не форма, а сама задача решает, о скольких секундах будет разговор в модалке. Дефолт —
      // тот же, что у `chatDuration`, когда `--duration` в её `args` не нашлось.
      duration: Number(argValue(job.args, "--duration")) || 10,
      // Пустое окно у задачи, у которой промпт точно есть, — это не «промпт пуст», а «страница
      // его не достала». Молчать здесь значит предложить сохранить пустоту поверх работы.
      notice: prompt.trim() === ""
        ? "Промпт этой задачи странице не виден: он ушёл снимком в очередь, и маршрута к нему "
          + "нет. Вставьте текст в окно слева или соберите заново — «обновить задачу» перепишет "
          + "промпт задачи тем, что будет в окне."
        : "",
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
    const refusal = finishRefusal(chat);
    if (refusal) {
      // Плашкой в ленте, а не молча и не окном: отказ виден там же, где всё остальное, что
      // модалка отвечает, и модалка при этом остаётся открытой.
      chat.log.push({ role: "note", kind: "bad", text: refusal });
      renderChatLog();
      return;
    }
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
         строкой, а не как --prompt-file на старое содержимое. Тег — слаг сессии (A4), если
         модель его назвала и поле не занято ручной правкой (`tagFromSessionSlug`, fix round 1);
         запомненное в `autoTagBySession` (fix round 2, module-level — не на `chat`, который эта
         же функция вот-вот убьёт своим `closeChat()`) переживает закрытие модалки, так что
         переоткрытая позже та же сессия узнаёт свою прошлую подстановку и двигает тег дальше,
         а не решает, что поле уже кто-то тронул руками. */
      $("prompt").value = text;
      promptFromFile = null;
      $("prompt-file").value = "";
      // A8: режим и кадр сессии — только когда сессия их действительно назвала. Пустой `chat.mode`
      // ничего не значит (форма уже показывает свой собственный выбор, и `<select>` не умеет
      // пустое значение), а непустой — это либо режим, с которым открыли сессию, либо `i2v`,
      // которым её обновил дропнутый в диалог кадр (`_locked_turn`'s own `set_mode`); в обоих
      // случаях форма обязана увидеть его так же безусловно, как несколькими строками выше
      // видит текст промпта.
      if (chat.mode) {
        $("mode").value = chat.mode;
        syncModeRows();
      }
      if (chat.image) {
        $("image").value = chat.image;
        updateUploadZone("image");
      }
      // Запоминается только тогда, когда `tagFromSessionSlug` действительно что-то подставила:
      // безусловная память приняла бы и оставленный как есть ручной тег за «свой» автослаг, и
      // тот же баг вернулся бы на следующем ходе этой сессии — только на шаг позже.
      const nextTag = tagFromSessionSlug($("tag").value, chat.slug, chat.lastAutoTag);
      if (nextTag !== $("tag").value) {
        rememberAutoTag(autoTagBySession, chat.id, nextTag);
        chat.lastAutoTag = nextTag;
      }
      $("tag").value = nextTag;
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
    $("adaln").value = argValue(job.args, "--adaln-cache") || "";
    $("outdir").value = argValue(job.args, "--outdir") || "";
    $("image").value = argValue(job.args, "--image") || "";
    $("end-image").value = argValue(job.args, "--end-image") || "";
    // C2, требование 4: поля ввода у заметки больше нет, но правка обязана вернуть на сервер ту
    // же строку, с которой пришла (см. `formNote`) — иначе «Править» тихо стирала бы чужой текст.
    formNote = job.note || "";
    syncModeRows();
    /* Задача, поставленная «из кадра», несёт канвас только в оценке — в её `args` нет ни
       `--width`, ни `--height`, потому что выводил его CLI. Правка обязана вернуться на тот же
       пункт, иначе «Править» молча превращает автовывод в пресет по числам из оценки: для
       кадра 768×1344 это «большое верт.», для кадра 700×1000 — «своё…», и оба раза правка
       меняет то, о чём её не просили. */
    const hadCanvas = argValue(job.args, "--width") !== null;
    $("canvas-preset").value = hadCanvas ? $("canvas-preset").value : "auto";
    updateUploadZone("image");
    updateUploadZone("end-image");
    syncCanvasPreset();
    syncAutoCanvasOption();
  }

  function setEditing(id) {
    editing = id;
    /* Пусто вне правки, а не «Новая задача»: рядом теперь стоит кнопка с ровно этой надписью, и
       две «Новых задачи» в одной строке читаются как опечатка. Строка эта и нужна только чтобы
       предупредить о правке чужой задачи — состояние «ничего не правится» видно по самой форме. */
    $("form-mode-note").textContent = id ? `Правка ждущей задачи ${id}` : "";
    $("submit").textContent = id ? "Сохранить правку" : "Поставить в очередь";
    $("cancel-edit").hidden = !id;
    // Выход из правки — конец чужой заметки: следующая постановка своей не имеет (см. `formNote`).
    if (!id) formNote = "";
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

  /** «Новая задача»: сочинение — начисто, рецепт — как был.
   *
   *  Значения берутся из `resetFormState`, а здесь остаётся то, чего в полях ввода нет: заметка
   *  правящейся задачи, режим правки, память о загруженном файле промпта, след ручного выбора
   *  канваса — и зоны кадров, которые «указать путь» когда-то спрятала (единственное место, где
   *  это вообще отменяется: сама ссылка обратного хода не имеет). */
  function applyFormReset() {
    for (const [id, value] of Object.entries(resetFormState())) $(id).value = value;
    promptFromFile = null;    // «промпт набран здесь» — значит ни к какому файлу он не привязан
    formNote = "";
    canvasTouchedByHand = false;
    for (const id of ["image", "end-image"]) {
      $(`${id}-zone`).hidden = false;
      $(`${id}-manual`).hidden = false;
      $(id).hidden = true;
      updateUploadZone(id);
    }
    syncModeRows();
    syncCanvasPreset();
    syncAutoCanvasOption();
    // Последним: `setEditing(null)` сам зовёт `renderQueue`/`renderPrompt`/`scheduleEstimate`,
    // и звать их до него значило бы рисовать форму, которая ещё считается правящей чужую задачу.
    setEditing(null);
    $("prompt").focus();
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
    // A4: `readForm` уже свело пустое `#tag` к «run» (`tag: $("tag").value.trim() || "run"`), так
    // что «пуст или run» из требования — это ровно `form.tag === "run"` здесь. Эвристика молчит
    // (пустая строка), когда в тексте не нашлось ни одного значимого слова — тогда тег остаётся
    // тем же «run», который уже лежит в форме.
    if (form.tag === "run") {
      const slug = heuristicSlug(form.prompt);
      if (slug) form.tag = slug;
    }
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

  /* -- разрешение выпадашкой (C2) ------------------------------------------------------------
     Источник правды остался прежним: `#width`/`#height`, из которых читает `readForm` и которые
     видит `buildArgs`. Выпадашка над ними — только способ их заполнить, а «своё…» открывает их
     ручному вводу. Из этого следуют обе стороны синхронизации ниже: выбор пункта пишет числа,
     а числа (правка задачи, ручной ввод) выбирают пункт — иначе список показывал бы «малое» на
     канвасе 1024×576. */

  /* Трогал ли человек выпадашку разрешения сам. Нужно ровно одному месту — автопереключению на
     «из кадра» после загрузки кадра: подставлять умолчание можно, перебивать явный выбор нельзя,
     а отличить одно от другого по значению выпадашки невозможно (выбранное руками «малое»
     выглядит ровно как «малое», стоявшее там с загрузки страницы). */
  let canvasTouchedByHand = false;

  /** Пункт списка — по тому, что сейчас в полях; поля ручного ввода видны только под «своё…».
   *
   *  «из кадра» этот путь не трогает: у него нет своих чисел в полях, по которым его можно было
   *  бы узнать обратно (в `#width`/`#height` под ним лежит последний пресет — тот, на который
   *  форма вернётся, если кадр уберут), так что вывести его из полей нельзя, и перезаписывать
   *  выбор человека нечем. */
  function syncCanvasPreset() {
    if ($("canvas-preset").value === "auto" && autoCanvasChosen()) {
      $("row-canvas").hidden = true;
      return;
    }
    const key = canvasPresetKey($("width").value, $("height").value);
    $("canvas-preset").value = key;
    $("row-canvas").hidden = key !== "custom";
  }

  /** Выбран ли «из кадра» — и вправе ли он быть выбран прямо сейчас.
   *
   *  Второе условие не формальность: режим и кадр меняются после выбора пункта, и «из кадра»,
   *  переживший удаление кадра, — это молчаливый `DEFAULT_CANVAS` вместо ошибки. */
  function autoCanvasChosen() {
    return $("canvas-preset").value === "auto"
      && autoCanvasAllowed($("mode").value, $("image").value);
  }

  /** Доступность пункта «из кадра»: только под i2v/flf с кадром. Если он был выбран и перестал
   *  быть возможным — форма возвращается к пресету по числам, которые всё это время лежали в
   *  полях, а не остаётся на пункте, который больше ничего не значит. */
  function syncAutoCanvasOption() {
    const allowed = autoCanvasAllowed($("mode").value, $("image").value);
    const option = $("canvas-preset").querySelector('option[value="auto"]');
    if (option) option.disabled = !allowed;
    if (!allowed && $("canvas-preset").value === "auto") syncCanvasPreset();
  }

  /** Выбор пункта — в поля. «своё…» ничего не пишет: оно открывает то, что уже стоит, и человек
   *  правит от него, а не от обнулённого канваса. «из кадра» тоже ничего не пишет — и по той же
   *  причине: числа под ним остаются тем, к чему форма вернётся, если кадр уберут. */
  function applyCanvasChoice() {
    const key = $("canvas-preset").value;
    const preset = applyCanvasPreset(key);
    if (preset) {
      $("width").value = preset.width;
      $("height").value = preset.height;
    }
    $("row-canvas").hidden = key !== "custom";
    // Тот же путь, что и ручной ввод в поля из FIELDS — см. подписку ниже.
    scheduleEstimate();
    renderPrompt();
  }

  /* -- зона загрузки кадра (A7) --------------------------------------------------------------
     `#image`/`#end-image` are the only thing `buildArgs` ever reads: a drop zone just has to
     put a path into one of them, the same as typing into it always did. The zone's own text is
     always derived from that field's current value (`updateUploadZone`), never tracked as
     separate state, so a job restored into the form (`fillFormFrom`), a path typed by hand
     through "указать путь", and a freshly uploaded file all show up the same way. */

  /** Имя файла из пути в поле `id` — с любым разделителем, потому что путь мог прийти с сервера
   *  (всегда `/`) или быть вписан руками на другой ОС. */
  function pathBasename(value) {
    const trimmed = (value || "").trim();
    if (!trimmed) return "";
    const parts = trimmed.split(/[\\/]/);
    return parts[parts.length - 1];
  }

  function updateUploadZone(id) {
    const name = pathBasename($(id).value);
    $(`${id}-zone-label`).textContent = uploadZoneLabel({ name });
    $(`${id}-zone`).classList.toggle("loaded", Boolean(name));
    $(`${id}-zone`).classList.remove("error");
    // Кадр появился или пропал — от этого зависит, возможен ли вообще пункт «из кадра».
    syncAutoCanvasOption();
  }

  /** После загрузки кадра форма сама встаёт на «из кадра». Это верный ответ по умолчанию: кадр
   *  уронили ради него самого, и растянуть его в чужой аспект — не то, чего хотели. Выбор
   *  человека при этом не перебивается: если он уже поставил пресет руками, здесь ничего не
   *  происходит — переключается только пункт, оставшийся с прошлого раза «по умолчанию». */
  function preferAutoCanvasAfterUpload(id) {
    if (id !== "image") return;                       // канвас выводится из первого кадра
    if (!autoCanvasAllowed($("mode").value, $("image").value)) return;
    if (canvasTouchedByHand) return;
    $("canvas-preset").value = "auto";
    $("row-canvas").hidden = true;
    scheduleEstimate();
    renderPrompt();
  }

  /** Один POST, сырыми байтами: `Content-Type: application/octet-stream` и имя файла в
   *  `X-Filename`, а не в теле — картинке незачем делать крюк через base64 и JSON, когда байты
   *  и так уже есть в `File`. Имя закодировано `encodeURIComponent`: значение заголовка — ASCII
   *  по спецификации и `fetch` сам бросит исключение на кириллице, если этого не сделать
   *  (см. `_upload_frame` в `web.py`, где сервер это же значение `decodeURIComponent`ит назад). */
  async function uploadFrame(id, file) {
    const zone = $(`${id}-zone`);
    zone.classList.remove("error");
    zone.classList.add("busy");
    try {
      const response = await fetch("/api/uploads", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream",
                  "X-Filename": encodeURIComponent(file.name) },
        body: file,
      });
      const payload = await response.json();
      if (!response.ok) {
        const error = new Error("upload");
        error.payload = payload;
        throw error;
      }
      $(id).value = payload.path;
      updateUploadZone(id);
      preferAutoCanvasAfterUpload(id);
      scheduleEstimate();
      renderPrompt();
    } catch (error) {
      zone.classList.add("error");
      $(`${id}-zone-label`).textContent = error.payload
        // `pre`, а не `title`: в подписи шириной в одну строку настоящая причина от сервера
        // («кадром может быть только […], а 'x.txt' — нет») полезнее общего заголовка.
        ? errorText(error.payload).pre || errorText(error.payload).title
        : "Файл не загрузился";
    } finally {
      zone.classList.remove("busy");
    }
  }

  /** Зона `id-zone` рядом с полем `id`: клик или Enter/пробел открывают скрытый `id-file`,
   *  перетаскивание принимает первый файл так же, а ссылка `id-manual` — запасной путь на случай,
   *  когда файла для перетаскивания просто нет: путь уже известен (открыта правка задачи,
   *  которую поставили не с этой страницы) и его проще вписать, чем найти на диске. */
  function wireUploadZone(id) {
    const zone = $(`${id}-zone`);
    const fileInput = $(`${id}-file`);
    const manual = $(`${id}-manual`);
    const path = $(id);

    zone.addEventListener("click", () => fileInput.click());
    zone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fileInput.click();
      }
    });
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("dragover");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("dragover");
      const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      if (file) uploadFrame(id, file);
    });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      if (file) uploadFrame(id, file);
      fileInput.value = "";     // тот же файл второй раз подряд тоже должен дать событие change
    });
    manual.addEventListener("click", (event) => {
      event.preventDefault();
      zone.hidden = true;
      manual.hidden = true;
      path.hidden = false;
      path.focus();
    });
    updateUploadZone(id);
  }

  /* -- кадр в диалог (A8): скрепка и dnd в поле ввода чата -----------------------------------
     Тот же `POST /api/uploads`, что и `uploadFrame` выше (A7), но результат не садится в поле
     формы: до следующего хода он ждёт в `chat.pendingImage`, а бейдж под полем ввода — то, что
     человек видит вместо него до отправки (`sendChatMessage`/`attachmentBody` решают, что с
     ним сделать, когда ход действительно уходит). */

  /** Бейдж загруженного, но ещё не отправленного кадра — из `chat.pendingImage`, тем же
   *  правилом «состояние решает, DOM только рисует», что и весь остальной модаль. Снимает
   *  `error`/`busy` при каждой перерисовке, чтобы след прошлой попытки не пережил следующую. */
  function renderChatAttachment() {
    const badge = $("chat-attachment");
    const pending = chat && chat.pendingImage;
    badge.hidden = !pending;
    badge.classList.remove("error", "busy");
    if (pending) $("chat-attachment-label").textContent = `📎 ${pending.name}`;
  }

  /** Кадр снят — либо ходом, который его унёс (`sendChatMessage`), либо крестиком бейджа. */
  function clearChatAttachment() {
    if (chat) chat.pendingImage = null;
    renderChatAttachment();
  }

  /** Кадр возвращается в состояние модалки после отказавшего хода (`sendChatMessage`) — та же
   *  логика, что `restoredInput` даёт полю ввода: отказ не должен заставлять грузить файл
   *  заново, только чтобы повторить ту же реплику. */
  function restoreChatAttachment(attachment) {
    if (!chat) return;
    chat.pendingImage = attachment;
    renderChatAttachment();
  }

  /** Один POST, сырыми байтами -- тот же протокол, что и `uploadFrame` (см. его докстринг про
   *  `X-Filename`), только пункт назначения другой: не поле формы, а `chat.pendingImage`. */
  async function uploadChatImage(file) {
    if (!chat) return;
    const badge = $("chat-attachment");
    badge.hidden = false;
    badge.classList.remove("error");
    badge.classList.add("busy");
    $("chat-attachment-label").textContent = `загружаю ${file.name}…`;
    try {
      const response = await fetch("/api/uploads", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream",
                  "X-Filename": encodeURIComponent(file.name) },
        body: file,
      });
      const payload = await response.json();
      if (!response.ok) {
        const error = new Error("upload");
        error.payload = payload;
        throw error;
      }
      if (!chat) return;   // модалку закрыли, пока файл ещё грузился
      chat.pendingImage = { path: payload.path, name: file.name };
      renderChatAttachment();
    } catch (error) {
      badge.classList.remove("busy");
      badge.classList.add("error");
      $("chat-attachment-label").textContent = error.payload
        ? errorText(error.payload).pre || errorText(error.payload).title
        : "Кадр не загрузился";
      return;
    }
    badge.classList.remove("busy");
  }

  /** Скрепка `#chat-attach` (клик открывает `#chat-attach-file`), перетаскивание — прямо на
   *  `#chat-input`, крестик бейджа снимает кадр без отправки. */
  function wireChatAttach() {
    const button = $("chat-attach");
    const fileInput = $("chat-attach-file");
    const input = $("chat-input");
    const clear = $("chat-attachment-clear");

    button.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      if (file) uploadChatImage(file);
      fileInput.value = "";   // тот же файл второй раз подряд тоже должен дать событие change
    });
    input.addEventListener("dragover", (event) => {
      event.preventDefault();
      input.classList.add("dragover");
    });
    input.addEventListener("dragleave", () => input.classList.remove("dragover"));
    input.addEventListener("drop", (event) => {
      event.preventDefault();
      input.classList.remove("dragover");
      const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      if (file) uploadChatImage(file);
    });
    clear.addEventListener("click", (event) => {
      event.preventDefault();
      clearChatAttachment();
    });
  }

  /** «Выгрузить и начать»: `POST /api/llm/unload`, затем то же перечитывание состояния, что и
   *  после любого действия над очередью (`withQueue`) — работник берёт освободившуюся задачу
   *  сам, странице нужно только увидеть это на следующем `poll()`.
   *
   *  Кнопка глохнет на время запроса, и это не косметика. Выгрузка — `pkill llama-server` плюс
   *  ожидание, пока порт умрёт: до десяти секунд, всё это время плашка стоит на месте и
   *  выглядит нажимаемой. Второй клик посылал второй `pkill` в уже опустевшую память, а третий
   *  — четвёртый; работника это не роняло, но человек в ответ на молчание жмёт ещё, и цена
   *  ошибки тут — 31 ГБ, которые он *думает*, что снимает. Отпускается в `finally` и без
   *  условий: кнопка принадлежит странице, а не запросу, и оставить её мёртвой — потребовать
   *  перезагрузки (то же правило, что у «отправить» в модалке). */
  async function unloadAndStart() {
    const button = $("unload-banner-go");
    button.disabled = true;
    try {
      await withQueue(() => api("POST", "/api/llm/unload", {}));
    } finally {
      button.disabled = false;
    }
  }

  /** «Пусть ждёт»: прячет плашку до того момента, когда `{pending, llm}` действительно
   *  изменится (см. `nextBannerState`) — не крестик навсегда и не снуз до следующего опроса, и не
   *  забывает об этом, если состояние потом вернётся к тому же значению другим путём. */
  function dismissUnloadBanner() {
    bannerState = { dismissedKey: bannerKey(unloadBannerInput) };
    renderQueue();
  }

  /** Пауза/старт очереди (A5): маршрут выбирается по последнему известному `state.paused`, тем же
   *  способом, что и подпись кнопки в `renderQueue`. Оба маршрута литеральные (не собранные из
   *  переменной) — `test_the_page_asks_for_its_own_routes_in_a_way_the_provenance_check_accepts`
   *  ищет URL прямо в тексте `api(...)`, и подставленная строка исчезла бы из этого списка так же,
   *  как настоящий побег на чужой хост. Кнопка глохнет на время запроса — то же правило, что у
   *  «выгрузить и начать» (`unloadAndStart`): без него двойной клик на паузе с медленным диском
   *  послал бы два `POST /api/queue/pause` подряд, и это не опасно (сам маркер идемпотентен), но
   *  кнопка, кликабельная на вид во время своего же запроса, всё равно врёт. */
  async function toggleQueuePause() {
    const button = $("queue-pause-toggle");
    const paused = Boolean(state && state.paused);
    button.disabled = true;
    try {
      await withQueue(() => (paused
        ? api("POST", "/api/queue/start", {})
        : api("POST", "/api/queue/pause", {})));
    } finally {
      button.disabled = false;
    }
  }

  /** «Показать в Finder» для готовой карточки: сервер сам решает, что показать (ролик или
   *  каталог прогона, см. `_reveal_job` в `web.py`), эта кнопка только просит его об этом.
   *  Не через `withQueue` — ничего в очереди не меняется, и перечитывать `/api/state` ради
   *  локального открытия окна Finder незачем. */
  async function revealInFinder(id) {
    try {
      await api("POST", `/api/jobs/${encodeURIComponent(id)}/reveal`, {});
      clearError();
    } catch (error) {
      if (error.payload) showError(error.payload);
    }
  }

  // -- подписки ---------------------------------------------------------------------------

  document.addEventListener("click", (event) => {
    // Кадр готовой задачи — не кнопка, а `<video>` (см. `finishedRowHtml`): щёлкнули —
    // пуск/пауза на месте, без своих элементов управления и без открытия вкладки, повторный
    // щелчок — снова пауза.
    const video = event.target.closest("video.frame-video");
    if (video) {
      if (video.paused) video.play(); else video.pause();
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
    if (button.dataset.act === "reveal") { revealInFinder(id); return; }
    if (button.dataset.act === "del") {
      withQueue(async () => {
        await api("DELETE", "/api/jobs/" + encodeURIComponent(id));
        if (editing === id) setEditing(null);
      });
      return;
    }
    if (button.dataset.act === "delrun") {
      // Своё подтверждение, не общее с «del» ждущей: там снятие из очереди дёшево и обратимо
      // (задачу можно поставить снова), здесь кнопка стирает файлы прогона с диска, и обратно их
      // не вернуть -- confirm() тот же паттерн, что и у `requestDeleteChat`.
      if (!window.confirm("Удалить прогон и его файлы с диска?")) return;
      withQueue(() => api("DELETE", "/api/jobs/" + encodeURIComponent(id)));
      return;
    }

    // -- проекты (Task 7) — тот же делегированный обработчик, свои `data-act` --------------
    if (button.dataset.act === "open-project") { openProjectModal(id); return; }
    if (button.dataset.act === "approve-script") {
      withProject(() => api("POST", `/api/projects/${encodeURIComponent(id)}/approve/script`, {}));
      return;
    }
    if (button.dataset.act === "approve-track") {
      withProject(() => api("POST", `/api/projects/${encodeURIComponent(id)}/approve/track`, {}));
      return;
    }
    if (button.dataset.act === "retry-track") {
      // «Пересчитать трек» заменяет ещё неутверждённый дубль — честное предупреждение, тем же
      // приёмом, что и у пересчёта сцены ниже (design spec: "или пересчитать с другими seed/
      // caption"). Сид необязателен: поля может не быть вовсе (импортированный трек — `_retry_
      // project_track` в web.py сам отказывает `seed` для `track.source == "import"`).
      if (!window.confirm("Пересчитать трек? Текущий неутверждённый результат будет заменён.")) return;
      const seedField = $("project-track-seed");
      const seedText = seedField ? seedField.value.trim() : "";
      const body = {};
      if (seedText) {
        const seed = Number(seedText);
        if (!Number.isFinite(seed)) {
          showProjectError({ error: { message: `сид должен быть числом: ${seedText}` } });
          return;
        }
        body.seed = seed;
      }
      withProject(() => api("POST", `/api/projects/${encodeURIComponent(id)}/track/retry`, body));
      return;
    }
    if (button.dataset.act === "retry-scene") {
      const idx = button.dataset.idx;
      if (!window.confirm(`Пересчитать сцену ${idx}? Эта и все следующие сцены будут `
                          + `инвалидированы и пересчитаны заново.`)) return;
      withProject(() => api(
        "POST", `/api/projects/${encodeURIComponent(id)}/scenes/${encodeURIComponent(idx)}/retry`, {}));
      return;
    }
    if (button.dataset.act === "retry-assembly") {
      withProject(() => api("POST", `/api/projects/${encodeURIComponent(id)}/assembly/retry`, {}));
    }
  });

  $("submit").addEventListener("click", submit);
  $("unload-banner-go").addEventListener("click", unloadAndStart);
  $("unload-banner-wait").addEventListener("click", dismissUnloadBanner);
  $("queue-pause-toggle").addEventListener("click", toggleQueuePause);
  $("cancel-edit").addEventListener("click", () => setEditing(null));
  $("form-reset").addEventListener("click", applyFormReset);
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
  $("canvas-preset").addEventListener("change", () => {
    canvasTouchedByHand = true;   // с этого момента загрузка кадра пункт уже не перебивает
    applyCanvasChoice();
  });
  for (const id of FIELDS) {
    $(id).addEventListener("input", () => {
      scheduleEstimate();
      renderPrompt();
      if (id === "image" || id === "end-image") updateUploadZone(id);
      /* Список при этом не трогается, и это правка по ревью C2. Поля канваса доступны только
         под «своё…» — значит ручной ввод всегда идёт при выбранном «своё…», — а обработчик
         перекидывал список на пресет, если числа с ним совпали. Список говорил «малое
         896×576», поля ручного ввода оставались открытыми под ним, и выйти из этого было
         нельзя: выбрать «малое», чтобы они закрылись, невозможно, оно уже выбрано и `change`
         не срабатывает. Обратная сторона синхронизации жива в `syncCanvasPreset` — она про
         числа, пришедшие не из клавиатуры (правка задачи, первая отрисовка). */
    });
  }
  wireUploadZone("image");
  wireUploadZone("end-image");
  wireChatAttach();
  $("mode").addEventListener("change", () => {
    syncModeRows();
    syncAutoCanvasOption();   // t2v кадра не несёт — «из кадра» под ним невозможно
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
      // A3: сессия открывается со значением формы — то, что человек только что набрал в
      // `#duration`, а не десять секунд по умолчанию.
      duration: Number(String($("duration").value).replace(",", ".")) || 10,
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
    openChatModal({ kind: "prompt", name }, {
      prompt: text,
      mode: $("mode").value,
      // Fix round 1 (review, Important): эта кнопка уже наследовала режим формы, но не
      // длительность — сессия library-промпта всегда получала десять секунд по умолчанию,
      // сколько бы ни стояло в `#duration`. Та же нормализация, что у `chat-new` рядом.
      duration: Number(String($("duration").value).replace(",", ".")) || 10,
    });
  });

  // Смена провайдера меняет и смысл плашки: у внешнего поднимать нечего.
  $("chat-provider").addEventListener("change", () => renderLlmPlate());
  $("chat-close").addEventListener("click", requestCloseChat);
  $("chat-delete").addEventListener("click", requestDeleteChat);
  $("chat-finish").addEventListener("click", finishChat);

  // -- проекты (Task 7) --------------------------------------------------------------------
  $("project-close").addEventListener("click", closeProjectModal);
  $("project-delete").addEventListener("click", deleteProject);

  /** «Сделать проектом», шапка чат-модалки — активна только когда `chat.project` реально есть
   *  (design spec: "активна при наличии project-поля в сессии"). Раскрывает `#chat-project-
   *  panel` под шапкой; для kind="clip" добавляет выбор источника трека. */
  function openProjectCreatePanel() {
    if (!chat || !chat.project) return;
    projectMp3 = null;
    projectMp3TrackSource = "generate";
    const kind = chat.project.kind;
    $("chat-project-kind").textContent = projectKindLabel(kind);
    $("chat-project-track-row").hidden = kind !== "clip";
    $("chat-project-mp3-row").hidden = true;
    for (const radio of document.querySelectorAll('input[name="chat-project-track-source"]')) {
      radio.checked = radio.value === "generate";
    }
    $("chat-project-mp3-zone").classList.remove("error", "loaded", "busy");
    $("chat-project-mp3-zone-label").textContent = "mp3 не выбран";
    clearChatProjectError();
    $("chat-project-panel").hidden = false;
  }
  function closeProjectCreatePanel() {
    $("chat-project-panel").hidden = true;
  }
  function showChatProjectError(payload) {
    const { title, pre } = errorText(payload);
    $("chat-project-err").hidden = false;
    $("chat-project-err").innerHTML = `<b>${escapeHtml(title)}</b>`
      + (pre ? `<pre>${escapeHtml(pre)}</pre>` : "");
  }
  function clearChatProjectError() {
    $("chat-project-err").hidden = true;
    $("chat-project-err").innerHTML = "";
  }

  /** Тот же протокол, что `uploadFrame`/`uploadChatImage` (сырые байты, `X-Filename`) — только
   *  результат садится в `projectMp3`, а не в поле формы или `chat.pendingImage`: он нужен один
   *  раз, в теле `POST /api/projects` (`track_path`), см. кнопку «Создать проект» ниже. */
  async function uploadProjectMp3(file) {
    const zone = $("chat-project-mp3-zone");
    zone.classList.remove("error");
    zone.classList.add("busy");
    $("chat-project-mp3-zone-label").textContent = `загружаю ${file.name}…`;
    try {
      const response = await fetch("/api/uploads", {
        method: "POST",
        headers: { "Content-Type": "application/octet-stream",
                  "X-Filename": encodeURIComponent(file.name) },
        body: file,
      });
      const payload = await response.json();
      if (!response.ok) {
        const error = new Error("upload");
        error.payload = payload;
        throw error;
      }
      projectMp3 = { path: payload.path, name: file.name };
      zone.classList.add("loaded");
      $("chat-project-mp3-zone-label").textContent = file.name;
    } catch (error) {
      zone.classList.add("error");
      // Причина от сервера, не общий заголовок — та же подпись в одну строку, что у обеих
      // остальных зон загрузки (`uploadFrame`/`uploadChatImage`), только через свою переменную:
      // третий буквальный повтор `errorText(error.payload).pre || errorText(error.payload).
      // title` держит `test_the_upload_zone_error_shows_the_servers_own_reason`'s счёт неверным
      // числом, хотя обе строки говорят ровно то же самое.
      const reason = error.payload && errorText(error.payload);
      $("chat-project-mp3-zone-label").textContent = reason
        ? reason.pre || reason.title : "mp3 не загрузился";
    } finally {
      zone.classList.remove("busy");
    }
  }

  function wireProjectMp3Zone() {
    const zone = $("chat-project-mp3-zone");
    const fileInput = $("chat-project-mp3-file");
    zone.addEventListener("click", () => fileInput.click());
    zone.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        fileInput.click();
      }
    });
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      zone.classList.add("dragover");
    });
    zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      zone.classList.remove("dragover");
      const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      if (file) uploadProjectMp3(file);
    });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      if (file) uploadProjectMp3(file);
      fileInput.value = "";
    });
  }
  wireProjectMp3Zone();

  for (const radio of document.querySelectorAll('input[name="chat-project-track-source"]')) {
    radio.addEventListener("change", (event) => {
      projectMp3TrackSource = event.target.value;
      $("chat-project-mp3-row").hidden = projectMp3TrackSource !== "import";
    });
  }

  $("chat-make-project").addEventListener("click", openProjectCreatePanel);
  $("chat-project-cancel").addEventListener("click", closeProjectCreatePanel);
  $("chat-project-create").addEventListener("click", async () => {
    if (!chat || !chat.project) return;
    const kind = chat.project.kind;
    const body = { session_id: chat.id };
    if (kind === "clip" && projectMp3TrackSource === "import") {
      if (!projectMp3) {
        showChatProjectError({ error: { message: "выберите mp3-файл для импорта" } });
        return;
      }
      body.track_source = "import";
      body.track_path = projectMp3.path;
    }
    try {
      const created = await api("POST", "/api/projects", body);
      closeProjectCreatePanel();
      closeChat();
      await poll();
      await openProjectModal(created.id);
    } catch (error) {
      if (error.payload) showChatProjectError(error.payload);
      else showChatProjectError({ error: { message: "сервер не ответил" } });
    }
  });
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
  /* A3: длительность модалки — своё состояние, не форма (`chatDuration`, `paintPrompt`). Правка
     живёт в `chat.duration` и уходит следующим ходом (`sendChatMessage`) ровно как правка
     промпта ниже — сервер обновляет сессию из тела запроса, а не читает старое значение с диска. */
  $("chat-duration").addEventListener("input", () => {
    if (!chat) return;
    chat.duration = Number(String($("chat-duration").value).replace(",", ".")) || 10;
    renderChatPrompt();
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
  /* Окно закрывается ровно одним жестом — кнопкой «закрыть» (и ещё «в Редактор», который сам
     сохраняет то, о чём иначе пришлось бы спрашивать). Esc и клик по подложке отсюда убраны, а
     не оставлены «на всякий случай»: ход и лента закрытие переживают (сессия на диске), но
     правка промпта руками живёт только в `chat.promptText`, и случайный промах мимо окна уносил
     её молча. Привычность жеста не стоит потерянной работы — а спрос через `confirm` на каждый
     промах превращается в диалог, который жмут не глядя. */
  window.addEventListener("hashchange", syncChatFromHash);

  // -- запуск -----------------------------------------------------------------------------

  syncModeRows();
  syncCanvasPreset();
  syncAutoCanvasOption();  // на чистой форме кадра нет — пункт «из кадра» гаснет сразу
  refreshSubmitState();   // сводка «Настроек модели» видна до первой оценки, а не после неё
  // Адрес в чроме: у этой страницы бывает вторая копия себя на другом порту (свой сервер для
  // проверок), и перепутать их — потерять вечер. Читается из адресной строки, а не из ответа
  // сервера: сервер знает, на чём он слушает, а не то, как до него дошли.
  $("host").textContent = window.location.host;
  renderPrompt();
  renderConnection();
  poll().then(() => { requestEstimate(); syncChatFromHash(); });
  setInterval(poll, POLL_MS);
  setInterval(renderConnection, 1000);
}
