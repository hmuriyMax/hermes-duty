/**
 * Hermes Duty — desktop-плагин «Дела»: страница /duty (экран «сейчас» + попытки).
 *
 * Форма — как у hermes-tickets: `export default { id, name, register(ctx) }`,
 * страница в `area: 'routes'`, пункт в `area: 'sidebar.nav'`. Данные — ТОЛЬКО
 * через `ctx.rest('/now')`, `ctx.rest('/deals')`, `ctx.rest('/spans')`,
 * `ctx.rest('/deal/{deal_id}')` и `ctx.rest('/duty/{deal_id}/attempts')`: прямого
 * fetch и шелла из UI нет, ядро не импортируется, файлы плагин не читает.
 *
 * Дело адресуется только своим id `d_<hex>` (0.0.6: доски нет). Классификация
 * состояний — не здесь: страница рисует `waiting.state` и `waiting.evidence` ровно
 * как их отдал бэкенд (`dashboard/plugin_api.py`, `WAITING_PRIORITY`). Логика UI —
 * группировка по `repo`, форматирование чисел и локальные фильтры; вся геометрия и
 * вычисления — в `desktop/model.js` (вшит ниже блоком `duty:model-start`).
 *
 * UI собран без JSX (файл грузится нескомпилированным, см. kanban-graph).
 */
import { Button, Codicon, Input, Streamdown } from '@hermes/plugin-sdk'
import { createElement as h, useCallback, useEffect, useMemo, useRef, useState } from 'react'

const ID = 'hermes-duty'
const PATH = '/duty'
const REFRESH_MS = 10_000
// «Здоровье» — агрегат по всей истории ранов: он не меняется за секунды, а скан
// всех досок дороже среза «сейчас», поэтому обновляется реже.
const STATS_REFRESH_MS = 30_000
const SEARCH_DEBOUNCE_MS = 240
const REQUEST_TIMEOUT_MS = 20_000

// Экраны страницы, в порядке спеки: «Сейчас» — срез, «Завершённые» — список
// закрытых дел, «Дело» — спецификация и таймлайн попыток, «Почему стоит» — тот
// же срез с объяснением, «Здоровье» — агрегат по ранам. «Дело» открывается из
// любой строки, но и сам по себе таб: экран, достижимый только кликом по строке,
// на своём месте не читается.
// Вкладки — как на вебе: «Дело» не таб, а экран, который открывается кликом по
// делу (09.11, Макс: «есть вкладка "Дело", которую мы убирали с веба»).
const TABS = [
  ['now', 'Сейчас'],
  ['why', 'Почему стоит'],
  ['health', 'Здоровье'],
]

// Последнее открытое дело: возврат с экрана дела ведёт к нему, а не к пустоте.
let lastDeal = null

// ctx.rest, забинденный в register().
let rest = null

// duty:model-start — генерируется bin/duty-model-sync из desktop/model.js; не править руками
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
  [2 * 3600, 20 * 60],
  [6 * 3600, 30 * 60],
  // Дальше — по часам: на суточном окне час читается, а три часа давали редкую
  // шкалу, по которой нельзя оценить, когда что было (09.11, Макс).
  [TL_WINDOW_SEC, 3600],
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
  const kind = String(b.failureKindText || b.failureKind || '').trim()
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
  lead_closed: 'закрыто лидом',
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
    // Имя класса словами приходит из шейпера сервера (`failure_kind` там — слаг):
    // слаг в подсказке человеку ничего не объясняет, а собственный перевод в
    // каждой поверхности разъехался бы между вебом и десктопом.
    failureKindText: String(a.kind_text || '').trim(),
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
// duty:model-end

/* ------------------------------- таймлайн вызовов внутри попытки (0.0.8) */

// Палитра — единственное своё у поверхности: тон приходит из шейпера ключом.
// Тон приходит из шейпера ключом; палитра — единственное своё у поверхности.
const DUTY_TONE_VAR = {
  read: 'var(--ui-blue)',
  search: 'var(--ui-purple)',
  write: 'var(--ui-green)',
  run: 'var(--ui-yellow)',
  net: 'var(--ui-red)',
  other: 'var(--ui-text-tertiary)',
}
function dutyToneVar(tone) {
  return DUTY_TONE_VAR[tone] || DUTY_TONE_VAR.other
}

/**
 * Строка попытки: фон-полоса на всю длительность, чипы вызовов и маркеры внутри.
 *
 * Границы попытки показывает фон, поэтому засечек старта/конца нет, а время
 * начала, конца и длительность живут в подсказке строки — они не спорят с чипами
 * за место на дорожке. Вид приходит готовым (`attemptView`), здесь только DOM.
 */
/* ---------------------------------------- слой подсказок поверх всей страницы */
/* Та же причина, что в вебе: внутри дорожки подсказку режет панель по
 * `overflow`, а соседние строки со своим контекстом наложения оказываются выше
 * любого `z-index` у хоста. Подсказка — один узел в `body`, наведение ловится
 * делегированием, потому что обновление страницы подменяет узлы. */
const DUTY_TIP_HOSTS = '.duty-tl-call, .duty-tl-band, .duty-tl-lbl, .duty-tl-mark, .duty-tl-wait'

let dutyTipBound = false
let dutyTipNode = null
let dutyTipHost = null

function dutyTipHide() {
  dutyTipHost = null
  if (!dutyTipNode) return
  dutyTipNode.style.opacity = '0'
  dutyTipNode.setAttribute('aria-hidden', 'true')
}

function dutyTipPlace(host) {
  const text = host.getAttribute('data-tip')
  if (!text) return dutyTipHide()
  if (!dutyTipNode || !dutyTipNode.isConnected) {
    dutyTipNode = document.createElement('div')
    dutyTipNode.className = 'duty-tl-tip'
    dutyTipNode.setAttribute('role', 'tooltip')
    dutyTipNode.setAttribute('aria-hidden', 'true')
    document.body.append(dutyTipNode)
  }
  if (dutyTipNode.textContent !== text) dutyTipNode.textContent = text
  dutyTipNode.style.opacity = '1'
  dutyTipNode.setAttribute('aria-hidden', 'false')
  const box = host.getBoundingClientRect()
  const own = dutyTipNode.getBoundingClientRect()
  const pad = 8
  const gap = 7
  let left = box.left + box.width / 2 - own.width / 2
  left = Math.max(pad, Math.min(left, window.innerWidth - own.width - pad))
  let top = box.top - own.height - gap
  if (top < pad) top = Math.min(box.bottom + gap, window.innerHeight - own.height - pad)
  dutyTipNode.style.left = `${Math.round(left)}px`
  dutyTipNode.style.top = `${Math.round(top)}px`
}

function dutyTipBind() {
  if (dutyTipBound || typeof document === 'undefined') return
  dutyTipBound = true
  const hostOf = (event) => {
    const target = event.target
    return target && target.closest ? target.closest(DUTY_TIP_HOSTS) : null
  }
  document.addEventListener('pointerover', (event) => {
    const host = hostOf(event)
    if (!host) return
    dutyTipHost = host
    dutyTipPlace(host)
  })
  document.addEventListener('pointerout', (event) => {
    const host = hostOf(event)
    if (!host || host !== dutyTipHost) return
    const next = event.relatedTarget
    if (next && host.contains(next)) return
    dutyTipHide()
  })
  document.addEventListener('focusin', (event) => {
    const host = hostOf(event)
    if (!host) return
    dutyTipHost = host
    dutyTipPlace(host)
  })
  document.addEventListener('focusout', dutyTipHide)
  const follow = () => {
    if (!dutyTipHost) return
    if (!dutyTipHost.isConnected) return dutyTipHide()
    dutyTipPlace(dutyTipHost)
  }
  window.addEventListener('scroll', follow, true)
  window.addEventListener('resize', follow)
}

function DutyAttemptRow({ view, callSel, onSelectCall, toolFilter }) {
  // Подсветка семейства: чужие вызовы гаснут, но остаются на месте — картина
  // дела не рассыпается, просто читается другое.
  const active = Array.isArray(toolFilter) ? toolFilter : []
  const label = h(
    'div',
    { className: 'duty-tl-lbl', 'data-tip': view.bandTip || view.title || '' },
    h('span', { className: 'duty-tl-n' }, view.label.number),
    h('span', { className: `duty-tl-st t-${view.label.tone}` }, view.label.status),
    view.badge
      ? h(
          'span',
          { className: `duty-tl-more${view.badgeCut ? ' is-cut' : ''}`, title: view.note || view.title },
          view.badge,
        )
      : null,
  )
  const track = []
  if (view.band) {
    track.push(
      h('span', {
        key: 'band',
        className: `duty-tl-band t-${view.band.tone || 'grey'}`,
        style: { left: `${view.band.leftPct}%`, width: `${view.band.widthPct}%` },
        'data-tip': view.bandTip,
      }),
    )
  }
  for (const [i, wait] of view.waits.entries()) {
    track.push(
      h('span', {
        key: `wait-${i}`,
        className: 'duty-tl-wait',
        style: { left: `${wait.fromPct}%`, width: `${wait.widthPct}%` },
        'data-tip': wait.title,
      }),
    )
  }
  for (const [i, chip] of view.chips.entries()) {
    track.push(
      h('span', {
        key: `call-${i}`,
        className: `duty-tl-call${chip.unfinished ? ' duty-tl-unfinished' : ''}${callSel === chip.id ? ' duty-tl-sel' : ''}${active.length && !active.includes(chip.tone) ? ' duty-tl-dim' : ''}`,
        'data-level': String(chip.level || 0),
        'data-tip': chip.tip || chip.title,
        'aria-label': chip.tip || chip.title,
        onClick: onSelectCall ? (event) => { event.stopPropagation(); onSelectCall(chip.id) } : undefined,
        style: chip.unfinished
          ? { left: `${chip.leftPct}%`, width: `${chip.widthPct}%`, top: `${chip.top}px`, color: dutyToneVar(chip.tone) }
          : { left: `${chip.leftPct}%`, width: `${chip.widthPct}%`, top: `${chip.top}px`, background: dutyToneVar(chip.tone) },
      }),
    )
  }
  if (view.marks.length) {
    track.push(
      h(
        'div',
        { key: 'marks', className: 'duty-tl-marks' },
        ...view.marks.map((mark, i) =>
          h(
            'span',
            {
              key: `mark-${i}`,
              className: `duty-tl-mark t-${mark.tone} anchor-${mark.anchor || 'middle'}`,
              style: { left: `${mark.pct}%` },
              'data-tip': mark.title,
              'aria-label': mark.ariaLabel,
            },
            h('i', null),
          ),
        ),
      ),
    )
  }
  return h(
    'div',
    // Свёрнутая высота — первый уровень вызовов и маркеры, полная раскрывается
    // под указателем. Числа из шейпера, CSS только переключает.
    {
      className: `duty-tl-row${view.failed ? ' is-failed' : ''}`,
      style: { '--duty-tl-collapsed': `${view.collapsed}px`, '--duty-tl-full': `${view.full}px` },
    },
    label,
    h('div', { className: 'duty-tl-track' }, ...track),
  )
}

/** Дорожка этапа: имя, значок вида, сводка и строки попыток. */
function DutyLane({ lane, bars, scale, callSel, onSelectCall, toolFilter }) {
  const view = laneView(lane, bars, scale)
  return h(
    'div',
    { className: 'duty-tl-lane' },
    h(
      'div',
      { className: 'duty-tl-lane-h' },
      h('span', { className: 'duty-tl-lane-name', title: view.name }, view.name),
      view.kind
        ? h(
            'span',
            {
              className: `duty-tl-kind duty-tl-kind-${view.kind}`,
              title: view.kind === 'agent' ? `профиль: ${view.kindText}` : view.kind,
            },
            h(
              'svg',
              { className: 'duty-tl-kind-ico', viewBox: '0 0 16 16', 'aria-hidden': 'true' },
              h('path', { d: view.icon }),
            ),
            view.kindText,
          )
        : null,
      h('span', { className: 'duty-tl-lane-meta' }, view.meta),
    ),
    view.empty
      ? h('div', { className: 'duty-tl-empty' }, 'попыток не было')
      : h(
          'div',
          { className: 'duty-tl-rows' },
          ...view.rows.map((row) =>
            h(DutyAttemptRow, { key: row.key, view: row.view, callSel, onSelectCall, toolFilter }),
          ),
        ),
  )
}

