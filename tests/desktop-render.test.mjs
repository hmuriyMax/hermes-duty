/**
 * Десктопная поверхность: разметка таймлайна рендерится и значок вида этапа —
 * настоящий `svg` с классом, который ловит правило из CSS плагина.
 *
 *   node --test tests/desktop-render.test.mjs
 *
 * Зачем: `desktop/plugin.js` целиком проверяется только глазами в приложении, и
 * регрессия «класс есть, правило есть, вместе не работают» проходит незамеченной
 * (так и случилось в 0.0.8 на вебе). Здесь компонент `DutyLane` рендерится через
 * `react-dom/server` — тот же React, что и в приложении, — и результат сверяется
 * с CSS, вшитым в плагин.
 *
 * React берётся из окружения Hermes (`~/.hermes/hermes-agent/node_modules`):
 * у плагина нет своих зависимостей, а тянуть node_modules в репозиторий ради
 * одного теста незачем. Нет React — тест честно пропускается, а не зеленеет.
 */

import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, mkdirSync, readFileSync, symlinkSync, writeFileSync } from 'node:fs'
import { homedir, tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'

/** Исходник десктопного плагина (с вшитым шейпером). */
function pluginSource() {
  return readFileSync(join(pluginRoot, 'desktop/plugin.js'), 'utf8')
}

const pluginRoot = new URL('..', import.meta.url).pathname
/** Окно jsdom последнего монтажа: держим ссылку, чтобы оно не собиралось GC. */
let keepWindow = null
const read = (path) => readFileSync(join(pluginRoot, path), 'utf8')

/** Где лежит React: свои node_modules у плагина не заводятся. */
function reactRoot() {
  const candidates = [
    process.env.HERMES_REACT_ROOT,
    join(homedir(), '.hermes', 'hermes-agent', 'node_modules'),
  ].filter(Boolean)
  return candidates.find((dir) => existsSync(join(dir, 'react-dom', 'package.json'))) || ''
}

/** Копия плагина, которую можно импортировать в Node: SDK и React подменены. */
function moduleForRender(root) {
  const dir = mkdtempSync(join(tmpdir(), 'duty-render-'))
  mkdirSync(join(dir, 'node_modules'))
  for (const name of ['react', 'react-dom', 'scheduler']) {
    const target = join(root, name)
    if (existsSync(target)) symlinkSync(target, join(dir, 'node_modules', name), 'dir')
  }
  writeFileSync(
    join(dir, 'sdk.mjs'),
    [
      'export const Button = (props) => null',
      'export const Input = (props) => null',
      'export const Codicon = (props) => null',
      'export const Streamdown = (props) => null',
      '',
    ].join('\n'),
  )
  const source = read('desktop/plugin.js')
    .replace(/from '@hermes\/plugin-sdk'/, "from './sdk.mjs'")
    .concat(
      '\nexport { DutyLane, DutyAttemptRow, ScalePanel, KpiStrip, SpecStrip, CallInspector, DutyMiniTimeline, FinishedBlock, shapeFinishedRow, dealTimeline, DutyPage }\n',
    )
  const file = join(dir, 'plugin.mjs')
  writeFileSync(file, source)
  return file
}

const sampleBars = [
  {
    kind: 'shell',
    role: '',
    number: 1,
    title: 'попытка 1',
    calls: [
      { name: 'shell.exec', at: 2_000, ms: 500 },
      { name: 'read_file', at: 9_000, ms: null },
    ],
    callsOmitted: 6,
    outcomeText: 'готово',
    tone: 'green',
    clockStart: '12:49',
  },
]

test('десктоп: DutyLane рисует svg-значок, который ловит правило CSS', async (t) => {
  const root = reactRoot()
  if (!root) return t.skip('нет React в ~/.hermes/hermes-agent/node_modules')

  const mod = await import(pathToFileURL(moduleForRender(root)).href)
  const React = (await import(pathToFileURL(join(root, 'react', 'index.js')).href)).default
  const { renderToStaticMarkup } = await import(
    pathToFileURL(join(root, 'react-dom', 'server.node.js')).href
  )

  const html = renderToStaticMarkup(
    React.createElement(mod.DutyLane, {
      lane: { label: 'pinned-review-service' },
      bars: sampleBars,
      scale: { axis: { start: 0, end: 60_000 } },
    }),
  )

  // Класс стоит на самом svg — иначе правило `.duty-tl-kind-ico{width:12px…}`
  // до него не достанет.
  assert.match(html, /<svg[^>]*class="duty-tl-kind-ico"/, 'значок должен быть svg с классом')

  const css = read('desktop/plugin.js').replace(/\/\*[\s\S]*?\*\//g, '')
  const at = css.indexOf('.duty-tl-kind-ico{')
  assert.ok(at > 0, 'нет правила .duty-tl-kind-ico')
  const rule = css.slice(at, css.indexOf('}', at))
  for (const declaration of ['width:12px', 'height:12px', 'fill:none', 'stroke:currentColor']) {
    assert.ok(rule.includes(declaration), `.duty-tl-kind-ico: нет ${declaration}`)
  }

  // Число вызовов названо на подписи попытки, а не только в тултипе.
  assert.match(html, /class="[^"]*\bduty-tl-more\b[^"]*"[^>]*>8 вызовов</, 'сумма вызовов не отрисована')
  // Потолок данных — единственное, что помечается: `is-cut`.
  assert.match(html, /class="[^"]*\bduty-tl-more is-cut\b/, 'усечённые вызовы не помечены')
  // Ось — линейка: метки-времена и вертикальная сетка по ним (макет d-merge).
  const modelMod = await import(pathToFileURL(join(pluginRoot, 'desktop/model.js')).href)
  const scaleModel = modelMod.shapeDealTime(
    {
      now: 2_000,
      terminal: true,
      // Журнал с фактами всех семейств: вопрос человеку, вердикт, закрытие.
      timeline: [
        { kind: 'created', at: 1_000, summary: '' },
        { kind: 'question_asked', at: 1_200, summary: 'нужно ок' },
        { kind: 'question_answered', at: 1_300, summary: 'ок' },
        { kind: 'collected', at: 1_800, summary: 'вердикт: собрано' },
        { kind: 'completed', at: 2_000, summary: 'закрыто' },
      ],
      stages: [{ id: 'impl', needs: [] }],
    },
    {
      attempts: sampleBars.map((bar, index) => ({
        ...bar,
        stage: 'impl',
        attempt: bar.number,
        // Ручка попыток зовёт границы `start`/`end`: именно их читает шейпер.
        start: 1_000 + index * 100,
        end: 1_600 + index * 100,
        duration_sec: 500,
      })),
    },
  )
  const panel = renderToStaticMarkup(React.createElement(mod.ScalePanel, { scale: scaleModel }))
  const gridLines = (panel.match(/duty-scale-grid-line/g) || []).length
  assert.ok(gridLines >= 3, `сетка оси: линий ${gridLines}, должно быть по числу меток`)
  assert.match(panel, /duty-scale-tick[^>]*>\s*нач\. /, 'край оси подписан словом «нач.»')
  assert.match(panel, /duty-scale-tick[^>]*>\s*кон\. /, 'край оси подписан словом «кон.»')
  // Между краями — подписи без слов: чистое время.
  assert.match(panel, /duty-scale-tick align-center[^>]*>\s*\d\d:\d\d:\d\d/, 'нет меток-времени между краями')

  // Легенда семейств — в левой колонке, ровно из шейпера (`scale.tools`), с числом.
  const keysHtml = renderToStaticMarkup(React.createElement(mod.ScalePanel, { scale: scaleModel }))
  assert.match(keysHtml, /duty-scale-keys/, 'нет колонки легенды')
  const keyBlocks = (keysHtml.match(/duty-scale-key"/g) || []).length
  assert.equal(keyBlocks, scaleModel.tools.length, `блоков легенды ${keyBlocks}, семейств ${scaleModel.tools.length}`)
  for (const item of scaleModel.tools) {
    assert.ok(
      new RegExp(`${item.label}<\\/span><span class="duty-scale-key-n">${item.count}<`).test(keysHtml),
      `у семейства ${item.label} нет числа ${item.count} рядом с подписью`,
    )
  }

  // Легенда — переключатель: клик по семейству гасит чужие вызовы на дорожках.
  const legendHtml = renderToStaticMarkup(
    React.createElement(mod.ScalePanel, { scale: scaleModel, toolFilter: ['read'], onToggleTool: () => {} }),
  )
  const pressed = (legendHtml.match(/aria-pressed="true"/g) || []).length
  assert.equal(pressed, 1, `выбранных семейств в легенде ${pressed}, ожидалось 1`)
  const tones = new Set(scaleModel.bars.flatMap((bar) => modelMod.attemptView(bar, scaleModel).chips.map((chip) => chip.tone)))
  const dimmed = (legendHtml.match(/duty-tl-dim/g) || []).length
  const shown = scaleModel.bars.reduce((sum, bar) => sum + modelMod.attemptView(bar, scaleModel).chips.length, 0)
  assert.ok(dimmed > 0 && dimmed < shown, `погашено ${dimmed} из ${shown} при семействах ${[...tones].join(',')}`)
  // Без фильтра ничего не гаснет: подсветка — состояние экрана, а не разметки.
  assert.doesNotMatch(keysHtml, /duty-tl-dim/, 'без фильтра часть вызовов погашена')

  // Ряды маркеров: вердикты/вопросы/публикация/засечки — отдельными строками.
  // Маркеры живут внутри строки попытки, фон показывает её длительность,
  // пауза вопроса подсвечена — отдельных рядов под шкалой больше нет.
  const banded = scaleModel.bars.find((bar) => bar.start !== null && bar.end !== null)
  assert.ok(banded, 'в фикстуре нет закрытой попытки — проверка фона потеряла предмет')
  const view = modelMod.attemptView(banded, scaleModel)
  const rowHtml = renderToStaticMarkup(React.createElement(mod.DutyAttemptRow, { view }))
  assert.match(rowHtml, /duty-tl-band/, 'нет фона-полосы длительности попытки')
  assert.match(rowHtml, /duty-tl-lbl[^>]*data-tip/, 'у подписи строки нет подсказки с временем начала/конца')
  assert.doesNotMatch(rowHtml, /duty-tl-tick/, 'в строке остались засечки старта/конца попытки')
  assert.doesNotMatch(rowHtml, /duty-smk-row/, 'вернулись отдельные ряды под шкалой')

  // KPI-строка: пять чисел шапки из шейпера.
  const kpiHtml = renderToStaticMarkup(React.createElement(mod.KpiStrip, { scale: scaleModel }))
  assert.equal((kpiHtml.match(/duty-kpi-cell/g) || []).length, scaleModel.kpi.length, 'KPI-ячеек не столько, сколько пунктов шейпера')
  for (const item of scaleModel.kpi) assert.ok(kpiHtml.includes(item.value), `в KPI нет значения ${item.value}`)

  // Граф спеки: узлы на месте, рёбра — из `needs`.
  // Граф — компактной полосой: узлы 96×30 (две строки текста), а не
  // прямоугольники на пол-экрана. Геометрию задаёт шейпер (`SPEC_NODE`).
  const specHtml = renderToStaticMarkup(React.createElement(mod.SpecStrip, { spec: scaleModel.spec }))
  if (scaleModel.spec) {
    assert.equal((specHtml.match(/duty-spec-node/g) || []).length, scaleModel.spec.nodes.length, 'узлов графа не столько, сколько этапов')
    assert.match(specHtml, /duty-spec-strip/, 'граф не в компактной полосе')
    assert.ok(scaleModel.spec.nodeW <= 96 && scaleModel.spec.nodeH <= 30, `узел графа крупный: ${scaleModel.spec.nodeW}×${scaleModel.spec.nodeH}`)
    // Ребро рисуется кривой из вида и заканчивается острием-стрелкой.
    const edges = scaleModel.spec.edges.length
    assert.equal((specHtml.match(/duty-spec-edge/g) || []).length, edges, 'рёбер графа не столько, сколько зависимостей')
    assert.equal((specHtml.match(/duty-spec-arrow/g) || []).length, edges, 'у рёбер нет стрелок')
    // В узле две строки текста: id и слово состояния.
    assert.match(specHtml, /duty-spec-id[^<]*<\/text>[\s\S]{0,160}duty-spec-state/, 'в узле нет второй строки с состоянием')
  }

  // Инспектор: пустой зовёт выбрать вызов, с вызовом называет инструмент и время.
  const emptyInsp = renderToStaticMarkup(React.createElement(mod.CallInspector, { call: null }))
  assert.match(emptyInsp, /выбери вызов/, 'пустой инспектор не подсказывает, что делать')
  const chip = scaleModel.bars.flatMap((bar) => modelMod.attemptView(bar, scaleModel).chips)[0]
  const insp = renderToStaticMarkup(React.createElement(mod.CallInspector, { call: chip }))
  assert.ok(insp.includes(chip.name) && insp.includes(chip.clock), 'инспектор не называет инструмент и время')

  // Разворот строки не должен держаться фокусом внутри (клик по чипу): только :hover.
  assert.doesNotMatch(
    pluginSource(),
    /duty-tl-row:focus-within/,
    'разворот строки по :focus-within — строка останется открытой после клика',
  )

  // Класс оборванного вызова — на элементе, а не подстрокой где-то в CSS: плагин
  // держит стили строкой в этом же модуле, и поиск подстроки «duty-tl-unfinished»
  // зеленел бы на тексте правила без единого чипа.
  assert.match(
    html,
    /class="[^"]*\bduty-tl-unfinished\b/,
    'оборванный вызов (ms:null) должен иметь класс на элементе',
  )
})

test('десктоп: к React не обращаются мимо импорта', () => {
  // Поломка, которую этот тест держит: в `DealScreen` стояло `React.useState`
  // без импорта `React` — экран дела падал с `ReferenceError` и не открывался
  // вовсе, а серверный рендер компонентов этого не видел.
  const source = pluginSource()
  const bare = source.match(/(^|[^.\w])React\./g) || []
  if (!bare.length) return
  assert.match(
    source,
    /import\s+React|const\s+React\s*=/,
    `в плагине ${bare.length} обращений к React, но React не импортирован`,
  )
})

test('десктоп: экран «Дело» монтируется и рисует дорожки', async (t) => {
  const reactModules = reactRoot()
  if (!reactModules) return t.skip('нет React в ~/.hermes/hermes-agent/node_modules')
  if (!existsSync(join(reactModules, 'jsdom', 'package.json'))) return t.skip('нет jsdom')

  // jsdom-монтаж вместо серверного рендера: только так выполняются эффекты, а
  // падение внутри компонента (`ReferenceError`, `TypeError`) видно тесту.
  const dir = mkdtempSync(join(tmpdir(), 'duty-mount-'))
  mkdirSync(join(dir, 'node_modules'))
  for (const name of ['react', 'react-dom', 'scheduler', 'jsdom']) {
    const target = join(reactModules, name)
    if (existsSync(target)) symlinkSync(target, join(dir, 'node_modules', name), 'dir')
  }
  writeFileSync(
    join(dir, 'sdk.mjs'),
    ['Button', 'Input', 'Codicon', 'Streamdown']
      .map((name) => `export const ${name} = (props) => null`)
      .join('\n'),
  )
  writeFileSync(
    join(dir, 'plugin.mjs'),
    pluginSource()
      .replace(/from '@hermes\/plugin-sdk'/, "from './sdk.mjs'")
      .concat("\nexport function __setRest(fn) { rest = fn }\nexport { DealScreen }\n"),
  )

  const { JSDOM } = await import(pathToFileURL(join(reactModules, 'jsdom', 'lib', 'api.js')).href)
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' })
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
  globalThis.HTMLElement = dom.window.HTMLElement
  globalThis.Element = dom.window.Element
  globalThis.Node = dom.window.Node
  globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0)
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id)

  let mounted = null
  const failures = []
  const origError = console.error
  console.error = (...args) => {
    const text = args.map((item) => (item && item.stack) || String(item)).join(' ')
    // Предупреждения act() — артефакт ручного монтажа, а не ошибка экрана.
    if (!/not wrapped in act/.test(text)) failures.push(text)
  }

  try {
    const mod = await import(pathToFileURL(join(dir, 'plugin.mjs')).href)
    mod.__setRest(async (path) => {
      if (path.startsWith('/deal/')) {
        return {
          repo: 'hermes-duty',
          title: 'дело из фикстуры',
          status: 'done',
          stages: [
            { id: 'check', needs: [], state: 'done' },
            { id: 'verify', needs: ['check'], state: 'done' },
          ],
          timeline: [
            { kind: 'question_asked', at: 1_200, summary: 'нужно ок', question_text: 'вопрос текстом' },
            { kind: 'question_answered', at: 1_300, summary: 'ок', answer_text: 'ответ текстом' },
          ],
          closed: { at: 2_000, cause: 'all_stages_done' },
        }
      }
      if (path.includes('/attempts')) {
        return {
          attempts: [
            {
              n: 1, attempt: 1, stage: 'check', start: 1_000, end: 1_500, outcome: 'done', status: 'completed',
              calls: [{ seq: 1, name: 'read_file', at: 1_050, ms: 120 }],
            },
            {
              n: 2, attempt: 2, stage: 'verify', start: 1_600, end: 2_000, outcome: 'failed', status: 'failed',
              kind: 'contract', reason: 'commit.foreign: чужое готовое',
              calls: [{ seq: 1, name: 'shell.exec', at: 1_650, ms: 400 }],
            },
          ],
        }
      }
      return {}
    })

    const React = (await import(pathToFileURL(join(reactModules, 'react', 'index.js')).href)).default
    const { createRoot } = await import(pathToFileURL(join(reactModules, 'react-dom', 'client.js')).href)
    const container = dom.window.document.getElementById('root')
    mounted = createRoot(container)
    mounted.render(
      React.createElement(mod.DealScreen, {
        sel: { id: 'd_fixture', titleFull: 'дело из фикстуры', repo: 'hermes-duty' },
        onBack: () => {},
      }),
    )
    await new Promise((resolve) => setTimeout(resolve, 800))

    assert.equal(failures.length, 0, `экран дела упал: ${failures[0] || ''}`.slice(0, 600))
    const text = container.textContent || ''
    assert.ok(text.length > 200, `экран дела пуст (${text.length} символов)`)
    assert.ok(container.querySelectorAll('.duty-tl-row').length >= 2, 'дорожки попыток не нарисованы')
    // Причина провала — в подсказке полосы, а не только словом «провал».
    const tips = [...container.querySelectorAll('.duty-tl-band')].map((node) => node.getAttribute('data-tip') || '')
    assert.ok(tips.some((tip) => tip.includes('отказ contract')), `нет причины провала в подсказке: ${tips.join(' | ')}`)
  } finally {
    console.error = origError
    // jsdom держит таймеры и окно: без размонтирования и `window.close()` тест
    // не отпускает event loop и `node --test` висит до внешнего таймаута.
    try {
      if (mounted) mounted.unmount()
    } catch {
      /* не смонтировалось — размонтировать нечего */
    }
    // Глобалы jsdom не снимаем: у плагина остаются отложенные таймеры (перерисовка
    // по `requestAnimationFrame`), и снятое `window` превращало их в
    // `ReferenceError: window is not defined` уже после теста. Файл идёт в своём
    // процессе `node --test`, так что подмена глобалов никому не мешает.
    keepWindow = dom.window
  }
})

