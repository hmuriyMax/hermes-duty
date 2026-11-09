/**
 * Hermes Duty — шейпер данных страницы /duty (чистые функции, без React и DOM).
 *
 * Здесь НЕТ классификации состояний и нет копии приоритета бэкенда: единственный
 * источник таксономии — `dashboard/plugin_api.py` (`WAITING_PRIORITY`,
 * `waiting.state`). `/now` уже отдаёт дела отсортированными по приоритету, поэтому
 * группы строятся в порядке первого появления состояния в ответе — страница
 * ничего не пересортировывает и не выводит состояние из фактов. `STATE_META`
 * ниже — только подписи и цвета бейджей для известных состояний; незнакомое
 * состояние рисуется своим именем серым.
 *
 * Группировка списка — по `repo` (0.0.6: доски нет): дело адресуется своим
 * `d_<hex>`, а «чей это рой» показывает репозиторий. Дела без репозитория идут
 * последней группой `без репозитория`; порядок внутри группы — ответ бэкенда.
 * Тот же блок `repos` считает CLI, поэтому числа дел и групп совпадают на вебе,
 * десктопе и в терминале.
 *
 * Два набора берутся из ответа, а не дублируются тут: порядок состояний — по
 * ключам `by_state` (чипы фильтра), набор «падений» — по `crash_outcomes`
 * (красный тон исхода). Так расширение набора на бэкенде не разъезжается с UI.
 *
 * ВАЖНО: этот файл не импортируется из `desktop/plugin.js`. Загрузчик
 * десктопных плагинов исполняет `plugin.js` как blob-модуль и отказывает любому
 * спекулятору вне `@hermes/plugin-sdk` / `react*` (включая относительные пути),
 * поэтому тело модуля до строки `export {` дословно вшито в `plugin.js` между
 * маркерами `duty:model-start` / `duty:model-end`. Синхронизация —
 * `bin/duty-model-sync`, расхождение ловит `tests/model.test.mjs`.
 */

// Подпись и тон бейджа по состоянию — только презентация, не правила. Подпись
// `wait_human` намеренно не говорит «человека»: факт ставится по любому живому
// вопросу пула, включая вопрос агенту (`origin='improver'` в evidence).
// `no_signal` — серый, а не зелёный: дело со `status=running` без рана, pid и
// heartbeat не работает, у него просто нет живого сигнала.
const STATE_META = {
  wait_human: { label: 'ждёт ответа', tone: 'yellow' },
  capability: { label: 'нужен доступ', tone: 'purple' },
  dead_lease: { label: 'лизинг истёк', tone: 'red' },
  crashed: { label: 'упало', tone: 'red' },
  working: { label: 'работает', tone: 'green' },
  no_signal: { label: 'нет сигнала', tone: 'gray' },
  wait_slot: { label: 'ждёт слот', tone: 'gray' },
  wait_gate: { label: 'ждёт гейт', tone: 'gray' },
}

// Подпись `wait_human` по `human_kind` бэкенда: `question` — есть живой вопрос,
// на который можно ответить, `status` — вопроса нет, решение принимается по
// статусу. Одна подпись на оба случая возвращала бы «ждёт ответа» блокировке без
// вопроса, пока шапка в том же кадре пишет «0 вопросов + 1 блокировка».
const WAIT_HUMAN_LABEL = {
  question: 'ждёт ответа',
  status: 'ждёт решения',
}

const TONE_COLOR = {
  yellow: 'var(--ui-yellow)',
  purple: 'var(--ui-purple)',
  red: 'var(--ui-red)',
  green: 'var(--ui-green)',
  gray: 'var(--ui-text-tertiary)',
  blue: 'var(--ui-blue)',
}

// Тон исхода попытки: успех — зелёное, отложенное — жёлтое. Падений здесь нет:
// их набор приходит с бэкенда (`crash_outcomes`), чтобы красный тон не разъехался
// с правилами сервера.
// Исход → тон. `failed` — собственный исход попытки, он красный без всякого
// набора «падений»: без этой записи провал попадал в «незнакомый исход» и
// выглядел серым. Краши (`crashed`, `gave_up`) по-прежнему узнаются только из
// `crash_outcomes` ответа — UI их не угадывает.
const OUTCOME_TONE = {
  done: 'green',
  completed: 'green',
  succeeded: 'green',
  failed: 'red',
  rate_limited: 'yellow',
  reclaimed: 'yellow',
}

const LEASE_LABEL = {
  live: 'идёт',
  expired: 'истёк',
  none: 'нет',
  // Срок лизинга нигде не записан: не «неизвестно» (слово ни о чём), а факт —
  // лизинга нет, и жив ли процесс, журнал не говорит.
  unknown: 'без лизинга',
}

/**
 * Мета состояния; неизвестное состояние показываем как есть серым.
 *
 * `humanKind` уточняет только `wait_human`: «ждёт ответа» — есть живой вопрос,
 * «ждёт решения» — вопроса нет. Остальные состояния от `human_kind` не зависят.
 */
function stateMeta(state, humanKind) {
  const key = String(state || '')
  const meta = STATE_META[key] || { label: key || '—', tone: 'gray' }
  if (key === 'wait_human' && WAIT_HUMAN_LABEL[String(humanKind || '')]) {
    return { label: WAIT_HUMAN_LABEL[String(humanKind)], tone: meta.tone }
  }
  return meta
}

/** Русская форма числа: `1 вопрос`, `2 вопроса`, `5 вопросов`. */
function plural(n, one, few, many) {
  const tail = Math.abs(Number(n) || 0) % 100
  const last = tail % 10
  if (tail > 10 && tail < 20) return many
  if (last === 1) return one
  if (last >= 2 && last <= 4) return few
  return many
}

/**
 * Сколько дел ждёт человека и в каком виде — по `waiting.human_kind` дел.
 *
 * Это два разных «нужен человек»: `question` — есть живой вопрос, на который
 * можно ответить, `status` — вопроса нет, решение принимается по статусу
 * (блок, разбор, ревью, мердж). Считать их одним числом значит обещать вопрос,
 * которого нет, поэтому UI показывает «N вопросов + M блокировок».
 */
function humanCounts(deals) {
  const counts = { question: 0, status: 0 }
  for (const deal of deals || []) {
    const kind = deal && deal.humanKind
    if (kind === 'question' || kind === 'status') counts[kind] += 1
  }
  return counts
}

/** Строка «N вопросов + M блокировок» для шапки экрана «Сейчас». */
function humanLine(counts) {
  const questions = Number(counts && counts.question) || 0
  const statuses = Number(counts && counts.status) || 0
  return `${questions} ${plural(questions, 'вопрос', 'вопроса', 'вопросов')} + ${statuses} ${plural(
    statuses,
    'блокировка',
    'блокировки',
    'блокировок',
  )}`
}

// Подпись группы «репозиторий не назван»: NULL в `repo` — законное состояние
// (`dashboard/plugin_api.py`, `_repos_of`), и в списке оно называется словами, а не
// пустой строкой. Слово то же, что у CLI (`dashboard/cli.py`, WITHOUT_REPO_LABEL):
// терминал и экран обязаны называть одну группу одинаково.
const WITHOUT_REPO_LABEL = 'без репозитория'

// Значение фильтра «все репозитории»: путь репозитория не бывает `*`, а пустая
// строка занята группой «без репозитория» — ровно так же её называет роут
// (`?repo=` — только дела без репозитория, docs/RUNBOOK.md).
const ALL_REPOS = '*'

/** Подпись репозитория: путь как есть, дела без него — «без репозитория». */
function repoLabel(repo) {
  return String(repo || '') || WITHOUT_REPO_LABEL
}

/**
 * Короткое имя репозитория для шапки: последний сегмент пути.
 *
 * В store репозиторий живёт абсолютным путём (`/Users/…/plugins/hermes-duty`), и
 * этот путь как кикер заголовка — шум: человеку нужно имя. Полный путь остаётся
 * в подсказке, `repoLabel` не меняется — по нему группируются дела.
 */
function repoShort(repo) {
  const text = String(repo || '').trim()
  if (!text) return WITHOUT_REPO_LABEL
  const tail = text.replace(/[\\/]+$/, '').split(/[\\/]/).pop()
  return tail || text
}

/** Имя файла без каталогов: полный путь — в подсказке, здесь только хвост. */
function fileLabel(path) {
  const text = String(path || '').trim()
  if (!text) return ''
  const tail = text.replace(/[\\/]+$/, '').split(/[\\/]/).pop()
  return tail || text
}

/** Возраст/длительность в человекочитаемом виде: `45 с`, `12 мин`, `3 ч`, `2 д`. */
function fmtAge(sec) {
  if (sec === null || sec === undefined || sec === '') return '—'
  const value = Number(sec)
  if (!Number.isFinite(value)) return '—'
  const s = Math.max(0, Math.floor(value))
  if (s < 60) return `${s} с`
  if (s < 3600) return `${Math.floor(s / 60)} мин`
  if (s < 86400) return `${Math.floor(s / 3600)} ч`
  return `${Math.floor(s / 86400)} д`
}

/** Заголовок одной строкой с многоточием: ровно не длиннее `max` символов. */
function fmtTitle(title, max) {
  const limit = Number.isFinite(Number(max)) && Number(max) > 4 ? Math.floor(Number(max)) : 64
  const text = String(title ?? '').replace(/\s+/g, ' ').trim()
  if (!text) return '—'
  if (text.length <= limit) return text
  return `${text.slice(0, limit - 1).trimEnd()}…`
}

/** Время события как ЧЧ:ММ:СС — попытки идут минутами, секунды нужны для порядка. */
function fmtClock(at) {
  if (!at) return '—'
  const dt = new Date(Number(at) * 1000)
  if (Number.isNaN(dt.getTime())) return '—'
  const pad = (n) => String(n).padStart(2, '0')
  return `${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`
}

/** Одна строка таблицы «сейчас» из дела ответа `/now`. */
function shapeDeal(deal) {
  const d = deal && typeof deal === 'object' ? deal : {}
  const attempt = d.attempt && typeof d.attempt === 'object' ? d.attempt : null
  const lease = d.lease && typeof d.lease === 'object' ? d.lease : {}
  const action = d.last_action && typeof d.last_action === 'object' ? d.last_action : {}
  const waiting = d.waiting && typeof d.waiting === 'object' ? d.waiting : {}
  const stage = d.stage && typeof d.stage === 'object' ? d.stage : null
  const humanKind = String(waiting.human_kind || '')
  const meta = stateMeta(waiting.state, humanKind)
  const pidAlive = attempt ? attempt.pid_alive : null
  const leaseState = String(lease.state || 'none')
  const attemptAge = attempt && attempt.age_sec !== undefined ? attempt.age_sec : null
  const lastAge = action.age_sec !== undefined ? action.age_sec : null
  const repo = String(d.repo || '')
  // Исполнитель: у дела он не всегда записан (`assignee` в store пуст у дел,
  // созданных без `--assignee`), но роль текущего этапа известна из журнала —
  // и это тот же ответ на вопрос «кто этим занят». Пустая колонка вместо него
  // читалась как «никто» (09.11).
  const assigned = String(d.assignee || '').trim()
  const stageRole = stage ? String(stage.role || '').trim() : ''
  const performer = assigned || stageRole || '—'
  const performerNote = assigned
    ? 'исполнитель дела'
    : stageRole
      ? `роль текущего этапа${stage && stage.id ? ` «${stage.id}»` : ''}`
      : 'исполнитель не записан'
  // «pid не записан» и «состояние pid неизвестно» (`os.kill` вернул OSError) — разные факты.
  let attemptNote = 'нет попытки'
  if (attempt) {
    attemptNote = attempt.pid ? 'состояние pid неизвестно' : 'pid не записан'
    if (pidAlive === true) attemptNote = 'pid жив'
    if (pidAlive === false) attemptNote = 'pid мёртв'
    // Часть фактов попытки бэкенд добрал из карточки, а не из записи рана
    // (`attempt.source`): подпись не должна звучать как факт рана.
    if (attempt.source === 'task') attemptNote = `${attemptNote}, факты карточки`
  }
  return {
    // Репозиторий дела (0.0.6): доски нет, «чей это рой» показывает группировка.
    // Пусто — законное состояние «без репозитория», подпись даёт `repoLabel`.
    repo,
    repoLabel: repoLabel(repo),
    // Хвост пути для списков и таймлайна: полный адрес — в подсказке и карточке.
    repoShort: repoShort(repo),
    id: String(d.id || ''),
    title: fmtTitle(d.title),
    titleFull: String(d.title || ''),
    // Начало дела: `created_at` из store; `ageSec` — возраст из ответа. Общий
    // таймлайн тянет полосу дела от начала и берёт одно из двух.
    createdAt: numOrNull(d.created_at),
    ageSec: numOrNull(d._age_sec),
    stageRole,
    assignee: performer,
    assigneeNote: performerNote,
    status: String(d.status || '—'),
    state: String(waiting.state || ''),
    stateLabel: meta.label,
    tone: meta.tone,
    evidence: String(waiting.evidence || ''),
    // `human_kind` бэкенда: `question` — есть живой вопрос, `status` — человек
    // нужен, но вопроса нет (`waiting.human_kind`, контракт docs/JOURNAL.md).
    // Пустое значение — состояние не про ожидание человека. Подпись бейджа тоже
    // считается по нему (см. `stateMeta`), иначе «ждёт ответа» звучит у блокировки
    // без вопроса.
    humanKind,
    attemptAge,
    attemptAgeText: fmtAge(attemptAge),
    attemptNote,
    attemptRunId: attempt ? (attempt.run_id ?? null) : null,
    leaseState,
    leaseText: LEASE_LABEL[leaseState] || leaseState,
    leaseExpiresIn: lease.expires_in_sec ?? null,
    lastActionAge: lastAge,
    lastActionAgeText: fmtAge(lastAge),
    lastActionKind: String(action.kind || '—'),
    lastActionSummary: String(action.summary || ''),
    // Этап и номер попытки из журнала (`/now.stage`): «что сейчас» видно в
    // строке, не открывая дело. Пусто — ни одной попытки у дела не было, и
    // подставлять «этап 0» значило бы выдумать граф.
    stageId: stage ? String(stage.id || '') : '',
    stageAttempt: stage ? Number(stage.attempt) || 0 : 0,
    stageLive: stage ? stage.live === true : false,
    stageText: stage
      ? `${String(stage.id || '—')} · попытка ${Number(stage.attempt) || 0}${stage.live === true ? ' · идёт' : ''}`
      : '',
    search: `${d.repo || ''} ${d.id || ''} ${d.title || ''} ${d.assignee || ''} ${stageRole} ${waiting.state || ''}`.toLowerCase(),
  }
}

/** Сколько дел в каждом состоянии — по строкам, а не по ответу бэкенда. */
function stateCounts(deals) {
  const counts = {}
  for (const deal of deals || []) {
    const state = (deal && deal.state) || '—'
    counts[state] = (counts[state] || 0) + 1
  }
  return counts
}

/* ---------------------------------- общий таймлайн активных дел (0.0.9) */

/**
 * Окно мини-таймлайна: сутки. Шире — картинка перестаёт читаться (дела недельной
 * давности сжимают сегодняшние в пиксель), а вопрос у верхушки страницы —
 * «что происходило сегодня», а не «вся история».
 */
const TL_WINDOW_SEC = 24 * 3600
/** Потолок строк таймлайна: активные плюс свежие завершённые, остальное — числом. */
const TL_MAX_ROWS = 14

/**
 * Тон полосы — состояние этапа, и только оно.
 *
 * Цвета берутся из той же таблицы, что у строк этапов и узлов графа на странице
 * дела (`stageMeta`): пройден — зелёный, идёт — акцент, отказ — красный. Свой
 * цвет на каждое имя этапа читался бы как легенда без подписи: на странице дела
 * такого словаря нет, и таймлайн спорил бы с ней (09.11, Макс: «цвета этапов
 * должны соответствовать принятым цветам на странице дела»).
 */
const TL_SEG_STATES = ['done', 'running', 'failed']

/** Шаг сетки: чем короче окно, тем мельче деления. */
const TL_TICKS = [
  [20 * 60, 5 * 60],
  [60 * 60, 10 * 60],
  [3 * 3600, 30 * 60],
  [8 * 3600, 3600],
  [TL_WINDOW_SEC, 3 * 3600],
]

/**
 * Полосы этапов внутри «сосиски» дела.
 *
 * Границы считаются в долях самой сосиски, а не оси: дело — единая фигура на
 * общей сетке (09.11, Макс), поэтому этап не может вылезти за её край и не
 * требует обрезки по форме. Пауза между попытками остаётся пустым местом внутри
 * сосиски — это факт («дело ждало диспетчера»), а не дефект рисунка.
 */
