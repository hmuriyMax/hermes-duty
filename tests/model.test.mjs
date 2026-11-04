/**
 * Регрессия шейпера страницы /duty и формы desktop-плагина.
 *
 *   node --test tests/model.test.mjs
 *
 * Фикстуры повторяют форму ответов бэкенда (`GET /now` и
 * `GET /duty/{board}/{task_id}/attempts`) — по одному делу на каждое из семи
 * состояний ожидания плюс недоступная доска и ошибка пула вопросов в `errors`.
 * Живой рендер здесь не проверяется: это отдельная приёмка в десктопе.
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import {
  STATE_META,
  WAIT_HUMAN_LABEL,
  filterNow,
  fmtAge,
  fmtTitle,
  groupDeals,
  humanCounts,
  humanLine,
  noticeLines,
  outcomeTone,
  shapeAttempts,
  shapeDeal,
  shapeNow,
  shapeStages,
  stageMeta,
  stateMeta,
} from '../desktop/model.js'

// Ожидаемый порядок приоритета — это ожидание теста к ответу бэкенда
// (WAITING_PRIORITY в dashboard/plugin_api.py), а не константа страницы.
const PRIORITY = [
  'wait_human',
  'capability',
  'dead_lease',
  'crashed',
  'working',
  'no_signal',
  'wait_slot',
  'wait_gate',
]

const nowPayload = JSON.parse(readFileSync(new URL('./fixtures/now.json', import.meta.url), 'utf8'))
const attemptsPayload = JSON.parse(readFileSync(new URL('./fixtures/attempts.json', import.meta.url), 'utf8'))
const dealPayload = JSON.parse(readFileSync(new URL('./fixtures/deal-stages.json', import.meta.url), 'utf8'))
const modelSource = readFileSync(new URL('../desktop/model.js', import.meta.url), 'utf8')
const pluginSource = readFileSync(new URL('../desktop/plugin.js', import.meta.url), 'utf8')

const model = shapeNow(nowPayload)

test('порядок групп — приоритет ответа бэкенда, по одной группе на состояние', () => {
  assert.deepEqual(
    model.groups.map((group) => group.state),
    PRIORITY,
  )
  assert.deepEqual(
    model.groups.map((group) => group.count),
    [1, 1, 1, 1, 1, 1, 1, 1],
  )
  assert.deepEqual(Object.keys(model.byState).sort(), [...PRIORITY].sort())
  assert.equal(model.count, 8)
  // чипы фильтра идут в порядке `by_state` бэкенда — второго списка приоритета нет
  assert.deepEqual(model.stateOrder, PRIORITY)
  // таблица подписей покрывает ровно те состояния, что присылает бэкенд
  assert.deepEqual([...Object.keys(STATE_META)].sort(), [...PRIORITY].sort())
})

test('у каждой группы порядок строк не пересортировывается', () => {
  const deals = [
    shapeDeal({ board: 'b', id: 't_1', waiting: { state: 'working' } }),
    shapeDeal({ board: 'b', id: 't_2', waiting: { state: 'working' } }),
  ]
  const groups = groupDeals(deals)
  assert.equal(groups.length, 1)
  assert.deepEqual(
    groups[0].deals.map((deal) => deal.id),
    ['t_1', 't_2'],
  )
})

test('страница не навязывает свой порядок: группы идут за ответом', () => {
  // обратный порядок состояний — единственный источник приоритета это бэкенд
  const groups = groupDeals([
    shapeDeal({ board: 'b', id: 't_1', waiting: { state: 'wait_gate' } }),
    shapeDeal({ board: 'b', id: 't_2', waiting: { state: 'wait_human' } }),
  ])
  assert.deepEqual(
    groups.map((group) => group.state),
    ['wait_gate', 'wait_human'],
  )
})

test('чипы фильтра идут в порядке by_state из ответа, а не таблицы подписей', () => {
  const reversed = [...PRIORITY].reverse()
  const byState = {}
  for (const state of reversed) byState[state] = 0
  const built = shapeNow({ ...nowPayload, by_state: byState })

  assert.deepEqual(built.stateOrder, reversed)
  // подписи при этом остаются на месте: порядок пришёл из данных, не из таблицы
  assert.equal(built.stateOrder.length, Object.keys(STATE_META).length)
  // без by_state порядок берётся из первого появления состояний в делах
  const noBackend = shapeNow({ deals: nowPayload.deals })
  assert.deepEqual(noBackend.stateOrder, PRIORITY)
})

test('набор «падений» приходит с бэкенда: локальной копии красного нет', () => {
  assert.equal(outcomeTone('crashed', 'failed', ['crashed', 'gave_up']), 'red')
  // тот же исход без набора с бэкенда — серый, а не красный: UI не угадывает
  assert.equal(outcomeTone('crashed', 'failed', []), 'gray')
  assert.equal(outcomeTone('crashed', 'failed'), 'gray')
  // успех и отложенное — презентация UI, набор падений её не касается
  assert.equal(outcomeTone('done', 'completed', ['crashed']), 'green')
  assert.equal(outcomeTone('reclaimed', 'released', ['crashed']), 'yellow')
  // набор из ответа таймлайна попадает в тон попытки
  const timeline = shapeAttempts(attemptsPayload)
  assert.deepEqual(
    timeline.attempts.map((attempt) => attempt.outcomeTone),
    ['red', 'red', 'green'],
  )
})

test('бейдж wait_human не обещает «человека»: вопрос может быть агенту', () => {
  assert.equal(stateMeta('wait_human', 'question').label, 'ждёт ответа')
  assert.ok(!STATE_META.wait_human.label.includes('человек'))
  const deal = model.deals.find((item) => item.id === 't_a1b2c3d4')
  assert.equal(deal.stateLabel, 'ждёт ответа')
  // чей это вопрос — видно в evidence (origin в ответе бэкенда)
  assert.match(deal.evidence, /origin='card'/)
})

test('подпись wait_human — по human_kind: «ждёт ответа» / «ждёт решения»', () => {
  // Блокировка без вопроса больше не выдаётся за ожидание ответа: шапка в том же
  // кадре пишет «0 вопросов + 1 блокировка», и бейдж обязан говорить то же.
  const asQuestion = shapeDeal({ waiting: { state: 'wait_human', human_kind: 'question' } })
  const asStatus = shapeDeal({ waiting: { state: 'wait_human', human_kind: 'status' } })
  assert.equal(asQuestion.stateLabel, 'ждёт ответа')
  assert.equal(asStatus.stateLabel, 'ждёт решения')
  // тон бейджа от human_kind не зависит: это всё ещё ожидание человека
  assert.equal(asStatus.tone, 'yellow')
  assert.equal(asQuestion.tone, 'yellow')

  // Без human_kind подпись базовая (не «ждёт решения» и не пустая).
  assert.equal(stateMeta('wait_human').label, STATE_META.wait_human.label)
  // Состояния, не связанные с ожиданием человека, от human_kind не зависят.
  assert.equal(stateMeta('working', 'status').label, 'работает')
  assert.equal(stateMeta('capability', 'question').label, 'нужен доступ')
  // Оба вида «нужен человек» названы: незаполненная подпись = дырка в бейдже.
  assert.deepEqual(Object.keys(WAIT_HUMAN_LABEL).sort(), ['question', 'status'])
})

test('подпись группы wait_human не обещает ответа, если вопросов в ней нет', () => {
  const onlyStatus = groupDeals([
    shapeDeal({ waiting: { state: 'wait_human', human_kind: 'status' } }),
    shapeDeal({ waiting: { state: 'wait_human', human_kind: 'status' } }),
  ])
  assert.equal(onlyStatus[0].label, 'ждёт решения')
  assert.equal(onlyStatus[0].count, 2)

  // Смешанная группа идёт под базовой подписью: «ждёт ответа» было бы неправдой
  // про половину строк, а «ждёт решения» — про другую.
  const mixed = groupDeals([
    shapeDeal({ waiting: { state: 'wait_human', human_kind: 'question' } }),
    shapeDeal({ waiting: { state: 'wait_human', human_kind: 'status' } }),
  ])
  assert.equal(mixed[0].label, STATE_META.wait_human.label)
})

test('human_kind: «вопрос» и «блокировка» считаются раздельно и одной строкой', () => {
  // Форма из ответа бэкенда: `waiting.human_kind` доезжает до строки таблицы.
  const asQuestion = shapeDeal({ waiting: { state: 'wait_human', human_kind: 'question' } })
  const asStatus = shapeDeal({ waiting: { state: 'wait_human', human_kind: 'status' } })
  const noHuman = shapeDeal({ waiting: { state: 'working' } })
  assert.equal(asQuestion.humanKind, 'question')
  assert.equal(asStatus.humanKind, 'status')
  assert.equal(noHuman.humanKind, '')

  // Счётчики — по делам, а не по состояниям: два разных «нужен человек».
  assert.deepEqual(
    humanCounts([asQuestion, asStatus, asStatus, noHuman]),
    { question: 1, status: 2 },
  )
  assert.deepEqual(humanCounts([]), { question: 0, status: 0 })

  // Строка шапки: русские формы числа, а не «1 вопросов».
  assert.equal(humanLine({ question: 1, status: 2 }), '1 вопрос + 2 блокировки')
  assert.equal(humanLine({ question: 21, status: 5 }), '21 вопрос + 5 блокировок')
  assert.equal(humanLine({ question: 3, status: 11 }), '3 вопроса + 11 блокировок')
  assert.equal(humanLine({ question: 0, status: 0 }), '0 вопросов + 0 блокировок')

  // Фикстура ответа: у дела в wait_human вопрос есть — модель это видит.
  assert.deepEqual(model.humanCounts, { question: 1, status: 0 })
})

test('фильтр пересчитывает счётчики человека по видимым делам', () => {
  const payload = {
    deals: [
      { board: 'one', id: 't_1', waiting: { state: 'wait_human', human_kind: 'question' } },
      { board: 'two', id: 't_2', waiting: { state: 'wait_human', human_kind: 'status' } },
    ],
    by_state: { wait_human: 2 },
  }
  const shaped = shapeNow(payload)
  assert.deepEqual(shaped.humanCounts, { question: 1, status: 1 })

  const onlyTwo = filterNow(shaped, { board: 'two' })
  assert.deepEqual(onlyTwo.humanCounts, { question: 0, status: 1 })
  assert.equal(humanLine(onlyTwo.humanCounts), '0 вопросов + 1 блокировка')
})

test('pid: «не записан» и «состояние неизвестно» — разные факты', () => {
  const unknown = shapeDeal({ attempt: { pid: 50123, pid_alive: null, age_sec: 5 }, waiting: { state: 'working' } })
  assert.equal(unknown.attemptNote, 'состояние pid неизвестно')
  const absent = shapeDeal({ attempt: { pid: null, pid_alive: null, age_sec: 5 }, waiting: { state: 'working' } })
  assert.equal(absent.attemptNote, 'pid не записан')
  assert.equal(shapeDeal({ attempt: { pid: 50123, pid_alive: true } }).attemptNote, 'pid жив')
  assert.equal(shapeDeal({ attempt: { pid: 50123, pid_alive: false } }).attemptNote, 'pid мёртв')
  assert.equal(shapeDeal({}).attemptNote, 'нет попытки')
})

test('незнакомое состояние — своей группой, без переклассификации', () => {
  const groups = groupDeals([
    shapeDeal({ board: 'b', id: 't_1', waiting: { state: 'working' } }),
    shapeDeal({ board: 'b', id: 't_2', waiting: { state: 'quantum' } }),
  ])
  assert.deepEqual(
    groups.map((group) => group.state),
    ['working', 'quantum'],
  )
  assert.equal(groups[1].label, 'quantum')
  assert.equal(groups[1].tone, 'gray')
})

test('длительности — человекочитаемо, без «0 мин» на секундах', () => {
  assert.equal(fmtAge(0), '0 с')
  assert.equal(fmtAge(45), '45 с')
  assert.equal(fmtAge(59), '59 с')
  assert.equal(fmtAge(60), '1 мин')
  assert.equal(fmtAge(720), '12 мин')
  assert.equal(fmtAge(3599), '59 мин')
  assert.equal(fmtAge(3600), '1 ч')
  assert.equal(fmtAge(10800), '3 ч')
  assert.equal(fmtAge(86400), '1 д')
  assert.equal(fmtAge(200000), '2 д')
  assert.equal(fmtAge(-5), '0 с')
  assert.equal(fmtAge(null), '—')
  assert.equal(fmtAge(undefined), '—')
  assert.equal(fmtAge(''), '—')
  assert.equal(fmtAge('nope'), '—')
})

test('заголовок обрезается многоточием ровно до лимита', () => {
  assert.equal(fmtTitle('короткий'), 'короткий')
  assert.equal(fmtTitle('  много\n пробелов  '), 'много пробелов')
  assert.equal(fmtTitle(''), '—')
  assert.equal(fmtTitle(null), '—')
  assert.equal(fmtTitle('x'.repeat(10), 6), 'xxxxx…')
  assert.equal(fmtTitle('x'.repeat(10), 6).length, 6)
  const long = model.deals.find((deal) => deal.id === 't_a1b2c3d4')
  assert.ok(long.title.endsWith('…'))
  assert.ok(long.title.length <= 64)
})

test('пустой attempt: возраст и лизинг показываются прочерком, а не нулём', () => {
  const deal = model.deals.find((item) => item.id === 't_6a7e0001')
  assert.equal(nowPayload.deals.find((item) => item.id === 't_6a7e0001').attempt, null)
  assert.equal(deal.attemptAge, null)
  assert.equal(deal.attemptAgeText, '—')
  assert.equal(deal.attemptNote, 'нет попытки')
  assert.equal(deal.leaseState, 'none')
  assert.equal(deal.leaseText, 'нет')
  assert.equal(deal.lastActionAgeText, '—')
  assert.equal(deal.lastActionKind, '—')
  assert.equal(deal.state, 'wait_gate')
  // дело без attempt: пид-фактов нет, но бейдж и доказательство на месте
  assert.equal(deal.stateLabel, 'ждёт гейт')
  assert.match(deal.evidence, /t_d0d7e8bc/)

  const withRun = model.deals.find((item) => item.id === 't_w0rk0001')
  assert.equal(withRun.attemptAgeText, '3 мин')
  assert.equal(withRun.attemptNote, 'pid жив')
  assert.equal(withRun.leaseText, 'жив')
})

test('строка таблицы — все поля запроса: доска, id, title, assignee, попытка, лизинг, действие, состояние', () => {
  const deal = model.deals.find((item) => item.id === 't_dead0001')
  assert.equal(deal.board, 'hermes-duty')
  assert.equal(deal.assignee, 'publisher')
  assert.equal(deal.attemptAgeText, '1 ч')
  assert.equal(deal.attemptNote, 'pid мёртв')
  assert.equal(deal.leaseState, 'expired')
  assert.equal(deal.leaseText, 'истёк')
  assert.equal(deal.leaseExpiresIn, -420)
  assert.equal(deal.lastActionAgeText, '1 ч')
  assert.equal(deal.state, 'dead_lease')
  assert.equal(deal.tone, 'red')
})

test('тон бейджа по состоянию: жёлтый/фиолетовый/красный/зелёный/серый', () => {
  assert.equal(stateMeta('wait_human').tone, 'yellow')
  assert.equal(stateMeta('capability').tone, 'purple')
  assert.equal(stateMeta('dead_lease').tone, 'red')
  assert.equal(stateMeta('crashed').tone, 'red')
  assert.equal(stateMeta('working').tone, 'green')
  assert.equal(stateMeta('no_signal').tone, 'gray')
  assert.equal(stateMeta('no_signal').label, 'нет сигнала')
  assert.equal(stateMeta('wait_slot').tone, 'gray')
  assert.equal(stateMeta('wait_gate').tone, 'gray')
  assert.equal(stateMeta('').tone, 'gray')
})

test('дело без живого сигнала не выдаётся за работающее', () => {
  const deal = model.deals.find((item) => item.id === 't_0519a001')
  assert.equal(deal.state, 'no_signal')
  assert.equal(deal.tone, 'gray')
  assert.equal(deal.stateLabel, 'нет сигнала')
  assert.equal(deal.attemptNote, 'нет попытки')
  assert.match(deal.evidence, /без живого сигнала/)
})

test('факты попытки из карточки помечены, а не выдаются за факт рана', () => {
  const fromTask = shapeDeal({
    attempt: { pid: 4111, pid_alive: true, source: 'task', age_sec: 180 },
    waiting: { state: 'working' },
  })
  assert.equal(fromTask.attemptNote, 'pid жив, факты карточки')
  // факты из записи рана идут без оговорки
  const fromRun = model.deals.find((item) => item.id === 't_dead0001')
  assert.equal(fromRun.attemptNote, 'pid мёртв')
  assert.equal(model.deals.find((item) => item.id === 't_w0rk0001').attemptNote, 'pid жив')
})

test('обрезанный срез и warnings доски не молчат: пометки рядом с недоступными', () => {
  const withNotices = shapeNow({
    ...nowPayload,
    boards: [
      ...nowPayload.boards,
      { slug: 'big', read: true, deals: 500, truncated: true, warnings: ['task_runs: no such table'] },
    ],
  })
  // доска прочиталась, но показано не всё — это видно текстом, а не только счётом
  assert.deepEqual(withNotices.truncatedNotices, [
    'доска big: срез обрезан лимитом, показано 500 дел',
  ])
  assert.ok(withNotices.warningNotices.includes('доска big: task_runs: no such table'))
  assert.ok(withNotices.warningNotices.includes('доска ozon: task_events: no such column: run_id'))
  // в фикстуре нет обрезанных досок, но warnings у ozon есть всегда
  assert.deepEqual(model.truncatedNotices, [])
  assert.deepEqual(model.warningNotices, ['доска ozon: task_events: no such column: run_id'])
})

test('устаревший снимок не показывает замечаний прошлого ответа', () => {
  // в фикстуре есть недоступная доска и ошибка источника — по свежему ответу это факт
  assert.ok(noticeLines(model).some((line) => line.startsWith('доска ozon-legacy недоступна')))
  assert.ok(noticeLines(model).some((line) => line.startsWith('источник messaging/pool.db')))

  // тот же снимок, но последний запрос не прошёл: на экране картина прошлого
  // ответа, и её замечания больше ничего не утверждают о настоящем
  assert.deepEqual(noticeLines(model, true), [])

  // «забыли передать снимок» не должно бросать исключение на пустом состоянии
  assert.deepEqual(noticeLines(null), [])
})

test('доски: счётчик прочитанных и причины недоступных — из ответа, без догадок', () => {
  assert.equal(model.boardsRead, 4)
  assert.equal(model.boardsTotal, 5)
  assert.deepEqual(model.unreachable, [{ board: 'ozon-legacy', reason: 'unable to open database file' }])
  // ошибка пула вопросов — не «доска недоступна»
  assert.deepEqual(
    model.otherErrors.map((item) => item.board),
    ['messaging/pool.db'],
  )
  assert.ok(!model.unreachable.some((item) => item.board === 'messaging/pool.db'))
})

test('фильтры локальные: доска, набор состояний, поиск по id и заголовку', () => {
  const byBoard = filterNow(model, { board: 'ozon' })
  assert.equal(byBoard.count, 3)
  assert.deepEqual(
    byBoard.groups.map((group) => group.state),
    ['capability', 'crashed', 'no_signal'],
  )
  // счётчики досок и ошибок фильтр не трогает — пустое состояние остаётся честным
  assert.equal(byBoard.boardsRead, model.boardsRead)
  assert.deepEqual(byBoard.unreachable, model.unreachable)

  const byState = filterNow(model, { states: new Set(['wait_human']) })
  assert.equal(byState.count, 1)
  assert.equal(byState.groups[0].state, 'wait_human')

  const byTwoStates = filterNow(model, { states: ['capability', 'wait_gate'] })
  assert.deepEqual(
    byTwoStates.groups.map((group) => group.state),
    ['capability', 'wait_gate'],
  )

  assert.equal(filterNow(model, { query: 'qaravai' }).count, 1)
  assert.equal(filterNow(model, { query: '  QARAVAI ' }).count, 1)
  assert.equal(filterNow(model, { query: 't_dead0001' }).count, 1)
  assert.equal(filterNow(model, { query: 'такого-нет' }).count, 0)
  assert.equal(filterNow(model, {}).count, 8)
})

test('пустой ответ /now не роняет модель', () => {
  const empty = shapeNow({ now: 1, count: 0, deals: [], by_state: {}, boards: [], errors: [] })
  assert.equal(empty.count, 0)
  assert.deepEqual(empty.groups, [])
  assert.equal(empty.boardsRead, 0)
  assert.equal(empty.boardsTotal, 0)
  assert.deepEqual(empty.unreachable, [])
  assert.deepEqual(filterNow(empty, { query: 'x' }).groups, [])
  assert.equal(shapeNow(null).count, 0)
})

test('таймлайн попыток: полоса относительно самой долгой, исход цветом, ошибка текстом', () => {
  const timeline = shapeAttempts(attemptsPayload)
  assert.equal(timeline.count, 3)
  assert.equal(timeline.title, 'IMPL: страница /duty — экран «сейчас» и таймлайн попыток')
  assert.deepEqual(
    timeline.attempts.map((attempt) => attempt.durationText),
    ['12 с', '10 мин', '2 мин'],
  )
  // самая долгая попытка — 100%, мгновенная не исчезает (минимум 2%)
  assert.deepEqual(
    timeline.attempts.map((attempt) => attempt.barPct),
    [2, 100, 20],
  )
  assert.deepEqual(
    timeline.attempts.map((attempt) => attempt.outcomeTone),
    ['red', 'red', 'green'],
  )
  assert.match(timeline.attempts[1].error, /nil pointer dereference/)
  assert.equal(timeline.attempts[0].outcome, 'spawn_failed')
  assert.deepEqual(
    timeline.attempts[1].events.map((event) => event.kind),
    ['claimed', 'tool_call', 'crashed'],
  )
  assert.equal(timeline.attempts[2].status, 'running')
  assert.equal(timeline.attempts[2].error, '')
})

test('таймлайн попыток: пустой ответ и попытка без длительности', () => {
  assert.equal(shapeAttempts({ attempts: [] }).count, 0)
  assert.equal(shapeAttempts(null).count, 0)
  const one = shapeAttempts({ attempts: [{ run_id: 1, status: 'running', duration_sec: null, events: [] }] })
  assert.equal(one.attempts[0].durationText, '—')
  assert.equal(one.attempts[0].barPct, 2)
  assert.equal(one.attempts[0].outcomeTone, 'green')
})

/* ------------------------------------- лента этапов дела (W6) ------------- */

