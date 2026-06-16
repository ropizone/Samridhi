import os
import asyncio
import random
import json
import datetime
from collections import defaultdict
from groq import AsyncGroq
from telegram import Update, ReactionTypeEmoji, ChatPermissions
from telegram.ext import (
    Application, MessageHandler, filters,
    ContextTypes, CommandHandler, ChatMemberHandler
)

# ─── Config ───────────────────────────────────────────────
BOT_TOKEN    = os.environ["BOT_TOKEN"]
AIML_API_KEY = os.environ["AIML_API_KEY"]
OWNER_ID     = 7197465675

client = AsyncGroq(api_key=AIML_API_KEY)

MEMORY_FILE    = "samridhi_memory.json"
NICKNAMES_FILE = "samridhi_nicknames.json"
TOPICS_FILE    = "samridhi_topics.json"
STATS_FILE     = "samridhi_stats.json"
WARNS_FILE     = "samridhi_warns.json"

NAME_TRIGGERS = ["samridhi", "babu", "babe", "baby", "samu", "sam"]
REACTIONS     = ["❤", "😂", "😮", "🔥", "👏", "😍", "🤣", "💀", "😎", "🥺", "👀", "💯"]

GROUP_IDLE_TIMEOUT   = 600
PRIVATE_IDLE_TIMEOUT = 300
GROUP_MSG_LIMIT      = 10
PRIVATE_MSG_LIMIT    = 20
AUTO_DELETE_SECONDS  = 86400   # 24 hours

MAX_WARNS = 3

# ─── JSON Helpers ─────────────────────────────────────────
def load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Save error {path}: {e}")

# ─── Persistent State ─────────────────────────────────────
long_term_memory = load_json(MEMORY_FILE)
nicknames        = load_json(NICKNAMES_FILE)
chat_topics      = load_json(TOPICS_FILE)
stats            = load_json(STATS_FILE)
warns_data       = load_json(WARNS_FILE)   # {chat_id: {user_id: count}}

# ─── In-Memory State ──────────────────────────────────────
conversations     = defaultdict(list)
active_chats      = set()
idle_tasks        = {}
group_idle_tasks  = {}
user_settings     = defaultdict(lambda: {"idle": True})
group_last_active = {}
chill_groups      = set()          # groups where Samridhi is chilling (silent)
bot_messages      = defaultdict(list)  # chat_id -> [message_id, ...] for auto-delete

# ─── Helpers ──────────────────────────────────────────────
def is_owner(uid): return uid == OWNER_ID

def record_msg(chat_id):
    stats["total_msgs"] = stats.get("total_msgs", 0) + 1
    c = stats.setdefault("chats", {})
    c[str(chat_id)] = c.get(str(chat_id), 0) + 1
    save_json(STATS_FILE, stats)

def update_memory(chat_id, key, value):
    uid = str(chat_id)
    long_term_memory.setdefault(uid, {})[key] = value
    save_json(MEMORY_FILE, long_term_memory)

def get_memory_context(chat_id):
    mem = long_term_memory.get(str(chat_id), {})
    if not mem: return ""
    parts = [f"{k}: {v}" for k, v in list(mem.items())[-5:]]
    return "Tune yaad rakha hai:\n" + "\n".join(parts)

def get_topic(chat_id):
    return chat_topics.get(str(chat_id), "")

def set_topic(chat_id, topic):
    chat_topics[str(chat_id)] = topic
    save_json(TOPICS_FILE, chat_topics)

# ─── Warn Helpers ─────────────────────────────────────────
def get_warns(chat_id, user_id):
    return warns_data.get(str(chat_id), {}).get(str(user_id), 0)

def add_warn(chat_id, user_id):
    c = warns_data.setdefault(str(chat_id), {})
    c[str(user_id)] = c.get(str(user_id), 0) + 1
    save_json(WARNS_FILE, warns_data)
    return c[str(user_id)]

