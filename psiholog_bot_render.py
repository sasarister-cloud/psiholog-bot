# =====================================================================================
#  Psiholog Bot - RENDER WEBHOOK VERZIJA (za Render FREE)
# =====================================================================================
#   ✅ radi bez pollinga
#   ✅ NE koristi Updater/run_webhook
#   ✅ koristi Flask webhook endpoint
#   ✅ kompatibilan s python-telegram-bot 20.8
#   ✅ kompatibilan s Python 3.13
#   ✅ koristi users.json + conversations.json + memory.json
#   ✅ admin (ADMIN_ID iz env) uvijek ima odobren premium bez isteka
#   ✅ osnovne komande: /start, /status, /pending, /approve, chat
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
MEMORY_FILE = "memory.json"


def ensure_files_exist():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    if not os.path.exists(CONVERSATIONS_FILE):
        with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
    if not os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
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


def load_memory() -> Dict[str, Any]:
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_memory(mem: Dict[str, Any]) -> None:
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2, ensure_ascii=False)


ensure_files_exist()

# =====================================================
# 3. GLOBALNI STATE ZA PTB APLIKACIJU I EVENT LOOP
# =====================================================

state: Dict[str, Any] = {
    "application": None,
    "loop": None,
}

# =====================================================
# 4. KORISNIČKI PODACI I STATUS
# =====================================================


def get_or_create_user(user_id: int, full_name: str = "") -> Dict[str, Any]:
    """
    Učitaj ili kreiraj korisnika.
    Admin (ADMIN_ID) uvijek dobiva odobren premium bez isteka (2099-12-31).
    Ostali korisnici idu na čekanje (waiting=True, approved=False).
    """
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
                "subscription_until": (datetime.utcnow() + timedelta(days=7)).strftime(
                    "%Y-%m-%d"
                ),
                "premium": False,
                "waiting": True,
            }
        changed = True

    # Svaki put kad se admin pojavi, osiguraj da mu je pristup aktivan
    if ADMIN_ID is not None and user_id == ADMIN_ID:
        user = users[uid]
        wanted = {
            "approved": True,
            "premium": True,
            "subscription_until": "2099-12-31",
            "waiting": False,
        }
        for k, v in wanted.items():
            if user.get(k) != v:
                user[k] = v
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


def extend_subscription(user_id: int, days: int) -> None:
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        return
    now = datetime.utcnow().date()
    current_str = users[uid].get("subscription_until")
    if current_str:
        try:
            current = datetime.strptime(current_str, "%Y-%m-%d").date()
            base = max(now, current)
        except Exception:
            base = now
    else:
        base = now
    new_date = base + timedelta(days=days)
    users[uid]["subscription_until"] = new_date.strftime("%Y-%m-%d")
    save_users(users)

# =====================================================
# 5. AI CHAT FUNKCIJA
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
                        "Odgovaraš jasno i podržavajuće, u kraćim odlomcima (2-5 rečenica). "
                        "Ako korisnik spominje samoozljeđivanje ili suicidalne misli, "
                        "naglašavaš važnost traženja stručne pomoći uživo i kontaktiranja hitnih službi."
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
# 6. SPREMANJE KONVERZACIJA I MEMORY
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


def append_memory_note(user_id: int, note: str) -> None:
    mem = load_memory()
    uid = str(user_id)
    mem.setdefault(uid, {"notes": []})
    mem[uid]["notes"].append(
        {
            "timestamp": datetime.utcnow().isoformat(),
            "note": note,
        }
    )
    save_memory(mem)

