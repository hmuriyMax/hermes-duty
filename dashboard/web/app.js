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
  shapeDealTime,
  shapeFinished,
  shapeFinishedPage,
  shapeNow,
  shapeStages,
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
} from './model.js'

const TOKEN = window.__HERMES_SESSION_TOKEN__ || ''
const BASE = '/api/plugins/hermes-duty'
const HDR = TOKEN ? { 'X-Hermes-Session-Token': TOKEN } : {}
const REFRESH_MS = 10000
const HEALTH_REFRESH_MS = 30000
const TIMEOUT_MS = 15000
const SEARCH_DEBOUNCE_MS = 240
// Сколько живёт подсветка изменившегося значения (только цвет, без движения).
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
  // Всё, что осталось за пределами нового списка, — пропало из ответа. Так
  // удаляется одна закрытая попытка или целый этап, а не вся панель.
  while (oldParent.childNodes.length > placed.length) oldParent.removeChild(oldParent.lastChild)
}

/** Свести новое поддерево со старым; вернуть узел, который остался в DOM. */
function morph(oldNode, newNode) {
  if (!oldNode || oldNode.nodeType !== newNode.nodeType || oldNode.nodeName !== newNode.nodeName) {
    if (oldNode && oldNode.parentNode) oldNode.parentNode.replaceChild(newNode, oldNode)
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
      { class: `mono muted${deal.stageLive ? ' stage-now' : ''}`, title: deal.stageText || 'этап не начат' },
      deal.stageText || '—',
    ),
    h('td', { class: 'n', title: attemptTitle }, deal.attemptAgeText),
    h('td', { title: leaseTitle }, deal.leaseText),
    h('td', { class: 'n', title: `${deal.lastActionKind}: ${deal.lastActionSummary || '—'}` }, deal.lastActionAgeText),
    h('td', null, Badge(deal.state, deal.humanKind)),
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
 * Границы этапов для дорожек: один запрос на все показанные дела.
 *
 * Спрашивается то, что нарисовано (активные и первая пятёрка каждого проекта), а
 * не весь store: подсказка этапов у строки, которую не видно, никому не нужна.
 * Ошибка не глушит таймлайн — дорожки рисуются одним тоном состояния.
 */
async function loadTimelineSpans(ids) {
  const wanted = [...new Set(ids.filter(Boolean))]
  const known = Object.keys(timelineSpans)
  if (wanted.length && wanted.every((id) => known.includes(id))) return
  if (!wanted.length) return
  try {
    const payload = await api(`/spans?ids=${encodeURIComponent(wanted.join(','))}`)
    const spans = payload && typeof payload.spans === 'object' && payload.spans ? payload.spans : {}
    for (const [id, list] of Object.entries(spans)) timelineSpans[id] = list
    render()
  } catch (err) {
    // Молча: подсказка этапов — украшение, а не факт, на котором стоит страница.
  }
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

function stageColumnBars(stage, timeline) {
  const rows = Array.isArray(stage.attempts) ? stage.attempts : []
  const extra = timeline && Array.isArray(timeline.attempts) ? timeline.attempts : []
  if (!rows.length) return extra.filter((attempt) => attempt.stage === stage.id)
  return rows.map((row) => {
    if (row.runId === null || row.runId === undefined) return row
    const found = extra.find((attempt) => String(attempt.runId) === String(row.runId))
    return found ? { ...row, events: found.events } : row
  })
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
  const open = rowOpen(stageOpen, stageShown, stageKey, view.openByDefault)
  if (open) stageShown.add(stageKey)
  // Умолчание раскрытия на текущем кадре: клик читает его отсюда, чтобы после
  // тихого обновления не спорить с устаревшим замыканием.
  stageDefaults.set(stageKey, view.openByDefault)
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
          rebuild()
        },
      },
      h('span', { class: 'mono stage-row-id' }, view.id),
      h('span', { class: 'stage-row-kind muted' }, view.kindText),
      h('span', { class: 'stage-row-att fresh', 'data-fresh': `stage:${stageKey}` }, view.attemptsText),
      h('span', { class: 'sp' }),
      h('span', { class: `badge t-${view.tone}`, title: view.stateLabel }, h('i'), view.stateLabel),
      h('span', { class: 'stage-row-tog' }, open ? '▾' : '▸'),
    ),
  ]
  if (view.waitText) {
    nodes.push(
      h(
        'div',
        { class: 'stage-wait' },
        view.waitHref ? h('a', { href: view.waitHref }, view.waitText) : view.waitText,
      ),
    )
  }
  if (!open) return h('div', { class: 'stage-block', 'data-key': `block:${stageKey}` }, ...nodes)
  if (!bars.length) nodes.push(h('div', { class: 'att-none muted' }, 'попыток нет'))
  bars.forEach((bar, index) => {
    // Ключ попытки — её номер, а не место в списке: новая попытка сдвинула бы
    // индексы, и раскрытие переехало бы на чужую строку.
    const attKey = `${stageKey}#${bar.number || index}`
    const barView = compactAttemptView(stage, bar)
    attDefaults.set(attKey, barView.openByDefault)
    const attOpenNow = rowOpen(attOpen, attShown, attKey, barView.openByDefault)
    if (attOpenNow) attShown.add(attKey)
    nodes.push(...attRowNodes(stage, bar, attKey, attOpenNow, rebuild))
  })
  return h('div', { class: 'stage-block', 'data-key': `block:${stageKey}` }, ...nodes)
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
  verdict: 'v',
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