/**
 * Строки столбца этапов: провал раскрыт без клика, обычная попытка — нет, а клик
 * по строке этапа и по строке попытки раскрывает их. Подсказка обрезанной
 * команды несёт полную строку: обрезка идёт в шейпере, CSS-эллипсис её не хранит.
 */
test('десктоп: строки этапов раскрываются кликом, провал виден без клика', async (t) => {
  const reactModules = reactRoot()
  if (!reactModules) return t.skip('нет React в ~/.hermes/hermes-agent/node_modules')
  if (!existsSync(join(reactModules, 'jsdom', 'package.json'))) return t.skip('нет jsdom')

  const dir = mkdtempSync(join(tmpdir(), 'duty-rows-'))
  mkdirSync(join(dir, 'node_modules'))
  for (const name of ['react', 'react-dom', 'scheduler', 'jsdom']) {
    const target = join(reactModules, name)
    if (existsSync(target)) symlinkSync(target, join(dir, 'node_modules', name), 'dir')
  }
  writeFileSync(
    join(dir, 'sdk.mjs'),
    ['Button', 'Input', 'Codicon', 'Streamdown'].map((name) => `export const ${name} = (props) => null`).join('\n'),
  )
  writeFileSync(
    join(dir, 'plugin.mjs'),
    pluginSource()
      .replace(/from '@hermes\/plugin-sdk'/, "from './sdk.mjs'")
      .concat("\nexport function __setRest(fn) { rest = fn }\nexport { DealScreen }\n"),
  )

  const { JSDOM } = await import(pathToFileURL(join(reactModules, 'jsdom', 'lib', 'api.js')).href)
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' })
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
  globalThis.HTMLElement = dom.window.HTMLElement
  globalThis.Element = dom.window.Element
  globalThis.Node = dom.window.Node
  globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0)
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id)

  // Длинная команда с абсолютным путём: шейпер режет её до 46 знаков, подсказка
  // обязана нести строку целиком.
  const fullCmd = '/Users/mshchemilkin/.hermes/hermes-agent/venv/bin/python3 -m pytest tests/ -q -p no:randomly'

  let mounted = null
  const failures = []
  const origError = console.error
  console.error = (...args) => {
    const text = args.map((item) => (item && item.stack) || String(item)).join(' ')
    if (!/not wrapped in act/.test(text)) failures.push(text)
  }

  const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

  try {
    const mod = await import(pathToFileURL(join(dir, 'plugin.mjs')).href)
    mod.__setRest(async (path) => {
      if (path.startsWith('/deal/')) {
        return {
          repo: 'hermes-duty',
          title: 'дело со столбцом этапов',
          status: 'running',
          // Документов нет: столбец этапов один на всю ширину.
          body: '',
          spec_text: '',
          stages: [
            {
              id: 'check',
              needs: [],
              kind: 'shell',
              state: 'done',
              attempts: [
                {
                  n: 1, run_id: 'check#1', outcome: 'done', started_at: 1_000, finished_at: 1_300, duration_sec: 300,
                  evidence: [{ cmd: fullCmd, exit: 0, started_at: 1_000 }],
                },
              ],
            },
            {
              id: 'verify',
              needs: ['check'],
              state: 'failed',
              attempts: [
                {
                  n: 1, run_id: 'verify#1', outcome: 'failed', kind: 'contract', started_at: 1_600,
                  finished_at: 2_000, duration_sec: 400, reason: 'commit.foreign: чужое готовое',
                  evidence: [{ cmd: 'npm test', exit: 1, started_at: 1_600 }],
                },
              ],
            },
          ],
          timeline: [{ kind: 'created', at: 900, summary: '' }],
        }
      }
      if (path.includes('/attempts')) return { attempts: [] }
      return {}
    })

    const React = (await import(pathToFileURL(join(reactModules, 'react', 'index.js')).href)).default
    const { createRoot } = await import(pathToFileURL(join(reactModules, 'react-dom', 'client.js')).href)
    const container = dom.window.document.getElementById('root')
    mounted = createRoot(container)
    mounted.render(
      React.createElement(mod.DealScreen, {
        sel: { id: 'd_fixture', titleFull: 'дело со столбцом этапов', repo: 'hermes-duty' },
        onBack: () => {},
      }),
    )
    await settle(800)

    assert.equal(failures.length, 0, `экран дела упал: ${failures[0] || ''}`.slice(0, 600))
    const blocks = [...container.querySelectorAll('.duty-stage-block')]
    assert.equal(blocks.length, 2, `строк этапов ${blocks.length}, а не 2: столбец не нарисован`)
    // Провал раскрыт без клика: в этапе `verify` видно и попытку, и доказательство.
    assert.equal(blocks[0].querySelectorAll('.duty-att-row').length, 0, 'обычная попытка раскрыта без клика')
    assert.equal(blocks[1].querySelectorAll('.duty-att-row').length, 1, 'провал свёрнут: его не видно без клика')
    assert.equal(blocks[1].querySelectorAll('.duty-att-ev').length, 1, 'доказательство провала не показано')

    // Клик по строке этапа раскрывает его попытки, повторный — сворачивает.
    const stageRow = blocks[0].querySelector('.duty-stage-row')
    assert.ok(stageRow, 'в этапе нет строки .duty-stage-row')
    stageRow.click()
    await settle(60)
    assert.equal(blocks[0].querySelectorAll('.duty-att-row').length, 1, 'клик по этапу не раскрыл попытки')
    assert.equal(blocks[0].querySelector('.duty-stage-row').getAttribute('aria-expanded'), 'true', 'строка этапа не пометила раскрытие')

    // Клик по строке попытки раскрывает доказательства; команда обрезана, полная — в title.
    blocks[0].querySelector('.duty-att-row').click()
    await settle(60)
    const ev = blocks[0].querySelector('.duty-att-ev')
    assert.ok(ev, 'клик по попытке не показал доказательства')
    const cmd = ev.querySelector('.duty-att-ev-cmd')
    assert.ok(cmd, 'в доказательстве нет строки команды')
    assert.equal(cmd.getAttribute('title'), fullCmd, 'подсказка не несёт полную команду')
    assert.ok(cmd.textContent.length < fullCmd.length, 'команда не обрезана')
    assert.ok(cmd.textContent.includes('python3 -m pytest'), `в обрезанной команде нет хвоста: ${cmd.textContent}`)
    assert.ok(!cmd.textContent.includes('/Users/'), `в обрезанной команде остался абсолютный путь: ${cmd.textContent}`)

    stageRow.click()
    await settle(60)
    assert.equal(blocks[0].querySelectorAll('.duty-att-row').length, 0, 'повторный клик не свернул этап')
  } finally {
    console.error = origError
    try {
      if (mounted) mounted.unmount()
    } catch {
      /* не смонтировалось — размонтировать нечего */
    }
    keepWindow = dom.window
  }
})

