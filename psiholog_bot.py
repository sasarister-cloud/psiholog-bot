# WEBHOOK-COMPATIBLE VERSION FOR RENDER FREE
# -------------------------------------------------
# Ova verzija uklanja polling i koristi webhook tako da
# bot može raditi na Render FREE Web Service okruženju.
# Admin panel je isključen u ovoj fazi dok se bot ne pokrene.

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

# Memory storage files
USERS_FILE = "users.json"
CONVERSATIONS_FILE = "conversations.json"

# Ensure JSON files exist
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w") as f:
        json.dump({}, f)

if not os.path.exists(CONVERSATIONS_FILE):
    with open(CONVERSATIONS_FILE, "w") as f:
        json.dump({}, f)

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

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    users = load_users()

    if user_id not in users:
        users[user_id] = {
            "approved": False,
            "created": str(datetime.now())
        }
        save_users(users)

    await update.message.reply_text("Hvala što ste kontaktirali Psiholog Bota. Pričekajte odobrenje.")

# Admin: list pending users
async def pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    users = load_users()
    pending_users = [uid for uid, data in users.items() if not data.get("approved")]

    if not pending_users:
        await update.message.reply_text("Nema korisnika na čekanju.")
        return

    text = "Korisnici na čekanju:\n" + "\n".join(pending_users)
    await update.message.reply_text(text)

# Admin: approve user
async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# Handle user messages
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

# MAIN BOT FUNCTION (webhook mode)
def main_bot():
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN nije postavljen!")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pending", pending))
    application.add_handler(CommandHandler("approve", approve))

    # Message handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Webhook setup for Render
    port = int(os.environ.get("PORT", 10000))
    external_url = os.environ.get("RENDER_EXTERNAL_URL")

    if not external_url:
        raise ValueError("RENDER_EXTERNAL_URL nije postavljen od Render-a!")

    webhook_url = f"{external_url}/webhook/{TELEGRAM_TOKEN}"

    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    print("🤖 Psiholog Bot WEBHOOK verzija pokrenut!")
    main_bot()