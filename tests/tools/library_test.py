"""Tests for tools/monitor/library.py, library_mcp.py and their routes in gufo-monitor.py.

Run: cd tests/tools && python3 -m unittest library_test
"""

import base64
import http.client
import importlib.util
import io
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
MONITOR = ROOT / "tools/monitor"
sys.path.insert(0, str(MONITOR))
import library  # noqa: E402
import library_mcp  # noqa: E402

SPEC = importlib.util.spec_from_file_location("gufo_monitor", MONITOR / "gufo-monitor.py")
gm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gm)

ADMIN_TOKEN = "admin-token-for-library-tests"


def model_says(**answer):
    """A COMPLETE callable that always answers with ANSWER as JSON."""
    return lambda messages: json.dumps(answer)


def ingest(lib, text, complete=None, **request):
    """File TEXT without asking a model unless COMPLETE is given."""
    request = {"text": text, "digest": complete is not None, **request}
    return lib.ingest(request, complete, lambda _: None)


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.lib = library.Library(self.dir)
        self.addCleanup(self.lib.close)

    def body(self, slug):
        return self.lib.get(slug)["body"]

    def test_a_new_page_links_to_existing_pages_and_existing_pages_link_to_it(self):
        ingest(self.lib, "Notes on Alpha Centauri and nothing else.", title="Alpha Centauri")
        ingest(self.lib, "Beta Orbit is where we go. Alpha Centauri is far.", title="Beta Orbit")
        # the new page links out to the page that already existed ...
        self.assertIn("[[alpha-centauri|Alpha Centauri]]", self.body("beta-orbit"))
        ingest(self.lib, "Later we learn that Beta Orbit is round.", title="Gamma")
        # ... and a page filed later links back in to both, without touching the first.
        self.assertIn("[[beta-orbit|Beta Orbit]]", self.body("gamma"))
        # An older page learns about a page filed after it.
        ingest(self.lib, "Delta Wave is new.", title="Delta Wave")
        ingest(self.lib, "Alpha Centauri also has a Delta Wave.", title="Epsilon")
        self.assertIn("[[delta-wave|Delta Wave]]", self.body("epsilon"))
        page = self.lib.read("alpha-centauri")
        self.assertEqual({b["slug"] for b in page["backlinks"]}, {"beta-orbit", "epsilon"})

    def test_an_older_page_gets_links_to_a_page_added_later(self):
        ingest(self.lib, "We study the Mars Rover and its wheels.", title="Field notes")
        ingest(self.lib, "The Mars Rover is a robot.", title="Mars Rover")
        self.assertIn("[[mars-rover|Mars Rover]]", self.body("field-notes"))
        self.assertEqual([b["slug"] for b in self.lib.read("mars-rover")["backlinks"]], ["field-notes"])

    def test_linking_skips_code_headings_links_and_later_mentions_and_is_stable(self):
        ingest(self.lib, "A flux capacitor.", title="Flux Capacitor")
        text = ("# About Flux Capacitor\n\nFirst Flux Capacitor here, then another Flux Capacitor.\n\n"
                "`Flux Capacitor` in code\n\n```\nFlux Capacitor fenced\n```\n\n"
                "[Flux Capacitor](http://example.org/flux-capacitor)\n")
        ingest(self.lib, text, title="Other")
        body = self.body("other")
        self.assertEqual(body.count("[[flux-capacitor|Flux Capacitor]]"), 1)
        self.assertIn("# About Flux Capacitor\n", body)  # heading untouched
        self.assertIn("First [[flux-capacitor|Flux Capacitor]] here", body)
        self.assertIn("`Flux Capacitor` in code", body)
        self.assertIn("Flux Capacitor fenced", body)
        self.assertEqual(self.lib.relink(), [])

    def test_a_page_is_not_linked_to_itself_and_ambiguous_names_are_left_alone(self):
        ingest(self.lib, "Apple is a fruit; an apple a day.", title="Apple")
        self.assertNotIn("[[", self.body("apple"))
        # Two pages answering to one name: no link guesses between them.
        ingest(self.lib, "Text.", title="Mercury Planet", tags=[])
        self.lib.save("mercury-metal", "---\ntitle: Mercury Metal\naliases: [\"Mercury\"]\n---\nA metal.")
        self.lib.save("mercury-planet", "---\ntitle: Mercury Planet\naliases: [\"Mercury\"]\n---\nA planet.")
        ingest(self.lib, "We talk about Mercury only.", title="Talk")
        self.assertNotIn("[[", self.body("talk"))

    def test_the_same_document_replaces_itself_and_concepts_gain_each_source_once(self):
        answer = model_says(title="Qubits", summary="About qubits.", aliases=[], tags=["Physics"],
                            concepts=[{"name": "Qubit", "aliases": ["quantum bit"],
                                       "definition": "A two-level system."}])
        first = ingest(self.lib, "A qubit stores one bit.", answer, filename="a.txt")
        again = ingest(self.lib, "A qubit stores one bit.", answer, filename="a.txt")
        self.assertEqual((first["slug"], again["slug"], again["replaced"]), ("qubits", "qubits", True))
        other = ingest(self.lib, "Another text about a quantum bit.", model_says(
            title="More qubits", summary="", aliases=[], tags=[],
            concepts=[{"name": "Qubit", "aliases": [], "definition": "ignored: page exists"}]))
        concept = self.lib.get("qubit")
        self.assertEqual(concept["kind"], "concept")
        self.assertEqual(concept["body"].count("[[qubits|Qubits]]"), 1)
        self.assertIn(f"[[{other['slug']}|More qubits]]", concept["body"])
        self.assertNotIn("ignored", concept["body"])
        self.assertEqual(self.lib.list(kind="source")["total"], 2)
        self.assertEqual(self.lib.get("qubits")["meta"]["tags"], ["physics"])

    def test_a_model_that_fails_still_files_the_document(self):
        def broken(messages):
            raise library.DigestError("model is down")
        log = []
        result = self.lib.ingest({"text": "# Plain\nSome text.", "digest": True}, broken, log.append)
        self.assertEqual(result["slug"], "plain")
        self.assertTrue(any("model is down" in line for line in log))
        self.assertEqual(self.lib.get("plain")["meta"]["kind"], "source")

    def test_clean_digest_accepts_fenced_json_and_clamps_the_rest(self):
        got = library.clean_digest('Sure!\n```json\n{"title": "T", "summary": "S", "tags": ["A B", 3], '
                                   '"concepts": [{"name": "N", "definition": "D"}, {"name": ""}, 7]}\n```')
        self.assertEqual((got["title"], got["tags"], got["concepts"]),
                         ("T", ["a-b", "3"], [{"name": "N", "definition": "D", "aliases": []}]))
        for bad in ("no json here", "[1, 2]", "{broken"):
            with self.assertRaises(library.DigestError):
                library.clean_digest(bad)

    def test_search_needs_every_word_first_then_any_and_snippets_are_clean_text(self):
        ingest(self.lib, "Cats sleep. Dogs bark loudly.", title="Pets")
        ingest(self.lib, "Dogs only.", title="Dogs")
        both = self.lib.search("cats dogs")
        self.assertEqual([r["slug"] for r in both], ["pets"])
        anyword = self.lib.search("cats zebra")
        self.assertEqual([r["slug"] for r in anyword], ["pets"])
        self.assertEqual(self.lib.search("dog"), self.lib.search("dogs"))  # stemmed
        self.assertEqual(self.lib.search("nothing-like-it"), [])
        self.assertEqual(self.lib.search("AND OR NOT ( \"quote"), [])  # operators are just words
        ingest(self.lib, "The Dogs page says more.", title="Notes")
        for result in self.lib.search("dogs"):
            self.assertNotIn("[[", result["snippet"])

    def test_a_search_offset_opens_its_section_and_reads_page_through(self):
        sections = "".join(f"## Part {i}\n{'word ' * 600}\n\n" for i in range(4))
        sections += "## Hidden\nThe zebra crossing is here.\n"
        ingest(self.lib, "# Big\n" + sections, title="Big")
        hit = self.lib.search("zebra")[0]
        self.assertEqual(hit["heading"], "Hidden")
        page = self.lib.read("big", hit["offset"], 200)
        self.assertTrue(page["text"].startswith("## Hidden"))
        first = self.lib.read("big", 0, 1000)
        self.assertEqual(first["next_offset"], 1000)
        tail = self.lib.read("big", page["total"] - 10, 1000)
        self.assertIsNone(tail["next_offset"])

    def test_hand_edited_files_are_picked_up_and_removed_ones_dropped(self):
        (Path(self.dir) / "pages" / "handmade.md").write_text(
            "---\ntitle: Handmade\n---\n# Handmade\nA sprocket story with [[Other Page]].", encoding="utf-8")
        self.lib.sync()
        self.assertEqual(self.lib.search("sprocket")[0]["slug"], "handmade")
        self.assertEqual(self.lib.read("handmade")["links"], [])  # no such page yet
        (Path(self.dir) / "pages" / "handmade.md").unlink()
        self.lib.sync()
        self.assertEqual(self.lib.search("sprocket"), [])

    def test_deleting_a_page_turns_links_to_it_into_plain_text(self):
        ingest(self.lib, "About Widgets.", title="Widgets")
        ingest(self.lib, "We sell Widgets daily.", title="Shop")
        self.assertIn("[[widgets|Widgets]]", self.body("shop"))
        self.assertTrue(self.lib.delete("widgets"))
        self.assertEqual(self.body("shop"), "# Shop\n\nWe sell Widgets daily.")
        self.assertFalse(self.lib.delete("widgets"))
        self.assertIsNone(self.lib.read("widgets"))

    def test_slugs_cannot_leave_the_pages_directory(self):
        for slug in ("../index", "a/b", ".hidden", "", "x" * 81, "a.b"):
            with self.assertRaises(library.LibraryError, msg=slug):
                self.lib.save(slug, "x")
            self.assertIsNone(self.lib.get(slug) if library.SLUG_RE.fullmatch(slug) else None)
        outside = Path(self.dir) / "secret.txt"
        outside.write_text("top secret zebra")
        (Path(self.dir) / "pages" / "link.md").symlink_to(outside)
        self.lib.sync()
        self.assertEqual(self.lib.search("zebra"), [])

    def test_saving_a_page_keeps_its_kind_and_links_its_mentions(self):
        ingest(self.lib, "Moon base plans.", title="Moon Base")
        self.lib.save("journal", "Visited the Moon Base today.")
        self.assertIn("[[moon-base|Moon Base]]", self.body("journal"))
        self.assertEqual(self.lib.get("journal")["title"], "journal")
        self.lib.save("moon-base", "---\ntitle: Moon Base\naliases: [\"Luna Outpost\"]\n---\nRevised.")
        ingest(self.lib, "The Luna Outpost is open.", title="News")
        self.assertIn("[[moon-base|Luna Outpost]]", self.body("news"))
        self.assertEqual(self.lib.get("moon-base")["meta"]["kind"], "source")  # kept from before

    def test_titles_in_other_scripts_get_usable_slugs(self):
        ingest(self.lib, "Текст за квантови компютри.", title="Квантови компютри")
        self.assertEqual(self.lib.list()["pages"][0]["slug"], "квантови-компютри")
        self.assertEqual(self.lib.search("квантови")[0]["slug"], "квантови-компютри")

    def test_a_pdf_is_read_reflowed_and_kept(self):
        if shutil.which("pdftotext") is None:
            self.skipTest("pdftotext is not installed")
        result = self.lib.ingest({"pdf": make_pdf("The ocelot hunts at night."), "filename": "wild_cats.pdf",
                                  "digest": False}, None, lambda _: None)
        self.assertEqual(result["title"], "wild cats")
        self.assertEqual(self.lib.search("ocelot")[0]["slug"], "wild-cats")
        self.assertEqual(len(list((Path(self.dir) / "sources").glob("*.pdf"))), 1)
        with self.assertRaises(library.LibraryError) as caught:
            self.lib.ingest({"pdf": b"not a pdf", "digest": False}, None, lambda _: None)
        self.assertEqual(caught.exception.status, 422)

    def test_reflow_joins_wrapped_lines_and_hyphenation(self):
        self.assertEqual(library.reflow("A long sen-\ntence wraps\nover lines.\n\nNext Para-\nGraph\n\f\nPage two"),
                         "A long sentence wraps over lines.\n\nNext Para- Graph\n\nPage two")