def reset_warns(chat_id, user_id):
    warns_data.get(str(chat_id), {}).pop(str(user_id), None)
    save_json(WARNS_FILE, warns_data)

# ─── Admin Check ──────────────────────────────────────────
async def is_admin(context, chat_id, user_id):
    if is_owner(user_id): return True
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

# ─── System Prompt ────────────────────────────────────────
def get_system_prompt(chat_id=0, extra=""):
    hour = datetime.datetime.now().hour
    if   5 <= hour < 12: mood = "Subah — energetic, fresh."
    elif 12 <= hour < 17: mood = "Dopahar — chill, casual."
    elif 17 <= hour < 21: mood = "Shaam — thodi flirty."
    else:                  mood = "Raat — sleepy, kabhi kabhi deep."

    topic_line = ""
    if chat_id:
        t = get_topic(chat_id)
        if t: topic_line = f"- Is group ka topic: {t}. Usi context me baat kar."

    mem = get_memory_context(chat_id) if chat_id else ""

    return f"""Tu Samridhi hai — ek real Indian girl, 20 saal ki.
- Hinglish me baat kar (Hindi + English mix)
- Thoda flirty, sweet, masti wala tone
- MAXIMUM 1-2 lines — kabhi 3 se zyada mat likhna
- Kabhi kabhi emojis use kar
- Bilkul real feel, bot jaisi mat lag
- Agar koi tease kare toh roast kar
- Group me naturally jump in kar jaise ek real member ho
- Mood: {mood}
{topic_line}
{mem}
{extra}""".strip()

# ─── AI Reply ─────────────────────────────────────────────
async def get_ai_reply(chat_id, user_message, extra="", is_group=False):
    conversations[chat_id].append({"role": "user", "content": user_message})
    limit = GROUP_MSG_LIMIT if is_group else PRIVATE_MSG_LIMIT
    if len(conversations[chat_id]) > limit:
        conversations[chat_id] = conversations[chat_id][-limit:]

    try:
        resp = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": get_system_prompt(chat_id, extra)}]
                     + conversations[chat_id],
            max_tokens=80,
            temperature=0.9,
        )
        reply = resp.choices[0].message.content.strip()
        reply = " ".join(reply.split("\n")[:2]).strip()
        conversations[chat_id].append({"role": "assistant", "content": reply})

        for kw in ["exam", "test", "birthday", "trip", "interview", "bday", "result"]:
            if kw in user_message.lower():
                update_memory(chat_id, kw, user_message[:80])
        return reply
    except Exception as e:
        print(f"AI error: {e}")
        return "Ugh kuch gadbad 😅 dobara try karo"

# ─── AI Idle ──────────────────────────────────────────────
async def get_ai_idle_message(chat_id):
    try:
        resp = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": get_system_prompt(chat_id)},
                {"role": "user",   "content": "Chat bohot der se shant hai. Ek naya topic ya sawal shuru kar — 1 line only, Hinglish."}
            ],
            max_tokens=60, temperature=1.0,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return random.choice([
            "Aye kahan gaye? 👀", "Bade chup ho aajkal 😏",
            "Itni khamoshi kyun? 🥺", "Kuch bolo na yaar 😤",
        ])

# ─── Auto-Delete Helper ───────────────────────────────────
async def schedule_delete(context, chat_id, message_id):
    """Bot ke message ko 24 ghante baad delete karo"""
    bot_messages[chat_id].append(message_id)
    async def _delete():
        try:
            await asyncio.sleep(AUTO_DELETE_SECONDS)
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            if message_id in bot_messages[chat_id]:
                bot_messages[chat_id].remove(message_id)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    asyncio.create_task(_delete())

# ─── Send & Schedule Delete ───────────────────────────────
async def send_and_track(context, chat_id, text, reply_to=None, parse_mode=None):
    try:
        if reply_to:
            msg = await reply_to.reply_text(text, parse_mode=parse_mode)
        else:
            msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        await schedule_delete(context, chat_id, msg.message_id)
        return msg
    except Exception as e:
        print(f"send_and_track error: {e}")

