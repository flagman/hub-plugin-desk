// «Сводка» — личный вид владельца: картина его дел. Дело — то, чем занимаются (задача, MR, тема), а не сессия.
// Что изменилось, что нужно от него, что готово к проверке, где каждое дело — пишет процесс плагина (cases.py) по
// журналу ядра; открытые вопросы-карточки — из снимка ядра и отвечаются прямо здесь; «Сейчас работают» — живое.
let hub = null;

const el = (...a) => hub.el(...a);

const DAY = 86400;

const URL_RE = /https?:\/\/[^\s<>()`'"]+[^\s<>()`'".,;:!?]/g;

const opened = new Set();      // развёрнутые тексты

const inputs = new Map();      // ключ -> поле ввода (hub.input): один узел между перерисовками — набранное не теряется

const typing = new Set();      // открытые поля «Своё…»

const chosen = new Map();      // request_id -> {вопрос: [выбор]}

const sent = new Map();        // request_id -> «ушло» | ошибка

let state = { root: null, key: "", retry: 0 };

function topicOf(x, by) {
  let cur = x;
  const seen = new Set();
  while (!cur.backlog && cur.raised_by && !seen.has(cur.name)) {
    seen.add(cur.name);
    const up = by.get(cur.raised_by);
    if (!up || up.kind === "hub") break;
    cur = up;
  }
  return cur.backlog || cur.name;
}

function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - (ts || 0));
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} мин`;
  if (s < DAY) return `${Math.round(s / 3600)} ч`;
  return `${Math.round(s / DAY)} д`;
}

// «N мин» — на месте раз в 30 с, а не перерисовкой списка (раньше раз в минуту пересобиралось всё)
function agoEl(ts, pre = "", post = "") {
  const e = el("span", "dk-ago", `${pre}${ago(ts)}${post}`);
  Object.assign(e.dataset, { ts: String(ts || 0), pre, post });
  return e;
}

// новое — только в изменившихся карточках: собранный заново список сверяется с тем, что на экране, и карточка, у которой
// ничего не поменялось, остаётся тем же узлом.
// Ключ карточки — data-k (иначе место среди соседей); контейнеры с data-deep сверяются глубже, остальное — целиком
function patch(old, now) {
  const olds = new Map([...old.children].map((o, i) => [o.dataset.k || `#${i}`, o]));
  const want = [...now.children].map((n, i) => {
    const o = olds.get(n.dataset.k || `#${i}`);
    if (!o || o.tagName !== n.tagName) return n;
    if (o.isEqualNode(n)) return o;
    if (n.dataset.deep !== undefined && o.className === n.className) {
      for (const a of [...o.attributes]) if (!n.hasAttribute(a.name)) o.removeAttribute(a.name);
      for (const a of [...n.attributes]) if (o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value);
      patch(o, n);
      return o;
    }
    return n;
  });
  want.forEach((n, i) => { if (old.children[i] !== n) old.insertBefore(n, old.children[i] || null); });
  while (old.children.length > want.length) old.lastElementChild.remove();
}

function doing(x) {
  const t = (x.todos || []).find((i) => i.status === "in_progress");
  return (x.recap && x.recap.text) || (t && (t.activeForm || t.content)) || (x.away && x.away.text) || "";
}

