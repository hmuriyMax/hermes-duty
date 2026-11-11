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
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { pathToFileURL } from 'node:url'

const root = new URL('..', import.meta.url)
const tree = root.pathname
const read = (path) => readFileSync(new URL(path, root), 'utf8')

const html = read('dashboard/web/index.html')
const app = read('dashboard/web/app.js')
const api = read('dashboard/plugin_api.py')
const pluginSource = read('desktop/plugin.js')

test('левый столбец: шапка дорожки липнет вместе с подписями, ширина — по контенту', () => {
  // Макс, 09.11: «очень криво обошелся с левым столбцом. попытки закреплены,
  // а названия этапов и профиль - нет. плюс ширина столбца уже, чем ширина
  // самого широкого контента».
  const css = read('dashboard/web/styles.css')
  // шапка дорожки — той же решёткой, что строка попытки
  assert.match(css, /\.tl-lane-h \{ display: grid; grid-template-columns: var\(--tl-gutter, 186px\) 1fr/, 'веб: шапка дорожки не решёткой')
  // имя и профиль — одна липнущая ячейка
  assert.match(app, /class: 'tl-lane-id'/, 'веб: имя и профиль не в одной ячейке')
  assert.match(css, /\.scale-main\.is-wide \.tl-lane-id \{ position: sticky/, 'веб: шапка дорожки не липнет')
  // ширина столбца подстраивается под самый широкий контент
  assert.match(app, /function fitScaleGutter/, 'веб: нет подгона ширины столбца')
  assert.match(app, /\.tl-lbl > \*, \.tl-lane-id'/, 'веб: замер не по ячейке имени целиком')
  assert.match(app, /Math\.min\(360, Math\.max\(186,/, 'веб: нет потолка 360px')
  assert.match(pluginSource, /duty-tl-lane-id/, 'десктоп: нет ячейки имени')
  assert.match(pluginSource, /duty-tl-lane-h\{display:grid;grid-template-columns:var\(--duty-tl-gutter,186px\) 1fr/, 'десктоп: шапка не решёткой')
  assert.match(pluginSource, /--duty-tl-gutter/, 'десктоп: нет подгона ширины')
})

test('шкала длиннее часа листается, мы висим на правой кромке', () => {
  // Макс, 09.11: «максимальную ширину таймлайна в час, больше — прокрутка».
  // Масштаб заморожен на «вьюпорт = час»: длинное дело не сжимается, а
  // скроллится, и пока человек не уехал влево сам, каждый тик держит в кадре
  // свежую правую кромку.
  const css = read('dashboard/web/styles.css')
  assert.match(app, /scale-main\$\{scale\.axis\.zoom > 1 \? ' is-wide' : ''\}/, 'веб: широкая шкала не помечена')
  assert.match(app, /--tl-zoom:\$\{scale\.axis\.zoom\}/, 'веб: зум не в переменной')
  assert.match(app, /pinScaleScroll\(panel\)/, 'веб: нет пина к правой кромке')
  assert.match(app, /el\.__tlPinned = el\.scrollLeft \+ el\.clientWidth >= el\.scrollWidth - 8/, 'веб: нет снятия пина при ручном уезде')
  assert.match(css, /\.scale-main\.is-wide \{[\s\S]*?overflow-x: auto/, 'веб: нет горизонтальной прокрутки')
  assert.match(css, /min-width: calc\(var\(--tl-zoom, 1\) \* 100%\)/, 'веб: дорожки не растягиваются до zoom')
  assert.match(css, /\.scale-main\.is-wide \.scale-head-gap, \.scale-main\.is-wide \.tl-lbl, \.scale-main\.is-wide \.tl-lane-id \{ position: sticky/, 'веб: подписи не прилипают')
  assert.match(pluginSource, /duty-scale-main\$\{wide \? ' is-wide' : ''\}/, 'десктоп: широкая шкала не помечена')
  assert.match(pluginSource, /el\.scrollLeft = el\.scrollWidth/, 'десктоп: нет пина')
  assert.match(pluginSource, /\.duty-scale-main\.is-wide\{overflow-x:auto/, 'десктоп: нет стиля прокрутки')
  // Пин снимает только жест человека, не программный скролл и не восстановление
  // позиции браузером после F5.
  assert.match(app, /if \(!el\.__tlUser\) return/, 'веб: пин снимается без жеста')
  assert.match(app, /event\.deltaX\) el\.__tlUser = true/, 'веб: колесо не считается жестом')
  assert.match(pluginSource, /if \(!el\.__tlUser\) return/, 'десктоп: пин снимается без жеста')
  // Подложка левого столбца — слой поверх содержимого вне скроллера: фон
  // скроллера прячется под чипами, а слой стоит на месте, пока они проезжают.
  assert.match(app, /class: 'tl-gutter-bg'/, 'веб: нет слоя подложки')
  assert.match(app, /scale\.axis\.zoom > 1 \? \[h\('div', \{ class: 'tl-gutter-bg'/, 'веб: подложка не только в прокрутке — в узком виде она закрывает текст подписей')
  assert.match(css, /\.tl-gutter-bg \{[\s\S]*?position: absolute/, 'веб: подложка не слоем поверх')
  assert.match(css, /\.tl-gutter-bg \{[\s\S]*?z-index: 2/, 'веб: подложка не между контентом и подписями')
  assert.match(css, /\.tl-gutter-bg \{[\s\S]*?pointer-events: none/, 'веб: подложка ловит клики')
  assert.match(pluginSource, /duty-tl-gutter-bg/, 'десктоп: нет слоя подложки')
  assert.match(pluginSource, /\.duty-tl-gutter-bg\{[\s\S]*?z-index:2/, 'десктоп: подложка не между контентом и подписями')
  // Ширина столбца монотонна, пока открыто то же дело: тик с пустыми попытками
  // не ужимает столбец обратно (Макс, 10.11: «столбец сужается после первого же
  // обновления»). Повторный замер — после кадров: шрифт догружается позже.
  assert.match(app, /scaleGutterSeen\.px = Math\.max\(scaleGutterSeen\.px, need\)/, 'веб: ширина не монотонна')
  assert.match(app, /fitScaleGutter\(panel, dealKey\)/, 'веб: замер не привязан к делу')
  assert.match(app, /fitScaleGutter\(panel, dealKey\)\n?[\s\S]{0,80}pinScaleScroll\(panel\)/, 'веб: нет повторного замера после кадров')
  assert.match(pluginSource, /seen\.px = Math\.max\(seen\.px,/, 'десктоп: ширина не монотонна')
  // Дорожки без собственного z-index, иначе подписи не поднимаются над подложкой.
  assert.ok(!/\.tl-lanes \{[^}]*z-index/.test(css), 'веб: у дорожек есть свой z-index — подписи под подложкой')
  assert.ok(!/\.duty-tl-lanes\{[^}]*z-index/.test(pluginSource), 'десктоп: у дорожек есть свой z-index')
})

test('маркеры: центр палки точно на своём моменте', () => {
  // Сдвиги якорей в стороны оставляли щели: из под палок выглядывала кромка
  // полосы (Макс, 09.11). Центр палки — на моменте, якорь — только семантика.
  const css = read('dashboard/web/styles.css')
  const plugin = read('desktop/plugin.js')
  for (const [name, text, cls] of [['веб', css, '.tl-mark'], ['десктоп', plugin, '.duty-tl-mark']]) {
    assert.doesNotMatch(text, new RegExp(`${cls}\\.anchor-end\\s*{[^}]*-100%`), `${name}: палка вывода сдвинута`)
    assert.doesNotMatch(text, new RegExp(`${cls}\\.anchor-start\\s*{[^}]*translateX\\(1px`), `${name}: палка вопроса сдвинута`)
  }
  assert.match(css, /\.tl-mark\.anchor-end, \.tl-mark\.anchor-start \{ transform: translateX\(-50%\)/)
  assert.match(plugin, /\.duty-tl-mark\.anchor-end,\.duty-tl-mark\.anchor-start\{transform:translateX\(-50%\)\}/)
})

test('свёрнутая строка: чипы линейной ширины, развёрнутая — логарифм', () => {
  const css = read('dashboard/web/styles.css')
  // В нахлёсте уровней нет: плотность цвета — единственная правда строки, и она
  // должна читаться как реальное время (Макс, 09.11). Логарифм возвращается при
  // развороте, иначе короткие вызовы становились бы точками.
  assert.match(app, /--w-log:\$\{chip\.widthPct\}%/, 'веб: у чипа нет лог-ширины')
  assert.match(app, /--w-lin:\$\{chip\.widthLinPct/, 'веб: у чипа нет линейной ширины')
  assert.match(css, /\.tl-row:hover \.tl-call, \.tl-row:focus-visible \.tl-call \{ width: var\(--w-log\)/, 'веб: развёрнутая не возвращает лог')
  assert.match(app, /width:var\(--w-lin\)/, 'веб: свёрнутая не линейна')
  assert.match(read('desktop/plugin.js'), /width: 'var\(--w-lin\)'/, 'десктоп: свёрнутая не линейна')
  assert.match(read('desktop/plugin.js'), /\.duty-tl-row:hover \.duty-tl-call,\.duty-tl-row:focus-visible \.duty-tl-call\{width:var\(--w-log\)\}/, 'десктоп: развёрнутая не возвращает лог')
})

test('палитра инструментов: интеграции и служебные не серые', () => {
  const css = read('dashboard/web/styles.css')
  // Серый — только «не знаю»: mcp-коннекторы (jira/gitlab/goland) и служебные
  // вызовы движка (tool_search, session_search) — сотни штук за дело, и серым
  // они глушили карту вызовов.
  assert.match(app, /mcp: 'var\(--teal\)'/, 'веб: у интеграций нет тона')
  assert.match(app, /meta: 'var\(--pink\)'/, 'веб: у служебных нет тона')
  assert.match(css, /\.scale-key-dot\.t-mcp \{ background: var\(--teal\)/, 'веб: легенда без интеграций')
  assert.match(css, /\.scale-key-dot\.t-meta \{ background: var\(--pink\)/, 'веб: легенда без служебных')
  assert.match(read('desktop/plugin.js'), /duty-t-mcp/, 'десктоп: легенда без интеграций')
  assert.match(read('desktop/plugin.js'), /duty-t-meta/, 'десктоп: легенда без служебных')
  assert.match(read('desktop/model.js'), /\^\(mcp__\|jira/, 'шейпер не узнаёт интеграции')
})

test('плавность дела: вход, уход и переезд строк анимированы', () => {
  // Патч правит узлы на месте (morph), поэтому движение отдаётся CSS: свежие
  // узлы помечаются, ушедшие доигрывают схлопывание, перестановка дорожек — FLIP.
  assert.match(app, /function tagEntering/, 'вход не помечается')
  assert.match(app, /function animateExit/, 'уход не анимируется')
  assert.match(app, /function flipPlay/, 'перестановка дорожек не доезжает плавно')
  assert.match(app, /is-new/, 'класс входа не ставится')
  assert.match(app, /is-gone/, 'класс ухода не ставится')
  assert.match(app, /`tick:\$\{tick\.at/, 'метки оси не имеют устойчивого ключа')
  // Стили: переходы геометрии + вход/уход, при reduced-motion — всё выключено.
  const css = read('dashboard/web/styles.css')
  assert.match(css, /\.tl-call \{[\s\S]*?transition: left/, 'чипы не переезжают плавно')
  assert.match(css, /@keyframes duty-row-in/, 'нет анимации входа')
  assert.match(css, /\.is-gone \{/, 'нет схлопывания ухода')
  assert.match(css, /prefers-reduced-motion[\s\S]*\.is-new \{ animation: none/, ' reduced-motion не гасит вход')
})

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

test('навигация: одна страница дел, «почему стоит» и «здоровье»', () => {
  // 09.11: «Сейчас», «Завершённые» и «Дело» стали одной страницей — вкладки,
  // ведущей на пустой выбор дела, больше нет, а завершённые живут в её подвале.
  assert.match(app, /parts\[0\] === 'deal'/)
  assert.match(app, /parts\[0\] === 'why'/)
  assert.match(app, /parts\[0\] === 'health'/)
  assert.match(app, /return \{ name: 'now' \}/)
  for (const tab of ['now', 'why', 'health']) {
    assert.match(html, new RegExp(`data-tab="${tab}"`), `нет вкладки ${tab}`)
  }
  for (const gone of ['deals', 'deal']) {
    assert.ok(!html.includes(`data-tab="${gone}"`), `вкладка ${gone} вернулась в навигацию`)
  }
  // Прежний адрес списка не 404: он открывает ту же страницу с раскрытым блоком.
  assert.match(app, /if \(route\.name === 'deals'\) \{/)
})

test('«Завершённые» листаются по проектам: пятёрка на репозиторий и свой курсор', () => {
  // Индекс групп — числа от бэкенда: `/deals?limit=1` даёт `repos` с total.
  assert.match(app, /api\('\/deals\?limit=1'\)/)
  assert.match(app, /shapeFinishedPage\(await api\(query\)\)/)
  // «+5» — следующая страница своей группы по курсору, а не сдвиг общего списка.
  assert.match(app, /`\/deals\?limit=\$\{finished\.perRepo\}&repo=\$\{encodeURIComponent\(repo\)\}`/)
  assert.match(app, /cursor=\$\{encodeURIComponent\(cursor\)\}/)
  assert.match(app, /group\.cursor = page\.next/)
  // Потолок «по 5 на проект» — константа, а не магия в трёх местах.
  assert.match(app, /const FINISHED_PER_REPO = 5/)
  // Десктоп читает историю ОДНИМ запросом и режет по проектам в памяти: у
  // старого бэкенда ключа `repos` нет, и постраничный список проектов не собрать.
  assert.match(pluginSource, /dealsPayload\(`\/deals\?limit=\$\{FINISHED_FETCH_LIMIT\}`\)/)
  assert.match(pluginSource, /const FINISHED_FETCH_LIMIT = 200/)
  assert.match(pluginSource, /const DEALS_ATTEMPTS = 3/)
  assert.match(pluginSource, /const FINISHED_PER_REPO = 5/)
  assert.ok(!/limit=\$\{FINISHED_PER_REPO\}&repo=/.test(pluginSource), 'десктоп снова листает проекты по сети')
  // второго счёта в строках нет: вопросы/попытки/длительность — поля ответа
  assert.match(app, /deal\.questions\.text/)
  assert.match(pluginSource, /deal\.questions\.text/)
  assert.ok(!/question_asked/.test(app), 'в app.js появился свой счёт вопросов')
  // пусто — словами бэкенда (`note`), а не пустым квадратом
  assert.match(app, /index\.note \|\| 'в store нет ни одного закрытого дела'/)
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
  // столбец встроен в карточку дела и собран компактным видом шейпера. Данные он
  // берёт из текущего снимка дела, а не из аргументов отрисовки: тихий тик правит
  // готовые узлы, и пересборка по клику обязана видеть свежий ответ.
  assert.match(app, /function stagesColumn\(dealId\)/)
  assert.match(app, /stagesColumn\(data\.id\)/)
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
    // Свёрнутая строка — нахлёст, а не прятание: все чипы на одной линии и
    // вполсилы, глубокие уровни лежат поверх (Макс, 09.11: «в свернутом виде
    // будем рисовать все квадратики с наложением… прозрачностью»). Рамки у
    // чипов больше нет: перекрытие читается плотностью цвета.
    assert.doesNotMatch(text, /box-shadow:\s*inset\s+0\s+0\s+0\s+1px\s+rgba\(0,\s*0,\s*0,\s*\.22\)/, `${name}: рамка чипа вернулась`)
    assert.match(text, /:not\(:hover\):not\(:focus-visible\)\s+[^{]*tl-call[^{]*\{[^}]*top:\s*calc\(50%[^}]*opacity:\s*\.45/, `${name}: свёрнутая строка не рисуется нахлёстом`)
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

  // Подсказка — свой слой в body, а не `::after` внутри дорожки: внутри её
  // резала панель по `overflow`, а соседние строки со своим контекстом наложения
  // оказывались выше любого `z-index` у хоста.
  for (const [name, text, cls] of [['веб', css, '.tl-tip'], ['десктоп', plugin, '.duty-tl-tip']]) {
    const start = text.indexOf(`${cls}{`) >= 0 ? text.indexOf(`${cls}{`) : text.indexOf(`${cls} {`)
    assert.ok(start > 0, `${name}: нет слоя подсказки`)
    const rule = text.slice(start, text.indexOf('}', start))
    assert.match(rule, /position:\s*fixed/, `${name}: подсказка не в своём слое`)
    assert.match(rule, /z-index:\s*9999/, `${name}: подсказка не поверх страницы`)
    assert.match(rule, /white-space:\s*normal/, `${name}: подсказка не переносится`)
    assert.match(rule, /max-width:\s*280px/, `${name}: у подсказки нет предела ширины`)
    assert.doesNotMatch(text, /data-tip\);position:absolute/, `${name}: подсказка всё ещё ::after в дорожке`)
  }
  assert.match(app, /function bindTipLayer\(\)[\s\S]{0,400}pointerover/, 'веб: подсказка не ловит наведение')
  assert.match(app, /'aria-hidden': 'true'/, 'веб: слой подсказки без aria-hidden')
  assert.match(plugin, /function dutyTipBind\(\)[\s\S]{0,400}pointerover/, 'десктоп: подсказка не ловит наведение')
  // Автообновление подменяет узлы: подсказку надо снять, иначе она висит на месте.
  assert.match(app, /dutyTipFollow/, 'веб: подсказка не сверяется с живостью хоста')
  assert.match(plugin, /!dutyTipHost\.isConnected/, 'десктоп: подсказка не сверяется с живостью хоста')

  // Граф этапов — в заголовке дела, статус — в правом углу.
  assert.match(app, /deal-head-top[\s\S]{0,900}specStrip\(scale\.spec\)/, 'веб: граф не в заголовке')
  assert.match(app, /deal-head-side/, 'веб: у заголовка нет правой колонки под статус')
  assert.match(plugin, /duty-deal-head-top[\s\S]{0,900}SpecStrip/, 'десктоп: граф не в заголовке')
  assert.match(plugin, /duty-deal-head-side[\s\S]{0,400}StateBadge/, 'десктоп: статус не в правой колонке')

  // Значок дела в заголовке: одна геометрия (`DEAL_ICON`) на обе поверхности,
  // рядом с именем, в стиле видов этапов (16×16, stroke).
  const modelSrc = read('desktop/model.js')
  assert.match(modelSrc, /const DEAL_ICON =/, 'шейпер: нет константы значка дела')
  assert.match(app, /class: 'deal-ico'[\s\S]{0,200}d: DEAL_ICON/, 'веб: значок дела не у заголовка')
  assert.match(css, /svg\.deal-ico\s*\{[^}]*stroke-width:\s*1\.5/, 'веб: стиль значка дела не в общем стиле')
  assert.match(plugin, /duty-deal-ico[\s\S]{0,200}d: DEAL_ICON/, 'десктоп: значок дела не у заголовка')

  // Значок плагина: favicon вкладки, шапка страницы и карточка хаба — один
  // рисунок. Шапку рисует app.js из `DEAL_ICON` (одна геометрия, не копия).
  assert.match(html, /rel="icon"[^>]*favicon-duty\.svg/, 'веб: favicon плагина не подключён')
  assert.match(app, /brand-ico-svg[\s\S]{0,200}d: DEAL_ICON/, 'веб: шапка не рисует значок из шейпера')
  assert.match(css, /svg\.brand-ico-svg\s*\{[^}]*stroke:\s*var\(--accent\)/, 'веб: значок шапки не в общем стиле')

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

  // Граф спеки: красный — про последнюю попытку, а не про память о провале.
  // Класса `has-fail` не существует ни в разметке, ни в правилах: узел, упавший
  // в середине истории и повторённый, больше не краснеет.
  assert.doesNotMatch(css, /has-fail/, 'веб: осталось правило провала-в-памяти')
  assert.doesNotMatch(app, /has-fail/, 'веб: узел красится по любому провалу')
  assert.doesNotMatch(plugin, /has-fail/, 'десктоп: узел красится по любому провалу')

  // Живой этап: признак `live` читается разметкой, обводка дышит, подпись состояния
  // в цвете акцента — иначе идущая попытка выглядит застывшей картинкой.
  for (const [name, text, breath, accent] of [
    ['веб', css, '@keyframes spec-breath', '.spec-node.is-live .spec-state'],
    ['десктоп', plugin, '@keyframes duty-spec-breath', '.duty-spec-node.is-live .duty-spec-state'],
  ]) {
    assert.ok(text.includes(breath), `${name}: нет анимации живого этапа`)
    assert.match(text, /(?:^|[\s}])\.(?:duty-)?spec-node\.is-live rect\s*\{[^}]*animation:/m, `${name}: живой узел не анимируется`)
    assert.ok(text.includes(accent), `${name}: подпись живого этапа не в цвете акцента`)
  }

  // Ребро — плавная кривая без штриха и со стрелкой: штрих читался как «ещё не готово».
  assert.doesNotMatch(css, /\.spec-edge\s*\{[^}]*stroke-dasharray/, 'веб: ребро снова штриховое')
  assert.match(css, /\.spec-arrow\s*\{[^}]*fill:\s*var\(--border\)/, 'веб: у ребра нет заливки стрелки')
  assert.match(plugin, /\.duty-spec-arrow\{[^}]*fill:var\(--ui-stroke-tertiary\)/, 'десктоп: у ребра нет заливки стрелки')
})

test('граф спеки: узел читает вид, ребро получает кривую и стрелку', () => {
  const app = read('dashboard/web/app.js')
  const css = read('dashboard/web/styles.css')
  // Вшитая копия шейпера вырезана: проверяем разметку плагина, а не тело модели.
  const plugin = read('desktop/plugin.js').replace(/\/\/ duty:model-start[\s\S]*?\/\/ duty:model-end/, '')

  /** Тело функции верхнего уровня: от заголовка до следующего `function `. */
  const fnBody = (source, head) => {
    const start = source.indexOf(head)
    assert.ok(start >= 0, `не найдена функция ${head}`)
    const next = source.indexOf('\nfunction ', start + head.length)
    return source.slice(start, next < 0 ? undefined : next)
  }

  for (const [name, source, head, cls, id, state, edge, arrow, live] of [
    [
      'веб',
      app,
      'function specStrip(',
      'spec-node',
      'spec-id',
      'spec-state',
      /class: 'spec-edge', d: edge\.d/,
      /h\('polygon', \{ class: 'spec-arrow', points: edge\.arrow\.points \}\)/,
      /node\.live \? ' is-live' : ''/,
    ],
    [
      'десктоп',
      plugin,
      'function SpecStrip(',
      'duty-spec-node',
      'duty-spec-id',
      'duty-spec-state',
      /className: 'duty-spec-edge', d: edge\.d/,
      /h\('polygon', \{ className: 'duty-spec-arrow', points: edge\.arrow\.points \}\)/,
      /node\.live \? ' is-live' : ''/,
    ],
  ]) {
    const body = fnBody(source, head)
    // Признак живого этапа ставится из вида, а не угадывается поверхностью.
    assert.match(body, live, `${name}: узел не помечает идущий этап`)
    assert.match(body, new RegExp('class(?:Name)?: `' + cls + ' t-\\$\\{node\\.tone\\}'), `${name}: узел не знает тона`)
    assert.doesNotMatch(body, /has-fail/, `${name}: вернулся класс провала-в-памяти`)
    // Подсказка — готовая строка шейпера.
    assert.match(body, /h\('title', null, node\.title\)/, `${name}: подсказка не из вида`)
    // Ребро: кривая из вида + треугольник острия. Координат ребра на поверхности нет.
    assert.match(body, edge, `${name}: ребро не берёт кривую из вида`)
    assert.match(body, arrow, `${name}: у ребра нет стрелки`)
    assert.doesNotMatch(body, /from\.x|from\.y|to\.x|to\.y/, `${name}: поверхность считает геометрию ребра`)
    // Две строки текста: id и слово состояния.
    assert.match(body, new RegExp(`class(?:Name)?: '${id}'[\\s\\S]{0,320}class(?:Name)?: '${state}'`), `${name}: в узле нет второй строки`)
    assert.match(body, /rx: 6/, `${name}: у узла нет скругления 6`)
  }

  // Подпись «граф спеки» — под картинкой: полоса стала колонкой, текст слева.
  for (const [name, text, strip, label] of [
    ['веб', css, /\.spec-strip\s*\{[^}]*flex-direction:\s*column/, /\.spec-strip-name\s*\{[^}]*text-align:\s*left/],
    ['десктоп', plugin, /\.duty-spec-strip\{[^}]*flex-direction:column/, /\.duty-spec-strip-name\{[^}]*text-align:left/],
  ]) {
    assert.match(text, strip, `${name}: подпись висит сбоку от графа`)
    assert.match(text, label, `${name}: подпись не прижата влево`)
  }
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

/* -------------------- сжатый простой на оси: настоящий DOM ------------- */

// jsdom берётся из окружения Hermes: своих зависимостей у плагина нет, а тянуть
// node_modules в репозиторий ради одного теста незачем. Нет jsdom — тест честно
// пропускается, а не зеленеет.
function jsdomRoot() {
  const candidates = [process.env.HERMES_REACT_ROOT, join(homedir(), '.hermes', 'hermes-agent', 'node_modules')]
    .filter(Boolean)
  return candidates.find((dir) => existsSync(join(dir, 'jsdom', 'package.json'))) || ''
}

/**
 * Страница дела в jsdom: свежий модуль на каждый вызов, поэтому вторая съёмка
 * (дело без сжатия) не делит состояние с первой. `./model.js` в вебе — тот же
 * шейпер, что вшит в десктоп (бэкенд отдаёт его своим роутом), в Node он
 * подкладывается файлом рядом с копией `app.js`.
 */
async function renderDealPanel(detail, attempts) {
  const root = jsdomRoot()
  const { JSDOM } = await import(pathToFileURL(join(root, 'jsdom', 'lib', 'api.js')).href)
  const dir = mkdtempSync(join(tmpdir(), 'duty-web-panel-'))
  writeFileSync(join(dir, 'app.js'), app)
  writeFileSync(join(dir, 'model.js'), read('desktop/model.js'))
  const dom = new JSDOM(
    '<!doctype html><div id="app"></div><span id="stamp"></span><span id="ver"></span><span id="human"></span><div id="tabs"></div><button id="refresh"></button>',
    { url: 'http://localhost/#/deal/d_0123456789abcdef' },
  )
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  globalThis.Node = dom.window.Node
  globalThis.localStorage = dom.window.localStorage
  globalThis.location = dom.window.location
  // Страница сама просит данные и сама ставит таймер обновления: тесту нужен
  // один кадр, поэтому на время импорта сеть и таймеры подменены. Глобалы
  // возвращаются на место — иначе таймеры переживут тест и удержат процесс.
  const saved = {
    fetch: globalThis.fetch,
    setTimeout: globalThis.setTimeout,
    setInterval: globalThis.setInterval,
    clearTimeout: globalThis.clearTimeout,
    clearInterval: globalThis.clearInterval,
  }
  globalThis.fetch = async (url) => ({
    ok: true,
    status: 200,
    json: async () => (String(url).includes('/attempts') ? attempts : detail),
  })
  globalThis.setTimeout = () => 0
  globalThis.setInterval = () => 0
  globalThis.clearTimeout = () => {}
  globalThis.clearInterval = () => {}
  try {
    await import(pathToFileURL(join(dir, 'app.js')).href)
    for (let i = 0; i < 50 && !dom.window.document.querySelector('.scale-plot'); i += 1) {
      await new Promise((resolve) => setImmediate(resolve))
    }
  } finally {
    Object.assign(globalThis, saved)
  }
  return dom.window.document
}

test('веб: сжатый простой подписан на оси, без сжатия — ни одного элемента', async (t) => {
  if (!jsdomRoot()) return t.skip('нет jsdom в ~/.hermes/hermes-agent/node_modules')

  // Дело из двух попыток: пауза между ними — тот самый простой.
  const detail = (idleSec) => ({
    id: 'd_0123456789abcdef',
    title: 'дело с простоем',
    repo: '/x/hermes-duty',
    status: 'done',
    terminal: true,
    timeline: [{ kind: 'created', at: 1, summary: '' }],
    stages: [{ id: 'impl', needs: [] }],
  })
  const attempts = (idleSec) => {
    const second = 1 + 1_000 + idleSec
    return {
      attempts: [
        { stage: 'impl', attempt: 1, start: 1, end: 1_001, status: 'done', outcome: 'done' },
        { stage: 'impl', attempt: 2, start: second, end: second + 3_000, status: 'done', outcome: 'done' },
      ],
    }
  }

  // Простой 45 минут: штрихованная полоса, знаки разрыва и пилюля со временем.
  const squeezed = await renderDealPanel(detail(45 * 60), attempts(45 * 60))
  const gapEl = squeezed.querySelectorAll('.tl-idle-gap')
  assert.equal(gapEl.length, 1, `промежутков ${gapEl.length}, а не один`)
  assert.equal(squeezed.querySelectorAll('.tl-idle-cut').length, 0, 'вернулись засечки разрыва')
  assert.ok(squeezed.querySelector('.tl-idle-band'), 'нет штрихования промежутка')
  const pill = squeezed.querySelector('.tl-idle-pill')
  assert.ok(pill, 'на полосе нет пилюли со временем')
  assert.equal(pill.textContent, '≈ 45 мин', `пилюля: ${pill.textContent}`)
  const tip = gapEl[0].getAttribute('title') || ''
  assert.ok(tip.includes('сжато'), 'в подсказке нет пояснения, что время сжато')
  assert.ok(tip.includes('простой 45 мин'), `подсказка простоя: ${tip}`)
  assert.ok(gapEl[0].className.includes('is-pause'), 'простой между этапами не назван')

  // Штриховка и пилюля — из styles.css: класс есть, правило есть, вместе они
  // говорят «здесь время сжато», а не рисуют границу полосы. Двойные засечки
  // по краям убраны: крупные и грязные (09.11, Макс).
  const css = read('dashboard/web/styles.css')
  assert.match(css, /\.tl-idle-band \{[^}]*repeating-linear-gradient/, 'нет штриховки у промежутка')
  assert.doesNotMatch(css, /tl-idle-cut/, 'в CSS остались засечки разрыва')
  assert.match(css, /\.tl-idle-pill \{[^}]*border-radius: 5px/s, 'нет пилюли со временем')

  // Пауза в три минуты не сжимается — ни одного элемента сжатия на странице.
  const plain = await renderDealPanel(detail(3 * 60), attempts(3 * 60))
  assert.equal(plain.querySelectorAll('line.tl-idle').length, 0, 'пунктир нарисован там, где сжатия нет')
  assert.equal(plain.querySelectorAll('text.tl-idle-label').length, 0, 'подпись нарисована там, где сжатия нет')
})


/* ------------------ страница дела обновляется сама и тихо (0.0.8) --------- */

const DEAL_ID = 'd_0123456789abcdef'
const DEAL_T0 = 1_700_000_000

/**
 * Два ответа одного дела: живая попытка, потом та же попытка закрыта.
 *
 * Форма — как у бэкенда: `stages[].attempts[]` знает `live`/`duration_sec`,
 * `/attempts` — те же попытки для шкалы. Между тиками меняется и длительность
 * (KPI), и состояние попытки — то есть проверять есть что.
 */
function dealRound(live, nowSec) {
  const attempt = {
    n: 1,
    run_id: `${DEAL_ID}:impl#1`,
    stage: 'impl',
    started_at: DEAL_T0 + 1,
    live: live === true,
    outcome: live ? '' : 'done',
    duration_sec: live ? null : 180,
    finished_at: live ? null : DEAL_T0 + 181,
  }
  return {
    detail: {
      id: DEAL_ID,
      title: 'дело с попыткой',
      repo: '/x/hermes-duty',
      status: live ? 'running' : 'done',
      terminal: !live,
      now: nowSec,
      created_at: DEAL_T0,
      timeline: [{ kind: 'created', at: DEAL_T0, summary: '' }],
      stages: [{ id: 'impl', kind: 'agent', role: 'implementer', state: live ? 'running' : 'done', needs: [], attempts: [attempt] }],
    },
    attempts: {
      now: nowSec,
      attempts: [
        {
          stage: 'impl',
          attempt: 1,
          start: DEAL_T0 + 1,
          end: live ? null : DEAL_T0 + 181,
          status: live ? 'running' : 'done',
          outcome: live ? '' : 'done',
          lease: live ? 'live' : '',
          duration_sec: live ? null : 180,
        },
      ],
    },
  }
}

/**
 * Страница дела с живой петлёй опроса: сеть отдаёт заранее заданные ответы, а
 * таймеры не идут сами — тест прогоняет ровно один тик. Так видно то, что
 * обещано: страница правит нарисованные узлы, а не собирается заново.
 */
async function liveDealPage(rounds, options = {}) {
  const root = jsdomRoot()
  const { JSDOM } = await import(pathToFileURL(join(root, 'jsdom', 'lib', 'api.js')).href)
  const dir = mkdtempSync(join(tmpdir(), 'duty-web-live-'))
  writeFileSync(join(dir, 'app.js'), app)
  writeFileSync(join(dir, 'model.js'), read('desktop/model.js'))
  const dom = new JSDOM(
    '<!doctype html><div id="app"></div><span id="stamp"></span><span id="ver"></span><span id="human"></span><div id="tabs"></div><button id="refresh"></button>',
    { url: `http://localhost/#/deal/${DEAL_ID}` },
  )
  const win = dom.window
  globalThis.window = win
  globalThis.document = win.document
  globalThis.Node = win.Node
  globalThis.localStorage = win.localStorage
  globalThis.location = win.location
  // jsdom держит документ в состоянии `prerender` (`hidden === true`), а опрос
  // по правилу молчит в скрытой вкладке. Тест изображает видимую вкладку.
  Object.defineProperty(win.document, 'hidden', { configurable: true, get: () => false })
  Object.defineProperty(win.document, 'visibilityState', { configurable: true, get: () => 'visible' })
  // `prefers-reduced-motion` подменяется на уровне поверхности: шейпер про
  // анимации ничего не знает, решает браузерная настройка.
  if (options.reduceMotion) win.matchMedia = () => ({ matches: true })

  const timers = new Map()
  let seq = 0
  let current = 0
  let seen = 0
  const clock = { ms: DEAL_T0 * 1000 + 60_000 }
  const saved = {
    fetch: globalThis.fetch,
    setTimeout: globalThis.setTimeout,
    setInterval: globalThis.setInterval,
    clearTimeout: globalThis.clearTimeout,
    clearInterval: globalThis.clearInterval,
    now: Date.now,
  }
  const reply = (body) => ({ ok: true, status: 200, json: async () => body })
  globalThis.fetch = async (url) => {
    const text = String(url)
    if (text.includes('/attempts')) {
      const round = rounds[Math.min(current, rounds.length - 1)]
      if (round.fail) throw new Error('сеть')
      return reply(round.attempts)
    }
    // `/health` за версией шапки — не дело: ответ пустой, счётчик ответов не двигает.
    if (!text.includes('/deal/')) return reply({})
    current = Math.min(seen, rounds.length - 1)
    seen += 1
    if (rounds[current].fail) throw new Error('сеть')
    return reply(rounds[current].detail)
  }
  globalThis.setTimeout = (fn) => {
    seq += 1
    timers.set(seq, fn)
    return seq
  }
  globalThis.clearTimeout = (id) => {
    timers.delete(id)
  }
  globalThis.setInterval = () => 0
  globalThis.clearInterval = () => {}
  Date.now = () => clock.ms

  const flush = async () => {
    for (let i = 0; i < 80; i += 1) await new Promise((resolve) => setImmediate(resolve))
  }

  try {
    await import(pathToFileURL(join(dir, 'app.js')).href)
    for (let i = 0; i < 60 && !win.document.querySelector('.scale-plot'); i += 1) await flush()
  } catch (err) {
    Object.assign(globalThis, { fetch: saved.fetch, setTimeout: saved.setTimeout, setInterval: saved.setInterval, clearTimeout: saved.clearTimeout, clearInterval: saved.clearInterval })
    Date.now = saved.now
    throw err
  }

  return {
    doc: win.document,
    /** Прогнать один запланированный тик: часы вперёд, ответ следующий, петля дальше. */
    async tick(advanceSec = 37) {
      clock.ms += advanceSec * 1000
      // Последний таймер — тот, что поставила петля: раньше него в очереди
      // лежат только снятые таймауты `api` и подсветки.
      const id = [...timers.keys()].pop()
      assert.ok(id !== undefined, 'петля опроса не поставила следующий тик')
      const fire = timers.get(id)
      timers.delete(id)
      fire()
      await flush()
    },
    restore() {
      Object.assign(globalThis, {
        fetch: saved.fetch,
        setTimeout: saved.setTimeout,
        setInterval: saved.setInterval,
        clearTimeout: saved.clearTimeout,
        clearInterval: saved.clearInterval,
      })
      Date.now = saved.now
    },
  }
}

test('страница дела не перезагружается целиком: ни location.reload, ни setInterval', () => {
  // `setInterval` в файле ровно один — часы в шапке тикают раз в секунду. Опрос
  // дела идёт `setTimeout`-петлёй: следующий тик ставится после ответа, иначе
  // медленный ответ накладывал бы тики друг на друга.
  assert.ok(!app.includes('location.reload'), 'страница перезагружается целиком')
  const intervals = app.match(/setInterval\(/g) || []
  assert.equal(intervals.length, 1, `setInterval не один: ${intervals.length}`)
  assert.match(app, /ticker = setInterval\(tickStamp, 1000\)/, 'единственный setInterval — не часы шапки')
  assert.match(app, /dealPoll\.timer = setTimeout\(\(\) => void dealPollTick\(dealId\)/, 'тик ставится не setTimeout-петлёй')
})

test('подпись «обновлено» — единственный индикатор тика, и она не для диктора', () => {
  const css = read('dashboard/web/styles.css')
  assert.match(app, /class: 'refreshed', 'aria-live': 'off'/, 'у отметки нет aria-live="off"')
  assert.match(app, /`обновлено \$\{fmtClock\(at\)\}`/, 'отметка не печатает время');
  assert.match(css, /\.panel-h \.refreshed \{[^}]*color: var\(--text-3\)[^}]*font-size: 10px[^}]*margin-left: auto/, 'отметка не мелкая и не у правого края')
  // Никаких «обновляется…», спиннеров и тостов: тик ничего не объявляет.
  assert.ok(!/обновляется/i.test(app), 'на странице появилось «обновляется…»')
})

test('подсветка изменившегося — только цвет, и её отключает prefers-reduced-motion', () => {
  const css = read('dashboard/web/styles.css')
  assert.match(css, /\.fresh \{ transition: color \.2s ease; \}/, 'у подсветки нет перехода цвета')
  assert.match(css, /\.fresh\.is-fresh \{ color: var\(--accent\); \}/, 'изменившееся не подсвечено')
  assert.match(
    css,
    /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?\.fresh\.is-fresh \{ color: inherit; \}/,
    'при prefers-reduced-motion подсветка не выключается',
  )
  // Только цвет: масштаба и сдвига в правилах нет.
  const fresh = css.slice(css.indexOf('.fresh '), css.indexOf('.panel-b {'))
  assert.ok(!/transform|scale|animation/.test(fresh), 'подсветка двигает узел, а не только красит')
})

test('веб: тик правит узлы на месте — раскрытая попытка остаётся раскрытой', async (t) => {
  if (!jsdomRoot()) return t.skip('нет jsdom в ~/.hermes/hermes-agent/node_modules')
  const page = await liveDealPage([dealRound(true, DEAL_T0 + 60), dealRound(false, DEAL_T0 + 181)])
  try {
    const stamp = page.doc.querySelector('.panel-h .refreshed')
    const row = page.doc.querySelector('.att-row')
    const dur = row && row.querySelector('.att-row-dur')
    const stagesPanel = page.doc.querySelector('.deal-stages')
    const block = page.doc.querySelector('.stage-block')
    const scale = page.doc.querySelector('[data-key="scale"]')
    assert.ok(stamp, 'в шапке панели нет отметки «обновлено»')
    assert.ok(row && row.classList.contains('open'), 'живая попытка не раскрыта без клика')
    assert.match(dur.textContent, /идёт/)
    const before = stamp.textContent

    await page.tick()

    // Отметка обновилась — это весь индикатор тика.
    assert.notEqual(page.doc.querySelector('.panel-h .refreshed').textContent, before, 'отметка «обновлено» не изменилась')
    assert.match(page.doc.querySelector('.panel-h .refreshed').textContent, /^обновлено \d\d:\d\d:\d\d$/)
    // Тот же самый узел строки: раскрытие, обработчик и прокрутка на месте.
    assert.equal(page.doc.querySelector('.att-row'), row, 'строка попытки заменена, а не обновлена')
    assert.equal(row.classList.contains('open'), true, 'раскрытая попытка схлопнулась после тика')
    assert.match(row.querySelector('.att-row-dur').textContent, /3 мин/, 'значение попытки не обновилось')
    // Панели и блоки этапов — те же узлы: перерисована не страница, а значения.
    assert.equal(page.doc.querySelector('.deal-stages'), stagesPanel, 'панель этапов пересобрана целиком')
    assert.equal(page.doc.querySelector('.stage-block'), block, 'блок этапа пересобран целиком')
    assert.equal(page.doc.querySelector('[data-key="scale"]'), scale, 'панель шкалы пересобрана целиком')
    assert.equal(page.doc.querySelectorAll('.att-row').length, 1, 'строк попыток стало не одна')
    // Изменившееся значение подсвечено, и только оно.
    const fresh = [...page.doc.querySelectorAll('.is-fresh')].map((el) => el.getAttribute('data-fresh'))
    assert.ok(fresh.includes('kpi:duration'), `подсветка не попала на изменившееся: ${fresh.join(', ')}`)
    assert.ok(!fresh.includes('kpi:attempts'), 'подсветилось и то, что не менялось')
  } finally {
    page.restore()
  }
})

test('веб: при prefers-reduced-motion подсветка не навешивается', async (t) => {
  if (!jsdomRoot()) return t.skip('нет jsdom в ~/.hermes/hermes-agent/node_modules')
  const page = await liveDealPage([dealRound(true, DEAL_T0 + 60), dealRound(false, DEAL_T0 + 181)], { reduceMotion: true })
  try {
    await page.tick()
    assert.equal(page.doc.querySelectorAll('.is-fresh').length, 0, 'подсветка навешена при prefers-reduced-motion')
    // Тик при этом сделал свою работу: значение попытки обновилось.
    assert.match(page.doc.querySelector('.att-row-dur').textContent, /3 мин/)
  } finally {
    page.restore()
  }
})

test('веб: список дел и «Здоровье» опрашивают себя сами — тик ставит следующий', async (t) => {
  // Макс, 11.11: «а че страница дел не обновляется автоматом». Таймер взводился
  // ровно один раз при смене маршрута: срабатывал, и цепочка обрывалась.
  const root = jsdomRoot()
  if (!root) return t.skip('jsdom не найден')
  const { JSDOM } = await import(pathToFileURL(join(root, 'jsdom', 'lib', 'api.js')).href)
  const dir = mkdtempSync(join(tmpdir(), 'duty-web-poll-'))
  writeFileSync(join(dir, 'app.js'), app)
  writeFileSync(join(dir, 'model.js'), read('desktop/model.js'))
  const dom = new JSDOM(
    '<!doctype html><div id="app"></div><span id="stamp"></span><span id="ver"></span><span id="human"></span><div id="tabs"></div><button id="refresh"></button>',
    // `pretendToBeVisual` — не косметика: jsdom по умолчанию считает вкладку
    // скрытой (`document.hidden === true`), а скрытая страница по нашему же
    // правилу не опрашивает сервер и таймер не взводит.
    { url: 'http://localhost/#/', pretendToBeVisual: true },
  )
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  globalThis.Node = dom.window.Node
  globalThis.localStorage = dom.window.localStorage
  globalThis.location = dom.window.location

  const saved = {
    fetch: globalThis.fetch,
    setTimeout: globalThis.setTimeout,
    setInterval: globalThis.setInterval,
    clearTimeout: globalThis.clearTimeout,
    clearInterval: globalThis.clearInterval,
  }
  const seen = []
  const pending = new Map()
  let seq = 1
  globalThis.fetch = async (url) => {
    seen.push(String(url))
    return { ok: true, status: 200, json: async () => ({ deals: [], groups: [], finished: [], kpi: {}, source: {} }) }
  }
  globalThis.setTimeout = (fn, ms) => {
    const id = seq++
    pending.set(id, { fn, ms })
    return id
  }
  globalThis.setInterval = () => 0
  globalThis.clearTimeout = (id) => { pending.delete(id) }
  globalThis.clearInterval = () => {}

  const settle = async () => {
    for (let i = 0; i < 40; i += 1) await new Promise((resolve) => setImmediate(resolve))
  }
  const fireNext = async () => {
    const entry = [...pending.entries()].pop()
    if (!entry) return null
    pending.delete(entry[0])
    entry[1].fn()
    await settle()
    return entry[1].ms
  }

  try {
    await import(pathToFileURL(join(dir, 'app.js')).href)
    await settle()
    const first = seen.filter((url) => url.includes('/now')).length
    assert.ok(first >= 1, 'первый запрос списка не ушёл')

    // Один тик: сработавший таймер обязан и сходить за данными, и взвести
    // следующий — иначе страница замирает после первого обновления.
    const delay = await fireNext()
    assert.equal(delay, 10000, `период списка должен быть 10 с, а не ${delay}`)
    const second = seen.filter((url) => url.includes('/now')).length
    assert.ok(second > first, `после тика второго запроса не было (${first} → ${second})`)
    assert.ok(
      [...pending.values()].some((entry) => entry.ms === 10000),
      'после тика следующий таймер не взведён — цепочка обрывается',
    )
  } finally {
    Object.assign(globalThis, saved)
  }
})

test('веб: отказы опроса молчат до третьего и только тогда говорят', async (t) => {
  if (!jsdomRoot()) return t.skip('нет jsdom в ~/.hermes/hermes-agent/node_modules')
  // Три отказа подряд после удачного кадра: до третьего на экране не должно
  // появиться ни строчки — одиночный таймаут это не сломанное дело.
  const page = await liveDealPage([dealRound(true, DEAL_T0 + 60), { fail: true }, { fail: true }, { fail: true }])
  try {
    const stamp = page.doc.querySelector('.panel-h .refreshed').textContent
    await page.tick()
    assert.equal(page.doc.querySelectorAll('.banner').length, 0, 'ошибка нарисована с первого отказа')
    await page.tick()
    assert.equal(page.doc.querySelectorAll('.banner').length, 0, 'ошибка нарисована со второго отказа')
    // Панель при этом цела: страница не превратилась в экран ошибки.
    assert.equal(page.doc.querySelector('.panel-h .refreshed').textContent, stamp, 'отметка сдвинулась без ответа')
    assert.ok(page.doc.querySelector('.scale-plot'), 'шкала исчезла после отказов')
    await page.tick()
    const banner = page.doc.querySelector('.banner')
    assert.ok(banner, 'третий отказ подряд прошёл молча')
    assert.match(banner.textContent, /обновление не удалось/)
  } finally {
    page.restore()
  }
})


test('веб: таймлайн — сосиски на общей сетке, кликабельны только дело и название', () => {
  const app = read('dashboard/web/app.js')
  const css = read('dashboard/web/styles.css').replace(/\/\*[\s\S]*?\*\//g, '')

  // Строка — раскладка, а не ссылка: обработчика на ней нет.
  assert.match(app, /class: `tl-mini-row\$\{row\.open \? '' : ' is-done'\}`, 'data-key': `tl:\$\{row\.key\}` \}/,
    'веб: на строке таймлайна появился обработчик клика')
  // Дело и его название — ссылки.
  assert.match(app, /class: `tl-sausage\$\{row\.open \? ' is-live' : ''\}\$\{row\.exact/, 'веб: сосиска не рисуется')
  assert.match(app, /onclick: open\(row\.id\)/, 'веб: сосиска не открывает дело')
  assert.match(app, /class: 'tl-title',\s+href: `#\/deal\/\$\{encodeURIComponent\(row\.id\)\}`/,
    'веб: название дела не ссылка')

  // Одна сетка на все строки: слой `.tl-grid` с треком, без сетки внутри дорожки.
  assert.match(app, /class: 'tl-grid'/, 'веб: нет общего слоя сетки')
  assert.match(css, /\.tl-grid-track \{[^}]*repeating-linear-gradient/s, 'веб: сетка не общая')
  assert.doesNotMatch(css, /\.tl-mini-track \{[^}]*background-image/s, 'веб: дорожка снова рисует свою сетку')

  // Строка не выделяется: ни фона, ни рамки, ни курсора.
  assert.doesNotMatch(css, /\.tl-mini-row \{[^}]*background/s, 'веб: у строки свой фон')
  assert.doesNotMatch(css, /\.tl-mini-row \{[^}]*cursor/s, 'веб: строка выглядит кликабельной')
  assert.doesNotMatch(css, /\.tl-mini-row:hover/s, 'веб: строка подсвечивается на наведении')

  // Скругление одно — у сосиски; этапы стыкуются без скруглений.
  // Скругление маленькое и одно на всю фигуру: дело — полоса на шкале, а не
  // таблетка (09.11, Макс: «сосиску ты конечно слишком закруглил»).
  assert.match(css, /\.tl-sausage \{[^}]*border-radius: 3px/s, 'веб: у сосиски нет единой формы')
  assert.doesNotMatch(css, /\.tl-seg \{[^}]*border-radius/s, 'веб: у этапа своё скругление')

  // Заливка — профиль этапа (соседние этапы различаются цветом), состояние —
  // подсветка верхнего и нижнего краёв (09.11, Макс).
  assert.match(app, /class: `tl-seg r-\$\{segment\.role\} shade-\$\{segment\.shade\} t-\$\{segment\.tone\}/, 'веб: роль этапа не приходит из шейпера')
  assert.doesNotMatch(css, /--st-[1-8]/, 'веб: палитра «свой цвет на этап» осталась')
  // Роль → цвет: постоянное соответствие, у каждой роли своя краска.
  for (const role of ['planner', 'implementer', 'documenter', 'tester', 'reviewer', 'publisher']) {
    assert.match(css, new RegExp(`\\.tl-seg\\.r-${role} \\{ --role-base: var\\(--role-${role}\\); \\}`), `веб: у роли ${role} нет своей краски`)
  }
  // Состояние больше не красит полосу: пройден и идёт выглядят цветом роли,
  // отказ — штрихованием, «идёт» относится к делу целиком и живёт пульсом.
  // Состояние — ореол снаружи полосы, а не рамка и не внутренняя заливка: тело
  // дела целиком остаётся цветом роли, а свет ложится вокруг него.
  // Отказ — красное штрихование поверх цвета роли, а не свечение по краям.
  assert.match(css, /\.tl-seg\.is-failed \{[^}]*repeating-linear-gradient\(\s*135deg/s, 'веб: отказ без штрихования')
  assert.match(css, /\.tl-seg\.is-failed \{[^}]*var\(--danger\)/s, 'веб: штрихование не красное')
  assert.doesNotMatch(css, /\.tl-seg::before/s, 'веб: подсветка по краям этапа осталась')
  // «В работе» — пульс вокруг всего дела, а не вокруг отдельного этапа.
  assert.match(css, /\.tl-sausage\.is-live \{ animation: tl-live/, 'веб: у дела в работе нет пульса')
  assert.match(css, /@keyframes tl-live \{/, 'веб: пульс не описан')
  assert.match(app, /tl-sausage\$\{row\.open \? ' is-live' : ''\}/, 'веб: разметка не помечает живое дело')
  assert.doesNotMatch(css, /\.tl-seg\.t-(green|blue|yellow|gray) \{/s, 'веб: состояние всё ещё красит полосу этапа')
  assert.doesNotMatch(css, /\.tl-seg::after \{[^}]*inset: 0/s, 'веб: ореол всё ещё лежит на самой полосе')
  // Внешний свет требует не обрезать тело дела: иначе ореол срежется по краю.
  assert.match(css, /\.tl-sausage \{[^}]*overflow: visible/s, 'веб: сосиска обрезает свечение')
  assert.match(css, /\.tl-sausage \.tl-seg:first-child \{[^}]*border-top-left-radius: 3px/s, 'веб: скругление не переехало на крайние этапы')

  // Окно таймлайна выбирается: пресеты и ввод с клавиатуры.
  assert.match(app, /TL_WINDOWS\.map/, 'веб: нет пресетов окна')
  assert.match(app, /const sec = parseWindow\(input\.value\)/, 'веб: ввод окна не разбирается')
  assert.match(app, /dealTimeline\(model\.deals, done, model\.now, spans, tlWindow\.sec\)/, 'веб: окно не доходит до шейпера')
})


test('веб: завершённые — без сворачивания, «ещё» под списком, скелетоны без моргания', () => {
  const app = read('dashboard/web/app.js')
  const css = read('dashboard/web/styles.css').replace(/\/\*[\s\S]*?\*\//g, '')

  // Сворачивания нет: шапка — заголовок панели, тела блок не прячет.
  assert.match(app, /class: 'panel-h finished-head'/, 'веб: шапка завершённых не заголовок')
  assert.doesNotMatch(app, /finished\.open/, 'веб: вернулось сворачивание блока')
  assert.doesNotMatch(app, /finished-chev/, 'веб: вернулся шеврон-переключатель')
  assert.doesNotMatch(css, /\.finished-head:hover/, 'веб: шапка снова выглядит кликабельной')

  // Кнопка подгрузки — по центру под таблицей и заметная (акцентная рамка).
  assert.match(css, /\.grp-more \{\s*display: flex;\s*justify-content: center;/s, 'веб: кнопка группы не по центру')
  assert.match(css, /\.grp-more \.more-btn \{[^}]*border-color: color-mix\(in srgb, var\(--accent\)/s, 'веб: кнопка подгрузки не выделена')

  // Кнопка подгрузки группы — после таблицы, в футере группы.
  const group = app.slice(app.indexOf('function finishedGroupSection'))
  const tableAt = group.indexOf("h(\n      'table'")
  const moreAt = group.indexOf("class: 'grp-more'")
  assert.ok(tableAt > -1 && moreAt > tableAt, 'веб: кнопка группы стоит не под таблицей')

  // Подгрузка рисует скелетоны и держит их минимум SKELETON_MIN_MS.
  assert.match(app, /const SKELETON_MIN_MS = 260/, 'веб: скелетоны не держатся минимальное время')
  assert.match(app, /const pending = group\.loading && !group\.done/, 'веб: скелетоны подгрузки не считаются')
  assert.match(app, /skelRow\(index, 'pending'\)/, 'веб: скелетоны подгрузки не рисуются')

  // Вход играет один раз: у блока и групп анимация только под `.is-enter`.
  assert.match(css, /\.finished-body\.is-enter \{ animation: finished-in/, 'веб: вход блока играет всегда')
  assert.match(css, /\.finished-body\.is-enter \.finished-group \{\s*animation: finished-in/, 'веб: вход группы играет всегда')
  assert.doesNotMatch(css, /^\.finished-body \{[^}]*animation/sm, 'веб: у блока входная анимация вне is-enter')

  // Строки переиспользуются по ключу — иначе блок моргает на каждой подгрузке.
  assert.match(app, /'data-key': `finished-row:\$\{deal\.id\}`/, 'веб: строки без ключа для morph')
  assert.match(app, /'data-key': `finished-group:\$\{group\.repo\}`/, 'веб: группы без ключа для morph')
})


/* ----------------- маркеры одной точки оси — группа (0.1.0) ---------------- */

/**
 * Дело, закрытое своим приговором: `collected` и `completed` — в одну секунду.
 *
 * `pctOf(event.at)` у обоих одинаковый, палки встают друг на друга, и по ленте
 * не понять, что дело закрыл именно приговор. Шейпер сводит их в группу —
 * поверхность обязана нарисовать всех участников, а не голову группы.
 */
function verdictClosedDeal() {
  const at = DEAL_T0 + 180
  return {
    detail: {
      id: DEAL_ID,
      title: 'дело закрыто приговором',
      repo: '/x/hermes-duty',
      status: 'done',
      terminal: true,
      now: at,
      created_at: DEAL_T0,
      timeline: [
        { kind: 'created', at: DEAL_T0, summary: '' },
        { kind: 'collected', at, summary: 'ревью приняло работу' },
        { kind: 'completed', at, summary: 'все этапы пройдены', cause: 'all_stages_done', stages: ['impl'] },
      ],
      stages: [{ id: 'impl', kind: 'agent', role: 'implementer', state: 'done', needs: [], attempts: [] }],
    },
    attempts: {
      now: at,
      attempts: [
        { stage: 'impl', attempt: 1, start: DEAL_T0, end: at, status: 'done', outcome: 'done', duration_sec: 180 },
      ],
    },
  }
}

test('веб: приговор и закрытие в одну секунду — один маркер с обоими видами', async (t) => {
  if (!jsdomRoot()) return t.skip('нет jsdom в ~/.hermes/hermes-agent/node_modules')
  const { detail, attempts } = verdictClosedDeal()
  const doc = await renderDealPanel(detail, attempts)

  const marks = [...doc.querySelectorAll('.tl-marks .tl-mark')]
  assert.equal(marks.length, 1, `на точке оси ${marks.length} маркеров: группа не собрана или маркер потерян`)
  const [mark] = marks
  assert.ok(mark.className.includes('is-grouped'), `группа не помечена: ${mark.className}`)

  // Палка на каждого участника — своим тоном: приговор синий, закрытие серое.
  const bars = [...mark.querySelectorAll('i')]
  assert.deepEqual(bars.map((bar) => bar.className), ['t-blue', 't-gray'], 'палки группы не своих тонов')

  // Время не двинуто: группа стоит на координате своих событий (конец оси).
  assert.equal(mark.style.left, '100%', `группа сдвинута с точки оси: ${mark.style.left}`)

  // Подпись-итог называет все события точки, а не первое.
  const tip = mark.getAttribute('data-tip') || ''
  assert.ok(tip.includes('вердикт'), `в подсказке группы нет приговора: ${tip}`)
  assert.ok(tip.includes('закрыто'), `в подсказке группы нет закрытия: ${tip}`)

  // Легенда знает оба вида: «закрыто» не пропало вместе с головой группы.
  const legend = [...doc.querySelectorAll('.scale-legend span')].map((span) => span.textContent)
  assert.ok(legend.some((text) => text.includes('вердикт')), `в легенде нет приговора: ${legend.join(' | ')}`)
  assert.ok(legend.some((text) => text.includes('закрыто')), `в легенде нет закрытия: ${legend.join(' | ')}`)
})

test('веб: одиночный маркер остаётся прежним — одна палка без класса группы', async (t) => {
  if (!jsdomRoot()) return t.skip('нет jsdom в ~/.hermes/hermes-agent/node_modules')
  const { detail, attempts } = verdictClosedDeal()
  const half = DEAL_T0 + 90
  detail.status = 'running'
  detail.terminal = false
  detail.now = half
  detail.timeline = [
    { kind: 'created', at: DEAL_T0, summary: '' },
    { kind: 'question_asked', at: DEAL_T0 + 45, summary: 'q-1' },
  ]
  attempts.now = half
  attempts.attempts = [{ stage: 'impl', attempt: 1, start: DEAL_T0, end: half, status: 'done', outcome: 'done', duration_sec: 90 }]
  const doc = await renderDealPanel(detail, attempts)

  const marks = [...doc.querySelectorAll('.tl-marks .tl-mark')]
  assert.equal(marks.length, 1, `маркеров ${marks.length}, а событие одно`)
  const [mark] = marks
  assert.ok(!mark.className.includes('is-grouped'), `одиночный маркер помечен группой: ${mark.className}`)
  assert.equal(mark.querySelectorAll('i').length, 1, 'у одиночного маркера больше одной палки')
  assert.equal(mark.style.left, '50%', `одиночный маркер сдвинут: ${mark.style.left}`)
})