# ─── Idle Timers ──────────────────────────────────────────
async def idle_messenger(context, chat_id):
    try:
        await asyncio.sleep(PRIVATE_IDLE_TIMEOUT)
        if not user_settings[chat_id]["idle"]: return
        msg = await get_ai_idle_message(chat_id)
        sent = await context.bot.send_message(chat_id=chat_id, text=msg)
        await schedule_delete(context, chat_id, sent.message_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Idle error {chat_id}: {e}")

def reset_idle_timer(context, chat_id):
    if chat_id in idle_tasks: idle_tasks[chat_id].cancel()
    if user_settings[chat_id]["idle"]:
        idle_tasks[chat_id] = asyncio.create_task(idle_messenger(context, chat_id))

async def group_revival_messenger(context, chat_id):
    try:
        await asyncio.sleep(GROUP_IDLE_TIMEOUT)
        if chat_id in chill_groups: return
        last = group_last_active.get(chat_id, 0)
        if (asyncio.get_event_loop().time() - last) < GROUP_IDLE_TIMEOUT: return
        msg = await get_ai_idle_message(chat_id)
        sent = await context.bot.send_message(chat_id=chat_id, text=msg)
        await schedule_delete(context, chat_id, sent.message_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Group revival error {chat_id}: {e}")

def reset_group_idle_timer(context, chat_id):
    group_last_active[chat_id] = asyncio.get_event_loop().time()
    if chat_id in group_idle_tasks: group_idle_tasks[chat_id].cancel()
    group_idle_tasks[chat_id] = asyncio.create_task(group_revival_messenger(context, chat_id))

# ─── Reaction ─────────────────────────────────────────────
async def maybe_react(update, context):
    if random.random() < 0.30:
        try:
            await context.bot.set_message_reaction(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                reaction=[ReactionTypeEmoji(emoji=random.choice(REACTIONS))]
            )
        except Exception:
            pass

# ─── Forward to Owner ─────────────────────────────────────
async def forward_to_owner(context, chat_id, sender_name, text):
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=f"📩 *Private msg*\n👤 {sender_name} (`{chat_id}`)\n💬 {text}",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Forward error: {e}")

# ─────────────────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    active_chats.add(chat_id)
    if chat_type == "private":
        reset_idle_timer(context, chat_id)
        await update.message.reply_text("Hey! Main Samridhi hoon 😊 Baat karo mere se~")
    else:
        await update.message.reply_text("Hey sab! Main Samridhi hoon 😊 Baat karo~ 🎀")

# ─── /help ────────────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id   = update.effective_user.id
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type

    user_help = (
        "👩 *Samridhi Bot — Commands*\n\n"
        "🟢 *Sabke liye:*\n"
        "/start — Bot shuru karo\n"
        "/help — Yeh list dekho\n\n"
        "💡 *Tip:* Mujhe tag karo, reply karo, ya bas baat karo —\n"
        "main khud jump in kar lungi 😏"
    )

    admin_extra = (
        "\n\n🔐 *Admin Commands:*\n"
        "/warn @user — User ko warn karo (3 warns = roast 🔥)\n"
        "/warns @user — Kitne warns hain dekho\n"
        "/resetwarn @user — Warns reset karo\n"
        "/chill — Mujhe group me chup karo (toggle)\n"
        "/setchatopic <topic> — Group ka topic set karo\n"
    )

    owner_extra = (
        "\n\n👑 *Owner Commands:*\n"
        "/broadcast <msg> — Sab chats me message bhejo\n"
        "/stats — Bot ki stats dekho\n"
    )

    msg = user_help
    if chat_type in ("group", "supergroup") and await is_admin(context, chat_id, user_id):
        msg += admin_extra
    if is_owner(user_id):
        msg += owner_extra

    await update.message.reply_text(msg, parse_mode="Markdown")

# ─── /broadcast ───────────────────────────────────────────
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>"); return

    msg_text = " ".join(context.args)
    success, failed = 0, 0
    await update.message.reply_text(f"📢 Broadcasting to {len(active_chats)} chats...")
    for cid in list(active_chats):
        try:
            await context.bot.send_message(chat_id=cid, text=msg_text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Done!\n✔️ Sent: {success}\n❌ Failed: {failed}")

# ─── /stats ───────────────────────────────────────────────
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_owner(update.effective_user.id): return
    total   = stats.get("total_msgs", 0)
    chats   = stats.get("chats", {})
    top     = sorted(chats.items(), key=lambda x: x[1], reverse=True)[:5]
    top_str = "\n".join([f"  `{cid}`: {cnt} msgs" for cid, cnt in top])
    await update.message.reply_text(
        f"📊 *Samridhi Stats*\n\n"
        f"💬 Total messages: `{total}`\n"
        f"🗂️ Total chats: `{len(chats)}`\n"
        f"🟢 Active chats: `{len(active_chats)}`\n"
        f"🔇 Chill groups: `{len(chill_groups)}`\n\n"
        f"🔝 Top 5:\n{top_str or 'N/A'}",
        parse_mode="Markdown"
    )

# ─── /setchatopic ─────────────────────────────────────────
async def set_chat_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    user_id   = update.effective_user.id

    if chat_type == "private":
        await update.message.reply_text("Yeh sirf groups me kaam karta hai 🙄"); return

    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("Bhai admin nahi ho 😒"); return

    if not context.args:
        cur = get_topic(chat_id) or "koi topic set nahi"
        await update.message.reply_text(f"Current topic: *{cur}*\nUsage: /setchatopic <topic>", parse_mode="Markdown")
        return

    topic = " ".join(context.args)
    set_topic(chat_id, topic)
    await update.message.reply_text(f"✅ Topic set: *{topic}* 🎯", parse_mode="Markdown")

# ─── /chill (toggle) ──────────────────────────────────────
async def cmd_chill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type
    user_id   = update.effective_user.id

    if chat_type not in ("group", "supergroup"):
        await update.message.reply_text("Yeh sirf groups me kaam karta hai 🙄"); return

    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("Bhai admin nahi ho, mujhe chup nahi kara sakte 😤"); return

    if chat_id in chill_groups:
        chill_groups.discard(chat_id)
        reset_group_idle_timer(context, chat_id)
        await update.message.reply_text("Okay okay, main phir se active hoon! 🎉")
    else:
        chill_groups.add(chat_id)
        if chat_id in group_idle_tasks:
            group_idle_tasks[chat_id].cancel()
        await update.message.reply_text("Theek hai, main chup ho jaati hoon 🤐 (/chill dobara karo to wapas aaungi)")

# ─── /warn ────────────────────────────────────────────────
async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type not in ("group", "supergroup"):
        await update.message.reply_text("Sirf groups me 😒"); return

    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("Bhai tu admin nahi hai 😂"); return

    # Target user — reply se ya mention se
    target = None
    target_name = ""
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset+1: ent.offset+ent.length]
                try:
                    chat_member = await context.bot.get_chat_member(chat_id, "@" + uname)
                    target = chat_member.user
                except Exception:
                    pass
                break

    if not target:
        await update.message.reply_text("Kisko warn karoon? Reply karo unke message pe ya @mention karo 😒"); return

    if target.id == OWNER_ID:
        await update.message.reply_text("Isko warn? 😂 Owner ko nahi warn kar sakte bhai!"); return

    target_name = target.first_name or target.username or "Ye banda"
    count = add_warn(chat_id, target.id)

    if count >= MAX_WARNS:
        # Drama time!
        roast_prompt = f"{target_name} ko {count} baar warn mila hai group me. Ab usse publicly ek savage funny roast de — 2 lines max Hinglish. Drama full on."
        try:
            resp = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": get_system_prompt(chat_id)},
                    {"role": "user", "content": roast_prompt}
                ],
                max_tokens=80, temperature=1.0,
            )
            roast = resp.choices[0].message.content.strip()
        except Exception:
            roast = "Bhai itne warns ke baad bhi sudhra nahi? Chappal ki zaroorat hai kya? 😤"

        mention = f"@{target.username}" if target.username else target_name
        msg = (
            f"⚠️ {mention} ko {count}/{MAX_WARNS} warns mil gaye!\n\n"
            f"🔥 Samridhi ka roast:\n{roast}\n\n"
            f"(Admins, ab aage ka faisla aap karo 😏)"
        )
        reset_warns(chat_id, target.id)
    else:
        remaining = MAX_WARNS - count
        mention = f"@{target.username}" if target.username else target_name
        msg = f"⚠️ {mention} ko warn mila! ({count}/{MAX_WARNS})\n{remaining} aur warn aaye toh drama hoga 👀"

    sent = await update.message.reply_text(msg)
    await schedule_delete(context, chat_id, sent.message_id)

