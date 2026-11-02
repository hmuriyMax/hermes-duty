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
  filterNow,
  fmtAge,
  fmtTitle,
  groupDeals,
  shapeAttempts,
  shapeDeal,
  shapeNow,
  stateMeta,
} from '../desktop/model.js'

// Ожидаемый порядок приоритета — это ожидание теста к ответу бэкенда
// (WAITING_PRIORITY в dashboard/plugin_api.py), а не константа страницы.
const PRIORITY = ['wait_human', 'capability', 'dead_lease', 'crashed', 'working', 'wait_slot', 'wait_gate']

const nowPayload = JSON.parse(readFileSync(new URL('./fixtures/now.json', import.meta.url), 'utf8'))
const attemptsPayload = JSON.parse(readFileSync(new URL('./fixtures/attempts.json', import.meta.url), 'utf8'))
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
    [1, 1, 1, 1, 1, 1, 1],
  )
  assert.deepEqual(Object.keys(model.byState).sort(), [...PRIORITY].sort())
  assert.equal(model.count, 7)
  // чипы фильтра перечисляют те же состояния в том же порядке — только подписи, без правил
  assert.deepEqual(Object.keys(STATE_META), PRIORITY)
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
  assert.equal(stateMeta('wait_slot').tone, 'gray')
  assert.equal(stateMeta('wait_gate').tone, 'gray')
  assert.equal(stateMeta('').tone, 'gray')
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
  assert.equal(byBoard.count, 2)
  assert.deepEqual(
    byBoard.groups.map((group) => group.state),
    ['capability', 'crashed'],
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
  assert.equal(filterNow(model, {}).count, 7)
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
