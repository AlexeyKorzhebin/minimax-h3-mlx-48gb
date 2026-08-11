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
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
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

/* ===========================================================================
   РАЗБОР ПРОМПТА
   Ничего не переписывает: только показывает, что видит. Формат задан моделью,
   а не нами, и правило, которого мы не знаем, не должно превращаться в запрет.
   =========================================================================== */

const RX = {
  head: /\[\s*\d+(?:\.\d+)?\s*s\s*,[^\]]*\]/g,
  block: /\[\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*s\s*\]/g,
  sec: /^(Characters|Breast physics|Camera|Style)\s*:/gm,
  snd: /^(overall_soundscape|non_diegetic_music)\s*:/gm,
  shot: /\b[A-Z][A-Z]+(?:[ ]+[A-Z]+)*\b(?=[ ,:])/g,
};

/** Непересекающиеся куски текста, которые надо подсветить. */
export function collectSpans(text) {
  const found = [];
  const push = (rx, cls) => {
    rx.lastIndex = 0;
    let m;
    while ((m = rx.exec(text))) found.push({ a: m.index, b: m.index + m[0].length, cls });
  };
  push(RX.head, "head");
  push(RX.block, "blk");
  push(RX.sec, "sec");
  push(RX.snd, "snd");
  push(RX.shot, "shot");
  found.sort((x, y) => x.a - y.a || (y.b - y.a) - (x.b - x.a));
  const out = [];
  let last = -1;
  for (const f of found) if (f.a >= last) { out.push(f); last = f.b; }
  return out;
}

/**
 * Разбор промпта: блоки, сумма таймингов против заявленной длительности,
 * разрывы и перехлёсты, наличие звуковых секций.
 *
 * `audio` — правда ли, что режим озвучен (`t2va`): без звуковых секций у
 * него пропадает звук, а у `t2v` их отсутствие ничего не ломает.
 */
export function analysePrompt(text, declaredSeconds, { audio = true } = {}) {
  const body = String(text == null ? "" : text);
  const declared = Number(declaredSeconds) || 0;
  const spans = collectSpans(body);
  const blocks = [];
  RX.block.lastIndex = 0;
  let m;
  while ((m = RX.block.exec(body))) blocks.push({ a: +m[1], b: +m[2], at: m.index });

  const notes = [];
  const bad = new Set();

  if (!blocks.length) {
    notes.push({ k: "warn", t: "Блоков вида [0.0-2.5s] не найдено — вся сцена одним куском" });
  } else {
    const coverage = blocks.reduce((s, x) => s + Math.max(0, x.b - x.a), 0);
    const span = blocks[blocks.length - 1].b - blocks[0].a;
    const diff = declared - span;
    const k = Math.abs(diff) < 0.05 ? "ok" : (Math.abs(diff) <= 0.5 ? "warn" : "bad");
    notes.push({
      k,
      t: `Блоков ${blocks.length}, покрывают <span class="num">${coverage.toFixed(1)}</span> с `
       + `на промежутке <span class="num">${blocks[0].a.toFixed(1)}–`
       + `${blocks[blocks.length - 1].b.toFixed(1)}</span> с; `
       + `в поле «длительность» <span class="num">${declared.toFixed(1)}</span> с`
       + (k === "ok" ? " — сходится"
                     : ` — расхождение <span class="num">${Math.abs(diff).toFixed(1)}</span> с`),
    });

    for (let i = 1; i < blocks.length; i++) {
      const p = blocks[i - 1];
      const c = blocks[i];
      if (c.a - p.b > 0.001) {
        notes.push({
          k: "warn",
          t: `Разрыв <span class="num">${(c.a - p.b).toFixed(1)}</span> с между `
           + `<span class="num">${p.b.toFixed(1)}</span> и `
           + `<span class="num">${c.a.toFixed(1)}</span>`,
        });
        bad.add(c.at);
      } else if (p.b - c.a > 0.001) {
        notes.push({
          k: "bad",
          t: `Перехлёст <span class="num">${(p.b - c.a).toFixed(1)}</span> с: блок с `
           + `<span class="num">${c.a.toFixed(1)}</span> начинается раньше конца предыдущего`,
        });
        bad.add(c.at);
      }
      if (c.b < c.a) bad.add(c.at);
    }
  }

  const hasSound = /^overall_soundscape\s*:/m.test(body);
  const hasMusic = /^non_diegetic_music\s*:/m.test(body);
  const tail = audio ? " — у режима t2va без него пропадает звук" : "";
  notes.push(hasSound
    ? { k: "ok", t: "overall_soundscape на месте" }
    : { k: audio ? "bad" : "warn", t: "Нет overall_soundscape" + tail });
  notes.push(hasMusic
    ? { k: "ok", t: "non_diegetic_music на месте" }
    : { k: audio ? "bad" : "warn", t: "Нет non_diegetic_music" + tail });

  return { spans, blocks, notes, bad };
}

