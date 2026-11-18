/* Hermes Duty — веб-версия.
 *
 * Три экрана поверх четырёх API-ответов плагина:
 *   #/now             «Дела»              — GET /now + GET /deals (подвал, свёрнут)
 *   #/why             «Почему стоит»      — GET /now (группировка + evidence)
 *   #/health          «Здоровье»          — GET /stats
 *   #/deal/<deal_id>  «Дело»              — GET /deal/{deal_id} + /duty/{deal_id}/attempts
 *
 * С 09.11 «Сейчас», «Завершённые» и «Дело» — одна страница: сверху мини-таймлайн
 * активных дел, ниже строки по репозиториям, в подвале свёрнутый блок завершённых
 * с курсорной пагинацией. Вкладок «Завершённые» и «Дело» больше нет: первая
 * дублировала список, вторая открывала пустой выбор дела. `#/deals` остался
 * старым адресом и открывает ту же страницу с раскрытым подвалом.
 *
 * Дело адресуется ТОЛЬКО своим id `d_<hex>` (0.0.6: доски нет). Прежний адрес
 * `#/deal/<board>/<id>` — отказ: страница называет, что доски больше нет, вместо
 * того чтобы открыть полусломанную карточку. Список дел группируется по `repo`,
 * дела без репозитория — отдельной группой и последней; в списках репозиторий
 * подписан хвостом пути, полный путь — на странице дела.
 *
 * Шейпер ответов не дублируется: он импортируется из `./model.js`, а бэкенд
 * отдаёт тот же файл, что вшит в десктопный плагин. Таксономия состояний и
 * формулировки доказательств — единственный источник бэкенда (`waiting.state`,
 * `waiting.evidence`), страница их только рисует.
 *
 * Токен берём из `window.__HERMES_SESSION_TOKEN__` (вшит бэкендом в HTML) и шлём
 * заголовком `X-Hermes-Session-Token` ко всем `/api/plugins/hermes-duty/*`.
 */
import {
  ALL_REPOS,
  WITHOUT_REPO_LABEL,
  attemptView,
  KIND_ICON,
  laneView,
  filterNow,
  dealTimeline,
  fmtAge,
  fmtClock,
  groupStatesText,
  humanLine,
  noticeLines,
  plural,
  repoLabel,
  repoShort,
  fileLabel,
  docBlocks,
  finishedStatusMeta,
  shapeDeal,
  shapeDecisions,
  decisionsCount,
  decisionGroups,
  shapeDealTime,
  shapeFinished,
  shapeFinishedPage,
  shapeNow,
  shapeStages,
  stageColumnBars,
  compactStageView,
  compactAttemptView,
  stateLabel,
  stateMeta,
  TOOL_LEGEND,
  REFRESH_FAIL_SILENT,
  refreshPlan,
  freshKeys,
  TL_WINDOWS,
  TL_WINDOW_KEY,
  TL_WINDOW_SEC,
  parseWindow,
  windowLabel,
  DEAL_ICON,
} from './model.js'

const TOKEN = window.__HERMES_SESSION_TOKEN__ || ''
const BASE = '/api/plugins/hermes-duty'
const HDR = TOKEN ? { 'X-Hermes-Session-Token': TOKEN } : {}
const REFRESH_MS = 10000
const HEALTH_REFRESH_MS = 30000
const TIMEOUT_MS = 15000
const SEARCH_DEBOUNCE_MS = 240
// Сколько живёт подсветка изменившегося значения (только цвет, без движения).
// Плавность дела: вход/уход строк и перестановка дорожек. Одно значение на все
// три движения, чтобы страница не дышала вразнобой.
const FLIP_MS = 320
const EXIT_MS = 280
const FRESH_HIGHLIGHT_MS = 600

/* ---------------------------------------------------------------- утилиты */

const app = document.getElementById('app')
const stampEl = document.getElementById('stamp')
const verEl = document.getElementById('ver')
const humanEl = document.getElementById('human')

// Элементы с этой осью — не HTML: `createElement('svg')` создаёт элемент в
// HTML-namespace, и браузер его не рисует вовсе. Ошибка не видна в разметке, и
// именно поэтому список назван явно, а не выводится из имени тега.
const SVG_NS = 'http://www.w3.org/2000/svg'
const SVG_TAGS = new Set(['svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'g', 'use', 'text', 'title'])

// Значок плагина в шапке: та же геометрия, что у значка дела (`DEAL_ICON` из
// шейпера) и у favicon вкладки — один рисунок на все места плагина. Блок стоит
// после `SVG_TAGS`: `h()` читает список тегов, и вызов до инициализации падает
// по TDZ, роняя весь модуль (страница остаётся на «загрузка…»).
const brandIcoEl = document.getElementById('brand-ico')
if (brandIcoEl) {
  brandIcoEl.append(
    h('svg', { class: 'brand-ico-svg', viewBox: '0 0 16 16', 'aria-hidden': 'true' }, h('path', { d: DEAL_ICON })),
  )
}

function h(tag, props, ...children) {
  const el = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag)
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue
    // `className` у SVGElement только для чтения: присваивание молча падает
    // исключением и роняет всю отрисовку, поэтому класс ставится атрибутом.
    if (key === 'class') el.setAttribute('class', value)
    else if (key === 'text') el.textContent = value
    else if (key === 'html') el.innerHTML = value
    else if (key.startsWith('on')) el.addEventListener(key.slice(2).toLowerCase(), value)
    else if (key === 'dataset') Object.assign(el.dataset, value)
    else el.setAttribute(key, value === true ? '' : value)
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue
    el.append(child instanceof Node ? child : document.createTextNode(String(child)))
  }
  return el
}

/* Markdown для тела дела. Библиотека вендорена (`vendor/marked.min.js`): страница
 * живёт в прослойке без сети, поэтому брать её с CDN нельзя. Рендер идёт через
 * `template`, из которого вычищается всё исполняемое (`script`/`style`/`iframe`,
 * атрибуты `on*`, `javascript:`-ссылки): тело дела пишет человек или воркер, но
 * страница не должна выполнять чужой код.
 */
function mdNode(text, fallbackClass) {
  const marked = window.marked
  if (!marked || typeof marked.parse !== 'function' || !text) {
    return h('pre', { class: fallbackClass || 'body' }, text || '')
  }
  const tpl = document.createElement('template')
  tpl.innerHTML = marked.parse(text, { gfm: true, breaks: false })
  for (const bad of tpl.content.querySelectorAll('script, style, iframe, object, embed, link, meta')) bad.remove()
  for (const el of tpl.content.querySelectorAll('*')) {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase()
      const value = String(attr.value || '').toLowerCase()
      if (name.startsWith('on') || (name === 'href' && value.startsWith('javascript:'))) el.removeAttribute(attr.name)
    }
  }
  const box = h('div', { class: 'md' })
  box.append(tpl.content)
  return box
}

const clear = (node) => {
  while (node.firstChild) node.removeChild(node.firstChild)
  return node
}

const pct = (share) => (share === null || share === undefined ? '—' : `${Math.round(share * 100)}%`)

/* --------------------------------------------- патч вместо перерисовки (0.0.8)
 *
 * Дело меняется минутами, а страница — снимок. Обновлять её заменой `innerHTML`
 * нельзя: заменённый узел теряет раскрытие, фокус и прокрутку, и человек видит
 * мигание там, где поменялась одна цифра. Поэтому тик сверяет новое дерево с уже
 * нарисованным и правит узлы на месте: то, что не изменилось, остаётся тем же
 * элементом (его и прокрутка, и обработчики), а вставляется или удаляется только
 * то, что появилось или пропало.
 *
 * Ключ узла — `data-key` (id этапа, номер попытки, ключ маркера), а без ключа
 * узлы сверяются по месту в списке. Ключами служат значения, которые уже есть в
 * виде: `stage.id`, `attempt.number`, `marker.key`, `gap.from`.
 */

/** Ключ узла дерева; пусто — сверка пойдёт по месту в списке. */
const treeKey = (node) => (node && node.nodeType === 1 ? node.getAttribute('data-key') || '' : '')

/** Свести новые атрибуты со старым узлом: лишние снять, недостающие поставить. */
function syncAttrs(oldEl, newEl) {
  for (const attr of [...newEl.attributes]) {
    if (oldEl.getAttribute(attr.name) !== attr.value) oldEl.setAttribute(attr.name, attr.value)
  }
  for (const attr of [...oldEl.attributes]) {
    if (!newEl.hasAttribute(attr.name)) oldEl.removeAttribute(attr.name)
  }
}

/** Слить детей: вернуть узлы, которые остались в родителе, в новом порядке. */
function morphChildren(oldParent, newParent) {
  const olds = [...oldParent.childNodes]
  const byKey = new Map()
  for (const node of olds) {
    const key = treeKey(node)
    if (key) byKey.set(key, node)
  }
  const wanted = [...newParent.childNodes]
  const used = new Set()
  const placed = wanted.map(() => null)
  // Первый проход — по ключам: узел с тем же ключом правится там, где он стоял,
  // даже если соседи поменялись местами или появились новые.
  wanted.forEach((next, index) => {
    const key = treeKey(next)
    if (!key) return
    const found = byKey.get(key)
    if (!found || used.has(found)) return
    used.add(found)
    placed[index] = morph(found, next)
  })
  const free = olds.filter((node) => !used.has(node))
  // Второй проход — по месту: узел без ключа берёт первый свободный старый узел.
  // Узел с ключом позиционного «двойника» не получает: его либо нашли по ключу,
  // либо это новый факт, который надо вставить.
  wanted.forEach((next, index) => {
    if (placed[index]) return
    if (treeKey(next) || !free.length) {
      placed[index] = next
      return
    }
    placed[index] = morph(free.shift(), next)
  })
  placed.forEach((node, index) => {
    if (oldParent.childNodes[index] !== node) oldParent.insertBefore(node, oldParent.childNodes[index] || null)
  })
  // Появившееся — свежие узлы, для которых не нашлось старого: помечаем, CSS
  // проиграет вход. Класс снимается по окончании анимации, а при следующем
  // патче его и так снёс бы syncAttrs.
  wanted.forEach((next, index) => {
    if (placed[index] === next && next.nodeType === 1) tagEntering(next)
  })
  // Всё, что осталось за пределами нового списка, — пропало из ответа. Так
  // удаляется одна закрытая попытка или целый этап, а не вся панель. Уход
  // анимированный: узел остаётся доигрывать схлопывание и убирается по таймеру.
  for (const gone of [...oldParent.childNodes].slice(placed.length)) {
    if (!animateExit(gone)) oldParent.removeChild(gone)
  }
}

/** Семейства узлов, чьё появление и уход стоят анимации: этапы и попытки. */
const ENTERING_RE = /^(lane:|attempt:|track:|stage:|att:|block:)/

function tagEntering(el) {
  const key = el.getAttribute('data-key') || ''
  if (!ENTERING_RE.test(key) || prefersReducedMotion() || el.classList.contains('is-new')) return
  el.classList.add('is-new')
  el.addEventListener('animationend', () => el.classList.remove('is-new'), { once: true })
}

/** Оставить узел доигрывать уход (схлопывание высоты); false — убрать сразу. */
function animateExit(node) {
  if (!node || node.nodeType !== 1 || prefersReducedMotion()) return false
  const key = node.getAttribute('data-key') || ''
  if (!ENTERING_RE.test(key)) return false
  // Замораживаем нынешнюю высоту: схлопывание идёт из неё, а не рывком.
  node.style.height = `${node.offsetHeight}px`
  node.style.overflow = 'hidden'
  void node.offsetHeight // reflow: иначе переход не начнётся
  node.classList.add('is-gone')
  setTimeout(() => {
    if (node.parentNode) node.parentNode.removeChild(node)
  }, EXIT_MS + 60)
  return true
}

/** Полный путь ключей узла: `lane:x/attempt:y` — уникален даже при `band`. */
function nodePathKey(el) {
  const parts = []
  for (let cur = el; cur && cur.nodeType === 1; cur = cur.parentElement) {
    const key = cur.getAttribute('data-key')
    if (key) parts.unshift(key)
  }
  return parts.join('/')
}

/** Снять координаты ключевых узлов до патча — исход для FLIP-плавности. */
function flipBefore(host) {
  if (prefersReducedMotion()) return null
  const map = new Map()
  for (const el of host.querySelectorAll('[data-key]')) {
    const key = el.getAttribute('data-key')
    if (!ENTERING_RE.test(key)) continue
    map.set(el.getAttribute('data-key'), el.getBoundingClientRect())
  }
  return map.size ? map : null
}

/** Доиграть перестановку: узлы, съехавшие по вертикали, доезжают плавно. */
function flipPlay(root, before) {
  if (!before) return
  for (const el of root.querySelectorAll('[data-key]')) {
    const key = el.getAttribute('data-key')
    if (!ENTERING_RE.test(key) || !before.has(key)) continue
    if (el.classList.contains('is-new') || el.classList.contains('is-gone')) continue
    const prev = before.get(key)
    const now = el.getBoundingClientRect()
    const dy = prev.top - now.top
    if (Math.abs(dy) < 1) continue
    el.style.transition = 'none'
    el.style.transform = `translateY(${dy}px)`
    requestAnimationFrame(() => {
      el.style.transition = `transform ${FLIP_MS}ms ease`
      el.style.transform = ''
      const done = () => {
        el.style.transition = ''
        el.style.transform = ''
      }
      el.addEventListener('transitionend', done, { once: true })
      setTimeout(done, FLIP_MS + 80)
    })
  }
}

/** Свести новое поддерево со старым; вернуть узел, который остался в DOM. */
function morph(oldNode, newNode) {
  if (!oldNode || oldNode.nodeType !== newNode.nodeType || oldNode.nodeName !== newNode.nodeName) {
    if (oldNode && oldNode.parentNode) {
      oldNode.parentNode.replaceChild(newNode, oldNode)
      if (newNode.nodeType === 1) tagEntering(newNode)
    }
    return newNode
  }
  if (oldNode.nodeType !== 1) {
    if (oldNode.nodeValue !== newNode.nodeValue) oldNode.nodeValue = newNode.nodeValue
    return oldNode
  }
  syncAttrs(oldNode, newNode)
  morphChildren(oldNode, newNode)
  return oldNode
}

async function api(path) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS)
  let res
  try {
    res = await fetch(BASE + path, { signal: ctrl.signal, headers: HDR, cache: 'no-store' })
  } catch (err) {
    throw new Error(err.name === 'AbortError' ? `таймаут: ${TIMEOUT_MS / 1000} с` : err.message || 'сеть')
  } finally {
    clearTimeout(timer)
  }
  let data = null
  try {
    data = await res.json()
  } catch {
    /* не JSON — разберём по коду */
  }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`)
  return data || {}
}

/* ------------------------------------------------------------------ бейдж */

function Badge(state, humanKind, label) {
  const meta = stateMeta(state, humanKind)
  return h('span', { class: `badge t-${meta.tone}`, title: state || '' }, h('i'), label || meta.label)
}

/* ------------------------------------------------------------- состояние */

let route = { name: 'now' }
let timer = null
let stampAt = null
let ticker = null

const snap = { data: null, error: null, loading: false }
const filter = { repo: ALL_REPOS, states: new Set(), query: '', effQuery: '' }
const health = { data: null, error: null, loading: false }
/**
 * Границы этапов, пришедшие из журнала: `deal_id -> [{stage,start,end,outcome}]`.
 *
 * Живёт вне блока завершённых: это факт о деле, а не о списке, и переживает и
 * переключение фильтра, и повторный запрос.
 */
const timelineSpans = {}

// Окно таймлайна — выбор человека и он переживает перезагрузку: разбирать свежее
// дело хочется в часовом окне, а «когда всё встало» — в недельном.
const tlWindow = {
  sec: Number(localStorage.getItem(TL_WINDOW_KEY)) || TL_WINDOW_SEC,
  set(sec) {
    const next = Number(sec)
    if (!Number.isFinite(next) || next <= 0) return
    this.sec = next
    try { localStorage.setItem(TL_WINDOW_KEY, String(next)) } catch { /* приватный режим */ }
    render()
  },
}

/**
 * Снимок страницы дела: своё состояние у каждого адреса, потому что вкладка
 * «Дело» живёт дольше одного запроса (поллинг, раскрытые попытки).
 */
const dealSnap = { key: '', data: null, attempts: null, error: null, loading: false, refreshedAt: 0, refreshError: '', spots: [] }

/**
 * Состояние блока «завершённые»: группы листаются сами.
 *
 * `perRepo` — сколько строк показывать на проект. Курсор живёт в группе, поэтому
 * «+5» подтягивает следующую пятёрку каждого проекта, не сдвигая соседей и не
 * повторяя строк (курсор — ключ `finished_at:id`, а не смещение).
 */
const FINISHED_PER_REPO = 5
// Сколько скелетоны держатся как минимум: локальный ответ приходит за десятки
// миллисекунд, и без этого «разворачивание» не увидеть — строки просто прыгали
// (09.11, Макс: «ожидал, что будут разворачиваться скелетоны»).
const SKELETON_MIN_MS = 260
const finished = {
  open: false,
  perRepo: FINISHED_PER_REPO,
  index: null, // ответ /deals: total, repos и ошибки источников
  groups: new Map(), // repo -> {label, total, rows, cursor, done, loading, error}
  error: null,
  loading: false,
  pages: 0, // сколько раз нажимали «загрузить ещё» — для счётчика «показано N»
  // Первый кадр с данными отыгран: входные анимации групп больше не повторяются,
  // иначе каждое нажатие «ещё» моргало всем блоком (09.11, Макс).
  painted: false,
  // Сворачивания нет: по умолчанию в списках по пятёрке на проект, и блок
  // занимает мало места (09.11, Макс: «будем сразу развернуто отображать»).
}

/** Группа по репозиторию: строка состояния заводится один раз. */
function finishedGroup(repo) {
  const key = String(repo || '')
  if (!finished.groups.has(key)) {
    finished.groups.set(key, {
      repo: key,
      rows: [],
      total: 0,
      cursor: null,
      done: false,
      loading: false,
      error: null,
    })
  }
  return finished.groups.get(key)
}

function setStamp(at) {
  stampAt = at
}

function tickStamp() {
  if (!stampAt) {
    stampEl.textContent = ''
    return
  }
  const ago = Math.max(0, Math.floor((Date.now() - stampAt) / 1000))
  stampEl.textContent = ago < 2 ? 'обновлено только что' : `обновлено ${ago} с назад`
}

/* ------------------------------------------------------------ экран «сейчас» */

async function loadNow() {
  snap.loading = true
  try {
    snap.data = await api('/now')
    snap.error = null
    setStamp(Date.now())
    // Границы этапов спрашиваются для того, что нарисовано: активные дела плюс
    // первая пятёрка каждого проекта. Уже известные не перезапрашиваются.
    const painted = shapeNow(snap.data).deals.map((deal) => deal.id)
    for (const group of finished.groups.values()) {
      for (const row of group.rows.slice(0, FINISHED_PER_REPO)) painted.push(row.id)
    }
    void loadTimelineSpans(painted)
  } catch (err) {
    snap.error = err.message
  } finally {
    snap.loading = false
    render()
  }
}

function noticesBlock(lines) {
  if (!lines.length) return null
  return h('div', { class: 'note' }, ...lines.map((line) => h('div', null, line)))
}

function errorBanner(text, retry) {
  return h('div', { class: 'banner' }, h('span', null, text), h('button', { class: 'icon-btn', onclick: retry, title: 'Повторить' }, '⟳'))
}

function dealRow(deal) {
  const attemptTitle =
    deal.attemptAge === null
      ? `попытки нет (${deal.attemptNote})`
      : `попытка ${deal.attemptAgeText} назад, ${deal.attemptNote}`
  const leaseTitle =
    deal.leaseExpiresIn === null
      ? `лизинг: ${deal.leaseState}`
      : `лизинг: ${deal.leaseState}, ${deal.leaseExpiresIn > 0 ? 'ещё' : 'истёк'} ${fmtAge(Math.abs(deal.leaseExpiresIn))}`
  // Адрес дела — только его id (0.0.6): доски в маршруте больше нет.
  const open = () => {
    location.hash = `#/deal/${encodeURIComponent(deal.id)}`
  }
  return h(
    'tr',
    {
      class: 'row',
      tabindex: '0',
      title: [deal.repoLabel, deal.evidence].filter(Boolean).join(' · '),
      onclick: open,
      onkeydown: (e) => (e.key === 'Enter' ? open() : null),
    },
    h('td', { class: 'mono' }, deal.id),
    h('td', { class: 'ttl', title: deal.titleFull }, deal.title),
    h('td', { title: deal.assigneeNote || 'исполнитель' }, deal.assignee),
    // Этап и номер попытки из журнала (`/now.stage`): «что сейчас» видно в
    // строке, без открытия дела. Прочерк — у дела ещё не было попытки. Идущий
    // этап подсвечен акцентом: это ответ на «где оно стоит», и он не должен
    // тонуть среди прочих ячеек (09.11).
    h(
      'td',
      { class: `mono muted${deal.stageLive ? ' stage-now' : ''}`, title: deal.stageHint || 'этап не начат' },
      deal.stageText || '—',
    ),
    h('td', { class: 'n', title: attemptTitle }, deal.attemptAgeText),
    h('td', { title: leaseTitle }, deal.leaseText),
    h('td', { class: 'n', title: `${deal.lastActionKind}: ${deal.lastActionSummary || '—'}` }, deal.lastActionAgeText),
    // Состояние — единственная ячейка, которую бэкенд формулирует словами, поэтому
    // лид дописывается сюда же строкой, как в `duty now` (`dashboard/cli.py`,
    // `_now_row`): своего столбца у него нет, а «кто ведёт дело» — часть состояния.
    h(
      'td',
      null,
      Badge(deal.state, deal.humanKind),
      deal.lead && deal.lead.known
        ? h(
            'span',
            { class: `lead-note t-${deal.lead.tone}`, title: deal.lead.evidence || 'состояние лида по журналу' },
            deal.lead.short,
          )
        : null,
    ),
  )
}

