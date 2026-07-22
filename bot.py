import logging
import os
import sys
import google.generativeai as genai
from collections import deque
from datetime import datetime
from telegram import Update, ChatMember, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ChatMemberHandler
)

# ---------- CONFIG ----------
TOKEN = os.getenv("TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

# --------------------------
#  AI SETUP (with fallback)
# --------------------------
USE_AI = False  # will be set to True if Gemini initialises successfully

try:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set")
    genai.configure(api_key=GEMINI_API_KEY)
    # Try the most common free model – we'll catch errors later
    MODEL_NAME = "gemini-1.5-flash"
    vision_model = genai.GenerativeModel(MODEL_NAME)
    chat_model = genai.GenerativeModel(MODEL_NAME)
    # Test the model with a simple prompt
    test_response = chat_model.generate_content("Say OK")
    if test_response and test_response.text:
        USE_AI = True
        logging.info(f"✅ Gemini model {MODEL_NAME} is ready.")
    else:
        logging.warning("Gemini test produced empty response – AI will be disabled.")
except Exception as e:
    logging.error(f"Gemini initialisation failed: {e}. AI will be disabled.")

# ---------- RULE-BASED FALLBACK (Russian only) ----------
import random

RUSSIAN_RESPONSES = [
    "Привет! Как дела?",
    "Я тебя слушаю.",
    "Расскажи подробнее.",
    "Понял!",
    "Хорошо, я запомнил.",
    "Можешь повторить?",
    "Интересно!",
    "Да, конечно.",
    "Нет, не согласен.",
    "Отлично!",
    "Спасибо за сообщение.",
    "Я здесь, чтобы помочь.",
    "Что-то я не понял. Повтори, пожалуйста.",
]

def get_russian_fallback(user_message: str) -> str:
    """Generate a simple Russian reply based on keywords."""
    msg = user_message.lower()
    if "привет" in msg or "здравствуй" in msg:
        return "Привет! Как у тебя дела?"
    if "как дела" in msg or "как жизнь" in msg:
        return "У меня всё отлично! А у тебя?"
    if "спасибо" in msg or "благодарю" in msg:
        return "Пожалуйста! Всегда рад помочь."
    if "пока" in msg or "до свидания" in msg:
        return "До встречи! Хорошего дня."
    if "помощь" in msg or "помоги" in msg:
        return "Конечно, я здесь, чтобы помочь. Что случилось?"
    return random.choice(RUSSIAN_RESPONSES)

# ---------- OTHER SETTINGS ----------
MUTE_DURATION = 300  # 5 minutes
SPAM_LIMIT = 5
SPAM_WINDOW = 10

BOT_DISABLED = False
user_activity = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # hide token in logs

# ---------- UNMUTE ----------
async def unmute_user(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    user_id = job_data["user_id"]
    try:
        await context.bot.restrict_chat_member(
            chat_id,
            user_id,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False
            )
        )
        await context.bot.send_message(chat_id, f"✅ Пользователь {user_id} разблокирован. Пожалуйста, соблюдайте правила!")
    except Exception as e:
        logger.error(f"Unmute failed: {e}")

# ---------- SPAM CHECK ----------
def is_spam(user_id: int) -> bool:
    now = datetime.now().timestamp()
    if user_id not in user_activity:
        user_activity[user_id] = deque(maxlen=SPAM_LIMIT)
    user_activity[user_id].append(now)
    if len(user_activity[user_id]) < SPAM_LIMIT:
        return False
    return (now - user_activity[user_id][0]) <= SPAM_WINDOW

# ---------- MUTE ----------
async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, ChatPermissions(can_send_messages=False))
        await update.message.reply_text(
            f"🔇 {user_name} был замучен на {MUTE_DURATION//60} минут.\n"
            f"Причина: {reason}\n"
            f"Правила: уважайте всех, будьте добры, не спамьте."
        )
        if context.job_queue:
            context.job_queue.run_once(unmute_user, MUTE_DURATION, data={"chat_id": chat_id, "user_id": user_id})
    except Exception as e:
        logger.error(f"Failed to mute: {e}")

