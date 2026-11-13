"""Regression tests for two /replies behaviours that are easy to lose when the scripts are edited:

- one spoken script, and so one mp3, per reply (no extra clips), and
- a question settled in the chat itself (dialog dismissed, answer typed) shows as settled on the page, and
- an answer sent from the page closes the open question in a cmux chat, as it does in tmux.

Run: python3 -I -m unittest discover -s tests
Only outside calls are faked (the headless Claude call, text-to-speech, the cmux CLI, sleep,
exec and exit); everything else is the real script.
"""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), os.path.join(SCRIPTS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = load("reply-audio")
server = load("reply-audio-server")
_LOGS = tempfile.TemporaryDirectory()
hook.LOG_PATH = os.path.join(_LOGS.name, "reply-audio.log")  # failures the tests provoke stay out of the real log


class OneMp3PerReply(unittest.TestCase):
    MODEL_OUT = ("Audio hook back to one clip\n\nThe audio is one recording again. It plays as before.\n"
                 "---\nDecision: keep the old clips\nOld entries still play their extra clips.")

    def test_rewrite_is_one_script_even_if_the_model_adds_a_divider(self):
        with mock.patch.object(hook, "ask_claude", return_value=self.MODEL_OUT):
            result = hook.conversational("written reply " * 80)
        self.assertEqual(len(result), 2)
        title, script = result
        self.assertEqual(title, "Audio hook back to one clip")
        self.assertIn("one recording again", script)
        self.assertNotIn("---", script)

    def test_render_voices_exactly_one_file_and_records_no_parts(self):
        with tempfile.TemporaryDirectory() as out:
            rid = "20990101-000000-abcdef"
            with open(os.path.join(out, rid + ".txt"), "w") as fh:
                fh.write("written reply " * 80)
            hook.write_json(os.path.join(out, rid + ".json"),
                            {"id": rid, "kind": "reply", "status": "rendering", "audio": rid + ".mp3"})
            with mock.patch.object(hook, "OUT_DIR", out), \
                    mock.patch.object(hook, "STYLE_FILE", os.path.join(out, "style.json")), \
                    mock.patch.object(hook, "ask_claude", return_value=self.MODEL_OUT), \
                    mock.patch.object(hook._NO_PROXY, "open", side_effect=OSError("no network in tests")), \
                    mock.patch.object(hook, "render") as tts:
                hook.render_reply(rid)
            meta = json.load(open(os.path.join(out, rid + ".json")))
        self.assertEqual(tts.call_count, 1)
        self.assertEqual(meta["status"], "ready")
        self.assertFalse(meta.get("parts"))


TID = "toolu_01TestQuestion"
SID = "11111111-2222-3333-4444-555555555555"


def ask():
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": TID, "name": "AskUserQuestion", "input": {"questions": []}}]}}


def result(is_error, answers=None):
    rec = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": TID, "is_error": is_error, "content": "x"}]}}
    rec["toolUseResult"] = {"questions": [], "answers": answers} if answers else "User rejected tool use"
    return rec


def typed(text):
    return {"type": "user", "message": {"content": text}}


class AnswersGivenInTheChat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "proj"))
        self.path = os.path.join(self.tmp.name, "proj", SID + ".jsonl")
        self.patch = mock.patch.object(server, "PROJECTS", self.tmp.name)
        self.patch.start()
        server._answer_scan.clear()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def write(self, *recs):
        with open(self.path, "a") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def test_dismissed_dialog_then_typed_answer_is_reported_as_replied_in_chat(self):
        self.write(ask(), result(True), typed("[Request interrupted by user for tool use]"),
                   typed("<task-notification>\n<task-id>x</task-id>"), typed("go with the private beta"))
        self.assertEqual(server.answer_in_transcript(TID, SID), {"in_chat": "go with the private beta"})

    def test_message_typed_while_busy_counts(self):
        self.write(ask(), result(True),
                   {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "option two"}})
        self.assertEqual(server.answer_in_transcript(TID, SID), {"in_chat": "option two"})

    def test_dialog_answer_the_hook_missed_is_recovered(self):
        self.write(ask(), result(False, {"Which one?": "B"}))
        self.assertEqual(server.answer_in_transcript(TID, SID), {"answers": {"Which one?": "B"}})

    def test_open_question_stays_open_until_something_settles_it(self):
        self.write(ask())
        self.assertIsNone(server.answer_in_transcript(TID, SID))
        self.write(result(True))
        self.assertIsNone(server.answer_in_transcript(TID, SID))  # dismissed, nothing typed yet
        self.write(typed("later answer"))
        self.assertEqual(server.answer_in_transcript(TID, SID), {"in_chat": "later answer"})

    def test_feed_marks_the_card_and_stops_offering_picks(self):
        with tempfile.TemporaryDirectory() as root:
            rid = "20990101-000000-abcdef"
            server.write_json(os.path.join(root, rid + ".json"),
                              {"id": rid, "kind": "questions", "tool_use_id": TID, "session_id": SID,
                               "created": 1, "questions": []})
            server.write_json(os.path.join(root, TID + ".answers.json"),
                              {"tool_use_id": TID, "answers": {}, "in_chat": "typed it in the chat"})
            with mock.patch.object(server, "ROOT", root):
                entry = next(e for e in server.entries_and_chats()["entries"] if e["id"] == rid)
        self.assertEqual(entry["in_chat"], "typed it in the chat")
        self.assertEqual(entry["answers"], {})