/**
 * Ширины колонок списков — доли, общие для всех групп и обеих поверхностей.
 *
 * Группа дел — отдельная таблица на репозиторий: без явных ширин браузер считает
 * колонки по содержимому, и заголовок одной группы сдвигает колонки относительно
 * соседней (09.11, Макс). Доли, а не пиксели: на узком окне колонки сжимаются
 * вместе, а не съедают «заголовок» до нуля.
 */
const NOW_COLS = ['9%', '28%', '12%', '10%', '7%', '6%', '8%', '12%']
const FINISHED_COLS = ['8%', '9%', '23%', '8%', '6%', '6%', '8%', '13%', '10%', '9%']

/** `<colgroup>` по списку ширин: одна геометрия у всех групп списка. */
function colGroup(widths) {
  return h(
    'colgroup',
    null,
    ...widths.map((width, index) => h('col', { key: index, style: width ? `width:${width}` : null })),
  )
}

/**
 * Группа списка — репозиторий (0.0.6). Заголовок называет репозиторий и число
 * дел в нём; состояния группы подписаны одной строкой, чтобы «ждёт ответа» не
 * терялось при смене группировки. Столбца «репозиторий» в строках нет — он был бы
 * повтором заголовка группы.
 */
function groupBlock(group) {
  return h(
    'section',
    { class: 'grp' },
    h(
      'div',
      { class: 'grp-h' },
      h('span', { class: group.repo ? 'grp-k mono' : 'grp-k mono muted', title: group.repo || 'репозиторий не назван' }, group.label),
      h('span', { class: 'n' }, `${group.count}`),
      h('span', { class: 'grp-s muted' }, groupStatesText(group)),
    ),
    h(
      'table',
      { class: 'grp-table' },
      colGroup(NOW_COLS),
      h(
        'thead',
        null,
        h(
          'tr',
          null,
          h('th', null, 'дело'),
          h('th', null, 'заголовок'),
          h('th', null, 'исполнитель'),
          h('th', null, 'этап'),
          h('th', null, 'попытка'),
          h('th', null, 'лизинг'),
          h('th', null, 'действие'),
          h('th', null, 'состояние'),
        ),
      ),
      h('tbody', null, ...group.deals.map(dealRow)),
    ),
  )
}

/**
 * Фильтр по репозиторию вместо доски: `ALL_REPOS` — все дела, пустое значение —
 * ровно дела без репозитория (так же их отличает роут: `?repo=`). Список
 * репозиториев — состав store из `/now.repos`, а не только видимый срез: иначе
 * фильтр по репозиторию с одними закрытыми делами было бы не выбрать.
 */
function filtersBlock(model) {
  const toggle = (state) => {
    if (filter.states.has(state)) filter.states.delete(state)
    else filter.states.add(state)
    render()
  }
  const options = [h('option', { value: ALL_REPOS, selected: filter.repo === ALL_REPOS }, 'все репозитории')]
  if (model.withoutRepo > 0) {
    options.push(
      h('option', { value: '', selected: filter.repo === '' }, `${WITHOUT_REPO_LABEL} (${model.withoutRepo})`),
    )
  }
  for (const item of model.repos.list) {
    options.push(
      h('option', { value: item.repo, selected: filter.repo === item.repo }, `${item.repo} (${item.deals})`),
    )
  }
  return h(
    'div',
    { class: 'filters' },
    h(
      'select',
      {
        title: 'Фильтр по репозиторию',
        onchange: (e) => {
          filter.repo = e.target.value
          render()
        },
      },
      ...options,
    ),
    h(
      'div',
      { class: 'chips' },
      h(
        'button',
        {
          class: filter.states.size ? 'chip' : 'chip on',
          onclick: () => {
            filter.states.clear()
            render()
          },
        },
        'все',
        h('span', { class: 'n' }, `${model.count}`),
      ),
      ...model.stateOrder.map((state) => {
        const meta = stateMeta(state)
        // Подпись чипа — та же, что у бейджа: «ждёт ответа» стоит только там,
        // где в этом состоянии действительно есть вопрос (видимые дела).
        return h(
          'button',
          {
            class: `chip t-${meta.tone}${filter.states.has(state) ? ' on' : ''}`,
            title: state,
            onclick: () => toggle(state),
          },
          stateLabel(state, model.deals),
          h('span', { class: 'n' }, `${model.byState[state] || 0}`),
        )
      }),
    ),
    h('input', {
      type: 'search',
      placeholder: 'поиск: id, заголовок, исполнитель',
      value: filter.query,
      oninput: (e) => {
        filter.query = e.target.value
        clearTimeout(filter.debounce)
        filter.debounce = setTimeout(() => {
          filter.effQuery = filter.query
          render()
        }, SEARCH_DEBOUNCE_MS)
      },
    }),
  )
}

/**
 * Панель поиска: единственный фильтр на странице.
 *
 * Чипы состояний и селект репозитория убраны (09.11, Макс: «я ими не пользуюсь»):
 * состояние и репозиторий видны в строках и группировке, а частый вопрос —
 * «где это дело» — закрывает поиск по id, заголовку, исполнителю и этапу.
 */
function searchBlock() {
  return h(
    'div',
    { class: 'filters' },
    h('input', {
      type: 'search',
      placeholder: 'поиск: id, заголовок, исполнитель, этап',
      value: filter.query,
      oninput: (e) => {
        filter.query = e.target.value
        clearTimeout(filter.debounce)
        filter.debounce = setTimeout(() => {
          filter.effQuery = filter.query
          render()
        }, SEARCH_DEBOUNCE_MS)
      },
    }),
  )
}

/**
 * Выбор окна таймлайна: пресеты плюс ввод с клавиатуры (`45m`, `12h`, `2d`).
 *
 * Кнопки отвечают на частый вопрос в один клик, поле — на точный: «полчаса назад
 * всё сломалось» не выразить пятёркой пресетов. Непонятный ввод не применяется:
 * поле краснеет, окно остаётся прежним — молча подставлять своё значение нельзя.
 */
function tlWindowControl() {
  const apply = (input) => {
    const sec = parseWindow(input.value)
    if (sec === null) {
      input.classList.add('is-bad')
      setTimeout(() => input.classList.remove('is-bad'), 900)
      return false
    }
    input.classList.remove('is-bad')
    input.value = ''
    tlWindow.set(sec)
    return true
  }
  return h(
    'span',
    { class: 'tl-window-pick', title: `окно ${windowLabel(tlWindow.sec)}` },
    ...TL_WINDOWS.map((item) =>
      h(
        'button',
        {
          type: 'button',
          class: `tl-window-btn${tlWindow.sec === item.sec ? ' is-on' : ''}`,
          title: `окно ${item.label}`,
          onclick: () => tlWindow.set(item.sec),
        },
        item.key.replace('h', 'ч').replace('d', 'д'),
      ),
    ),
    h('input', {
      class: 'tl-window-input',
      type: 'text',
      inputmode: 'numeric',
      placeholder: 'своё',
      title: 'окно: 45m · 12h · 2d · число = часы',
      onkeydown: (e) => {
        if (e.key === 'Enter') {
          if (apply(e.target)) e.target.blur()
        } else if (e.key === 'Escape') {
          e.target.value = ''
          e.target.blur()
        }
      },
      onblur: (e) => {
        if (e.target.value.trim()) apply(e.target)
      },
    }),
  )
}

/**
 * Мини-таймлайн дел: дорожка на дело, полосы этапов внутри.
 *
 * Сверху — шкала с делениями, под ней дорожки одним блоком: сетка и линия «сейчас»
 * принадлежат полотну, а не каждой строке, иначе картинка рассыпается на набор
 * серых полосок. Цвет полосы — этап (легенда под таймлайном), пустое место —
 * пауза между попытками.
 */
function timelineBlock(model) {
  const done = finished.index
    ? [...finished.groups.values()].flatMap((group) => group.rows)
    : []
  const spans = timelineSpans
  const view = dealTimeline(model.deals, done, model.now, spans, tlWindow.sec)
  if (view.empty) {
    return h(
      'div',
      { class: 'panel tl-mini' },
      h('div', { class: 'panel-h' }, 'таймлайн дел', h('span', { class: 'tl-spacer' }), tlWindowControl()),
      // Окно называем словами: «за сутки» при выбранном часе было бы неправдой.
      h('div', { class: 'panel-b muted' }, `за ${windowLabel(tlWindow.sec)} дел не было`),
    )
  }
  const ticks = view.ticks || []
  const gridStyle = `--grid-step:${view.grid.stepPct}%;--grid-first:${view.grid.firstPct}%`
  const open = (id) => () => {
    location.hash = `#/deal/${encodeURIComponent(id)}`
  }
  return h(
    'div',
    { class: 'panel tl-mini' },
    h(
      'div',
      { class: 'panel-h' },
      'таймлайн дел',
      view.windowText ? h('span', { class: 'muted tl-window' }, view.windowText) : null,
      view.omitted ? h('span', { class: 'muted tl-omitted' }, `ещё ${view.omitted} не показано`) : null,
      h('span', { class: 'tl-spacer' }),
      tlWindowControl(),
    ),
    h(
      'div',
      { class: 'tl-body' },
      h(
        'div',
        { class: 'tl-rows', style: gridStyle },
        // Сетка — один слой под всеми делами: вертикали общие, строки своих
        // клеток не рисуют. Сосиски лежат на этой решётке, а не в рамках.
        h(
          'div',
          { class: 'tl-grid', 'aria-hidden': 'true' },
          h('span', { class: 'tl-grid-gap' }),
          h('span', { class: 'tl-grid-track' }),
        ),
        ...view.rows.map((row) =>
          h(
            'div',
            { class: `tl-mini-row${row.open ? '' : ' is-done'}`, 'data-key': `tl:${row.key}` },
            h(
              'span',
              { class: 'tl-mini-lbl' },
              h('span', { class: 'tl-repo' }, row.repo),
              // Кликабельны дело и его название, а не строка: длительность и
              // состояние — показания, а не ссылки (09.11, Макс).
              h(
                'a',
                {
                  class: 'tl-title',
                  href: `#/deal/${encodeURIComponent(row.id)}`,
                  title: row.hint,
                  'data-key': `tl-title:${row.key}`,
                },
                row.title || row.id,
              ),
            ),
            h(
              'span',
              { class: 'tl-mini-track' },
              // Сосиска — одно тело дела: этапы стыкуются без скруглений, форму
              // задаёт она сама. Начало раньше окна срезано её левым краем.
              h(
                'span',
                {
                  class: `tl-sausage${row.open ? ' is-live' : ''}${row.exact ? '' : ' inexact'}${row.clipped ? ' is-clipped' : ''}`,
                  style: `left:${row.bar.leftPct}%;width:${row.bar.widthPct}%`,
                  role: 'link',
                  tabindex: '0',
                  title: row.hint,
                  'aria-label': `открыть дело ${row.id}`,
                  'data-key': `tl-bar:${row.key}`,
                  onclick: open(row.id),
                  onkeydown: (e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      open(row.id)()
                    }
                  },
                },
                row.segments.length
                  ? row.segments.map((segment) =>
                      h('i', {
                        class: `tl-seg r-${segment.role} shade-${segment.shade} t-${segment.tone} is-${segment.state}${segment.clipped ? ' clipped' : ''}`,
                        style: `left:${segment.leftPct}%;width:${segment.widthPct}%`,
                        title: segment.title,
                      }),
                    )
                  : null,
              ),
            ),
            h('span', { class: 'tl-mini-age mono' }, row.durationText),
            h('span', { class: 'tl-mini-state' }, row.stateLabel),
          ),
        ),
      ),
      // Шкала — в тех же колонках, что дорожки: подпись стоит под своим делением.
      h(
        'div',
        { class: 'tl-axis' },
        h('span', { class: 'tl-axis-gap' }),
        h(
          'span',
          { class: 'tl-axis-track' },
          ...ticks.map((tick) => h('span', { class: 'tl-tick', style: `left:${tick.leftPct}%` }, tick.label)),
        ),
      ),
    ),
    view.legend.length
      ? h(
          'div',
          { class: 'tl-legend' },
          ...view.legend.map((item) =>
            h(
              'span',
              { class: `tl-legend-item${item.state === 'running' ? ' is-running' : ''}` },
              h('i', { class: `tl-legend-dot t-${item.tone}` }),
              item.label,
              h('span', { class: 'muted' }, item.durationText),
            ),
          ),
        )
      : null,
  )
}

/**
 * Границы этапов для дорожек: запросы пачками, по потолку бэкенда.
 *
 * `SPAN_MAX_DEALS` (40) — потолок ручки `/spans`, а рисуется за раз больше:
 * активные дела плюс пятёрка на проект переваливают за сорок. Один общий запрос
 * получал 400, ошибка глушилась («украшение»), и дорожки оставались без этапов —
 * на каждом обновлении заново (Макс, 16.11). Поэтому — пачками, и по ответу
 * пачки досчитываем дела без журнальных событий: пустой список — тоже ответ,
 * спрашивать их снова каждый тик незачем.
 *
 * Спрашивается то, что нарисовано (активные и первая пятёрка каждого проекта), а
 * не весь store: подсказка этапов у строки, которую не видно, никому не нужна.
 * Ошибка не глушит таймлайн — дорожки рисуются одним тоном состояния.
 */
const SPAN_BATCH = 40

async function loadTimelineSpans(ids) {
  const wanted = [...new Set(ids.filter(Boolean))]
  const known = new Set(Object.keys(timelineSpans))
  const pending = wanted.filter((id) => !known.has(id))
  if (!pending.length) return
  let painted = 0
  for (let from = 0; from < pending.length; from += SPAN_BATCH) {
    const batch = pending.slice(from, from + SPAN_BATCH)
    try {
      const payload = await api(`/spans?ids=${encodeURIComponent(batch.join(','))}`)
      const spans = payload && typeof payload.spans === 'object' && payload.spans ? payload.spans : {}
      for (const [id, list] of Object.entries(spans)) timelineSpans[id] = list
      for (const id of batch) if (!timelineSpans[id]) timelineSpans[id] = []
      painted += batch.length
    } catch (err) {
      // Молча: подсказка этапов — украшение, а не факт, на котором стоит страница.
    }
  }
  // Один рендер на все пачки: страница не мигает после каждой.
  if (painted) render()
}




function viewNow() {
  if (!snap.data && snap.error) return errorBanner(`ошибка запроса /now: ${snap.error}`, loadNow)
  if (!snap.data) {
    return h(
      'div',
      { class: 'grp' },
      h(
        'div',
        { class: 'panel-b' },
        ...[0, 1, 2, 3, 4, 5].map((i) => h('div', { class: 'skel', style: `width:${95 - i * 9}%` })),
      ),
    )
  }
  const model = shapeNow(snap.data)
  // Индекс завершённых нужен и со свёрнутым блоком: таймлайн рисует и закрытое
  // (09.11). Грузится один раз, дальше группы живут своей жизнью.
  if (!finished.index && !finished.loading) void loadFinishedIndex()
  const view = filterNow(model, filter)
  const stale = Boolean(snap.error)
  const lines = noticeLines(model, stale)
  const parts = []

  if (verEl && !verEl.textContent && snap.data.by_state) verEl.textContent = ''

  // «N вопросов + M блокировок» по тому, что сейчас на экране (с поиском):
  // вопрос — на него можно ответить, блокировка — нужен человек без вопроса.
  if (humanEl) {
    humanEl.textContent = view.count ? humanLine(view.humanCounts) : ''
    humanEl.title = view.count
      ? `ждёт человека: ${humanLine(view.humanCounts)}`
      : 'никто не ждёт человека'
  }

  if (snap.error) {
    parts.push(
      errorBanner(`снимок от ${fmtClock(model.now)} — обновление не удалось: ${snap.error}`, loadNow),
    )
  }
  if (view.count > 0) {
    const note = noticesBlock(lines)
    if (note) parts.push(note)
  }
  parts.push(searchBlock())
  // Таймлайн — по тому, что видно (фильтр поиска), но по полному набору, когда
  // поиск пуст: верхушка страницы описывает срез, а не выборку под строкой.
  parts.push(timelineBlock(view))

  if (!view.count) {
    parts.push(
      h(
        'div',
        { class: 'empty' },
        h('b', null, model.count > 0 ? 'поиск ничего не нашёл' : 'нет активных дел'),
        h(
          'span',
          { class: 'muted' },
          `репозиториев в store: ${model.repoCount} · дел без репозитория: ${model.withoutRepo}`,
        ),
        lines.length ? h('ul', null, ...lines.map((line) => h('li', null, line))) : null,
      ),
    )
  } else {
    parts.push(...view.groups.map(groupBlock))
  }
  // Завершённые — на той же странице, свёрнутыми: отдельная вкладка для «что уже
  // сделано» заставляла переключаться ради справки (09.11).
  parts.push(finishedSection())
  return h('div', null, ...parts)
}

/* ----------------------------------------------------- экран «Завершённые» */

