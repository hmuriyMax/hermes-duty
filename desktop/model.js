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
const OUTCOME_TONE = {
  done: 'green',
  completed: 'green',
  succeeded: 'green',
  rate_limited: 'yellow',
  reclaimed: 'yellow',
}

const LEASE_LABEL = {
  live: 'жив',
  expired: 'истёк',
  none: 'нет',
  unknown: 'неизвестен',
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
    id: String(d.id || ''),
    title: fmtTitle(d.title),
    titleFull: String(d.title || ''),
    assignee: String(d.assignee || '—'),
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
    search: `${d.repo || ''} ${d.id || ''} ${d.title || ''} ${d.assignee || ''} ${waiting.state || ''}`.toLowerCase(),
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
    label: repo,
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
function shapeStageAttempt(attempt) {
  const a = attempt && typeof attempt === 'object' ? attempt : {}
  const outcome = String(a.outcome || '')
  const live = a.live === true
  const dead = a.dead === true
  const lease = String(a.lease || '')
  const duration = a.duration_sec === undefined ? null : a.duration_sec
  let liveText = 'закрыта без исхода'
  if (live) liveText = 'идёт'
  else if (dead) liveText = 'процесс мёртв: лизинг истёк, завершения нет'
  else if (lease === 'unknown') liveText = 'живость неизвестна: лизинг не записан'
  else if (outcome) liveText = `исход: ${outcome}`
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
function shapeStage(stage) {
  const s = stage && typeof stage === 'object' ? stage : {}
  const meta = stageMeta(s.state)
  const waits = s.waits && typeof s.waits === 'object' ? s.waits : null
  const needs = (Array.isArray(s.needs) ? s.needs : []).map(String)
  const attempts = (Array.isArray(s.attempts) ? s.attempts : []).map(shapeStageAttempt)
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
function shapeStages(payload) {
  const data = payload && typeof payload === 'object' ? payload : {}
  const stages = (Array.isArray(data.stages) ? data.stages : []).map(shapeStage)
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

/* ---------------------------------------- шкала времени дела (E2, 0.0.5) */

// Минимальная ширина полосы на общей оси: мгновенная попытка не должна исчезать
// из шкалы (те же 2%, что были у относительной полосы), но подпись при этом
// честная — `<1с`, иначе две ширины в процентах читались бы как две секунды.
const BAR_MIN_PCT = 2

// Подпись и тон маркера: журнал назвал событие — экран назвал его тем же словом.
// `tick` — тонкая засечка старта/закрытия попытки (их на оси много); остальные
// виды — то, что случилось с делом, и они рисуются заметно.
const MARKER_META = {
  question: { label: 'вопрос человеку', tone: 'yellow', tick: false },
  answer: { label: 'ответ человека', tone: 'green', tick: false },
  verdict: { label: 'вердикт', tone: 'blue', tick: false },
  budget: { label: 'отказ по бюджету', tone: 'red', tick: false },
  published: { label: 'публикация', tone: 'green', tick: false },
  closed: { label: 'закрыто', tone: 'gray', tick: false },
  started: { label: 'старт попытки', tone: 'gray', tick: true },
  ended: { label: 'конец попытки', tone: 'gray', tick: true },
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

/** Лизинг незакрытой попытки словом: `жив`/`лизинг истёк`/`неизвестно`. */
function leaseWord(lease, status) {
  const key = String(lease || '')
  if (key === 'live') return 'жив'
  if (key === 'expired') return 'лизинг истёк'
  if (key === 'dead' || status === 'orphan') return 'лизинг истёк'
  return 'неизвестно'
}

/** Сколько дел в секундах — только конечные числа, иначе `null`. */
function numOrNull(value) {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

/** Попытка из ответа `/attempts` в полосу шкалы: время, дорожка, подписи. */
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
    outcome: String(a.outcome || ''),
    status: String(a.status || ''),
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
function shapeDealTime(detail, attemptsPayload) {
  const d = detail && typeof detail === 'object' ? detail : {}
  const a = attemptsPayload && typeof attemptsPayload === 'object' ? attemptsPayload : {}
  const now = numOrNull(d.now) ?? numOrNull(a.now)
  // «Живое» — по `terminal` ответа: закрытое дело ось не тянет вперёд, и его
  // длительность перестаёт расти в ту же секунду, когда записан `completed`.
  // Событие закрытия в журнале — тот же признак, если сервер не отдал `terminal`.
  const events = (Array.isArray(d.timeline) ? d.timeline : []).map((event) => {
    const e = event && typeof event === 'object' ? event : {}
    return { kind: String(e.kind || ''), at: numOrNull(e.at) || 0, summary: String(e.summary || '') }
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
  const pctOf = (at) => {
    if (axisStart === null || !(at > 0) || span === 0) return 0
    const value = ((at - axisStart) / span) * 100
    return Math.round(Math.max(0, Math.min(100, value)) * 10) / 10
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
    const stop = bar.open
      ? (now !== null ? now : bar.start)
      : bar.end !== null
        ? bar.end
        : bar.start === null
          ? null
          : bar.start + (bar.duration || 0)
    bar.endAt = stop
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
    // Подпись внутри полосы — исход, а не догадка: у незакрытой это слово о
    // лизинге, у закрытой без исхода — её длительность.
    bar.label = bar.open ? bar.leaseText : bar.outcome || bar.status || bar.durationText
    const details = [bar.progressText, bar.toolsText].filter(Boolean).join(', ')
    const head = `${bar.laneLabel} · попытка ${bar.number || '—'}`
    const interval = `${bar.clockStart}–${bar.clockEnd} (${bar.durationText})`
    const tail = bar.open ? `лизинг: ${bar.leaseText}` : `исход: ${bar.outcome || bar.status || 'не записан'}`
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
    const title = `${clock} · ${meta.label}${event.summary ? ` — ${event.summary}` : ''}`
    const marker = {
      type,
      kind: event.kind,
      at: event.at,
      pct: pctOf(event.at),
      clock,
      label: meta.label,
      tone: meta.tone,
      tick: meta.tick,
      summary: event.summary,
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
  const pub = Object.keys(pubRaw).length ? pubRaw : null
  const published = pub
    ? {
        sha: String(pub.sha || ''),
        shaShort: String(pub.sha || '').slice(0, 8),
        branch: String(pub.branch || ''),
        ref: String(pub.ref || ''),
        mrUrl: String(pub.mr_url || ''),
        verifiedBy: String(pub.verified_by || ''),
        text: [String(pub.sha || '').slice(0, 8), String(pub.branch || pub.ref || ''), String(pub.verified_by || '')]
          .filter(Boolean)
          .join(' · '),
      }
    : null
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
        text: `закрыто ${fmtClock(closedRaw.at)}${closedRaw.cause ? `: ${closedRaw.cause}` : ''}`,
      }
    : null
  const durationSec = axisStart === null || axisEnd === null ? null : Math.max(0, axisEnd - axisStart)

  const midAt = axisStart === null || axisEnd === null ? null : axisStart + Math.round((axisEnd - axisStart) / 2)
  return {
    now,
    live,
    empty: bars.length === 0 && markers.length === 0,
    axis: {
      start: axisStart,
      end: axisEnd,
      spanSec: durationSec,
      live,
      text: axisStart === null || axisEnd === null ? 'времени в журнале нет' : `${fmtClockShort(axisStart)}–${fmtClockShort(axisEnd)}`,
      ticks:
        axisStart === null || axisEnd === null
          ? []
          : [
              { pct: 0, edge: 'нач.', align: 'start', clock: fmtClock(axisStart) },
              { pct: 50, edge: 'сер.', align: 'center', clock: fmtClock(midAt) },
              { pct: 100, edge: 'кон.', align: 'end', clock: fmtClock(axisEnd) },
            ],
    },
    lanes,
    bars,
    markers,
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

export {
  STATE_META,
  WAIT_HUMAN_LABEL,
  TONE_COLOR,
  STAGE_META,
  WITHOUT_REPO_LABEL,
  ALL_REPOS,
  repoLabel,
  stateMeta,
  stageMeta,
  humanCounts,
  humanLine,
  fmtAge,
  fmtTitle,
  fmtClock,
  shapeDeal,
  stateCounts,
  stateLabel,
  groupDeals,
  groupByState,
  groupStatesText,
  shapeRepos,
  shapeNow,
  filterNow,
  outcomeTone,
  shapeAttempts,
  shapeDealTime,
  shapeStages,
  noticeLines,
}
