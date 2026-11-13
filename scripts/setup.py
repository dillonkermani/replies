#!/usr/bin/env python3
"""SessionStart hook for the replies plugin: make sure the page is up, wire the phone link, and
tell the user where it is.

1. Start reply-audio-server.py on 127.0.0.1:<port> if nothing is listening there.
2. Phone access: if Tailscale is installed and signed in, point `tailscale serve` (tailnet only,
   HTTPS) at the server's second listener on <port - 1>. Reuses an existing mapping; never
   touches a port already serving something else; never uses `tailscale funnel` (public).
3. If something is missing, point to /replies-setup (a guided, interactive setup). With the
   plugin option install_deps=on it instead installs ffmpeg, whisper-cpp and tmux with Homebrew
   and fetches the Whisper model in a detached background job, retried at most once a day.
4. Print the URLs as a systemMessage.

REPLIES_SETUP_DRYRUN=1 prints what it would change instead of running tailscale serve or brew.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

# The plugin's own folder (config.json lives here). Hooks get CLAUDE_PLUGIN_DATA; skills pass it in
# as REPLIES_PLUGIN_DATA, because plugin env vars and options aren't visible in a skill's shell.
PLUGIN_DATA = (os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("REPLIES_PLUGIN_DATA")
               or os.path.expanduser("~/.replies"))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(PLUGIN_DATA, "config.json")  # SessionStart sees the plugin options; skills don't


def _saved(name):
    try:
        with open(CONFIG) as fh:
            return str(json.load(fh).get(name) or "")
    except (OSError, ValueError):
        return ""


def _opt(name):
    return (os.environ.get("CLAUDE_PLUGIN_OPTION_" + name) or "").strip()


# Where replies are stored: the data_dir option (an existing reply store), else the plugin's folder.
# Only skills (which can't see options) fall back to the saved value: in a hook the option is
# authoritative, so clearing it moves the server to the folder the reply hook now writes to.
_IN_HOOK = "CLAUDE_PLUGIN_OPTION_DATA_DIR" in os.environ
DATA = os.path.expanduser(os.environ.get("REPLIES_HOME") or _opt("DATA_DIR")
                          or ("" if _IN_HOOK else _saved("data_dir")) or PLUGIN_DATA)
KEY_ON = (_opt("REQUIRE_KEY") or _saved("require_key") or "on").lower() != "off"
PORT = int(os.environ.get("REPLIES_PORT") or os.environ.get("CLAUDE_PLUGIN_OPTION_PORT")
           or _saved("port") or 8799)
MODEL = (os.environ.get("REPLIES_MODEL") or os.environ.get("CLAUDE_PLUGIN_OPTION_MODEL")
         or _saved("model") or "claude-sonnet-5-5")
PHONE = (os.environ.get("CLAUDE_PLUGIN_OPTION_PHONE") or "on").lower() != "off"
INSTALL = (os.environ.get("CLAUDE_PLUGIN_OPTION_INSTALL_DEPS") or "off").lower() == "on"  # else /replies-setup asks
DRY = os.environ.get("REPLIES_SETUP_DRYRUN") == "1"
TS_PORTS = (8443, 8444, 8445, 10443)
# Pinned to a commit, and checked against the sha256 Hugging Face publishes for that file.
MODEL_URL = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/"
             "5359861c739e955e79d9a303bcbc70fb988958b1/ggml-base.en.bin")
MODEL_SHA256 = "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002"
MODEL_NAMES = ("large-v3-turbo", "medium.en", "small.en", "small", "base.en", "base")
BREW_DEPS = (("ffmpeg", "ffmpeg"), ("whisper-cli", "whisper-cpp"), ("tmux", "tmux"))
RETRY_SECONDS = 86400


def which(cmd, *extra):
    found = shutil.which(cmd)
    if found:
        return found
    for path in extra + (f"/opt/homebrew/bin/{cmd}", f"/usr/local/bin/{cmd}"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def listening(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server():
    if listening(PORT):
        return True
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_PLUGIN_")}
    env.update(REPLIES_HOME=DATA, REPLIES_PORT=str(PORT), REPLIES_REQUIRE_KEY="1" if KEY_ON else "0")
    subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "reply-audio-server.py"), str(PORT)],
                     env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(20):
        if listening(PORT):
            return True
        time.sleep(0.25)
    return False


def read_key():
    """The install's key, written by the server on start (owner-only); "" with the key off."""
    if not KEY_ON:
        return ""
    path = os.path.join(DATA, "key")
    for _ in range(20):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as fh:
                key = fh.read().strip()
            if key:
                return key
        except OSError:
            pass
        time.sleep(0.1)
    return ""


def with_key(url, key):
    return f"{url}#k={key}" if key else url