function timelineSegments(spans, barFrom, barTo) {
  const a = numOrNull(barFrom)
  const b = numOrNull(barTo)
  if (a === null || b === null) return { segments: [], sec: {} }
  const width = Math.max(1, b - a)
  const segments = []
  const sec = { done: 0, running: 0, failed: 0 }
  for (const item of Array.isArray(spans) ? spans : []) {
    const from = numOrNull(item && item.start)
    const end = numOrNull(item && item.end)
    if (from === null) continue
    const to = end === null ? b : end
    const left = Math.max(a, from)
    const right = Math.min(b, to)
    if (right <= left) continue
    const outcome = String((item && item.outcome) || '')
    // Провал и инфраструктурный отказ дают один цвет: он говорит «этап не
    // дошёл», а какой именно отказ — в подсказке.
    const state = outcome === 'failed' ? 'failed' : end === null ? 'running' : 'done'
    const meta = stageMeta(state)
    const duration = Math.max(0, to - from)
    sec[state] += duration
    segments.push({
      stage: String((item && item.stage) || ''),
      state,
      tone: meta.tone,
      // Доли сосиски: экран не пересчитывает координаты, он их только ставит.
      leftPct: Number((((left - a) / width) * 100).toFixed(2)),
      widthPct: Number(Math.max(0.6, ((right - left) / width) * 100).toFixed(2)),
      clipped: from < a,
      durationText: fmtAge(duration),
      title:
        `${String((item && item.stage) || 'этап')}` +
        (Number.isInteger(item && item.attempt) ? ` · попытка ${item.attempt}` : '') +
        ` · ${meta.label} · ${fmtAge(duration)}`,
    })
  }
  return { segments, sec }
}

/**
 * Деления оси: ровные шаги от круглого времени, а не от начала дел.
 *
 * `stepPct`/`firstPct` — те же деления как фон полотна: сетка рисуется
 * градиентом, поэтому все сосиски лежат на одной решётке, и вертикали сходятся
 * от строки к строке без отдельных элементов на каждую линию.
 */
function timelineTicks(axisFrom, axisTo) {
  const a = numOrNull(axisFrom)
  const b = numOrNull(axisTo)
  if (a === null || b === null || b <= a) return { ticks: [], stepPct: 0, firstPct: 0, stepSec: 0 }
  const span = b - a
  const step = (TL_TICKS.find(([limit]) => span <= limit) || TL_TICKS[TL_TICKS.length - 1])[1]
  const ticks = []
  for (let at = Math.ceil(a / step) * step; at <= b; at += step) {
    // На коротком окне секунды — факт (шаг 5 минут), на длинном — шум.
    const label = step < 600 ? fmtClock(at) : fmtClock(at).slice(0, 5)
    ticks.push({ leftPct: Number((((at - a) / span) * 100).toFixed(2)), label, at })
  }
  return {
    ticks,
    stepSec: step,
    stepPct: Number(((step / span) * 100).toFixed(3)),
    firstPct: ticks.length ? ticks[0].leftPct : 0,
  }
}

/**
 * Мини-таймлайн дел: одна «сосиска» на дело на общей сетке.
 *
 * Верхушка страницы отвечает на «что происходило и сколько это длится» одним
 * взглядом: активные дела тянутся до `now`, завершённые — до своего `finishedAt`
 * (09.11, Макс: «показывать и завершённые»). Внутри сосиски — полосы этапов,
 * цвет которых говорит состояние (см. `TL_SEG_STATES`), пустое место — пауза
 * между попытками. Геометрия считается здесь, а не на поверхности: веб и
 * десктоп обязаны рисовать одну картину.
 *
 * `spansById` — границы этапов из журнала (`GET /spans`): без них сосиска
 * заливается тоном состояния целиком, как до 0.0.9.
 *
 * Окно — не длиннее суток (`TL_WINDOW_SEC`). Дело, начавшееся раньше окна, не
 * пропадает: его сосиска начинается у левого края и помечена `clipped` — рисунок
 * говорит «началось раньше», а не выдумывает начало. Дело без записанного начала
 * рисуется полупрозрачной сосиской от края (`exact: false`).
 */
function dealTimeline(activeDeals, finishedDeals, now, spansById) {
  const moment = numOrNull(now)
  const windowFrom = moment === null ? null : moment - TL_WINDOW_SEC
  const spans = spansById && typeof spansById === 'object' ? spansById : {}
  const rows = []
  for (const deal of Array.isArray(activeDeals) ? activeDeals : []) {
    if (!deal) continue
    const started = numOrNull(deal.createdAt)
    const age = numOrNull(deal.ageSec)
    const from = started !== null ? started : age !== null && moment !== null ? moment - age : null
    rows.push({ deal, from, end: null, open: true, stateLabel: deal.stateLabel || deal.state || '', tone: deal.tone || 'gray' })
  }
  for (const deal of Array.isArray(finishedDeals) ? finishedDeals : []) {
    if (!deal) continue
    const end = numOrNull(deal.finishedAt)
    // Завершённое дело вне окна не рисуем вовсе: его сосиска была бы нулевой
    // длины у левого края и только шумела бы.
    if (end !== null && windowFrom !== null && end < windowFrom) continue
    rows.push({
      deal,
      from: numOrNull(deal.createdAt),
      end,
      open: false,
      stateLabel: deal.statusLabel || deal.status || 'завершено',
      tone: deal.tone || 'gray',
    })
  }
  const visible = rows.slice(0, TL_MAX_ROWS)
  const omitted = rows.length - visible.length
  const axisTo = moment !== null ? moment : visible.reduce((max, row) => (row.end !== null && (max === null || row.end > max) ? row.end : max), null)
  const startOf = (row) => {
    const raw = row.from !== null ? row.from : row.end
    if (raw === null) return null
    if (windowFrom !== null && raw < windowFrom) return windowFrom
    return raw
  }
  const starts = visible.map(startOf).filter((value) => value !== null)
  // Ось — по фактическим делам, но не длиннее суток: пустые часы слева
  // (когда дела начались час назад) растянули бы сосиски в нитки.
  const earliest = starts.length ? Math.min(...starts) : null
  const axisFrom = windowFrom !== null ? (earliest === null ? windowFrom : Math.max(windowFrom, earliest)) : earliest
  const span = axisFrom === null || axisTo === null ? 0 : Math.max(1, axisTo - axisFrom)
  const pct = (at) => Math.max(0, Math.min(100, ((at - axisFrom) / span) * 100))
  const legendSec = { done: 0, running: 0, failed: 0 }
  const outRows = visible.map((row) => {
    const from = startOf(row)
    const to = row.open ? axisTo : row.end
    const duration = from === null || to === null ? null : Math.max(0, to - from)
    const clipped = row.from !== null && windowFrom !== null && row.from < windowFrom
    const deal = row.deal
    // Сосиска занимает ровно время дела на оси; всё внутри считается её долями.
    const barLeft = from === null ? 0 : pct(from)
    const barRight = to === null ? 100 : pct(to)
    const barFrom = from === null ? axisFrom : Math.max(axisFrom, from)
    const barTo = to === null ? axisTo : Math.min(axisTo, to)
    const { segments, sec } = timelineSegments(spans[String(deal.id || '')], barFrom, barTo)
    for (const state of TL_SEG_STATES) legendSec[state] += sec[state]
    const title = String(deal.titleFull || deal.title || '')
    const repoFull = deal.repoLabel || String(deal.repo || '')
    return {
      key: String(deal.id || ''),
      id: String(deal.id || ''),
      repo: deal.repoShort || repoShort(deal.repo),
      repoFull,
      title,
      open: row.open,
      state: deal.state || deal.status || '',
      stateLabel: row.stateLabel,
      tone: row.tone,
      stageText: deal.stageText || '',
      stageLive: deal.stageLive === true,
      performer: deal.assignee || '',
      segments,
      durationText: duration === null ? 'начало неизвестно' : fmtAge(duration),
      bar: {
        leftPct: Number(barLeft.toFixed(2)),
        widthPct: Number(Math.max(1.5, barRight - barLeft).toFixed(2)),
      },
      // Начало раньше окна или неизвестно вовсе: рисунок помечает это, а не
      // выдаёт догадку за факт.
      clipped,
      exact: row.from !== null,
      hint:
        `${title || '(без названия)'}\n${deal.id || ''}${repoFull ? ` · ${repoFull}` : ''}` +
        `\n${row.stateLabel || '—'}${duration === null ? '' : ` · ${fmtAge(duration)}`}` +
        (segments.length
          ? `\n${segments
              .map((segment) => `${segment.stage} ${segment.durationText}${segment.state === 'failed' ? ' ✕' : segment.state === 'running' ? ' …' : ''}`)
              .join(' · ')}`
          : '') +
        (clipped ? '\nначалось раньше окна суток' : ''),
    }
  })
  const axis = timelineTicks(axisFrom, axisTo)
  return {
    empty: outRows.length === 0,
    windowText: axisFrom === null || axisTo === null ? '' : `${fmtSpan(axisFrom, axisTo)} · окно до суток`,
    ticks: axis.ticks,
    grid: { stepPct: axis.stepPct, firstPct: axis.firstPct, stepSec: axis.stepSec },
    // Легенда называет состояния, а не этапы: цвет в сосиске — это состояние.
    legend: TL_SEG_STATES.filter((state) => legendSec[state] > 0).map((state) => {
      const meta = stageMeta(state)
      return { state, tone: meta.tone, label: meta.label, durationText: fmtAge(legendSec[state]) }
    }),
    omitted,
    rows: outRows,
  }
}

/**
 * Подпись состояния для набора строк: `wait_human` с вопросом — «ждёт ответа»,
 * без вопроса — «ждёт решения» (та же развилка, что у бейджа, см. `stateMeta`).
 *
 * Нужна чипам фильтра и подписи группы: подпись считается по видимым делам, а не
 * берётся из одной группы, которой после 0.0.6 (группировка — репозиторий) уже
 * нет.
 */
function stateLabel(state, deals) {
  const kinds = new Set(
    (deals || []).filter((deal) => deal && deal.state === state).map((deal) => deal.humanKind || ''),
  )
  return kinds.size === 1 ? stateMeta(state, [...kinds][0]).label : stateMeta(state).label
}

/**
 * Группы строк по `repo`: сначала названные репозитории, «без репозитория» —
 * отдельной группой и последней.
 *
 * Порядок названных — по имени: репозиторий это подпись группы, а не приоритет
 * (приоритет задаёт бэкенд и он виден по строкам — `/now` сортирует дела
 * `WAITING_PRIORITY`). Внутри группы порядок ответа не пересортировывается, иначе
 * два опроса одного снимка давали бы разный список. То же самое делает CLI
 * (`dashboard/cli.py`, `_repo_groups`): числа дел и групп обязаны совпадать.
 */
function groupDeals(deals) {
  const named = new Map()
  const without = []
  for (const deal of deals || []) {
    const repo = String((deal && deal.repo) || '')
    if (!repo) {
      without.push(deal)
      continue
    }
    if (!named.has(repo)) named.set(repo, [])
    named.get(repo).push(deal)
  }
  const groups = [...named.keys()].sort().map((repo) => ({
    key: repo,
    repo,
    // В списке — только хвост пути (`payout-sx-widgets-api`): абсолютный путь
    // как заголовок группы читается как шум. Полный путь остаётся в `repo` и в
    // подсказке шапки; по `repo` же группируются дела.
    label: repoShort(repo),
    // Выдуманного тона у группы нет: она про репозиторий, а не про ожидание.
    // Состояния видны в шапке группы и бейджами в строках.
    tone: '',
    state: '',
    count: named.get(repo).length,
    deals: named.get(repo),
    byState: stateCounts(named.get(repo)),
  }))
  if (without.length) {
    groups.push({
      key: '',
      repo: '',
      label: WITHOUT_REPO_LABEL,
      tone: '',
      state: '',
      count: without.length,
      deals: without,
      byState: stateCounts(without),
    })
  }
  return groups
}

/**
 * Состояния группы одной строкой: `ждёт ответа 2 · работает 1`.
 *
 * Порядок — по первому появлению в группе (то есть по приоритету ответа), а не
 * по таблице подписей: второго списка приоритетов в UI нет.
 */
function groupStatesText(group) {
  const rows = []
  for (const deal of (group && group.deals) || []) {
    const state = deal.state || '—'
    const found = rows.find((row) => row.state === state)
    if (found) {
      found.count += 1
      continue
    }
    rows.push({ state, count: 1 })
  }
  return rows.map((row) => `${stateLabel(row.state, group.deals)} ${row.count}`).join(' · ')
}

/**
 * Группы строк по `waiting.state` — в порядке первого появления в ответе.
 *
 * Нужны экрану «Почему стоит»: там группировка по ожиданию и есть объяснение
 * («почему стоит» — это состояние), а список дел с 0.0.6 группируется по `repo`.
 * Порядок групп приоритетный без второго списка приоритетов: `/now` сортирует
 * дела `WAITING_PRIORITY`, а строки внутри группы сохраняют порядок ответа.
 */
function groupByState(deals) {
  const buckets = new Map()
  for (const deal of deals || []) {
    const state = (deal && deal.state) || '—'
    if (!buckets.has(state)) buckets.set(state, [])
    buckets.get(state).push(deal)
  }
  const groups = []
  for (const [state, rows] of buckets) {
    const meta = stateMeta(state)
    groups.push({
      key: state,
      state,
      // «ждёт ответа» — только если вопрос есть у каждого дела в группе; иначе
      // базовая подпись: блокировка без вопроса не обещает ответа.
      label: stateLabel(state, rows),
      tone: meta.tone,
      count: rows.length,
      deals: rows,
      byState: stateCounts(rows),
    })
  }
  return groups
}

/** Группировка-состав репозиториев из ответа (`/now.repos`, `/stats.repos`). */
function shapeRepos(raw) {
  const data = raw && typeof raw === 'object' ? raw : {}
  const list = (Array.isArray(data.list) ? data.list : [])
    .map((item) => ({ repo: String((item && item.repo) || ''), deals: Number(item && item.deals) || 0 }))
    .filter((item) => item.repo)
  return {
    list,
    withoutRepo: Number(data.without_repo) || 0,
    // `read: false` — store дел не прочитался: «репозиториев нет» и «группировка не
    // посчитана» разные факты, и экран не имеет права показывать первое вместо второго.
    read: data.read !== false,
  }
}

/**
 * Модель экрана из ответа `/now`: строки, группы по репозиториям и честные ошибки
 * источников.
 *
 * Досок в ответе больше нет (0.0.6): `repos` отвечает, какие репозитории названы
 * делами store и сколько дел без репозитория — тот же блок считает CLI. Ошибки
 * источников приходят парой `{source, reason}`: источник назван, а не выдан за
 * «доску».
 */
function shapeNow(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const deals = (Array.isArray(data.deals) ? data.deals : []).map(shapeDeal)
  const repos = shapeRepos(data.repos)
  const errors = (Array.isArray(data.errors) ? data.errors : []).map((error) => ({
    source: String((error && error.source) || 'источник'),
    reason: String((error && error.reason) || 'причина не записана'),
  }))
  const byState = stateCounts(deals)
  // Порядок состояний — из ответа бэкенда (`by_state` идёт в порядке
  // WAITING_PRIORITY), а не из порядка ключей таблицы подписей.
  const backendStates =
    data.by_state && typeof data.by_state === 'object' ? Object.keys(data.by_state) : []
  const stateOrder = backendStates.length
    ? backendStates.map(String)
    : [...new Set(deals.map((deal) => deal.state))]
  return {
    now: Number.isFinite(Number(data.now)) ? Number(data.now) : null,
    deals,
    groups: groupDeals(deals),
    repos,
    errors,
    repoCount: repos.list.length,
    withoutRepo: repos.withoutRepo,
    count: deals.length,
    byState,
    stateGroups: groupByState(deals),
    humanCounts: humanCounts(deals),
    stateOrder,
  }
}

/**
 * Локальные фильтры страницы: репозиторий, набор состояний, поиск.
 *
 * `repo` — `ALL_REPOS` (или не задан) — все дела, `''` — ровно дела без
 * репозитория, иначе путь репозитория. Пустая строка как значение — не выдумка:
 * ровно так отличает «без репозитория» от «фильтра нет» бэкенд (`?repo=`).
 */
function filterNow(model, filter) {
  const f = filter && typeof filter === 'object' ? filter : {}
  const repo = f.repo === undefined || f.repo === null ? ALL_REPOS : String(f.repo)
  const states = f.states instanceof Set ? f.states : new Set(Array.isArray(f.states) ? f.states : [])
  const query = String(f.query || '').trim().toLowerCase()
  const deals = (model && model.deals ? model.deals : []).filter(
    (deal) =>
      (repo === ALL_REPOS || (repo === '' ? !deal.repo : deal.repo === repo)) &&
      (!states.size || states.has(deal.state)) &&
      (!query || deal.search.includes(query)),
  )
  return {
    ...model,
    deals,
    groups: groupDeals(deals),
    stateGroups: groupByState(deals),
    count: deals.length,
    byState: stateCounts(deals),
    humanCounts: humanCounts(deals),
  }
}

/* ------------------------------------------- экран «Завершённые» (0.0.7) */

// Подписи терминальных статусов: у закрытого дела нет состояния ожидания, и
// бейдж рисуется по статусу строки store (`done`/`archived`). Тон — тот же набор,
// что у остальных бейджей (`TONE_COLOR`).
const FINISHED_STATUS_META = {
  done: { label: 'завершено', tone: 'green' },
  archived: { label: 'в архиве', tone: 'gray' },
}