const CSS = `
.duty-page{display:flex;flex-direction:column;height:100%;min-height:0;background:var(--ui-surface-background);color:var(--ui-text-primary);font-size:13px}
.duty-head{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--ui-stroke-tertiary);flex:0 0 auto}
.duty-head h1{font-size:14px;font-weight:600;margin:0}
.duty-muted{color:var(--ui-text-tertiary);font-size:11px}
.duty-num{font-variant-numeric:tabular-nums}
.duty-spacer{flex:1 1 auto}
.duty-icon-btn{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:5px;border:0;background:transparent;color:var(--ui-text-tertiary);cursor:pointer}
.duty-icon-btn:hover{background:color-mix(in srgb, var(--ui-text-primary) 8%, transparent);color:var(--ui-text-primary)}
.duty-banner{display:flex;align-items:center;gap:8px;margin:8px 16px 0;padding:6px 10px;border-radius:6px;color:var(--ui-red);border:1px solid color-mix(in srgb, var(--ui-red) 40%, transparent);background:color-mix(in srgb, var(--ui-red) 12%, transparent);font-size:12px}
.duty-banner span{flex:1 1 auto;word-break:break-word}
.duty-note{margin:6px 16px 0;padding:6px 10px;border-radius:6px;font-size:11px;color:var(--ui-yellow);border:1px solid color-mix(in srgb, var(--ui-yellow) 35%, transparent);background:color-mix(in srgb, var(--ui-yellow) 10%, transparent)}
.duty-filters{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 16px;border-bottom:1px solid var(--ui-stroke-tertiary);flex:0 0 auto}
.duty-select{height:26px;max-width:200px;border-radius:6px;border:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-elevated);color:inherit;font-size:12px;padding:0 6px}
.duty-chips{display:flex;flex-wrap:wrap;gap:4px}
.duty-chip{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 8px;border-radius:999px;border:1px solid var(--ui-stroke-tertiary);background:transparent;color:var(--ui-text-secondary);font-size:11px;cursor:pointer;white-space:nowrap;font-family:inherit}
.duty-chip:hover{background:color-mix(in srgb, var(--ui-text-primary) 8%, transparent)}
.duty-chip.on{border-color:currentColor;color:var(--ui-text-primary)}
.duty-chip .duty-num{opacity:.6}
.duty-body{display:flex;min-height:0;flex:1 1 auto}
.duty-list{flex:1 1 auto;min-width:0;overflow:auto;padding:8px 12px 24px}
.duty-detail{flex:0 0 400px;max-width:46%;border-left:1px solid var(--ui-stroke-tertiary);overflow:auto;padding:12px 14px}
.duty-group{margin:0 0 16px}
.duty-group-h{display:flex;align-items:center;gap:6px;margin:6px 2px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-tertiary)}
.duty-table{width:100%;table-layout:fixed;border-collapse:collapse}
.duty-table th{text-align:left;font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-tertiary);padding:4px 6px;border-bottom:1px solid var(--ui-stroke-tertiary);white-space:nowrap}
.duty-table td{padding:5px 6px;border-bottom:1px solid color-mix(in srgb, var(--ui-stroke-tertiary) 60%, transparent);vertical-align:middle;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.duty-row{cursor:pointer}
.duty-row:hover td{background:color-mix(in srgb, var(--ui-text-primary) 6%, transparent)}
.duty-row.sel td{background:color-mix(in srgb, var(--ui-accent) 12%, transparent)}
.duty-mono{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11px;color:var(--ui-text-secondary)}
/* Идущий этап дела — акцентом и точкой: «где оно стоит» не должно тонуть
 * среди прочих ячеек строки. */
.duty-stage-now{color:var(--ui-accent) !important;font-weight:600}
.duty-stage-now::before{content:'● ';font-size:9px;vertical-align:2px}
.duty-badge{display:inline-flex;align-items:center;gap:4px;padding:1px 7px;border-radius:999px;font-size:11px;white-space:nowrap;border:1px solid color-mix(in srgb, currentColor 45%, transparent);background:color-mix(in srgb, currentColor 14%, transparent)}
.duty-badge i{display:inline-block;width:6px;height:6px;border-radius:50%;background:currentColor}
.duty-empty{display:flex;flex-direction:column;gap:8px;align-items:flex-start;padding:48px 16px;color:var(--ui-text-secondary)}
.duty-empty b{color:var(--ui-text-primary);font-size:13px}
.duty-empty ul{margin:0;padding-left:18px;font-size:12px;color:var(--ui-text-tertiary)}
.duty-skel{height:10px;border-radius:4px;background:linear-gradient(90deg,var(--ui-bg-quaternary) 25%,var(--ui-bg-quinary,var(--ui-bg-quaternary)) 37%,var(--ui-bg-quaternary) 63%);background-size:400% 100%;animation:duty-shimmer 1.4s ease-in-out infinite}
.duty-skel-row{display:flex;flex-direction:column;gap:6px;padding:8px 0}
@keyframes duty-shimmer{0%{background-position:100% 0}100%{background-position:0 0}}
.duty-panel-h{display:flex;align-items:flex-start;gap:8px;margin-bottom:6px}
.duty-panel-h .duty-mono{color:var(--ui-text-primary)}
.duty-panel-title{font-size:12px;font-weight:600;word-break:break-word}
.duty-evidence{margin:6px 0 12px;font-size:11px;color:var(--ui-text-secondary);background:var(--ui-bg-quaternary);border-radius:6px;padding:6px 8px;word-break:break-word}
.duty-att-h{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--ui-text-secondary)}
.duty-att-h .duty-num{color:var(--ui-text-primary)}
.duty-error{color:var(--ui-red);font-size:11px;white-space:pre-wrap;word-break:break-word;margin-top:4px}
.duty-summary{font-size:11px;color:var(--ui-text-tertiary);margin-top:4px;word-break:break-word}
/* ------------------------- столбец этапов и попыток (0.0.8)
   Одна панель на две бывшие: строки этапов (~26 px) и попытки внутри них
   (~24 px, отступ 12 px). Провал и живая попытка раскрыты без клика — это
   решение прошлого круга; остальные свёрнуты, иначе столбец снова простыня. */
.duty-deal-stages{display:flex;flex-direction:column;gap:2px}
.duty-stage-block{display:flex;flex-direction:column;min-width:0}
.duty-stage-row{display:flex;align-items:center;gap:6px;height:26px;padding:0 2px;font-size:12px;white-space:nowrap;overflow:hidden;cursor:pointer}
.duty-stage-block + .duty-stage-block .duty-stage-row{border-top:1px solid var(--ui-stroke-tertiary)}
.duty-stage-row:hover{background:color-mix(in srgb, var(--ui-text-primary) 6%, transparent)}
.duty-stage-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.duty-stage-row-id{color:var(--ui-text-primary);font-weight:600;overflow:hidden;text-overflow:ellipsis}
.duty-stage-row-kind{overflow:hidden;text-overflow:ellipsis}
.duty-stage-row-att{color:var(--ui-text-tertiary)}
.duty-stage-row-tog,.duty-att-row-tog{color:var(--ui-text-tertiary);font-size:10px}
.duty-att-item{display:flex;flex-direction:column;min-width:0}
.duty-att-row{display:flex;align-items:center;gap:6px;height:24px;margin-left:12px;padding:0 2px 0 0;font-size:12px;white-space:nowrap;overflow:hidden;cursor:pointer;color:var(--ui-text-secondary)}
.duty-att-row:hover{background:color-mix(in srgb, var(--ui-text-primary) 6%, transparent)}
.duty-att-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:-2px}
.duty-att-row-n{color:var(--ui-text-primary)}
.duty-att-row-tog{margin-left:auto}
.duty-att-ev{display:flex;align-items:baseline;gap:6px;margin-left:12px;padding:2px 2px 2px 0;font-size:11px;color:var(--ui-text-tertiary)}
.duty-att-ev-cmd{color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.duty-att-log{margin-left:12px;padding:2px 2px 2px 0;font-size:11px;color:var(--ui-text-tertiary)}
.duty-att-none{margin-left:12px;padding:2px 2px 2px 0;font-size:11px}
/* Ожидание человека — видимой строкой под этапом: дело может стоять на нём. */
.duty-stage-wait{margin-left:0;padding:2px 2px 4px 12px;font-size:11px;color:var(--ui-yellow);word-break:break-word}
.duty-stage-wait a{color:var(--ui-yellow);text-decoration:underline}
.duty-tabs{display:flex;gap:2px;margin-left:6px}
.duty-tab{height:22px;padding:0 9px;border-radius:999px;border:1px solid transparent;background:transparent;color:var(--ui-text-tertiary);font:inherit;font-size:11px;cursor:pointer;white-space:nowrap}
.duty-tab:hover{background:color-mix(in srgb, var(--ui-text-primary) 8%, transparent);color:var(--ui-text-primary)}
.duty-tab.on{color:var(--ui-accent);background:color-mix(in srgb, var(--ui-accent) 14%, transparent);border-color:color-mix(in srgb, var(--ui-accent) 40%, transparent)}
.duty-scroll{flex:1 1 auto;min-height:0;overflow:auto;padding:10px 16px 24px}
.duty-why-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
.duty-why-card{border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:10px 12px;min-width:0}
.duty-why-title{font-size:12px;font-weight:600;line-height:1.35;word-break:break-word}
.duty-why-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin:5px 0 0}
.duty-why-foot{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--ui-text-tertiary)}
.duty-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-bottom:14px}
.duty-kpi{border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:10px 12px}
.duty-kpi-k{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--ui-text-tertiary)}
.duty-kpi-v{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums;margin:4px 0 2px}
.duty-kpi-track{height:6px;border-radius:3px;background:var(--ui-bg-quaternary);overflow:hidden;margin-top:8px}
.duty-kpi-fill{height:6px;border-radius:3px;background:var(--ui-green)}
.duty-tax{display:flex;flex-direction:column;gap:5px}
.duty-tax-row{display:grid;grid-template-columns:170px 1fr 56px;align-items:center;gap:8px;font-size:11px}
.duty-tax-name{font-family:var(--ui-font-mono, ui-monospace, monospace);color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.duty-tax-track{height:7px;border-radius:4px;background:var(--ui-bg-quaternary);overflow:hidden}
.duty-tax-fill{height:7px;border-radius:4px}
.duty-health-table td{white-space:nowrap;text-overflow:ellipsis}
.duty-finished-table td{white-space:nowrap;text-overflow:ellipsis}
.duty-deal-head{display:flex;flex-direction:column;gap:6px;border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:10px 12px;margin-bottom:12px}
.duty-deal-title{font-size:13px;font-weight:600;word-break:break-word}
.duty-deal-facts{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--ui-text-secondary)}
.duty-deal-facts b{color:var(--ui-text-primary);font-weight:600}
.duty-deal-body{display:flex;flex-direction:column;gap:12px;min-width:0}
/* Две колонки карточки дела: документы (растут) и тонкий столбец этапов — ровно
   320 px, не растёт: в нём строки этапов и попыток, а не абзацы. */
.duty-deal-cols{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:14px;align-items:start;min-width:0}
/* Узкое окно: колонки друг под другом, документы — первыми, этапы — следом. */
@media (max-width:1100px){.duty-deal-cols{grid-template-columns:1fr}}
.duty-deal-spec,.duty-deal-docs,.duty-deal-stages{min-width:0}
.duty-deal-scale{grid-column:1/-1;min-width:0}
.duty-spec{margin:0;white-space:pre-wrap;word-break:break-word;font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11px;line-height:1.5;color:var(--ui-text-secondary);max-height:60vh;overflow:auto;border:1px solid var(--ui-stroke-tertiary);border-radius:6px;padding:8px}
/* Спецификация дела — markdown, а не дамп: pre-блок переносил строки так, что
   YAML разъезжался, а заголовки и списки читались как текст. Шрифт возвращаем к
   обычному, моно — только для кода. */
.duty-md{white-space:normal;font-family:var(--ui-font-default, inherit);font-size:12.5px;line-height:1.55;padding:10px 12px}
.duty-md h1,.duty-md h2,.duty-md h3,.duty-md h4{margin:12px 0 6px;line-height:1.3;font-weight:600;color:var(--ui-text-primary)}
.duty-md h1{font-size:15px}
.duty-md h2{font-size:13.5px;border-bottom:1px solid var(--ui-stroke-tertiary);padding-bottom:4px}
.duty-md h3{font-size:13px}
.duty-md h4{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-secondary)}
.duty-md p{margin:6px 0}
.duty-md ul,.duty-md ol{margin:6px 0;padding-left:20px;list-style:revert}
.duty-md li{margin:2px 0}
.duty-md code{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11px;background:var(--ui-bg-quaternary);border:1px solid var(--ui-stroke-tertiary);border-radius:4px;padding:1px 5px}
.duty-md pre{background:var(--ui-bg-quaternary);border:1px solid var(--ui-stroke-tertiary);border-radius:8px;padding:9px 11px;overflow:auto;margin:8px 0}
.duty-md pre code{background:none;border:none;padding:0;line-height:1.5}
.duty-md blockquote{margin:8px 0;padding:2px 0 2px 10px;border-left:2px solid var(--ui-stroke-tertiary);color:var(--ui-text-tertiary)}
.duty-md table{border-collapse:collapse;margin:8px 0;font-size:12px;display:block;overflow-x:auto}
.duty-md th,.duty-md td{border:1px solid var(--ui-stroke-tertiary);padding:5px 9px;text-align:left}
.duty-md th{background:var(--ui-bg-quaternary);font-weight:600;color:var(--ui-text-primary)}
.duty-md hr{border:none;border-top:1px solid var(--ui-stroke-tertiary);margin:12px 0}
.duty-md a{text-decoration:underline;text-underline-offset:2px}
.duty-scale-axis{position:relative;height:15px;margin:0 0 6px}
.duty-scale-tick{position:absolute;font-size:11px;color:var(--ui-text-tertiary);white-space:nowrap;font-variant-numeric:tabular-nums}
.duty-scale-tick.align-start{transform:translateX(0)}
.duty-scale-tick.align-center{transform:translateX(-50%)}
.duty-scale-tick.align-end{transform:translateX(-100%)}
/* ------------------------------------- таймлайн вызовов (0.0.8)
 * Свёрнутая попытка — 20 px, раскрытие по наведению/фокусу. Высота приходит
 * переменной --duty-tl-full со строки, переход делает CSS. */

.duty-scale-plot{position:relative}
/* Ось, сетка и слой сжатий начинаются там же, где дорожка строки, — после
 * колонки подписей. Без этого сдвига пунктир и подпись сжатия встают на 194 px
 * левее промежутка и меряют проценты по ширине плагина, а не дорожки. */
.duty-scale-grid{position:absolute;top:-2px;bottom:0;left:calc(var(--duty-tl-gutter, 186px) + var(--duty-tl-gap, 8px));right:0;pointer-events:none;z-index:0}
.duty-scale-grid-line{position:absolute;top:0;bottom:0;border-left:1px dashed color-mix(in srgb, var(--ui-text-tertiary) 34%, transparent)}
/* Сжатый промежуток оси: два пунктира по краям и подпись посередине — иначе
 * смена масштаба читается как ошибка данных. */
.duty-tl-idle-layer{position:absolute;top:-2px;left:calc(var(--duty-tl-gutter, 186px) + var(--duty-tl-gap, 8px));width:calc(100% - var(--duty-tl-gutter, 186px) - var(--duty-tl-gap, 8px));height:calc(100% + 2px);z-index:0;overflow:visible;fill:none;stroke:none}
.duty-tl-idle{stroke:var(--ui-border-primary, rgba(127,127,127,.3));stroke-dasharray:2 3;stroke-width:1}
.duty-tl-idle-label{fill:var(--ui-text-tertiary);font-size:9px;letter-spacing:.02em;pointer-events:none}
.duty-tl-lanes{position:relative;z-index:1;display:flex;flex-direction:column;gap:10px}
.duty-tl-lane{display:flex;flex-direction:column;gap:4px}
.duty-tl-lane + .duty-tl-lane{border-top:1px solid var(--ui-border-primary, rgba(127,127,127,.2));padding-top:10px}
.duty-tl-lane-h{display:flex;align-items:center;gap:8px;min-height:20px}
.duty-tl-lane-name{font-size:12.5px;font-weight:600;color:var(--ui-text-primary);letter-spacing:.01em}
.duty-tl-lane-meta{margin-left:auto;font-size:11px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums;white-space:nowrap}
.duty-tl-kind{display:inline-flex;align-items:center;gap:5px;height:19px;padding:0 7px;border-radius:5px;border:1px solid var(--ui-border-primary, rgba(127,127,127,.3));background:var(--ui-bg-quaternary);font-size:10.5px;font-weight:600;color:var(--ui-text-secondary)}
.duty-tl-kind-ico{width:12px;height:12px;fill:none;stroke:currentColor;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.duty-tl-kind-agent{color:var(--ui-text-primary);border-color:var(--ui-blue);background:color-mix(in srgb, var(--ui-blue) 12%, transparent)}
.duty-tl-kind-agent .duty-tl-kind-ico{stroke:var(--ui-blue)}
.duty-tl-kind-gate{color:var(--ui-yellow);border-color:var(--ui-yellow);background:color-mix(in srgb, var(--ui-yellow) 12%, transparent)}
.duty-tl-rows{display:flex;flex-direction:column;gap:4px}
.duty-tl-row{position:relative;display:grid;grid-template-columns:var(--duty-tl-gutter, 186px) 1fr;gap:var(--duty-tl-gap, 8px);align-items:stretch;border-radius:6px;height:var(--duty-tl-collapsed, 20px);transition:height .12s ease}
.duty-tl-row:hover,.duty-tl-row:focus-visible{height:var(--duty-tl-full, 20px)}
/* Глубокие уровни вызовов видны только в раскрытой строке: visibility, а не display —
 * чипы позиционированы абсолютно, разметка не прыгает, подсказки не обрезаются. */
.duty-tl-call[data-level]:not([data-level="0"]){visibility:hidden}
.duty-tl-row:hover .duty-tl-call[data-level],.duty-tl-row:focus-visible .duty-tl-call[data-level]{visibility:visible}
.duty-tl-row:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
/* Колонки подписи фиксированы: иначе статус разной ширины двигал время в каждой строке. */
.duty-tl-lbl{display:grid;grid-template-columns:26px 1fr auto;align-self:center;align-items:center;gap:6px;line-height:20px;white-space:nowrap;font-variant-numeric:tabular-nums}
.duty-tl-lbl>*{overflow:hidden;text-overflow:ellipsis}
.duty-tl-n{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:10px;color:var(--ui-text-tertiary);text-align:right}
.duty-tl-st{font-size:10.5px;font-weight:700;color:var(--ui-text-secondary)}
.duty-tl-st.t-green{color:var(--ui-green)}
.duty-tl-st.t-red{color:var(--ui-red)}
.duty-tl-st.t-yellow{color:var(--ui-yellow)}
.duty-tl-st.t-blue{color:var(--ui-blue)}
.duty-tl-ms{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:10px;color:var(--ui-text-tertiary);text-align:right}
/* Суммарное число вызовов попытки: факт, а не предупреждение — тон нейтральный.
 * Жёлтым помечается только усечённое потолком: у части вызовов чипа нет. */
.duty-tl-more{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:9.5px;font-weight:700;color:var(--ui-text-tertiary);padding:0 3px;border-radius:3px;background:color-mix(in srgb, var(--ui-text-tertiary) 14%, transparent)}
.duty-tl-more.is-cut{color:var(--ui-yellow);background:color-mix(in srgb, var(--ui-yellow) 16%, transparent)}
/* Вызов без результата: полый чип — длительности нет, а не ноль. */
.duty-tl-call.duty-tl-unfinished,.duty-tl-tick.duty-tl-unfinished{background:transparent !important;box-shadow:inset 0 0 0 1.5px currentColor !important;opacity:.9}
.duty-tl-track{position:relative;height:100%}
.duty-tl-track{position:relative;min-height:20px}
/* Фон попытки: длительность видна полосой, а не только по чипам вызовов. */
.duty-tl-band{position:absolute;top:1px;bottom:1px;border-radius:4px;background:color-mix(in srgb, var(--ui-text-tertiary) 13%, transparent);border:1px solid color-mix(in srgb, var(--ui-text-tertiary) 22%, transparent)}
/* Тон исхода: провал читается полосой на дорожке, а не только словом слева. */
.duty-tl-band.t-red{background:color-mix(in srgb, var(--ui-red) 15%, transparent);border-color:color-mix(in srgb, var(--ui-red) 45%, transparent)}
.duty-tl-band.t-yellow{background:color-mix(in srgb, var(--ui-yellow) 13%, transparent);border-color:color-mix(in srgb, var(--ui-yellow) 38%, transparent)}
.duty-tl-band.t-blue{background:color-mix(in srgb, var(--ui-blue) 12%, transparent);border-color:color-mix(in srgb, var(--ui-blue) 34%, transparent)}
/* Провалившаяся попытка: красная кромка в подписи и номер красным. */
.duty-tl-row.is-failed .duty-tl-lbl{border-left:2px solid var(--ui-red);padding-left:6px;margin-left:-8px}
.duty-tl-row.is-failed .duty-tl-n{color:var(--ui-red)}
/* Пауза «вопрос → ответ»: время, которое дело стояло на человеке. */
.duty-tl-wait{position:absolute;top:1px;bottom:1px;border-radius:3px;background:color-mix(in srgb, var(--ui-yellow) 26%, transparent);border-left:1.5px solid var(--ui-yellow);border-right:1.5px solid var(--ui-yellow)}
/* Маркеры событий — внутри строки той попытки, где событие произошло. */
/* Маркеры журнала — вертикальные палки на всю высоту строки: точка на нижней кромке
 * убегала при разворачивании, а палка стоит на своём времени всегда. */
.duty-tl-marks{position:absolute;inset:0;pointer-events:none}
.duty-tl-mark{position:absolute;top:-3px;bottom:-3px;width:3px;transform:translateX(-50%);pointer-events:auto;cursor:pointer}
.duty-tl-mark.anchor-end{transform:translateX(calc(-100% - 1px))}
.duty-tl-mark.anchor-start{transform:translateX(1px)}
.duty-tl-mark:hover{z-index:40}
/* Палка тонкая: попадание мышью расширяется невидимым «полем» вокруг. */
.duty-tl-mark::before{content:'';position:absolute;left:-4px;right:-4px;top:0;bottom:0}
.duty-tl-mark i{display:block;width:100%;height:100%;border-radius:2px;background:currentColor;opacity:.9}
.duty-tl-mark.t-green{color:var(--ui-green)}
.duty-tl-mark.t-red{color:var(--ui-red)}
.duty-tl-mark.t-yellow{color:var(--ui-yellow)}
.duty-tl-mark.t-blue{color:var(--ui-blue)}
.duty-tl-mark.t-gray{color:var(--ui-text-tertiary)}
.duty-tl-call{position:absolute;height:10px;border-radius:2px;cursor:pointer;box-shadow:inset 0 0 0 1px rgba(0,0,0,.22)}
.duty-tl-row:hover .duty-tl-call{box-shadow:inset 0 0 0 1px rgba(0,0,0,.45)}
/* Подсказка — общий слой для чипа, полосы попытки, подписи и маркера. */
.duty-tl-tip{position:fixed;left:0;top:0;z-index:9999;padding:3px 7px;border-radius:5px;white-space:normal;max-width:280px;width:max-content;text-align:left;background:var(--ui-surface-raised, var(--ui-surface-background));color:var(--ui-text-primary);border:1px solid var(--ui-stroke-tertiary);box-shadow:0 8px 20px rgba(0,0,0,.38);font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:10.5px;line-height:1.5;opacity:0;pointer-events:none;transition:opacity .09s ease}
.duty-tl-call,.duty-tl-band,.duty-tl-lbl,.duty-tl-mark,.duty-tl-wait{cursor:pointer}
.duty-tl-call.duty-tl-sel{box-shadow:inset 0 0 0 1.5px var(--ui-accent),0 0 0 2px color-mix(in srgb, var(--ui-accent) 35%, transparent)}
.duty-tl-insp{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:26px;margin-top:10px;padding:4px 9px;border-radius:6px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-surface-raised, var(--ui-surface-background));font-size:11.5px}
.duty-tl-insp.empty{color:var(--ui-text-tertiary);border-style:dashed;background:transparent}
.duty-tl-insp-dot{width:8px;height:8px;border-radius:2px;background:var(--ui-text-tertiary)}
.duty-kpi{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
.duty-kpi-cell{display:grid;grid-auto-flow:column;align-items:baseline;gap:6px;padding:5px 10px;border-radius:7px;border:1px solid var(--ui-stroke-tertiary);background:var(--ui-surface-raised, var(--ui-surface-background))}
.duty-kpi-v{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums}
.duty-kpi-l{font-size:10.5px;color:var(--ui-text-tertiary)}
.duty-kpi-n{font-size:10px;color:var(--ui-text-tertiary);opacity:.8}
/* Ось и сетка начинаются там же, где дорожки: колонка подписей — одна ширина. */
.duty-scale-head{display:grid;grid-template-columns:var(--duty-tl-gutter, 186px) 1fr;gap:var(--duty-tl-gap, 8px);align-items:end;margin-bottom:6px;--duty-tl-gutter:186px;--duty-tl-gap:8px}
.duty-scale-keys{display:flex;flex-direction:column;gap:1px;font-size:10px;color:var(--ui-text-tertiary);line-height:1.45}
.duty-scale-keys-h{font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;opacity:.7}
.duty-scale-key{display:grid;grid-template-columns:7px 1fr auto;align-items:center;gap:5px;white-space:nowrap;cursor:pointer;border-radius:4px;padding:1px 3px;margin:0 -3px}
.duty-scale-key:hover{background:color-mix(in srgb, var(--ui-text-tertiary) 12%, transparent)}
.duty-scale-key:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
/* Выбранное семейство: легенда говорит, что именно подсвечено на дорожках. */
.duty-scale-key.on{background:color-mix(in srgb, var(--ui-accent) 16%, transparent);color:var(--ui-text-primary)}
.duty-scale-key.on .duty-scale-key-n{opacity:1;font-weight:600}
/* Чужие семейства гаснут, а не исчезают: картина дела остаётся целой. */
.duty-tl-call.duty-tl-dim{opacity:.18;filter:saturate(.35);pointer-events:none}
.duty-tl-row:hover .duty-tl-call.duty-tl-dim{opacity:.3}
.duty-scale-key-l{overflow:hidden;text-overflow:ellipsis}
.duty-scale-key-n{font-family:var(--ui-font-mono, ui-monospace, monospace);font-variant-numeric:tabular-nums;opacity:.8}
.duty-scale-key-dot{width:7px;height:7px;border-radius:2px;background:var(--ui-text-tertiary)}
.duty-scale-key-dot.duty-t-read{background:var(--ui-blue)}
.duty-scale-key-dot.duty-t-search{background:var(--ui-purple)}
.duty-scale-key-dot.duty-t-write{background:var(--ui-green)}
.duty-scale-key-dot.duty-t-run{background:var(--ui-yellow)}
.duty-scale-key-dot.duty-t-net{background:var(--ui-red)}
/* Граф — полосой: мелкие узлы, а не главный объект экрана. Подпись стоит ПОД
   картинкой мелким шрифтом слева: сбоку она спорила с узлами. */
.duty-spec-strip{display:flex;flex-direction:column;align-items:flex-start;gap:4px;margin-top:4px}
.duty-deal-head-top{align-items:flex-start}
.duty-deal-head-side{display:flex;align-items:center;gap:8px;flex:none}
.duty-scale-wrap{display:grid;grid-template-columns:minmax(0, 1fr) auto;gap:14px;align-items:start}
.duty-scale-main{min-width:0}
.duty-scale-side{display:flex;flex-direction:column;gap:1px;min-width:104px;padding-left:10px;border-left:1px solid var(--ui-stroke-tertiary);font-size:10px;color:var(--ui-text-tertiary);line-height:1.45}
.duty-spec-strip-name{font-size:10px;color:var(--ui-text-tertiary);text-align:left;white-space:nowrap}
.duty-spec-graph{display:block;width:auto;height:auto;max-width:100%;overflow:visible;fill:none;stroke:none}
/* Ребро — плавная кривая со стрелкой: кривую и острие считает шейпер. */
.duty-spec-edge{fill:none;stroke:var(--ui-stroke-tertiary);stroke-width:1.1}
.duty-spec-arrow{fill:var(--ui-stroke-tertiary);stroke:none}
.duty-spec-node rect{fill:var(--ui-surface-raised, var(--ui-surface-background));stroke:var(--ui-stroke-tertiary);stroke-width:1.1;rx:6}
.duty-spec-node .duty-spec-id{fill:var(--ui-text-secondary);font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:9.5px}
.duty-spec-node .duty-spec-state{fill:var(--ui-text-tertiary);font-size:8.5px}
.duty-spec-node.t-green rect{stroke:var(--ui-green)}
.duty-spec-node.t-red rect{stroke:var(--ui-red)}
.duty-spec-node.t-yellow rect{stroke:var(--ui-yellow)}
.duty-spec-node.t-blue rect{stroke:var(--ui-accent)}
/* Живой этап дышит: пока попытка идёт, граф не должен выглядеть застывшим. */
.duty-spec-node.is-live rect{animation:duty-spec-breath 1.6s ease-in-out infinite}
.duty-spec-node.is-live .duty-spec-state{fill:var(--ui-accent)}
@keyframes duty-spec-breath{0%,100%{stroke-opacity:.45}50%{stroke-opacity:1}}
@media (prefers-reduced-motion: reduce){.duty-tl-row,.duty-tl-mini,.duty-tl-stack,.duty-tl-call{transition:none}.duty-spec-node.is-live rect{animation:none}}

.duty-scale-lanes{display:flex;flex-direction:column;gap:5px}
.duty-scale-lane{display:grid;grid-template-columns:92px 1fr;gap:8px;align-items:center}
.duty-scale-lane-k{font-family:var(--ui-font-mono, ui-monospace, monospace);font-size:11.5px;color:var(--ui-text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Дорожка этапа без попыток — пустая: точка-многоточие отличает её от этапа, где попытка просто шла вне окна. */
.duty-scale-lane-none{color:var(--ui-text-tertiary);font-style:normal}
.duty-scale-track{position:relative;height:20px;border-radius:5px;background:var(--ui-bg-quaternary);overflow:hidden}
.duty-scale-bar{position:absolute;top:3px;bottom:3px;display:flex;align-items:center;overflow:hidden;border-radius:4px;border:1px solid currentColor;background:color-mix(in srgb, currentColor 22%, transparent);color:var(--ui-text-tertiary)}
.duty-scale-bar:focus-visible{outline:2px solid var(--ui-accent);outline-offset:1px}
.duty-scale-bar.open{background:transparent;border-style:dashed}
.duty-scale-bar-k{font-size:10.5px;color:var(--ui-text-primary);padding:0 5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.duty-scale-markers{position:relative;height:42px;margin-top:10px;border-top:1px solid var(--ui-stroke-tertiary)}
.duty-scale-marker{position:absolute;display:flex;flex-direction:column;align-items:center;transform:translateX(-50%)}
.duty-scale-dot{display:grid;place-items:center;width:14px;height:14px;border-radius:50%;border:1px solid currentColor;background:var(--ui-surface-background);font-size:9.5px;line-height:1}
.duty-scale-marker.tick .duty-scale-dot{width:1px;height:12px;border:0;border-radius:0;background:currentColor}
.duty-scale-marker:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.duty-scale-wait{position:absolute;bottom:0;height:0;border-top:2px dashed currentColor;transform:translateY(-4px);color:var(--ui-yellow)}
.duty-scale-wait-k{position:absolute;left:4px;top:-14px;font-size:10.5px;white-space:nowrap}
.duty-scale-legend{display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;font-size:11.5px}
/* ------------------------- мини-таймлайн и листание завершённых (0.0.9)
 *
 * Геометрия приходит из шейпера: здесь только раскладка. Сетка — один слой под
 * всеми делами (--duty-mtl-step / --duty-mtl-first), строки своих клеток не
 * рисуют; сосиска — одно тело с небольшим скруглением, этапы внутри стыкуются
 * вплотную, и границей служит только цвет.
 */
.duty-mtl{margin:2px 0 14px;border:1px solid var(--ui-stroke-tertiary);border-radius:8px;background:color-mix(in srgb, var(--ui-text-primary) 2%, transparent);overflow:hidden}
.duty-mtl-h{display:flex;align-items:baseline;gap:10px;padding:8px 12px 6px;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-tertiary)}
.duty-mtl-omitted{margin-left:auto}
.duty-mtl-body{padding:0 12px 2px}
.duty-mtl-rows{position:relative;display:flex;flex-direction:column;--duty-mtl-cols:minmax(180px,26%) 1fr 74px 108px}
.duty-mtl-grid{position:absolute;inset:0;display:grid;grid-template-columns:var(--duty-mtl-cols);gap:12px;pointer-events:none}
.duty-mtl-grid-track{grid-column:2;background-image:repeating-linear-gradient(to right,color-mix(in srgb,var(--ui-text-tertiary) 30%,transparent) 0 1px,transparent 1px var(--duty-mtl-step,25%));background-position:var(--duty-mtl-first,0) 0;background-repeat:repeat-y;border-right:1px solid var(--ui-accent)}
.duty-mtl-row{display:grid;grid-template-columns:var(--duty-mtl-cols);gap:12px;align-items:center;padding:4px 0;font-size:12px}
.duty-mtl-lbl{display:flex;flex-direction:column;gap:1px;overflow:hidden}
.duty-mtl-repo{font-size:10px;letter-spacing:.04em;color:var(--ui-text-tertiary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.duty-mtl-title{font-size:12.5px;color:var(--ui-text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
.duty-mtl-title:hover{color:var(--ui-accent);text-decoration:underline;text-underline-offset:2px}
.duty-mtl-title:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px;border-radius:3px}
.duty-mtl-row.is-done .duty-mtl-title{color:var(--ui-text-secondary)}
.duty-mtl-track{position:relative;height:16px}
.duty-mtl-sausage{position:absolute;top:0;bottom:0;overflow:hidden;border-radius:3px;background:color-mix(in srgb,var(--ui-text-tertiary) 22%,transparent);cursor:pointer}
.duty-mtl-sausage:hover{filter:brightness(1.08)}
.duty-mtl-sausage:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.duty-mtl-sausage.is-clipped{border-top-left-radius:0;border-bottom-left-radius:0}
.duty-mtl-sausage.inexact{opacity:.45}
.duty-mtl-seg{position:absolute;top:0;bottom:0;min-width:2px;background:var(--seg,var(--ui-stroke-secondary))}
.duty-mtl-seg.t-green{--seg:var(--ui-green)}
.duty-mtl-seg.t-blue{--seg:var(--ui-blue)}
.duty-mtl-seg.t-yellow{--seg:var(--ui-yellow)}
.duty-mtl-seg.t-red{--seg:var(--ui-red)}
.duty-mtl-seg.t-gray{--seg:var(--ui-stroke-secondary)}
.duty-mtl-seg.is-running{animation:duty-mtl-breathe 2.6s ease-in-out infinite}
.duty-mtl-seg.clipped{border-left:1px solid var(--ui-surface-background)}
@keyframes duty-mtl-breathe{0%,100%{filter:brightness(1)}50%{filter:brightness(1.3)}}
.duty-mtl-age{color:var(--ui-text-secondary);text-align:right;font-size:11.5px}
.duty-mtl-state{color:var(--ui-text-tertiary);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11.5px}
.duty-mtl-axis{display:grid;grid-template-columns:var(--duty-mtl-cols);gap:12px;height:15px;margin-top:4px}
.duty-mtl-axis-track{position:relative;height:15px}
.duty-mtl-tick{position:absolute;transform:translateX(-50%);font-size:10px;font-variant-numeric:tabular-nums;color:var(--ui-text-tertiary);white-space:nowrap}
.duty-mtl-legend{display:flex;flex-wrap:wrap;gap:4px 14px;padding:8px 12px 10px;border-top:1px solid var(--ui-stroke-tertiary);font-size:11.5px}
.duty-mtl-legend-item{display:inline-flex;align-items:center;gap:6px;color:var(--ui-text-secondary)}
.duty-mtl-dot{width:9px;height:9px;border-radius:2px;background:var(--ui-stroke-secondary)}
.duty-mtl-dot.t-green{background:var(--ui-green)}
.duty-mtl-dot.t-blue{background:var(--ui-blue)}
.duty-mtl-dot.t-yellow{background:var(--ui-yellow)}
.duty-mtl-dot.t-red{background:var(--ui-red)}
.duty-mtl-dot.t-gray{background:var(--ui-stroke-secondary)}
.duty-mtl-legend-item.is-running{color:var(--ui-text-primary)}
@media (prefers-reduced-motion: reduce){.duty-mtl-seg.is-running{animation:none}}

/* Кнопка подгрузки под списком проекта: по центру и заметная — единственный
 * способ получить следующие строки, искать её в углу не надо. */
.duty-finished-block{margin:18px 0 0;border-top:1px solid var(--ui-stroke-tertiary);padding-top:10px}
.duty-finished-head{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--ui-text-tertiary)}
.duty-more-row{display:flex;justify-content:center;padding:10px 0 14px}
.duty-more-btn{font-family:inherit;font-size:12px;padding:5px 20px;border-radius:999px;cursor:pointer;color:var(--ui-text-primary);border:1px solid color-mix(in srgb,var(--ui-accent) 45%,var(--ui-stroke-secondary));background:color-mix(in srgb,var(--ui-accent) 10%,transparent)}
.duty-more-btn:hover:not([disabled]){border-color:var(--ui-accent);background:color-mix(in srgb,var(--ui-accent) 18%,transparent)}
.duty-more-btn[disabled]{cursor:default}
.duty-more-btn.is-done{border-color:transparent;background:transparent;color:var(--ui-text-tertiary)}
.duty-skel-tr td{padding:6px}
.duty-skel-tr .duty-skel{height:9px;border-radius:4px}
.duty-row-in{animation:duty-row-in .24s ease both}
@keyframes duty-row-in{from{opacity:0;transform:translateY(-2px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion: reduce){.duty-row-in{animation:none}}

`

