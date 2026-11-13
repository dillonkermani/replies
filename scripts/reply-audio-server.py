#!/usr/bin/env python3
"""Localhost-only audio + decision-tree server for reply-audio.py.

GET /                 -> the page: a vertical tree of spoken replies (newest at top), filterable
                         by chat; question sets (AskUserQuestion, e.g. /ask-me, /grill-me) fan
                         out horizontally on their layer as cards with per-question audio
GET /c/<slug>         -> the same page on one chat's detail view; slug = "<title>-<first 8 of
                         session id>" (or just the 8), so the link survives a chat rename
GET /a/pi             -> the same page on the pi agent's chats; "/" is Claude Code's
GET /api/entries      -> {"entries": [...], "chats": [...]} — the feed the page polls; each
                         entry and chat carries "agent": "claude" or "pi"
GET /latest           -> the newest fully rendered Claude reply mp3 (the footer "play reply" link)
GET /r/<id>.mp3       -> a specific reply or question mp3
GET /samples/<name>.mp3 -> a voice comparison clip from ~/.voicemode/audio/voice-samples
GET /api/screen?chat=<session id> -> read-only text of an open chat's terminal
GET /api/heard        -> {"heard": {"<file>.mp3": 0..1}} — how far each clip has been listened to,
                         shared by every device viewing the page
POST /api/heard       -> merge {"heard": {...}} into that; a clip's level only ever goes up
GET /api/marks        -> {"marks": {"<entry id>": "check"|"star"|"open"}} — marks set by hand on
                         the timeline dots, shared by every device; no mark = checked once heard
POST /api/marks       -> set {"id": "<entry id>", "state": "check"|"star"|"open"|null}; last write wins
POST /api/respond     -> queue a typed or dictated response to a reply or question set, or a
                         message to a chat; a closed chat is reopened in tmux to receive it
POST /api/respond/cancel -> drop a response that has not been delivered yet
POST /api/respond/retry  -> send a stuck response again: requeue one that failed before anything was
                            typed, or let one held by a cmux prompt box that looked full be typed once
POST /api/newchat     -> start a new Claude chat in tmux with {"text"} as its first message
POST /api/stt         -> local Whisper transcription of a recorded clip
GET /api/todo?list=<id> -> {id, name, md, mtime, auto, lists} — one todo list (default master)
GET /api/todos        -> {lists: [{id, name, done, total, auto}], max}
POST /api/todo/<op>   -> toggle|add|edit|remove|undo|chatnote|auto|create|rename|delete; item ops
                         carry the line and text the page saw and get a 409 with the fresh list if
                         the file moved on. Lists are ~/.claude/todo/<id>.md (master.md the default);
                         delete moves the file to ~/.claude/todo/.trash/

Todo auto-update: a thread waits for new finished replies, then a headless Haiku proposes edits
to the lists with auto on (tick, add, note; never delete or untick). Edits that don't match a
real line are dropped; every edit, applied or dropped, goes to ~/.claude/todo/ai-changes.jsonl.

Chats: open Claude Code sessions come from ~/.claude/sessions/<pid>.json (live pid = open);
their names are the session's title from its transcript (custom title from /rename first,
then the auto title), falling back to the session's short name.

pi chats share the reply store but stay apart on the page, behind the agent selector. Its
extension (~/.pi/agent/extensions/replies-local.ts) tags entries "agent": "pi" and lists running
chats in ~/.pi/agent/replies-sessions/<pid>.json; older entries are matched by their session id
against pi's transcripts. pi chats can't be messaged from here: its input box isn't the one
prompt_is_empty recognises, so they are left out of open_sessions and delivery never sees them.

Binds 127.0.0.1 only and rejects any Host header that is not localhost/127.0.0.1, so a web
page in the browser cannot read the feed through DNS rebinding (entries can carry private
data). The page loads nothing from outside this server. Started on demand by reply-audio.py.

Phone access: a second listener on 127.0.0.1:<port - 1> exists only for `tailscale serve`
(tailnet HTTPS, which a phone browser needs before it allows the microphone). It fails closed:
every request must carry this machine's tailnet hostname and the Tailscale-User-Login of the
account that owns this machine. Never point `tailscale funnel` at it. That header check stops
tailnet peers; a local process can forge it, which is why plugin installs also require the key.

Key (plugin installs, or REPLIES_REQUIRE_KEY=1): every data and audio request needs this install's
secret in an X-Replies-Key header. Links carry it as #k=<key>, which no server receives; the page
keeps it in localStorage. Never a cookie: cookies aren't scoped by port, so another 127.0.0.1
listener would get it. The page shell and icons hold no data and load without the key.

Responses go into the chat's own terminal (tmux or cmux), and only when that chat is at an
empty prompt, or after a Retry on a cmux chat whose box looked full; Enter is then pressed only if
the box holds exactly the message. A session in "waiting" has a dialog open, where Enter would
pick the highlighted option (an approval, a merge), so a response stays queued until the chat is
free. Speech is transcribed by whisper-cli on this machine and never leaves it, because it can
contain private data; the browser's own speech recognition is not used for the same reason.

A response written to a closed chat reopens it with `claude --resume` in a detached tmux session
in the folder it started in, then waits for the same empty prompt. Only that response can start
it, and only once: a chat that closes while something is queued for it stays closed.

The `replies` terminal panel (reply-audio-tui.py) is the same thing for SSH. It only talks to
this server, because cmux refuses connections from processes started outside it.
"""
import glob
import hmac
import html
import itertools
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import zlib
from struct import pack as struct_pack
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLIES_HOME = os.environ.get("REPLIES_HOME", "")  # set when installed as a plugin
ROOT = (os.path.join(REPLIES_HOME, "r") if REPLIES_HOME
        else os.path.expanduser("~/.voicemode/audio/reply-web/r"))
# Every request must carry this install's key when REPLIES_REQUIRE_KEY=1, or by default in a plugin
# install (REPLIES_HOME set): 127.0.0.1 is reachable by every account on the Mac, and the server
# can type into chats. REPLIES_REQUIRE_KEY=0 turns it off (the plugin's require_key option).
REQUIRE_KEY = {"1": True, "0": False}.get(os.environ.get("REPLIES_REQUIRE_KEY", ""), bool(REPLIES_HOME))
KEY_FILE = os.path.join(os.path.dirname(ROOT), "key")
KEY = ""
HEARD_FILE = os.path.join(os.path.dirname(ROOT), "heard.json")  # beside ROOT: the hook prunes ROOT by age
STYLE_FILE = os.path.join(os.path.dirname(ROOT), "style.json")  # read by the reply hook for each new reply
VOICE_STYLES = ("relaxed", "standard", "brief")
SESSIONS = os.path.expanduser("~/.claude/sessions")
PROJECTS = os.path.expanduser("~/.claude/projects")
PI_LIVE = os.path.expanduser("~/.pi/agent/replies-sessions")
PI_TRANSCRIPTS = os.path.expanduser("~/.pi/agent/sessions")
PI_TRANSCRIPT = re.compile(r"_([0-9a-f-]{36})\.jsonl$")
PI_TITLE_CHARS = 70
REPLY_MP3 = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}\.mp3$")
ANY_MP3 = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}(-q\d+|-p\d)?\.mp3$")  # -q: question, -p: extra clip
ENTRY_JSON = re.compile(r"^(\d{8}-\d{6}-[0-9a-f]{6})\.json$")
SAMPLES = os.path.expanduser("~/.voicemode/audio/voice-samples")
SAMPLE = re.compile(r"^[a-z0-9_]+\.mp3$")
CHAT_PATH = re.compile(r"^/c/[A-Za-z0-9-]{1,120}$")
RESP_JSON = re.compile(r"^(\d{8}-\d{6}-[0-9a-f]{6})\.resp-[0-9a-f]{8}\.json$")
RESP_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}\.resp-[0-9a-f]{8}$")
ENTRY_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
SESSION_ID = re.compile(r"^[0-9a-f-]{36}$")
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}
MAX_ENTRIES = 300  # per agent, so a busy agent can't push the other's replies out of the feed
MAX_JSON_BODY = 32_000
MAX_AUDIO_BODY = 25_000_000
MAX_RESPONSE_CHARS = 4000
PICKUP_SECONDS = 6 * 3600  # how long to watch for a busy chat to read a message it was given
DELIVERY_POLL = 1.5
CONFIRM_SECONDS = 10
RESUME_SECONDS = 90
RETRY_SECONDS = 30  # a Retry is spent on the next delivery attempt, or lapses after this
NO_TERMINAL = "That chat isn't in tmux or cmux, so nothing can type into it."
CANT_REOPEN = "Couldn't reopen that chat."
# Set inside a chat. A server started by a chat's hook has them, and a resumed chat must not.
SESSION_ENV = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_CHILD_SESSION",
               "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_MESSAGING_SOCKET",
               "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
               "CLAUDE_CODE_SESSION_ID", "TMUX", "TMUX_PANE"}
CMUX = shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
TAILSCALE = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
WHISPER_DIR = os.path.expanduser("~/.voicemode/services/whisper")
WHISPER_CLI = os.path.join(WHISPER_DIR, "build/bin/whisper-cli")
if not os.path.isfile(WHISPER_CLI):
    WHISPER_CLI = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
WHISPER_MODEL_DIRS = [os.path.join(WHISPER_DIR, "models")] + (
    [os.path.join(REPLIES_HOME, "models")] if REPLIES_HOME else [])
SILENCE_DB = -50
WHISPER_MODELS = ("large-v3-turbo", "medium.en", "small.en", "small", "base.en", "base")
TITLE_SCAN_BYTES = 2_000_000
CONV_WINDOW_BYTES = 3_000_000  # transcripts reach tens of MB, so only the tail is read at first
CONV_MAX_WINDOWS = 8           # how far back one request walks to find enough messages
CONV_AFTER_BYTES = 8_000_000
CONV_MIN_ITEMS = 30
CONV_MAX_ITEMS = 200
CONV_MAX_TEXT = 40_000
CONV_TOOL_CHARS = 140
CONV_NOISE = re.compile(r"^\s*<(system-reminder|command-|local-command|task-notification|bash-)")
CONV_SENT_PREFIX = re.compile(r'^(?:Re "[^"]*" \(from the replies page\)|From the replies panel): ')
CSP = ("default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
       "media-src 'self' blob:; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")
STATIC_CSP = CSP.replace("'nonce-{nonce}'", "'none'")  # anything that isn't the page runs no script
_title_cache = {}  # transcript path -> (mtime, title)
_terminals = {}  # (pid, session id) -> where a live chat runs, see terminal_of
_cwds = {}  # session id -> the folder it started in, see resume_cwd
_reopening = {}  # session id -> (when, tmux session), see reopen
_tailnet = {"at": 0, "host": "", "owner": ""}
_resp_lock = threading.Lock()  # a cancel can't race the delivery claiming the same response
_stt_lock = threading.Lock()   # one transcription at a time
_heard_lock = threading.Lock()  # both listeners merge into the one map
_heard = None                   # file name -> fraction listened, loaded on first use


def newest_reply():
    """The newest Claude reply: the footer link is on Claude chats, so it never plays a pi one."""
    files = [f for f in os.listdir(ROOT) if REPLY_MP3.match(f)]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(ROOT, f)), reverse=True)
    pi = pi_transcripts()
    return next((f for f in files
                 if agent_of(load(os.path.join(ROOT, f[:-4] + ".json")) or {}, pi) == "claude"), None)


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def transcript_title(session_id):
    """Latest custom (/rename) or auto title recorded in the session's transcript."""
    paths = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl")) if session_id else []
    if not paths:
        return ""
    path = paths[0]
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = _title_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    custom = auto = ""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - TITLE_SCAN_BYTES))
            for raw in fh:
                if b'"customTitle"' not in raw and b'"aiTitle"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                custom = rec.get("customTitle") or custom
                auto = rec.get("aiTitle") or auto
    except OSError:
        return ""
    title = custom or auto
    _title_cache[path] = (mtime, title)
    return title


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def open_sessions():
    out = {}
    for path in glob.glob(os.path.join(SESSIONS, "*.json")):
        s = load(path)
        if not s or not s.get("sessionId") or not alive(s.get("pid")):
            continue
        out[s["sessionId"]] = s
    return out


def open_pi_sessions():
    out = {}
    for path in glob.glob(os.path.join(PI_LIVE, "*.json")):
        s = load(path)
        if s and s.get("sessionId") and alive(s.get("pid")):
            out[s["sessionId"]] = s
    return out


def pi_transcripts():
    """Session id -> transcript path for every saved pi chat."""
    out = {}
    for path in glob.glob(os.path.join(PI_TRANSCRIPTS, "*", "*.jsonl")):
        m = PI_TRANSCRIPT.search(path)
        if m:
            out[m.group(1)] = path
    return out


def agent_of(entry, pi):
    """Which agent wrote an entry. pi entries from before the tag are matched by session id."""
    return "pi" if entry.get("agent") == "pi" or entry.get("session_id") in pi else "claude"


def pi_title(path):
    """The name a pi chat was given with /name, else its first message: pi has no auto title."""
    try:
        mtime = os.path.getmtime(path or "")
    except OSError:
        return ""
    hit = _title_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    name = first = ""
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b'"session_info"' not in raw and (first or b'"user"' not in raw):
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                msg = rec.get("message") or {}
                if rec.get("type") == "session_info":
                    name = rec.get("name") or ""
                elif not first and msg.get("role") == "user":
                    body = msg.get("content")
                    first = body if isinstance(body, str) else " ".join(
                        b.get("text") or "" for b in body or [] if isinstance(b, dict))
    except OSError:
        return ""
    title = name or clean_response(first)[:PI_TITLE_CHARS]
    _title_cache[path] = (mtime, title)
    return title


def write_json(path, obj):
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    os.replace(tmp, path)


def load_key():
    """This install's key, created owner-only on first start. O_NOFOLLOW: a planted symlink in
    the data folder can't redirect it."""
    try:
        fd = os.open(KEY_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as fh:
            key = fh.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(32)
    tmp = f"{KEY_FILE}.{secrets.token_hex(8)}"  # not the pid: a crashed start's leftover can't block
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key)
    os.replace(tmp, KEY_FILE)
    return key


INSTALLED = os.path.expanduser("~/.claude/plugins/installed_plugins.json")


def installed_server(me):
    """The server script of the installed copy of this plugin from the same marketplace, or ""."""
    versions = os.path.dirname(os.path.dirname(os.path.dirname(me)))  # .../cache/<marketplace>/replies
    try:
        with open(INSTALLED, encoding="utf-8") as fh:
            plugins = json.load(fh).get("plugins") or {}
    except (OSError, ValueError, AttributeError, RecursionError):
        return ""
    if not isinstance(plugins, dict):
        return ""
    for key, installs in plugins.items():
        if key.split("@")[0] != "replies" or not isinstance(installs, list):
            continue
        for inst in installs:
            path = inst.get("installPath") if isinstance(inst, dict) else None
            if not isinstance(path, str) or not path:
                continue
            path = os.path.abspath(path)
            script = os.path.join(path, "scripts", "reply-audio-server.py")
            if os.path.dirname(path) == versions and os.path.isfile(script):
                return script
    return ""


def delivering():
    """True while a response is being typed into a chat: switching then would cut it off."""
    try:
        names = os.listdir(ROOT)
    except OSError:
        return False
    return any(RESP_JSON.match(n) and (load(os.path.join(ROOT, n)) or {}).get("status") == "sending"
               for n in names)


def follow_installs(me=None):
    """A detached server outlives the plugin version that started it. Once another version is
    installed (a plugin update), it re-execs into that version's server: the same process, so it
    stays under cmux, which typing into cmux chats depends on. With its own files gone and no
    install to move to, it stops."""
    me = me or os.path.abspath(__file__)
    while True:
        time.sleep(60)
        try:
            nxt = installed_server(me)
            if nxt and nxt != me:
                sys.stdout.flush()
                sys.stderr.flush()
                # deliver() marks a response "sending" under this lock, so none can start being
                # typed between the check and the exec.
                with _resp_lock:
                    if delivering():
                        continue
                    os.execv(sys.executable, [sys.executable, "-I", nxt, *sys.argv[1:]])
        except Exception as exc:  # a failed exec keeps this version running and watching
            print(f"follow installs: {type(exc).__name__}: {exc}", file=sys.stderr)
        if not os.path.exists(me):
            os._exit(0)


def tailnet_identity():
    """(this machine's tailnet hostname, its owner's login), re-read at most once a minute."""
    if time.time() - _tailnet["at"] > 60:
        _tailnet["at"] = time.time()
        try:
            out = subprocess.run([TAILSCALE, "status", "--json"], capture_output=True,
                                 text=True, timeout=5).stdout
            status = json.loads(out)
            me = status["Self"]
            _tailnet["host"] = me["DNSName"].rstrip(".").lower()
            _tailnet["owner"] = status["User"][str(me["UserID"])]["LoginName"].lower()
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            _tailnet.update(host="", owner="")
    return _tailnet["host"], _tailnet["owner"]


def terminal_of(pid, session_id):
    """Where a live chat runs: {"kind": "tmux", "socket", "pane"}, {"kind": "cmux", "surface"},
    or None. Keyed with the session so a reused pid can't send a response to the wrong terminal.
    tmux wins, because a tmux pane inside cmux is what the chat actually reads from."""
    key = (pid, session_id)
    if key not in _terminals:
        try:
            out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        sock = re.search(r"(?:^|\s)TMUX=([^\s,]+),", out)
        pane = re.search(r"(?:^|\s)TMUX_PANE=(%\d+)(?:\s|$)", out)
        surface = re.search(r"(?:^|\s)CMUX_SURFACE_ID=([0-9A-Fa-f-]{36})(?:\s|$)", out)
        if sock and pane:
            _terminals[key] = {"kind": "tmux", "socket": sock.group(1), "pane": pane.group(1)}
        elif surface:
            _terminals[key] = {"kind": "cmux", "surface": surface.group(1)}
        else:
            _terminals[key] = None
    return _terminals[key]


def resume_cwd(session_id):
    """The folder a closed chat can be resumed from, or "" if it can't. That's the first cwd in
    its transcript: `claude --resume` looks where the chat started, wherever it `cd`ed later."""
    if not SESSION_ID.match(str(session_id or "")):
        return ""
    if session_id not in _cwds:
        paths = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
        if not paths:
            return ""  # not a Claude chat, e.g. one from another agent harness
        found = ""
        try:
            with open(paths[0], "rb") as fh:
                for raw in itertools.islice(fh, 200):
                    if b'"cwd"' in raw:
                        try:
                            found = json.loads(raw).get("cwd") or ""
                        except ValueError:
                            continue
                        if found:
                            break
        except OSError:
            return ""
        _cwds[session_id] = found
    cwd = _cwds[session_id]
    return cwd if cwd and os.path.isdir(cwd) else ""  # a deleted worktree can't be resumed


def full_session_id(short):
    """The full id behind the 8 characters older entries stored, if one transcript matches."""
    if not re.fullmatch(r"[0-9a-f]{8}", str(short or "")):
        return ""
    names = [os.path.basename(p)[:-6]
             for p in glob.glob(os.path.join(PROJECTS, "*", short + "*.jsonl"))]
    return names[0] if len(set(names)) == 1 else ""


def clean_response(text):
    """One line with no control characters: a newline or escape would act on the terminal."""
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def terminal_message(title, text):
    """Always opens with a fixed prefix, so a response can't start a slash command or shell
    escape in the chat's prompt."""
    about = clean_response(title).replace('"', "'")
    return f'Re "{about}" (from the replies page): {text}' if about \
        else f"From the replies panel: {text}"


def prompt_is_empty(screen):
    """True when the chat's input box is on screen with nothing typed in it."""
    lines = [ln.strip() for ln in screen.splitlines()]
    rule = lambda ln: len(ln) > 10 and set(ln) == {"─"}
    return any(ln == "❯" and rule(lines[i - 1]) and rule(lines[i + 1])
               for i, ln in enumerate(lines) if 0 < i < len(lines) - 1)


def cmux(*args):
    return subprocess.run([CMUX, *args], capture_output=True, text=True, timeout=10)


def tmux(term, *args):
    return subprocess.run([TMUX, "-S", term["socket"], *args], capture_output=True, text=True,
                          timeout=10)


SGR = re.compile(r"\x1b\[([0-9;:]*)m")  # tmux writes some styles with colons, e.g. \x1b[0;4:3m
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def drop_dim(screen):
    """The screen without its dim text, and with no escape codes left. Claude dims the suggested
    next prompt it shows in an empty input box, so that suggestion is not something typed."""
    out, dim, pos = [], False, 0
    for m in SGR.finditer(screen):
        if not dim:
            out.append(screen[pos:m.start()])
        codes, i = (m.group(1) or "0").split(";"), 0
        while i < len(codes):
            code = codes[i]
            if code in ("38", "48", "58"):  # a colour's own arguments (5;n or 2;r;g;b) are not attributes
                i += {"5": 3, "2": 5}.get(codes[i + 1] if i + 1 < len(codes) else "", 1)
                continue
            if code == "2":
                dim = True
            elif code in ("", "0", "22"):
                dim = False
            i += 1
        pos = m.end()
    if not dim:
        out.append(screen[pos:])
    return ANSI.sub("", "".join(out))


def read_terminal(term, typed_only=False):
    """The chat's visible screen as text, or None if it can't be reached. typed_only drops dim
    text where the terminal can tell (tmux); cmux only gives plain text."""
    try:
        if term["kind"] == "tmux":
            res = tmux(term, "capture-pane", "-p", *(["-e"] if typed_only else []),
                       "-t", term["pane"])
        else:
            res = cmux("read-screen", "--surface", term["surface"], "--lines", "40")
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return drop_dim(res.stdout) if typed_only and term["kind"] == "tmux" else res.stdout


def type_into(term, message, ok_to_enter=lambda: True):
    """Type one line and press Enter: "sent", "failed", or "held" when ok_to_enter said no after
    the typing, so the text was left in the box and Enter was never pressed."""
    try:
        if term["kind"] == "tmux":
            typed = tmux(term, "send-keys", "-t", term["pane"], "-l", "--", message)
        else:
            # The raw RPC, not `cmux send`: that expands \n and \t even in a doubled backslash,
            # so a Windows path would arrive with real newlines in it.
            params = {"surface_id": term["surface"], "text": message}
            typed = cmux("rpc", "surface.send_text", json.dumps(params))
        if typed.returncode != 0:
            return "failed"
        time.sleep(0.2)
        if not ok_to_enter():
            return "held"
        entered = tmux(term, "send-keys", "-t", term["pane"], "Enter") if term["kind"] == "tmux" \
            else cmux("send-key", "--surface", term["surface"], "enter")
    except (OSError, subprocess.SubprocessError):
        return "failed"
    return "sent" if entered.returncode == 0 else "failed"


def box_holds(screen, message):
    """True when the chat's input box is on screen and starts with the message: what was just
    typed went into the prompt, not into a question that opened meanwhile."""
    head = message[:20]
    return screen is not None and any(
        ln.strip().startswith("❯") and ln.strip()[1:].strip().startswith(head)
        for ln in screen.splitlines())


def prompt_box(screen, one_line=False):
    """What the chat's input box holds, wrapped lines joined, or None if no input box is on screen:
    a ❯ line under a rule, closed by a rule below (right below it when one_line, the shape of an
    empty box showing Claude's suggestion, which a list of options doesn't have)."""
    lines = [ln.strip() for ln in (screen or "").splitlines()]
    rule = lambda ln: len(ln) > 10 and set(ln) == {"─"}
    for i, ln in enumerate(lines):
        if ln.startswith("❯") and 0 < i and rule(lines[i - 1]):
            end = next((j for j in range(i + 1, len(lines)) if rule(lines[j])), None)
            if end is not None and (end == i + 1 or not one_line):
                return " ".join(" ".join([ln[1:]] + lines[i + 1:end]).split())
    return None


def landed(session_id, snippet):
    """True once the chat's transcript holds a user message containing the snippet, or the same
    text typed mid-turn, which Claude records as a queued_command attachment."""
    paths = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
    if not paths:
        return False
    try:
        with open(paths[0], "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - 400_000))
            tail = fh.read()
    except OSError:
        return False
    wanted = json.dumps(snippet, ensure_ascii=False)[1:-1]  # as it appears once JSON-escaped
    for raw in tail.splitlines():
        if b'"user"' not in raw and b'"queued_command"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if rec.get("type") == "user" and wanted in json.dumps(
                (rec.get("message") or {}).get("content"), ensure_ascii=False):
            return True
        att = rec.get("attachment") if rec.get("type") == "attachment" else None
        if isinstance(att, dict) and att.get("type") == "queued_command" \
                and wanted in json.dumps(att.get("prompt"), ensure_ascii=False):
            return True
    return False


def resp_path(rid):
    return os.path.join(ROOT, rid + ".json")


def save_resp(r, **changes):
    r.update(changes)
    write_json(resp_path(r["id"]), r)


def save_queued(r, **changes):
    """save_resp for a response that is still queued, so a cancel can't be undone. False if not."""
    with _resp_lock:
        if (load(resp_path(r["id"])) or {}).get("status") != "queued":
            return False
        save_resp(r, **changes)
    return True