class CloseQuestionInCmux(unittest.TestCase):
    QUESTION = "Merge api#935 first, then api#936 after dev is merged into its branch?"
    TERM = {"kind": "cmux", "surface": "SURFACE-1"}

    def run_close(self, screen, answered=False):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            out = screen if "read-screen" in cmd else ""
            return mock.Mock(returncode=0, stdout=out, stderr="")

        with tempfile.TemporaryDirectory() as root:
            rid, tid = "20990101-000000-abcdef", "toolu_close_q"
            server.write_json(os.path.join(root, rid + ".json"),
                              {"id": rid, "kind": "questions", "tool_use_id": tid,
                               "questions": [{"question": self.QUESTION}]})
            if answered:
                server.write_json(os.path.join(root, tid + ".answers.json"),
                                  {"tool_use_id": tid, "answers": {self.QUESTION: "Merge"}})
            with mock.patch.object(server, "ROOT", root), \
                    mock.patch.object(server.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(server.time, "sleep"):
                closed = server.close_question(self.TERM, rid, asking=lambda: False)
        return closed, [c for c in calls if "send-key" in c]

    def test_presses_escape_when_that_question_is_on_screen(self):
        closed, keys = self.run_close("  ☐ api#935  ☐ api#936\n│ " + self.QUESTION + "\n❯ 1. Merge")
        self.assertTrue(closed)
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0][1:], ["send-key", "--surface", "SURFACE-1", "escape"])

    def test_never_presses_escape_when_another_screen_is_showing(self):
        closed, keys = self.run_close("❯ some other prompt the chat is busy with")
        self.assertFalse(closed)
        self.assertEqual(keys, [])

    def test_never_presses_escape_on_another_dialog_once_the_question_was_answered(self):
        # Answered in the chat already: its text is still in the transcript, but the open
        # dialog is a different one, so Esc would dismiss that instead.
        closed, keys = self.run_close("· " + self.QUESTION + " → Merge\n\nDo you want to proceed?\n❯ 1. Yes",
                                      answered=True)
        self.assertFalse(closed)
        self.assertEqual(keys, [])


class FollowPluginUpdates(unittest.TestCase):
    """A running server moves itself onto a newly installed plugin version, in the same process."""

    RID = "20990101-000000-abcdef.resp-0123abcd"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "cache", "the-marketplace", "replies")
        self.old = self.script("0.2.2")
        self.installed = os.path.join(self.tmp.name, "installed_plugins.json")
        self.root = os.path.join(self.tmp.name, "r")
        os.makedirs(self.root)
        self.patches = [mock.patch.object(server, "INSTALLED", self.installed),
                        mock.patch.object(server, "ROOT", self.root)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def script(self, version, cache=None):
        d = os.path.join(cache or self.cache, version, "scripts")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "reply-audio-server.py")
        open(path, "w").close()
        return path

    def install(self, path):
        with open(self.installed, "w") as fh:
            json.dump({"plugins": {"replies@the-marketplace": [
                {"installPath": os.path.dirname(os.path.dirname(path))}]}}, fh)

    def response(self, status):
        server.write_json(os.path.join(self.root, self.RID + ".json"), {"id": self.RID, "status": status})

    def run_once(self, me, execv=None):
        """One pass of follow_installs: ("exec", argv, lock held), ("exit",), or None when it kept
        running."""
        def stop(*args):
            raise SystemExit(args)
        execv = execv or (lambda exe, argv: stop("exec", argv, server._resp_lock.locked()))
        with mock.patch.object(server.time, "sleep", side_effect=[None, StopIteration]), \
                mock.patch.object(server.os, "execv", side_effect=execv), \
                mock.patch.object(server.os, "_exit", side_effect=lambda code: stop("exit")):
            try:
                server.follow_installs(me)
            except SystemExit as done:
                return done.code
            except StopIteration:
                return None

    def test_switches_to_the_newly_installed_version(self):
        new = self.script("0.2.3")
        self.install(new)
        self.response("queued")  # waiting its turn, not being typed: no reason to hold the switch
        kind, argv, locked = self.run_once(self.old)
        self.assertEqual((kind, argv[1:3]), ("exec", ["-I", new]))
        self.assertTrue(locked, "exec'd without the lock deliver() needs to start typing")

    def test_stays_put_while_a_message_is_being_typed(self):
        self.install(self.script("0.2.3"))
        self.response("sending")
        self.assertIsNone(self.run_once(self.old))

    def test_stays_put_when_it_is_the_installed_version(self):
        self.install(self.old)
        self.assertIsNone(self.run_once(self.old))

    def test_ignores_a_replies_plugin_from_another_marketplace(self):
        other = self.script("9.9.9", cache=os.path.join(self.tmp.name, "cache", "elsewhere", "replies"))
        self.install(other)
        self.assertEqual(server.installed_server(self.old), "")

    def test_a_malformed_install_record_is_ignored(self):
        for body in ('{"plugins": "x"}', '{"plugins": []}', '{"plugins": {"replies@the-marketplace": ["x"]}}',
                     '{"plugins": {"replies@the-marketplace": [{"installPath": 5}]}}', "[" * 100000, "{"):
            with open(self.installed, "w") as fh:
                fh.write(body)
            self.assertEqual(server.installed_server(self.old), "", body[:40])
            self.assertIsNone(self.run_once(self.old), body[:40])

    def test_exits_once_its_files_are_gone_and_nothing_is_installed(self):
        os.remove(self.old)
        self.assertEqual(self.run_once(self.old), ("exit",))

    def test_an_update_that_removed_its_files_switches_rather_than_exits(self):
        new = self.script("0.2.3")
        self.install(new)
        os.remove(self.old)
        self.assertEqual(self.run_once(self.old)[0], "exec")
        self.response("sending")  # and while a message is being typed it neither switches nor exits
        self.assertIsNone(self.run_once(self.old))

    def test_a_failed_exec_keeps_it_running(self):
        def fail(exe, argv):
            raise OSError("exec failed")
        self.install(self.script("0.2.3"))
        self.assertIsNone(self.run_once(self.old, execv=fail))
        self.assertFalse(server._resp_lock.locked())