/* ---------------- данные ---------------- */

/** Текст ошибки из чего угодно, что бросил ctx.rest. */
function errText(error) {
  if (!error) return 'неизвестная ошибка'
  const detail = error.detail ?? error.payload?.detail ?? error.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail) return JSON.stringify(detail)
  return String(error.message || error)
}

/** Экран «сейчас»: первый запрос сразу, дальше раз в 10 с; старые данные не затираются. */
function useNow() {
  const [snap, setSnap] = useState({ data: null, error: null, loading: true, updatedAt: 0 })
  const inflight = useRef(false)
  const alive = useRef(true)

  const load = useCallback(async () => {
    if (inflight.current) return
    inflight.current = true
    try {
      const data = await rest('/now', { timeoutMs: REQUEST_TIMEOUT_MS })
      if (!alive.current) return
      setSnap({ data, error: null, loading: false, updatedAt: Date.now() })
    } catch (error) {
      if (!alive.current) return
      setSnap((prev) => ({ ...prev, error: errText(error), loading: false }))
    } finally {
      inflight.current = false
    }
  }, [])

  useEffect(() => {
    alive.current = true
    void load()
    const timer = setInterval(() => void load(), REFRESH_MS)
    return () => {
      alive.current = false
      clearInterval(timer)
    }
  }, [load])

  return { ...snap, refresh: load }
}

