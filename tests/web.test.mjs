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

test('карточка дела рисует столбец этапов из ответа /deal, а не из своего расчёта', () => {
  // один шейпер на обе поверхности: правила состояний этапа живут в model.js
  assert.match(app, /shapeStages/)
  assert.ok(!/STAGE_META\s*=\s*\{/.test(app), 'в app.js появилась своя таблица состояний этапа')
  // столбец встроен в карточку дела и собран компактным видом шейпера
  assert.match(app, /function stagesColumn\(dealId, data, timeline\)/)
  assert.match(app, /stagesColumn\(data\.id, data, timeline\)/)
  assert.match(app, /compactStageView\(stage\)/)
  assert.match(app, /compactAttemptView\(stage, bar\)/)
  // причина пустоты приходит с бэкенда (`stages_note`), а не выдумывается экраном
  assert.match(app, /feed\.note/)
  // вопрос ведёт в панель пула: отвечают там, а столбец — read-only по делу
  assert.match(app, /waitHref/)
})

test('столбец этапов без прогресс-баров: «сколько прошло» никто не мерил', () => {
  const start = app.indexOf('function stagesColumn(')
  const end = app.indexOf('function viewDeal(')
  assert.ok(start > 0 && end > start, 'не найден блок столбца этапов в app.js')
  const feedSource = app.slice(start, end)
  assert.ok(!feedSource.includes("class: 'track'"), 'в столбце этапов появился прогресс-бар')
  assert.ok(!feedSource.includes('barPct'), 'в столбце этапов появился процент прогресса')
  // и ни одной кнопки, двигающей дело: экран не решает
  assert.ok(!/<button/i.test(feedSource), 'в столбце этапов появилась кнопка')
  assert.ok(!/fetch\(/.test(feedSource), 'столбец этапов ходит в сеть сам')
  // клик только раскрывает строку: состояние раскрытия — поверхности, не делу
  for (const match of feedSource.matchAll(/onclick: \(\) => \{([\s\S]*?)\}/g)) {
    assert.match(match[1], /(stageOpen|attOpen)\.set\(/, `клик по строке делает не только раскрытие: ${match[1].trim()}`)
  }
})

test('десктопный столбец берёт те же `stages` из ctx.rest(/deal/…)', () => {
  // данные — только через ctx.rest, ни одного прямого fetch
  assert.match(pluginSource, /shapeStages\(detail\.data\)/)
  assert.match(pluginSource, /h\(CompactStages, \{ feed, timeline \}\)/)
  assert.ok(!pluginSource.includes('fetch('), 'plugin.js: прямой fetch запрещён')
  // ожидание вопроса — ссылкой на пул, как и в вебе
  assert.match(pluginSource, /href: view\.waitHref/)
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

  // Провал попытки виден на дорожке: красный фон и кромка в подписи, а не
  // только слово «провал» цветным текстом.
  for (const [name, text, cls, lbl] of [
    ['веб', css, '.tl-band.t-red', '.tl-row.is-failed .tl-lbl'],
    ['десктоп', plugin, '.duty-tl-band.t-red', '.duty-tl-row.is-failed .duty-tl-lbl'],
  ]) {
    assert.ok(text.includes(cls), `${name}: у красной полосы нет своего стиля`)
    assert.ok(text.includes(lbl), `${name}: провалившаяся строка не помечена`)
  }
  assert.match(app, /class: `tl-band t-\$\{view\.band\.tone \|\| 'grey'\}`/, 'веб: фон попытки не знает тона')
  assert.match(app, /tl-row\$\{view\.failed \? ' is-failed' : ''\}/, 'веб: строка не помечает провал')
  assert.match(plugin, /duty-tl-band t-\$\{view\.band\.tone \|\| 'grey'\}/, 'десктоп: фон попытки не знает тона')
  assert.match(plugin, /duty-tl-row\$\{view\.failed \? ' is-failed' : ''\}/, 'десктоп: строка не помечает провал')

  // Маркеры-палки: по ним ходят мышью, значит курсор — поинтер, и попадание
  // шире самого штриха (3 px иначе не поймать).
  for (const [name, text, sel] of [
    ['веб', css, '.tl-mark {'],
    ['десктоп', plugin, '.duty-tl-mark{'],
  ]) {
    const at = text.indexOf(sel)
    assert.ok(at > -1, `${name}: нет правила маркера`)
    const rule = text.slice(at, text.indexOf('}', at))
    assert.match(rule, /cursor:\s*pointer/, `${name}: у палки нет курсора-поинтера`)
    assert.ok(text.includes(name === 'веб' ? '.tl-mark::before' : '.duty-tl-mark::before'), `${name}: попадание мышью не расширено`)
  }

  // Легенда семейств — переключатель: клик подсвечивает семейство на дорожках.
  for (const [name, text] of [['веб', css], ['десктоп', plugin]]) {
    assert.match(text, /scale-key\.on[^{]*\{[^}]*background/, `${name}: выбранное семейство не отмечено`)
  }
  assert.match(css, /^\.tl-call\.tl-dim \{[^}]*opacity:\s*\.18/m, 'веб: чужие вызовы не гаснут')
  assert.match(plugin, /\.duty-tl-call\.duty-tl-dim\{[^}]*opacity:\.18/, 'десктоп: чужие вызовы не гаснут')
  assert.match(app, /'data-tone':\s*chip\.tone/, 'веб: чип не знает своего семейства')
  assert.match(plugin, /active\.includes\(chip\.tone\)[^}]*duty-tl-dim/, 'десктоп: чип не гаснет по фильтру')
  // Фильтр применяется к нарисованной панели и переключается кликом/Enter.
  assert.match(app, /\n  applyToolFilter\(panel\)\n/, 'веб: фильтр не применяется при отрисовке')
  assert.match(
    app,
    /panel\.addEventListener\('click',\s*\(event\)\s*=>\s*\{\s*const key = event\.target\.closest[\s\S]{0,180}toggleTool\(key\)/,
    'веб: клик по легенде не подключён',
  )
  assert.match(app, /event\.key !== 'Enter'[\s\S]{0,220}toggleTool\(key\)/, 'веб: клавиатура не переключает семейство')
  assert.match(app, /'aria-pressed':\s*'false'/, 'веб: ключ легенды не кнопка')
  assert.match(plugin, /'aria-pressed':\s*active\.includes\(item\.tone\)/, 'десктоп: ключ легенды не кнопка')
  assert.match(plugin, /onClick:\s*\(\)\s*=>\s*onToggleTool/, 'десктоп: клик по легенде не подключён')

  // Провал попытки отмечается на узле графа.
  assert.match(css, /\.spec-node\.has-fail rect\s*\{[^}]*var\(--danger\)/, 'веб: провал не отмечен')
  assert.match(plugin, /\.duty-spec-node\.has-fail rect\{[^}]*var\(--ui-red\)/, 'десктоп: провал не отмечен')
})

test('карточка дела не повторяет числа и не подменяет спеку телом', () => {
  const app = read('dashboard/web/app.js')
  // Длительность и попытки живут в KPI: в строке фактов их больше нет.
  const facts = app.slice(app.indexOf('function dealFactSpans'), app.indexOf('function kpiStrip'))
  assert.doesNotMatch(facts, /'длительность: '/, 'длительность повторяется в фактах и в KPI')
  assert.doesNotMatch(facts, /'попыток: '/, 'попытки повторяются в фактах и в KPI')
  // Документы дела строятся шейпером; колонки нет только когда нет ни тела, ни спеки.
  assert.match(app, /const docs = docBlocks\(data\)/, 'документы дела решает поверхность, а не шейпер')
  assert.match(app, /const left = docs\.length/, 'панель документов рисуется всегда, даже пустая')
  // Без документов столбец этапов один и на всю ширину.
  assert.match(app, /left \? h\('div', \{ class: 'deal-cols' \}, left, stages\) : stages/, 'без документов этапы уезжают в узкую колонку')
  // Заголовок этапов не повторяет счёт попыток из KPI.
  assert.doesNotMatch(app, /этапы · \$\{feed\.count\}, попыток/, 'счёт попыток дублируется в заголовке этапов')
  // Пути — именами, полный путь в подсказке.
  assert.match(app, /repoShort\(data\.repo\)/, 'шапка печатает полный путь репозитория')
  assert.match(app, /fileLabel\(feed\.spec\)/, 'путь к спеке печатается целиком')
})

test('десктопная карточка дела: те же правила, что в вебе', () => {
  const plugin = read('desktop/plugin.js')
  const facts = plugin.slice(plugin.indexOf('function DealFactSpans'), plugin.indexOf('function CompactStages'))
  assert.doesNotMatch(facts, /'длительность: '/, 'десктоп: длительность дублируется')
  assert.doesNotMatch(facts, /'попыток: '/, 'десктоп: попытки дублируются')
  assert.match(plugin, /const docs = docBlocks\(detail\.data\)/, 'десктоп: документы дела решает поверхность')
  // Без документов столбец этапов один и на всю ширину.
  assert.match(plugin, /docs\.length/, 'десктоп: столбец этапов рисуется мимо развилки документов')
  assert.match(plugin, /className: 'duty-deal-cols'/, 'десктоп: нет контейнера двух колонок')
  assert.match(plugin, /fileLabel\(feed\.spec\)/, 'десктоп: путь к спеке печатается целиком')
  assert.match(plugin, /repoShort\(/, 'десктоп: шапка печатает полный путь репозитория')
  assert.doesNotMatch(plugin, /попыток \$\{feed\.attempts\}/, 'десктоп: счёт попыток дублируется в заголовке этапов')
})

/* --------------------------------- столбец этапов и попыток (0.0.8) */

test('две колонки дела: документы растут, этапы — ровно 320 px на обеих поверхностях', () => {
  const css = read('dashboard/web/styles.css').replace(/\/\*[\s\S]*?\*\//g, '')
  const rule = /\.deal-cols\s*\{([^}]*)\}/.exec(css)
  assert.ok(rule, 'в styles.css нет правила .deal-cols')
  assert.match(rule[1], /display:\s*grid/, 'колонки собраны не гридом')
  assert.match(rule[1], /grid-template-columns:\s*minmax\(0, 1fr\) 320px/, 'правая колонка не 320 px или растёт')
  assert.match(rule[1], /gap:\s*14px/, 'зазор между колонками не 14 px')
  assert.match(rule[1], /align-items:\s*start/, 'колонки тянутся друг под друга')
  assert.match(css, /@media \(max-width: 1100px\) \{[\s\S]{0,120}\.deal-cols \{ grid-template-columns: 1fr; \}/, 'узкое окно не складывает колонки в одну')

  const plugin = read('desktop/plugin.js')
  const dutyRule = /\.duty-deal-cols\{([^}]*)\}/.exec(plugin)
  assert.ok(dutyRule, 'в CSS плагина нет правила .duty-deal-cols')
  assert.match(dutyRule[1], /grid-template-columns:minmax\(0,1fr\) 320px/, 'десктоп: правая колонка не 320 px или растёт')
  assert.match(dutyRule[1], /gap:14px/, 'десктоп: зазор не 14 px')
  assert.match(dutyRule[1], /align-items:start/, 'десктоп: колонки тянутся')
  assert.match(plugin, /@media \(max-width:1100px\)\{\.duty-deal-cols\{grid-template-columns:1fr\}\}/, 'десктоп: узкое окно не складывает колонки')
})

test('панели попыток больше нет ни в вебе, ни на десктопе', () => {
  const plugin = read('desktop/plugin.js')
  for (const [name, text] of [['веб', read('dashboard/web/app.js')], ['десктоп', plugin]]) {
    assert.ok(!/function attemptCard\(/.test(text), `${name}: остался attemptCard`)
    assert.ok(!/function AttemptCard\(/.test(text), `${name}: остался AttemptCard`)
    assert.ok(!text.includes('duty-deal-attempts'), `${name}: осталась панель «попытки»`)
    assert.ok(!text.includes('stageFeedPanel'), `${name}: осталась широкая лента этапов`)
    assert.ok(!/function StageFeed\(/.test(text), `${name}: остался StageFeed`)
    assert.ok(!/'попытки · '/.test(text) && !/`попытки · /.test(text), `${name}: заголовок панели «попытки» вернулся`)
  }
})

test('обе поверхности рисуют одни и те же классы строк и раскрытий', () => {
  const plugin = read('desktop/plugin.js')
  const app = read('dashboard/web/app.js')
  const css = read('dashboard/web/styles.css')
  // Имена общие, префиксы свои: `stage-row`/`att-row` в вебе, `duty-…` на десктопе.
  for (const cls of ['stage-row', 'att-row', 'att-ev', 'att-log']) {
    const duty = `duty-${cls}`
    assert.ok(app.includes(cls), `веб: нет класса ${cls}`)
    assert.ok(plugin.includes(duty), `десктоп: нет класса ${duty}`)
    assert.ok(css.includes(`.${cls}`), `styles.css: нет правила .${cls}`)
    assert.ok(plugin.includes(`.${duty}{`), `CSS плагина: нет правила .${duty}`)
  }
  // Раскрытая строка попытки помечена классом `open` на обеих поверхностях.
  for (const [name, text, row] of [
    ['веб', app, "'att-row open' : 'att-row'"],
    ['десктоп', plugin, "'duty-att-row open' : 'duty-att-row'"],
  ]) {
    assert.ok(text.includes(row), `${name}: строка попытки не помечает раскрытие`)
  }
  // Строка этапа — одна линия ~26 px, попытка ~24 px с отступом 12 px.
  assert.match(css, /\.stage-row \{[^}]*height: 26px/, 'веб: строка этапа не 26 px')
  assert.match(css, /\.att-row \{[^}]*height: 24px[^}]*margin-left: 12px/s, 'веб: строка попытки не 24 px с отступом')
  assert.match(plugin, /\.duty-stage-row\{[^}]*height:26px/, 'десктоп: строка этапа не 26 px')
  assert.match(plugin, /\.duty-att-row\{[^}]*height:24px[^}]*margin-left:12px/, 'десктоп: строка попытки не 24 px с отступом')
})

test('обрезанная команда доказательства несёт полную строку в подсказке', () => {
  const plugin = read('desktop/plugin.js')
  const app = read('dashboard/web/app.js')
  // Обрезка — в шейпере (`cmdShort`), подсказка — полная команда (`cmdTitle`).
  for (const [name, text] of [['веб', app], ['десктоп', plugin]]) {
    assert.match(text, /title: row\.cmdTitle/, `${name}: у обрезанной команды нет подсказки`)
    assert.match(text, /row\.cmdShort/, `${name}: команда доказательства печатается целиком`)
  }
  // и это именно `title` на строке команды, а не подсказка в другом месте
  assert.match(app, /class: 'mono att-ev-cmd', title: row\.cmdTitle \}, row\.cmdShort\)/, 'веб: title не на самой команде')
  assert.match(plugin, /className: 'duty-mono duty-att-ev-cmd', title: row\.cmdTitle \}, row\.cmdShort\)/, 'десктоп: title не на самой команде')
})

