#!/usr/bin/env python3
"""Run with python3 tests/tools/shares_test.py. The rules of shared chats: who gets in, how the one conversation of a link
is kept and shown, what a visitor may send, how the limits count, and that an older file is carried over."""

import base64
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

    def test_a_room_starts_with_the_thinking_steps_and_stats_of_the_chat_replies_but_not_their_cost_or_figure_bytes(self):
        chat = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "a", "reasoning": "hm", "stats": {"tps": 40.0, "cost_micro": 9, "calls": 1},
                 "steps": [{"name": "run_python", "label": "Ran Python", "state": "ok", "excerpt": "1", "figures": ["data:image/png;base64,AA"],
                            "code": "print(1)"}, {"name": "library_admin", "label": "x", "state": "ok"}]}]
        share = self.store.create("admin", "Demo", seed=shares.clean_seed(chat), chat_id="c", base=2)
        reply = self.store.conversation(share["token"])["messages"][1]
        self.assertEqual(reply["reasoning"], "hm")
        self.assertEqual(reply["stats"], {"tps": 40.0, "calls": 1})
        self.assertEqual([(s["name"], s.get("figureCount"), "figures" in s) for s in reply["steps"]], [("run_python", 1, False)])

    def test_a_reply_seeded_while_it_was_being_written_is_completed_from_the_chat(self):
        partial = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "The answer is"}]
        share = self.store.create("admin", "Demo", seed=shares.clean_seed(partial), chat_id="c", base=2)
        finished = [partial[0], {"role": "assistant", "content": "The answer is 42.", "stats": {"tps": 9.5}}]
        self.store.refresh_seed("admin", share["id"], finished)
        reply = self.store.conversation(share["token"])["messages"][1]
        self.assertEqual((reply["content"], reply["stats"]), ("The answer is 42.", {"tps": 9.5}))
        self.store.refresh_seed("admin", share["id"], [partial[0], {"role": "assistant", "content": "Something else", "stats": {"tps": 1}}])
        self.assertEqual(self.store.conversation(share["token"])["messages"][1]["content"], "The answer is 42.")

    def test_a_room_made_before_replies_carried_their_thinking_takes_it_from_the_chat_once(self):
        old = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        share = self.store.create("admin", "Demo", seed=shares.clean_seed(old), chat_id="c", base=2)
        token = share["token"]
        self.assertNotIn("reasoning", self.store.conversation(token)["messages"][1])
        rev = self.store.conversation(token)["rev"]
        richer = [old[0], {**old[1], "reasoning": "hm"}]
        self.store.refresh_seed("admin", share["id"], richer)
        self.assertEqual(self.store.conversation(token)["messages"][1]["reasoning"], "hm")
        self.assertGreater(self.store.conversation(token)["rev"], rev)
        self.assertEqual(self.store.conversation(token)["messages"][1]["content"], "a")
        self.store.refresh_seed("admin", share["id"], [old[0], {**old[1], "reasoning": "other"}])
        self.assertEqual(self.store.conversation(token)["messages"][1]["reasoning"], "hm", "a reply that has its thinking keeps it")

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
        view = self.store.view(self.store.find(share["token"]), "model-x", True, {"speed": 1.25}, True, True)
        self.assertEqual((view["title"], view["model"], view["speed"]), ("Demo", "model-x", 1.25))
        for secret in (share["token"], share["id"], "admin"):
            self.assertNotIn(secret, repr(view))

    def test_a_room_offers_the_owners_tools_and_effort_the_library_only_where_there_is_one(self):
        share = self.store.create("admin", "Tools", tools=["read_library", "run_python", "web_search", "search_library", 5,
                                                           "list_library"], effort="high")
        self.assertEqual((share["tools"], share["effort"]),
                         (["web_search", "search_library", "read_library", "run_python"], "high"))
        found = self.store.find(share["token"])
        view = self.store.view(found, "m", False, {}, True, True)
        self.assertEqual((view["tools"], view["effort"], view["efforts"]),
                         (["web_search", "search_library", "read_library", "run_python"], "high", list(shares.EFFORTS)))
        self.assertEqual(self.store.view(found, "m", False, {}, False, True)["tools"],
                         ["web_search", "search_library", "read_library"], "no Python without its runtime")
        self.assertEqual(self.store.view(found, "m", False, {}, True, False)["tools"], ["web_search", "run_python"],
                         "no library tools without a library")
        self.assertEqual(shares.room_tools(found, False, False), ["web_search"])
        for bad in ({"tools": "web_search"}, {"effort": "turbo"}, {"effort": 1}):
            with self.assertRaises(shares.ShareError, msg=bad):
                self.store.create("admin", "x", **bad)

    def test_the_owner_changes_the_tools_and_effort_of_an_active_link_only(self):
        share = self.store.create("admin", "Tools", tools=["web_search"])
        changed = self.store.settings("admin", share["id"], ["fetch_url", "search_library", "save_library"], "max")
        self.assertEqual((changed["tools"], changed["effort"]), (["fetch_url", "search_library"], "max"))
        self.assertEqual(self.store.find(share["token"])["tools"], ["fetch_url", "search_library"])
        self.assertIsNone(self.store.settings("someone", share["id"], [], ""))
        with self.assertRaises(shares.ShareError):
            self.store.settings("admin", share["id"], [], "turbo")
        self.store.remove("admin", share["id"])
        self.assertIsNone(self.store.settings("admin", share["id"], [], ""), "revoked")

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
        self.store.end_round(self.token, row, False)

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
        self.store.end_round(self.token, row, False)
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
        self.store.end_round(self.token, row, False)
        self.assertEqual([r for r, _, _ in self.contents()].count("assistant"), 1, "only the seed's answer remains")
        self.assertEqual(self.contents()[-1], ("user", "Ana", "hello"))

    def test_the_model_sees_the_conversation_with_names_once_more_than_one_person_has_spoken(self):
        alone = self.store.create("admin", "Alone")
        row = self.store.ask(alone["token"], "Ana", "hello")
        self.store.extend_reply(alone["token"], row, "hi Ana")
        self.store.end_round(alone["token"], row, False)
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
            small.end_round(link["token"], row, False)
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


