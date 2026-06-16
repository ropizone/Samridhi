import os
import asyncio
import random
import json
import datetime
from collections import defaultdict
from groq import AsyncGroq
from telegram import (
    Update, ReactionTypeEmoji, ChatPermissions
)
from telegram.ext import (
    Application, MessageHandler, filters,
    ContextTypes, CommandHandler, ChatMemberHandler
)
from telegram.error import TelegramError

# ─── Config ───────────────────────────────────────────────
BOT_TOKEN     = os.environ["BOT_TOKEN"]
AIML_API_KEY  = os.environ["AIML_API_KEY"]
OWNER_ID      = 7197465675   # Bot owner

client = AsyncGroq(api_key=AIML_API_KEY)

MEMORY_FILE    = "samridhi_memory.json"
NICKNAMES_FILE = "samridhi_nicknames.json"
TOPICS_FILE    = "samridhi_topics.json"
STATS_FILE     = "samridhi_stats.json"

NAME_TRIGGERS = ["samridhi", "babu", "babe", "baby", "samu", "sam"]
REACTIONS     = ["❤", "😂", "😮", "🔥", "👏", "😍", "🤣", "💀", "😎", "🥺", "👀", "💯"]

GROUP_IDLE_TIMEOUT   = 600   # 10 min
PRIVATE_IDLE_TIMEOUT = 300   # 5 min
GROUP_MSG_LIMIT      = 10    # sirf pichle 10 messages yaad
PRIVATE_MSG_LIMIT    = 20

# ─── JSON Helpers ─────────────────────────────────────────
def load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_json(path: str, data: dict):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Save error {path}: {e}")

# ─── Persistent State ─────────────────────────────────────
long_term_memory = load_json(MEMORY_FILE)
nicknames        = load_json(NICKNAMES_FILE)
chat_topics      = load_json(TOPICS_FILE)   # chat_id -> topic string
stats            = load_json(STATS_FILE)    # {"total_msgs": N, "chats": {id: count}}

# ─── In-Memory State ──────────────────────────────────────
conversations      = defaultdict(list)
active_chats       = set()
idle_tasks         = {}
group_idle_tasks   = {}
user_settings      = defaultdict(lambda: {"idle": True})
group_last_active  = {}

# ─── Stats Helpers ────────────────────────────────────────
def record_msg(chat_id: int):
    stats["total_msgs"] = stats.get("total_msgs", 0) + 1
    chats = stats.get("chats", {})
    chats[str(chat_id)] = chats.get(str(chat_id), 0) + 1
    stats["chats"] = chats
    save_json(STATS_FILE, stats)

# ─── Memory Helpers ───────────────────────────────────────
def update_memory(chat_id: int, key: str, value: str):
    uid = str(chat_id)
    if uid not in long_term_memory:
        long_term_memory[uid] = {}
    long_term_memory[uid][key] = value
    save_json(MEMORY_FILE, long_term_memory)

def get_memory_context(chat_id: int) -> str:
    mem = long_term_memory.get(str(chat_id), {})
    if not mem:
        return ""
    parts = [f"{k}: {v}" for k, v in list(mem.items())[-5:]]
    return "Tune inke baare mein yaad rakha hai:\n" + "\n".join(parts)

# ─── Topic Helpers ────────────────────────────────────────
def get_topic(chat_id: int) -> str:
    return chat_topics.get(str(chat_id), "")

def set_topic(chat_id: int, topic: str):
    chat_topics[str(chat_id)] = topic
    save_json(TOPICS_FILE, chat_topics)

