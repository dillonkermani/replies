---
name: replies-setup
description: >-
  Guided, interactive setup for the replies plugin on this Mac and the user's phone: checks every
  requirement, installs the missing helpers with the user's OK, connects Tailscale for phone
  access, walks through opening it on a phone, and finishes with a spoken test reply. Use when
  the user runs /replies-setup, just installed the replies plugin, the startup message says
  "Not set up yet", or they ask how to set up replies, the phone link, or Tailscale for it.
---

# replies-setup

You are walking someone through setting up spoken replies. Assume they are not technical. Keep
each message short, one step at a time, plain words, no jargon they didn't use first. Do the
technical work yourself; only ask them for choices and for the few things that have to happen on
their side (signing in to an app, a setting on their phone).

All checks and fixes go through one script. Run it exactly like this (the paths are filled in for
you):

```bash
# Replies setup helper: check | install ITEM... | phone | test | links | open
REPLIES_PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}" python3 -I "${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py" check
```

`check` prints a JSON list; each item has `id`, `ok`, `label`, `detail`, `fix`, `optional`.

## Steps

### 1. Check

Run `check`. Show the result as a short checklist (✓ done, ✗ needed, ○ optional and missing), one
line each, using `label` and `detail`. Don't show items' JSON or paths unless asked.

Stop and explain if a required item fails (`optional: false`):
- `macos`: this plugin only works on a Mac for now.
- `python` / `claude`: tell them the one fix line, ask them to run it, then re-run `check`.
- `server`: say it starts with each new Claude Code session; ask them to open a new session and
  run `/replies-setup` there. If `detail` says the port answers as something else, explain
  another app uses port 8799 and they can change the `port` option with
  `claude plugin configure replies@replies`.

### 2. Helpers (one question)

If any of `ffmpeg`, `whisper`, `model`, `tmux` is missing, ask with AskUserQuestion:

- **Install everything (Recommended)**: ffmpeg (makes the audio), whisper.cpp + a ~150 MB speech
  model (lets them talk instead of type, all on this Mac), tmux (lets the page send messages into
  their Claude chats). Uses Homebrew.
- **Just the audio**: only ffmpeg. They can listen to replies but not talk back by voice.
- **Skip for now**: nothing is installed; replies may have no audio.

If `ffmpeg` is the only thing missing, still ask, but with just Install / Skip.
If Homebrew (`brew`) is missing and they chose to install, tell them to install it from
https://brew.sh (one command from that page, in Terminal), wait for them to say done (ask with
AskUserQuestion: Done / Skip helpers), re-run `check`, then continue.

Then run `install` with the chosen items, e.g. `... doctor.py install ffmpeg whisper model tmux`.
It prints progress; it can take a few minutes. Say so before you start. Report the result in one
line. If something failed, show the last lines of its output and offer to retry once.

### 3. Phone access with Tailscale

Explain in two sentences: Tailscale is a free private network between their own devices. With it,
their phone can open this page securely from anywhere, and nothing is put on the public internet.

Ask with AskUserQuestion: **Set up phone access (Recommended)** / **Skip, this Mac only**.

If they want it, go through these in order. After each thing they must do themselves, wait for
them with AskUserQuestion (**Done** / **Stuck** / **Skip phone setup**), then re-run `check`:

1. `tailscale` not ok → install the Mac app from https://tailscale.com/download/mac (or the Mac
   App Store), open it, and allow the VPN configuration when macOS asks.
2. `tailscale_login` not ok → click the Tailscale icon in the menu bar → Log in. Any account works
   (Google, Apple, Microsoft, GitHub). Tell them to remember which one; the phone must use the
   same account.
3. `magicdns` or `https` not ok → open https://login.tailscale.com/admin/dns, turn on **MagicDNS**
   if it's off, then under **HTTPS Certificates** click **Enable**. (HTTPS is what lets the phone
   browser use its microphone.)
4. Run `phone`. It prints `{"url", "note"}`. If `url` is empty, show `note` and help with that one
   thing, then run `phone` again. Never run `tailscale funnel`: that would make the page public.

### 4. On the phone

Give these steps, numbered, in one message. Use the account name from the `tailscale_login`
detail in step 2:

1. Install **Tailscale** from the App Store (iPhone) or Google Play (Android).
2. Open it and sign in with the **same account** as the Mac (say which one). Turn it on.
3. iPhone only: Settings → your name → iCloud → **Private Relay** → off (or turn it off just for
   the current Wi-Fi). It hides Tailscale's addresses and the link won't open.
4. Open the phone link in Safari or Chrome. Send it to yourself (AirDrop, Messages, Notes) rather
   than typing it, because it ends with a long private key.
5. Tap the mic once and allow microphone access. Tap again to stop.
6. Optional: add it to the home screen (Share → Add to Home Screen). The home-screen app keeps
   its own storage, so the first time it opens, paste the same link into the box it shows.

Print the phone link on its own line (from `links` or the `phone` result). Tell them not to post
it anywhere: the part after `#k=` is a private key.

### 5. Test

Say you'll make one spoken test reply (it uses one short Claude request on their account). Run
`test`. If `ok` is true, run `open` to open the page in their browser, and tell them to press play
on the newest card titled with `title`. If it failed, read the last lines of the log `test`
names (`reply-audio.log` in the reply store folder) and fix what it says (usually ffmpeg missing).

### 6. Replying from the page (one tip)

The page can only type into chats that run inside tmux. If tmux is installed, tell them: start
Claude with `tmux new -s claude claude` when they want to answer from the phone. A message waits
until Claude is at an empty prompt, and never types into an open question or permission dialog.

### 7. Wrap up

One short summary: what works now (listen / talk back / phone), what was skipped and how to add
it later (run `/replies-setup` again any time). Mention `/replies` opens the page on the current
chat.

## Rules

- Never change Tailscale settings beyond the one `tailscale serve` that `phone` runs. Never use
  `tailscale funnel`.
- Never install anything they didn't choose.
- Never print the key on its own or in any other place than the links above.
- If they're stuck on a step for more than one retry, offer to skip it and continue; everything
  else still works without the phone.