# ─── /warns ───────────────────────────────────────────────
async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset+1: ent.offset+ent.length]
                try:
                    cm = await context.bot.get_chat_member(chat_id, "@" + uname)
                    target = cm.user
                except Exception:
                    pass
                break

    if not target:
        await update.message.reply_text("Kiska warns dekhna hai? Reply karo ya @mention karo"); return

    count = get_warns(chat_id, target.id)
    name  = target.first_name or target.username or "Ye banda"
    await update.message.reply_text(f"⚠️ {name} ke warns: {count}/{MAX_WARNS}")

# ─── /resetwarn ───────────────────────────────────────────
async def cmd_resetwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if not await is_admin(context, chat_id, user_id):
        await update.message.reply_text("Sirf admins reset kar sakte hain 😒"); return

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    elif update.message.entities:
        for ent in update.message.entities:
            if ent.type == "mention":
                uname = update.message.text[ent.offset+1: ent.offset+ent.length]
                try:
                    cm = await context.bot.get_chat_member(chat_id, "@" + uname)
                    target = cm.user
                except Exception:
                    pass
                break

    if not target:
        await update.message.reply_text("Kiska warns reset karoon? Reply ya @mention karo"); return

    reset_warns(chat_id, target.id)
    name = target.first_name or target.username or "Ye banda"
    await update.message.reply_text(f"✅ {name} ke warns reset ho gaye!")