# ---------- STICKER MODERATION ----------
async def check_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DISABLED
    if BOT_DISABLED or update.effective_user.id == OWNER_ID:
        return
    if is_spam(update.effective_user.id):
        await mute_user(update, context, "Спам стикерами (5+ за 10 сек)")
        return

    sticker = update.message.sticker
    if not sticker:
        return

    if not USE_AI:
        # If AI is off, we can't check stickers – just ignore
        return

    try:
        file = await context.bot.get_file(sticker.file_id)
        file_bytes = await file.download_as_bytearray()
        response = vision_model.generate_content([
            "Does this sticker contain offensive content, hate speech, bullying, NSFW, or spam? Reply ONLY with 'SAFE' or 'UNSAFE: <reason in English>'.",
            {"mime_type": "image/webp", "data": bytes(file_bytes)}
        ])
        result = response.text.strip()
        if result.startswith("UNSAFE"):
            reason = result.replace("UNSAFE:", "").strip()
            await mute_user(update, context, f"Нарушение в стикере: {reason}")
    except Exception as e:
        logger.error(f"Sticker vision failed: {e}")

# ---------- MAIN MESSAGE HANDLER ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DISABLED

    if BOT_DISABLED:
        return
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    user_message = update.message.text.strip()

    logger.info(f"📩 From {user_name} ({user_id}): {user_message[:50]}")

    # --- Moderation (skip owner) ---
    if user_id != OWNER_ID:
        if is_spam(user_id):
            await mute_user(update, context, "Спам сообщениями (5+ за 10 сек)")
            return

        if USE_AI:
            try:
                mod_prompt = f"""Rules: No profanity, hate, bullying, spam, NSFW. 
If the text breaks ANY rule, reply ONLY with 'MUTE: <short reason>'. 
If it is completely safe, reply ONLY with 'SAFE'.
Text: {user_message}"""
                mod_response = chat_model.generate_content(mod_prompt)
                result = mod_response.text.strip()
                logger.info(f"🛡️ Moderation: {result}")

                if "MUTE:" in result.upper():
                    reason = result.replace("MUTE:", "").strip()
                    await mute_user(update, context, reason)
                    return
                elif "SAFE" not in result.upper():
                    logger.warning(f"Unclear moderation result: {result}")
            except Exception as e:
                logger.error(f"Moderation error: {e}")

    # --- Generate reply ---
    reply = None

    if USE_AI:
        try:
            # Ask Gemini for a short reply in the same language
            chat_prompt = f"""
You are a friendly, concise Telegram assistant.
Keep replies VERY SHORT (1-2 sentences).
Be empathetic, casual.
If health advice is requested, give brief safe tips and recommend a doctor.
ALWAYS reply in the EXACT SAME LANGUAGE as the user's last message.

User: {user_message}
Assistant:"""
            chat_response = chat_model.generate_content(chat_prompt)
            reply = chat_response.text.strip()
            logger.info(f"🤖 AI reply: {reply[:50]}...")
        except Exception as e:
            logger.error(f"AI chat error: {e}")

    # --- Fallback if AI didn't produce a reply ---
    if not reply:
        reply = get_russian_fallback(user_message)
        logger.info(f"🔄 Fallback reply: {reply}")

    # --- Send the reply ---
    try:
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")

# ---------- GROUP ACCESS ----------
async def track_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if not result:
        return
    if result.new_chat_member.status == ChatMember.MEMBER and result.new_chat_member.user.id == context.bot.id:
        inviter = result.invite_link_creator or result.from_user
        if inviter and inviter.id != OWNER_ID:
            await context.bot.send_message(update.effective_chat.id, "❌ Только владелец может добавить меня. Пока!")
            await context.bot.leave_chat(update.effective_chat.id)
        else:
            await context.bot.send_message(update.effective_chat.id, "🤖 Привет! Я ваш AI-друг и модератор.")

# ---------- COMMANDS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Я жив! Добавьте меня как админа с правом 'Restrict Members'.")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏓 Pong! Бот работает.")

async def disable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DISABLED
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("⛔ Только владелец.")
    BOT_DISABLED = True
    await update.message.reply_text("🛑 Бот отключён.")

async def enable_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DISABLED
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("⛔ Только владелец.")
    BOT_DISABLED = False
    await update.message.reply_text("✅ Бот включён.")

# ---------- MAIN ----------
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(ChatMemberHandler(track_chat_members, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("disable_bot", disable_bot))
    app.add_handler(CommandHandler("enable_bot", enable_bot))
    app.add_handler(MessageHandler(filters.Sticker.ALL, check_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if WEBHOOK_URL:
        logger.info(f"🚀 Starting webhook on {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES
        )
    else:
        logger.info("🔄 Starting polling (local)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    print(f"Python version: {sys.version}")
    main()