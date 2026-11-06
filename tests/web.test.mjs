/**
 * Регрессия веб-сборки плагина: пять экранов и одна копия шейпера.
 *
 *   node --test tests/web.test.mjs
 *
 * Проверяется ровно то, что ломается тихо: страница остаётся на месте, шейпер
 * импортируется (а не копируется в веб — иначе бейджи разъезжаются с десктопом),
 * маршруты пяти экранов существуют, токен вшивается маркером, а порт backend
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

test('пять экранов маршрутизируются', () => {
  assert.match(app, /parts\[0\] === 'deal'/)
  assert.match(app, /parts\[0\] === 'why'/)
  assert.match(app, /parts\[0\] === 'health'/)
  assert.match(app, /parts\[0\] === 'deals'/)
  assert.match(app, /return \{ name: 'now' \}/)
  // «Дело» — таб, а не только следствие клика по строке: экран, до которого
  // нельзя дойти из навигации, на своём месте не читается.
  for (const tab of ['now', 'deals', 'deal', 'why', 'health']) {
    assert.match(html, new RegExp(`data-tab="${tab}"`), `нет вкладки ${tab}`)
  }
})

test('«Завершённые» — список из `/deals`, числа берутся у бэкенда', () => {
  // страница ходит в тот же роут, что `bin/duty deals`, и группирует тем же шейпером
  assert.match(app, /api\('\/deals'\)/)
  assert.match(app, /shapeFinished\(finished\.data\)/)
  assert.match(pluginSource, /rest\('\/deals'/)
  assert.match(pluginSource, /shapeFinished\(finished\.data\)/)
  // второго счёта в строках нет: вопросы/попытки/длительность — поля ответа
  assert.match(app, /deal\.questions\.text/)
  assert.match(pluginSource, /deal\.questions\.text/)
  assert.ok(!/question_asked/.test(app), 'в app.js появился свой счёт вопросов')
  // пусто — словами бэкенда (`note`), а не пустым квадратом
  assert.match(app, /model\.note \|\| 'в store нет ни одного закрытого дела'/)
})

test('«Дело» без выбранного дела показывает выбор, а не пустоту', () => {
  assert.match(app, /route\.id \? viewDeal\(route\.id\) : viewDealPicker\(\)/)
  assert.match(app, /function viewDealPicker\(\)/)
  assert.match(app, /localStorage\.setItem\(LAST_DEAL, String\(dealId\)\)/)
})

test('адрес дела — только `#/deal/<deal_id>`; старый `<board>/<id>` получает отказ', () => {
  // разбор: один сегмент — дело, два — прежний адрес (доски больше нет)
  assert.match(app, /if \(parts\.length > 2\) return \{ name: 'deal', bad: true, board: /)
  assert.match(app, /if \(parts\[1\]\) return \{ name: 'deal', id: decodeURIComponent\(parts\[1\]\) \}/)
  // отказ — своим экраном, а не полусобранной карточкой
  assert.match(app, /route\.bad\) body\.append\(viewDealBadAddress\(route\.board, route\.id\)\)/)
  assert.match(app, /function viewDealBadAddress\(board, dealId\)/)
  assert.match(app, /доски больше нет/)
  // старый ключ localStorage (два сегмента) не открывается как дело
  assert.match(app, /raw\.includes\('\/'\)\) return null/)
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

test('карточка дела рисует ленту этапов из ответа /deal, а не из своего расчёта', () => {
  // один шейпер на обе поверхности: правила состояний этапа живут в model.js
  assert.match(app, /shapeStages/)
  assert.ok(!/STAGE_META\s*=\s*\{/.test(app), 'в app.js появилась своя таблица состояний этапа')
  // лента встроена в карточку дела
  assert.match(app, /function stageFeedPanel\(data\)/)
  assert.match(app, /stageFeedPanel\(data\)/)
  // причина пустоты приходит с бэкенда (`stages_note`), а не выдумывается экраном
  assert.match(app, /feed\.note/)
  // вопрос ведёт в панель пула: отвечают там, а лента — read-only
  assert.match(app, /questionHref/)
})

test('лента этапов без прогресс-баров: «сколько прошло» никто не мерил', () => {
  const start = app.indexOf('function stageFeedPanel(')
  const end = app.indexOf('function attemptCard(')
  assert.ok(start > 0 && end > start, 'не найден блок ленты этапов в app.js')
  const feedSource = app.slice(start, end)
  assert.ok(!feedSource.includes("class: 'track'"), 'в ленте этапов появился прогресс-бар')
  assert.ok(!feedSource.includes('barPct'), 'в ленте этапов появился процент прогресса')
  // и ни одной кнопки, двигающей дело: экран не решает
  assert.ok(!/<button/i.test(feedSource), 'в ленте этапов появилась кнопка')
  assert.ok(!/onclick|onClick/.test(feedSource), 'лента этапов обрабатывает клики')
})

test('десктопная лента берёт те же `stages` из ctx.rest(/deal/…)', () => {
  // данные — только через ctx.rest, ни одного прямого fetch
  assert.match(pluginSource, /shapeStages\(detail\.data\)/)
  assert.match(pluginSource, /h\(StageFeed, \{ feed \}\)/)
  assert.ok(!pluginSource.includes('fetch('), 'plugin.js: прямой fetch запрещён')
  // ожидание вопроса — ссылкой на пул, как и в вебе
  assert.match(pluginSource, /href: stage\.questionHref/)
})

test('разворот строки и правки экрана дела: правила есть на обеих поверхностях', () => {
  const css = read('dashboard/web/styles.css')
  const app = read('dashboard/web/app.js')
  const plugin = read('desktop/plugin.js')

  // Разворот: свёрнутая высота, полная по наведению, глубокие чипы скрыты.
  const surfaces = [
    ['веб', css, '--tl-collapsed', '--tl-full'],
    ['десктоп', plugin, '--duty-tl-collapsed', '--duty-tl-full'],
  ]
  for (const [name, text, collapsed, full] of surfaces) {
    assert.match(text, new RegExp(`height:\\s*var\\(${collapsed}`), `${name}: нет свёрнутой высоты строки`)
    assert.match(text, new RegExp(`:hover[^{]*\\{[^}]*height:\\s*var\\(${full}`), `${name}: наведение не раскрывает строку`)
    assert.match(text, /data-level\]:not\(\[data-level="0"\]\)[^{]*\{[^}]*visibility:\s*hidden/, `${name}: глубокие чипы не прячутся`)
  }
  assert.match(app, /'data-level':\s*String\(chip\.level/, 'веб не отдаёт уровень чипа в разметку')
  assert.match(plugin, /'data-level':\s*String\(chip\.level/, 'десктоп не отдаёт уровень чипа в разметку')
  assert.match(app, /--tl-collapsed:\$\{view\.collapsed\}px;--tl-full:\$\{view\.full\}px/, 'веб не передаёт высоты из шейпера')
  assert.match(plugin, /'--duty-tl-collapsed':\s*`\$\{view\.collapsed\}px`/, 'десктоп не передаёт высоты из шейпера')

  // Маркеры — палки на всю высоту строки, а не точки на нижней кромке: точка
  // уезжала при разворачивании, потому что жила в отдельной полосе под чипами.
  for (const [name, text] of [['веб', css], ['десктоп', plugin]]) {
    assert.match(text, /tl-marks?\s*\{[^}]*inset:\s*0/, `${name}: контейнер маркеров не на всю строку`)
    assert.match(text, /tl-mark\s*\{[^}]*top:\s*-3px[^}]*bottom:\s*-3px/, `${name}: маркер не тянется по высоте`)
    assert.doesNotMatch(text, /tl-mark i\s*\{[^}]*border-radius:\s*50%/, `${name}: маркер снова точка`)
  }

  // Подсказки переносятся по словам, а не висят одной строкой.
  for (const [name, text] of [['веб', css], ['десктоп', plugin]]) {
    const rule = text.slice(text.indexOf('content:attr(data-tip)') >= 0 ? text.indexOf('content:attr(data-tip)') - 400 : text.indexOf('content: attr(data-tip)') - 400)
    assert.match(rule.slice(0, 900), /white-space:\s*normal/, `${name}: подсказка не переносится`)
    assert.match(rule.slice(0, 900), /max-width:\s*240px/, `${name}: у подсказки нет предела ширины`)
  }

  // Граф этапов — в заголовке дела, статус — в правом углу.
  assert.match(app, /deal-head-top[\s\S]{0,700}specStrip\(scale\.spec\)/, 'веб: граф не в заголовке')
  assert.match(app, /deal-head-side/, 'веб: у заголовка нет правой колонки под статус')
  assert.match(plugin, /duty-deal-head-top[\s\S]{0,700}SpecStrip/, 'десктоп: граф не в заголовке')
  assert.match(plugin, /duty-deal-head-side[\s\S]{0,400}StateBadge/, 'десктоп: статус не в правой колонке')

  // Статистика инструментов — правой колонкой от дорожек.
  assert.match(app, /class: 'scale-side'/, 'веб: нет правой колонки легенды')
  assert.match(plugin, /className: 'duty-scale-side'/, 'десктоп: нет правой колонки легенды')

  // Провал попытки отмечается на узле графа.
  assert.match(css, /\.spec-node\.has-fail rect\s*\{[^}]*var\(--danger\)/, 'веб: провал не отмечен')
  assert.match(plugin, /\.duty-spec-node\.has-fail rect\{[^}]*var\(--ui-red\)/, 'десктоп: провал не отмечен')
})
