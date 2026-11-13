#!/usr/bin/env python3
"""Checks and fixes for the /replies-setup walkthrough. Every step is deterministic; the skill
only decides what to ask the person and in what order.

  doctor.py check            JSON: one item per requirement, with ok / detail / fix
  doctor.py install ITEM...  install ffmpeg | whisper | tmux | model (foreground, with output)
  doctor.py phone            set up the tailnet link with `tailscale serve`; JSON with the URL
  doctor.py test             make one spoken test reply through the real pipeline; JSON result
  doctor.py links            JSON: the local and phone links, each carrying the key
  doctor.py open [CHAT8]     start the page if needed, open it in the browser (on a chat), JSON links
  doctor.py cli              add `replies` (terminal panel, for SSH) and `replies-server` to ~/.local/bin

Skills run it as REPLIES_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py".
Plugin options aren't visible to a skill's shell, so port and model come from the config.json that
SessionStart writes into the data folder.
"""
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("replies_setup", os.path.join(HERE, "setup.py"))
setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(setup)  # shares paths, port, key and the tailscale logic with SessionStart

DATA, PORT = setup.DATA, setup.PORT
BREW_PKG = {"ffmpeg": "ffmpeg", "whisper": "whisper-cpp", "tmux": "tmux"}
VOICEMODE_WHISPER = os.path.expanduser("~/.voicemode/services/whisper/build/bin/whisper-cli")
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def item(id_, ok, label, detail="", fix="", optional=False):
    return {"id": id_, "ok": bool(ok), "label": label, "detail": detail, "fix": fix,
            "optional": optional}