/** Таймлайн попыток выбранного дела. Смена выбора отменяет старый ответ (по ключу). */
function useAttempts(dealId) {
  const [state, setState] = useState({ key: '', data: null, error: null, loading: false })
  const key = dealId ? String(dealId) : ''

  const load = useCallback(async () => {
    if (!dealId) return
    const mine = String(dealId)
    setState((prev) => ({ key: mine, data: prev.key === mine ? prev.data : null, error: null, loading: true }))
    try {
      const data = await rest(
        `/duty/${encodeURIComponent(dealId)}/attempts`,
        { timeoutMs: REQUEST_TIMEOUT_MS },
      )
      setState((prev) => (prev.key === mine ? { key: mine, data, error: null, loading: false } : prev))
    } catch (error) {
      setState((prev) => (prev.key === mine ? { ...prev, error: errText(error), loading: false } : prev))
    }
  }, [dealId])

  useEffect(() => {
    void load()
  }, [load])

  return { ...state, reload: load }
}

/* ---------------- данные: «дело» и «здоровье» ---------------- */

/** Экран «Здоровье»: агрегат по ранам. Запрашивается только когда экран открыт. */
function useStats(enabled) {
  const [snap, setSnap] = useState({ data: null, error: null, loading: false })
  const alive = useRef(true)

  const load = useCallback(async () => {
    setSnap((prev) => ({ ...prev, loading: true }))
    try {
      const data = await rest('/stats', { timeoutMs: REQUEST_TIMEOUT_MS })
      if (alive.current) setSnap({ data, error: null, loading: false })
    } catch (error) {
      // Прошлый снимок не затираем: экран не мигает пустотой при обрыве.
      if (alive.current) setSnap((prev) => ({ data: prev.data, error: errText(error), loading: false }))
    }
  }, [])

  useEffect(() => {
    alive.current = true
    if (!enabled) return () => { alive.current = false }
    void load()
    const timer = setInterval(() => void load(), STATS_REFRESH_MS)
    return () => {
      alive.current = false
      clearInterval(timer)
    }
  }, [enabled, load])

  return { ...snap, reload: load }
}

/* ---------------- данные: «завершённые» (0.0.7) ---------------- */

/**
 * Экран «Завершённые»: индекс проектов и своя страница у каждого.
 *
 * `GET /deals?limit=1` называет, сколько закрытых дел у каждого репозитория
 * (`repos[].total`) — из него строится индекс. Дальше каждая группа листается
 * сама (`?repo=…&limit=5&cursor=…`): своя пятёрка и свой курсор, поэтому «ещё»
 * под одним списком не сдвигает соседние и не повторяет строки. Курсор — ключ
 * `finished_at:id` с бэкенда, а не смещение.
 *
 * Запрашивается только когда экран открыт: это чтение всей истории закрытых дел,
 * и держать его в памяти «Сейчас» незачем. Прошлый снимок не затирается ошибкой —
 * экран не мигает пустотой при обрыве.
 */
const FINISHED_PER_REPO = 5
// Сколько скелетоны держатся как минимум: ответ с локального бэкенда приходит
// быстрее глаза, и без этого «разворачивание» не увидеть — строки просто прыгали.
const SKELETON_MIN_MS = 260
// История закрытых дел читается ОДНИМ запросом, а по проектам режется в памяти:
// у старого бэкенда (без `repos`) постраничный список по репозиториям не собрать,
// да и группировка по `repo` — чистая функция. 200 строк с запасом покрывают
// реальный store; если он больше, честно пишем, что список усечён.
const FINISHED_FETCH_LIMIT = 200
// Сколько раз пробуем список закрытых дел: первый запрос может совпасть с
// подъёмом бэкенда, и пустой ответ нельзя показывать как «завершённых дел нет».
const DEALS_ATTEMPTS = 3

/** Ответ `/deals` со своим путём в тексте ошибки: путь — половина диагноза. */
async function dealsPayload(path) {
  try {
    return await rest(path, { timeoutMs: REQUEST_TIMEOUT_MS })
  } catch (err) {
    throw new Error(`${path} → ${errText(err)}`)
  }
}

function useFinishedPages(enabled) {
  const [snapshot, setSnapshot] = useState(null)
  // Сколько строк проекта показано: листание в памяти, без сети.
  const [shown, setShown] = useState({})
  const [error, setError] = useState(null)
  const alive = useRef(true)

  const load = useCallback(async () => {
    setError(null)
    let lastError = ''
    const startedAt = Date.now()
    // Ответ без `deals` — не «завершённых нет», а «ответа ещё нет»: три попытки
    // и честная причина в баннере вместо пустого «0 из 0» (09.11).
    for (let attempt = 1; attempt <= DEALS_ATTEMPTS; attempt += 1) {
      try {
        const raw = await dealsPayload(`/deals?limit=${FINISHED_FETCH_LIMIT}`)
        if (!raw || typeof raw !== 'object' || !Array.isArray(raw.deals)) {
          const keys = raw && typeof raw === 'object' ? Object.keys(raw).join(', ') || 'пусто' : typeof raw
          throw new Error(`ответ /deals без списка дел (ключи: ${keys})`)
        }
        const page = shapeFinished(raw)
        // Store не прочитался (частый случай — писатель держит базу): это не факт
        // «завершённых дел нет», а неудачное чтение. Пробуем ещё раз.
        if (!page.deals.length && page.errors.length) {
          throw new Error(`store: ${page.errors[0].reason}`)
        }
        // Скелетоны успевают показаться: локальный ответ быстрее глаза.
        const wait = SKELETON_MIN_MS - (Date.now() - startedAt)
        if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait))
        if (!alive.current) return
        setSnapshot({
          repos: page.groups,
          total: page.total || page.deals.length,
          truncated: Boolean(raw.truncated),
          errors: page.errors,
          note: page.note,
        })
        setShown({})
        return
      } catch (err) {
        lastError = errText(err)
        if (attempt < DEALS_ATTEMPTS) await new Promise((resolve) => setTimeout(resolve, 400 * attempt))
      }
    }
    if (alive.current) setError(lastError)
  }, [])

  useEffect(() => {
    alive.current = true
    if (!enabled) return () => { alive.current = false }
    void load()
    return () => { alive.current = false }
  }, [enabled, load])

  const reveal = useCallback((repo) => {
    setShown((prev) => ({ ...prev, [repo]: (prev[repo] ?? FINISHED_PER_REPO) + FINISHED_PER_REPO }))
  }, [])

  const revealAll = useCallback((repos) => {
    setShown((prev) => {
      const next = { ...prev }
      for (const group of repos) {
        next[group.repo] = (prev[group.repo] ?? FINISHED_PER_REPO) + FINISHED_PER_REPO
      }
      return next
    })
  }, [])

  return { snapshot, shown, error, reload: load, reveal, revealAll }
}

/**
 * Границы этапов для дорожек таймлайна: один запрос на все показанные дела.
 *
 * Ошибка не глушит таймлайн — дорожки рисуются одним тоном состояния, без
 * разбивки на этапы.
 */
function useSpans(ids) {
  const key = useMemo(
    () => [...new Set((ids || []).filter(Boolean))].sort().join(','),
    [ids],
  )
  const [map, setMap] = useState({})
  const alive = useRef(true)
  const known = useRef({})

  useEffect(() => {
    alive.current = true
    if (!key) return () => { alive.current = false }
    const wanted = key.split(',')
    if (wanted.every((id) => known.current[id] !== undefined)) return () => { alive.current = false }
    void (async () => {
      try {
        const payload = await rest(`/spans?ids=${encodeURIComponent(key)}`, { timeoutMs: REQUEST_TIMEOUT_MS })
        const spans = payload && typeof payload.spans === 'object' && payload.spans ? payload.spans : {}
        if (!alive.current) return
        known.current = { ...known.current, ...spans }
        setMap((prev) => ({ ...prev, ...spans }))
      } catch {
        // Молча: разбивка на этапы — украшение, а не факт, на котором стоит экран.
      }
    })()
    return () => { alive.current = false }
  }, [key])

  return map
}

