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
  ALL_REPOS,
  STATE_META,
  WAIT_HUMAN_LABEL,
  WITHOUT_REPO_LABEL,
  TL,
  filterNow,
  fmtAge,
  fmtTitle,
  groupByState,
  groupDeals,
  groupStatesText,
  humanCounts,
  humanLine,
  noticeLines,
  attemptView,
  laneView,
  layoutCalls,
  outcomeTone,
  repoLabel,
  shapeAttempts,
  shapeCalls,
  shapeDeal,
  shapeDealTime,
  shapeFinished,
  shapeNow,
  shapeRepos,
  shapeStages,
  stageMeta,
  stateLabel,
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

test('группы — по репозиторию: названные по имени, «без репозитория» последней', () => {
  // Доска ушла (0.0.6): группировка списка — `repo`, а дело адресуется своим id.
  assert.deepEqual(
    model.groups.map((group) => group.label),
    [
      '~/.hermes/plugins/hermes-duty',
      '~/go/src/gitlab.ozon.ru/review/pinned-review-service',
      '~/go/src/gitlab.ozon.ru/sx/sx-widgets-api',
      WITHOUT_REPO_LABEL,
    ],
  )
  assert.deepEqual(
    model.groups.map((group) => group.count),
    [2, 2, 2, 2],
  )
  // «без репозитория» — последняя группа, и это законное состояние, а не пропуск
  assert.equal(model.groups[3].repo, '')
  assert.equal(model.groups.slice(0, 3).reduce((sum, group) => sum + group.count, 0), 6)
  // состояния при этом не потеряны: они в строках и в подписи группы
  assert.deepEqual(Object.keys(model.byState).sort(), [...PRIORITY].sort())
  assert.equal(model.count, 8)
  // чипы фильтра идут в порядке `by_state` бэкенда — второго списка приоритета нет
  assert.deepEqual(model.stateOrder, PRIORITY)
  // таблица подписей покрывает ровно те состояния, что присылает бэкенд
  assert.deepEqual([...Object.keys(STATE_META)].sort(), [...PRIORITY].sort())
})

test('у каждой группы порядок строк не пересортировывается', () => {
  const deals = [
    shapeDeal({ repo: 'r', id: 'd_1', waiting: { state: 'working' } }),
    shapeDeal({ repo: 'r', id: 'd_2', waiting: { state: 'working' } }),
  ]
  const groups = groupDeals(deals)
  assert.equal(groups.length, 1)
  assert.deepEqual(
    groups[0].deals.map((deal) => deal.id),
    ['d_1', 'd_2'],
  )
})

test('внутри группы порядок — ответа бэкенда, подписи группы — по её состояниям', () => {
  // Приоритет задаёт `/now` (WAITING_PRIORITY); экран не переставляет строки.
  const groups = groupDeals([
    shapeDeal({ repo: 'r', id: 'd_1', waiting: { state: 'wait_gate' } }),
    shapeDeal({ repo: 'r', id: 'd_2', waiting: { state: 'wait_human' } }),
  ])
  assert.deepEqual(
    groups[0].deals.map((deal) => deal.state),
    ['wait_gate', 'wait_human'],
  )
  // Состояния группы названы одной строкой в порядке появления
  assert.equal(groupStatesText(groups[0]), 'ждёт гейт 1 · ждёт ответа 1')
})