# =====================================================
# 7. TELEGRAM HANDLERI
# =====================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    full_name = update.effective_user.full_name or "Nepoznat"
    user = get_or_create_user(user_id, full_name)

    # Admin uvijek ima pristup
    if ADMIN_ID is not None and user_id == ADMIN_ID:
        await update.message.reply_text(
            "👋 Pozdrav, admin! Pristup ti je uvijek odobren i premium.\n"
            "Kako se osjećaš danas?"
        )
        return

    if not user.get("approved", False):
        await update.message.reply_text(
            "⏳ Tvoj pristup još nije odobren.\n"
            "Admin će pregledati tvoj zahtjev i odobriti pristup ako je sve u redu."
        )
        return

    if not is_subscription_active(user):
        await update.message.reply_text(
            "⚠️ Tvoja pretplata je istekla ili nije aktivna.\n"
            "Javi se administratoru kako bi produžio/la pristup."
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
    waiting = "DA" if user.get("waiting", False) else "NE"

    await update.message.reply_text(
        f"📊 Status profila:\n"
        f"- Ime: {user.get('name','')}\n"
        f"- Odobren: {approved}\n"
        f"- Na čekanju: {waiting}\n"
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

    user_text = update.message.text or ""
    append_conversation(user_id, "user", user_text)

    # Primjer: upiši bilješku u memory ako poruka sadrži riječ "bitno"
    if "bitno" in user_text.lower():
        append_memory_note(user_id, user_text)

    reply = await ai_chat_reply(user, user_text)
    append_conversation(user_id, "bot", reply)

    await update.message.reply_text(reply)


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID is None or update.effective_user.id != ADMIN_ID:
        return
    users = load_users()
    waiting = [
        f"{uid}: {data.get('name','')} (pretplata do: {data.get('subscription_until','-')})"
        for uid, data in users.items()
        if data.get("waiting", False)
    ]
    if not waiting:
        await update.message.reply_text("Nema korisnika na čekanju.")
    else:
        await update.message.reply_text("📝 Korisnici na čekanju:\n" + "\n".join(waiting))


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
        await update.message.reply_text("❌ Nepoznat ID korisnika.")


async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID is None or update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Koristi: /extend <user_id> <dani>")
        return
    target_id_str, days_str = context.args
    try:
        days = int(days_str)
    except ValueError:
        await update.message.reply_text("Broj dana mora biti cijeli broj.")
        return

    try:
        target_id = int(target_id_str)
    except ValueError:
        await update.message.reply_text("user_id mora biti broj.")
        return

    extend_subscription(target_id, days)
    await update.message.reply_text(
        f"📅 Produžio/la si pretplatu korisniku {target_id} za {days} dana."
    )


async def setpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_ID is None or update.effective_user.id != ADMIN_ID:
        return
    if len(context.args) != 2:
        await update.message.reply_text("Koristi: /setpremium <user_id> <0/1>")
        return
    target_id_str, flag_str = context.args
    try:
        flag = int(flag_str)
        if flag not in (0, 1):
            raise ValueError
    except ValueError:
        await update.message.reply_text("Flag mora biti 0 ili 1.")
        return

    users = load_users()
    if target_id_str not in users:
        await update.message.reply_text("Nepoznat ID korisnika.")
        return

    users[target_id_str]["premium"] = bool(flag)
    save_users(users)
    await update.message.reply_text(
        f"⭐ Premium za korisnika {target_id_str} postavljen na {bool(flag)}."
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Placeholder za buduće callback gumbe (meni, testovi, itd.)
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Opcija zaprimljena (callback placeholder).")

# =====================================================
# 8. WEBHOOK SERVER (FLASK + PTB BEZ UPDATER-A)
# =====================================================

app = Flask(__name__)


@app.get("/")
def index():
    return "Psiholog Bot webhook aktivan.", 200


@app.post(f"/webhook/{TELEGRAM_TOKEN}")
def telegram_webhook():
    from telegram import Update as TgUpdate

    application = state.get("application")
    loop = state.get("loop")

    if application is None or loop is None:
        return "Application not ready", 500

    data = request.get_json(force=True)
    if not data:
        return "No JSON", 400

    update = TgUpdate.de_json(data, application.bot)

    # Pošalji update u PTB event loop
    asyncio.run_coroutine_threadsafe(
        application.process_update(update),
        loop,
    )

    return "OK", 200


async def init_telegram_application():
    """Inicijalizacija PTB aplikacije i postavljanje webhooka."""
    application = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()

    # Spremi u globalni state
    state["application"] = application

    # Handleri
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_cmd))

    application.add_handler(CommandHandler("pending", pending_cmd))
    application.add_handler(CommandHandler("approve", approve_cmd))
    application.add_handler(CommandHandler("extend", extend_cmd))
    application.add_handler(CommandHandler("setpremium", setpremium_cmd))

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


def run_bot_loop():
    """Pokreće PTB aplikaciju u zasebnom threadu s vlastitim event loopom."""
    loop = asyncio.new_event_loop()
    state["loop"] = loop
    asyncio.set_event_loop(loop)

    try:
        loop.run_until_complete(init_telegram_application())
        print("✅ Telegram Application inicijaliziran i webhook postavljen.")
        loop.run_forever()
    finally:
        loop.close()


if __name__ == "__main__":
    print("🤖 Psiholog Bot WEBHOOK verzija za Render FREE pokrenut!")

    # Pokreni PTB event loop u pozadinskom threadu
    bot_thread = threading.Thread(target=run_bot_loop, daemon=True)
    bot_thread.start()

    port = int(os.environ.get("PORT", "10000"))
    print(f"🚀 Pokrećem Flask na portu {port}...")
    app.run(host="0.0.0.0", port=port)