/** Мета терминального статуса; неизвестный — своим именем серым. */
function finishedStatusMeta(status) {
  const key = String(status || '')
  return FINISHED_STATUS_META[key] || { label: key || '—', tone: 'gray' }
}

/** Вопросы человеку одной строкой: `2 вопроса · 1 ждёт ответа`. */
function questionsText(questions) {
  const block = questions && typeof questions === 'object' ? questions : {}
  const asked = Number(block.asked) || 0
  const waiting = Number(block.waiting) || 0
  const tail = waiting ? ` · ${waiting} ${plural(waiting, 'ждёт', 'ждут', 'ждут')} ответа` : ''
  return `${asked} ${plural(asked, 'вопрос', 'вопроса', 'вопросов')}${tail}`
}

/**
 * Публикация словами: `{sha, shaShort, branch, ref, mrUrl, verifiedBy, text}`.
 *
 * Один формат на карточку дела (`shapeDealTime`) и на строку «Завершённых»:
 * sha подтверждён `git ls-remote` движка (`dashboard/publish.py`), а не заявкой
 * исполнителя. Нет факта — `null`, а не «опубликовано ничего».
 */
function fmtPublication(raw) {
  const pub = raw && typeof raw === 'object' ? raw : null
  if (!pub || !Object.keys(pub).length) return null
  const sha = String(pub.sha || '')
  return {
    sha,
    shaShort: sha.slice(0, 8),
    branch: String(pub.branch || ''),
    ref: String(pub.ref || ''),
    mrUrl: String(pub.mr_url || ''),
    verifiedBy: String(pub.verified_by || ''),
    text: [sha.slice(0, 8), String(pub.branch || pub.ref || ''), String(pub.verified_by || '')]
      .filter(Boolean)
      .join(' · '),
  }
}

/**
 * Строка экрана «Завершённые» из строки ответа `/deals`.
 *
 * Все числа — поля ответа: `attempts`, `questions`, `duration_sec`, `published`,
 * `cause`, `stages`. Экран ничего не пересчитывает: строка списка и карточка
 * того же дела (`bin/duty deal <id>`) обязаны называть одни числа, а второй
 * расчёт разошёлся бы с первым на первой же нестандартной попытке.
 */
function shapeFinishedRow(raw) {
  const row = raw && typeof raw === 'object' ? raw : {}
  const questions = row.questions && typeof row.questions === 'object' ? row.questions : {}
  const askCount = Number(questions.asked) || 0
  const waitCount = Number(questions.waiting) || 0
  const meta = finishedStatusMeta(row.status)
  const finishedAt = numOrNull(row.finished_at)
  const duration = numOrNull(row.duration_sec)
  const stages = (Array.isArray(row.stages) ? row.stages : []).map(String)
  const repo = String(row.repo || '')
  return {
    id: String(row.id || ''),
    repo,
    repoLabel: repoLabel(repo),
    // Хвост пути — для списков и таймлайна; полный адрес остаётся в подсказке.
    repoShort: repoShort(repo),
    title: fmtTitle(row.title, 90),
    titleFull: String(row.title || ''),
    assignee: String(row.assignee || '—'),
    status: String(row.status || ''),
    statusLabel: meta.label,
    tone: meta.tone,
    createdAt: numOrNull(row.created_at),
    createdText: fmtClock(row.created_at),
    finishedAt,
    finishedText: fmtClock(finishedAt),
    // `null` — факта закрытия в журнале нет: «время не записано» честнее нуля.
    duration,
    durationText: duration === null ? '—' : fmtAge(duration),
    cause: String(row.cause || ''),
    causeText: String(row.cause || '') || 'причина не записана',
    stages,
    stagesText: stages.join(' → ') || '—',
    attempts: Number(row.attempts) || 0,
    questions: {
      asked: askCount,
      answered: Number(questions.answered) || 0,
      waiting: waitCount,
      text: questionsText(questions),
    },
    published: fmtPublication(row.published),
    search: `${repo} ${row.id || ''} ${row.title || ''} ${row.assignee || ''} ${row.status || ''}`.toLowerCase(),
  }
}

/**
 * Модель экрана «Завершённые» из ответа `/deals`.
 *
 * Порядок — ответа бэкенда (свежие сверху), группы — по `repo` тем же
 * `groupDeals`, что у «Сейчас»: внутри группы строки остаются свежими, а имена
 * групп совпадают с `bin/duty deals`. `note` — причина пустоты словами бэкенда
 * («закрытых дел нет» / «store не прочитался» / «в этом репозитории нет»),
 * экран её печатает, а не выдумывает свою.
 */
function shapeFinished(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const deals = (Array.isArray(data.deals) ? data.deals : []).map(shapeFinishedRow)
  const errors = (Array.isArray(data.errors) ? data.errors : []).map((error) => ({
    source: String((error && error.source) || 'источник'),
    reason: String((error && error.reason) || 'причина не записана'),
  }))
  return {
    now: numOrNull(data.now),
    deals,
    groups: groupDeals(deals),
    count: deals.length,
    total: Number(data.total) || 0,
    limit: Number(data.limit) || 0,
    truncated: data.truncated === true,
    statuses: (Array.isArray(data.statuses) ? data.statuses : []).map(String),
    // Репозитории с числом закрытых дел: страница листает группы отдельно, и
    // «5 из 23» берётся отсюда, а не из длины загруженной страницы.
    repos: (Array.isArray(data.repos) ? data.repos : []).map((item) => {
      const repo = String((item && item.repo) || '')
      return { repo, short: repoShort(repo), label: repoLabel(repo), total: Number(item && item.total) || 0 }
    }),
    note: String(data.note || ''),
    errors,
  }
}

/**
 * Одна страница завершённых дел одного репозитория.
 *
 * Группа листается сама: у каждой свой курсор, поэтому «+5» не сдвигает соседей.
 * `next` — курсор следующей страницы этой группы (`null` — она последняя).
 */
function shapeFinishedPage(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const rows = (Array.isArray(data.deals) ? data.deals : []).map(shapeFinishedRow)
  return {
    now: numOrNull(data.now),
    rows,
    total: Number(data.total) || 0,
    next: data.next_cursor ? String(data.next_cursor) : null,
    errors: (Array.isArray(data.errors) ? data.errors : []).map((error) => ({
      source: String((error && error.source) || 'источник'),
      reason: String((error && error.reason) || 'причина не записана'),
    })),
  }
}

/** Тон исхода попытки: «падение» — по набору бэкенда, дальше успех/отложено. */
function outcomeTone(outcome, status, crashOutcomes) {
  const key = String(outcome || '')
  if (key && Array.isArray(crashOutcomes) && crashOutcomes.includes(key)) return 'red'
  if (OUTCOME_TONE[key]) return OUTCOME_TONE[key]
  if (key) return 'gray'
  return String(status || '') === 'running' ? 'green' : 'gray'
}

/**
 * Таймлайн попыток из ответа `/duty/{deal_id}/attempts`.
 *
 * Геометрии здесь нет: полосы на общей оси дела считает `shapeDealTime`
 * (минимум и максимум по `timeline` и `attempts`), и второй расчёт ширины —
 * «доля от самой долгой попытки» — дал бы на экране две несравнимые шкалы.
 * Здесь только факты одной попытки для карточки под шкалой.
 */
function shapeAttempts(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const list = Array.isArray(data.attempts) ? data.attempts : []
  const crashes = Array.isArray(data.crash_outcomes) ? data.crash_outcomes.map(String) : []
  const attempts = list.map((attempt) => {
    const duration = attempt.duration_sec === undefined ? null : attempt.duration_sec
    const events = (Array.isArray(attempt.events) ? attempt.events : []).map((event) => ({
      kind: String(event.kind || '—'),
      at: event.at ?? null,
      clock: fmtClock(event.at),
      summary: String(event.summary || ''),
    }))
    return {
      runId: attempt.run_id ?? null,
      profile: String(attempt.profile || '—'),
      stage: String(attempt.stage || ''),
      status: String(attempt.status || '—'),
      start: attempt.start ?? null,
      end: attempt.end ?? null,
      duration,
      durationText: fmtAge(duration),
      open: attempt.start !== undefined && attempt.start !== null && (attempt.end === undefined || attempt.end === null),
      lease: String(attempt.lease || ''),
      progress: attempt.progress ?? null,
      tools: attempt.tools ?? null,
      // Числа процесса подписываются словами здесь, а не в рендере: у обеих
      // поверхностей одна форма (`шагов N`, `инструментов M`), и разъехаться
      // этой подписи негде.
      progressText: attempt.progress === null || attempt.progress === undefined ? '' : `шагов ${fmtCount(attempt.progress)}`,
      toolsText: attempt.tools === null || attempt.tools === undefined ? '' : `инструментов ${fmtCount(attempt.tools)}`,
      calls: shapeCalls(attempt.calls),
      callsOmitted: numOrNull(attempt.calls_omitted) || 0,
      outcome: String(attempt.outcome || ''),
      outcomeTone: outcomeTone(attempt.outcome, attempt.status, crashes),
      error: String(attempt.error || ''),
      summary: String(attempt.summary || ''),
      events,
    }
  })
  return {
    repo: String(data.repo || ''),
    dealId: String(data.deal_id || ''),
    title: String(data.title || ''),
    count: attempts.length,
    attempts,
  }
}

/**
 * Строки неполноты ответа `/now`: упавший источник и непосчитанная группировка.
 *
 * `stale` — последний запрос не прошёл, на экране прошлый снимок: его замечания
 * уже не факт о настоящем, поэтому отдаём пусто. Иначе «источник X недоступен»
 * переживает починку кода и висит, пока страницу не перезагрузят.
 */
function noticeLines(model, stale = false) {
  if (stale || !model) return []
  const lines = (model.errors || []).map((bad) => `источник ${bad.source} недоступен: ${bad.reason}`)
  // Store дел не прочитан — группировки по репозиториям нет: пустой список групп
  // тут означал бы «репозиториев не названо», а это другой факт.
  if (model.repos && model.repos.read === false) {
    lines.push('store дел не прочитан: группировка по репозиториям не посчитана')
  }
  return lines
}

/* ------------------------------------------- лента этапов и попыток (W6) */

// Состояния этапа приходят из `transit.stage_states` (`done|running|pending|
// failed|wait_human|wait_gate|parked`). Правила не дублируются: здесь только
// подпись и тон. Цвет по заданию: `done` — спокойный зелёный, `running` —
// акцент, `wait_*` — янтарный, `failed` — красный, `pending` — серый.
const STAGE_META = {
  done: { label: 'пройден', tone: 'green' },
  running: { label: 'идёт', tone: 'blue' },
  wait_human: { label: 'ждёт человека', tone: 'yellow' },
  wait_gate: { label: 'ждёт гейт', tone: 'yellow' },
  pending: { label: 'ждёт хода', tone: 'gray' },
  failed: { label: 'отказ', tone: 'red' },
  parked: { label: 'припаркован', tone: 'yellow' },
}

// Чем кончился процесс: `blocked`/`refused` янтарные — это не падение
// исполнения, а отказ гейта или вопрос человеку. Незнакомый исход остаётся
// серым: тон не выдумывается (тот же принцип, что у `outcomeTone`).
// Исход попытки по-русски. Английские слова из журнала (`done`, `failed`)
// на экран не идут: подпись строки — для человека, а не телеметрия.
const OUTCOME_WORD = {
  done: 'готово',
  ok: 'готово',
  success: 'готово',
  passed: 'готово',
  accepted: 'готово',
  failed: 'провал',
  blocked: 'отказ',
  refused: 'отказ',
  rate_limited: 'лимит',
  reclaimed: 'снята',
}

/**
 * Причина отказа одной фразой — для подсказки провалившейся попытки.
 *
 * «Провал» без причины ничего не объясняет, поэтому рядом с исходом идут класс
 * отказа (`contract`, `infra`, …) и текст причины из журнала. Текст обрезается
 * до подписи: целиком он живёт в ленте этапа и в журнале, а подсказка читается
 * поверх таймлайна. Нет причины — нет и фразы: выдумывать её экран не станет.
 */
function barReasonText(bar) {
  const b = bar && typeof bar === 'object' ? bar : {}
  const reason = String(b.reason || '').trim()
  if (!reason) return ''
  const kind = String(b.failureKind || '').trim()
  return `${kind ? `отказ ${kind}` : 'причина'}: ${fmtTitle(reason, 140)}`
}

/**
 * Слово исхода для подписи строки; живёт рядом с тоном, чтобы не разъезжаться.
 *
 * Незнакомый исход показывается как есть: новый класс в журнале — факт, и
 * подменять его на «—» значило бы его спрятать.
 */
function outcomeWord(value) {
  const key = String(value || '')
  return OUTCOME_WORD[key] || key
}

const ATTEMPT_TONE = {
  done: 'green',
  ok: 'green',
  success: 'green',
  passed: 'green',
  accepted: 'green',
  blocked: 'yellow',
  refused: 'yellow',
  rate_limited: 'yellow',
  reclaimed: 'yellow',
  failed: 'red',
}

/** Подпись и тон состояния этапа; незнакомое состояние — как есть, серым. */
function stageMeta(state) {
  const key = String(state || '')
  return STAGE_META[key] || { label: key || '—', tone: 'gray' }
}