/** Экран «Дело»: спецификация + тот же факт-набор, что в `/now`. */
function useDeal(dealId) {
  const [state, setState] = useState({ key: '', data: null, error: null, loading: false })
  const key = dealId ? String(dealId) : ''

  const load = useCallback(async () => {
    if (!key) return
    setState((prev) => ({ key, data: prev.key === key ? prev.data : null, error: null, loading: true }))
    try {
      const data = await rest(`/deal/${encodeURIComponent(dealId)}`, {
        timeoutMs: REQUEST_TIMEOUT_MS,
      })
      setState((prev) => (prev.key === key ? { key, data, error: null, loading: false } : prev))
    } catch (error) {
      setState((prev) => (prev.key === key ? { ...prev, error: errText(error), loading: false } : prev))
    }
  }, [dealId, key])

  useEffect(() => {
    void load()
    const timer = setInterval(() => void load(), REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  return { ...state, reload: load }
}

/* ---------------- отрисовка ---------------- */

function StateBadge({ label, tone, title }) {
  return h(
    'span',
    { className: 'duty-badge', style: { color: TONE_COLOR[tone] || TONE_COLOR.gray }, title },
    h('i', null),
    label,
  )
}

function DealRow({ deal, selected, onPick }) {
  const attemptTitle = deal.attemptAge === null
    ? `попытки нет (${deal.attemptNote})`
    : `попытка ${deal.attemptAgeText} назад, ${deal.attemptNote}`
  const leaseTitle = deal.leaseExpiresIn === null
    ? `лизинг: ${deal.leaseState}`
    : `лизинг: ${deal.leaseState}, ${deal.leaseExpiresIn > 0 ? 'ещё' : 'истёк'} ${fmtAge(Math.abs(deal.leaseExpiresIn))}`
  return h(
    'tr',
    {
      className: selected ? 'duty-row sel' : 'duty-row',
      onClick: () => onPick(deal),
      title: [deal.repoLabel, deal.evidence].filter(Boolean).join(' · '),
    },
    h('td', { className: 'duty-mono', title: deal.id }, deal.id),
    h('td', { title: deal.titleFull }, deal.title),
    h('td', { title: deal.assigneeNote || 'исполнитель' }, deal.assignee),
    h(
      'td',
      {
        className: `duty-muted duty-mono${deal.stageLive ? ' duty-stage-now' : ''}`,
        title: deal.stageText || 'этап не начат',
      },
      deal.stageText || '—',
    ),
    h('td', { className: 'duty-num', title: attemptTitle }, deal.attemptAgeText),
    h('td', { title: leaseTitle }, deal.leaseText),
    h('td', { className: 'duty-num', title: `${deal.lastActionKind}: ${deal.lastActionSummary || '—'}` }, deal.lastActionAgeText),
    h('td', null, h(StateBadge, { label: deal.stateLabel, tone: deal.tone, title: deal.evidence })),
  )
}

/**
 * Группа списка — репозиторий (0.0.6): имя, число дел и состояния группы одной
 * строкой. Столбца «репозиторий» в строках нет — он повторял бы заголовок.
 */
function DealGroup({ group, selectedKey, onPick }) {
  return h(
    'section',
    { className: 'duty-group' },
    h(
      'div',
      { className: 'duty-group-h' },
      h('span', { className: group.repo ? 'duty-mono' : 'duty-mono duty-muted', title: group.repo || 'репозиторий не назван' }, group.label),
      h('span', { className: 'duty-num' }, `${group.count}`),
      h('span', { className: 'duty-spacer' }),
      h('span', { className: 'duty-muted' }, groupStatesText(group)),
    ),
    h(
      'table',
      { className: 'duty-table' },
      h(
        'colgroup',
        null,
        h('col', { style: { width: '9%' } }),
        h('col', { style: { width: '28%' } }),
        h('col', { style: { width: '12%' } }),
        h('col', { style: { width: '10%' } }),
        h('col', { style: { width: '7%' } }),
        h('col', { style: { width: '6%' } }),
        h('col', { style: { width: '8%' } }),
        h('col', { style: { width: '12%' } }),
      ),
      h(
        'thead',
        null,
        h(
          'tr',
          null,
          h('th', null, 'дело'),
          h('th', null, 'заголовок'),
          h('th', null, 'исполнитель'),
          h('th', null, 'этап'),
          h('th', null, 'попытка'),
          h('th', null, 'лизинг'),
          h('th', null, 'действие'),
          h('th', null, 'состояние'),
        ),
      ),
      h(
        'tbody',
        null,
        ...group.deals.map((deal) =>
          h(DealRow, {
            key: deal.id,
            deal,
            selected: selectedKey === deal.id,
            onPick,
          }),
        ),
      ),
    ),
  )
}

/* ---------------- столбец этапов и попыток дела (0.0.8) ---------------- */

/**
 * Попытки этапа для столбца.
 *
 * Факты процесса (`live`/`dead`, лизинг, код выхода) знает только лента `/deal`,
 * журнал попытки — только ручка `/duty/{id}/attempts`. Своих номеров и исходов
 * экран не считает: попытка ленты ищется в ответе ручки по `runId` (оба зовут
 * одну попытку одним именем), и ей подмешивается один факт — `events`.
 */
function stageColumnBars(stage, timeline) {
  const rows = Array.isArray(stage.attempts) ? stage.attempts : []
  const extra = timeline && Array.isArray(timeline.attempts) ? timeline.attempts : []
  if (!rows.length) return extra.filter((attempt) => attempt.stage === stage.id)
  return rows.map((row) => {
    if (row.runId === null || row.runId === undefined) return row
    const found = extra.find((attempt) => String(attempt.runId) === String(row.runId))
    return found ? { ...row, events: found.events } : row
  })
}

/**
 * Строка попытки: `#n`, длительность, исход словами, глиф раскрытия.
 *
 * Раскрытая показывает доказательства, потом `summary` и `error`, потом журнал —
 * сводкой по видам (`5 прогрессов · 1 старт · 1 конец`), а не списком событий:
 * у одной попытки их бывает триста, и в столбце 320 px это лог, а не строка.
 */
function CompactAttemptRow({ stage, attempt, open, onToggle }) {
  const view = compactAttemptView(stage, attempt)
  const children = [
    h(
      'div',
      {
        key: 'row',
        className: open ? 'duty-att-row open' : 'duty-att-row',
        title: view.outcomeText,
        'aria-expanded': open ? 'true' : 'false',
        onClick: onToggle,
      },
      h('span', { className: 'duty-mono duty-att-row-n' }, view.label),
      h('span', { className: 'duty-num duty-att-row-dur' }, view.durationText),
      h(StateBadge, { label: view.outcomeText, tone: view.tone, title: view.outcomeText }),
      h('span', { className: 'duty-spacer' }),
      h('span', { className: 'duty-att-row-tog' }, open ? '▾' : '▸'),
    ),
  ]
  if (!open) return h('div', { className: 'duty-att-item' }, ...children)
  view.evidenceRows.forEach((row, i) => {
    children.push(
      h(
        'div',
        { key: `ev-${i}`, className: 'duty-att-ev' },
        // Полная команда — в подсказке: обрезка сделана в шейпере, а не CSS-эллипсисом.
        h(StateBadge, { label: `exit ${row.exitText}`, tone: row.tone, title: row.cmdTitle }),
        h('span', { className: 'duty-mono duty-att-ev-cmd', title: row.cmdTitle }, row.cmdShort),
        h('span', { className: 'duty-muted' }, row.clock),
      ),
    )
  })
  if (view.summary) children.push(h('div', { key: 'summary', className: 'duty-summary' }, view.summary))
  if (view.error) children.push(h('div', { key: 'error', className: 'duty-error' }, view.error))
  if (view.eventsText) children.push(h('div', { key: 'log', className: 'duty-att-log duty-muted' }, view.eventsText))
  return h('div', { className: 'duty-att-item' }, ...children)
}

/**
 * Столбец этапов и попыток в карточке дела.
 *
 * Одна панель вместо двух (широкой ленты этапов и отдельной панели попыток):
 * шапка попытки повторялась в обеих, и страница росла на три с половиной экрана.
 * Раскрытие строк — состояние экрана (`useState`): шейпер отдаёт только умолчание
 * (`openByDefault` — провал и живая попытка раскрыты без клика).
 * Пустой граф печатает причину (`note` бэкенда: нет спеки, спека не читается).
 */
function CompactStages({ feed, timeline }) {
  const [openStages, setOpenStages] = useState({})
  const [openAtts, setOpenAtts] = useState({})
  const head = h('div', { className: 'duty-group-h' }, h('span', null, feed.empty ? 'этапы' : `этапы · ${feed.count}`))
  if (feed.empty) {
    return h('div', { className: 'duty-deal-stages' }, head, h('div', { className: 'duty-muted' }, feed.note || 'этапов нет'))
  }
  const blocks = feed.stages.map((stage) => {
    const view = compactStageView(stage)
    const open = openStages[view.id] === undefined ? view.openByDefault : openStages[view.id]
    const bars = stageColumnBars(stage, timeline)
    const children = [
      h(
        'div',
        {
          key: 'row',
          className: open ? 'duty-stage-row open' : 'duty-stage-row',
          // Служебное (`needs`) — в подсказке строки: отдельной строкой оно
          // съедало высоту, которую занимают попытки.
          title: view.needsText || view.toggleTitle,
          'aria-expanded': open ? 'true' : 'false',
          onClick: () => setOpenStages((prev) => ({ ...prev, [view.id]: !open })),
        },
        h('span', { className: 'duty-mono duty-stage-row-id' }, view.id),
        h('span', { className: 'duty-muted duty-stage-row-kind' }, view.kindText),
        h('span', { className: 'duty-num duty-stage-row-att' }, view.attemptsText),
        h('span', { className: 'duty-spacer' }),
        h(StateBadge, { label: view.stateLabel, tone: view.tone, title: view.stateLabel }),
        h('span', { className: 'duty-stage-row-tog' }, open ? '▾' : '▸'),
      ),
    ]
    // Ожидание человека остаётся видимой строкой: дело может стоять на нём.
    if (view.waitText) {
      children.push(
        h(
          'div',
          { key: 'wait', className: 'duty-stage-wait' },
          view.waitHref ? h('a', { href: view.waitHref }, view.waitText) : view.waitText,
        ),
      )
    }
    if (!bars.length && open) children.push(h('div', { key: 'none', className: 'duty-muted duty-att-none' }, 'попыток нет'))
    if (open) {
      bars.forEach((attempt, index) => {
        const key = `${stage.id}#${index}`
        const attOpen = openAtts[key] === undefined ? compactAttemptView(stage, attempt).openByDefault : openAtts[key]
        children.push(
          h(CompactAttemptRow, {
            key: `att-${key}`,
            stage,
            attempt,
            open: attOpen,
            onToggle: () => setOpenAtts((prev) => ({ ...prev, [key]: !attOpen })),
          }),
        )
      })
    }
    return h('div', { className: 'duty-stage-block', key: `st-${stage.id}` }, ...children)
  })
  return h(
    'div',
    { className: 'duty-deal-stages' },
    head,
    // Путь к спеке — именем файла, полный путь в подсказке.
    feed.spec ? h('div', { className: 'duty-mono duty-muted', title: feed.spec }, fileLabel(feed.spec)) : null,
    ...blocks,
  )
}


// Неполнота ответа /now одной строкой — общая для жёлтой полосы над списком и
// для пустого состояния, иначе «досок 19/19» врёт про полноту среза. Собирает её
// модель (`noticeLines`), потому что правило «устаревший снимок замечаний не
// имеет» проверяется тестом, а не глазами при ревью рендера.

/* ---------------- шкала времени дела (E2) ---------------- */

// Глиф маркера — только значок: слово и время приходят из шейпера (`label`,
// `title`), чтобы подпись не разъезжалась между десктопом и вебом.
const MARKER_GLYPH = {
  question: '?',
  answer: '+',
  verdict: 'v',
  budget: '!',
  published: '↑',
  closed: '■',
  started: '',
  ended: '',
}

/** Строка фактов шапки дела: те же числа, что в вебе (`shapeDealTime`). */
function DealFactSpans({ scale }) {
  const facts = scale.facts
  return h(
    'div',
    { className: 'duty-deal-facts' },
    // Попытки и длительность стоят в KPI: второй раз они читаются как другой факт.
    h('span', { title: 'вопросы человеку по таймлайну' }, 'вопросы: ', h('b', null, facts.questions.text)),
    facts.budget
      ? h(
          'span',
          { title: facts.budget.exhausted ? `потолок исчерпан: ${facts.budget.exhausted}` : 'потолок дела целиком' },
          'бюджет: ',
          h('b', null, facts.budget.text || '—'),
        )
      : null,
    facts.published
      ? h(
          'span',
          { title: facts.published.mrUrl || facts.published.ref || 'факт журнала' },
          'публикация: ',
          h('b', { className: 'duty-mono' }, facts.published.text || '—'),
        )
      : null,
    facts.closed ? h('span', { title: facts.closed.cause || '' }, 'закрытие: ', h('b', null, facts.closed.text)) : null,
  )
}

/** Инспектор вызова: инструмент, начало, длительность, место в деле. */
function CallInspector({ call }) {
  if (!call) {
    return h('div', { className: 'duty-tl-insp empty' }, 'выбери вызов на дорожке — покажу инструмент, начало и длительность')
  }
  return h(
    'div',
    { className: 'duty-tl-insp' },
    h('span', { className: 'duty-tl-insp-dot', style: { background: TONE_COLOR[call.tone] || TONE_COLOR.gray } }),
    h('b', null, call.name),
    h('span', { className: 'duty-mono' }, call.clock),
    h('span', null, call.msText),
    h('span', { className: 'duty-muted' }, `${call.stage} · попытка ${call.attempt}${call.seq === null ? '' : ` · вызов ${call.seq}`}`),
  )
}

/** KPI-строка дела: пять чисел шапки, все — из шейпера (`scale.kpi`). */
function KpiStrip({ scale }) {
  if (!scale.kpi || !scale.kpi.length) return null
  return h(
    'div',
    { className: 'duty-kpi' },
    ...scale.kpi.map((item) =>
      h(
        'div',
        { className: 'duty-kpi-cell', key: `kpi-${item.key}`, title: item.title || '' },
        h('span', { className: 'duty-kpi-v' }, item.value),
        h('span', { className: 'duty-kpi-l' }, item.label),
        item.note ? h('span', { className: 'duty-kpi-n' }, item.note) : null,
      ),
    ),
  )
}

/**
 * Граф спеки компактной полосой: узлы и рёбра `needs` со стрелками.
 *
 * Схема зависимостей — не главный объект экрана. Координаты, кривую ребра, острие
 * стрелки и признаки узла (тон, `live`) считает шейпер; здесь только DOM. Подпись
 * «граф спеки» стоит под картинкой мелким шрифтом.
 */
function SpecStrip({ spec }) {
  if (!spec) return null
  return h(
    'div',
    { className: 'duty-spec-strip' },
    h(
      'svg',
      {
        className: 'duty-spec-graph',
        viewBox: `0 0 ${spec.width} ${spec.height}`,
        width: spec.width,
        height: spec.height,
        role: 'img',
        'aria-label': `граф этапов: ${spec.nodes.map((node) => node.id).join(' → ')}`,
      },
      ...spec.edges.map((edge) =>
        h(
          'g',
          { key: `e-${edge.from}-${edge.to}` },
          h('path', { className: 'duty-spec-edge', d: edge.d }),
          h('polygon', { className: 'duty-spec-arrow', points: edge.arrow.points }),
        ),
      ),
      ...spec.nodes.map((node) =>
        h(
          'g',
          { key: `n-${node.id}`, className: `duty-spec-node t-${node.tone}${node.live ? ' is-live' : ''}` },
          h('title', null, node.title),
          h('rect', { x: node.x, y: node.y, width: spec.nodeW, height: spec.nodeH, rx: 6 }),
          h(
            'text',
            {
              x: node.x + spec.nodeW / 2,
              y: node.y + 12,
              className: 'duty-spec-id',
              textAnchor: 'middle',
            },
            node.id,
          ),
          h(
            'text',
            {
              x: node.x + spec.nodeW / 2,
              y: node.y + 23,
              className: 'duty-spec-state',
              textAnchor: 'middle',
            },
            node.label,
          ),
        ),
      ),
    ),
    h('span', { className: 'duty-spec-strip-name' }, 'граф спеки'),
  )
}

/**
 * Выбранный вызов по ключу `stage:attempt:seq`.
 *
 * Ищется тем же видом, которым рисуются чипы (`attemptView`): второго описания
 * вызова у инспектора нет.
 */
function callOf(scale, callId) {
  if (!callId) return null
  for (const bar of scale.bars) {
    for (const chip of attemptView(bar, scale).chips) {
      if (chip.id === callId) return chip
    }
  }
  return null
}

/** Панель шкалы времени: подписанная ось, дорожка на этап, полосы и маркеры. */
function ScalePanel({ scale, callSel, onSelectCall, toolFilter, onToggleTool }) {
  const active = Array.isArray(toolFilter) ? toolFilter : []
  const head = h(
    'div',
    { className: 'duty-group-h' },
    h('span', null, 'шкала времени'),
    h('span', { className: 'duty-mono' }, scale.axis.text),
  )
  if (scale.empty) {
    return h('div', { className: 'duty-deal-scale' }, head, h('div', { className: 'duty-muted' }, 'событий и попыток нет: рисовать нечего'))
  }
  const kinds = []
  for (const marker of scale.markers) {
    if (marker.tick || kinds.some((item) => item.type === marker.type)) continue
    kinds.push(marker)
  }
  // Шкала: дорожки слева, легенда семейств — правой колонкой, как в макете
  // `d-merge`. Ось стоит ровно над дорожками: слева пустая колонка шириной
  // подписей, справа — та же область, где рисуются чипы.
  return h(
    'div',
    { className: 'duty-deal-scale' },
    head,
    h(
      'div',
      { className: 'duty-scale-wrap' },
      h(
        'div',
        { className: 'duty-scale-main' },
        h(
          'div',
          { className: 'duty-scale-head' },
          h('div', { className: 'duty-scale-head-gap' }),
          h(
            'div',
            { className: 'duty-scale-axis' },
            ...scale.axis.ticks.map((tick) =>
              h(
                'span',
                { className: `duty-scale-tick align-${tick.align}`, style: { left: `${tick.pct}%` }, title: tick.clock },
                tick.edge ? `${tick.edge} ${tick.clock}` : tick.clock,
              ),
            ),
          ),
        ),
        // Дорожки этапов — таймлайн вызовов: свёрнутая полоса, карта вызовов по
        // наведению. Сетка по меткам оси одна на все дорожки: время читается
        // поперёк, а не угадывается по трём подписям (макет `d-merge`).
        h(
          'div',
          { className: 'duty-scale-plot' },
          h(
            'div',
            { className: 'duty-scale-grid', 'aria-hidden': 'true' },
            ...scale.axis.ticks.map((tick) =>
              h('span', { key: `grid-${tick.pct}`, className: 'duty-scale-grid-line', style: { left: `${tick.pct}%` } }),
            ),
          ),
          // Сжатые промежутки оси: пара пунктиров по краям и подпись посередине
          // (`idle.gaps` считает шейпер — поверхность ничего не досчитывает).
          scale.idle.gaps.length
            ? h(
                'svg',
                { key: 'idle-layer', className: 'duty-tl-idle-layer' },
                ...scale.idle.gaps.flatMap((gap) => [
                  h('line', {
                    key: `idle-l-${gap.from}`,
                    className: 'duty-tl-idle',
                    x1: `${gap.leftPct}%`,
                    y1: '0',
                    x2: `${gap.leftPct}%`,
                    y2: '100%',
                  }),
                  h('line', {
                    key: `idle-r-${gap.from}`,
                    className: 'duty-tl-idle',
                    x1: `${gap.rightPct}%`,
                    y1: '0',
                    x2: `${gap.rightPct}%`,
                    y2: '100%',
                  }),
                  h(
                    'text',
                    {
                      key: `idle-t-${gap.from}`,
                      className: 'duty-tl-idle-label',
                      x: `${gap.labelPct}%`,
                      y: '50%',
                      textAnchor: gap.labelAnchor,
                      dominantBaseline: 'middle',
                    },
                    h('title', null, gap.title),
                    gap.text,
                  ),
                ]),
              )
            : null,
          h(
            'div',
            { className: 'duty-tl-lanes' },
            ...scale.lanes.map((lane, index) =>
              h(DutyLane, {
                key: `lane-${lane.key}`,
                lane,
                bars: scale.bars.filter((bar) => bar.lane === index),
                scale,
                callSel,
                onSelectCall,
                toolFilter: active,
              }),
            ),
          ),
        ),
      ),
      // Легенда семейств инструментов: числа вызовов приходят из шейпера
      // (`scale.tools`), поверхность их только печатает.
      h(
        'div',
        { className: 'duty-scale-side' },
        h('span', { className: 'duty-scale-keys-h' }, 'инструменты'),
        ...scale.tools.map((item) =>
          h(
            'span',
            {
              className: `duty-scale-key${active.includes(item.tone) ? ' on' : ''}`,
              key: `key-${item.tone}`,
              role: 'button',
              tabIndex: 0,
              'aria-pressed': active.includes(item.tone) ? 'true' : 'false',
              'aria-label': `${item.label}: ${item.count}. Клик — подсветить вызовы этого семейства на дорожках`,
              onClick: () => onToggleTool && onToggleTool(item.tone),
              onKeyDown: (event) => {
                if (!onToggleTool || (event.key !== 'Enter' && event.key !== ' ')) return
                event.preventDefault()
                onToggleTool(item.tone)
              },
            },
            h('span', { className: `duty-scale-key-dot duty-t-${item.tone}` }),
            h('span', { className: 'duty-scale-key-l' }, item.label),
            h('span', { className: 'duty-scale-key-n' }, String(item.count)),
          ),
        ),
      ),
    ),
    h(CallInspector, { call: callOf(scale, callSel) }),
    kinds.length
      ? h(
          'div',
          { className: 'duty-scale-legend' },
          ...kinds.map((marker) =>
            h(
              'span',
              { key: `lg-${marker.type}`, style: { color: TONE_COLOR[marker.tone] || TONE_COLOR.gray } },
              `${MARKER_GLYPH[marker.type] || '•'} ${marker.label}`,
            ),
          ),
        )
      : null,
    scale.facts.attempts.journal ? h('div', { className: 'duty-muted' }, 'полосы собраны из журнала: ответ попыток пуст') : null,
  )
}

/* ---------------- экран «почему стоит» ---------------- */

/**
 * Карточка одного дела: состояние и доказательство словами бэкенда.
 *
 * Ничего не досчитывается: `waiting.evidence` — это факты, которые сработали в
 * бэкенде (лизинг, pid, heartbeat, граф зависимостей, пул вопросов). Экран
 * показывает их целиком, чтобы «почему стоит» можно было проверить, а не
 * поверить.
 */
function WhyCard({ deal, onOpen }) {
  return h(
    'article',
    { className: 'duty-why-card' },
    h('div', { className: 'duty-why-title' }, deal.titleFull || deal.title),
    h(
      'div',
      { className: 'duty-why-meta' },
      h(StateBadge, { label: deal.stateLabel, tone: deal.tone, title: deal.state }),
      h('span', { className: 'duty-mono' }, `${deal.repoLabel} · ${deal.id}`),
      h('span', { className: 'duty-muted' }, deal.assignee),
    ),
    h('div', { className: 'duty-evidence', style: { margin: '6px 0 0' } }, deal.evidence || 'доказательство не записано'),
    h(
      'div',
      { className: 'duty-why-foot' },
      h('span', null, `попытка: ${deal.attemptAgeText} (${deal.attemptNote})`),
      h('span', null, `действие: ${deal.lastActionAgeText} назад`),
      h(Button, { size: 'xs', variant: 'secondary', onClick: () => onOpen(deal) }, 'открыть дело'),
    ),
  )
}

/**
 * Группа «Почему стоит»: состояния по-прежнему группируют объяснения, поэтому
 * заголовок секции — подпись состояния; репозиторий в карточке назван рядом с id.
 */
function WhyScreen({ model, stale, onOpen }) {
  const lines = noticeLines(model, stale)
  return h(
    'div',
    { className: 'duty-scroll' },
    lines.length
      ? h('div', { className: 'duty-note', style: { margin: '0 0 10px' } }, ...lines.map((line, i) => h('div', { key: `n-${i}` }, line)))
      : null,
    h(
      'div',
      { className: 'duty-muted', style: { margin: '0 2px 10px' } },
      'группа — состояние ожидания, текст в рамке — доказательство из бэкенда; страница ничего не досчитывает',
    ),
    !model.count
      ? h('div', { className: 'duty-empty' }, h('b', null, 'нечего объяснять: активных дел нет'))
      : h(
          'div',
          null,
          ...model.stateGroups.map((group) =>
            h(
              'section',
              { key: `why-${group.state}`, style: { marginBottom: '16px' } },
              h(
                'div',
                { className: 'duty-group-h' },
                h(StateBadge, { label: group.label, tone: group.tone, title: group.state }),
                h('span', { className: 'duty-num' }, `${group.count}`),
                h('span', { className: 'duty-mono' }, group.state),
              ),
              h(
                'div',
                { className: 'duty-why-grid' },
                ...group.deals.map((deal) => h(WhyCard, { key: deal.id, deal, onOpen })),
              ),
            ),
          ),
        ),
  )
}

/* ---------------- экран «здоровье» ---------------- */

function Kpi({ title, value, sub, barPct }) {
  return h(
    'div',
    { className: 'duty-kpi' },
    h('div', { className: 'duty-kpi-k' }, title),
    h('div', { className: 'duty-kpi-v' }, value),
    h('div', { className: 'duty-muted' }, sub),
    barPct === null || barPct === undefined
      ? null
      : h('div', { className: 'duty-kpi-track' }, h('div', { className: 'duty-kpi-fill', style: { width: `${barPct}%` } })),
  )
}

/**
 * Таксономия исходов: доля каждого исхода среди всех ранов.
 *
 * Наборы успеха и падения приходят в ответе (`success_outcomes`/
 * `crash_outcomes`) — страница не решает сама, что считать падением.
 */
function Taxonomy({ stats }) {
  const entries = Object.entries(stats.totals.attempts_by_outcome || {}).sort((a, b) => b[1] - a[1])
  const crashes = new Set(stats.crash_outcomes || [])
  const success = new Set(stats.success_outcomes || [])
  if (!entries.length) return h('div', { className: 'duty-muted' }, 'попыток нет: таксономия считается по журналу дел')
  const max = entries[0][1] || 1
  return h(
    'div',
    { className: 'duty-tax' },
    ...entries.map(([key, count]) => {
      const tone = crashes.has(key) ? TONE_COLOR.red : success.has(key) ? TONE_COLOR.green : TONE_COLOR.gray
      return h(
        'div',
        { className: 'duty-tax-row', key: `tax-${key}` },
        h('span', { className: 'duty-tax-name', title: key }, key),
        h('div', { className: 'duty-tax-track' }, h('div', { className: 'duty-tax-fill', style: { width: `${Math.max(1, Math.round((count / max) * 100))}%`, background: tone } })),
        h('span', { className: 'duty-num', style: { textAlign: 'right' } }, `${count}`),
      )
    }),
  )
}

/**
 * Таблица «по репозиториям»: те же группы и числа, что печатает `bin/duty stats`.
 * Источник — `repos[]` и `without_repo` ответа `/stats`: доски в статистике не
 * участвуют (0.0.6), «без репозитория» — законная строка.
 */
function RepoHealthTable({ stats }) {
  const groups = [...(stats.repos || []), ...(stats.without_repo && stats.without_repo.deals ? [stats.without_repo] : [])]
  if (!groups.length) return h('div', { className: 'duty-muted' }, 'дел в store нет: считать нечего')
  return h(
    'table',
    { className: 'duty-table duty-health-table' },
    h(
      'thead',
      null,
      h(
        'tr',
        null,
        h('th', null, 'репозиторий'),
        h('th', null, 'дел'),
        h('th', null, 'попыток'),
        h('th', null, 'с первой попытки'),
        h('th', null, 'ожидание p50 / p90'),
        h('th', null, 'ожиданий'),
      ),
    ),
    h(
      'tbody',
      null,
      ...groups.map((group) => {
        const share = group.first_try.share
        return h(
          'tr',
          { key: `rh-${group.repo || 'without'}` },
          h('td', { className: 'duty-mono' }, group.repo || WITHOUT_REPO_LABEL),
          h('td', { className: 'duty-num' }, `${group.deals}`),
          h('td', { className: 'duty-num' }, `${group.attempts_total}`),
          h('td', { className: 'duty-num' }, `${group.first_try.ok}/${group.first_try.closed} · ${share === null ? '—' : `${Math.round(share * 100)}%`}`),
          h('td', { className: 'duty-num' }, group.waiting_sec.p50 === null ? '—' : `${fmtAge(group.waiting_sec.p50)} / ${fmtAge(group.waiting_sec.p90)}`),
          h('td', { className: 'duty-num' }, group.waiting_sec.count ? `${group.waiting_sec.count}` : '—'),
        )
      }),
    ),
  )
}

function HealthScreen({ stats }) {
  if (stats.error && !stats.data) {
    return h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-banner', style: { margin: 0 } }, h('span', null, stats.error), h(Button, { size: 'xs', variant: 'secondary', onClick: () => void stats.reload() }, 'повторить')))
  }
  if (!stats.data) {
    return h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 5 }, (_, i) => h('div', { className: 'duty-skel', key: `hs-${i}`, style: { width: `${92 - i * 10}%` } }))))
  }
  const data = stats.data
  const totals = data.totals
  const repos = data.repos || []
  const without = data.without_repo || null
  const crashes = new Set(data.crash_outcomes || [])
  const crashRuns = Object.entries(totals.attempts_by_outcome || {}).reduce((acc, [key, count]) => (crashes.has(key) ? acc + count : acc), 0)
  const share = totals.first_try.share
  return h(
    'div',
    { className: 'duty-scroll' },
    stats.error ? h('div', { className: 'duty-banner', style: { margin: '0 0 10px' } }, h('span', null, `снимок не обновлён: ${stats.error}`), h(Button, { size: 'xs', variant: 'secondary', onClick: () => void stats.reload() }, 'повторить')) : null,
    data.errors.length
      ? h('div', { className: 'duty-note', style: { margin: '0 0 10px' } }, ...data.errors.map((err, i) => h('div', { key: `he-${i}` }, `источник ${err.source} недоступен: ${err.reason}`)))
      : null,
    data.warnings.length
      ? h('div', { className: 'duty-note', style: { margin: '0 0 10px' } }, ...data.warnings.map((text, i) => h('div', { key: `hw-${i}` }, text)))
      : null,
    // Окно — часть числа: «за сутки» и «за всё время» — разные метрики.
    h(
      'div',
      { className: 'duty-muted', style: { margin: '0 0 10px' } },
      `окно: ${(data.window || {}).source === 'default' ? 'сутки по умолчанию' : 'задано вручную'} (${fmtAge((data.window || {}).sec)}) · числа считаются по журналу дел; карточки kanban — отдельное наблюдение`,
    ),
    h(
      'div',
      { className: 'duty-kpis' },
      h(Kpi, { title: 'дела с первой попытки', value: share === null ? '—' : `${Math.round(share * 100)}%`, sub: `${totals.first_try.ok} из ${totals.first_try.closed} этапов, закрытых попытками`, barPct: share === null ? null : Math.round(share * 100) }),
      h(Kpi, { title: 'ожидание до первой попытки: p50 / p90', value: totals.waiting_sec.p50 === null ? '—' : `${fmtAge(totals.waiting_sec.p50)} / ${fmtAge(totals.waiting_sec.p90)}`, sub: totals.waiting_sec.count ? `p99: ${fmtAge(totals.waiting_sec.p99)} · максимум: ${fmtAge(totals.waiting_sec.max)} · выборка: ${totals.waiting_sec.count} дел (created_at → started_at)` : 'выборки нет: у дел среза нет ни одного рана (created_at → started_at)' }),
      h(Kpi, { title: 'попыток всего', value: `${totals.attempts_total}`, sub: `падений: ${crashRuns}${totals.attempts_total ? ` · ${Math.round((crashRuns / totals.attempts_total) * 100)}%` : ''}` }),
      h(Kpi, { title: 'репозиториев', value: `${repos.length}`, sub: `дел в store: ${totals.deals} · без репозитория: ${without ? without.deals : 0}` }),
    ),
    h('div', { className: 'duty-group-h' }, h('span', null, 'таксономия исходов попыток'), h('span', { className: 'duty-mono' }, 'по журналу дел')),
    h(Taxonomy, { stats: data }),
    h('div', { className: 'duty-group-h', style: { marginTop: '14px' } }, h('span', null, 'репозитории')),
    h(RepoHealthTable, { stats: data }),
    h(
      'div',
      { className: 'duty-muted', style: { marginTop: '10px' } },
      'успех и падение — наборы исходов из ответа бэкенда; «с первой попытки» — этап журнала закрыт первой же попыткой и успешно; p50/p90/p99 — метод ближайшего ранга по объединённой выборке дел репозитория (сортировка одна, интерполяции нет); доски в статистике не участвуют',
    ),
  )
}

