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

const pluginRoot = new URL('..', import.meta.url).pathname
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
    .concat('\nexport { DutyLane, DutyAttemptRow, ScalePanel }\n')
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

  // Потолок назван числом на экране, а не только в тултипе.
  assert.match(html, /class="[^"]*\bduty-tl-more\b[^>]*>\+6</, 'надбавка +6 не отрисована')
  // Ось — линейка: метки-времена и вертикальная сетка по ним (макет d-merge).
  const scaleModel = (await import(pathToFileURL(join(pluginRoot, 'desktop/model.js')).href)).shapeDealTime(
    { now: 2_000, terminal: false, timeline: [{ kind: 'created', at: 1_000, summary: '' }], stages: [{ id: 'impl' }] },
    { attempts: sampleBars.map((bar) => ({ ...bar, stage: 'impl', attempt: bar.number, started_at: 1_000, ended_at: 2_000 })) },
  )
  const panel = renderToStaticMarkup(React.createElement(mod.ScalePanel, { scale: scaleModel }))
  const gridLines = (panel.match(/duty-scale-grid-line/g) || []).length
  assert.ok(gridLines >= 3, `сетка оси: линий ${gridLines}, должно быть по числу меток`)
  assert.match(panel, /duty-scale-tick[^>]*>\s*нач\. /, 'край оси подписан словом «нач.»')
  assert.match(panel, /duty-scale-tick[^>]*>\s*кон\. /, 'край оси подписан словом «кон.»')
  // Между краями — подписи без слов: чистое время.
  assert.match(panel, /duty-scale-tick align-center[^>]*>\s*\d\d:\d\d:\d\d/, 'нет меток-времени между краями')

  // Класс оборванного вызова — на элементе, а не подстрокой где-то в CSS: плагин
  // держит стили строкой в этом же модуле, и поиск подстроки «duty-tl-unfinished»
  // зеленел бы на тексте правила без единого чипа.
  assert.match(
    html,
    /class="[^"]*\bduty-tl-unfinished\b/,
    'оборванный вызов (ms:null) должен иметь класс на элементе',
  )
})