// Список закрытых дел — единственное место, где видно, что уже сделано: `/now`
// отвечает только про активные. Данные — `GET /deals` дословно: причина, этапы,
// попытки, вопросы, длительность и публикация приходят с бэкенда, страница их не
// пересчитывает (`shapeFinished`, `desktop/model.js`).

/**
 * Индекс групп: сколько закрытых дел у каждого проекта.
 *
 * Один запрос на всю картину (`/deals?limit=1` — строки не нужны, нужны числа),
 * дальше каждая группа тянет свою первую пятёрку. Показывать числа из уже
 * загруженной страницы нельзя: там ровно столько строк, сколько влезло, и «5 из
 * 5» на деле с двадцатью закрытиями было бы враньём.
 */
async function loadFinishedIndex() {
  finished.loading = true
  finished.error = null
  render()
  try {
    const page = shapeFinished(await api('/deals?limit=1'))
    // Store не прочитался — это не «завершённых дел нет»: оставляем блок пустым
    // по факту ошибки, а не по факту отсутствия дел.
    finished.index = page
    for (const item of page.repos) {
      const group = finishedGroup(item.repo)
      group.total = item.total
    }
  } catch (err) {
    finished.error = err.message
    finished.loading = false
    render()
    return
  }
  finished.loading = false
  await Promise.all((finished.index?.repos || []).map((item) => loadFinishedGroup(item.repo, 'reset')))
  render()
}

/**
 * Страница одной группы: `reset` — первая пятёрка, `more` — следующая по курсору.
 *
 * Ошибка остаётся в группе, а не на всей панели: один недоступный репозиторий не
 * повод гасить остальные.
 */
async function loadFinishedGroup(repo, dir = 'more') {
  const group = finishedGroup(repo)
  if (group.loading) return
  const cursor = dir === 'more' ? group.cursor : null
  if (dir === 'more' && group.done) return
  group.loading = true
  group.error = null
  render()
  const startedAt = Date.now()
  try {
    const query =
      `/deals?limit=${finished.perRepo}&repo=${encodeURIComponent(repo)}` +
      (cursor ? `&cursor=${encodeURIComponent(cursor)}` : '')
    const page = shapeFinishedPage(await api(query))
    group.rows = dir === 'more' ? group.rows.concat(page.rows) : page.rows
    group.cursor = page.next
    group.done = page.next === null
    group.total = page.total || group.rows.length
    // Пришедшие строки сразу просят свои границы этапов: дорожки таймлайна
    // рисуются цветом этапа, а не одной полосой.
    void loadTimelineSpans(group.rows.map((row) => row.id))
  } catch (err) {
    group.error = err.message
  } finally {
    // Скелетоны успевают показаться: ответ с локального бэкенда быстрее глаза.
    const wait = SKELETON_MIN_MS - (Date.now() - startedAt)
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait))
    group.loading = false
    render()
  }
}

/** «Загрузить ещё»: по следующей пятёрке в каждом проекте, у которого есть что. */
async function loadFinishedMore() {
  const pending = [...finished.groups.values()].filter((group) => !group.done)
  if (!pending.length) return
  finished.pages += 1
  await Promise.all(pending.map((group) => loadFinishedGroup(group.repo, 'more')))
}

/* Сворачивания нет: блок «завершённые» всегда раскрыт — по пятёрке на проект он
 * занимает пол-экрана и не мешает списку активных дел (09.11, Макс). */

/** Бейдж терминального статуса: у закрытого дела нет состояния ожидания. */
function finishedBadge(deal) {
  return h('span', { class: `badge t-${deal.tone}`, title: deal.status }, h('i'), deal.statusLabel)
}

function finishedRow(deal) {
  const open = () => {
    location.hash = `#/deal/${encodeURIComponent(deal.id)}`
  }
  const pubTitle = deal.published ? deal.published.mrUrl || deal.published.ref || 'факт журнала' : 'публикации нет'
  return h(
    'tr',
    {
      class: 'row',
      // Ключ — id дела: при подгрузке morph переиспользует строки, и входная
      // анимация играет только у новых (иначе блок моргал целиком).
      'data-key': `finished-row:${deal.id}`,
      tabindex: '0',
      title: `${deal.repoLabel} · ${deal.causeText}`,
      onclick: open,
      onkeydown: (e) => (e.key === 'Enter' ? open() : null),
    },
    h('td', { class: 'mono' }, deal.finishedText),
    h('td', { class: 'mono', title: deal.id }, deal.id),
    h('td', { class: 'ttl', title: deal.titleFull }, deal.title),
    h('td', null, finishedBadge(deal)),
    h('td', { class: 'n', title: 'попыток в журнале' }, `${deal.attempts}`),
    h('td', { class: 'n', title: 'вопросы человеку по журналу' }, deal.questions.text),
    h('td', { class: 'n', title: 'длительность дела: события журнала и попытки' }, deal.durationText),
    h('td', { title: deal.causeText }, deal.causeText),
    h('td', { class: 'muted', title: deal.stagesText }, deal.stagesText),
    h('td', { class: 'muted', title: pubTitle }, deal.published ? deal.published.text : '—'),
  )
}

/** Скелет группы: показываем форму строк, а не пустоту на месте чисел. */
function finishedSkeleton(count = 3) {
  return h(
    'div',
    { class: 'panel-b' },
    h('div', { class: 'skel skel-title' }),
    ...[0, 1, 2].slice(0, count).map((index) =>
      h('div', { class: 'skel skel-row', style: `animation-delay:${index * 90}ms` }),
    ),
  )
}

/**
 * Блок завершённых дел внутри страницы «Дела».
 *
 * Группы — по репозиторию, у каждой своя пятёрка и свой «+5»: список листается
 * проектами, потому что смотреть «что делали в этом репозитории» и есть вопрос.
 * Общая кнопка поднимает по пятёрке сразу во всех группах.
 */
function finishedSection() {
  const index = finished.index
  const groups = [...finished.groups.values()]
  const shown = groups.reduce((sum, group) => sum + group.rows.length, 0)
  const total = index ? index.total : 0
  // Шапка — заголовок панели, без переключателя: блок всегда раскрыт.
  const head = h(
    'div',
    { class: 'panel-h finished-head' },
    'завершённые',
    index ? h('span', { class: 'muted finished-count' }, `${shown} из ${total}`) : null,
    h('span', { class: 'muted finished-hint' }, `по ${finished.perRepo} на проект`),
  )
  const body = []
  // Первый кадр с настоящими строками: вход групп играет один раз. Дальше блок
  // меняется на месте — иначе каждое «ещё» моргало всем списком.
  const enter = !finished.painted && groups.some((group) => group.rows.length)
  if (finished.error && !index) {
    body.push(errorBanner(`ошибка запроса /deals: ${finished.error}`, () => void loadFinishedIndex()))
  } else if (!index) {
    body.push(finishedSkeleton())
  } else {
    if (index.errors.length) {
      body.push(noticesBlock(index.errors.map((err) => `источник ${err.source} недоступен: ${err.reason}`)))
    }
    if (!groups.length) {
      // Сначала — ошибка чтения, и только потом «дел нет»: пустой ответ
      // нечитаемого store не доказывает, что закрытых дел не существует.
      body.push(
        h(
          'div',
          { class: 'panel-b muted' },
          index.errors.length
            ? `store: ${index.errors[0].reason}`
            : index.note || 'в store нет ни одного закрытого дела',
        ),
      )
    } else {
      body.push(
        h(
          'div',
          { class: `finished-body${enter ? ' is-enter' : ''}` },
          ...groups.map((group, position) => finishedGroupSection(group, position, enter)),
        ),
      )
      // Общая кнопка — по пятёрке во всех проектах сразу; своя у каждой группы.
      const pending = groups.some((group) => !group.done || group.loading)
      body.push(
        h(
          'div',
          { class: 'pager' },
          h(
            'button',
            {
              class: `chip more-btn${groups.some((group) => group.loading) ? ' busy' : ''}`,
              disabled: !pending,
              onclick: () => void loadFinishedMore(),
              'data-key': 'finished-more',
            },
            pending ? `загрузить ещё по ${finished.perRepo}` : 'всё загружено',
          ),
          h(
            'span',
            { class: 'muted' },
            `проектов: ${groups.length} · по ${finished.perRepo} на проект${finished.pages ? ` · ещё ${finished.pages}` : ''}`,
          ),
        ),
      )
    }
  }
  if (enter) finished.painted = true
  return h('div', { class: 'panel finished-panel', 'data-key': 'finished' }, head, ...body)
}

/**
 * Группа завершённых: имя проекта, «5 из 23» и своя кнопка под списком.
 *
 * Кнопка стоит **под** таблицей, а не в шапке: новые строки появляются внизу, и
 * ожидание «нажму и увижу здесь» совпадает с тем, что происходит (09.11, Макс).
 * Пока идёт подгрузка, на месте будущих строк стоят скелетоны той же геометрии —
 * страница не мигает и не прыгает, а показывает, где окажутся данные.
 */
function finishedGroupSection(group, position, enter) {
  const label = repoLabelOf(group.repo)
  const left = Math.max(0, group.total - group.rows.length)
  const rows = group.rows.length
    ? group.rows.map((deal) => finishedRow(deal))
    : [0, 1, 2].map((index) => skelRow(index))
  // Подгрузка: пятёрка скелетонов встаёт после показанных строк.
  const pending = group.loading && !group.done ? Math.min(finished.perRepo, Math.max(1, left)) : 0
  for (let index = 0; index < pending; index += 1) rows.push(skelRow(index, 'pending'))
  const moreText = group.done
    ? 'это все закрытые дела'
    : group.loading
      ? 'загружаю…'
      : `ещё ${Math.min(finished.perRepo, Math.max(1, left))}${left > finished.perRepo ? ` · осталось ${left}` : ''}`
  return h(
    'section',
    {
      class: 'finished-group',
      'data-key': `finished-group:${group.repo}`,
      style: enter ? `animation-delay:${Math.min(position * 60, 300)}ms` : 'animation:none',
    },
    h(
      'div',
      { class: 'grp-h' },
      h('span', { class: 'grp-k mono', title: group.repo || 'репозиторий не назван' }, label),
      h('span', { class: 'n' }, `${group.rows.length} из ${group.total}`),
      group.error ? h('span', { class: 'grp-s danger' }, `ошибка: ${group.error}`) : null,
    ),
    h(
      'table',
      { class: 'grp-table' },
      colGroup(FINISHED_COLS),
      h(
        'thead',
        null,
        h(
          'tr',
          null,
          h('th', null, 'закрыто'),
          h('th', null, 'дело'),
          h('th', null, 'заголовок'),
          h('th', null, 'статус'),
          h('th', null, 'попыток'),
          h('th', null, 'вопросов'),
          h('th', null, 'длительность'),
          h('th', null, 'причина'),
          h('th', null, 'этапы'),
          h('th', null, 'публикация'),
        ),
      ),
      h('tbody', null, ...rows),
    ),
    // Под списком — там, где появятся новые строки.
    h(
      'div',
      { class: 'grp-more' },
      h(
        'button',
        {
          class: `chip more-btn${group.loading ? ' busy' : ''}`,
          disabled: group.loading || group.done,
          onclick: () => void loadFinishedGroup(group.repo, 'more'),
          'data-key': `finished-more:${group.repo}`,
        },
        moreText,
      ),
    ),
  )
}

/** Строка-скелетон: держит геометрию настоящей строки, пока данных нет. */
function skelRow(index, kind = 'first') {
  return h(
    'tr',
    {
      class: `skel-tr is-${kind}`,
      'data-key': `skel:${kind}:${index}`,
      style: `animation-delay:${index * 70}ms`,
    },
    h('td', { colspan: '10' }, h('span', { class: 'skel skel-cell' })),
  )
}

/** Подпись репозитория для группы: хвост пути, полный — в подсказке. */
function repoLabelOf(repo) {
  const path = String(repo || '')
  if (!path) return 'без репозитория'
  const parts = path.split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : path
}



/* -------------------------------------------------------- экран «почему стоит» */

function whyCard(deal) {
  const open = () => {
    location.hash = `#/deal/${encodeURIComponent(deal.id)}`
  }
  return h(
    'article',
    { class: 'why-card' },
    h('h3', null, deal.titleFull || deal.title),
    h(
      'div',
      { class: 'why-meta' },
      Badge(deal.state, deal.humanKind),
      h('span', { class: 'mono' }, `${deal.repoLabel} · ${deal.id}`),
      h('span', { class: 'muted' }, deal.assignee),
    ),
    h('div', { class: 'why-evidence', text: deal.evidence || 'доказательство не записано' }),
    h(
      'div',
      { class: 'why-foot' },
      h('span', null, `попытка: ${deal.attemptAgeText} (${deal.attemptNote})`),
      h('span', null, `действие: ${deal.lastActionAgeText} назад`),
      h('a', { href: `#/deal/${encodeURIComponent(deal.id)}`, onclick: open }, 'открыть дело →'),
    ),
  )
}

function viewWhy() {
  if (!snap.data && snap.error) return errorBanner(`ошибка запроса /now: ${snap.error}`, loadNow)
  if (!snap.data) return h('div', { class: 'empty' }, h('b', null, 'загрузка…'))

  const model = shapeNow(snap.data)
  const stale = Boolean(snap.error)
  const parts = []
  if (snap.error) parts.push(errorBanner(`снимок от ${fmtClock(model.now)} — обновление не удалось: ${snap.error}`, loadNow))
  const note = noticesBlock(noticeLines(model, stale))
  if (note) parts.push(note)

  parts.push(
    h(
      'div',
      { class: 'note' },
      h(
        'div',
        null,
        'Здесь только то, что бэкенд назвал сам: группа — это состояние ожидания, текст под заголовком — его доказательство (факты из лизинга, pid, heartbeat, графа зависимостей и пула вопросов). Страница ничего не досчитывает.',
      ),
    ),
  )

  if (!model.count) {
    parts.push(h('div', { class: 'empty' }, h('b', null, 'нечего объяснять: активных дел нет')))
    return h('div', null, ...parts)
  }

  for (const group of model.stateGroups) {
    parts.push(
      h('div', { class: 'sec-h' }, h('h2', null, group.label), h('span', { class: 'num' }, `${group.count} · ${group.state}`)),
    )
    parts.push(h('div', { class: 'why-grid' }, ...group.deals.map(whyCard)))
  }
  return h('div', null, ...parts)
}

/* ----------------------------------------------------------- экран «здоровье» */

async function loadHealth() {
  health.loading = true
  try {
    health.data = await api('/stats')
    health.error = null
    setStamp(Date.now())
  } catch (err) {
    health.error = err.message
  } finally {
    health.loading = false
    render()
  }
}

function kpi(title, value, sub, barPct) {
  return h(
    'div',
    { class: 'kpi' },
    h('div', { class: 'k' }, title),
    h('div', { class: 'v' }, value),
    h('div', { class: 's' }, sub),
    barPct === null || barPct === undefined ? null : h('div', { class: 'kpi-bar' }, h('i', { style: `width:${barPct}%` })),
  )
}

function taxonomy(stats) {
  const totals = stats.totals
  const entries = Object.entries(totals.attempts_by_outcome || {}).sort((a, b) => b[1] - a[1])
  if (!entries.length) return h('div', { class: 'empty' }, h('b', null, 'попыток нет'), h('span', { class: 'muted' }, 'таксономия считается по журналу дел'))
  const max = entries[0][1] || 1
  const crashes = new Set(stats.crash_outcomes || [])
  const success = new Set(stats.success_outcomes || [])
  return h(
    'div',
    { class: 'tax' },
    ...entries.map(([key, count]) => {
      const tone = crashes.has(key) ? 'crash' : success.has(key) ? 'ok' : key === 'blocked' ? 'warn' : ''
      return h(
        'div',
        { class: `tax-row ${tone}` },
        h('span', { class: 'name', title: key }, key),
        h('div', { class: 'tbar' }, h('i', { style: `width:${Math.max(1, Math.round((count / max) * 100))}%` })),
        h('span', { class: 'tnum' }, `${count}`),
      )
    }),
  )
}

/**
 * Таблица «по репозиториям»: те же группы и числа, что печатает `bin/duty stats`.
 *
 * Источник — `repos[]` и `without_repo` ответа `/stats`: доски в статистике не
 * участвуют (0.0.6), «без репозитория» — законная строка, а не пропуск.
 */
function repoTable(stats) {
  const groups = [...(stats.repos || []), ...(stats.without_repo && stats.without_repo.deals ? [stats.without_repo] : [])]
  const rows = groups.map((group) => {
    const share = group.first_try.share
    return h(
      'tr',
      null,
      h('td', { class: 'mono' }, group.repo || WITHOUT_REPO_LABEL),
      h('td', { class: 'n' }, `${group.deals}`),
      h('td', { class: 'n' }, `${group.attempts_total}`),
      h('td', { class: 'n' }, `${group.first_try.ok}/${group.first_try.closed} · ${pct(share)}`),
      h('td', { class: 'n' }, group.waiting_sec.p50 === null ? '—' : `${fmtAge(group.waiting_sec.p50)} / ${fmtAge(group.waiting_sec.p90)}`),
      h('td', { class: 'n' }, group.waiting_sec.count ? `${group.waiting_sec.count}` : '—'),
      h('td', { class: 'muted' }, '—'),
    )
  })
  return h(
    'div',
    { class: 'panel' },
    h('div', { class: 'panel-h' }, 'по репозиториям'),
    groups.length
      ? h(
          'table',
          null,
          h(
            'thead',
            null,
            h(
              'tr',
              null,
              h('th', null, 'репозиторий'),
              h('th', null, 'дел'),
              h('th', null, 'попыток'),
              h('th', null, 'с первой попытки'),
              h('th', null, 'ожидание p50 / p90'),
              h('th', null, 'ожиданий'),
              h('th', null, 'неполнота'),
            ),
          ),
          h('tbody', null, ...rows),
        )
      : h('div', { class: 'panel-b muted' }, 'дел в store нет: считать нечего'),
  )
}