/* ---------------- экран «завершённые» (0.0.7) ---------------- */

/**
 * Список закрытых дел: причина, этапы, попытки, вопросы, длительность, публикация.
 *
 * Числа — поля ответа `/deals` (`shapeFinished`, шейпер общий с вебом): ни
 * страница, ни шейпер их не пересчитывают, иначе строка списка спорила бы с
 * карточкой того же дела (`bin/duty deal <id>`).
 */
const FINISHED_COLUMNS = ['8%', '9%', '23%', '8%', '6%', '6%', '8%', '13%', '10%', '9%']

function FinishedTable({ rows, pending, onPick }) {
  return h(
    'table',
    { className: 'duty-table duty-finished-table' },
    // Колонки заданы явно: у каждого репозитория своя таблица, и без ширин
    // браузер считает их по содержимому — колонки групп разъезжались (09.11).
    h('colgroup', null, ...FINISHED_COLUMNS.map((width, i) => h('col', { key: `col-${i}`, style: { width } }))),
    h(
      'thead',
      null,
      h(
        'tr',
        null,
        h('th', null, 'закрыто'),
        h('th', null, 'дело'),
        h('th', null, 'заголовок'),
        h('th', null, 'статус'),
        h('th', null, 'попыток'),
        h('th', null, 'вопросов'),
        h('th', null, 'длительность'),
        h('th', null, 'причина'),
        h('th', null, 'этапы'),
        h('th', null, 'публикация'),
      ),
    ),
    h(
      'tbody',
      null,
      ...rows.map((deal) =>
        h(
          'tr',
          {
            key: deal.id,
            className: 'duty-row duty-row-in',
            title: [deal.repoLabel, deal.causeText].filter(Boolean).join(' · '),
            onClick: () => onPick(deal),
          },
          h('td', { className: 'duty-mono' }, deal.finishedText),
          h('td', { className: 'duty-mono', title: deal.id }, deal.id),
          h('td', { title: deal.titleFull }, deal.title),
          h('td', null, h(StateBadge, { label: deal.statusLabel, tone: deal.tone, title: deal.status })),
          h('td', { className: 'duty-num', title: 'попыток в журнале' }, `${deal.attempts}`),
          h('td', { className: 'duty-num', title: 'вопросы человеку по журналу' }, deal.questions.text),
          h('td', { className: 'duty-num', title: 'длительность дела: события журнала и попытки' }, deal.durationText),
          h('td', { title: deal.causeText }, deal.causeText),
          h('td', { className: 'duty-muted', title: deal.stagesText }, deal.stagesText),
          h(
            'td',
            {
              className: 'duty-muted',
              title: deal.published ? deal.published.mrUrl || deal.published.ref || 'факт журнала' : 'публикации нет',
            },
            deal.published ? deal.published.text : '—',
          ),
        ),
      ),
      // Скелетоны встают на место будущих строк: список не мигает и не прыгает,
      // а показывает, где окажутся данные.
      ...Array.from({ length: pending }, (_, i) =>
        h(
          'tr',
          { key: `skel-${i}`, className: 'duty-skel-tr' },
          h('td', { colSpan: FINISHED_COLUMNS.length }, h('div', { className: 'duty-skel' })),
        ),
      ),
    ),
  )
}