/** Слой подсветки: тот же текст, что в поле, с обёрнутыми кусками. */
export function highlightHtml(text, analysis) {
  const body = String(text == null ? "" : text);
  let html = "";
  let i = 0;
  for (const s of analysis.spans) {
    html += escapeHtml(body.slice(i, s.a));
    const cls = s.cls === "blk" && analysis.bad.has(s.a) ? "blk bad" : s.cls;
    html += `<mark class="${cls}">${escapeHtml(body.slice(s.a, s.b))}</mark>`;
    i = s.b;
  }
  return html + escapeHtml(body.slice(i)) + "\n";
}

/** Полоска покрытия под полем: блоки, разрывы, перехлёсты и неописанный хвост. */
export function scaleHtml(analysis, declaredSeconds) {
  const blocks = analysis.blocks;
  if (!blocks.length) return "";
  const declared = Number(declaredSeconds) || 0;
  const end = Math.max(declared, blocks[blocks.length - 1].b) || 1;
  const pc = (x) => (x / end) * 100 + "%";
  let html = "";
  blocks.forEach((b, n) => {
    html += `<div class="seg" style="left:${pc(b.a)};width:${pc(Math.max(0, b.b - b.a))}"`
          + ` title="блок ${n + 1}: ${b.a}–${b.b} с">${(b.b - b.a).toFixed(1)}</div>`;
  });
  for (let i = 1; i < blocks.length; i++) {
    const p = blocks[i - 1];
    const c = blocks[i];
    if (c.a - p.b > 0.001) {
      html += `<div class="gap" style="left:${pc(p.b)};width:${pc(c.a - p.b)}"`
            + ` title="разрыв ${(c.a - p.b).toFixed(1)} с"></div>`;
    } else if (p.b - c.a > 0.001) {
      html += `<div class="over" style="left:${pc(c.a)};width:${pc(p.b - c.a)}"`
            + ` title="перехлёст ${(p.b - c.a).toFixed(1)} с"></div>`;
    }
  }
  const lastEnd = blocks[blocks.length - 1].b;
  if (declared - lastEnd > 0.001) {
    html += `<div class="tail" style="left:${pc(lastEnd)};width:${pc(declared - lastEnd)}">`
          + `не описано ${(declared - lastEnd).toFixed(1)} с</div>`;
  }
  return html;
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

/** Завершённые за сутки, свежие сверху. Задача без `finished_at` не
 *  отбрасывается: она закончилась, момент просто не записан. */
export function finishedWithin(jobs, now, hours = FINISHED_WINDOW_HOURS) {
  const at = (now instanceof Date ? now : new Date(now || Date.now())).getTime();
  const window = hours * 3600 * 1000;
  return (Array.isArray(jobs) ? jobs : [])
    .filter((job) => {
      const stamp = Date.parse(job.finished_at || "");
      return Number.isNaN(stamp) ? true : at - stamp <= window;
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
 * Ждущая задача: три действия — править, наверх, удалить.
 *
 * Действия есть только здесь. У идущей и завершённой их нет вовсе — не серые
 * кнопки, а их отсутствие: серая кнопка обещает, что когда-нибудь нажмётся.
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
    + `<button data-act="edit" data-id="${id}">Править</button>`
    + `<button data-act="top" data-id="${id}">Наверх</button>`
    + `<button data-act="del" data-id="${id}">Удалить</button>`
    + `</span>`
    + `</div>`;
}

/**
 * Завершённая задача: код возврата виден всегда, причина — у упавших.
 * Ни одного действия: прогон уже случился, отменять и поднимать нечего.
 */
export function finishedRowHtml(job) {
  const code = job.exit_code;
  const ok = code === 0;
  const clip = ok ? clipUrl(job) : null;
  const stem = String(job.output_stem || "");
  const name = stem.slice(stem.lastIndexOf("/") + 1);
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
    case "host_not_allowed":
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

  function renderPrompt() {
    const text = $("prompt").value;
    const declared = Number(String($("duration").value).replace(",", ".")) || 0;
    const analysis = analysePrompt(text, declared, { audio: $("mode").value === "t2va" });
    $("hl").innerHTML = highlightHtml(text, analysis);
    $("scale").innerHTML = scaleHtml(analysis, declared);
    $("parse").innerHTML = analysis.notes.map((n) => `<li class="${n.k}">${n.t}</li>`).join("");

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
      + `<option value=" new">— новый файл… —</option>`;
    select.value = selected || "";
    // Каталог промптов сервер называет сам; путь во флаге должен быть его,
    // а не собранным здесь из догадок.
    select.dataset.dir = answer.dir;
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
    if (!name || name === " new") {
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
        window.scrollTo({ top: 0, behavior: "smooth" });
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
    const button = event.target.closest("button[data-act]");
    if (!button) return;
    const id = button.dataset.id;
    if (button.dataset.act === "edit") { setEditing(id); return; }
    if (button.dataset.act === "top") {
      withQueue(() => api("POST", `/api/jobs/${encodeURIComponent(id)}/top`, {}));
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
    if (name === " new") { event.target.value = ""; promptFromFile = null; renderPrompt(); }
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

  // -- запуск -----------------------------------------------------------------------------

  syncModeRows();
  renderPrompt();
  renderConnection();
  loadPromptList();
  poll().then(() => { requestEstimate(); });
  setInterval(poll, POLL_MS);
  setInterval(renderConnection, 1000);
}