/** Число с разделителем разрядов: `15 234` глазом читается, `15234` — нет. */
function fmtCount(value) {
  if (value === null || value === undefined || value === '') return '—'
  const n = Number(value)
  if (!Number.isFinite(n)) return '—'
  return String(Math.trunc(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ' ')
}

/**
 * Запись объявленной команды: спека против отчёта попытки.
 *
 * `ok: false` без записи — не «упало», а «не доказано»: подпись говорит именно
 * это, иначе отсутствие evidence читалось бы как проваленный build.
 */
function shapeStageEvidence(entry) {
  const e = entry && typeof entry === 'object' ? entry : {}
  const must = e.must === null || e.must === undefined ? null : Number(e.must)
  const got = e.got === null || e.got === undefined ? null : Number(e.got)
  let label = 'не доказано: записи нет'
  let tone = 'gray'
  if (got !== null) {
    const proved = e.ok === true
    label = proved ? `exit ${got} — как объявлено` : `exit ${got}, объявлено ${must === null ? '—' : must}`
    tone = proved ? 'green' : 'red'
  } else if (e.ok === true) {
    label = 'подтверждено'
    tone = 'green'
  }
  return {
    cmd: String(e.cmd || '—'),
    must,
    got,
    mustText: must === null ? '—' : String(must),
    gotText: got === null ? '—' : String(got),
    label,
    tone,
    at: e.at ?? null,
    clock: fmtClock(e.at),
  }
}

/**
 * Одна попытка этапа — процесс: время, длительность, код выхода, токены,
 * инструменты, причина отказа.
 *
 * Длительность есть только у закрытой попытки. У идущей её нет, и подставлять
 * «сколько уже идёт» значило бы мерить прогресс, которого никто не мерил.
 * Живость — отдельный факт от журнала: `live` (лизинг действует), `dead`
 * (лизинг истёк, завершения нет), `unknown` (срок лизинга не записан).
 */
function shapeStageAttempt(attempt, events) {
  const a = attempt && typeof attempt === 'object' ? attempt : {}
  // События приходят отдельно: в записи этапа их нет (`events` собирает
  // `shapeStages` из журнала дела и раскладывает по номеру попытки).
  const ownEvents = Array.isArray(events) ? events : []
  const outcome = String(a.outcome || '')
  const live = a.live === true
  const dead = a.dead === true
  const lease = String(a.lease || '')
  const duration = a.duration_sec === undefined ? null : a.duration_sec
  let liveText = 'закрыта без исхода'
  if (live) liveText = 'идёт'
  else if (dead) liveText = 'процесс мёртв: лизинг истёк, завершения нет'
  else if (lease === 'unknown') liveText = 'живость неизвестна: лизинг не записан'
  else if (outcome) liveText = `исход: ${outcomeWord(outcome)}`
  const tone = live ? 'blue' : dead ? 'red' : ATTEMPT_TONE[outcome] || 'gray'
  const records = (Array.isArray(a.evidence) ? a.evidence : []).map((record) => {
    const r = record && typeof record === 'object' ? record : {}
    const exit = r.exit === null || r.exit === undefined ? null : Number(r.exit)
    return {
      cmd: String(r.cmd || '—'),
      exit,
      exitText: exit === null ? '—' : String(exit),
      clock: fmtClock(r.started_at),
    }
  })
  return {
    n: Number(a.n) || null,
    number: `#${Number(a.n) || '—'}`,
    runId: a.run_id ?? null,
    stage: String(a.stage || ''),
    role: String(a.role || ''),
    pid: a.pid ?? null,
    pidText: a.pid ? `pid ${a.pid}` : 'pid не записан',
    start: a.started_at ?? null,
    clock: fmtClock(a.started_at),
    finishedAt: a.finished_at ?? null,
    finishClock: fmtClock(a.finished_at),
    duration,
    durationText: live ? 'идёт' : fmtAge(duration),
    exit: a.exit ?? null,
    exitText: a.exit === null || a.exit === undefined ? '—' : String(a.exit),
    outcome,
    kind: String(a.kind || ''),
    tokens: a.tokens ?? null,
    tokensText: fmtCount(a.tokens),
    tools: a.tools ?? null,
    toolsText: fmtCount(a.tools),
    reason: String(a.reason || ''),
    events: ownEvents,
    live,
    dead,
    lease,
    liveText,
    tone,
    evidence: records,
    hasEvidence: records.length > 0,
  }
}

/**
 * Этап дела: имя, роль, состояние, номер попытки, ожидание, попытки, evidence.
 *
 * Ожидание вопроса вынесено отдельной строкой с `question_id` и ссылкой на
 * панель пула (`/imsg`): вопрос живёт там, где на него отвечают, а лента — нет.
 * Своей кнопки «ответить»/«перезапустить» экран не заводит: он ничего не решает.
 */
function shapeStage(stage, journal) {
  const s = stage && typeof stage === 'object' ? stage : {}
  const byAttempt = journal && typeof journal.get === 'function' ? journal : null
  const meta = stageMeta(s.state)
  const waits = s.waits && typeof s.waits === 'object' ? s.waits : null
  const needs = (Array.isArray(s.needs) ? s.needs : []).map(String)
  const attempts = (Array.isArray(s.attempts) ? s.attempts : []).map((item) => {
    const n = item && typeof item === 'object' ? Number(item.n) || null : null
    const key = byAttempt && n !== null ? `${String(s.id || '')}#${n}` : ''
    return shapeStageAttempt(item, key ? byAttempt.get(key) : null)
  })
  const evidence = (Array.isArray(s.evidence) ? s.evidence : []).map(shapeStageEvidence)
  const kind = waits ? String(waits.kind || '') : ''
  const questionId =
    waits && waits.question_id !== null && waits.question_id !== undefined ? String(waits.question_id) : ''
  let waitText = ''
  if (kind === 'question') waitText = `вопрос ${questionId ? `#${questionId}` : 'без номера'} — ждёт ответа`
  else if (kind === 'human') waitText = 'ждёт человека: вопроса в пуле нет'
  else if (kind === 'gate') waitText = `ждёт гейт: ${String(waits.reason || 'граф не пускает')}`
  else if (kind === 'parked') {
    // Парковка — не отдельное состояние: экран называет счётчик и правило,
    // а решение остаётся за человеком (docs/JOURNAL.md).
    const cls = String(waits.class || '')
    const spent = waits.attempts === null || waits.attempts === undefined ? '?' : String(waits.attempts)
    const limit = waits.limit === null || waits.limit === undefined ? '?' : String(waits.limit)
    const counter = cls ? `${spent}/${limit} ${cls}` : 'попытки исчерпаны'
    const park = String(waits.park_reason || '')
    waitText = `попытки исчерпаны: ${counter}${park ? ` (${park})` : ''} — решает человек${questionId ? `, вопрос #${questionId}` : ''}`
  } else if (kind === 'retry') {
    waitText = `пауза до ${fmtClock(waits.retry_at)}: повтор после отказа`
  }
  const attempt = Number(s.attempt) || 0
  return {
    id: String(s.id || '—'),
    // Вид этапа (`agent`/`shell`/`gate`) из спеки: подпись строки столбца и
    // значок графа берут его отсюда, а не угадывают по имени этапа.
    kind: String(s.kind || 'agent'),
    role: String(s.role || '—'),
    state: String(s.state || ''),
    stateLabel: meta.label,
    tone: meta.tone,
    attempt,
    attemptText: `попытка ${attempt}`,
    rounds: Number(s.rounds) || 0,
    needs,
    needsText: needs.length ? `после: ${needs.join(', ')}` : '',
    reason: String(s.reason || ''),
    waits,
    waitKind: kind,
    questionId,
    questionHref: questionId ? '/imsg' : '',
    waitText,
    attempts,
    attemptsCount: attempts.length,
    hasAttempts: attempts.length > 0,
    evidence,
    hasEvidence: evidence.length > 0,
  }
}

/**
 * Лента этапов из ответа `/deal` (`stages` + `stages_note`).
 *
 * Пустой список — не ошибка рендера: у дела может не быть спеки, а у этапов —
 * попыток. Причину пустоты называет `note` бэкенда, поэтому экран не гадает.
 * Своих правил «готово/не готово» здесь нет: состояние приходит посчитанным.
 */
/**
 * События журнала по попыткам: `Map<"этап#номер", [{kind, clock, summary}]>`.
 *
 * `summary` события — JSON движка (`{stage, attempt, phase, name}`), и только в
 * нём есть привязка к попытке: ключей `stage`/`attempt` на верхнем уровне нет.
 * Событие без разбираемой привязки пропускается — приписать его первой попытке
 * значило бы выдумать факт.
 */
function stageJournal(timeline) {
  const map = new Map()
  for (const event of Array.isArray(timeline) ? timeline : []) {
    const e = event && typeof event === 'object' ? event : {}
    let payload = null
    try {
      payload = JSON.parse(String(e.summary || ''))
    } catch {
      payload = null
    }
    if (!payload || typeof payload !== 'object') continue
    const stage = String(payload.stage || '')
    const attempt = Number(payload.attempt) || null
    if (!stage || attempt === null) continue
    const key = `${stage}#${attempt}`
    const list = map.get(key) || []
    list.push({ kind: String(e.kind || ''), clock: fmtClock(e.at), summary: String(e.summary || '') })
    map.set(key, list)
  }
  return map
}

function shapeStages(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  // Журнал дела лежит рядом с этапами, а не внутри них: `stages[].attempts[]`
  // знает вызовы и доказательства, события `attempt_progress` — только в
  // `timeline`. Раскладываем их по номеру попытки здесь, чтобы поверхность не
  // склеивала два источника сама.
  const journal = stageJournal(data.timeline)
  const stages = (Array.isArray(data.stages) ? data.stages : []).map((stage) => shapeStage(stage, journal))
  const attempts = stages.reduce((sum, stage) => sum + stage.attemptsCount, 0)
  return {
    spec: String(data.spec || ''),
    note: String(data.stages_note || ''),
    stages,
    count: stages.length,
    attempts,
    empty: stages.length === 0,
  }
}

/* ------------- компактный столбец этапов и попыток (0.0.8) ------------- */

// Слова видов событий журнала: сводка журнала говорит «5 прогрессов», а не
// «5 attempt_progress». Незнакомый вид печатается как есть — новый класс
// события на экране остаётся фактом, а не прячется за «—».
const EVENT_WORD = {
  attempt_progress: ['прогресс', 'прогресса', 'прогрессов'],
  attempt_started: ['старт', 'старта', 'стартов'],
  attempt_ended: ['конец', 'конца', 'концов'],
  question_asked: ['вопрос', 'вопроса', 'вопросов'],
  question_answered: ['ответ', 'ответа', 'ответов'],
  published: ['публикация', 'публикации', 'публикаций'],
  released: ['отпускание', 'отпускания', 'отпусканий'],
  completed: ['закрытие', 'закрытия', 'закрытий'],
  collected: ['вердикт', 'вердикта', 'вердиктов'],
  changes_requested: ['правки', 'правки', 'правок'],
}

// Предел строки команды в сводке доказательств: столбец 320 px, длинная команда
// в нём — абзац. Полная строка живёт в подсказке (`cmdTitle`).
const CMD_SHORT_LIMIT = 46

// Обёртка запуска, которая не является частью команды: `env -u PYTHONPATH`
// повторяется в каждой строке и не говорит ничего о том, что запускали.
const CMD_ENV_WRAPPER = /^env\s+(-u\s+\S+\s+)+/

/** Слова вида события: тройка для `plural`; незнакомый — как есть. */
function eventWord(kind) {
  return EVENT_WORD[String(kind || '')] || [String(kind || ''), String(kind || ''), String(kind || '')]
}

/**
 * Журнал попытки сводкой по видам: `5 прогрессов · 1 старт · 1 конец`.
 *
 * У одной попытки бывает 374 события `attempt_progress`, и в столбце 320 px они
 * превращают страницу в лог. Порядок постоянный — по убыванию числа, равные — по
 * первому появлению в журнале: иначе сводка прыгала бы между опросами одного и
 * того же дела. Длиннее трёх видов хвост не печатается вовсе (без «и ещё…»):
 * столбец не место для перечня телеметрии.
 */
function eventsSummary(events) {
  const counts = new Map()
  for (const event of Array.isArray(events) ? events : []) {
    const kind = String((event && event.kind) || '').trim()
    if (!kind) continue
    counts.set(kind, (counts.get(kind) || 0) + 1)
  }
  const rows = [...counts.entries()].map(([kind, count], index) => ({ kind, count, index }))
  rows.sort((left, right) => right.count - left.count || left.index - right.index)
  return rows
    .slice(0, 3)
    .map((row) => `${row.count} ${plural(row.count, ...eventWord(row.kind))}`)
    .join(' · ')
}

/**
 * Хвост команды для узкого столбца: без обёртки `env -u PYTHONPATH`, без
 * абсолютных путей, не длиннее 46 знаков.
 *
 * `python3 -m pytest tests/ -q` вместо `/Users/…/venv/bin/python3 -m pytest
 * tests/ -q`: абсолютный путь в строке команды — машинная подробность, и под
 * подсказкой она не нужна. Обрезка здесь, а не CSS-эллипсисом: подсказка обязана
 * нести полную команду, а `text-overflow` её не сохраняет.
 */
function cmdShort(cmd) {
  let text = String(cmd ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  text = text.replace(CMD_ENV_WRAPPER, '')
  text = text
    .split(' ')
    .map((token) =>
      token.startsWith('/')
        ? token.replace(/\/+$/, '').split('/').filter(Boolean).pop() || token
        : token,
    )
    .join(' ')
  if (!text) return '—'
  if (text.length <= CMD_SHORT_LIMIT) return text
  return `${text.slice(0, CMD_SHORT_LIMIT - 1).trimEnd()}…`
}

/**
 * Строка этапа в компактном столбце: имя, вид, попытки, состояние словами.
 *
 * Служебное (`needs`) отдаётся отдельным полем, а не строкой: в столбце 320 px
 * строка «после: …» съедает высоту, которую занимают попытки, — поверхность
 * кладёт её в `title` строки.
 *
 * Раскрыт по умолчанию этап, где дело стоит сейчас (`running`/`wait_human`/
 * `wait_gate`/`parked`, а также `failed` — это тоже «здесь падало»), и этап с
 * проваленной попыткой: решение прошлого круга «провал виден без клика» не
 * отменяется компактностью.
 */
function compactStageView(stage) {
  const s = stage && typeof stage === 'object' ? stage : {}
  const state = String(s.state || '')
  const meta = s.stateLabel ? { label: String(s.stateLabel), tone: String(s.tone || 'gray') } : stageMeta(state)
  const attempts = Array.isArray(s.attempts) ? s.attempts : []
  const count = Number(s.attemptsCount) || attempts.length
  const failed = attempts.some((attempt) => attempt && attempt.tone === 'red')
  const standing = ['running', 'wait_human', 'wait_gate', 'parked', 'failed'].includes(state)
  const open = standing || failed
  const id = String(s.id || '—')
  const kind = String(s.kind || 'agent')
  return {
    id,
    // Вид этапа: у агентских — роль (`implementer`), у остальных — сам вид
    // (`shell`, `gate`) из спеки. Та же подпись, что у дорожки шкалы.
    kindText: kind === 'agent' ? String(s.role || 'профиль') : kind,
    attemptsText: count ? `${count} ${plural(count, 'попытка', 'попытки', 'попыток')}` : 'попыток нет',
    stateLabel: meta.label,
    tone: meta.tone,
    needsText: String(s.needsText || ''),
    // Вопрос человеку остаётся видимой строкой под этапом, а не подсказкой:
    // дело может стоять на человеке, и это не деталь.
    waitText: String(s.waitText || ''),
    waitHref: String(s.questionHref || ''),
    openByDefault: open,
    toggleTitle: `${open ? 'свернуть' : 'развернуть'} попытки этапа ${id}`,
  }
}

/** Запись доказательства строкой столбца: код выхода, команда, время. */
function compactEvidenceRow(record) {
  const r = record && typeof record === 'object' ? record : {}
  // Две формы записи: отчёт попытки (`exit`) и объявленная команда спеки
  // (`got` — что реально получили). Подпись одна: код выхода.
  const exit = r.exit === undefined || r.exit === null ? numOrNull(r.got) : numOrNull(r.exit)
  const cmd = String(r.cmd || '—')
  return {
    exitText: exit === null ? '—' : String(exit),
    // Ноль — доказано, иное — нет; «записи нет» остаётся серой, это не провал.
    tone: exit === null ? 'gray' : exit === 0 ? 'green' : 'red',
    cmdShort: cmdShort(cmd),
    cmdTitle: cmd,
    clock: String(r.clock || fmtClock(r.at ?? r.started_at)),
  }
}

/**
 * Строка попытки в компактном столбце: номер, длительность, исход словами.
 *
 * `stage` — этап из ленты (`shapeStage`), `bar` — попытка с фактами процесса:
 * полоса шкалы (`shapeDealTime`) или попытка ручки `/attempts` (`shapeAttempts`).
 * Доказательства живут только в ленте этапа, поэтому `stage.attempts` ищется по
 * `runId`, а при его отсутствии — по номеру: у двух источников одна попытка
 * называется по-разному.
 *
 * Раскрыта по умолчанию проваленная и живая попытка: провал обязан быть виден
 * без клика.
 */
function compactAttemptView(stage, bar) {
  const s = stage && typeof stage === 'object' ? stage : {}
  const b = bar && typeof bar === 'object' ? bar : {}
  const rows = Array.isArray(s.attempts) ? s.attempts : []
  const runId = b.runId === undefined || b.runId === null ? null : String(b.runId)
  const number = String(b.number || '')
  const n = numOrNull(b.n)
  const own =
    rows.find((row) => {
      if (!row || typeof row !== 'object') return false
      if (runId !== null && row.runId !== undefined && row.runId !== null && String(row.runId) === runId) return true
      if (n !== null && Number(row.n) === n) return true
      return Boolean(number) && String(row.number || '') === number
    }) || null

  // Номер попытки: `#1` из полосы, из записи этапа или из хвоста `runId`
  // (`d_abcdef12:impl#2`). Нет ни одного факта — «—», а не выдуманный ноль.
  let label = number
  if (!label && n !== null) label = `#${n}`
  if (!label) {
    const tail = /#(\d+)$/.exec(runId || '')
    label = tail ? `#${tail[1]}` : '—'
  }

  const open =
    b.open === true || b.live === true || (own !== null && (own.live === true || own.open === true))
  const dead = b.dead === true || (own !== null && own.dead === true)
  const tone = dead
    ? 'red'
    : open
      ? 'blue'
      : String(b.tone || b.outcomeTone || (own && own.tone) || 'gray')

  const outcome = String(b.outcome || (own && own.outcome) || '')
  let outcomeText = 'закрыта без исхода'
  if (dead) outcomeText = 'процесс мёртв'
  else if (open) outcomeText = 'идёт'
  else if (outcome) outcomeText = outcomeWord(outcome)

  // Длительность есть только у закрытой попытки: у идущей её никто не мерил, а
  // у мёртвой нет и конца.
  const durationText = dead
    ? '—'
    : open
      ? 'идёт'
      : String(b.durationText || (own && own.durationText) || '—')

  // Доказательства: свои у попытки, а если их нет — объявленные команды этапа,
  // и только у последней попытки (иначе один и тот же список повторился бы под
  // каждой строкой этапа).
  const ownRecords = own && Array.isArray(own.evidence) ? own.evidence : []
  const stageRecords = Array.isArray(s.evidence) ? s.evidence : []
  const mine = ownRecords.length
    ? ownRecords
    : !rows.length || own === rows[rows.length - 1]
      ? stageRecords
      : []
  // Два источника журнала: полоса шкалы (лента дела) и запись этапа (журнал).
  // Порядок предпочтения — по непустоте, а не по «первому присвоенному».
  const barEvents = Array.isArray(b.events) ? b.events : []
  const ownEvents = own && Array.isArray(own.events) ? own.events : []

  const evidenceRows = mine.map(compactEvidenceRow)

  return {
    label,
    durationText,
    outcomeText,
    tone,
    openByDefault: tone === 'red' || open,
    evidenceRows,
    summary: String(b.summary || ''),
    // `error` — как есть; у ленты этапа вместо него `reason` (один факт, два
    // имени в двух ручках), и подписи «ошибка» здесь не выдумывается.
    error: String(b.error || (own && own.reason) || b.reason || ''),
    // Журнал: полоса несёт события из ленты дела, запись этапа — из журнала.
    // Пустой список в полосе не значит «событий нет»: ручка попыток отдаёт их
    // отдельно, и наложение пустого массива поверх непустого молча съедало
    // сводку на живых данных (`[]` в JS — правда).
    eventsText: eventsSummary(barEvents.length ? barEvents : ownEvents),
  }
}

/* ---------------------------------------- шкала времени дела (E2, 0.0.5) */

// Минимальная ширина полосы на общей оси: мгновенная попытка не должна исчезать
// из шкалы (те же 2%, что были у относительной полосы), но подпись при этом
// честная — `<1с`, иначе две ширины в процентах читались бы как две секунды.
const BAR_MIN_PCT = 2

// Пустое ожидание на шкале сжимается: дело, где между этапами был простой в час
// сорок минут, иначе показывает работу двумя тонкими полосками по краям. Порог —
// и по длительности, и по доле оси: короткая пауза в долгом деле и так не мешает,
// а длинная пауза в очень долгом деле не искажает вид.
const IDLE_MIN_SEC = 600
// Доля оси, ниже которой простой не трогаем. Восьми процентов не хватало: в деле
// d_d9087160 пауза 13 минут на трёхчасовой оси — это 7.4 %, и сжатия не было
// вовсе, хотя пауза глазом читается как провал в работе.
const IDLE_MIN_SHARE = 0.05
// Сжатый промежуток занимает столько процентов оси, но все сжатия вместе — не
// больше суммы: иначе ось врала бы про доли работы.
const IDLE_GAP_PCT = 3
const IDLE_TOTAL_PCT = 20
// Подпись простоя ближе этого к краю шкалы переезжает внутрь: у правой границы
// текст по центру уезжал за поле графика.
const IDLE_LABEL_EDGE_PCT = 12

// Подпись и тон маркера: журнал назвал событие — экран назвал его тем же словом.
// `tick` — тонкая засечка старта/закрытия попытки (их на оси много); остальные
// виды — то, что случилось с делом, и они рисуются заметно.
// `anchor` — куда маркер «прицеплен» к своему моменту. Вывод (вердикт, ответ,
// закрытие) принадлежит тому, что уже случилось, поэтому стоит ПЕРЕД моментом:
// иначе вердикт читается как начало следующего этапа — его момент совпадает со
// стартом следующей попытки. Вопрос, наоборот, открывает ожидание и стоит после.
const MARKER_META = {
  question: { label: 'вопрос человеку', tone: 'yellow', tick: false, anchor: 'start' },
  answer: { label: 'ответ человека', tone: 'green', tick: false, anchor: 'end' },
  verdict: { label: 'вердикт', tone: 'blue', tick: false, anchor: 'end' },
  budget: { label: 'отказ по бюджету', tone: 'red', tick: false, anchor: 'end' },
  published: { label: 'публикация', tone: 'green', tick: false, anchor: 'end' },
  closed: { label: 'закрыто', tone: 'gray', tick: false, anchor: 'end' },
  started: { label: 'старт попытки', tone: 'gray', tick: true, anchor: 'middle' },
  ended: { label: 'конец попытки', tone: 'gray', tick: true, anchor: 'middle' },
}

// Причина закрытия по-русски: `cause` из журнала — машинное слово, экран его
// не показывает, но и не выдумывает: незнакомая причина остаётся как есть.
const CLOSE_CAUSE_WORD = {
  all_stages_done: 'все этапы пройдены',
  closed_by_human: 'закрыто человеком',
  stopped_by_human: 'остановлено человеком',
}

/**
 * Тело подписи маркера — то, что человек читает после названия события.
 *
 * Вопрос и ответ берутся текстом из пула (`question_text`/`answer_text` ответа
 * бэкенда), закрытие — словами причины и списком этапов. Выжимка payload'а
 * (`summary`) остаётся запасным путём: у приговора и отказа по бюджету она уже
 * написана словами движком, и второго форматтера здесь не заводится.
 */
function markerBody(type, event) {
  const e = event && typeof event === 'object' ? event : {}
  const text = String(e.summary || '')
  if (type === 'question') {
    const question = String(e.question_text || '')
    if (question) return `«${question}»`
    return 'текст вопроса не сохранён'
  }
  if (type === 'answer') {
    const answer = String(e.answer_text || '')
    if (answer) return `«${answer}»`
    return 'ответ без текста'
  }
  if (type === 'closed') {
    const cause = CLOSE_CAUSE_WORD[String(e.cause || '')] || String(e.cause || '')
    const stages = Array.isArray(e.stages) ? e.stages.filter(Boolean).map(String) : []
    return [cause, stages.length ? `этапы: ${stages.join(' → ')}` : ''].filter(Boolean).join(' · ')
  }
  // `summary` приговора и отказа по бюджету — уже фраза движка, а не JSON;
  // JSON-выжимку в подсказку не пускаем: это телеметрия.
  return text.startsWith('{') ? '' : text
}

/**
 * Вид маркера по событию журнала; пусто — маркера нет.
 *
 * Гейт и отказ по бюджету узнаются по причине из `attempt_ended`
 * (`_payload_summary` берёт `reason`): своего `kind` у отказа нет, он назван
 * словами движка — «гейт: …» и «бюджет дела исчерпан: N/M токенов». Незнакомое
 * событие маркера не получает: «нет события — нет маркера» — правило экрана.
 */
function markerType(kind, summary) {
  const text = String(summary || '')
  if (kind === 'question_asked') return 'question'
  if (kind === 'question_answered') return 'answer'
  if (kind === 'attempt_started') return 'started'
  if (kind === 'attempt_ended') {
    if (/^гейт/.test(text)) return 'verdict'
    if (/бюджет/i.test(text)) return 'budget'
    return 'ended'
  }
  // Приговор ревью — два факта журнала (`journal.VERDICT_FACTS`), а не текст.
  if (kind === 'collected' || kind === 'changes_requested') return 'verdict'
  if (kind === 'published') return 'published'
  if (kind === 'completed') return 'closed'
  return ''
}

/** Время как ЧЧ:ММ — для подписи оси: секунды на шкале дела не читаются. */
function fmtClockShort(at) {
  const full = fmtClock(at)
  return full === '—' ? '—' : full.slice(0, 5)
}

/** Длительность с нижней границей: ноль — `<1с`, а не `0 с`. */
function fmtDuration(sec) {
  const value = Number(sec)
  if (!Number.isFinite(value) || value <= 0) return '<1с'
  return fmtAge(value)
}

/**
 * Слово о лизинге попытки: что с ней происходит.
 *
 * Решает не только поле `lease`, но и `status`: `_own_attempt` уже сложил
 * живость в статус (`running` — лизинг живой, `orphan` — срок вышел, `unknown` —
 * срока нет ни в store, ни в событии). Ключ по одному `lease` давал «неизвестно»
 * на идущей попытке, потому что ручка попыток это поле не отдавала (09.11).
 */
function leaseWord(lease, status) {
  const key = String(lease || '')
  const mark = String(status || '')
  if (key === 'live' || mark === 'running') return 'идёт'
  if (key === 'expired' || key === 'dead' || mark === 'orphan') return 'лизинг истёк'
  // Срок нигде не записан: «идёт» было бы догадкой, а «неизвестно» не отвечало
  // ни на один вопрос.
  return 'без лизинга'
}

/**
 * Ось таймлайна словами: `16:31 → 16:41`, а через полночь — с датой слева.
 *
 * Время суток без даты на переходе через полночь читалось бы как «конец раньше
 * начала».
 */
function fmtSpan(from, to) {
  const a = numOrNull(from)
  const b = numOrNull(to)
  if (a === null || b === null) return ''
  const day = (value) => new Date(value * 1000).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' })
  const left = day(a) === day(b) ? fmtClock(a) : `${day(a)} ${fmtClock(a)}`
  return `${left} → ${fmtClock(b)}`
}

/** Сколько дел в секундах — только конечные числа, иначе `null`. */
function numOrNull(value) {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** Попытка из ответа `/attempts` в полосу шкалы: время, дорожка, подписи. */
function shapeCalls(raw) {
  const list = Array.isArray(raw) ? raw : []
  const calls = []
  for (const item of list) {
    const call = item && typeof item === 'object' ? item : {}
    const at = numOrNull(call.at)
    if (at === null) continue
    const ms = numOrNull(call.ms)
    calls.push({
      seq: numOrNull(call.seq),
      name: String(call.name || '—'),
      at,
      ms,
      // Длительность словами: `null` — результат не пришёл, и «0 мс» здесь было
      // бы враньём (вызов не мгновенный, он просто не доложил).
      msText: ms === null ? 'длительность неизвестна' : fmtDuration(Math.max(0, Math.round(ms / 1000))),
      clock: fmtClock(at),
    })
  }
  return calls
}

/**
 * Раскладка вызовов на дорожке попытки: где чип, какой ширины, на каком уровне.
 *
 * Ширина чипа — логарифм длительности (`3 + 11·log10(1+ms/100)/log10(1+12000/100)`):
 * линейная шкала превратила бы 136 мс в точку, а 11.5 с — в половину дорожки, и
 * таймлайн перестал бы читаться. Уровни нужны, потому что чипы идут вплотную:
 * сосед занят — берём следующий уровень, дорожка растёт вниз.
 *
 * Арифметика общая для веб-страницы и десктопа: разъехавшиеся формулы дали бы две
 * несравнимые картинки одного и того же дела.
 */
function layoutCalls(calls, options) {
  const opts = options && typeof options === 'object' ? options : {}
  const from = numOrNull(opts.from)
  const to = numOrNull(opts.to)
  // Ширина — виртуальная: раскладка считается в ней, а на экран уходит долями
  // (`leftPct`/`widthPct`). Так обе поверхности рисуют одно и то же без замера
  // DOM: чипы масштабируются вместе с дорожкой, а не разъезжаются по ней.
  const width = Number(opts.width) || 1000
  const levelH = Number(opts.levelH) || 12
  const top = Number(opts.top) || 0
  const maxLevels = Math.max(1, Number(opts.maxLevels) || 5)
  const gap = Number(opts.gap) || 2
  const span = from === null || to === null ? 0 : to - from
  // Позиция вызова — та же карта оси, что у полос и маркеров (`pctOf` из
  // `shapeDealTime`): сжатие простоя меняет масштаб, и вторая линейная формула
  // развела бы чип и маркер одного и того же мгновения. Ширины чипов и уровни
  // остаются в виртуальных пикселях — их задаёт логарифм длительности.
  const toPct = typeof opts.toPct === 'function' ? opts.toPct : null
  const pxOf = (at) => {
    const pct = toPct ? Math.max(0, Math.min(100, Number(toPct(at)) || 0)) : ((at - from) / span) * 100
    return (pct / 100) * width
  }
  const list = Array.isArray(calls) ? calls : []
  if (!list.length || width <= 0 || span <= 0) {
    return { chips: [], levels: 0, height: top + levelH, clipped: 0 }
  }
  const chipWidth = (ms) => {
    const value = Math.max(0, Number(ms) || 0)
    return Math.round(3 + (11 * Math.log10(1 + value / 100)) / Math.log10(1 + 12000 / 100))
  }
  // `ms: null` — вызов без результата. Рисовать его минимальной шириной значило
  // бы утверждать «0 мс»: длительности нет, а не она нулевая. Отдельная ширина и
  // флаг `unfinished` дают поверхности возможность показать это иначе.
  const UNFINISHED_W = 5
  const lastEnd = []
  const chips = []
  let clipped = 0
  for (const call of list) {
    const px = Math.max(0, Math.min(width, pxOf(call.at)))
    const unfinished = call.ms === null || call.ms === undefined
    const size = unfinished ? UNFINISHED_W : chipWidth(call.ms)
    let level = lastEnd.findIndex((end) => px - end >= gap)
    if (level < 0) {
      if (lastEnd.length >= maxLevels) {
        // Уровни кончились — вызов всё равно показываем: прятать факт хуже, чем
        // наложить его на соседа. Число наложенных считается в `clipped`.
        level = lastEnd.indexOf(Math.min(...lastEnd))
        clipped += 1
      } else {
        level = lastEnd.length
      }
    }
    lastEnd[level] = px + size
    chips.push({
      call,
      left: Math.round(px * 10) / 10,
      width: size,
      leftPct: Math.round((px / width) * 10000) / 100,
      widthPct: Math.round((size / width) * 10000) / 100,
      level,
      top: top + level * levelH,
      unfinished,
    })
  }
  const levels = lastEnd.length
  return { chips, levels, height: top + levels * levelH, clipped }
}

/* ------------------------------------------- вид таймлайна: одна арифметика
 * Обе поверхности рисуют по этим функциям, а сами только подставляют свой
 * элемент и свои CSS-переменные. Иначе «один расчёт на два экрана» — обещание в
 * комментарии, которое никто не проверяет.
 */

// Геометрия строки: свёрнутая полоса, шаг уровня, отступ сверху, потолок уровней
// и запас под раскрытую карту. Числа живут здесь, а не в двух файлах.
/**
 * Геометрия узла графа спеки: одна на обе поверхности.
 *
 * В узле две строки текста (id 9.5 px и слово состояния 8.5 px), поэтому высота
 * выросла с 18; `gapX` оставляет место хвосту стрелки между колонками — при
 * прежних 14 px он наезжал на соседний узел.
 */
const SPEC_NODE = { w: 96, h: 30, gapX: 34, gapY: 12 }

// Отступ по краям картинки: обводка узла и острие стрелки рисуются поверх
// координат, и без запаса `viewBox` их обрезал бы.
const SPEC_PAD = 2

// Геометрия строки попытки: полоса фона, уровни чипов и строка маркеров.
// Резерв оси под подписи краёв («нач. 13:37:35», «кон. 14:08:28»): метка
// центрируется на своей вертикали, поэтому линия метки проходит через текст
// подписи края. Доля считается от ширины графика в 900 px: подпись края там
// занимает 8 %, половина подписи метки — ещё 2.7 %.
const TICK_EDGE_RESERVE_PCT = 12

const TL = { collapsed: 20, levelH: 12, top: 6, maxLevels: 5, pad: 8 }

// Тон вызова — семантический ключ («чтение», «правка», «запуск», «внешнее»), а не
// CSS-цвет: палитры у поверхностей разные, а вид работы — общий факт.
const TOOL_TONE = [
  // Поиск отделён от чтения: в деле их сотни, и одним цветом карта вызовов
  // читалась как «всё одно и то же».
  [/^(search_files|grep|glob|find|ripgrep)/i, 'search'],
  [/^(read_file|view|cat|ls|list|head|tail|stat)/i, 'read'],
  [/^(write_file|patch|apply_patch|edit|create|mkdir|mv|cp|rm)/i, 'write'],
  [/^(terminal|shell|bash|zsh|sh|run|exec|python|node|go|npm|git|make|cargo)/i, 'run'],
  [/^(web_|browser|http|fetch|curl|wget)/i, 'net'],
]

/**
 * Легенда семейств инструментов: одна на обе поверхности.
 *
 * Цвет на экране имеет смысл только вместе с подписью — иначе «жёлтый» надо
 * угадывать. Легенда живёт слева от дорожек, в той же колонке, что и подписи
 * попыток.
 */
const TOOL_LEGEND = [
  { tone: 'read', label: 'файлы' },
  { tone: 'search', label: 'поиск' },
  { tone: 'write', label: 'правка' },
  { tone: 'run', label: 'команды' },
  { tone: 'net', label: 'сеть' },
]

/** Тон инструмента по имени; неизвестный — `other` («не знаю, что это» — факт). */
function toolTone(name) {
  const hit = TOOL_TONE.find(([pattern]) => pattern.test(String(name || '')))
  return hit ? hit[1] : 'other'
}

// Значки вида этапа: пути в `viewBox 0 0 16 16`. Одна геометрия на обе
// поверхности — разъехавшиеся значки читались бы как разные виды этапов.
const KIND_ICON = {
  agent: 'M8 2.4a2.6 2.6 0 1 0 0 5.2 2.6 2.6 0 0 0 0-5.2ZM3 13.2c0-2.5 2.2-4.1 5-4.1s5 1.6 5 4.1',
  shell: 'M4 5.2 7 8l-3 2.8M8.6 11.4h3.6',
  gate: 'M8 2.2 13.4 8 8 13.8 2.6 8Z',
}

/**
 * Вид строки попытки: подпись, штрихи, чипы и суммарное число вызовов.
 *
 * `badge` — сколько вызовов инструментов было у попытки: показанные плюс
 * отрезанные потолком (`calls_omitted`), то есть факт из журнала, а не «сколько
 * влезло на дорожку». Сколько чипов легло друг на друга из-за нехватки уровней —
 * свойство раскладки, а не работы: на экран это число не выносится.
 */
function attemptView(bar, scale) {
  const b = bar && typeof bar === 'object' ? bar : {}
  const calls = Array.isArray(b.calls) ? b.calls : []
  const axis = (scale && scale.axis) || {}
  const layout = calls.length
    ? layoutCalls(calls, {
        from: axis.start,
        to: axis.end,
        // Чип встаёт на ту же кусочную карту оси, что полосы и маркеры: на
        // сжатом простое линейная пропорция сдвинула бы вызов относительно
        // маркера того же мгновения на четверть ширины.
        toPct: scale && typeof scale.pctOf === 'function' ? scale.pctOf : null,
        levelH: TL.levelH,
        top: TL.top,
        maxLevels: TL.maxLevels,
      })
    : { chips: [], height: TL.top + TL.levelH, clipped: 0 }
  const omitted = numOrNull(b.callsOmitted) || 0
  const total = calls.length + omitted
  const notes = []
  if (omitted) notes.push(`ещё ${fmtCount(omitted)} ${plural(omitted, 'вызов', 'вызова', 'вызовов')} не показано (потолок)`)
  const chips = layout.chips.map((chip) => {
    const call = chip.call || {}
    // Подсказка и инспектор читают одни и те же поля: имя инструмента, время
    // начала и длительность. Поверхность ничего не досчитывает.
    const tip = `${call.name || '—'} · ${call.clock || '—'} · ${call.msText || '—'}`
    return {
      // Идентификатор вызова — ключ выбора в инспекторе (`stage:attempt:seq`).
      id: `${b.stage || '—'}:${b.number || '—'}:${call.seq === null || call.seq === undefined ? '—' : call.seq}`,
      name: call.name || '—',
      clock: call.clock || '—',
      msText: call.msText || '—',
      seq: call.seq === null || call.seq === undefined ? null : call.seq,
      stage: b.stage || '—',
      attempt: b.number || '—',
      leftPct: chip.leftPct,
      widthPct: chip.widthPct,
      top: chip.top,
      level: chip.level,
      tone: toolTone(call.name),
      unfinished: chip.unfinished === true,
      tip,
      title: tip,
    }
  })
  const status = b.outcomeText || (b.open ? b.leaseText : b.outcome || b.status || b.durationText)

  // Фон попытки: длительность видна полосой, а не только по чипам вызовов.
  // Кромка не бывает уже 0.6% — иначе короткая попытка исчезла бы с экрана.
  const bandLeft = b.start === null ? null : b.leftPct
  const bandWidth =
    b.start === null || b.endPct === null
      ? null
      : Math.max(0.6, Math.round((b.endPct - b.leftPct) * 10) / 10)
  const bandCause = barReasonText(b)
  const bandTip =
    b.start === null
      ? ''
      : `${b.clockStart || '—'}–${b.clockEnd || (b.open ? 'идёт' : '—')} · ${b.durationText || '—'}` +
        `${calls.length ? ` · ${fmtCount(calls.length)} ${plural(calls.length, 'вызов', 'вызова', 'вызовов')}` : ''}` +
        `${bandCause ? ` · ${bandCause}` : ''}`

  // Маркеры внутри попытки: вердикты, вопросы человеку, публикация, закрытие.
  // Засечки старта/конца больше не рисуются — их роль играет фон попытки.
  //
  // Интервал попытки полуоткрытый: событие ровно в секунду стыка принадлежит
  // следующей попытке, а не обеим. Иначе вердикт на стыке рисовался дважды —
  // в той попытке, что закончилась, и в той, что началась.
  // Попытка «владеет» отрезком от своего начала до начала следующей: так
  // событие в промежутке между попытками (человек ответил, следующая ещё не
  // началась) остаётся видимым у той попытки, где вопрос задали, и при этом
  // ни одно событие не рисуется дважды.
  const siblings = scale && Array.isArray(scale.bars) ? scale.bars : []
  const nextStart = siblings.reduce((min, other) => {
    if (other === b || other.start === null || b.start === null || other.start <= b.start) return min
    return min === null || other.start < min ? other.start : min
  }, null)
  const inside = (scale && Array.isArray(scale.markers) ? scale.markers : []).filter((marker) => {
    if (marker.type === 'started' || marker.type === 'ended') return false
    if (b.start === null) return false
    if (marker.at < b.start) return false
    return nextStart === null || marker.at < nextStart
  })
  const marks = inside.map((marker) => ({
    type: marker.type,
    tone: marker.tone,
    pct: marker.pct,
    // Якорь едет вместе с маркером: палка вывода стоит ПЕРЕД своим моментом,
    // иначе вердикт читается как начало следующего этапа.
    anchor: marker.anchor,
    title: marker.title,
    ariaLabel: marker.ariaLabel,
  }))
  // Пауза рисуется в строке попытки, где вопрос задан: ответ мог прийти уже в
  // следующей попытке, но «стояло на человеке» — здесь.
  const waits = (scale && Array.isArray(scale.waitSpans) ? scale.waitSpans : [])
    .filter((span) => b.start !== null && b.end !== null && span.fromAt >= b.start && span.fromAt <= b.end)
    .map((span) => ({
      fromPct: span.fromPct,
      widthPct: span.widthPct,
      title: span.title,
    }))
  // Свёрнутая строка показывает первый уровень вызовов и маркеры; остальные
  // уровни раскрываются по наведению. Высоту считает шейпер — иначе две
  // поверхности разойдутся в том, «сколько строки видно без наведения».
  // Маркеры журнала рисуются вертикальными палками на всю высоту строки, поэтому
  // отдельной полосы под чипами больше нет: высота строки не зависит от того,
  // случился ли в ней вердикт, — иначе соседние попытки разъезжались по высоте.
  const full = Math.max(TL.collapsed, layout.height + TL.pad)
  const collapsed = Math.min(full, TL.collapsed)
  return {
    full,
    collapsed,
    // Тон фона — тон исхода попытки: провал виден полосой на дорожке, а не
    // только словом в подписи слева.
    band:
      bandLeft === null || bandWidth === null
        ? null
        : { leftPct: bandLeft, widthPct: bandWidth, tone: b.tone || 'grey' },
    failed: b.tone === 'red',
    bandTip,
    label: {
      number: b.number || '—',
      status: status || '—',
      tone: b.tone || 'grey',
    },
    ticks: chips.map((chip) => ({
      leftPct: chip.leftPct,
      widthPct: chip.widthPct,
      tone: chip.tone,
      unfinished: chip.unfinished,
    })),
    chips,
    marks,
    waits,
    // Надбавка на экране: суммарное число вызовов попытки. `badgeCut` — у части
    // вызовов чипа нет (потолок данных), и это единственное, что стоит выделять.
    badge: total ? `${fmtCount(total)} ${plural(total, 'вызов', 'вызова', 'вызовов')}` : '',
    badgeCut: Boolean(omitted),
    note: notes.join(' · '),
    title: [b.title || '', notes.join(' · ')].filter(Boolean).join(' · '),
  }
}

/**
 * Разбивка попыток по этапам одной фразой: `check 1 · review 1 · verify 3`.
 *
 * Пустая строка, когда разбивать нечего (попыток нет или все в одном этапе):
 * подсказка, повторяющая число, ничего не добавляет.
 */
function attemptsBreakdown(lanes, bars) {
  const list = Array.isArray(lanes) ? lanes.filter((lane) => Number(lane.count) > 0) : []
  if (list.length < 2) return ''
  return list.map((lane) => `${lane.label} ${fmtCount(lane.count)}`).join(' · ')
}

/**
 * Блоки документации дела: `[{kind, label, name, path, text, truncated}]`.
 *
 * `body` из store и спека — разные документы, и называть одно другим нельзя.
 * У дел из `bin/duty create` свободного `body` нет вовсе: описание живёт
 * комментариями в шапке `duty.yaml`, поэтому спека показывается первой, а `body`
 * — отдельным блоком, когда он есть. Пустой список — нечего рисовать: панель,
 * которая говорит «пусто», отбирает колонку и ничего не сообщает. Порядок
 * стабилен (тело, затем спека), чтобы поверхность не решала, что важнее.
 */
function docBlocks(data) {
  const src = data || {}
  const out = []
  const body = String(src.body || '').trim()
  if (body) {
    out.push({
      kind: 'body',
      label: 'описание',
      name: '',
      path: '',
      text: body,
      truncated: Boolean(src.body_truncated),
    })
  }
  const specText = String(src.spec_text || '').trim()
  if (specText) {
    out.push({
      kind: 'spec',
      label: 'спецификация',
      name: fileLabel(src.spec),
      path: String(src.spec || ''),
      text: specText,
      truncated: Boolean(src.spec_text_truncated),
    })
  }
  return out
}

/** Вид дорожки этапа: имя, вид, сводка попыток и строки. */
function laneView(lane, bars, scale) {
  const list = Array.isArray(bars) ? bars : []
  const first = list[0]
  const shown = list.reduce((sum, bar) => sum + (Array.isArray(bar.calls) ? bar.calls.length : 0), 0)
  const omitted = list.reduce((sum, bar) => sum + (numOrNull(bar.callsOmitted) || 0), 0)
  const total = shown + omitted
  const meta = [
    `${list.length} ${plural(list.length, 'попытка', 'попытки', 'попыток')}`,
    total
      ? `${fmtCount(total)} ${plural(total, 'вызов', 'вызова', 'вызовов')}${
          omitted ? ` (показано ${fmtCount(shown)})` : ''
        }`
      : '',
  ]
    .filter(Boolean)
    .join(' · ')
  return {
    name: String((lane && lane.label) || '—'),
    kind: first ? String(first.kind || 'agent') : '',
    kindText: first ? (first.kind === 'agent' ? String(first.role || 'профиль') : String(first.kind)) : '',
    icon: first ? KIND_ICON[first.kind] || KIND_ICON.shell : '',
    meta,
    empty: list.length === 0,
    rows: list.map((bar) => ({ key: `${bar.laneKey || ''}-${bar.number || ''}`, view: attemptView(bar, scale) })),
  }
}

function attemptBar(attempt, crashes) {
  const a = attempt && typeof attempt === 'object' ? attempt : {}
  const start = numOrNull(a.start)
  const end = numOrNull(a.end)
  const duration = numOrNull(a.duration_sec)
  const lease = String(a.lease || '')
  const open = start !== null && end === null
  const dead = open && (lease === 'expired' || a.dead === true || a.status === 'orphan')
  const stage = String(a.stage || a.profile || '—')
  return {
    key: String(a.run_id === null || a.run_id === undefined ? '' : a.run_id),
    number: a.attempt ? `#${a.attempt}` : '',
    stage,
    laneKey: stage,
    laneLabel: stage,
    lane: 0,
    // Вид этапа и роль — из попытки: значок на дорожке («профиль», `shell`)
    // говорит то же, что и лента этапов, а не угадывается по имени этапа.
    kind: String(a.stage_kind || 'agent'),
    // Роль приходит как `role` (`/deal`) или `profile` (`/duty/{id}/attempts`) —
    // две ручки зовут одно и то же по-разному, и значок должен знать оба имени.
    role: String(a.role || a.profile || ''),
    start,
    end,
    endAt: null,
    duration,
    durationSec: null,
    durationText: '',
    open,
    lease,
    leaseText: open ? leaseWord(lease, a.status) : '',
    progress: numOrNull(a.progress),
    tools: numOrNull(a.tools),
    // Вызовы инструментов: `[{seq, name, at, ms}]` из телеметрии. Пустой список —
    // у попытки не было телеметрии, а не «вызовов не делала»: это разные факты,
    // и разницу называет `callsText`.
    calls: shapeCalls(a.calls),
    callsOmitted: numOrNull(a.calls_omitted) || 0,
    // Израсходовано токенов, если телеметрия их записала: `null` — не «ноль».
    tokens: numOrNull(a.tokens),
    callsText: '',
    outcome: String(a.outcome || ''),
    status: String(a.status || ''),
    // Причина отказа и его класс (`contract`, `infra`, …): экран называет их
    // в подсказке провалившейся попытки — «провал» без причины ничего не
    // объясняет, а лезть за ней в журнал руками человек не должен.
    reason: String(a.reason || '').trim(),
    failureKind: String(a.kind || '').trim(),
    tone: open ? (dead ? 'red' : 'blue') : outcomeTone(a.outcome, a.status, crashes),
    leftPct: 0,
    endPct: 0,
    widthPct: 0,
    clockStart: fmtClock(start),
    clockEnd: '',
    label: '',
    title: '',
    ariaLabel: '',
    progressText: '',
    toolsText: '',
    // Откуда полоса: `attempts` — ответ ручки попыток, `journal` — пара
    // `attempt_started`/`attempt_ended`, когда ручка попыток пуста (старый бэкенд
    // или дело без рана kanban). Экран показывает разницу, а не прячет.
    source: 'attempts',
  }
}

/**
 * Стадия из выжимки `attempt_started`: там лежит JSON (`{"run_id": "review#1",
 * "stage": "review", …}`), обрезанный по `SUMMARY_LIMIT`. Разбор JSON ломался бы
 * на обрезке, поэтому берём два поля по образцу — их видно в начале строки.
 */
function stageOfSummary(summary) {
  const text = String(summary || '')
  const stage = /"stage":\s*"([^"]+)"/.exec(text)
  if (stage) return stage[1]
  const run = /"run_id":\s*"([^"#]+)#(\d+)"/.exec(text)
  return run ? run[1] : ''
}

/** Номер попытки из той же выжимки: `attempt` первым делом, иначе из `run_id`. */
function attemptOfSummary(summary) {
  const text = String(summary || '')
  const attempt = /"attempt":\s*(\d+)/.exec(text)
  if (attempt) return Number(attempt[1])
  const run = /"run_id":\s*"([^"#]+)#(\d+)"/.exec(text)
  return run ? Number(run[2]) : 0
}

/**
 * Строковые поля выжимки события по образцу `"key": "value"`.
 *
 * `JSON.parse` здесь не годится: бэкенд обрезает `summary` по `SUMMARY_LIMIT`
 * и строка часто недописана. Берём только нужные ключи, которые стоят в начале
 * выжимки; чего нет — того нет, и маркер это честно покажет.
 */
function summaryFields(summary, keys) {
  const text = String(summary || '')
  const out = {}
  for (const key of keys) {
    const found = new RegExp(`"${key}":\\s*"([^"]*)"`).exec(text)
    if (found) out[key] = found[1]
  }
  return out
}

/**
 * Полосы из журнала: пары `attempt_started` ↔ `attempt_ended`, когда ответ
 * попыток пуст.
 *
 * Второй копии расчёта здесь нет — это тот же шейпер, просто другой источник
 * факта: `attempt_ended` гейта и отказа по бюджету пишется без начала
 * (`docs/JOURNAL.md`), и такие события полосой не становятся — они уходят на
 * ленту маркеров.
 *
 * Исход берётся из выжимки `attempt_ended` по образцу `"outcome": "…"`: она
 * обрезана по `SUMMARY_LIMIT`, но ключ стоит в начале строки, и цвет полосы не
 * приходится выдумывать по её длительности.
 */
function journalBars(events, crashes) {
  const bars = []
  const open = []
  for (const event of events) {
    if (!(event.at > 0)) continue
    if (event.kind === 'attempt_started') {
      const stage = stageOfSummary(event.summary)
      const bar = {
        key: `journal:${event.at}:${bars.length}`,
        number: attemptOfSummary(event.summary) ? `#${attemptOfSummary(event.summary)}` : '',
        stage: stage || 'попытка',
        laneKey: stage || 'попытка',
        laneLabel: stage || 'попытка',
        lane: 0,
        start: event.at,
        end: null,
        endAt: null,
        duration: null,
        durationSec: null,
        durationText: '',
        open: false,
        lease: '',
        leaseText: '',
        progress: null,
        tools: null,
        // У полосы из журнала телеметрии нет: событие `attempt_started` знает
        // только про старт. Пустой список здесь — «вызовов в журнале нет», а не
        // «попытка ничего не делала»: экран так и подписывает.
        calls: [],
        callsOmitted: 0,
        callsText: '',
        kind: 'agent',
        role: '',
        outcome: '',
        status: '',
        tone: 'gray',
        leftPct: 0,
        endPct: 0,
        widthPct: 0,
        clockStart: fmtClock(event.at),
        clockEnd: '',
        label: '',
        title: '',
        ariaLabel: '',
        progressText: '',
        toolsText: '',
        source: 'journal',
      }
      bars.push(bar)
      open.push(bar)
    } else if (event.kind === 'attempt_ended' && markerType(event.kind, event.summary) === 'ended') {
      const bar = open.shift()
      if (!bar) continue
      bar.end = event.at
      bar.duration = event.at - bar.start
      bar.outcome = String((/"outcome":\s*"([^"]*)"/.exec(event.summary) || [])[1] || '')
      // Журнал пишет причину и класс отказа рядом с исходом — берём их оттуда,
      // иначе у дела без ручки попыток провал остаётся без объяснения.
      const ended = summaryFields(event.summary, ['reason', 'kind'])
      bar.reason = String(ended.reason || '').trim()
      bar.failureKind = String(ended.kind || '').trim()
      bar.tone = outcomeTone(bar.outcome, '', crashes)
    }
  }
  // Незакрытая пара — та же незакрытая попытка: тянется до `now` пунктиром.
  for (const bar of open) bar.open = true
  return bars
}

/**
 * Шкала времени дела: одна ось, дорожки этапов, полосы попыток, маркеры событий
 * и строка фактов шапки. Единственный расчёт геометрии на обе поверхности.
 *
 * Ось: начало — минимум из `timeline[].at` и `attempts[].start`, конец — максимум
 * из `at`/`end`, у живого дела — `now`. Полоса ставится по `start` и тянется на
 * `duration_sec`; параллельные этапы живут на своих дорожках, поэтому перекрытие
 * видно глазом, а не по легенде.
 *
 * Свой расчёт чисел шапки не заводится: бюджет берётся блоком `budget` ответа,
 * публикация — блоком `published`, закрытие — блоком `closed`; здесь только
 * раскладка по словам и процентам.
 */
/**
 * Граф спеки: узлы — этапы, рёбра — `needs`. Считается здесь, а не на
 * поверхностях: обе рисуют одни и те же координаты, поэтому картинка не может
 * «поехать» на одной из них. Глубина узла — самый длинный путь от корня, так что
 * этап встаёт правее всех, после кого он ждёт.
 *
 * Красный — про ПОСЛЕДНЮЮ попытку, а не про память о провале: этап, упавший в
 * середине истории и повторённый успешно, красным не остаётся. Число провалов
 * живёт в подсказке (`title`) — память есть, но она не красит узел.
 *
 * Живой этап помечается `live`, и поверхности анимируют обводку: граф не должен
 * выглядеть застывшей картинкой, пока попытка идёт.
 */
function specView(rawStages) {
  const stages = (Array.isArray(rawStages) ? rawStages : []).map(shapeStage)
  if (!stages.length) return null
  const byId = new Map(stages.map((stage) => [stage.id, stage]))
  const depths = new Map()
  const depthOf = (id, trail) => {
    if (depths.has(id)) return depths.get(id)
    // Цикл в `needs` — не повод падать: этап встаёт на первый уровень.
    if (trail.has(id)) return 0
    trail.add(id)
    const stage = byId.get(id)
    const needs = stage ? stage.needs.filter((need) => byId.has(need)) : []
    const depth = needs.length ? Math.max(...needs.map((need) => depthOf(need, trail) + 1)) : 0
    trail.delete(id)
    depths.set(id, depth)
    return depth
  }
  const columns = []
  const nodes = stages.map((stage) => {
    const depth = depthOf(stage.id, new Set())
    const row = columns[depth] === undefined ? 0 : columns[depth]
    columns[depth] = row + 1
    const attempts = Array.isArray(stage.attempts) ? stage.attempts : []
    const last = attempts[attempts.length - 1]
    const fails = attempts.filter((attempt) => attempt.tone === 'red').length
    // Тон: провал последней попытки — красный; отказ этапа без попыток вовсе —
    // тоже красный (иначе «упало» выглядело бы серым «ждёт хода»); остальное —
    // состояние этапа.
    let tone = stage.tone
    if (last && last.tone === 'red') tone = 'red'
    else if (!attempts.length && stage.state === 'failed') tone = 'red'
    return {
      id: stage.id,
      role: stage.role,
      label: stage.stateLabel,
      tone,
      live: stage.state === 'running',
      kind: stage.kind || 'agent',
      needsText: stage.needsText,
      // Подсказка одной строкой: подпись узла, память о провалах и ожидание.
      title: [
        `${stage.id} · ${stage.stateLabel}`,
        fails ? `провалов ${fails}` : '',
        stage.needsText,
      ]
        .filter(Boolean)
        .join(' · '),
      depth,
      row,
      x: SPEC_PAD + depth * (SPEC_NODE.w + SPEC_NODE.gapX),
      y: SPEC_PAD + row * (SPEC_NODE.h + SPEC_NODE.gapY),
    }
  })
  const pos = new Map(nodes.map((node) => [node.id, node]))
  const edges = []
  for (const stage of stages) {
    const to = pos.get(stage.id)
    for (const need of stage.needs) {
      if (!byId.has(need)) continue
      const from = pos.get(need)
      const x1 = from.x + SPEC_NODE.w
      const y1 = from.y + SPEC_NODE.h / 2
      const x2 = to.x
      const y2 = to.y + SPEC_NODE.h / 2
      // Прямая линия между колонками читалась как «здесь ничего не течёт»:
      // кубическая кривая даёт ребро, у которого видно направление, а стрелка
      // называет его точкой. Вся геометрия — здесь, поверхность её не считает.
      const dx = Math.max(12, (x2 - x1) * 0.45)
      edges.push({
        from: need,
        to: stage.id,
        d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`,
        // Треугольник острием в цель: длина 6, полувысота 3.
        arrow: { points: `${x2 - 6},${y2 - 3} ${x2},${y2} ${x2 - 6},${y2 + 3}` },
      })
    }
  }
  const width = Math.max(...nodes.map((node) => node.x + SPEC_NODE.w)) + SPEC_PAD
  const height = Math.max(...nodes.map((node) => node.y + SPEC_NODE.h)) + SPEC_PAD
  return { nodes, edges, width, height, nodeW: SPEC_NODE.w, nodeH: SPEC_NODE.h }
}

function shapeDealTime(detail, attemptsPayload) {
  const d = detail && typeof detail === 'object' ? detail : {}
  const a = attemptsPayload && typeof attemptsPayload === 'object' ? attemptsPayload : {}
  const now = numOrNull(d.now) ?? numOrNull(a.now)
  // «Живое» — по `terminal` ответа: закрытое дело ось не тянет вперёд, и его
  // длительность перестаёт расти в ту же секунду, когда записан `completed`.
  // Событие закрытия в журнале — тот же признак, если сервер не отдал `terminal`.
  const events = (Array.isArray(d.timeline) ? d.timeline : []).map((event) => {
    const e = event && typeof event === 'object' ? event : {}
    // Текст вопроса и ответа, причина закрытия — поля ответа бэкенда, а не
    // разбор `summary`: журнал хранит номер вопроса, пул — сам вопрос.
    return {
      kind: String(e.kind || ''),
      at: numOrNull(e.at) || 0,
      summary: String(e.summary || ''),
      question_text: e.question_text ? String(e.question_text) : '',
      answer_text: e.answer_text ? String(e.answer_text) : '',
      cause: e.cause ? String(e.cause) : '',
      stages: Array.isArray(e.stages) ? e.stages.map(String) : [],
    }
  })
  const closedInJournal = events.some((event) => markerType(event.kind, event.summary) === 'closed')
  const live = d.terminal !== true && !closedInJournal
  const rows = Array.isArray(a.attempts) ? a.attempts : []
  const crashes = Array.isArray(a.crash_outcomes) ? a.crash_outcomes.map(String) : []
  const bars = rows.length ? rows.map((row) => attemptBar(row, crashes)) : journalBars(events, crashes)

  // Ось: только записанные времена. Пусто — шкалы нет, и рисовать «нулевую ось»
  // значило бы выдумать интервал, которого в журнале не было.
  const stamps = []
  for (const event of events) if (event.at > 0) stamps.push(event.at)
  for (const bar of bars) if (bar.start > 0) stamps.push(bar.start)
  const known = stamps.length > 0
  const axisStart = known ? Math.min(...stamps) : null
  let axisEnd = known ? Math.max(...stamps) : null
  if (live && now !== null) axisEnd = axisEnd === null ? now : Math.max(axisEnd, now)
  // Закрытая попытка без `end`, но с длительностью: конец считаем по ней, иначе
  // полоса оборвалась бы на `start` и шкала «съела» самый длинный этап.
  for (const bar of bars) {
    if (bar.start === null) continue
    const stop = bar.end !== null ? bar.end : bar.start + (bar.duration || 0)
    if (stop > (axisEnd === null ? 0 : axisEnd) && !live) axisEnd = stop
  }
  const span = axisStart === null || axisEnd === null ? 0 : Math.max(1, axisEnd - axisStart)

  // Конец полосы — один расчёт на весь шейпер: `open` тянется до `now` (живая
  // работа простоем не бывает), закрытая без `end` — до `start + duration`.
  for (const bar of bars) {
    bar.endAt = bar.open
      ? (now !== null ? now : bar.start)
      : bar.end !== null
        ? bar.end
        : bar.start === null
          ? null
          : bar.start + (bar.duration || 0)
  }

  // Простой — промежутки между занятым временем, включая края оси: дело, которое
  // стоит в ожидании движка, тоже простой. Сличаются только интервалы с обеими
  // границами; пересекающиеся и касающиеся сливаются — паузы между ними нет.
  const idleGaps = []
  const idleSegments = []
  let idleTotalSec = 0
  let idleCompressedPct = 0
  let axisVirtual = 0
  if (axisStart !== null && axisEnd !== null) {
    const busy = bars
      .filter((bar) => bar.start !== null && bar.endAt !== null)
      .map((bar) => [
        Math.max(axisStart, Math.min(bar.start, bar.endAt)),
        Math.min(axisEnd, Math.max(bar.start, bar.endAt)),
      ])
      .filter(([from, to]) => to > from)
      .sort((left, right) => left[0] - right[0])
    const merged = []
    for (const [from, to] of busy) {
      const last = merged[merged.length - 1]
      if (last && from <= last[1]) last[1] = Math.max(last[1], to)
      else merged.push([from, to])
    }
    const regions = []
    let cursor = axisStart
    for (const [from, to] of merged) {
      if (from > cursor) regions.push({ from: cursor, to: from, busy: false })
      regions.push({ from, to, busy: true })
      cursor = to
    }
    if (merged.length && axisEnd > cursor) regions.push({ from: cursor, to: axisEnd, busy: false })
    // Полос нет вовсе (только события): занятого времени не знаем, сжимать
    // нечего — ось остаётся линейной. Нулевая ось (всё в одну секунду) тоже:
    // растягивать её нечем.
    if (!merged.length) {
      axisVirtual = span
      idleSegments.push({ from: axisStart, to: axisEnd, virt: axisEnd > axisStart ? span : 0 })
    } else {
      const tight = regions.filter((region) => !region.busy && region.to - region.from > IDLE_MIN_SEC && region.to - region.from >= span * IDLE_MIN_SHARE)
      const width = tight.length ? Math.min(IDLE_GAP_PCT, IDLE_TOTAL_PCT / tight.length) : 0
      // Сжатие — смена масштаба, а не выкинутое время: внутри промежутка
      // координата линейна по сжатой ширине, занятое время идёт один к одному.
      // Сжатая ширина — доля оси, поэтому виртуальная длина оси растягивает
      // занятое время так, чтобы сжатия заняли ровно свои проценты.
      const busySec = span - tight.reduce((sum, region) => sum + (region.to - region.from), 0)
      axisVirtual = busySec / (1 - (width * tight.length) / 100)
      for (const region of regions) {
        const squeeze = !region.busy && tight.includes(region)
        idleSegments.push({ from: region.from, to: region.to, virt: squeeze ? (width / 100) * axisVirtual : region.to - region.from })
        if (!squeeze) continue
        idleTotalSec += region.to - region.from
        idleCompressedPct += width
        idleGaps.push({ from: region.from, to: region.to })
      }
    }
  }
  const pctOf = (at) => {
    if (axisStart === null || !(at > 0) || !(axisVirtual > 0)) return 0
    // Промежуток ищется по времени: внутри сжатого координата линейна по сжатой
    // ширине, занятое время — один к одному. Второй формулы координат нет.
    let virt = 0
    for (const segment of idleSegments) {
      if (at >= segment.to) {
        virt += segment.virt
        continue
      }
      if (at > segment.from) virt += ((at - segment.from) / (segment.to - segment.from)) * segment.virt
      break
    }
    const value = (virt / axisVirtual) * 100
    return Math.round(Math.max(0, Math.min(100, value)) * 10) / 10
  }
  // Сжатый промежуток подписан: молча сжатая ось читается как ошибка данных.
  // `text` — само время простоя, `title` — то же плюс пояснение про сжатие.
  const idle = {
    gaps: idleGaps.map((gap) => {
      const minutes = Math.floor((gap.to - gap.from) / 60) % 60
      const sec = gap.to - gap.from
      const text = `простой ${fmtDuration(sec)}${sec >= 3600 && minutes ? ` ${minutes} м` : ''}`
      const leftPct = pctOf(gap.from)
      const rightPct = pctOf(gap.to)
      const center = (leftPct + rightPct) / 2
      // Подпись у самого края шкалы вылезала за панель: промежуток в 3 % у правой
      // границы ставил текст по центру, и половина его уходила наружу. У краёв
      // якорь переезжает внутрь — `end` у правого, `start` у левого, — поэтому
      // подпись всегда стоит над своей «дырой», но целиком в поле графика.
      const edge = center <= IDLE_LABEL_EDGE_PCT
        ? { anchor: 'start', at: leftPct }
        : center >= 100 - IDLE_LABEL_EDGE_PCT
          ? { anchor: 'end', at: rightPct }
          : { anchor: 'middle', at: center }
      return {
        from: gap.from,
        to: gap.to,
        sec,
        text,
        leftPct,
        rightPct,
        labelPct: Math.round(edge.at * 10) / 10,
        labelAnchor: edge.anchor,
        title: `${fmtClock(gap.from)}–${fmtClock(gap.to)}: ${text} · время на шкале сжато`,
      }
    }),
    totalSec: idleTotalSec,
    compressedPct: Math.round(idleCompressedPct * 10) / 10,
  }

  // Дорожки: сначала этапы графа в его порядке (`stages[]` ответа `/deal`) —
  // тогда дорожек столько же, сколько этапов, и видно, какой этап ещё не
  // начинался; потом этапы, которых в графе нет (попытка без спеки, дело
  // kanban), в порядке появления на оси.
  const lanes = []
  const laneIndex = new Map()
  const addLane = (key) => {
    if (!key || laneIndex.has(key)) return
    laneIndex.set(key, lanes.length)
    lanes.push({ key, label: key, count: 0 })
  }
  for (const stage of Array.isArray(d.stages) ? d.stages : []) addLane(String((stage && stage.id) || ''))
  for (const bar of [...bars].sort((left, right) => (left.start || 0) - (right.start || 0))) {
    addLane(bar.laneKey)
    bar.lane = laneIndex.get(bar.laneKey)
    lanes[bar.lane].count += 1
  }
  if (!lanes.length) bars.forEach((bar) => { bar.lane = 0 })

  for (const bar of bars) {
    // Конец полосы посчитан выше — тем же расчётом, по которому сжимается ось.
    const stop = bar.endAt
    bar.leftPct = pctOf(bar.start)
    bar.endPct = pctOf(stop)
    bar.widthPct = bar.start === null ? 0 : Math.max(BAR_MIN_PCT, Math.round((bar.endPct - bar.leftPct) * 10) / 10)
    bar.durationSec = bar.open
      ? (bar.start !== null && now !== null ? Math.max(0, now - bar.start) : null)
      : bar.duration !== null
        ? bar.duration
        : bar.start !== null && bar.end !== null
          ? bar.end - bar.start
          : null
    bar.durationText = bar.open ? 'идёт' : fmtDuration(bar.durationSec)
    bar.clockEnd = bar.open ? (now !== null ? fmtClock(now) : '—') : fmtClock(stop)
    bar.progressText = bar.progress === null ? '' : `шагов ${fmtCount(bar.progress)}`
    bar.toolsText = bar.tools === null ? '' : `инструментов ${fmtCount(bar.tools)}`
    // Вызовы: сколько их в телеметрии и не обрезано ли. Это отдельная строка от
    // `инструментов` — там счётчик движка, здесь то, что реально можно нарисовать.
    // Здесь только то, что реально нарисовано: «ещё N не показано» называет
    // вид строки (`attemptView`), иначе одна и та же цифра звучала бы дважды.
    bar.callsText = (bar.calls || []).length
      ? `${fmtCount(bar.calls.length)} ${plural(bar.calls.length, 'вызов', 'вызова', 'вызовов')}`
      : ''
    // Подпись внутри полосы — исход, а не догадка: у незакрытой это слово о
    // лизинге, у закрытой без исхода — её длительность.
    bar.label = bar.open ? bar.leaseText : outcomeWord(bar.outcome) || bar.status || bar.durationText
    // Короткая подпись строки таймлайна: исход по-русски, у незакрытой — лизинг.
    bar.outcomeText = bar.open ? bar.leaseText : outcomeWord(bar.outcome) || bar.status || bar.durationText
    const details = [bar.progressText, bar.toolsText, bar.callsText].filter(Boolean).join(', ')
    const head = `${bar.laneLabel} · попытка ${bar.number || '—'}`
    const interval = `${bar.clockStart}–${bar.clockEnd} (${bar.durationText})`
    const cause = barReasonText(bar)
    const tail = bar.open
      ? `лизинг: ${bar.leaseText}`
      : `исход: ${outcomeWord(bar.outcome) || bar.status || 'не записан'}${cause ? ` · ${cause}` : ''}`
    bar.title = `${head} · ${interval} · ${tail}${details ? ` · ${details}` : ''}`
    bar.ariaLabel = bar.title
  }

  // Маркеры: только факты журнала. Вопрос без ответа получает интервал до `now` —
  // это и есть «сколько человек думает», а не догадка о его решении.
  const markers = []
  const pending = []
  for (const event of events) {
    const type = markerType(event.kind, event.summary)
    if (!type) continue
    const meta = MARKER_META[type]
    const clock = fmtClock(event.at)
    // Подпись маркера — слова, а не payload: номер вопроса и JSON причины
    // человеку ничего не говорят. Текст вопроса и ответа даёт бэкенд
    // (`question_text`/`answer_text` из пула), причину закрытия — поля `cause`.
    const body = markerBody(type, event)
    const title = `${clock} · ${meta.label}${body ? ` — ${body}` : ''}`
    const marker = {
      type,
      kind: event.kind,
      at: event.at,
      pct: pctOf(event.at),
      clock,
      label: meta.label,
      tone: meta.tone,
      tick: meta.tick,
      anchor: meta.anchor,
      summary: event.summary,
      // Человеческое тело подписи: вопрос/ответ текстом, причина закрытия
      // словами. Пауза «вопрос → ответ» называет им сам вопрос.
      text: body,
      title,
      ariaLabel: title,
      answered: false,
      waitPct: 0,
      waitText: '',
    }
    if (type === 'question') pending.push(marker)
    // Ответ закрывает первый неотвеченный вопрос: порядок событий и есть порядок
    // ответов, а `question_id` в выжимке бывает обрезан.
    else if (type === 'answer' && pending.length) pending.shift().answered = true
    markers.push(marker)
  }
  // Паузы «вопрос → ответ»: считается на всю ось, потому что ответ часто
  // приходит уже в следующей попытке. Полоса рисуется в строке той попытки, где
  // вопрос был задан, — там, где дело и встало.
  const waitSpans = []
  {
    let asked = null
    for (const marker of markers) {
      if (marker.type === 'question') asked = marker
      else if (marker.type === 'answer' && asked) {
        waitSpans.push({
          fromAt: asked.at,
          toAt: marker.at,
          fromPct: asked.pct,
          widthPct: Math.max(0.4, Math.round((marker.pct - asked.pct) * 10) / 10),
          title: `${asked.clock} → ${marker.clock}: ждал человека${asked.text ? ` — ${asked.text}` : ''}`,
        })
        asked = null
      }
    }
    if (asked && now !== null) {
      waitSpans.push({
        fromAt: asked.at,
        toAt: now,
        fromPct: asked.pct,
        widthPct: Math.max(0.4, Math.round((pctOf(now) - asked.pct) * 10) / 10),
        title: `${asked.clock} → сейчас: ждёт ответа`,
      })
    }
  }

  for (const marker of pending) {
    marker.waitText = 'ждёт ответа'
    if (now !== null && marker.at > 0) {
      marker.waitPct = Math.max(0, Math.round((pctOf(now) - marker.pct) * 10) / 10)
      marker.title = `${marker.title} · ждёт ответа ${fmtAge(now - marker.at)}`
      marker.ariaLabel = marker.title
    }
  }

  // Шапка дела: числа из ответа, ни одного нового расчёта на бэкенде.
  const answered = markers.filter((marker) => marker.type === 'answer').length
  const waiting = pending.length
  const asked = markers.filter((marker) => marker.type === 'question').length
  const budgetRaw = d.budget && typeof d.budget === 'object' ? d.budget : a.budget && typeof a.budget === 'object' ? a.budget : null
  let budget = null
  if (budgetRaw) {
    const units = []
    for (const [unit, label] of [['tokens', 'токенов'], ['minutes', 'минут']]) {
      const part = budgetRaw[unit]
      if (!part || typeof part !== 'object') continue
      const spent = Number(part.spent) || 0
      const limit = Number(part.limit) || 0
      const share = limit > 0 ? Math.min(100, Math.round((spent / limit) * 100)) : 0
      units.push({ unit, label, spent, limit, pct: share, text: `${label} ${fmtCount(spent)} / ${fmtCount(limit)} (${share}%)` })
    }
    const rawText = String(budgetRaw.text || '')
    budget = {
      units,
      exhausted: budgetRaw.exhausted ? String(budgetRaw.exhausted) : '',
      attempts: Number(budgetRaw.attempts) || 0,
      text: units.length ? units.map((item) => item.text).join(' · ') : rawText,
    }
  }
  // Публикация: поле ответа, а если его нет (сервер старше 0.0.5) — событие
  // `published` журнала: те же sha/branch/verified_by, второй расчёт не нужен.
  const pubEvent = [...events].reverse().find((event) => event.kind === 'published')
  const pubRaw = d.published && typeof d.published === 'object'
    ? d.published
    : summaryFields(pubEvent && pubEvent.summary, ['sha', 'branch', 'ref', 'verified_by', 'mr_url'])
  // Форма публикации одна с экраном «Завершённые»: `fmtPublication`.
  const published = fmtPublication(pubRaw)
  // Закрытие: поле ответа, иначе событие журнала (`completed`/`blocked`/`failed`
  // с `cause`) — тот же факт, что рисует маркер `closed`.
  const closedEvent = [...events].reverse().find((event) => markerType(event.kind, event.summary) === 'closed')
  const closedRaw = d.closed && typeof d.closed === 'object'
    ? d.closed
    : closedEvent
      ? { at: closedEvent.at, cause: summaryFields(closedEvent.summary, ['cause']).cause || '' }
      : null
  const closed = closedRaw
    ? {
        at: numOrNull(closedRaw.at),
        clock: fmtClock(closedRaw.at),
        cause: String(closedRaw.cause || ''),
        // Причина закрытия — словами, как и в маркере: `all_stages_done` в
        // человеческой строке читается как сбой экрана, а не как факт.
        text: `закрыто ${fmtClock(closedRaw.at)}${
          closedRaw.cause ? `: ${CLOSE_CAUSE_WORD[String(closedRaw.cause)] || closedRaw.cause}` : ''
        }`,
      }
    : null
  const durationSec = axisStart === null || axisEnd === null ? null : Math.max(0, axisEnd - axisStart)

  // Ось — линейка, а не три подписи (как в согласованном макете `d-merge`):
  // деление выбирается так, чтобы на ширине было 4–8 меток, и через каждую метку
  // идёт вертикальная линия по всем дорожкам. Шаг кратен 2/5/10 минутам, поэтому
  // метки попадают на «круглые» секунды и не пляшут при обновлении.
  const axisTicks = []
  if (axisStart !== null && axisEnd !== null && durationSec > 0) {
    // Лестница делений: берём первое, при котором на оси не больше восьми меток.
    // Иначе часовое дело получало бы тринадцать подписей, а минутное — две.
    const step = [60, 120, 300, 600, 900, 1200, 1800, 2400, 3600, 7200, 10800, 21600, 43200, 86400]
      .find((candidate) => durationSec / candidate <= 8) || 86400
    axisTicks.push({ pct: 0, edge: 'нач.', align: 'start', clock: fmtClock(axisStart) })
    for (let at = Math.ceil(axisStart / step) * step; at < axisEnd; at += step) {
      // Метка на оси — той же координатой, что полосы и маркеры: внутри сжатого
      // промежутка она встаёт на его сжатую ширину, а не на реальное время.
      const pct = pctOf(at)
      // Внутри сжатого промежутка деление встало бы вплотную к соседям: время
      // там называет подпись сжатия, а не три наложенные друг на друга метки.
      if (idle.gaps.some((gap) => at > gap.from && at < gap.to)) continue
      // Подписи краёв широкие («нач. 13:37:35» — двенадцать знаков), а метка
      // центрируется на своей линии: её текст и вертикаль заходят на подпись
      // края. Резерв — ширина подписи края (8 % при графике в 900 px) плюс
      // половина подписи метки, итого 12 %; внутри него метку не рисуем вовсе,
      // чтобы её линия не перечёркивала слово.
      if (pct < TICK_EDGE_RESERVE_PCT || pct > 100 - TICK_EDGE_RESERVE_PCT) continue
      axisTicks.push({ pct, edge: '', align: 'center', clock: fmtClock(at) })
    }
    axisTicks.push({ pct: 100, edge: 'кон.', align: 'end', clock: fmtClock(axisEnd) })
  }
  // KPI-строка (макет `d-merge`): пять чисел шапки, все — суммы по уже собранным
  // полосам и попыткам. Токены показываем только когда их записала телеметрия:
  // «—» здесь честнее нуля.
  const callsShown = bars.reduce((sum, bar) => sum + bar.calls.length, 0)
  const callsHidden = bars.reduce((sum, bar) => sum + (bar.callsOmitted || 0), 0)
  const tokensKnown = bars.some((bar) => bar.tokens !== null && bar.tokens !== undefined)
  const tokensTotal = bars.reduce((sum, bar) => sum + (numOrNull(bar.tokens) || 0), 0)
  // Легенда семейств инструментов с числами: цвет без подписи — ребус, без
  // числа — «что-то было». Порядок как в `TOOL_LEGEND`, чтобы легенда не
  // прыгала от дела к делу.
  const toolCounts = new Map()
  for (const bar of bars) {
    for (const call of bar.calls) {
      const tone = toolTone(call.name)
      toolCounts.set(tone, (toolCounts.get(tone) || 0) + 1)
    }
  }
  const tools = TOOL_LEGEND.map((item) => ({ tone: item.tone, label: item.label, count: toolCounts.get(item.tone) || 0 }))
    .filter((item) => item.count > 0)

  const kpi = [
    { key: 'duration', label: 'длительность', value: durationSec === null ? '—' : fmtDuration(durationSec) },
    { key: 'stages', label: 'этапов', value: fmtCount(lanes.filter((lane) => lane.count > 0).length) },
    // Разбивка по этапам живёт подсказкой этой ячейки: отдельной строкой она
    // повторяла то же число второй раз.
    {
      key: 'attempts',
      label: 'попыток',
      value: fmtCount(bars.length),
      title: attemptsBreakdown(lanes, bars),
    },
    {
      key: 'calls',
      label: 'вызовов',
      value: callsShown + callsHidden > 0 ? fmtCount(callsShown + callsHidden) : '—',
      note: callsHidden > 0 ? `показано ${fmtCount(callsShown)}` : '',
    },
    tokensKnown
      ? { key: 'tokens', label: 'токенов', value: fmtCount(tokensTotal) }
      : { key: 'tokens', label: 'токенов', value: '—', note: 'телеметрия не писала' },
  ]

  return {
    now,
    live,
    empty: bars.length === 0 && markers.length === 0,
    // Карта оси — единственная формула координат: полосы, маркеры, засечки,
    // пауза вопроса и чипы вызовов (`attemptView` → `layoutCalls`) берут время
    // отсюда. Второй формулы нет: иначе сжатый простой развёл бы чип и маркер
    // одного и того же мгновения.
    pctOf,
    axis: {
      start: axisStart,
      end: axisEnd,
      spanSec: durationSec,
      live,
      text: axisStart === null || axisEnd === null ? 'времени в журнале нет' : `${fmtClockShort(axisStart)}–${fmtClockShort(axisEnd)}`,
      ticks: axisTicks,
    },
    lanes,
    bars,
    markers,
    waitSpans,
    // Сжатые промежутки оси — уже с координатами: поверхность ничего не считает.
    idle,
    tools,
    kpi,
    // Граф спеки — из `stages[]` ответа `/deal`; нет этапов — нет и графа.
    spec: specView(d.stages),
    facts: {
      attempts: {
        total: bars.length,
        // «по этапам» — только этапы, где попытки были: `gate 0` в шапке читалось
        // бы как сломанный этап, а не как «ещё не начинался».
        byStage: lanes
          .filter((lane) => lane.count > 0)
          .map((lane) => ({ key: lane.key, label: lane.label, count: lane.count })),
        text: lanes
          .filter((lane) => lane.count > 0)
          .map((lane) => `${lane.label} ${lane.count}`)
          .join(' · '),
        journal: bars.length > 0 && bars.every((bar) => bar.source === 'journal'),
      },
      duration: { sec: durationSec, text: durationSec === null ? '—' : fmtDuration(durationSec), live },
      questions: {
        asked,
        answered,
        waiting,
        text: `${asked} ${plural(asked, 'вопрос', 'вопроса', 'вопросов')}${
          waiting ? ` · ${waiting} ${plural(waiting, 'ждёт', 'ждут', 'ждут')} ответа` : ''
        }`,
      },
      budget,
      published,
      closed,
    },
    counts: { bars: bars.length, lanes: lanes.length, markers: markers.length },
  }
}

/* ------------------------------------- тихое обновление страницы дела (0.0.8)
 *
 * Дело меняется минутами, а страница дела — снимок: чтобы увидеть, что попытка
 * закончилась, надо жать обновление руками. Грубый `location.reload()` раз в
 * пять секунд был бы худшим из решений — мигание, потеря прокрутки, сброс
 * раскрытых попыток. Поэтому страница тихо опрашивает тот же адрес и правит
 * изменившиеся узлы; период и пороги живут здесь, чтобы веб и десктоп не
 * разъехались в правилах.
 */

// Идущее дело обновляем часто, закрытое — редко: у закрытого не меняется ничего,
// кроме, разве что, поздней телеметрии.
const REFRESH_LIVE_SEC = 5
const REFRESH_DONE_SEC = 30
// Отказы подряд: 5 → 10 → 20 → 40. Дальше не чаще раза в 40 секунд — сервер,
// который лежит, не станет здоровее от третьего запроса в минуту.
const REFRESH_BACKOFF_SEC = [5, 10, 20, 40]
// Пока отказов меньше трёх, экран молчит: одиночный таймаут — не сломанное дело,
// и рисовать по нему ошибку значило бы мигать баннером на каждом сбое сети.
const REFRESH_FAIL_SILENT = 3

/**
 * План следующего тиха: когда опрашивать и надо ли вообще.
 *
 * `skip` — тик пропускается, но не навсегда: `delaySec` остаётся периодом (кроме
 * скрытой вкладки, где опрос стоит до `visibilitychange`). Решение принимает
 * шейпер, а не поверхность: иначе веб и десктоп начнут считать секунды по-своему.
 */
function refreshPlan(state) {
  const s = state && typeof state === 'object' ? state : {}
  const terminal = s.terminal === true
  const period = terminal ? REFRESH_DONE_SEC : REFRESH_LIVE_SEC
  // Скрытая вкладка не опрашивает вовсе: `delaySec: 0` — «сразу после возврата».
  if (s.hidden === true) return { delaySec: 0, skip: true, reason: 'вкладка не видна' }
  // Человек печатает или правит — нечего забирать у него набранный текст.
  if (s.focused === true) return { delaySec: period, skip: true, reason: 'введён текст' }
  const failures = Math.max(0, Math.floor(Number(s.failures) || 0))
  if (failures >= 1) {
    const delaySec = REFRESH_BACKOFF_SEC[Math.min(failures - 1, REFRESH_BACKOFF_SEC.length - 1)]
    return { delaySec, skip: false, reason: `отказов подряд: ${failures}` }
  }
  return { delaySec: period, skip: false, reason: '' }
}

// Поля вида, по которым видно изменение значения. Сверка идёт по ним, а не по
// всему узлу: у полосы меняется ширина каждую секунду у идущей попытки, и
// подсвечивать от этого всю шкалу незачем.
const FRESH_FIELDS = ['state', 'outcome', 'durationText', 'label', 'text', 'value', 'pct']

/** Стабильный ключ узла вида: `key`, иначе `id`, иначе начало промежутка. */
function freshNodeKey(node) {
  const n = node && typeof node === 'object' ? node : {}
  if (n.key !== undefined && n.key !== null && n.key !== '') return String(n.key)
  if (n.id !== undefined && n.id !== null && n.id !== '') return String(n.id)
  if (n.from !== undefined && n.from !== null) return `from:${n.from}`
  return ''
}

/** Подпись значения: только поля вида, в одном порядке. */
function freshSignature(node) {
  const n = node && typeof node === 'object' ? node : {}
  return FRESH_FIELDS.map((field) => {
    const value = n[field]
    return value === undefined || value === null ? '' : String(value)
  }).join('\u0000')
}

/**
 * Ключи узлов, чьё значение изменилось между двумя снимками вида.
 *
 * Пропавший или появившийся узел сюда не попадает: его вставляет или удаляет
 * патч, а подсветка — про изменившееся значение уже нарисованного узла. Иначе
 * новая попытка вспыхивала бы целиком, хотя она просто появилась.
 */
function freshKeys(prev, next) {
  const before = new Map()
  for (const node of Array.isArray(prev) ? prev : []) {
    const key = freshNodeKey(node)
    if (key) before.set(key, node)
  }
  const keys = []
  for (const node of Array.isArray(next) ? next : []) {
    const key = freshNodeKey(node)
    if (!key || !before.has(key)) continue
    if (freshSignature(before.get(key)) === freshSignature(node)) continue
    if (keys.includes(key)) continue
    keys.push(key)
  }
  return keys
}

export {
  STATE_META,
  WAIT_HUMAN_LABEL,
  TONE_COLOR,
  STAGE_META,
  FINISHED_STATUS_META,
  WITHOUT_REPO_LABEL,
  ALL_REPOS,
  repoLabel,
  repoShort,
  fileLabel,
  docBlocks,
  stateMeta,
  stageMeta,
  finishedStatusMeta,
  humanCounts,
  humanLine,
  fmtAge,
  fmtTitle,
  fmtClock,
  shapeDeal,
  shapeFinished,
  shapeFinishedPage,
  questionsText,
  stateCounts,
  stateLabel,
  groupDeals,
  groupByState,
  groupStatesText,
  shapeRepos,
  shapeNow,
  filterNow,
  dealTimeline,
  outcomeTone,
  layoutCalls,
  laneView,
  attemptView,
  toolTone,
  TOOL_LEGEND,
  KIND_ICON,
  TL,
  outcomeWord,
  plural,
  shapeAttempts,
  shapeCalls,
  shapeDealTime,
  shapeStages,
  stageJournal,
  compactStageView,
  compactAttemptView,
  cmdShort,
  eventsSummary,
  noticeLines,
  REFRESH_LIVE_SEC,
  REFRESH_DONE_SEC,
  REFRESH_BACKOFF_SEC,
  REFRESH_FAIL_SILENT,
  refreshPlan,
  freshKeys,
}
