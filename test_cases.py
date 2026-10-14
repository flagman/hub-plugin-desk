"""Тесты процесса «картина по делам» (cases.py): HUB_REPO=<ядро> python3 -m unittest test_cases"""
import json
import os
import tempfile
import unittest

import cases as C

NOW = 1_800_000_000.0


def ev(seq, ts, type, entity=None, **data):
    return {"seq": seq, "ts": ts, "type": type, "entity": entity, "data": data}


PIC = {"changed": "c", "need": [{"case": "IT-1", "text": "решить X", "session": "maps"}], "ready": [],
       "cases": [{"id": "IT-1", "title": "t", "where": "w", "next": "n", "blocker": "", "sessions": ["maps"], "needs_owner": True}]}


class Parse(unittest.TestCase):
    def test_needs_owner_follows_need(self):
        # блокер «ждёт владельца» без пункта в need — флаг снимается; пункт need есть — флаг ставится
        text = json.dumps({"changed": "x", "need": [{"case": "A", "text": "да или нет"}, {"case": "B"}],
                           "cases": [{"id": "A", "needs_owner": False}, {"id": "C", "needs_owner": True, "blocker": "ждёт тебя"}]})
        d = C.parse("```json\n" + text + "\n```")
        self.assertEqual([c["needs_owner"] for c in d["cases"]], [True, False])
        self.assertEqual([n["case"] for n in d["need"]], ["A"])   # пункт без текста — выброшен
        self.assertEqual(d["ready"], [])

    def test_no_cases_is_error(self):
        with self.assertRaises(ValueError):
            C.parse('{"changed": "x"}')


class Step(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def call(self, system, text, account=None):
        self.calls.append((system, text))
        return PIC, 0.07

    def test_first_run_full(self):
        events = [ev(1, NOW - 100, "session.said", "maps", text="готово")]
        s = C.step({}, events, ["- maps: да или нет?"], NOW, call=self.call)
        self.assertEqual(s["picture"], PIC)
        self.assertIs(self.calls[0][0], C.PICTURE)
        self.assertIn("- maps: да или нет?", self.calls[0][1])
        self.assertIn("maps: готово", self.calls[0][1])
        self.assertEqual(s["full_at"], NOW)

    def test_update_only_new_and_after_gap(self):
        st = {"picture": PIC, "as_of": NOW - 1000, "tried": NOW - 1000, "full_at": NOW - 1000}
        old = ev(1, NOW - 2000, "session.said", "maps", text="старое")
        new = ev(2, NOW - 50, "session.said", "maps", text="новое")
        self.assertIs(C.step(st, [old], [], NOW, call=self.call), st)          # нового нет — без вызова
        s = C.step(st, [old, new], [], NOW, call=self.call)
        self.assertIs(self.calls[0][0], C.UPDATE)
        self.assertIn("новое", self.calls[0][1])
        self.assertNotIn("старое", self.calls[0][1])
        self.assertIn('"IT-1"', self.calls[0][1])                            # прошлая картина — в запросе
        self.assertEqual(s["as_of"], NOW)
        self.assertIs(C.step(s, [old, new, ev(3, NOW + 10, "session.said", "maps", text="ещё")], [], NOW + 60,
                             call=self.call), s)                              # раньше GAP — ждём
        self.assertEqual(len(self.calls), 1)

    def test_failure_keeps_picture(self):
        st = {"picture": PIC, "as_of": NOW - 1000, "tried": NOW - 1000, "full_at": NOW - 1000}

        def boom(*a, **k):
            raise ValueError("лимит")
        s = C.step(st, [ev(2, NOW - 50, "session.said", "maps", text="новое")], [], NOW, call=boom)
        self.assertEqual(s["picture"], PIC)
        self.assertEqual(s["as_of"], NOW - 1000)   # новое не потеряно: следующая попытка возьмёт его снова
        self.assertEqual(s["tried"], NOW)

    def test_full_rebuild_after_full_period(self):
        st = {"picture": PIC, "as_of": NOW - 1000, "tried": NOW - 1000, "full_at": NOW - C.FULL - 1}
        C.step(st, [], [], NOW, call=self.call)
        self.assertIs(self.calls[0][0], C.PICTURE)


class Facts(unittest.TestCase):
    def test_open_cards_from_snapshot(self):
        snap = {"sessions": [{"name": "maps", "pending": [{"tool": "AskUserQuestion",
                                                          "input": {"questions": [{"question": "Мержить?"}]}}]},
                             {"name": "idle", "pending": []}]}
        self.assertEqual(C.open_cards(snap), ["- maps: Мержить?"])

    def test_owner_words_kept_and_window(self):
        events = [ev(1, NOW - 10, "message.received", None, to="maps", text="делай", owner=True),
                  ev(2, NOW - 5, "session.said", "maps", text="сделал"),
                  ev(3, NOW - C.WINDOW - 5, "session.said", "maps", text="давно")]
        f = C.facts(events, NOW - C.WINDOW, NOW, [])
        self.assertIn("ВЛАДЕЛЕЦ → maps: делай", f)
        self.assertIn("maps: сделал", f)
        self.assertNotIn("давно", f)
        self.assertIn("ОТКРЫТЫЕ КАРТОЧКИ", f)


class Journal(unittest.TestCase):
    def write(self, path, events):
        with open(path, "a") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_archive_tail_and_rotation(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "journal-archive"))
        self.write(os.path.join(d, "journal-archive", "journal-1-10.jsonl"),
                   [ev(0, NOW - 9 * C.WINDOW, "core.snapshot"),                     # первая строка архива — снимок со старым ts
                    ev(1, NOW - C.WINDOW - 100, "session.said", "maps", text="за окном"),
                    ev(2, NOW - 3600, "session.said", "maps", text="в окне"),
                    ev(3, NOW - 3500, "turn.tool", "maps")])                       # не нужное — не берём
        self.write(os.path.join(d, "journal-archive", "journal-1-20.jsonl"),
                   [ev(0, NOW - 9 * C.WINDOW, "core.snapshot"), ev(5, NOW - 600, "session.said", "maps", text="новее")])
        self.write(os.path.join(d, "journal.jsonl"), [ev(11, NOW - 60, "session.said", "maps", text="хвост")])
        j = C.Journal(d, now=NOW)
        self.assertEqual([e["seq"] for e in j.events], [2, 5, 11])   # оба архива: снимок в начале не обрывает чтение
        self.write(os.path.join(d, "journal.jsonl"), [ev(12, NOW - 30, "message.received", None, to="maps", text="ещё")])
        j.poll(now=NOW)
        self.assertEqual([e["seq"] for e in j.events], [2, 5, 11, 12])
        os.replace(os.path.join(d, "journal.jsonl"), os.path.join(d, "journal-archive", "journal-11-12.jsonl"))
        self.write(os.path.join(d, "journal.jsonl"), [ev(13, NOW - 10, "session.said", "maps", text="новый файл")])
        j.poll(now=NOW)
        self.assertEqual([e["seq"] for e in j.events], [2, 5, 11, 12, 13])


if __name__ == "__main__":
    unittest.main()
