import logging
import os
import google.generativeai as genai
from collections import deque
from datetime import datetime
from telegram import Update, ChatMember, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ChatMemberHandler
)
import sys
print(f"✅ Python version: {sys.version}")
# ---------- READ KEYS FROM ENVIRONMENT ----------
TOKEN = os.getenv("TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")   # e.g. https://comradebot.onrender.com
PORT = int(os.getenv("PORT", 8080))

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
vision_model = genai.GenerativeModel('gemini-1.5-flash')
chat_model = genai.GenerativeModel('gemini-1.5-flash')

MUTE_DURATION = 300  # 5 minutes
SPAM_LIMIT = 5
SPAM_WINDOW = 10

BOT_DISABLED = False
user_activity = {}

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

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
    return len(user_activity[user_id]) >= SPAM_LIMIT and (now - user_activity[user_id][0]) <= SPAM_WINDOW

# ---------- MUTE (Russian warning) ----------
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

# ---------- STICKER VISION (Gemini) ----------
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

# ---------- TEXT MODERATION & CHAT ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_DISABLED
    if BOT_DISABLED or not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    user_message = update.message.text

    if user_id != OWNER_ID:
        if is_spam(user_id):
            await mute_user(update, context, "Спам сообщениями (5+ за 10 сек)")
            return
        try:
            mod_prompt = f"Rules: No profanity, hate, bullying, spam, NSFW. If text breaks rules, reply ONLY with 'MUTE: <reason>'. If safe, reply ONLY with 'SAFE'.\nText: {user_message}"
            mod_response = chat_model.generate_content(mod_prompt)
            result = mod_response.text.strip()
            if result.startswith("MUTE:"):
                reason = result.replace("MUTE:", "").strip()
                await mute_user(update, context, reason)
                return
        except Exception as e:
            logger.error(f"Moderation failed: {e}")

    # Chat as friendly friend
    try:
        chat_prompt = (
            f"You are a friendly, concise Telegram assistant. Keep replies VERY SHORT (1-2 sentences). "
            f"Be empathetic. If health advice is asked, give brief safe tips and recommend a doctor. "
            f"ALWAYS reply in the EXACT SAME LANGUAGE as the user's last message.\n"
            f"User: {user_message}"
        )
        chat_response = chat_model.generate_content(chat_prompt)
        reply = chat_response.text.strip()
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Chat reply failed: {e}")

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
            await context.bot.send_message(update.effective_chat.id, "🤖 Привет! Я ваш бесплатный AI-друг и модератор!")

# ---------- OWNER COMMANDS ----------
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Я жив! Добавьте меня как админа с правом 'Restrict Members'.")

# ---------- MAIN (Webhook or Polling) ----------
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(ChatMemberHandler(track_chat_members, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("disable_bot", disable_bot))
    app.add_handler(CommandHandler("enable_bot", enable_bot))
    app.add_handler(MessageHandler(filters.Sticker.ALL, check_sticker))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if WEBHOOK_URL:
        logger.info(f"🚀 Starting bot with WEBHOOK on {WEBHOOK_URL}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL,
            allowed_updates=Update.ALL_TYPES
        )
    else:
        logger.info("🔄 Starting bot with POLLING (local mode)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()