/**
 * Блок «завершённые» на экране «Сейчас»: по пятёрке на проект, свой курсор у
 * каждой группы, кнопка «ещё» под списком.
 *
 * Сворачивания нет: блок всегда раскрыт (09.11, Макс) — по пятёрке на проект он
 * занимает пол-экрана и не мешает списку активных дел. Общая кнопка поднимает по
 * пятёрке сразу во всех проектах, своя — только в одном.
 */
function FinishedBlock({ finished, onPick }) {
  const { snapshot, shown } = finished
  if (finished.error && !snapshot) {
    return h(
      'div',
      { className: 'duty-panel duty-finished-block', style: { margin: '10px 0 0' } },
      h(
        'div',
        { className: 'duty-banner', style: { margin: 0 } },
        h('span', null, finished.error),
        h(Button, { size: 'xs', variant: 'secondary', onClick: () => void finished.reload() }, 'повторить'),
      ),
    )
  }
  if (!snapshot) {
    return h(
      'div',
      { className: 'duty-panel duty-finished-block' },
      h('div', { className: 'duty-group-h' }, h('span', null, 'завершённые')),
      h(
        'div',
        { className: 'duty-skel-row' },
        ...Array.from({ length: 4 }, (_, i) => h('div', { className: 'duty-skel', key: `fs-${i}`, style: { width: `${88 - i * 9}%` } })),
      ),
    )
  }
  const groups = snapshot.repos || []
  const visible = (group) => Math.min(group.deals.length, shown[group.repo] ?? FINISHED_PER_REPO)
  const shownRows = groups.reduce((sum, group) => sum + visible(group), 0)
  const anyPending = groups.some((group) => visible(group) < group.deals.length)
  return h(
    'div',
    { className: 'duty-panel duty-finished-block' },
    h(
      'div',
      { className: 'duty-group-h duty-finished-head' },
      h('span', null, 'завершённые'),
      h('span', { className: 'duty-num' }, `${shownRows} из ${snapshot.total}`),
      h('span', { className: 'duty-spacer' }),
      h('span', { className: 'duty-muted' }, `по ${FINISHED_PER_REPO} на проект`),
    ),
    finished.error
      ? h(
          'div',
          { className: 'duty-banner', style: { margin: '0 0 8px' } },
          h('span', null, `завершённые не обновились: ${finished.error}`),
          h(Button, { size: 'xs', variant: 'secondary', onClick: () => void finished.reload() }, 'повторить'),
        )
      : null,
    snapshot.errors.length
      ? h(
          'div',
          { className: 'duty-note', style: { margin: '0 0 8px' } },
          ...snapshot.errors.map((err, i) => h('div', { key: `fe-${i}` }, `источник ${err.source} недоступен: ${err.reason}`)),
        )
      : null,
    // Список усечён — это факт о выборке, а не о store: пишем его прямо в шапке
    // блока, чтобы счётчики проектов не выглядели полными.
    snapshot.truncated
      ? h(
          'div',
          { className: 'duty-note', style: { margin: '0 0 8px' } },
          `в store больше ${FINISHED_FETCH_LIMIT} закрытых дел — показаны последние`,
        )
      : null,
    groups.length
      ? h(
          'div',
          null,
          ...groups.map((group) => {
            const rows = group.deals.slice(0, visible(group))
            const left = group.deals.length - rows.length
            const done = left === 0
            const more =
              done
                ? 'это все закрытые дела'
                : `ещё ${Math.min(FINISHED_PER_REPO, left)}${left > FINISHED_PER_REPO ? ` · осталось ${left}` : ''}`
            return h(
              'section',
              { className: 'duty-group', key: `fd-${group.repo || 'without'}` },
              h(
                'div',
                { className: 'duty-group-h' },
                h(
                  'span',
                  { className: group.repo ? 'duty-mono' : 'duty-mono duty-muted', title: group.repo || 'репозиторий не назван' },
                  group.label,
                ),
                h('span', { className: 'duty-num' }, `${rows.length} из ${group.count}`),
                h('span', { className: 'duty-spacer' }),
              ),
              h(FinishedTable, { rows, pending: 0, onPick }),
              // Кнопка стоит по центру под списком — там, где появятся новые
              // строки, и выделена акцентом: это единственный способ их получить.
              h(
                'div',
                { className: 'duty-more-row' },
                h(
                  'button',
                  {
                    type: 'button',
                    className: done ? 'duty-more-btn is-done' : 'duty-more-btn',
                    disabled: done,
                    onClick: () => finished.reveal(group.repo),
                  },
                  more,
                ),
              ),
            )
          }),
          // Общая кнопка — по пятёрке во всех проектах сразу.
          h(
            'div',
            { className: 'duty-more-row duty-more-all' },
            h(
              'button',
              {
                type: 'button',
                className: anyPending ? 'duty-more-btn' : 'duty-more-btn is-done',
                disabled: !anyPending,
                onClick: () => finished.revealAll(groups),
              },
              anyPending ? `показать ещё по ${FINISHED_PER_REPO}` : 'всё загружено',
            ),
          ),
        )
      : h(
          'div',
          { className: 'duty-empty' },
          h('b', null, snapshot.errors.length ? 'завершённые не прочитались' : 'завершённых дел нет'),
          // Причина пустоты — словами бэкенда (`note`/`errors`), а не своя догадка.
          h(
            'span',
            { className: 'duty-muted' },
            snapshot.errors.length
              ? `store: ${snapshot.errors[0].reason}`
              : snapshot.note || 'в store нет ни одного закрытого дела',
          ),
        ),
  )
}

/* ---------------- мини-таймлайн «Сейчас» ---------------- */

/**
 * Мини-таймлайн дел: дорожка на дело, полосы этапов внутри.
 *
 * Геометрия целиком в шейпере (`dealTimeline`) — здесь только раскладка, чтобы
 * веб и десктоп не считали оси по-своему. Сверху шкала с делениями, под ней
 * дорожки: сетка и правый край «сейчас» принадлежат полотну, а не каждой строке,
 * иначе картинка рассыпается на набор серых полосок. Цвет полосы — состояние
 * этапа (`stageMeta`), пустое место — пауза между попытками.
 *
 * Кликабельны дело и его название, а не вся строка: длительность и состояние —
 * показания, а не ссылки (09.11, Макс).
 */
function DutyMiniTimeline({ view, onOpen }) {
  if (!view) return null
  if (view.empty) {
    return h(
      'div',
      { className: 'duty-mtl' },
      h('div', { className: 'duty-mtl-h' }, h('span', null, 'таймлайн дел')),
      h('div', { className: 'duty-muted', style: { padding: '0 12px 10px' } }, 'за сутки дел не было'),
    )
  }
  const ticks = view.ticks || []
  const cols = {
    '--duty-mtl-step': `${view.grid.stepPct}%`,
    '--duty-mtl-first': `${view.grid.firstPct}%`,
  }
  const open = (row) => () => onOpen(row)
  return h(
    'div',
    { className: 'duty-mtl' },
    h(
      'div',
      { className: 'duty-mtl-h' },
      h('span', null, 'таймлайн дел'),
      view.windowText ? h('span', { className: 'duty-muted' }, view.windowText) : null,
      view.omitted ? h('span', { className: 'duty-muted duty-mtl-omitted' }, `ещё ${view.omitted} не показано`) : null,
    ),
    h(
      'div',
      { className: 'duty-mtl-body' },
      h(
        'div',
        { className: 'duty-mtl-rows', style: cols },
        // Сетка — один слой под всеми делами: вертикали общие, строки своих
        // клеток не рисуют.
        h(
          'div',
          { className: 'duty-mtl-grid', 'aria-hidden': 'true' },
          h('span', { className: 'duty-mtl-gap' }),
          h('span', { className: 'duty-mtl-grid-track' }),
        ),
        ...view.rows.map((row) =>
          h(
            'div',
            { className: row.open ? 'duty-mtl-row' : 'duty-mtl-row is-done', key: `mtl-${row.key}` },
            h(
              'span',
              { className: 'duty-mtl-lbl' },
              h('span', { className: 'duty-mtl-repo' }, row.repo),
              h(
                'span',
                {
                  className: 'duty-mtl-title',
                  role: 'link',
                  tabIndex: 0,
                  title: row.hint,
                  onClick: open(row),
                  onKeyDown: (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      open(row)()
                    }
                  },
                },
                row.title || row.id,
              ),
            ),
            h(
              'span',
              { className: 'duty-mtl-track' },
              h(
                'span',
                {
                  className: `duty-mtl-sausage${row.exact ? '' : ' inexact'}${row.clipped ? ' is-clipped' : ''}`,
                  style: { left: `${row.bar.leftPct}%`, width: `${row.bar.widthPct}%` },
                  role: 'link',
                  tabIndex: 0,
                  title: row.hint,
                  'aria-label': `открыть дело ${row.id}`,
                  onClick: open(row),
                  onKeyDown: (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      open(row)()
                    }
                  },
                },
                (row.segments || []).map((segment) =>
                  h('i', {
                    key: `seg-${segment.stage}-${segment.leftPct}`,
                    className: `duty-mtl-seg t-${segment.tone} is-${segment.state}${segment.clipped ? ' clipped' : ''}`,
                    style: { left: `${segment.leftPct}%`, width: `${segment.widthPct}%` },
                    title: segment.title,
                  }),
                ),
              ),
            ),
            h('span', { className: 'duty-mtl-age duty-num' }, row.durationText),
            h('span', { className: 'duty-mtl-state' }, row.stateLabel),
          ),
        ),
      ),
      // Шкала — в тех же колонках, что дорожки: подпись стоит под своим делением.
      h(
        'div',
        { className: 'duty-mtl-axis' },
        h('span', { className: 'duty-mtl-gap' }),
        h(
          'span',
          { className: 'duty-mtl-axis-track' },
          ...ticks.map((tick) => h('span', { className: 'duty-mtl-tick', key: `tick-${tick.at}`, style: { left: `${tick.leftPct}%` } }, tick.label)),
        ),
      ),
    ),
    view.legend && view.legend.length
      ? h(
          'div',
          { className: 'duty-mtl-legend' },
          ...view.legend.map((item) =>
            h(
              'span',
              { className: item.state === 'running' ? 'duty-mtl-legend-item is-running' : 'duty-mtl-legend-item', key: `lg-${item.state}` },
              h('i', { className: `duty-mtl-dot t-${item.tone}` }),
              item.label,
              h('span', { className: 'duty-muted' }, item.durationText),
            ),
          ),
        )
      : null,
  )
}

/* ---------------- экран «дело» ---------------- */

