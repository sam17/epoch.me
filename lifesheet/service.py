#!/usr/bin/env python3
"""Lifesheet Discord service — deterministic daily life-tracking in #lifesheet.

A thin Discord layer over lifesheet.py (the survey state machine). NO LLM, no
judgement: it posts the next due question, records replies, advances. Context
comes from lifesheet.py's state file, never from chat history — so leftover
messages in the channel are irrelevant by design.

Env: DISCORD_BOT_TOKEN, DATABASE_URL, LIFESHEET_CHANNEL_ID,
     LIFESHEET_SCRIPT (default /app/lifesheet.py), LIFESHEET_STATE (state path).
"""
import os
import sys
import json
import asyncio
import subprocess
import datetime
import logging

import discord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lifesheet")

from zoneinfo import ZoneInfo
IST = ZoneInfo(os.environ.get("LIFESHEET_TZ", "Asia/Kolkata"))  # travel-configurable timezone (was hardcoded IST). Name kept as IST for compatibility.
CHANNEL_ID = int(os.environ["LIFESHEET_CHANNEL_ID"])
SCRIPT = os.environ.get("LIFESHEET_SCRIPT", "/app/lifesheet.py")

# Telegram-style relative scheduler: hourly, prompt whatever is overdue (per lifesheet.py `due`).
# Telegram's scheduler.js ran 24/7; we restrict to waking hours so no overnight pings.
WAKING_HOURS = set(range(7, 24))  # 7am–11pm IST


def ls(*args):
    """Run lifesheet.py and return parsed JSON (last stdout line), or None."""
    try:
        r = subprocess.run([sys.executable, SCRIPT, *args],
                           capture_output=True, text=True, timeout=30)
        out = (r.stdout or "").strip()
        if not out:
            log.warning("lifesheet %s -> empty (err: %s)", args, (r.stderr or "")[-200:])
            return None
        return json.loads(out.splitlines()[-1])
    except Exception as e:
        log.exception("lifesheet %s failed: %s", args, e)
        return None


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def post_action(channel, action):
    """Post a question from a tick/answer-next action dict; stay silent on wait/idle."""
    if action and action.get("action") == "ask" and action.get("post"):
        await channel.send(action["post"])


@client.event
async def on_ready():
    log.info("logged in as %s (id %s)", client.user, getattr(client.user, "id", "?"))
    ch = client.get_channel(CHANNEL_ID)
    if ch is None:
        log.error("cannot see channel %s — is the bot invited with access?", CHANNEL_ID)
    if not getattr(client, "_sched_started", False):
        client._sched_started = True
        client.loop.create_task(scheduler())


SURVEY_CMDS = {"awake", "asleep", "week", "workout"}  # whoop excluded: auto-collected
HELP = "Commands: `/awake` `/asleep` `/week` `/workout` `/mood` `/skip` `/status`"


async def handle_command(channel, cmd):
    if cmd in SURVEY_CMDS:
        enq = ls("enqueue", cmd) or {}
        active = enq.get("active")
        queue = enq.get("queue") or []
        action = ls("tick")
        if action and action.get("action") == "ask":
            if action.get("survey") != cmd:
                await channel.send(f"Added **{cmd}** to the queue — finishing **{action.get('survey')}** first.")
            await post_action(channel, action)
        else:
            cur = ls("current") or {}
            if cur.get("pending"):
                if cur.get("survey") == cmd:
                    await channel.send(f"You're already on **{cmd}**:\n{cur.get('post', '')}")
                else:
                    await channel.send(f"Added **{cmd}** to the queue — answer **{cur.get('survey')}** first:\n{cur.get('post', '')}")
            elif cmd == active or cmd in queue:
                await channel.send(f"**{cmd}** is queued — I'll start it shortly.")
            else:
                await channel.send(f"**{cmd}** is already done for today. ✅")
    elif cmd == "mood":
        action = ls("tick", "--mood")
        if action and action.get("action") == "ask":
            await post_action(channel, action)
        else:
            await channel.send("Can't start mood right now — finish the current survey first.")
    elif cmd == "skip":
        nxt = (ls("skip") or {}).get("next") or {}
        if nxt.get("action") == "ask":
            await post_action(channel, nxt)
        else:
            await channel.send("Skipped. Nothing else pending right now. ✅")
    elif cmd in ("status", "pending"):
        cur = ls("current")
        if cur and cur.get("pending"):
            await channel.send(f"Currently on **{cur['survey']}**:\n{cur['post']}")
        else:
            await channel.send(f"Nothing pending right now. {HELP}")
    elif cmd in ("help", "commands"):
        await channel.send(HELP)
    else:
        await channel.send(f"Unknown command. {HELP}")


@client.event
async def on_message(message):
    if message.author.bot or message.channel.id != CHANNEL_ID:
        return
    text = (message.content or "").strip()
    if not text:
        return
    if text.startswith("/"):                       # Telegram-style slash commands
        await handle_command(message.channel, text[1:].split()[0].lower())
        return
    res = ls("answer", text)                        # otherwise it's an answer
    if res is None:
        return
    if res.get("ok") is False:
        if res.get("post"):                         # validation error -> re-ask
            await message.channel.send(res["post"])
        return
    done = res.get("done_survey")
    nxt = res.get("next") or {}
    if done:
        await message.channel.send("Mood logged ✅" if done == "mood"
                                   else f"**{done}** done — nice work 💪")
    if nxt.get("action") == "ask":
        await message.channel.send(nxt.get("post") or "")
    elif done:
        await message.channel.send("That's everything for now. ✅")


async def scheduler():
    await client.wait_until_ready()
    ch = client.get_channel(CHANNEL_ID)
    last_hour = None
    while not client.is_closed():
        now = datetime.datetime.now(IST)
        hour_key = now.strftime("%Y-%m-%d %H")
        if now.minute == 0 and hour_key != last_hour and now.hour in WAKING_HOURS:
            last_hour = hour_key
            if ch is None:
                ch = client.get_channel(CHANNEL_ID)
            due = ls("due") or {}
            for survey in due.get("overdue", []):       # start any overdue survey
                ls("enqueue", survey)
            action = ls("tick", "--mood") if due.get("mood_due") else ls("tick")
            if ch is not None:
                await post_action(ch, action)
        await asyncio.sleep(20)


if __name__ == "__main__":
    client.run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)
