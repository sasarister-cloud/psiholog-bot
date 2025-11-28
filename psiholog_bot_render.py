# Psiholog Bot – Render webhook verzija
# Integrirani meni, terapijski mod, dnevnik emocija, /menu, /help i povratak na glavni meni

import os
import json
import asyncio
from datetime import datetime, timedelta, time as dtime
from typing import Dict, Any, List
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


def load_users() -> Dict[str, Any]:
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_users(data: Dict[str, Any]) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_conversations() -> Dict[str, Any]:
    with open(CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_conversations(data: Dict[str, Any]) -> None:
    with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


ensure_files_exist()

# =====================================================
# 3. KORISNICI
# =====================================================


USER_DEFAULTS: Dict[str, Any] = {
    "approved": True,              # default: odmah odobren (osim ako želiš ručno mijenjati)
    "waiting": False,
    "subscription_until": (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%d"),
    "premium": False,
    # terapija & stil
    "therapist": "standard",
    "therapy_mode": "NONE",      # NONE, CBT, ACT, DBT
    # dnevnik emocija
    "mood_log": [],               # lista dictova {timestamp, rating, note}
    "mood_pending_rating": None,  # privremeno, kad čekaš opis nakon odabira 1–5
    "daily_check": False,         # automatska dnevna provjera
}


def get_or_create_user(user_id: int, name: str) -> Dict[str, Any]:
    users = load_users()
    uid = str(user_id)

    if uid not in users:
        # novi korisnik
        data = USER_DEFAULTS.copy()
        data["name"] = name
        # admin uvijek premium i "beskonačna" pretplata
        if ADMIN_ID and user_id == ADMIN_ID:
            data["approved"] = True
            data["premium"] = True
            data["subscription_until"] = "2099-12-31"
            data["waiting"] = False
        users[uid] = data
        save_users(users)
        return data

    # već postoji – dopuni eventualno nove ključeve
    user = users[uid]
    for k, v in USER_DEFAULTS.items():
        user.setdefault(k, v)

    user.setdefault("name", name)

    # admin zaštita
    if ADMIN_ID and user_id == ADMIN_ID:
        user["approved"] = True
        user["premium"] = True
        user["subscription_until"] = "2099-12-31"
        user["waiting"] = False

    users[uid] = user
    save_users(users)
    return user


def save_user(user_id: int, new_data: Dict[str, Any]) -> None:
    users = load_users()
    uid = str(user_id)
    if uid in users:
        users[uid].update(new_data)
        save_users(users)


def get_user_str(uid: str) -> Dict[str, Any] | None:
    return load_users().get(uid)


def is_subscription_active(u: Dict[str, Any]) -> bool:
    try:
        until = datetime.strptime(u.get("subscription_until", "1970-01-01"), "%Y-%m-%d").date()
        return until >= datetime.utcnow().date()
    except Exception:
        return False


# =====================================================
# 4. KONVERZACIJE
# =====================================================


def append_conversation(user_id: int, role: str, text: str) -> None:
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


# =====================================================
# 5. AI – TERAPIJSKI MODOVI
# =====================================================

THERAPY_PROMPTS: Dict[str, str] = {
    "NONE": (
        "Ti si empatičan, topao psihološki asistent. "
        "Odgovaraj jasno, u 3–6 kraćih rečenica, na hrvatskom jeziku."
    ),
    "CBT": (
        "Ti si psihološki asistent koji koristi elemente kognitivno-bihevioralne terapije (CBT). "
        "Pomažeš korisniku da prepozna misli, emocije i ponašanja, te predlažeš konkretne korake i vježbe. "
        "Odgovaraj u 3–6 rečenica, jednostavno i praktično."
    ),
    "ACT": (
        "Ti si psihološki asistent u ACT stilu (Acceptance and Commitment Therapy). "
        "Naglašavaš prihvaćanje emocija, povezivanje s osobnim vrijednostima i male korake prema onome što je važno. "
        "Odgovaraj u 3–6 rečenica, smireno i podržavajuće."
    ),
    "DBT": (
        "Ti si psihološki asistent u DBT stilu (dialektičko-bihevioralna terapija). "
        "Naglašavaš regulaciju emocija, toleranciju distresa i mindfulness vježbe. "
        "Odgovaraj u 3–6 rečenica, vrlo strukturirano i validirajuće."
    ),
}


async def ai_chat_reply(user: Dict[str, Any], text: str) -> str:
    mode = user.get("therapy_mode", "NONE")
    system_prompt = THERAPY_PROMPTS.get(mode, THERAPY_PROMPTS["NONE"])

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=900,
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"⚠️ Greška AI servisa: {e}"


# =====================================================
# 6. DNEVNIK EMOCIJA I DNEVNA PROVJERA
# =====================================================


def add_mood_entry(user: Dict[str, Any], rating: int, note: str | None = None) -> None:
    mood_log: List[Dict[str, Any]] = user.get("mood_log", [])
    mood_log.append(
        {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
            "rating": rating,
            "note": note or "",
        }
    )
    # ograniči na zadnjih 90 unosa
    if len(mood_log) > 90:
        mood_log[:] = mood_log[-90:]
    user["mood_log"] = mood_log


async def send_emotion_analysis(chat_id: int, user: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> None:
    log = user.get("mood_log", [])
    if len(log) < 3:
        await context.bot.send_message(chat_id, "Za analizu treba barem 3 unosa u dnevnik emocija.")
        return

    last = log[-21:]
    lines = [
        f"{e['timestamp']}: {e['rating']} – {e.get('note','')[:80]}" for e in last
    ]
    joined = "\n".join(lines)

    prompt = (
        "Na temelju ovih unosa u dnevniku emocija:\n\n"
        f"{joined}\n\n"
        "Analiziraj kako se korisnik otprilike osjeća kroz vrijeme, moguće okidače, "
        "obrasce razmišljanja i predloži 3–5 konkretnih koraka za brigu o sebi."
    )

    result = await ai_chat_reply(user, prompt)
    await context.bot.send_message(chat_id, "📊 *Analiza emocija:*\n\n" + result, parse_mode="Markdown")


async def daily_check_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    uid = str(chat_id)
    user = get_user_str(uid)
    if not user or not user.get("daily_check"):
        return

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
    await context.bot.send_message(
        chat_id,
        "⏰ Dnevna provjera: kako si danas (1–5)?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def schedule_daily(app: Application, chat_id: int) -> None:
    # svaki dan u 20:00 po server vremenu
    app.job_queue.run_daily(
        daily_check_job,
        time=dtime(hour=20, minute=0),
        chat_id=chat_id,
        name=f"daily_{chat_id}",
    )


# =====================================================
# 7. GLAVNI MENI
# =====================================================


def build_main_menu(user: Dict[str, Any]) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton("💬 Počni razgovor", callback_data="CHAT_START")],
        [
            InlineKeyboardButton("📓 Dnevnik emocija", callback_data="OPEN_MOOD_DIARY"),
            InlineKeyboardButton("📊 Analiza emocija", callback_data="EMOTION_ANALYSIS"),
        ],
        [
            InlineKeyboardButton("⏰ Dnevna provjera", callback_data="TOGGLE_DAILY"),
            InlineKeyboardButton("🧠 Terapijski mod", callback_data="CHOOSE_MODE"),
        ],
        [
            InlineKeyboardButton("🎲 Dnevni izazov", callback_data="DAILY_CHALLENGE"),
            InlineKeyboardButton("⭐ Premium info", callback_data="PREMIUM_INFO"),
        ],
        [InlineKeyboardButton("ℹ️ Pomoć", callback_data="HELP_MENU")],
    ]
    return InlineKeyboardMarkup(kb)


def main_menu_text(user: Dict[str, Any]) -> str:
    mode = user.get("therapy_mode", "NONE")
    mode_txt = "isključen" if mode == "NONE" else mode
    premium_txt = "DA" if user.get("premium") else "NE"
    return (
        "🤖 *Psiholog Bot – glavni izbornik*\n\n"
        f"⭐ Premium: {premium_txt}\n"
        f"🎯 Terapijski mod: {mode_txt}\n\n"
        "Odaberi što želiš raditi upravo sada."
    )


async def send_main_menu(chat_id: int, user: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.send_message(
        chat_id,
        main_menu_text(user),
        reply_markup=build_main_menu(user),
        parse_mode="Markdown",
    )


async def edit_to_main_menu(query, user: Dict[str, Any]) -> None:
    await query.edit_message_text(
        main_menu_text(user),
        reply_markup=build_main_menu(user),
        parse_mode="Markdown",
    )


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Glavni meni", callback_data="BACK_MAIN")]]
    )


# =====================================================
# 8. KOMANDE
# =====================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    name = update.effective_user.full_name
    user = get_or_create_user(user_id, name)

    if not is_subscription_active(user):
        await update.message.reply_text(
            "⚠️ Tvoja probna pretplata je istekla. Javi se administratoru za nastavak."
        )
        return

    await update.message.reply_text(
        "👋 Dobrodošao/la u Psiholog bota!\n\n"
        "Koristi me za kratke razgovore, vođenje dnevnika emocija i male terapijske korake.",
    )
    await send_main_menu(update.effective_chat.id, user, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📋 *Komande: *\n"
        "/start – pokretanje / nastavak rada\n"
        "/help – ova pomoć\n"
        "/status – stanje pretplate i premiuma\n"
        "/profile – (opcionalno) kratka forma o tebi (još u izradi)\n"
        "/menu ili /meni – prikaži glavni izbornik\n"
        "/mood – brzi unos raspoloženja (1–5 + bilješka)\n"
        "/history – (opcionalno) arhiva razgovora (osnovna verzija)\n"
        "\nVećinu vremena dovoljno je koristiti glavni meni."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    uid = str(user_id)
    user = get_user_str(uid)
    if not user:
        await update.message.reply_text("Nisi registriran. Pošalji /start.")
        return

    expiry_str = user.get("subscription_until")
    if not expiry_str:
        await update.message.reply_text("⚠️ Problem s pretplatom. Javite se administratoru.")
        return

    expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
    days_left = (expiry.date() - datetime.utcnow().date()).days
    premium_flag = "DA" if user.get("premium") else "NE"

    await update.message.reply_text(
        f"📅 Pretplata vrijedi do: {expiry_str}\n"
        f"Preostalo dana: {max(days_left, 0)}\n"
        f"⭐ Premium: {premium_flag}"
    )


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Profil i detaljnija personalizacija su u pripremi. Za sada me slobodno koristi i bez toga."
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    name = update.effective_user.full_name
    user = get_or_create_user(user_id, name)

    if not is_subscription_active(user):
        await update.message.reply_text("⚠️ Tvoja pretplata je istekla.")
        return

    await send_main_menu(update.effective_chat.id, user, context)


async def mood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = get_or_create_user(user_id, update.effective_user.full_name)

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
    await update.message.reply_text(
        "Kako si sada na skali 1–5?", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = str(update.effective_chat.id)
    conv = load_conversations().get(uid, [])
    if not conv:
        await update.message.reply_text("Nema spremljene povijesti razgovora.")
        return

    last = conv[-10:]
    lines = [f"{c['timestamp']}: {c['role']}: {c['text'][:80]}" for c in last]
    await update.message.reply_text("📜 Zadnji dijelovi razgovora:\n\n" + "\n".join(lines))


async def weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Tjedni psihološki izvještaj će uskoro biti dodat kao posebna opcija."
    )


async def tests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Psihološki testovi (PHQ-9, GAD-7 i slično) bit će dodani u sljedećoj verziji."
    )


# =====================================================
# 9. HANDLE MESSAGE – GLAVNA LOGIKA
# =====================================================


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    user = get_or_create_user(user_id, update.effective_user.full_name)

    if not is_subscription_active(user):
        await update.message.reply_text("❌ Tvoja pretplata je istekla.")
        return

    # Ako čekamo opis raspoloženja nakon odabira 1–5
    if user.get("mood_pending_rating") is not None:
        rating = user["mood_pending_rating"]
        add_mood_entry(user, rating, text)
        user["mood_pending_rating"] = None
        save_user(user_id, user)
        await update.message.reply_text("Hvala ti, zapisao sam tvoj unos u dnevnik emocija.")
        return

    # inače – običan razgovor
    append_conversation(user_id, "user", text)
    reply = await ai_chat_reply(user, text)
    append_conversation(user_id, "bot", reply)

    await update.message.reply_text(reply)


# =====================================================
# 10. INLINE GUMBI (MENI, TERAPIJSKI MOD, DNEVNIK…)
# =====================================================


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    await query.answer()

    user = get_or_create_user(user_id, query.from_user.full_name)

    if not is_subscription_active(user):
        await query.edit_message_text("❌ Tvoja pretplata je istekla.")
        return

    # Povratak na glavni meni
    if data == "BACK_MAIN":
        await edit_to_main_menu(query, user)
        return

    # Glavni chat
    if data == "CHAT_START":
        await query.edit_message_text(
            "💬 Slobodno mi napiši što te muči ili o čemu želiš razgovarati.",
            reply_markup=back_keyboard(),
        )
        return

    # Dnevnik emocija – odabir 1–5
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
            "📓 Dnevnik emocija – odaberi trenutno raspoloženje (1–5):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("MOOD_"):
        try:
            rating = int(data.replace("MOOD_", ""))
        except ValueError:
            await query.edit_message_text("Nevažeća vrijednost raspoloženja.")
            return

        add_mood_entry(user, rating, None)
        user["mood_pending_rating"] = rating
        save_user(user_id, user)

        await query.edit_message_text(
            "Hvala ti. Ako želiš, ukratko opiši što se događa (ili samo napiši /menu za povratak).",
        )
        return

    if data == "EMOTION_ANALYSIS":
        await query.edit_message_text(
            "⏳ Radim analizu tvojih unosa u dnevniku emocija…",
            reply_markup=back_keyboard(),
        )
        await send_emotion_analysis(chat_id, user, context)
        return

    if data == "TOGGLE_DAILY":
        user["daily_check"] = not user.get("daily_check", False)
        save_user(user_id, user)

        # makni prijašnji job, ako postoji
        jq = context.application.job_queue
        jobs = jq.get_jobs_by_name(f"daily_{chat_id}")
        for j in jobs:
            j.schedule_removal()

        if user["daily_check"]:
            schedule_daily(context.application, chat_id)
            msg = "✅ Uključena je dnevna provjera raspoloženja u 20:00."
        else:
            msg = "⛔ Isključena je dnevna provjera raspoloženja."

        await query.edit_message_text(msg, reply_markup=back_keyboard())
        return

    if data == "CHOOSE_MODE":
        kb = [
            [InlineKeyboardButton("Isključen", callback_data="MODE_NONE")],
            [InlineKeyboardButton("CBT", callback_data="MODE_CBT")],
            [InlineKeyboardButton("ACT", callback_data="MODE_ACT")],
            [InlineKeyboardButton("DBT", callback_data="MODE_DBT")],
        ]
        await query.edit_message_text(
            "🎯 Odaberi terapijski mod (stil odgovora):",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("MODE_"):
        mode = data.replace("MODE_", "")
        if mode == "NONE":
            user["therapy_mode"] = "NONE"
            save_user(user_id, user)
            await query.edit_message_text(
                "🎯 Terapijski mod je isključen.", reply_markup=back_keyboard()
            )
            return
        if mode not in ("CBT", "ACT", "DBT"):
            await query.edit_message_text("Nepoznat terapijski mod.")
            return
        user["therapy_mode"] = mode
        save_user(user_id, user)
        await query.edit_message_text(
            f"🎯 Terapijski mod postavljen na: *{mode}*.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(),
        )
        return

    if data == "PREMIUM_INFO":
        await query.edit_message_text(
            "⭐ *Premium uključuje: *\n"
            "• Analizu emocija\n"
            "• Različite terapijske stilove (CBT, ACT, DBT)\n"
            "• Dnevnu emocionalnu provjeru\n"
            "• Dodatne napredne opcije u budućnosti\n\n"
            "Za nadogradnju javi se administratoru.",
            parse_mode="Markdown",
            reply_markup=back_keyboard(),
        )
        return

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
            reply_markup=back_keyboard(),
        )
        return

    if data == "HELP_MENU":
        await query.edit_message_text(
            "ℹ️ Ovdje si uvijek možeš: \n"
            "• otvoriti razgovor (Počni razgovor)\n"
            "• upisati kako se osjećaš (Dnevnik emocija)\n"
            "• dobiti analizu raspoloženja (Analiza emocija)\n"
            "• uključiti/isključiti dnevnu provjeru\n"
            "• prilagoditi stil odgovora (Terapijski mod).\n\n"
            "Za listu komandi koristi /help.",
            reply_markup=back_keyboard(),
        )
        return

    if data == "TEST_MENU":
        await query.edit_message_text(
            "🧪 Psihološki testovi će biti dodani u sljedećoj verziji.",
            reply_markup=back_keyboard(),
        )
        return

    # Fallback
    await query.edit_message_text("✅ Opcija je zaprimljena.")


# =====================================================
# 11. WEBHOOK + EVENT LOOP ZA RENDER
# =====================================================

app = Flask(__name__)
application: Application | None = None
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


async def init_telegram_application() -> None:
    global application

    application = Application.builder().token(TELEGRAM_TOKEN).updater(None).build()

    # komande
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("profile", profile_cmd))
    application.add_handler(CommandHandler("menu", menu_cmd))
    application.add_handler(CommandHandler("meni", menu_cmd))
    application.add_handler(CommandHandler("mood", mood_cmd))
    application.add_handler(CommandHandler("history", history_cmd))
    application.add_handler(CommandHandler("weekly", weekly_cmd))
    application.add_handler(CommandHandler("tests", tests_cmd))

    # inline gumbi
    application.add_handler(CallbackQueryHandler(handle_button))

    # tekst poruke
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await application.initialize()
    await application.start()

    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        raise RuntimeError("RENDER_EXTERNAL_URL nije postavljen!")

    webhook_url = f"{external_url}/webhook/{TELEGRAM_TOKEN}"
    print(f"🌍 Registriram webhook: {webhook_url}")
    await application.bot.set_webhook(url=webhook_url)


def start_flask() -> None:
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

    # drži event loop živim
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