const feed = shapeStages(dealPayload)

test('лента этапов: этапы в порядке графа, у попытки — факты процесса', () => {
  assert.deepEqual(
    feed.stages.map((stage) => stage.id),
    ['impl', 'review', 'ask'],
  )
  assert.equal(feed.count, 3)
  assert.equal(feed.attempts, 2)
  assert.equal(feed.spec, '/home/mshchemilkin/repo/duty.yaml')

  const impl = feed.stages[0]
  assert.equal(impl.role, 'implementer')
  assert.equal(impl.stateLabel, 'идёт')
  assert.equal(impl.tone, 'blue')
  assert.equal(impl.attemptText, 'попытка 2')

  // попытка — процесс: время, длительность, код выхода, токены, инструменты, причина
  const first = impl.attempts[0]
  assert.equal(first.number, '#1')
  assert.equal(first.pidText, 'pid 20658')
  assert.equal(first.duration, 300)
  assert.equal(first.durationText, '5 мин')
  assert.equal(first.exitText, '0')
  assert.equal(first.tokensText, '15 234')
  assert.equal(first.toolsText, '17')
  assert.equal(first.kind, 'contract')
  assert.equal(first.reason, 'нет evidence по go build ./...')
  assert.equal(first.liveText, 'исход: failed')
  assert.equal(first.tone, 'red')
})