test('«Почему стоит» группирует по ожиданию: группы идут за ответом бэкенда', () => {
  const stateGroups = groupByState([
    shapeDeal({ repo: 'r', id: 'd_1', waiting: { state: 'wait_gate' } }),
    shapeDeal({ repo: 'r', id: 'd_2', waiting: { state: 'wait_human' } }),
  ])
  assert.deepEqual(
    stateGroups.map((group) => group.state),
    ['wait_gate', 'wait_human'],
  )
  assert.equal(stateGroups[0].label, 'ждёт гейт')
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
  const deal = model.deals.find((item) => item.id === 'd_a1b2c3d4')
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

test('подпись группы ожидания не обещает ответа, если вопросов в ней нет', () => {
  const onlyStatus = groupByState([
    shapeDeal({ waiting: { state: 'wait_human', human_kind: 'status' } }),
    shapeDeal({ waiting: { state: 'wait_human', human_kind: 'status' } }),
  ])
  assert.equal(onlyStatus[0].label, 'ждёт решения')
  assert.equal(onlyStatus[0].count, 2)

  // Смешанная группа идёт под базовой подписью: «ждёт ответа» было бы неправдой
  // про половину строк, а «ждёт решения» — про другую.
  const mixed = groupByState([
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
      { repo: 'one', id: 'd_1', waiting: { state: 'wait_human', human_kind: 'question' } },
      { repo: 'two', id: 'd_2', waiting: { state: 'wait_human', human_kind: 'status' } },
    ],
    by_state: { wait_human: 2 },
  }
  const shaped = shapeNow(payload)
  assert.deepEqual(shaped.humanCounts, { question: 1, status: 1 })

  const onlyTwo = filterNow(shaped, { repo: 'two' })
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
  const groups = groupByState([
    shapeDeal({ repo: 'r', id: 'd_1', waiting: { state: 'working' } }),
    shapeDeal({ repo: 'r', id: 'd_2', waiting: { state: 'quantum' } }),
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
  const long = model.deals.find((deal) => deal.id === 'd_a1b2c3d4')
  assert.ok(long.title.endsWith('…'))
  assert.ok(long.title.length <= 64)
})

test('пустой attempt: возраст и лизинг показываются прочерком, а не нулём', () => {
  const deal = model.deals.find((item) => item.id === 'd_6a7e0001')
  assert.equal(nowPayload.deals.find((item) => item.id === 'd_6a7e0001').attempt, null)
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
  assert.match(deal.evidence, /d_d0d7e8bc/)

  const withRun = model.deals.find((item) => item.id === 'd_w0rk0001')
  assert.equal(withRun.attemptAgeText, '3 мин')
  assert.equal(withRun.attemptNote, 'pid жив')
  assert.equal(withRun.leaseText, 'жив')
})

test('строка таблицы — все поля запроса: репозиторий, id, title, assignee, попытка, лизинг, действие, состояние', () => {
  const deal = model.deals.find((item) => item.id === 'd_dead0001')
  assert.equal(deal.repo, '~/.hermes/plugins/hermes-duty')
  assert.equal(deal.repoLabel, '~/.hermes/plugins/hermes-duty')
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
  const deal = model.deals.find((item) => item.id === 'd_0519a001')
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
  const fromRun = model.deals.find((item) => item.id === 'd_dead0001')
  assert.equal(fromRun.attemptNote, 'pid мёртв')
  assert.equal(model.deals.find((item) => item.id === 'd_w0rk0001').attemptNote, 'pid жив')
})

test('упавший источник назван словами, а не выдан за недоступную доску', () => {
  // Ошибки ответа — пара `{source, reason}`: store дел и пул вопросов.
  assert.deepEqual(
    model.errors.map((error) => error.source),
    ['duty/duty.db', 'messaging/pool.db'],
  )
  // `repos.read` в фикстуре true: группировка посчитана
  assert.equal(model.repos.read, true)
  assert.deepEqual(model.repos.list.map((item) => [item.repo, item.deals]), [
    ['~/.hermes/plugins/hermes-duty', 2],
    ['~/go/src/gitlab.ozon.ru/review/pinned-review-service', 2],
    ['~/go/src/gitlab.ozon.ru/sx/sx-widgets-api', 2],
  ])
  assert.equal(model.withoutRepo, 2)
  assert.equal(model.repoCount, 3)
})

test('непосчитанная группировка названа, а не показана пустой', () => {
  const broken = shapeNow({ deals: [], repos: { list: [], without_repo: 0, read: false }, errors: [] })
  assert.deepEqual(noticeLines(broken), ['store дел не прочитан: группировка по репозиториям не посчитана'])
  assert.deepEqual(noticeLines(model).filter((line) => line.startsWith('store дел не прочитан')), [])
})

test('устаревший снимок не показывает замечаний прошлого ответа', () => {
  // в фикстуре есть упавшие источники — по свежему ответу это факт
  assert.ok(noticeLines(model).some((line) => line.startsWith('источник duty/duty.db недоступен')))
  assert.ok(noticeLines(model).some((line) => line.startsWith('источник messaging/pool.db недоступен')))

  // тот же снимок, но последний запрос не прошёл: на экране картина прошлого
  // ответа, и её замечания больше ничего не утверждают о настоящем
  assert.deepEqual(noticeLines(model, true), [])

  // «забыли передать снимок» не должно бросать исключение на пустом состоянии
  assert.deepEqual(noticeLines(null), [])
})

test('фильтры локальные: репозиторий, набор состояний, поиск по id и заголовку', () => {
  const byRepo = filterNow(model, { repo: '~/go/src/gitlab.ozon.ru/sx/sx-widgets-api' })
  assert.equal(byRepo.count, 2)
  assert.deepEqual(
    byRepo.deals.map((deal) => deal.state),
    ['wait_human', 'capability'],
  )
  // счётчики группировки фильтр не трогает — пустое состояние остаётся честным
  assert.equal(byRepo.repoCount, model.repoCount)
  assert.equal(byRepo.withoutRepo, model.withoutRepo)

  // пустое значение — ровно дела без репозитория (так же их отличает роут: ?repo=)
  const withoutRepo = filterNow(model, { repo: '' })
  assert.equal(withoutRepo.count, 2)
  assert.deepEqual(
    withoutRepo.groups.map((group) => group.label),
    [WITHOUT_REPO_LABEL],
  )
  // ALL_REPOS и отсутствие фильтра — одно и то же
  assert.equal(filterNow(model, { repo: ALL_REPOS }).count, 8)
  assert.equal(filterNow(model, {}).count, 8)

  const byState = filterNow(model, { states: new Set(['wait_human']) })
  assert.equal(byState.count, 1)
  assert.equal(byState.deals[0].state, 'wait_human')

  const byTwoStates = filterNow(model, { states: ['capability', 'wait_gate'] })
  assert.deepEqual(
    byTwoStates.deals.map((deal) => deal.state),
    ['capability', 'wait_gate'],
  )

  assert.equal(filterNow(model, { query: 'qaravai' }).count, 1)
  assert.equal(filterNow(model, { query: '  QARAVAI ' }).count, 1)
  assert.equal(filterNow(model, { query: 'd_dead0001' }).count, 1)
  assert.equal(filterNow(model, { query: 'такого-нет' }).count, 0)
  // поиск знает и репозиторий: «чей это рой» ищется тем же полем
  assert.equal(filterNow(model, { query: 'pinned-review-service' }).count, 2)
})

test('пустой ответ /now не роняет модель', () => {
  const empty = shapeNow({ now: 1, count: 0, deals: [], by_state: {}, repos: { list: [], without_repo: 0, read: true }, errors: [] })
  assert.equal(empty.count, 0)
  assert.deepEqual(empty.groups, [])
  assert.deepEqual(empty.stateGroups, [])
  assert.equal(empty.repoCount, 0)
  assert.equal(empty.withoutRepo, 0)
  assert.deepEqual(empty.errors, [])
  assert.deepEqual(filterNow(empty, { query: 'x' }).groups, [])
  assert.equal(shapeNow(null).count, 0)
})

test('подписи репозитория и формы его отсутствия — одни на обе поверхности', () => {
  assert.equal(repoLabel('~/go/src/x'), '~/go/src/x')
  assert.equal(repoLabel(''), WITHOUT_REPO_LABEL)
  assert.equal(repoLabel(null), WITHOUT_REPO_LABEL)
  assert.equal(shapeDeal({ repo: null }).repoLabel, WITHOUT_REPO_LABEL)
  // состояние группы подписано по её делам: вопрос есть у всех — «ждёт ответа»
  assert.equal(stateLabel('wait_human', model.deals.filter((deal) => deal.state === 'wait_human')), 'ждёт ответа')
  assert.deepEqual(shapeRepos(null), { list: [], withoutRepo: 0, read: true })
  assert.equal(shapeRepos({ read: false }).read, false)
})

test('таймлайн попыток: длительность словами, исход цветом, ошибка текстом', () => {
  const timeline = shapeAttempts(attemptsPayload)
  assert.equal(timeline.count, 3)
  assert.equal(timeline.title, 'IMPL: страница /duty — экран «сейчас» и таймлайн попыток')
  assert.deepEqual(
    timeline.attempts.map((attempt) => attempt.durationText),
    ['12 с', '10 мин', '2 мин'],
  )
  assert.deepEqual(
    timeline.attempts.map((attempt) => attempt.outcomeTone),
    ['red', 'red', 'green'],
  )
  // Относительной полосы в шейпере нет: ширину полосы считает общая ось дела
  // (`shapeDealTime`), а `barPct` давал бы на том же экране вторую шкалу.
  assert.ok(!('barPct' in timeline.attempts[0]), 'в попытке остался относительный процент')
  assert.match(timeline.attempts[1].error, /nil pointer dereference/)
  assert.equal(timeline.attempts[0].outcome, 'spawn_failed')
  assert.deepEqual(
    timeline.attempts[1].events.map((event) => event.kind),
    ['claimed', 'tool_call', 'crashed'],
  )
  assert.equal(timeline.attempts[2].status, 'running')
  // Попытка без `end` — незакрытая: шкала тянет её до «сейчас» пунктиром.
  assert.equal(timeline.attempts[2].open, true)
  assert.equal(timeline.attempts[1].open, false)
  assert.equal(timeline.attempts[2].error, '')
})

test('таймлайн попыток: пустой ответ и попытка без длительности', () => {
  assert.equal(shapeAttempts({ attempts: [] }).count, 0)
  assert.equal(shapeAttempts(null).count, 0)
  const one = shapeAttempts({ attempts: [{ run_id: 1, status: 'running', duration_sec: null, events: [] }] })
  assert.equal(one.attempts[0].durationText, '—')
  assert.equal(one.attempts[0].outcomeTone, 'green')
  // Номеров процесса нет — подписи пустые, а не «шагов 0»: ноль шагов никто не мерил.
  assert.equal(one.attempts[0].progressText, '')
  assert.equal(one.attempts[0].toolsText, '')
})

/* ------------------------------- шкала времени дела (E2) ----------------- */

// Ось в 500 с: `created` в 1000, два перекрывающихся этапа (impl 1060–1180,
// verify 1120–1240), живая попытка impl с 1250, нулевая попытка verify в 1240,
// вопрос человеку с ответом и вопрос без ответа. `now` = 1500 — дело не закрыто,
// поэтому ось тянется до него, а не до последнего события.
const TIME_DETAIL = {
  now: 1500,
  status: 'running',
  terminal: false,
  timeline: [
    { kind: 'created', at: 1000, summary: 'ready' },
    { kind: 'attempt_started', at: 1060, summary: '{"run_id": "impl#1", "stage": "impl", "attempt": 1}' },
    { kind: 'attempt_started', at: 1120, summary: '{"run_id": "verify#1", "stage": "verify", "attempt": 1}' },
    { kind: 'attempt_ended', at: 1180, summary: 'impl: готово' },
    { kind: 'question_asked', at: 1200, summary: '{"question_id": 7}' },
    { kind: 'attempt_ended', at: 1240, summary: 'verify: упало' },
    { kind: 'question_answered', at: 1300, summary: '{"question_id": 7, "answer_len": 4}' },
    { kind: 'published', at: 1320, summary: '{"sha": "f750c7850a706b089ff3ea783c86cc9f8d0a791a"}' },
    { kind: 'question_asked', at: 1340, summary: '{"question_id": 8}' },
  ],
  budget: { tokens: { spent: 1500, limit: 6000 }, minutes: null, text: 'токенов 1 500 / 6 000', attempts: 3, exhausted: null },
  published: { sha: 'f750c7850a706b089ff3ea783c86cc9f8d0a791a', branch: 'release/pub', verified_by: 'git ls-remote' },
  closed: null,
}

const TIME_ATTEMPTS = {
  crash_outcomes: ['crashed', 'gave_up'],
  attempts: [
    { run_id: 'impl#1', stage: 'impl', attempt: 1, status: 'completed', start: 1060, end: 1180, duration_sec: 120, outcome: 'done', progress: 12, tools: 7 },
    { run_id: 'verify#1', stage: 'verify', attempt: 1, status: 'failed', start: 1120, end: 1240, duration_sec: 120, outcome: 'crashed' },
    { run_id: 'impl#2', stage: 'impl', attempt: 2, status: 'running', start: 1250, end: null, duration_sec: null, outcome: null, lease: 'live' },
    { run_id: 'verify#2', stage: 'verify', attempt: 2, status: 'completed', start: 1240, end: 1240, duration_sec: 0, outcome: 'done' },
  ],
}

test('маркеры: событие принадлежит одной попытке — стык и промежуток не дублируют', () => {
  const scale = shapeDealTime(
    {
      now: 1_600,
      terminal: false,
      timeline: [
        { kind: 'created', at: 1_000, summary: '' },
        { kind: 'collected', at: 1_200, summary: 'вердикт: собрано' },
        { kind: 'question_asked', at: 1_300, summary: 'нужно ок' },
        { kind: 'question_answered', at: 1_450, summary: 'ок' },
        { kind: 'collected', at: 1_600, summary: 'вердикт: собрано' },
        // Засечки старта/конца попыток: их роль играет фон, в строки они не идут.
        { kind: 'attempt_started', at: 1_000, summary: '{"run_id": "impl#1", "stage": "impl", "attempt": 1}' },
        { kind: 'attempt_ended', at: 1_200, summary: 'done' },
      ],
      stages: [{ id: 'impl', needs: [] }],
    },
    {
      attempts: [
        { stage: 'impl', attempt: 1, start: 1_000, end: 1_200, status: 'done' },
        { stage: 'impl', attempt: 2, start: 1_200, end: 1_250, status: 'done' },
        { stage: 'impl', attempt: 3, start: 1_500, end: 1_600, status: 'done' },
      ],
    },
  )
  const rows = scale.bars.map((bar) => attemptView(bar, scale).marks.map((mark) => mark.title))
  const placed = rows.flat()
  // Вердикт ровно на стыке попыток 1 и 2 — один раз, у начавшейся.
  assert.equal(placed.filter((title) => title.startsWith('13:20:00') || title.includes('12:20')).length <= 1, true)
  assert.equal(new Set(placed).size, placed.length, `дубликаты: ${placed.length - new Set(placed).size}`)
  // Все незасечки попали в строки, а засечки — нет: их роль играет фон попытки.
  assert.equal(placed.some((title) => /старт попытки|конец попытки/.test(title)), false, 'засечка попытки попала в строку')
  const facts = scale.markers.filter((marker) => marker.type !== 'started' && marker.type !== 'ended')
  assert.equal(placed.length, facts.length, `в строках ${placed.length}, фактов ${facts.length}`)
  // Ответ человеку, пришедший в промежутке между попытками, остаётся у той, где спросили.
  const answerRow = rows.findIndex((marks) => marks.some((title) => title.includes('13:30:00') || title.includes('ответ')))
  assert.ok(answerRow >= 0, 'ответ человека не попал ни в одну строку')
})

test('разворот строки: свёрнутая высота меньше полной, маркеры видны и без наведения', () => {
  // Вызовы в одну секунду накладываются друг на друга — уровней больше одного.
  const calls = Array.from({ length: 6 }, (_, i) => ({ seq: i + 1, name: 'read_file', at: 1_000, ms: 400 }))
  const scale = shapeDealTime(
    { now: 1_100, terminal: true, timeline: [{ kind: 'created', at: 1_000, summary: '' }], stages: [{ id: 'impl', needs: [] }] },
    { attempts: [{ stage: 'impl', attempt: 1, start: 1_000, end: 1_010, status: 'done', outcome: 'done', calls }] },
  )
  const view = attemptView(scale.bars[0], scale)
  const levels = new Set(view.chips.map((chip) => chip.level))
  assert.ok(levels.size > 1, `уровней ${levels.size}: разворачивать нечего`)
  assert.ok(view.collapsed > 0 && view.collapsed < view.full, `свёрнутая ${view.collapsed}, полная ${view.full}`)
  // Свёрнутая высота — первый уровень плюс строка маркеров, иначе точки фактов
  // исчезали бы вместе с глубокими чипами.
  // 20 px — свёрнутая строка из `TL.collapsed`, 12 px — строка маркеров (`TL.marksH`).
  assert.equal(view.collapsed, 20 + (view.marks.length ? 12 : 0))
  // Уровень приходит в вид: поверхности прячут глубокие чипы, не считая сами.
  assert.deepEqual([...new Set(view.chips.map((chip) => chip.level))].sort(), [...levels].sort())
})

test('граф спеки: провал попытки этапа виден, даже если этап потом прошли', () => {
  const scale = shapeDealTime(
    {
      now: 1_600,
      terminal: true,
      timeline: [{ kind: 'created', at: 1_000, summary: '' }],
      stages: [
        { id: 'impl', role: 'implementer', state: 'done', needs: [], attempts: [{ attempt: 1, outcome: 'done' }] },
        { id: 'check', role: 'reviewer', state: 'done', needs: ['impl'], attempts: [{ attempt: 1, outcome: 'failed' }, { attempt: 2, outcome: 'done' }] },
      ],
    },
    {},
  )
  const check = scale.spec.nodes.find((node) => node.id === 'check')
  const impl = scale.spec.nodes.find((node) => node.id === 'impl')
  assert.equal(check.fails, 1, 'провал не посчитан')
  assert.equal(check.failsText, 'провалов 1')
  assert.equal(impl.fails, 0)
  // Тон узла остаётся состоянием этапа: провал — отдельная отметка, а не подмена.
  assert.equal(check.tone, 'green')
})

test('легенда инструментов: семейства с числами вызовов, порядок постоянный', () => {
  const scale = shapeDealTime(
    { now: 1_400, terminal: true, timeline: [{ kind: 'created', at: 1_000, summary: '' }], stages: [{ id: 'impl', needs: [] }] },
    {
      attempts: [
        {
          stage: 'impl',
          attempt: 1,
          started_at: 1_000,
          ended_at: 1_400,
          status: 'done',
          calls: [
            { seq: 1, name: 'read_file', at: 1_010, ms: 40 },
            { seq: 2, name: 'read_file', at: 1_020, ms: 30 },
            { seq: 3, name: 'search_files', at: 1_030, ms: 50 },
            { seq: 4, name: 'terminal', at: 1_040, ms: 60 },
            { seq: 5, name: 'skill_view', at: 1_050, ms: 20 },
          ],
        },
      ],
    },
  )
  // Семейства идут в порядке константы, а не по частоте: легенда не прыгает.
  assert.deepEqual(
    scale.tools.map((item) => [item.tone, item.count]),
    [
      ['read', 2],
      ['search', 1],
      ['run', 1],
    ],
  )
  // Инструмент без семейства в легенду не попадает: цвет у него значил бы «не знаю».
  assert.equal(scale.tools.some((item) => item.tone === 'other'), false)
  // Сумма по легенде плюс неизвестные = все вызовы дела.
  const known = scale.tools.reduce((sum, item) => sum + item.count, 0)
  const all = scale.bars.reduce((sum, bar) => sum + bar.calls.length, 0)
  assert.equal(known, all - 1, `в легенде ${known}, вызовов ${all}`)
})

test('ось: шаг линейки растёт с длиной дела, метки не липнут к краям', () => {
  const long = shapeDealTime(
    { now: 9000, terminal: false, timeline: [{ kind: 'created', at: 1000, summary: '' }], stages: [] },
    { attempts: [{ stage: 'impl', attempt: 1, started_at: 1000, ended_at: 9000, status: 'done' }] },
  )
  // 8000 с > часа: шаг 10 минут, промежуточные метки — через 600 с.
  assert.equal(long.axis.spanSec, 8000)
  const middles = long.axis.ticks.slice(1, -1)
  assert.ok(middles.length >= 2 && middles.length <= 8, `меток между краями: ${middles.length}`)
  for (const tick of middles) assert.ok(tick.pct > 5 && tick.pct < 95, `метка на краю: ${tick.pct}`)

  const short = shapeDealTime(
    { now: 1500, terminal: false, timeline: [{ kind: 'created', at: 1000, summary: '' }], stages: [] },
    { attempts: [{ stage: 'impl', attempt: 1, started_at: 1000, ended_at: 1400, status: 'done' }] },
  )
  // 500 с: шаг 2 минуты → метки не чаще, чем каждые 120 с.
  const secOfDay = (clock) => {
    const [h, m, sec] = clock.split(':').map(Number)
    return h * 3600 + m * 60 + sec
  }
  const shortTicks = short.axis.ticks.slice(1, -1)
  assert.equal(shortTicks.length, 4, `меток между краями: ${shortTicks.length}`)
  for (let i = 1; i < shortTicks.length; i += 1) {
    // шаг деления — две минуты, а не «как получилось»
    assert.equal(secOfDay(shortTicks[i].clock) - secOfDay(shortTicks[i - 1].clock), 120)
  }
})

test('шкала времени: смещение полосы — от начала оси, а не от нуля', () => {
  const scale = shapeDealTime(TIME_DETAIL, TIME_ATTEMPTS)
  // ось: начало — первое событие, конец — `now` живого дела
  assert.equal(scale.axis.start, 1000)
  assert.equal(scale.axis.end, 1500)
  assert.equal(scale.axis.spanSec, 500)
  // Ось — линейка: края подписаны словами, между ними метки шагом оси.
  const ticks = scale.axis.ticks
  assert.equal(ticks[0].edge, 'нач.')
  assert.equal(ticks[ticks.length - 1].edge, 'кон.')
  assert.deepEqual(
    ticks.map((tick) => tick.pct),
    [0, 16, 40, 64, 88, 100],
  )
  // промежуточные метки — без слова, только время
  assert.deepEqual(
    ticks.slice(1, -1).map((tick) => tick.edge),
    ['', '', '', ''],
  )
  // offsets — проценты от общей оси дела, а не от собственной длительности
  assert.deepEqual(
    scale.bars.map((bar) => [bar.leftPct, bar.widthPct]),
    [
      [12, 24],
      [24, 24],
      [50, 50],
      [48, 2],
    ],
  )
  // нулевая длительность не исчезает: минимальная ширина и подпись `<1с`
  assert.equal(scale.bars[3].durationSec, 0)
  assert.equal(scale.bars[3].durationText, '<1с')
  // живая попытка тянется до `now` и называется по лизингу
  assert.equal(scale.bars[2].open, true)
  assert.equal(scale.bars[2].endAt, 1500)
  assert.equal(scale.bars[2].leaseText, 'жив')
  assert.equal(scale.bars[2].durationText, 'идёт')
  assert.equal(scale.bars[2].tone, 'blue')
  // подписи с абсолютным временем — у каждой полосы
  assert.match(scale.bars[0].title, /impl · попытка #1 · \d\d:\d\d:\d\d–\d\d:\d\d:\d\d \(2 мин\)/)
  assert.equal(scale.bars[0].ariaLabel, scale.bars[0].title)
})

test('шкала времени: перекрывающиеся этапы — на разных дорожках, дорожек по числу этапов', () => {
  const scale = shapeDealTime(TIME_DETAIL, TIME_ATTEMPTS)
  assert.deepEqual(
    scale.lanes.map((lane) => [lane.key, lane.count]),
    [
      ['impl', 2],
      ['verify', 2],
    ],
  )
  assert.deepEqual(
    scale.bars.map((bar) => bar.lane),
    [0, 1, 0, 1],
  )
  // перекрытие видно глазом: impl идёт 1060–1180, verify — 1120–1240 на своей дорожке
  assert.ok(scale.bars[1].leftPct < scale.bars[0].endPct, 'второй этап должен начинаться до конца первого')
  assert.equal(scale.facts.attempts.total, 4)
  assert.equal(scale.facts.attempts.text, 'impl 2 · verify 2')
  assert.equal(scale.facts.attempts.journal, false)
})

test('шкала времени: маркеры — только факты журнала, вопрос без ответа ждёт «сейчас»', () => {
  const scale = shapeDealTime(TIME_DETAIL, TIME_ATTEMPTS)
  assert.equal(scale.markers.length, 8)
  assert.deepEqual(
    scale.markers.filter((marker) => !marker.tick).map((marker) => marker.type),
    ['question', 'answer', 'published', 'question'],
  )
  assert.deepEqual(
    scale.markers.filter((marker) => marker.tick).map((marker) => marker.type),
    ['started', 'started', 'ended', 'ended'],
  )
  const waiting = scale.markers.filter((marker) => marker.waitText)
  assert.equal(waiting.length, 1)
  assert.equal(waiting[0].type, 'question')
  assert.equal(waiting[0].waitText, 'ждёт ответа')
  assert.equal(waiting[0].pct, 68)
  assert.equal(waiting[0].waitPct, 32)
  // тултип — `summary` события, а не придуманная фраза
  assert.match(scale.markers[3].title, /вопрос человеку — \{"question_id": 7\}/)
  assert.equal(scale.counts.markers, 8)
  assert.equal(scale.counts.lanes, 2)
})

test('шкала времени: событие без маркера — не маркер (телеметрия попытки не считается)', () => {
  const quiet = shapeDealTime(
    { now: 2000, terminal: true, timeline: [{ kind: 'attempt_progress', at: 1500, summary: 'tool_use' }] },
    { attempts: [] },
  )
  assert.equal(quiet.markers.length, 0)
  // ось есть — по ней видно, что дело закрыто и событие одно
  assert.equal(quiet.axis.start, 1500)
})

test('шкала времени: пустой timeline и пустые попытки не роняют модель', () => {
  const none = shapeDealTime({ now: 1000, terminal: true, timeline: [], budget: null, published: null }, null)
  assert.equal(none.empty, true)
  assert.equal(none.axis.start, null)
  assert.deepEqual(none.axis.ticks, [])
  assert.equal(none.facts.duration.text, '—')
  assert.equal(none.facts.questions.text, '0 вопросов')
  assert.equal(none.facts.attempts.total, 0)
  assert.equal(none.facts.budget, null)
  assert.equal(none.facts.published, null)
  assert.deepEqual(none.counts, { bars: 0, lanes: 0, markers: 0 })
  assert.equal(shapeDealTime(null, null).empty, true)
})

test('шкала времени: закрытие и публикация — из журнала, если сервер не отдал поля', () => {
  // Сервер старше 0.0.5 не отдаёт `published`/`closed`/`terminal` — те же факты
  // лежат событиями журнала, и экран не имеет права показывать «—» там, где
  // движок уже всё записал.
  const scale = shapeDealTime(
    {
      now: 9999,
      timeline: [
        ...TIME_DETAIL.timeline,
        { kind: 'completed', at: 1400, summary: '{"cause": "all_stages_done", "stages": ["impl", "verify"]}' },
      ],
    },
    { attempts: [] },
  )
  assert.equal(scale.live, false)
  assert.equal(scale.axis.end, 1400)
  assert.equal(scale.facts.closed.text, `закрыто ${scale.facts.closed.clock}: all_stages_done`)
  assert.equal(scale.facts.published.shaShort, 'f750c785')
  assert.equal(scale.facts.published.text, 'f750c785')
  assert.deepEqual(
    scale.markers.map((marker) => marker.type).slice(-1),
    ['closed'],
  )
})

test('шкала времени: закрытое дело не тянет ось до «сейчас»', () => {
  const closed = shapeDealTime(
    { ...TIME_DETAIL, now: 9999, terminal: true, closed: { at: 1330, cause: 'all_stages_done' } },
    { ...TIME_ATTEMPTS, attempts: TIME_ATTEMPTS.attempts.slice(0, 2) },
  )
  assert.equal(closed.live, false)
  assert.equal(closed.axis.end, 1340)
  assert.equal(closed.facts.duration.live, false)
  assert.equal(closed.facts.closed.text, `закрыто ${closed.facts.closed.clock}: all_stages_done`)
  // бюджет и публикация — блоками ответа, без своего пересчёта
  assert.equal(closed.facts.budget.text, 'токенов 1 500 / 6 000 (25%)')
  assert.equal(closed.facts.published.text, 'f750c785 · release/pub · git ls-remote')
})

test('шкала времени: дорожка на каждый этап графа — в том числе неначатый', () => {
  const scale = shapeDealTime(
    { ...TIME_DETAIL, stages: [{ id: 'impl' }, { id: 'verify' }, { id: 'gate' }] },
    TIME_ATTEMPTS,
  )
  // дорожек столько же, сколько этапов: `gate` не начинался — дорожка пустая
  assert.deepEqual(
    scale.lanes.map((lane) => [lane.key, lane.count]),
    [
      ['impl', 2],
      ['verify', 2],
      ['gate', 0],
    ],
  )
  // в шапке — только этапы с попытками: «gate 0» читалось бы как сломанный этап
  assert.equal(scale.facts.attempts.text, 'impl 2 · verify 2')
  assert.deepEqual(
    scale.bars.map((bar) => bar.lane),
    [0, 1, 0, 1],
  )
})

test('шкала времени: числа шапки — из timeline (попытки, вопросы, длительность)', () => {
  const scale = shapeDealTime(TIME_DETAIL, TIME_ATTEMPTS)
  assert.equal(scale.facts.duration.sec, 500)
  assert.equal(scale.facts.duration.text, '8 мин')
  assert.equal(scale.facts.duration.live, true)
  assert.deepEqual(scale.facts.questions, {
    asked: 2,
    answered: 1,
    waiting: 1,
    text: '2 вопроса · 1 ждёт ответа',
  })
})

test('шкала времени: пустой ответ попыток — полосы из пар журнала, без выдумки', () => {
  const scale = shapeDealTime(TIME_DETAIL, { attempts: [], crash_outcomes: [] })
  assert.equal(scale.facts.attempts.journal, true)
  assert.deepEqual(
    scale.bars.map((bar) => [bar.stage, bar.leftPct, bar.widthPct]),
    [
      ['impl', 12, 24],
      ['verify', 24, 24],
    ],
  )
  // гейт и отказ по бюджету пишутся без `attempt_started` — полосой они не становятся
  const gate = shapeDealTime(
    {
      now: 1200,
      terminal: true,
      timeline: [
        { kind: 'attempt_started', at: 1000, summary: '{"run_id": "review#1", "stage": "review"}' },
        { kind: 'attempt_ended', at: 1100, summary: 'гейт: ответ «публиковать» входит в allow [публиковать]' },
        { kind: 'attempt_ended', at: 1150, summary: 'бюджет дела исчерпан: 1000/1000 токенов' },
      ],
    },
    { attempts: [] },
  )
  assert.equal(gate.bars.length, 1)
  assert.deepEqual(
    gate.markers.map((marker) => marker.type),
    ['started', 'verdict', 'budget'],
  )
  // Исход пары берётся из выжимки `attempt_ended`, а не выдумывается по цвету
  const ended = shapeDealTime(
    {
      now: 1200,
      terminal: true,
      timeline: [
        { kind: 'attempt_started', at: 1000, summary: '{"run_id": "impl#1", "stage": "impl", "attempt": 1}' },
        { kind: 'attempt_ended', at: 1100, summary: '{"run_id": "impl#1", "outcome": "done", "kind": null}' },
      ],
    },
    { attempts: [], crash_outcomes: ['crashed'] },
  )
  assert.equal(ended.bars[0].outcome, 'done')
  assert.equal(ended.bars[0].tone, 'green')
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
  const withStage = shapeDeal({ repo: 'r', id: 'd_1', stage: { id: 'impl', attempt: 2, state: 'running', live: true } })
  assert.equal(withStage.stageText, 'impl · попытка 2 · идёт')
  assert.equal(withStage.stageId, 'impl')
  assert.equal(withStage.stageAttempt, 2)
  // без попыток строки нет: «этап 0» был бы выдуманным графом
  assert.equal(shapeDeal({ repo: 'r', id: 'd_2' }).stageText, '')
})

/* ----------------------------------- экран «Завершённые» (0.0.7) ---------- */

// Ответ `/deals` по одному закрытому делу: те же факты, что в
// `tests/test_finished.py::test_finished_row_numbers_match_the_deal_card` —
// создано 1000, закрыто 1700, попытка `verify` дожила до 1800.
const FINISHED_ROW = {
  id: 'd_a7040118',
  repo: '~/.hermes/plugins/hermes-duty',
  title: 'карточки вон из /stats и экран завершённых',
  assignee: 'implementer',
  status: 'done',
  created_at: 1000,
  finished_at: 1700,
  duration_sec: 800,
  cause: 'all_stages_done',
  stages: ['impl', 'verify'],
  attempts: 2,
  questions: { asked: 2, answered: 1, waiting: 1 },
  published: {
    sha: 'f750c7850a706b089ff3ea783c86cc9f8d0a791a',
    branch: 'release/v0.0.7',
    verified_by: 'git ls-remote',
    mr_url: 'https://gitlab.example/mr/7',
  },
}

// Карточка того же дела: `/deal` + `/duty/{id}/attempts`. Шкала считает ось
// `1000 … 1800` (конец попытки позже закрытия), то есть 800 с — как строка.
const FINISHED_DETAIL = {
  now: 1900,
  status: 'done',
  terminal: true,
  timeline: [
    { kind: 'created', at: 1000, summary: 'ready' },
    { kind: 'attempt_started', at: 1100, summary: '{"run_id": "impl#1", "stage": "impl", "attempt": 1}' },
    { kind: 'attempt_ended', at: 1200, summary: 'impl: готово' },
    { kind: 'attempt_started', at: 1300, summary: '{"run_id": "verify#1", "stage": "verify", "attempt": 1}' },
    { kind: 'question_asked', at: 1350, summary: '{"question_id": 7}' },
    { kind: 'question_answered', at: 1360, summary: '{"question_id": 7, "answer_len": 2}' },
    { kind: 'question_asked', at: 1370, summary: '{"question_id": 8}' },
    { kind: 'published', at: 1500, summary: '{"sha": "f750c7850a706b089ff3ea783c86cc9f8d0a791a"}' },
    { kind: 'completed', at: 1700, summary: '{"cause": "all_stages_done"}' },
  ],
  closed: { at: 1700, cause: 'all_stages_done', stages: ['impl', 'verify'], status: 'done' },
  published: { sha: 'f750c7850a706b089ff3ea783c86cc9f8d0a791a', branch: 'release/v0.0.7', verified_by: 'git ls-remote' },
}

const FINISHED_ATTEMPTS = {
  attempts: [
    { run_id: 'impl#1', stage: 'impl', attempt: 1, status: 'completed', start: 1100, end: 1200, duration_sec: 100, outcome: 'done' },
    { run_id: 'verify#1', stage: 'verify', attempt: 1, status: 'failed', start: 1300, end: 1800, duration_sec: 500, outcome: 'crashed' },
  ],
}

test('завершённые: числа строки — те же, что у шкалы карточки дела', () => {
  // Красная проба: подставить в строку свой расчёт (длительность
  // `created_at → finished_at` = 700, вопросы разностью, «попыток» числом
  // событий) — числа разъедутся с карточкой того же дела, и тест упадёт.
  const row = shapeFinished({ deals: [FINISHED_ROW], total: 1, limit: 50 }).deals[0]
  const scale = shapeDealTime(FINISHED_DETAIL, FINISHED_ATTEMPTS)

  assert.equal(row.duration, scale.facts.duration.sec)
  assert.equal(row.duration, 800)
  assert.notEqual(row.duration, row.finishedAt - row.createdAt)
  assert.equal(row.attempts, scale.facts.attempts.total)
  assert.deepEqual(row.questions, scale.facts.questions)
  assert.equal(row.questions.text, '2 вопроса · 1 ждёт ответа')
  assert.equal(row.published.text, scale.facts.published.text)
  assert.equal(row.causeText, 'all_stages_done')
  assert.equal(row.stagesText, 'impl → verify')
  assert.equal(row.finishedText, scale.facts.closed.clock)
  assert.equal(row.statusLabel, 'завершено')
  assert.equal(row.tone, 'green')
})

test('завершённые: группы по repo — свежие внутри, «без репозитория» последней', () => {
  const modelFinished = shapeFinished({
    deals: [
      { ...FINISHED_ROW, id: 'd_1', repo: 'b-repo', finished_at: 1700 },
      { ...FINISHED_ROW, id: 'd_2', repo: 'a-repo', finished_at: 1600 },
      { ...FINISHED_ROW, id: 'd_3', repo: 'b-repo', finished_at: 1500 },
      { ...FINISHED_ROW, id: 'd_4', repo: null, finished_at: 1400 },
    ],
    count: 4,
    total: 4,
    limit: 50,
    statuses: ['done', 'archived'],
    note: '',
  })

  // Группы — как в CLI (`_repo_groups`): названные по имени, без репозитория —
  // последней. Внутри группы порядок ответа (свежие сверху) не пересортировывается.
  assert.deepEqual(
    modelFinished.groups.map((group) => [group.label, group.deals.map((deal) => deal.id)]),
    [
      ['a-repo', ['d_2']],
      ['b-repo', ['d_1', 'd_3']],
      [WITHOUT_REPO_LABEL, ['d_4']],
    ],
  )
  assert.equal(modelFinished.groups[2].repo, '')
  assert.equal(modelFinished.count, 4)
})

test('завершённые: пусто — словами бэкенда, архивное дело — без выдуманной причины', () => {
  const empty = shapeFinished({ deals: [], total: 0, limit: 50, note: 'завершённых дел нет: в store ни одного дела в статусе done, archived' })
  assert.equal(empty.count, 0)
  assert.deepEqual(empty.groups, [])
  // Причина пустоты — с бэкенда: «дел нет» и «store не прочитался» разные факты.
  assert.match(empty.note, /завершённых дел нет/)
  assert.deepEqual(shapeFinished(null), {
    now: null,
    deals: [],
    groups: [],
    count: 0,
    total: 0,
    limit: 0,
    truncated: false,
    statuses: [],
    note: '',
    errors: [],
  })

  const archived = shapeFinished({
    deals: [{ ...FINISHED_ROW, status: 'archived', cause: '', stages: [], finished_at: null, duration_sec: null, published: null, questions: { asked: 0, answered: 0, waiting: 0 } }],
  }).deals[0]
  assert.equal(archived.statusLabel, 'в архиве')
  assert.equal(archived.tone, 'gray')
  assert.equal(archived.causeText, 'причина не записана')
  assert.equal(archived.stagesText, '—')
  assert.equal(archived.finishedText, '—')
  assert.equal(archived.durationText, '—')
  assert.equal(archived.published, null)
  assert.equal(archived.questions.text, '0 вопросов')
})

test('завершённые: лимит и обрезка — из ответа, а не из своего счёта', () => {
  const modelFinished = shapeFinished({
    deals: [FINISHED_ROW],
    count: 1,
    total: 36,
    limit: 1,
    truncated: true,
    statuses: ['done', 'archived'],
  })
  assert.equal(modelFinished.count, 1)
  assert.equal(modelFinished.total, 36)
  assert.equal(modelFinished.limit, 1)
  assert.equal(modelFinished.truncated, true)
  assert.deepEqual(modelFinished.statuses, ['done', 'archived'])
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
  assert.match(pluginSource, /\/duty\/\$\{encodeURIComponent\(dealId\)\}\/attempts/)
  assert.match(pluginSource, /rest\(`\/deal\/\$\{encodeURIComponent\(dealId\)\}`/)
  // старых адресов и досок в поверхностях не осталось
  for (const [name, text] of [['model.js', modelSource], ['plugin.js', pluginSource]]) {
    assert.equal(/\bboard\b|\bдоск/i.test(text), false, `${name}: доски больше нет`)
  }
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


/* --------------------------------- вызовы инструментов и их раскладка (0.0.8) */

test('вызовы: длительность остаётся null, если результат не пришёл', () => {
  const calls = shapeCalls([
    { seq: 1, name: 'read_file', at: 1000, ms: 412 },
    { seq: 3, name: 'terminal', at: 1010, ms: null },
    { name: 'мусор' },
    'мусор',
  ])
  assert.equal(calls.length, 2)
  assert.equal(calls[0].name, 'read_file')
  assert.equal(calls[0].msText, '<1с')
  assert.match(calls[1].msText, /неизвестна/)
  assert.equal(calls[1].clock.length > 0, true)
  assert.equal(shapeCalls(null).length, 0)
})

test('раскладка вызовов: ширина растёт с длительностью, уровень не пересекается', () => {
  const calls = shapeCalls([
    { seq: 1, name: 'a', at: 0, ms: 50 },
    { seq: 3, name: 'b', at: 0.2, ms: 100 },
    { seq: 5, name: 'c', at: 5, ms: 11000 },
  ])
  const layout = layoutCalls(calls, { from: 0, to: 10, width: 700, levelH: 12, top: 6 })
  assert.equal(layout.chips.length, 3)
  // Вызовы разнесены по оси — одним уровнем и обходимся: лишние уровни означали
  // бы, что дорожка растёт вниз без причины.
  assert.equal(layout.levels, 1)
  assert.equal(layout.height, 6 + layout.levels * 12)
  const widths = layout.chips.map((chip) => chip.width)
  assert.ok(widths[2] > widths[1], 'медленный вызов шире быстрого')
  assert.ok(widths[1] >= widths[0])
  // Соседи по уровню не наезжают друг на друга
  for (const level of new Set(layout.chips.map((chip) => chip.level))) {
    const row = layout.chips.filter((chip) => chip.level === level)
    for (let i = 1; i < row.length; i += 1) {
      assert.ok(row[i].left >= row[i - 1].left + row[i - 1].width - 0.01, 'чипы одного уровня перекрылись')
    }
  }
})

test('раскладка вызовов: вызовы в одну секунду расходятся по уровням, а не липнут', () => {
  const calls = shapeCalls(
    Array.from({ length: 6 }, (_, index) => ({ seq: index * 2 + 1, name: 'read_file', at: 5 + index * 0.05, ms: 120 })),
  )
  const layout = layoutCalls(calls, { from: 0, to: 10, width: 700, levelH: 12, top: 6 })
  assert.equal(layout.chips.length, 6)
  assert.ok(layout.levels > 1, 'шесть вызовов в 0.3 с не влезают в один уровень')
  assert.equal(layout.clipped, 0, 'пока уровней хватает, наложений быть не должно')
})

test('раскладка вызовов: потолок уровней не теряет вызовы, а считает наложения', () => {
  const calls = shapeCalls(
    Array.from({ length: 20 }, (_, index) => ({ seq: index * 2 + 1, name: 'read_file', at: index * 0.01, ms: 10 })),
  )
  const layout = layoutCalls(calls, { from: 0, to: 10, width: 700, maxLevels: 2, levelH: 12, top: 6 })
  assert.equal(layout.chips.length, 20)
  assert.equal(layout.levels, 2)
  assert.ok(layout.clipped > 0)
})

test('раскладка вызовов: без оси рисовать нечего, а ширина — виртуальная', () => {
  const calls = shapeCalls([{ name: 'a', at: 1, ms: 5 }])
  assert.deepEqual(layoutCalls([], { from: 0, to: 10, width: 700 }).chips, [])
  // Нет `from`/`to` — нет шкалы: пустая картинка честнее выдуманной оси.
  assert.deepEqual(layoutCalls(calls, { width: 700 }).chips, [])
  assert.deepEqual(layoutCalls(calls, { from: 5, to: 5 }).chips, [])
  // Ширина не задана — считается в виртуальной (1000): на экран уходят доли.
  const chips = layoutCalls(calls, { from: 0, to: 10 }).chips
  assert.equal(chips.length, 1)
  assert.equal(chips[0].leftPct, 10)
  assert.ok(chips[0].widthPct > 0 && chips[0].widthPct < 2)
})


/* ------------------------------------- вид таймлайна: общее для двух поверхностей */

/** Попытка в форме шейпера: минимум полей, которые читает вид. */
function bar(calls, extra = {}) {
  return {
    number: '#1',
    tone: 'green',
    open: false,
    outcomeText: 'готово',
    clockStart: '13:38:02',
    title: 'check · попытка #1',
    calls: shapeCalls(calls || []),
    callsOmitted: 0,
    ...extra,
  }
}

const axis = { axis: { start: 0, end: 10 } }

test('обрезанные вызовы видны на экране, а не только в данных', () => {
  const view = attemptView(bar([{ name: 'read_file', at: 1, ms: 100 }], { callsOmitted: 3 }), axis)
  // Нарисовано ровно то, что пришло; остальное названо надбавкой.
  assert.equal(view.chips.length, 1)
  assert.equal(view.badge, '+3')
  assert.match(view.note, /ещё 3 вызова не показано/)
  assert.match(view.title, /не показано/)
})

test('вызов без результата ≠ вызов длиной 0 мс', () => {
  const unfinished = attemptView(bar([{ name: 'terminal', at: 1, ms: null }]), axis)
  const instant = attemptView(bar([{ name: 'terminal', at: 1, ms: 0 }]), axis)
  assert.equal(unfinished.chips[0].unfinished, true)
  assert.equal(instant.chips[0].unfinished, false)
  assert.notEqual(unfinished.chips[0].widthPct, instant.chips[0].widthPct)
})

test('попытка без поля calls не роняет вид', () => {
  const broken = bar([])
  delete broken.calls
  const view = attemptView(broken, axis)
  assert.deepEqual(view.chips, [])
  assert.deepEqual(view.ticks, [])
  assert.equal(view.badge, '')
})

test('нехватка уровней названа, а не спрятана', () => {
  // Двенадцать вызовов в одном и том же месте: уровней пять, остальные лягут
  // друг на друга — и это число обязано быть на экране.
  const calls = Array.from({ length: 12 }, (_, index) => ({ name: 'read_file', at: 1, ms: 100 + index }))
  const view = attemptView(bar(calls), axis)
  assert.match(view.badge, /⋯/)
  assert.match(view.note, /уровней не хватило/)
})

test('сводка дорожки считает всё, включая невидимое', () => {
  const bars = [
    bar([{ name: 'read_file', at: 1, ms: 100 }], { callsOmitted: 4 }),
    bar([{ name: 'patch', at: 2, ms: 200 }], { number: '#2' }),
  ]
  const view = laneView({ label: 'review' }, bars, axis)
  // Шесть вызовов было, показано два: сводка называет оба числа.
  assert.match(view.meta, /6 вызовов \(показано 2\)/)
  assert.match(view.meta, /2 попытки/)
  assert.equal(view.rows.length, 2)
  assert.equal(view.kind, 'agent')
  assert.match(view.meta, /попытки/)
})

test('склонение одно на обе поверхности', () => {
  const three = laneView({ label: 'check' }, [bar([]), bar([]), bar([])], axis)
  assert.match(three.meta, /3 попытки/)
  const many = laneView({ label: 'check' }, Array.from({ length: 5 }, () => bar([])), axis)
  assert.match(many.meta, /5 попыток/)
  assert.doesNotMatch(three.meta, /попыток/)
})

test('пустая дорожка говорит об этом словами, а не пустотой', () => {
  const view = laneView({ label: 'verify' }, [], axis)
  assert.equal(view.empty, true)
  assert.equal(view.icon, '')
  assert.deepEqual(view.rows, [])
})

test('обе поверхности берут геометрию и склонения из шейпера', () => {
  const shaper = readFileSync(new URL('../desktop/model.js', import.meta.url), 'utf8')
  for (const [name, file] of [
    ['веб', '../dashboard/web/app.js'],
    ['десктоп', '../desktop/plugin.js'],
  ]) {
    const raw = readFileSync(new URL(file, import.meta.url), 'utf8')
    // У десктопа шейпер вшит блоком `duty:model-*` — там свои константы законны.
    // Проверяем то, что написано руками ВОКРУГ блока.
    const text = raw.replace(/\/\/ duty:model-start[\s\S]*?\/\/ duty:model-end/, '')
    assert.ok(!/^const TL_/m.test(text), `${name}: своя геометрия TL_* вместо шейпера`)
    assert.ok(!/const TOOL_TONE/.test(text), `${name}: своя палитра инструментов`)
    assert.ok(!/function plural\(/.test(text), `${name}: своё склонение вместо plural из шейпера`)
    assert.ok(/attemptView|laneView/.test(text), `${name}: вид таймлайна не из шейпера`)
  }
  assert.ok(/function attemptView\(/.test(shaper))
})
