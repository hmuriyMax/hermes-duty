/**
 * Регрессия веб-сборки плагина: четыре экрана и одна копия шейпера.
 *
 *   node --test tests/web.test.mjs
 *
 * Проверяется ровно то, что ломается тихо: страница остаётся на месте, шейпер
 * импортируется (а не копируется в веб — иначе бейджи разъезжаются с десктопом),
 * маршруты четырёх экранов существуют, токен вшивается маркером, а порт backend
 * в файлах не захардкожен (страница обязана ходить по относительным путям).
 */

import assert from 'node:assert/strict'
import { readFileSync, existsSync } from 'node:fs'
import { test } from 'node:test'

const root = new URL('..', import.meta.url)
const read = (path) => readFileSync(new URL(path, root), 'utf8')

const html = read('dashboard/web/index.html')
const app = read('dashboard/web/app.js')
const api = read('dashboard/plugin_api.py')
const pluginSource = read('desktop/plugin.js')

test('веб-сборка на месте: html, модуль и стили', () => {
  for (const file of ['dashboard/web/index.html', 'dashboard/web/app.js', 'dashboard/web/styles.css']) {
    assert.ok(existsSync(new URL(file, root)), `${file} отсутствует`)
  }
})

test('токен вшивается маркером, а вход — модулем ./app.js', () => {
  assert.match(html, /<!--__HERMES_TOKEN__-->/)
  assert.match(html, /<script type="module" src="\.\/app\.js/)
})

test('шейпер один на две поверхности: веб импортирует model.js, а не копию', () => {
  assert.match(app, /from '\.\/model\.js'/)
  // копия таксономии в вебе — это дрейф: STATE_META живёт только в model.js
  assert.ok(!app.includes('wait_human:'), 'в app.js появилась своя таблица состояний')
})

test('бейдж дела в вебе подписывается по human_kind, а не по одному состоянию', () => {
  // «ждёт ответа» и «ждёт решения» — два разных ожидания человека, и подпись
  // обязана различать их на обеих поверхностях (шейпер общий).
  assert.match(app, /function Badge\(state, humanKind, label\)/)
  assert.match(app, /stateMeta\(state, humanKind\)/)
  assert.ok(
    !/Badge\(deal\.state\)/.test(app),
    'бейдж дела без human_kind: блокировка без вопроса снова скажет «ждёт ответа»',
  )
})

test('четыре экрана маршрутизируются', () => {
  assert.match(app, /parts\[0\] === 'deal'/)
  assert.match(app, /parts\[0\] === 'why'/)
  assert.match(app, /parts\[0\] === 'health'/)
  assert.match(app, /return \{ name: 'now' \}/)
  // «Дело» — таб, а не только следствие клика по строке: экран, до которого
  // нельзя дойти из навигации, на своём месте не читается.
  for (const tab of ['now', 'deal', 'why', 'health']) {
    assert.match(html, new RegExp(`data-tab="${tab}"`), `нет вкладки ${tab}`)
  }
})

test('«Дело» без выбранного дела показывает выбор, а не пустоту', () => {
  assert.match(app, /route\.board \? viewDeal\(route\.board, route\.id\) : viewDealPicker\(\)/)
  assert.match(app, /function viewDealPicker\(\)/)
  assert.match(app, /localStorage\.setItem\(LAST_DEAL/)
})

test('бэкенд монтирует страницу и отдаёт общий шейпер', () => {
  assert.ok(api.includes('"/web/hermes-duty"'), 'нет роута /web/hermes-duty')
  assert.ok(api.includes('"/web/hermes-duty/model.js"'), 'нет роута шейпера')
  assert.ok(api.includes('"/web/hermes-duty/{path:path}"'), 'нет статики')
})

test('«N вопросов + M блокировок»: счётчик шапки берётся из шейпера, а не из своей копии', () => {
  const modelSource = read('desktop/model.js')
  // веб-шапка: пустой span, который заполняет app.js той же функцией шейпера
  assert.match(html, /id="human"/)
  assert.match(app, /humanLine\(view\.humanCounts\)/)
  // десктоп: та же строка в шапке плагина (шейпер вшит в plugin.js дословно)
  assert.match(pluginSource, /humanLine\(view\.humanCounts\)/)
  // таблица форм числа и сама строка живут в одном месте — в шейпере
  assert.match(modelSource, /function plural\(/)
  assert.match(modelSource, /function humanLine\(/)
  assert.ok(!/function plural\(/.test(app), 'в app.js появилась своя таблица форм числа')
})

test('порт и хост backend не захардкожены в веб-сборке', () => {
  for (const [name, text] of [['app.js', app], ['index.html', html]]) {
    assert.ok(!/18742|127\.0\.0\.1|localhost:\d+/.test(text), `${name}: захардкоженный адрес`)
  }
})