def tailscale_bin():
    return which("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def phone_link():
    """(url, note): the tailnet URL, or None with a one-line reason."""
    if not PHONE:
        return None, "phone link is off (plugin option `phone`)"
    ts = tailscale_bin()
    if not ts:
        return None, "install Tailscale and sign in to open this on your phone"
    try:
        status = json.loads(subprocess.run([ts, "status", "--json"], capture_output=True,
                                           text=True, timeout=8).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, "Tailscale isn't responding; open the Tailscale app, then start a new session"
    if status.get("BackendState") != "Running":
        return None, "sign in to Tailscale, then start a new session"
    host = ((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if not host:
        return None, "Tailscale has no MagicDNS name for this Mac; turn on MagicDNS"
    target = f"http://127.0.0.1:{PORT - 1}"
    try:
        serve = json.loads(subprocess.run([ts, "serve", "status", "--json"], capture_output=True,
                                          text=True, timeout=8).stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        serve = {}
    for hostport, web in (serve.get("Web") or {}).items():
        if ((web.get("Handlers") or {}).get("/") or {}).get("Proxy") == target:
            return f"https://{hostport.split(':')[0]}:{hostport.rsplit(':', 1)[1]}/", None
    used = set((serve.get("TCP") or {}).keys())
    port = next((p for p in TS_PORTS if str(p) not in used), None)
    if port is None:
        return None, f"tailscale serve already uses ports {', '.join(map(str, TS_PORTS))}"
    cmd = [ts, "serve", "--bg", "--yes", f"--https={port}", target]
    if DRY:
        return f"https://{host}:{port}/", "dry run: would run " + " ".join(cmd[1:])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"tailscale serve failed: {exc}"
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().splitlines()
        hint = next((m for m in msg if "https://" in m), msg[0] if msg else "unknown error")
        return None, f"tailscale serve needs HTTPS turned on for your tailnet: {hint.strip()}"
    return f"https://{host}:{port}/", None


def have_model():
    dirs = [os.path.join(DATA, "models"), os.path.expanduser("~/.voicemode/services/whisper/models")]
    return any(os.path.isfile(os.path.join(d, f"ggml-{n}.bin")) for d in dirs for n in MODEL_NAMES)


def install_missing():
    """Kick off a background install of what's missing; returns a one-line note or None."""
    voicemode_whisper = os.path.expanduser("~/.voicemode/services/whisper/build/bin/whisper-cli")
    missing = [pkg for cmd, pkg in BREW_DEPS
               if not which(cmd, *([voicemode_whisper] if cmd == "whisper-cli" else []))]
    need_model = not have_model()
    if not missing and not need_model:
        return None
    marker = os.path.join(DATA, "setup-attempt")
    log = os.path.join(DATA, "setup.log")
    if os.path.isfile(marker) and time.time() - os.path.getmtime(marker) < RETRY_SECONDS:
        if os.path.isfile(os.path.join(DATA, "setup-running")):
            return f"still installing in the background (log: {log})"
        what = ", ".join(missing + (["Whisper model"] if need_model else []))
        return f"missing: {what}. See {log}"
    if not INSTALL:
        what = ", ".join(missing + (["Whisper model"] if need_model else []))
        return f"Not set up yet: {what}. Run /replies-setup for a guided setup."
    brew = which("brew")
    if missing and not brew:
        return ("install Homebrew (brew.sh), then start a new session; needed for: "
                + ", ".join(missing))
    # Every path and value goes in as a positional argument, never into the script text.
    script = 'touch "$1"; '
    if missing:
        script += 'NONINTERACTIVE=1 "$2" install $3; '
    if need_model:
        script += ('mkdir -p "$(dirname "$4")" && curl -fL --proto =https --retry 3 -o "$4.part" "$5" '
                   '&& [ "$(shasum -a 256 "$4.part" | cut -d" " -f1)" = "$6" ] '
                   '&& mv "$4.part" "$4" || { rm -f "$4.part"; echo "model download failed or '
                   'checksum mismatch"; }; ')
    script += 'rm -f "$1"'
    running = os.path.join(DATA, "setup-running")
    args = [running, brew or "", " ".join(missing), os.path.join(DATA, "models", "ggml-base.en.bin"),
            MODEL_URL, MODEL_SHA256]
    what = ", ".join(missing + (["a local Whisper model (~150 MB)"] if need_model else []))
    if DRY:
        return f"dry run: would install {what}"
    open(marker, "w").close()
    with open(log, "a") as fh:
        fh.write(f"\n--- {time.ctime()}\n")
        subprocess.Popen(["/bin/sh", "-c", script, "replies-setup", *args], stdin=subprocess.DEVNULL, stdout=fh,
                         stderr=subprocess.STDOUT, start_new_session=True)
    return f"installing {what} in the background (log: {log})"


def main():
    try:
        json.load(sys.stdin)
    except ValueError:
        pass
    os.umask(0o077)
    os.makedirs(os.path.join(DATA, "r"), exist_ok=True)
    try:
        with open(CONFIG, "w") as fh:
            json.dump({"port": PORT, "model": MODEL, "data_dir": DATA,
                       "require_key": "on" if KEY_ON else "off"}, fh)
    except OSError:
        pass
    lines = []
    up = start_server()
    key = read_key() if up else ""
    if up:
        lines.append(f"Replies: {with_key(f'http://localhost:{PORT}/', key)}")
    else:
        lines.append(f"Replies: the server didn't start on port {PORT} (is something else using it?)")
    url, note = phone_link()
    if url:
        lines.append(f"Phone: {with_key(url, key)}  (your devices on your tailnet)")
    if note:
        lines.append(f"Phone: {note}" if not url else note)
    if sys.platform == "darwin":
        dep = install_missing()
        if dep:
            lines.append(dep)
    else:
        lines.append("Spoken replies need macOS (or a local Kokoro server); the page still works.")
    print(json.dumps({"systemMessage": "\n".join(lines)}))


if __name__ == "__main__":
    main()
