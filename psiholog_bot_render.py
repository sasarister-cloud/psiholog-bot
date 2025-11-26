# =====================================================================================
#  Psiholog Bot - RENDER WEBHOOK VERZIJA (potpuno prilagođena za Render FREE)
# =====================================================================================
#   ✅ radi bez pollinga
#   ✅ radi bez Updater-a
#   ✅ koristi Flask webhook endpoint
#   ✅ kompatibilan sa python-telegram-bot 20.8
#   ✅ radi na Python 3.13 (Render FREE)
#   ✅ koristi osnovne funkcionalnosti lokalne verzije
#   ✅ koristi conversations.json + users.json
#   ✅ admin uvijek ima odobren i premium pristup (ADMIN_ID iz env)
# =====================================================================================

import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any
import threading

from flask import Flask, request
from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =====================================================
# 1. ENV VARIJABLE I OSNOVNE POSTAVKE
# =====================================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN nije postavljen u .env ili Renderu!")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY nije postavljen u .env ili Renderu!")

try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else None
except ValueError:
    raise RuntimeError("ADMIN_ID mora biti broj!")

client = OpenAI(api_key=OPENAI_API_KEY)

# =====================================================
# 2. LOKALNE JSON DATOTEKE
# =====================================================

USERS_FILE = "users.json"
CONVERSATIONS_FILE = "conversations.json"


def ensure_files_exist():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    if not os.path.exists(CONVERSATIONS_FILE):
        with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)


def load_users() -> Dict[str, Any]:
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(users: Dict[str, Any]) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)


def load_conversations() -> Dict[str, Any]:
    with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_conversations(convs: Dict[str, Any]) -> None:
    with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(convs, f, indent=2, ensure_ascii=False)


ensure_files_exist()

# =====================================================
# 3. KORISNIČKI PODACI I STATUS
# =====================================================