def make_pdf(text):
    """A one-page PDF whose only content is TEXT (poppler rebuilds the missing xref)."""
    stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET"
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               "/Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


class McpTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.lib = library.Library(self.dir)
        self.addCleanup(self.lib.close)
        ingest(self.lib, "# Rockets\nLiquid fuel engines.\n\n## Staging\nStages drop away.", title="Rockets")

    def rpc(self, method, params=None, id=1):
        status, body = library_mcp.handle_body(json.dumps(
            {"jsonrpc": "2.0", "id": id, "method": method, **({"params": params} if params else {})}).encode(),
            self.lib)
        return status, json.loads(body)

    def test_the_handshake_lists_tools_and_negotiates_the_version(self):
        _, answer = self.rpc("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(answer["result"]["protocolVersion"], "2025-03-26")
        _, answer = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(answer["result"]["protocolVersion"], library_mcp.LATEST)
        _, answer = self.rpc("tools/list")
        self.assertEqual([t["name"] for t in answer["result"]["tools"]],
                         ["search_library", "read_library", "list_library"])
        status, body = library_mcp.handle_body(b'{"jsonrpc":"2.0","method":"notifications/initialized"}', self.lib)
        self.assertEqual((status, body), (202, None))

    def test_tools_search_read_and_report_their_own_mistakes_as_results(self):
        _, answer = self.rpc("tools/call", {"name": "search_library", "arguments": {"query": "stages"}})
        text = answer["result"]["content"][0]["text"]
        self.assertIn("[rockets] Rockets — Staging", text)
        _, answer = self.rpc("tools/call", {"name": "read_library", "arguments": {"slug": "rockets", "limit": 10}})
        self.assertIn("[more: read again with offset 10", answer["result"]["content"][0]["text"])
        _, answer = self.rpc("tools/call", {"name": "read_library", "arguments": {"slug": "missing"}})
        self.assertTrue(answer["result"]["isError"])
        _, answer = self.rpc("tools/call", {"name": "search_library", "arguments": {"query": ""}})
        self.assertTrue(answer["result"]["isError"])
        _, answer = self.rpc("tools/call", {"name": "delete_everything", "arguments": {}})
        self.assertEqual(answer["error"]["code"], -32602)
        self.assertEqual(self.rpc("nonsense")[1]["error"]["code"], -32601)
        self.assertEqual(library_mcp.handle_body(b"{nope", self.lib)[0], 400)


class LibraryHttpTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.lib = library.Library(self.dir)
        self.addCleanup(self.lib.close)
        monitor = gm.Monitor(gm.GufoClient("http://127.0.0.1:9", None), NoTelemetry())
        self.monitor = monitor
        self.server = gm.Server(("127.0.0.1", 0), monitor, gm.AdminAuth(ADMIN_TOKEN), False,
                                chat_key="inference-key", library=self.lib)
        thread = __import__("threading").Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.port = self.server.server_address[1]
        self.token = self.server.library_token

    def call(self, method, path, body=None, token=ADMIN_TOKEN, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = None if body is None else json.dumps(body).encode()
        merged = dict(headers or {})
        if token:
            merged["Authorization"] = "Bearer " + token
        if data is not None:
            merged.update({"Content-Type": "application/json", "Content-Length": str(len(data))})
        connection.request(method, path, data, merged)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, (json.loads(payload) if payload else None)

    def run_ingest(self, body):
        status, started = self.call("POST", "/api/library/ingest", body)
        self.assertEqual(status, 202, started)
        for _ in range(100):
            _, job = self.call("GET", "/api/library/jobs/" + started["job"])
            if job["state"] != "running":
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def file(self, title, text, **extra):
        return self.run_ingest({"title": title, "text": text, **extra})

    def test_the_library_token_reads_but_cannot_change_or_reach_the_monitor(self):
        self.assertEqual(self.file("Orbits", "Orbit text.", digest=False)["state"], "done")
        self.assertEqual(self.call("GET", "/api/library/search?q=orbit", token=self.token)[0], 200)
        status, page = self.call("GET", "/api/library/pages/orbits", token=self.token)
        self.assertEqual((status, page["title"]), (200, "Orbits"))
        self.assertEqual(self.call("GET", "/api/library/pages", token=self.token)[1]["total"], 1)
        for method, path, body in [("POST", "/api/library/ingest", {"text": "x"}),
                                   ("DELETE", "/api/library/pages/orbits", None),
                                   ("PUT", "/api/library/pages/orbits", {"markdown": "x"}),
                                   ("POST", "/api/library/relink", None),
                                   ("GET", "/api/library/token", None),
                                   ("GET", "/api/state", None)]:
            self.assertEqual(self.call(method, path, body, token=self.token)[0], 401, path)
        self.assertEqual(self.call("GET", "/api/library/search?q=orbit", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/api/library/search?q=orbit", token="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/api/library/token")[1], {"token": self.token})
        self.assertTrue(self.token.startswith("glib-"))

    def test_ingest_files_links_and_reports_through_a_job(self):
        self.file("Orbits", "Orbit text.", digest=False)
        job = self.file("Launch", "We reach Orbits quickly.", digest=False)
        self.assertEqual((job["state"], job["result"]["slug"]), ("done", "launch"))
        _, page = self.call("GET", "/api/library/pages/launch")
        self.assertIn("[[orbits|Orbits]]", page["text"])
        self.assertEqual(page["links"], ["orbits"])
        _, back = self.call("GET", "/api/library/pages/orbits")
        self.assertEqual([b["slug"] for b in back["backlinks"]], ["launch"])
        self.assertEqual(self.call("DELETE", "/api/library/pages/orbits")[0], 204)
        self.assertEqual(self.call("GET", "/api/library/pages/orbits")[0], 404)
        self.assertNotIn("[[", self.call("GET", "/api/library/pages/launch")[1]["text"])

    def test_digest_uses_the_admin_chat_key_and_its_concepts_become_pages(self):
        answer = {"title": "Engines", "summary": "About engines.", "aliases": [], "tags": [],
                  "concepts": [{"name": "Turbopump", "aliases": [], "definition": "Feeds propellant."}]}
        reply = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()
        with mock.patch.object(self.monitor.gufo, "open_post",
                               return_value=(200, io.BytesIO(reply))) as sent:
            job = self.file("raw title", "A turbopump spins.")
        self.assertEqual(job["result"]["concepts_created"], ["turbopump"])
        self.assertEqual(sent.call_args.kwargs["api_key"], "inference-key")
        self.assertEqual(self.call("GET", "/api/library/pages/engines")[1]["summary"], "About engines.")

    def test_pdf_and_bad_ingest_requests(self):
        for body in ({}, {"text": "a", "pdf_base64": "AA=="}, {"text": "   "}, {"text": 5},
                     {"text": "a", "digest": "yes"}, {"text": "a", "title": 3}, {"pdf_base64": "!!!"}):
            self.assertEqual(self.call("POST", "/api/library/ingest", body)[0], 400, body)
        if shutil.which("pdftotext"):
            encoded = base64.b64encode(make_pdf("Zeppelins float.")).decode()
            job = self.run_ingest({"pdf_base64": encoded, "filename": "airships.pdf", "digest": False})
            self.assertEqual((job["state"], job["result"]["slug"]), ("done", "airships"))
        job = self.run_ingest({"pdf_base64": base64.b64encode(b"junk").decode(), "digest": False})
        self.assertEqual(job["state"], "error")
        self.assertEqual(self.call("GET", "/api/library/jobs/nope")[0], 404)

    def test_put_replaces_a_page(self):
        self.file("Notes", "old text", digest=False)
        status, saved = self.call("PUT", "/api/library/pages/notes", {"markdown": "fresh text"})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/library/pages/notes")[1]["text"], "fresh text")
        self.assertEqual(self.call("PUT", "/api/library/pages/notes", {"markdown": 3})[0], 400)
        self.assertEqual(self.call("PUT", "/api/library/pages/..%2Fx", {"markdown": "x"})[0], 400)
        self.assertEqual(self.call("GET", "/api/library/search?q=x&limit=nope")[0], 400)

    def test_mcp_takes_the_library_token_only_and_rejects_foreign_origins(self):
        self.file("Orbits", "Orbit text.", digest=False)
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "search_library", "arguments": {"query": "orbit"}}}
        status, answer = self.call("POST", "/mcp", message, token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("[orbits] Orbits", answer["result"]["content"][0]["text"])
        self.assertEqual(self.call("POST", "/mcp", message, token=ADMIN_TOKEN)[0], 401)  # not the admin token
        self.assertEqual(self.call("POST", "/mcp", message, token=None)[0], 401)
        self.assertEqual(self.call("POST", "/mcp", message, token=self.token,
                                   headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/mcp", message, token=self.token,
                                   headers={"Origin": f"http://127.0.0.1:{self.port}",
                                            "Host": f"127.0.0.1:{self.port}"})[0], 200)
        self.assertEqual(self.call("GET", "/mcp", token=self.token)[0], 405)
        self.assertEqual(self.call("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                   token=self.token)[0], 202)

    def test_the_chat_calls_tools_through_the_same_code_as_mcp(self):
        self.file("Orbits", "Orbit text.", digest=False)
        status, found = self.call("POST", "/api/library/call", {"name": "search_library", "arguments": {"query": "orbit"}})
        self.assertEqual((status, found["is_error"]), (200, False))
        self.assertEqual(found["text"], library_mcp.call_tool(self.lib, "search_library", {"query": "orbit"}))
        _, missing = self.call("POST", "/api/library/call", {"name": "read_library", "arguments": {"slug": "nope"}})
        self.assertTrue(missing["is_error"])
        self.assertEqual(self.call("POST", "/api/library/call", {"name": "relink"})[0], 400)
        self.assertEqual(self.call("POST", "/api/library/call", {"arguments": {}})[0], 400)
        self.assertEqual(self.call("POST", "/api/library/call", {"name": "list_library"}, token=None)[0], 401)
        # The page learns whether to offer the tools; only the admin has a library.
        self.assertEqual(self.call("GET", "/api/chat/models")[1]["library"], True)
        self.server.library = None
        self.assertEqual(self.call("GET", "/api/chat/models")[1]["library"], False)

    def test_without_a_library_every_route_is_absent(self):
        self.server.library = None
        self.assertEqual(self.call("GET", "/api/library/search?q=a")[0], 404)
        self.assertEqual(self.call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                   token=self.token)[0], 404)


class NoTelemetry:
    def snapshot(self):
        return {}

    def read(self):
        return {}


if __name__ == "__main__":
    unittest.main()