class TodoInProgress(unittest.TestCase):
    """To-do items can be open "[ ]", in progress "[/]" or done "[x]"; in progress counts as not done."""

    LIST = "# Things <!-- auto:on -->\n\n## Now\n- [ ] write it\n- [/] test it\n- [x] plan it\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(server, "TODO_DIR", self.tmp.name)
        self.patch.start()
        with open(os.path.join(self.tmp.name, "master.md"), "w") as fh:
            fh.write(self.LIST)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def lines(self):
        with open(os.path.join(self.tmp.name, "master.md")) as fh:
            return fh.read().split("\n")

    def test_ticking_an_in_progress_item_marks_it_done(self):
        code, _ = server.toggle_todo(4, "test it", True, "master", state="done")
        self.assertEqual(code, 200)
        self.assertEqual(self.lines()[4], "- [x] test it")

    def test_pages_from_before_in_progress_still_tick_and_untick(self):
        self.assertEqual(server.toggle_todo(3, "write it", True, "master")[0], 200)
        self.assertEqual(self.lines()[3], "- [x] write it")
        self.assertEqual(server.toggle_todo(3, "write it", False, "master")[0], 200)
        self.assertEqual(self.lines()[3], "- [ ] write it")

    def test_in_progress_does_not_count_as_done(self):
        master = next(x for x in server.todo_lists() if x["id"] == "master")
        self.assertEqual((master["done"], master["total"]), (1, 3))

    def test_ai_can_tick_an_in_progress_item_but_not_a_done_one(self):
        rid = "20990101-000000-abcdef"
        results = server.auto_apply("master", [
            {"op": "check", "list": "master", "line": 4, "text": "test it", "reply": rid, "reason": "shipped"},
            {"op": "check", "list": "master", "line": 5, "text": "plan it", "reply": rid, "reason": "again"},
        ], {rid})
        why = {e["text"]: reason for e, reason in results}
        self.assertIsNone(why["test it"])
        self.assertEqual(why["plan it"], "already ticked")
        self.assertTrue(self.lines()[4].startswith("- [x] test it"))

    def test_the_toggle_route_passes_the_state_through(self):
        toggle = server.TODO_POSTS["/api/todo/toggle"]
        self.assertEqual(toggle({"list": "master", "line": 3, "text": "write it", "state": "progress"})[0], 200)
        self.assertEqual(self.lines()[3], "- [/] write it")

    def test_a_bad_state_is_refused(self):
        for bad in ("blocked", ["done"], {"a": 1}, 1):
            self.assertEqual(server.toggle_todo(3, "write it", None, "master", state=bad)[0], 400)

    def test_undoing_an_ai_tick_puts_an_in_progress_item_back_in_progress(self):
        rid = "20990101-000000-abcdef"
        server.auto_apply("master", [
            {"op": "check", "list": "master", "line": 4, "text": "test it", "reply": rid, "reason": "shipped"},
            {"op": "check", "list": "master", "line": 3, "text": "write it", "reply": rid, "reason": "shipped"},
        ], {rid})
        ticked = self.lines()
        self.assertEqual(server.undo_todo("master", 4, ticked[4][len("- [x] "):], "done")[0], 200)
        self.assertEqual(server.undo_todo("master", 3, ticked[3][len("- [x] "):], "done")[0], 200)
        self.assertEqual(self.lines()[3:5], ["- [ ] write it", "- [/] test it"])

    def test_undo_of_an_ai_tick_is_refused_once_the_item_is_no_longer_ticked(self):
        rid = "20990101-000000-abcdef"
        with open(os.path.join(self.tmp.name, "master.md"), "w") as fh:
            fh.write(self.LIST.replace("- [/] test it", f"- [/] test it <!-- ai done={rid} -->"))
        code, out = server.undo_todo("master", 4, f"test it <!-- ai done={rid} -->", "done")
        self.assertNotEqual(code, 200)
        self.assertEqual(self.lines()[4], f"- [/] test it <!-- ai done={rid} -->")


