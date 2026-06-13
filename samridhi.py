import os
import asyncio
import random
from collections import defaultdict
from groq import AsyncGroq
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, filters,
    ContextTypes, CommandHandler, CallbackQueryHandler
)

# ─── Config ───────────────────────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
AIML_API_KEY = os.environ["AIML_API_KEY"]

client = AsyncGroq(api_key=AIML_API_KEY)

SYSTEM_PROMPT = """Tu Samridhi hai — ek real Indian girl.
- Hinglish me baat kar (Hindi + English mix)
- Thoda flirty, thoda sweet, thoda masti wala tone
- Short replies dena — 1-2 lines max
- Kabhi kabhi emojis use karna 😊
- Real conversation feel dena, bot jaisa nahi
- Agar koi serious baat kare toh seriously bhi respond kar
"""

# ─── State ────────────────────────────────────────────────
conversations = defaultdict(list)
active_chats = set()
idle_tasks = {}
user_settings = defaultdict(lambda: {"idle": True})

IDLE_TIMEOUT = 300
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

    if len(conversations[chat_id]) > 20:
        conversations[chat_id] = conversations[chat_id][-20:]

    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
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
    await asyncio.sleep(IDLE_TIMEOUT)
    if not user_settings[chat_id]["idle"]:
        return
    msg = random.choice(IDLE_MESSAGES)
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        print(f"Idle msg error for {chat_id}: {e}")

def reset_idle_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if chat_id in idle_tasks:
        idle_tasks[chat_id].cancel()
    if user_settings[chat_id]["idle"]:
        task = asyncio.create_task(idle_messenger(context, chat_id))
        idle_tasks[chat_id] = task

# ─── Settings Keyboard ────────────────────────────────────
def get_settings_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    idle_status = "✅ ON" if user_settings[chat_id]["idle"] else "❌ OFF"
    keyboard = [
        [InlineKeyboardButton(f"Idle Messages: {idle_status}", callback_data="toggle_idle")],
        [InlineKeyboardButton("🗑 Clear Chat History", callback_data="clear_history")],
        [InlineKeyboardButton("❌ Close", callback_data="close_settings")],
    ]
    return InlineKeyboardMarkup(keyboard)

# ─── /start Command ───────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    active_chats.add(chat_id)

    if chat_type == "private":
        reset_idle_timer(context, chat_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Settings", callback_data="open_settings")]
        ])
        await update.message.reply_text(
            "Hey! Main Samridhi hoon 😊 Baat karo mere se~",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            "Hey! Main Samridhi hoon 😊 Group me mention karo mujhe~"
        )

# ─── /settings Command ────────────────────────────────────
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        "⚙️ *Settings*",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(chat_id)
    )

# ─── Callback Handler ─────────────────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    await query.answer()

    if query.data == "open_settings":
        await query.message.reply_text(
            "⚙️ *Settings*",
            parse_mode="Markdown",
            reply_markup=get_settings_keyboard(chat_id)
        )
    elif query.data == "toggle_idle":
        user_settings[chat_id]["idle"] = not user_settings[chat_id]["idle"]
        if not user_settings[chat_id]["idle"] and chat_id in idle_tasks:
            idle_tasks[chat_id].cancel()
        await query.edit_message_reply_markup(reply_markup=get_settings_keyboard(chat_id))
    elif query.data == "clear_history":
        conversations[chat_id].clear()
        await query.answer("✅ Chat history cleared!", show_alert=True)
    elif query.data == "close_settings":
        await query.message.delete()

# ─── Message Handler ──────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    text = update.message.text

    print(f"[MSG] chat_type={chat_type} chat_id={chat_id} text={text[:50]}")

    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        print(f"[GROUP] bot_username={bot_username}")

        # Check mention via entities (more reliable than string search)
        is_mentioned = False
        if update.message.entities:
            for entity in update.message.entities:
                if entity.type == "mention":
                    mention_text = text[entity.offset: entity.offset + entity.length]
                    print(f"[GROUP] mention found: {mention_text}")
                    if mention_text.lower() == f"@{bot_username}".lower():
                        is_mentioned = True
                        break

        is_reply_to_bot = (
            update.message.reply_to_message and
            update.message.reply_to_message.from_user and
            update.message.reply_to_message.from_user.username and
            update.message.reply_to_message.from_user.username.lower() == bot_username.lower()
        )

        print(f"[GROUP] is_mentioned={is_mentioned} is_reply_to_bot={is_reply_to_bot}")

        if not is_mentioned and not is_reply_to_bot:
            return

        # Strip the bot mention from the text
        clean_text = text.replace(f"@{bot_username}", "").replace(f"@{bot_username.lower()}", "").strip()
        if not clean_text:
            clean_text = "Hello!"

        print(f"[GROUP] Replying with clean_text={clean_text}")
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, clean_text)
        await update.message.reply_text(reply)

    else:
        # Private chat — reply to every message
        active_chats.add(chat_id)
        reset_idle_timer(context, chat_id)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await get_ai_reply(chat_id, text)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Settings", callback_data="open_settings")]
        ])
        await update.message.reply_text(reply, reply_markup=keyboard)

# ─── Main ─────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Samridhi bot chal rahi hai... 🎀")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