function viewHealth() {
  if (!health.data && health.error) return errorBanner(`ошибка запроса /stats: ${health.error}`, loadHealth)
  if (!health.data) return h('div', { class: 'empty' }, h('b', null, 'считаю здоровье…'))

  const stats = health.data
  const totals = stats.totals
  const repos = stats.repos || []
  const without = stats.without_repo || null
  // Падения — по наборам исходов бэкенда, а не по своему списку.
  const crashes = new Set(stats.crash_outcomes || [])
  const outcomeEntries = Object.entries(totals.attempts_by_outcome || {})
  const crashRuns = outcomeEntries
    .filter(([key]) => crashes.has(key))
    .reduce((acc, [, count]) => acc + count, 0)

  const parts = []
  if (health.error) parts.push(errorBanner(`обновление не удалось: ${health.error}`, loadHealth))
  if (stats.errors.length) {
    parts.push(noticesBlock(stats.errors.map((err) => `источник ${err.source} недоступен: ${err.reason}`)))
  }
  if (stats.warnings && stats.warnings.length) parts.push(noticesBlock(stats.warnings))

  // Окно — часть числа, а не украшение: «за сутки» и «за всё время» — разные метрики.
  const win = stats.window || {}
  parts.push(
    h(
      'div',
      { class: 'muted' },
      `окно: ${win.source === 'default' ? 'сутки по умолчанию' : 'задано вручную'} (${fmtAge(win.sec)}) · числа считаются по журналу дел; карточки kanban — отдельное наблюдение`,
    ),
  )

  parts.push(
    h(
      'div',
      { class: 'kpis' },
      kpi(
        'дела с первой попытки',
        pct(totals.first_try.share),
        `${totals.first_try.ok} из ${totals.first_try.closed} этапов, закрытых попытками`,
        totals.first_try.share === null ? null : Math.round(totals.first_try.share * 100),
      ),
      kpi(
        'ожидание до первой попытки: p50 / p90',
        totals.waiting_sec.p50 === null ? '—' : `${fmtAge(totals.waiting_sec.p50)} / ${fmtAge(totals.waiting_sec.p90)}`,
        totals.waiting_sec.count
          ? `p99: ${fmtAge(totals.waiting_sec.p99)} · максимум: ${fmtAge(totals.waiting_sec.max)} · выборка: ${totals.waiting_sec.count} дел (created_at → started_at)`
          : 'выборки нет: у дел среза нет ни одного рана (created_at → started_at)',
      ),
      kpi('попыток всего', `${totals.attempts_total}`, `падений: ${crashRuns}${totals.attempts_total ? ` · ${pct(crashRuns / totals.attempts_total)}` : ''}`),
      kpi(
        'репозиториев',
        `${repos.length}`,
        `дел в store: ${totals.deals} · без репозитория: ${without ? without.deals : 0}`,
      ),
    ),
  )

  parts.push(h('div', { class: 'sec-h' }, h('h2', null, 'таксономия исходов попыток'), h('span', { class: 'num' }, 'по журналу дел')))
  parts.push(h('div', { class: 'panel' }, h('div', { class: 'panel-b' }, taxonomy(stats))))

  parts.push(h('div', { class: 'sec-h' }, h('h2', null, 'репозитории')))
  parts.push(repoTable(stats))

  parts.push(
    h(
      'div',
      { class: 'note' },
      h(
        'div',
        null,
        'Как это считается: успех попытки — исход из набора успеха (передаётся бэкендом), падение — из набора crash. «С первой попытки» — этап журнала закрыт первой же попыткой и успешно. p50/p90/p99 ожидания — метод ближайшего ранга (позиция ceil(p/100 · n) в возрастающем ряду, без интерполяции) по объединённой выборке дел репозитория. Группа — `repo` строки дела; доски в статистике не участвуют.',
      ),
    ),
  )
  return h('div', null, ...parts)
}

/* ------------------------------------------------------------- экран «дело» */

// «Дело» — полноценный экран: таб открывает последнее открытое дело, а если
// такого нет — список активных дел, из которого видно, куда идти. Запоминаем в
// localStorage: переключение табов не должно терять контекст. Адрес дела — его id
// (0.0.6): доски в ключе больше нет, и старый ключ `board/id` при чтении просто не
// совпадёт с форматом — покажется «последнего нет», а не битая ссылка.
const LAST_DEAL = 'duty:last-deal'

function rememberDeal(dealId) {
  try {
    localStorage.setItem(LAST_DEAL, String(dealId))
  } catch {
    /* приватный режим / выключенное хранилище — не повод ломать экран */
  }
}

function lastDeal() {
  let raw = ''
  try {
    raw = localStorage.getItem(LAST_DEAL) || ''
  } catch {
    return null
  }
  // Прежний ключ был `board/id` — два сегмента; такой адрес больше не открыть.
  if (!raw || raw.includes('/')) return null
  return { id: raw }
}

async function loadDeal(dealId) {
  rememberDeal(dealId)
  const key = String(dealId)
  if (dealSnap.key !== key) {
    dealSnap.key = key
    dealSnap.data = null
    dealSnap.attempts = null
    dealSnap.error = null
    dealSnap.refreshedAt = 0
    dealSnap.refreshError = ''
    dealSnap.spots = []
    // Раскрытия принадлежат прошлому делу: ключи включают его id, и помнить чужие
    // незачем — новое дело начинается с умолчаний вида.
    stageOpen.clear()
    attOpen.clear()
    stageShown.clear()
    attShown.clear()
  }
  dealSnap.loading = true
  try {
    const [detail, attempts] = await Promise.all([
      api(`/deal/${encodeURIComponent(dealId)}`),
      api(`/duty/${encodeURIComponent(dealId)}/attempts`),
    ])
    if (dealSnap.key === key) {
      dealSnap.data = detail
      dealSnap.attempts = attempts
      dealSnap.error = null
      dealSnap.refreshError = ''
      dealSnap.refreshedAt = Math.floor(Date.now() / 1000)
      dealSnap.spots = dealSpots(detail, attempts)
      setStamp(Date.now())
    }
  } catch (err) {
    if (dealSnap.key === key) dealSnap.error = err.message
  } finally {
    dealSnap.loading = false
    // Полная перерисовка — только когда страницы дела ещё нет. Дальше правка на
    // месте: ручное обновление не должно отбирать прокрутку и раскрытия.
    if (dealOn(key) && dealSnap.data && app.querySelector('[data-key="scale"]')) patchDeal(key)
    else render()
  }
}

/**
 * «Дело» без выбранного дела: последнее открытое плюс активные дела списком.
 * Экран обязан быть достижим сам по себе — иначе он существует только как
 * побочный эффект клика по строке, и его «нет» на самом деле «не видно».
 */
function viewDealPicker() {
  const model = snap.data ? shapeNow(snap.data) : null
  const last = lastDeal()
  const parts = [h('div', { class: 'sec-h' }, h('h2', null, 'дело'), h('span', { class: 'num' }, 'открыть дело'))]
  if (last) {
    parts.push(
      h(
        'div',
        { class: 'note' },
        h('div', null, 'последнее открытое: '),
        h('a', { href: `#/deal/${encodeURIComponent(last.id)}` }, last.id),
      ),
    )
  }
  if (!model) {
    parts.push(h('div', { class: 'panel' }, h('div', { class: 'panel-b' }, h('div', { class: 'skel', style: 'width:60%' }))))
    return h('div', null, ...parts)
  }
  parts.push(...(model.groups.length ? model.groups.map(groupBlock) : [h('div', { class: 'empty' }, 'активных дел нет')]))
  return h('div', null, ...parts)
}

/**
 * Отказ по старому адресу дела: `#/deal/<board>/<id>`.
 *
 * Молча открыть дело по второму сегменту значило бы оставить в закладках адрес,
 * которого нет; показать половину карточки — выдать отказ за успех. Поэтому экран
 * называет, что доски больше нет, и даёт ссылку на id из прежнего адреса.
 */
function viewDealBadAddress(board, dealId) {
  // Форма id — та же, что у бэкенда (`deals.is_own`: `^d_[0-9a-f]{6,}$`): адрес,
  // который бэкенд заведомо не примет, не должен превращаться в ссылку.
  const id = dealId && /^d_[0-9a-f]{6,}$/.test(dealId) ? dealId : ''
  return h(
    'div',
    { class: 'empty' },
    h('b', null, 'так дело больше не адресуется'),
    h(
      'span',
      { class: 'muted' },
      `в адресе ${board}/${dealId} первая часть — доска, а досок с 0.0.6 нет: дело называется своим id d_<hex>.`,
    ),
    id
      ? h('a', { href: `#/deal/${encodeURIComponent(id)}` }, `открыть ${id} →`)
      : h('span', { class: 'muted' }, 'id дела выглядит как d_<hex> — возьмите его из списка.'),
    h('a', { href: '#/deal' }, '← к списку дел'),
    h('a', { href: '#/now' }, '← к срезу «сейчас»'),
  )
}

/* ------------------------------- столбец этапов и попыток (0.0.8) */

// Раскрытие строк — состояние поверхности, а не факт дела: шейпер отдаёт только
// умолчание (`openByDefault` — провал и живая попытка раскрыты без клика), а что
// человек открыл или свернул руками, помнит экран. Ключи включают id дела: имена
// этапов у двух дел совпадают, а раскрытия — нет.
const stageOpen = new Map()
const attOpen = new Map()
// Умолчание раскрытия на последнем нарисованном кадре вида. Клик читает его
// отсюда: тихое обновление правит узлы на месте, и замыкание отрисовки иначе
// держало бы раскрытие по устаревшему виду.
const stageDefaults = new Map()
const attDefaults = new Map()

// Строки, которые экран уже показывал раскрытыми: живая попытка, только что
// закончившаяся, не должна схлопываться под руками у человека, который на неё
// смотрел. Раскрытое остаётся раскрытым, пока его не свернут руками.
const stageShown = new Set()
const attShown = new Set()

/**
 * Раскрытие строки с учётом показанного: ручное решение важнее всего, иначе —
 * умолчание вида, иначе — «её уже видели раскрытой».
 */
const rowOpen = (overrides, shown, key, byDefault) =>
  overrides.has(key) ? overrides.get(key) === true : byDefault === true || shown.has(key)

/**
 * Попытки этапа для столбца.
 *
 * Факты процесса (`live`/`dead`, лизинг, код выхода) знает только лента `/deal`,
 * журнал попытки — только ручка `/duty/{id}/attempts`. Своих номеров и исходов
 * экран не считает: попытка ленты ищется в ответе ручки по `runId` (оба зовут
 * одну попытку одним именем), и ей подмешивается один факт — `events`.
 */
/* ---------------------------------------- слой подсказок поверх всей страницы */
/* Подсказка не может жить внутри дорожки: панель обрезает её по `overflow`, а
 * соседние строки с собственным контекстом наложения оказываются выше. Поэтому
 * подсказка — один элемент в `body`, положение считается от прямоугольника
 * хоста: над ним, а если сверху не влезла — под ним; по горизонтали прижимается
 * к краю окна. Наведение ловится делегированием: автообновление меняет узлы. */
const TIP_HOSTS = '.tl-call, .tl-band, .tl-lbl, .tl-mark, .tl-wait, .tl-say, .tl-silence'

let tipEl = null
let tipHost = null

function tipNode() {
  if (tipEl && tipEl.isConnected) return tipEl
  tipEl = h('div', { class: 'tl-tip', role: 'tooltip', 'aria-hidden': 'true' })
  document.body.append(tipEl)
  return tipEl
}

function hideTip() {
  tipHost = null
  if (!tipEl) return
  tipEl.style.opacity = '0'
  tipEl.setAttribute('aria-hidden', 'true')
}

function placeTip(host) {
  const text = host.getAttribute('data-tip')
  if (!text) return hideTip()
  const tip = tipNode()
  if (tip.textContent !== text) tip.textContent = text
  tip.style.opacity = '1'
  tip.setAttribute('aria-hidden', 'false')
  const box = host.getBoundingClientRect()
  const own = tip.getBoundingClientRect()
  const pad = 8
  const gap = 7
  const vw = window.innerWidth
  const vh = window.innerHeight
  let left = box.left + box.width / 2 - own.width / 2
  left = Math.max(pad, Math.min(left, vw - own.width - pad))
  let top = box.top - own.height - gap
  if (top < pad) top = Math.min(box.bottom + gap, vh - own.height - pad)
  tip.style.left = `${Math.round(left)}px`
  tip.style.top = `${Math.round(top)}px`
}

function bindTipLayer() {
  const hostOf = (event) => {
    const target = event.target
    return target && target.closest ? target.closest(TIP_HOSTS) : null
  }
  document.addEventListener('pointerover', (event) => {
    const host = hostOf(event)
    if (!host) return
    tipHost = host
    placeTip(host)
  })
  document.addEventListener('pointerout', (event) => {
    const host = hostOf(event)
    if (!host || host !== tipHost) return
    const next = event.relatedTarget
    if (next && host.contains(next)) return
    hideTip()
  })
  document.addEventListener('focusin', (event) => {
    const host = hostOf(event)
    if (!host) return
    tipHost = host
    placeTip(host)
  })
  document.addEventListener('focusout', hideTip)
  // Прокрутка и изменение размера: подсказка привязана к месту хоста.
  const follow = () => {
    if (!tipHost) return
    if (!tipHost.isConnected) return hideTip()
    placeTip(tipHost)
  }
  window.addEventListener('scroll', follow, true)
  window.addEventListener('resize', follow)
  // Автообновление подменяет узлы: если хост исчез, подсказку надо снять.
  window.dutyTipFollow = follow
}


/** Доказательство строкой: код выхода тоном, обрезанная команда, время. */
function attEvNode(row) {
  return h(
    'div',
    { class: 'att-ev' },
    // Полная команда — в подсказке: обрезка сделана в шейпере, а не CSS-эллипсисом.
    h('span', { class: `badge t-${row.tone}` }, `exit ${row.exitText}`),
    h('span', { class: 'mono att-ev-cmd', title: row.cmdTitle }, row.cmdShort),
    h('span', { class: 'muted' }, row.clock),
  )
}

/**
 * Попытка столбца: число, длительность, исход словами, глиф раскрытия.
 *
 * Раскрытая показывает доказательства, потом `summary` и `error`, потом журнал —
 * сводкой по видам (`5 прогрессов · 1 старт · 1 конец`), а не списком событий:
 * у одной попытки их бывает триста, и в столбце 320 px это лог, а не строка.
 */
function attRowNodes(stage, bar, key, open, rebuild) {
  const view = compactAttemptView(stage, bar)
  const nodes = [
    h(
      'div',
      {
        class: open ? 'att-row open' : 'att-row',
        title: view.outcomeText,
        'aria-expanded': open ? 'true' : 'false',
        'data-key': key,
        onclick: () => {
          // Текущее состояние читается здесь, а не из замыкания отрисовки: после
          // тихого обновления умолчание вида могло смениться.
          const current = rowOpen(attOpen, attShown, key, attDefaults.get(key) === true)
          attOpen.set(key, !current)
          if (current) attShown.delete(key)
          else attShown.add(key)
          rebuild()
        },
      },
      h('span', { class: 'mono att-row-n' }, view.label),
      h('span', { class: 'att-row-dur fresh', 'data-fresh': `att:${key}` }, view.durationText),
      h('span', { class: `att-row-out t-${view.tone}` }, view.outcomeText),
      h('span', { class: 'sp' }),
      h('span', { class: 'att-row-tog' }, open ? '▾' : '▸'),
    ),
  ]
  if (!open) return nodes
  // Числа попытки — здесь, в подробностях «этапов», а не значком на шкале:
  // гуттер узкий, «136 вызовов · 11 646 133 токена» в нём обрезалось.
  if (view.statsText) {
    nodes.push(
      h(
        'div',
        {
          class: `att-stats mono${view.statsCut ? ' is-cut' : ''}`,
          title: view.statsNote || view.statsText,
          'data-key': `stats:${key}`,
        },
        view.statsText,
      ),
    )
  }
  for (const row of view.evidenceRows) nodes.push(attEvNode(row))
  if (view.summary) nodes.push(h('div', { class: 'att-summary' }, view.summary))
  if (view.error) nodes.push(h('div', { class: 'att-error' }, view.error))
  if (view.eventsText) nodes.push(h('div', { class: 'att-log' }, view.eventsText))
  return nodes
}

/**
 * Этап столбца: одна строка (~26 px) плюс попытки, если раскрыт.
 *
 * Служебное (`needs`) живёт в подсказке строки: отдельной строкой «после: …» оно
 * съедало высоту, которую занимают попытки. Ожидание вопроса — наоборот, видимой
 * строкой: дело может стоять на человеке, и это не деталь для подсказки.
 */
function stageBlockNode(dealId, stage, timeline, rebuild) {
  const view = compactStageView(stage)
  const stageKey = `${dealId}:${view.id}`
  // По умолчанию свёрнуты все этапы, включая текущий и упавший (Макс, 18.11):
  // раскрытие — это чтение попыток, а не состояние дела, и оно не должно
  // разворачиваться само на каждом обновлении. Состояние этапа видно в бейдже.
  const open = rowOpen(stageOpen, stageShown, stageKey, false)
  if (open) stageShown.add(stageKey)
  // Умолчание раскрытия на текущем кадре: клик читает его отсюда, чтобы после
  // тихого обновления не спорить с устаревшим замыканием.
  stageDefaults.set(stageKey, false)
  const bars = stageColumnBars(stage, timeline)
  const nodes = [
    h(
      'div',
      {
        class: open ? 'stage-row open' : 'stage-row',
        title: view.needsText || view.toggleTitle,
        'aria-expanded': open ? 'true' : 'false',
        onclick: () => {
          const current = rowOpen(stageOpen, stageShown, stageKey, stageDefaults.get(stageKey) === true)
          stageOpen.set(stageKey, !current)
          if (current) stageShown.delete(stageKey)
          else stageShown.add(stageKey)
          // Кадр не пересобираем: пересборка заменила бы узел, и переход высоты
          // не сыграл бы — этап раскрывался бы рывком. Класс и атрибуты правим
          // здесь же, а следующий тик обновления нарисует то же состояние (Макс,
          // 18.11: «разворачивались с анимацией»).
          const block = document.querySelector(`.stage-block[data-key="block:${stageKey}"]`)
          if (!block) return
          block.classList.toggle('is-open', !current)
          block.querySelector('.stage-row')?.classList.toggle('open', !current)
          block.querySelector('.stage-body')?.setAttribute('aria-hidden', current ? 'true' : 'false')
          const row = block.querySelector('.stage-row')
          if (row) row.setAttribute('aria-expanded', current ? 'false' : 'true')
          const tog = block.querySelector('.stage-row-tog')
          if (tog) tog.textContent = current ? '▸' : '▾'
        },
      },
      // Крупно — код этапа: по нему ищут в журнале и графе. Описание из спеки
      // идёт строкой ниже (две строки), статистика — последней строкой.
      h('span', { class: 'mono stage-row-code', title: view.nameHint }, view.id),
      h('span', { class: `badge t-${view.tone}`, title: view.stateLabel }, h('i'), view.stateLabel),
      h('span', { class: 'stage-row-tog' }, open ? '▾' : '▸'),
      view.name && view.name !== view.id ? h('span', { class: 'stage-row-desc', title: view.name }, view.name) : null,
      h(
        'span',
        { class: 'stage-row-meta', title: view.statsTitle || '' },
        h('span', { class: 'fresh', 'data-fresh': `stage:${stageKey}` }, view.attemptsText),
        // Работа этапа: вызовы инструментов и токены — те же числа, что в
        // раскрытой попытке, но видны без раскрытия.
        view.statsText ? ` · ${view.statsText}` : '',
        view.kindText ? ` · ${view.kindText}` : '',
      ),
    ),
  ]
  // Состояние лида — строкой у координаторского этапа, а не только в шапке:
  // «кто сейчас работает» ищут в списке этапов. Слово то же, что у шапки
  // (`leadfeed.status`), а расхождение «этап идёт, лида нет» называется одной
  // фразой (`leadNoteOf` в шейпере), а не двумя спорящими фактами.
  if (view.leadText) {
    nodes.push(
      h(
        'div',
        {
          class: `stage-lead${view.leadWarn ? ' is-warn' : ''}`,
          title: view.leadTitle || 'состояние лида по журналу',
        },
        view.leadText,
      ),
    )
  }
  if (view.waitText) {
    nodes.push(
      h(
        'div',
        { class: 'stage-wait' },
        view.waitHref ? h('a', { href: view.waitHref }, view.waitText) : view.waitText,
      ),
    )
  }
  // «Работы не было» — исход пройденного этапа, а не провал: строка стоит под
  // этапом тем же шрифтом, что «ждёт», но не жёлтым (`stage-note`), потому что
  // ждать нечего и чинить нечего.
  if (view.nothingText) nodes.push(h('div', { class: 'stage-note' }, view.nothingText))
  // Тело рендерится всегда и лежит в DOM свёрнутым (`grid-template-rows: 0fr`):
  // раскрытие — это смена класса, поэтому браузер анимирует высоту, а пересборка
  // кадра ничего не рвёт (морф сохраняет узлы по `data-key`).
  const body = nodes.splice(1)
  if (!bars.length) body.push(h('div', { class: 'att-none muted' }, 'попыток нет'))
  bars.forEach((bar, index) => {
    // Ключ попытки — её номер, а не место в списке: новая попытка сдвинула бы
    // индексы, и раскрытие переехало бы на чужую строку.
    const attKey = `${stageKey}#${bar.number || index}`
    const barView = compactAttemptView(stage, bar)
    attDefaults.set(attKey, barView.openByDefault)
    const attOpenNow = rowOpen(attOpen, attShown, attKey, barView.openByDefault)
    if (attOpenNow) attShown.add(attKey)
    body.push(...attRowNodes(stage, bar, attKey, attOpenNow, rebuild))
  })
  return h(
    'div',
    { class: open ? 'stage-block is-open' : 'stage-block', 'data-key': `block:${stageKey}` },
    ...nodes,
    h(
      'div',
      { class: 'stage-body', 'aria-hidden': open ? 'false' : 'true' },
      h('div', { class: 'stage-body-inner' }, ...body),
    ),
  )
}

