# psiholog_bot_render.py
# ------------------------------------------------------------
# WEBHOOK verzija Psiholog Bota za Render FREE hosting.
# - zadržava istu logiku kao lokalna polling verzija
# - koristi webhook umjesto pollinga
# - NE pokreće admin panel
# - koristi iste JSON datoteke kao lokalna verzija
# - koristi iste AI odgovore i autorizaciju korisnika
# ------------------------------------------------------------

import os
import json
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
import openai

# Load environment variables
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

openai.api_key = OPENAI_API_KEY

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Storage files
USERS_FILE = "users.json"
CONVERSATIONS_FILE = "conversations.json"

# Ensure JSON files exist
def ensure_data_files():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w") as f:
            json.dump({}, f)

    if not os.path.exists(CONVERSATIONS_FILE):
        with open(CONVERSATIONS_FILE, "w") as f:
            json.dump({}, f)

ensure_data_files()

# Load user data
def load_users():
    with open(USERS_FILE, "r") as f:
        return json.load(f)

# Save user data
def save_users(data):
    with open(USERS_FILE, "w") as f:
        json.dump(data, f, indent=4)

# Load conversation history
def load_conversations():
    with open(CONVERSATIONS_FILE, "r") as f:
        return json.load(f)

# Save conversation history
def save_conversations(data):
    with open(CONVERSATIONS_FILE, "w") as f:
        json.dump(data, f, indent=4)

# START command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    users = load_users()

    if user_id not in users:
        users[user_id] = {
            "approved": False,
            "created": str(datetime.now()),
            "premium": False,
            "expires": None
        }
        save_users(users)

    if not users[user_id]["approved"]:
        await update.message.reply_text(
            "Hvala što ste kontaktirali Psiholog Bota. Pričekajte odobrenje administratora."
        )
        return

    await update.message.reply_text("Dobrodošli nazad! Kako se osjećate danas?")

# ADMIN — list pending users
async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    users = load_users()
    pending_users = [uid for uid, data in users.items() if not data.get("approved")]

    if not pending_users:
        await update.message.reply_text("Nema korisnika na čekanju.")
        return

    text = "Korisnici na čekanju:\n" + "\n".join(pending_users)
    await update.message.reply_text(text)

# ADMIN — approve user
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    args = context.args
    if len(args) != 1:
        await update.message.reply_text("Upišite user ID: /approve <id>")
        return

    user_id = args[0]
    users = load_users()

    if user_id not in users:
        await update.message.reply_text("Korisnik ne postoji.")
        return

    users[user_id]["approved"] = True
    save_users(users)

    await update.message.reply_text(f"Korisnik {user_id} je odobren.")

# HANDLE user messages
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    users = load_users()

    if user_id not in users or not users[user_id].get("approved"):
        await update.message.reply_text("Niste odobreni. Pričekajte administratora.")
        return

    user_message = update.message.text

    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": user_message}]
    )

    reply = response.choices[0].message.content
    await update.message.reply_text(reply)

# MAIN WEBHOOK BOT FUNCTION
def main_bot_webhook():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN nije postavljen!")

    #application = Application.builder().token(TELEGRAM_TOKEN).build()
    application = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()


    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pending", pending_cmd))
    application.add_handler(CommandHandler("approve", approve_cmd))

    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Webhook setup for Render
    port = int(os.environ.get("PORT", 10000))
    external_url = os.environ.get("RENDER_EXTERNAL_URL")

    if not external_url:
        raise ValueError("RENDER_EXTERNAL_URL nije postavljen od Render-a!")

    webhook_path = f"webhook/{TELEGRAM_TOKEN}"
    webhook_url = f"{external_url}/{webhook_path}"

    print(f"🌎 Webhook URL registriran: {webhook_url}")

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=webhook_url,
    )


# ENTRY POINT
if __name__ == "__main__":
    print("🤖 Psiholog Bot WEBHOOK verzija za Render FREE pokrenut!")
    main_bot_webhook()
