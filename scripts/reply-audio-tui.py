#!/usr/bin/python3
"""`replies`: a terminal panel for the reply-audio server, made for a phone over SSH.

Lists the chats on this machine, shows a chat's conversation, sends it a message, shows its
live terminal screen, and starts new chats in tmux. Every keyboard action also has a tappable
label in the bottom bar, because a phone keyboard has no Esc key.

It never touches a chat's terminal itself: messages go through reply-audio-server.py on
127.0.0.1:8799, which holds them until the chat is at an empty prompt. That also matters for
cmux, which refuses connections from an SSH shell. Conversations are read from the transcripts
on this machine, so nothing here leaves it. Python standard library only, no curses, so
wide characters and narrow terminals behave the same everywhere.
"""
import codecs
import glob
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import time
import tty
import unicodedata
import urllib.error
import urllib.request
import uuid

def _plugin_config():
    """The replies plugin's saved settings (written at each session start), newest install first."""
    import glob as _glob
    paths = sorted(_glob.glob(os.path.expanduser("~/.claude/plugins/data/replies-*/config.json")),
                   key=os.path.getmtime, reverse=True)
    for path in paths:
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return {}


_CFG = _plugin_config()
PORT = int(_CFG.get("port") or 8799)
SERVER = f"http://127.0.0.1:{PORT}"
DATA_DIR = _CFG.get("data_dir") or ""


def _key():
    if _CFG.get("require_key", "off") == "off" or not DATA_DIR:
        return ""
    try:
        with open(os.path.join(DATA_DIR, "key")) as fh:
            return fh.read().strip()
    except OSError:
        return ""


KEY = _key()
SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "reply-audio-server.py")
PROJECTS = os.path.expanduser("~/.claude/projects")
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
TAIL_BYTES = 1_500_000
MAX_MESSAGES = 60
REFRESH = 2.0
CONTROL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
NOISE = re.compile(r"^\s*<(system-reminder|command-|local-command|task-notification|bash-)")
MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
KEYS = {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
        "\x1bOA": "up", "\x1bOB": "down", "\x1b[5~": "pgup", "\x1b[6~": "pgdn",
        "\x1b[H": "home", "\x1b[F": "end", "\r": "enter", "\n": "enter", "\x7f": "backspace",
        "\x08": "backspace", "\x03": "ctrl-c", "\x15": "ctrl-u"}
STATES = {"idle": ("ready", 32), "shell": ("ready", 32), "busy": ("working", 36),
          "waiting": ("needs you", 33)}
HOLDS = {"busy": "Queued until it finishes", "dialog": "Queued: a question is open",
         "draft": "Queued: prompt box has text", "unreachable": "Queued: can't see its terminal",
         "resuming": "Reopening the chat"}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- text

def clean(text):
    return CONTROL.sub("", str(text or "")).replace("\t", "    ")


