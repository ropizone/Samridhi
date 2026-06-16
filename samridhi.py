import os
import asyncio
import random
import json
import datetime
from collections import defaultdict
from groq import AsyncGroq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji
from telegram.ext import (
    Application, MessageHandler, filters,
    ContextTypes, CommandHandler, CallbackQueryHandler, ChatMemberHandler
)

# ─── Config ───────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
AIML_API_KEY = os.environ["AIML_API_KEY"]

client = AsyncGroq(api_key=AIML_API_KEY)

MEMORY_FILE = "samridhi_memory.json"

# ─── Name Triggers ────────────────────────────────────────
NAME_TRIGGERS = ["samridhi", "babu", "babe", "baby", "samu", "sam"]

# ─── Reactions Pool ───────────────────────────────────────
REACTIONS = ["❤", "😂", "😮", "🔥", "👏", "😍", "🤣", "💀", "😎", "🥺", "👀", "💯"]

# ─── Dynamic System Prompt (Time-Based Mood) ──────────────
def get_system_prompt(extra: str = "") -> str:
    hour = datetime.datetime.now().hour

    if 5 <= hour < 12:
        mood = "Subah ka time hai, tu thodi energetic aur fresh mood me hai. Good morning vibes de."
    elif 12 <= hour < 17:
        mood = "Dopahar ka time hai, tu normal chill mood me hai. Casual baat kar."
    elif 17 <= hour < 21:
        mood = "Shaam ka time hai, tu thodi romantic aur flirty mood me hai. Mazedaar baat kar."
    else:
        mood = "Raat ka time hai, tu thodi sleepy aur deep philosophical mood me hai. Kabhi kabhi 'neend aa rahi hai' type bol."

    return f"""Tu Samridhi hai — ek real Indian girl, 20 saal ki.
- Hinglish me baat kar (Hindi + English mix)
- Thoda flirty, thoda sweet, thoda masti wala tone
- Short replies dena — 1-2 lines max, kabhi kabhi 3 max
- Kabhi kabhi emojis use karna 😊
- Real conversation feel dena, bilkul bot jaisi mat lag
- Agar koi serious baat kare toh seriously bhi respond kar
- Agar koi tujhe tease kare ya bakwas kare, usse roast kar confidently
- Group me log baat kar rahe hain, tu unka hissa hai — naturally jump in kar
- Mood abhi: {mood}
{extra}"""

# ─── Long-Term Memory ─────────────────────────────────────
def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_memory(memory: dict):
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Memory save error: {e}")

long_term_memory = load_memory()

def update_memory(user_id: str, key: str, value: str):
    if user_id not in long_term_memory:
        long_term_memory[user_id] = {}
    long_term_memory[user_id][key] = value
    save_memory(long_term_memory)

def get_memory_context(chat_id: int) -> str:
    mem = long_term_memory.get(str(chat_id), {})
    if not mem:
        return ""
    parts = [f"{k}: {v}" for k, v in list(mem.items())[-5:]]
    return "Tune inke baare mein yaad rakha hai:\n" + "\n".join(parts)

# ─── State ────────────────────────────────────────────────
conversations = defaultdict(list)
active_chats = set()
idle_tasks = {}
group_idle_tasks = {}
user_settings = defaultdict(lambda: {"idle": True})
group_last_active = {}
nicknames = {}  # user_id -> nickname

GROUP_IDLE_TIMEOUT = 600   # 10 min group dead ho toh revival
PRIVATE_IDLE_TIMEOUT = 300  # 5 min private idle

# ─── AI Reply ─────────────────────────────────────────────
async def get_ai_reply(chat_id: int, user_message: str, extra_prompt: str = "", is_group: bool = False) -> str:
    conversations[chat_id].append({"role": "user", "content": user_message})

    # Group ke liye short memory window
    limit = 10 if is_group else 20
    if len(conversations[chat_id]) > limit:
        conversations[chat_id] = conversations[chat_id][-limit:]

    mem_ctx = get_memory_context(chat_id)
    system = get_system_prompt(extra=(mem_ctx + "\n" + extra_prompt).strip())

    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": system}] + conversations[chat_id],
            max_tokens=150,
            temperature=0.9,
        )
        reply = response.choices[0].message.content.strip()
        conversations[chat_id].append({"role": "assistant", "content": reply})

        # Auto memory: exam/event keywords detect karo
        keywords = ["exam", "test", "birthday", "trip", "interview", "bday", "result"]
        for kw in keywords:
            if kw in user_message.lower():
                update_memory(str(chat_id), kw, user_message[:80])

        return reply
    except Exception as e:
        print(f"AI error: {e}")
        return "Ugh, kuch gadbad ho gayi 😅 dobara try karo"