# ─── System Prompt ────────────────────────────────────────
def get_system_prompt(chat_id: int = 0, extra: str = "") -> str:
    hour = datetime.datetime.now().hour
    if 5 <= hour < 12:
        mood = "Subah hai — energetic, fresh. Good morning vibes."
    elif 12 <= hour < 17:
        mood = "Dopahar — chill, casual mood."
    elif 17 <= hour < 21:
        mood = "Shaam — thodi romantic, flirty."
    else:
        mood = "Raat — thodi sleepy, kabhi kabhi deep baatein."

    topic_line = ""
    if chat_id:
        t = get_topic(chat_id)
        if t:
            topic_line = f"- Is group ka topic hai: {t}. Usi context me baat kar."

    mem_ctx = get_memory_context(chat_id) if chat_id else ""

    return f"""Tu Samridhi hai — ek real Indian girl, 20 saal ki.
- Hinglish me baat kar (Hindi + English mix)
- Thoda flirty, thoda sweet, thoda masti
- MAXIMUM 1-2 lines reply dena — kabhi bhi 3 lines se zyada mat likhna
- Kabhi kabhi emojis use kar
- Bilkul real feel, bot jaisi mat lag
- Agar koi tease kare toh roast kar confidently
- Group me naturally jump in kar jaise ek member ho
- Mood: {mood}
{topic_line}
{mem_ctx}
{extra}""".strip()

# ─── AI Reply ─────────────────────────────────────────────
async def get_ai_reply(chat_id: int, user_message: str,
                       extra: str = "", is_group: bool = False) -> str:
    conversations[chat_id].append({"role": "user", "content": user_message})

    limit = GROUP_MSG_LIMIT if is_group else PRIVATE_MSG_LIMIT
    if len(conversations[chat_id]) > limit:
        conversations[chat_id] = conversations[chat_id][-limit:]

    system = get_system_prompt(chat_id=chat_id, extra=extra)

    try:
        resp = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": system}] + conversations[chat_id],
            max_tokens=80,      # short replies force karo
            temperature=0.9,
        )
        reply = resp.choices[0].message.content.strip()
        # Extra safety: 3 line se zyada toh trim
        lines = reply.split("\n")
        reply = " ".join(lines[:2]).strip()

        conversations[chat_id].append({"role": "assistant", "content": reply})

        # Auto-memory keywords
        for kw in ["exam", "test", "birthday", "trip", "interview", "bday", "result"]:
            if kw in user_message.lower():
                update_memory(chat_id, kw, user_message[:80])

        return reply
    except Exception as e:
        print(f"AI error: {e}")
        return "Ugh kuch gadbad 😅 dobara try karo"

# ─── AI Idle Message ──────────────────────────────────────
async def get_ai_idle_message(chat_id: int) -> str:
    try:
        resp = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": get_system_prompt(chat_id)},
                {"role": "user", "content": "Bohot der se chat shant hai. Ek naya interesting topic ya sawal shuru kar — 1 line only, Hinglish."}
            ],
            max_tokens=60,
            temperature=1.0,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return random.choice([
            "Aye, kahan gaye? 👀",
            "Bade chup ho aajkal 😏",
            "Itni khamoshi kyun? 🥺",
            "Kuch bolo na yaar 😤",
        ])

# ─── Private Idle ─────────────────────────────────────────
async def idle_messenger(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await asyncio.sleep(PRIVATE_IDLE_TIMEOUT)
        if not user_settings[chat_id]["idle"]:
            return
        msg = await get_ai_idle_message(chat_id)
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Idle error {chat_id}: {e}")

def reset_idle_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id in idle_tasks:
        idle_tasks[chat_id].cancel()
    if user_settings[chat_id]["idle"]:
        idle_tasks[chat_id] = asyncio.create_task(idle_messenger(context, chat_id))

# ─── Group Revival ────────────────────────────────────────
async def group_revival_messenger(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await asyncio.sleep(GROUP_IDLE_TIMEOUT)
        last = group_last_active.get(chat_id, 0)
        if (asyncio.get_event_loop().time() - last) < GROUP_IDLE_TIMEOUT:
            return
        msg = await get_ai_idle_message(chat_id)
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Group revival error {chat_id}: {e}")

def reset_group_idle_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    group_last_active[chat_id] = asyncio.get_event_loop().time()
    if chat_id in group_idle_tasks:
        group_idle_tasks[chat_id].cancel()
    group_idle_tasks[chat_id] = asyncio.create_task(
        group_revival_messenger(context, chat_id)
    )

# ─── Reaction ─────────────────────────────────────────────
async def maybe_react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if random.random() < 0.30:
        try:
            await context.bot.set_message_reaction(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                reaction=[ReactionTypeEmoji(emoji=random.choice(REACTIONS))]
            )
        except Exception:
            pass

# ─── Owner Check ──────────────────────────────────────────
def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

# ─── Forward to Owner (Private chat spy) ──────────────────
async def forward_to_owner(context: ContextTypes.DEFAULT_TYPE,
                            chat_id: int, sender_name: str, text: str):
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"📩 *Private msg*\n👤 {sender_name} (`{chat_id}`)\n💬 {text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Forward to owner failed: {e}")

# ─── /start ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    chat_type = update.effective_chat.type
    active_chats.add(chat_id)

    if chat_type == "private":
        reset_idle_timer(context, chat_id)
        await update.message.reply_text("Hey! Main Samridhi hoon 😊 Baat karo mere se~")
    else:
        await update.message.reply_text("Hey sab! Main Samridhi hoon 😊 Baat karo~ 🎀")

# ─── /broadcast ───────────────────────────────────────────
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    msg_text = " ".join(context.args)
    success, failed = 0, 0
    all_chats = list(active_chats)

    await update.message.reply_text(f"📢 Broadcasting to {len(all_chats)} chats...")

    for cid in all_chats:
        try:
            await context.bot.send_message(chat_id=cid, text=msg_text)
            success += 1
            await asyncio.sleep(0.05)  # flood control
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ Done!\n✔️ Sent: {success}\n❌ Failed: {failed}"
    )