def get(url, key="", timeout=3):
    req = urllib.request.Request(url, headers={"X-Replies-Key": key} if key else {})
    try:
        with NO_PROXY.open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def tailscale_status():
    ts = setup.tailscale_bin()
    if not ts:
        return None, {}
    try:
        return ts, json.loads(subprocess.run([ts, "status", "--json"], capture_output=True,
                                             text=True, timeout=8).stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ts, {}


def check():
    out = []
    mac = platform.mac_ver()[0]
    out.append(item("macos", sys.platform == "darwin", "macOS", f"macOS {mac}" if mac else sys.platform,
                    "Spoken replies need macOS for now."))
    out.append(item("python", sys.version_info >= (3, 9), "Python 3.9+", platform.python_version(),
                    "Install the Xcode Command Line Tools: xcode-select --install"))
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    out.append(item("claude", os.path.isfile(claude), "Claude Code CLI", claude,
                    "Install Claude Code so `claude` is on your PATH."))
    brew = setup.which("brew")
    out.append(item("brew", brew, "Homebrew", brew or "not found",
                    "Install from https://brew.sh, then run /replies-setup again."))
    out.append(item("ffmpeg", setup.which("ffmpeg"), "ffmpeg (makes the audio files)",
                    setup.which("ffmpeg") or "not found", "doctor.py install ffmpeg"))
    kokoro = get("http://127.0.0.1:8880/v1/models") == 200
    out.append(item("voice", kokoro or setup.which("say"), "A voice",
                    "Kokoro (natural voice)" if kokoro else "macOS built-in voice",
                    "", optional=True))
    whisper = setup.which("whisper-cli", VOICEMODE_WHISPER)
    out.append(item("whisper", whisper, "whisper.cpp (speech-to-text on this Mac)",
                    whisper or "not found", "doctor.py install whisper", optional=True))
    out.append(item("model", setup.have_model(), "Whisper speech model",
                    "found" if setup.have_model() else "not downloaded (~150 MB)",
                    "doctor.py install model", optional=True))
    out.append(item("tmux", setup.which("tmux"), "tmux (lets the page type into your chats)",
                    setup.which("tmux") or "not found", "doctor.py install tmux", optional=True))
    up = setup.listening(PORT)
    key = setup.read_key() if up else ""
    api = get(f"http://127.0.0.1:{PORT}/api/heard", key) if up else 0
    out.append(item("server", up and api == 200, f"Replies page on port {PORT}",
                    "running" if api == 200 else (f"port {PORT} answers but not as Replies ({api})" if up
                                                  else "not running"),
                    "Start a new Claude Code session, or set another `port` in the plugin options."))
    ts, st = tailscale_status()
    running = st.get("BackendState") == "Running"
    me = st.get("Self") or {}
    login = ((st.get("User") or {}).get(str(me.get("UserID"))) or {}).get("LoginName", "")
    out.append(item("tailscale", ts, "Tailscale app", ts or "not installed",
                    "Install Tailscale from https://tailscale.com/download/mac and sign in.",
                    optional=True))
    out.append(item("tailscale_login", running, "Signed in to Tailscale",
                    f"signed in as {login}" if running and login else (st.get("BackendState") or "no"),
                    "Open the Tailscale menu-bar app and sign in.", optional=True))
    out.append(item("magicdns", bool(me.get("DNSName")), "MagicDNS name for this Mac",
                    (me.get("DNSName") or "").rstrip(".") or "none",
                    "Admin console → DNS → enable MagicDNS.", optional=True))
    out.append(item("https", bool(st.get("CertDomains")), "Tailscale HTTPS certificates",
                    "enabled" if st.get("CertDomains") else "off",
                    "Admin console → DNS → HTTPS Certificates → Enable (needed for the phone mic).",
                    optional=True))
    url = phone_url_if_set(ts)
    out.append(item("phone", url, "Phone link", url or "not set up", "doctor.py phone", optional=True))
    return out


def phone_url_if_set(ts):
    if not ts:
        return ""
    try:
        serve = json.loads(subprocess.run([ts, "serve", "status", "--json"], capture_output=True,
                                          text=True, timeout=8).stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    target = f"http://127.0.0.1:{PORT - 1}"
    for hostport, web in (serve.get("Web") or {}).items():
        if ((web.get("Handlers") or {}).get("/") or {}).get("Proxy") == target:
            return f"https://{hostport}/"
    return ""


def install(items):
    rc = 0
    brew = setup.which("brew")
    pkgs = [BREW_PKG[i] for i in items if i in BREW_PKG]
    if pkgs:
        if not brew:
            print("Homebrew isn't installed; get it from https://brew.sh first.")
            return 1
        print(f"Installing with Homebrew: {' '.join(pkgs)}", flush=True)
        rc |= subprocess.run([brew, "install", *pkgs], env=dict(os.environ, NONINTERACTIVE="1")).returncode
    if "model" in items and not setup.have_model():
        dest = os.path.join(DATA, "models", "ggml-base.en.bin")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        print("Downloading the Whisper speech model (~150 MB)…", flush=True)
        part = dest + ".part"
        r = subprocess.run(["curl", "-fL", "--proto", "=https", "--retry", "3", "-o", part, setup.MODEL_URL])
        sha = subprocess.run(["shasum", "-a", "256", part], capture_output=True, text=True).stdout.split(" ")[0] \
            if r.returncode == 0 else ""
        if sha == setup.MODEL_SHA256:
            os.replace(part, dest)
            print("Model downloaded and verified.")
        else:
            if os.path.exists(part):
                os.remove(part)
            print("Model download failed or didn't match its checksum; nothing was kept.")
            rc |= 1
    return rc


LAUNCHER = """#!/bin/sh
# {what} from the installed replies plugin. Looks the plugin up on every run, so it keeps
# working when the plugin updates (its folder name changes with the version).
dir=$(/usr/bin/python3 -I -c 'import json,os,sys
p=json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json"))).get("plugins",{{}})
d=[v[0]["installPath"] for k,v in p.items() if k.split("@")[0]=="replies" and v]
print(d[0]) if d else sys.exit("replies plugin is not installed")') || exit 1
exec /usr/bin/python3 -I "$dir/scripts/{script}" "$@"
"""

# The page server needs the settings SessionStart saved (data folder, port, key mode); started bare
# it would serve the legacy store with the key off.
SERVER_LAUNCHER = """#!/bin/sh
# The page server from the installed replies plugin, with the settings the plugin saved at the last
# session start, so it serves the store the hooks write to and keeps the key on when it is on.
exec /usr/bin/python3 -I -c 'import glob,json,os,sys
p=json.load(open(os.path.expanduser("~/.claude/plugins/installed_plugins.json"))).get("plugins",{})
d=[v[0]["installPath"] for k,v in p.items() if k.split("@")[0]=="replies" and v]
d or sys.exit("replies plugin is not installed")
c={}
for f in sorted(glob.glob(os.path.expanduser("~/.claude/plugins/data/replies-*/config.json")),
                key=os.path.getmtime, reverse=True):
    try:
        c=json.load(open(f)); break
    except (OSError, ValueError):
        pass
c.get("data_dir") or sys.exit("start a Claude Code session once so the replies plugin saves its settings")
port=str(c.get("port") or 8799)
os.environ.update(REPLIES_HOME=c["data_dir"], REPLIES_PORT=port,
                  REPLIES_REQUIRE_KEY="0" if c.get("require_key")=="off" else "1")
s=os.path.join(d[0],"scripts","reply-audio-server.py")
os.execv(sys.executable,[sys.executable,"-I",s]+(sys.argv[1:] or [port]))' "$@"
"""


def cli():
    """Write ~/.local/bin/replies and replies-server. Replaces only these two names (a symlink
    there is removed first, never followed)."""
    bindir = os.path.expanduser("~/.local/bin")
    os.makedirs(bindir, exist_ok=True)
    made = []
    for name, script, what in (("replies", "reply-audio-tui.py", "The terminal panel"),
                               ("replies-server", "reply-audio-server.py", "The page server")):
        path = os.path.join(bindir, name)
        if os.path.islink(path):
            os.remove(path)
        with open(path, "w") as fh:
            fh.write(SERVER_LAUNCHER if name == "replies-server"
                     else LAUNCHER.format(what=what, script=script))
        os.chmod(path, 0o755)
        made.append(path)
    on_path = bindir in os.environ.get("PATH", "").split(":")
    return {"written": made, "on_path": on_path}


def links():
    key = setup.read_key() if setup.listening(PORT) else ""
    ts, _ = tailscale_status()
    phone = phone_url_if_set(ts)
    return {"local": setup.with_key(f"http://localhost:{PORT}/", key),
            "phone": setup.with_key(phone, key) if phone else ""}


def test():
    """One synthetic reply through the real hook: rewrite (one small Claude call), voice, page."""
    msg = ("Setup check. If you can hear this, spoken replies are working on this Mac. Longer replies "
           "from Claude will show up on the replies page as short audio clips you can play, from your "
           "computer or your phone. Questions Claude asks you appear as cards, and you can answer by "
           "typing or by tapping the microphone and talking. Everything is spoken and transcribed on "
           "this machine, and the page only opens for you.")
    payload = {"hook_event_name": "Stop", "session_id": "00000000-0000-4000-8000-00000000c0de",
               "cwd": "replies-setup", "last_assistant_message": msg}
    hook = os.path.join(HERE, "reply-audio.py")
    env = dict(os.environ, REPLIES_HOME=DATA, REPLIES_PORT=str(PORT), REPLIES_MODEL=setup.MODEL,
               REPLIES_REQUIRE_KEY="1" if setup.KEY_ON else "0")
    before = set(os.listdir(os.path.join(DATA, "r"))) if os.path.isdir(os.path.join(DATA, "r")) else set()
    subprocess.run([sys.executable, "-I", hook], input=json.dumps(payload), text=True,
                   capture_output=True, timeout=30, env=env)
    deadline = time.time() + 180
    while time.time() < deadline:
        new = [n for n in os.listdir(os.path.join(DATA, "r")) if n.endswith(".json") and n not in before
               and ".resp-" not in n and ".answers" not in n]
        for n in new:
            try:
                e = json.load(open(os.path.join(DATA, "r", n)))
            except (OSError, ValueError):
                continue
            if e.get("status") in ("ready", "failed"):
                return {"ok": e.get("status") == "ready", "title": e.get("title", ""), **links()}
        time.sleep(2)
    return {"ok": False, "error": f"timed out; see {os.path.join(DATA, 'reply-audio.log')}"}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        print(json.dumps(check(), indent=1))
    elif cmd == "install":
        sys.exit(install(sys.argv[2:]))
    elif cmd == "phone":
        url, note = setup.phone_link()
        key = setup.read_key() if setup.listening(PORT) else ""
        print(json.dumps({"url": setup.with_key(url, key) if url else "", "note": note or ""}))
    elif cmd == "links":
        print(json.dumps(links()))
    elif cmd == "cli":
        print(json.dumps(cli()))
    elif cmd == "open":
        os.makedirs(os.path.join(DATA, "r"), exist_ok=True)
        up = setup.start_server()
        out = links()
        chat = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2].isalnum() and len(sys.argv[2]) == 8 else ""
        if chat:
            out = {k: (v.replace("/#k=", f"/c/{chat}#k=") if "#k=" in v else (v + f"c/{chat}" if v else v))
                   for k, v in out.items()}
        if up and out["local"]:
            subprocess.run(["open", out["local"]], capture_output=True)
        print(json.dumps(dict(out, running=up)))
    elif cmd == "test":
        print(json.dumps(test()))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