/**
 * Решения лида — лентой на вертикальной оси времени, в правом рельсе дела.
 *
 * Таблица во всю ширину повторяла журнал терминала: сорок строк, прочитанных
 * один раз, и половина панели пустой. Здесь у решений своя ось: точка вида,
 * часы, слово вида, зазор до прошлого решения, автор и слова до трёх строк.
 *
 * Свежие сверху (порядок — шейпер). По умолчанию шесть строк, остальные — по
 * кнопке: рельс не должен расти на экран из-за истории. Пустая лента не
 * рисуется: «решений нет» на свежем деле — шум, а не факт.
 */
const DECISION_PREVIEW = 6

function decisionsPanel(items, plan, dealId) {
  const total = decisionsCount(items)
  if (!total) return null
  const rows = shapeDecisions(items)
  const groups = decisionGroups(rows)
  const key = String(dealId || '')
  const open = decisionsOpen.has(key)
  const panel = h(
    'div',
    { class: `panel dec-panel${open ? ' is-open' : ''}`, 'data-key': 'decisions', 'data-deal': key, 'data-total': String(total) },
    h(
      'div',
      { class: 'panel-h' },
      h('span', { class: 'dec-panel-title', title: plan ? plan.hint : '' }, `решения лида · ${total}`),
      // Кнопка раскрытия — единственный контрол в шапке: фильтр живёт строкой
      // ниже, и два ряда кнопок в 340 px читались бы как пульт.
      h(
        'button',
        { class: 'dec-toggle', type: 'button', 'data-act': 'toggle', 'aria-expanded': open ? 'true' : 'false' },
        open ? 'свежие' : `все ${total}`,
      ),
    ),
    // Фильтр семейств: вопрос «что решалось по этапам» задаётся один раз, а не
    // перечитыванием всей ленты. Выключенная строка гаснет, но остаётся на
    // месте — пропавшие строки читались бы как «решений не было».
    h(
      'div',
      { class: 'dec-filters', role: 'group', 'aria-label': 'фильтр решений по семействам' },
      h('button', { class: 'dec-filter on', type: 'button', 'data-group': '', 'aria-pressed': 'true' }, `все ${total}`),
      ...groups.map((group) =>
        h(
          'button',
          { class: 'dec-filter', type: 'button', 'data-group': group.key, 'aria-pressed': 'false' },
          `${group.label} ${group.count}`,
        ),
      ),
    ),
    // Решения — таблица, а не таймлайн: у записи есть поля (время, вид, автор,
    // этап), и человек сравнивает их по колонкам, а не выискивает на оси. Слова
    // решения — вторая строка во всю ширину: в 340 px втиснуть их в колонку
    // значит резать мысль до трёх слов (Макс, 14.11: «таблицей, а не таймлайном»).
    h(
      'div',
      { class: 'dec-table', role: 'table', 'aria-label': 'решения лида' },
      h(
        'div',
        { class: 'dec-thead', role: 'row' },
        h('span', { role: 'columnheader' }, 'время'),
        h('span', { role: 'columnheader' }, 'решение'),
        h('span', { role: 'columnheader' }, 'кто'),
        h('span', { role: 'columnheader' }, 'этап'),
      ),
      ...rows.map((row) =>
        h(
          'div',
          { class: 'dec-row', key: row.key, role: 'row', 'data-group': row.group, 'data-at': row.at === null ? '' : String(row.at) },
          h('span', { class: 'dec-time mono', role: 'cell', title: row.ageText ? `${row.ageText} назад` : '' }, row.clock),
          h(
            'span',
            { class: `dec-kind t-${row.tone}`, role: 'cell', title: row.gapText ? `${row.gapText} после предыдущего решения` : '' },
            row.typeLabel,
          ),
          h('span', { class: 'dec-who', role: 'cell', title: row.author ? `автор: ${row.author}` : '' }, row.authorLabel || ''),
          h('span', { class: 'dec-stage mono', role: 'cell', title: row.stage ? `этап: ${row.stage}` : '' }, row.stage || ''),
          h(
            'span',
            { class: 'dec-say', role: 'cell', title: `${row.text}${row.gapText ? ` · ${row.gapText}` : ''}` },
            row.text,
          ),
        ),
      ),
    ),
  )
  wireDecisionsPanel(panel)
  return panel
}

// Раскрытые ленты и выбранные семейства принадлежат экрану и переживают тихий
// тик — так же, как подсветка инструментов (`dutyToolFilter`).
const decisionsOpen = new Set()
const dutyDecisionFilter = new Set()

// Шапка журнала решений слушает клик один раз: морф переиспользует узлы, и без
// отметки каждый тик добавлял бы ещё один обработчик.
const wiredDecisionHosts = new WeakSet()

/** Применить фильтр ленты решений к уже нарисованной панели. */
function applyDecisionFilter(panel) {
  const active = dutyDecisionFilter.size
  for (const row of panel.querySelectorAll('.dec-table .dec-row')) {
    const on = !active || dutyDecisionFilter.has(row.getAttribute('data-group'))
    row.classList.toggle('is-dim', !on)
  }
  for (const chip of panel.querySelectorAll('.dec-filter')) {
    const group = chip.getAttribute('data-group') || ''
    const on = group ? dutyDecisionFilter.has(group) : !active
    chip.classList.toggle('on', on)
    chip.setAttribute('aria-pressed', on ? 'true' : 'false')
  }
}

/**
 * Обработчики ленты решений: чипы семейств, раскрытие ленты и раскрытие строки.
 *
 * Ставятся один раз на панель (морф переиспользует узел), а состояние живёт в
 * модульных множествах: тихий тик пересобирает содержимое, но не сбрасывает
 * ни фильтр, ни раскрытую ленту.
 */
function wireDecisionsPanel(panel) {
  if (wiredDecisionHosts.has(panel)) return
  wiredDecisionHosts.add(panel)
  panel.addEventListener('click', (event) => {
    const chip = event.target.closest('.dec-filter')
    if (chip) {
      const group = chip.getAttribute('data-group') || ''
      if (!group) {
        dutyDecisionFilter.clear()
      } else if (dutyDecisionFilter.has(group)) {
        dutyDecisionFilter.delete(group)
      } else {
        dutyDecisionFilter.add(group)
      }
      applyDecisionFilter(panel)
      return
    }
    const toggle = event.target.closest('.dec-toggle')
    if (toggle) {
      const key = panel.getAttribute('data-deal') || ''
      const open = !panel.classList.contains('is-open')
      panel.classList.toggle('is-open', open)
      if (open) decisionsOpen.add(key)
      else decisionsOpen.delete(key)
      toggle.textContent = open ? 'свежие' : `все ${panel.getAttribute('data-total') || ''}`
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false')
      return
    }
    // Клик по самой строке раскрывает её слова целиком: обрезка тремя строками
    // — мера чтения в узком рельсе, а не запрет на подробности.
    const item = event.target.closest('.dec-row')
    if (item) item.classList.toggle('is-open')
  })
  applyDecisionFilter(panel)
}

/**
 * Столбец этапов и попыток в карточке дела.
 *
 * Одна панель вместо двух (широкой ленты этапов и отдельной панели попыток):
 * шапка попытки повторялась в обеих, и страница росла на три с половиной экрана.
 * Пустой граф печатает причину (`stages_note` бэкенда: нет спеки, спека не
 * читается) — «этапов нет» и «спеки нет» разные факты.
 */
function stagesColumn(dealId) {
  const host = h('div', { class: 'stages-list', 'data-key': 'stages-list' })
  // Пересборка по клику читает текущий снимок дела, а не тот, что был при
  // создании панели: иначе после тихого обновления раскрытие рисовало бы старые
  // попытки, а новая строка исчезала бы до следующего тика.
  const rebuild = () => {
    clear(host)
    const feed = shapeStages(dealSnap.data)
    if (feed.empty) {
      host.append(h('div', { class: 'panel-b muted' }, feed.note || 'этапов нет'))
      return
    }
    host.append(...feed.stages.map((stage) => stageBlockNode(dealId, stage, dealSnap.attempts, rebuild)))
  }
  rebuild()
  const feed = shapeStages(dealSnap.data)
  return h(
    'div',
    { class: 'panel deal-stages', 'data-key': 'stages' },
    h('div', { class: 'panel-h' }, feed.empty ? 'этапы' : `этапы · ${feed.count}`),
    // Путь к спеке — именем файла, полный путь в подсказке.
    feed.spec ? h('div', { class: 'stages-spec mono muted', title: feed.spec }, fileLabel(feed.spec)) : null,
    host,
  )
}

/* -------------------------------------------- шкала времени дела (E2) */

// Глиф маркера — только значок; слово и время приходят из шейпера (`label`,
// `title`), чтобы подпись не разъезжалась между вебом и десктопом.
const MARKER_GLYPH = {
  question: '?',
  answer: '+',
  verdict: '✓',
  budget: '!',
  published: '↑',
  closed: '■',
  started: '',
  ended: '',
}

/**
 * Строка фактов шапки дела. Числа целиком из `shapeDealTime`: попытки по этапам,
 * длительность (у живого дела — до «сейчас», с пометкой), вопросы человеку,
 * бюджет дела и факт публикации.
 */
function dealFactSpans(scale) {
  const facts = scale.facts
  // Числа, которые уже стоят в KPI (попытки, длительность), здесь не повторяются:
  // одна цифра в двух местах читается как два разных факта.
  const parts = [
    h('span', { title: 'вопросы человеку по таймлайну', 'data-key': 'fact:questions' }, 'вопросы: ', h('b', null, facts.questions.text)),
  ]
  if (facts.budget) {
    parts.push(
      h(
        'span',
        {
          title: facts.budget.exhausted ? `потолок исчерпан: ${facts.budget.exhausted}` : 'потолок дела целиком',
          'data-key': 'fact:budget',
        },
        'бюджет: ',
        h('b', null, facts.budget.text || '—'),
      ),
    )
  }
  if (facts.published) {
    parts.push(
      h(
        'span',
        {
          title: facts.published.mrUrl || facts.published.ref || 'факт журнала',
          'data-key': 'fact:published',
        },
        'публикация: ',
        h('b', { class: 'mono' }, facts.published.text || '—'),
      ),
    )
  }
  if (facts.closed) {
    parts.push(h('span', { title: facts.closed.cause || '', 'data-key': 'fact:closed' }, 'закрытие: ', h('b', null, facts.closed.text)))
  }
  return parts
}

/**
 * Маркер события: засечка старта/закрытия попытки или заметная точка события.
 *
 * Группа (несколько событий в одной точке оси) рисуется столбиком: глиф на
 * каждого участника, своим тоном, в одном и том же `left` — время не сдвинуто.
 * Одиночный маркер остаётся ровно прежним: один глиф, без класса группы.
 */
function markerNode(marker, index) {
  const dots =
    marker.grouped && marker.members.length
      ? marker.members.map((member, i) =>
          h('span', { class: `scale-marker-dot t-${member.tone}` }, MARKER_GLYPH[marker.types[i]] || ''),
        )
      : [h('span', { class: `scale-marker-dot t-${marker.tone}` }, MARKER_GLYPH[marker.type] || '')]
  return h(
    'span',
    {
      class: `scale-marker t-${marker.tone}${marker.tick ? ' tick' : ''}${marker.grouped ? ' is-grouped' : ''} anchor-${marker.anchor || 'middle'}`,
      style: `left:${marker.pct}%;top:${(index % 2) * 13}px`,
      title: marker.title,
      'data-key': `marker:${marker.type}:${marker.at}`,
      'aria-label': marker.ariaLabel,
      role: 'img',
      tabindex: '0',
    },
    ...dots,
  )
}

/**
 * Легенда шкалы: из чего она собрана и что означают метки этого дела.
 *
 * Постоянная часть (полоса, чипы, тишина) объясняет сам язык строки — без неё
 * легенда перечисляла только факты и не отвечала, что такое полоса и штриховка
 * (Макс, 14.11: «легенда неинформативная»). Дальше — только те виды маркеров,
 * что реально есть в журнале: группа называет все свои виды, иначе у дела,
 * закрытого приговором, «закрыто» пропало бы.
 */
function markerLegend(scale) {
  const fixed = [
    { key: 'band', swatch: 'band', label: 'полоса попытки — этап шёл' },
    { key: 'call', swatch: 'call', label: 'вызов инструмента, цвет — вид' },
    { key: 'silence', swatch: 'silence', label: 'штриховка — тишина: ни вызова, ни слова' },
    { key: 'wait', swatch: 'wait', label: 'разрыв и штриховка — ждали человека' },
  ]
  const seen = []
  for (const marker of scale.markers || []) {
    if (marker.tick) continue
    const types = marker.grouped && marker.types.length ? marker.types : [marker.type]
    const labels = marker.grouped && marker.labels.length ? marker.labels : [marker.label]
    for (const [i, type] of types.entries()) {
      if (seen.some((item) => item.type === type)) continue
      const tone = marker.grouped && marker.members[i] ? marker.members[i].tone : marker.tone
      // Обмен с ролью — стрелка: легенда показывает тот же знак, что на дорожке.
      const role = Boolean(marker.address && marker.address.kind === 'role')
      seen.push({ type, tone, label: labels[i] || marker.label, role })
    }
  }
  return h(
    'div',
    { class: 'scale-legend' },
    ...fixed.map((item) =>
      h(
        'span',
        { class: 'scale-legend-item', 'data-key': `legend:${item.key}` },
        h('i', { class: `scale-legend-sw is-${item.swatch}` }),
        item.label,
      ),
    ),
    // Факты на шкале — вертикальные засечки, а не значки: легенда показывает то
    // же, что видно на дорожке (Макс, 14.11: «v — вердикт — такого не видел»).
    ...seen.map((marker) =>
      h(
        'span',
        { class: 'scale-legend-item', 'data-key': `legend:${marker.type}` },
        h('i', { class: marker.role ? `scale-legend-arrow t-${marker.tone}` : `scale-legend-stick t-${marker.tone}` }),
        marker.label,
      ),
    ),
  )
}

/* ------------------------------------- таймлайн вызовов внутри попытки (0.0.8) */

// Палитра — единственное, что остаётся своего у поверхности: тон вызова
// приходит из шейпера ключом (`read`/`write`/`run`/`net`/`other`), а во что он
// красится, знает только свой CSS.
// Тон вызова приходит из шейпера ключом; здесь — только палитра поверхности.
// Поиск отделён от чтения: в деле их сотни, и одним цветом карта вызовов
// читалась как «всё одно и то же».
const TONE_VAR = {
  read: 'var(--accent)',
  search: 'var(--purple)',
  write: 'var(--ok)',
  run: 'var(--warn)',
  net: 'var(--danger)',
  mcp: 'var(--teal)',
  meta: 'var(--pink)',
  other: 'var(--text-3)',
}
function toneVar(tone) {
  return TONE_VAR[tone] || TONE_VAR.other
}

/**
 * Строка попытки: фон-полоса на всю длительность, чипы вызовов и маркеры внутри.
 *
 * Границы попытки показывает фон, поэтому засечек старта/конца больше нет, а
 * время начала, конца и длительность живут в подсказке строки — они не спорят с
 * чипами за место на дорожке.
 */