def start_resumed(session_id, cwd):
    """Resume a closed chat in a detached tmux session. Its name, or "" if that failed."""
    name = "replies-" + uuid.uuid4().hex[:4]
    env = {k: v for k, v in os.environ.items()
           if k not in SESSION_ENV and not k.startswith("CMUX_")}
    try:
        res = subprocess.run([TMUX, "new-session", "-d", "-s", name, "-c", cwd, "-x", "100",
                              "-y", "40", CLAUDE, "--resume", session_id],
                             capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    return name if res.returncode == 0 else ""


def start_new_chat(text):
    """(code, payload). A fresh Claude chat in a detached tmux session, with the text as its first
    message, in the folder of the most recently started open chat (home if none is open)."""
    text = clean_response(text)
    if not text:
        return 400, {"error": "Nothing to send."}
    if len(text) > MAX_RESPONSE_CHARS:
        return 413, {"error": f"Keep it under {MAX_RESPONSE_CHARS} characters."}
    newest = max(open_sessions().values(), key=lambda s: s.get("startedAt") or 0, default={})
    cwd = newest.get("cwd") if os.path.isdir(str(newest.get("cwd") or "")) else os.path.expanduser("~")
    env = {k: v for k, v in os.environ.items()
           if k not in SESSION_ENV and not k.startswith("CMUX_")}
    first = re.sub(r"^[\s-]+", "", text)  # a leading dash would be read as a claude flag
    if not first:
        return 400, {"error": "Nothing to send."}
    if first.endswith(";"):
        first = first[:-1] + "\\;"  # tmux reads a trailing ";" as a command separator
    try:
        res = subprocess.run([TMUX, "new-session", "-d", "-s", "replies-" + uuid.uuid4().hex[:4], "-c", cwd,
                              "-x", "100", "-y", "40", CLAUDE, first],
                             capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return 500, {"error": "Couldn't start a chat. Is tmux installed?"}
    if res.returncode != 0:
        return 500, {"error": "Couldn't start a chat."}
    return 200, {"ok": True, "folder": os.path.basename(cwd) or cwd}


def tmux_session(action, name):
    """Run has-session or kill-session on one of our sessions. True if it succeeded."""
    try:
        return subprocess.run([TMUX, action, "-t", "=" + name], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def reopen(r):
    """Start a closed chat for this response, and leave the response queued until the chat is up
    for deliver to type into. One attempt per response, so a broken chat can't respawn forever."""
    sid, now = r["session_id"], time.time()
    at, name = _reopening.get(sid, (0, ""))
    started = r.get("resume_at") or (at if now - at < RESUME_SECONDS else 0)
    if not started:
        cwd = resume_cwd(sid)
        if not save_queued(r, hold="resuming", resume_at=now):
            return  # cancelled: nothing has been started
        name = start_resumed(sid, cwd) if cwd else ""
        if name:
            _reopening[sid] = (now, name)
        else:
            save_queued(r, status="failed", error=CANT_REOPEN, retryable=bool(cwd))
        return
    if not r.get("resume_at") and not save_queued(r, hold="resuming", resume_at=started):
        return
    exited = name and not tmux_session("has-session", name)
    if exited or now - started > RESUME_SECONDS:
        if name:
            tmux_session("kill-session", name)
        _reopening.pop(sid, None)
        save_queued(r, status="failed", error=CANT_REOPEN, retryable=True)


def question_on_screen(entry, screen):
    """True when the screen shows one of this entry's questions, i.e. its dialog is the open one."""
    flat = " ".join(re.sub(r"[│|]", " ", screen or "").split())
    return any(len(q) >= 20 and q[:60] in flat for q in
               (" ".join(clean_response(x.get("question")).split())
                for x in (entry or {}).get("questions") or [] if isinstance(x, dict)))


def close_question(term, entry_id, asking):
    """Press Esc on an open question when the response answers that very question, so the answer
    can be typed in as a message. Only once the screen shows that question: Esc anywhere else
    would interrupt the chat. True once the question has closed."""
    entry = load(os.path.join(ROOT, str(entry_id) + ".json")) if ENTRY_ID.match(str(entry_id)) \
        else None
    tid = (entry or {}).get("tool_use_id") or ""
    # Already settled in the chat: its text can linger in the transcript above another dialog.
    settled = re.match(r"^[A-Za-z0-9_-]{1,100}$", tid) \
        and os.path.exists(os.path.join(ROOT, tid + ".answers.json"))
    if not entry or entry.get("kind") != "questions" or settled \
            or not question_on_screen(entry, read_terminal(term)):
        return False
    try:
        res = tmux(term, "send-keys", "-t", term["pane"], "Escape") if term["kind"] == "tmux" \
            else cmux("send-key", "--surface", term["surface"], "escape")
        if res.returncode != 0:
            return False
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.time() + CONFIRM_SECONDS
    while time.time() < deadline:
        time.sleep(0.5)
        if not asking():
            time.sleep(1)  # let the chat redraw its input box
            return True
    return False


def deliver(r):
    """Try once to put a queued response into its chat; leaves it queued if the chat isn't free."""
    sess = open_sessions().get(r["session_id"])
    if not sess:
        if r.get("reopen"):
            reopen(r)
        else:
            save_queued(r, status="failed", error="That chat has closed.",  # unless cancelled meanwhile
                        retryable=bool(resume_cwd(r["session_id"])))  # a Retry reopens it
        return
    term = terminal_of(sess.get("pid"), r["session_id"])
    if not term:
        save_queued(r, status="failed", error=NO_TERMINAL, retryable=False)
        return
    # A busy chat takes the message too: Claude queues what's typed mid-turn and reads it at the
    # next step. Only an open question holds, because Enter there would pick an option.
    sid = r["session_id"]
    asking = lambda: (open_sessions().get(sid) or {}).get("status") == "waiting"
    hold = "dialog" if asking() else ""
    if hold and r.get("answers") and close_question(term, r.get("entry"), asking):
        hold = ""  # it answered the question that was open; that question is closed now
    # A Retry, spent on this attempt. cmux gives plain text only, so Claude's dim suggested prompt
    # in an empty box reads as typed: a Retry lets it be typed over, as long as an input box is on
    # screen, and Enter then waits for the box to hold exactly the message (no draft mixed in).
    retried = "force_at" in r
    force = time.time() - (r.pop("force_at", None) or 0) < RETRY_SECONDS and not hold
    if not hold:
        screen = read_terminal(term, typed_only=True)
        if screen is None:
            hold = "unreachable"
        elif not prompt_is_empty(screen) and not (force and prompt_box(screen, one_line=True) is not None):
            hold = "draft"
    if hold:
        # Retry only helps where the box can't be read for dim text (cmux), never on a dialog.
        retryable = hold == "draft" and term.get("kind") == "cmux" \
            and prompt_box(screen, one_line=True) is not None
        if r.get("hold") != hold or retried or bool(r.get("retryable")) != retryable:
            save_queued(r, hold=hold, retryable=retryable)
        return
    if not save_queued(r, status="sending", hold="", retryable=False):
        return  # cancelled while the chat was being checked
    message = terminal_message(r.get("title"), r["text"])
    want = " ".join(message.split())
    # Checked again between typing and Enter: a question can open mid-turn at any moment.
    typed = type_into(term, message, lambda: not asking() and (
        prompt_box(read_terminal(term, typed_only=True)) == want if force
        else box_holds(read_terminal(term, typed_only=True), message)))
    if typed == "held":
        save_resp(r, status="failed", error="The prompt box had other text in it, so this was left "
                  "there unsent. Check the chat." if force and not asking() else "A question opened in "
                  "the chat while this was being typed, so it was left in the prompt box unsent. Check the chat.")
        return
    if typed != "sent":
        save_resp(r, status="failed", error="Couldn't reach the chat's terminal.")
        return
    deadline = time.time() + CONFIRM_SECONDS
    while time.time() < deadline:
        if landed(sid, r["text"][:80]):
            save_resp(r, status="sent", sent=time.time())
            return
        time.sleep(0.5)
    screen = read_terminal(term, typed_only=True)
    if screen is not None and prompt_is_empty(screen):
        # Enter took it out of the box but the chat hasn't read it yet: it's in Claude's queue.
        save_resp(r, status="sent", sent=time.time(), pickup=True)
        return
    # Never retried: the text may be in the prompt box, and a second copy would be worse.
    save_resp(r, status="failed", error="Typed into the chat but not confirmed. Check the chat.")


ANSWER_POLL = 5
ANSWER_MAX_AGE = 7 * 86400  # the hook prunes entries after a week
NOT_AN_ANSWER = ("[Request interrupted", "<command-", "<local-command", "<system-reminder", "<bash-",
                 "Caveat:", "<task-notification")
_answer_scan = {}  # tool_use_id -> [transcript, byte offset of its tool_use line, size last read]


def user_text(rec):
    """What the person typed in this transcript record, or '' if it isn't a typed message."""
    if rec.get("type") == "attachment":
        a = rec.get("attachment") or {}
        c = a.get("prompt") if a.get("type") == "queued_command" else ""
    elif rec.get("type") != "user" or rec.get("isMeta") or rec.get("isCompactSummary"):
        return ""
    else:
        c = (rec.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return ""
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    t = (c or "").strip() if isinstance(c, str) else ""
    return "" if t.startswith(NOT_AN_ANSWER) else t


def answer_in_transcript(tid, session_id):
    """How a question was settled in the chat itself: {"answers": ...} when it was answered in the
    terminal dialog but the hook missed it, {"in_chat": text} when the dialog was dismissed and the
    person typed the answer as a message, else None (still open, or nothing typed yet)."""
    st = _answer_scan.get(tid)
    if not st:
        paths = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
        if not paths:
            return None
        st = _answer_scan[tid] = [paths[0], None, -1]
    path, start, seen = st
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == seen:
        return None
    st[2] = size
    needle, rejected = tid.encode(), False
    with open(path, "rb") as fh:
        fh.seek(start or 0)
        pos = start or 0
        for raw in fh:
            here, pos = pos, pos + len(raw)
            if not rejected and needle not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rejected:
                text = user_text(rec)
                if text:
                    return {"in_chat": text[:MAX_RESPONSE_CHARS]}
                continue
            if start is None:
                st[1] = start = here
            for b in (rec.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == tid:
                    tur = rec.get("toolUseResult")
                    if not b.get("is_error") and isinstance(tur, dict) and tur.get("answers"):
                        return {"answers": tur["answers"]}
                    rejected = bool(b.get("is_error"))
    return None


def chat_answer_loop():
    """Polls chats for questions settled in the chat rather than on the page, so a card stops
    asking for answers already given. Writes the same answers file the hook writes."""
    while True:
        time.sleep(ANSWER_POLL)
        try:
            now = time.time()
            for n in os.listdir(ROOT):
                if not ENTRY_JSON.match(n):
                    continue
                e = load(os.path.join(ROOT, n))
                tid, sid = (e or {}).get("tool_use_id") or "", (e or {}).get("session_id") or ""
                if (not e or e.get("kind") != "questions" or not SESSION_ID.match(sid)
                        or not re.match(r"^[A-Za-z0-9_-]{1,100}$", tid)
                        or now - (e.get("created") or 0) > ANSWER_MAX_AGE
                        or os.path.exists(os.path.join(ROOT, tid + ".answers.json"))):
                    continue
                found = answer_in_transcript(tid, sid)
                if found:
                    write_json(os.path.join(ROOT, tid + ".answers.json"),
                               {"tool_use_id": tid, "answers": found.get("answers") or {},
                                "in_chat": found.get("in_chat") or "", "source": "transcript",
                                "created": now})
                    _answer_scan.pop(tid, None)
        except Exception as exc:  # one bad transcript must not stop the thread for good
            print(f"chat answers: {type(exc).__name__}: {exc}", file=sys.stderr)


def delivery_loop():
    while True:
        try:
            paths = [os.path.join(ROOT, n) for n in os.listdir(ROOT) if RESP_JSON.match(n)]
            pending = [(r, p) for p in paths for r in [load(p)] if r]
            # One queued response per chat per pass, so a later one can't overtake an earlier. The ones
            # behind it take the hold (busy, draft, ...) it got, and share its reopen attempt.
            held = {}
            # An answer to a question goes ahead of its chat's other messages: it's what the open
            # question is waiting on, and the messages queued behind it can't go in until it does.
            for _, path in sorted(pending, key=lambda x: (not x[0].get("answers"),
                                                          x[0].get("created") or 0, x[0].get("id") or "")):
                r = load(path)  # re-read: an earlier delivery can wait seconds, and this one may be cancelled meanwhile
                if r and r.get("status") == "queued":
                    sid = r.get("session_id")
                    if sid in held:  # only the chat's first message can be retried
                        if any(r.get(k) != v for k, v in held[sid].items()) or r.get("retryable"):
                            save_queued(r, **held[sid], retryable=False)
                        continue
                    deliver(r)
                    keep = ("hold", "resume_at") if r.get("hold") == "resuming" else ("hold",)
                    held[sid] = {k: r[k] for k in keep if r.get(k)} if r.get("status") == "queued" else {}
                elif r and r.get("status") == "sending":  # server stopped mid-send: don't resend
                    save_resp(r, status="failed", error="Interrupted. Check the chat.")
                elif r and r.get("pickup") and time.time() - (r.get("sent") or 0) < PICKUP_SECONDS \
                        and landed(r.get("session_id"), r["text"][:80]):
                    save_resp(r, pickup=False)  # the chat has read it out of Claude's queue
        except Exception as exc:
            print(f"delivery error: {type(exc).__name__}", file=sys.stderr)
        time.sleep(DELIVERY_POLL)


def queue_response(entry_id, text, chat=None):
    """(code, payload). Writes a response to one reply, or a message to a chat when `chat` is
    given, for delivery_loop to pick up."""
    text = clean_response(text)
    if not text:
        return 400, {"error": "Nothing to send."}
    if len(text) > MAX_RESPONSE_CHARS:
        return 413, {"error": f"Keep it under {MAX_RESPONSE_CHARS} characters."}
    live = open_sessions()
    title = ""
    if chat is not None:
        if not SESSION_ID.match(str(chat)):
            return 400, {"error": "Unknown chat."}
        sid = chat
        entry_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    else:
        if not ENTRY_ID.match(str(entry_id or "")):
            return 400, {"error": "Unknown reply."}
        entry = load(os.path.join(ROOT, entry_id + ".json"))
        if not entry or entry.get("kind") not in ("reply", "questions"):
            return 404, {"error": "Unknown reply."}
        title = entry.get("title") or " / ".join(
            q.get("header") or "" for q in entry.get("questions") or [] if isinstance(q, dict))
        answers = entry.get("kind") == "questions"
        sid = entry.get("session_id") or next(
            (k for k in live if k[:8] == entry.get("session")), "") \
            or full_session_id(entry.get("session"))
    sess = live.get(sid)
    if not sess and not resume_cwd(sid):
        return 409, {"error": "That chat has closed."}
    if sess and not terminal_of(sess.get("pid"), sid):
        return 409, {"error": NO_TERMINAL}
    rid = f"{entry_id}.resp-{uuid.uuid4().hex[:8]}"
    r = {"id": rid, "entry": entry_id, "session_id": sid, "title": title,
         "text": text, "status": "queued", "hold": "", "created": time.time(),
         "reopen": not sess, "answers": chat is None and answers}
    write_json(resp_path(rid), r)
    return 200, public_resp(r)


def retry_response(rid):
    """Send a stuck response again, where deliver() marked it retryable. A failed one goes back in
    the queue, which only happens for failures before anything was typed, so it can't arrive twice.
    A queued one held by a cmux prompt box that looked full gets one try at typing over it."""
    if not RESP_ID.match(str(rid or "")):
        return 400, {"error": "Unknown response."}
    with _resp_lock:
        r = load(resp_path(rid))
        if not r:
            return 404, {"error": "Unknown response."}
        if not r.get("retryable") or not (r.get("status") == "failed" or r.get("hold") == "draft"
                                          and r.get("status") == "queued"):
            return 409, {"error": "That can't be retried. Check the chat."}
        if r.get("status") == "failed":
            for k in ("error", "retryable", "resume_at", "force_at"):
                r.pop(k, None)
            save_resp(r, status="queued", hold="", reopen=True)
        else:
            save_resp(r, force_at=time.time())
    return 200, {"id": rid}


def cancel_response(rid):
    if not RESP_ID.match(str(rid or "")):
        return 400, {"error": "Unknown response."}
    with _resp_lock:
        r = load(resp_path(rid))
        if not r:
            return 404, {"error": "Unknown response."}
        if r.get("status") != "queued":
            return 409, {"error": "It has already gone to the chat."}
        os.remove(resp_path(rid))
    return 200, {"id": rid}


def chat_screen(session_id):
    """(code, payload) for the panel's read-only look at a chat's terminal."""
    sess = open_sessions().get(session_id) if SESSION_ID.match(session_id or "") else None
    term = terminal_of(sess.get("pid"), session_id) if sess else None
    if not term:
        return 404, {"error": NO_TERMINAL}
    text = read_terminal(term)
    if text is None:
        return 502, {"error": "Couldn't read that chat's terminal."}
    return 200, {"text": text}


def tool_summary(name, args):
    """One short line saying what a tool call did. Tool output never reaches the page."""
    if not isinstance(args, dict):
        return ""
    questions = args.get("questions")
    if name == "AskUserQuestion" and isinstance(questions, list) and questions \
            and isinstance(questions[0], dict):
        return clean_response(questions[0].get("question"))[:CONV_TOOL_CHARS]
    for key in ("description", "file_path", "path", "pattern", "command", "url", "query", "prompt"):
        if isinstance(args.get(key), str) and args[key].strip():
            return clean_response(args[key])[:CONV_TOOL_CHARS]
    return ""


def record_time(rec):
    try:
        return datetime.fromisoformat(str(rec.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def conversation_items(lines):
    """What a person would read, from (offset, raw line) pairs of a Claude or pi transcript:
    messages and tool calls, without tool output, thinking, subagents or injected context."""
    items, last = [], 0
    for pos, raw in lines:
        if b'"user"' not in raw and b'"assistant"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        msg = rec.get("message") if isinstance(rec, dict) else None
        if not isinstance(msg, dict) or rec.get("type") not in ("user", "assistant", "message"):
            continue
        role = msg.get("role") or rec.get("type")
        if role not in ("user", "assistant") or rec.get("isMeta") or rec.get("isSidechain") \
                or rec.get("isCompactSummary"):
            continue
        last = record_time(rec) or last
        base = str(rec.get("uuid") or rec.get("id") or pos)
        content = msg.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            item = {"id": f"{base}:{i}", "t": last, "pos": pos}
            if block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if role == "user":
                    if CONV_NOISE.match(text):
                        continue
                    text = CONV_SENT_PREFIX.sub("", text)
                if text:
                    items.append({**item, "role": role, "text": text[:CONV_MAX_TEXT]})
            elif block.get("type") in ("tool_use", "toolCall"):
                name = str(block.get("name") or "tool")
                args = block.get("input") or block.get("arguments")
                items.append({**item, "role": "tool", "name": name,
                              "text": tool_summary(name, args)})
    return items


def read_lines(path, start, stop):
    """([(offset, raw line)], begin, end) for the complete lines in [start, stop). A line cut by
    either edge is left out, so `end` can be passed back as the next `start`."""
    first = max(0, start - 1)
    with open(path, "rb") as fh:
        fh.seek(first)
        data = fh.read(stop - first)
    begin = 0
    if start > 0:
        cut = data.find(b"\n")  # at offset 0 when `start` is already on a line boundary
        if cut < 0:
            return [], start, stop
        data, begin = data[cut + 1:], first + cut + 1
    end = data.rfind(b"\n") + 1
    out, pos = [], begin
    for raw in data[:end].splitlines(keepends=True):
        out.append((pos, raw))
        pos += len(raw)
    return out, begin, begin + end


def chat_conversation(session_id, after=None, before=None):
    """(code, payload) for the chat view: the tail of a transcript, what was added `after` an
    offset, or the stretch `before` one. `start` and `end` are the offsets to page from."""
    if not SESSION_ID.match(session_id or ""):
        return 400, {"error": "Unknown chat."}
    paths = glob.glob(os.path.join(PROJECTS, "*", session_id + ".jsonl"))
    path = paths[0] if paths else pi_transcripts().get(session_id)
    if not path:
        return 404, {"error": "No transcript for that chat."}
    try:
        size = os.path.getsize(path)
        if after is not None:
            if after > size:
                return 200, {"reset": True}  # the file was replaced: offsets no longer apply
            stop = min(size, after + CONV_AFTER_BYTES)
            lines, _, end = read_lines(path, after, stop)
            if end == after and stop - after >= CONV_AFTER_BYTES:
                end = stop  # one line bigger than the read: step past it rather than stall
            start, items = after, conversation_items(lines)
        else:
            start, end, items = size if before is None else min(before, size), None, []
            for _ in range(CONV_MAX_WINDOWS):
                if start <= 0 or len(items) >= CONV_MIN_ITEMS:
                    break
                lines, start, line_end = read_lines(path, max(0, start - CONV_WINDOW_BYTES), start)
                end = line_end if end is None else end
                items = conversation_items(lines) + items
            if len(items) > CONV_MAX_ITEMS:
                cut = len(items) - CONV_MAX_ITEMS
                while cut < len(items) and items[cut]["pos"] == items[cut - 1]["pos"]:
                    cut += 1  # one line can hold several items, and paging is by line
                items = items[cut:]
                start = items[0]["pos"] if items else start
    except OSError:
        return 500, {"error": "Couldn't read that chat."}
    for item in items:
        del item["pos"]
    return 200, {"items": items, "start": start, "end": size if end is None else end}


def public_resp(r):
    return {k: r.get(k) for k in ("id", "entry", "text", "status", "hold", "error", "created",
                                  "pickup", "retryable")}


def whisper_model():
    for name in WHISPER_MODELS:
        for folder in WHISPER_MODEL_DIRS:
            path = os.path.join(folder, f"ggml-{name}.bin")
            if os.path.isfile(path):
                return path
    return None


def transcribe(audio):
    """Text of a recorded clip, using ffmpeg and whisper-cli on this machine only."""
    model = whisper_model()
    if not model or not os.path.isfile(WHISPER_CLI):
        raise RuntimeError("Local Whisper isn't installed.")
    with _stt_lock, tempfile.TemporaryDirectory(prefix="reply-stt-") as tmp:
        clip, wav = os.path.join(tmp, "clip"), os.path.join(tmp, "clip.wav")
        with open(clip, "wb") as fh:
            fh.write(audio)
        conv = subprocess.run([FFMPEG, "-hide_banner", "-y", "-i", clip, "-af", "volumedetect",
                               "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav],
                              check=True, capture_output=True, text=True, timeout=60)
        peak = re.search(r"max_volume: (-?[\d.]+) dB", conv.stderr)
        if peak and float(peak.group(1)) < SILENCE_DB:
            return ""  # Whisper invents a word ("you") from silence, e.g. a muted mic
        out = subprocess.run([WHISPER_CLI, "-m", model, "-f", wav, "-l", "en", "-nt", "-np"],
                             check=True, capture_output=True, text=True, timeout=180).stdout
    return clean_response(re.sub(r"\[[A-Z_ ]+\]|\((?:[a-z ]+)\)", " ", out))


def clean_heard(levels):
    """The well-formed (clip, level) pairs of a map; NaN fails the range check."""
    return {name: round(level, 4) for name, level in levels.items()
            if ANY_MP3.match(name) and type(level) in (int, float) and 0 < level <= 1}


def heard_levels():
    """Caller holds _heard_lock."""
    global _heard
    if _heard is None:
        saved = load(HEARD_FILE)
        _heard = clean_heard(saved) if isinstance(saved, dict) else {}
    return _heard


def heard_json():
    with _heard_lock:
        return json.dumps({"heard": heard_levels()})


def merge_heard(levels):
    """Keep the furthest level per clip, so devices can report in any order and never undo each
    other. Clips the hook has pruned are dropped, which keeps the map the size of the feed."""
    global _heard
    with _heard_lock:
        known = heard_levels()
        ahead = {k: v for k, v in clean_heard(levels).items() if v > known.get(k, 0)}
        if ahead:
            merged = {**known, **ahead}
            _heard = {k: v for k, v in merged.items() if os.path.isfile(os.path.join(ROOT, k))}
            write_json(HEARD_FILE, _heard)


# Tab icons, like the header button: the theme's fill with the play triangle's height at
# size / phi^2, its centroid on the centre. Tab icons are cut to the iPhone app-icon shape (a
# squircle); home-screen (apple-touch) icons run to the edges, because iOS cuts that shape itself
# and fills transparent corners with black. Served as PNG because WebKit
# browsers (Safari, cmux's browser pane) don't show SVG favicons. Drawn on first request, then kept.
ICON_THEMES = {"clay": ("#2a8f9e", "#145564", "#ffc95a"), "violet": ("#7c4dff", "#c084fc", "#ffffff"),
               "clay-dark": ("#45aebd", "#1f6f7c", "#ffd27a"), "violet-dark": ("#a78bfa", "#d946ef", "#ffffff")}
ICON_ROUTES = {"/favicon.ico": ("clay", 64, True), "/favicon.png": ("clay", 64, True),
               "/favicon-violet.png": ("violet", 64, True),
               "/apple-touch-icon.png": ("clay", 180, False), "/apple-touch-icon-violet.png": ("violet", 180, False),
               "/favicon-dark.png": ("clay-dark", 64, True), "/favicon-violet-dark.png": ("violet-dark", 64, True),
               "/apple-touch-icon-dark.png": ("clay-dark", 180, False),
               "/apple-touch-icon-violet-dark.png": ("violet-dark", 180, False)}
SQUIRCLE_N = 5  # superellipse |x|^n + |y|^n <= 1: close to the continuous-corner iPhone icon shape
_icons = {}
_icons_lock = threading.Lock()


def draw_icon(theme, size, squircle=False, ss=4):
    rgb = lambda h: [int(h[i:i + 2], 16) for i in (1, 3, 5)]
    c0, c1, tri = (rgb(c) for c in ICON_THEMES[theme])
    r = size / 2
    th = size / ((1 + 5 ** .5) / 2) ** 2
    tw = th * math.sqrt(3) / 2
    cx = r + tw / 6
    pts = ((cx - tw * 2 / 3, r - th / 2), (cx - tw * 2 / 3, r + th / 2), (cx + tw / 3, r))

    def in_tri(x, y):
        d = [(b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]) for a, b in zip(pts, pts[1:] + pts[:1])]
        return all(v <= 0 for v in d) or all(v >= 0 for v in d)
    rows = []
    for y in range(size):
        row = bytearray(b"\0")
        for x in range(size):
            acc, hits = [0.0, 0.0, 0.0], 0
            for sy in range(ss):
                for sx in range(ss):
                    px, py = x + (sx + .5) / ss, y + (sy + .5) / ss
                    if squircle and abs(px / r - 1) ** SQUIRCLE_N + abs(py / r - 1) ** SQUIRCLE_N > 1:
                        continue  # outside the icon shape: this sample stays transparent
                    if in_tri(px, py):
                        col = tri
                    else:
                        t = min(1, (px + py) / (2 * size))
                        k = max(0, 1 - math.hypot(px - size * .3, py - size * .25) / (size * .55)) * .18
                        col = [(a + (b - a) * t) * (1 - k) + 255 * k for a, b in zip(c0, c1)]
                    acc = [a + c for a, c in zip(acc, col)]
                    hits += 1
            row += bytes([round(a / hits) for a in acc] + [round(255 * hits / ss ** 2)]) if hits else bytes(4)
        rows.append(bytes(row))
    chunk = lambda tag, d: struct_pack(">I", len(d)) + tag + d + struct_pack(">I", zlib.crc32(tag + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct_pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def icon_png(path):
    with _icons_lock:
        if path not in _icons:
            _icons[path] = draw_icon(*ICON_ROUTES[path])
        return _icons[path]


MARKS_FILE = os.path.join(os.path.dirname(ROOT), "marks.json")
MARK_STATES = ("check", "star", "open")
_marks_lock = threading.Lock()


def marks_map():
    """Entry id -> {"s": state, "t": when set}. Caller holds _marks_lock."""
    saved = load(MARKS_FILE)
    if not isinstance(saved, dict):
        return {}
    return {k: v for k, v in saved.items() if ENTRY_ID.fullmatch(k) and isinstance(v, dict)
            and v.get("s") in MARK_STATES and type(v.get("t")) in (int, float)}


def marks_json():
    with _marks_lock:
        return json.dumps({"marks": {k: v["s"] for k, v in marks_map().items()}})


def set_mark(entry_id, state):
    """null clears a hand-set mark (back to auto). Keeps the newest 3000 so the file stays small."""
    if not isinstance(entry_id, str) or not ENTRY_ID.fullmatch(entry_id) \
            or (state is not None and state not in MARK_STATES):
        return 400, {"error": "Bad request."}
    with _marks_lock:
        marks = marks_map()
        if state is None:
            marks.pop(entry_id, None)
        else:
            marks[entry_id] = {"s": state, "t": time.time()}
        keep = sorted(marks.items(), key=lambda kv: kv[1]["t"], reverse=True)[:3000]
        write_json(MARKS_FILE, dict(keep))
    return 200, {"ok": True}


# Todo lists: one plain markdown file per list, <id>.md, so Claude and a person can edit them by hand.
# master.md is the default list other tooling knows by path: its file is never moved or deleted.
TODO_DIR = os.path.expanduser("~/.claude/todo")
TODO_MASTER = "master"
TODO_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")  # fullmatch: no dots, slashes or dotfiles
# Item states: "[ ]" open, "[/]" in progress (the common markdown-tasks marker), "[x]" done.
TODO_BOX = re.compile(r"^(\s*[-*] \[)([ xX/])(\] .*)$")  # toggle: any indent, as before
TODO_ITEM = re.compile(r"^([-*] \[)([ xX/])(\] )(.*)$")  # top-level items: what the page lists
TODO_STATES = {"open": " ", "progress": "/", "done": "x"}
TODO_NOTE = re.compile(r"^(\s+)([-*] )(.*)$")  # an indented line under an item
TODO_LISTITEM = re.compile(r"^\s*[-*] ")
TODO_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f  ]")
# Provenance, kept in the file as an HTML comment at the end of the line (markdown hides it):
# "- [x] text <!-- ai add=<reply id> done=<reply id> -->", "  - note <!-- ai note=<reply id> -->".
# "prog=1" beside done: the item was in progress when the AI ticked it, so undo puts it back there.
TODO_AI = re.compile(r"\s*<!-- ai((?: (?:add|done|note|prog)=[0-9a-f-]{1,40})+) -->\s*$")
TODO_AUTO = re.compile(r"\s*<!--\s*auto:(on|off)\s*-->")  # on the `# ` heading; absent = off
TODO_COMMENT = re.compile(r"\s*<!--.*?-->")
TODO_MAX_LISTS = 30
TODO_MAX_TEXT = 500
TODO_MAX_NAME = 60
TODO_MAX_NOTE = 200
TODO_MAX_BYTES = 256_000
_todo_lock = threading.Lock()


class TodoConflict(Exception):
    """The file changed between our read and our write (Claude or a person edited it)."""


def todo_valid_id(lid):
    return isinstance(lid, str) and bool(TODO_ID.fullmatch(lid))


def todo_path(lid):
    return os.path.join(TODO_DIR, lid + ".md")


def todo_clean(s, cap):
    """Item text or a list name: one line, no control characters, within the cap; else None."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    return s if 0 < len(s) <= cap and not TODO_BAD_CHARS.search(s) else None


def todo_clean_plain(s, cap):
    """todo_clean, and no opening HTML comment: the file keeps provenance and the auto switch in
    comments the page hides, so a list name or model text must not be able to forge or hide one."""
    s = todo_clean(s, cap)
    return s if s is not None and "<!--" not in s else None


def todo_split_ai(text):
    """(text without the provenance comment, {"add"/"done"/"note": reply id})."""
    m = TODO_AI.search(text)
    if not m:
        return text, {}
    return text[:m.start()], dict(p.split("=", 1) for p in m.group(1).split())


def todo_join_ai(text, ai):
    marks = " ".join(f"{k}={ai[k]}" for k in ("add", "done", "note", "prog") if ai.get(k))
    return f"{text} <!-- ai {marks} -->" if marks else text


def _todo_read(lid):
    """(text, sig) where sig pins the version we read; a missing master reads as empty."""
    try:
        with open(todo_path(lid), encoding="utf-8") as fh:
            text = fh.read()
            st = os.fstat(fh.fileno())
        return text, (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        if lid == TODO_MASTER:
            return "", None
        raise


def _todo_write(lid, text, sig):
    """Atomic replace, refused if the file moved on since `sig` was read: the lock only orders this
    server's writes, while Claude edits the file directly. Caller holds _todo_lock."""
    if len(text.encode("utf-8")) > TODO_MAX_BYTES:
        raise ValueError("too big")
    path = todo_path(lid)
    os.makedirs(TODO_DIR, exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex[:6]}.tmp"  # not *.md, so a crash never leaves a phantom list
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:  # checked after the temp file is written, right before the swap, to keep the window small
        st = os.stat(path)
        now = (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        now = None
    if now != sig:
        os.remove(tmp)
        raise TodoConflict()
    os.replace(tmp, path)


def _todo_heading(lines):
    return next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)


def todo_name(text, lid):
    lines = text.split("\n")
    at = _todo_heading(lines)
    name = TODO_COMMENT.sub("", lines[at][2:]).strip() if at is not None else ""
    return name or lid


def todo_auto(text, lid):
    """Whether replies may update this list: only when its heading carries the auto:on marker. Off
    by default for every list, master too, so nothing goes to the model until a person turns it on."""
    lines = text.split("\n")
    at = _todo_heading(lines)
    m = TODO_AUTO.search(lines[at]) if at is not None else None
    return bool(m) and m.group(1) == "on"


def todo_lists():
    """Every list, master first: id, display name (first `# ` heading, else the id), done/total."""
    out = []
    try:
        names = os.listdir(TODO_DIR)
    except OSError:
        names = []
    ids = {n[:-3] for n in names if n.endswith(".md") and TODO_ID.fullmatch(n[:-3])
           and os.path.isfile(os.path.join(TODO_DIR, n))}
    ids.add(TODO_MASTER)
    for lid in ids:
        try:
            text, _ = _todo_read(lid)
        except OSError:
            continue
        boxes = [m.group(2) for m in map(TODO_ITEM.match, text.split("\n")) if m]
        out.append({"id": lid, "name": todo_name(text, lid), "total": len(boxes),
                    "done": sum(b in "xX" for b in boxes), "auto": todo_auto(text, lid)})
    out.sort(key=lambda x: (x["id"] != TODO_MASTER, x["name"].casefold(), x["id"]))
    return out


def todo_json(lid=TODO_MASTER, lists=True):
    """One list's raw markdown, for the checklist above the feed. `md`/`mtime` keep the original shape."""
    try:
        text, sig = _todo_read(lid)
        out = {"id": lid, "name": todo_name(text, lid), "md": text, "auto": todo_auto(text, lid),
               "mtime": sig[0] / 1e9 if sig else 0}
    except OSError:
        out = {"id": lid, "name": lid, "md": "", "auto": False, "mtime": 0, "missing": True}
    if lists:
        out["lists"] = todo_lists()
    return out


def todo_log(entry):
    """Append-only record of every AI change, and of a person undoing one, in the todo folder."""
    try:
        os.makedirs(TODO_DIR, exist_ok=True)
        with open(os.path.join(TODO_DIR, TODO_LOG), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), **entry},
                                ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"todo log not written: {type(exc).__name__}", file=sys.stderr)


def _todo_edit(lid, change, err="The list changed. Reloaded it."):
    """Read-modify-write one list under the lock. `change(lines)` mutates lines in place, or
    returns an error string for a 409 (stale line, gone section). Answers carry the fresh list."""
    if lid is None:
        lid = TODO_MASTER
    if not todo_valid_id(lid):
        return 400, {"error": "Bad request."}
    with _todo_lock:
        try:
            text, sig = _todo_read(lid)
        except OSError:
            return 404, dict(todo_json(TODO_MASTER), error="That list is gone.")
        lines = text.split("\n")
        problem = change(lines)
        if problem:
            return 409, dict(todo_json(lid), error=problem)
        try:
            _todo_write(lid, "\n".join(lines), sig)
        except TodoConflict:
            return 409, dict(todo_json(lid), error=err)
        except ValueError:
            return 413, dict(todo_json(lid), error="That list is too long.")
    return 200, todo_json(lid)


def _todo_find(lines, line, text, top_only=True):
    """The match for the item the page saw at `line`, or None if the file has moved on since."""
    if type(line) is not int or not 0 <= line < len(lines):
        return None
    m = (TODO_ITEM if top_only else TODO_BOX).match(lines[line])
    if not m:
        return None
    return m if (m.group(4) if top_only else m.group(3)[2:]) == text else None


def _todo_item_end(lines, line):
    """The line after an item's indented notes."""
    end = line + 1
    while end < len(lines) and lines[end].strip() and lines[end][:1] in (" ", "\t"):
        end += 1
    return end


def _todo_insert(lines, new_line, section):
    """Insert one line at the end of the `## section` named (first match; "" = the part above the
    first `##`), or at the end of the file for None. An error string if the section is gone."""
    start, end = 0, len(lines)
    if section:
        heads = [i for i, ln in enumerate(lines) if ln.startswith("## ") and ln[3:].strip() == section]
        if not heads:
            return "That section is gone. Reloaded the list."
        start = heads[0] + 1
        end = next((i for i in range(start, len(lines))
                    if lines[i].startswith("## ") or lines[i].startswith("# ")), len(lines))
    elif section == "":
        end = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
    # After the span's last non-blank line, so the notes under its last item stay with it and
    # the blank lines (and the file's trailing newline) after it stay where they were.
    at = next((i + 1 for i in range(end - 1, start - 1, -1) if lines[i].strip()), start)
    new = [new_line]
    prev = lines[at - 1] if at > 0 else ""
    if prev.strip() and not TODO_LISTITEM.match(prev) and not prev.startswith("## "):
        if at < len(lines) and not lines[at].strip():
            at += 1  # after a title or note: reuse the blank line that already follows it
        else:
            new.insert(0, "")
    if at < len(lines) and lines[at].strip():
        new.append("")  # never butt up against the next heading
    lines[at:at] = new
    return None


def _todo_add_note(lines, line, note):
    """Put an indented note at the end of the item's notes, at the indent they already use."""
    end = _todo_item_end(lines, line)
    m = TODO_NOTE.match(lines[end - 1]) if end - 1 > line else None
    lines.insert(end, (m.group(1) if m else "  ") + "- " + note)


def toggle_todo(line, text, done, lid=None, state=None):
    """Set one item open, in progress or done (`state`; pages from before in-progress send only the
    `done` bool). The page sends the line and text it saw; if the file has moved on since, refuse
    rather than change the wrong item. A person changing an AI item takes it over, so its
    provenance comment goes."""
    if state is None and isinstance(done, bool):
        state = "done" if done else "open"
    if type(line) is not int or not isinstance(text, str) or not isinstance(state, str) or state not in TODO_STATES:
        return 400, {"error": "Bad request."}

    def change(lines):
        m = _todo_find(lines, line, text, top_only=False)
        if not m:
            return "The list changed. Reloaded it."
        lines[line] = m.group(1) + TODO_STATES[state] + "] " + todo_split_ai(m.group(3)[2:])[0]
    return _todo_edit(lid, change)


def add_todo(lid, text, section=None):
    """Append an unticked item, under a `## section` if one is named."""
    item = todo_clean(text, TODO_MAX_TEXT)
    if item is None or (section is not None and not isinstance(section, str)):
        return 400, {"error": "Bad request."}
    return _todo_edit(lid, lambda lines: _todo_insert(lines, f"- [ ] {item}", section))


def edit_todo(lid, line, text, new):
    """Rewrite one item's text, keeping its tick and any indented notes under it. The new text
    carries no provenance comment: a person who edits an AI item owns it."""
    item = todo_clean(new, TODO_MAX_TEXT)
    if item is None or type(line) is not int or not isinstance(text, str):
        return 400, {"error": "Bad request."}

    def change(lines):
        m = _todo_find(lines, line, text)
        if not m:
            return "The list changed. Reloaded it."
        lines[line] = m.group(1) + m.group(2) + m.group(3) + item
    return _todo_edit(lid, change)


def remove_todo(lid, line, text):
    """Drop one item and the indented notes that belong to it; every other line stays."""
    if type(line) is not int or not isinstance(text, str):
        return 400, {"error": "Bad request."}

    def change(lines):
        if not _todo_find(lines, line, text):
            return "The list changed. Reloaded it."
        del lines[line:_todo_item_end(lines, line)]
    return _todo_edit(lid, change)


def undo_todo(lid, line, text, kind):
    """One tap on an AI badge: untick what the AI ticked, or remove the item or note it added.
    Logged like the AI's own changes."""
    if kind not in ("add", "done", "note") or type(line) is not int or not isinstance(text, str):
        return 400, {"error": "Bad request."}
    done = {}

    def change(lines):
        if kind == "note":
            m = TODO_NOTE.match(lines[line]) if 0 <= line < len(lines) else None
            if not m or m.group(3) != text or not todo_split_ai(text)[1].get("note"):
                return "The list changed. Reloaded it."
            done["reply"] = todo_split_ai(text)[1]["note"]
            del lines[line]
            return None
        m = _todo_find(lines, line, text)
        body, ai = todo_split_ai(text) if m else ("", {})
        if not ai.get(kind) or (kind == "done" and m.group(2) not in "xX"):
            return "The list changed. Reloaded it."
        done["reply"] = ai.pop(kind)
        if kind == "add":
            end = _todo_item_end(lines, line)
            if any(not todo_split_ai(ln)[1].get("note") for ln in lines[line + 1:end]):
                return "It has notes under it the AI didn’t add. Use Remove if you want it gone."
            del lines[line:end]
        else:
            lines[line] = m.group(1) + ("/" if ai.pop("prog", None) else " ") + m.group(3) + todo_join_ai(body, ai)
    code, out = _todo_edit(lid, change)
    if code == 200:
        todo_log({"by": "person", "op": "undo-" + kind, "list": lid or TODO_MASTER,
                  "item": todo_split_ai(text)[0], "reply": done.get("reply")})
    return code, out


def chat_note_todo(lid, line, text):
    """After the page starts a chat for an item: a dated note under it, so the updater (and a
    person) can see work was started when that chat's replies come in."""
    if type(line) is not int or not isinstance(text, str):
        return 400, {"error": "Bad request."}

    def change(lines):
        if not _todo_find(lines, line, text):
            return "The list changed. Reloaded it."
        _todo_add_note(lines, line, f"chat started {datetime.now():%m/%d %H:%M}")
    return _todo_edit(lid, change)


def set_auto_todo(lid, on):
    """Turn reply-driven updates on or off for one list: a marker on its `# ` heading."""
    if not isinstance(on, bool):
        return 400, {"error": "Bad request."}

    def change(lines):
        at = _todo_heading(lines)
        mark = f" <!-- auto:{'on' if on else 'off'} -->"
        if at is None:
            lines[0:0] = [f"# {lid or TODO_MASTER}{mark}", ""]
        else:
            lines[at] = TODO_AUTO.sub("", lines[at]).rstrip() + mark
    return _todo_edit(lid, change)


def todo_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-") or "list"


def _todo_claim(base):
    """A free id from `base` (base, base-2, ...), claimed by creating its file exclusively."""
    for n in range(1, 100):
        lid = base if n == 1 else f"{base[:44]}-{n}"
        if lid == TODO_MASTER:
            continue
        try:
            return lid, os.open(todo_path(lid), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
    raise OSError("no free id")


def create_todo(name):
    name = todo_clean_plain(name, TODO_MAX_NAME)
    if name is None:
        return 400, {"error": "Give the list a name (up to 60 characters, one line)."}
    with _todo_lock:
        if len(todo_lists()) >= TODO_MAX_LISTS:
            return 409, dict(todo_json(TODO_MASTER), error=f"That's the limit of {TODO_MAX_LISTS} lists.")
        os.makedirs(TODO_DIR, exist_ok=True)
        try:
            lid, fd = _todo_claim(todo_slug(name))
        except OSError:
            return 409, dict(todo_json(TODO_MASTER), error="Couldn't find a free file name.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"# {name}\n\n")
    return 200, todo_json(lid)


def rename_todo(lid, name):
    """Rewrite the list's first `# ` heading (or add one), keeping its auto marker. Other lists also
    move to a file named after the new name, without ever replacing an existing file; master keeps
    its path."""
    name = todo_clean_plain(name, TODO_MAX_NAME)
    if name is None or not todo_valid_id(lid):
        return 400, {"error": "Give the list a name (up to 60 characters, one line)."}

    def change(lines):
        at = _todo_heading(lines)
        if at is None:
            lines[0:0] = [f"# {name}", ""]
        else:
            m = TODO_AUTO.search(lines[at])
            lines[at] = f"# {name}" + (f" <!-- auto:{m.group(1)} -->" if m else "")
    code, out = _todo_edit(lid, change)
    if code != 200 or lid == TODO_MASTER or todo_slug(name) == lid:
        return code, out
    with _todo_lock:
        base = todo_slug(name)
        for n in range(1, 100):
            new = base if n == 1 else f"{base[:44]}-{n}"
            if new == lid:
                break  # e.g. "Errands" again on errands-2: the current file already fits
            if new == TODO_MASTER:
                continue
            try:
                os.link(todo_path(lid), todo_path(new))  # fails if taken: never clobbers
            except FileExistsError:
                continue
            except OSError:
                break  # renamed in place; the file name just stays as it was
            os.unlink(todo_path(lid))
            lid = new
            break
    return 200, todo_json(lid)


def delete_todo(lid):
    """Move the list to TODO_DIR/.trash/ (stamped, so a later list of the same name can't collide).
    Nothing is ever hard-deleted, and master can't be removed."""
    if not todo_valid_id(lid):
        return 400, {"error": "Bad request."}
    if lid == TODO_MASTER:
        return 403, dict(todo_json(TODO_MASTER), error="The master list can't be deleted.")
    with _todo_lock:
        trash = os.path.join(TODO_DIR, ".trash")
        os.makedirs(trash, mode=0o700, exist_ok=True)
        dest = os.path.join(trash, f"{lid}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}.md")
        try:
            os.rename(todo_path(lid), dest)
        except FileNotFoundError:
            return 404, dict(todo_json(TODO_MASTER), error="That list is already gone.")
    return 200, todo_json(TODO_MASTER)


TODO_POSTS = {
    "/api/todo/toggle": lambda r: toggle_todo(r.get("line"), r.get("text"), r.get("done"), r.get("list"),
                                              r.get("state")),
    "/api/todo/add": lambda r: add_todo(r.get("list"), r.get("text"), r.get("section")),
    "/api/todo/edit": lambda r: edit_todo(r.get("list"), r.get("line"), r.get("text"), r.get("new")),
    "/api/todo/remove": lambda r: remove_todo(r.get("list"), r.get("line"), r.get("text")),
    "/api/todo/undo": lambda r: undo_todo(r.get("list"), r.get("line"), r.get("text"), r.get("kind")),
    "/api/todo/chatnote": lambda r: chat_note_todo(r.get("list"), r.get("line"), r.get("text")),
    "/api/todo/auto": lambda r: set_auto_todo(r.get("list"), r.get("on")),
    "/api/todo/create": lambda r: create_todo(r.get("name")),
    "/api/todo/rename": lambda r: rename_todo(r.get("list"), r.get("name")),
    "/api/todo/delete": lambda r: delete_todo(r.get("list")),
}


# Reply-driven updates. A background thread watches for replies that finished rendering, waits for
# a quiet spell, then asks a small headless Claude for edits to the lists that have auto on. It may
# only tick an open item, add an item, or add a note under one; anything else, or anything that
# doesn't match a real line, is dropped. Every applied and dropped edit is logged.
TODO_LOG = "ai-changes.jsonl"
TODO_STATE = ".auto-state.json"
AUTO_MODEL = "haiku"  # cheap and plenty for matching replies to items
AUTO_TIMEOUT = 120
AUTO_BUDGET_USD = "0.10"  # per run; the CLI stops the call past this
AUTO_POLL = 10
AUTO_QUIET = 60  # seconds after the newest new reply, so a burst of replies is one run
AUTO_MAX_WAIT = 300  # but never sit on a reply longer than this while replies keep coming
AUTO_MAX_AGE = 12 * 3600  # older unseen replies (server was down) are skipped, not replayed
AUTO_BATCH = 6
AUTO_MAX_EDITS = 12
AUTO_MAX_ADDS = 4
AUTO_REPLY_CHARS = 3000
AUTO_PROMPT = """You keep a person's markdown todo lists in step with what their coding assistant \
reports. You get recent assistant replies (spoken summaries, each with its chat's title) and the \
current lists with numbered lines. Propose only edits the replies clearly support.

Answer with JSON only, no prose and no code fence: {"edits": [...]}. Each edit is one of:
{"op":"check","list":"<list id>","line":<n>,"text":"<the item's text, copied exactly>","reply":"<reply id>","reason":"<why>"}
  - the reply clearly says this open item is finished (merged, shipped, done, answered).
{"op":"note","list":"<list id>","line":<n>,"text":"<the item's text, copied exactly>","note":"<short status>","reply":"<reply id>","reason":"<why>"}
  - a short status update on an existing item, under 140 characters.
{"op":"add","list":"<list id>","section":"<a ## heading of that list, copied exactly, or null if it has none>","text":"<new item>","reply":"<reply id>","reason":"<why>"}
  - the reply clearly creates a new open follow-up or an action someone still owes, and no item \
already covers it. Under 120 characters, in the list's own style.

Rules: never delete or untick anything, and never touch a line you were not shown. Lines are \
"L<n>: <line>"; items are "- [ ] text" (open), "- [/] text" (in progress, still open: it can be \
checked) or "- [x] text" (done). Prefer a note over a new \
item when an item already covers it. At most 4 adds. Reasons under 100 characters. When unsure, \
leave it out. Return {"edits": []} when nothing applies."""


def todo_claude(system_prompt, user_input):
    """Headless, tool-less, hook-less Claude call, as reply-audio.py's ask_claude does it: hooks off
    and REPLY_AUDIO_CHILD set, or the reply hook would voice this run as a reply. Safe mode keeps
    CLAUDE.md and memory out (not needed, and they'd multiply the cost). stdout or None."""
    env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV and not k.startswith("CMUX_")}
    env["REPLY_AUDIO_CHILD"] = "1"
    try:
        res = subprocess.run(
            [CLAUDE, "-p", "--model", AUTO_MODEL, "--tools", "", "--strict-mcp-config",
             "--disable-slash-commands", "--no-session-persistence", "--safe-mode",
             "--max-budget-usd", AUTO_BUDGET_USD,
             "--settings", '{"disableAllHooks": true}', "--system-prompt", system_prompt],
            input=user_input, capture_output=True, text=True, timeout=AUTO_TIMEOUT,
            cwd=os.path.dirname(ROOT), env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"todo auto: claude call failed: {type(exc).__name__}", file=sys.stderr)
        return None
    if res.returncode != 0:
        print(f"todo auto: claude rc={res.returncode} {res.stderr.strip()[-200:]!r}", file=sys.stderr)
        return None
    return res.stdout.strip()


def _auto_parse(out):
    """The edits list from the model's answer, or None if it isn't the JSON asked for."""
    if not out:
        return None
    s = out.strip()
    if s.startswith("```"):
        s = s.strip("`").partition("\n")[2] if "\n" in s else ""
    a, b = s.find("{"), s.rfind("}")
    try:
        obj = json.loads(s[a:b + 1]) if a >= 0 else None
    except ValueError:
        return None
    edits = obj.get("edits") if isinstance(obj, dict) else None
    return [e for e in edits if isinstance(e, dict)] if isinstance(edits, list) else None


def _auto_check(lines, e, rids):
    """Why this edit can't go in (a short reason), or None. Run against the file as it is now, so a
    line that moved or changed since the model saw it fails the same line+text guard as toggle."""
    op = e.get("op")
    if op not in ("check", "note", "add"):
        return f"op {op!r} not allowed"
    if e.get("reply") not in rids:
        return "unknown reply"
    if op == "add":
        text = todo_clean_plain(e.get("text"), 120)
        if text is None:
            return "bad text"
        have = {todo_split_ai(m.group(4))[0].strip().casefold()
                for m in map(TODO_ITEM.match, lines) if m}
        if text.casefold() in have:
            return "already listed"
        sec = e.get("section")
        heads = [ln[3:].strip() for ln in lines if ln.startswith("## ")]
        if sec is None and heads:
            return "no section given"
        if sec is not None and sec not in heads:
            return "no such section"
        return None
    line = e.get("line")
    if type(line) is not int or not 0 <= line < len(lines):
        return "no such line"
    m = TODO_ITEM.match(lines[line])
    if not m or todo_split_ai(m.group(4))[0] != e.get("text"):
        return "line doesn't match"
    if op == "check" and m.group(2) in "xX":
        return "already ticked"
    if op == "note" and todo_clean_plain(e.get("note"), TODO_MAX_NOTE) is None:
        return "bad note"
    return None


def auto_apply(lid, edits, rids):
    """Apply one list's edits in one atomic write; returns [(edit, None or why dropped)]. Ticks
    change no line numbers, notes go bottom-up so earlier lines keep theirs, adds go last (by
    section name, not line)."""
    results = []
    with _todo_lock:
        try:
            text, sig = _todo_read(lid)
        except OSError:
            return [(e, "list gone") for e in edits]
        if not todo_auto(text, lid):
            return [(e, "auto is off") for e in edits]
        lines = text.split("\n")
        ok, adds, seen = [], 0, set()
        for e in edits:
            why = _auto_check(lines, e, rids)
            key = (e.get("op"), e.get("line"), e.get("text"), e.get("note"))
            if not why and key in seen:
                why = "duplicate"
            if not why and e.get("op") == "add":
                adds += 1
                why = "too many adds" if adds > AUTO_MAX_ADDS else None
            seen.add(key)
            results.append((e, why))
            if not why:
                ok.append(e)
        for e in (e for e in ok if e["op"] == "check"):
            m = TODO_ITEM.match(lines[e["line"]])
            body, ai = todo_split_ai(m.group(4))
            lines[e["line"]] = m.group(1) + "x" + m.group(3) + todo_join_ai(body, dict(ai, done=e["reply"], prog="1" if m.group(2) == "/" else None))
        for e in sorted((e for e in ok if e["op"] == "note"), key=lambda e: -e["line"]):
            _todo_add_note(lines, e["line"], todo_join_ai(todo_clean_plain(e["note"], TODO_MAX_NOTE), {"note": e["reply"]}))
        for e in (e for e in ok if e["op"] == "add"):
            _todo_insert(lines, "- [ ] " + todo_join_ai(todo_clean_plain(e["text"], 120), {"add": e["reply"]}),
                         e["section"])
        if ok:
            try:
                _todo_write(lid, "\n".join(lines), sig)
            except (TodoConflict, ValueError):
                return [(e, why or "list changed while writing") for e, why in results]
    return results


def _auto_input(replies, lists):
    parts = ["<lists>"]
    for lid, text in lists:
        parts.append(f'<list id="{lid}" name="{todo_name(text, lid)}">')
        parts += [f"L{i}: {TODO_AI.sub('', ln)}" for i, ln in enumerate(text.split("\n")) if ln.strip()]
        parts.append("</list>")
    parts += ["</lists>", "<replies>"]
    for r in replies:
        parts.append(f'<reply id="{r["id"]}" chat="{r.get("chat") or ""}">\n{r.get("title") or ""}\n'
                     f'{(r.get("script") or "")[:AUTO_REPLY_CHARS]}\n</reply>')
    parts.append("</replies>")
    return "\n".join(parts)


def auto_run(replies, call=None):
    """One updater run over a batch of replies. Returns the applied edits."""
    call = call or todo_claude  # looked up at call time, so a stub set on the module wins
    rids = {r["id"] for r in replies}
    lists = []
    for x in todo_lists():
        if x["auto"]:
            try:
                lists.append((x["id"], _todo_read(x["id"])[0]))
            except OSError:
                pass
    if not lists:
        return []
    edits = _auto_parse(call(AUTO_PROMPT, _auto_input(replies, lists)))
    if edits is None:
        todo_log({"by": "ai", "op": "run", "replies": sorted(rids), "result": "no usable answer"})
        return []
    applied, by_list = [], {}
    for e in edits[:AUTO_MAX_EDITS]:
        if not all(isinstance(v, (str, int, float, bool, type(None))) for v in e.values()):
            todo_log({"by": "ai", "op": str(e.get("op")), "list": str(e.get("list")), "result": "dropped: bad fields"})
            continue  # a list or object where a value belongs would raise further on and lose the run
        by_list.setdefault(e.get("list"), []).append(e)
    for e in edits[AUTO_MAX_EDITS:]:
        todo_log({"by": "ai", "op": e.get("op"), "list": e.get("list"), "result": "dropped: too many edits"})
    auto_ids = {lid for lid, _ in lists}
    for lid, group in by_list.items():
        results = auto_apply(lid, group, rids) if lid in auto_ids \
            else [(e, "list not auto-updated") for e in group]
        for e, why in results:
            todo_log({"by": "ai", "op": e.get("op"), "list": lid, "line": e.get("line"),
                      "item": e.get("text"), "note": e.get("note"), "section": e.get("section"),
                      "reply": e.get("reply"), "reason": str(e.get("reason") or "")[:200],
                      "result": "applied" if why is None else "dropped: " + why})
            if why is None:
                applied.append(e)
    todo_log({"by": "ai", "op": "run", "replies": sorted(rids), "result": f"{len(applied)} applied"})
    return applied


class TodoUpdater:
    """Which replies have been handled lives in TODO_DIR/.auto-state.json, so a restart never
    handles one twice. The first start marks every existing reply handled: no history replay."""

    def __init__(self, call=None):
        self.call, self.waiting_since = call or (lambda s, u: todo_claude(s, u)), 0
        state = load(os.path.join(TODO_DIR, TODO_STATE))
        if isinstance(state, dict) and isinstance(state.get("seen"), list):
            self.seen = {s for s in state["seen"] if isinstance(s, str)}
        else:
            self.seen = {m.group(1) for m in map(ENTRY_JSON.match, self._names()) if m}
            self.save()

    @staticmethod
    def _names():
        try:
            return os.listdir(ROOT)
        except OSError:
            return []

    def save(self):
        os.makedirs(TODO_DIR, exist_ok=True)
        present = {m.group(1) for m in map(ENTRY_JSON.match, self._names()) if m}
        if present:
            self.seen &= present  # the hook prunes replies after a week; forget those too
        write_json(os.path.join(TODO_DIR, TODO_STATE), {"seen": sorted(self.seen)})

    def pending(self, now):
        """New finished replies, oldest first, as (entry, file mtime). Entries that will never be a
        finished reply (questions, failed, too old) are marked seen on the way."""
        out, skipped = [], False
        for n in self._names():
            m = ENTRY_JSON.match(n)
            if not m or m.group(1) in self.seen:
                continue
            path = os.path.join(ROOT, n)
            e = load(path)
            if not isinstance(e, dict) or e.get("status") in ("rendering", "voicing", "summarizing"):
                continue
            if e.get("kind") != "reply" or e.get("status") != "ready" or not e.get("script") \
                    or now - (e.get("created") or 0) > AUTO_MAX_AGE:
                self.seen.add(m.group(1))
                skipped = True
                continue
            try:
                out.append((e, os.path.getmtime(path)))
            except OSError:
                pass
        if skipped:
            self.save()
        return sorted(out, key=lambda x: x[0].get("created") or 0)

    def tick(self, now=None):
        """One look; runs the model when a quiet spell has passed. Returns the applied edits."""
        now = time.time() if now is None else now
        todo = self.pending(now)
        if not todo:
            self.waiting_since = 0
            return []
        self.waiting_since = self.waiting_since or now
        if now - max(t for _, t in todo) < AUTO_QUIET and now - self.waiting_since < AUTO_MAX_WAIT:
            return []
        batch = [dict(e, chat=transcript_title(e["session_id"]) if SESSION_ID.fullmatch(
            str(e.get("session_id") or "")) else "") for e, _ in todo[:AUTO_BATCH]]
        try:
            applied = auto_run(batch, self.call)
        finally:  # handled even when the run fails: a failing model must not be retried forever
            self.seen |= {e["id"] for e in batch}
            self.save()
            self.waiting_since = 0 if len(todo) <= AUTO_BATCH else now - AUTO_MAX_WAIT
        return applied


def todo_auto_loop():
    try:
        updater = TodoUpdater()
    except Exception as exc:
        print(f"todo auto: not started: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    print("todo auto: watching for replies", file=sys.stderr)
    while True:
        time.sleep(AUTO_POLL)
        try:
            updater.tick()
        except Exception as exc:  # one bad run must not stop the thread for good
            print(f"todo auto: {type(exc).__name__}: {exc}", file=sys.stderr)


def todo_post(path, req):
    return TODO_POSTS[path](req)


def clip_script(file):
    """The spoken script of one clip (a reply, one of its parts, or one question), for captions."""
    if not isinstance(file, str) or not ANY_MP3.match(file):
        return 400, {"error": "Bad request."}
    e = load(os.path.join(ROOT, file[:22] + ".json"))
    if not e:
        return 404, {"error": "Not found."}
    for clip in [e] + list(e.get("parts") or []) + list(e.get("questions") or []):
        if isinstance(clip, dict) and clip.get("audio") == file and isinstance(clip.get("script"), str):
            out = {"text": clip["script"]}
            if clip is e:  # a reply's own clip may carry word timings and a visual plan
                out.update({k: e[k] for k in ("words", "visuals") if isinstance(e.get(k), list)})
            return 200, out
    return 404, {"error": "Not found."}


def voice_style():
    saved = load(STYLE_FILE)
    style = saved.get("style") if isinstance(saved, dict) else None
    return style if style in VOICE_STYLES else "standard"


def set_voice_style(style):
    """Relaxed, standard or brief: how new replies are written and voiced (see reply-audio.py STYLES)."""
    if style not in VOICE_STYLES:
        return 400, {"error": "Bad request."}
    write_json(STYLE_FILE, {"style": style})
    return 200, {"style": style}


def entries_and_chats():
    names = os.listdir(ROOT)
    responses, by_chat = {}, {}
    for n in names:
        m = RESP_JSON.match(n)
        r = load(os.path.join(ROOT, n)) if m else None
        if r:
            responses.setdefault(m.group(1), []).append(public_resp(r))
            by_chat.setdefault(r.get("session_id"), []).append(public_resp(r))
    answers = {}
    for n in names:
        if n.endswith(".answers.json"):
            a = load(os.path.join(ROOT, n))
            if a and a.get("tool_use_id"):
                answers[a["tool_use_id"]] = a
    out, described = [], set()
    for n in names:
        m = ENTRY_JSON.match(n)
        if not m:
            continue
        e = load(os.path.join(ROOT, n))
        if not e:
            continue
        described.add(m.group(1) + ".mp3")
        for k in ("script", "words", "visuals"):  # captions fetch these one clip at a time (/api/script), so the list stays small
            e.pop(k, None)
        for q in (e.get("questions") or []) + (e.get("parts") or []):
            q.pop("script", None)
        if e.get("kind") == "questions":
            a = answers.get(e.get("tool_use_id")) or {}
            e["answers"], e["in_chat"] = a.get("answers") or {}, a.get("in_chat") or ""
        e["responses"] = sorted(responses.get(m.group(1), []), key=lambda r: r["created"] or 0)
        out.append(e)
    for n in names:  # replies rendered before the page existed: audio only, no chat
        if REPLY_MP3.match(n) and n not in described:
            out.append({"id": n[:-4], "kind": "reply", "status": "ready", "audio": n,
                        "created": os.path.getmtime(os.path.join(ROOT, n)), "legacy": True})
    out.sort(key=lambda e: e.get("created") or 0, reverse=True)
    pi = pi_transcripts()
    kept, per_agent = [], {}
    for e in out:
        e["agent"] = agent_of(e, pi)
        per_agent[e["agent"]] = per_agent.get(e["agent"], 0) + 1
        if per_agent[e["agent"]] <= MAX_ENTRIES:
            kept.append(e)
    out = kept

    live = open_sessions()
    chats = {}
    for sid, s in open_pi_sessions().items():
        chats[sid] = {"id": sid, "short": sid[:8], "agent": "pi", "open": True,
                      "status": s.get("status") or "", "fallback": "", "count": 0, "last": 0,
                      "project": os.path.basename((s.get("cwd") or "").rstrip("/")),
                      "cwd": s.get("cwd") or "", "reply": False, "terminal": "", "tmux": None}
    for sid, s in live.items():
        chats[sid] = {"id": sid, "short": sid[:8], "agent": "claude", "open": True,
                      "status": s.get("status") or "",
                      "project": os.path.basename((s.get("cwd") or "").rstrip("/")),
                      "fallback": s.get("name") or "", "count": 0, "last": 0,
                      "cwd": s.get("cwd") or ""}
        term = terminal_of(s.get("pid"), sid)
        chats[sid].update(reply=bool(term), terminal=term["kind"] if term else "",
                          tmux=term if term and term["kind"] == "tmux" else None)
    by_short = {sid[:8]: sid for sid in chats}
    for e in out:
        if e.get("session_id"):
            by_short.setdefault(e["session_id"][:8], e["session_id"])
    for e in out:
        short = e.get("session") or ""
        if short and short not in by_short:  # closed chat from before full ids were stored
            by_short[short] = full_session_id(short) or short
        sid = e.get("session_id") or by_short.get(short)
        if not sid:
            continue
        e["chat"] = sid
        c = chats.setdefault(sid, {"id": sid, "short": sid[:8], "agent": e["agent"],
                                   "open": False, "status": "",
                                   "project": e.get("project") or "", "fallback": "",
                                   "count": 0, "last": 0})
        c["count"] += 1
        c["last"] = max(c["last"], e.get("created") or 0)
    for c in chats.values():
        recent = sorted(by_chat.get(c["id"], []), key=lambda r: r["created"] or 0)
        c["responses"] = recent[-5:]
        c["resume"] = not c["open"] and bool(resume_cwd(c["id"]))
        named = pi_title(pi.get(c["id"])) if c["agent"] == "pi" \
            else transcript_title(c["id"]) if len(c["id"]) > 8 else ""
        c["title"] = named or c["fallback"] or c["project"] or c["short"]
    ordered = sorted(chats.values(), key=lambda c: (not c["open"], -c["last"]))
    return {"entries": out, "chats": ordered}


class Handler(BaseHTTPRequestHandler):
    local_port = 8799
    tailnet_port = None  # the listener `tailscale serve` targets

    def _send(self, code, ctype, data, extra=None, csp=STATIC_CSP):
        if code == 200 and self.command == "GET" and ctype.startswith(("application/json", "text/html")):
            code, data, extra = self._shrink(data, extra)
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _send_page(self):
        # A fresh nonce per load: only the page's own two scripts run, so markup that slips into
        # the feed (a reply quoting a malicious page) can't execute and reach /api/respond.
        nonce = secrets.token_urlsafe(18)
        body = PAGE.replace("<script>", f'<script nonce="{nonce}">').encode()
        self._send(200, "text/html; charset=utf-8", body, csp=CSP.format(nonce=nonce))

    def _key_ok(self):
        if not REQUIRE_KEY:
            return True
        sent = self.headers.get("X-Replies-Key") or ""
        return bool(sent) and bool(KEY) and hmac.compare_digest(sent.encode(), KEY.encode())

    def _key_gate(self, path):
        """True if the request may go on. Only a header carries the key: cookies aren't scoped by
        port, so any other 127.0.0.1 listener (another account's, too) would receive one. The page
        shell and icons hold no data and load without it; the page's script reads the key from the
        link's #k= fragment (never sent to a server), keeps it in this origin's localStorage and
        sends it on every request."""
        if not REQUIRE_KEY or path in ICON_ROUTES:
            return True
        if self.command == "GET" and (path in ("/", "/index.html", "/a/pi") or CHAT_PATH.match(path)):
            return True
        if self._key_ok():
            return True
        self._send(401, "application/json", json.dumps({"error": "key required"}).encode())
        return False

    def send_error(self, code, message=None, explain=None):
        # The stdlib version writes its own headers, so errors went out with no CSP or nosniff.
        short, long = self.responses.get(code, ("???", "???"))
        self.close_connection = True
        body = self.error_message_format % {"code": code, "message": html.escape(message or short, False),
                                            "explain": html.escape(explain or long, False)}
        self._send(code, self.error_content_type, body.encode("utf-8", "replace"), {"Connection": "close"})

    def _shrink(self, data, extra):
        # A phone polls /api/entries (~300 KB) every 2.5 s, often over cellular. The page sends the
        # last ETag back itself (no-store keeps the browser cache out of it), so an unchanged body is
        # a bodiless 304, and a changed one goes gzipped.
        tag = '"%08x%08x%x"' % (zlib.crc32(data), zlib.adler32(data), len(data))
        extra = dict(extra or {}, ETag=tag, Vary="Accept-Encoding")
        if self.headers.get("If-None-Match") == tag:
            return 304, b"", extra
        if len(data) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            z = zlib.compressobj(6, zlib.DEFLATED, 31)
            data = z.compress(data) + z.flush()
            extra["Content-Encoding"] = "gzip"
        return 200, data, extra

    def _allowed(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if self.server.server_address[1] == Handler.tailnet_port:
            ts_host, owner = tailnet_identity()
            ok = bool(owner) and host == ts_host \
                and (self.headers.get("Tailscale-User-Login") or "").lower() == owner
        else:
            ok = host in ALLOWED_HOSTS
        if not ok:
            self.send_error(403, "not allowed")
        return ok

    def do_GET(self):
        if not self._allowed():
            return
        # Another site (another localhost port is "same-site") may link here, since a top-level
        # navigation can't read the page, but not embed it (iframe, <audio src>) or fetch it.
        if (self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site")
                and (self.headers.get("Sec-Fetch-Mode") != "navigate"
                     or self.headers.get("Sec-Fetch-Dest") != "document")):
            self.send_error(403, "localhost only")
            return
        path = self.path.split("?", 1)[0]
        if not self._key_gate(path):
            return
        if path in ("/", "/index.html", "/a/pi") or CHAT_PATH.match(path):
            self._send_page()
            return
        if path == "/api/entries":
            self._send(200, "application/json", json.dumps(entries_and_chats()).encode())
            return
        if path == "/api/heard":
            self._send(200, "application/json", heard_json().encode())
            return
        if path == "/api/marks":
            self._send(200, "application/json", marks_json().encode())
            return
        if path in ("/api/todo", "/api/todos"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            lid = (query.get("list") or [TODO_MASTER])[0]
            if path == "/api/todos":
                code, out = 200, {"lists": todo_lists(), "max": TODO_MAX_LISTS}
            elif not todo_valid_id(lid):
                code, out = 400, {"error": "Bad request."}
            else:
                code, out = 200, todo_json(lid)
                if out.get("missing") and lid != TODO_MASTER:  # deleted elsewhere: page falls back
                    code, out = 404, dict(todo_json(TODO_MASTER), error="That list is gone.")
            self._send(code, "application/json", json.dumps(out).encode())
            return
        if path == "/api/script":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            code, out = clip_script((query.get("file") or [""])[0])
            self._send(code, "application/json", json.dumps(out).encode())
            return
        if path == "/api/style":
            self._send(200, "application/json", json.dumps({"style": voice_style()}).encode())
            return
        if path == "/api/screen":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            code, out = chat_screen((query.get("chat") or [""])[0])
            self._send(code, "application/json", json.dumps(out).encode())
            return
        if path == "/api/conversation":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            offset = lambda name: int(query[name][0]) if query.get(name, [""])[0].isdigit() else None
            code, out = chat_conversation((query.get("chat") or [""])[0], offset("after"),
                                          offset("before"))
            self._send(code, "application/json", json.dumps(out).encode())
            return
        if path in ICON_ROUTES:
            self._send(200, "image/png", icon_png(path))
            return
        if path in ("/latest", "/latest.mp3"):
            name = newest_reply()
            full = os.path.join(ROOT, name) if name else None
        elif path.startswith("/r/") and ANY_MP3.match(path[3:]):
            full = os.path.join(ROOT, path[3:])
        elif path.startswith("/samples/") and SAMPLE.match(path[9:]):
            full = os.path.join(SAMPLES, path[9:])
        else:
            full = None
        if not full or not os.path.isfile(full):
            self.send_error(404, "No audio yet")
            return
        with open(full, "rb") as fh:
            data = fh.read()
        self._send_audio(data)

    def do_POST(self):
        if not self._allowed():
            return
        # Forms can't set this header and CORS is never granted, so another page can't post here.
        if (self.headers.get("X-Replies") != "1"
                or self.headers.get("Sec-Fetch-Site") not in (None, "same-origin")):
            self.send_error(403, "not allowed")
            return
        path = self.path.split("?", 1)[0]
        if not self._key_gate(path):
            return
        limit = MAX_AUDIO_BODY if path == "/api/stt" else MAX_JSON_BODY
        try:
            size = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self.send_error(411, "length required")
            return
        if size <= 0 or size > limit:
            self.send_error(413, "wrong size")
            return
        body = self.rfile.read(size)
        if path == "/api/stt":
            code, out = self._stt(body)
        elif path in ("/api/respond", "/api/respond/cancel", "/api/respond/retry", "/api/heard", "/api/marks",
                      "/api/newchat", "/api/style") \
                or path in TODO_POSTS:
            try:
                req = json.loads(body)
            except ValueError:
                req = None
            if not isinstance(req, dict):
                code, out = 400, {"error": "Bad request."}
            elif path == "/api/heard":
                levels = req.get("heard")
                if isinstance(levels, dict):
                    merge_heard(levels)
                    code, out = 200, {"ok": True}
                else:
                    code, out = 400, {"error": "Bad request."}
            elif path == "/api/marks":
                code, out = set_mark(req.get("id"), req.get("state"))
            elif path == "/api/newchat":
                code, out = start_new_chat(req.get("text"))
            elif path == "/api/style":
                code, out = set_voice_style(req.get("style"))
            elif path in TODO_POSTS:
                code, out = todo_post(path, req)
            elif path == "/api/respond":
                code, out = queue_response(req.get("entry"), req.get("text"), req.get("chat"))
            elif path == "/api/respond/retry":
                code, out = retry_response(req.get("id"))
            else:
                code, out = cancel_response(req.get("id"))
        else:
            self.send_error(404, "not found")
            return
        self._send(code, "application/json", json.dumps(out).encode())

    def _stt(self, audio):
        try:
            return 200, {"text": transcribe(audio)}
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"stt error: {type(exc).__name__}", file=sys.stderr)  # no audio or text in logs
            return 500, {"error": "Couldn't transcribe that. Try again or type it."}

    def _send_audio(self, data):
        # Chrome only seeks (scrubs) in media whose server answers byte ranges with 206.
        size = len(data)
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if not m or not (m.group(1) or m.group(2)):
            self._send(200, "audio/mpeg", data, {"Accept-Ranges": "bytes"})
            return
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        else:
            start, end = max(0, size - int(m.group(2))), size - 1
        if start > end:
            self._send(416, "audio/mpeg", b"", {"Content-Range": f"bytes */{size}"})
            return
        self._send(206, "audio/mpeg", data[start:end + 1],
                   {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}"})

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" id="tcl" media="(prefers-color-scheme: light)" content="#e9f1f0">
<meta name="theme-color" id="tcd" media="(prefers-color-scheme: dark)" content="#0a171b">
<script>
// With a key (plugin installs), the link carries it as #k=, which never reaches a server. It's kept
// in this origin's localStorage, which unlike a cookie is scoped to the port, and sent as a header
// on every request; all of the page's requests, audio included, go through fetch.
const RKEY=(()=>{const m=location.hash.match(/(?:^#|&)k=([A-Za-z0-9_-]{20,200})/);let k=m?m[1]:'';
  try{if(m)localStorage.setItem('replies-key',k);else k=localStorage.getItem('replies-key')||''}catch(_){}
  if(m)history.replaceState(history.state,'',location.pathname+location.search);return k})();
{const f0=window.fetch.bind(window);let told=false;
  window.fetch=(u,o={})=>{const h=new Headers(o.headers||{});if(RKEY)h.set('X-Replies-Key',RKEY);
    return f0(u,{...o,headers:h}).then(r=>{if(r.status===401&&!told){told=true;const d=document.createElement('div');
      d.className='keymsg';d.setAttribute('role','alert');
      const p=document.createElement('p');p.textContent='Open the Replies link from your Claude Code session (run /replies there), or paste it here. It carries this Mac\'s key.';
      const f=document.createElement('form'),i=document.createElement('input'),b=document.createElement('button');
      i.type='url';i.placeholder='Paste the Replies link';i.setAttribute('aria-label','Replies link');i.autocomplete='off';
      b.type='submit';b.textContent='Use link';f.append(i,b);d.append(p,f);
      f.addEventListener('submit',ev=>{ev.preventDefault();const m=i.value.match(/[#&]k=([A-Za-z0-9_-]{20,200})/);
        if(!m){p.textContent='That link has no key in it. Copy the whole link, including the part after #k=.';return}
        try{localStorage.setItem('replies-key',m[1])}catch(_){}location.reload()});
      document.body.prepend(d)}return r})}}
const THEMES={clay:['#e9f1f0','#121110'],violet:['#f7f7f8','#0f0e13']};
// Light/dark is Auto (follow the system), Light or Dark, remembered per theme; the tab icon follows both.
let schemes={};try{schemes=JSON.parse(localStorage.getItem('replies-scheme')||'{}')||{}}catch(e){}
const darkQ=matchMedia('(prefers-color-scheme: dark)');
function applyScheme(){const t=document.documentElement.dataset.theme||'clay',sc=schemes[t]||'auto',el=document.documentElement;
  if(sc==='auto')delete el.dataset.scheme;else el.dataset.scheme=sc;
  const dark=sc==='dark'||(sc==='auto'&&darkQ.matches),v=t==='violet'?'-violet':'';
  document.getElementById('tcl').content=THEMES[t][sc==='dark'?1:0];document.getElementById('tcd').content=THEMES[t][sc==='light'?0:1];
  const fi=document.getElementById('ficon'),ai=document.getElementById('aicon');
  if(fi)fi.href='/favicon'+v+(dark?'-dark':'')+'.png';if(ai)ai.href='/apple-touch-icon'+v+(dark?'-dark':'')+'.png';
  const b=document.getElementById('scheme');if(b){b.dataset.s=sc;b.setAttribute('aria-label','Appearance: '+sc);b.title='Appearance: '+sc+' (tap to change)'}}
function applyTheme(t){if(!THEMES[t])t='clay';document.documentElement.dataset.theme=t;
  document.querySelectorAll('.th[data-t]').forEach(b=>b.setAttribute('aria-checked',String(b.dataset.t===t)));applyScheme();return t}
let theme;try{theme=localStorage.getItem('replies-theme')}catch(e){}
theme=applyTheme(theme);
darkQ.addEventListener&&darkQ.addEventListener('change',applyScheme);
</script>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Replies">
<title>Replies</title>
<link rel="icon" type="image/png" sizes="64x64" href="/favicon.png" id="ficon">
<link rel="apple-touch-icon" href="/apple-touch-icon.png" id="aicon">
<style>
/* Themes: clay (default) = rustic clay-textured cyan + rusted glowing gold; violet = the original palette. */
:root{color-scheme:light dark;
  --clay:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.8' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 .35 0 0 0 0 .22 0 0 0 0 .14 0 0 0 .32 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  --gold-sheen:linear-gradient(90deg,#9c3f16 0%,#d8701c 18%,#ffc24f 34%,#fff0c0 42%,#ffc24f 50%,#d8701c 66%,#9c3f16 84%,#9c3f16 100%);
  --gold-chip:linear-gradient(90deg,#e88a2a 0%,#f6b546 30%,#ffe3a0 45%,#f6b546 60%,#e88a2a 100%);
  /* stone (clay dark): a fine grayscale grit over a slow mottle, so it reads as stone, not tint */
  --stone-fine:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='g' color-interpolation-filters='sRGB'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3CfeComponentTransfer%3E%3CfeFuncA type='linear' slope='0' intercept='.06'/%3E%3C/feComponentTransfer%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23g)'/%3E%3C/svg%3E");
  --stone:var(--stone-fine),url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='520' height='520'%3E%3Cfilter id='m' color-interpolation-filters='sRGB'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.011' numOctaves='4' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3CfeComponentTransfer%3E%3CfeFuncA type='linear' slope='0' intercept='.1'/%3E%3C/feComponentTransfer%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23m)'/%3E%3C/svg%3E");
  --grain:var(--clay); --card-tex:none;
  --bg:#e9f1f0; --panel:rgba(255,252,247,.86); --panel-2:rgba(240,235,227,.8); --panel-solid:#fcf9f4;
  --ink:#172327; --muted:#55666a; --line:#d9d2c5; --spine:#cfc5b4;
  --accent:#1b6f80; --accent-soft:#dcecee; --on-accent:#fff;
  --accent-fill:var(--clay),linear-gradient(140deg,#23808f,#185f6e); --play-fill:var(--accent-fill);
  --amber:#9c460c; --amber-soft:#fbe6cc; --glow:rgba(232,138,42,.55);
  --ok:#1f8a4c; --ok-soft:#e3f5ea; --bad:#c2372f; --grain-o:.45;
  --bg-paint:radial-gradient(120% 80% at 0% 0%,rgba(35,128,143,.24),transparent 60%),
    radial-gradient(100% 70% at 100% 100%,rgba(224,122,31,.2),transparent 60%),
    linear-gradient(160deg,#e6f0ef 0%,#f2eee6 55%,#f6e8da 100%);
  --shadow:0 1px 2px rgba(40,30,20,.06),0 6px 20px rgba(27,111,128,.08); --press:brightness(.9);
}
@media (prefers-color-scheme: dark){
  :root:not([data-scheme="light"]){
    /* dark clay: warm stone grays, neutral enough that text contrast holds in sun or a dim room */
    --bg:#121110; --panel:rgba(45,42,38,.92); --panel-2:rgba(60,56,51,.92); --panel-solid:#2d2a26;
    --ink:#f6f3ee; --muted:#cbc4b9; --line:#524d46; --spine:#625c54;
    --accent:#62c6d3; --accent-soft:#25363a; --on-accent:#0b1416;
    --accent-fill:var(--clay),linear-gradient(140deg,#6accd8,#45aebd);
    --amber:#ffb54d; --amber-soft:#3b2c1c; --glow:rgba(255,160,50,.5);
    --ok:#62e394; --ok-soft:#1d2b22; --bad:#ff8a80; --grain-o:.7;
    --grain:var(--stone); --card-tex:var(--stone-fine);
    --bg-paint:radial-gradient(120% 80% at 0% 0%,rgba(255,240,220,.06),transparent 60%),
      radial-gradient(100% 70% at 100% 100%,rgba(224,122,31,.09),transparent 60%),
      linear-gradient(160deg,#171615 0%,#121110 55%,#17130f 100%);
    --shadow:0 1px 2px rgba(0,0,0,.5),0 8px 24px rgba(0,0,0,.38); --press:brightness(1.3);
  }
}
:root[data-scheme="dark"]{
    /* dark clay: warm stone grays, neutral enough that text contrast holds in sun or a dim room */
    --bg:#121110; --panel:rgba(45,42,38,.92); --panel-2:rgba(60,56,51,.92); --panel-solid:#2d2a26;
    --ink:#f6f3ee; --muted:#cbc4b9; --line:#524d46; --spine:#625c54;
    --accent:#62c6d3; --accent-soft:#25363a; --on-accent:#0b1416;
    --accent-fill:var(--clay),linear-gradient(140deg,#6accd8,#45aebd);
    --amber:#ffb54d; --amber-soft:#3b2c1c; --glow:rgba(255,160,50,.5);
    --ok:#62e394; --ok-soft:#1d2b22; --bad:#ff8a80; --grain-o:.7;
    --grain:var(--stone); --card-tex:var(--stone-fine);
    --bg-paint:radial-gradient(120% 80% at 0% 0%,rgba(255,240,220,.06),transparent 60%),
      radial-gradient(100% 70% at 100% 100%,rgba(224,122,31,.09),transparent 60%),
      linear-gradient(160deg,#171615 0%,#121110 55%,#17130f 100%);
    --shadow:0 1px 2px rgba(0,0,0,.5),0 8px 24px rgba(0,0,0,.38); --press:brightness(1.3);
  }
:root[data-theme="violet"]{
  --bg:#f7f7f8; --panel:#ffffff; --panel-2:#f2f1f5; --panel-solid:#ffffff; --ink:#1b1a21; --muted:#6b6878;
  --line:#e3e1ea; --spine:#d9d6e3; --accent:#7c4dff; --accent-soft:#efe9ff; --on-accent:#fff; --accent-fill:var(--accent);
  --play-fill:linear-gradient(140deg,#7c4dff,#d946ef);
  --amber:#c27c0e; --amber-soft:#fff3dc; --ok:#1f8a4c; --ok-soft:#e3f5ea; --bad:#c2372f; --bg-paint:none;
  --shadow:0 1px 2px rgba(20,16,40,.05),0 4px 16px rgba(20,16,40,.05);
}
@media (prefers-color-scheme: dark){
  :root[data-theme="violet"]:not([data-scheme="light"]){
    --bg:#0f0e13; --panel:#17161d; --panel-2:#1e1c26; --panel-solid:#17161d; --ink:#ecebf2; --muted:#9a97a8;
    --line:#2a2834; --spine:#2f2c3b; --accent:#a78bfa; --accent-soft:#251d3d; --on-accent:#17161d;
    --play-fill:linear-gradient(140deg,#a78bfa,#f0abfc);
    --amber:#f0b44c; --amber-soft:#2e2412; --ok:#4ade80; --ok-soft:#132a1d; --bad:#f87171;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 6px 20px rgba(0,0,0,.25);
  }
}
:root[data-theme="violet"][data-scheme="dark"]{
    --bg:#0f0e13; --panel:#17161d; --panel-2:#1e1c26; --panel-solid:#17161d; --ink:#ecebf2; --muted:#9a97a8;
    --line:#2a2834; --spine:#2f2c3b; --accent:#a78bfa; --accent-soft:#251d3d; --on-accent:#17161d;
    --play-fill:linear-gradient(140deg,#a78bfa,#f0abfc);
    --amber:#f0b44c; --amber-soft:#2e2412; --ok:#4ade80; --ok-soft:#132a1d; --bad:#f87171;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 6px 20px rgba(0,0,0,.25);
  }
*{box-sizing:border-box}
html{background:var(--bg);-webkit-text-size-adjust:100%;text-size-adjust:100%;touch-action:manipulation;-webkit-tap-highlight-color:transparent}
body{background:transparent}
html,body{margin:0;color:var(--ink);
  font:15px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
  -webkit-font-smoothing:antialiased}
header{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 85%,transparent);
  backdrop-filter:saturate(1.4) blur(12px);border-bottom:1px solid var(--line)}
.bar{max-width:1080px;margin:0 auto;padding:max(12px,env(safe-area-inset-top)) max(16px,env(safe-area-inset-right)) 8px max(16px,env(safe-area-inset-left));display:flex;align-items:center;gap:14px}
/* One header widget: the logo is the play/pause button, the name sits over what is playing,
   and a wash of the accent spreads out from the logo, stronger while something plays. */
.player{position:relative;display:flex;align-items:center;gap:12px;min-width:0;flex:1 1 auto;
  background:linear-gradient(100deg,color-mix(in srgb,var(--accent) 9%,var(--panel-solid)),var(--panel) 42%);
  border:1px solid var(--line);border-radius:18px;padding:6px 14px 6px 6px;box-shadow:var(--shadow);transition:border-color .3s,background .3s}
body.playing .player{border-color:color-mix(in srgb,var(--accent) 35%,var(--line));
  background:linear-gradient(100deg,color-mix(in srgb,var(--accent) 16%,var(--panel-solid)),var(--panel) 55%)}
.bar #pp{width:42px;height:42px}
.bar #pp svg{width:16px;height:16px}
.pbody{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.ptop{display:flex;align-items:baseline;gap:8px;min-width:0}
.pname{flex:none;font-weight:650;font-size:14px;letter-spacing:-.01em;color:var(--ink)}
.pname::after{content:"·";margin-left:8px;color:var(--muted);font-weight:400}
.player .t{flex:1;font-size:13px;line-height:1.3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.player .t.idle{color:var(--muted)}
/* Auto: a quiet dot and word at the end of the title line; filled while on, pulsing while the browser waits for a tap.
   The hit area reaches past the text (to ~44px wide) but not down over the speed and caption buttons. */
.auto{flex:none;align-self:center;position:relative;display:inline-flex;align-items:center;gap:5px;border:0;background:none;padding:0 2px;
  font:inherit;font-size:11.5px;font-weight:600;color:var(--muted);cursor:pointer}
.auto::before{content:"";width:7px;height:7px;border-radius:50%;border:1.5px solid currentColor;box-sizing:border-box}
.auto::after{content:"";position:absolute;inset:-10px -8px -3px}
.auto[aria-pressed="true"]{color:var(--accent)}.auto[aria-pressed="true"]::before{background:currentColor}
.auto.blocked::before{animation:autowait 1.6s ease-in-out infinite}
@keyframes autowait{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.auto.blocked::before{animation:none;background:none}}
@media (hover:hover){.auto:hover{color:var(--accent)}}
.auto:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}
.seekrow{display:flex;align-items:center;gap:8px}
.time{flex:none;font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.spd{flex:none;min-width:44px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);border-radius:999px;
  padding:1px 7px;font:inherit;font-size:11.5px;font-weight:600;font-variant-numeric:tabular-nums;cursor:pointer}
@media (hover:hover){.spd:hover{border-color:var(--accent);color:var(--accent)}}
.spd.fast{background:var(--accent-soft);border-color:color-mix(in srgb,var(--accent) 45%,var(--line));color:var(--accent)}
.spd:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
#shr{display:grid;place-items:center;align-self:stretch}#shr[hidden]{display:none}#shr svg{width:15px;height:15px}
#seek{--p:0%;flex:1;min-width:0;height:16px;margin:0;background:transparent;-webkit-appearance:none;appearance:none;cursor:pointer}
#seek:disabled{cursor:default;opacity:.45}
#seek::-webkit-slider-runnable-track{height:4px;border-radius:4px;
  background:linear-gradient(var(--accent),var(--accent)) 0/var(--p) 100% no-repeat,var(--line)}
#seek::-webkit-slider-thumb{-webkit-appearance:none;width:12px;height:12px;margin-top:-4px;border-radius:50%;background:var(--accent);border:0}
#seek::-moz-range-track{height:4px;border-radius:4px;background:var(--line)}
#seek::-moz-range-progress{height:4px;border-radius:4px;background:var(--accent)}
#seek::-moz-range-thumb{width:12px;height:12px;border-radius:50%;background:var(--accent);border:0}
#seek:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}
.agrow{max-width:1080px;margin:0 auto;padding:0 max(16px,env(safe-area-inset-right)) 8px max(16px,env(safe-area-inset-left))}
.agents{display:inline-flex;gap:2px;padding:2px;border:1px solid var(--line);border-radius:999px;background:var(--panel-2)}
.ag{display:inline-flex;align-items:center;gap:6px;min-height:28px;padding:3px 12px;border:0;border-radius:999px;
  background:transparent;color:var(--muted);font:inherit;font-size:13px;font-weight:600;cursor:pointer}
@media (hover:hover){.ag:hover{color:var(--ink)}}
.ag:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.ag.on{background:var(--panel);color:var(--ink);box-shadow:var(--shadow)}
.ag .c{color:var(--muted);font-size:11.5px;font-weight:400;font-variant-numeric:tabular-nums}
.ag .c:empty{display:none}
.chats{max-width:1080px;margin:0 auto;padding:0 max(16px,env(safe-area-inset-right)) 10px max(16px,env(safe-area-inset-left));display:flex;align-items:flex-start;gap:8px}
.crow{flex:1;min-width:0;display:flex;flex-wrap:wrap;gap:8px}
.pastwrap{position:relative;flex:none}
.cf.past{color:var(--muted)}
.cf.past[aria-expanded="true"]{border-color:var(--accent);color:var(--ink)}
.cf.past .car{font-size:10px;transition:transform .15s}
.cf.past[aria-expanded="true"] .car{transform:rotate(180deg)}
.panel{position:absolute;right:0;top:calc(100% + 6px);z-index:20;width:min(380px,calc(100vw - 32px));
  background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:8px}
.panel input{width:100%;font:inherit;font-size:13px;color:var(--ink);background:var(--panel-2);
  border:1px solid var(--line);border-radius:9px;padding:6px 10px;outline:none}
.panel input:focus{border-color:var(--accent)}
.plist{max-height:min(60vh,420px);overflow-y:auto;margin-top:6px;display:flex;flex-direction:column;gap:2px}
.pi{display:flex;align-items:center;gap:8px;width:100%;text-align:left;font:inherit;font-size:13px;color:var(--ink);
  background:transparent;border:0;border-radius:8px;padding:6px 8px;cursor:pointer}
.pi:focus-visible{background:var(--panel-2);outline:none}
@media (hover:hover){.pi:hover{background:var(--panel-2)}}
.pi.on{background:var(--accent-soft)}
.pi .sw{width:8px;height:8px;border-radius:50%;flex:none}
.pi .n{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pi .c,.pi .ago{flex:none;color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
.pnone{color:var(--muted);font-size:12.5px;padding:10px 8px}
.qfold{margin-top:6px}
.qfold>summary{list-style:none;cursor:pointer;display:flex;flex-wrap:wrap;gap:4px 14px;padding:6px 0 2px;font-size:13px;color:var(--muted)}
.qfold>summary::-webkit-details-marker{display:none}
.qfold>summary::before{content:'▸';flex:none;width:1em}
.qfold[open]>summary::before{content:'▾'}
.qfold .qa{min-width:0;overflow-wrap:anywhere}
.qfold .qa b{color:var(--ink);font-weight:600;margin-right:4px}
.qfold .qa i{font-style:normal;color:var(--amber)}
/* Focus captions: the playing clip's words, a few at a time, in a calm serif over a soft wash.
   Only the size pill takes taps; the header above stays usable. --cchead is the header's height
   (set by the script). The caption's first line sits at --ccy: about mid-screen, low enough to leave
   room for a visual under the header, and high enough that five lines clear the size pill. */
.cc{position:fixed;inset:0;z-index:9;pointer-events:none;--cchead:0px;
  --ccfs:min(calc(clamp(30px,6.4vw,58px) * var(--ccz,1)),11vw);
  --ccy:min(calc(100% - 92px - 6.1 * var(--ccfs)),max(52%,calc(var(--cchead) + 180px)));
  background:radial-gradient(ellipse 70% 45% at 50% 52%,color-mix(in srgb,var(--bg) 92%,transparent) 0%,color-mix(in srgb,var(--bg) 70%,transparent) 55%,transparent 100%)}
.cc[hidden]{display:none}
/* The caption's first line sits at a fixed height (--ccy) and longer phrases grow downward, so a
   phrase change, a visual appearing or a size step never moves the line being read. */
.cct{position:absolute;top:var(--ccy);left:0;right:0;margin:0 auto;width:min(16ch,90vw);isolation:isolate;
  text-align:center;color:var(--ink);font-family:ui-serif,"New York","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  font-size:var(--ccfs);font-weight:400;line-height:1.22;letter-spacing:.005em;text-wrap:balance;overflow-wrap:anywhere;transition:opacity .4s ease}
.cct.in{animation:ccin .35s ease both}
.cct .ccw{color:color-mix(in srgb,var(--ink) 50%,transparent);transition:color .12s ease-out}
.cct .ccw.past{color:color-mix(in srgb,var(--ink) 82%,transparent)}
.cct .ccw.on{color:var(--ink)}
.ccpill{position:absolute;left:0;top:0;z-index:-1;border-radius:.22em;background:var(--accent-soft);opacity:0;
  transition:transform .18s cubic-bezier(.3,.7,.3,1),width .18s cubic-bezier(.3,.7,.3,1),height .18s ease,opacity .2s ease}
.ccpill.jump{transition:opacity .2s ease}
/* Caption size: a small pill at the foot of the captions, the one part of them that takes taps. */
.ccsize{position:absolute;left:0;right:0;bottom:max(24px,calc(env(safe-area-inset-bottom) + 14px));margin:0 auto;width:max-content;
  display:flex;gap:2px;padding:4px;border-radius:999px;background:var(--panel-solid);border:1px solid var(--line);box-shadow:var(--shadow);pointer-events:auto}
.ccsize button{min-width:48px;min-height:38px;border:0;border-radius:999px;background:transparent;color:var(--ink);font:inherit;font-size:15px;font-weight:650;cursor:pointer}
@media (hover:hover){.ccsize button:hover:not(:disabled){background:var(--panel-2)}}
.ccsize button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.ccsize button:disabled{opacity:.35;cursor:default}
.cc.paused .cct{opacity:.5}.cc.paused .ccvs{opacity:.85}
.cc.withv{background:radial-gradient(ellipse 90% 62% at 50% 50%,color-mix(in srgb,var(--bg) 97%,transparent) 0%,color-mix(in srgb,var(--bg) 92%,transparent) 58%,transparent 100%)}
/* Visuals stand above the caption, bottom-aligned to it, and grow upward. */
.ccvs{position:absolute;left:0;right:0;margin:0 auto;bottom:calc(100% - var(--ccy) + 22px);width:min(620px,92vw);
  max-height:calc(var(--ccy) - var(--cchead) - 34px);overflow:hidden;display:flex;align-items:flex-end;justify-content:center}
.ccvs>.ccv{transform-origin:50% 100%}
.ccv{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;width:100%}
.ccv>*{animation:ccpop .5s cubic-bezier(.2,.8,.2,1) both;animation-delay:calc(var(--i,0) * .14s)}
@keyframes ccpop{from{opacity:0;transform:scale(.96)}}
.ccv .chip{padding:8px 16px;border-radius:999px;background:var(--accent-soft);color:var(--accent);font-weight:650;font-size:clamp(15px,3.4vw,20px)}
.ccv.k-code{flex-direction:column;align-items:stretch;gap:0;padding:14px 16px;border-radius:14px;background:var(--panel-solid);border:1px solid var(--line);box-shadow:var(--shadow);
  font:500 clamp(13px,3vw,16px)/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;text-align:left}
.ccv .ln{white-space:pre-wrap;overflow-wrap:anywhere;padding:0 6px;border-radius:6px;color:var(--muted)}
.ccv .ln.hl{background:var(--accent-soft);color:var(--ink)}
.ccv .step{position:relative;padding:10px 14px;border-radius:12px;background:var(--panel-solid);border:1px solid var(--line);box-shadow:var(--shadow);font-weight:600;font-size:clamp(14px,3.2vw,18px)}
.ccv .step+.step{margin-left:26px}
.ccv .step+.step::before{content:"→";position:absolute;left:-26px;width:26px;top:50%;transform:translateY(-50%);text-align:center;color:var(--accent);font-weight:700}
.ccv.k-compare{flex-wrap:nowrap;align-items:stretch}
.ccv .col{flex:1;min-width:0;padding:12px 14px;border-radius:14px;background:var(--panel-solid);border:1px solid var(--line);box-shadow:var(--shadow);text-align:left}
.ccv .ct{font-weight:700;font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-bottom:6px}
.ccv .ci{font-size:clamp(14px,3.2vw,17px);line-height:1.35;margin-top:4px}
.ccv.k-list{flex-direction:column;align-items:flex-start;width:auto}
.ccv .li{font-size:clamp(16px,3.6vw,21px);padding-left:22px;position:relative}
.ccv .li::before{content:"";position:absolute;left:4px;top:.55em;width:8px;height:8px;border-radius:50%;background:var(--accent)}
.ccv.k-callout{flex-direction:column;align-items:center;gap:4px}
.ccv .val{font:600 clamp(34px,8vw,56px)/1.1 ui-serif,"New York",Georgia,serif;color:var(--accent);text-wrap:balance}
.ccv .txt{font-size:clamp(15px,3.4vw,20px);color:var(--muted)}
@media (prefers-reduced-motion:reduce){.ccv>*{animation:none}}
@keyframes ccin{from{opacity:.3}}
@media (prefers-reduced-motion:reduce){.cct.in{animation:none}.cct,.ccpill{transition:none}}
.mpanel .mlab{margin-top:12px;font-size:12px;font-weight:650;color:var(--muted)}
.mtracks.vstyle{grid-template-columns:repeat(3,1fr);margin-top:6px}
.pgh{font-size:11px;font-weight:650;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);padding:8px 8px 2px}
@media (max-width:640px) and (hover:none){.crow{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;
  -webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent);mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent)}
  .crow::-webkit-scrollbar{display:none}}
.cf{flex:none;display:inline-flex;align-items:center;gap:7px;max-width:260px;padding:5px 12px;border-radius:999px;
  border:1px solid var(--line);background:var(--panel);color:var(--ink);font:inherit;font-size:13px;cursor:pointer}
.cf span.n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cf .c{color:var(--muted);font-size:11.5px}
@media (hover:hover){.cf:hover{border-color:var(--accent)}}
.cf.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.cf.on .c{color:color-mix(in srgb,var(--on-accent) 80%,transparent)}
.cf.closed{opacity:.6}
.cf .sw{width:8px;height:8px;border-radius:50%;flex:none}
.cf.busy .sw{animation:pulse 1.4s ease-in-out infinite}
main{max-width:1080px;margin:0 auto;padding:14px max(16px,env(safe-area-inset-right)) calc(80px + env(safe-area-inset-bottom)) max(16px,env(safe-area-inset-left))}
.day{margin:20px 0 6px 34px;font-size:12px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em}
.node{position:relative;padding-left:34px;margin:8px 0}
.node::before{content:"";position:absolute;left:11px;top:-8px;bottom:-8px;width:2px;background:var(--spine)}
.dot{position:absolute;left:4px;top:15px;width:16px;height:16px;border-radius:50%;
  background:var(--panel);border:2px solid var(--accent);z-index:1}
.node.q .dot{border-color:var(--amber)}
.node.busy .dot{animation:pulse 1.4s ease-in-out infinite}
/* the dot is a checkbox: empty -> check (auto once heard) -> star -> empty; position is restated here so
   the iOS haptic rule (.ios button{position:relative}) can't pull it off the timeline */
.node .dot{position:absolute;left:2px;top:13px;width:20px;height:20px;padding:0;margin:0;font:inherit;display:grid;place-items:center;
  cursor:pointer;color:var(--on-accent);transition:background .2s,border-color .2s,transform .15s;-webkit-tap-highlight-color:transparent}
.node .dot::after{content:"";position:absolute;inset:-12px;border-radius:50%}
.node .dot svg{width:11px;height:11px}
.node .dot:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (hover:hover){.node .dot.open:hover{background:var(--accent-soft);transform:scale(1.12)}
  .node .dot.check:hover,.node .dot.star:hover{transform:scale(1.12)}}
.node .dot.check{background:var(--play-fill);border-color:transparent;color:#fff}
.node .dot.star{background:var(--gold-chip);border-color:transparent;color:#5a2a06;box-shadow:0 0 8px var(--glow)}
.node .dot.pop{animation:pop .5s cubic-bezier(.3,1.7,.5,1),burst .8s ease-out}
@media (prefers-reduced-motion:reduce){.node .dot.pop{animation:none}}
@keyframes pulse{50%{box-shadow:0 0 0 6px color-mix(in srgb,var(--accent) 18%,transparent)}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:10px 12px 10px 14px;
  box-shadow:var(--shadow);display:flex;align-items:center;gap:12px}
.card.qwrap{display:block}
.card[data-open]{cursor:pointer;-webkit-tap-highlight-color:transparent}
@media (hover:hover){.card[data-open]:hover{border-color:color-mix(in srgb,var(--accent) 30%,var(--line))}}
.grow{flex:1;min-width:0}
.title{font-weight:600;letter-spacing:-.01em;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{font-size:12px;color:var(--muted);display:flex;gap:8px;align-items:center;white-space:nowrap;overflow:hidden}
.meta .who{overflow:hidden;text-overflow:ellipsis}
.meta .sw{width:7px;height:7px;border-radius:50%;flex:none}
.status{color:var(--accent)} .status.failed{color:var(--bad)}
.play{flex:none;width:34px;height:34px;border-radius:50%;border:1px solid var(--line);background:var(--panel-2);
  color:var(--ink);display:grid;place-items:center;cursor:pointer;transition:all .15s}
@media (hover:hover){.play:hover{border-color:var(--accent);color:var(--accent)}}
.play.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.play:disabled{opacity:.35;cursor:default}
.play svg{width:14px;height:14px}
.play[data-key]{position:relative;overflow:hidden;isolation:isolate;border-radius:50%}
.play[data-key] svg{position:relative;z-index:1}
.lvl{--water:color-mix(in srgb,var(--accent) 42%,transparent);position:absolute;left:0;top:0;bottom:0;
  width:var(--p);background:var(--water);transition:width 1.2s cubic-bezier(.3,.7,.4,1);pointer-events:none}
.lvl::after{content:"";position:absolute;top:-24px;bottom:-24px;will-change:transform;right:-6px;width:6px;background:var(--water);
  -webkit-mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='6' height='24'%3E%3Cpath d='M0 0H4.5C4.5 4.8 1.5 7.2 1.5 12C1.5 16.8 4.5 19.2 4.5 24H0Z'/%3E%3C/svg%3E") 0 0/6px 24px repeat-y;
  mask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='6' height='24'%3E%3Cpath d='M0 0H4.5C4.5 4.8 1.5 7.2 1.5 12C1.5 16.8 4.5 19.2 4.5 24H0Z'/%3E%3C/svg%3E") 0 0/6px 24px repeat-y;
  animation:wave 3.2s linear infinite}
.lvl::before{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(255,255,255,.22),transparent 55%)}
.play.on .lvl{--water:color-mix(in srgb,var(--on-accent) 30%,transparent)}
.play.on .lvl::after{animation-duration:2s}
@keyframes wave{from{transform:translate3d(0,0,0)}to{transform:translate3d(0,24px,0)}}
/* Unheard clips are the loud ones: a bright ring with a slow ripple that asks to be played.
   Done clips step back to a faint check, so what is left to hear is what stands out. */
.play .ck{display:none}
/* unplayed is a bright ring round an open centre, so it reads as still to do; listening fills it in,
   only the clip that is playing is solid, and done ones fade */
.play[data-key]:not(.done):not(.on):not(:disabled):not(.warming){border:2px solid transparent;color:var(--accent);
  background:linear-gradient(var(--panel-solid),var(--panel-solid)) padding-box,var(--play-fill) border-box;
  animation:beckon 2.8s ease-out infinite}
@media (hover:hover){.play[data-key]:not(.done):not(.on):not(:disabled):not(.warming):hover{color:var(--accent);transform:scale(1.08);filter:saturate(1.2)}}
.play[data-key]:not(.done):not(.on) .lvl{--water:color-mix(in srgb,var(--accent) 35%,transparent)}
.play[data-key]:not(.done):not(.on):not(:disabled):not(.warming) svg{width:calc(55.6% + 2.22px);height:calc(55.6% + 2.22px)}  /* golden ratio inside the 2px ring */
/* over the level fill (clay's gold especially) the accent icon can match the fill's brightness; an outline
   in the centre's own colour keeps it readable there and cannot be seen on the open centre */
.play[data-key]:not(.done):not(.on):not(:disabled):not(.warming) .pl path{stroke:var(--panel-solid);stroke-width:1.5px;stroke-linejoin:round;paint-order:stroke}
.play.on{transform:scale(1.06);animation:playing 1.6s ease-in-out infinite}
.play.done:not(.on){background:transparent;border-color:color-mix(in srgb,var(--line) 80%,transparent);color:var(--muted);
  opacity:.5;box-shadow:none;transition:opacity .9s ease,color .9s ease,border-color .9s ease}
.play.done:not(.on) .lvl,.play.done:not(.on) .pl{display:none}
.play.done:not(.on) .ck{display:block}
@media (hover:hover){.play.done:not(.on):hover{opacity:1;color:var(--accent);border-color:var(--accent);transition:all .15s}}
@media (hover:hover){.play.done:not(.on):hover .ck{display:none}}
@media (hover:hover){.play.done:not(.on):hover .pl{display:block}}
/* just finished: bounce, burst ring, check draws in, then it settles into the faint done look */
.play.done.pop:not(.on){opacity:1;color:var(--accent);border-color:var(--accent);
  animation:pop .6s cubic-bezier(.3,1.7,.5,1),burst .9s ease-out}
.play.pop .ck path{stroke-dasharray:1;stroke-dashoffset:1;animation:draw .4s .18s ease-out forwards}
@keyframes beckon{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 45%,transparent)}
  70%,100%{box-shadow:0 0 0 8px color-mix(in srgb,var(--accent) 0%,transparent)}}
@keyframes playing{0%,100%{box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 30%,transparent)}
  50%{box-shadow:0 0 0 6px color-mix(in srgb,var(--accent) 12%,transparent)}}
@keyframes pop{0%{transform:scale(.7)}45%{transform:scale(1.28)}100%{transform:scale(1)}}
@keyframes burst{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 65%,transparent),0 0 0 0 color-mix(in srgb,var(--amber) 55%,transparent)}
  100%{box-shadow:0 0 0 14px color-mix(in srgb,var(--accent) 0%,transparent),0 0 0 24px color-mix(in srgb,var(--amber) 0%,transparent)}}
@keyframes draw{to{stroke-dashoffset:0}}
@media (prefers-reduced-motion:reduce){.lvl::after{animation:none}.lvl{transition:none}
  .play[data-key],.play.on,.play.done.pop:not(.on),.play.pop .ck path{animation:none;stroke-dashoffset:0}}
.btn{flex:none;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);border-radius:999px;
  padding:4px 12px;font:inherit;font-size:12.5px;cursor:pointer}
@media (hover:hover){.btn:hover{border-color:var(--accent);color:var(--accent)}}
.qtop{display:flex;align-items:center;gap:12px}
.fan{position:relative;padding-top:14px;margin-top:6px}
.fan::before{content:"";position:absolute;left:22px;top:0;width:2px;height:14px;
  background:color-mix(in srgb,var(--amber) 55%,transparent)}
.lane{padding:0 0 4px}
.rail{display:grid;grid-template-columns:repeat(var(--n),minmax(0,1fr));gap:8px}
.qc{min-width:0;background:var(--panel-2);border:1px solid var(--line);
  border-radius:11px;padding:8px 9px;position:relative;margin-top:14px}
.qc::before{content:"";position:absolute;top:-15px;left:21px;width:2px;height:14px;
  background:color-mix(in srgb,var(--amber) 55%,transparent)}
.qc:not(:last-child)::after{content:"";position:absolute;top:-15px;left:21px;width:calc(100% + 8px);height:2px;
  background:color-mix(in srgb,var(--amber) 55%,transparent)}
.qhead{display:flex;align-items:flex-start;gap:6px}
.qhead .play{width:28px;height:28px}
.qhead .play svg{width:12px;height:12px}
.qtag{display:inline-block;font-size:10px;font-weight:650;color:var(--amber);background:var(--amber-soft);
  padding:1px 6px;border-radius:999px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px}
.qsum{font-weight:600;font-size:12.5px;line-height:1.3}
.opts{display:flex;flex-direction:column;gap:3px;margin-top:6px}
.opt{font-size:11.5px;line-height:1.3;padding:3px 6px;border-radius:7px;border:1px solid var(--line);background:var(--panel);
  display:flex;align-items:center;gap:5px}
.opt .l{flex:1;min-width:0}
@media (max-width:760px){.rail{grid-template-columns:repeat(2,minmax(0,1fr))}.qc::after{display:none}}
.opt.rec-o{border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
.opt.chosen{border-color:var(--ok);background:var(--ok-soft)}
button.opt.tap{font:inherit;font-size:11.5px;color:inherit;text-align:left;width:100%;cursor:pointer;min-height:32px}
button.opt.tap:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.opt.tap.pick{border-color:var(--ok);background:var(--ok-soft)}
.badge{flex:none;font-size:10px;font-weight:650;border-radius:999px;padding:0 6px}
.badge.rec{color:var(--accent);background:var(--accent-soft)}
.badge.ok{color:var(--ok)}
.other{margin-top:6px;font-size:12px;border-left:3px solid var(--ok);padding:3px 7px;background:var(--ok-soft);border-radius:6px}
.inchat{margin:8px 0 0;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.skel{height:10px;border-radius:6px;background:linear-gradient(90deg,var(--panel-2),var(--line),var(--panel-2));
  background-size:200% 100%;animation:sh 1.3s linear infinite;margin:6px 0}
@keyframes sh{to{background-position:-200% 0}}
.empty{color:var(--muted);text-align:center;padding:60px 0}
#todo{margin:0 0 14px}
#todo:empty{display:none}
body.chatv #todo{display:none}
.td{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.td>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:12px;padding:11px 14px;font-weight:650}
.td>summary::-webkit-details-marker,.tds>summary::-webkit-details-marker{display:none}
.td .tbar{flex:1;max-width:220px;height:6px;border-radius:99px;background:var(--panel-2);overflow:hidden}
.td .tbar i{display:block;height:100%;background:var(--ok);transition:width .3s ease}
.td .cnt{margin-left:auto;color:var(--muted);font-size:13px;font-weight:500;font-variant-numeric:tabular-nums}
.tdb{padding:0 14px 10px}
.tdn{color:var(--muted);font-size:12px;margin:0 0 4px}
.tds{margin-top:6px}
.tds>summary{list-style:none;cursor:pointer;font-size:12px;font-weight:650;text-transform:uppercase;
  letter-spacing:.05em;color:var(--muted);padding:6px 0 4px}
.tds>summary::before{content:'▸ ';display:inline-block;width:1em}
.tds[open]>summary::before{content:'▾ '}
.tdd{border-top:1px solid var(--line)}
.tdd>summary{list-style:none;cursor:pointer;padding:6px 0;font-size:13px;color:var(--muted)}
.tdd>summary::-webkit-details-marker{display:none}
.tdd>summary::before{content:'▸ ';display:inline-block;width:1em}
.tdd[open]>summary::before{content:'▾ '}
.tdd>.tdi:first-of-type{border-top:0}
.tdi{display:flex;gap:10px;align-items:flex-start;padding:6px 0;border-top:1px solid var(--line)}
.tdi input[type=checkbox]{margin:3px 0 0;width:17px;height:17px;accent-color:var(--accent);flex:none;cursor:pointer}
.tdck{display:contents}
.tdi .tx{flex:1;min-width:0;overflow-wrap:anywhere}
.tdi.done .l{color:var(--muted);text-decoration:line-through}
.tdprog{display:inline-block;margin-left:8px;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:650;
  letter-spacing:.02em;vertical-align:1px;background:var(--accent-soft);color:var(--accent);white-space:nowrap}
.tdic[data-a="prog"][aria-pressed="true"]{color:var(--accent);opacity:1}
.tdi ul{margin:2px 0 0;padding-left:16px;color:var(--muted);font-size:13px}
.td code{font-size:12.5px;background:var(--panel-2);padding:1px 4px;border-radius:4px}
.tdw{color:var(--amber)}
.td>summary .tdt{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tdauto{flex:none;font-size:10.5px;font-weight:650;letter-spacing:.04em;text-transform:uppercase;color:var(--accent);
  background:var(--accent-soft);border-radius:999px;padding:1px 7px}
.tdauto[hidden],.tdsel[hidden]{display:none}
.td.busy .tdb{cursor:progress}
.tdls{display:flex;align-items:center;gap:6px;margin:0 0 6px}
.tdcs{position:relative;flex:1;min-width:0;display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding:2px 1px}
.tdcs::-webkit-scrollbar{display:none}
.tdc{flex:none;display:inline-flex;align-items:center;gap:6px;max-width:220px;height:30px;padding:0 11px;border-radius:999px;
  border:1px solid var(--line);background:var(--panel-2);color:var(--ink);font:inherit;font-size:13px;cursor:pointer}
.tdc>span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tdc .n{color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
.tdc[aria-pressed="true"]{background:var(--accent-soft);border-color:color-mix(in srgb,var(--accent) 45%,var(--line));color:var(--accent);font-weight:600}
.tdbt{flex:none;height:30px;padding:0 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);
  font:inherit;font-size:13px;cursor:pointer}
.tdbt.ic{width:30px;padding:0;font-size:17px;line-height:1}
.tdbt.pri{background:var(--accent-fill);border-color:transparent;color:var(--on-accent);font-weight:600}
.tdbt.dng{color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,var(--line))}
.tdbt.dng.armed{background:var(--bad);border-color:var(--bad);color:#fff}
@media (hover:hover){.tdbt:hover,.tdc:hover{border-color:var(--accent)}}
@media (hover:hover){.tdbt.pri:hover{filter:brightness(1.07)}}
.tdbt:focus-visible,.tdc:focus-visible,.tdic:focus-visible,.tdai:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tdin{flex:1 1 160px;min-width:0;height:34px;padding:0 11px;border:1px solid var(--line);border-radius:10px;
  background:var(--panel-solid);color:var(--ink);font:inherit;font-size:16px}
.tdin:focus,.tdsel:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 22%,transparent)}
.tdsel{flex:0 1 190px;min-width:0;height:34px;padding:0 8px;border:1px solid var(--line);border-radius:10px;
  background:var(--panel-solid);color:var(--ink);font:inherit;font-size:16px}
.tdx:empty,.tdmsg:empty{display:none}
.tdf{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:8px;margin:0 0 6px;border:1px solid var(--line);
  border-radius:12px;background:var(--panel-2)}
.tdf .q{flex:1 1 100%;font-size:13px}
.tdh{flex:1 1 100%;color:var(--muted);font-size:12px}
.tdmsg{margin:0 0 6px;font-size:12.5px;color:var(--muted)}
.tdmsg.bad{color:var(--bad)}
.tda{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}
.tdempty{color:var(--muted);font-size:13px;margin:6px 0}
.tdi .acts{flex:none;display:flex;gap:2px;margin:-4px -6px 0 0}
.tdic{width:30px;height:30px;display:grid;place-items:center;border:0;border-radius:8px;background:none;color:var(--muted);
  cursor:pointer;opacity:0;padding:0}
.tdic svg{width:15px;height:15px}
.tdic:focus-visible{opacity:.85}
@media (hover:hover){.tdi:hover .tdic{opacity:.85}}
@media (hover:hover){.tdic:hover{color:var(--accent);background:var(--panel-2)}}
@media (hover:none){.tdic{opacity:.6}}
.tdai{display:inline-block;vertical-align:1px;margin-left:6px;padding:0 5px;border:1px solid color-mix(in srgb,var(--accent) 35%,var(--line));
  border-radius:5px;background:none;color:var(--accent);font:inherit;font-size:9.5px;font-weight:700;letter-spacing:.04em;
  line-height:14px;cursor:pointer;text-decoration:none}
@media (hover:hover){.tdai:hover{background:var(--accent-soft)}}
.tded{display:flex;flex-direction:column;gap:6px}
.tded .tdin{flex:none;width:100%}
.tdrow{display:flex;flex-wrap:wrap;gap:6px}
button{touch-action:manipulation;-webkit-tap-highlight-color:transparent}
button,summary,.tdck{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
@media (hover:none){button:not(:disabled):active,summary:active,.card[data-open]:active:not(:has(button:active)){filter:var(--press)}}
.rbtn{flex:none;display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 12px;border-radius:999px;
  border:1px solid var(--line);background:var(--panel-2);color:var(--ink);font:inherit;font-size:13px;cursor:pointer}
@media (hover:hover){.rbtn:hover{border-color:var(--accent);color:var(--accent)}}
.rbtn:focus-visible,.send:focus-visible,.mic:focus-visible,.x:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.rbtn svg{width:15px;height:15px}
.resps{display:flex;flex-direction:column;align-items:flex-end;gap:6px;margin:6px 0 0 28px}
.resp{max-width:100%;background:var(--accent-soft);border:1px solid color-mix(in srgb,var(--accent) 30%,var(--line));
  border-radius:14px 14px 4px 14px;padding:7px 11px;font-size:14px;overflow-wrap:anywhere}
.resp.failed{background:transparent;border-color:var(--bad)}
.rstate{display:flex;align-items:center;justify-content:flex-end;gap:8px;font-size:12px;color:var(--muted);margin-top:3px}
.resp.failed .rstate{color:var(--bad)}
.rcancel{border:0;background:none;color:var(--accent);font:inherit;font-size:12px;padding:0;cursor:pointer;text-decoration:underline}
.scrim{position:fixed;inset:0;z-index:30;background:rgba(10,8,20,.42);touch-action:none;animation:fadein .2s ease-out}
.sheet:not(.dock){animation:sheetin .26s cubic-bezier(.2,.9,.25,1);transition:transform .22s ease}
.sheet.drag{transition:none}
@media (hover:none){.sheet:not(.dock)::before{content:"";position:absolute;left:50%;top:5px;width:36px;height:5px;margin-left:-18px;border-radius:3px;background:var(--line)}}
@keyframes sheetin{from{transform:translateY(100%)}}
@keyframes fadein{from{opacity:0}}
.sheet{--kb:0px;position:fixed;left:0;right:0;bottom:var(--kb);z-index:31;max-width:640px;margin:0 auto;
  background:var(--panel);border:1px solid var(--line);border-bottom:0;border-radius:18px 18px 0 0;box-shadow:var(--shadow);
  padding:12px max(16px,env(safe-area-inset-right)) calc(12px + env(safe-area-inset-bottom)) max(16px,env(safe-area-inset-left));
  overscroll-behavior:contain}
.sheet.kb{padding-bottom:12px}
.shead{display:flex;align-items:flex-start;gap:10px;margin-bottom:8px}
.slabel{font-size:12px;color:var(--muted)}
.stitle{font-weight:600;font-size:15px;line-height:1.3;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden}
.x{flex:none;width:44px;height:44px;margin:-8px -10px 0 0;border:0;background:none;color:var(--muted);font-size:24px;line-height:1;cursor:pointer}
#stext{display:block;width:100%;min-height:76px;max-height:38vh;resize:none;font:inherit;font-size:16px;line-height:1.4;color:var(--ink);
  background:var(--panel-2);border:1px solid var(--line);border-radius:12px;padding:10px 12px;outline:none}
#stext:focus{border-color:var(--accent)}
.serr{margin-top:8px;font-size:13px;color:var(--bad)}
.sact{display:flex;align-items:center;gap:10px;margin-top:10px}
.sact .mic{align-self:flex-end}  /* the waveform box is a little taller than the mic; keep the mic still when it shows */
.mic{flex:none;width:52px;height:52px;border-radius:50%;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);
  display:grid;place-items:center;cursor:pointer;touch-action:none;user-select:none;-webkit-user-select:none;
  -webkit-touch-callout:none;-webkit-tap-highlight-color:transparent;transition:transform .12s,background .15s}
.mic svg{width:22px;height:22px;pointer-events:none}
/* recording: an inner ring tracks the live mic level (--lvl, 0..1); it stays inside the button so
   it never spills onto Send beside it */
.mic.rec{background:var(--bad);border-color:var(--bad);color:#fff;
  box-shadow:inset 0 0 0 calc(2px + var(--lvl,0) * 9px) rgba(255,255,255,.32)}
.mic:disabled{opacity:.45;cursor:default}
.mbox{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
#wave{display:block;width:100%;height:34px}
#wave[hidden]{display:none}
.mstat{min-width:0;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.send{flex:none;height:44px;padding:0 22px;border:0;border-radius:999px;background:var(--accent);color:var(--on-accent);font:inherit;font-weight:600;cursor:pointer}
.send:disabled{opacity:.45;cursor:default}
@media (max-width:640px){
  .bar #pp{width:44px;height:44px}
  .bar{padding-top:calc(8px + env(safe-area-inset-top));padding-left:max(12px,env(safe-area-inset-left));padding-right:max(12px,env(safe-area-inset-right));gap:10px}
  .chats,.agrow{padding-left:max(12px,env(safe-area-inset-left));padding-right:max(12px,env(safe-area-inset-right))}
  main{padding:10px max(12px,env(safe-area-inset-right)) calc(40px + env(safe-area-inset-bottom)) max(12px,env(safe-area-inset-left))}
  .title{white-space:normal;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-height:1.3}
  .card{gap:8px;padding:10px}
  .rbtn{width:44px;height:44px;padding:0;justify-content:center}
  .rbtn span{display:none}
  .rbtn svg{width:18px;height:18px}
  .crow{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch;
    -webkit-mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent);mask-image:linear-gradient(90deg,#000 calc(100% - 28px),transparent)}
  .crow::-webkit-scrollbar{display:none}
  .panel input{font-size:16px}
  .spd{min-height:32px}
}
@media (max-width:640px),(hover:none){
  .play{width:44px;height:44px}
  .play svg{width:16px;height:16px}
  .qhead .play{width:44px;height:44px}
  .cf{min-height:44px;padding:7px 14px}
  .btn{min-height:44px;padding:8px 14px}
  .pi{min-height:44px}
  .ag{min-height:44px;padding:3px 16px}
  #seek{height:44px}
  #seek::-webkit-slider-thumb{width:18px;height:18px;margin-top:-7px}
  #seek::-moz-range-thumb{width:18px;height:18px}
}
@media (max-width:480px){.rail{grid-template-columns:minmax(0,1fr)}}
@media (hover:none){html,body{overscroll-behavior-y:none}}
.plist,#stext{overscroll-behavior:contain}
.crow,.tdcs,.md pre,.md .tw{overscroll-behavior-x:contain}
main{transition:transform .2s ease}
main.pulling{transition:none}
#ptr{position:relative;height:0;z-index:5;pointer-events:none}
.ptrp{--o:0;--r:0deg;position:absolute;left:50%;top:10px;display:flex;align-items:center;gap:8px;padding:6px 12px;white-space:nowrap;
  font-size:12.5px;color:var(--muted);background:var(--panel);border:1px solid var(--line);border-radius:999px;box-shadow:var(--shadow);
  opacity:var(--o);transform:translateX(-50%)}
.ptrp.bad{color:var(--bad)}
.ptrp.bad .pspin{display:none}
.pspin{width:14px;height:14px;border-radius:50%;border:2px solid var(--line);border-top-color:var(--accent);transform:rotate(var(--r))}
.ptrp.spin .pspin{animation:ptrspin .8s linear infinite}
@keyframes ptrspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){main{transition:none}.ptrp.spin .pspin{animation-duration:2.4s}
  .cf.busy .sw,.node.busy .dot,.skel,.scrim,.sheet:not(.dock){animation:none}.sheet:not(.dock){transition:none}}
.hsw{position:fixed;left:0;top:0;width:1px;height:1px;margin:0;opacity:0;pointer-events:none}
.ios button{position:relative}
.hz{position:absolute;inset:0;z-index:2;-webkit-tap-highlight-color:transparent}
button:disabled .hz{display:none}
.chead{display:none;align-items:center;gap:4px;max-width:760px;margin:0 auto;
  padding:max(6px,env(safe-area-inset-top)) max(12px,env(safe-area-inset-right)) 6px max(8px,env(safe-area-inset-left))}
body.chatv .chead{display:flex}
body.chatv .agrow,body.chatv .chats{display:none}
body.chatv:not(.playing) .bar{display:none}
body.chatv main{max-width:760px;padding-bottom:calc(var(--dockh,24px) + var(--kbh,0px) + 16px)}
.back{flex:none;width:44px;height:44px;border:0;border-radius:12px;background:none;color:var(--ink);cursor:pointer;display:grid;place-items:center}
.back svg{width:22px;height:22px}
.back:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.ctitle{font-weight:650;line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.csub{font-size:12.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.conv .day{margin:18px 0 6px;text-align:center}
.conv .node{padding-left:0}
.conv .node::before,.conv .dot{display:none}
.msg{display:flex;flex-direction:column;margin:12px 0;min-width:0}
.msg.me{align-items:flex-end}
.bub{max-width:min(85%,560px);background:var(--accent-soft);border:1px solid color-mix(in srgb,var(--accent) 30%,var(--line));
  border-radius:16px 16px 4px 16px;padding:8px 12px;white-space:pre-wrap;overflow-wrap:anywhere}
.msg.failed .bub{background:transparent;border-color:var(--bad)}
.msg.failed .rstate{color:var(--bad)}
.md{min-width:0;overflow-wrap:anywhere}
.md>:first-child{margin-top:0}.md>:last-child{margin-bottom:0}
.md p,.md ul,.md ol,.md blockquote,.md pre,.md .tw{margin:8px 0}
.md ul,.md ol{padding-left:22px}
.md h1,.md h2,.md h3,.md h4,.md h5,.md h6{font-size:1em;font-weight:650;margin:14px 0 6px}
.md hr{border:0;border-top:1px solid var(--line);margin:12px 0}
.md blockquote{margin-left:0;padding-left:12px;border-left:3px solid var(--spine);color:var(--muted)}
.md a{color:var(--accent)}
.md code{font:.9em ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--panel-2);border-radius:5px;padding:1px 4px}
.md pre{background:var(--panel-2);border:1px solid var(--line);border-radius:10px;padding:10px 12px;overflow-x:auto;overflow-wrap:normal}
.md pre code{background:none;padding:0;white-space:pre}
.md .tw{overflow-x:auto}
.md table{border-collapse:collapse;font-size:14px;overflow-wrap:normal}
.md th,.md td{border:1px solid var(--line);padding:5px 9px;text-align:left;vertical-align:top}
.md th{background:var(--panel-2);font-weight:600}
details.tools{margin:6px 0;font-size:13px;color:var(--muted)}
details.tools summary{cursor:pointer;padding:4px 0;width:max-content;max-width:100%}
details.tools ul{list-style:none;margin:2px 0 6px;padding:0 0 0 12px;border-left:2px solid var(--spine)}
details.tools li{padding:2px 0;overflow-wrap:anywhere}
details.tools b{font-weight:600;color:var(--ink)}
.apill{display:flex;align-items:center;gap:10px;margin:8px 0;padding:6px 12px 6px 6px;width:fit-content;max-width:100%;
  background:var(--panel);border:1px solid var(--line);border-radius:14px}
.apill .t{min-width:0;font-size:13.5px;line-height:1.3}
.earlier{display:block;margin:4px auto 10px;min-height:40px;padding:0 16px;border:1px solid var(--line);border-radius:999px;
  background:var(--panel);color:var(--ink);font:inherit;font-size:13.5px;cursor:pointer}
.cstate{display:flex;align-items:center;gap:8px;margin:12px 0;font-size:13.5px;color:var(--muted)}
.cstate.busy::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--accent);animation:pulse 1.4s ease-in-out infinite}
.cnote{margin:16px 0 8px;font-size:13px;color:var(--muted);text-align:center}
.sheet.dock:not([hidden]){display:grid;grid-template-columns:minmax(0,1fr) auto auto;align-items:end;gap:0 10px;max-width:760px;border-radius:16px 16px 0 0}
.sheet.dock .shead{display:none}
.sheet.dock .sact{display:contents}
.sheet.dock .mic{grid-column:2;grid-row:3;width:44px;height:44px}
.sheet.dock #stext{grid-column:1;grid-row:3;min-height:44px;max-height:30vh}
.sheet.dock .send{grid-column:3;grid-row:3;padding:0 16px}
.sheet.dock .mbox{grid-column:1/-1;grid-row:2;margin-bottom:8px}  /* above the controls: the composer grows upward, so the mic stays put for the stop tap */
.sheet.dock .mbox:has(#wave[hidden]):has(.mstat:empty){display:none}
.sheet.dock .serr{grid-column:1/-1;grid-row:1;margin:0 0 8px}
.panel,.sheet{background:var(--panel-solid)}
.keymsg{position:fixed;z-index:99;left:max(16px,env(safe-area-inset-left));right:max(16px,env(safe-area-inset-right));top:max(16px,env(safe-area-inset-top));padding:14px 16px;border-radius:12px;background:var(--panel-solid);color:var(--ink);border:1px solid var(--line);font-size:15px;box-shadow:0 6px 24px rgba(0,0,0,.18)}
.keymsg p{margin:0 0 10px}.keymsg form{display:flex;gap:8px}
.keymsg input{flex:1;min-width:0;height:44px;padding:0 12px;border:1px solid var(--line);border-radius:10px;background:var(--panel-2);color:var(--ink);font:inherit;font-size:16px}
.keymsg button{height:44px;padding:0 16px;border:0;border-radius:999px;background:var(--accent);color:var(--on-accent);font:inherit;font-weight:600}
.cf.more{color:var(--muted);font-weight:600}
.cf.more[hidden]{display:none}
/* New chat: the row's trailing action, filled with the accent and cut to the play button's
   squircle corners (a 22.4%-of-height radius; the true superellipse where corner-shape works) */
.cf.newchat{flex:none;font-weight:600;background:var(--accent);border-color:var(--accent);color:var(--on-accent);border-radius:7px}
@media (hover:hover){.cf.newchat:hover{background:var(--accent);color:var(--on-accent);filter:brightness(1.08)}}
@media (hover:none) and (min-width:641px){.cf.newchat{border-radius:10px}}  /* 22.4% of the 44px touch height */
@supports (corner-shape:superellipse(2)){.cf.newchat{border-radius:999px;corner-shape:superellipse(2.32)}}  /* clamps to half its height */
.cf.newchat[hidden]{display:none}
@media (hover:hover){.cf.more:hover{color:var(--ink)}}
@media (min-width:641px){.chats:not(.open) .crow{max-height:var(--rowh,36px);overflow:hidden;padding:2px;margin:-2px}}
@media (max-width:640px){.cf.more{display:none}}
.agrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.themes{margin-left:auto;display:inline-flex;gap:2px;padding:2px;border:1px solid var(--line);border-radius:999px;background:var(--panel-2)}
.th{display:inline-flex;align-items:center;gap:6px;min-height:28px;padding:3px 10px;border:0;border-radius:999px;
  background:transparent;color:var(--muted);font:inherit;font-size:12.5px;font-weight:600;cursor:pointer}
@media (hover:hover){.th:hover{color:var(--ink)}}
.th:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.th[aria-checked="true"]{background:var(--panel);color:var(--ink);box-shadow:var(--shadow)}
.th .tsw{width:12px;height:12px;border-radius:50%;flex:none;box-shadow:inset 0 0 0 1px rgba(0,0,0,.12)}
.th[data-t="clay"] .tsw{background:linear-gradient(135deg,#23808f 50%,#f0a03a 50%)}
.th[data-t="violet"] .tsw{background:linear-gradient(135deg,#7c4dff,#c084fc)}
@media (max-width:640px),(hover:none){.th{min-height:44px;padding:3px 14px}}
/* clay theme: textured cyan primary, animated rusted-gold secondary, drifting gradient page */
html[data-theme="clay"] body::before{content:"";position:fixed;inset:-10%;z-index:-1;pointer-events:none;
  background:var(--bg-paint);animation:drift 40s ease-in-out infinite alternate}
html[data-theme="clay"] body::after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:var(--grain);opacity:var(--grain-o)}
@keyframes drift{to{transform:translate3d(3%,-2%,0) scale(1.06)}}
@keyframes sheen{to{background-position:-200% 0}}
@keyframes ember{0%,100%{box-shadow:0 0 5px var(--glow)}50%{box-shadow:0 0 14px 2px var(--glow)}}
html[data-theme="clay"] header::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:2px;pointer-events:none;
  background:var(--gold-sheen);background-size:200% 100%;box-shadow:0 0 10px var(--glow);animation:sheen 7s linear infinite}
/* the header play/pause wears the Replies logo */
#pp,#pp.on{border:0;border-radius:50%;background:linear-gradient(135deg,var(--accent),#c084fc);color:#fff}
/* the header play button wears the tab icon's shape, the iPhone app-icon squircle: a 22.4% radius
   (the usual rounded-square stand-in) everywhere, the true superellipse (n=5, as the favicon is
   drawn) where corner-shape is supported. Glow and halo follow the edge; the loading ring spins,
   so it stays a circle, which fits inside the squircle at every angle. */
#pp,#pp.on{border-radius:22.4%}
@supports (corner-shape:superellipse(2)){
  #pp,#pp.on{border-radius:50%;corner-shape:superellipse(2.32)}}
@media (hover:hover){#pp:hover{color:#fff;filter:brightness(1.08)}}
body:not(.playing) #pp{opacity:.8}
html[data-theme="clay"] #pp,html[data-theme="clay"] #pp.on{background:var(--accent-fill);color:#ffc95a;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.18),0 0 0 1px rgba(156,63,22,.3),0 0 10px var(--glow)}
html[data-theme="clay"] #pp svg{filter:drop-shadow(0 0 3px rgba(255,170,60,.85))}
html[data-theme="clay"] .cf.on,html[data-theme="clay"] .play.on,html[data-theme="clay"] .send:not(:disabled){background:var(--accent-fill)}
html[data-theme="clay"] .send:not(:disabled){box-shadow:inset 0 1px 0 rgba(255,255,255,.2),0 2px 10px color-mix(in srgb,var(--accent) 35%,transparent)}
html[data-theme="clay"] .qtag{color:#3a1a04;background:var(--gold-chip);background-size:200% 100%;
  box-shadow:0 0 0 1px rgba(156,63,22,.35),0 0 10px var(--glow);animation:sheen 4s linear infinite}
html[data-theme="clay"] .node.q .dot.open{background:var(--gold-sheen);background-size:200% 100%;border-color:#ffd58a;
  animation:sheen 5s linear infinite,ember 2.6s ease-in-out infinite}
html[data-theme="clay"] .fan::before,html[data-theme="clay"] .qc::before{background:linear-gradient(180deg,#f0a03a,#c2561b);box-shadow:0 0 6px var(--glow)}
html[data-theme="clay"] .qc:not(:last-child)::after{background:linear-gradient(90deg,#c2561b,#f0a03a,#c2561b);box-shadow:0 0 6px var(--glow)}
html[data-theme="clay"] .cstate.busy::before{background:#f0a03a;animation:ember 1.4s ease-in-out infinite}
html[data-theme="clay"] #seek::-webkit-slider-thumb{background:radial-gradient(circle at 35% 35%,#fff0c0,#f6b546 45%,#c2561b);box-shadow:0 0 8px var(--glow)}
html[data-theme="clay"] #seek::-moz-range-thumb{background:radial-gradient(circle at 35% 35%,#fff0c0,#f6b546 45%,#c2561b);box-shadow:0 0 8px var(--glow)}
html[data-theme="clay"] .card{box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.06);background-image:var(--card-tex)}
/* clay dark: white on the light --bad fill is under 3:1, so the armed trash label and the recording mic go dark */
@media (prefers-color-scheme: dark){html[data-theme="clay"]:not([data-scheme="light"]) :is(.tdbt.dng.armed,.mic.rec){color:#2a0f0c}}
html[data-theme="clay"][data-scheme="dark"] :is(.tdbt.dng.armed,.mic.rec){color:#2a0f0c}
/* playing: the button looks like the clay logo and rusted gold eats up the cyan as it is heard,
   using the original wave-edged level fill; the dark icon with a cream halo reads on both */
html[data-theme="clay"] .play.on{color:#fff;border-color:transparent;background:var(--accent-fill);animation:ember 2.4s ease-in-out infinite}
/* white icon on the cyan; a soft dark shadow keeps it readable once the gold level fill is behind it */
html[data-theme="clay"] .play.on svg{position:relative;z-index:1;
  filter:drop-shadow(0 1px 1.5px rgba(0,0,0,.35))}
html[data-theme="clay"] .play .lvl{background:linear-gradient(90deg,#8a3412,#c2561b 40%,#f08a24 80%,#ffc24f)}
html[data-theme="clay"] .play .lvl::after{background:#ffc24f;filter:drop-shadow(0 0 3px rgba(255,190,80,.95))}
html[data-theme="clay"] .play.on .lvl{background:linear-gradient(90deg,transparent 40%,rgba(255,240,200,.5) 50%,transparent 60%) 0 0/200% 100%,
  linear-gradient(90deg,#8a3412,#c2561b 40%,#f08a24 80%,#ffc24f);animation:lvlsheen 3.5s linear infinite}
@keyframes lvlsheen{from{background-position:200% 0,0 0}to{background-position:-100% 0,0 0}}
@media (prefers-reduced-motion:reduce){
  html[data-theme="clay"] body::before,html[data-theme="clay"] header::after,html[data-theme="clay"] .qtag,
  html[data-theme="clay"] .node.q .dot,html[data-theme="clay"] .cstate.busy::before,html[data-theme="clay"] .play.on,html[data-theme="clay"] .play.on .lvl{animation:none}}
/* golden ratio: the play triangle's visible height is the circle's diameter / phi^2 (~.382). It fills
   11/16 of its 16-unit box, so the box is .382 / .6875 ~ 55.6% of the circle, at every button size. */
.play[data-key],.bar #pp{padding:0}
.play[data-key] svg{width:calc(55.6% + 1.11px);height:calc(55.6% + 1.11px)}  /* % is of the box inside the 1px border */
.bar #pp svg{width:55.6%;height:55.6%}
/* background music menu: small, tucked under the header player */
.mpanel{position:absolute;right:8px;top:calc(100% + 8px);z-index:30;width:min(290px,calc(100vw - 32px));display:flex;flex-direction:column;gap:10px;
  background:var(--panel-solid);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:10px}
.mpanel[hidden]{display:none}
.mtracks{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.mt{min-height:38px;border:1px solid var(--line);border-radius:10px;background:var(--panel-2);color:var(--ink);font:inherit;font-size:13px;cursor:pointer}
@media (hover:hover){.mt:hover{border-color:var(--accent)}}
.mt:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.mt[aria-checked="true"]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.mvol{display:flex;align-items:center;gap:10px;font-size:12.5px;color:var(--muted)}
.mvol input{flex:1;min-width:0;accent-color:var(--accent)}
.mnote{font-size:11.5px;line-height:1.35;color:var(--muted)}
#mus.on{color:var(--accent);border-color:color-mix(in srgb,var(--accent) 45%,var(--line));background:var(--accent-soft)}
/* violet theme: an "iris" set that mirrors clay effect for effect (drifting backdrop, sliding header
   sheen, glowing playing button, shimmering level fill, sheen chips) in cool violet, fuchsia and sky */
:root[data-theme="violet"]{
  --iris:linear-gradient(90deg,#7c4dff 0%,#c084fc 22%,#f0abfc 38%,#7dd3fc 52%,#c084fc 68%,#7c4dff 86%,#7c4dff 100%);
  --iris-chip:linear-gradient(90deg,#ede4ff 0%,#f5d0fe 35%,#e0f2fe 55%,#ede4ff 100%);
  --halo:rgba(124,77,255,.42);
  --bg-paint:radial-gradient(110% 75% at 0% 0%,rgba(124,77,255,.16),transparent 60%),
    radial-gradient(90% 70% at 100% 100%,rgba(217,70,239,.12),transparent 60%),
    radial-gradient(60% 50% at 85% 10%,rgba(56,189,248,.10),transparent 60%),
    linear-gradient(160deg,#f7f6fb 0%,#f5f3fa 55%,#f8f1fa 100%);
}
@media (prefers-color-scheme: dark){
  :root[data-theme="violet"]:not([data-scheme="light"]){
    --iris-chip:linear-gradient(90deg,#3b2a6b 0%,#5b2a6e 35%,#1f3b57 55%,#3b2a6b 100%);
    --halo:rgba(167,139,250,.45);
    --bg-paint:radial-gradient(110% 75% at 0% 0%,rgba(124,77,255,.20),transparent 60%),
      radial-gradient(90% 70% at 100% 100%,rgba(217,70,239,.13),transparent 60%),
      radial-gradient(60% 50% at 85% 10%,rgba(56,189,248,.09),transparent 60%),
      linear-gradient(160deg,#0f0e13 0%,#120f1a 55%,#160f1a 100%);
  }
}
:root[data-theme="violet"][data-scheme="dark"]{
    --iris-chip:linear-gradient(90deg,#3b2a6b 0%,#5b2a6e 35%,#1f3b57 55%,#3b2a6b 100%);
    --halo:rgba(167,139,250,.45);
    --bg-paint:radial-gradient(110% 75% at 0% 0%,rgba(124,77,255,.20),transparent 60%),
      radial-gradient(90% 70% at 100% 100%,rgba(217,70,239,.13),transparent 60%),
      radial-gradient(60% 50% at 85% 10%,rgba(56,189,248,.09),transparent 60%),
      linear-gradient(160deg,#0f0e13 0%,#120f1a 55%,#160f1a 100%);
  }
@keyframes halo{0%,100%{box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 35%,transparent),0 0 8px var(--halo)}
  50%{box-shadow:0 0 0 5px color-mix(in srgb,var(--accent) 12%,transparent),0 0 18px 2px var(--halo)}}
@keyframes iris-sheen{to{background-position:-200% 0}}
@keyframes iris-drift{0%{transform:translate3d(0,0,0) scale(1)}50%{transform:translate3d(-2%,2%,0) scale(1.05)}100%{transform:translate3d(3%,-1%,0) scale(1.03)}}
html[data-theme="violet"] body::before{content:"";position:fixed;inset:-10%;z-index:-1;pointer-events:none;
  background:var(--bg-paint);animation:iris-drift 48s ease-in-out infinite alternate}
html[data-theme="violet"] header::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:2px;pointer-events:none;
  background:var(--iris);background-size:200% 100%;box-shadow:0 0 10px var(--halo);animation:iris-sheen 8s linear infinite}
html[data-theme="violet"] #pp,html[data-theme="violet"] #pp.on{background:var(--play-fill);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.25),0 0 0 1px rgba(124,77,255,.25),0 0 12px var(--halo)}
html[data-theme="violet"] #pp svg{filter:drop-shadow(0 0 3px rgba(255,255,255,.7))}
html[data-theme="violet"] #pp.on{animation:halo 2.4s ease-in-out infinite}
html[data-theme="violet"] .cf.on,html[data-theme="violet"] .send:not(:disabled){background:var(--play-fill);border-color:transparent}
html[data-theme="violet"] .send:not(:disabled){box-shadow:inset 0 1px 0 rgba(255,255,255,.2),0 2px 12px var(--halo)}
html[data-theme="violet"] .play.on{background:var(--play-fill);border-color:transparent;animation:halo 2.4s ease-in-out infinite}
html[data-theme="violet"] .play .lvl{background:linear-gradient(90deg,#6d3ff5,#a855f7 45%,#e879f9 85%,#f0abfc)}
html[data-theme="violet"] .play .lvl::after{background:#f0abfc;filter:drop-shadow(0 0 3px rgba(240,171,252,.9))}
html[data-theme="violet"] .play.on .lvl{background:linear-gradient(90deg,transparent 40%,rgba(255,255,255,.45) 50%,transparent 60%) 0 0/200% 100%,
  linear-gradient(90deg,#6d3ff5,#a855f7 45%,#e879f9 85%,#f0abfc);animation:lvlsheen 3.5s linear infinite}
html[data-theme="violet"] .qtag{color:var(--ink);background:var(--iris-chip);background-size:200% 100%;
  box-shadow:0 0 0 1px rgba(124,77,255,.25),0 0 10px var(--halo);animation:iris-sheen 5s linear infinite}
html[data-theme="violet"] .node.q .dot.open{background:var(--iris);background-size:200% 100%;border-color:#c4b5fd;
  animation:iris-sheen 6s linear infinite,halo 2.8s ease-in-out infinite}
html[data-theme="violet"] .fan::before,html[data-theme="violet"] .qc::before{background:linear-gradient(180deg,#a78bfa,#d946ef);box-shadow:0 0 6px var(--halo)}
html[data-theme="violet"] .qc:not(:last-child)::after{background:linear-gradient(90deg,#d946ef,#a78bfa,#d946ef);box-shadow:0 0 6px var(--halo)}
html[data-theme="violet"] .cstate.busy::before{background:#a78bfa;animation:halo 1.4s ease-in-out infinite}
html[data-theme="violet"] #seek::-webkit-slider-thumb{background:radial-gradient(circle at 35% 35%,#fff,#c4b5fd 45%,#7c4dff);box-shadow:0 0 8px var(--halo)}
html[data-theme="violet"] #seek::-moz-range-thumb{background:radial-gradient(circle at 35% 35%,#fff,#c4b5fd 45%,#7c4dff);box-shadow:0 0 8px var(--halo)}
html[data-theme="violet"] .card{box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.5);transition:transform .2s ease,box-shadow .2s ease,border-color .2s}
@media (hover:hover){html[data-theme="violet"] .card:hover{transform:translateY(-1px);box-shadow:0 2px 4px rgba(20,16,40,.05),0 10px 26px rgba(124,77,255,.12)}}
@media (prefers-color-scheme: dark){
  html[data-theme="violet"]:not([data-scheme="light"]) .card{box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.04)}
}
html[data-theme="violet"][data-scheme="dark"] .card{box-shadow:var(--shadow),inset 0 1px 0 rgba(255,255,255,.04)}
@media (prefers-reduced-motion:reduce){
  html[data-theme="violet"] body::before,html[data-theme="violet"] header::after,html[data-theme="violet"] .qtag,
  html[data-theme="violet"] .node.q .dot.open,html[data-theme="violet"] .cstate.busy::before,html[data-theme="violet"] .play.on,
  html[data-theme="violet"] .play.on .lvl,html[data-theme="violet"] #pp.on{animation:none}
  html[data-theme="violet"] .card:hover{transform:none}}
/* light/dark button, beside the theme switch */
.sch{display:inline-grid;place-items:center;width:32px;min-height:28px;margin-left:2px;border:0;border-radius:999px;background:transparent;color:var(--muted);cursor:pointer}
@media (hover:hover){.sch:hover{color:var(--ink)}}
.sch:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.sch svg{width:15px;height:15px;display:none}
.sch[data-s="auto"] .i-auto,.sch[data-s="light"] .i-light,.sch[data-s="dark"] .i-dark{display:block}
@media (max-width:640px),(hover:none){.sch{width:44px;min-height:44px}}
:root[data-scheme="light"]{color-scheme:light}
:root[data-scheme="dark"]{color-scheme:dark}
/* a clip is fetched before its button unlocks; "loading" is pressed but not yet sounding */
.play.warming{cursor:pointer;opacity:.55;animation:none}
/* tapped while still loading: queued to play once ready */
.play.warming.wanted{opacity:1;border-color:var(--accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 30%,transparent)}
.play.warming::after,.play.loading::after{content:"";position:absolute;inset:3px;border-radius:50%;border:2px solid transparent;
  border-top-color:currentColor;animation:rspin .8s linear infinite;opacity:.7;pointer-events:none}
@keyframes rspin{to{transform:rotate(360deg)}}
/* a reply's labelled sub-recordings, nested under its card like a short branch of the spine */
.subs{margin:6px 0 0 14px;padding-left:14px;border-left:2px solid var(--spine);display:flex;flex-direction:column;gap:6px}
.sub{position:relative;display:flex;align-items:center;gap:10px;min-height:44px;padding:5px 8px 5px 10px;
  background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
.sub::before{content:"";position:absolute;left:-16px;top:50%;width:14px;height:2px;background:var(--spine)}
.sk{flex:none;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:2px 8px;border-radius:999px}
.sk.dec{color:var(--amber);background:var(--amber-soft)}
.sk.warn{color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent)}
.sk.next{color:var(--accent);background:var(--accent-soft)}
.sub .st{flex:1;min-width:0;font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub .play{width:30px;height:30px}
@media (max-width:640px){.sub .play{width:44px;height:44px}}
@media (prefers-reduced-motion:reduce){.play.warming::after,.play.loading::after{animation-duration:2.4s}}
/* the unplayed rule above outranks the plain reduced-motion reset, so name it here too */
@media (prefers-reduced-motion:reduce){.play[data-key]:not(.done):not(.on):not(:disabled):not(.warming){animation:none}}
/* phone pass: keep the pinned header to two short rows under the player, give the seek bar room,
   and make every control a 40-44px target. Inputs stay at 16px so iOS Safari doesn't zoom on focus. */
@media (max-width:640px){
  .time{display:none}
  .th{font-size:0;gap:0;padding:3px 14px}
  .th .tsw{width:16px;height:16px}
  .agrow{flex-wrap:nowrap}
  .agents .ag{padding:3px 10px;min-width:44px;justify-content:center}
  #newchat{flex:none;width:44px;padding:0;justify-content:center;font-size:0;gap:0;border-radius:22.4%}
  @supports (corner-shape:superellipse(2)){#newchat{border-radius:50%}}
  /* New chat sits right of the past-chats button, so hang its panel from the row's edge, not the button's */
  .chats{position:relative}.pastwrap{position:static}
  #pastpanel{right:max(12px,env(safe-area-inset-right));top:calc(100% - 4px)}
  #newchat::before{content:"";flex:none;width:12px;height:12px;background:linear-gradient(currentColor 0 0) 50%/100% 2px no-repeat,linear-gradient(currentColor 0 0) 50%/2px 100% no-repeat}
  .cf.past .car{display:none}
  .spd{min-height:44px}
  .tdbt{height:44px}
  .tdbt.ic{width:44px}
  .tdc{height:44px}
  .tdic{width:44px;height:44px}
  .tdi .acts{margin:-8px -8px 0 0}
  .tdi input[type=checkbox]{width:22px;height:22px;margin-top:1px}
  .tdck{display:flex;flex:none;padding:11px;margin:-11px}
  .tdai{padding:3px 8px;min-height:24px;position:relative}
  .tdai::after{content:"";position:absolute;inset:-9px -7px}
  .tdin,.tdsel{height:44px}
  .rcancel{min-width:44px;min-height:44px}
  .sub .st{white-space:normal;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-height:1.3}
}
@media (max-width:640px),(pointer:coarse){
  input:not([type]),input[type=text],input[type=search],select,textarea{font-size:16px}
  .mt,.earlier,.panel input{min-height:44px}
  .node .dot::after{inset:-14px}  /* 16 px inside the border + 2 x 14 = 44 px to tap */
  #mvol{height:44px}
  /* text-only toggles: a taller hit area without moving anything */
  .tds>summary{padding-top:14px;padding-bottom:12px;margin:-8px 0}
  .tdd>summary{padding:14px 0;margin:-8px 0}
  details.tools summary{position:relative;padding:12px 0;margin:-8px 0}
  .qfold>summary{padding-top:14px;padding-bottom:12px;margin:-8px 0}
}
/* a card well off screen (set by script) holds its looping animations still; nothing visible changes */
.offv,.offv *,.offv ::before,.offv ::after{animation-play-state:paused!important}
</style>
</head>
<body>
<header>
  <div class="bar">
    <div class="player"><button class="play" id="pp" aria-label="Play or pause"></button>
      <div class="pbody"><div class="ptop"><span class="pname">Replies</span><div class="t idle" id="pt">Nothing playing</div><button class="auto" id="aut" title="Auto-play new replies as they arrive" aria-label="Auto-play new replies" aria-pressed="false">Auto</button></div>
      <div class="seekrow"><input type="range" id="seek" min="0" max="1000" step="1" value="0" aria-label="Seek" disabled>
      <span class="time" id="tm">0:00 / 0:00</span>
      <button class="spd" id="spd" title="Playback speed (Shift-click to go back)">1×</button>
      <button class="spd" id="ccb" title="Focus captions: a few words at a time" aria-label="Focus captions" aria-pressed="false">CC</button>
      <button class="spd" id="mus" title="Music and voice" aria-label="Music and voice" aria-haspopup="true" aria-expanded="false">♪</button>
      <button class="spd" id="shr" title="Share this recording" aria-label="Share this recording" hidden><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 10V2M5 4.8 8 2l3 2.8M4.5 7H3.5v7h9V7h-1"/></svg></button></div></div>
      <div class="mpanel" id="mpanel" role="group" aria-label="Music and voice" hidden>
        <div class="mtracks" role="radiogroup" aria-label="Track">
          <button class="mt" role="radio" data-track="off" aria-checked="true">Off</button>
          <button class="mt" role="radio" data-track="432" aria-checked="false">432 Hz calm</button>
          <button class="mt" role="radio" data-track="40" aria-checked="false">40 Hz focus</button>
          <button class="mt" role="radio" data-track="quest" aria-checked="false">Quest</button></div>
        <label class="mvol">Volume<input type="range" id="mvol" min="0" max="100" value="40" aria-label="Music volume"></label>
        <div class="mnote">Made on this device. Goes quiet under a reply and silent while you talk. 40 Hz works best in headphones.</div>
        <div class="mlab" id="vlab">Voice for new replies</div>
        <div class="mtracks vstyle" role="radiogroup" aria-labelledby="vlab">
          <button class="mt" role="radio" data-style="relaxed" aria-checked="false" title="Fuller, learning-paced, spoken a little slower">Relaxed</button>
          <button class="mt" role="radio" data-style="standard" aria-checked="true">Standard</button>
          <button class="mt" role="radio" data-style="brief" aria-checked="false" title="Key points only, spoken a little quicker">Brief</button></div>
</div></div>
  </div>
  <div class="agrow"><div class="agents" role="group" aria-label="Agent">
    <button class="ag" data-agent="claude"><span class="n">Claude Code</span><span class="c"></span></button>
    <button class="ag" data-agent="pi"><span class="n">pi</span><span class="c"></span></button>
  </div>
  <div class="themes" role="radiogroup" aria-label="Theme">
    <button class="th" role="radio" data-t="clay" aria-checked="false"><span class="tsw"></span>Clay</button>
    <button class="th" role="radio" data-t="violet" aria-checked="false"><span class="tsw"></span>Violet</button>
    <button class="sch" id="scheme" data-s="auto" aria-label="Appearance: auto"><svg class="i-auto" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor"/></svg><svg class="i-light" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="8" cy="8" r="3"/><path d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3 3l1.1 1.1M11.9 11.9 13 13M3 13l1.1-1.1M11.9 4.1 13 3"/></svg><svg class="i-dark" viewBox="0 0 16 16"><path d="M13.5 10.2A6 6 0 0 1 5.8 2.5a6 6 0 1 0 7.7 7.7z" fill="currentColor"/></svg></button>
  </div></div>
  <nav class="chats" id="chats" aria-label="Filter by chat">
    <div class="crow" id="crow"></div>
    <button class="cf more" id="morebtn" aria-controls="crow" aria-expanded="false" hidden></button>
    <div class="pastwrap" id="pastwrap" hidden>
      <button class="cf past" id="pastbtn" aria-haspopup="true" aria-expanded="false" aria-controls="pastpanel"></button>
      <div class="panel" id="pastpanel" hidden>
        <input id="pq" type="search" placeholder="Search all chats" aria-label="Search all chats" autocomplete="off">
        <div class="plist" id="plist"></div>
      </div>
    </div>
    <button class="cf newchat" id="newchat" aria-label="New chat">+ New chat</button>
  </nav>
  <div class="chead"><button class="back" id="back" aria-label="Back to all chats"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg></button>
    <div class="grow"><div class="ctitle" id="ctitle"></div><div class="csub" id="csub"></div></div></div>
</header>
<div id="ptr"><div class="ptrp" id="ptrp" role="status"><span class="pspin"></span><span id="ptrt"></span></div></div>
<main><div id="todo"></div><div id="tree"><div class="empty">Loading…</div></div></main>
<div class="cc" id="cc" hidden><div class="ccvs" aria-hidden="true"></div><div class="cct" aria-hidden="true"></div><div class="ccsize" role="group" aria-label="Caption size"><button id="ccm" title="Smaller captions" aria-label="Smaller captions">A−</button><button id="ccp" title="Larger captions" aria-label="Larger captions">A+</button></div></div>
<div class="scrim" id="scrim" hidden></div>
<section class="sheet" id="sheet" role="dialog" aria-modal="true" aria-labelledby="stitle" hidden>
  <div class="shead"><div class="grow"><div class="slabel" id="swho"></div><div class="stitle" id="stitle"></div></div>
    <button class="x" id="sclose" aria-label="Close">×</button></div>
  <textarea id="stext" rows="3" placeholder="Type a reply, or tap the mic and talk" aria-label="Your reply" maxlength="4000"></textarea>
  <div class="serr" id="serr" role="alert" hidden></div>
  <div class="sact"><div class="mbox"><canvas id="wave" aria-hidden="true" hidden></canvas><div class="mstat" id="mstat" aria-live="polite"></div></div>
    <button class="mic" id="mic" aria-label="Tap to talk" title="Tap to talk"></button>
    <button class="send" id="ssend" disabled>Send</button></div>
</section>
<label class="hsw" id="hlabel" for="hsw" aria-hidden="true"></label>
<input class="hsw" id="hsw" type="checkbox" switch tabindex="-1" aria-hidden="true">
<script>
applyTheme(theme);
document.getElementById('scheme').addEventListener('click',()=>{const t=document.documentElement.dataset.theme||'clay',order=['auto','light','dark'];
  schemes[t]=order[(order.indexOf(schemes[t]||'auto')+1)%3];try{localStorage.setItem('replies-scheme',JSON.stringify(schemes))}catch(e){}applyScheme()});
document.querySelectorAll('.th[data-t]').forEach(b=>b.addEventListener('click',()=>{
  theme=applyTheme(b.dataset.t);try{localStorage.setItem('replies-theme',theme)}catch(e){}}));
const PLAY='<svg viewBox="0 0 16 16" fill="currentColor"><path d="M4 2.5v11l9-5.5z"/></svg>';
const PAUSE='<svg viewBox="0 0 16 16" fill="currentColor"><path d="M4 2h3v12H4zM9 2h3v12H9z"/></svg>';
// a clip button carries both icons; CSS shows the check once it is done (and play again on hover)
const PLAYC=PLAY.replace('<svg','<svg class="pl"')+'<svg class="ck" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path pathLength="1" d="M3.5 8.5l3 3 6-6.5"/></svg>';
const audio=new Audio(); let cur=null, queue=[], last='', data={entries:[],chats:[]}, pull=null;
const slugify=s=>String(s||'').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60);
const chatPath=c=>'/c/'+(slugify(c.title)?slugify(c.title)+'-':'')+c.short;
const fromPath=()=>{const m=location.pathname.match(/^\/c\/(?:[a-z0-9-]*-)?([0-9a-f]{8})$/i);return m?m[1].toLowerCase():''};
const agentPath=a=>a==='pi'?'/a/pi':'/';
const fromAgent=()=>location.pathname==='/a/pi'?'pi':'claude';
let want=fromPath(), view=want?'?'+want:'', filter='all', agent=fromAgent();  // view: the chat open in detail; filter: the chip narrowing the home feed
function resolveWanted(){if(!want)return;const c=data.chats.find(x=>x.short===want);if(!c)return;
  view=c.id;agent=c.agent;const p=chatPath(c);if(location.pathname!==p)history.replaceState(null,'',p)}
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const hue=s=>{let h=0;for(const c of String(s))h=(h*31+c.charCodeAt(0))%360;return h};
const sw=id=>`<span class="sw" style="background:hsl(${hue(id)} 70% 55%)"></span>`;
const fmtTime=t=>new Date(t*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
function dayLabel(t){const d=new Date(t*1000),n=new Date();const k=x=>x.toDateString();
  if(k(d)===k(n))return'Today';const y=new Date(n);y.setDate(n.getDate()-1);
  if(k(d)===k(y))return'Yesterday';return d.toLocaleDateString([], {weekday:'long',month:'short',day:'numeric'})}
const STATUS={rendering:'writing…',summarizing:'summarizing…',voicing:'voicing…',failed:'audio failed'};
const HEARD='replies.heard.v1';let heard={};
try{heard=JSON.parse(localStorage.getItem(HEARD)||'{}')||{}}catch(_){heard={}}
let heardDirty=0;function saveHeard(force,leaving){const now=Date.now();if(!force&&now-heardDirty<1500)return;heardDirty=now;
  let s={};try{s=JSON.parse(localStorage.getItem(HEARD)||'{}')||{}}catch(_){}
  let ahead=false;for(const k in heard)if(heard[k]>(+s[k]||0))ahead=true;
  if(typeof s==='object')for(const k in s)if(+s[k]>(heard[k]||0)){heard[k]=Math.min(1,+s[k]);setLevel(k)}
  if(ahead)try{localStorage.setItem(HEARD,JSON.stringify(heard))}catch(_){}
  pushHeard(leaving)}
// The server's map is what other devices see. heardSent = what it is known to hold, so only progress
// it lacks is sent. A batch stays under MAX_JSON_BODY; the rest goes on the next sync.
let heardSent={},pushing=false;
async function pushHeard(leaving){if(pushing&&!leaving)return;
  const body={};for(const k of Object.keys(heard).filter(k=>heard[k]>(heardSent[k]||0)).slice(0,400))body[k]=heard[k];
  if(!Object.keys(body).length)return;pushing=true;
  try{const r=await fetch('/api/heard',{method:'POST',headers:{'X-Replies':'1'},body:JSON.stringify({heard:body}),keepalive:!!leaving});
    if(r.ok)for(const k in body)heardSent[k]=Math.max(heardSent[k]||0,body[k])}catch(_){}
  pushing=false}
function mergeHeard(m){const changed=new Set();
  for(const k in m){const v=Math.min(1,+m[k]||0);heardSent[k]=Math.max(heardSent[k]||0,v);if(v>(heard[k]||0)){heard[k]=v;changed.add(k)}}
  if(!changed.size)return;document.querySelectorAll('.play[data-key]').forEach(b=>{if(changed.has(b.dataset.file))showLevel(b)});
  changed.forEach(f=>paintDots(f,false));saveHeard(true)}
let heardTag='';  // merging the same map twice changes nothing, so an unchanged one comes back as a 304
async function syncHeard(){try{const r=await fetch('/api/heard',{cache:'no-store',headers:heardTag?{'If-None-Match':heardTag}:{}});
    if(r.ok){const j=await r.json();heardTag=r.headers.get('ETag')||'';mergeHeard(j.heard)}}catch(_){}
  pushHeard()}
// Timeline dots: a hand-set mark wins; otherwise a reply is checked once every clip in it is heard.
// Tapping cycles empty -> check -> star -> empty; landing back on what listening alone would show
// clears the hand mark, so the dot goes back to following the audio.
let marks={},markBusy=0;
const NEXT={open:'check',check:'star',star:'open'},
  MARK_LABEL={open:'Mark as done',check:'Done. Tap to star',star:'Starred. Tap to clear'},
  CHECK_I='<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 8.5l3 3 6-6.5"/></svg>',
  STAR_I='<svg viewBox="0 0 16 16" fill="currentColor"><path d="M8 1.2l2 4.3 4.7.5-3.5 3.2 1 4.6L8 11.4l-4.2 2.4 1-4.6L1.3 6l4.7-.5z"/></svg>';
const markIcon=s=>s==='check'?CHECK_I:s==='star'?STAR_I:'';
const autoMark=files=>files.length&&files.every(f=>pct(f)>=99)?'check':'open';
const markOf=(id,files)=>marks[id]||autoMark(files);
const dotFiles=d=>(d.dataset.files||'').split(' ').filter(Boolean);
function dotHTML(id,files){const s=markOf(id,files);
  return `<button class="dot ${s}" data-mark="${esc(id)}" data-files="${esc(files.join(' '))}" aria-label="${MARK_LABEL[s]}">${markIcon(s)}</button>`}
function paintDot(d,pop){const s=markOf(d.dataset.mark,dotFiles(d));if(d.classList.contains(s))return;
  d.classList.remove('open','check','star');d.classList.add(s);d.innerHTML=markIcon(s);d.setAttribute('aria-label',MARK_LABEL[s]);
  if(pop&&s!=='open'){d.classList.remove('pop');void d.offsetWidth;d.classList.add('pop');setTimeout(()=>d.classList.remove('pop'),900)}return true}
function paintDots(file,pop){let q=false;document.querySelectorAll(file?`.dot[data-files~="${CSS.escape(file)}"]`:'.dot[data-mark]').forEach(d=>{if(paintDot(d,pop)&&d.closest('.node.q'))q=true});
  if(q)setTimeout(renderKeepFocus)}  // a question card folds or unfolds with its dot
// Re-renders, then puts keyboard focus back on the dot or play button that had it.
function renderKeepFocus(){const a=document.activeElement,k=a&&a.dataset||{};
  const sel=k.mark!=null?`.dot[data-mark="${CSS.escape(k.mark)}"]`:k.key!=null?`.play[data-key="${CSS.escape(k.key)}"]`:'';
  render();if(sel&&!a.isConnected){const n=document.querySelector(sel),c=n&&n.closest('details:not([open])');  // a closed fold can't hold focus: its summary takes it
    const t=c?c.querySelector('summary'):n;if(t)t.focus()}}
async function syncMarks(){if(markBusy)return;
  try{const r=await fetch('/api/marks',{cache:'no-store'});if(r.ok&&!markBusy){marks=(await r.json()).marks||{};paintDots()}}catch(_){}}
async function cycleMark(d){const id=d.dataset.mark,files=dotFiles(d),s=NEXT[markOf(id,files)],state=s===autoMark(files)?null:s;
  if(state)marks[id]=state;else delete marks[id];paintDot(d,true);markBusy++;
  if(d.closest('.node.q'))setTimeout(()=>{renderKeepFocus();const n=document.querySelector(`.dot[data-mark="${CSS.escape(id)}"]`);  // once the click is done with the old dot
    if(n&&n!==d&&s!=='open'){n.classList.add('pop');setTimeout(()=>n.classList.remove('pop'),900)}});
  try{await post('/api/marks',JSON.stringify({id,state}),{'Content-Type':'application/json'})}catch(_){}finally{markBusy--}}
const pct=f=>Math.round(Math.min(1,heard[f]||0)*1000)/10;
const lvl=f=>f&&heard[f]>0?`<span class="lvl" style="--p:${pct(f)}%"></span>`:'';
function showLevel(b){const p=pct(b.dataset.file);let l=b.querySelector('.lvl');if(!l){l=document.createElement('span');l.className='lvl';b.prepend(l)}
  l.style.setProperty('--p',p+'%');b.title=`Listened ${Math.round(p)}%`;
  const was=b.classList.contains('done'),d=p>=99;b.classList.toggle('done',d);
  // celebrate only a clip finished here, not ones that arrive already heard from another device
  if(d&&!was&&cur&&cur.file===b.dataset.file){justDone.add(b.dataset.file);celebrate(b)}}
// Pops a just-finished button, or waits for it to stop playing (syncButtons calls again on pause/end).
const justDone=new Set();
function celebrate(b){if(b.classList.contains('on')||!justDone.has(b.dataset.file))return;justDone.delete(b.dataset.file);
  b.classList.remove('pop');void b.offsetWidth;b.classList.add('pop');buzz('ok');setTimeout(()=>b.classList.remove('pop'),1500)}
function setLevel(file){document.querySelectorAll('.play[data-key]').forEach(b=>{if(b.dataset.file===file)showLevel(b)});paintDots(file,true)}
// Clips are fetched ahead into memory (the server sends no-store, so the browser keeps nothing) as
// their buttons near the screen, and a button only unlocks once its clip is all here, so a press
// starts sound at once. The newest CLIP_KEEP stay; older ones are let go.
const clips=new Map(),CLIP_KEEP=60;
const clipUrl=f=>(clips.get(f)||{}).url||'';
// Clips average ~1 MB. Fetched all at once they split a phone's bandwidth a dozen ways (so none
// unlocks for a long time) and take every connection the feed's polls need, so CLIP_PAR load at a
// time and the next one is whichever waiting clip's button sits nearest the screen. A press, or
// the next clip of "Play all", skips the line.
const CLIP_PAR=2,clipWait=[];let clipBusy=0,clipTick=0;
function clipNear(c){let best=Infinity;document.querySelectorAll(`.play[data-file="${CSS.escape(c.file)}"]`).forEach(b=>{
  const r=b.getBoundingClientRect(),d=r.bottom<0?-r.bottom:r.top>innerHeight?r.top-innerHeight:0;if(d<best)best=d});return best}
function clipNext(){while(clipBusy<CLIP_PAR&&clipWait.length){let k=0,best=Infinity;
  clipWait.forEach((c,i)=>{const d=clipNear(c);if(d<best){best=d;k=i}});clipWait.splice(k,1)[0].go()}}
function fetchClip(file,now){let c=clips.get(file);if(c){clips.delete(file);clips.set(file,c);if(now&&c.go)c.go();return c.p}
  c={url:'',p:null,file};clips.set(file,c);
  c.p=new Promise(start=>{c.go=()=>{c.go=null;const i=clipWait.indexOf(c);if(i>=0)clipWait.splice(i,1);clipBusy++;start()};
      if(now)c.go();else{clipWait.push(c);if(!clipTick)clipTick=setTimeout(()=>{clipTick=0;clipNext()})}})  // a whole batch is queued before the nearest is picked
    .then(()=>fetch('/r/'+file)).then(r=>{if(!r.ok)throw 0;return r.blob()}).then(b=>{c.blob=b;c.url=URL.createObjectURL(b);setTimeout(tryWant);
      document.querySelectorAll('.play[data-key]').forEach(x=>{if(x.dataset.file===file){x.classList.remove('warming','wanted');x.disabled=false;x.setAttribute('aria-label','Play');x.removeAttribute('aria-pressed')}});return c.url})
    .catch(()=>{clips.delete(file);if(!armRetry)armRetry=setTimeout(()=>{armRetry=0;armClips()},4000);return ''})  // one retry timer, however many clips failed
    .finally(()=>{clipBusy--;clipNext()});
  for(const [k,v] of clips){if(clips.size<=CLIP_KEEP)break;if(cur&&cur.file===k)continue;clips.delete(k);if(v.url)URL.revokeObjectURL(v.url)}
  return c.p}
const clipIO=new IntersectionObserver(es=>es.forEach(en=>{if(en.isIntersecting){clipIO.unobserve(en.target);fetchClip(en.target.dataset.file)}}),{rootMargin:'1600px 0px'});
let armRetry=0;
function armClips(){let eager=0;  // the newest dozen are fetched now, wherever they sit; the rest as they near the screen
  const bs=[...document.querySelectorAll('.play.warming[data-file]')];if(view)bs.reverse();  // a chat reads oldest first, so its newest are last
  bs.forEach(b=>{if(!b.dataset.file)return;if(eager<12&&!b.closest('#todo')){eager++;fetchClip(b.dataset.file)}else clipIO.observe(b)})}
// Only added elements can bring new buttons; text-only changes (the seek time ~4x a second, the
// recording clock every frame) are skipped so they don't rescan the page.
const addsEls=ms=>ms.some(m=>{for(const n of m.addedNodes)if(n.nodeType===1)return true;return false});
let armQ=0;new MutationObserver(ms=>{if(!armQ&&addsEls(ms))armQ=requestAnimationFrame(()=>{armQ=0;armClips();armAnims()})}).observe(document.body,{childList:true,subtree:true});
// Offscreen cards stop their looping CSS animations (spinners, sheens, glows): with hundreds of
// cards those kept a phone's main thread repainting all the time, which janked every scroll.
// What is on screen looks and moves the same.
const animIO=new IntersectionObserver(es=>es.forEach(en=>en.target.classList.toggle('offv',!en.isIntersecting)),{rootMargin:'200px 0px'});
let animSeen=new WeakSet();
function animReset(){animIO.disconnect();animSeen=new WeakSet()}  // the tree was replaced: let the old cards go
function armAnims(){document.querySelectorAll('#tree .node,#tree .apill').forEach(n=>{if(!animSeen.has(n)){animSeen.add(n);animIO.observe(n)}})}
let sounding=false;  // true from the 'playing' event until it pauses, stalls or ends
// A clip still being written or voiced can be tapped: it's queued ("wanted") and plays as soon
// as it's ready. Only a failed clip is disabled.
let wantPlay=null,wantTick=0,wantTry=0,playLoading=0;
function playBtn(key,file,label,failed){const mine=cur&&cur.key===key&&!audio.paused,on=mine&&sounding,ready=!!(file&&clipUrl(file));
  const t=file&&heard[file]>0?` title="Listened ${Math.round(pct(file))}%"`:'',pending=!ready&&!failed,wanted=pending&&!!wantPlay&&wantPlay.key===key;
  return `<button class="play ${on?'on':''} ${mine&&!sounding?'loading':''} ${pending?'warming':''} ${wanted?'wanted':''} ${pct(file)>=99?'done':''}" data-key="${esc(key)}" data-file="${esc(file||'')}" data-label="${esc(label)}" ${failed?'disabled':''} aria-label="${wanted?'Queued to play when ready':pending?'Play when ready':'Play'}"${pending?` aria-pressed="${wanted}"`:''}${t}>${lvl(file)}${on?PAUSE:PLAYC}</button>`}
// iOS lets audio start later only if this element already played inside a tap, so a queued tap
// plays a split second of silence first (only when nothing is loaded).
let audioUnlocked=false;
function unlockAudio(){if(audioUnlocked||cur||queue.length)return;audioUnlocked=true;
  const n=800,b=new ArrayBuffer(44+n*2),v=new DataView(b),w=(o,s)=>[...s].forEach((c,i)=>v.setUint8(o+i,c.charCodeAt(0)));
  w(0,'RIFF');v.setUint32(4,36+n*2,true);w(8,'WAVEfmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,8000,true);v.setUint32(28,16000,true);v.setUint16(32,2,true);v.setUint16(34,16,true);w(36,'data');v.setUint32(40,n*2,true);
  const u=URL.createObjectURL(new Blob([b],{type:'audio/wav'}));audio.src=u;audio.play().then(()=>{if(!cur)audio.pause()}).catch(()=>{}).finally(()=>URL.revokeObjectURL(u))}
function paintWant(){document.querySelectorAll('.play.warming[data-key]').forEach(b=>{const on=!!wantPlay&&b.dataset.key===wantPlay.key;
  b.classList.toggle('wanted',on);b.setAttribute('aria-pressed',String(on));b.setAttribute('aria-label',on?'Queued to play when ready':'Play when ready')})}
// The 700 ms check runs only while something is queued, and asks for a queued clip that isn't on
// the server yet at most every 2.5 s (the feed's own poll); the usual 4 s retry still runs too.
function setWant(w){if(!w&&!wantPlay)return;wantPlay=w;wantTry=0;if(w&&!wantTick)wantTick=setInterval(tryWant,700);
  if(!w&&wantTick){clearInterval(wantTick);wantTick=0}paintWant()}
function tryWant(){if(!wantPlay)return;const id=wantPlay.key.split('#')[0],b=[...document.querySelectorAll('.play[data-key]')].find(x=>x.dataset.key===wantPlay.key);
  if(!data.entries.some(e=>e.id===id)||b&&b.disabled){setWant(null);return}  // pruned, or its audio failed
  if(!b){setWant(null);return}  // its view was left (another chat or filter): nothing on screen would show it
  if(!b.dataset.file)return;if(clipUrl(b.dataset.file)){const w=wantPlay;setWant(null);queue=[];play(w.key,b.dataset.file,w.label);return}
  if(Date.now()-wantTry>=2500){wantTry=Date.now();fetchClip(b.dataset.file,true)}}
function chosenSet(q,answers){const a=answers&&answers[q.question];if(!a)return{set:new Set(),other:''};
  const labels=q.options.map(o=>o.label);
  if(labels.includes(String(a)))return{set:new Set([String(a)]),other:''};
  const set=new Set(String(a).split(/,\s*/).filter(p=>labels.includes(p)));
  return{set,other:set.size?'':String(a)}}
const chatTitle=id=>{const c=data.chats.find(x=>x.id===id);return c?c.title:''};
function meta(e){const st=STATUS[e.status]?` · <span class="status ${e.status}">${STATUS[e.status]}</span>`:'';
  const who=e.chat&&filter==='all'&&!view?`${sw(e.chat)}<span class="who">${esc(chatTitle(e.chat))}</span>`:'';
  return `<div class="meta"><span>${fmtTime(e.created)}</span>${who}${st}</div>`}
// Options can be tapped on a question set nobody has answered yet, for when the terminal is out of
// reach. Picks live here until Send, which sends them as the chat's answer (the server closes the
// open question first). One pick per question, or several where the question allows it.
const picks={};
const pickable=e=>e.kind==='questions'&&!e.legacy&&canSend(data.chats.find(c=>c.id===e.chat))  // tree and chat view alike
  &&!Object.keys(e.answers||{}).length&&!e.in_chat&&!(e.responses||[]).some(r=>r.status!=='failed');
const plain=l=>l.replace(/\s*\(Recommended\)\s*/i,'');
function togglePick(id,qi,oi){const e=data.entries.find(x=>x.id===id);const q=e&&e.questions[qi];if(!q||!pickable(e))return;
  const p=picks[id]||(picks[id]={}),cur=p[qi]||[];
  p[qi]=cur.includes(oi)?cur.filter(x=>x!==oi):q.multiSelect?[...cur,oi]:[oi];
  if(!p[qi].length)delete p[qi];if(!Object.keys(p).length){delete picks[id];openTools.add('q:'+id)}buzz('tap');render()}  // clearing the last pick keeps a finished card open
async function sendPicks(id,btn){const e=data.entries.find(x=>x.id===id),p=picks[id];if(!e||!p||btn.disabled)return;
  const text='My answers, picked on the replies page. '+e.questions.map((q,i)=>`${q.header||'Question '+(i+1)}: `+
    (p[i]?p[i].map(oi=>plain(q.options[oi].label)).join(' + '):'no pick, ask me again')).join('; ');
  btn.disabled=true;btn.textContent='Sending…';
  try{const r=await post('/api/respond',JSON.stringify({entry:id,text}),{'Content-Type':'application/json'});
    if(!r.ok){btn.disabled=false;btn.textContent=await reason(r,'Couldn’t send');buzz('bad');return}
    delete picks[id];buzz('ok');autoAnswered();last='';poll()}
  catch(_){btn.disabled=false;btn.textContent='Couldn’t reach your Mac';buzz('bad')}}
function qCard(e,q,i){const ch=chosenSet(q,e.answers);const tap=pickable(e),mine=(picks[e.id]||{})[i]||[];
  const opts=q.options.map((o,oi)=>{const rec=/\(Recommended\)/i.test(o.label);const lab=plain(o.label);
    const picked=ch.set.has(o.label),on=mine.includes(oi);
    const inner=`<span class="l">${esc(lab)}</span>${picked||on?'<span class="badge ok">✓</span>':rec?'<span class="badge rec">Rec</span>':''}`;
    return tap?`<button type="button" class="opt tap ${rec?'rec-o':''} ${on?'pick':''}" data-pick="${esc(e.id)}" data-q="${i}" data-o="${oi}" aria-pressed="${on}">${inner}</button>`
      :`<div class="opt ${rec?'rec-o':''} ${picked?'chosen':''}">${inner}</div>`}).join('');
  return `<div class="qc"><div class="qhead"><div class="grow"><span class="qtag">${esc(q.header||('Q'+(i+1)))}</span>
    ${q.summary?`<div class="qsum">${esc(q.summary)}</div>`:'<div class="skel" style="width:85%"></div><div class="skel" style="width:60%"></div>'}</div>
    ${playBtn(e.id+'#'+i,q.audio,(q.header||'Question')+': '+(q.summary||''),!q.audio&&(e.status==='failed'||!STATUS[e.status]))}</div>
    <div class="opts">${opts}</div>${ch.other?`<div class="other">${esc(ch.other)}</div>`:''}</div>`}
const REPLY='<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6.5 3 2 7.2l4.5 4.2M2.4 7.2h6.8c2.7 0 4.8 1.9 4.8 5"/></svg>';
const MIC='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>';
const HOLD={busy:'Queued until the chat finishes',dialog:'Queued: the chat has a question open',draft:'Queued: the chat’s prompt box has text in it',unreachable:'Queued: can’t see the chat’s terminal',resuming:'Reopening the chat…'};
const canSend=c=>!!c&&(!!c.reply||!!c.resume);
const canReply=e=>!view&&(e.kind==='reply'||e.kind==='questions')&&!e.legacy&&canSend(data.chats.find(c=>c.id===e.chat));
const replyBtn=e=>canReply(e)?`<button class="rbtn" data-reply="${esc(e.id)}" aria-label="Reply to this">${REPLY}<span>Reply</span></button>`:'';
// Retry only where the server says it can help (see retry_response): a failure before anything
// was typed, or a cmux prompt box that may only hold Claude's dim suggestion.
const retryBtn=r=>r.retryable&&(r.status==='failed'||r.hold==='draft')?` · <button class="rcancel" data-retry="${esc(r.id)}">Retry</button>`:'';
function respState(r){if(r.status==='sent')return r.pickup?'In the chat’s queue: it reads it at its next step':'Sent to chat';if(r.status==='sending')return'Sending…';
  if(r.status==='failed')return `${esc(r.error||'Couldn’t send')}${retryBtn(r)}`;
  return `${esc(HOLD[r.hold]||'Queued')}${retryBtn(r)} · <button class="rcancel" data-cancel="${esc(r.id)}">Cancel</button>`}
function resps(e){if(view||!(e.responses||[]).length)return'';  // the chat view shows them as messages
  return `<div class="resps">${e.responses.map(r=>`<div class="resp ${esc(r.status)}">${esc(r.text)}<div class="rstate">${respState(r)}</div></div>`).join('')}</div>`}
const openAttr=e=>!view&&data.chats.some(c=>c.id===e.chat)?` data-open="${esc(e.chat)}"`:'';
// A finished question card (its dot shows a check or star, every question is answered, or it was
// answered in the chat) folds to one line of answers; tapping it opens the cards again for this session.
// A card with picks waiting to be sent stays open, so a tap never folds the options being picked.
function qDone(e){if(picks[e.id]&&pickable(e))return false;const m=markOf(e.id,e.questions.map(q=>q.audio).filter(Boolean));
  return m==='check'||m==='star'||!!e.in_chat||(e.questions.length>0&&e.questions.every(q=>(e.answers||{})[q.question]))}
function qFold(e,fan){if(!qDone(e))return fan;const k='q:'+e.id;
  const line=e.questions.map((q,i)=>{const ch=chosenSet(q,e.answers),a=[...ch.set].map(plain).join(' + ')||ch.other;
    return `<span class="qa"><b>${esc(q.header||'Q'+(i+1))}</b> ${a?esc(a):e.in_chat?'in the chat':'<i>not answered</i>'}</span>`}).join('');
  return `<details class="qfold" data-k="${esc(k)}"${openTools.has(k)?' open':''}><summary>${line}</summary>${fan}</details>`}
function node(e){const isQ=e.kind==='questions';const busy=STATUS[e.status]&&e.status!=='failed';
  if(isQ){const n=e.questions.length;const answered=Object.keys(e.answers||{}).length;
    const files=e.questions.some(q=>q.audio);
    return `<div class="node q ${busy?'busy':''}">${dotHTML(e.id,e.questions.map(q=>q.audio).filter(Boolean))}<div class="card qwrap"${openAttr(e)}>
      <div class="qtop"><div class="grow"><div class="title">${n} decision${n>1?'s':''}${answered?` · ${answered} answered`:e.in_chat?' · replied in chat':''}</div>${meta(e)}</div>
      ${picks[e.id]&&pickable(e)?`<button class="btn sendpicks" data-sendpicks="${esc(e.id)}">Send ${Object.keys(picks[e.id]).length}/${n}</button>`:''}${replyBtn(e)}${files?`<button class="btn" data-all="${esc(e.id)}">▶ Play all</button>`:''}</div>
      ${qFold(e,`<div class="fan"><div class="lane"><div class="rail" style="--n:${n}">${e.questions.map((q,i)=>qCard(e,q,i)).join('')}</div></div></div>`)}${e.in_chat?`<div class="other inchat">Replied in chat: ${esc(e.in_chat)}</div>`:''}</div>${resps(e)}</div>`}
  const title=e.title||(e.legacy?'Earlier reply':'');
  return `<div class="node ${busy?'busy':''}">${dotHTML(e.id,e.status==='ready'&&e.audio?[e.audio]:[])}<div class="card"${openAttr(e)}>
    <div class="grow"><div class="title">${title?esc(title):'<div class="skel" style="width:55%"></div>'}</div>${meta(e)}</div>
    ${replyBtn(e)}
    ${playBtn(e.id,e.status==='ready'?e.audio:null,title||'Reply',e.status==='failed'||!!e.legacy&&!e.audio)}</div>${subCards(e,title)}${resps(e)}</div>`}
// A reply's extra recordings exist only for a named reason (a decision, a warning, a next step): each
// gets its own small titled card under the reply, played only if wanted. Unlabelled ones never show.
const SUB_KINDS={Decision:'dec',Warning:'warn',Next:'next'};
function subCards(e,title){const ps=e.status==='ready'?(e.parts||[]).filter(p=>p.ready&&p.audio&&p.title&&Object.prototype.hasOwnProperty.call(SUB_KINDS,p.kind)):[];if(!ps.length)return'';
  return `<div class="subs">${ps.map((p,i)=>`<div class="sub"><span class="sk ${SUB_KINDS[p.kind]}">${esc(p.kind)}</span><span class="st" title="${esc(p.title)}">${esc(p.title)}</span>`
    +playBtn(e.id+'#p'+(i+2),p.audio,`${title||'Reply'} · ${p.kind}: ${p.title}`)+'</div>').join('')}</div>`}
function ago(t){const s=Math.max(0,Date.now()/1000-t);if(s<3600)return Math.max(1,Math.round(s/60))+'m';
  if(s<86400)return Math.round(s/3600)+'h';if(s<86400*7)return Math.round(s/86400)+'d';
  return new Date(t*1000).toLocaleDateString([], {month:'short',day:'numeric'})}
const pastChats=()=>data.chats.filter(c=>c.agent===agent&&!c.open&&c.count);
// The lookup lists every chat of this agent, open ones first, each group newest first.
const allChats=()=>data.chats.filter(c=>c.agent===agent&&(c.open||c.count))
  .sort((a,b)=>(b.open-a.open)||((b.last||0)-(a.last||0)));
function renderAgents(){document.querySelectorAll('.ag').forEach(b=>{const on=b.dataset.agent===agent;
  b.classList.toggle('on',on);b.setAttribute('aria-pressed',String(on));
  b.querySelector('.c').textContent=data.chats.filter(c=>c.agent===b.dataset.agent&&(c.open||c.count)).length||''})}
function renderChats(){renderAgents();const open=data.chats.filter(c=>c.agent===agent&&c.open),past=pastChats();
  const sel=past.find(c=>c.id===filter);  // a picked past chat stays visible as a chip
  const chip=c=>`<button class="cf ${filter===c.id?'on':''} ${c.open?'':'closed'} ${c.status==='busy'?'busy':''}" data-chat="${esc(c.id)}" title="${esc(c.project)} · ${esc(c.short)}">${sw(c.id)}<span class="n">${esc(c.title)}</span>${c.count?`<span class="c">${c.count}</span>`:''}</button>`;
  $('#crow').innerHTML=`<button class="cf ${filter==='all'?'on':''}" data-chat="all"><span class="n">All chats</span></button>`
    +open.map(chip).join('')+(sel?chip(sel):'');
  const all=allChats().length;
  $('#pastwrap').hidden=!all;$('#newchat').hidden=agent!=='claude';
  $('#pastbtn').innerHTML=`<span class="n">${all} chat${all===1?'':'s'}</span><span class="car">▾</span>`;
  $('#pastbtn').setAttribute('aria-label',`Find a chat: ${all} chat${all===1?'':'s'}`);
  if(!$('#pastpanel').hidden)renderPast();
  fitChats()}
let chatsOpen=false;try{chatsOpen=localStorage.getItem('chatsOpen')==='1'}catch(_){}
$('#chats').classList.toggle('open',chatsOpen);
function fitChats(){const row=$('#crow'),btn=$('#morebtn'),kids=[...row.children];
  if(!kids.length||matchMedia('(max-width:640px)').matches){btn.hidden=true;return}
  row.style.setProperty('--rowh',kids[0].offsetHeight+4+'px');
  const below=()=>kids.filter(k=>k.offsetTop>kids[0].offsetTop+2);
  btn.hidden=false;btn.textContent=chatsOpen?'Show less ▴':'+99 more ▾';  // reserve the button's width before measuring
  if(!chatsOpen){const on=row.querySelector('.cf.on');  // keep the picked chat in view when collapsed
    if(on&&on!==kids[0]&&below().includes(on)){row.insertBefore(on,kids[1]);kids.splice(kids.indexOf(on),1);kids.splice(1,0,on)}}
  const n=below().length;btn.hidden=!n;
  btn.textContent=chatsOpen?'Show less ▴':`+${n} more ▾`;btn.setAttribute('aria-expanded',String(chatsOpen))}
$('#morebtn').addEventListener('click',()=>{chatsOpen=!chatsOpen;$('#chats').classList.toggle('open',chatsOpen);
  try{localStorage.setItem('chatsOpen',chatsOpen?'1':'0')}catch(_){};fitChats()});
new ResizeObserver(()=>requestAnimationFrame(fitChats)).observe($('#chats'));
function renderPast(){const q=$('#pq').value.trim().toLowerCase();
  const list=allChats().filter(c=>!q||(c.title+' '+c.project+' '+c.short).toLowerCase().includes(q));
  const keep=document.activeElement&&document.activeElement.closest('#plist')?document.activeElement.dataset.chat:'';
  const row=c=>`<button class="pi ${filter===c.id?'on':''}" data-chat="${esc(c.id)}" title="${esc(c.project)} · ${esc(c.short)}">${sw(c.id)}<span class="n">${esc(c.title)}</span>${c.count?`<span class="c">${c.count}</span>`:''}<span class="ago">${c.last?ago(c.last):''}</span></button>`;
  const group=(name,cs)=>cs.length?`<div class="pgh" role="presentation">${name} · ${cs.length}</div>`+cs.map(row).join(''):'';
  $('#plist').innerHTML=list.length?group('Open',list.filter(c=>c.open))+group('Past',list.filter(c=>!c.open))
    :`<div class="pnone">No chats match “${esc(q)}”.</div>`;
  if(keep){const b=[...$('#plist').querySelectorAll('[data-chat]')].find(x=>x.dataset.chat===keep);if(b)b.focus()}}
function setPast(open,focusBtn){$('#pastpanel').hidden=!open;$('#pastbtn').setAttribute('aria-expanded',String(open));
  if(open){renderPast();$('#pq').focus()}else if(focusBtn)$('#pastbtn').focus()}
$('#pastbtn').addEventListener('click',ev=>{ev.stopPropagation();setPast($('#pastpanel').hidden)});
$('#pq').addEventListener('input',renderPast);
$('#pastpanel').addEventListener('keydown',ev=>{const items=[...$('#plist').querySelectorAll('[data-chat]')];
  const i=items.indexOf(document.activeElement);
  if(ev.key==='Escape'){ev.preventDefault();setPast(false,true)}
  else if(ev.key==='ArrowDown'){ev.preventDefault();(items[i+1]||items[0])?.focus()}
  else if(ev.key==='ArrowUp'){ev.preventDefault();if(i<=0)$('#pq').focus();else items[i-1].focus()}
  else if(ev.key==='Enter'&&document.activeElement===$('#pq')&&items.length===1){ev.preventDefault();items[0].click()}});
// Transcript text is escaped first, so the only tags on the page are the ones added here.
function inline(s){const code=[];
  s=esc(s).replace(/`([^`\n]+)`/g,(m,c)=>{code.push(c);return '\u0000'+(code.length-1)+'\u0000'});
  s=s.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\s][^*\n]*)\*(?=[\s).,;:!?]|$)/g,'$1<em>$2</em>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]*[^\s<).,;:!?])/g,'$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  return s.replace(/\u0000(\d+)\u0000/g,(m,i)=>`<code>${code[i]}</code>`)}
const LIST=/^\s*(?:[-*+]|\d+[.)])\s+/,RULE=/^\s*([-*_])\1{2,}\s*$/,TSEP=/^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
const cells=l=>l.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(x=>x.trim());
const isTable=(L,i)=>L[i].includes('|')&&i+1<L.length&&L[i+1].includes('-')&&TSEP.test(L[i+1]);
function md(src){const L=String(src).split('\n');let o='',i=0;
  while(i<L.length){const l=L[i];
    if(!l.trim()){i++;continue}
    if(/^\s*```/.test(l)){const b=[];i++;while(i<L.length&&!/^\s*```/.test(L[i]))b.push(L[i++]);i++;o+=`<pre><code>${esc(b.join('\n'))}</code></pre>`;continue}
    const h=l.match(/^(#{1,6})\s+(.*)$/);if(h){o+=`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`;i++;continue}
    if(RULE.test(l)){o+='<hr>';i++;continue}
    if(isTable(L,i)){const head=cells(l);i+=2;let rows='';
      while(i<L.length&&L[i].includes('|')&&L[i].trim())rows+=`<tr>${cells(L[i++]).map(c=>`<td>${inline(c)}</td>`).join('')}</tr>`;
      o+=`<div class="tw"><table><thead><tr>${head.map(c=>`<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>`;continue}
    if(LIST.test(l)){const tag=/^\s*\d/.test(l)?'ol':'ul',b=[];
      while(i<L.length&&(LIST.test(L[i])||(L[i].trim()&&/^\s+/.test(L[i])))){if(LIST.test(L[i]))b.push(L[i].replace(LIST,''));else b[b.length-1]+=' '+L[i].trim();i++}
      o+=`<${tag}>${b.map(x=>`<li>${inline(x)}</li>`).join('')}</${tag}>`;continue}
    if(/^\s*>/.test(l)){const b=[];while(i<L.length&&/^\s*>/.test(L[i]))b.push(L[i++].replace(/^\s*>\s?/,''));o+=`<blockquote>${md(b.join('\n'))}</blockquote>`;continue}
    const p=[];while(i<L.length&&L[i].trim()&&!/^\s*```/.test(L[i])&&!/^#{1,6}\s/.test(L[i])&&!LIST.test(L[i])&&!RULE.test(L[i])&&!isTable(L,i)&&!/^\s*>/.test(L[i]))p.push(inline(L[i++]));
    o+=`<p>${p.join('<br>')}</p>`}
  return o}
let conv=null,treeHtml='',homeHtml='',wasChat=false;const openTools=new Set();
const docked=()=>sheet.classList.contains('dock');
function addItems(v,items,front){const add=[];for(const it of items||[]){if(v.seen.has(it.id))continue;v.seen.add(it.id);
    if(it.role==='assistant')it.html=md(it.text);add.push(it)}
  v.items=front?add.concat(v.items):v.items.concat(add);return add.length}
async function loadConv(v){let failed=false;
  try{const r=await fetch('/api/conversation?chat='+encodeURIComponent(v.id),{cache:'no-store'});if(conv!==v)return;
    if(r.ok){const d=await r.json();if(conv!==v)return;addItems(v,d.items);v.start=d.start;v.end=d.end;v.live=true}}
  catch(_){failed=true}
  if(conv===v){v.retry=failed;v.ready=true;render()}}
async function pollConv(){const v=conv;if(!v||!v.ready||v.busy||document.hidden)return;
  if(v.retry){v.retry=false;return loadConv(v)}
  if(!v.live)return;v.busy=true;
  try{const r=await fetch(`/api/conversation?chat=${encodeURIComponent(v.id)}&after=${v.end}`,{cache:'no-store'});
    if(!r.ok||conv!==v)return;const d=await r.json();if(conv!==v)return;
    if(d.reset){conv=null;render();return}
    v.end=d.end;if(addItems(v,d.items))render()}
  catch(_){}finally{v.busy=false}}
async function loadEarlier(){const v=conv;if(!v||v.busy||!v.start)return;v.busy=true;
  try{const r=await fetch(`/api/conversation?chat=${encodeURIComponent(v.id)}&before=${v.start}`,{cache:'no-store'});
    if(!r.ok||conv!==v)return;const d=await r.json();if(conv!==v)return;
    v.anchor=document.documentElement.scrollHeight-scrollY;addItems(v,d.items,true);v.start=d.start;render()}
  catch(_){}finally{v.busy=false}}
function syncConv(){if(!view||view[0]==='?'){conv=null;return}
  if(!conv||conv.id!==view){conv={id:view,items:[],seen:new Set(),start:0,end:0,ready:false,live:false,stick:true};treeHtml='';loadConv(conv)}}
function dock(c){if(!sheet.hidden)closeSheet();sheet.classList.add('dock');sheet.setAttribute('aria-modal','false');stext.rows=1;
  target={id:'chat:'+c.id,chat:c.id,title:c.title,direct:true};stext.value=drafts[target.id]||'';sheetError('');setMic('idle');sheet.hidden=false;fit()}
function undock(){if(docked())closeSheet()}
function setTree(html){if(html===treeHtml)return;const el=document.documentElement,near=innerHeight+scrollY>=el.scrollHeight-160;
  animReset();$('#tree').innerHTML=html;treeHtml=html;homeHtml='';if(!conv)return;
  if(conv.anchor!=null){scrollTo(0,el.scrollHeight-conv.anchor);conv.anchor=null}
  else if(near||conv.stick)scrollTo(0,el.scrollHeight);
  if(conv.ready)conv.stick=false}
function timeline(c){const items=conv.items,floor=conv.start>0&&items.length?items[0].t:0,rows=items.map(it=>({t:it.t,it}));
  for(const e of data.entries)if(e.chat===view&&(e.created||0)>=floor)rows.push({t:e.created,e});
  for(const r of (c&&c.responses)||[])if(r.status!=='sent'&&(r.created||0)>=floor)rows.push({t:r.created,r});
  rows.sort((a,b)=>a.t-b.t);let html='',day='',tools=[];
  const flush=()=>{if(!tools.length)return;const k=tools[0].id,n=tools.length;
    html+=`<details class="tools" data-k="${esc(k)}"${openTools.has(k)?' open':''}><summary>${n} tool call${n>1?'s':''}</summary><ul>${tools.map(x=>`<li><b>${esc(x.name)}</b> ${esc(x.text)}</li>`).join('')}</ul></details>`;tools=[]};
  for(const row of rows){if(row.it&&row.it.role==='tool'){tools.push(row.it);continue}
    flush();const d=row.t?dayLabel(row.t):day;if(d!==day){day=d;html+=`<div class="day">${esc(d)}</div>`}
    if(row.it)html+=row.it.role==='user'?`<div class="msg me"><div class="bub">${esc(row.it.text)}</div></div>`:`<div class="msg"><div class="md">${row.it.html}</div></div>`;
    else if(row.r)html+=`<div class="msg me ${esc(row.r.status)}"><div class="bub">${esc(row.r.text)}<div class="rstate">${respState(row.r)}</div></div></div>`;
    else if(row.e.kind==='questions')html+=node(row.e);
    else{const t=row.e.title||'Reply';html+=`<div class="apill">${playBtn(row.e.id,row.e.status==='ready'?row.e.audio:null,t,row.e.status==='failed')}<span class="t">${esc(t)}</span></div>`}}
  flush();return html}
function renderChatView(){syncConv();const c=data.chats.find(x=>x.id===view);wasChat=true;
  $('#ctitle').textContent=c?c.title:'Chat';
  $('#csub').textContent=c?[c.project,!c.open?'Closed':c.status==='busy'?'Working':c.status==='waiting'?'Question open':'Ready'].filter(Boolean).join(' · '):'';
  if(canSend(c)){if(!docked()||!target||target.chat!==c.id)dock(c);stext.placeholder=c.resume?'Sending reopens this chat':'Message this chat'}else undock();
  if(!conv){setTree(`<div class="empty">${last?'This chat isn’t open and has no audio. <a href="/">Show all chats</a>':'Loading…'}</div>`);return}
  if(!conv.ready){setTree('<div class="empty">Loading…</div>');return}
  let html=timeline(c)||'<div class="empty">Nothing to show for this chat yet.</div>';
  if(conv.start>0)html=`<button class="earlier" data-earlier="1">Show earlier messages</button>`+html;
  if(c&&c.open&&c.status==='busy')html+='<div class="cstate busy">Working…</div>';
  else if(c&&c.open&&c.status==='waiting')html+='<div class="cstate">Waiting on a question: tap your answers on its card, then Send</div>';
  const note=!c||canSend(c)?'':c.agent==='pi'?'pi chats can’t be messaged from this page.':c.open?'Not in tmux or cmux, so nothing can type into it.':'Closed, and it can’t be reopened from here.';
  if(note)html+=`<div class="cnote">${note}</div>`;
  setTree(`<div class="conv">${html}</div>`)}
function render(){renderChats();const chat=!!view;document.body.classList.toggle('chatv',chat);
  if(chat){renderChatView();return}
  undock();conv=null;treeHtml='';if(wasChat){wasChat=false;scrollTo(0,0)}
  const list=data.entries.filter(e=>filter==='all'?e.agent===agent:e.chat===filter);
  if(!list.length){const c=data.chats.find(x=>x.id===filter);homeHtml='';  // a chat with no audio has no card to tap, so link its view here
    $('#tree').innerHTML=`<div class="empty">${filter==='all'?'No replies from this agent yet.':'No audio for this chat yet.'}${c?` <a href="${esc(chatPath(c))}" data-open="${esc(c.id)}">Open chat</a>`:''}</div>`;return}
  let html='',day='';for(const e of list){const d=dayLabel(e.created);if(d!==day){day=d;html+=`<div class="day">${esc(d)}</div>`}html+=node(e)}
  // A feed change elsewhere (a chat going busy or idle, another agent's reply) often leaves this
  // list's markup as it was; rebuilding hundreds of cards then only costs time and restarts their animations.
  if(html===homeHtml)return;animReset();$('#tree').innerHTML=html;homeHtml=html}
// The feed is ~300 KB, so the ETag of the body in `last` goes back and an unchanged feed is a 304
// with no body. Clearing `last` (to force a redraw) also drops the tag, so a full body comes back.
let lastTag='';
async function poll(ms){try{const r=await fetch('/api/entries',{cache:'no-store',headers:last&&lastTag?{'If-None-Match':lastTag}:{},signal:ms?AbortSignal.timeout(ms):undefined});
  if(r.status===304){autoPump();return true}
  if(!r.ok)return false;const t=await r.text();
  if(t!==last&&!pull){last=t;lastTag=r.headers.get('ETag')||'';data=JSON.parse(t);resolveWanted();render();
    autoKnown=autoNote(data.entries,autoKnown,autoWait,autoOn);autoPump()}return true}catch(_){return false}}
window.addEventListener('popstate',()=>{want=fromPath();view=want?'?'+want:'';agent=fromAgent();resolveWanted();
  const f=data.chats.find(c=>c.id===filter);if(f&&f.agent!==agent)filter='all';render()});
async function play(key,file,label){if(cur&&cur.key===key){audio.paused?audio.play():audio.pause();return}
  let url=clipUrl(file);if(!url){playLoading++;try{url=await fetchClip(file,true)}finally{playLoading--}}if(!url)return;  // a queued clip may not be fetched yet
  cur={key,label,file};sounding=false;audio.src=url;
  // A clip listened to part-way picks up a couple of seconds before where listening stopped (any
  // device: the level is shared). Finished or barely-started clips play from the top.
  const h=heard[file]||0;if(h>.02&&h<.95)audio.addEventListener('loadedmetadata',()=>{if(cur&&cur.file===file&&dur())audio.currentTime=Math.max(0,h*dur()-2)},{once:true});
  const p=audio.play();$('#pt').textContent=label;$('#pt').classList.remove('idle');return p}
// Only a button whose play/pause icon flips is redrawn: rewriting all of them (hundreds on the home
// feed) on every play, pause and stall made each press slow, and on iOS re-armed every haptic label.
// Share: the playing clip goes to the device's own share sheet (Messages, AirDrop, Files...) as an
// mp3. Only offered where the sheet takes audio files, and only once the clip is on this device.
const canShareAudio=(()=>{try{return !!(navigator.share&&navigator.canShare&&navigator.canShare({files:[new File([new Blob()],'r.mp3',{type:'audio/mpeg'})]}))}catch(_){return false}})();
const shareBlob=()=>cur&&cur.file&&(clips.get(cur.file)||{}).blob;
function shareName(label){return((label||'reply').replace(/[^A-Za-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60).toLowerCase()||'reply')+'.mp3'}
function syncShare(){const b=$('#shr');if(b)b.hidden=!(canShareAudio&&shareBlob())}
function syncButtons(){syncShare();document.querySelectorAll('.play[data-key]').forEach(b=>{const mine=cur&&b.dataset.key===cur.key&&!audio.paused,on=mine&&sounding;
  const was=b.classList.contains('on');b.classList.toggle('on',on);b.classList.toggle('loading',mine&&!sounding);
  if(was!==on||!b.querySelector('svg'))b.innerHTML=lvl(b.dataset.file)+(on?PAUSE:PLAYC);celebrate(b)});
  const pp=$('#pp'),pon=!!cur&&!audio.paused&&sounding;pp.innerHTML=pon?PAUSE:PLAY;pp.classList.toggle('on',pon);pp.classList.toggle('loading',!!cur&&!audio.paused&&!sounding);
  document.body.classList.toggle('playing',!!cur)}
function markHeard(frac,force){if(!cur||!cur.file||!(frac>(heard[cur.file]||0)))return;
  heard[cur.file]=Math.min(1,frac);setLevel(cur.file);saveHeard(force)}
audio.addEventListener('play',()=>{from=audio.currentTime;saveHeard(true);syncButtons()});  // saveHeard: pick up other tabs' progress first
audio.addEventListener('pause',()=>{sounding=false;syncButtons();saveHeard(true)});
audio.addEventListener('playing',()=>{sounding=true;syncButtons()});
for(const t of['waiting','stalled','emptied','ended'])audio.addEventListener(t,()=>{sounding=false;syncButtons()});
const seek=$('#seek');let dragging=false,lastT=-1,run=false,from=0;
const SPEEDS=[1,1.25,1.5,1.75,2],SPD='replies.speed.v1';let speed=1;
try{const v=+localStorage.getItem(SPD);if(SPEEDS.includes(v))speed=v}catch(_){}
function setSpeed(v){speed=v;audio.defaultPlaybackRate=v;audio.playbackRate=v;  // default survives a new src
  const b=$('#spd');b.textContent=v+'×';b.setAttribute('aria-label','Playback speed '+v+'×');b.classList.toggle('fast',v!==1);
  try{localStorage.setItem(SPD,String(v))}catch(_){}}
$('#shr').addEventListener('click',ev=>{ev.stopPropagation();const blob=shareBlob();if(!blob||!canShareAudio)return;
  const file=new File([blob],shareName(cur.label),{type:'audio/mpeg'});
  // The file alone: with a title too, the sheet's Copy puts two items on the clipboard (the file
  // and the title's text). The file is already named after the reply's title.
  navigator.share({files:[file]}).catch(e=>{if(e&&e.name!=='AbortError')console.warn('share failed',e)})});
$('#spd').addEventListener('click',ev=>{const i=SPEEDS.indexOf(speed);
  setSpeed(SPEEDS[(i+(ev.shiftKey?SPEEDS.length-1:1))%SPEEDS.length])});
setSpeed(speed);
const dur=()=>isFinite(audio.duration)?audio.duration:0;
const fmt=s=>{s=Math.max(0,Math.floor(s||0));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
function showTime(t){const d=dur();seek.value=d?Math.round(t/d*1000):0;
  seek.style.setProperty('--p',seek.value/10+'%');$('#tm').textContent=fmt(t)+' / '+fmt(d)}
audio.addEventListener('timeupdate',()=>{const t=audio.currentTime,d=dur();if(!dragging)showTime(t);
  // Only time actually played counts: a run of playback has to start (from = where it began) inside
  // the stretch already heard, so seeking ahead (or dragging to the end) and playing from there adds
  // nothing. A run ends only on a seek or a new clip, so a stalled timeupdate doesn't stop it.
  if(d&&cur&&lastT>=0&&t>lastT&&t-lastT<1.5*Math.max(1,audio.playbackRate)&&(run||from<=(heard[cur.file]||0)*d+1.5)){run=true;markHeard(t/d)}
  lastT=t});
audio.addEventListener('seeking',()=>{lastT=-1;run=false});audio.addEventListener('seeked',()=>{from=audio.currentTime});audio.addEventListener('emptied',()=>{lastT=-1;run=false});
audio.addEventListener('durationchange',()=>{seek.disabled=!dur();showTime(audio.currentTime)});
seek.addEventListener('input',()=>{dragging=true;showTime(seek.value/1000*dur())});
seek.addEventListener('change',()=>{if(dur())audio.currentTime=seek.value/1000*dur();dragging=false});
// Focus captions (CC): the playing clip's spoken script, a few words at a time, with the word being
// spoken lit up. The voice's own word timings place every word; without them each phrase gets a
// share of the clip by its length (a beat more after a comma or sentence) and each word a share of
// its phrase. Everything follows seeking and playback speed through currentTime.
const ccScripts=new Map();let ccOn=false,ccChunks=null,ccFile='',ccIdx=-1,ccWi=-2,ccRaf=0;
try{ccOn=localStorage.getItem('replies-cc')==='1'}catch(_){}
const CC_END=/[.!?…,;:—–]["”’)]*$/,CC_LEAD=.2,CC_SIZES=[.6,.7,.8,.9,1,1.15,1.3,1.5,1.75];
function ccSplit(text){const words=text.split(/\s+/).filter(Boolean),out=[];let cur=[],len=0,first=0;
  words.forEach((w,i)=>{if(!cur.length)first=i;cur.push(w);len+=w.length+1;
    const stop=/[.!?…]["”’)]*$/.test(w),pause=!stop&&CC_END.test(w);
    // a full chunk waits up to two more words for a clause end, so no word is left on its own
    const soon=[1,2].some(k=>words[i+k]&&CC_END.test(words[i+k])&&cur.length+k<=6);
    if(stop||pause||((cur.length>=4||len>=26)&&!soon)){out.push({t:cur.join(' '),i0:first,ws:cur,w:len+(stop?9:pause?5:0)});cur=[];len=0}});
  if(cur.length)out.push({t:cur.join(' '),i0:first,ws:cur,w:len});
  const total=out.reduce((a,c)=>a+c.w,0)||1;let acc=0;out.forEach(c=>{c.at=acc/total;acc+=c.w;
    const n=c.ws.reduce((a,x)=>a+x.length+1,0)||1;let k=0;c.f=c.ws.map(x=>{const f=k/n;k+=x.length+1;return f})});  // each word's share of its phrase
  return out}
// Each script word's start in seconds from the voice's timed tokens ([word, start, end]), or null.
// The voice gives punctuation its own tokens and may split a word in two, so a word matches the run
// of tokens that spells it; one it spelled differently gets a time between its neighbours.
const ccNorm=x=>String(x).toLowerCase().replace(/[^a-z0-9]+/g,'');
// A match more than a few tokens ahead (after the voice read a link or path out piece by piece)
// counts only when the next word follows it too, so a common word can't pull the timing ahead.
function ccAlign(words,timed){const tn=timed.map(x=>ccNorm(x[0])),out=words.map(()=>null),ns=words.map(ccNorm);let k=0;
  const spell=(s,j)=>{if(!tn[j]||!s.startsWith(tn[j]))return -1;let acc='',e=j;
    while(e<tn.length&&acc.length<s.length){if(tn[e]){if(!s.startsWith(acc+tn[e]))break;acc+=tn[e]}e++}
    return acc===s?e:-1};
  const next=(i,e)=>{let n=i+1;while(n<ns.length&&!ns[n])n++;if(n>=ns.length)return true;
    while(e<tn.length&&!tn[e])e++;return spell(ns[n],e)>=0};
  ns.forEach((s,i)=>{if(!s)return;
    for(let j=k;j<Math.min(tn.length,k+120);j++){const e=spell(s,j);
      if(e>=0&&(j-k<16||next(i,e))){out[i]=Math.max(0,+timed[j][1]||0);k=e;break}}});
  for(let i=0;i<out.length;i++){if(out[i]!=null)continue;let a=i-1,b=i;while(b<out.length&&out[b]==null)b++;
    if(a>=0&&b<out.length)for(let m=i;m<b;m++)out[m]=out[a]+(out[b]-out[a])*(m-a)/(b-a);i=b}
  return out}
// With the voice's word timings, each phrase starts when its first word is spoken; a reply can
// also carry a visual plan: beats (chips, code, flow, compare, list, callout) shown on their phrase.
async function ccLoad(file,force){if(!force&&ccScripts.has(file))return ccScripts.get(file);
  let j={};try{const r=await fetch('/api/script?file='+encodeURIComponent(file));if(r.ok)j=await r.json()}catch(_){}
  if(!j.text)return null;const chunks=ccSplit(j.text),w=Array.isArray(j.words)?j.words:null;
  if(w&&w.length){const ts=ccAlign(j.text.split(/\s+/).filter(Boolean),w);
    chunks.forEach(c=>{c.ts=c.ws.map((_,m)=>ts[c.i0+m]);if(c.ts[0]!=null)c.t0=c.ts[0]})}  // a phrase whose first word wasn't found keeps its length-based share
  const beats=(Array.isArray(j.visuals)?j.visuals:[]).filter(b=>b&&CC_KINDS.includes(b.kind));
  // a reply that has just turned ready gets its plan a little later: until then it is not cached
  const data={chunks,beats,pending:!!(w&&w.length)&&!Array.isArray(j.visuals),asked:Date.now(),tries:0};
  if(!data.pending)ccScripts.set(file,data);return data}
function ccRefresh(){const c=ccChunks,file=ccFile;if(c.busy||c.tries>=80||Date.now()-c.asked<3000)return;c.busy=true;c.tries++;
  ccLoad(file,true).then(n=>{c.busy=false;c.asked=Date.now();if(n&&!n.pending&&ccChunks===c&&ccFile===file){ccChunks=n;ccBeat=-1;ccShow()}})}
const CC_KINDS=['chips','code','flow','compare','list','callout'];
function ccEl(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=String(text);return e}
const ccArr=(v,n)=>(Array.isArray(v)?v:[]).slice(0,n);
function ccVisual(b){const box=ccEl('div','ccv k-'+b.kind),d=b.data||{};let i=0;
  const add=n=>{n.style.setProperty('--i',i++);box.append(n);return n};
  if(b.kind==='chips')ccArr(d.items,5).forEach(x=>add(ccEl('span','chip',x)));
  else if(b.kind==='code'){const hl=ccArr(d.highlight,8);ccArr(d.lines,8).forEach((x,k)=>add(ccEl('div','ln'+(hl.includes(k)?' hl':''),x)))}
  else if(b.kind==='flow')ccArr(d.steps,5).forEach(x=>add(ccEl('span','step',x)));
  else if(b.kind==='compare')[d.left,d.right].forEach(c=>{c=c||{};const col=add(ccEl('div','col'));col.append(ccEl('div','ct',c.title||''));ccArr(c.items,3).forEach(x=>col.append(ccEl('div','ci',x)))});
  else if(b.kind==='list')ccArr(d.items,4).forEach(x=>add(ccEl('div','li',x)));
  else{if(d.value)add(ccEl('div','val',d.value));add(ccEl('div','txt',d.text||''))}
  return box}
let ccBeat=-1,ccClk={ct:-1,pt:0,run:false};
// currentTime moves in steps on some browsers; between steps the clock runs on from the last one
// (from the first playing frame, so a resume after a pause doesn't jump ahead)
function ccNow(){const ct=audio.currentTime,p=performance.now();
  if(audio.paused||!ccClk.run||ct!==ccClk.ct){ccClk={ct,pt:p,run:!audio.paused};return ct}
  return ct+Math.min(.25,(p-ccClk.pt)/1000*(audio.playbackRate||1))}
function ccRender(c){const t=$('#cc .cct');t.replaceChildren(ccEl('span','ccpill'));
  c.ws.forEach((x,m)=>{if(m)t.append(' ');t.append(ccEl('span','ccw',x))});
  t.classList.remove('in');void t.offsetWidth;t.classList.add('in')}
// Lights the spoken word: words before it are read, words after it still to come; a soft marker
// glides from word to word. Only colour and the marker change, so the line itself never moves.
function ccMark(){const t=$('#cc .cct'),ws=t.querySelectorAll('.ccw'),pill=t.querySelector('.ccpill');
  ws.forEach((e,m)=>{e.classList.toggle('past',m<ccWi);e.classList.toggle('on',m===ccWi)});
  if(!pill)return;const e=ws[ccWi];if(!e){pill.style.opacity='0';return}
  const p=parseFloat(getComputedStyle(t).fontSize)*.12,first=pill.style.opacity!=='1';
  if(first)pill.classList.add('jump');  // a new phrase: the marker appears in place rather than sliding there
  pill.style.width=(e.offsetWidth+2*p)+'px';pill.style.height=e.offsetHeight+'px';
  pill.style.transform=`translate(${e.offsetLeft-p}px,${e.offsetTop}px)`;pill.style.opacity='1';
  if(first){void pill.offsetWidth;pill.classList.remove('jump')}}
// A visual taller than the room under the header shrinks to fit rather than hide its top.
function ccFit(){const vs=$('#cc .ccvs'),v=vs.firstElementChild;if(!v)return;v.style.transform='';
  const k=Math.min(1,vs.clientHeight/(v.offsetHeight||1),vs.clientWidth/(v.offsetWidth||1));if(k<1)v.style.transform=`scale(${k})`}
function ccLayout(){const h=document.querySelector('header');$('#cc').style.setProperty('--cchead',(h?Math.round(h.getBoundingClientRect().height):0)+'px');ccFit();if(ccWi>=0)ccMark()}
function ccShow(){const box=$('#cc'),on=!!(ccOn&&cur&&cur.file&&ccChunks&&ccFile===cur.file&&!audio.ended);box.hidden=!on;
  cancelAnimationFrame(ccRaf);ccRaf=0;if(!on)return;
  // until the clip's length is known, a chunk or beat timed by its share waits, unless it opens the clip
  // CC_LEAD is real seconds: the word lights a beat before it is heard, so its quick fade and the marker's
  // glide finish as it is said rather than after. Scaled by speed, so 2x is just as far ahead to the eye.
  const d=dur(),now=ccNow()+CC_LEAD*(audio.playbackRate||1),ch=ccChunks.chunks,share=x=>d?x*d:x?Infinity:0;
  const at=c=>c.t0!=null?c.t0:share(c.at);let i=0;while(i+1<ch.length&&at(ch[i+1])<=now)i++;
  const bs=ccChunks.beats,bt=b=>b.t!=null?b.t:share(b.f||0);let bi=-1;
  bs.forEach((b,k)=>{if(bt(b)<=now&&(bi<0||bt(b)>=bt(bs[bi])))bi=k});  // the latest beat to have started
  if(ccChunks.pending)ccRefresh();
  box.classList.toggle('withv',bi>=0);
  if(bi!==ccBeat){ccBeat=bi;const vs=box.querySelector('.ccvs');vs.replaceChildren(...(bi>=0?[ccVisual(ccChunks.beats[bi])]:[]));ccFit()}
  if(i!==ccIdx){ccIdx=i;ccWi=-2;ccRender(ch[i])}
  // the word being spoken: the last one whose start has passed (its own time, or its share of the phrase)
  const c=ch[i],s0=at(c);let s1=i+1<ch.length?at(ch[i+1]):d;if(!isFinite(s1)||!(s1>s0))s1=s0+c.ws.length*.35;
  const wt=m=>c.ts&&c.ts[m]!=null?c.ts[m]:s0+(s1-s0)*c.f[m];
  let wi=-1;for(let m=0;m<c.ws.length&&wt(m)<=now;m++)wi=m;
  if(wi!==ccWi){ccWi=wi;ccMark()}
  box.classList.toggle('paused',audio.paused);if(!audio.paused)ccRaf=requestAnimationFrame(ccShow)}
async function ccSync(){if(ccOn)ccLayout();if(ccOn&&cur&&cur.file&&ccFile!==cur.file){const file=cur.file;ccFile=file;ccIdx=-1;ccBeat=-1;ccChunks=null;
    const ch=await ccLoad(file);if(!cur||cur.file!==file)return;ccChunks=ch}
  ccShow()}
function ccSet(on){ccOn=on;try{localStorage.setItem('replies-cc',on?'1':'0')}catch(_){}
  const b=$('#ccb');b.setAttribute('aria-pressed',String(on));b.classList.toggle('fast',on);ccFile='';ccIdx=-1;ccBeat=-1;ccSync()}
$('#ccb').addEventListener('click',()=>ccSet(!ccOn));
['play','playing','pause','seeked','ended','emptied'].forEach(n=>audio.addEventListener(n,ccSync));
// caption size: A− and A+ at the foot of the captions step through a few sizes, kept per browser
let ccZ=CC_SIZES.indexOf(1);try{const v=localStorage.getItem('replies-cc-size');if(v!==null&&CC_SIZES[+v])ccZ=+v}catch(_){}
function ccSize(step){ccZ=Math.max(0,Math.min(CC_SIZES.length-1,ccZ+step));try{localStorage.setItem('replies-cc-size',String(ccZ))}catch(_){}
  $('#cc').style.setProperty('--ccz',CC_SIZES[ccZ]);$('#ccm').disabled=ccZ===0;$('#ccp').disabled=ccZ===CC_SIZES.length-1;ccFit();if(ccWi>=0)ccMark()}
$('#ccm').addEventListener('click',()=>ccSize(-1));$('#ccp').addEventListener('click',()=>ccSize(1));
window.addEventListener('resize',()=>{if(ccOn)ccLayout()});
ccSize(0);ccSet(ccOn);
audio.addEventListener('ended',()=>{if(cur&&(heard[cur.file]||0)>=.9)markHeard(1,true);if(queue.length){const n=queue.shift();cur=null;play(n.key,n.file,n.label)}else syncButtons()});
// Auto: a reply that first shows up after the page loaded plays by itself once its audio is ready,
// oldest first, and only when nothing else has the speaker: not over a clip that is playing or paused
// part-way, a "Play all" queue, a tapped clip still loading, or the mic. Replies already heard (on any
// device) or older than AUTO_FRESH by then are dropped. On or off per browser, like the speed.
const AUTO_FRESH=1800;
function autoNote(entries,known,wait,on){  // known: ids seen so far; null before the first feed, which only sets it
  const first=!known;known=known||new Set();
  for(const e of entries){if(e.kind==='questions'||known.has(e.id))continue;known.add(e.id);if(on&&!first&&!e.legacy)wait.add(e.id)}
  return known}
function autoNext(entries,wait,heard,now,busy){  // the reply to start now, or null
  let pick=null;
  for(const id of [...wait]){const e=entries.find(x=>x.id===id),ready=!!e&&e.status==='ready';
    if(!e||e.status==='failed'||ready&&!e.audio||now-(e.created||0)>AUTO_FRESH||ready&&(heard[e.audio]||0)>=.9){wait.delete(id);continue}
    if(ready&&(!pick||(e.created||0)<(pick.created||0)))pick=e}
  return busy?null:pick}
let autoOn=false,autoKnown=null,autoBlocked=false,autoLetGo='';const autoWait=new Set();
try{autoOn=localStorage.getItem('replies-auto')==='1'}catch(_){}
const autoBusy=()=>!!hold||!!wantPlay||playLoading>0||queue.length>0||!!cur&&!audio.ended&&(!audio.paused||audio.currentTime>0&&cur.key!==autoLetGo);
// Talking back pauses the reply (the mic does it); once the answer is sent that pause stops holding
// Auto, so the chat's next reply plays. Playing that clip again puts it back under the usual rule.
function autoAnswered(){if(cur&&audio.paused)autoLetGo=cur.key;setTimeout(autoPump)}
function autoPump(){if(!autoOn||autoBlocked)return;const e=autoNext(data.entries,autoWait,heard,Date.now()/1000,autoBusy());if(!e)return;
  if(!clipUrl(e.audio)){fetchClip(e.audio,true).then(u=>{if(u)autoPump()});return}
  autoWait.delete(e.id);cur=null;
  // A browser that hasn't been tapped since the page loaded refuses to start sound: the reply stays
  // loaded in the player and waiting, and the next tap anywhere lets Auto carry on.
  play(e.id,e.audio,e.title||'Reply').catch(err=>{if(!err||err.name!=='NotAllowedError')return;autoWait.add(e.id);autoBlocked=true;paintAuto()})}
function paintAuto(){const b=$('#aut'),wait=autoOn&&autoBlocked;b.setAttribute('aria-pressed',String(autoOn));b.classList.toggle('blocked',wait);
  b.title=wait?'Auto-play is waiting: tap the page once so this browser lets replies play':autoOn?'Auto-play is on: new replies play as they arrive':'Auto-play new replies as they arrive'}
$('#aut').addEventListener('click',()=>{autoOn=!autoOn;autoBlocked=false;try{localStorage.setItem('replies-auto',autoOn?'1':'0')}catch(_){}
  if(autoOn)unlockAudio();else autoWait.clear();paintAuto();autoPump()});
document.addEventListener('pointerdown',()=>{if(!autoBlocked)return;autoBlocked=false;paintAuto();setTimeout(autoPump,300)},true);  // after the tap's own click
audio.addEventListener('play',()=>{autoLetGo='';if(cur)autoWait.delete(cur.key);if(autoBlocked){autoBlocked=false;paintAuto()}});
audio.addEventListener('ended',()=>setTimeout(autoPump,400));
paintAuto();
window.addEventListener('pagehide',()=>saveHeard(true,true));
// A phone switching apps often never fires pagehide, and a tab coming back shouldn't wait for the timer.
document.addEventListener('visibilitychange',()=>{if(document.hidden)saveHeard(true,true);else syncHeard()});
const post=(path,body,headers)=>fetch(path,{method:'POST',headers:{'X-Replies':'1',...headers},body});
async function reason(r,fallback){try{return(await r.json()).error||fallback}catch(_){return fallback}}
// Background music, made live with Web Audio (no files, no network): 432 Hz calm, a slow song tuned
// to A = 432; 40 Hz focus, a steady arpeggio song pulsing at 40 Hz (works on speakers) over a quiet
// binaural 40 Hz beat for headphones; and Quest, an original fantasy loop in D Dorian. All three are
// generated, so they never repeat exactly. It ducks under a reply and goes silent while the mic is held.
const music=(()=>{let ctx=null,out=null,bus=null,stops=[],timer=0,track='off',vol=.4;const duck=new Set();
  try{const m=JSON.parse(localStorage.getItem('replies.music.v1')||'{}');if(typeof m.vol==='number')vol=m.vol}catch(_){}
  const level=()=>track==='off'?0:vol*vol*.35*(duck.has('rec')?0:duck.has('voice')?.3:1);
  const apply=()=>{if(out)out.gain.setTargetAtTime(level(),ctx.currentTime,.3)};
  const hz=n=>440*Math.pow(2,(n-69)/12);
  function init(){if(!ctx){const AC=window.AudioContext||window.webkitAudioContext;ctx=new AC();out=ctx.createGain();out.gain.value=0;out.connect(ctx.destination)}
    ctx.resume().catch(()=>{})}
  function osc(type,f,dest,detune){const o=ctx.createOscillator();o.type=type;o.frequency.value=f;o.detune.value=detune||0;o.connect(dest);o.start();stops.push(()=>o.stop());return o}
  function lfo(rate,depth,param){const o=ctx.createOscillator(),g=ctx.createGain();o.frequency.value=rate;g.gain.value=depth;o.connect(g);g.connect(param);o.start();stops.push(()=>o.stop())}
  function noise(dest,gain,cut){const len=ctx.sampleRate*3,buf=ctx.createBuffer(1,len,ctx.sampleRate),d=buf.getChannelData(0);let last=0;
    for(let i=0;i<len;i++){last=(last+.02*(Math.random()*2-1))/1.02;d[i]=last*3.5}  // brown noise: soft, low rumble
    const src=ctx.createBufferSource(),lp=ctx.createBiquadFilter(),g=ctx.createGain();src.buffer=buf;src.loop=true;lp.type='lowpass';lp.frequency.value=cut;g.gain.value=gain;
    src.connect(lp);lp.connect(g);g.connect(dest);src.start();stops.push(()=>src.stop())}
  function pluck(type,f,t,dur,peak,dest,attack){const o=ctx.createOscillator(),g=ctx.createGain();o.type=type;o.frequency.value=f;
    g.gain.setValueAtTime(0,t);g.gain.linearRampToValueAtTime(peak,t+(attack||.008));g.gain.exponentialRampToValueAtTime(.0001,t+dur);
    o.connect(g);g.connect(dest);o.start(t);o.stop(t+dur+.05)}
  // A soft chord tone: swells in, holds, fades out, so consecutive chords crossfade.
  function pad(f,t,dur,peak,dest){const g=ctx.createGain();g.gain.setValueAtTime(0,t);g.gain.linearRampToValueAtTime(peak,t+dur*.3);
    g.gain.setValueAtTime(peak,t+dur*.65);g.gain.linearRampToValueAtTime(0,t+dur);g.connect(dest);
    [['sine',-4],['triangle',5]].forEach(([ty,d])=>{const o=ctx.createOscillator();o.type=ty;o.frequency.value=f;o.detune.value=d;o.connect(g);o.start(t);o.stop(t+dur+.05)})}
  function echo(src,dest,time,feedback,level){const dl=ctx.createDelay(2),fb=ctx.createGain(),wet=ctx.createGain();
    dl.delayTime.value=time;fb.gain.value=feedback;wet.gain.value=level;src.connect(dl);dl.connect(fb);fb.connect(dl);dl.connect(wet);wet.connect(dest)}
  const walk=(i,max,steps)=>Math.max(0,Math.min(max,i+steps[Math.floor(Math.random()*steps.length)]));
  const TRACKS={
    '432'(b){const tune=n=>432*Math.pow(2,(n-69)/12);  // every note tuned to A = 432 Hz
      const mix=ctx.createGain(),lp=ctx.createBiquadFilter();mix.gain.value=2;lp.type='lowpass';lp.frequency.value=2200;mix.connect(lp);lp.connect(b);
      echo(lp,b,.75,.38,.32);lfo(.04,600,lp.frequency);noise(b,.03,400);
      // A  F#m  D  E at 60 bpm, two bars each; melody on A major pentatonic, sparse and bell-like
      const chords=[[57,64,68,73],[54,61,64,69],[50,57,62,66],[52,59,64,68]],scale=[64,66,69,71,73,76,78,81,83,85],beat=1;
      let next=ctx.currentTime+.2,step=0,mel=4;
      const tick=()=>{while(next<ctx.currentTime+.6){const bar=Math.floor(step/4),ch=chords[Math.floor(bar/2)%4],e=step%4,t=next;
          if(e===0&&bar%2===0){ch.forEach(n=>pad(tune(n),t,beat*8.6,.045,mix));pluck('sine',tune(ch[0]-12),t,beat*7.5,.14,mix,.6)}
          if(Math.random()<(e===0?.7:e===2?.45:.2)){mel=walk(mel,scale.length-1,[-2,-1,-1,0,1,1,2]);
            pluck('sine',tune(scale[mel]),t+(e%2?beat*.5:0),beat*3,.06,mix,.015);pluck('sine',tune(scale[mel])*2,t,beat*1.2,.012,mix,.015)}
          next+=beat;step++}};
      tick();timer=setInterval(tick,100)},
    '40'(b){// the whole song pulses at 40 Hz, which works on speakers; under it a quiet binaural 40 Hz
      // beat (200 Hz left, 240 Hz right) adds the effect in headphones
      const pulse=ctx.createGain(),merge=ctx.createChannelMerger(2);pulse.gain.value=.75;pulse.connect(b);lfo(40,.3,pulse.gain);merge.connect(pulse);
      [[200,0],[240,1]].forEach(([f,ch])=>{const g=ctx.createGain();g.gain.value=.12;g.connect(merge,0,ch);osc('sine',f,g)});
      noise(b,.05,350);
      const mix=ctx.createGain();mix.gain.value=4;mix.connect(pulse);echo(mix,pulse,.47,.25,.22);
      // Dm9  Bbmaj7  F  C at 96 bpm, a bar each, eighth-note arpeggio: steady enough to work to
      const chords=[[50,57,60,64,65],[46,53,57,60,62],[41,48,53,57,60],[48,55,59,62,64]],order=[1,2,3,4,3,2,4,3],beat=60/96;
      let next=ctx.currentTime+.15,step=0,mel=2;
      const tick=()=>{while(next<ctx.currentTime+.5){const ch=chords[Math.floor(step/8)%4],e=step%8,t=next;
          if(e===0){ch.slice(1,4).forEach(n=>pad(hz(n),t,beat*4.4,.035,mix));pluck('triangle',hz(ch[0]-12),t,beat*3.6,.16,mix,.01)}
          pluck('triangle',hz(ch[order[e]]+12),t,beat*.8,.045,mix,.006);
          if(e===4&&Math.random()<.5){mel=walk(mel,ch.length-1,[-1,0,1]);pluck('sine',hz(ch[mel]+24),t,beat*2.5,.035,mix,.03)}
          next+=beat/2;step++}};
      tick();timer=setInterval(tick,60)},
    quest(b){const mix=ctx.createGain(),dl=ctx.createDelay(1),fb=ctx.createGain(),wet=ctx.createGain();
      dl.delayTime.value=.33;fb.gain.value=.3;wet.gain.value=.28;mix.gain.value=4;mix.connect(b);  // plucks are sparse: lift to sit level with the pads
      mix.connect(dl);dl.connect(fb);fb.connect(dl);dl.connect(wet);wet.connect(b);
      const chords=[[50,53,57],[48,52,55],[46,50,53],[45,48,52]],scale=[62,64,65,67,69,71,72,74,76,77],beat=60/88;  // Dm C Bb Am, D Dorian
      let next=ctx.currentTime+.15,step=0,mel=4;
      const tick=()=>{while(next<ctx.currentTime+.5){const ch=chords[Math.floor(step/8)%4],e=step%8,t=next;
          if(e===0)pluck('triangle',hz(ch[0]-12),t,beat*3.6,.2,mix);
          pluck('triangle',hz(ch[[0,1,2,1][e%4]]+12),t,beat*.9,.06,mix);
          if(e%2===0&&Math.random()<.6){mel=Math.max(0,Math.min(scale.length-1,mel+[-2,-1,-1,0,1,1,2][Math.floor(Math.random()*7)]));
            if(e===0&&!ch.some(c=>(c-scale[mel])%12===0))mel=Math.max(0,mel-1);  // land the bar's first note near the chord
            pluck('sine',hz(scale[mel]),t,beat*(Math.random()<.3?1.9:.95),.09,mix,.05);pluck('triangle',hz(scale[mel]),t,beat*.6,.03,mix,.05)}
          next+=beat/2;step++}};
      tick();timer=setInterval(tick,60)}};
  function stop(){clearInterval(timer);timer=0;const fns=stops,b=bus;stops=[];bus=null;
    if(b){b.gain.setTargetAtTime(0,ctx.currentTime,.12);setTimeout(()=>{fns.forEach(f=>{try{f()}catch(_){}});b.disconnect()},800)}}
  return{get track(){return track},get vol(){return vol},
    play(t){if(t===track)return;stop();track=t;if(t!=='off'){init();bus=ctx.createGain();bus.gain.value=1;bus.connect(out);TRACKS[t](bus)}apply()},
    volume(v){vol=v;apply();try{localStorage.setItem('replies.music.v1',JSON.stringify({vol}))}catch(_){}},
    duck(k,on){if(on)duck.add(k);else duck.delete(k);apply()}}})();
let vStyle='standard';
const showStyle=()=>document.querySelectorAll('.mt[data-style]').forEach(b=>b.setAttribute('aria-checked',String(b.dataset.style===vStyle)));
async function loadStyle(){try{const r=await fetch('/api/style',{cache:'no-store'});if(r.ok){vStyle=(await r.json()).style||'standard';showStyle()}}catch(_){}}
function showMusic(){document.querySelectorAll('.mt[data-track]').forEach(b=>b.setAttribute('aria-checked',String(b.dataset.track===music.track)));
  $('#mus').classList.toggle('on',music.track!=='off');$('#mvol').value=Math.round(music.vol*100)}
function musicPanel(open){$('#mpanel').hidden=!open;$('#mus').setAttribute('aria-expanded',String(open));if(open){showMusic();loadStyle()}}
$('#mus').addEventListener('click',ev=>{ev.stopPropagation();musicPanel($('#mpanel').hidden)});
$('#mpanel').addEventListener('click',ev=>{ev.stopPropagation();const b=ev.target.closest('.mt');if(!b)return;
  if(b.dataset.style){const was=vStyle;vStyle=b.dataset.style;showStyle();
    post('/api/style',JSON.stringify({style:vStyle}),{'Content-Type':'application/json'}).then(r=>{if(!r.ok){vStyle=was;showStyle()}}).catch(()=>{vStyle=was;showStyle()});return}
  music.play(b.dataset.track);showMusic()});
$('#mvol').addEventListener('input',ev=>music.volume(ev.target.value/100));
document.addEventListener('click',ev=>{if(!$('#mpanel').hidden&&!ev.target.closest('#mpanel,#mus'))musicPanel(false)});
document.addEventListener('keydown',ev=>{if(ev.key==='Escape'&&!$('#mpanel').hidden){musicPanel(false);$('#mus').focus()}});
audio.addEventListener('play',()=>music.duck('voice',true));
for(const t of['pause','ended','emptied'])audio.addEventListener(t,()=>music.duck('voice',!audio.paused));
const stext=$('#stext'),sheet=$('#sheet'),mic=$('#mic');let target=null,opener=null,drafts={},sending=false;
let hold=null,warm=null,warmTimer=0,recRun=0,transcribing=false;
function sheetError(m){const s=$('#serr');s.textContent=m||'';s.hidden=!m;if(m)buzz('bad')}
function setMic(state,text){music.duck('rec',state==='rec');mic.innerHTML=MIC;mic.classList.toggle('rec',state==='rec');if(state!=='rec')mic.style.removeProperty('--lvl');
  mic.disabled=state==='busy';mic.title=state==='rec'?'Tap to stop':'Tap to talk';mic.setAttribute('aria-label',state==='rec'?'Recording. Tap to stop':'Tap to talk');mic.setAttribute('aria-pressed',String(state==='rec'));
  $('#mstat').textContent=text||'';$('#wave').hidden=state!=='rec'}
function fit(){stext.style.height='auto';stext.style.height=Math.max(docked()?44:76,stext.scrollHeight+2)+'px';
  $('#ssend').disabled=!stext.value.trim()||sending||!!hold||transcribing}
function openSheet(e,btn){target=e;opener=btn;const c=data.chats.find(x=>x.id===e.chat),name=c?c.title:'this chat';
  sheet.setAttribute('aria-modal','true');$('#swho').textContent=e.newChat?'Starts a new Claude Code chat':c&&c.resume?'Sending reopens '+name:'Replying in '+name;
  $('#stitle').textContent=e.title||(e.questions?e.questions.length+' decisions':'Reply');
  stext.value=drafts[e.id]||'';sheetError('');setMic('idle');$('#scrim').hidden=false;sheet.hidden=false;fit();
  if(matchMedia('(hover:hover)').matches)stext.focus()}
function closeSheet(){holdEnd(false);recRun++;transcribing=false;micDown();
  if(target)drafts[target.id]=stext.value;target=null;$('#scrim').hidden=true;sheet.hidden=true;sheet.classList.remove('dock');stext.rows=3;
  if(opener&&opener.isConnected)opener.focus();opener=null}
function keyboard(){const v=window.visualViewport;if(!v)return;const kb=Math.max(0,Math.round(innerHeight-v.height-v.offsetTop));
  const opened=kb>0&&!sheet.classList.contains('kb');
  sheet.style.setProperty('--kb',kb+'px');sheet.classList.toggle('kb',kb>0);
  document.documentElement.style.setProperty('--kbh',kb+'px');
  if(opened&&docked())requestAnimationFrame(()=>scrollTo(0,document.documentElement.scrollHeight))}
if(window.visualViewport){visualViewport.addEventListener('resize',keyboard);visualViewport.addEventListener('scroll',keyboard)}
new ResizeObserver(()=>{const el=document.documentElement,near=innerHeight+scrollY>=el.scrollHeight-160;
  el.style.setProperty('--dockh',sheet.offsetHeight+'px');if(near&&view)scrollTo(0,el.scrollHeight)}).observe(sheet);  // the room under the messages is only known once the composer is laid out
// Tap to talk: tap the mic to start, tap again to stop; another recording adds more. Raw PCM is taken
// from Web Audio (the same path on iOS Safari and desktop) and sent as WAV to this Mac's Whisper
// about every 1.2s while recording, so the words fill in live; one full pass runs on stop. No browser
// speech service is used: Chrome's and Safari's send audio off the machine, and replies can carry private data.
const STT_RATE=16000,HOLD_MAX=120,MIC_IDLE=20000;
async function micUp(ctx){if(warm)return warm;
  const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
  const src=ctx.createMediaStreamSource(stream),an=ctx.createAnalyser(),proc=ctx.createScriptProcessor(4096,1,1),mute=ctx.createGain();
  an.fftSize=1024;mute.gain.value=0;
  proc.onaudioprocess=ev=>{const h=hold;if(h&&h.on){const d=ev.inputBuffer.getChannelData(0);h.pcm.push(new Float32Array(d));h.n+=d.length}};
  src.connect(an);src.connect(proc);proc.connect(mute);mute.connect(ctx.destination);  // a processor only runs when it reaches the output
  warm={stream,ctx,an,rate:ctx.sampleRate};return warm}
// The mic stays open briefly after a stop so the next recording starts instantly, then it is let go.
function micDown(){clearTimeout(warmTimer);if(!warm)return;warm.stream.getTracks().forEach(t=>t.stop());warm.ctx.close().catch(()=>{});warm=null}
function armIdle(){clearTimeout(warmTimer);warmTimer=setTimeout(()=>{if(!hold)micDown()},MIC_IDLE)}
function toWav(chunks,rate){let n=0;for(const c of chunks)n+=c.length;const all=new Float32Array(n);let o=0;for(const c of chunks){all.set(c,o);o+=c.length}
  const ratio=rate/STT_RATE,len=Math.floor(n/ratio),buf=new ArrayBuffer(44+len*2),v=new DataView(buf);
  const str=(p,s)=>{for(let i=0;i<s.length;i++)v.setUint8(p+i,s.charCodeAt(i))};
  str(0,'RIFF');v.setUint32(4,36+len*2,true);str(8,'WAVE');str(12,'fmt ');v.setUint32(16,16,true);v.setUint16(20,1,true);v.setUint16(22,1,true);
  v.setUint32(24,STT_RATE,true);v.setUint32(28,STT_RATE*2,true);v.setUint16(32,2,true);v.setUint16(34,16,true);str(36,'data');v.setUint32(40,len*2,true);
  for(let i=0;i<len;i++){const a=Math.floor(i*ratio),b=Math.max(a+1,Math.floor((i+1)*ratio));let s=0;  // box-average down to 16 kHz
    for(let j=a;j<b;j++)s+=all[j];v.setInt16(44+i*2,Math.max(-1,Math.min(1,s/(b-a)))*32767,true)}
  return new Blob([buf],{type:'audio/wav'})}
async function sttPass(h){
  try{const r=await post('/api/stt',toWav(h.pcm,h.rate),{'Content-Type':'audio/wav'});
    if(!r.ok)return{err:await reason(r,'Couldn’t transcribe that. Try again or type it.')};
    return{text:((await r.json()).text||'').trim()}}
  catch(_){return{err:'Couldn’t reach your Mac. Try again or type it.'}}}
function showLive(h,t){stext.value=h.base+(h.base&&t?' ':'')+t;fit();stext.scrollTop=stext.scrollHeight;if(target)drafts[target.id]=stext.value}
async function holdStart(){if(hold||transcribing||!target)return;sheetError('');
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){sheetError('This browser can’t record here. Open the page over https or localhost.');return}
  const AC=window.AudioContext||window.webkitAudioContext,ctx=warm?warm.ctx:new AC();
  ctx.resume().catch(()=>{});  // iOS only lets audio start inside the press itself
  audio.pause();clearTimeout(warmTimer);
  const h=hold={on:false,up:false,pcm:[],n:0,rate:0,base:stext.value.trimEnd(),run:++recRun,last:0,text:'',t0:0};
  setMic('rec','');
  try{await micUp(ctx)}
  catch(_){if(!warm)ctx.close().catch(()=>{});if(hold===h)hold=null;setMic('idle');
    sheetError('Microphone access is off for this page. Allow it in the browser’s settings.');return}
  if(hold!==h){if(!hold)armIdle();return}  // the sheet closed while the mic was opening: let it go
  ctx.resume().catch(()=>{});
  if(h.up){hold=null;setMic('idle','Mic’s ready. Tap it to talk.');armIdle();return}  // stopped during the permission prompt
  h.rate=warm.rate;h.on=true;h.t0=performance.now();fit();drawWave(h);liveLoop(h)}
function holdEnd(keep){const h=hold;if(!h||h.up)return;h.up=true;
  if(!h.on){if(!keep){hold=null;setMic('idle')}return}  // still waiting on the mic; holdStart sees h.up
  h.on=false;hold=null;armIdle();
  if(!keep){stext.value=h.base;setMic('idle');fit();return}
  if(h.n/h.rate<0.4&&!h.text){stext.value=h.base;setMic('idle','Tap the mic, talk, then tap it again.');fit();return}
  finalize(h)}
async function liveLoop(h){while(hold===h&&h.on){
  const secs=h.n/h.rate;if(secs>=HOLD_MAX){holdEnd(true);return}
  const due=1200-(performance.now()-h.last);if(due>0||secs<0.8){await wait(Math.max(150,due));continue}
  h.last=performance.now();const res=await sttPass(h);
  if(hold===h&&h.on&&res.text){h.text=res.text;showLive(h,res.text)}}}
async function finalize(h){transcribing=true;setMic('busy','Finishing on your Mac…');fit();
  const res=await sttPass(h);if(h.run!==recRun)return;transcribing=false;
  const t=res.text||h.text;  // a failed or empty last pass keeps what the live passes caught
  if(t){showLive(h,t);setMic('idle');if(res.text)buzz('ok')}else{stext.value=h.base;setMic('idle')}
  if(!t)sheetError(res.err||'Didn’t catch anything. Try again.');fit()}
// The waveform is the real mic level: RMS of each analyser frame in dB, mapped -55..-12 dB to 0..1,
// one bar per 45 ms scrolling left, and the same level drives the ring around the mic.
function drawWave(h){const cv=$('#wave'),g=cv.getContext('2d'),an=warm.an,buf=new Float32Array(an.fftSize),bars=[];
  const col=getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()||'#1b6f80';let peak=0,lastBar=performance.now();
  const frame=now=>{if(hold!==h||!h.on||!warm)return;
    an.getFloatTimeDomainData(buf);let s=0;for(let i=0;i<buf.length;i++)s+=buf[i]*buf[i];
    const db=20*Math.log10(Math.sqrt(s/buf.length)+1e-9),lv=Math.max(0,Math.min(1,(db+55)/43));
    peak=Math.max(peak,lv);mic.style.setProperty('--lvl',lv.toFixed(3));
    if(now-lastBar>=45){bars.push(peak);peak=0;lastBar=now;if(bars.length>400)bars.shift()}
    const dpr=devicePixelRatio||1,w=cv.clientWidth,ht=cv.clientHeight;
    if(cv.width!==Math.round(w*dpr)||cv.height!==Math.round(ht*dpr)){cv.width=Math.round(w*dpr);cv.height=Math.round(ht*dpr)}
    g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,w,ht);g.fillStyle=col;
    const step=5,bw=3,fit_=Math.floor(w/step),from=Math.max(0,bars.length-fit_);
    for(let i=from;i<bars.length;i++){const bh=Math.max(2,bars[i]*(ht-2)),x=w-(bars.length-i)*step;
      g.globalAlpha=.35+.65*bars[i];g.beginPath();g.roundRect?g.roundRect(x,(ht-bh)/2,bw,bh,1.5):g.rect(x,(ht-bh)/2,bw,bh);g.fill()}
    g.globalAlpha=1;$('#mstat').textContent=fmt((now-h.t0)/1000)+' · tap to stop';
    requestAnimationFrame(frame)};
  requestAnimationFrame(frame)}
mic.innerHTML=MIC;
// A click is a tap, Enter or Space; it runs inside the gesture, which iOS needs to start audio.
mic.addEventListener('click',ev=>{ev.preventDefault();if(mic.disabled)return;if(hold)holdEnd(true);else holdStart()});
// Keep focus in the reply box so a phone keyboard stays up and Enter still sends. mousedown, not
// pointerdown: cancelling pointerdown stops WebKit from firing the click on a touch.
mic.addEventListener('mousedown',ev=>ev.preventDefault());
mic.addEventListener('keydown',ev=>{if(ev.repeat&&(ev.key==='Enter'||ev.key===' '))ev.preventDefault()});  // a held key toggles once
mic.addEventListener('contextmenu',ev=>ev.preventDefault());
document.addEventListener('visibilitychange',()=>{if(document.hidden){holdEnd(true);micDown()}});
stext.addEventListener('input',()=>{if(target)drafts[target.id]=stext.value;fit()});
stext.addEventListener('keydown',ev=>{const plain=docked()&&!ev.shiftKey&&!ev.isComposing&&matchMedia('(hover:hover)').matches;  // on a phone, Return stays a newline
  if(ev.key==='Enter'&&(ev.metaKey||ev.ctrlKey||plain)){ev.preventDefault();if(!$('#ssend').disabled)$('#ssend').click()}});
sheet.addEventListener('keydown',ev=>{if(ev.key==='Escape'&&!docked())closeSheet()});
// Like a native sheet: drag it down (anywhere but the text box and buttons) and let go past 80 px
// to close it. Holding the drag also keeps the page behind from scrolling.
let sdrag=null;
sheet.addEventListener('touchstart',ev=>{sdrag=null;if(docked()||ev.touches.length>1||ev.target.closest('textarea,button,input'))return;
  sdrag={y:ev.touches[0].clientY,d:0}},{passive:true});
sheet.addEventListener('touchmove',ev=>{if(!sdrag)return;if(ev.cancelable)ev.preventDefault();
  sdrag.d=Math.max(0,ev.touches[0].clientY-sdrag.y);sheet.classList.add('drag');sheet.style.transform=sdrag.d?`translateY(${sdrag.d}px)`:''},{passive:false});
function sdragEnd(ev){const d=sdrag?sdrag.d:0;sdrag=null;sheet.classList.remove('drag');sheet.style.transform='';
  if(ev.type==='touchend'&&d>80)closeSheet()}
sheet.addEventListener('touchend',sdragEnd);sheet.addEventListener('touchcancel',sdragEnd);
$('#sclose').addEventListener('click',closeSheet);$('#scrim').addEventListener('click',closeSheet);
$('#ssend').addEventListener('click',async()=>{const e=target,text=stext.value.trim();if(!e||!text||sending)return;
  sending=true;sheetError('');fit();
  try{const r=await post(e.newChat?'/api/newchat':'/api/respond',JSON.stringify(e.newChat?{text}:e.direct?{chat:e.chat,text}:{entry:e.id,text}),{'Content-Type':'application/json'});
    if(!r.ok){sheetError(await reason(r,'Couldn’t send that.'));return}
    delete drafts[e.id];stext.value='';if(docked()){if(conv)conv.stick=true}else closeSheet();buzz('ok');autoAnswered();last='';poll();if(e.todo)tdChatNote(e.todo)}
  catch(_){sheetError('Couldn’t reach your Mac. Your reply is still here.')}
  finally{sending=false;fit()}});
document.addEventListener('click',ev=>{
  const dismissed=!$('#pastpanel').hidden&&!ev.target.closest('#pastwrap');if(dismissed)setPast(false);
  const dm=ev.target.closest('.dot[data-mark]');if(dm){cycleMark(dm);return}
  const nc=ev.target.closest('#newchat');if(nc){if(docked())undock();openSheet({id:'new-chat',newChat:true,title:'New chat'},nc);return}
  const pk=ev.target.closest('[data-pick]');if(pk){togglePick(pk.dataset.pick,+pk.dataset.q,+pk.dataset.o);return}
  const sp=ev.target.closest('[data-sendpicks]');if(sp){sendPicks(sp.dataset.sendpicks,sp);return}
  const rb=ev.target.closest('[data-reply]');if(rb){const e=data.entries.find(x=>x.id===rb.dataset.reply);if(e)openSheet(e,rb);return}
  if(ev.target.closest('#back')){view='';want='';if(location.pathname!==agentPath(agent))history.pushState(null,'',agentPath(agent));setTimeout(render);return}
  if(ev.target.closest('[data-earlier]')){loadEarlier();return}
  const rt=ev.target.closest('[data-retry]');if(rt){rt.disabled=true;post('/api/respond/retry',JSON.stringify({id:rt.dataset.retry}),{'Content-Type':'application/json'}).finally(()=>{last='';poll()});return}
  const cx=ev.target.closest('[data-cancel]');if(cx){post('/api/respond/cancel',JSON.stringify({id:cx.dataset.cancel}),{'Content-Type':'application/json'}).finally(()=>{last='';poll()});return}
  const ag=ev.target.closest('[data-agent]');if(ag){agent=ag.dataset.agent;filter='all';want='';
    if(location.pathname!==agentPath(agent))history.pushState(null,'',agentPath(agent));setTimeout(render);return}
  const c=ev.target.closest('[data-chat]');if(c){if(c.closest('#pastpanel')){$('#pq').value='';setPast(false,true)}
    filter=c.dataset.chat;setTimeout(render);return}  // after the click: render replaces the chip, and a removed label can't forward its tap
  const b=ev.target.closest('.play[data-key]');if(b&&!b.disabled){
    if(b.classList.contains('warming')&&!clipUrl(b.dataset.file)){setWant(wantPlay&&wantPlay.key===b.dataset.key?null:{key:b.dataset.key,label:b.dataset.label});if(wantPlay)unlockAudio();tryWant();return}
    if(!(cur&&cur.key===b.dataset.key&&!audio.paused))setWant(null);  // pausing the playing clip keeps a queued one
    queue=[];play(b.dataset.key,b.dataset.file,b.dataset.label);return}
  const all=ev.target.closest('[data-all]');if(all){const e=data.entries.find(x=>x.id===all.dataset.all);if(!e)return;setWant(null);
    queue=e.questions.map((q,i)=>({key:e.id+'#'+i,file:q.audio,label:(q.header||'Question')+': '+(q.summary||'')})).filter(x=>x.file);
    const f=queue.shift();if(f){cur=null;play(f.key,f.file,f.label)}return}
  if(ev.target.closest('#pp')&&cur){if(audio.paused){setWant(null);audio.play()}else audio.pause();return}
  const o=ev.target.closest('[data-open]');if(!o||dismissed||ev.target.closest('button,.qfold>summary')||String(getSelection()))return;  // a disabled play button, or a drag to select text, isn't a tap on the card
  ev.preventDefault();const ch=data.chats.find(x=>x.id===o.dataset.open);if(!ch)return;
  view=ch.id;want=ch.short;const p=chatPath(ch);if(location.pathname!==p)history.pushState(null,'',p);setTimeout(render)});
// Haptics. Android has the Vibration API. iPhones don't, but Safari plays the system tick when an
// <input switch> toggles. A real tap is the dependable trigger, so on iOS every button carries a
// transparent label for #hsw and the tap is forwarded as a trusted click. Moments with no tap to
// forward (the pull, a finished send) click a label from script, which newer iOS may ignore.
const hsw=$('#hsw'),hlabel=$('#hlabel'),VIB=typeof navigator.vibrate==='function',
  IOS=!VIB&&matchMedia('(pointer:coarse)').matches;
function buzz(kind){const twice=kind==='ok'||kind==='bad';
  if(VIB){try{navigator.vibrate(kind==='bad'?[30,60,30]:twice?[12,50,12]:10)}catch(_){}}
  else if(IOS){hlabel.click();if(twice)setTimeout(()=>hlabel.click(),120)}}
for(const el of[hsw,hlabel])el.addEventListener('click',ev=>ev.stopPropagation());  // the page's click handler closes panels
if(VIB)document.addEventListener('click',ev=>{if(ev.target.closest('button'))buzz()},true);
if(IOS){document.documentElement.classList.add('ios');
  const arm=()=>document.querySelectorAll('button:not(:has(>.hz))').forEach(b=>b.insertAdjacentHTML('beforeend','<label class="hz" for="hsw" aria-hidden="true"></label>'));
  // a removed element can be a button's label (textContent on a button drops it), so removals count too
  const els=ms=>ms.some(m=>{for(const l of[m.addedNodes,m.removedNodes])for(const n of l)if(n.nodeType===1)return true;return false});
  new MutationObserver(ms=>{if(els(ms))arm()}).observe(document.body,{childList:true,subtree:true});arm()}
// Pull to refresh: re-reads the feed without reloading, so audio keeps playing and drafts survive.
// poll() won't render while `pull` is set: replacing the tree would remove the element the finger
// started on, and touch events stop reaching the window once their target is gone.
const PULL_ARM=56,PULL_MAX=100,PULL_HOLD=52,ptrp=$('#ptrp'),ptrt=$('#ptrt'),mainEl=$('main');
let refreshing=false;
function ptrShow(d,text,state){mainEl.style.transform=d?`translateY(${d}px)`:'';
  ptrp.style.setProperty('--o',text?Math.min(1,d/40||1):0);ptrp.style.setProperty('--r',d*4+'deg');
  ptrp.className='ptrp '+(state||'');if(ptrt.textContent!==text)ptrt.textContent=text}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function refresh(){refreshing=true;ptrShow(PULL_HOLD,'Refreshing…','spin');
  last='';const [ok]=await Promise.all([poll(8000),wait(500)]);
  if(ok){buzz('ok');ptrShow(0,'')}
  else{buzz('bad');ptrShow(0,'Couldn’t reach your Mac','bad');await wait(2200);ptrShow(0,'')}
  refreshing=false}
addEventListener('touchstart',ev=>{pull=null;
  if(refreshing||ev.touches.length>1||scrollY>0||!sheet.hidden||view||ev.target.closest('.crow,.panel,#seek,input,textarea'))return;
  pull={x:ev.touches[0].clientX,y:ev.touches[0].clientY,d:0,armed:false,on:false}},{passive:true});
function pullMove(ev){if(!pull)return;
  const t=ev.touches[0],dx=t.clientX-pull.x,dy=t.clientY-pull.y;
  if(ev.touches.length>1){const was=pull.on;pull=null;mainEl.classList.remove('pulling');if(was)ptrShow(0,'');return}
  if(!pull.on){if(dy<=0||Math.abs(dx)>dy||scrollY>0){pull=null;return}
    pull.on=true;pull.y=t.clientY;mainEl.classList.add('pulling')}
  if(ev.cancelable)ev.preventDefault();  // on the first move too: once the browser starts scrolling it can't be stopped
  pull.d=Math.min(PULL_MAX,Math.max(0,(t.clientY-pull.y)*.5));const armed=pull.d>=PULL_ARM;
  if(armed&&!pull.armed)buzz();pull.armed=armed;
  ptrShow(pull.d,armed?'Release to refresh':'Pull to refresh')}
// A pull can only start at the very top, and a blocking touchmove listener makes the browser check
// with the page before every scroll step, so it is attached only while the page sits at the top.
let pullOn=false;
function pullArm(){const top=scrollY<=0||!!pull;if(top===pullOn)return;pullOn=top;
  if(top)addEventListener('touchmove',pullMove,{passive:false});else removeEventListener('touchmove',pullMove,{passive:false})}
addEventListener('scroll',pullArm,{passive:true});pullArm();
function endPull(ev){const p=pull;pull=null;if(!p||!p.on)return;mainEl.classList.remove('pulling');
  if(p.armed&&ev.type==='touchend')refresh();else ptrShow(0,'')}
addEventListener('touchend',endPull);addEventListener('touchcancel',endPull);
$('#tree').addEventListener('toggle',ev=>{const k=ev.target.dataset&&ev.target.dataset.k;if(!k)return;
  if(ev.target.open)openTools.add(k);else openTools.delete(k)},true);
// Todo lists: one markdown file each under ~/.claude/todo, picked from the chips at the top. The
// widget's frame is built once and only its parts are redrawn, so the add box keeps focus (and a
// phone keeps its keyboard) while the list redraws under it. Every change names the line and text
// it saw; the server refuses with the fresh list (409) if the file moved on in between.
let todoM='';const tdOpen=(()=>{try{return JSON.parse(localStorage.getItem('todoOpen')||'{}')}catch(_){return{}}})();
function tdSave(){try{localStorage.setItem('todoOpen',JSON.stringify(tdOpen))}catch(_){}}
const tdInline=s=>esc(s).replace(/&lt;!--[\s\S]*?--&gt;/g,'').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/⚠/g,'<span class="tdw">⚠</span>');
const tdStore=(k,v)=>{try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(_){}return null};
let tdCur=tdStore('todoList')||'master',tdData=null,tdLists='',tdMode='',tdEdit=null,tdBusy=false,tdPend=null,tdMsgT=0;
const tdSec=(()=>{try{return JSON.parse(localStorage.getItem('todoSec')||'{}')}catch(_){return{}}})();
const tdKey=s=>tdCur==='master'?s:tdCur+'/'+s;  // master keeps the section keys it always had
const TD_AI=/\s*<!-- ai((?: (?:add|done|note|prog)=[0-9a-f-]{1,40})+) -->\s*$/;
function tdAi(t){const m=TD_AI.exec(t);if(!m)return{text:t,ai:{}};const ai={};
  m[1].trim().split(' ').forEach(p=>{const[k,v]=p.split('=');ai[k]=v});return{text:t.slice(0,m.index),ai}}
const TD_PEN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  TD_PROG='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/></svg>',
  TD_CHAT='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.6A8 8 0 1 1 21 12Z"/></svg>';
function tdPick(id){tdCur=id;tdStore('todoList',id)}
function tdSay(m,bad){const p=$('#todo .tdmsg');if(!p)return;p.textContent=m||'';p.classList.toggle('bad',!!bad);
  clearTimeout(tdMsgT);if(m)tdMsgT=setTimeout(()=>{p.textContent=''},5000)}
function tdReply(id){const e=data&&data.entries&&data.entries.find(x=>x.id===id);return e&&e.title?'“'+e.title+'”':'reply '+id}
function tdParse(md){let note='',cur=null,item=null,done=0,total=0;const secs=[];
  md.split('\n').forEach((ln,i)=>{let m;
    if(/^# /.test(ln))return;
    if((m=/^> ?(.*)/.exec(ln)))note=m[1];
    else if((m=/^## (.*)/.exec(ln))){cur={name:m[1].trim(),items:[],head:true};secs.push(cur);item=null}
    else if((m=/^[-*] \[([ xX/])\] (.*)/.exec(ln))){if(!cur){cur={name:'',items:[]};secs.push(cur)}
      item={i,done:/[xX]/.test(m[1]),prog:m[1]==='/',raw:m[2],...tdAi(m[2]),notes:[]};cur.items.push(item);total++;if(item.done)done++}
    else if(item&&(m=/^\s+[-*] (.*)/.exec(ln)))item.notes.push({i,raw:m[1],...tdAi(m[1])})});
  return{note,secs,done,total}}
function tdBadge(x,kind){const by={add:'Added',done:'Ticked',note:'Noted'}[kind];
  return `<button type="button" class="tdai" data-a="undo" data-k="${kind}" data-l="${x.i}" data-t="${esc(x.raw)}" title="${by} by AI from ${esc(tdReply(x.ai[kind]))}. Tap to undo." aria-label="${by} by AI. Undo">AI</button>`}
function tdItem(x){const kind=x.ai.done&&x.done?'done':x.ai.add?'add':'';
  return `<div class="tdi${x.done?' done':''}${x.prog?' prog':''}" data-row="${x.i}"><label class="tdck"><input type="checkbox" data-l="${x.i}" data-t="${esc(x.raw)}"${x.done?' checked':''}${x.prog?' data-prog':''} aria-label="${esc(x.text)}"></label>`+
    `<div class="tx"><span class="l">${tdInline(x.text)}</span>${x.prog?'<span class="tdprog">In progress</span>':''}${kind?tdBadge(x,kind):''}${x.notes.length?'<ul>'+x.notes.map(n=>`<li>${tdInline(n.text)}${n.ai.note?tdBadge(n,'note'):''}</li>`).join('')+'</ul>':''}</div>`+
    `<div class="acts">${x.done?'':`<button type="button" class="tdic" data-a="prog" data-l="${x.i}" aria-pressed="${x.prog}" aria-label="In progress" title="In progress">${TD_PROG}</button><button type="button" class="tdic" data-a="chat" data-l="${x.i}" aria-label="Start a chat about this item" title="Start a chat about this">${TD_CHAT}</button>`}<button type="button" class="tdic" data-a="edit" data-l="${x.i}" aria-label="Edit item" title="Edit">${TD_PEN}</button></div></div>`}
function tdBuild(){const box=$('#todo');if(box.firstChild)return box;
  box.innerHTML=`<details class="td" data-s="__main"${tdOpen.__main!==false?' open':''}><summary><span class="tdt"></span><span class="tdauto" hidden title="Updates from new replies">auto</span><span class="tbar"><i></i></span><span class="cnt"></span></summary><div class="tdb">`+
    `<div class="tdls"><div class="tdcs" role="group" aria-label="Todo lists"></div><button type="button" class="tdbt ic" data-a="new" aria-label="New list" title="New list">+</button><button type="button" class="tdbt ic" data-a="menu" aria-label="List options" title="List options" aria-expanded="false">⋯</button></div>`+
    `<div class="tdx"></div><p class="tdmsg" role="status" aria-live="polite"></p><div class="tdm"></div>`+
    `<form class="tda" autocomplete="off"><input class="tdin" maxlength="500" placeholder="Add an item" aria-label="New item" enterkeyhint="done"><select class="tdsel" aria-label="Section" hidden></select><button class="tdbt pri" type="submit">Add</button></form></div></details>`;
  return box}
function tdRender(j,force){const box=tdBuild();tdData=j;
  const lk=JSON.stringify([j.lists,j.id]);
  if(lk!==tdLists){tdLists=lk;const cs=box.querySelector('.tdcs');
    cs.innerHTML=(j.lists||[]).map(l=>`<button type="button" class="tdc" data-id="${esc(l.id)}" aria-pressed="${l.id===j.id}" title="${esc(l.name)}"><span>${esc(l.name)}</span><span class="n">${l.done}/${l.total}</span></button>`).join('');
    const on=cs.querySelector('[aria-pressed="true"]');if(on&&(on.offsetLeft<cs.scrollLeft||on.offsetLeft+on.offsetWidth>cs.scrollLeft+cs.clientWidth))cs.scrollLeft=on.offsetLeft-8}
  const key=j.id+'@'+j.mtime;
  if(tdEdit&&!force){tdPend=j;return}  // an open editor isn't torn down under the person typing in it
  if(key===todoM&&!force)return;todoM=key;tdPend=null;
  const p=tdParse(j.md);
  box.querySelector('.tdt').innerHTML=tdInline(j.name);box.querySelector('.tdauto').hidden=!j.auto;
  box.querySelector('.tbar i').style.width=(p.total?Math.round(p.done/p.total*100):0)+'%';
  box.querySelector('.cnt').textContent=p.done+'/'+p.total;
  // Checked items fold into a closed "N done" row at the end of their section, so the open ones
  // stay easy to find. A section already named "Done..." is folded whole and lists everything.
  const items=(s,k)=>{const todo=s.items.filter(x=>!x.done),done=s.items.filter(x=>x.done);
    if(!done.length||/^done/i.test(s.name))return s.items.map(tdItem).join('');
    const dk=k+'#done';
    return todo.map(tdItem).join('')+`<details class="tdd" data-s="${esc(dk)}"${tdOpen[dk]?' open':''}><summary>${done.length} done</summary>${done.map(tdItem).join('')}</details>`};
  const sec=s=>{const k=tdKey(s.name),open=k in tdOpen?tdOpen[k]:!/^done/i.test(s.name),d=s.items.filter(x=>x.done).length;
    return s.head||s.name?`<details class="tds" data-s="${esc(k)}"${open?' open':''}><summary>${esc(s.name)} · ${d}/${s.items.length}</summary>${items(s,k)}</details>`
      :`<div class="tds">${items(s,k)}</div>`};
  box.querySelector('.tdm').innerHTML=(p.note?`<p class="tdn">${tdInline(p.note)}</p>`:'')+(p.total||p.secs.length?p.secs.map(sec).join(''):'<p class="tdempty">Nothing here yet. Add the first item below.</p>');
  box.querySelectorAll('.tdm input[data-prog]').forEach(c=>{c.indeterminate=true});
  // Section picker for new items: only when the list has ## sections. The choice sticks per list.
  const sel=box.querySelector('.tdsel'),opts=p.secs.filter(s=>s.head).map(s=>s.name),ok=JSON.stringify(opts);
  if(sel.dataset.o!==ok){sel.dataset.o=ok;sel.innerHTML=opts.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('')}
  sel.hidden=!opts.length;if(opts.length)sel.value=opts.includes(tdSec[j.id])?tdSec[j.id]:opts[0];
  if(tdMode==='menu')tdModeDraw()}  // the forms aren't redrawn: that would reset what's being typed
function tdModeDraw(){const x=$('#todo .tdx'),j=tdData,master=j.id==='master';
  $('#todo [data-a="menu"]').setAttribute('aria-expanded',String(tdMode==='menu'));
  x.innerHTML=tdMode==='new'?`<form class="tdf" data-f="new"><input class="tdin" maxlength="60" placeholder="New list name" aria-label="New list name" enterkeyhint="done"><button class="tdbt pri" type="submit">Create</button><button class="tdbt" type="button" data-a="close">Cancel</button></form>`
    :tdMode==='ren'?`<form class="tdf" data-f="ren"><input class="tdin" maxlength="60" value="${esc(j.name)}" aria-label="List name" enterkeyhint="done"><button class="tdbt pri" type="submit">Rename</button><button class="tdbt" type="button" data-a="close">Cancel</button>${master?'<span class="tdh">Changes the heading only; master.md keeps its name.</span>':''}</form>`
    :tdMode==='del'?`<div class="tdf"><span class="q">Move “${esc(j.name)}” to the trash? It can be restored from ~/.claude/todo/.trash.</span><button class="tdbt dng armed" type="button" data-a="delok">Move to trash</button><button class="tdbt" type="button" data-a="close">Cancel</button></div>`
    :tdMode==='menu'?`<div class="tdf"><button class="tdbt" type="button" data-a="ren">Rename</button>${master?'':'<button class="tdbt dng" type="button" data-a="del">Delete list</button>'}<button class="tdbt" type="button" data-a="auto" aria-pressed="${!!j.auto}">Auto-update: ${j.auto?'on':'off'}</button><button class="tdbt" type="button" data-a="close">Close</button><span class="tdh">Auto-update lets Claude tick, add and note items from new replies. AI changes carry an AI badge (tap to undo) and are logged in ai-changes.jsonl.</span></div>`:'';
  const inp=x.querySelector('input');if(inp){inp.focus();inp.select()}}
function tdSetMode(m){tdMode=tdMode===m&&(m==='menu'||m==='new')?'':m;tdModeDraw()}
async function tdPost(op,body){if(tdBusy)return false;tdBusy=true;const box=$('#todo');box.firstChild.classList.add('busy');
  try{const r=await fetch('/api/todo/'+op,{method:'POST',headers:{'X-Replies':'1','Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json().catch(()=>({}));
    if(j.md!=null){if(j.id!==tdCur){tdPick(j.id);tdMode=''}tdRender(j,!tdEdit)}else{todoM='';pollTodo()}
    tdSay(j.error||'',!r.ok);if(!r.ok)buzz('bad');return r.ok?j:null}
  catch(_){tdSay('Couldn’t reach your Mac.',true);return false}  // false = never reached the server
  finally{tdBusy=false;box.firstChild.classList.remove('busy')}}
let todoTag='';  // sent only while a list is drawn (todoM set): clearing todoM still forces a full answer
async function pollTodo(){if(document.hidden&&todoM)return;const want=tdCur;
  try{const r=await fetch('/api/todo?list='+encodeURIComponent(want),{cache:'no-store',headers:todoM&&todoTag?{'If-None-Match':todoTag}:{}});
    if(r.status===304)return;
    const j=await r.json().catch(()=>({}));if(r.ok)todoTag=r.headers.get('ETag')||'';
    if(r.status===400||r.status===404){tdPick('master');tdMode='';if(j.md==null)return pollTodo()}  // deleted elsewhere: back to master
    else if(!r.ok||j.md==null||want!==tdCur)return;  // a slow answer for a list no longer shown
    tdRender(j)}catch(_){}}
function tdCloseEdit(){tdEdit=null;tdRender(tdPend||tdData,true)}
function tdOpenEdit(l){const row=$(`#todo [data-row="${l}"]`),cb=row&&row.querySelector('input[data-l]');if(!cb)return;
  if(tdEdit)tdCloseEdit();const x=tdAi(cb.dataset.t);tdEdit={l,t:cb.dataset.t};
  const r=$(`#todo [data-row="${l}"]`);r.querySelector('input').disabled=true;r.querySelector('.acts').hidden=true;
  r.querySelector('.tx').innerHTML=`<div class="tded"><input class="tdin" maxlength="500" value="${esc(x.text)}" aria-label="Item text" enterkeyhint="done"><div class="tdrow"><button class="tdbt pri" type="button" data-a="save">Save</button><button class="tdbt" type="button" data-a="cancel">Cancel</button><button class="tdbt dng" type="button" data-a="rm">Remove</button></div></div>`;
  const inp=r.querySelector('.tded input');inp.focus();inp.setSelectionRange(inp.value.length,inp.value.length)}
async function tdSaveEdit(){const inp=$('#todo .tded input');if(!tdEdit||!inp)return;const v=inp.value.trim();
  if(!v){tdSay('Empty text. Use Remove to drop the item.',true);return}
  if(v===tdAi(tdEdit.t).text){tdCloseEdit();return}
  const e=tdEdit;tdEdit=null;if(await tdPost('edit',{list:tdCur,line:e.l,text:e.t,new:v})===false)tdEdit=e}  // offline: the editor stays open
$('#todo').addEventListener('toggle',ev=>{const k=ev.target.dataset&&ev.target.dataset.s;if(k==null)return;tdOpen[k]=ev.target.open;tdSave()},true);
$('#todo').addEventListener('change',async ev=>{const c=ev.target;
  if(c.matches('.tdsel')){tdSec[tdCur]=c.value;tdStore('todoSec',JSON.stringify(tdSec));return}
  if(!c.matches('input[data-l]'))return;c.disabled=true;
  // ticking an in-progress item (shown half-ticked) marks it done; unticking a done one reopens it
  const was=c.hasAttribute('data-prog');
  if(!await tdPost('toggle',{list:tdCur,line:+c.dataset.l,text:c.dataset.t,done:c.checked,state:c.checked?'done':'open'})&&c.isConnected){
    c.checked=!c.checked;c.indeterminate=was;c.disabled=false}});
$('#todo').addEventListener('submit',async ev=>{ev.preventDefault();const f=ev.target,inp=f.querySelector('input'),v=inp.value.trim();
  if(!v){inp.focus();return}
  if(f.classList.contains('tda')){const sel=f.querySelector('.tdsel');
    if(await tdPost('add',{list:tdCur,text:v,section:sel.hidden?null:sel.value}))inp.value=''}  // focus stays put for the next item
  else if(f.dataset.f==='new'){if(await tdPost('create',{name:v})){tdMode='';tdModeDraw()}}
  else if(f.dataset.f==='ren'){if(await tdPost('rename',{list:tdCur,name:v})){tdMode='';tdModeDraw()}}});
$('#todo').addEventListener('keydown',ev=>{if(ev.key==='Escape'){if(tdEdit){ev.preventDefault();tdCloseEdit()}else if(tdMode){ev.preventDefault();tdMode='';tdModeDraw()}}
  else if(ev.key==='Enter'&&!ev.isComposing&&ev.target.closest('.tded')&&ev.target.matches('input')){ev.preventDefault();tdSaveEdit()}});
$('#todo').addEventListener('click',async ev=>{const t=ev.target.closest('[data-id],[data-a]');if(!t||!$('#todo').contains(t))return;
  if(t.dataset.id){if(t.dataset.id===tdCur)return;tdPick(t.dataset.id);tdMode='';tdEdit=null;tdPend=null;tdModeDraw();
    $('#todo .tdcs').querySelectorAll('.tdc').forEach(b=>b.setAttribute('aria-pressed',String(b===t)));pollTodo();return}
  const a=t.dataset.a,l=+t.dataset.l;
  if(a==='new'||a==='menu'||a==='ren'||a==='del')tdSetMode(a);
  else if(a==='close'){tdMode='';tdModeDraw()}
  else if(a==='delok'){if(await tdPost('delete',{list:tdCur})){tdMode='';tdModeDraw();tdSay('Moved to the trash.')}}
  else if(a==='auto'){if(await tdPost('auto',{list:tdCur,on:!tdData.auto}))tdModeDraw()}
  else if(a==='edit')tdOpenEdit(l);
  else if(a==='prog'){const cb=$(`#todo [data-row="${l}"] input[data-l]`);if(!cb)return;
    await tdPost('toggle',{list:tdCur,line:l,text:cb.dataset.t,state:cb.hasAttribute('data-prog')?'open':'progress'})}
  else if(a==='cancel')tdCloseEdit();
  else if(a==='save')tdSaveEdit();
  else if(a==='rm'){if(!t.classList.contains('armed')){t.classList.add('armed');t.textContent='Confirm remove';return}
    const e=tdEdit;tdEdit=null;if(await tdPost('remove',{list:tdCur,line:e.l,text:e.t})===false)tdEdit=e}
  else if(a==='undo'){const k=t.dataset.k;if(await tdPost('undo',{list:tdCur,line:l,text:t.dataset.t,kind:k}))
    tdSay(k==='done'?'Unticked it.':k==='add'?'Removed the item the AI added.':'Removed the AI’s note.')}
  else if(a==='chat'){const cb=$(`#todo [data-row="${l}"] input[data-l]`);if(!cb)return;const x=tdAi(cb.dataset.t),id='todo:'+tdCur+':'+x.text;
    if(!(id in drafts))drafts[id]=x.text+'\n\n(From my “'+tdData.name+'” todo list.)';
    if(docked())undock();openSheet({id,newChat:true,title:'New chat',todo:{list:tdCur,line:l,text:cb.dataset.t}},t)}});
function tdChatNote(t){tdPost('chatnote',t)}  // after the chat starts: a dated note under the item
pollTodo();setInterval(pollTodo,3000);
// A hidden tab (phone locked, app switched) stops polling; coming back polls at once.
const shown=f=>()=>{if(!document.hidden)f()};
document.addEventListener('visibilitychange',()=>{if(!document.hidden){poll();syncMarks()}});
$('#pp').innerHTML=PLAY;poll();setInterval(shown(poll),2500);setInterval(pollConv,1500);syncHeard();setInterval(shown(syncHeard),2500);syncMarks();setInterval(shown(syncMarks),2500);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    os.umask(0o077)  # entries can carry private data: owner-only
    os.makedirs(ROOT, exist_ok=True)  # fresh machine: no reply has been rendered yet
    Handler.tailnet_port = port - 1
    Handler.local_port = port
    local = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if REQUIRE_KEY:  # after the bind: a second start fails there instead of rewriting the key
        KEY = load_key()
    if REPLIES_HOME or REQUIRE_KEY:  # follow plugin updates; elsewhere only exit once deleted
        threading.Thread(target=follow_installs, daemon=True).start()
    threading.Thread(target=delivery_loop, daemon=True).start()
    threading.Thread(target=chat_answer_loop, daemon=True).start()
    threading.Thread(target=todo_auto_loop, daemon=True).start()
    try:
        tailnet = ThreadingHTTPServer(("127.0.0.1", port - 1), Handler)
        threading.Thread(target=tailnet.serve_forever, daemon=True).start()
    except OSError as exc:
        print(f"tailnet listener not started: {exc}", file=sys.stderr)
    local.serve_forever()
