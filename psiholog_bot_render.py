import os
import json
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any
import threading

from flask import Flask, request
from dotenv import load_dotenv
from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =====================================================
# 1. ENV VARIJABLE
# =====================================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN nije postavljen!")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY nije postavljen!")

try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else None
except Exception:
    raise RuntimeError("ADMIN_ID mora biti broj!")

client = OpenAI(api_key=OPENAI_API_KEY)

# =====================================================
# 2. JSON FILES
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


def load_users():
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(data):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_conversations():
    with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_conversations(data):
    with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


ensure_files_exist()

# =====================================================
# 3. KORISNICI
# =====================================================


def get_or_create_user(user_id: int, name: str) -> Dict[str, Any]:
    users = load_users()
    uid = str(user_id)
    changed = False

    if uid not in users:
        if ADMIN_ID and user_id == ADMIN_ID:
            users[uid] = {
                "name": name,
                "approved": True,
                "subscription_until": "2099-12-31",
                "premium": True,
                "waiting": False,
            }
        else:
            users[uid] = {
                "name": name,
                "approved": False,
                "subscription_until": (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d"),
                "premium": False,
                "waiting": True,
            }
        changed = True

    if ADMIN_ID and user_id == ADMIN_ID:
        u = users[uid]
        if not u.get("premium"):
            u["premium"] = True
            u["approved"] = True
            u["subscription_until"] = "2099-12-31"
            changed = True

    if changed:
        save_users(users)

    return users[uid]


def save_user(user_id: int, new_data: Dict[str, Any]):
    users = load_users()
    uid = str(user_id)
    if uid in users:
        users[uid].update(new_data)
        save_users(users)


def is_subscription_active(u: Dict[str, Any]) -> bool:
    try:
        until = datetime.strptime(u.get("subscription_until", "1970-01-01"), "%Y-%m-%d").date()
        return until >= datetime.utcnow().date()
    except Exception:
        return False

# =====================================================
# 4. POMOĆNE FUNKCIJE – AI I DNEVNIK
# =====================================================

async def ai_chat_reply(user: Dict[str, Any], text: str) -> str:
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ti si empatičan psihološki asistent. "
                        "Odgovaraj sažeto (3–6 rečenica), toplo i podržavajuće. "
                        "Izbjegavaj dijagnosticiranje, fokusiraj se na podršku i praktične korake."
                    ),
                },
                {"role": "user", "content": text},
            ],
            max_tokens=900,
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"⚠️ Greška AI servisa: {e}"


def append_conversation(user_id: int, role: str, text: str):
    all_conv = load_conversations()
    uid = str(user_id)

    all_conv.setdefault(uid, []).append(
        {
            "timestamp": datetime.utcnow().isoformat(),
            "role": role,
            "text": text,
        }
    )

    save_conversations(all_conv)


def add_mood_entry(user: Dict[str, Any], rating: int, note: str):
    diary = user.get("mood_diary") or []
    diary.append(
        {
            "timestamp": datetime.utcnow().isoformat(),
            "rating": rating,
            "note": note,
        }
    )
    user["mood_diary"] = diary