class RetryStuckResponses(unittest.TestCase):
    """Retry on a message held or failed on its way to a chat, driven through the real deliver()
    with only the terminal (tmux, cmux and ps) and sleep faked."""

    RID = "20990101-000000-abcdef.resp-0123abcd"
    SID = "11111111-2222-3333-4444-555555555555"
    SURFACE = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    RULE = "─" * 40
    MESSAGE = "From the replies panel: please run the tests"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.kind, self.status = "cmux", "idle"
        self.suggestion, self.typed, self.cursor_at_start = "", "", False
        self.screen = None  # a whole screen to show instead of the input box
        self.question_opens_while_typing = False
        self.submitted, self.started = [], []
        self.new_session_ok, self.tmux_session_alive = True, True
        for name in ("ROOT", "SESSIONS", "PROJECTS"):
            os.makedirs(os.path.join(self.root, name))
        patches = [mock.patch.object(server, n, os.path.join(self.root, n)) for n in ("ROOT", "SESSIONS", "PROJECTS")]
        patches += [mock.patch.object(server.subprocess, "run", side_effect=self.fake_run),
                    mock.patch.object(server.time, "sleep", side_effect=self.nap),
                    mock.patch.object(server, "REQUIRE_KEY", False),
                    mock.patch.dict(server._terminals, clear=True), mock.patch.dict(server._cwds, clear=True),
                    mock.patch.dict(server._reopening, clear=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.chat_open(True)

    def nap(self, seconds):
        if seconds == server.DELIVERY_POLL:
            raise KeyboardInterrupt  # one pass of delivery_loop is enough

    def resumable(self):
        """Give the chat a transcript that names an existing folder, so it can be reopened."""
        os.makedirs(os.path.join(server.PROJECTS, "proj"), exist_ok=True)
        with open(os.path.join(server.PROJECTS, "proj", self.SID + ".jsonl"), "a") as fh:
            fh.write(json.dumps({"type": "user", "cwd": self.root}) + "\n")

    def chat_open(self, is_open):
        path = os.path.join(server.SESSIONS, "chat.json")
        if is_open:
            server.write_json(path, {"sessionId": self.SID, "pid": os.getpid(), "status": self.status})
        elif os.path.exists(path):
            os.remove(path)

    def set_status(self, status):
        self.status = status
        self.chat_open(True)

    def box(self):
        if self.screen is not None:
            return self.screen
        # cmux sends plain text, so Claude's dim suggestion reads like typed text; tmux keeps the dim code
        shown = self.typed or (self.suggestion and ("\x1b[2m" + self.suggestion + "\x1b[0m" if self.kind == "tmux"
                                                    else self.suggestion))
        return f"{self.RULE}\n❯ {shown}\n{self.RULE}\n" if shown else f"{self.RULE}\n❯\n{self.RULE}\n"

    def type_text(self, text):
        self.typed = text if not self.typed else text + self.typed if self.cursor_at_start else self.typed + text
        if self.question_opens_while_typing:
            self.set_status("waiting")

    def fake_run(self, cmd, **kw):
        ok = lambda out="": mock.Mock(returncode=0, stdout=out, stderr="")
        if "new-session" in cmd:
            self.started.append(cmd)
            return mock.Mock(returncode=0 if self.new_session_ok else 1, stdout="", stderr="")
        if "has-session" in cmd:
            return mock.Mock(returncode=0 if self.tmux_session_alive else 1, stdout="", stderr="")
        if cmd[0] == "ps":
            env = {"tmux": "TMUX=/tmp/sock,1,0 TMUX_PANE=%1", "cmux": f"CMUX_SURFACE_ID={self.SURFACE}"}.get(self.kind, "")
            return ok(f"claude {env}\n")
        if "read-screen" in cmd or "capture-pane" in cmd:
            return ok(self.box())
        if "surface.send_text" in cmd:
            self.type_text(json.loads(cmd[-1])["text"])
        elif "send-keys" in cmd and "-l" in cmd:
            self.type_text(cmd[-1])
        elif cmd[-1] in ("enter", "Enter"):  # the chat takes the line and writes it to its transcript
            self.submitted.append(self.typed)
            os.makedirs(os.path.join(server.PROJECTS, "proj"), exist_ok=True)
            with open(os.path.join(server.PROJECTS, "proj", self.SID + ".jsonl"), "a") as fh:
                fh.write(json.dumps({"type": "user", "message": {"content": self.typed}}) + "\n")
            self.typed = ""
        return ok()

    def put(self, **r):
        server.write_json(server.resp_path(self.RID), dict(
            {"id": self.RID, "text": "please run the tests", "session_id": self.SID, "status": "queued",
             "created": 1}, **r))

    def get(self):
        return server.load(server.resp_path(self.RID))

    def deliver(self):
        r = self.get()
        if r and r.get("status") == "queued":
            server.deliver(r)
        return self.get()

    def post_retry(self):
        """POST /api/respond/retry through the real request handler, as the page sends it."""
        body = json.dumps({"id": self.RID}).encode()
        h = server.Handler.__new__(server.Handler)
        h.command, h.path, h.request_version, h.requestline = "POST", "/api/respond/retry", "HTTP/1.1", ""
        h.client_address, h.server = ("127.0.0.1", 0), mock.Mock(server_address=("127.0.0.1", 0))
        h.headers = {"Host": "127.0.0.1", "X-Replies": "1", "Content-Length": str(len(body))}
        h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
        with mock.patch.object(h, "log_message"):
            h.do_POST()
        return int(h.wfile.getvalue().split(b" ", 2)[1])

    def held_by_a_suggestion(self):
        self.suggestion = "try: summarise the diff"
        self.put()
        r = self.deliver()
        self.assertEqual((r["hold"], self.submitted), ("draft", []))
        return r

    def test_a_cmux_suggestion_is_typed_over_once_on_retry(self):
        self.assertTrue(server.public_resp(self.held_by_a_suggestion())["retryable"])
        self.assertEqual(self.post_retry(), 200)
        self.assertEqual(self.deliver()["status"], "sent")
        self.assertEqual(server.retry_response(self.RID)[0], 409)
        self.deliver()
        self.assertEqual(self.submitted, [self.MESSAGE])

    def test_a_real_draft_is_never_sent_mixed_in(self):
        for at_start in (False, True):
            with self.subTest(cursor_at_start=at_start):
                self.cursor_at_start, self.typed, self.submitted = at_start, "my own half-written thought", []
                self.put(hold="draft", retryable=True, force_at=server.time.time())
                r = self.deliver()
                self.assertEqual(self.submitted, [])
                self.assertEqual((r["status"], r["retryable"]), ("failed", False))
                self.assertIn("other text", r["error"])

    def test_retry_types_nothing_without_a_one_line_input_box(self):
        self.screen = f"{self.RULE}\n❯ 1. Yes\n  2. No\n{self.RULE}\n"
        self.put(hold="draft", retryable=True, force_at=server.time.time())
        r = self.deliver()
        self.assertEqual((r["hold"], self.typed, "force_at" in r, r["retryable"]), ("draft", "", False, False))

    def test_a_stale_retry_lapses(self):
        self.suggestion = "try: summarise the diff"
        self.put(hold="draft", retryable=True, force_at=server.time.time() - server.RETRY_SECONDS - 1)
        r = self.deliver()
        self.assertEqual((r["hold"], self.typed, "force_at" in r), ("draft", "", False))

    def test_retry_waits_while_a_question_is_open(self):
        self.held_by_a_suggestion()
        self.assertEqual(server.retry_response(self.RID)[0], 200)
        self.set_status("waiting")
        r = self.deliver()
        self.assertEqual((r["hold"], r["retryable"], self.typed), ("dialog", False, ""))
        self.assertEqual(server.retry_response(self.RID)[0], 409)

    def test_a_held_message_whose_chat_is_reopening_is_not_retried(self):
        self.held_by_a_suggestion()
        self.chat_open(False)
        self.resumable()
        self.put(hold="draft", retryable=True, reopen=True)
        self.assertEqual(self.deliver()["hold"], "resuming")
        self.assertEqual(server.retry_response(self.RID)[0], 409)

    def test_retry_is_not_offered_where_the_draft_is_real(self):
        self.kind, self.typed = "tmux", "my own half-written thought"  # tmux shows dim text, so this is typed
        self.put()
        r = self.deliver()
        self.assertEqual((r["hold"], r["retryable"]), ("draft", False))
        self.assertEqual(server.retry_response(self.RID)[0], 409)

    def test_a_message_that_may_have_reached_the_chat_is_never_retried(self):
        self.question_opens_while_typing = True
        self.put()
        r = self.deliver()
        self.assertEqual((r["status"], self.submitted), ("failed", []))
        self.assertEqual(server.retry_response(self.RID)[0], 409)
        self.put(status="failed", error="Interrupted. Check the chat.")  # a record from before Retry
        self.assertEqual(server.retry_response(self.RID)[0], 409)

    def test_a_chat_that_closed_is_reopened_on_retry(self):
        self.chat_open(False)
        self.put()
        self.assertEqual(self.deliver()["error"], "That chat has closed.")
        self.assertEqual(server.retry_response(self.RID)[0], 409)  # nothing to reopen it from
        self.resumable()
        self.put()
        self.assertTrue(self.deliver()["retryable"])
        self.assertEqual(server.retry_response(self.RID)[0], 200)
        r = self.get()
        self.assertEqual((r["status"], "error" in r, "retryable" in r), ("queued", False, False))
        self.deliver()
        self.assertEqual(len(self.started), 1)
        self.chat_open(True)
        self.deliver()
        self.assertEqual(self.submitted, [self.MESSAGE])

    def test_a_chat_that_could_not_reopen_is_started_again_on_retry(self):
        self.chat_open(False)
        self.resumable()
        for cause in ("start failed", "chat exited"):
            with self.subTest(cause):
                self.started = []
                server._reopening.clear()
                self.new_session_ok, self.tmux_session_alive = cause != "start failed", cause != "chat exited"
                self.put(reopen=True)
                r = self.deliver() if cause == "start failed" else (self.deliver(), self.deliver())[1]
                self.assertEqual((r["error"], r["retryable"]), (server.CANT_REOPEN, True))
                self.assertEqual(server.retry_response(self.RID)[0], 200)
                self.new_session_ok = True
                self.deliver()
                self.assertEqual(len(self.started), 2)

    def test_only_the_first_message_to_a_chat_can_be_retried(self):
        self.suggestion = "try: summarise the diff"
        self.put(hold="draft", retryable=True, created=2)  # it was first, until an earlier one came in
        first = "20990101-000000-abcdef.resp-99999999"
        server.write_json(server.resp_path(first), {"id": first, "text": "first", "session_id": self.SID,
                                                    "status": "queued", "created": 1})
        with self.assertRaises(KeyboardInterrupt):
            server.delivery_loop()
        self.assertEqual((self.get()["hold"], self.get()["retryable"]), ("draft", False))
        self.assertEqual(server.retry_response(self.RID)[0], 409)

    def test_a_failure_retry_cannot_fix_offers_no_retry(self):
        with self.subTest("chat left tmux and cmux"):
            self.kind = "plain"
            self.put(hold="draft", retryable=True)
            r = self.deliver()
            self.assertEqual((r["error"], r["retryable"]), (server.NO_TERMINAL, False))
        with self.subTest("no folder to reopen it in"):
            self.chat_open(False)
            self.put(reopen=True)
            r = self.deliver()
            self.assertEqual((r["error"], r["retryable"], self.started), (server.CANT_REOPEN, False, []))

    def test_a_message_cancelled_while_its_chat_is_checked_stays_cancelled(self):
        self.chat_open(False)
        self.resumable()
        self.put()
        r = self.get()  # the delivery loop's copy
        self.assertEqual(server.cancel_response(self.RID)[0], 200)
        server.deliver(r)
        self.assertIsNone(self.get())
        self.assertEqual(server.retry_response(self.RID)[0], 404)

    def test_a_retried_message_can_still_be_cancelled(self):
        self.held_by_a_suggestion()
        self.assertEqual(server.retry_response(self.RID)[0], 200)
        self.assertEqual(server.cancel_response(self.RID)[0], 200)
        self.assertIsNone(self.deliver())
        self.assertEqual(self.typed, "")

    def test_unknown_or_malformed_ids_are_refused(self):
        self.assertEqual(server.retry_response(self.RID)[0], 404)
        for bad in ("../x", "", None, "20990101-000000-abcdef"):
            self.assertEqual(server.retry_response(bad)[0], 400, bad)


class CaptionScripts(unittest.TestCase):
    """GET /api/script serves the spoken script of exactly the clip asked for."""

    RID = "20990101-000000-abcdef"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(server, "ROOT", self.tmp.name)
        self.patch.start()
        server.write_json(os.path.join(self.tmp.name, self.RID + ".json"), {
            "id": self.RID, "kind": "questions", "audio": self.RID + ".mp3", "script": "The reply itself.",
            "questions": [{"audio": self.RID + "-q0.mp3", "script": "First question, spoken."},
                          {"audio": self.RID + "-q1.mp3"}]})

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_each_clip_gets_its_own_script(self):
        self.assertEqual(server.clip_script(self.RID + ".mp3"), (200, {"text": "The reply itself."}))
        self.assertEqual(server.clip_script(self.RID + "-q0.mp3"), (200, {"text": "First question, spoken."}))

    def test_a_clip_without_a_script_is_not_found(self):
        self.assertEqual(server.clip_script(self.RID + "-q1.mp3")[0], 404)
        self.assertEqual(server.clip_script("20990101-000000-000000.mp3")[0], 404)

    def test_only_clip_file_names_are_accepted(self):
        for bad in ("../../etc/passwd", self.RID + ".json", "", None, 5):
            self.assertEqual(server.clip_script(bad)[0], 400, bad)


class VoiceStyle(unittest.TestCase):
    """The page saves Relaxed, Standard or Brief; the reply hook writes and voices new replies in it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.join(self.tmp.name, "r")
        os.makedirs(root)
        self.patches = [mock.patch.object(server, "ROOT", root),
                        mock.patch.object(server, "STYLE_FILE", os.path.join(self.tmp.name, "style.json")),
                        mock.patch.object(hook, "STYLE_FILE", os.path.join(self.tmp.name, "style.json"))]
        for pt in self.patches:
            pt.start()

    def tearDown(self):
        for pt in self.patches:
            pt.stop()
        self.tmp.cleanup()

    def test_the_hook_reads_what_the_page_saved(self):
        self.assertEqual(hook.voice_style(), "standard")
        self.assertEqual(server.set_voice_style("relaxed"), (200, {"style": "relaxed"}))
        self.assertEqual((server.voice_style(), hook.voice_style()), ("relaxed", "relaxed"))

    def test_only_known_styles_are_saved(self):
        for bad in ("slow", "", None, ["brief"]):
            self.assertEqual(server.set_voice_style(bad)[0], 400, bad)
        self.assertEqual(hook.voice_style(), "standard")

    def test_a_hand_edited_style_file_falls_back_to_standard(self):
        for raw in ('{"style": []}', '{"style": {}}', '["brief"]', '"brief"', "not json", ""):
            with open(hook.STYLE_FILE, "w") as fh:
                fh.write(raw)
            self.assertEqual(hook.voice_style(), "standard", raw)

    def test_a_new_reply_is_written_and_voiced_in_the_saved_style(self):
        out = os.path.join(self.tmp.name, "r")
        rid = "20990101-000000-abcdef"
        for style, speed in (("relaxed", 0.85), ("standard", 1.0), ("brief", 1.1)):
            server.set_voice_style(style)
            with open(os.path.join(out, rid + ".txt"), "w") as fh:
                fh.write("written reply " * 80)
            hook.write_json(os.path.join(out, rid + ".json"),
                            {"id": rid, "kind": "reply", "status": "rendering", "audio": rid + ".mp3"})
            with mock.patch.object(hook, "OUT_DIR", out), \
                    mock.patch.object(hook, "ask_claude", return_value=OneMp3PerReply.MODEL_OUT) as rewrite, \
                    mock.patch.object(hook._NO_PROXY, "open", side_effect=OSError("no network in tests")), \
                    mock.patch.object(hook, "render") as tts:
                hook.render_reply(rid)
            self.assertEqual(rewrite.call_args_list[0][0][0], hook.rewrite_prompt(style))
            self.assertEqual(tts.call_args[0][2], speed)
            self.assertEqual(json.load(open(os.path.join(out, rid + ".json")))["style"], style)

    def test_the_page_posts_its_style_to_an_allowed_route(self):
        handler = open(os.path.join(SCRIPTS, "reply-audio-server.py"), encoding="utf-8").read()
        posts = handler[handler.index('elif path in ("/api/respond"'):handler.index("or path in TODO_POSTS")]
        self.assertIn('"/api/style"', posts)
        self.assertIn("post('/api/style'", server.PAGE)

    def test_each_style_changes_the_script_length_asked_for(self):
        prompts = {st: hook.rewrite_prompt(st) for st in hook.STYLES}
        self.assertEqual(len(set(prompts.values())), 3)
        self.assertNotIn("@@LENGTH@@", "".join(prompts.values()))

    def test_brief_asks_for_a_short_script(self):
        # Brief asked for "40 to 100 words" and came out at 172-185: the model reads a range as room.
        # It now states one hard ceiling, no longer than the shortest Standard script.
        brief = hook.STYLES["brief"]["length"]
        ceiling = re.findall(r"at most (\d+) words", brief)
        standard = re.search(r"(\d+) to \d+ words", hook.STYLES["standard"]["length"])
        self.assertEqual(len(ceiling), 1, brief)
        self.assertNotRegex(brief, r"\d+ to \d+ words")
        self.assertLessEqual(int(ceiling[0]), int(standard.group(1)))
        self.assertIn(brief, hook.rewrite_prompt("brief"))

    def test_relaxed_and_brief_are_voiced_slower_and_quicker(self):
        sent = []
        with mock.patch.object(hook, "_post", side_effect=lambda url, payload, tmp, out: sent.append(payload)), \
                mock.patch.object(hook, "voicemode_tts", return_value=("http://127.0.0.1:8880/v1/audio/speech", "af_sky")):
            for st in ("relaxed", "standard", "brief"):
                hook.render("hello", os.path.join(self.tmp.name, st + ".mp3"), hook.STYLES[st]["speed"])
        speeds = [p.get("speed", 1.0) for p in sent]
        self.assertTrue(speeds[0] < speeds[1] == 1.0 < speeds[2], speeds)


class VisualCaptions(unittest.TestCase):
    """A reply's visual plan: cleaned beats, timed to the voice's own word timings, served with the script."""

    SCRIPT = "The login test was failing. I made it wait for the cookie. It passed fifty runs."
    WORDS = [[w, round(i * 0.4, 2), round(i * 0.4 + 0.35, 2)] for i, w in enumerate(SCRIPT.replace(".", "").split())]

    def test_beats_are_cleaned_to_what_the_page_draws(self):
        beats = [{"at": "The login", "kind": "chips", "data": {"items": ["a" * 99, 7, "ok"]}},
                 {"at": "x", "kind": "iframe", "data": {}},
                 {"at": "", "kind": "list", "data": {"items": ["a"]}},
                 {"at": "made it", "kind": "code", "data": {"lines": "not a list", "highlight": [0]}},
                 "nonsense"]
        cleaned = [c for c in map(hook.clean_beat, beats) if c]
        self.assertEqual(cleaned, [{"at": "The login", "kind": "chips", "data": {"items": ["a" * 40, "ok"]}}])

    def test_each_beat_starts_when_its_words_are_spoken(self):
        beats = [{"at": "I made it wait", "kind": "list", "data": {"items": ["x"]}},
                 {"at": "passed fifty runs", "kind": "list", "data": {"items": ["y"]}}]
        out = hook.align_beats(beats, self.WORDS, self.SCRIPT)
        self.assertEqual([b["t"] for b in out], [round(5 * 0.4, 2), round(13 * 0.4, 2)])

    def test_without_word_timings_a_beat_gets_its_share_of_the_script(self):
        out = hook.align_beats([{"at": "passed fifty", "kind": "list", "data": {"items": ["y"]}}], None, self.SCRIPT)
        self.assertTrue(0.7 < out[0]["f"] < 1, out)

    def test_a_plan_comes_from_claude_json_or_not_at_all(self):
        reply = 'Sure: {"beats": [{"at": "The login", "kind": "callout", "data": {"text": "flaky", "value": "1 in 10"}}]}'
        with mock.patch.object(hook, "ask_claude", return_value=reply):
            self.assertEqual(hook.plan_visuals(self.SCRIPT)[0]["data"], {"text": "flaky", "value": "1 in 10"})
        for bad in (None, "no json here", '{"beats": "x"}'):
            with mock.patch.object(hook, "ask_claude", return_value=bad):
                self.assertEqual(hook.plan_visuals(self.SCRIPT), [], bad)

    def test_timed_voice_saves_the_audio_and_returns_word_timings(self):
        body = json.dumps({"audio": "SUQz", "timestamps": [{"word": "The", "start_time": 0.0, "end_time": 0.2}]}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(body)
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(hook, "voicemode_tts", return_value=("http://127.0.0.1:8880/v1/audio/speech", "af_sky")), \
                mock.patch.object(hook._NO_PROXY, "open", return_value=resp) as opened:
            words = hook.render_timed("The", os.path.join(d, "a.mp3"))
            self.assertEqual(open(os.path.join(d, "a.mp3"), "rb").read(), b"ID3")
        self.assertEqual(words, [["The", 0.0, 0.2]])
        self.assertTrue(opened.call_args[0][0].full_url.endswith("/dev/captioned_speech"))

    def test_timed_voice_falls_back_to_the_plain_voice(self):
        with mock.patch.object(hook, "voicemode_tts", return_value=("http://127.0.0.1:8880/v1/audio/speech", "af_sky")), \
                mock.patch.object(hook._NO_PROXY, "open", side_effect=OSError("down")), \
                mock.patch.object(hook, "render") as plain:
            self.assertIsNone(hook.render_timed("hi", "/tmp/x.mp3", 0.85))
        plain.assert_called_once_with("hi", "/tmp/x.mp3", 0.85)

    def test_the_script_route_carries_the_words_and_the_plan(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(server, "ROOT", root):
            rid = "20990101-000000-abcdef"
            server.write_json(os.path.join(root, rid + ".json"), {"id": rid, "audio": rid + ".mp3", "script": self.SCRIPT,
                                                                   "words": self.WORDS, "visuals": [{"t": 1}]})
            code, out = server.clip_script(rid + ".mp3")
        self.assertEqual((code, out["words"], out["visuals"]), (200, self.WORDS, [{"t": 1}]))

    def test_malformed_beats_are_dropped_without_losing_the_plan(self):
        good = {"at": "The login", "kind": "list", "data": {"items": ["x"]}}
        for bad in ({"at": "made it", "kind": "code", "data": {"lines": ["a"], "highlight": 3}},
                    {"at": "made it", "kind": "code", "data": {"lines": ["a"], "highlight": {"0": 1}}},
                    {"at": "made it", "kind": "compare", "data": {"left": "before", "right": {"title": "after"}}}):
            with mock.patch.object(hook, "ask_claude", return_value=json.dumps({"beats": [good, bad]})):
                self.assertEqual(len(hook.plan_visuals(self.SCRIPT)), 2, bad)

    def _render(self, plan_reply):
        """render_reply with only the outside calls faked: Claude (rewrite and plan) and the voice server."""
        writes = []
        body = json.dumps({"audio": "SUQz", "timestamps": [{"word": w, "start_time": a, "end_time": b}
                                                           for w, a, b in self.WORDS]}).encode()
        resp = mock.MagicMock()
        resp.__enter__.side_effect = lambda: io.BytesIO(body)
        claude = lambda prompt, text: plan_reply if prompt == hook.VISUALS_PROMPT else self.SCRIPT
        real_write = hook.write_json
        with tempfile.TemporaryDirectory() as out:
            rid = "20990101-000000-abcdef"
            with open(os.path.join(out, rid + ".txt"), "w") as fh:
                fh.write("written reply " * 80)
            real_write(os.path.join(out, rid + ".json"), {"id": rid, "kind": "reply", "status": "rendering", "audio": rid + ".mp3"})
            with mock.patch.object(hook, "OUT_DIR", out), \
                    mock.patch.object(hook, "STYLE_FILE", os.path.join(out, "style.json")), \
                    mock.patch.object(hook, "ask_claude", side_effect=claude), \
                    mock.patch.object(hook, "voicemode_tts", return_value=("http://127.0.0.1:8880/v1/audio/speech", "af_sky")), \
                    mock.patch.object(hook._NO_PROXY, "open", return_value=resp), \
                    mock.patch.object(hook, "write_json", side_effect=lambda p, o: (writes.append(json.loads(json.dumps(o))), real_write(p, o))):
                hook.render_reply(rid)
            meta = json.load(open(os.path.join(out, rid + ".json")))
        return meta, writes

    def test_a_reply_is_playable_before_its_plan_then_gets_it(self):
        plan = json.dumps({"beats": [{"at": "passed fifty runs", "kind": "callout", "data": {"text": "green", "value": "50 of 50"}}]})
        meta, writes = self._render(plan)
        ready = next(w for w in writes if w.get("status") == "ready")
        self.assertNotIn("visuals", ready)  # the first ready write does not wait for the plan
        self.assertEqual(ready["words"], self.WORDS)
        self.assertEqual([(b["kind"], b["t"]) for b in meta["visuals"]], [("callout", round(13 * 0.4, 2))])

    def test_a_failed_plan_is_stored_empty_so_the_page_stops_asking(self):
        meta, _ = self._render("no json here")
        self.assertEqual((meta["status"], meta["visuals"]), ("ready", []))

    def test_only_the_replys_own_clip_carries_words_and_plan(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(server, "ROOT", root):
            rid = "20990101-000000-abcdef"
            server.write_json(os.path.join(root, rid + ".json"), {
                "id": rid, "audio": rid + ".mp3", "script": self.SCRIPT, "words": self.WORDS, "visuals": [{"t": 1}],
                "parts": [{"audio": rid + "-p1.mp3", "script": "a part"}]})
            self.assertEqual(server.clip_script(rid + "-p1.mp3"), (200, {"text": "a part"}))

    def test_the_polled_feed_leaves_out_words_and_plan(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(server, "ROOT", root):
            rid = "20990101-000000-abcdef"
            server.write_json(os.path.join(root, rid + ".json"), {"id": rid, "kind": "reply", "status": "ready", "created": 1,
                                                                   "audio": rid + ".mp3", "script": self.SCRIPT,
                                                                   "words": self.WORDS, "visuals": [{"t": 1}]})
            entry = next(e for e in server.entries_and_chats()["entries"] if e["id"] == rid)
        self.assertFalse({"script", "words", "visuals"} & set(entry), entry)


NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class CaptionWordTiming(unittest.TestCase):
    """The page's own caption code (cut from the served page, run in node) lines each script word
    up with the voice's timed tokens, so the highlighted word is the one being spoken."""

    def run_js(self, expr):
        page = server.PAGE
        code = page[page.index("const CC_END="):page.index("// With the voice's word timings, each phrase")]
        out = subprocess.run([NODE, "-e", code + "\nprocess.stdout.write(JSON.stringify(" + expr + "))"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def align(self, script, timed):
        return self.run_js(f"ccAlign({json.dumps(script.split())}, {json.dumps(timed)})")

    def test_each_word_starts_when_the_voice_says_it(self):
        timed = [["The", -0.01, 0.09], ["move", 0.09, 0.37], ["is", 0.37, 0.5], ["done", 0.5, 0.9],
                 [".", 0.9, 1.0], ["It's", 1.0, 1.2], ["live", 1.2, 1.6], [",", 1.6, 1.7], ["now", 1.7, 2.0]]
        self.assertEqual(self.align("The move is done. It's live, now", timed),
                         [0, 0.09, 0.37, 0.5, 1.0, 1.2, 1.7])

    def test_a_word_the_voice_splits_starts_at_its_first_part(self):
        timed = [["a", 0, 0.1], ["follow", 0.1, 0.4], ["-", 0.4, 0.45], ["up", 0.45, 0.7], ["call", 0.7, 1.0]]
        self.assertEqual(self.align("a follow-up call", timed), [0, 0.1, 0.7])

    def test_a_word_spoken_differently_gets_a_time_between_its_neighbours(self):
        timed = [["costs", 0, 0.4], ["five", 0.4, 0.8], ["dollars", 0.8, 1.2], ["today", 1.2, 1.6]]
        self.assertEqual(self.align("costs 5 dollars today", timed), [0, 0.4, 0.8, 1.2])

    def test_a_word_the_voice_skipped_does_not_derail_the_rest(self):
        timed = [["one", 0, 0.3], ["two", 0.3, 0.6], ["three", 0.6, 0.9], ["four", 0.9, 1.2]]
        self.assertEqual(self.align("one um two three four", timed), [0, 0.15, 0.3, 0.6, 0.9])

    def test_phrases_keep_every_word_with_its_index_and_share(self):
        script = "The move to GitHub is done, and your Mac updates from there too. Nothing else changed."
        chunks = self.run_js(f"ccSplit({json.dumps(script)})")
        words = script.split()
        self.assertEqual([w for c in chunks for w in c["ws"]], words)
        for c in chunks:
            self.assertEqual(c["ws"], words[c["i0"]:c["i0"] + len(c["ws"])])
            self.assertEqual(c["f"][0], 0)
            self.assertEqual(c["f"], sorted(c["f"]))
            self.assertLess(c["f"][-1], 1)


if __name__ == "__main__":
    unittest.main()
