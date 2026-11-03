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

test('бэкенд монтирует страницу и отдаёт общий шейпер', () => {
test('«Дело» без выбранного дела показывает выбор, а не пустоту', () => {
  assert.match(app, /route\.board \? viewDeal\(route\.board, route\.id\) : viewDealPicker\(\)/)
  assert.match(app, /function viewDealPicker\(\)/)
  assert.match(app, /localStorage\.setItem\(LAST_DEAL/)
})

  assert.ok(api.includes('"/web/hermes-duty"'), 'нет роута /web/hermes-duty')
  assert.ok(api.includes('"/web/hermes-duty/model.js"'), 'нет роута шейпера')
  assert.ok(api.includes('"/web/hermes-duty/{path:path}"'), 'нет статики')
})

test('порт и хост backend не захардкожены в веб-сборке', () => {
  for (const [name, text] of [['app.js', app], ['index.html', html]]) {
    assert.ok(!/18742|127\.0\.0\.1|localhost:\d+/.test(text), `${name}: захардкоженный адрес`)
  }
})