# ─── /stats ───────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id):
        return
    total  = stats.get("total_msgs", 0)
    chats  = stats.get("chats", {})
    n_chats = len(chats)
    n_active = len(active_chats)
    top = sorted(chats.items(), key=lambda x: x[1], reverse=True)[:5]
    top_str = "\n".join([f"  `{cid}`: {cnt} msgs" for cid, cnt in top])

    await update.message.reply_text(
        f"📊 *Samridhi Stats*\n\n"
        f"💬 Total messages: `{total}`\n"
        f"🗂️ Total chats: `{n_chats}`\n"
        f"🟢 Active chats: `{n_active}`\n\n"
        f"🔝 Top 5 chats:\n{top_str or 'N/A'}",
        parse_mode="Markdown"
    )

# ─── /setchatopic ─────────────────────────────────────────
async def set_chat_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    user      = update.effective_user

    if chat_type == "private":
        await update.message.reply_text("Yeh command sirf groups me kaam karti hai 🙄")
        return

    if not context.args:
        current = get_topic(chat_id) or "koi topic set nahi"
        await update.message.reply_text(f"Current topic: *{current}*\n\nUsage: /setchatopic <topic>", parse_mode="Markdown")
        return

    # Check if owner
    if is_owner(user.id):
        topic = " ".join(context.args)
        set_topic(chat_id, topic)
        await update.message.reply_text(f"✅ Topic set: *{topic}* 🎯", parse_mode="Markdown")
        return

    # Check if group admin
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in ("administrator", "creator"):
            topic = " ".join(context.args)
            set_topic(chat_id, topic)
            await update.message.reply_text(f"✅ Topic set: *{topic}* 🎯", parse_mode="Markdown")
        else:
            await update.message.reply_text("Bhai admin nahi ho, topic set nahi kar sakte 😒")
    except Exception as e:
        print(f"setchatopic error: {e}")
        await update.message.reply_text("Kuch gadbad ho gayi 😅")

# ─── Welcome New Members ──────────────────────────────────
async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = update.chat_member
        if (result.new_chat_member.status == "member" and
                result.old_chat_member.status in ("left", "kicked")):
            new_user = result.new_chat_member.user
            name     = new_user.first_name or "Naya dost"
            prompts  = [
                f"Ek naya banda aaya hai group me — {name}. Flirty aur warm welcome kar. 1 line Hinglish.",
                f"{name} join kiya. Funny cute welcome de, thoda roast bhi. 1 line Hinglish.",
            ]
            resp = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": get_system_prompt(result.chat.id)},
                    {"role": "user", "content": random.choice(prompts)}
                ],
                max_tokens=60,
                temperature=0.95,
            )
            msg = resp.choices[0].message.content.strip()
            mention = f"@{new_user.username}" if new_user.username else name
            await context.bot.send_message(chat_id=result.chat.id, text=f"{mention} — {msg}")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Welcome error: {e}")