/** Маркер события: засечка старта/закрытия попытки или заметная точка события. */
function markerNode(marker, index) {
  return h(
    'span',
    {
      class: `scale-marker t-${marker.tone}${marker.tick ? ' tick' : ''} anchor-${marker.anchor || 'middle'}`,
      style: `left:${marker.pct}%;top:${(index % 2) * 13}px`,
      title: marker.title,
      'data-key': `marker:${marker.type}:${marker.at}`,
      'aria-label': marker.ariaLabel,
      role: 'img',
      tabindex: '0',
    },
    h('span', { class: 'scale-marker-dot' }, MARKER_GLYPH[marker.type] || ''),
  )
}

/** Легенда маркеров: только те виды, что реально есть в журнале этого дела. */
function markerLegend(scale) {
  const seen = []
  for (const marker of scale.markers) {
    if (marker.tick || seen.some((item) => item.type === marker.type)) continue
    seen.push(marker)
  }
  if (!seen.length) return null
  return h(
    'div',
    { class: 'scale-legend' },
    ...seen.map((marker) =>
      h('span', { class: `t-${marker.tone}` }, `${MARKER_GLYPH[marker.type] || '•'} ${marker.label}`),
    ),
    // Про засечки старта/конца здесь молчим: их больше нет, длительность
    // показывает фон попытки, а факты — палки-маркеры.

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
function attemptRowNode(view, key) {
  const label = h(
    'div',
    { class: 'tl-lbl', 'data-tip': view.bandTip || view.title || '' },
    h('span', { class: 'tl-n' }, view.label.number),
    // Исход попытки — то, что меняется от тика к тику: подсвечивается он, а не
    // вся дорожка (полоса растёт непрерывно, вспышка на ней была бы миганием).
    h('span', { class: `tl-st t-${view.label.tone} fresh`, 'data-fresh': key }, view.label.status),
    // Суммарное число вызовов попытки — в подписи, а не поверх дорожки: оно не
    // должно перекрывать чипы.
    view.badge
      ? h('span', { class: `tl-more${view.badgeCut ? ' is-cut' : ''}`, title: view.note || view.title }, view.badge)
      : null,
  )
  const track = h('div', { class: 'tl-track', 'data-key': `track:${key}` })
  if (view.band) {
    track.append(
      h('span', {
        class: `tl-band t-${view.band.tone || 'grey'}`,
        style: `left:${view.band.leftPct}%;width:${view.band.widthPct}%`,
        'data-tip': view.bandTip,
        'data-key': 'band',
      }),
    )
  }
  // Тишина: зазор, где не было ни вызова, ни слова. Полоса попытки остаётся на
  // месте — тишина рисуется поверх неё, иначе «шло» и «молчало» слились бы вновь.
  for (const silence of view.silences || []) {
    track.append(
      h('span', {
        class: 'tl-silence',
        style: `left:${silence.leftPct}%;width:${silence.widthPct}%`,
        'data-tip': silence.tip,
        'data-key': `silence:${silence.leftPct}`,
      }),
    )
  }
  // Пауза «вопрос → ответ» — подсветка времени, которое дело стояло на человеке.
  for (const wait of view.waits) {
    track.append(
      h('span', {
        class: 'tl-wait',
        style: `left:${wait.fromPct}%;width:${wait.widthPct}%`,
        'data-tip': wait.title,
        'data-key': `wait:${wait.fromPct}`,
      }),
    )
  }
  for (const chip of view.chips) {
    track.append(
      h('span', {
        class: `tl-call${chip.unfinished ? ' tl-unfinished' : ''}`,
        'data-level': String(chip.level || 0),
        'data-tone': chip.tone,
        'data-key': `call:${chip.id}`,
        style: chip.unfinished
          ? `left:${chip.leftPct}%;top:${chip.top}px;width:${chip.widthPct}%;color:${toneVar(chip.tone)}`
          : `left:${chip.leftPct}%;top:${chip.top}px;width:${chip.widthPct}%;background:${toneVar(chip.tone)}`,
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
    track.append(
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
      marks.append(
        h(
          'span',
          {
            class: `tl-mark t-${mark.tone} anchor-${mark.anchor || 'middle'}`,
            style: `left:${mark.pct}%`,
            'data-tip': mark.title,
            'aria-label': mark.ariaLabel,
            'data-key': `mark:${mark.type}:${mark.pct}`,
          },
          h('i'),
        ),
      )
    }
    track.append(marks)
  }
  return h(
    'div',
    // Свёрнутая высота показывает первый уровень вызовов и маркеры, полная
    // раскрывается под указателем. Числа — из шейпера, CSS только переключает.
    {
      class: `tl-row${view.failed ? ' is-failed' : ''}`,
      style: `--tl-collapsed:${view.collapsed}px;--tl-full:${view.full}px`,
      'data-key': key,
    },
    label,
    track,
  )
}

/** Дорожка этапа: имя, значок вида, состояние и строки попыток. */
function laneNode(lane, bars, scale) {
  const view = laneView(lane, bars, scale)
  const head = h(
    'div',
    { class: 'tl-lane-h' },
    h('span', { class: 'tl-lane-name', title: view.name }, view.name),
    view.kind
      ? h(
          'span',
          {
            class: `tl-kind tl-kind-${view.kind}`,
            title: view.kind === 'agent' ? `профиль: ${view.kindText}` : view.kind,
          },
          h(
            'svg',
            { class: 'tl-kind-ico', viewBox: '0 0 16 16', 'aria-hidden': 'true' },
            h('path', { d: view.icon }),
          ),
          view.kindText,
        )
      : null,
    h('span', { class: 'sp' }),
    h('span', { class: 'tl-lane-meta fresh', 'data-fresh': `lane:${lane.key}` }, view.meta),
  )
  return h(
    'div',
    { class: 'tl-lane', 'data-key': `lane:${lane.key}` },
    head,
    view.empty
      ? h('div', { class: 'tl-row-empty' }, 'попыток не было')
      : h('div', { class: 'tl-rows' }, ...view.rows.map((row) => attemptRowNode(row.view, `attempt:${row.key}`))),
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

// Выбранный вызов и его данные: выбор живёт на экране «Дело» и переживает
// перерисовку панели. Ключ — `stage:attempt:seq` из шейпера, индекс собирается
// при отрисовке шкалы: у поверхности нет своего знания о вызовах.
let dutySelectedCallId = ''
const dutyCallIndex = new Map()

/** Выбранный вызов или `null`; индекс наполняет `scalePanel`. */
function selectedCall() {
  return dutySelectedCallId ? dutyCallIndex.get(dutySelectedCallId) || null : null
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
 * Инспектор вызова: что за инструмент, когда начался, сколько шёл.
 *
 * Экран показывает факты вызова, а не «открывает» попытку второй раз: строка
 * попытки остаётся свёрнутой, развёртка живёт только под указателем.
 */
function inspectorNode(call) {
  if (!call) {
    return h('div', { class: 'tl-insp empty' }, 'выбери вызов на дорожке — покажу инструмент, начало и длительность')
  }
  return h(
    'div',
    { class: 'tl-insp' },
    h('span', { class: `tl-insp-dot t-${call.tone}` }),
    h('b', null, call.name),
    h('span', { class: 'mono muted' }, call.clock),
    h('span', null, call.msText),
    h('span', { class: 'muted' }, `${call.stage} · попытка ${call.attempt}${call.seq === null ? '' : ` · вызов ${call.seq}`}`),
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
  // Индекс вызовов: чип и инспектор читают один и тот же вид (`attemptView`).
  dutyCallIndex.clear()
  for (const bar of scale.bars) {
    for (const chip of attemptView(bar, scale).chips) dutyCallIndex.set(chip.id, chip)
  }
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
    h('div', { class: 'panel-h' }, `шкала времени · ${scale.axis.text}`, refreshedNode()),
    h(
      'div',
      { class: 'panel-b' },
      // Ось стоит ровно над дорожками: слева пустая колонка шириной подписей,
      // справа — та же область, где рисуются чипы.
      h(
        'div',
        { class: 'scale-wrap' },
        h(
        'div',
        { class: 'scale-main' },
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
              { class: `scale-tick align-${tick.align}`, style: `left:${tick.pct}%`, title: tick.clock, 'data-key': `tick:${tick.pct}` },
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
            h('span', { class: 'scale-grid-line', style: `left:${tick.pct}%`, 'data-key': `grid:${tick.pct}` }),
          ),
        ),
        // Сжатые промежутки оси: пара пунктиров по краям и подпись посередине.
        // Ось сжимает пустое время — это должно быть видно, а не угадываться по
        // «дыре» между полосами (`idle.gaps` считает шейпер, здесь только DOM).
        scale.idle.gaps.length
          ? h(
              'svg',
              { class: 'tl-idle-layer' },
              ...scale.idle.gaps.flatMap((gap) => [
                h('line', { class: 'tl-idle', 'data-key': `idle:${gap.from}:l`, x1: `${gap.leftPct}%`, y1: '0', x2: `${gap.leftPct}%`, y2: '100%' }),
                h('line', { class: 'tl-idle', 'data-key': `idle:${gap.from}:r`, x1: `${gap.rightPct}%`, y1: '0', x2: `${gap.rightPct}%`, y2: '100%' }),
                h(
                  'text',
                  {
                    class: 'tl-idle-label',
                    'data-key': `idle:${gap.from}:t`,
                    x: `${gap.labelPct}%`,
                    y: '50%',
                    'text-anchor': gap.labelAnchor,
                    'dominant-baseline': 'middle',
                  },
                  h('title', null, gap.title),
                  gap.text,
                ),
              ]),
            )
          : null,
        h(
        'div',
        { class: 'tl-lanes' },
        ...scale.lanes.map((lane, index) =>
          laneNode(
            lane,
            scale.bars.filter((bar) => bar.lane === index),
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
      h('div', { class: 'tl-insp-host' }, inspectorNode(selectedCall(scale))),
      // Легенда маркеров: что означают точки внутри строк попыток.
      markerLegend(scale),
      scale.facts.attempts.journal ? h('div', { class: 'muted' }, 'полосы собраны из журнала: ответ попыток пуст') : null,
    ),
  )
  // Фильтр, выбранный в легенде, переживает перерисовку данных: экран
  // пересобирается по новому кадру, а подсветка остаётся там, где её оставили.
  wireScalePanel(panel)
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

  // Клик по вызову выбирает его, но не разворачивает строку попытки: развёртка
  // живёт под указателем, а выбор — в инспекторе под шкалой.
  panel.addEventListener('click', (event) => {
    const chip = event.target.closest && event.target.closest('.tl-call')
    const host = panel.querySelector('.tl-insp-host')
    if (!chip || !host || chip.classList.contains('tl-dim')) return
    const id = chip.getAttribute('data-call-id') || ''
    dutySelectedCallId = dutySelectedCallId === id ? '' : id
    for (const el of panel.querySelectorAll('.tl-call.tl-sel')) el.classList.remove('tl-sel')
    if (dutySelectedCallId) chip.classList.add('tl-sel')
    host.replaceChildren(inspectorNode(selectedCall()))
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur()
  })
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
        h('h1', null, data.title || '—'),
        specStrip(scale.spec),
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
      deal ? h('span', { 'data-key': 'fact:attempt' }, 'попытка: ', h('b', null, `${deal.attemptAgeText} (${deal.attemptNote})`)) : null,
      deal ? h('span', { 'data-key': 'fact:lease' }, 'лизинг: ', h('b', null, `${deal.leaseText}`)) : null,
      h('span', null, 'создано: ', h('b', null, fmtClock(data.created_at))),
      deal ? h('span', { 'data-key': 'fact:action' }, 'последнее действие: ', h('b', null, `${deal.lastActionAgeText} назад`)) : null,
    ),
    // Факты шкалы: попытки по этапам, длительность, вопросы, бюджет, публикация.
    h('div', { class: 'deal-facts', 'data-key': 'facts' }, ...dealFactSpans(scale)),
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

  // Этапы и попытки — одним тонким столбцом (320 px) рядом с описанием: шапка
  // попытки раньше повторялась в широкой ленте этапов и в отдельной панели
  // попыток, и страница дела росла на три с половиной экрана.
  const stages = stagesColumn(data.id)

  return h(
    'div',
    { 'data-key': 'deal' },
    h('div', { style: 'margin-bottom:10px' }, h('a', { href: '#/deal' }, '← к списку дел')),
    head,
    kpiStrip(scale),
    scalePanel(scale),
    left ? h('div', { class: 'deal-cols' }, left, stages) : stages,
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

// Фильтр легенды и выбранный вызов — состояние экрана, а не ответа: после патча
// узлы приходят «чистыми», и отметки надо вернуть, иначе подсветка гаснет на
// каждом тике.
function syncScaleState() {
  applyToolFilter(app)
  for (const chip of app.querySelectorAll('.tl-call')) {
    const on = Boolean(dutySelectedCallId) && chip.getAttribute('data-call-id') === dutySelectedCallId
    chip.classList.toggle('tl-sel', on)
  }
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
  morph(host, viewDeal(dealId))
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
    return
  }
  if (!route.id || route.bad || route.name !== 'deal') return
  dealPollStop()
  void dealPollTick(route.id)
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

function render() {
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
}

function schedule() {
  clearTimeout(timer)
  // Страница дела опрашивает себя сама (`dealPollLater`): её период зависит от
  // состояния дела — идущее обновляем часто, закрытое редко, — а общий таймер
  // экранов знает только «медленно/быстро».
  if (route.name === 'deal' && route.id && !route.bad) {
    dealPollLater(route.id)
    return
  }
  // «Здоровье» и «Завершённые» — по журналу за всю историю: они не меняются за
  // секунды, и опрашивать их так же часто, как срез «сейчас», незачем.
  const slow = route.name === 'health'
  const ms = slow ? HEALTH_REFRESH_MS : REFRESH_MS
  timer = setTimeout(run, ms)
}

function run() {
  if (route.name === 'now') void loadNow()
  else if (route.name === 'why') void loadNow()
  else if (route.name === 'health') void loadHealth()
  else if (route.bad) void loadNow()
  else if (route.id) void loadDeal(route.id)
  else void loadNow()
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
  run()
  schedule()
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

bindTipLayer()
ticker = setInterval(tickStamp, 1000)
tickStamp()
void loadVersion()
onRoute()