test('идущая попытка не меряет себя: длительности нет, живость словом', () => {
  const live = feed.stages[0].attempts[1]
  assert.equal(live.duration, null)
  assert.equal(live.durationText, 'идёт')
  assert.equal(live.liveText, 'идёт')
  assert.equal(live.tone, 'blue')
  assert.equal(live.exitText, '—')
  assert.equal(live.tokensText, '—')
})

test('мёртвая попытка называется мёртвой, а не «идёт»', () => {
  const dead = shapeStages({
    stages: [{ id: 'impl', state: 'running', attempts: [{ n: 1, live: false, dead: true, lease: 'dead' }] }],
  }).stages[0].attempts[0]
  assert.equal(dead.liveText, 'процесс мёртв: лизинг истёк, завершения нет')
  assert.equal(dead.tone, 'red')
  // живость не записана — «неизвестна», а не «мёртв»
  const unknown = shapeStages({
    stages: [{ id: 'impl', state: 'running', attempts: [{ n: 1, live: null, lease: 'unknown' }] }],
  }).stages[0].attempts[0]
  assert.equal(unknown.liveText, 'живость неизвестна: лизинг не записан')
  assert.equal(unknown.tone, 'gray')
})

test('ожидание вопроса — отдельной строкой со ссылкой на панель пула', () => {
  const ask = feed.stages[2]
  assert.equal(ask.waitKind, 'question')
  assert.equal(ask.questionId, '284')
  assert.equal(ask.questionHref, '/imsg')
  assert.match(ask.waitText, /#284/)

  const review = feed.stages[1]
  assert.equal(review.waitKind, 'gate')
  assert.equal(review.questionHref, '')
  assert.match(review.waitText, /ждёт needs: impl/)
})

test('состояние этапа цветом: done спокойный, running акцент, wait_* янтарный, failed красный', () => {
  assert.equal(stageMeta('done').tone, 'green')
  assert.equal(stageMeta('running').tone, 'blue')
  assert.equal(stageMeta('wait_human').tone, 'yellow')
  assert.equal(stageMeta('wait_gate').tone, 'yellow')
  assert.equal(stageMeta('failed').tone, 'red')
  assert.equal(stageMeta('pending').tone, 'gray')
  // незнакомое состояние не переклассифицируется
  assert.deepEqual(stageMeta('своё'), { label: 'своё', tone: 'gray' })
})

test('evidence: «не доказано» и «код не совпал» — разные подписи', () => {
  const mismatched = feed.stages[0].evidence[0]
  assert.equal(mismatched.label, 'exit 1, объявлено 0')
  assert.equal(mismatched.tone, 'red')

  const entry = (evidence) => shapeStages({ stages: [{ id: 'x', evidence: [evidence] }] }).stages[0].evidence[0]
  const missing = entry({ cmd: 'go vet ./...', must: 0, got: null, ok: false })
  assert.equal(missing.label, 'не доказано: записи нет')
  assert.equal(missing.tone, 'gray')
  const proved = entry({ cmd: 'go build ./...', must: 0, got: 0, ok: true })
  assert.equal(proved.label, 'exit 0 — как объявлено')
  assert.equal(proved.tone, 'green')
})

test('парковка и пауза — своими строками: счётчик класса и время повтора', () => {
  const parked = shapeStages({
    stages: [
      {
        id: 'impl',
        state: 'failed',
        waits: {
          kind: 'parked',
          question_id: 91,
          reason: 'Попытки исчерпаны: 2/1 infra',
          class: 'infra',
          attempts: 2,
          limit: 1,
          park_reason: 'retries_exhausted',
        },
      },
    ],
  }).stages[0]
  assert.equal(parked.waitKind, 'parked')
  assert.equal(parked.questionId, '91')
  assert.match(parked.waitText, /2\/1 infra/)
  assert.match(parked.waitText, /retries_exhausted/)
  assert.match(parked.waitText, /#91/)

  // счётчик называет класс; без класса не выдумывает его
  const bare = shapeStages({ stages: [{ id: 'x', waits: { kind: 'parked', attempts: 3, limit: 2 } }] }).stages[0]
  assert.match(bare.waitText, /попытки исчерпаны: попытки исчерпаны/)
  assert.doesNotMatch(bare.waitText, /infra/)

  const retry = shapeStages({
    stages: [{ id: 'impl', waits: { kind: 'retry', retry_at: 1790000030 } }],
  }).stages[0]
  assert.equal(retry.waitKind, 'retry')
  assert.match(retry.waitText, /пауза до \d\d:\d\d:\d\d/)
})

test('пустая лента и этап без попыток не роняют рендер', () => {
  for (const empty of [null, {}, { stages: [] }, { stages: null }]) {
    const bare = shapeStages(empty)
    assert.equal(bare.count, 0)
    assert.equal(bare.empty, true)
    assert.deepEqual(bare.stages, [])
    assert.equal(bare.note, '')
  }
  // причину пустоты называет бэкенд, а не экран
  assert.equal(shapeStages({ stages: [], stages_note: 'у дела нет спеки' }).note, 'у дела нет спеки')

  const stage = shapeStages({ stages: [{ id: 'impl', state: 'pending' }] }).stages[0]
  assert.equal(stage.hasAttempts, false)
  assert.deepEqual(stage.attempts, [])
  assert.equal(stage.hasEvidence, false)
  assert.equal(stage.role, '—')
  assert.equal(stage.waitText, '')
})

test('/now: этап и номер попытки видны в строке, без открытия дела', () => {
  const withStage = shapeDeal({ board: 'b', id: 'd_1', stage: { id: 'impl', attempt: 2, state: 'running', live: true } })
  assert.equal(withStage.stageText, 'impl · попытка 2 · идёт')
  assert.equal(withStage.stageId, 'impl')
  assert.equal(withStage.stageAttempt, 2)
  // без попыток строки нет: «этап 0» был бы выдуманным графом
  assert.equal(shapeDeal({ board: 'b', id: 'd_2' }).stageText, '')
})

test('вшитый в plugin.js шейпер дословно совпадает с desktop/model.js', () => {
  const cut = modelSource.lastIndexOf('\nexport {')
  assert.ok(cut > 0, 'model.js: не найден блок экспортов')
  const want = modelSource.slice(0, cut).trimEnd()
  const start = pluginSource.indexOf('// duty:model-start')
  const end = pluginSource.indexOf('// duty:model-end')
  assert.ok(start > 0 && end > start, 'plugin.js: маркеры вшитого шейпера не найдены')
  const got = pluginSource
    .slice(pluginSource.indexOf('\n', start) + 1, end)
    .trimEnd()
  assert.equal(got, want, 'шейпер в plugin.js разъехался с desktop/model.js — запусти bin/duty-model-sync')
})

test('plugin.js: форма плагина, маршрут, пункт сайдбара и только разрешённые импорты', () => {
  assert.match(pluginSource, /const ID = 'hermes-duty'/)
  assert.match(pluginSource, /export default \{\s*\n\s*id: ID,/)
  assert.match(pluginSource, /name: 'Дела'/)
  assert.match(pluginSource, /id: 'duty-page',\s*\n\s*area: 'routes',/)
  assert.match(pluginSource, /data: \{ path: '\/duty' \}/)
  assert.match(pluginSource, /id: 'duty-nav',\s*\n\s*area: 'sidebar\.nav',\s*\n\s*order: 61,/)
  assert.match(pluginSource, /data: \{ label: 'Дела', path: '\/duty', codicon: 'pulse' \}/)
  // данные — только через ctx.rest
  assert.match(pluginSource, /rest = ctx\.rest\.bind\(ctx\)/)
  assert.match(pluginSource, /rest\('\/now'/)
  assert.match(pluginSource, /\/duty\/\$\{encodeURIComponent\(board\)\}/)
  // ни прямого fetch, ни шелла, ни относительных импортов (загрузчик их отвергает)
  assert.ok(!pluginSource.includes('fetch('), 'plugin.js: прямой fetch запрещён')
  assert.ok(!pluginSource.includes('child_process'), 'plugin.js: шелл из UI запрещён')
  const specifiers = [...pluginSource.matchAll(/^\s*import .*?from '([^']+)'/gm)].map((match) => match[1])
  assert.ok(specifiers.length > 0)
  assert.deepEqual(
    specifiers.filter((spec) => spec !== '@hermes/plugin-sdk' && !spec.startsWith('react')),
    [],
  )
})