def get_or_create_user(user_id: int, full_name: str = "") -> Dict[str, Any]:
    """Učitaj ili kreiraj korisnika. Admin uvijek dobiva odobren premium bez isteka."""
    users = load_users()
    uid = str(user_id)
    changed = False

    if uid not in users:
        if ADMIN_ID is not None and user_id == ADMIN_ID:
            users[uid] = {
                "name": full_name or "Admin",
                "approved": True,
                "subscription_until": "2099-12-31",
                "premium": True,
                "waiting": False,
            }
        else:
            users[uid] = {
                "name": full_name or "Nepoznat",
                "approved": False,
                "subscription_until": (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d"),
                "premium": False,
                "waiting": True,
            }
        changed = True

    # Svaki put kad se admin pojavi, osiguraj da je odobren i premium
    if ADMIN_ID is not None and user_id == ADMIN_ID:
        user = users[uid]
        if not user.get("approved") or not user.get("premium") or user.get("subscription_until") != "2099-12-31":
            user["approved"] = True
            user["premium"] = True
            user["subscription_until"] = "2099-12-31"
            user["waiting"] = False
            changed = True

    if changed:
        save_users(users)

    return users[uid]


def update_user(user_id: int, new_data: Dict[str, Any]) -> None:
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        return
    users[uid].update(new_data)
    save_users(users)


def is_subscription_active(user: Dict[str, Any]) -> bool:
    until_str = user.get("subscription_until")
    if not until_str:
        return False
    try:
        until_date = datetime.strptime(until_str, "%Y-%m-%d").date()
        return until_date >= datetime.utcnow().date()
    except Exception:
        return False


# =====================================================
# 4. AI CHAT FUNKCIJA
# =====================================================

async def ai_chat_reply(user: Dict[str, Any], user_text: str) -> str:
    """Glavni AI odgovor psihologa na poruku korisnika."""
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ti si empatičan psihološki asistent na hrvatskom jeziku. "
                        "Odgovaraš jasno i podržavajuće, u kraćim odlomcima (2-5 rečenica)."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            max_tokens=1200,
            temperature=0.8,
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"⚠️ Greška AI servisa: {e}"


# =====================================================
# 5. SPREMANJE KONVERZACIJA
# =====================================================

def append_conversation(user_id: int, role: str, text: str) -> None:
    convs = load_conversations()
    uid = str(user_id)
    convs.setdefault(uid, []).append(
        {
            "timestamp": datetime.utcnow().isoformat(),
            "role": role,
            "text": text,
        }
    )
    save_conversations(convs)


# =====================================================
# 6. TELEGRAM HANDLERI
# =====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    full_name = update.effective_user.full_name or "Nepoznat"
    user = get_or_create_user(user_id, full_name)

    # Admin uvijek ima pristup
    if ADMIN_ID is not None and user_id == ADMIN_ID:
        await update.message.reply_text(
            "👋 Pozdrav adminu! Pristup ti je uvijek odobren. Kako se osjećaš danas?"
        )
        return

    if not user.get("approved", False):
        await update.message.reply_text(
            "⏳ Tvoj pristup još nije odobren. Admin će pregledati tvoj zahtjev."
        )
        return

    if not is_subscription_active(user):
        await update.message.reply_text(
            "⚠️ Tvoja pretplata je istekla ili nije aktivna. Javi se administratoru za produženje."
        )
        return

    await update.message.reply_text(
        "👋 Dobrodošao/la natrag! Možeš mi ukratko opisati kako se osjećaš ili što te muči."
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    full_name = update.effective_user.full_name or "Nepoznat"
    user = get_or_create_user(user_id, full_name)

    sub_until = user.get("subscription_until", "nije postavljeno")
    premium = "DA" if user.get("premium", False) else "NE"
    approved = "DA" if user.get("approved", False) else "NE"

    await update.message.reply_text(
        f"📊 Status profila:\n"
        f"- Ime: {user.get('name','')}\n"
        f"- Odobren: {approved}\n"
        f"- Pretplata do: {sub_until}\n"
        f"- Premium: {premium}"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    full_name = update.effective_user.full_name or "Nepoznat"
    user = get_or_create_user(user_id, full_name)

    # Admin uvijek može pričati
    if not (ADMIN_ID is not None and user_id == ADMIN_ID):
        if not user.get("approved", False):
            await update.message.reply_text(
                "⚠️ Još nemaš odobren pristup. Pričekaj da te admin odobri."
            )
            return
        if not is_subscription_active(user):
            await update.message.reply_text(
                "⚠️ Tvoja pretplata je istekla. Javi se administratoru za produženje."
            )
            return

    user_text = update.message.text
    append_conversation(user_id, "user", user_text)

    reply = await ai_chat_reply(user, user_text)
    append_conversation(user_id, "bot", reply)

    await update.message.reply_text(reply)


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID is None or update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    waiting = [f"{uid}: {data.get('name','')}" for uid, data in users.items() if data.get("waiting", False)]
    if not waiting:
        await update.message.reply_text("Nema korisnika na čekanju.")
    else:
        await update.message.reply_text("📝 Na čekanju:\n" + "\n".join(waiting))


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID is None or update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 1:
        await update.message.reply_text("Koristi: /approve <user_id>")
        return
    target_id = context.args[0]
    users = load_users()
    if target_id in users:
        users[target_id]["approved"] = True
        users[target_id]["waiting"] = False
        save_users(users)
        await update.message.reply_text(f"✅ Odobren korisnik {target_id}")
    else:
        await update.message.reply_text("Nepoznat ID korisnika.")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Placeholder za callback gumbe (menu, testovi itd.)."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Opcija zaprimljena.")


# =====================================================
# 7. WEBHOOK SERVER (FLASK + PTB BEZ UPDATER-A)
# =====================================================

app = Flask(__name__)
application: Application | None = None
loop: asyncio.AbstractEventLoop | None = None


@app.get("/")
def index():
    return "Psiholog Bot webhook aktivan.", 200


@app.post(f"/webhook/{TELEGRAM_TOKEN}")
def telegram_webhook():
    from telegram import Update as TgUpdate
    global application, loop

    if application is None or loop is None:
        return "Application not ready", 500

    data = request.get_json(force=True)
    if not data:
        return "No JSON", 400

    update = TgUpdate.de_json(data, application.bot)
    asyncio.run_coroutine_threadsafe(
        application.process_update(update), loop
    )

    return "OK", 200


async def init_telegram_application():
    global application

    application = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("pending", pending_cmd))
    application.add_handler(CommandHandler("approve", approve_cmd))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    await application.start()

    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        raise RuntimeError("RENDER_EXTERNAL_URL nije postavljen od Render-a!")

    webhook_url = f"{external_url}/webhook/{TELEGRAM_TOKEN}"
    print(f"🌎 Webhook URL registriran: {webhook_url}")

    await application.bot.set_webhook(url=webhook_url)


def start_flask() -> None:
    port = int(os.environ.get("PORT", "10000"))
    print(f"🚀 Pokrećem Flask na portu {port}...")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    print("🤖 Psiholog Bot WEBHOOK verzija za Render FREE pokrenut!")

    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Inicijaliziraj Telegram aplikaciju i webhook u event loopu
    loop.run_until_complete(init_telegram_application())

    # Pokreni Flask u zasebnom threadu
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    print("✅ Psiholog Bot na Render FREE je pokrenut i sluša webhook!")

    # Drži event loop živim
    loop.run_forever()
