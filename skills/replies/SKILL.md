---
name: replies
description: >-
  Open the replies page (spoken versions of Claude's replies, question cards, and each chat's
  conversation, where you can answer by text or voice) with THIS chat selected, starting the
  local server only if nothing is listening, and give the phone (tailnet) link. Never restarts
  or kills a running server. Use when asked for /replies, the replies page, the reply audio,
  the phone link, or why spoken replies aren't working.
---

# replies

One job: the page is up, it opens on the chat that ran `/replies`, and the user has both links.

The plugin's SessionStart hook normally starts the server and sets up the phone link, so it is
usually already running. Everything the plugin stores lives in its data folder, not the project.
If anything isn't set up yet (no audio, no phone link), suggest `/replies-setup`, the guided setup.

## Steps

1. Run this, exactly once (the paths are filled in for you):

```bash
# Start the replies page if needed, open it on this chat, and print the local and phone links
REPLIES_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py" open "$(printf '%s' "${CLAUDE_CODE_SESSION_ID:-}" | cut -c1-8)"
```

It prints `{"local", "phone", "running"}` and opens `local` in the browser.

2. Reply with the local link on its own line, the phone link on the next if there is one, then one
   line of status. No phone link: say "Run /replies-setup to connect your phone." If `running` is
   false, say the page couldn't start and suggest `/replies-setup`, which checks why.

## The key

Every link ends in `#k=<key>`. The part after `#` never leaves the browser; the page keeps the key
in that site's own storage, sends it with each request, and drops it from the address bar. The key
is in the plugin data folder (owner-only). It stops other accounts on the same Mac and web pages
from reading chats or typing into them (apps running as you can still read the file). Don't paste
a link with the key anywhere public. If the page says "Open the Replies link…", run `/replies`
again (a new browser or an iPhone home-screen app has its own storage).

## When something isn't working

- **No audio on a reply:** only replies of about 60 spoken words or more get audio. Otherwise
  read `reply-audio.log` in the reply store folder (the `data_dir` in
  `${CLAUDE_PLUGIN_DATA}/config.json`; no reply content is logged). "fallback … say" means no
  local Kokoro server, which is fine on macOS; a failure after that usually means ffmpeg is
  missing — see `setup.log` in the same folder.
- **Mic says Whisper isn't installed:** run `/replies-setup` and choose to install the helpers.
  Speech is transcribed on this Mac only.
- **Can't send to a chat:** the page can only type into chats running inside tmux (or cmux).
  Start Claude with `tmux new -s work claude` to make a chat reachable. A response waits until
  the chat is at an empty prompt, never while a dialog is open. In cmux, Retry on a response held
  for "the prompt box has text in it" types it once, and sends it only if the box holds just it.
- **Phone link doesn't load:** the phone must be signed in to the same tailnet. On iPhone, turn
  off iCloud Private Relay for that network, which skips Tailscale's DNS.
- Never use `tailscale funnel` for this page: it makes it public, and replies carry your code
  and conversation.

Never kill or restart a running server from this skill. If the user asks for a restart after
changing settings: stop the process listening on the page's port (`lsof -ti tcp:8799 -sTCP:LISTEN`, or the port they configured), then run step 1 again. Do it from a cmux terminal
if they use cmux: a server started anywhere else can't reach cmux chats, so messages to them stop
delivering.
