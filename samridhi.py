import os
import asyncio
import random
from collections import defaultdict
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import (
    Application, MessageHandler, filters,
    ContextTypes, CommandHandler
)

# ─── Config ───────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
AIML_API_KEY = os.environ["AIML_API_KEY"]

client = AsyncOpenAI(
    api_key=AIML_API_KEY,
    base_url="https://api.aimlapi.com/v1"
)

SYSTEM_PROMPT = """Tu Samridhi hai — ek real Indian girl.
- Hinglish me baat kar (Hindi + English mix)
- Thoda flirty, thoda sweet, thoda masti wala tone
- Short replies dena — 1-2 lines max
- Kabhi kabhi emojis use karna 😊
- Real conversation feel dena, bot jaisa nahi
- Agar koi serious baat kare toh seriously bhi respond kar
"""

# ─── State ────────────────────────────────────────────────
message_count = defaultdict(int)       # chat_id -> message count
conversations = defaultdict(list)      # chat_id -> message history
active_chats = set()                   # chat IDs jo active hain
idle_tasks = {}                        # chat_id -> asyncio task

IDLE_TIMEOUT = 300        # 5 min baad idle message
IDLE_MESSAGES = [
    "Aye, kahan gaye? 👀",
    "Baat karo na yaar 😏",
    "Boring ho raha hai tujhke bina 🙄",
    "Hello?? Sune ho? 🥺",
    "Arrey kuch bolo bhi 😤",
    "Main yahan hoon... akeli 😌",
]

# ─── AI Reply ─────────────────────────────────────────────
async def get_ai_reply(chat_id: int, user_message: str) -> str:
    conversations[chat_id].append({"role": "user", "content": user_message})

    # Keep last 20 messages only
    if len(conversations[chat_id]) > 20:
        conversations[chat_id] = conversations[chat_id][-20:]

    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + conversations[chat_id],
            max_tokens=150,
            temperature=0.85,
        )
        reply = response.choices[0].message.content.strip()
        conversations[chat_id].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        print(f"AI error: {e}")
        return "Ugh, kuch gadbad ho gayi 😅 dobara try karo"

# ─── Idle Task ────────────────────────────────────────────
async def idle_messenger(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Agar koi chat nahi kar raha toh Samridhi khud message kare"""
    await asyncio.sleep(IDLE_TIMEOUT)
    msg = random.choice(IDLE_MESSAGES)
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        print(f"Idle msg error for {chat_id}: {e}")

def reset_idle_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Timer reset karo har message pe"""
    if chat_id in idle_tasks:
        idle_tasks[chat_id].cancel()
    task = asyncio.create_task(idle_messenger(context, chat_id))
    idle_tasks[chat_id] = task

# ─── Message Handler ──────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    text = update.message.text

    # Group me sirf tab reply karo jab mention ho ya reply ho
    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        is_mentioned = f"@{bot_username}" in text
        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            update.message.reply_to_message.from_user.username == bot_username
        )
        if not is_mentioned and not is_reply_to_bot:
            # Count karo but reply mat karo
            message_count[chat_id] += 1
            return

    # Mark chat active & reset idle timer
    active_chats.add(chat_id)
    reset_idle_timer(context, chat_id)

    # Har 3rd message pe reply karo
    message_count[chat_id] += 1
    if message_count[chat_id] % 3 != 0:
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await get_ai_reply(chat_id, text)
    await update.message.reply_text(reply)

# ─── /start Command ───────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    active_chats.add(chat_id)
    reset_idle_timer(context, chat_id)
    await update.message.reply_text(
        "Hey! Main Samridhi hoon 😊 Baat karo mere se~"
    )

# ─── Main ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Samridhi bot chal rahi hai... 🎀")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