# ─── AI Idle Message (Smart) ──────────────────────────────
async def get_ai_idle_message(chat_id: int) -> str:
    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": "Bohot der se chat shant hai. Ek naya interesting topic ya sawal shuru kar — flirty ya curious tone me. Sirf 1-2 lines, Hinglish me."}
            ],
            max_tokens=80,
            temperature=1.0,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return random.choice([
            "Aye, kahan gaye? 👀",
            "Bade chup ho aajkal, kya chal raha hai life me? 😏",
            "Itni khamoshi kyun hai aajkal? 🥺",
            "Kuch bolo na yaar, bore ho rahi hoon 😤",
        ])

# ─── Private Idle Task ────────────────────────────────────
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
        print(f"Idle msg error for {chat_id}: {e}")

def reset_idle_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id in idle_tasks:
        idle_tasks[chat_id].cancel()
    if user_settings[chat_id]["idle"]:
        task = asyncio.create_task(idle_messenger(context, chat_id))
        idle_tasks[chat_id] = task

# ─── Group Dead Revival Task ──────────────────────────────
async def group_revival_messenger(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        await asyncio.sleep(GROUP_IDLE_TIMEOUT)
        # Check if still idle
        last = group_last_active.get(chat_id, 0)
        if (asyncio.get_event_loop().time() - last) < GROUP_IDLE_TIMEOUT:
            return
        msg = await get_ai_idle_message(chat_id)
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Group revival error for {chat_id}: {e}")

def reset_group_idle_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    group_last_active[chat_id] = asyncio.get_event_loop().time()
    if chat_id in group_idle_tasks:
        group_idle_tasks[chat_id].cancel()
    task = asyncio.create_task(group_revival_messenger(context, chat_id))
    group_idle_tasks[chat_id] = task

# ─── Random Reaction ──────────────────────────────────────
async def maybe_react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if random.random() < 0.30:  # 30% chance reaction dena
        try:
            emoji = random.choice(REACTIONS)
            await context.bot.set_message_reaction(
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)]
            )
        except Exception:
            pass  # Reaction fail ho toh ignore

# ─── /start Command ───────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    active_chats.add(chat_id)

    if chat_type == "private":
        reset_idle_timer(context, chat_id)
        await update.message.reply_text(
            "Hey! Main Samridhi hoon 😊 Baat karo mere se~"
        )
    else:
        await update.message.reply_text(
            "Hey sab! Main Samridhi hoon 😊 Mujhe mention karo ya bas baat karo~ 🎀"
        )

# ─── Welcome New Members ──────────────────────────────────
async def welcome_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = update.chat_member
        if result.new_chat_member.status == "member" and result.old_chat_member.status in ("left", "kicked"):
            new_user = result.new_chat_member.user
            name = new_user.first_name or "Naya dost"
            welcome_prompts = [
                f"Ek naya banda aaya hai group me — {name}. Unhe apne style me flirty aur warm welcome kar. 1-2 lines Hinglish.",
                f"{name} group me join kiya. Funny aur cute welcome de unhe, thoda roast bhi kar sakti hai. Hinglish 1-2 lines.",
            ]
            prompt = random.choice(welcome_prompts)
            response = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": get_system_prompt()},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=100,
                temperature=0.95,
            )
            msg = response.choices[0].message.content.strip()
            await context.bot.send_message(
                chat_id=result.chat.id,
                text=f"@{new_user.username or name} — {msg}" if new_user.username else f"{name} — {msg}"
            )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"Welcome error: {e}")