# =====================================================
# 5. HANDLERI – KOMANDE
# =====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.effective_user.full_name
    user = get_or_create_user(user_id, name)

    # Admin ima sve
    if ADMIN_ID and user_id == ADMIN_ID:
        await update.message.reply_text("👋 Pozdrav, admin! Sve opcije su ti dostupne.")
    else:
        # Provjera pristupa
        if not user.get("approved"):
            await update.message.reply_text(
                "⏳ Tvoj pristup još nije odobren.\n\n"
                "Pričekaj da te administrator uključi u sustav."
            )
            return

        if not is_subscription_active(user):
            await update.message.reply_text(
                "⚠️ Tvoja pretplata je istekla.\n\n"
                "Ako želiš nastaviti koristiti bota, javi se administratoru."
            )
            return

    # GLAVNI MENI – kao u lokalnoj verziji
    buttons = [
        [InlineKeyboardButton("📓 Dnevnik emocija", callback_data="OPEN_MOOD_DIARY")],
        [
            InlineKeyboardButton("🧠 AI psiholog", callback_data="CHAT_START"),
            InlineKeyboardButton("🎯 Terapijski mod", callback_data="CHOOSE_MODE"),
        ],
        [InlineKeyboardButton("📊 Analiza emocija", callback_data="EMOTION_ANALYSIS")],
        [
            InlineKeyboardButton("⏰ Dnevna provjera", callback_data="DAILY_CHECK_INFO"),
            InlineKeyboardButton("🗂 Arhiva", callback_data="SHOW_HISTORY"),
        ],
        [
            InlineKeyboardButton("🎲 Dnevni izazov", callback_data="DAILY_CHALLENGE"),
            InlineKeyboardButton("🧪 Testovi", callback_data="TEST_MENU"),
        ],
        [InlineKeyboardButton("🚨 Hitni način", callback_data="EMERGENCY_MODE")],
        [InlineKeyboardButton("👤 Profil", callback_data="PROFILE")],
    ]

    if not user.get("premium", False) and not (ADMIN_ID and user_id == ADMIN_ID):
        buttons.append(
            [InlineKeyboardButton("⭐ Premium paketi (B i C)", callback_data="PREMIUM_INFO")]
        )

    menu = InlineKeyboardMarkup(buttons)

    await update.message.reply_text(
        "🧭 *Glavni izbornik*\nOdaberi što želiš:",
        parse_mode="Markdown",
        reply_markup=menu,
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_or_create_user(update.effective_user.id, update.effective_user.full_name)
    await update.message.reply_text(
        f"📊 Status:\n"
        f"Odobren: {u.get('approved', False)}\n"
        f"Premium: {u.get('premium', False)}\n"
        f"Pretplata do: {u.get('subscription_until', 'N/A')}"
    )

# =====================================================
# 6. HANDLER – PORUKE
# =====================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.effective_user.full_name
    user = get_or_create_user(user_id, name)

    # Admin uvijek prolazi, ostali podložni provjerama
    if not (ADMIN_ID and user_id == ADMIN_ID):
        if not user.get("approved"):
            await update.message.reply_text("⚠️ Još nemaš odobren pristup.")
            return
        if not is_subscription_active(user):
            await update.message.reply_text("⚠️ Tvoja pretplata je istekla.")
            return

    text = update.message.text or ""

    # 1) Ako korisnik dovršava unos u dnevnik emocija (bilješka)
    if "mood_pending_rating" in user:
        rating = user.pop("mood_pending_rating")
        add_mood_entry(user, rating, note=text)
        save_user(user_id, user)
        await update.message.reply_text(
            "📓 Zabilježio sam tvoju emociju i bilješku u dnevnik.\n"
            "Hvala ti što dijeliš kako se osjećaš. ❤️"
        )
        return

    # 2) Klasičan AI razgovor
    append_conversation(user_id, "user", text)
    reply = await ai_chat_reply(user, text)
    append_conversation(user_id, "bot", reply)

    await update.message.reply_text(reply)

# =====================================================
# 7. HANDLER – GUMBI (CALLBACK QUERY)
# =====================================================

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    user = get_or_create_user(user_id, query.from_user.full_name)

    await query.answer()

    # Osnovne provjere pristupa (osim za admina)
    if not (ADMIN_ID and user_id == ADMIN_ID):
        if not user.get("approved"):
            await query.edit_message_text("⚠️ Još nemaš odobren pristup.")
            return
        if not is_subscription_active(user):
            await query.edit_message_text("⚠️ Tvoja pretplata je istekla.")
            return

    premium = bool(user.get("premium", False))

    # --- DNEVNIK EMOCIJA / RASPOLOŽENJE ---
    if data == "OPEN_MOOD_DIARY":
        keyboard = [
            [
                InlineKeyboardButton("1 😞", callback_data="MOOD_1"),
                InlineKeyboardButton("2 🙁", callback_data="MOOD_2"),
                InlineKeyboardButton("3 😐", callback_data="MOOD_3"),
            ],
            [
                InlineKeyboardButton("4 🙂", callback_data="MOOD_4"),
                InlineKeyboardButton("5 😄", callback_data="MOOD_5"),
            ],
        ]
        await query.edit_message_text(
            "📓 Kako se osjećaš (1–5)?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("MOOD_"):
        try:
            rating = int(data.replace("MOOD_", ""))
        except ValueError:
            rating = 3
        user["mood_pending_rating"] = rating
        save_user(user_id, user)
        await query.edit_message_text(
            f"📓 Zabilježio sam ocjenu {rating}.\n\n"
            "Ako želiš, napiši mi poruku s kratkim opisom što se dogodilo – "
            "ta poruka će biti spremljena uz ovaj unos."
        )
        return

    # --- AI RAZGOVOR ---
    if data == "CHAT_START":
        await query.edit_message_text(
            "💬 Slobodno napiši što te muči.\n"
            "Ja ću ti odgovoriti kao podržavajući AI psiholog."
        )
        return

    # --- PROFIL ---
    if data == "PROFILE":
        await query.edit_message_text(
            "👤 *Tvoj profil*\n\n"
            f"Ime: {user.get('name', 'N/A')}\n"
            f"Odobren: {user.get('approved', False)}\n"
            f"Pretplata do: {user.get('subscription_until', 'N/A')}\n"
            f"Premium: {user.get('premium', False)}",
            parse_mode="Markdown",
        )
        return

    # --- PAKET B / ANALIZA EMOCIJA (premium) ---
    if data == "EMOTION_ANALYSIS":
        if not premium and not (ADMIN_ID and user_id == ADMIN_ID):
            await query.edit_message_text(
                "🔒 Analiza emocija je dio ⭐ Paketa B.\n\n"
                "Javi se administratoru ako želiš nadogradnju."
            )
            return
        await query.edit_message_text(
            "📊 Analiza emocija će uskoro biti potpuno integrirana u ovu verziju bota.\n"
            "Za sada nastavi koristiti dnevnik emocija i AI psihologa. 😊"
        )
        return

    # --- DNEVNE RUTINE / PROVJERA ---
    if data == "DAILY_CHECK_INFO":
        await query.edit_message_text(
            "🕒 *Dnevna provjera raspoloženja*\n\n"
            "U ovoj verziji bota dnevna provjera funkcionira kroz:\n"
            "• Dnevnik emocija (📓)\n"
            "• Kraće provjere kada ti zatreba\n\n"
            "U budućnosti ćemo dodati automatske notifikacije u određeno vrijeme dana. 🙂",
            parse_mode="Markdown",
        )
        return

    # --- ARHIVA RAZGOVORA ---
    if data == "SHOW_HISTORY":
        conv = load_conversations()
        msgs = conv.get(str(user_id), [])
        if not msgs:
            await query.edit_message_text("🗂 Trenutno nema sačuvanih poruka u arhivi.")
            return

        tail = msgs[-20:]
        lines = []
        for msg in tail:
            ts = msg.get("timestamp", "")[:16].replace("T", " ")
            role = "👤" if msg.get("role") == "user" else "🤖"
            text = msg.get("text", "")
            lines.append(f"[{ts}] {role}: {text}")

        txt = "🗂 *Zadnjih 20 poruka:*\n\n" + "\n".join(lines)
        await query.edit_message_text(txt, parse_mode="Markdown")
        return

    # --- DNEVNI IZAZOV / DNEVNE RUTINE (Paket B/C feeling) ---
    if data == "DAILY_CHALLENGE":
        prompt = (
            "Smisli jedan mali, jednostavan dnevni izazov za mentalno zdravlje "
            "(npr. kratka vježba zahvalnosti, disanja, kontakt s nekim bliskim). "
            "Odgovori kratko, 2–3 rečenice, na hrvatskom."
        )
        challenge = await ai_chat_reply(user, prompt)
        await query.edit_message_text(
            "🎲 *Dnevni izazov:*\n\n" + challenge,
            parse_mode="Markdown",
        )
        return

    # --- TESTOVI (Paket B/C) – zasad informativno ---
    if data == "TEST_MENU":
        await query.edit_message_text(
            "🧪 Psihološki testovi (PHQ-9, GAD-7 i drugi) bit će uskoro dostupni u ovoj verziji bota.\n\n"
            "Za sada se možeš koristiti dnevnikom emocija i AI psihologom.",
        )
        return

    # --- HITNI NAČIN ---
    if data == "EMERGENCY_MODE":
        user["emergency_mode"] = True
        save_user(user_id, user)
        crisis_text = (
            "🚨 *Hitni način uključen.*\n\n"
            "Ako si u neposrednoj opasnosti ili razmišljaš o samoozljeđivanju, "
            "ODMAH nazovi 112 ili lokalnu hitnu psihijatriju.\n"
            "Također, javi se osobi od povjerenja.\n\n"
            "Ovdje možeš napisati kako se osjećaš, ali imaj na umu da sam AI "
            "i ne mogu zamijeniti stručnu pomoć."
        )
        await query.edit_message_text(crisis_text, parse_mode="Markdown")
        return

    # --- PAKETI B I C INFO ---
    if data == "PREMIUM_INFO":
        await query.edit_message_text(
            "⭐ *Premium paketi (B i C)*\n\n"
            "🌟 *Paket B – Napredna emocionalna podrška*\n"
            "• Analiza emocija\n"
            "• Dnevne refleksije i izazovi\n"
            "• Osnovni emocionalni uvidi kroz vrijeme\n\n"
            "🔥 *Paket C – Napredni osobni razvoj*\n"
            "• Sve iz paketa B\n"
            "• Detaljniji uvidi u obrasce razmišljanja\n"
            "• Podrška u ciljevima i promjeni navika\n\n"
            "Za više informacija i nadogradnju javi se administratoru.",
            parse_mode="Markdown",
        )
        return

    # Fallback
    await query.edit_message_text("✅ Opcija zaprimljena.")

# =====================================================
# 8. WEBHOOK + EVENT LOOP
# =====================================================

app = Flask(__name__)
application = None
loop = None


@app.get("/")
def index():
    return "Webhook radi.", 200


@app.post(f"/webhook/{TELEGRAM_TOKEN}")
def telegram_webhook():
    from telegram import Update as TgUpdate
    global application, loop

    data = request.get_json(force=True)
    if not data:
        return "No JSON", 400

    update = TgUpdate.de_json(data, application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
    return "OK", 200


async def init_telegram_application():
    global application

    # .updater(None) je KLJUČ da izbjegnemo Updater bug na PTB 20.8 + Python 3.13
    application = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    await application.start()

    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        raise RuntimeError("RENDER_EXTERNAL_URL nije postavljen!")

    webhook_url = f"{external_url}/webhook/{TELEGRAM_TOKEN}"
    print(f"🌍 Registriram webhook: {webhook_url}")
    await application.bot.set_webhook(url=webhook_url)


def start_flask():
    port = int(os.environ.get("PORT", "10000"))
    print(f"🚀 Flask na portu {port}")
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    print("🤖 Pokrećem Psiholog Bot WEBHOOK verziju (Render)…")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(init_telegram_application())

    threading.Thread(target=start_flask, daemon=True).start()

    print("✅ Bot i webhook su pokrenuti.")
    loop.run_forever()