function attemptRowNode(view, key, hint = '') {
  // Внутри дорожки — своя коробка со сдвигом влево: полоса 58 px под подпись
  // попытки, чтобы номер не ложился на «колбаску» даже у самой первой попытки,
  // которая начинается с кромки (Макс, 14.11).
  const track = h('div', { class: 'tl-track', 'data-key': `track:${key}` })
  const plot = h('div', { class: 'tl-plot' })
  track.append(plot)
  if (view.band) {
    plot.append(
      h('span', {
        class: `tl-band t-${view.band.tone || 'grey'}${view.open ? ' is-live' : ''}`,
        style: `left:${view.band.leftPct}%;width:${view.band.widthPct}%`,
        'data-tip': view.bandTip,
        'data-key': 'band',
      }),
    )
  }
  // Подпись попытки стоит у самой полосы, а не в гуттере: колонка подписей
  // забирала 186 px у шкалы (Макс, 14.11: «название попытки и её статус рисовать
  // рядом с колбаской слева»). Полоса под подпись отведена внутри дорожки, поэтому
  // номер стоит слева от полосы даже у первой попытки, которая началась с кромки.
  const bandLeft = view.band ? view.band.leftPct : 0
  plot.append(
    h(
      'span',
      {
        class: 'tl-att',
        style: `left:${bandLeft}%`,
        'data-tip': view.bandTip || view.title || '',
        'data-key': `att:${key}`,
      },
      // Название этапа — у каждой своей попытки: в гуттере слева живёт только
      // профиль (Макс, 18.11). Подсказка имени — человеческое описание этапа из
      // шейпера: имя из спеки, исполнитель, команда shell-этапа, `id` для журнала.
      view.tag ? h('span', { class: 'tl-lane-tag mono', title: hint }, view.tag) : null,
      // Номер попытки — тем же стилем, что имя этапа: `reviewer #1` читается как
      // одно название, а не как имя с мелким хвостом (Макс, 18.11).
      h('span', { class: 'tl-n' }, view.label.number),
      // Исход попытки — то, что меняется от тика к тику: подсвечивается он, а не
      // вся дорожка (полоса растёт непрерывно, вспышка на ней была бы миганием).
      h('span', { class: `tl-st t-${view.label.tone} fresh`, 'data-fresh': key }, view.label.status),
    ),
  )
  // Тишина: зазор, где не было ни вызова, ни слова. Полоса попытки остаётся на
  // месте — тишина рисуется поверх неё, иначе «шло» и «молчало» слились бы вновь.
  for (const silence of view.silences || []) {
    plot.append(
      h('span', {
        class: 'tl-silence',
        style: `left:${silence.leftPct}%;width:${silence.widthPct}%`,
        'data-tip': silence.tip,
        'data-key': `silence:${view.chips.length}-${silence.leftPct < 50 ? 'l' : 'r'}`,
      }),
    )
  }
  // Пауза «вопрос → ответ» — подсветка времени, которое дело стояло на человеке.
  for (const wait of view.waits) {
    plot.append(
      h('span', {
        class: 'tl-wait',
        style: `left:${wait.fromPct}%;width:${wait.widthPct}%`,
        'data-tip': wait.title,
        'data-key': `wait:${wait.title}`,
      }),
    )
  }
  // Чип — один вызов. Свёрнутая лента кладёт их одной линией с осознанным
  // наложением (CSS), развёрнутая — по уровням (Макс, 14.11: наложение микроколов
  // в свёрнутом виде было намеренным).
  for (const chip of view.chips) {
    plot.append(
      h('span', {
        class: `tl-call${chip.unfinished ? ' tl-unfinished' : ''}`,
        'data-level': String(chip.level || 0),
        'data-tone': chip.tone,
        'data-key': `call:${chip.id}`,
        style: chip.unfinished
          ? `left:${chip.leftPct}%;top:${chip.top}px;--w-log:${chip.widthPct}%;--w-lin:${chip.widthLinPct ?? chip.widthPct}%;width:var(--w-lin);color:${toneVar(chip.tone)}`
          : `left:${chip.leftPct}%;top:${chip.top}px;--w-log:${chip.widthPct}%;--w-lin:${chip.widthLinPct ?? chip.widthPct}%;width:var(--w-lin);background:${toneVar(chip.tone)}`,
        'data-tip': chip.tip,
        role: 'button',
        tabindex: '-1',
        'aria-label': chip.tip,
        'data-call-id': chip.id,
      }),
    )
  }
  // Сказанное агентом: точки на верхней кромке строки. Вопрос, на который они
  // отвечают, — «он думал или просто ничего не приходило», поэтому точка это
  // факт телеметрии, а не украшение.
  for (const say of view.sayTicks || []) {
    plot.append(
      h('span', {
        class: 'tl-say',
        style: `left:${say.leftPct}%`,
        'data-tip': say.tip,
        'data-key': `say:${say.id}`,
      }),
    )
  }
  if (view.marks.length) {
    const marks = h('div', { class: 'tl-marks', 'data-key': 'marks' })
    for (const mark of view.marks) {
      // Группа — столбик на одном `pct`: палка на участника, своим тоном, все
      // стоят на времени события. Сдвиг по секундам соврал бы про журнал.
      const bars =
        mark.grouped && mark.members.length
          ? mark.members.map((member, i) => h('i', { class: `t-${member.tone}`, 'data-key': `member:${i}` }))
          : [h('i')]
      marks.append(
        h(
          'span',
          {
            // Обмен с ролью — стрелка в сторону адресата, а не палка: у вопроса
            // роли есть направление, у вопроса человеку его нет (Макс, 14.11).
            class: `tl-mark t-${mark.tone} anchor-${mark.anchor || 'middle'}${mark.grouped ? ' is-grouped' : ''}${mark.arrow ? ` is-arrow dir-${mark.arrow}` : ''}`,
            style: `left:${mark.pct}%`,
            'data-tip': mark.title,
            'aria-label': mark.ariaLabel,
            'data-key': `mark:${mark.type}:${mark.pct}`,
            // Адрес обмена: по нему провод идёт до дорожки адресата, а по номеру
            // момента поверхность находит пару «вопрос → ответ».
            ...(mark.role
              ? {
                  'data-ask-role': mark.role,
                  'data-ask-what': mark.type,
                  ...(mark.spanIndex === null || mark.spanIndex === undefined
                    ? {}
                    : { 'data-ask-span': String(mark.spanIndex) }),
                }
              : {}),
          },
          ...bars,
        ),
      )
    }
    plot.append(marks)
  }
  return h(
    'div',
    // Свёрнутая высота показывает первый уровень вызовов и маркеры, полная
    // раскрывается под указателем. Числа — из шейпера, CSS только переключает.
    {
      class: `tl-row${view.failed ? ' is-failed' : ''}${view.shell ? ' is-shell' : ''}`,
      style: `--tl-collapsed:${view.collapsed}px;--tl-full:${view.full}px`,
      'data-key': key,
    },
    track,
  )
}

/** Дорожка лида: одна строка над этапами, полоса на каждый заход. Лид не резидент:
 *  cron поднимает его раз в минуту, и на шкале видно ровно это — где он работал, чем
 *  кончился заход, идёт ли он сейчас. Отдельная дорожка, а не попытка этапа: у лида
 *  нет исхода этапа, и `attemptRowNode` выдумал бы ему статус. */
function leadLaneNode(leadLane, scale) {
  const runs = leadLane.runs || []
  // Лид — такая же зона, как профили этапов: рельс с именем по центру зоны, а не
  // шапка строкой сверху. Иначе единственная подпись «лид» висела не по центру
  // своей полосы (Макс, 18.11).
  return h(
    'div',
    { class: 'tl-group-block is-lead', 'data-key': 'block:lead', 'data-group': 'lead', 'data-role': 'lead' },
    h(
      'div',
      { class: 'tl-group-rail', title: 'лид: процесс, который ведёт дело' },
      // Лид — управляющий: свой значок, а не «ещё один агент» (Макс, 18.11).
      h(
        'svg',
        { class: 'tl-group-ico', viewBox: '0 0 16 16', 'aria-hidden': 'true' },
        h('path', { d: KIND_ICON.lead }),
      ),
      h('span', { class: 'tl-group-name' }, 'лид'),
    ),
    h(
      'div',
      { class: 'tl-group-body' },
      h(
        'div',
        { class: 'tl-lane is-lead', 'data-key': 'lane:lead', 'data-lane-role': 'lead' },
        runs.length
          ? h(
              'div',
              { class: 'tl-rows' },
              h(
                'div',
                // Высота захода — 26 px в инлайновых переменных: у строки попытки
                // высота равна `--tl-full` на наведении, и правило из CSS ниже по
                // файлу возвращало бы рейку к 20 px под указателем.
                { class: 'tl-row', style: '--tl-collapsed:26px;--tl-full:26px' },
                h(
                  'div',
                  { class: 'tl-track', 'data-tip': `${leadLane.text}: полосы на оси — заходы лида, от подъёма до выхода` },
                  ...runs.map((run, index) =>
                    h(
                      'span',
                      {
                        class: `tl-lead-run t-${run.tone}${run.open ? ' is-open' : ''}${run.stale ? ' is-stale' : ''}`,
                        'data-key': `lead-run:${index}`,
                        style: `left:${run.startPct}%;width:${run.widthPct}%`,
                        'data-tip': run.title,
                        'aria-label': run.title,
                        role: 'img',
                      },
                      // Рейка + засечки + слово режима: разметка «лид держал дело»,
                      // а не вторая полоса попытки (см. styles.css).
                      h('span', { class: 'tl-lead-rail' }),
                      h('span', { class: 'tl-lead-cap is-start' }),
                      h('span', { class: 'tl-lead-cap is-end' }),
                      run.labeled ? h('span', { class: 'tl-lead-label' }, run.label) : null,
                      run.open ? h('span', { class: 'tl-lead-head' }) : null,
                    ),
                  ),
                ),
              ),
            )
          : h('div', { class: 'tl-row-empty' }, h('span', { class: 'tl-row-empty-t' }, 'заходов не было')),
      ),
    ),
  )
}

/**
 * Блок группы дорожек: рельс профиля слева, попытки справа.
 *
 * Дело держит 5–10 этапов и 3–4 роли. Группа читается тремя якорями: рельс с
 * профилем и счётчиками (слева, по всей высоте зоны), подложка во всю ширину и
 * волосяная линия на границе зоны. Счётчики переехали в левую колонку, потому что
 * в полотне они стояли над полосами и мешали читать шкалу (Макс, 18.11:
 * «количество этапов/попыток на роль переместить в колонку левую»).
 */
function laneGroupBlock(group, lanes, scale) {
  return h(
    'div',
    {
      class: `tl-group-block${group.role ? '' : ' is-engine'}`,
      'data-key': `block:${group.key}`,
      'data-group': group.key,
      // Цвет зоны выбирается по роли: палитра та же, что у полос на главной.
      'data-role': group.roleKey || 'other',
    },
    laneGroupRail(group),
    h(
      'div',
      { class: 'tl-group-body' },
      ...lanes.map(([lane, bars]) => laneNode(lane, bars, scale)),
    ),
  )
}

/** Рельс профиля: значок роли, её имя и счёт этапов и попыток. Липнет к кромке. */
function laneGroupRail(group) {
  return h(
    'div',
    { class: 'tl-group-rail', 'data-tip': group.hint },
    h(
      'svg',
      { class: 'tl-group-ico', viewBox: '0 0 16 16', 'aria-hidden': 'true' },
      h('path', { d: group.icon || '' }),
    ),
    h('span', { class: 'tl-group-name' }, group.label),
    // Этапы и попытки — одной строкой: так табличка роли складывается в три строки
    // и влезает даже в зону из одного этапа (Макс, 18.11).
    h('span', { class: 'tl-group-meta' }, group.summary),
  )
}

/** Дорожка этапа: строки попыток; имени и профиля в гуттере нет.
 *
 *  Слева остаётся только профиль — заголовок группы ролей (`laneGroupNode`), а
 *  название этапа печатается у своей попытки внутри шкалы (Макс, 18.11: «убрать
 *  подпись профиля у каждого этапа, название этапа перенести к попытке, слева
 *  оставить только профиль»). Так колонка подписей сузилась до имени роли, а шкала
 *  получила освободившиеся пиксели. */
function laneNode(lane, bars, scale) {
  const view = laneView(lane, bars, scale)
  return h(
    'div',
    // Роль дорожки — адрес провода вопроса: по ней поверхность находит, куда
    // вести линию от момента вопроса (`drawAskWires`).
    { class: 'tl-lane', 'data-key': `lane:${lane.key}`, 'data-lane-role': lane.role || '' },
    view.empty
      ? h('div', { class: 'tl-row-empty' }, h('span', { class: 'tl-row-empty-t' }, 'попыток не было'))
      : h('div', { class: 'tl-rows' }, ...view.rows.map((row) => attemptRowNode(row.view, `attempt:${row.key}`, view.hint))),
  )
}

// Подсветка семейства инструментов: набор тонов, выбранных в легенде. Клик по
// семейству гасит всё остальное, повторный — снимает подсветку. Состояние
// принадлежит экрану (как выбор вызова), а цвета и подписи даёт шейпер.
const dutyToolFilter = new Set()

// Применить фильтр к уже нарисованной панели: чипы чужих семейств гаснут,
// легенда показывает, какие семейства выбраны.
function applyToolFilter(panel) {
  const active = dutyToolFilter.size > 0
  for (const key of panel.querySelectorAll('.scale-key')) {
    const on = dutyToolFilter.has(key.getAttribute('data-tone'))
    key.classList.toggle('on', on)
    key.setAttribute('aria-pressed', on ? 'true' : 'false')
  }
  for (const chip of panel.querySelectorAll('.tl-call')) {
    const dim = active && !dutyToolFilter.has(chip.getAttribute('data-tone'))
    chip.classList.toggle('tl-dim', dim)
    // Погашенный чип не должен ловить клик и подсказку: он «не участвует».
    if (dim) chip.setAttribute('aria-hidden', 'true')
    else chip.removeAttribute('aria-hidden')
  }
}

/** KPI-строка дела: пять чисел шапки, все — из шейпера (`scale.kpi`). */
function kpiStrip(scale) {
  return h(
    'div',
    { class: 'duty-kpi', 'data-key': 'kpi' },
    ...scale.kpi.map((item) =>
      h(
        'div',
        { class: 'duty-kpi-cell', title: item.title || '', 'data-key': `kpi:${item.key}` },
        h('span', { class: 'duty-kpi-v fresh', 'data-fresh': `kpi:${item.key}` }, item.value),
        h('span', { class: 'duty-kpi-l' }, item.label),
        item.note ? h('span', { class: 'duty-kpi-n' }, item.note) : null,
      ),
    ),
  )
}

/**
 * Граф спеки компактной полосой: узлы и рёбра `needs` со стрелками.
 *
 * Это схема зависимостей, а не главный объект экрана. Координаты, кривую ребра,
 * острие стрелки и признаки узла (тон, `live`) считает шейпер (`scale.spec`),
 * поверхность только перекладывает вид в SVG. Подпись «граф спеки» стоит под
 * картинкой: сбоку она спорила с узлами.
 */
function specStrip(spec) {
  if (!spec) return null
  return h(
    'div',
    { class: 'spec-strip' },
    h(
      'svg',
      {
        class: 'spec-graph',
        viewBox: `0 0 ${spec.width} ${spec.height}`,
        width: spec.width,
        height: spec.height,
        role: 'img',
        'aria-label': `граф этапов: ${spec.nodes.map((node) => node.id).join(' → ')}`,
      },
      ...spec.edges.map((edge) => [
        h('path', { class: 'spec-edge', d: edge.d }),
        h('polygon', { class: 'spec-arrow', points: edge.arrow.points }),
      ]),
      ...spec.nodes.map((node) =>
        h(
          'g',
          { class: `spec-node t-${node.tone}${node.live ? ' is-live' : ''}` },
          h('title', null, node.title),
          h('rect', { x: node.x, y: node.y, width: spec.nodeW, height: spec.nodeH, rx: 6 }),
          h('text', { x: node.x + spec.nodeW / 2, y: node.y + 12, class: 'spec-id', 'text-anchor': 'middle' }, node.id),
          h('text', { x: node.x + spec.nodeW / 2, y: node.y + 23, class: 'spec-state', 'text-anchor': 'middle' }, node.label),
        ),
      ),
    ),
    h('span', { class: 'spec-strip-name' }, 'граф спеки'),
  )
}

/**
 * Панель шкалы времени: подписанная ось, дорожка на этап, полосы попыток и лента
 * маркеров. Геометрия приходит посчитанной (`shapeDealTime`); здесь только DOM.
 */
function scalePanel(scale) {
  if (scale.empty) {
    return h(
      'div',
      { class: 'panel', style: 'margin-bottom:14px', 'data-key': 'scale' },
      h('div', { class: 'panel-h' }, 'шкала времени', refreshedNode()),
      h('div', { class: 'panel-b muted' }, 'событий и попыток нет: рисовать нечего'),
    )
  }
  const panel = h(
    'div',
    { class: 'panel', style: 'margin-bottom:14px', 'data-key': 'scale' },
    // Подпись «обновлено ЧЧ:ММ:СС» — весь индикатор тихого обновления, и он же
    // единственное место, где видно, что страница живая. `aria-live="off"`:
    // диктор не должен читать время каждые пять секунд.
    h(
      'div',
      { class: 'panel-h' },
      `шкала времени · ${scale.axis.text}`,
      scale.axis.zoom > 1 ? h('span', { class: 'scale-scroll-hint', title: `шкала шире часа в ${Math.round(scale.axis.zoom)} экрана — листается по горизонтали, правая кромка свежая` }, '⟷ прокрутка') : null,
      h('span', { class: 'tl-spacer' }),
      refreshedNode(),
    ),
    h(
      'div',
      { class: 'panel-b' },
      // Ось стоит ровно над дорожками: слева пустая колонка шириной подписей,
      // справа — та же область, где рисуются чипы.
      h(
        'div',
        // `is-live` включает переезд геометрии по времени, `--tl-tick` держит его
        // равным периоду опроса: данные приходят раз в 5 с, а ось едет к новой
        // кромке всё это время (Макс, 18.11).
        { class: 'scale-wrap is-live', style: `--tl-tick:${Math.max(1, Number(dealPlan().delaySec) || 5)}s` },
        // Подложка левого столбца: живёт ВНЕ скроллера, поэтому не едет с
        // содержимым и накрывает его целиком — чипы не видны ни в зазорах
        // между строками, ни между шапкой дорожки и строками. Только в режиме
        // прокрутки: подписи поднимаются над слоем липкостью (`is-wide`),
        // в узком виде слой закрыл бы их текст.
        ...(scale.axis.zoom > 1 ? [h('div', { class: 'tl-gutter-bg', 'aria-hidden': 'true' })] : []),
        h(
        'div',
        {
          class: `scale-main${scale.axis.zoom > 1 ? ' is-wide' : ''}`,
          // Ось заморожена на «вьюпорт = час»: дело длиннее — листается, и мы
          // держимся правой свежей кромки (см. pinScaleScroll).
          style: scale.axis.zoom > 1 ? `--tl-zoom:${scale.axis.zoom}` : '',
        },
        h(
        'div',
        { class: 'scale-head' },
        h('div', { class: 'scale-head-gap' }),
        h(
          'div',
          { class: 'scale-axis' },
          // Линейка: подпись у каждой метки, у краёв — словами «нач./кон.».
          ...scale.axis.ticks.map((tick) =>
            h(
              'span',
              { class: `scale-tick align-${tick.align}`, style: `left:${tick.pct}%`, title: tick.clock, 'data-key': `tick:${tick.at ?? tick.clock}` },
              tick.edge ? `${tick.edge} ${tick.clock}` : tick.clock,
            ),
          ),
        ),
      ),
      // Дорожки этапов — таймлайн вызовов: полоса на попытку, раскрывается по
      // наведению. Полосы `scale-bar` остались в шейпере (desktop 0.0.7), но
      // второй рисунок того же дела на экране не нужен.
      // Одна нулевая линия на все дорожки: часы читаются по сетке, а не
      // угадываются по трём подписям на оси (как в макете `d-merge`).
      h(
        'div',
        { class: 'scale-plot' },
        h(
          'div',
          { class: 'scale-grid', 'aria-hidden': 'true' },
          ...scale.axis.ticks.map((tick) =>
            h('span', { class: 'scale-grid-line', style: `left:${tick.pct}%`, 'data-key': `grid:${tick.at ?? tick.clock}` }),
          ),
        ),
        // Сжатые промежутки оси: штрихованная полоса и пилюля со временем. Ось
        // сжимает пустое время — это должно читаться как «здесь шкала рвётся», а
        // не как дыра в данных (`idle.gaps` считает шейпер, здесь только DOM).
        scale.idle.gaps.length
          ? h(
              'div',
              { class: 'tl-idle-layer', 'aria-hidden': 'true' },
              ...scale.idle.gaps.map((gap) =>
                h(
                  'span',
                  {
                    class: `tl-idle-gap is-${gap.kind}`,
                    'data-key': `idle:${gap.from}`,
                    style: `left:${gap.leftPct}%;width:${Math.max(0.6, gap.rightPct - gap.leftPct)}%`,
                    title: gap.title,
                  },
                  h('span', { class: 'tl-idle-band' }),
                  gap.labeled ? h('span', { class: 'tl-idle-pill' }, `≈ ${gap.short}`) : null,
                ),
              ),
            )
          : null,
        h(
        'div',
        { class: 'tl-lanes' },
        // Заходы лида — над этапами: «кто ведёт дело» отвечает первым, а не
        // выясняется подписью в шапке.
        leadLaneNode(scale.leadLane, scale),
        // Дорожки идут группами по роли исполнителя, и группа — это блок: подложка
        // во всю ширину, волосяная линия слева по всей высоте и счётчики в шапке.
        // Плоский список остаётся источником индексов (`bar.lane`), блоки — только
        // порядок и обвязка (Макс, 18.11: «более явно группировать профили»).
        ...(scale.laneGroups || []).map((group) =>
          laneGroupBlock(
            group,
            group.lanes.map((index) => [scale.lanes[index], scale.bars.filter((bar) => bar.lane === index)]),
            scale,
          ),
        ),
        ),
      ),
        ),
        // Легенда семейств — правой колонкой: числа вызовов из шейпера
        // (`scale.tools`), а не пять слов из константы.
        h(
          'div',
          { class: 'scale-side', 'data-key': 'tools' },
          h('span', { class: 'scale-keys-h' }, 'инструменты'),
          ...scale.tools.map((item) =>
            h(
              'span',
              {
                class: 'scale-key',
                'data-tone': item.tone,
                'data-key': `tool:${item.tone}`,
                role: 'button',
                tabindex: '0',
                'aria-pressed': 'false',
                'aria-label': `${item.label}: ${item.count}. Клик — подсветить вызовы этого семейства на дорожках`,
              },
              h('span', { class: `scale-key-dot t-${item.tone}` }),
              h('span', { class: 'scale-key-l' }, item.label),
              h('span', { class: 'scale-key-n fresh', 'data-fresh': `tool:${item.tone}` }, String(item.count)),
            ),
          ),
        ),
      ),
      // Легенда маркеров: что означают точки внутри строк попыток.
      markerLegend(scale),
      scale.facts.attempts.journal ? h('div', { class: 'muted' }, 'полосы собраны из журнала: ответ попыток пуст') : null,
    ),
  )
  // Фильтр, выбранный в легенде, переживает перерисовку данных: экран
  // пересобирается по новому кадру, а подсветка остаётся там, где её оставили.
  wireScalePanel(panel)
    // «Висим на правой границе»: пока человек не уехал влево, каждый тик держит
  // в кадре свежую кромку. Флаг живёт на элементе — морф его не теряет.
  // Микрозадача, а не setTimeout: панель монтируется в этом же синхронном
  // проходе, а мериеть и пинить можно только по приделке к документу; таймер
  // здесь ломал бы петлю опроса — тесты ждут тик последним запланированным
  // таймером. rAF не годится: фоновая вкладка не рисует кадры.
  const dealKey = String(route.id || '')
  queueMicrotask(() => {
    fitScaleGutter(panel, dealKey)
    fitAttemptLabels(panel)
    drawAskWires(panel, scale)
    if (scale.axis.zoom > 1) pinScaleScroll(panel)
  })
  // Повторный замер после кадров: подгрузившийся шрифт и дорисованные в морфе
  // дорожки шире, чем микрозадача успела померять. Заодно это пин к правой
  // кромке после F5: браузер восстанавливает прежний scrollLeft уже после
  // нашего пина (Макс, 09.11).
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        fitScaleGutter(panel, dealKey)
        fitAttemptLabels(panel)
        drawAskWires(panel, scale)
        if (scale.axis.zoom > 1) pinScaleScroll(panel)
      }),
    )
  }