// ---- элементы ----
function button(cls, text, title, onClick) {
  const b = el("button", cls, text);
  b.type = "button";
  if (title) b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

function openSession(name, typeIn) {
  location.hash = `#/s/${encodeURIComponent(name)}`;
  // поле ответа ленты (#msg) появляется после перерисовки страницы — фокус следующим кадром, нет поля — ничего
  if (typeIn) setTimeout(() => requestAnimationFrame(() => document.getElementById("msg")?.focus()), 50);
}

// ссылки в тексте: адреса и ключи задач Jira (проекты и адрес — из настроек хаба)
function jiraRe(snap) {
  const j = snap.jira || {};
  return j.url && (j.projects || []).length ? new RegExp(`\\b(${j.projects.join("|")})-\\d+\\b`, "g") : null;
}

function linkify(text) {
  const snap = hub.snapshot() || {};
  const jr = jiraRe(snap);
  const parts = [];
  for (const m of text.matchAll(URL_RE)) parts.push([m.index, m[0], m[0]]);
  if (jr) for (const m of text.matchAll(jr)) {
    if (!parts.some(([i, t]) => m.index >= i && m.index < i + t.length)) parts.push([m.index, m[0], `${snap.jira.url}/browse/${m[0]}`]);
  }
  parts.sort((a, b) => a[0] - b[0]);
  const frag = document.createDocumentFragment();
  let at = 0;
  for (const [i, t, href] of parts) {
    if (i < at) continue;
    frag.append(text.slice(at, i));
    const a = el("a", "dk-a", t);
    a.href = href; a.target = "_blank"; a.rel = "noopener";
    a.addEventListener("click", (e) => e.stopPropagation());
    frag.append(a);
    at = i + t.length;
  }
  frag.append(text.slice(at));
  return { frag, links: parts.map(([, t, href]) => [t, href]) };
}

// свёрнутое — текстом без значков разметки (** ` ##), раскрытое — разметкой, как в ленте
function plain(text) {
  return String(text).replace(/```[\w-]*\n?/g, "").replace(/\*\*|__|`/g, "").replace(/^#{1,6}\s+/gm, "");
}

function clampText(id, text, cls = "dk-text") {
  let p;
  if (opened.has(id)) {
    p = el("div", `${cls} dk-full`);
    const md = markdown(String(text));
    for (const a of md.querySelectorAll("a[href]")) { a.target = "_blank"; a.rel = "noopener"; }
    p.append(md);
  } else {
    p = el("p", `${cls} clamp`);
    p.append(linkify(plain(text)).frag);
  }
  p.addEventListener("click", (e) => { e.stopPropagation(); if (!e.target.closest("a")) toggleOpen(id); });
  return p;
}

function toggleOpen(id) {
  if (getSelection && String(getSelection())) return;   // выделяют текст — не сворачивать
  if (opened.has(id)) opened.delete(id); else opened.add(id);
  repaint(true);
}

// клик по карточке мимо кнопок и ссылок раскрывает её текст
function expandOn(node, id) {
  node.classList.add("dk-expand");
  node.addEventListener("click", (e) => { if (!e.target.closest("button, a, input, textarea, select, label")) toggleOpen(id); });
}

// поле ввода, как у чата: сообщение уходит сессии от владельца; при открытой карточке — ответом на неё
function field(key, to, placeholder, prefix) {
  if (!inputs.has(key)) {
    inputs.set(key, hub.input({ to, key: `desk:${key}`, placeholder, prefix,
      onSent: () => { typing.delete(key); repaint(true); } }));
  }
  return inputs.get(key).node;
}

function toggleTyping(key) {
  if (typing.has(key)) typing.delete(key); else typing.add(key);
  repaint(true);
  if (typing.has(key)) setTimeout(() => inputs.get(key)?.focus(), 0);
}

async function answer(x, ask) {
  const qs = ask.input.questions || [];
  const pick = chosen.get(ask.request_id) || {};
  const answers = {};
  for (const q of qs) answers[q.question] = (pick[q.question] || []).join(", ");
  sent.set(ask.request_id, "…");
  repaint(true);
  const r = await hub.post("/api/answer", { session: x.name, request_id: ask.request_id, answers });
  sent.set(ask.request_id, r.error || "Ответ ушёл");
  repaint(true);
}

function askCard(item, ask) {
  const x = item.x;
  const box = el("div", "dk-ask");
  if (ask.tool !== "AskUserQuestion") {   // разрешение на инструмент
    const inp = ask.input || {};
    box.append(el("p", "dk-text", `Просит разрешение: ${ask.tool}`),
      el("code", "dk-cmd", String(inp.command || inp.file_path || inp.url || "").slice(0, 300)));
    const bar = el("div", "dk-opts");
    for (const [b, label] of [["allow", "Разрешить"], ["deny", "Запретить"]]) {
      bar.append(button(`dk-opt${b === "allow" ? " rec" : ""}`, label, null, async () => {
        const r = await hub.post("/api/permission", { session: x.name, request_id: ask.request_id, behavior: b });
        sent.set(ask.request_id, r.error || "Ушло");
        repaint(true);
      }));
    }
    box.append(bar);
    return box;
  }
  const qs = (ask.input || {}).questions || [];
  const pick = chosen.get(ask.request_id) || {};
  for (const [i, q] of qs.entries()) {
    const sec = el("div", "dk-q");
    if (q.header) sec.append(el("span", "dk-qh", q.header));
    sec.append(clampText(`${ask.request_id}:${i}`, q.question));
    const bar = el("div", "dk-opts");
    for (const o of q.options || []) {
      const label = o.label;
      const on = (pick[q.question] || []).includes(label);
      const b = button(`dk-opt${/\(Recommended\)|рекоменд/i.test(label) ? " rec" : ""}${on ? " on" : ""}`,
        label.replace(/\s*\(Recommended\)\s*/i, "").trim(), o.description || "", () => {
          const cur = pick[q.question] || [];
          pick[q.question] = q.multiSelect ? (on ? cur.filter((v) => v !== label) : [...cur, label]) : [label];
          chosen.set(ask.request_id, pick);
          if (qs.length === 1 && !q.multiSelect) answer(x, ask); else repaint(true);
        });
      bar.append(b);
    }
    if (i === qs.length - 1) bar.append(button("dk-opt own", "Своё…", "Ответить своими словами — прямо здесь", () => toggleTyping(`own:${ask.request_id}`)));
    const why = `why:${ask.request_id}:${i}`;
    if ((q.options || []).some((o) => o.description)) {
      bar.append(button("dk-opt own", opened.has(why) ? "Скрыть пояснения" : "Пояснения", "Что значит каждый вариант", () => {
        if (opened.has(why)) opened.delete(why); else opened.add(why);
        repaint(true);
      }));
    }
    sec.append(bar);
    if (opened.has(why)) {
      const dl = el("dl", "dk-why");
      for (const o of q.options || []) dl.append(el("dt", null, o.label.replace(/\s*\(Recommended\)\s*/i, "")), el("dd", null, o.description || ""));
      sec.append(dl);
    }
    box.append(sec);
  }
  const done = sent.get(ask.request_id);
  if (qs.length > 1 || qs.some((q) => q.multiSelect)) {
    const ready = qs.every((q) => (pick[q.question] || []).length);
    const go = button("dk-send", "Ответить", ready ? "" : "Выберите ответ на каждый вопрос", () => answer(x, ask));
    go.disabled = !ready || done === "…";
    box.append(go);
  }
  if (done) box.append(el("p", "dk-sent", done));
  const k = `own:${ask.request_id}`;
  if (typing.has(k)) box.append(field(k, x.name, `Ответ ${x.name} своими словами — уйдёт ответом на карточку`));
  return box;
}

// подпись ссылки: MR — «MR !11 · репо», остальное — адрес без схемы
function chipLabel(t) {
  const mr = t.match(/^https?:\/\/[^/]+\/(?:.*\/)?([^/]+)\/-\/merge_requests\/(\d+)/);
  if (mr) return `MR !${mr[2]} · ${mr[1]}`;
  const pr = t.match(/^https?:\/\/github\.com\/[^/]+\/([^/]+)\/pull\/(\d+)/);
  if (pr) return `PR #${pr[2]} · ${pr[1]}`;
  return t.replace(/^https?:\/\//, "").slice(0, 48);
}

function workGroups(work) {
  const by = new Map();
  for (const it of work) by.set(it.topic, [...(by.get(it.topic) || []), it]);
  const box = el("div", "dk-work");
  for (const [topic, list] of [...by].sort((a, b) => b[1].length - a[1].length)) {
    const g = el("div", "dk-wg");
    g.append(el("b", "dk-topic", topic));
    const ul = el("ul");
    for (const it of list) {
      const li = el("li");
      const a = button("dk-wl", "", it.x.name, () => openSession(it.x.name));
      a.append(el("span", "dk-who", it.x.name === topic ? "сам" : it.x.name.replace(topic + "-", "")),
        document.createTextNode(" " + (doing(it.x) || "работает")), agoEl(it.x.since, " · идёт "));
      li.append(a);
      ul.append(li);
    }
    g.append(ul);
    box.append(g);
  }
  return box;
}

function section(id, title, count, body, empty) {
  const s = el("section", "dk-sec");
  Object.assign(s.dataset, { g: id, k: id, deep: "" });
  const h = el("h2", null, title);
  h.append(el("span", "dk-n", String(count)));
  s.append(h);
  if (count) s.append(...body); else s.append(el("p", "dk-empty", empty));
  return s;
}

function markdown(text) {
  const box = el("div", "dk-md");
  if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
    box.innerHTML = DOMPurify.sanitize(marked.parse(text), { FORBID_TAGS: ["img", "style", "form", "input", "button", "iframe", "svg", "math"] });
  } else box.textContent = text;
  return box;
}

// «Ответить» — в ленту сессии: весь разговор там, курсор — в поле ответа. Своей панели с разговором нет: второй
// интерфейс того же: не дублировать интерфейсы ядра, а переходить в контекст
function replyButton(name, rec) {
  return button(`dk-opt${rec ? " rec" : ""}`, "Ответить", "Лента сессии, курсор в поле ответа", () => openSession(name, true));
}

// «N мин» — на месте раз в 30 с, а не перерисовкой списка
setInterval(() => {
  for (const e of state.root ? state.root.querySelectorAll(".dk-ago[data-ts]") : []) {
    const t = `${e.dataset.pre}${ago(Number(e.dataset.ts))}${e.dataset.post}`;
    if (e.textContent !== t) e.textContent = t;
  }
}, 30000);

// ---- картина по делам: пишет процесс плагина (cases.py) в out/picture.json; страница берёт
// файл раз в 10 с. Дело — не сессия: одно дело ведут несколько сессий. Прочтение снимает только отметку «новое»,
// само дело и решение остаются, пока они живы ----
const pic = { data: {}, raw: "", at: 0, busy: false, tried: false };
function needPicture() {
  if (pic.busy || Date.now() - pic.at < 10000) return;
  pic.busy = true;
  pic.at = Date.now();
  fetch(hub.url("out/picture.json")).then((r) => (r.ok ? r.text() : ""))
    .then((t) => { if (t && t !== pic.raw) { pic.raw = t; pic.data = JSON.parse(t); } })
    .catch(() => {}).finally(() => { pic.busy = false; pic.tried = true; repaint(false); });
}

// «новое» у дела: его содержание сменилось с тех пор, как владелец отметил «Прочитал» — в браузере
const CASE_KEY = "desk.cases";
let caseSeen = JSON.parse(localStorage.getItem(CASE_KEY) || "null");
const caseHash = (c) => JSON.stringify([c.where, c.next, c.blocker]);
function markCasesRead(list) {
  caseSeen = Object.fromEntries(list.map((c) => [c.id, caseHash(c)]));
  localStorage.setItem(CASE_KEY, JSON.stringify(caseSeen));
}

function sessionLink(name) {
  const a = el("a", "dk-sess", name);
  a.href = `#/s/${encodeURIComponent(name)}`;
  a.title = "Лента сессии";
  return a;
}

// карточка вопроса из ядра — отвечается прямо здесь (ядро — источник правды: открыта, пока сессия в диалоге)
function askRow(x) {
  const r = el("article", "dk-row you");
  r.dataset.k = `ask:${x.name}`;
  const h = el("div", "dk-row-head");
  h.append(sessionLink(x.name), el("span", "dk-spacer"), agoEl(x.since));
  r.append(h);
  for (const a of x.pending) r.append(askCard({ x, topic: x.name }, a));
  return r;
}

function needRow(n, i) {
  const r = el("article", "dk-row you");
  r.dataset.k = `need:${n.case}:${i}`;
  const h = el("div", "dk-row-head");
  h.append(el("span", "dk-topic", n.case || ""));
  if (n.session) h.append(sessionLink(n.session));
  r.append(h, el("p", "dk-text strong", plain(n.text)));
  if (n.why) r.append(el("p", "dk-text dk-why-l", plain(n.why)));
  if (n.recommend) r.append(el("p", "dk-text dk-rec", `Совет: ${plain(n.recommend)}`));
  if (n.session) {
    const bar = el("div", "dk-opts");
    bar.append(replyButton(n.session, true));
    r.append(bar);
  }
  return r;
}

function readyRow(it, i) {
  const r = el("article", "dk-row");
  r.dataset.k = `ready:${it.case}:${i}`;
  const h = el("div", "dk-row-head");
  h.append(el("span", "dk-topic", it.case || ""));
  if (it.session) h.append(sessionLink(it.session));
  r.append(h, el("p", "dk-text", plain(it.text)));
  if (it.where) {
    const p = el("p", "dk-text dk-where");
    p.append("Где смотреть: ", linkify(it.where).frag);
    r.append(p);
  }
  return r;
}

function caseRow(c) {
  const r = el("article", `dk-row dk-case${c.needs_owner ? " own" : ""}`);
  r.dataset.k = `case:${c.id}`;
  const h = el("div", "dk-row-head");
  const id = el("span", "dk-id");
  id.append(linkify(c.id).frag);
  h.append(el("span", "dk-topic", c.title || c.id), id, el("span", "dk-spacer"));
  if (caseSeen && caseSeen[c.id] !== caseHash(c)) h.append(el("span", "dk-new", caseSeen[c.id] ? "изменилось" : "новое"));
  r.append(h);
  const row = (label, text, cls = "") => {
    if (!text) return;
    const p = el("p", `dk-text dk-kv ${cls}`);
    p.append(el("b", null, label), linkify(plain(text)).frag);
    r.append(p);
  };
  row("Сейчас: ", c.where);
  row("Дальше: ", c.next);
  row("Мешает: ", c.blocker, "dk-block");
  if ((c.sessions || []).length) {
    const s = el("div", "dk-sessions");
    for (const n of c.sessions) s.append(sessionLink(n));
    r.append(s);
  }
  return r;
}

// ---- вид ----
function render(root) {
  const snap = hub.snapshot() || {};
  const p = pic.data.picture || null;
  const live = (snap.sessions || []).filter((x) => x.state !== "removed");
  const asks = live.filter((x) => (x.pending || []).length);
  const asked = new Set(asks.map((x) => x.name));
  // пункт картины, на который у сессии сейчас открыта карточка, — показывает сама карточка
  const need = ((p && p.need) || []).filter((n) => !asked.has(n.session));
  const ready = (p && p.ready) || [];
  const cases = (p && p.cases) || [];
  if (p && !caseSeen) markCasesRead(cases);   // первый заход: всё, что есть, — не «новое»
  const by = new Map(live.map((x) => [x.name, x]));
  const work = live.filter((x) => x.kind !== "hub" && ["busy", "starting"].includes(x.state)).map((x) => ({ x, topic: topicOf(x, by) }));

  const box = el("div", "dk");
  box.dataset.deep = "";
  const top = el("header", "dk-top");
  top.dataset.k = "top";
  const sub = el("span", "dk-sub", [`${asks.length + need.length} нужно от тебя`, `${ready.length} готово к проверке`,
    `${cases.length} дел`].join(" · "));
  if (pic.data.as_of) sub.append(" · ", agoEl(pic.data.as_of, "обновлено ", " назад"));
  top.append(el("h1", null, "Сводка"), sub);
  box.append(top);

  if (!p) {
    box.append(el("p", "dk-empty", pic.tried ? "Картина дел собирается — первая займёт около минуты." : "Загружаю…"));
  } else if (p.changed) {
    const ch = el("section", "dk-sec dk-changed");
    ch.dataset.k = "changed";
    ch.append(el("h2", null, "Что изменилось"), el("p", "dk-text", plain(p.changed)));
    box.append(ch);
  }
  box.append(section("need", "Нужно от тебя", asks.length + need.length, [...asks.map(askRow), ...need.map(needRow)],
    "Ничего — решать сейчас нечего"));
  if (ready.length) box.append(section("ready", "Готово к проверке", ready.length, ready.map(readyRow), ""));
  if (p) {
    const fresh = cases.filter((c) => caseSeen && caseSeen[c.id] !== caseHash(c)).length;
    const s = section("cases", "Дела", cases.length, cases.map(caseRow), "Дел за последние сутки нет");
    if (fresh) s.querySelector("h2").append(button("dk-opt dk-h-btn", "Прочитал", "Снять отметки «новое» и «изменилось»",
      () => { markCasesRead(cases); repaint(true); }));
    box.append(s);
  }
  box.append(section("work", "Сейчас работают", work.length, [workGroups(work)], "Сейчас никто не работает"));

  if (!state.list || state.list.parentNode !== root) {
    state.list = el("div", "dk-list");
    root.replaceChildren(state.list);
  }
  const was = state.list.firstElementChild;
  if (was && was.className === box.className) patch(was, box); else state.list.replaceChildren(box);
}

// перерисовать, только если изменилось то, что видно
function repaint(force) {
  if (!state.root) return;
  const snap = hub.snapshot() || {};
  needPicture();
  const key = JSON.stringify([(snap.sessions || []).map((x) => [x.name, x.state, x.since, (x.pending || []).length,
    x.recap?.ts]), pic.raw.length, pic.data.as_of, pic.tried]);
  if (!force && key === state.key) return;
  // набирают в поле — не перерисовывать под рукой: доделаем, когда фокус уйдёт
  const a = document.activeElement;
  if (!force && a && state.root.contains(a) && (a.isContentEditable || /^(TEXTAREA|INPUT)$/.test(a.tagName))) {
    if (!state.retry) state.retry = setTimeout(() => { state.retry = 0; repaint(false); }, 1500);
    return;
  }
  state.key = key;
  render(state.root);
}

export default function register(h) {
  hub = h;
  hub.addStyle("desk.css");
  hub.addView({ id: "desk", title: "Сводка", subtitle: "картина по делам", icon: "board",
    render(root) {
      if (state.root !== root) root.addEventListener("focusout", () => setTimeout(() => repaint(false), 0));
      state.root = root;
      repaint(false);
    } });
  setInterval(() => repaint(false), 10000);   // картина приходит файлом, а не событием ядра
}