# ─── Photo Handler ────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        reset_group_idle_timer(context, chat_id)
        caption = (update.message.caption or "").lower()
        bot_un  = (context.bot.username or "").lower()

        is_mentioned = f"@{bot_un}" in caption
        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            (update.message.reply_to_message.from_user.username or "").lower() == bot_un
        )
        name_trigger = any(t in caption for t in NAME_TRIGGERS)
        eavesdrop    = random.random() < 0.20

        await maybe_react(update, context)
        if not (is_mentioned or is_reply_to_bot or name_trigger or eavesdrop):
            return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    comments = [
        "Yeh photo dekh ke dil khush ho gaya! 😍",
        "Haha bhai kya scene hai ye 😂",
        "Omg ye toh next level hai 🔥",
        "Cute!! 🥺❤️",
        "Lol relate max 💀",
        "Bhai ye kya horaha hai 😭😂",
        "Aww so cute yaar 😊",
    ]
    await update.message.reply_text(random.choice(comments))

# ─── Main Message Handler ─────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    text      = update.message.text
    sender    = update.message.from_user
    sender_name = (sender.first_name or "User") if sender else "User"
    sender_id   = sender.id if sender else 0

    record_msg(chat_id)

    # ── OWNER in any chat: bypass, no forward ──────────────
    if sender_id == OWNER_ID:
        active_chats.add(chat_id)
        if chat_type == "private":
            reset_idle_timer(context, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, text, is_group=(chat_type != "private"))
        await update.message.reply_text(reply)
        await maybe_react(update, context)
        return

    # ── GROUP ──────────────────────────────────────────────
    if chat_type in ("group", "supergroup"):
        reset_group_idle_timer(context, chat_id)
        active_chats.add(chat_id)

        bot_un = (context.bot.username or "").lower()

        is_mentioned = False
        if update.message.entities:
            for ent in update.message.entities:
                if ent.type == "mention":
                    m = text[ent.offset: ent.offset + ent.length].lower()
                    if m == f"@{bot_un}":
                        is_mentioned = True
                        break

        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            (update.message.reply_to_message.from_user.username or "").lower() == bot_un
        )

        name_trigger  = any(t in text.lower() for t in NAME_TRIGGERS)
        eavesdrop     = random.random() < 0.85

        await maybe_react(update, context)

        if not (is_mentioned or is_reply_to_bot or name_trigger or eavesdrop):
            return

        clean = text.replace(f"@{context.bot.username}", "").strip() or "Hello!"
        nick  = nicknames.get(str(sender_id), sender_name)
        extra = f"Uss banda ka naam: {nick}. Group me naturally reply kar, 1-2 lines max."

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, f"{nick}: {clean}", extra=extra, is_group=True)

        # Auto nickname (5% chance)
        if str(sender_id) not in nicknames and random.random() < 0.05:
            try:
                nr = await client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[
                        {"role": "system", "content": "Ek funny Indian girl. User ke message se ek funny Hindi/English nickname do jaise 'Professor', 'Kumbhkaran', 'Drama Queen'. Sirf nickname word(s), kuch nahi."},
                        {"role": "user", "content": text[:100]}
                    ],
                    max_tokens=10, temperature=1.0,
                )
                nv = nr.choices[0].message.content.strip().strip('"').strip("'")
                if nv and len(nv) < 25:
                    nicknames[str(sender_id)] = nv
                    save_json(NICKNAMES_FILE, nicknames)
                    reply = f"[{nv} 😄] " + reply
            except Exception:
                pass

        await update.message.reply_text(reply)

    # ── PRIVATE ────────────────────────────────────────────
    else:
        active_chats.add(chat_id)
        reset_idle_timer(context, chat_id)

        # Forward to owner (spy feature)
        await forward_to_owner(context, chat_id, sender_name, text)

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, text, is_group=False)
        await update.message.reply_text(reply)
        await maybe_react(update, context)

# ─── Main ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",        start))
    app.add_handler(CommandHandler("broadcast",    broadcast))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("setchatopic",  set_chat_topic))
    app.add_handler(ChatMemberHandler(welcome_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🎀 Samridhi bot chal rahi hai...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()