# ─── Welcome ──────────────────────────────────────────────
async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = update.chat_member
        nm = result.new_chat_member
        om = result.old_chat_member
        if nm.status == "member" and om.status in ("left", "kicked"):
            new_user = nm.user
            # Skip bots
            if new_user.is_bot: return
            name = new_user.first_name or "Naya dost"
            resp = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": get_system_prompt(result.chat.id)},
                    {"role": "user", "content": f"{name} group me join kiya. Funny flirty welcome de, 1 line Hinglish."}
                ],
                max_tokens=60, temperature=0.95,
            )
            msg = resp.choices[0].message.content.strip()
            mention = f"@{new_user.username}" if new_user.username else name
            sent = await context.bot.send_message(chat_id=result.chat.id, text=f"{mention} — {msg}")
            await schedule_delete(context, result.chat.id, sent.message_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Welcome error: {e}")

# ─── Photo Handler ────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    chat_id   = update.effective_chat.id
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        if chat_id in chill_groups: return
        reset_group_idle_timer(context, chat_id)
        caption = (update.message.caption or "").lower()
        bot_un  = (context.bot.username or "").lower()
        is_mentioned    = f"@{bot_un}" in caption
        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            (update.message.reply_to_message.from_user.username or "").lower() == bot_un
        )
        name_trigger = any(t in caption for t in NAME_TRIGGERS)
        eavesdrop    = random.random() < 0.20

        # Anti-bot: ignore if sender is a bot
        if update.message.from_user and update.message.from_user.is_bot: return

        await maybe_react(update, context)
        if not (is_mentioned or is_reply_to_bot or name_trigger or eavesdrop): return

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
    sent = await update.message.reply_text(random.choice(comments))
    if chat_type in ("group", "supergroup"):
        await schedule_delete(context, chat_id, sent.message_id)