PNG = b"\x89PNG\r\n\x1a\n" + bytes(32)
FIGURE = "data:image/png;base64," + base64.b64encode(PNG).decode()
MEDIA = {"kind": "image", "title": " <i>An</i>\n owl ", "src": "x", "thumb": "https://thumb.wikimedia.org/a.jpg",
         "page": "https://commons.wikimedia.org/wiki/File:A.jpg", "license": "CC0", "author": None, "extra": "dropped"}


class Monotonic:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ReplyRoundsTest(unittest.TestCase):
    """A reply written over several model rounds, with the asker's tools run between them."""

    def setUp(self):
        self.clock = Clock()
        self.store = shares.ShareStore(":memory:", self.clock)
        self.addCleanup(self.store.close)
        self.token = self.store.create("admin", "Room", tools=["web_search"])["token"]
        self.reply = self.store.ask(self.token, "Ana", "find owls")
        self.turn = self.store.turn(self.reply)

    def last(self):
        return self.store.conversation(self.token)["messages"][-1]

    def test_only_the_asker_continues_their_reply_one_round_at_a_time(self):
        other = self.store.create("admin", "Other")["token"]
        for token, nonce in ((self.token, "x" * 24), (self.token, None), (self.token, "é"), (other, self.turn.nonce)):
            with self.assertRaises(shares.ShareError) as refused:
                self.store.begin_round(token, self.reply, nonce)
            self.assertEqual(refused.exception.status, 403, (token, nonce))
        with self.assertRaises(shares.ShareError) as busy:
            self.store.begin_round(self.token, self.reply, self.turn.nonce)
        self.assertEqual(busy.exception.status, 409, "the first round is still streaming")
        self.store.end_round(self.token, self.reply, True)
        self.assertEqual(self.store.begin_round(self.token, self.reply, self.turn.nonce).rounds, 1)
        self.store.end_round(self.token, self.reply, False)
        with self.assertRaises(shares.ShareError) as finished:
            self.store.begin_round(self.token, self.reply, self.turn.nonce)
        self.assertEqual(finished.exception.status, 403, "a finished reply has no turn")
        self.assertIsNone(self.store.turn(self.reply))

    def test_a_reply_has_at_most_max_rounds(self):
        for index in range(1, shares.MAX_ROUNDS):
            self.store.extend_reply(self.token, self.reply, f"r{index - 1}", separate=True)
            self.store.end_round(self.token, self.reply, True)
            self.assertEqual(self.store.begin_round(self.token, self.reply, self.turn.nonce).rounds, index)
        self.store.end_round(self.token, self.reply, True)
        with self.assertRaises(shares.ShareError):
            self.store.begin_round(self.token, self.reply, self.turn.nonce)

    def test_rounds_add_text_with_a_blank_line_and_reasoning_apart(self):
        self.store.extend_reply(self.token, self.reply, "", "think ", separate=True)
        self.store.extend_reply(self.token, self.reply, "Let me", "more", separate=True)
        self.store.extend_reply(self.token, self.reply, " look.")
        self.store.end_round(self.token, self.reply, True)
        self.store.begin_round(self.token, self.reply, self.turn.nonce)
        self.store.extend_reply(self.token, self.reply, "", "again", separate=True)  # no text yet: no blank line
        self.store.extend_reply(self.token, self.reply, "Found it.", separate=True)
        self.assertEqual((self.last()["content"], self.last()["reasoning"]), ("Let me look.\n\nFound it.", "think moreagain"))

    def test_a_reply_left_open_waits_for_its_tools_and_is_given_up_after_its_asker_goes_quiet(self):
        self.store.extend_reply(self.token, self.reply, "Let me look.")
        self.store.end_round(self.token, self.reply, True)
        self.assertFalse(self.last()["done"], "the model called tools: the reply waits for them")
        with self.assertRaises(shares.ShareError):
            self.store.ask(self.token, "Boris", "me too")
        # Every write keeps it alive: the time counts from the last one, not from the question.
        self.clock.now += shares.PENDING_S - 10
        self.store.set_steps(self.token, self.reply, self.turn.nonce, [{"name": "web_search", "label": "s", "state": "run",
                                                                        "excerpt": ""}])
        self.clock.now += shares.PENDING_S - 10
        with self.assertRaises(shares.ShareError):
            self.store.ask(self.token, "Boris", "me too")
        self.clock.now += 11
        rev = self.store.conversation(self.token)["rev"]
        self.store.ask(self.token, "Boris", "me too")
        given_up = [m for m in self.store.conversation(self.token)["messages"] if m["id"] == self.reply][0]
        self.assertEqual((given_up["done"], given_up["content"]), (True, "Let me look."))
        self.assertGreater(self.store.conversation(self.token)["rev"], rev)
        with self.assertRaises(shares.ShareError) as gone:
            self.store.begin_round(self.token, self.reply, self.turn.nonce)
        self.assertEqual(gone.exception.status, 403)

    def test_stats_are_summed_over_the_rounds_by_the_server(self):
        clock = Monotonic()
        turn = shares.Turn(self.token, clock)
        clock.now += 0.25
        turn.piece()
        turn.add_usage({"prompt_tokens": 100, "completion_tokens": 20, "completion_tokens_per_second": 40,
                        "prompt_tokens_details": {"cached_tokens": 60}}, None)
        turn.add_usage({"prompt_tokens": 150, "completion_tokens": 30, "cached_tokens": 90}, {"predicted_per_second": 60})
        turn.add_usage({"prompt_tokens": "lots", "completion_tokens": -5}, "x")
        turn.add_usage(None, None)
        self.assertEqual(turn.stats(2), {"tps": 50.0, "ttft_ms": 250, "prompt_tokens": 250, "cached_tokens": 150,
                                         "completion_tokens": 50, "calls": 2})
        # Without a decode rate, the pieces over the time since the first one.
        slow = shares.Turn(self.token, clock)
        for _ in range(11):
            slow.piece()
            clock.now += 0.5
        slow.add_usage({"prompt_tokens": 10, "completion_tokens": 11}, None)
        self.assertEqual(slow.stats(0), {"tps": 2.0, "ttft_ms": 0, "prompt_tokens": 10, "cached_tokens": 0, "completion_tokens": 11})
        self.assertEqual(shares.Turn(self.token).stats(1), {}, "no usage, no stats")
        # A finished reply keeps them, with its steps counted as calls.
        self.turn.add_usage({"prompt_tokens": 5, "completion_tokens": 3}, None)
        self.store.extend_reply(self.token, self.reply, "Done.")
        self.store.set_steps(self.token, self.reply, self.turn.nonce, *shares.clean_steps(
            [{"name": "web_search", "label": "a", "state": "ok"}, {"name": "web_search", "label": "b", "state": "bad"}],
            ["web_search"]))
        self.store.end_round(self.token, self.reply, False)
        stats = self.last()["stats"]
        self.assertEqual((stats["prompt_tokens"], stats["completion_tokens"], stats["calls"]), (5, 3, 2))

    def test_steps_replace_the_earlier_ones_and_their_figures_are_served_under_the_link(self):
        rev = self.store.conversation(self.token)["rev"]
        steps, figures = shares.clean_steps([{"name": "web_search", "label": "Searching", "state": "run",
                                              "figures": [FIGURE, FIGURE]}], ["web_search"])
        self.store.set_steps(self.token, self.reply, self.turn.nonce, steps, figures)
        shown = self.store.conversation(self.token, rev)
        self.assertGreater(shown["rev"], rev, "pages polling the room see the steps")
        self.assertEqual(shown["messages"][-1]["steps"][0]["figures"],
                         [f"/api/share/{self.token}/figure/{self.reply}/0/{n}.png" for n in (0, 1)])
        self.assertEqual(self.store.figure(self.token, self.reply, 0, 1), PNG)
        self.assertIsNone(self.store.figure(self.store.create("admin", "Other")["token"], self.reply, 0, 1),
                          "a figure is only reachable under its own link")
        self.store.set_steps(self.token, self.reply, self.turn.nonce,
                             *shares.clean_steps([{"name": "web_search", "label": "Searched", "state": "ok"}], ["web_search"]))
        self.assertEqual(self.last()["steps"], [{"name": "web_search", "label": "Searched", "state": "ok", "excerpt": ""}])
        self.assertIsNone(self.store.figure(self.token, self.reply, 0, 0), "replaced with the steps")
        self.store.set_steps(self.token, self.reply, self.turn.nonce, *shares.clean_steps(
            [{"name": "web_search", "label": "Drew", "state": "ok", "figures": [FIGURE]}], ["web_search"]))
        self.store.set_steps(self.token, self.reply, self.turn.nonce, None, abort=True)
        done = self.last()
        self.assertEqual((done["done"], done["content"], done["steps"][0]["label"]), (True, "", "Drew"),
                         "a stopped reply with steps but no text stays")
        self.assertEqual(self.store.transcript("admin", self.store.find(self.token)["id"])[-1]["steps"],
                         [{"name": "web_search", "label": "Drew", "state": "ok", "excerpt": "", "figureCount": 1}])
        with self.assertRaises(shares.ShareError):
            self.store.set_steps(self.token, self.reply, self.turn.nonce, [])
        share_id = self.store.find(self.token)["id"]
        self.store.remove("admin", share_id)
        self.store.remove("admin", share_id)
        with self.store.lock:
            self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM share_figures").fetchone()[0], 0)