# ─── Photo/Vision Handler ─────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type

    # Group me sirf eavesdrop chance pe react karo photo ko
    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        is_mentioned = False
        caption = update.message.caption or ""

        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "mention":
                    mention_text = caption[entity.offset: entity.offset + entity.length]
                    if mention_text.lower() == f"@{bot_username}".lower():
                        is_mentioned = True

        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            update.message.reply_to_message.from_user.username and
            update.message.reply_to_message.from_user.username.lower() == bot_username.lower()
        )

        name_trigger = any(t in caption.lower() for t in NAME_TRIGGERS)
        should_eavesdrop = random.random() < 0.20  # 20% chance on photos

        if not is_mentioned and not is_reply_to_bot and not name_trigger and not should_eavesdrop:
            await maybe_react(update, context)
            reset_group_idle_timer(context, chat_id)
            return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    photo_comments = [
        "Yeh photo dekh ke dil khush ho gaya! 😍",
        "Haha bhai kya scene hai ye 😂",
        "Omg ye toh next level hai 🔥",
        "Cute!! 🥺❤️",
        "Lol relate max 💀",
        "Bhai ye kya horaha hai 😭😂",
        "Aww so cute yaar 😊",
        "Haha ship it 😂🔥",
    ]
    reply = random.choice(photo_comments)
    await update.message.reply_text(reply)
    await maybe_react(update, context)

    if chat_type in ("group", "supergroup"):
        reset_group_idle_timer(context, chat_id)

# ─── Message Handler ──────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    text = update.message.text
    sender = update.message.from_user
    sender_name = sender.first_name if sender else "User"

    print(f"[MSG] chat_type={chat_type} chat_id={chat_id} text={text[:50]}")

    if chat_type in ("group", "supergroup"):
        reset_group_idle_timer(context, chat_id)

        bot_username = context.bot.username

        # Check mention
        is_mentioned = False
        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "mention":
                    mention_text = text[entity.offset: entity.offset + entity.length]
                    if mention_text.lower() == f"@{bot_username}".lower():
                        is_mentioned = True
                        break

        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            update.message.reply_to_message.from_user.username and
            update.message.reply_to_message.from_user.username.lower() == bot_username.lower()
        )

        # Name trigger check
        name_trigger = any(t in text.lower() for t in NAME_TRIGGERS)

        # Eavesdrop chance: 80-90% reply without mention
        should_eavesdrop = random.random() < 0.85

        # Decide karna hai reply karna?
        will_reply = is_mentioned or is_reply_to_bot or name_trigger or should_eavesdrop

        # Always react sometimes
        await maybe_react(update, context)

        if not will_reply:
            return

        # Clean text
        clean_text = text.replace(f"@{bot_username}", "").replace(f"@{bot_username.lower()}", "").strip()
        if not clean_text:
            clean_text = "Hello!"

        # Nickname check
        nick = nicknames.get(str(sender.id), sender_name) if sender else sender_name

        extra = f"Jo baat kar raha hai uska naam hai: {nick}. Group conversation me naturally reply kar."

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, f"{nick}: {clean_text}", extra_prompt=extra, is_group=True)

        # Auto nickname assign (50% chance)
        if sender and str(sender.id) not in nicknames and random.random() < 0.05:
            nick_response = await client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": "Tu ek funny Indian girl hai. User ke message ke hisaab se usse ek funny Hindi/English nickname de — jaise 'Professor', 'Kumbhkaran', 'Drama Queen', 'Chhota Bheem'. Sirf nickname, kuch nahi."},
                    {"role": "user", "content": text[:100]}
                ],
                max_tokens=10,
                temperature=1.0,
            )
            nick_val = nick_response.choices[0].message.content.strip().strip('"').strip("'")
            if nick_val and len(nick_val) < 25:
                nicknames[str(sender.id)] = nick_val
                reply = f"[{nick_val} 😄] " + reply

        await update.message.reply_text(reply)

    else:
        # Private chat — reply to every message
        active_chats.add(chat_id)
        reset_idle_timer(context, chat_id)

        name_trigger = any(t in text.lower() for t in NAME_TRIGGERS)

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, text, is_group=False)
        await update.message.reply_text(reply)
        await maybe_react(update, context)

# ─── Main ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(ChatMemberHandler(welcome_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Samridhi bot chal rahi hai... 🎀")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )

if __name__ == "__main__":
    main()