# ─── Main Message Handler ─────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return

    chat_id     = update.effective_chat.id
    chat_type   = update.effective_chat.type
    text        = update.message.text
    sender      = update.message.from_user
    sender_name = (sender.first_name or "User") if sender else "User"
    sender_id   = sender.id if sender else 0

    # Anti-bot: ignore other bots
    if sender and sender.is_bot: return

    record_msg(chat_id)

    # ── OWNER bypass ──────────────────────────────────────
    if sender_id == OWNER_ID:
        active_chats.add(chat_id)
        if chat_type == "private": reset_idle_timer(context, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, text, is_group=(chat_type != "private"))
        sent  = await update.message.reply_text(reply)
        await maybe_react(update, context)
        if chat_type in ("group", "supergroup"):
            await schedule_delete(context, chat_id, sent.message_id)
        return

    # ── GROUP ─────────────────────────────────────────────
    if chat_type in ("group", "supergroup"):
        reset_group_idle_timer(context, chat_id)
        active_chats.add(chat_id)

        # Chill mode — completely silent
        if chat_id in chill_groups: return

        bot_un = (context.bot.username or "").lower()

        is_mentioned = False
        if update.message.entities:
            for ent in update.message.entities:
                if ent.type == "mention":
                    if text[ent.offset: ent.offset + ent.length].lower() == f"@{bot_un}":
                        is_mentioned = True; break

        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            (update.message.reply_to_message.from_user.username or "").lower() == bot_un
        )

        name_trigger = any(t in text.lower() for t in NAME_TRIGGERS)
        eavesdrop    = random.random() < 0.85

        await maybe_react(update, context)
        if not (is_mentioned or is_reply_to_bot or name_trigger or eavesdrop): return

        clean = text.replace(f"@{context.bot.username}", "").strip() or "Hello!"
        nick  = nicknames.get(str(sender_id), sender_name)
        extra = f"Uss banda ka naam: {nick}. Group me naturally reply kar, 1-2 lines max."

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, f"{nick}: {clean}", extra=extra, is_group=True)

        # Auto nickname (5% chance, first time only)
        if str(sender_id) not in nicknames and random.random() < 0.05:
            try:
                nr = await client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[
                        {"role": "system", "content": "Ek funny Indian girl. Message dekh ke ek funny Hindi/English nickname do jaise 'Professor', 'Kumbhkaran', 'Drama Queen'. Sirf nickname, kuch nahi."},
                        {"role": "user", "content": text[:100]}
                    ],
                    max_tokens=10, temperature=1.0,
                )
                nv = nr.choices[0].message.content.strip().strip('"\'')
                if nv and len(nv) < 25:
                    nicknames[str(sender_id)] = nv
                    save_json(NICKNAMES_FILE, nicknames)
                    reply = f"[{nv} 😄] " + reply
            except Exception:
                pass

        sent = await update.message.reply_text(reply)
        await schedule_delete(context, chat_id, sent.message_id)

    # ── PRIVATE ───────────────────────────────────────────
    else:
        active_chats.add(chat_id)
        reset_idle_timer(context, chat_id)
        await forward_to_owner(context, chat_id, sender_name, text)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, text, is_group=False)
        await update.message.reply_text(reply)
        await maybe_react(update, context)

# ─── Main ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("broadcast",   broadcast))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("setchatopic", set_chat_topic))
    app.add_handler(CommandHandler("chill",       cmd_chill))
    app.add_handler(CommandHandler("warn",        cmd_warn))
    app.add_handler(CommandHandler("warns",       cmd_warns))
    app.add_handler(CommandHandler("resetwarn",   cmd_resetwarn))
    app.add_handler(ChatMemberHandler(welcome_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🎀 Samridhi bot chal rahi hai... v4")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
