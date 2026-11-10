/**
 * Разметка и CSS должны сходиться: значок вида этапа.
 *
 *   node --test tests/markup.test.mjs
 *
 * Повод — регрессия 0.0.8: разметка переехала с `i > svg` на `svg.tl-kind-ico`,
 * а правила в `styles.css` остались «потомковыми» (`.tl-kind-ico svg`). Ни один
 * прогон этого не видел: класс в разметке есть, правило в CSS есть, а вместе они
 * не работают. Тест сверяет пару «элемент + правило», а не наличие строк.
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')

/** CSS без комментариев: пояснение к правилу — не правило. */
const cssRules = (text) => text.replace(/\/\*[\s\S]*?\*\//g, '')

/** Объявления правила по точному селектору (без вложенности). */
function ruleBody(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = new RegExp(`(?:^|[},])\\s*${escaped}\\s*\\{([^}]*)\\}`, 'm').exec(css)
  return match ? match[1] : ''
}

/** Классы, которые разметка вешает на элемент `<тег>`. */
function classesOnTag(source, tag) {
  const out = new Set()
  const patterns = [
    new RegExp(`h\\(\\s*'${tag}',\\s*\\{[^}]*?class(?:Name)?:\\s*[\`'"]([^\`'"]+)`, 'g'),
  ]
  for (const pattern of patterns) {
    let hit
    while ((hit = pattern.exec(source)) !== null) {
      for (const name of hit[1].split(/\s+/)) if (name) out.add(name)
    }
  }
  return [...out]
}

test('веб: класс на самом svg, и правило — про него, а не про потомка', () => {
  const app = read('../dashboard/web/app.js')
  const css = cssRules(read('../dashboard/web/styles.css'))
  const classes = classesOnTag(app, 'svg')
  assert.ok(classes.length > 0, 'в разметке веба нет svg с классом — проверка потеряла предмет')
  for (const name of classes) {
    const own = ruleBody(css, `svg.${name}`)
    assert.ok(own, `нет правила svg.${name}: класс на svg, а правило про потомка`)
    for (const declaration of ['width', 'height', 'fill', 'stroke']) {
      assert.match(own, new RegExp(`(?:^|;|\\s)${declaration}\\s*:`), `svg.${name}: нет ${declaration}`)
    }
    assert.equal(ruleBody(css, `.${name} svg`), '', `.${name} svg — правило про потомка, оно не совпадёт`)
  }
})

test('десктоп: класс на самом svg, правило про него', () => {
  const raw = read('../desktop/plugin.js')
  const serialized = raw.replace(/\/\/ duty:model-start[\s\S]*?\/\/ duty:model-end/, '')
  const css = cssRules(serialized)
  const classes = classesOnTag(serialized, 'svg')
  assert.ok(classes.length > 0, 'в разметке десктопа нет svg с классом')
  for (const name of classes) {
    const own = ruleBody(css, `.${name}`)
    assert.ok(own, `нет правила .${name} для класса на svg`)
    for (const declaration of ['width', 'height', 'fill', 'stroke']) {
      assert.match(own, new RegExp(`(?:^|;|\\s)${declaration}\\s*:`), `.${name}: нет ${declaration}`)
    }
    assert.equal(ruleBody(css, `.${name} svg`), '', `.${name} svg — правило про потомка`)
  }
})

test('веб создаёт svg в SVG-namespace, а не через createElement', () => {
  const app = read('../dashboard/web/app.js')
  assert.match(app, /createElementNS\(\s*SVG_NS/, 'h() должен создавать svg через createElementNS')
  assert.match(app, /SVG_TAGS/, 'список не-HTML тегов должен быть явным')
  // У SVGElement `className` — геттер без сеттера: присваивание бросает исключение
  // и роняет всю отрисовку, поэтому класс ставится только атрибутом.
  assert.doesNotMatch(app, /el\.className\s*=/, 'класс у svg ставится setAttribute, а не className')
})

test('группа маркеров: класс в разметке и правило в CSS на обеих поверхностях', () => {
  // Регрессия 0.1.0: шейпер сводит маркеры одной точки оси в группу. Если
  // поверхность рисует голову группы одной палкой — «приговор + закрытие» снова
  // выглядит одним событием, а если у палок группы нет своих тонов, — все палки
  // цвета головы, и группа неотличима от одиночного маркера.
  const web = read('../dashboard/web/app.js')
  const webCss = cssRules(read('../dashboard/web/styles.css'))
  assert.match(web, /is-grouped/, 'веб: разметка не помечает группу маркеров')
  assert.match(ruleBody(webCss, '.tl-mark.is-grouped'), /display:\s*flex/, 'веб: палки группы не раскладываются столбиком')

  const serialized = read('../desktop/plugin.js').replace(/\/\/ duty:model-start[\s\S]*?\/\/ duty:model-end/, '')
  const pluginCss = cssRules(serialized)
  assert.match(serialized, /is-grouped/, 'десктоп: разметка не помечает группу маркеров')
  assert.match(ruleBody(pluginCss, '.duty-tl-mark.is-grouped'), /display:\s*flex/, 'десктоп: палки группы не раскладываются столбиком')

  for (const tone of ['green', 'red', 'yellow', 'blue', 'gray']) {
    assert.match(ruleBody(webCss, `.tl-mark i.t-${tone}`), /color:/, `веб: у палки группы нет своего тона ${tone}`)
    assert.match(
      ruleBody(pluginCss, `.duty-tl-mark i.t-${tone}`),
      /color:/,
      `десктоп: у палки группы нет своего тона ${tone}`,
    )
  }
})

/**
 * Каждый тег, который разметка создаёт через `h('<tag>')`, обязан быть
 * классифицирован: либо он в `SVG_TAGS` (тогда namespace правильный), либо в
 * HTML-списке ниже. Новый элемент (`polygon`, `text`, `tspan`) без классификации
 * снова даст «класс есть, правило есть, элемента не видно» — этот тест требует
 * принять решение при добавлении, а не после жалобы.
 */
const HTML_TAGS = [
  'a', 'article', 'b', 'br', 'button', 'code', 'div', 'em', 'h1', 'h2', 'h3', 'h4', 'hr', 'i',
  'input', 'label', 'li', 'ol', 'option', 'p', 'pre', 'section', 'select', 'small',
  'span', 'strong', 'table', 'tbody', 'td', 'textarea', 'th', 'thead', 'tr', 'ul', 'col', 'colgroup',
]

test('веб: каждый тег разметки классифицирован (svg-namespace либо HTML)', () => {
  const app = read('../dashboard/web/app.js')
  const svgTags = new Set((/SVG_TAGS\s*=\s*new Set\(\[([^\]]*)\]\)/.exec(app)?.[1] || '')
    .split(',')
    .map((part) => part.trim().replace(/^['"]|['"]$/g, ''))
    .filter(Boolean))
  assert.ok(svgTags.size > 0, 'SVG_TAGS не разобран — проверка потеряла предмет')

  const used = new Set([...app.matchAll(/(?<![\w.$])h\(\s*'([a-z][a-z0-9]*)'/g)].map((hit) => hit[1]))
  assert.ok(used.size > 5, 'в разметке мало тегов — регулярка не совпала с кодом')

  const unknown = [...used].filter((tag) => !svgTags.has(tag) && !HTML_TAGS.includes(tag))
  assert.deepEqual(
    unknown,
    [],
    `не классифицированы: ${unknown.join(', ')} — добавить в SVG_TAGS (если это svg-элемент) или в HTML_TAGS теста`,
  )
})
