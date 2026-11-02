#!/usr/bin/env python3
"""Run with python3 tests/tools/shares_test.py. The rules of shared chats: who gets in, how the one conversation of a link
is kept and shown, what a visitor may send, how the limits count, and that an older file is carried over."""

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("shares", Path(__file__).parents[2] / "tools/monitor/shares.py")
shares = importlib.util.module_from_spec(spec)
sys.modules["shares"] = shares
spec.loader.exec_module(shares)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = shares.ShareStore(":memory:", self.clock)
        self.addCleanup(self.store.close)

    def test_only_a_link_that_is_active_lets_someone_in(self):
        share = self.store.create("admin", "Demo", days=1)
        self.assertEqual(self.store.find(share["token"])["id"], share["id"])
        self.clock.now += 86400 + 1
        self.assertIsNone(self.store.find(share["token"]), "expired")
        revoked = self.store.create("admin", "Gone")
        self.assertEqual(self.store.remove("admin", revoked["id"]), "revoked")
        self.assertIsNone(self.store.find(revoked["token"]), "revoked")
        for token in ("", "short", "x" * 100, "../" + "a" * 40, None, 5, "a b" + "c" * 40):
            self.assertIsNone(self.store.find(token), repr(token))
        never = self.store.create("admin", "Always", days=0)
        self.clock.now += 10_000 * 86400
        self.assertIsNotNone(self.store.find(never["token"]))

    def test_the_secret_is_the_token_and_the_owner_handle_is_not(self):
        a, b = self.store.create("admin", "A"), self.store.create("admin", "B")
        self.assertNotEqual(a["token"], b["token"])
        self.assertTrue(shares.TOKEN.fullmatch(a["token"]))
        self.assertTrue(shares.SHARE_ID.fullmatch(a["id"]))
        self.assertIsNone(self.store.find(a["id"]), "the handle opens nothing")

    def test_messages_and_speech_are_counted_against_separate_limits_and_stop_at_them(self):
        share = self.store.create("admin", "Demo", max_messages=2, max_audio=3)
        token = share["token"]
        self.assertEqual([self.store.take_message(token) for _ in range(3)], [True, True, False])
        self.assertEqual([self.store.take_audio(token) for _ in range(4)], [True, True, True, False])
        listed = self.store.list("admin")[0]
        self.assertEqual((listed["messages"], listed["audio"]), (2, 3))
        self.store.refund_message(token)
        self.assertTrue(self.store.take_message(token), "a refused request does not cost a message")
        self.assertFalse(self.store.take_message(token))

    def test_a_revoked_or_expired_link_counts_nothing(self):
        share = self.store.create("admin", "Demo", days=1)
        self.clock.now += 2 * 86400
        self.assertFalse(self.store.take_message(share["token"]))
        self.assertFalse(self.store.take_audio(share["token"]))
        other = self.store.create("admin", "Other")
        self.store.remove("admin", other["id"])
        self.assertFalse(self.store.take_message(other["token"]))

    def test_revoking_then_removing_and_only_the_owner(self):
        share = self.store.create("admin", "Demo")
        self.assertIsNone(self.store.remove("someone", share["id"]))
        self.assertEqual(self.store.remove("admin", share["id"]), "revoked")
        self.assertEqual(self.store.list("admin")[0]["status"], "revoked")
        self.assertEqual(self.store.remove("admin", share["id"]), "removed")
        self.assertEqual(self.store.list("admin"), [])
        self.assertIsNone(self.store.remove("admin", share["id"]))

    def test_the_list_shows_every_link_with_its_state_newest_first(self):
        self.store.create("admin", "Old", days=1)
        self.clock.now += 100
        self.store.create("admin", "New")
        self.clock.now += 86400
        self.assertEqual([(s["title"], s["status"]) for s in self.store.list("admin")], [("New", "active"), ("Old", "expired")])
        self.assertEqual(self.store.list("nobody"), [])

    def test_what_a_visitor_is_told_holds_no_secret(self):
        share = self.store.create("admin", "Demo")
        view = self.store.view(self.store.find(share["token"]), "model-x", True, {"speed": 1.25})
        self.assertEqual((view["title"], view["model"], view["speed"]), ("Demo", "model-x", 1.25))
        for secret in (share["token"], share["id"], "admin"):
            self.assertNotIn(secret, repr(view))

    def test_limits_are_validated(self):
        for bad in ({"days": -1}, {"days": shares.MAX_DAYS + 1}, {"days": "7"}, {"days": True}, {"max_messages": 0},
                    {"max_messages": shares.MAX_LIMIT + 1}, {"max_audio": 1.5}, {"max_audio": None}):
            with self.assertRaises(shares.ShareError, msg=bad):
                self.store.create("admin", "x", **bad)
        self.assertEqual(self.store.create("admin", "  ")["title"], "Shared chat")
        self.assertEqual(len(self.store.create("admin", "y" * 500)["title"]), shares.TITLE_MAX)

    def test_a_chat_has_one_active_link_at_a_time(self):
        first = self.store.create("admin", "A", chat_id="chat-1")
        with self.assertRaises(shares.ShareError) as refused:
            self.store.create("admin", "B", chat_id="chat-1")
        self.assertEqual(refused.exception.status, 409)
        self.store.create("admin", "C", chat_id="chat-2")
        self.store.remove("admin", first["id"])
        self.assertEqual(self.store.create("admin", "D", chat_id="chat-1")["chat_id"], "chat-1")


class RoomTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = shares.ShareStore(":memory:", self.clock)
        self.addCleanup(self.store.close)
        self.share = self.store.create("admin", "Room", seed=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}])
        self.token = self.share["token"]

    def answer(self, name, text, reply):
        row = self.store.ask(self.token, name, text)
        self.store.extend_reply(self.token, row, reply)
        self.store.finish_reply(self.token, row)

    def contents(self):
        return [(m["role"], m["name"], m["content"]) for m in self.store.conversation(self.token)["messages"]]

    def test_everyone_sees_the_same_conversation_starting_with_the_owners_chat(self):
        self.answer("Ana", "hello", "hi Ana")
        self.answer("Boris", "hola", "hi Boris")
        self.assertEqual(self.contents(), [
            ("user", "Owner", "q"), ("assistant", "", "a"), ("user", "Ana", "hello"), ("assistant", "", "hi Ana"),
            ("user", "Boris", "hola"), ("assistant", "", "hi Boris")])
        self.assertEqual(len(self.store.transcript("admin", self.share["id"])), 6)
        self.assertIsNone(self.store.transcript("someone", self.share["id"]))

    def test_a_page_asks_for_changes_by_revision_and_is_told_when_there_are_none(self):
        first = self.store.conversation(self.token)
        self.assertEqual(self.store.conversation(self.token, first["rev"]), {"rev": first["rev"], "unchanged": True})
        row = self.store.ask(self.token, "Ana", "hello")
        asked = self.store.conversation(self.token, first["rev"])
        self.assertGreater(asked["rev"], first["rev"])
        self.assertEqual(asked["messages"][-1], {**asked["messages"][-1], "role": "assistant", "content": "", "done": False})
        self.store.extend_reply(self.token, row, "par")
        self.assertEqual(self.store.conversation(self.token, asked["rev"])["messages"][-1]["content"], "par",
                         "everyone watches the reply as it is written")
        self.store.extend_reply(self.token, row, "tial")
        self.store.finish_reply(self.token, row)
        done = self.store.conversation(self.token)["messages"][-1]
        self.assertEqual((done["content"], done["done"]), ("partial", True))
        self.assertIsNone(self.store.conversation("z" * 32))

    def test_one_reply_is_written_at_a_time_and_a_stale_one_is_given_up(self):
        self.store.ask(self.token, "Ana", "one")
        with self.assertRaises(shares.ShareError) as busy:
            self.store.ask(self.token, "Boris", "two")
        self.assertEqual(busy.exception.status, 409)
        self.assertNotIn("two", [c for _, _, c in self.contents()], "a refused message is not added")
        self.clock.now += shares.PENDING_S + 1
        self.store.ask(self.token, "Boris", "two")

    def test_a_reply_that_got_no_text_is_dropped(self):
        row = self.store.ask(self.token, "Ana", "hello")
        self.store.finish_reply(self.token, row)
        self.assertEqual([r for r, _, _ in self.contents()].count("assistant"), 1, "only the seed's answer remains")
        self.assertEqual(self.contents()[-1], ("user", "Ana", "hello"))

    def test_the_model_sees_the_conversation_with_names_once_more_than_one_person_has_spoken(self):
        alone = self.store.create("admin", "Alone")
        row = self.store.ask(alone["token"], "Ana", "hello")
        self.store.extend_reply(alone["token"], row, "hi Ana")
        self.store.finish_reply(alone["token"], row)
        self.assertEqual(self.store.context(alone["token"]), [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi Ana"}],
                         "one speaker needs no label")
        self.answer("Ana", "hello", "hi Ana")        # the room also has the owner's seed: two people
        self.store.ask(self.token, "Boris", "and me?")
        context = self.store.context(self.token)
        self.assertEqual(context[-1], {"role": "user", "content": "Boris: and me?"})
        self.assertIn({"role": "user", "content": "Ana: hello"}, context)
        self.assertEqual(context[0]["role"], "user", "it starts with a visitor's message")
        self.assertFalse(any(m["content"] == "" for m in context), "the unfinished reply is not in it")

    def test_the_model_sees_only_the_latest_messages_that_fit(self):
        for i in range(shares.CONTEXT_MESSAGES):
            self.answer("Ana", f"question {i}", f"answer {i}")
        context = self.store.context(self.token)
        self.assertLessEqual(len(context), shares.CONTEXT_MESSAGES)
        self.assertEqual(context[-1]["content"], f"answer {shares.CONTEXT_MESSAGES - 1}")
        self.assertEqual(context[0]["role"], "user")

    def test_a_conversation_shows_its_latest_messages_and_is_full_at_a_limit(self):
        for i in range(shares.SHOWN):
            self.answer("Ana", f"m{i}", "r")
        shown = self.store.conversation(self.token)["messages"]
        self.assertEqual(len(shown), shares.SHOWN)
        self.assertEqual(shown[-1]["role"], "assistant")
        small = shares.ShareStore(":memory:", self.clock)
        self.addCleanup(small.close)
        link = small.create("admin", "Full")
        for _ in range(shares.MAX_ROWS // 2):
            row = small.ask(link["token"], "Ana", "x")
            small.extend_reply(link["token"], row, "y")
            small.finish_reply(link["token"], row)
        with self.assertRaises(shares.ShareError) as full:
            small.ask(link["token"], "Ana", "x")
        self.assertEqual(full.exception.status, 403)

    def test_a_page_scrolls_back_through_the_history_a_page_at_a_time(self):
        link = self.store.create("admin", "Long", seed=[{"role": "user" if i % 2 == 0 else "assistant", "content": f"seed {i}"}
                                                       for i in range(500)])
        token = link["token"]
        latest = self.store.conversation(token)
        self.assertEqual((len(latest["messages"]), latest["more"], latest["messages"][-1]["content"]), (shares.SHOWN, True, "seed 499"))
        seen = [m["content"] for m in latest["messages"]]
        before = latest["messages"][0]["id"]
        while True:
            page = self.store.conversation(token, before=before)
            seen = [m["content"] for m in page["messages"]] + seen
            if not page["more"]:
                break
            before = page["messages"][0]["id"]
        self.assertEqual(seen, [f"seed {i}" for i in range(500)], "all of it, once, in order")
        small = self.store.conversation(self.token)
        self.assertFalse(small["more"])

    def test_removing_a_link_removes_its_conversation_but_revoking_keeps_it_for_the_owner(self):
        self.answer("Ana", "hello", "hi")
        self.assertEqual(self.store.remove("admin", self.share["id"]), "revoked")
        self.assertEqual(len(self.store.transcript("admin", self.share["id"])), 4)
        self.assertEqual(self.store.remove("admin", self.share["id"]), "removed")
        with self.store.lock:
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM share_messages").fetchone()[0], 0)


class MigrationTest(unittest.TestCase):
    def test_what_an_older_file_holds_is_carried_over_and_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite"
            old = sqlite3.connect(path)
            old.executescript(
                "CREATE TABLE shares (id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, title TEXT NOT NULL, "
                "created INTEGER NOT NULL, expires INTEGER NOT NULL, max_messages INTEGER NOT NULL, messages INTEGER NOT NULL "
                "DEFAULT 0, max_audio INTEGER NOT NULL, audio INTEGER NOT NULL DEFAULT 0, seed TEXT NOT NULL DEFAULT '', revoked "
                "INTEGER NOT NULL DEFAULT 0, last_used INTEGER NOT NULL DEFAULT 0, log INTEGER NOT NULL DEFAULT 0); "
                "CREATE TABLE share_log (id INTEGER PRIMARY KEY AUTOINCREMENT, share_id TEXT NOT NULL, at INTEGER NOT NULL, "
                "role TEXT NOT NULL, content TEXT NOT NULL, conversation TEXT NOT NULL DEFAULT '', turn TEXT NOT NULL DEFAULT ''); "
                "INSERT INTO shares VALUES ('aaaaaaaaaaaa', 'T' || printf('%031d', 1), 'admin', 'Old', 1, 0, 5, 0, 5, 0, '', 0, 0, 1); "
                "INSERT INTO share_log (share_id, at, role, content, conversation) VALUES "
                "('aaaaaaaaaaaa', 11, 'assistant', 'hi', 'c1'), ('aaaaaaaaaaaa', 10, 'user', 'hello', 'c1'), "
                "('zzzzzzzzzzzz', 12, 'user', 'orphan', 'c9');")
            old.commit()
            old.close()
            store = shares.ShareStore(path, Clock())
            self.addCleanup(store.close)
            self.assertEqual([(m["role"], m["content"]) for m in store.transcript("admin", "aaaaaaaaaaaa")],
                             [("user", "hello"), ("assistant", "hi")], "carried over, oldest first")
            with store.lock:
                tables = {row[0] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertNotIn("share_log", tables)
            self.assertEqual(store.list("admin")[0]["title"], "Old")
            reopened = shares.ShareStore(path, Clock())
            self.addCleanup(reopened.close)
            self.assertEqual(len(reopened.transcript("admin", "aaaaaaaaaaaa")), 2, "opening it again changes nothing")


class RequestTest(unittest.TestCase):
    def test_a_post_is_plain_text_with_a_clean_name_and_a_short_note(self):
        self.assertEqual(shares.clean_post({"text": "  Hi  ", "name": "  Ana\nthe\x00Great  ", "note": "n" * 5000}),
                         ("Hi", "Ana the Great", "n" * shares.NOTE_CHARS))
        self.assertEqual(shares.clean_post({"text": "Hi"}), ("Hi", "Guest", ""))
        self.assertEqual(shares.clean_post({"text": "Hi", "name": "x" * 100})[1], "x" * shares.NAME_CHARS)
        self.assertEqual(shares.clean_post({"text": "Hi", "name": 5, "note": 7}), ("Hi", "Guest", ""))
        for bad in (None, [], "text", {}, {"text": ""}, {"text": "  "}, {"text": 5}, {"text": ["a"]}):
            with self.assertRaises(shares.ShareError, msg=bad):
                shares.clean_post(bad)
        with self.assertRaises(shares.ShareError) as long:
            shares.clean_post({"text": "x" * (shares.MESSAGE_CHARS + 1)})
        self.assertEqual(long.exception.status, 413)

    def test_the_request_to_the_model_is_built_from_a_whitelist(self):
        request = shares.build_request([{"role": "user", "content": "Hi"}], "m", "Be brief.", "Reply in Bulgarian.")
        self.assertEqual(sorted(request), ["max_tokens", "messages", "model", "reasoning_effort", "stream", "stream_options"])
        self.assertEqual((request["model"], request["stream"], request["reasoning_effort"], request["max_tokens"]),
                         ("m", True, "none", shares.MAX_TOKENS))
        self.assertEqual(request["messages"][0], {"role": "system", "content": "Be brief.\n\nReply in Bulgarian."})
        self.assertEqual(shares.build_request([{"role": "user", "content": "Hi"}], "m", None)["messages"][0]["role"], "user",
                         "no system text, no system message")

    def test_the_seed_keeps_all_the_text_whole_and_starts_with_the_user(self):
        seed = shares.clean_seed([
            {"role": "assistant", "content": "orphan"}, {"role": "user", "content": "q", "images": ["x"]},
            {"role": "assistant", "content": "a", "reasoning": "secret thoughts", "steps": [{"x": 1}]},
            {"role": "assistant", "content": "oops", "error": True}, {"role": "system", "content": "hidden"},
            {"role": "user", "content": "  "}, {"role": "user", "content": [1]}])
        self.assertEqual(seed, [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}])
        self.assertEqual(shares.clean_seed(None), [])
        with self.assertRaises(shares.ShareError):
            shares.clean_seed("text")
        long = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 20_000} for i in range(150)]
        kept = shares.clean_seed(long)
        self.assertEqual((len(kept), kept[0]["content"], len(kept[149]["content"])), (150, long[0]["content"], len(long[149]["content"])),
                         "no message is dropped or cut: the room shows the whole chat")

    def test_a_chat_that_cannot_be_held_is_refused_and_not_cut(self):
        too_many = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x"} for i in range(shares.MAX_ROWS)]
        with self.assertRaises(shares.ShareError) as refused:
            shares.clean_seed(too_many)
        self.assertEqual(refused.exception.status, 413)
        too_big = [{"role": "user", "content": "x" * (1 << 20)}] * 5
        with self.assertRaises(shares.ShareError):
            shares.clean_seed(too_big)


if __name__ == "__main__":
    unittest.main()