/**
 * Сжатый простой на оси: пара пунктиров по краям промежутка и подпись с его
 * временем. Без сжатия таких элементов нет вовсе — иначе «пустая» ось врала бы
 * про сжатие, которого не было.
 */
test('десктоп: сжатый простой подписан на оси, без сжатия — ни одного элемента', async (t) => {
  const root = reactRoot()
  if (!root) return t.skip('нет React в ~/.hermes/hermes-agent/node_modules')

  const mod = await import(pathToFileURL(moduleForRender(root)).href)
  const React = (await import(pathToFileURL(join(root, 'react', 'index.js')).href)).default
  const { renderToStaticMarkup } = await import(
    pathToFileURL(join(root, 'react-dom', 'server.node.js')).href
  )
  const modelMod = await import(pathToFileURL(join(pluginRoot, 'desktop/model.js')).href)

  /** Дело из двух попыток с паузой между ними; длительность паузы — в секундах. */
  const dealWithIdle = (idleSec) => {
    const second = 1 + 1_000 + idleSec
    return modelMod.shapeDealTime(
      { now: second + 3_000, terminal: true, timeline: [{ kind: 'created', at: 1, summary: '' }], stages: [{ id: 'impl', needs: [] }] },
      {
        attempts: [
          { stage: 'impl', attempt: 1, start: 1, end: 1_001, status: 'done', outcome: 'done' },
          { stage: 'impl', attempt: 2, start: second, end: second + 3_000, status: 'done', outcome: 'done' },
        ],
      },
    )
  }

  // Простой 45 минут: ось сжимается, промежуток подписан.
  const squeezed = dealWithIdle(45 * 60)
  assert.equal(squeezed.idle.gaps.length, 1, 'фикстура без сжатия — проверка потеряла предмет')
  const panel = renderToStaticMarkup(React.createElement(mod.ScalePanel, { scale: squeezed }))
  assert.match(panel, /<svg[^>]*class="duty-tl-idle-layer"/, 'сжатие рисуется не отдельным слоем оси')
  assert.equal((panel.match(/class="duty-tl-idle"/g) || []).length, 2, 'пунктиров не пара на промежуток')
  assert.match(panel, /class="duty-tl-idle-label"[^>]*text-anchor="middle"/, 'подпись не по центру промежутка')
  assert.ok(panel.includes('простой 45 мин'), `подписи простоя нет на оси: ${squeezed.idle.gaps[0].text}`)
  assert.match(panel, /<title>[^<]*сжато/, 'в подписи нет пояснения, что время сжато')
  // CSS плагина рисует пунктир, а не сплошную линию: иначе пара читается как
  // границы полосы, а не как «здесь время сжато».
  const css = pluginSource().replace(/\/\*[\s\S]*?\*\//g, '')
  assert.match(css, /\.duty-tl-idle\{[^}]*stroke-dasharray:2 3/, 'нет пунктира у .duty-tl-idle')
  assert.match(css, /\.duty-tl-idle-label\{[^}]*fill:var\(--ui-text-tertiary\)/, 'нет цвета у подписи сжатия')
  // Слой сжатия и сетка оси начинаются там же, где дорожка строки: после
  // колонки подписей (gutter + gap). При left:0 пунктир и подпись встают на
  // ~194 px левее промежутка и мерят проценты по ширине плагина, а не дорожки —
  // то есть подпись опровергает то, что нарисовано.
  const track = 'left:calc\\(var\\(--duty-tl-gutter, 186px\\) \\+ var\\(--duty-tl-gap, 8px\\)\\)'
  assert.match(css, new RegExp(`\\.duty-tl-idle-layer\\{[^}]*${track}`), 'слой сжатия не сдвинут на колонку подписей')
  assert.match(
    css,
    /\.duty-tl-idle-layer\{[^}]*width:calc\(100% - var\(--duty-tl-gutter, 186px\) - var\(--duty-tl-gap, 8px\)\)/,
    'ширина слоя сжатия — не ширина дорожки',
  )
  assert.match(css, new RegExp(`\\.duty-scale-grid\\{[^}]*${track}`), 'линии сетки оси сдвинуты относительно подписей')

  // Пауза в три минуты не сжимается — ни одного элемента сжатия на панели.
  const plain = dealWithIdle(3 * 60)
  assert.deepEqual(plain.idle.gaps, [], 'короткая пауза сжата')
  const plainPanel = renderToStaticMarkup(React.createElement(mod.ScalePanel, { scale: plain }))
  assert.doesNotMatch(plainPanel, /duty-tl-idle/, 'сжатие нарисовано там, где его нет')
})



/* ---------------- мини-таймлайн и завершённые (0.0.9) ---------------- */

const sampleNow = Date.parse('2026-11-09T15:00:00Z') / 1000

function sampleTimelineView(mod) {
  const deals = [
    {
      id: 'd_aaaaaaaa',
      title: 'собрать релиз',
      titleFull: 'собрать релиз',
      repo: '/Users/x/GolandProjects/hermes-duty',
      repoShort: 'hermes-duty',
      state: 'working',
      stateLabel: 'работает',
      tone: 'blue',
      createdAt: sampleNow - 5400,
      updatedAt: sampleNow - 60,
    },
  ]
  const spans = {
    // Форма `/spans`: пара start/end по попытке, живая — с end: null.
    d_aaaaaaaa: [
      { start: sampleNow - 5400, end: sampleNow - 3600, stage: 'plan', outcome: 'ok' },
      { start: sampleNow - 3600, end: null, stage: 'impl', outcome: null },
    ],
  }
  return mod.dealTimeline(deals, [], sampleNow, spans)
}

test('десктоп: мини-таймлайн — сосиска на общей сетке, кликабельны дело и название', async (t) => {
  const root = reactRoot()
  if (!root) return t.skip('нет React в ~/.hermes/hermes-agent/node_modules')

  const mod = await import(pathToFileURL(moduleForRender(root)).href)
  const React = (await import(pathToFileURL(join(root, 'react', 'index.js')).href)).default
  const { renderToStaticMarkup } = await import(pathToFileURL(join(root, 'react-dom', 'server.node.js')).href)

  const view = sampleTimelineView(mod)
  assert.equal(view.rows.length, 1)
  const html = renderToStaticMarkup(React.createElement(mod.DutyMiniTimeline, { view, onOpen: () => {} }))

  // Сетка — отдельный слой под всеми дорожками, а не рисунок строки.
  assert.match(html, /class="duty-mtl-grid"/, 'нет слоя сетки')
  assert.match(html, /class="duty-mtl-grid-track"/, 'нет дорожки сетки')
  assert.match(html, /--duty-mtl-step:/, 'шаг сетки не передан')
  // Сосиска — одно тело дела; этапы внутри без своих скруглений.
  assert.match(html, /class="duty-mtl-sausage/, 'нет сосиски')
  assert.match(html, /class="duty-mtl-seg t-/, 'нет полос этапов')
  assert.ok(!/duty-mtl-seg[^"]*" style="[^"]*border-radius/.test(html), 'у полосы этапа своё скругление')
  // Кликабельны дело и название: строка — только раскладка.
  const rowTag = html.slice(html.indexOf('<div class="duty-mtl-row'), html.indexOf('>', html.indexOf('<div class="duty-mtl-row')) + 1)
  assert.ok(!/role=|tabindex=/.test(rowTag), 'строка сама стала кликабельной')
  assert.match(html, /class="duty-mtl-title"[^>]*role="link"/, 'название не ссылка')
  assert.match(html, /class="duty-mtl-sausage[^"]*"[^>]*role="link"/, 'сосиска не ссылка')
  // Две ссылки на строку — название и сосиска, и больше ничего.
  assert.equal((html.match(/role="link"/g) || []).length, 2, 'в строке больше двух кликабельных мест')
  // Шкала: подписи стоят в тех же колонках, что дорожки.
  assert.match(html, /class="duty-mtl-axis-track"/, 'нет полотна шкалы')
  assert.match(html, /class="duty-mtl-tick"[^>]*style="left:/, 'нет подписей делений')
  // Легенда называет состояния, а не имена этапов.
  assert.match(html, /class="duty-mtl-legend"/, 'нет легенды')

  const css = read('desktop/plugin.js').replace(/\/\*[\s\S]*?\*\//g, '')
  const at = css.indexOf('.duty-mtl-sausage{')
  assert.ok(at > 0, 'нет правила .duty-mtl-sausage')
  const rule = css.slice(at, css.indexOf('}', at))
  assert.ok(rule.includes('border-radius:3px'), 'у сосиски не то скругление')
  assert.ok(!/999px/.test(rule), 'сосиска снова таблетка')
  // Подпись деления центрируется на своей линии — иначе время съезжает.
  const tickAt = css.indexOf('.duty-mtl-tick{')
  const tickRule = css.slice(tickAt, css.indexOf('}', tickAt))
  assert.ok(tickRule.includes('translateX(-50%)'), 'подпись деления не центрируется')
})

test('десктоп: блок завершённых — без сворачивания, «ещё» по центру под списком', async (t) => {
  const root = reactRoot()
  if (!root) return t.skip('нет React в ~/.hermes/hermes-agent/node_modules')

  const mod = await import(pathToFileURL(moduleForRender(root)).href)
  const React = (await import(pathToFileURL(join(root, 'react', 'index.js')).href)).default
  const { renderToStaticMarkup } = await import(pathToFileURL(join(root, 'react-dom', 'server.node.js')).href)

  const row = mod.shapeFinishedRow({
    id: 'd_bbbbbbbb',
    repo: '/Users/x/GolandProjects/hermes-duty',
    status: 'completed',
    finished_at: sampleNow - 600,
    title: 'закрыть сделку',
  })
  const repo = '/Users/x/GolandProjects/hermes-duty'
  // Контракт блока: снимок (`snapshot`) + карта показанных строк (`shown`).
  // Запросов у блока нет — листание в памяти, поэтому и мок без сети.
  const render = ({ count = 12, shown = {}, rows = [row], errors = [], truncated = false } = {}) =>
    renderToStaticMarkup(
      React.createElement(mod.FinishedBlock, {
        onPick: () => {},
        finished: {
          snapshot: {
            repos:
              count === 0
                ? []
                : [{ key: repo, repo, label: 'hermes-duty', count, deals: Array.from({ length: count }, () => row) }],
            total: count,
            truncated,
            errors,
            note: '',
          },
          shown,
          error: null,
          reload: () => {},
          reveal: () => {},
          revealAll: () => {},
        },
      }),
    )

  const loaded = render()
  // Кнопка — под таблицей и с курсом на остаток, а не общий сдвиг списка.
  const tableEnd = loaded.indexOf('</table>')
  const moreAt = loaded.indexOf('duty-more-row')
  assert.ok(tableEnd > 0 && moreAt > tableEnd, 'кнопка «ещё» стоит не под таблицей')
  assert.match(loaded, /class="duty-more-btn"[^>]*>ещё 5 · осталось 7</, 'кнопка не называет остаток')
  assert.match(loaded, /5 из 12/, 'нет счётчика группы')
  // Показано ровно пять строк, хотя в снимке двенадцать.
  assert.equal((loaded.match(/class="duty-row/g) || []).length, 5, 'на проект показано не пять строк')

  // Развернули на десяток — кнопка говорит о хвосте.
  const revealed = render({ shown: { [repo]: 10 } })
  assert.equal((revealed.match(/class="duty-row/g) || []).length, 10, 'раскрытие не добавило строк')
  assert.match(revealed, /ещё 2</, 'кнопка не называет остаток в две строки')

  // Всё показано — кнопка гаснет и говорит словами, а не остаётся яркой.
  const done = render({ count: 5 })
  assert.match(done, /duty-more-btn is-done/, 'состояние «всё» не отличается от кнопки')

  // Сворачивания нет: блок не прячет тело под переключателем.
  assert.ok(!/duty-finished-toggle|aria-expanded/.test(loaded), 'вернулось сворачивание блока')
  // Общая кнопка поднимает по пятёрке сразу во всех проектах.
  assert.match(loaded, /duty-more-all/, 'нет общей кнопки «показать ещё по 5»')

  // Усечённая выборка и непрочитанный store — разные факты, оба видны словами.
  assert.match(render({ truncated: true }), /показаны последние/, 'усечение списка не сказано')
  const failed = render({ count: 0, rows: [], errors: [{ source: 'deals', reason: 'database is locked' }] })
  assert.match(failed, /завершённые не прочитались/, 'ошибка чтения выдана за «дел нет»')
})

/**
 * Страница целиком: таймлайн на «Сейчас» и листание «Завершённых» — те же данные,
 * что у веба, и те же правила. Монтаж в jsdom нужен потому, что здесь работают
 * эффекты и состояние: без них не видно ни подгрузки по курсору, ни того, что
 * скелетоны появляются до ответа.
 */
test('десктоп: страница — таймлайн и развёрнутый блок завершённых на «Сейчас»', async (t) => {
  const reactModules = reactRoot()
  if (!reactModules) return t.skip('нет React в ~/.hermes/hermes-agent/node_modules')
  if (!existsSync(join(reactModules, 'jsdom', 'package.json'))) return t.skip('нет jsdom')

  const dir = mkdtempSync(join(tmpdir(), 'duty-page-'))
  mkdirSync(join(dir, 'node_modules'))
  for (const name of ['react', 'react-dom', 'scheduler', 'jsdom']) {
    const target = join(reactModules, name)
    if (existsSync(target)) symlinkSync(target, join(dir, 'node_modules', name), 'dir')
  }
  writeFileSync(
    join(dir, 'sdk.mjs'),
    ['Button', 'Input', 'Codicon', 'Streamdown']
      .map((name) => `export const ${name} = (props) => null`)
      .join('\n'),
  )
  writeFileSync(
    join(dir, 'plugin.mjs'),
    pluginSource()
      .replace(/from '@hermes\/plugin-sdk'/, "from './sdk.mjs'")
      .concat('\nexport function __setRest(fn) { rest = fn }\nexport { DutyPage }\n'),
  )

  const { JSDOM } = await import(pathToFileURL(join(reactModules, 'jsdom', 'lib', 'api.js')).href)
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' })
  globalThis.window = dom.window
  globalThis.document = dom.window.document
  Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
  globalThis.HTMLElement = dom.window.HTMLElement
  globalThis.Element = dom.window.Element
  globalThis.Node = dom.window.Node
  globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0)
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id)

  let mounted = null
  const failures = []
  const origError = console.error
  console.error = (...args) => {
    const text = args.map((item) => (item && item.stack) || String(item)).join(' ')
    if (!/not wrapped in act/.test(text)) failures.push(text)
  }

  const nowFixture = JSON.parse(read('tests/fixtures/now.json'))
  const repo = '~/go/src/gitlab.ozon.ru/sx/sx-widgets-api'
  const finishedRow = (index) => ({
    id: `d_f000000${index}`,
    repo,
    title: `закрытое дело ${index}`,
    status: 'completed',
    created_at: nowFixture.now - 7200,
    finished_at: nowFixture.now - 600 - index * 60,
    cause: 'all_stages_done',
    attempts: 1,
    questions: { asked: 0, answered: 0, waiting: 0 },
  })
  const dealsCalls = []

  try {
    const mod = await import(pathToFileURL(join(dir, 'plugin.mjs')).href)
    // Ответ СТАРОГО бэкенда: без `repos` — ровно то, на чём блок показывал
    // «0 из 0» (09.11). Группы считаются на клиенте, поэтому список работает.
    mod.__setRest(async (path) => {
      if (path === '/now') return nowFixture
      if (path.startsWith('/deals')) {
        dealsCalls.push(path)
        return {
          now: nowFixture.now,
          total: 12,
          limit: 200,
          truncated: false,
          deals: Array.from({ length: 12 }, (_, i) => finishedRow(i + 1)),
          statuses: ['completed'],
          note: '',
          errors: [],
        }
      }
      if (path.startsWith('/spans')) {
        return {
          spans: {
            d_a1b2c3d4: [
              { start: nowFixture.now - 3400, end: nowFixture.now - 2000, stage: 'impl', outcome: 'ok' },
              { start: nowFixture.now - 2000, end: null, stage: 'verify', outcome: null },
            ],
          },
        }
      }
      return {}
    })

    const React = (await import(pathToFileURL(join(reactModules, 'react', 'index.js')).href)).default
    const { createRoot } = await import(pathToFileURL(join(reactModules, 'react-dom', 'client.js')).href)
    const container = dom.window.document.getElementById('root')
    mounted = createRoot(container)
    mounted.render(React.createElement(mod.DutyPage))
    await new Promise((resolve) => setTimeout(resolve, 700))

    // «Сейчас»: таймлайн нарисован, сосиски лежат на общей сетке.
    assert.equal(failures.length, 0, `страница упала: ${failures[0] || ''}`.slice(0, 600))
    assert.ok(container.querySelector('.duty-mtl'), 'на «Сейчас» нет таймлайна')
    assert.ok(container.querySelectorAll('.duty-mtl-sausage').length >= 1, 'нет сосисок')
    assert.ok(container.querySelectorAll('.duty-mtl-seg').length >= 1, 'полосы этапов не пришли из /spans')
    assert.ok(container.querySelectorAll('.duty-mtl-tick').length >= 5, 'шкала без делений')

    // Отдельной страницы «Завершённые» больше нет — блок живёт на «Сейчас».
    const tabs = [...container.querySelectorAll('.duty-tab')].map((node) => node.textContent)
    assert.ok(!tabs.includes('Завершённые'), `вкладка «Завершённые» вернулась: ${tabs.join(', ')}`)
    // «Дело» — экран по клику, а не вкладка (как на вебе).
    assert.ok(!tabs.includes('Дело'), `вкладка «Дело» вернулась: ${tabs.join(', ')}`)
    await new Promise((resolve) => setTimeout(resolve, 600))

    assert.ok(container.querySelector('.duty-finished-block'), 'блока завершённых нет на «Сейчас»')
    const group = container.querySelector('.duty-finished-block .duty-group')
    assert.ok(group, 'группа завершённых не нарисована')
    assert.equal(group.querySelectorAll('tbody tr.duty-row').length, 5, 'на проект показано не пять строк')
    const more = group.querySelector('.duty-more-btn')
    assert.ok(more && more.textContent.includes('ещё 5'), 'нет кнопки «ещё 5» под списком')
    assert.ok(
      group.querySelector('table').compareDocumentPosition(more) & dom.window.Node.DOCUMENT_POSITION_FOLLOWING,
      'кнопка стоит не после таблицы',
    )

    // Листание в памяти: вторая пятёрка появляется сразу и без нового запроса.
    const before = dealsCalls.length
    more.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    await new Promise((resolve) => setTimeout(resolve, 120))
    assert.equal(group.querySelectorAll('tbody tr.duty-row').length, 10, 'вторая пятёрка не показалась')
    assert.equal(dealsCalls.length, before, 'листание сходило в сеть за пятёркой')
    more.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }))
    await new Promise((resolve) => setTimeout(resolve, 120))
    assert.equal(group.querySelectorAll('tbody tr.duty-row').length, 12, 'хвост не показался')
    assert.ok(group.querySelector('.duty-more-btn.is-done'), 'кнопка не погасла на последней строке')
    assert.match(dealsCalls[0], /limit=200/, 'история читается не одним запросом')
    assert.equal(failures.length, 0, `страница ругалась: ${failures[0] || ''}`.slice(0, 600))
  } finally {
    console.error = origError
    try {
      if (mounted) mounted.unmount()
    } catch {
      /* не смонтировалось — размонтировать нечего */
    }
    keepWindow = dom.window
  }
})
