# Needs-human notifications

honestapply can **ping your phone** when the apply agent hits something only a
human should handle — a **CAPTCHA**, a **login wall**, or a **field it can't
answer truthfully**. You solve it yourself in the open browser, and the **same run
continues** (no restart). It's opt-in, fully local, and has no third-party browser.

This is deliberately *not* a CAPTCHA auto-solver — a human (you) solves it, which
is exactly what a CAPTCHA is for, and it keeps honestapply honest and ban-safe.

## How it works
1. The agent hits a CAPTCHA/needs-human step and emits a `NOTIFY` signal.
2. honestapply sends you a **Telegram** (or **ntfy**) message.
3. You solve it in the browser honestapply already has open on your machine.
4. The agent sees it clear and **resumes** → submits.
5. If you don't get to it within the window (default 4 min), it falls back to
   `needs_human`, as before.

## Setup (Telegram)
1. Message **@BotFather** → `/newbot` → copy the **token**.
2. Message your bot once, then read your **chat id** from
   `https://api.telegram.org/bot<token>/getUpdates` (`chat.id`).
3. Add to `.env`:
   ```bash
   HONESTAPPLY_NOTIFY_PROVIDER=telegram
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```

### Or ntfy (self-hostable, no account)
```bash
HONESTAPPLY_NOTIFY_PROVIDER=ntfy
NTFY_TOPIC=<a long random topic>     # public ntfy.sh topics are world-readable
# NTFY_URL=https://ntfy.sh           # or your self-hosted ntfy
```

### Check it
```bash
honestapply doctor   # → "Notifications: telegram"
```

## Tuning
- `HONESTAPPLY_CAPTCHA_WAIT_SECONDS` (default 240) — how long the agent waits for
  you to solve before falling back to `needs_human`.

Notifications fire for any needs-human stop, so honestapply can run mostly
unattended and only tap your shoulder when it genuinely needs you.