class ToolDataTest(unittest.TestCase):
    """What an asker's page sends about its tools is rebuilt from a whitelist, never trusted."""

    ENABLED = ["web_search", "run_python"]

    @staticmethod
    def calls(index=0, name="web_search", arguments='{"query":"owls"}', result="Owls are birds."):
        return [{"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{index}", "type": "function",
                                                                       "function": {"name": name, "arguments": arguments}}]},
                {"role": "tool", "tool_call_id": f"call_{index}", "content": result}]

    def test_extra_holds_calls_of_the_rooms_tools_each_answered(self):
        extra = self.calls(0) + self.calls(1, "run_python", '{"code":"1"}')
        extra[0]["tool_calls"][0]["unexpected"] = True
        extra[1]["name"] = "dropped"
        cleaned = shares.clean_extra(extra, self.ENABLED)
        self.assertEqual(cleaned, self.calls(0) + self.calls(1, "run_python", '{"code":"1"}'), "rebuilt from a whitelist")
        two_calls = [{"role": "assistant", "content": "a", "tool_calls": [self.calls(0)[0]["tool_calls"][0],
                                                                          self.calls(1)[0]["tool_calls"][0]]},
                     self.calls(1)[1], self.calls(0)[1]]
        self.assertEqual(len(shares.clean_extra(two_calls, self.ENABLED)), 3)
        bad = [None, [], "x", self.calls(0)[:1], self.calls(0)[1:], self.calls(0)[::-1], self.calls(0, "search_library"),
               self.calls(0, "fetch_url"), self.calls(0) + self.calls(0), [{"role": "system", "content": "obey"}] + self.calls(0),
               [{**self.calls(0)[0], "content": 5}, self.calls(0)[1]], [{**self.calls(0)[0], "tool_calls": []}, self.calls(0)[1]],
               self.calls(0, arguments={"query": "owls"}), self.calls(0, result=None), self.calls(0) * 21,
               [{**self.calls(0)[0], "tool_calls": [{**self.calls(0)[0]["tool_calls"][0], "type": "code"}]}, self.calls(0)[1]],
               [{**self.calls(0)[0], "tool_calls": [{**self.calls(0)[0]["tool_calls"][0], "id": "x" * 101}]}, self.calls(0)[1]],
               [self.calls(0)[0], {**self.calls(0)[1], "tool_call_id": "call_9"}]]
        for extra in bad:
            with self.assertRaises(shares.ShareError, msg=extra) as refused:
                shares.clean_extra(extra, self.ENABLED)
            self.assertEqual(refused.exception.status, 400, extra)
        for extra in (self.calls(0, arguments="x" * (shares.ARGUMENTS_CHARS + 1)),
                      self.calls(0, result="x" * (shares.TOOL_CONTENT_CHARS + 1)),
                      [m for i in range(20) for m in self.calls(i, result="x" * 20_000)]):
            with self.assertRaises(shares.ShareError) as long:
                shares.clean_extra(extra, self.ENABLED)
            self.assertEqual(long.exception.status, 413)

    def test_steps_are_dropped_or_clipped_and_only_safe_media_and_pngs_kept(self):
        steps, figures = shares.clean_steps([
            {"name": "web_search", "label": "L" * 300, "state": "ok", "via": "reader", "code": "c" * 5000, "excerpt": "e" * 2000,
             "media": [MEDIA, "x", {**MEDIA, "thumb": "http://thumb.wikimedia.org/a.jpg"}], "figures": "nope", "secret": 1},
            {"name": "search_library", "label": "read your notes", "state": "ok"},
            {"name": "web_search", "label": "x", "state": "done"},
            {"name": "run_python", "label": 5, "state": "bad", "via": "direct", "code": 7, "excerpt": None,
             "figures": [FIGURE, "data:image/jpeg;base64,AAAA", "data:image/png;base64,AAAA", "data:image/png;base64,!!",
                         FIGURE, FIGURE, FIGURE]},
            "step"] + [{"name": "web_search", "label": str(i), "state": "run"} for i in range(20)], self.ENABLED)
        self.assertEqual(len(steps), shares.STEPS_MAX)
        self.assertEqual(steps[0], {"name": "web_search", "label": "L" * 200, "state": "ok", "via": "reader", "code": "c" * 4000,
                                    "excerpt": "e" * 1500, "media": [{"kind": "image", "title": "An owl",
                                    "src": "https://thumb.wikimedia.org/a.jpg", "thumb": "https://thumb.wikimedia.org/a.jpg",
                                    "page": "https://commons.wikimedia.org/wiki/File:A.jpg", "license": "CC0", "author": ""}]})
        self.assertEqual(steps[1], {"name": "run_python", "label": "", "state": "bad", "excerpt": "", "figures": 1},
                         "of its first four figures only one is a PNG")
        self.assertEqual(figures, [(1, 0, PNG)])
        with self.assertRaises(shares.ShareError):
            shares.clean_steps({"name": "web_search"}, self.ENABLED)

    def test_library_tools_count_where_the_room_offers_them_and_their_steps_show_only_text(self):
        enabled = ["search_library", "read_library"]
        extra = self.calls(0, "search_library", '{"query":"owls"}') + self.calls(1, "read_library", '{"slug":"owls"}')
        self.assertEqual(shares.clean_extra(extra, enabled), extra)
        with self.assertRaises(shares.ShareError):
            shares.clean_extra(self.calls(0, "list_library", "{}"), enabled)
        offered = shares.clean_offered([{"type": "function", "function": {"name": "read_library"}},
                                        {"type": "function", "function": {"name": "list_library"}}], enabled)
        self.assertEqual([tool["function"]["name"] for tool in offered], ["read_library"])
        steps, figures = shares.clean_steps([
            {"name": "search_library", "label": "Searched owls", "state": "ok", "excerpt": "e" * 2000, "media": [MEDIA],
             "figures": [FIGURE]},
            {"name": "list_library", "label": "Listed", "state": "ok"}], enabled)
        self.assertEqual((steps, figures), ([{"name": "search_library", "label": "Searched owls", "state": "ok",
                                              "excerpt": "e" * shares.EXCERPT_CHARS}], []))

    def test_figures_are_bounded_one_by_one_and_per_reply(self):
        big = b"\x89PNG\r\n\x1a\n" + bytes(shares.FIGURE_BYTES - 8)
        url = "data:image/png;base64," + base64.b64encode(big).decode()
        self.assertEqual(shares.figure_png(url), big)
        self.assertIsNone(shares.figure_png("data:image/png;base64," + base64.b64encode(big + b"x").decode()))
        self.assertIsNone(shares.figure_png("data:image/png;base64," + base64.b64encode(b"GIF89a").decode()))
        steps, figures = shares.clean_steps([{"name": "web_search", "label": "", "state": "ok", "figures": [url] * 4}] * 2,
                                            self.ENABLED)
        self.assertEqual(sum(len(png) for _, _, png in figures), 4 * len(big), "6 MiB in all: the fifth does not fit")
        self.assertEqual([step.get("figures") for step in steps], [4, None])

    def test_media_are_kept_only_on_the_wikimedia_hosts(self):
        video = {**MEDIA, "kind": "video", "src": "https://upload.wikimedia.org/v.webm"}
        self.assertEqual(shares.safe_media(video)["src"], "https://upload.wikimedia.org/v.webm")
        for bad in ({**MEDIA, "kind": "audio"}, {**video, "src": "https://thumb.wikimedia.org/v.webm"},
                    {**MEDIA, "thumb": "https://upload.wikimedia.org.evil.example/a.jpg"},
                    {**MEDIA, "thumb": "https://user@upload.wikimedia.org/a.jpg"},
                    {**MEDIA, "thumb": "https://upload.wikimedia.org@evil.example/a.jpg"},
                    {**MEDIA, "thumb": "https://upload.wikimedia.org\\@evil.example/a.jpg"},
                    {**MEDIA, "thumb": "https://upload.wikimedia.org:8443/a.jpg"},
                    {**MEDIA, "thumb": "javascript:alert(1)"}, {**MEDIA, "page": "https://evil.example/"}, None, "x"):
            self.assertIsNone(shares.safe_media(bad), bad)


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

    def test_a_file_from_before_room_tools_gains_their_columns_and_an_open_reply_is_given_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rooms.sqlite"
            old = sqlite3.connect(path)
            old.executescript(
                "CREATE TABLE shares (id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, owner TEXT NOT NULL, title TEXT NOT NULL, "
                "created INTEGER NOT NULL, expires INTEGER NOT NULL, max_messages INTEGER NOT NULL, messages INTEGER NOT NULL "
                "DEFAULT 0, max_audio INTEGER NOT NULL, audio INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0, "
                "last_used INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 0, chat_id TEXT NOT NULL DEFAULT '', "
                "base INTEGER NOT NULL DEFAULT 0, seeded INTEGER NOT NULL DEFAULT 0); "
                "CREATE TABLE share_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, share_id TEXT NOT NULL, at INTEGER NOT NULL, "
                "role TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', content TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 1); "
                "INSERT INTO shares (id, token, owner, title, created, expires, max_messages, max_audio) "
                "VALUES ('bbbbbbbbbbbb', 'U' || printf('%031d', 2), 'admin', 'Room', 1, 0, 5, 5); "
                "INSERT INTO share_messages (share_id, at, role, name, content, done) VALUES "
                "('bbbbbbbbbbbb', 999999, 'user', 'Ana', 'hello', 1), ('bbbbbbbbbbbb', 999999, 'assistant', '', 'hal', 0);")
            old.commit()
            old.close()
            store = shares.ShareStore(path, Clock())
            self.addCleanup(store.close)
            share = store.list("admin")[0]
            self.assertEqual((share["tools"], share["effort"]), ([], ""))
            token = "U" + "0" * 30 + "2"
            self.assertEqual([(m["content"], m["done"]) for m in store.conversation(token)["messages"]],
                             [("hello", True), ("hal", False)])
            store.ask(token, "Boris", "still there?")  # the open reply has no turn and no touch: it is given up
            self.assertEqual([m["done"] for m in store.conversation(token)["messages"]], [True, True, True, False])
            self.assertEqual(store.settings("admin", "bbbbbbbbbbbb", ["web_search"], "low")["tools"], ["web_search"])


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
        self.assertEqual(sorted(request), ["max_tokens", "messages", "model", "stream", "stream_options"],
                         "no effort chosen: the model's default; no tools offered")
        self.assertEqual((request["model"], request["stream"], request["max_tokens"]), ("m", True, shares.MAX_TOKENS))
        self.assertEqual(shares.MAX_TOKENS, 8192)
        self.assertEqual(request["messages"][0], {"role": "system", "content": "Be brief.\n\nReply in Bulgarian."})
        self.assertEqual(shares.build_request([{"role": "user", "content": "Hi"}], "m", None)["messages"][0]["role"], "user",
                         "no system text, no system message")
        tools = [{"type": "function", "function": {"name": "web_search", "description": "", "parameters": {}}}]
        full = shares.build_request([], "m", None, "", "Tools: search.", "low", tools)
        self.assertEqual((full["reasoning_effort"], full["tools"], full["messages"][0]["content"]), ("low", tools, "Tools: search."))

    def test_only_the_rooms_tools_are_offered_from_what_a_page_sends(self):
        enabled = ["web_search", "fetch_url"]
        offered = shares.clean_offered([
            {"type": "function", "function": {"name": "web_search", "description": "Search", "parameters": {"type": "object"},
                                              "strict": True}, "extra": 1},
            {"type": "function", "function": {"name": "search_library"}}, {"type": "function", "function": {"name": ["x"]}},
            {"type": "other", "function": {"name": "fetch_url"}}, {"type": "function", "function": {"name": "web_search"}},
            "fetch_url", {"type": "function", "function": {"name": "fetch_url", "description": 5}}], enabled)
        self.assertEqual(offered, [
            {"type": "function", "function": {"name": "web_search", "description": "Search", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "fetch_url", "description": "",
                                              "parameters": {"type": "object", "properties": {}}}}])
        self.assertEqual(shares.clean_offered(None, enabled), [])
        with self.assertRaises(shares.ShareError):
            shares.clean_offered({"web_search": 1}, enabled)
        with self.assertRaises(shares.ShareError) as long:
            shares.clean_offered([{"type": "function", "function": {"name": "web_search", "description": "x" * 20_001}}], enabled)
        self.assertEqual(long.exception.status, 413)
        self.assertEqual(shares.clean_effort(None), "")
        self.assertEqual(shares.clean_note("n" * 5000, shares.TOOL_NOTE_CHARS), "n" * shares.TOOL_NOTE_CHARS)

    def test_the_seed_keeps_all_the_text_whole_and_starts_with_the_user(self):
        seed = shares.clean_seed([
            {"role": "assistant", "content": "orphan"}, {"role": "user", "content": "q", "images": ["x"]},
            {"role": "assistant", "content": "a", "reasoning": "thoughts", "steps": [{"x": 1}]},
            {"role": "assistant", "content": "oops", "error": True}, {"role": "system", "content": "hidden"},
            {"role": "user", "content": "  "}, {"role": "user", "content": [1]}])
        self.assertEqual(seed, [{"role": "user", "content": "q"},
                                {"role": "assistant", "content": "a", "meta": {"reasoning": "thoughts"}}])
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