function DealScreen({ sel, onBack }) {
  const detail = useDeal(sel.id)
  const query = useAttempts(sel.id)
  const deal = useMemo(() => (detail.data && detail.data.deal ? shapeDeal(detail.data.deal) : null), [detail.data])
  const timeline = useMemo(() => (query.data ? shapeAttempts(query.data) : null), [query.data])
  // Шкала времени — тот же шейпер, что в вебе (`shapeDealTime`): ось, дорожки,
  // полосы и маркеры считаются в одном месте, второго расчёта геометрии нет.
  const scale = useMemo(() => shapeDealTime(detail.data, query.data), [detail.data, query.data])
  // Лента этапов приходит тем же ответом `/deal` (`stages`), что и голова дела:
  // одна ручка — один кадр, этапы не могут отставать от фактов дела.
  const feed = useMemo(() => shapeStages(detail.data), [detail.data])
  // Выбранный вызов живёт на экране дела: клик по чипу открывает инспектор, но
  // строку попытки не разворачивает — развёртка осталась под указателем.
  const [callSel, setCallSel] = useState('')
  const selectCall = useCallback((id) => {
    setCallSel((prev) => (prev === id ? '' : id))
  }, [])
  // Подсветка семейства инструментов: набор тонов из легенды. Клик по семейству
  // гасит остальные вызовы, повторный — снимает подсветку.
  const [toolFilter, setToolFilter] = useState([])
  const toggleTool = useCallback((tone) => {
    setToolFilter((prev) => (prev.includes(tone) ? prev.filter((item) => item !== tone) : [...prev, tone]))
  }, [])

  const head = h(
    'div',
    { className: 'duty-deal-head' },
    // Заголовок: слева имя дела и граф этапов (что прошло и где падало), справа
    // в углу — статус и возврат к списку. Граф раньше висел отдельной панелью
    // внизу и занимал место, ничего не сообщая.
    h(
      'div',
      { className: 'duty-panel-h duty-deal-head-top' },
      h(
        'div',
        { style: { flex: '1 1 auto', minWidth: 0 } },
        h(
          'div',
          // На подробной странице путь стоит целиком: списки сокращают его до
          // хвоста, а тут человек пришёл смотреть одно дело — адрес репозитория
          // и есть факт, а не шум (09.11).
          { className: 'duty-mono', title: repoLabel((detail.data && detail.data.repo) || sel.repo) },
          `${repoLabel((detail.data && detail.data.repo) || sel.repo)} · ${sel.id}`,
        ),
        h('div', { className: 'duty-deal-title' }, (detail.data && detail.data.title) || sel.titleFull || '—'),
        detail.data ? h(SpecStrip, { spec: scale.spec }) : null,
      ),
      h(
        'div',
        { className: 'duty-deal-head-side' },
        deal
          ? h(StateBadge, { label: deal.stateLabel, tone: deal.tone, title: deal.state })
          : detail.data
            // Закрытое дело: статус словами (`завершено`/`в архиве`), значение
            // store — в подсказке.
            ? h(StateBadge, { label: finishedStatusMeta(detail.data.status).label, tone: 'gray', title: detail.data.status })
            : null,
        h(Button, { size: 'xs', variant: 'secondary', onClick: onBack }, '← к списку'),
      ),
    ),
    deal ? h('div', { className: 'duty-evidence' }, deal.evidence || 'доказательство не записано') : null,
    detail.data ? h(DealFactSpans, { scale }) : null,
    detail.data
      ? h(
          'div',
          { className: 'duty-deal-facts' },
          h('span', null, 'статус: ', h('b', null, detail.data.status || '—')),
          h('span', null, 'исполнитель: ', h('b', null, detail.data.assignee || '—')),
          deal ? h('span', null, 'попытка: ', h('b', null, `${deal.attemptAgeText} (${deal.attemptNote})`)) : null,
          deal ? h('span', null, 'лизинг: ', h('b', null, deal.leaseText)) : null,
          deal ? h('span', null, 'действие: ', h('b', null, `${deal.lastActionAgeText} назад`)) : null,
        )
      : null,
  )

  let body
  if (detail.error && !detail.data) {
    body = h('div', { className: 'duty-banner', style: { margin: 0 } }, h('span', null, detail.error), h(Button, { size: 'xs', variant: 'secondary', onClick: () => void detail.reload() }, 'повторить'))
  } else if (!detail.data) {
    body = h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 4 }, (_, i) => h('div', { className: 'duty-skel', key: `ds-${i}`, style: { width: `${90 - i * 12}%` } })))
  } else {
    const docs = docBlocks(detail.data)
    // Документы дела: тело из store и спека — разные документы, подменять один
    // другим нельзя (у дел из `bin/duty create` описания в store нет вовсе).
    // Нет документов — нет колонки: столбец этапов остаётся один на всю ширину.
    const stages = h(CompactStages, { feed, timeline })
    body = h(
      'div',
      { className: 'duty-deal-body' },
      // Шкала — первой и во всю ширину: это главный факт дела, а спецификация и
      // столбец этапов — пояснение к нему.
      h(KpiStrip, { scale }),
      h(ScalePanel, { scale, callSel, onSelectCall: selectCall, toolFilter, onToggleTool: toggleTool }),
      docs.length
        ? h(
            'div',
            { className: 'duty-deal-cols' },
            h(
              'div',
              { className: 'duty-deal-docs' },
              ...docs.map((doc) =>
                h(
                  'div',
                  { className: 'duty-deal-spec', key: `doc-${doc.kind}` },
                  h(
                    'div',
                    { className: 'duty-group-h', title: doc.path || '' },
                    h('span', null, doc.name ? `${doc.label} · ${doc.name}` : doc.label),
                    doc.truncated ? h('span', { className: 'duty-mono' }, 'обрезана лимитом') : null,
                  ),
                  doc.kind === 'spec'
                    ? h('pre', { className: 'duty-spec duty-doc-src duty-mono' }, doc.text)
                    : h('div', { className: 'duty-spec duty-md' }, h(Streamdown, null, doc.text)),
                ),
              ),
            ),
            stages,
          )
        : stages,
    )
  }

  return h('div', { className: 'duty-scroll' }, head, body)
}

function EmptyState({ view, model, onReset, stale }) {
  const filtered = model.count > 0 && view.count === 0
  const lines = noticeLines(model, stale)
  return h(
    'div',
    { className: 'duty-empty' },
    h('b', null, filtered ? 'под фильтры ничего не подошло' : 'нет активных дел'),
    h(
      'span',
      { className: 'duty-muted' },
      `репозиториев в store: ${model.repoCount} · дел без репозитория: ${model.withoutRepo}`,
    ),
    filtered ? h(Button, { size: 'xs', variant: 'secondary', onClick: onReset }, 'сбросить фильтры') : null,
    lines.length
      ? h('ul', null, ...lines.map((line, i) => h('li', { key: `notice-${i}` }, line)))
      : null,
  )
}

function DutyPage() {
  dutyTipBind()
  const snap = useNow()
  const [screen, setScreen] = useState('now')
  // Фильтр по репозиторию вместо доски: ALL_REPOS — все, '' — без репозитория.
  const [repo, setRepo] = useState(ALL_REPOS)
  const [states, setStates] = useState(() => new Set())
  const [query, setQuery] = useState('')
  const [effQuery, setEffQuery] = useState('')
  const [sel, setSel] = useState(null)
  const [tick, setTick] = useState(() => Date.now())
  // «Здоровье» запрашивается только когда экран открыт: это скан всех досок.
  const stats = useStats(screen === 'health')
  // «Завершённые» — часть экрана «Сейчас» (как на вебе): блок стоит под списком
  // активных дел, по пятёрке на проект, и сворачивать его больше нечем.
  const finished = useFinishedPages(true)

  // Поиск — локальный и с задержкой: данные не перезапрашиваются на каждый символ.
  useEffect(() => {
    const timer = setTimeout(() => setEffQuery(query), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query])

  // Подпись «обновлено N с назад» тикает раз в секунду, независимо от запросов.
  useEffect(() => {
    const timer = setInterval(() => setTick(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])

  const model = useMemo(() => (snap.data ? shapeNow(snap.data) : null), [snap.data])
  const view = useMemo(
    () => (model ? filterNow(model, { repo, states, query: effQuery }) : null),
    [model, repo, states, effQuery],
  )

  // Мини-таймлайн: активные дела плюс те закрытые, что уже загружены списком.
  // Цвет и границы этапов считает шейпер — здесь только данные для него.
  const finishedRows = useMemo(
    () => (finished.snapshot ? finished.snapshot.repos : []).flatMap((group) => group.deals || []),
    [finished.snapshot],
  )
  const timelineIds = useMemo(
    () => [...(model ? model.deals.map((deal) => deal.id) : []), ...finishedRows.map((row) => row.id)],
    [model, finishedRows],
  )
  const spans = useSpans(timelineIds)
  const timeline = useMemo(
    () => (model ? dealTimeline(model.deals, finishedRows, model.now, spans) : null),
    [model, finishedRows, spans],
  )

  if (!rest) {
    return h('div', { className: 'duty-page' }, h('div', { className: 'duty-banner' }, 'плагин не зарегистрирован'))
  }

  const updatedAgo = snap.updatedAt ? Math.max(0, Math.floor((tick - snap.updatedAt) / 1000)) : null
  const selectedKey = sel ? sel.id : ''
  // Клик по делу открывает его собственным экраном: у дела есть спецификация и
  // таймлайн попыток, и в узкую панель они не помещаются.
  const pick = (deal) => {
    lastDeal = deal
    setSel(deal)
    setScreen('deal')
  }
  // Клик по дорожке открывает дело тем же экраном, что и строка списка.
  const openTimelineRow = (row) => {
    const deal =
      (model && model.deals.find((item) => item.id === row.id)) ||
      (finishedRows.find((item) => item.id === row.id)) ||
      { id: row.id, repo: row.repoFull, titleFull: row.title }
    pick(deal)
  }
  const toggleState = (state) =>
    setStates((prev) => {
      const next = new Set(prev)
      if (next.has(state)) next.delete(state)
      else next.add(state)
      return next
    })
  const resetFilters = () => {
    setRepo(ALL_REPOS)
    setStates(new Set())
    setQuery('')
    setEffQuery('')
  }

  const header = h(
    'header',
    { className: 'duty-head' },
    h('h1', null, 'Дела'),
    h(
      'div',
      { className: 'duty-tabs' },
      ...TABS.map(([key, label]) =>
        h(
          'button',
          {
            key,
            type: 'button',
            className: screen === key ? 'duty-tab on' : 'duty-tab',
            onClick: () => {
              setScreen(key)
              // «Дело» возвращает к последнему открытому; остальные табы — без выбора.
              setSel(key === 'deal' ? lastDeal : null)
            },
          },
          label,
        ),
      ),
    ),
    model ? h('span', { className: 'duty-muted duty-num' }, `сейчас: ${view.count}`) : null,
    model && view
      ? h(
          'span',
          {
            className: 'duty-muted duty-num',
            title: 'ждёт человека: N вопросов (есть живой вопрос) + M блокировок (вопроса нет)',
          },
          humanLine(view.humanCounts),
        )
      : null,
    model ? h('span', { className: 'duty-muted' }, `· репозиториев ${model.repoCount} · без репозитория ${model.withoutRepo}`) : null,
    h('span', { className: 'duty-spacer' }),
    snap.loading && !model
      ? h('span', { className: 'duty-muted' }, 'загрузка…')
      : h('span', { className: 'duty-muted duty-num' }, updatedAgo === null ? 'ещё не обновлялось' : `обновлено ${updatedAgo} с назад`),
    h(
      'button',
      { type: 'button', className: 'duty-icon-btn', title: 'Обновить сейчас', onClick: () => void snap.refresh() },
      h(Codicon, { name: 'refresh', size: '0.85rem' }),
    ),
  )

  const banner = snap.error
    ? h(
        'div',
        { className: 'duty-banner' },
        h(
          'span',
          null,
          model
            ? `снимок от ${fmtClock(model.now)} — обновление не удалось: ${snap.error}`
            : `ошибка запроса /now: ${snap.error}`,
        ),
        h(Button, { size: 'xs', variant: 'secondary', onClick: () => void snap.refresh() }, 'повторить'),
      )
    : null

  // Упавшие источники и неполный срез — жёлтой полосой над списком, чтобы
  // неполнота ответа была видна и тогда, когда дела есть (в пустом состоянии те же
  // строки перечисляет EmptyState). Показываем их только по свежему ответу: при
  // `snap.error` экран держит прошлый снимок, и его замечания — не факт о
  // настоящем (иначе «источник X недоступен» висит вечно, хотя последний ответ его
  // уже не содержит).
  const stale = Boolean(snap.error)
  const noticeRows = noticeLines(model, stale)
  const notices = noticeRows.length && view && view.count > 0
    ? h(
        'div',
        { className: 'duty-note' },
        ...noticeRows.map((line, i) => h('div', { key: `notice-${i}` }, line)),
      )
    : null

  const filters = model
    ? h(
        'div',
        { className: 'duty-filters' },
        h(
          'select',
          {
            className: 'duty-select',
            value: repo,
            onChange: (e) => setRepo(e.target.value),
            title: 'Фильтр по репозиторию',
          },
          h('option', { value: ALL_REPOS }, 'все репозитории'),
          // «Без репозитория» — отдельная группа, и в списке фильтра она тоже
          // отдельная строка: пустое значение отличает её от «фильтра нет».
          model.withoutRepo > 0
            ? h('option', { value: '' }, `${WITHOUT_REPO_LABEL} (${model.withoutRepo})`)
            : null,
          ...model.repos.list.map((item) =>
            h('option', { value: item.repo, key: `repo-${item.repo}` }, `${item.repo} (${item.deals})`),
          ),
        ),
        h(
          'div',
          { className: 'duty-chips' },
          h(
            'button',
            {
              type: 'button',
              className: states.size ? 'duty-chip' : 'duty-chip on',
              onClick: () => setStates(new Set()),
            },
            'все',
            h('span', { className: 'duty-num' }, `${model.count}`),
          ),
          ...model.stateOrder.map((state) => {
            const meta = stateMeta(state)
            // Подпись чипа — та же, что у бейджа: «ждёт ответа» стоит только там,
            // где в этом состоянии действительно есть вопрос.
            const count = model.byState[state] || 0
            return h(
              'button',
              {
                type: 'button',
                key: `chip-${state}`,
                className: states.has(state) ? 'duty-chip on' : 'duty-chip',
                style: { color: TONE_COLOR[meta.tone] },
                title: state,
                onClick: () => toggleState(state),
              },
              stateLabel(state, model.deals),
              h('span', { className: 'duty-num' }, `${count}`),
            )
          }),
        ),
        h(Input, {
          style: { width: '220px', height: '26px' },
          placeholder: 'поиск: id, заголовок',
          value: query,
          onChange: (e) => setQuery(e.target.value),
        }),
      )
    : null

  let list
  if (!model) {
    list = h(
      'div',
      { className: 'duty-list' },
      h(
        'div',
        { className: 'duty-skel-row' },
        ...Array.from({ length: 6 }, (_, i) => h('div', { className: 'duty-skel', key: `boot-${i}`, style: { width: `${95 - i * 8}%` } })),
      ),
    )
  } else {
    // Таймлайн и завершённые видны и при пустом списке активных: «за сутки дел не
    // было» — это тоже ответ, а история закрытых дел от фильтров не зависит.
    list = h(
      'div',
      { className: 'duty-list' },
      h(DutyMiniTimeline, { view: timeline, onOpen: openTimelineRow }),
      view.count
        ? view.groups.map((group) => h(DealGroup, { key: `grp-${group.repo || 'without'}`, group, selectedKey, onPick: pick }))
        : h(EmptyState, { view, model, onReset: resetFilters, stale }),
      // Завершённые — тем же экраном, ниже активных: отдельной страницы нет
      // (09.11, Макс: «так же как и на вебе убираем отдельную страницу»).
      h(FinishedBlock, { finished, onPick: pick }),
    )
  }

  // Экраны переключаются табами; данные для каждого — свои, из одного и того же
  // бэкенда. «Дело» открывается из списка или из «Почему стоит».
  let content
  if (screen === 'deal' && sel) {
    content = h(DealScreen, {
      key: sel.id,
      sel,
      onBack: () => {
        setSel(null)
        setScreen('now')
      },
    })
  } else if (screen === 'deal') {
    // Дело ещё не выбрано: показываем активные дела — экран обязан быть
    // самостоятельным, а не только следствием клика по строке.
    content = model
      ? h(
          'div',
          { className: 'duty-scroll' },
          h('div', { className: 'duty-why-meta' }, 'выбери дело из активных'),
          ...view.groups.map((group) => h(DealGroup, { key: `pick-${group.repo || 'without'}`, group, selectedKey, onPick: pick })),
        )
      : h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 4 }, (_, i) => h('div', { className: 'duty-skel', key: `ds-${i}`, style: { width: `${92 - i * 9}%` } }))))
  } else if (screen === 'why') {
    content = model
      ? h(WhyScreen, { model, stale, onOpen: pick })
      : h('div', { className: 'duty-scroll' }, h('div', { className: 'duty-skel-row' }, ...Array.from({ length: 6 }, (_, i) => h('div', { className: 'duty-skel', key: `ws-${i}`, style: { width: `${95 - i * 8}%` } }))))
  } else if (screen === 'health') {
    content = h(HealthScreen, { stats })
  } else {
    content = h('div', { className: 'duty-body' }, list)
  }

  return h(
    'div',
    { className: 'duty-page' },
    header,
    screen === 'now' || screen === 'why' ? banner : null,
    screen === 'now' ? notices : null,
    screen === 'now' ? filters : null,
    content,
  )
}

/* ---------------- plugin ---------------- */

export default {
  id: ID, // должен совпадать с именем папки
  name: 'Дела',
  register(ctx) {
    rest = ctx.rest.bind(ctx)

    const style = document.createElement('style')
    style.dataset.plugin = ID
    style.textContent = CSS
    document.head.appendChild(style)

    ctx.onDispose(() => {
      style.remove()
      rest = null
    })

    // Полноэкранная страница «Дела» — вместо чата (area 'routes').
    ctx.register({
      id: 'duty-page',
      area: 'routes',
      data: { path: '/duty' },
      render: () => h(DutyPage),
    })

    // Пункт в сайдбаре рядом с «Тикетами» (area 'sidebar.nav', order 61).
    ctx.register({
      id: 'duty-nav',
      area: 'sidebar.nav',
      order: 61,
      data: { label: 'Дела', path: '/duty', codicon: 'pulse' },
    })
  },
}