return panel
}

// Панели, которые уже слушают клики. Тихий патч правит узлы на месте, поэтому
// панель — тот же элемент, что и до тика, и второй набор обработчиков на ней
// doubling'ом ловил бы каждый клик дважды.
const wiredPanels = new WeakSet()

/**
 * Обработчики панели шкалы: легенда семейств и выбор вызова.
 *
 * Ставятся один раз на элемент, а не на каждую отрисовку: клики делегированы
 * панели, поэтому заменённые внутри неё чипы продолжают работать. Состояние
 * (фильтр, выбранный вызов) живёт на экране, а не в узлах.
 */
function wireScalePanel(panel) {
  if (wiredPanels.has(panel)) return
  wiredPanels.add(panel)
  applyToolFilter(panel)

  // Легенда семейств — переключатели: клик подсвечивает семейство на дорожках,
  // повторный снимает. Клавиатура ходит теми же шагами (Enter/Space).
  const toggleTool = (key) => {
    const tone = key.getAttribute('data-tone') || ''
    if (!tone) return
    if (dutyToolFilter.has(tone)) dutyToolFilter.delete(tone)
    else dutyToolFilter.add(tone)
    applyToolFilter(panel)
  }
  panel.addEventListener('click', (event) => {
    const key = event.target.closest && event.target.closest('.scale-key')
    if (!key) return
    event.preventDefault()
    toggleTool(key)
  })
  panel.addEventListener('keydown', (event) => {
    const key = event.target.closest && event.target.closest('.scale-key')
    if (!key || (event.key !== 'Enter' && event.key !== ' ')) return
    event.preventDefault()
    toggleTool(key)
  })

}

/**
 * Строка отказа плана под графом дела.
 *
 * Живое дело d_12d5fc44: план отклоняли дважды (04:09:26 и 04:10:52), а экран
 * всё это время говорил «coordinator: running» — без плана, без причины, без
 * счёта кругов. Причина — первая строка отказа, остальные считаются в `(+N)`,
 * полный список раскрывается: `details`/`summary` живут без обработчика, и
 * тихий тик, перерисовывая голову, не сбрасывает раскрытие.
 *
 * После принятого плана строки нет вовсе (`shapePlan.refused`): счётчики кругов
 * и отказов остаются в подсказке истории решений (`plan.hint`).
 */
function planLine(plan) {
  return h(
    'details',
    { class: `plan-refusal t-${plan.tone}`, 'data-key': 'fact:plan', title: plan.hint },
    h(
      'summary',
      null,
      plan.text,
      plan.note ? h('span', { class: 'plan-note' }, ` · ${plan.note}`) : null,
    ),
    plan.issues.length
      ? h('ul', { class: 'plan-issues' }, ...plan.issues.map((item) => h('li', null, item)))
      : null,
    plan.moreText ? h('div', { class: 'plan-more muted' }, plan.moreText) : null,
  )
}

function viewDeal(dealId) {
  if (dealSnap.error && !dealSnap.data) {
    return h(
      'div',
      null,
      errorBanner(dealSnap.error, () => loadDeal(dealId)),
      h('a', { href: '#/deal' }, '← к списку дел'),
    )
  }
  if (!dealSnap.data) {
    return h('div', { class: 'grp' }, h('div', { class: 'panel-b' }, h('div', { class: 'skel', style: 'width:60%' }), h('div', { class: 'skel', style: 'width:90%' })))
  }

  const data = dealSnap.data
  // `deal` в ответе — сырые факты (attempt/lease/last_action/waiting); подписи и
  // «pid жив» собирает общий шейпер, чтобы экран «Дело» и таблица «Сейчас»
  // говорили об одном и том же одними словами.
  const deal = data.deal ? shapeDeal(data.deal) : null
  // Шкала времени: ось, дорожки этапов, полосы и маркеры — из одного шейпера
  // (`shapeDealTime`), того же, что вшит в десктопный плагин.
  const scale = shapeDealTime(data, dealSnap.attempts)

  // Заголовок: слева repo/id, имя дела и граф этапов (список того, что прошло,
  // и того, где падало), справа — статус. Статус раньше стоял в левой колонке и
  // отбирал место у графа, а сам граф висел отдельной панелью внизу.
  const head = h(
    'div',
    { class: 'deal-head', 'data-key': 'head' },
    h(
      'div',
      { class: 'deal-head-top' },
      h(
        'div',
        { class: 'deal-head-main' },
        h('div', { class: 'mono muted', title: repoLabel(data.repo) }, `${repoShort(data.repo)} · ${data.id}`),
        // Значок дела рядом с именем: та же геометрия, что у видов этапов
        // (шейпер, `DEAL_ICON`), — «это дело», а не вид этапа.
        h(
          'div',
          { class: 'deal-title-row' },
          h('svg', { class: 'deal-ico', viewBox: '0 0 16 16', 'aria-hidden': 'true' }, h('path', { d: DEAL_ICON })),
          h('h1', null, data.title || '—'),
        ),
        specStrip(scale.spec),
        // Отказ плана — строкой под графом: граф говорит «этапы такие», отказ
        // говорит «почему их до сих пор нет». Строка уходит после приёмки плана.
        deal && deal.plan && deal.plan.refused ? planLine(deal.plan) : null,
      ),
      h(
        'div',
        { class: 'deal-head-side', 'data-key': 'status' },
        // Закрытое дело: статус словами (`завершено`/`в архиве`), а не значением store.
        deal ? Badge(deal.state, deal.humanKind) : h('span', { class: 'badge t-gray' }, h('i'), finishedStatusMeta(data.status).label),
      ),
    ),
    h(
      'div',
      { class: 'deal-facts' },
      // Статус уже стоит бейджем справа, исполнитель у дела без рана не записан:
      // «исполнитель: —» — это пустое поле, а не факт.
      data.assignee ? h('span', null, 'исполнитель: ', h('b', null, data.assignee)) : null,
      // Состояние лида — факт журнала (`leadfeed.status`), рядом с попыткой и
      // лизингом: «кто ведёт дело» отвечается тем же словом, что в терминале.
      deal && deal.lead
        ? h(
            'span',
            { 'data-key': 'fact:lead', title: deal.lead.evidence || 'состояние лида по журналу' },
            'лид: ',
            h('b', { class: `t-${deal.lead.tone}` }, deal.lead.short),
            ' · ',
            h('span', { class: 'muted' }, deal.lead.resumesText),
          )
        : null,
      deal ? h('span', { 'data-key': 'fact:attempt' }, 'попытка: ', h('b', null, `${deal.attemptAgeText} (${deal.attemptNote})`)) : null,
      deal ? h('span', { 'data-key': 'fact:lease' }, 'лизинг: ', h('b', null, `${deal.leaseText}`)) : null,
      h('span', null, 'создано: ', h('b', null, fmtClock(data.created_at))),
      deal ? h('span', { 'data-key': 'fact:action' }, 'последнее действие: ', h('b', null, `${deal.lastActionAgeText} назад`)) : null,
    ),
    // Факты шкалы: попытки по этапам, длительность, вопросы, бюджет, публикация.
    h('div', { class: 'deal-facts', 'data-key': 'facts' }, ...dealFactSpans(scale)),
    // KPI — низ той же карточки, а не отдельная полоса под шапкой: числа дела
    // принадлежат делу, и волосок внутри шапки говорит это без второй рамки.
    h('div', { class: 'deal-head-kpi', 'data-key': 'kpi-strip' }, kpiStrip(scale)),
    deal && deal.evidence ? h('div', { class: 'why-evidence', style: 'margin-top:10px', text: deal.evidence }) : null,
    // Отказы опроса молчат, пока их меньше трёх: одиночный таймаут — не повод
    // махать баннером. Три подряд — это уже сломанное обновление, и о нём честно.
    dealSnap.refreshError ? errorBanner(`обновление не удалось: ${dealSnap.refreshError}`, () => loadDeal(dealId)) : null,
  )

  // Документы дела: тело из store и спека — разные вещи, и рисовать вместо одной
  // другую нельзя (у дел из `bin/duty create` описания в store нет вовсе).
  // Нет документов — нет колонки: описание забирает ширину, а столбец этапов
  // остаётся один и на всю ширину.
  const docs = docBlocks(data)
  const left = docs.length
    ? h(
        'div',
        { class: 'panel', 'data-key': 'docs' },
        ...docs.map((doc) =>
          h(
            'div',
            { class: 'panel-doc' },
            h(
              'div',
              { class: 'panel-h', title: doc.path || '' },
              doc.name ? `${doc.label} · ${doc.name}` : doc.label,
              doc.truncated ? ' (обрезана лимитом)' : '',
            ),
            doc.kind === 'spec'
              ? h('pre', { class: 'panel-b doc-src mono' }, doc.text)
              : h('div', { class: 'panel-b' }, mdNode(doc.text)),
          ),
        ),
      )
    : null

  // Правый рельс дела: решения лида лентой на оси времени, под ними этапы и
  // попытки. Оба списка — про одно и то же дело и оба читаются сверху вниз,
  // поэтому стоят рядом, а не двумя полосами во всю ширину.
  const decisions = decisionsPanel(data.decisions, deal && deal.plan, data.id)
  const stages = stagesColumn(data.id)
  const rail = h('div', { class: 'deal-rail' }, decisions, stages)

  return h(
    'div',
    { 'data-key': 'deal' },
    h('div', { style: 'margin-bottom:10px' }, h('a', { href: '#/deal' }, '← к списку дел')),
    head,
    scalePanel(scale),
    // Документов нет — нет и левой колонки: рельс забирает ширину, а не стоит
    // половиной пустой страницы.
    left ? h('div', { class: 'deal-cols' }, left, rail) : rail,
  )
}

/* ------------------------------------------ тихое обновление дела (0.0.8) */

/** Дело открыто прямо сейчас: маршрут тот же и это не отказ по старому адресу. */
const dealOn = (dealId) => route.name === 'deal' && route.bad !== true && String(route.id || '') === String(dealId || '')

/** Подпись «обновлено ЧЧ:ММ:СС» в шапке панели — весь индикатор тихого тика. */
function refreshedNode() {
  const at = dealSnap.refreshedAt
  return h('span', { class: 'refreshed', 'aria-live': 'off', 'data-key': 'refreshed' }, at ? `обновлено ${fmtClock(at)}` : '')
}

/**
 * Плоский снимок вида «ключ → значение» для сверки двух тиков.
 *
 * Ключи те же, что стоят в разметке (`data-fresh`), поэтому подсветить
 * изменившееся — это найти узел по ключу, а не сравнивать деревья целиком.
 */
function dealSpots(detail, attempts) {
  const d = detail && typeof detail === 'object' ? detail : {}
  const scale = shapeDealTime(d, attempts)
  const feed = shapeStages(d)
  const spots = []
  for (const bar of scale.bars) {
    spots.push({
      key: `attempt:${bar.laneKey || ''}-${bar.number || ''}`,
      state: bar.outcomeText,
      outcome: bar.outcome,
      durationText: bar.durationText,
      pct: bar.endPct,
    })
  }
  for (const lane of scale.lanes) spots.push({ key: `lane:${lane.key}`, value: String(lane.count) })
  for (const item of scale.kpi) spots.push({ key: `kpi:${item.key}`, value: item.value })
  for (const item of scale.tools) spots.push({ key: `tool:${item.tone}`, value: String(item.count) })
  // Столбец этапов: те же значения, что напечатаны в `stage-row`/`att-row`.
  for (const stage of feed.stages || []) {
    const view = compactStageView(stage)
    const stageKey = `${d.id}:${view.id}`
    spots.push({ key: `stage:${stageKey}`, value: view.attemptsText, state: view.stateLabel })
    stageColumnBars(stage, attempts).forEach((bar, index) => {
      const barView = compactAttemptView(stage, bar)
      spots.push({ key: `att:${stageKey}#${bar.number || index}`, value: barView.durationText, outcome: barView.outcomeText })
    })
  }
  return spots
}

/** Системная настройка «меньше движения»: подсветка при ней не навешивается. */
function prefersReducedMotion() {
  return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
}

// Таймеры снятия подсветки: повторный тик по тому же узлу не должен гасить
// подсветку раньше срока, который поставил предыдущий.
const freshTimers = new WeakMap()

/** Коротко подсветить значения, которые изменились между тиками. */
function markFresh(keys) {
  if (!keys.length || prefersReducedMotion()) return
  const wanted = new Set(keys)
  for (const el of app.querySelectorAll('[data-fresh]')) {
    if (!wanted.has(el.getAttribute('data-fresh'))) continue
    el.classList.add('is-fresh')
    const prev = freshTimers.get(el)
    if (prev) clearTimeout(prev)
    freshTimers.set(el, setTimeout(() => el.classList.remove('is-fresh'), FRESH_HIGHLIGHT_MS))
  }
}

// Фильтр легенды — состояние экрана, а не ответа: после патча узлы приходят
// «чистыми», и подсветку надо вернуть, иначе она гаснет на каждом тике.
function syncScaleState() {
  applyToolFilter(app)
}

/**
 * Обновить страницу дела по свежему ответу: правка на месте, без перерисовки.
 *
 * Полная сборка `viewDeal` нужна и здесь — она даёт новое дерево; но в DOM
 * остаётся старое, и `morph` переносит в него только разницу. Поэтому раскрытые
 * попытки, прокрутка и фильтр переживают тик.
 */
function patchDeal(dealId) {
  const host = app.firstElementChild
  // Страницы ещё нет (первый заход) — рисуем целиком, патчить нечего.
  if (!host || !host.hasAttribute('data-key')) {
    render()
    return
  }
  const prev = dealSnap.spots
  const next = dealSpots(dealSnap.data, dealSnap.attempts)
  const before = flipBefore(host)
  morph(host, viewDeal(dealId))
  flipPlay(app, before)
  // Хост подсказки мог исчезнуть вместе со старой строкой: тогда подсказку снимаем.
  if (typeof window.dutyTipFollow === 'function') window.dutyTipFollow()
  const panel = app.querySelector('[data-key="scale"]')
  if (panel) wireScalePanel(panel)
  syncScaleState()
  markFresh(freshKeys(prev, next))
  dealSnap.spots = next
}

// Петля опроса: следующий тик ставится после завершения предыдущего
// (`setTimeout`, а не `setInterval`) — иначе медленный ответ накладывал бы тики.
const dealPoll = { timer: null, failures: 0 }

function dealPollStop() {
  if (dealPoll.timer !== null) clearTimeout(dealPoll.timer)
  dealPoll.timer = null
}

/** Человек правит текст на странице — тик уступает: набранное не отбираем. */
function editingNow() {
  const active = document.activeElement
  if (!active || active === document.body) return false
  const tag = String(active.tagName || '').toUpperCase()
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  return active.isContentEditable === true
}

/** Когда опрашивать — по правилам шейпера, а не по своему счёту секунд. */
function dealPlan() {
  return refreshPlan({
    terminal: Boolean(dealSnap.data && dealSnap.data.terminal === true),
    hidden: document.hidden === true,
    failures: dealPoll.failures,
    focused: editingNow(),
  })
}

/**
 * Поставить следующий тик.
 *
 * Скрытая вкладка не опрашивает вовсе — таймера нет, а вернувшаяся вкладка
 * получает один немедленный тик из `visibilitychange`. Пропущенный из-за фокуса
 * тик ставится снова через период: после ухода фокуса обновление обычное.
 */
function dealPollLater(dealId) {
  dealPollStop()
  if (document.hidden === true) return
  const plan = dealPlan()
  dealPoll.timer = setTimeout(() => void dealPollTick(dealId), plan.delaySec * 1000)
}

async function dealPollTick(dealId) {
  dealPoll.timer = null
  // Маршрут сменился или дело закрыли — тик не наш: петля умирает молча.
  if (!dealOn(dealId)) {
    dealPoll.failures = 0
    return
  }
  if (dealSnap.loading || dealPlan().skip) {
    dealPollLater(dealId)
    return
  }
  try {
    const [detail, attempts] = await Promise.all([
      api(`/deal/${encodeURIComponent(dealId)}`),
      api(`/duty/${encodeURIComponent(dealId)}/attempts`),
    ])
    if (!dealOn(dealId)) return
    dealSnap.data = detail
    dealSnap.attempts = attempts
    dealSnap.error = null
    dealSnap.refreshError = ''
    dealPoll.failures = 0
    dealSnap.refreshedAt = Math.floor(Date.now() / 1000)
    // Шапка страницы считает возраст снимка — тик её тоже двигает, иначе она
    // врала бы «обновлено 5 минут назад» под свежей панелью.
    setStamp(Date.now())
    patchDeal(dealId)
  } catch (err) {
    if (!dealOn(dealId)) return
    dealPoll.failures += 1
    // Молчим, пока отказов меньше трёх: одиночный таймаут — не сломанное дело.
    // Три подряд — уже сломанное обновление, и о нём говорится словами.
    if (dealPoll.failures >= REFRESH_FAIL_SILENT) {
      dealSnap.refreshError = err.message || 'сеть'
      patchDeal(dealId)
    }
  }
  dealPollLater(dealId)
}

/** Вернувшаяся вкладка: один немедленный тик и дальше по расписанию. */
document.addEventListener('visibilitychange', () => {
  if (document.hidden === true) {
    dealPollStop()
    // Ушли из вкладки — общий таймер тоже снимается: скрытая страница не
    // опрашивает сервер (deal-петля выходит сама через `refreshPlan`).
    clearTimeout(timer)
    timer = null
    return
  }
  if (route.name === 'deal' && route.id && !route.bad) {
    dealPollStop()
    void dealPollTick(route.id)
    return
  }
  void run()
})

