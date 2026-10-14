"""Картина по делам: процесс плагина «Сводка» (run в манифесте).

Скрипт дочитывает журнал ядра (только реплики сессий, сообщения, вопросы и ответы — за 36 ч), открытые карточки берёт из
снимка ядра. Есть новое и с прошлого обновления прошло не меньше GAP — один вызов Sonnet: прошлая картина + новое →
новая картина (дела, а не сессии: что изменилось, что нужно от владельца, что готово к проверке, где каждое дело).
Раз в FULL часов — пересборка с нуля по всем фактам окна (пошаговое не копит ошибки). Итог — out/picture.json."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.environ.get("HUB_REPO") or ".")   # ядро хаба: HUB_REPO ставит hub-up процессу плагина

WINDOW = 36 * 3600
TICK = 15
GAP = int(os.environ.get("DESK_GAP", "600"))         # картину переписывать не чаще раза в GAP с
FULL = int(os.environ.get("DESK_FULL", str(6 * 3600)))   # с нуля — раз в FULL с
MODEL = os.environ.get("DESK_PICTURE_MODEL", "sonnet")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "picture.json")
KEEP = {"message.received", "session.said", "permission.requested", "permission.answered", "question.dismissed",
        "question.withdrawn", "session.waiting", "owner.decided", "delegation.created"}
TYPE = re.compile(rb'"type":\s*"([^"]+)"')

PICTURE = """Ты готовишь владельцу картину его дел по фактам ниже: журнал его флота агентов (сессии Claude Code) за 36 ч до момента T. Владелец теряет общую картину: где нужен именно он, а что идёт без него. Ему нужно понять это за минуту, не обходя сессии.

Дело — то, чем занимаются (задача, ключ Jira, MR, тема), а не сессия: одно дело часто ведут несколько сессий (фронт, аналитика, jira, исполнители) — объедини их в одно. Время в фактах — местное, «[дд.мм чч:мм]».

Ответ — строго JSON, без текста вокруг:
{"changed": "2–4 предложения: что существенно изменилось за последние часы и что из этого следует — связи между делами (что что разблокирует, что идёт само, кто кого ждёт)",
 "need": [{"case": "id дела", "text": "что решить владельцу, одним предложением", "why": "почему нужен именно он", "recommend": "что советуешь, если в фактах есть основание, иначе пусто", "session": "кому отвечать"}],
 "ready": [{"case": "id дела", "text": "что готово к проверке владельцем", "where": "где смотреть: адрес, MR, стенд — дословно из фактов", "session": "чья работа"}],
 "cases": [{"id": "ключ Jira, MR или короткий слаг", "title": "по-человечески, 3–6 слов", "where": "где дело сейчас", "next": "следующий шаг и кто его делает", "blocker": "что мешает или кого ждёт, иначе пусто", "sessions": ["имена сессий"], "needs_owner": true|false}]}