def width(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def clip(text, cols):
    """Text cut to fit cols display columns, with an ellipsis if anything was cut."""
    if width(text) <= cols:
        return text
    out, used = "", 0
    for c in text:
        w = width(c)
        if used + w > cols - 1:
            break
        out, used = out + c, used + w
    return out + "…"


def wrap(text, cols):
    """Lines of at most cols columns. Keeps paragraph breaks and code indentation."""
    out = []
    for para in clean(text).split("\n"):
        if not para.strip():
            out.append("")
            continue
        indent = para[:len(para) - len(para.lstrip())][:8]
        out += textwrap.wrap(para.strip(), max(8, cols - len(indent)), initial_indent=indent,
                             subsequent_indent=indent, break_long_words=True,
                             break_on_hyphens=False) or [""]
    return out


def parse_input(buf):
    """(events, rest) from raw terminal input. Events are key names, single characters, or
    ("mouse", button, col, row, pressed). `rest` is an unfinished escape sequence."""
    events = []
    while buf:
        m = MOUSE.match(buf)
        if m:
            events.append(("mouse", int(m.group(1)), int(m.group(2)), int(m.group(3)),
                           m.group(4) == "M"))
            buf = buf[m.end():]
            continue
        if buf[0] == "\x1b":
            seq = next((k for k in KEYS if buf.startswith(k)), None)
            if seq:
                events.append(KEYS[seq])
                buf = buf[len(seq):]
            elif len(buf) == 1 or (buf[1] in "[O" and not re.match(r"\x1b[\[O][0-9;<]*[@-~]", buf)):
                return events, buf
            else:
                m = re.match(r"\x1b[\[O][0-9;<]*[@-~]", buf)
                buf = buf[m.end():] if m else buf[1:]  # a key this panel doesn't use
            continue
        c = buf[0]
        buf = buf[1:]
        events.append(KEYS.get(c, c if c >= " " else None))
    return [e for e in events if e], ""


# ---------------------------------------------------------------- data

def api(path, body=None):
    """(status, json). Status 0 means the server can't be reached."""
    req = urllib.request.Request(
        SERVER + path, data=None if body is None else json.dumps(body).encode(),
        headers={"X-Replies": "1", "Content-Type": "application/json",
                 **({"X-Replies-Key": KEY} if KEY else {})})
    try:
        with _NO_PROXY.open(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except ValueError:
            return exc.code, {}
    except (OSError, ValueError):
        return 0, {}


def start_server():
    env = dict(os.environ, REPLIES_PORT=str(PORT))
    if DATA_DIR:
        env.update(REPLIES_HOME=DATA_DIR, REPLIES_REQUIRE_KEY="0" if _CFG.get("require_key") == "off" else "1")
    subprocess.Popen([sys.executable, "-I", SERVER_SCRIPT, str(PORT)], env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def block_text(block):
    """Readable text of one transcript content block, or "" for tool plumbing."""
    if not isinstance(block, dict):
        return ""
    if block.get("type") == "text":
        return block.get("text") or ""
    if block.get("type") == "tool_use" and block.get("name") == "AskUserQuestion":
        lines = []
        for q in (block.get("input") or {}).get("questions") or []:
            lines.append("Question: " + str(q.get("question") or ""))
            lines += ["  - " + str(o.get("label") or "") for o in q.get("options") or []]
        return "\n".join(lines)
    return ""


def conversation(raw_lines):
    """[(role, text)] from transcript lines, leaving out tool calls and injected context."""
    out = []
    for raw in raw_lines:
        if b'"user"' not in raw and b'"assistant"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        role = rec.get("type")
        if role not in ("user", "assistant") or rec.get("isMeta") or rec.get("isSidechain"):
            continue
        content = (rec.get("message") or {}).get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        text = "\n\n".join(t for t in map(block_text, blocks) if t.strip())
        if text.strip() and not NOISE.match(text):
            out.append((role, text.strip()))
    return out[-MAX_MESSAGES:]


def transcript_path(session_id):
    paths = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
    return paths[0] if paths else None


def load_conversation(session_id):
    path = transcript_path(session_id)
    if not path:
        return []
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read()
    except OSError:
        return []
    return conversation(data.splitlines())


def state_of(chat):
    """(label, colour) for a chat's row and header."""
    if not chat.get("open"):
        return "closed", 90
    return STATES.get(chat.get("status") or "", (chat.get("status") or "open", 37))


def new_chat(cwd, prompt):
    """Start `claude` in a detached tmux session, so the server can type into it later."""
    name = "replies-" + uuid.uuid4().hex[:4]
    res = subprocess.run([TMUX, "new-session", "-d", "-s", name, "-c", cwd, "-x", "100", "-y", "40",
                          CLAUDE, "--", prompt], capture_output=True, text=True, timeout=10)
    return res.returncode == 0, name


# ---------------------------------------------------------------- panel

class Panel:
    def __init__(self):
        self.view, self.chats, self.chat = "chats", [], None
        self.sel, self.top = 0, 0
        self.lines, self.scroll, self.stick = [], 0, True
        self.draft, self.compose_for, self.compose_cwd = "", None, ""
        self.back_to = "chats"
        self.toast, self.toast_at = "", 0
        self.tap_rows, self.bar = {}, []
        self.cols, self.rows = 44, 24
        self.seen = None
        self.down = False

    # -- output
    def size(self):
        self.cols, self.rows = shutil.get_terminal_size((44, 24))

    def say(self, text):
        self.toast, self.toast_at = text, time.time()

    def actions(self):
        if self.view == "chats":
            return [("Open", "enter"), ("New", "n"), ("Quit", "q")]
        if self.view == "chat":
            acts = [("Back", "b")]
            if self.chat and self.chat.get("open"):
                acts += [("Message", "m"), ("Screen", "s")]
                if self.chat.get("tmux"):
                    acts.append(("Attach", "a"))
            return acts
        if self.view == "screen":
            return [("Back", "b"), ("Message", "m")]
        return [("Cancel", "esc"), ("Send", "enter")]

    def draw(self):
        self.size()
        body_rows = max(3, self.rows - 3)
        title, sub, body = self.render(body_rows)
        out = ["\x1b[?25l"]

        def put(row, text):
            out.append(f"\x1b[{row};1H\x1b[2K{text}\x1b[0m")

        put(1, "\x1b[1m" + clip(title, self.cols))
        put(2, sub)
        for i in range(body_rows):
            put(3 + i, body[i] if i < len(body) else "")
        self.bar, col, labels = [], 1, []
        for label, key in self.actions():
            text = f" {label} "
            if col + width(text) - 1 > self.cols:
                break
            self.bar.append((col, col + width(text) - 1, key))
            labels.append(f"\x1b[7m{text}\x1b[0m")
            col += width(text) + 1
        recent = self.toast and time.time() - self.toast_at < 6
        if recent:
            put(self.rows - 1 if body_rows > 3 else 2, "\x1b[33m" + clip(self.toast, self.cols))
        put(self.rows, " ".join(labels))
        if self.view == "compose":
            lines = self.compose_lines()
            row = min(3 + len(lines) - 1, self.rows - 2)
            out.append(f"\x1b[{row};{min(width(lines[-1]) + 1, self.cols)}H\x1b[?25h")
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def render(self, rows):
        self.tap_rows = {}
        return {"chats": self.render_chats, "chat": self.render_body, "screen": self.render_body,
                "compose": self.render_compose}[self.view](rows)

    def render_chats(self, rows):
        live = sum(1 for c in self.chats if c.get("open"))
        waiting = sum(1 for c in self.chats if c.get("status") == "waiting")
        sub = f"\x1b[2m{live} open" + (f" · {waiting} need you" if waiting else "")
        if self.down:
            sub = "\x1b[31mServer not reachable, retrying"
        if not self.chats:
            return "Replies", sub, ["", "  No chats yet."]
        per = 2
        fit = max(1, rows // per)
        self.top = min(self.top, self.sel)
        if self.sel >= self.top + fit:
            self.top = self.sel - fit + 1
        body = []
        for i, c in enumerate(self.chats[self.top:self.top + fit], self.top):
            on = i == self.sel
            label, colour = state_of(c)
            mark = "\x1b[7m" if on else ""
            name = clip(clean(c.get("title")) or c.get("short") or "chat", self.cols - 3)
            body.append(f"{mark}\x1b[{colour}m●\x1b[0m{mark} {name}")
            where = clean(c.get("project"))
            note = " · no terminal" if c.get("open") and not c.get("reply") else ""
            meta = clip(f"{label}{note}" + (f" · {where}" if where else ""), self.cols - 2)
            body.append(f"  \x1b[2m{meta}")
            self.tap_rows[3 + len(body) - 2] = i
            self.tap_rows[3 + len(body) - 1] = i
        return "Replies", sub, body

    def chat_sub(self):
        c = self.chat or {}
        label, colour = state_of(c)
        pending = next((r for r in reversed(c.get("responses") or [])
                        if r.get("status") in ("queued", "sending", "failed")), None)
        if pending and pending["status"] == "failed":
            return "\x1b[31m" + clip(clean(pending.get("error")) or "Couldn't send", self.cols)
        if pending:
            return "\x1b[33m" + clip(HOLDS.get(pending.get("hold"), "Sending…"), self.cols)
        return f"\x1b[{colour}m" + clip(label, self.cols)

    def render_body(self, rows):
        limit = max(0, len(self.lines) - rows)
        if self.stick:
            self.scroll = limit
        self.scroll = max(0, min(self.scroll, limit))
        title = clean((self.chat or {}).get("title")) or "Chat"
        if self.view == "screen":
            title = "Screen: " + title
        return title, self.chat_sub(), self.lines[self.scroll:self.scroll + rows]

    def compose_lines(self):
        text = self.draft
        lines = []
        while True:
            cut, used = 0, 0
            for ch in text:
                if used + width(ch) >= self.cols:
                    break
                cut, used = cut + 1, used + width(ch)
            lines.append(text[:cut])
            text = text[cut:]
            if not text:
                return lines

    def render_compose(self, rows):
        about = "New chat" if self.compose_for is None \
            else "To: " + (clean(self.compose_for.get("title")) or "chat")
        sub = "\x1b[2m" + clip("Enter sends. Ctrl-U clears.", self.cols)
        return about, sub, self.compose_lines()[-rows:]

    # -- data refresh
    def refresh(self):
        code, data = api("/api/entries")
        self.down = code != 200
        if code != 200:
            return
        chats = [c for c in data.get("chats", [])
                 if c.get("agent") != "pi" and (c.get("open") or c.get("count"))]
        order = {"waiting": 0}
        chats.sort(key=lambda c: (not c.get("open"), order.get(c.get("status"), 1), -(c.get("last") or 0)))
        selected = self.chats[self.sel]["id"] if self.sel < len(self.chats) else None
        self.chats = chats
        ids = [c["id"] for c in chats]
        self.sel = ids.index(selected) if selected in ids else min(self.sel, max(0, len(ids) - 1))
        if self.chat:
            self.chat = next((c for c in chats if c["id"] == self.chat["id"]), self.chat)
        if self.view == "chat":
            self.load_body()
        elif self.view == "screen":
            self.load_screen()

    def load_body(self):
        self.size()
        path = transcript_path(self.chat["id"])
        stamp = (self.cols, os.path.getmtime(path) if path else 0)
        if stamp == self.seen:
            return
        self.seen = stamp
        lines = []
        for role, text in load_conversation(self.chat["id"]):
            you = role == "user"
            lines.append(f"\x1b[1;{35 if you else 36}m{'You' if you else 'Claude'}")
            lines += wrap(text, self.cols - 1)
            lines.append("")
        self.lines = lines or ["", "  Nothing in this chat yet."]

    def load_screen(self):
        code, data = api("/api/screen?chat=" + self.chat["id"])
        text = data.get("text") if code == 200 else data.get("error") or "Couldn't read the screen."
        lines = [clip(clean(ln).rstrip(), self.cols) for ln in str(text).rstrip().split("\n")]
        self.lines = lines

    # -- actions
    def open_chat(self):
        if not self.chats:
            return
        self.chat = self.chats[self.sel]
        self.view, self.seen, self.stick = "chat", None, True
        self.load_body()

    def compose(self, chat):
        if chat is not None and not chat.get("open"):
            self.say("That chat has closed.")
        elif chat is not None and not chat.get("reply"):
            self.say("Not in tmux or cmux. Type in its own terminal.")
        else:
            self.back_to, self.view, self.compose_for, self.draft = self.view, "compose", chat, ""

    def new(self):
        source = self.chats[self.sel] if self.chats and self.sel < len(self.chats) else {}
        self.compose_cwd = source.get("cwd") or os.getcwd()
        if not os.path.isdir(self.compose_cwd):
            self.compose_cwd = os.getcwd()
        self.compose(None)

    def send(self):
        text = self.draft.strip()
        if not text:
            return
        if self.compose_for is None:
            ok, name = new_chat(self.compose_cwd, text)
            self.say(f"Started {name} in {os.path.basename(self.compose_cwd)}" if ok
                     else "Couldn't start tmux.")
            self.view = "chats"
        else:
            code, data = api("/api/respond", {"chat": self.compose_for["id"], "text": text})
            self.say("Queued for the chat." if code == 200 else data.get("error") or "Couldn't send.")
            if code != 200:
                return
            self.view = self.back_to
        self.draft = ""
        self.refresh()

    def attach(self, term):
        info = (self.chat or {}).get("tmux")
        if not info:
            self.say("Only tmux chats can be attached.")
            return
        base = [TMUX, "-S", info["socket"]]
        session = subprocess.run(base + ["display-message", "-p", "-t", info["pane"], "#S"],
                                 capture_output=True, text=True).stdout.strip()
        term.leave()
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}  # may be inside tmux already
        subprocess.run(base + ["attach-session", "-t", "=" + session], env=env)
        term.enter()

    def scroll_by(self, n):
        self.stick = False
        self.scroll = max(0, self.scroll + n)
        if self.scroll >= max(0, len(self.lines) - max(3, self.rows - 3)):
            self.stick = True

    # -- input
    def key(self, k, term):
        """Handle one key. False quits."""
        if k == "ctrl-c":
            return False
        if self.view == "compose":
            if k in ("esc",):
                self.view = self.back_to
            elif k == "enter":
                self.send()
            elif k == "backspace":
                self.draft = self.draft[:-1]
            elif k == "ctrl-u":
                self.draft = ""
            elif len(k) == 1:
                self.draft += k
            return True
        page = max(3, self.rows - 4)
        if self.view == "chats":
            if k in ("q", "esc"):
                return False
            if k in ("down", "j"):
                self.sel = min(self.sel + 1, max(0, len(self.chats) - 1))
            elif k in ("up", "k"):
                self.sel = max(0, self.sel - 1)
            elif k == "pgdn":
                self.sel = min(self.sel + page // 2, max(0, len(self.chats) - 1))
            elif k == "pgup":
                self.sel = max(0, self.sel - page // 2)
            elif k in ("enter", "right"):
                self.open_chat()
            elif k == "n":
                self.new()
            return True
        if k in ("b", "esc", "left", "q"):
            self.view = "chat" if self.view == "screen" else "chats"
            if self.view == "chat":
                self.seen, self.stick = None, True
                self.load_body()
            else:
                self.chat = None
        elif k in ("down", "j"):
            self.scroll_by(1)
        elif k in ("up", "k"):
            self.scroll_by(-1)
        elif k in ("pgdn", " "):
            self.scroll_by(page)
        elif k == "pgup":
            self.scroll_by(-page)
        elif k in ("m", "r", "enter"):
            self.compose(self.chat)
        elif k == "s" and self.view == "chat" and self.chat.get("open"):
            self.view, self.stick = "screen", True
            self.load_screen()
        elif k == "a":
            self.attach(term)
        return True

    def mouse(self, button, col, row, pressed, term):
        if button in (64, 65):
            if self.view == "chats":
                return self.key("down" if button == 65 else "up", term)
            self.scroll_by(3 if button == 65 else -3)
            return True
        if not pressed or button != 0:
            return True
        if row == self.rows:
            hit = next((key for start, end, key in self.bar if start <= col <= end), None)
            return self.key(hit, term) if hit else True
        if self.view == "chats" and row in self.tap_rows:
            self.sel = self.tap_rows[row]
            self.open_chat()
        return True


class Terminal:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        self.decoder = codecs.getincrementaldecoder("utf-8")("ignore")
        self.buf = ""

    def enter(self):
        tty.setraw(self.fd)
        sys.stdout.write("\x1b[?1049h\x1b[2J\x1b[?1000h\x1b[?1006h")
        sys.stdout.flush()

    def leave(self):
        sys.stdout.write("\x1b[?1006l\x1b[?1000l\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def read(self, timeout):
        """Events waiting on the terminal, after at most `timeout` seconds."""
        try:
            ready, _, _ = select.select([self.fd], [], [], timeout)
        except InterruptedError:
            return ["resize"]
        if not ready:
            if self.buf == "\x1b":  # a lone Esc, not the start of a sequence
                self.buf = ""
                return ["esc"]
            return []
        data = os.read(self.fd, 4096)
        if not data:
            raise EOFError
        events, self.buf = parse_input(self.buf + self.decoder.decode(data))
        if self.buf == "\x1b" and not select.select([self.fd], [], [], 0.05)[0]:
            self.buf = ""
            events.append("esc")
        return events


def main():
    if not sys.stdin.isatty():
        sys.exit("replies needs a terminal.")
    code, _ = api("/api/entries")
    if code == 0:
        start_server()
        for _ in range(10):
            time.sleep(0.3)
            if api("/api/entries")[0]:
                break
    term, panel = Terminal(), Panel()
    signal.signal(signal.SIGWINCH, lambda *_: None)
    term.enter()
    try:
        panel.refresh()
        panel.draw()
        last = time.time()
        while True:
            events = term.read(0.5)
            if time.time() - last > REFRESH and panel.view != "compose":
                panel.refresh()
                last = time.time()
                events = events or ["tick"]
            for ev in events:
                if isinstance(ev, tuple):
                    alive = panel.mouse(ev[1], ev[2], ev[3], ev[4], term)
                elif ev in ("tick", "resize"):
                    alive = True
                else:
                    alive = panel.key(ev, term)
                if not alive:
                    return
            if events:
                panel.seen = None if "resize" in events else panel.seen
                if "resize" in events and panel.view == "chat":
                    panel.load_body()
                panel.draw()
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        term.leave()


if __name__ == "__main__":
    main()