/* ---------------------------------------------------------------- маршруты */

function parseRoute() {
  const raw = (location.hash || '#/now').replace(/^#/, '')
  const parts = raw.split('/').filter(Boolean)
  if (parts[0] === 'deal') {
    // Адрес дела — один сегмент: `#/deal/<d_hex>`. Два сегмента — прежний адрес
    // `#/deal/<board>/<id>`; он получает отказ, а не открытую наполовину карточку.
    if (parts.length > 2) return { name: 'deal', bad: true, board: decodeURIComponent(parts[1]), id: decodeURIComponent(parts[2]) }
    if (parts[1]) return { name: 'deal', id: decodeURIComponent(parts[1]) }
    return { name: 'deal' }
  }
  if (parts[0] === 'why') return { name: 'why' }
  if (parts[0] === 'health') return { name: 'health' }
  // «Завершённые» — отдельный экран списка: `#/deals` (0.0.7).
  if (parts[0] === 'deals') return { name: 'deals' }
  return { name: 'now' }
}

/**
 * Фокус переживает перерисовку.
 *
 * `render()` собирает страницу заново, поэтому заменённое поле теряет и фокус, и
 * каретку. Заметно это ровно там, где человек печатает: поиск перерисовывается
 * своим же debounce (240 мс после последнего символа), фокус снимается посреди
 * слова, и каждая следующая буква уходит в никуда — поле выглядит «неактивным»
 * (Макс, 16.11). Поэтому до сборки запоминаем, что набирали, а после — возвращаем
 * ввод в то же поле по его ключу: плейсхолдер и есть то, чем поля отличаются.
 */
function fieldKey(el) {
  return el.getAttribute('data-key') || el.getAttribute('placeholder') || el.id || el.className || el.tagName
}

function keepEditing() {
  const el = document.activeElement
  if (!el || !app.contains(el)) return null
  if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') return null
  const snap = { key: fieldKey(el), value: el.value, start: null, end: null }
  try {
    snap.start = el.selectionStart
    snap.end = el.selectionEnd
  } catch {
    // у поля без выделения каретку не спросить — тогда только фокус
  }
  return snap
}

function resumeEditing(snap) {
  if (!snap) return
  for (const el of app.querySelectorAll('input, textarea')) {
    if (fieldKey(el) !== snap.key) continue
    // Набранное перерисовка не съедает: у поля окна таймлайна значение не
    // приходит из состояния, и без этой строки ввод пропадал вместе с фокусом.
    if (snap.value && el.value !== snap.value) el.value = snap.value
    el.focus()
    if (snap.start !== null && el.setSelectionRange) {
      try {
        el.setSelectionRange(snap.start, snap.end)
      } catch {
        // поле без выделения — фокус уже вернули
      }
    }
    return
  }
}

function render() {
  const editing = keepEditing()
  const body = clear(app)
  if (route.name === 'now') body.append(viewNow())
  else if (route.name === 'why') body.append(viewWhy())
  else if (route.name === 'health') body.append(viewHealth())
  else if (route.bad) body.append(viewDealBadAddress(route.board, route.id))
  else body.append(route.id ? viewDeal(route.id) : viewDealPicker())

  for (const link of document.querySelectorAll('#tabs a')) {
    link.classList.toggle('on', link.dataset.tab === route.name)
  }
  // Счётчик «вопросов + блокировок» — про срез «сейчас»: на других экранах он
  // показывал бы число от прошлого снимка.
  if (humanEl && route.name !== 'now') humanEl.textContent = ''
  resumeEditing(editing)
}

/**
 * Поставить следующий тик экрана.
 *
 * Таймер один на все экраны (`timer`), поэтому перед постановкой прежний
 * снимается: две цепочки опроса одновременно — это двойные запросы и мигание.
 */
function schedule() {
  clearTimeout(timer)
  timer = null
  // Страница дела опрашивает себя сама (`dealPollLater`): её период зависит от
  // состояния дела — идущее обновляем часто, закрытое редко, — а общий таймер
  // экранов знает только «медленно/быстро».
  if (route.name === 'deal' && route.id && !route.bad) {
    dealPollLater(route.id)
    return
  }
  // Скрытая вкладка не опрашивает вовсе: вернувшаяся получает немедленный тик
  // из `visibilitychange` (см. ниже) и дальше идёт по расписанию.
  if (document.hidden === true) return
  // «Здоровье» и «Завершённые» — по журналу за всю историю: они не меняются за
  // секунды, и опрашивать их так же часто, как срез «сейчас», незачем.
  const slow = route.name === 'health'
  timer = setTimeout(() => void run(), slow ? HEALTH_REFRESH_MS : REFRESH_MS)
}

/**
 * Один цикл опроса экрана: запрос, перерисовка, постановка следующего тика.
 *
 * Следующий тик ставится в `finally`, а не в `onRoute`: раньше таймер взводился
 * ровно один раз при смене маршрута, срабатывал — и цепочка обрывалась, так что
 * список дел и «Здоровье» после первого же тика замирали навсегда (Макс, 11.11).
 */
async function run() {
  try {
    if (route.name === 'now') await loadNow()
    else if (route.name === 'why') await loadNow()
    else if (route.name === 'health') await loadHealth()
    else if (route.bad) await loadNow()
    else if (route.id) await loadDeal(route.id)
    else await loadNow()
  } finally {
    schedule()
  }
}

function onRoute() {
  // Ушли со страницы дела — петля опроса останавливается здесь, а не на
  // следующем тике: иначе смена маршрута оставила бы висеть чужой запрос.
  dealPollStop()
  route = parseRoute()
  // `#/deals` — прежний отдельный экран списка: он больше не вкладка, но старый
  // адрес открывает ту же страницу с развёрнутым блоком завершённых.
  if (route.name === 'deals') {
    route = { name: 'now' }
  }
  if (route.name !== 'deal') dealPoll.failures = 0
  // Данные, которых нет для нового экрана, не переиспользуем: иначе «Здоровье»
  // покажет старый снимок «сейчас» под своими KPI, а «Завершённые» — вчерашний
  // список под новым заголовком.
  if (route.name === 'health' && !health.data) {
    stampAt = null
    tickStamp()
  }
  render()
  void run()
}

window.addEventListener('hashchange', onRoute)
document.getElementById('refresh').addEventListener('click', () => run())

// Версия плагина — из /health: одна правда о сборке, вшитая в шапку.
async function loadVersion() {
  try {
    const info = await api('/health')
    if (verEl) verEl.textContent = info.version ? `v${info.version}` : ''
  } catch {
    /* шапка без версии — не повод ломать экран */
  }
}

// Прокрутка широкой шкалы: пока человек не уехал влево сам, каждый тик
// дожимает скролл к правой свежей кромке; уехал — шкала ждёт его, вернулся
// к краю — снова висит на «сейчас».
// Левый столбец шкалы подстраивается под самый широкий контент (Макс, 09.11:
// «ширина столбца уже, чем ширина самого широкого контента»): имя этапа и
// профиль не должны выпирать за 186px по умолчанию. Меряем естественную ширину
// подписей (scrollWidth у усечённых многоточием), результат — в `--tl-gutter`.
// Ширина монотонна, пока открыто то же дело (Макс, 10.11: «столбец сужается
// после первого же обновления»): тик с пустыми попытками или недорисованным
// контентом не имеет права ужимать столбец обратно.
const scaleGutterSeen = { deal: null, px: 0 }
function fitScaleGutter(root, dealKey) {
  const wrap = root && root.querySelector ? root.querySelector('.scale-wrap') : null
  if (!wrap) return
  if (scaleGutterSeen.deal !== dealKey) {
    scaleGutterSeen.deal = dealKey
    scaleGutterSeen.namePx = 0
  }
  // В гуттере осталась одна колонка — имя профиля (заголовок группы ролей). Имена
  // и значки этапов уехали к своим попыткам, подписи попыток стоят у полос
  // (Макс, 18.11). Ширина — по самому широкому имени роли: `scrollWidth` у
  // обрезанного элемента равен его ширине, поэтому меряем детей, а не ячейку.
  let namePx = 0
  for (const name of root.querySelectorAll('.tl-group-name, .tl-group-meta')) {
    namePx = Math.max(namePx, name.scrollWidth || name.getBoundingClientRect().width || 0)
  }
  if (!namePx) return
  scaleGutterSeen.namePx = Math.max(scaleGutterSeen.namePx || 0, namePx)
  const named = Math.min(300, Math.max(88, Math.ceil(scaleGutterSeen.namePx) + 18))
  wrap.style.setProperty('--tl-name', `${named}px`)
}
/**
 * Поле под подпись попытки: подпись стоит СЛЕВА от своей полосы и никогда не
 * наезжает на неё.
 *
 * Мерить надо: имя этапа плюс номер и исход занимают до двухсот пикселей, а у
 * попытки, начавшейся от кромки окна, слева от полосы нет ничего. Раньше подпись
 * в таком случае переносилась внутрь полосы — и наезжала на «сосиску» (Макс,
 * 18.11). Теперь место отдаётся подписи: поле шкалы растягивается по самой
 * широкой подписи, полосы начинаются правее, окно оси при этом не меняется.
 */
function fitAttemptLabels(root) {
  const wrap = root && root.querySelector ? root.querySelector('.scale-wrap') : null
  if (!wrap) return
  let pad = 0
  for (const att of wrap.querySelectorAll('.tl-att')) {
    pad = Math.max(pad, att.scrollWidth || att.getBoundingClientRect().width || 0)
  }
  if (pad) wrap.style.setProperty('--tl-plot-pad', `${Math.min(320, Math.max(64, Math.ceil(pad) + 12))}px`)
}

/**
 * Провода обмена с ролью: путь от палки вопроса до дорожки адресата и обратно.
 *
 * Адрес вопроса — это не значок у палки, а линия до того, кого спросили (Макс,
 * 14.11: «я думал, стрелка будет красиво прямо до адресата идти»). Форма пути —
 * «туда и обратно»: подъём от момента вопроса к дорожке адресата, перекладина по
 * ней до момента ответа и спуск обратно, с наконечниками на обоих концах. Так
 * пара читается одним заходом, а не двумя палками рядом.
 *
 * Геометрию знает только DOM: дорожки разной высоты, строки раскрываются по
 * наведению, шкала листается. Проценты оси дают x (их считает шейпер), высоты
 * дают y (их знает вёрстка). Поэтому пересчёт идёт после отрисовки и по
 * наблюдателю за размером дорожек, а не по кадрам.
 */
function drawAskWires(root, scale) {
  if (!root || !root.querySelector) return
  const lanes = root.querySelector('.tl-lanes')
  if (!lanes) return
  // Шейпер кладём на сам узел: наблюдатель за размером держит только его, а
  // пересчёт после разворота строки обязан видеть те же пары.
  if (scale) lanes.__tlWireScale = scale
  const shaped = lanes.__tlWireScale
  if (!lanes.__tlWireWatch && typeof ResizeObserver === 'function') {
    lanes.__tlWireWatch = new ResizeObserver(() => {
      const owner = lanes.closest('.scale-wrap') || lanes.closest('.panel')
      if (owner) drawAskWires(owner)
    })
    lanes.__tlWireWatch.observe(lanes)
  }
  for (const old of lanes.querySelectorAll(':scope > .tl-wire')) old.remove()
  for (const mark of lanes.querySelectorAll('.tl-mark.is-wired')) mark.classList.remove('is-wired')
  const spans = shaped && Array.isArray(shaped.roleSpans) ? shaped.roleSpans : []
  if (!spans.length) return
  const box = lanes.getBoundingClientRect()
  // Пока панель не в документе, все прямоугольники нулевые и вести провод некуда.
  // Короткий повтор вместо кадра: `requestAnimationFrame` в фоновой вкладке не
  // приходит вовсе, а отрисовка — не повод ставить таймер в петлю опроса.
  if (box.width < 1 || lanes.clientWidth < 1) {
    if (typeof ResizeObserver === 'function' && (lanes.__tlWireRetry || 0) < 3) {
      lanes.__tlWireRetry = (lanes.__tlWireRetry || 0) + 1
      setTimeout(() => {
        const owner = lanes.closest('.scale-wrap') || lanes.closest('.panel')
        if (owner) drawAskWires(owner)
      }, 40)
    }
    return
  }
  lanes.__tlWireRetry = 0
  for (const [index, span] of spans.entries()) {
    if (!span || !span.role) continue
    const role = String(span.role)
    // Пара «вопрос → ответ» находится по номеру обмена, а не по времени: у двух
    // вопросов один момент бывает совпасть до процента.
    const ask = lanes.querySelector(`.tl-mark[data-ask-span="${index}"]`)
    const lane = lanes.querySelector(`[data-lane-role="${CSS.escape(role)}"]`)
    if (!ask || !lane) continue
    const row = ask.closest('.tl-row')
    const plot = ask.closest('.tl-plot')
    if (!row || !plot) continue
    // Адресат — строка его дорожки, а если попыток нет — сама дорожка: прийти
    // надо к тому, кого спросили, а не в пустоту рядом.
    const targetRow = lane.querySelector('.tl-row') || lane
    const plotBox = plot.getBoundingClientRect()
    const rowBox = row.getBoundingClientRect()
    const targetBox = targetRow.getBoundingClientRect()
    const x1 = Math.round(plotBox.left + (Number(span.fromPct) / 100) * plotBox.width - box.left)
    const x2 = span.toPct === null || span.toPct === undefined
      ? x1
      : Math.round(plotBox.left + (Number(span.toPct) / 100) * plotBox.width - box.left)
    const yAsk = Math.round(rowBox.top + rowBox.height / 2 - box.top)
    const yTarget = Math.round(targetBox.top + targetBox.height / 2 - box.top)
    // Путь короче восьми пикселей — это не дорога, а точка: рисовать нечего.
    if (Math.abs(yTarget - yAsk) < 8 || !x1) continue
    const up = yTarget < yAsk
    const wire = document.createElement('div')
    wire.className = `tl-wire${span.open ? ' is-open' : ''}`
    wire.setAttribute('aria-hidden', 'true')
    wire.setAttribute('data-tip', span.title || '')
    // Стойка у момента вопроса: отсюда ушёл вопрос, сюда вернулся ответ.
    wire.append(wireLeg(x1, yAsk, yTarget, up, span.open ? 'tail' : 'both'))
    if (!span.open && Math.abs(x2 - x1) > 2) {
      wire.append(wireCross(Math.min(x1, x2), yTarget, Math.abs(x2 - x1)))
      // Обратная стойка — у момента ответа: она приходит ОТ адресата к этапу.
      wire.append(wireLeg(x2, yAsk, yTarget, !up, 'back'))
    }
    lanes.append(wire)
    // Местная стрелка у палки стала бы вторым знаком того же пути.
    ask.classList.add('is-wired')
  }
}

/** Стойка провода: линия от строки до адресата, с наконечником и точкой начала. */
function wireLeg(x, yAsk, yTarget, up, kind) {
  const leg = document.createElement('span')
  leg.className = `tl-wire-leg ${up ? 'dir-up' : 'dir-down'} is-${kind}`
  leg.style.left = `${x}px`
  leg.style.top = `${Math.min(yAsk, yTarget)}px`
  leg.style.height = `${Math.abs(yTarget - yAsk)}px`
  return leg
}

/** Перекладина в дорожке адресата: от момента вопроса до момента ответа. */
function wireCross(left, y, width) {
  const cross = document.createElement('span')
  cross.className = 'tl-wire-cross'
  cross.style.left = `${left}px`
  cross.style.top = `${y}px`
  cross.style.width = `${width}px`
  return cross
}

/**
 * Место шкалы при обновлении данных: своё остаётся своим, правый край — правым.
 *
 * Ось пересчитывается от новых границ дела: справа приезжают свежие секунды, и
 * ширина полотна меняется. Пиксели место не держат — полотно уезжает под руками.
 * Поэтому у уехавшего от кромки человека сохраняется доля прокрутки (тот же кусок
 * времени остаётся в кадре), а у стоящего на правой кромке — новая кромка: дело
 * растёт, кадр идёт за ним (Макс, 18.11).
 */
function pinScaleScroll(root) {
  const scroller = root && root.querySelector ? root.querySelector('.scale-main.is-wide') : null
  if (!scroller) return
  // Уехал от кромки — не трогаем вовсе. Масштаб оси заморожен на «вьюпорт = час»,
  // поэтому новая секунда справа удлиняет полотно, а не сжимает нарисованное: в
  // кадре у такого человека ничего не двигается. Править здесь `scrollLeft`
  // значило бы увозить кадр на каждом тике (Макс, 18.11).
  if (scroller.__tlPinned === false) return
  scroller.scrollLeft = scroller.scrollWidth
}
document.addEventListener(
  'scroll',
  (event) => {
    const el = event.target
    if (el instanceof Element && el.classList && el.classList.contains('scale-main')) {
      if (!el.__tlUser) return
      el.__tlPinned = el.scrollLeft + el.clientWidth >= el.scrollWidth - 8
    }
  },
  { capture: true, passive: true },
)
// «Уехал» — только жест человека: колесо с горизонтальной составляющей или
// перетаскивание скроллбара. Программные скроллы (наш пин, восстановление
// позиции браузером после F5) пин не снимают (Макс, 09.11: после обновления
// страницы остаёмся у правой кромки).
document.addEventListener(
  'wheel',
  (event) => {
    const el = event.target instanceof Element ? event.target.closest('.scale-main.is-wide') : null
    if (el && event.deltaX) el.__tlUser = true
  },
  { capture: true, passive: true },
)
document.addEventListener(
  'pointerdown',
  (event) => {
    // Скроллбар — единственный pointerdown прямо по контейнеру: клики по чипам
    // и подписям пин не снимают.
    const el = event.target instanceof Element ? event.target.closest('.scale-main.is-wide') : null
    if (el && event.target === el) el.__tlUser = true
  },
  { capture: true },
)
// Строка раскрывается под указателем: высоты дорожек меняются, и концы провода
// уезжают вместе с ними. Пересчёт — по наведению с запасом на переход (0.28 s):
// `transitionend` приходит не всегда (строка могла быть уже раскрыта), а кадры в
// фоновой вкладке не рисуются.
document.addEventListener(
  'pointerover',
  (event) => {
    const row = event.target instanceof Element ? event.target.closest('.tl-row') : null
    const owner = row ? row.closest('.scale-wrap') : null
    if (!owner) return
    clearTimeout(owner.__tlWireHover)
    owner.__tlWireHover = setTimeout(() => drawAskWires(owner), 340)
  },
  true,
)
// Строка раскрылась под указателем — высоты дорожек изменились, и концы провода
// уехали бы вместе с ними. Пересчёт по концу перехода, а не на каждом кадре:
// раскрытие анимировано, и считать геометрию в середине анимации незачем.
document.addEventListener(
  'transitionend',
  (event) => {
    if (!(event.target instanceof Element)) return
    if (!event.target.classList || !event.target.classList.contains('tl-row')) return
    if (event.propertyName !== 'height') return
    const panel = event.target.closest('.panel, .scale-wrap')
    if (panel) drawAskWires(panel)
  },
  true,
)
window.addEventListener('resize', () => {
  for (const panel of document.querySelectorAll('.scale-wrap')) drawAskWires(panel)
})
bindTipLayer()
ticker = setInterval(tickStamp, 1000)
tickStamp()
void loadVersion()
onRoute()