Правила:
- need — только то, что ждёт решения владельца СЕЙЧАС и ещё не решено: открытые карточки (список вверху — только они ещё висят; карточка в хронологии, которой нет в списке, уже закрыта: ответили или сессия пошла дальше), прямые вопросы ему в последних репликах, на которые он позже не ответил и сессия после не пошла дальше. Не решение владельца: предложения «скажешь — сделаю», «если нужно — могу», вопросы одной сессии другой или хабу, то, что он уже ответил (смотри его сообщения и ответы позже вопроса), то, что он сам отложил.
- case в need и ready — ровно тот же id, что у дела в cases.
- needs_owner=true у дела — только если его пункт есть в need; блокер «ждёт владельца» без пункта в need — противоречие, так не пиши.
- ready — только то, что сессия прямо сдала владельцу на проверку и он ещё не смотрел; адрес — только из фактов, не придумывай.
- Сверяй по времени: поздняя реплика отменяет раннюю (слили, выкатили, заменили — значит, сделано).
- Не выдумывай: нет факта — не пиши. Без номеров коммитов и путей.
- Дела без движения больше суток и без нужды во владельце — не включай. Не больше 12 дел, важные для владельца первыми.
- Пиши по-русски, просто, без жаргона хаба."""

UPDATE = PICTURE.replace(
    "Ты готовишь владельцу картину его дел по фактам ниже: журнал его флота агентов (сессии Claude Code) за 36 ч до момента T.",
    "Ты обновляешь картину дел владельца. Даны: ПРОШЛАЯ КАРТИНА (JSON) и НОВОЕ — факты журнала его флота агентов (сессии "
    "Claude Code) с прошлого обновления, плюс список открытых карточек на сейчас. Перепиши картину целиком: дела, где нового "
    "нет, оставь как были (если они ещё живы), изменившиеся — обнови, новые — добавь, законченные больше суток назад — "
    "убери. «changed» — что изменилось с прошлого обновления и что из этого следует.")


TZ = None   # пояс владельца (state.owner.tz) — сервер может жить в UTC, а владелец читает своё время


def hm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")


def clip(t, n):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    return t if len(t) <= n else t[:n] + "…"


def line(e):
    """Факт одной строкой: (сессия, строка) или (None, None)."""
    d, t, s = e.get("data") or {}, e.get("type"), e.get("entity")
    w = hm(e["ts"])
    if t == "message.received":
        who = "ВЛАДЕЛЕЦ" if d.get("owner") else (d.get("sender") or "?")
        return d.get("to"), f"[{w}] {who} → {d.get('to')}: {clip(d.get('text'), 700 if d.get('owner') else 400)}"
    if t == "session.said":
        return s, f"[{w}] {s}: {clip(d.get('text'), 900)}"
    if t == "permission.requested":
        qs = (d.get("input") or {}).get("questions") or []
        if d.get("tool") == "AskUserQuestion" and qs:
            q = qs[0]
            opts = "; ".join(o.get("label", "") for o in q.get("options") or [])
            return s, f"[{w}] {s} спросил владельца карточкой: {clip(q.get('question'), 700)} Варианты: {clip(opts, 300)}"
        return s, f"[{w}] {s} просит разрешение на {d.get('tool')}"
    if t == "permission.answered":
        txt = "; ".join(str(v) for v in (d.get("answers") or {}).values()) or d.get("behavior")
        return s, f"[{w}] ВЛАДЕЛЕЦ ответил {s} на карточку: {clip(txt, 300)}"
    if t in ("question.dismissed", "question.withdrawn"):
        return s, f"[{w}] карточку {s} сняли"
    if t == "session.waiting":
        return s, f"[{w}] {s} отметила, чего ждёт: {clip(d.get('text'), 300)}"
    if t == "owner.decided":
        return d.get("sender"), f"[{w}] решение владельца (записала {d.get('sender')}): {clip(d.get('text'), 300)}"
    if t == "delegation.created":
        return d.get("session"), f"[{w}] поручение {d.get('session')}: {clip(d.get('context'), 400)}"
    return None, None


def open_cards(snap):
    """Открытые карточки вопросов — из снимка ядра (pending), а не по журналу: ядро снимает вопрос, как только сессия
    вышла из диалога, и событием это не пишется (первый прогон 14.10 видел 50–270 фантомных карточек)."""
    out = []
    for x in snap.get("sessions") or []:
        for a in x.get("pending") or []:
            qs = (a.get("input") or {}).get("questions") or []
            text = qs[0].get("question") if qs else f"разрешение на {a.get('tool')}"
            out.append(f"- {x['name']}: {clip(text, 400)}")
    return out


def facts(events, lo, hi, cards, cap=110000):
    """Факты по сессиям в окне [lo, hi): у каждой хронология, свежие сессии первыми; сверху — открытые карточки."""
    by = {}
    for e in events:
        if lo <= e["ts"] < hi and e.get("type") in KEEP:
            s, l = line(e)
            if s and l:
                by.setdefault(s, []).append((e["ts"], e["type"], l))
    parts = []
    for s, items in sorted(by.items(), key=lambda kv: -kv[1][-1][0]):
        own = [x for x in items if "ВЛАДЕЛ" in x[2]]
        rest = [x for x in items if "ВЛАДЕЛ" not in x[2]]
        said = [x for x in rest if x[1] == "session.said"][-4:]
        other = [x for x in rest if x[1] != "session.said"][-5:]
        parts.append(f"### {s}\n" + "\n".join(x[2] for x in sorted(own[-8:] + said + other)))
    head = f"ОТКРЫТЫЕ КАРТОЧКИ ВОПРОСОВ ВЛАДЕЛЬЦУ на момент {hm(hi)}:\n" + ("\n".join(cards) or "нет")
    return (head + "\n\n" + "\n\n".join(parts))[:cap]


def parse(text):
    t = str(text or "")
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise ValueError(f"нет JSON: {t[:200]}")
    d = json.loads(t[a:b + 1])
    if not isinstance(d.get("cases"), list):
        raise ValueError("нет cases")
    for k in ("need", "ready"):
        d[k] = [i for i in d.get(k) or [] if isinstance(i, dict) and i.get("text")]
    need = {n.get("case") for n in d["need"]}
    for c in d["cases"]:   # блокер «ждёт владельца» без пункта в need — не его: решает need, а не флаг
        c["needs_owner"] = c.get("id") in need
    d["changed"] = str(d.get("changed") or "")
    return d


def model_env(account):
    from core import catalog
    from modules.accounts import accounts
    base = {**os.environ, **catalog.user_env()}
    return accounts.model_env(account, {k: v for k, v in base.items() if not k.startswith("HUB_")})


def write(system, text, account=None):
    """Картина одним вызовом `claude -p` без инструментов: (картина, цена $). Оборванный JSON — до трёх попыток."""
    if os.environ.get("HUB_NO_MODEL"):
        raise ValueError("модель выключена (HUB_NO_MODEL)")
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    err, cost = None, 0
    for _ in range(3):
        r = subprocess.run([claude, "-p", "--model", MODEL, "--tools", "", "--setting-sources", "", "--strict-mcp-config",
                            "--no-session-persistence", "--output-format", "json", "--system-prompt", system],
                           input=text, capture_output=True, text=True, timeout=600, cwd=tempfile.gettempdir(),
                           env=model_env(account))
        try:
            res = json.loads(r.stdout)
            cost += res.get("total_cost_usd") or 0
            if res.get("is_error"):
                raise ValueError(str(res.get("result"))[:200])
            return parse(res.get("result")), cost
        except (ValueError, json.JSONDecodeError) as e:
            err = e
    raise ValueError(f"картина не собралась: {err}")


def step(state, events, cards, now, account=None, call=write):
    """Один такт: новое в журнале с прошлого обновления и прошло ≥ GAP — переписать картину. Отдаёт новое состояние
    (или то же, если ничего не делали). Сбой — картина прежняя, следующая попытка через GAP."""
    last = state.get("as_of") or 0
    if now - (state.get("tried") or 0) < GAP:
        return state
    fresh = [e for e in events if e["ts"] >= last and e.get("type") in KEEP]
    full = not state.get("picture") or now - (state.get("full_at") or 0) >= FULL
    if not fresh and not full:
        return state
    try:
        if full:
            pic, cost = call(PICTURE, f"Момент T: {hm(now)}.\n\n" + facts(events, now - WINDOW, now, cards), account)
        else:
            pic, cost = call(UPDATE, f"Сейчас: {hm(now)}. Прошлое обновление: {hm(last)}.\n\nПРОШЛАЯ КАРТИНА:\n"
                             + json.dumps(state["picture"], ensure_ascii=False) + "\n\nНОВОЕ:\n"
                             + facts(events, last, now, cards, cap=60000), account)
    except Exception as e:   # noqa: BLE001 — модель, сеть, лимит: картина остаётся прежней
        print(f"desk: картина не обновилась: {str(e)[:200]}", file=sys.stderr, flush=True)
        return {**state, "tried": now}
    print(f"desk: картина {'с нуля' if full else 'обновлена'} (${cost or 0:.3f})", file=sys.stderr, flush=True)
    return {"picture": pic, "as_of": now, "tried": now, "full_at": now if full else state.get("full_at"),
            "cost": cost}


class Journal:
    """Нужные события журнала ядра за окно: при старте — хвост и архивы назад до начала окна, потом дочитывает новое."""

    def __init__(self, state_dir, now=None):
        self.path = os.path.join(state_dir, "journal.jsonl")
        self.events, self.offset, self.ino = [], 0, None
        lo = (now or time.time()) - WINDOW
        arch = os.path.join(state_dir, "journal-archive")
        files = sorted((f for f in os.listdir(arch) if re.fullmatch(r"journal-\d+-\d+\.jsonl", f)),
                       key=lambda f: int(f.split("-")[2].split(".")[0]), reverse=True) if os.path.isdir(arch) else []
        old = []
        for f in files[:60]:   # новые первыми, пока не дошли до начала окна; время — по событиям (первая строка
            got = self._read(os.path.join(arch, f), 0)[0]   # архива — снимок состояния со старым ts)
            old = got + old
            if got and min(e["ts"] for e in got) < lo:
                break
        self.events = [e for e in old if e["ts"] >= lo]
        self.poll()

    @staticmethod
    def _read(path, offset):
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except FileNotFoundError:
            return [], offset
        end = chunk.rfind(b"\n")
        if end < 0:
            return [], offset
        out = []
        for raw in chunk[:end].split(b"\n"):
            m = TYPE.search(raw[:300])
            if not m or m.group(1).decode() not in KEEP:
                continue
            try:
                out.append(json.loads(raw))
            except ValueError:
                pass
        return out, offset + end + 1

    def poll(self, now=None):
        try:
            ino = os.stat(self.path).st_ino
        except FileNotFoundError:
            return False
        if self.ino is not None and ino != self.ino:   # журнал начат заново (ротация): новый файл — с начала
            self.offset = 0
        self.ino = ino
        got, self.offset = self._read(self.path, self.offset)
        seen = {e.get("seq") for e in self.events[-2000:]}
        self.events += [e for e in got if e.get("seq") not in seen]
        lo = (now or time.time()) - WINDOW
        if self.events and self.events[0]["ts"] < lo:
            self.events = [e for e in self.events if e["ts"] >= lo]
        return bool(got)


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    from core import tail
    from core.daemon import default_state_dir
    state_dir = os.environ.get("HUB_STATE_DIR") or default_state_dir()
    t = tail.Tail(os.path.join(state_dir, "journal.jsonl"))
    j = Journal(state_dir)
    state = load(OUT)
    global TZ
    while True:
        t.poll()
        try:
            from zoneinfo import ZoneInfo
            TZ = ZoneInfo((t.reader.state.get("owner") or {}).get("tz") or "UTC")
        except Exception:   # noqa: BLE001 — неизвестный пояс: время сервера
            TZ = None
        j.poll()
        snap = t.snapshot
        account = (snap.get("account") or {}).get("active")
        new = step(state, j.events, open_cards(snap), time.time(), account)
        if new is not state:
            state = new
            save(OUT, state)
        time.sleep(TICK)


if __name__ == "__main__":
    main()
