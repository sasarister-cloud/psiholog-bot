import os
import json
import asyncio
from datetime import datetime, timedelta, time as dtime
import sys
from typing import Dict, Any, List

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from openai import OpenAI

# =====================================================
# 1. .env I OSNOVNE POSTAVKE
# =====================================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN nije postavljen u .env")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY nije postavljen u .env")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID nije postavljen u .env (tvoj Telegram user ID)")

ADMIN_ID = int(ADMIN_ID_RAW)

client = OpenAI(api_key=OPENAI_API_KEY)

USERS_FILE = "users.json"
CONV_FILE = "conversations.json"

# =====================================================
# 2. TERAPEUTI, MODOVI, PROMPTOVI
# =====================================================

BASE_SYSTEM_PROMPT = (
    "Ti si empatičan, topao i stručan psiholog. "
    "Odgovaraš na hrvatskom jeziku, jasno i razumljivo. "
    "Pomažeš korisniku da razumije svoje emocije, predlažeš zdrave obrasce razmišljanja, "
    "postavljaš pitanja koja potiču na refleksiju. Ne dijagnosticiraš mentalne poremećaje "
    "i ne daješ medicinske savjete. Uvijek si podržavajući, nenametljiv i diskretan."
)

THERAPISTS = {
    "standard": "Odgovaraj kao smiren, topao, klinički psiholog.",
    "coach": "Odgovaraj kao direktan, motivirajući mentalni coach, fokusiran na akciju.",
    "mindfulness": "Odgovaraj kao psiholog koji koristi mindfulness, disanje i prihvaćanje.",
}

THERAPY_MODES = {
    "NONE": "",
    "CBT": "Koristi principe kognitivno-bihevioralne terapije (CBT): identificiraj misli, emocije i ponašanja, prepoznaj kognitivne distorzije i predloži alternativne, realističnije misli.",
    "ACT": "Koristi principe ACT terapije: prihvaćanje neugodnih emocija, razdvajanje od misli, fokus na vrijednosti i posvećeno djelovanje.",
    "DBT": "Koristi principe DBT-a: regulacija emocija, tolerancija na stres, mindfulness i interpersonalne vještine.",
}

# Jednostavni psihološki testovi (PHQ-9, GAD-7 skraceni)
TESTS = {
    "PHQ9": {
        "title": "PHQ-9 – procjena depresivnih simptoma",
        "description": "Odgovori za posljednja 2 tjedna. Skala: 0=nikad, 1=nekoliko dana, 2=više od pola dana, 3=gotovo svaki dan.",
        "questions": [
            "Malo zanimanja ili užitka u stvarima?",
            "Osjećaj potištenosti, depresije ili beznađa?",
            "Poteškoće sa spavanjem ili prespavljivanje?",
            "Umor ili manjak energije?",
            "Loš apetit ili prejedanje?",
            "Loše mišljenje o sebi, osjećaj da si neuspjeh?",
            "Poteškoće s koncentracijom?",
            "Krećeš se ili govoriš toliko sporo da su to drugi primijetili, ili obratno – nemir, nemogućnost mirovanja?",
            "Misli da bi bilo bolje da nisi živ/a ili da se ozlijediš?",
        ],
    },
    "GAD7": {
        "title": "GAD-7 – procjena anksioznosti",
        "description": "Odgovori za posljednja 2 tjedna. Skala: 0=nikad, 1=nekoliko dana, 2=više od pola dana, 3=gotovo svaki dan.",
        "questions": [
            "Osjećaj nervoze, tjeskobe ili napetosti?",
            "Nemogućnost zaustavljanja ili kontroliranja brige?",
            "Pretjerana briga o različitim stvarima?",
            "Poteškoće s opuštanjem?",
            "Nemir do te mjere da se teško možeš smiriti?",
            "Lako se uznemiriš ili razljutiš?",
            "Osjećaj kao da će se dogoditi nešto strašno?",
        ],
    },
}

# =====================================================
# 3. RAD S users.json I conversations.json
# =====================================================

def load_users() -> Dict[str, Any]:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_users(data: Dict[str, Any]) -> None:
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def load_conversations() -> Dict[str, List[Dict[str, Any]]]:
    if not os.path.exists(CONV_FILE):
        return {}
    try:
        with open(CONV_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_conversations(data: Dict[str, List[Dict[str, Any]]]) -> None:
    with open(CONV_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def append_message(chat_id_str: str, role: str, text: str) -> None:
    conv = load_conversations()
    if chat_id_str not in conv:
        conv[chat_id_str] = []
    conv[chat_id_str].append(
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "role": role,
            "text": text,
        }
    )
    save_conversations(conv)


# =====================================================
# 4. POMOĆNE FUNKCIJE
# =====================================================

def get_user(chat_id_str: str) -> Dict[str, Any] | None:
    users = load_users()
    return users.get(chat_id_str)


def save_user(chat_id_str: str, user_data: Dict[str, Any]) -> None:
    users = load_users()
    users[chat_id_str] = user_data
    save_users(users)


def is_admin(update: Update) -> bool:
    return update.effective_chat.id == ADMIN_ID


async def send_long(
    chat_id: int,
    text: str,
    context: ContextTypes.DEFAULT_TYPE,
    chunk_size: int = 3500,
):
    if len(text) <= chunk_size:
        await context.bot.send_message(chat_id, text)
        return
    for i in range(0, len(text), chunk_size):
        await context.bot.send_message(chat_id, text[i: i + chunk_size])
        await asyncio.sleep(0.2)


def build_system_prompt(user: Dict[str, Any]) -> str:
    therapist_key = user.get("therapist", "standard")
    therapist_style = THERAPISTS.get(therapist_key, THERAPISTS["standard"])
    mode_key = user.get("therapy_mode", "NONE")
    mode_text = THERAPY_MODES.get(mode_key, "")

    profile = user.get("profile", {})
    parts = []
    if profile.get("age"):
        parts.append(f"korisnik ima {profile['age']} godina")
    if profile.get("goals"):
        parts.append(f"ciljevi rada: {profile['goals']}")
    if profile.get("topics"):
        parts.append(f"ključne teme: {profile['topics']}")
    profile_text = ""
    if parts:
        profile_text = "Osnovni podaci o korisniku: " + "; ".join(parts) + "."

    prompt = BASE_SYSTEM_PROMPT
    prompt += " " + therapist_style
    if mode_text:
        prompt += " " + mode_text
    if profile_text:
        prompt += " " + profile_text
    return prompt


async def ai_chat_reply(user: Dict[str, Any], user_text: str) -> str:
    """Glavni AI odgovor psihologa na poruku korisnika."""
    try:
        system_prompt = build_system_prompt(user)
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            max_tokens=1500,
            temperature=0.7,
        )
        return completion.choices[0].message.content
    except Exception as e:
        return f"⚠️ Greška AI servisa: {e}"


async def ai_emotion_tone(text: str) -> str:
    """Kratka analiza emocionalnog tona poruke (za internu uporabu)."""
    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Analiziraj emocionalni ton teksta. "
                        "Vrati jednu kratku rečenicu na hrvatskom opisujući dominantne emocije."
                    ),
                },
                {"role": "user", "content": text},
            ],
            max_tokens=60,
            temperature=0.3,
        )
        return completion.choices[0].message.content
    except Exception:
        return ""


# =====================================================
# 5. GLAVNI IZBORNIK I INLINE GUMBI
# =====================================================

def build_main_menu(user: Dict[str, Any]) -> InlineKeyboardMarkup:
    premium = bool(user.get("premium", False))
    buttons: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton("📓 Dnevnik emocija", callback_data="OPEN_MOOD_DIARY")],
        [
            InlineKeyboardButton("🧠 AI psiholog", callback_data="CHOOSE_THERAPIST"),
            InlineKeyboardButton("🎯 Terapijski mod", callback_data="CHOOSE_MODE"),
        ],
        [InlineKeyboardButton("📊 Analiza emocija", callback_data="EMOTION_ANALYSIS")],
        [
            InlineKeyboardButton("⏰ Dnevna provjera", callback_data="TOGGLE_DAILY"),
            InlineKeyboardButton("🗂 Arhiva", callback_data="SHOW_HISTORY"),
        ],
        [
            InlineKeyboardButton("🎲 Dnevni izazov", callback_data="DAILY_CHALLENGE"),
            InlineKeyboardButton("🧪 Testovi", callback_data="TEST_MENU"),
        ],
        [InlineKeyboardButton("🚨 Hitni način", callback_data="EMERGENCY_MODE")],
    ]
    if not premium:
        buttons.append(
            [InlineKeyboardButton("⭐ Premium info", callback_data="PREMIUM_INFO")]
        )
    return InlineKeyboardMarkup(buttons)


async def send_main_menu(chat_id: int, user: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id,
        "🧭 *Glavni izbornik*\nOdaberi što želiš:",
        parse_mode="Markdown",
        reply_markup=build_main_menu(user),
    )


# =====================================================
# 6. KOMANDE: /start, /help, /status, /profile, /menu, ...
# =====================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)
    full_name = update.effective_user.full_name or "Korisnik"

    users = load_users()
    if chat_id_str not in users:
        # Novi korisnik
        users[chat_id_str] = {
            "name": full_name,
            "approved": False,
            "subscription_until": None,
            "waiting": True,
            "premium": False,
            "therapist": "standard",
            "therapy_mode": "NONE",
            "profile_step": 0,
            "profile": {},
            "mood_log": [],
            "daily_check": False,
            "emergency_mode": False,
            "test_state": None,  # {type, index, answers}
            "mood_pending_rating": None,
        }
        save_users(users)

        await update.message.reply_text(
            "👋 Dobrodošao/la! Tvoj zahtjev je zaprimljen.\n"
            "Administrator će te odobriti, a onda možeš koristiti AI psihologa."
        )

        await context.bot.send_message(
            ADMIN_ID,
            f"🆕 Novi korisnik traži odobrenje:\n"
            f"👤 {full_name}\n"
            f"🆔 ID: `{chat_id_str}`",
            parse_mode="Markdown",
        )
        return

    user = users[chat_id_str]

    if not user.get("approved", False):
        await update.message.reply_text(
            "⏳ Još čekaš odobrenje administratora. "
            "Kad budeš odobren/a, obavijestit ću te."
        )
        return

    expiry_str = user.get("subscription_until")
    if not expiry_str:
        await update.message.reply_text(
            "⚠️ Problem s pretplatom. Javite se administratoru."
        )
        return

    expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
    if expiry < datetime.now():
        await update.message.reply_text(
            "❌ Tvoja pretplata je istekla. Javi se administratoru za produženje."
        )
        return

    await update.message.reply_text(
        f"👋 Dobrodošao natrag, {user['name']}!\n"
        "Kako se danas osjećaš?"
    )
    await send_main_menu(chat_id, user, context)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 *Komande:*\n"
        "/start – pokretanje / nastavak rada\n"
        "/help – ova pomoć\n"
        "/status – stanje pretplate i premiuma\n"
        "/profile – kratka forma o tebi\n"
        "/menu – prikaži glavni izbornik\n"
        "/mood – brzi unos raspoloženja\n"
        "/history – sažetak arhive razgovora\n"
        "/weekly – tjedni psihološki izvještaj\n"
        "/tests – psihološki testovi (PHQ-9, GAD-7)\n\n"
        "🛠 *Admin:*\n"
        "/approve <user_id> [dani]\n"
        "/pending – lista korisnika na čekanju\n"
        "/extend <user_id> <dani>\n"
        "/setpremium <user_id> <on/off>\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    user = get_user(chat_id_str)
    if not user:
        await update.message.reply_text("Nisi registriran. Pošalji /start.")
        return

    if not user.get("approved", False):
        await update.message.reply_text("Još čekaš odobrenje administratora.")
        return

    expiry_str = user.get("subscription_until")
    if not expiry_str:
        await update.message.reply_text(
            "⚠️ Problem s pretplatom. Javite se administratoru."
        )
        return

    expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
    days_left = (expiry - datetime.now()).days
    premium_flag = "DA" if user.get("premium") else "NE"

    await update.message.reply_text(
        f"📅 Pretplata vrijedi do: {expiry_str}\n"
        f"Preostalo dana: {max(days_left, 0)}\n"
        f"⭐ Premium: {premium_flag}"
    )


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    user = get_user(chat_id_str)
    if not user:
        await update.message.reply_text("Prvo pošalji /start.")
        return

    user["profile_step"] = 1
    user.setdefault("profile", {})
    save_user(chat_id_str, user)

    await update.message.reply_text(
        "📝 Krenimo s kratkom formom.\n\n"
        "1️⃣ Koliko imaš godina?"
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    user = get_user(chat_id_str)
    if not user:
        await update.message.reply_text("Prvo pošalji /start.")
        return
    await send_main_menu(update.effective_chat.id, user, context)


async def mood_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    user = get_user(chat_id_str)
    if not user:
        await update.message.reply_text("Prvo pošalji /start.")
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
    await update.message.reply_text(
        "📓 Kako se osjećaš (1 – jako loše, 5 – jako dobro)?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    conv = load_conversations()
    msgs = conv.get(chat_id_str, [])
    if not msgs:
        await update.message.reply_text("🗂 Nema arhiviranih razgovora.")
        return
    tail = msgs[-20:]
    lines = []
    for m in tail:
        ts = m.get("timestamp", "")
        role = "Ti" if m.get("role") == "user" else "Psiholog"
        tx = m.get("text", "")
        if len(tx) > 120:
            tx = tx[:120] + "…"
        lines.append(f"[{ts}] {role}: {tx}")
    txt = "🗂 *Zadnjih 20 poruka:*\n\n" + "\n".join(lines)
    await update.message.reply_text(txt, parse_mode="Markdown")


async def weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tjedni psihološki izvještaj: analiza dnevnika + zadnjih poruka."""
    chat_id_str = str(update.effective_chat.id)
    user = get_user(chat_id_str)
    if not user:
        await update.message.reply_text("Prvo pošalji /start.")
        return

    mood_log = user.get("mood_log", [])
    conv = load_conversations().get(chat_id_str, [])

    last_moods = mood_log[-21:]
    last_msgs = conv[-40:]

    mood_summary = []
    for m in last_moods:
        mood_summary.append(
            f"{m['timestamp']}: {m['rating']} – {m.get('note','')[:80]}"
        )
    mood_text = "\n".join(mood_summary)

    msg_summary = []
    for m in last_msgs:
        if m.get("role") == "user":
            msg_summary.append(f"{m['timestamp']}: {m['text'][:120]}")
    msgs_text = "\n".join(msg_summary)

    prompt = (
        "Ovo su unosi iz dnevnika emocija i korisnikove poruke u zadnjem periodu.\n\n"
        "Dnevnik emocija:\n"
        f"{mood_text}\n\n"
        "Poruke korisnika:\n"
        f"{msgs_text}\n\n"
        "Izradi tjedni psihološki izvještaj: sažetak stanja, primjetni obrasci, "
        "snage korisnika i 3–5 konkretnih prijedloga za idući tjedan. "
        "Odgovori jasno, u nekoliko odlomaka i listom."
    )

    analysis = await ai_chat_reply(user, prompt)
    await send_long(update.effective_chat.id, "📅 *Tjedni izvještaj:*\n\n" + analysis, context)


async def tests_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = str(update.effective_chat.id)
    user = get_user(chat_id_str)
    if not user:
        await update.message.reply_text("Prvo pošalji /start.")
        return

    keyboard = [
        [InlineKeyboardButton("PHQ-9 (depresija)", callback_data="TEST_PHQ9")],
        [InlineKeyboardButton("GAD-7 (anksioznost)", callback_data="TEST_GAD7")],
    ]
    await update.message.reply_text(
        "🧪 Odaberi psihološki test:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =====================================================
# 7. ADMIN KOMANDE (APPROVE, PENDING, EXTEND, SETPREMIUM)
# =====================================================

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Uporaba: /approve <user_id> [dani]")
        return
    user_id = args[0]
    days = int(args[1]) if len(args) > 1 else 7

    users = load_users()
    if user_id not in users:
        await update.message.reply_text("Korisnik ne postoji.")
        return

    until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    u = users[user_id]
    u["approved"] = True
    u["waiting"] = False
    u["subscription_until"] = until
    u.setdefault("premium", True)
    users[user_id] = u
    save_users(users)

    await update.message.reply_text(
        f"✅ Odobren {u['name']} (ID: {user_id}) do {until}."
    )
    # obavijest korisniku
    try:
        await context.bot.send_message(
            int(user_id),
            f"✅ Tvoj pristup AI psihologu je odobren do *{until}*.\n"
            "Pošalji /start da kreneš.",
            parse_mode="Markdown",
        )
    except Exception:
        pass


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    users = load_users()
    waiting = [ (uid, u) for uid, u in users.items() if u.get("waiting") ]
    if not waiting:
        await update.message.reply_text("Nema korisnika na čekanju.")
        return
    lines = []
    for uid, u in waiting:
        lines.append(f"{u['name']} (ID: {uid})")
    await update.message.reply_text("🕒 Na čekanju:\n" + "\n".join(lines))


async def extend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Uporaba: /extend <user_id> <dani>")
        return
    user_id, days_raw = args[0], args[1]
    try:
        days = int(days_raw)
    except ValueError:
        await update.message.reply_text("Dani moraju biti broj.")
        return

    users = load_users()
    if user_id not in users:
        await update.message.reply_text("Korisnik ne postoji.")
        return

    cur = users[user_id].get("subscription_until")
    if not cur:
        base = datetime.now()
    else:
        base = datetime.strptime(cur, "%Y-%m-%d")
    new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%d")
    users[user_id]["subscription_until"] = new_exp
    save_users(users)

    await update.message.reply_text(
        f"📅 Pretplata produžena do {new_exp}."
    )


async def setpremium_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Uporaba: /setpremium <user_id> <on/off>")
        return
    user_id, flag = args[0], args[1].lower()
    if flag not in ("on", "off"):
        await update.message.reply_text("Drugi argument mora biti on/off.")
        return

    users = load_users()
    if user_id not in users:
        await update.message.reply_text("Korisnik ne postoji.")
        return

    users[user_id]["premium"] = (flag == "on")
    save_users(users)
    await update.message.reply_text(
        f"⭐ Premium za {users[user_id]['name']} postavljen na {flag.upper()}."
    )


# =====================================================
# 8. DNEVNIK EMOCIJA, ANALIZA, DNEVNA PROVJERA
# =====================================================

def add_mood_entry(user: Dict[str, Any], rating: int, note: str = ""):
    log = user.setdefault("mood_log", [])
    log.append(
        {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "rating": int(rating),
            "note": note,
        }
    )


async def send_emotion_analysis(chat_id: int, user: Dict[str, Any], context: ContextTypes.DEFAULT_TYPE):
    log = user.get("mood_log", [])
    if not log:
        await context.bot.send_message(chat_id, "📊 Nema unosa u dnevniku emocija.")
        return

    last = log[-21:]
    lines = [
        f"{e['timestamp']}: {e['rating']} – {e.get('note','')[:80]}"
        for e in last
    ]
    joined = "\n".join(lines)

    prompt = (
        "Na temelju ovih unosa u dnevniku emocija:\n\n"
        f"{joined}\n\n"
        "Analiziraj kako se korisnik otprilike osjeća kroz vrijeme, moguće okidače, "
        "obrasce razmišljanja i predloži 3–5 konkretnih koraka za brigu o sebi."
    )

    result = await ai_chat_reply(user, prompt)
    await send_long(chat_id, "📊 *Analiza emocija:*\n\n" + result, context)


async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    chat_id_str = str(chat_id)
    user = get_user(chat_id_str)
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


def schedule_daily(app: Application, chat_id: int):
    # svaki dan u 20:00 po server vremenu
    app.job_queue.run_daily(
        daily_check_job,
        time=dtime(hour=20, minute=0),
        chat_id=chat_id,
        name=f"daily_{chat_id}",
    )


# =====================================================
# 9. PSIH TESTOVI – PHQ-9, GAD-7
# =====================================================

def start_test(user: Dict[str, Any], test_key: str):
    test = TESTS[test_key]
    user["test_state"] = {
        "type": test_key,
        "index": 0,
        "answers": [],
    }


def score_test(test_type: str, answers: List[int]) -> str:
    total = sum(answers)
    if test_type == "PHQ9":
        if total <= 4:
            level = "minimalni ili nema znakova depresivnosti"
        elif total <= 9:
            level = "blagi simptomi"
        elif total <= 14:
            level = "umjereni simptomi"
        elif total <= 19:
            level = "umjereno teški simptomi"
        else:
            level = "teški simptomi"
        return f"Ukupni rezultat PHQ-9: {total} – {level}."
    elif test_type == "GAD7":
        if total <= 4:
            level = "minimalna anksioznost"
        elif total <= 9:
            level = "blaga anksioznost"
        elif total <= 14:
            level = "umjerena anksioznost"
        else:
            level = "teška anksioznost"
        return f"Ukupni rezultat GAD-7: {total} – {level}."
    else:
        return f"Ukupni rezultat: {total}."


# =====================================================
# 10. HANDLE MESSAGE – GLAVNA LOGIKA
# =====================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)
    text = (update.message.text or "").strip()

    user = get_user(chat_id_str)
    if not user:
        await update.message.reply_text("Prvo pošalji /start.")
        return

    # Provjera odobrenja/pretplate
    if not user.get("approved", False):
        await update.message.reply_text("Još čekaš odobrenje administratora.")
        return
    expiry_str = user.get("subscription_until")
    if not expiry_str:
        await update.message.reply_text("⚠️ Problem s pretplatom. Javite se administratoru.")
        return
    expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
    if expiry < datetime.now():
        await update.message.reply_text("❌ Tvoja pretplata je istekla.")
        return

    # 1) Profil – forma
    step = user.get("profile_step", 0)
    if step == 1:
        user.setdefault("profile", {})["age"] = text
        user["profile_step"] = 2
        save_user(chat_id_str, user)
        await update.message.reply_text("2️⃣ Koji su ti glavni ciljevi rada na sebi?")
        return
    elif step == 2:
        user.setdefault("profile", {})["goals"] = text
        user["profile_step"] = 3
        save_user(chat_id_str, user)
        await update.message.reply_text("3️⃣ Koje teme ili poteškoće su ti trenutno najvažnije?")
        return
    elif step == 3:
        user.setdefault("profile", {})["topics"] = text
        user["profile_step"] = 0
        save_user(chat_id_str, user)
        await update.message.reply_text(
            "✅ Hvala ti! Profil je spremljen. To će pomoći da odgovori budu prilagođeniji tebi."
        )
        return

    # 2) Dnevnik emocija – bilješka nakon ocjene
    if user.get("mood_pending_rating") is not None:
        rating = user["mood_pending_rating"]
        add_mood_entry(user, rating, note=text)
        user["mood_pending_rating"] = None
        save_user(chat_id_str, user)
        await update.message.reply_text("📓 Bilješka dodana uz tvoj unos raspoloženja. Hvala što dijeliš.")
        return

    # 3) Psihološki test u tijeku?
    test_state = user.get("test_state")
    if test_state:
        test_type = test_state["type"]
        index = test_state["index"]
        answers = test_state["answers"]
        test = TESTS[test_type]

        # očekujemo broj 0–3
        try:
            val = int(text)
        except ValueError:
            await update.message.reply_text("Molim upiši broj 0, 1, 2 ili 3.")
            return
        if val < 0 or val > 3:
            await update.message.reply_text("Molim upiši broj između 0 i 3.")
            return

        answers.append(val)
        test_state["index"] = index + 1

        if test_state["index"] >= len(test["questions"]):
            # kraj testa
            user["test_state"] = None
            save_user(chat_id_str, user)
            result_text = score_test(test_type, answers)
            await update.message.reply_text("✅ Test je dovršen.\n" + result_text)
            return
        else:
            # sljedeće pitanje
            q = test["questions"][test_state["index"]]
            await update.message.reply_text(
                f"Sljedeće pitanje ({test_state['index']+1}/{len(test['questions'])}):\n{q}\n\n"
                "Odgovori brojem 0–3."
            )
            return

    # 4) Hitni način
    if user.get("emergency_mode"):
        crisis_text = (
            "🚨 *Hitni način je uključen.*\n\n"
            "Žao mi je što prolaziš kroz teško razdoblje. "
            "Važno je znati da sam ja samo AI i ne mogu zamijeniti stručnu pomoć.\n\n"
            "Ako razmišljaš o samoozljeđivanju ili si u opasnosti:\n"
            "• Odmah nazovi hitnu službu (112) ili najbližu hitnu psihijatriju.\n"
            "• Javi se osobi od povjerenja (prijatelj, član obitelji).\n\n"
            "Ovdje možeš podijeliti kako se osjećaš – poslužit ću kao siguran prostor, "
            "ali ne mogu dati medicinski savjet.\n\n"
            "Za izlazak iz hitnog načina: pošalji /start i /menu kad budeš spreman/na."
        )
        await send_long(chat_id, crisis_text, context)
        append_message(chat_id_str, "user", text)
        append_message(chat_id_str, "assistant", crisis_text)
        return

    # 5) Regularan AI odgovor + bilježenje
    emo_tone = await ai_emotion_tone(text)
    chat_prompt = f"Korisnik kaže: {text}\n\nOdgovori kao empatičan psiholog."
    reply = await ai_chat_reply(user, chat_prompt)

    # snimi u arhivu
    append_message(chat_id_str, "user", text)
    if emo_tone:
        append_message(chat_id_str, "assistant", f"[emotional_tone] {emo_tone}")
    append_message(chat_id_str, "assistant", reply)

    if emo_tone:
        reply = f"_(Primjećujem otprilike: {emo_tone})_\n\n" + reply

    await send_long(chat_id, reply, context)


# =====================================================
# 11. INLINE CALLBACK HANDLER
# =====================================================

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)

    user = get_user(chat_id_str)
    if not user:
        await query.edit_message_text("Prvo pošalji /start.")
        return

    premium = bool(user.get("premium", False))

    # Dnevnik emocija – izbor ocjene
    if data.startswith("MOOD_"):
        rating = int(data.replace("MOOD_", ""))
        add_mood_entry(user, rating, note="")
        user["mood_pending_rating"] = rating
        save_user(chat_id_str, user)
        await query.edit_message_text(
            f"📓 Zabilježio sam ocjenu {rating}.\n"
            "Ako želiš, napiši poruku s kratkim opisom što se dogodilo – "
            "ta poruka će biti spremljena uz ovaj unos."
        )
        return

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

    if data == "EMOTION_ANALYSIS":
        if not premium:
            await query.edit_message_text(
                "📊 Analiza emocija dostupna je u ⭐ premium verziji.\n"
                "Javi se administratoru za nadogradnju."
            )
            return
        await query.edit_message_text("⏳ Radim analizu tvojih emocionalnih unosa…")
        await send_emotion_analysis(chat_id, user, context)
        return

    if data == "TOGGLE_DAILY":
        user["daily_check"] = not user.get("daily_check", False)
        save_user(chat_id_str, user)
        if user["daily_check"]:
            schedule_daily(context.application, chat_id)
            await query.edit_message_text(
                "⏰ Dnevna emocionalna provjera je *uključena* (svaki dan u 20:00).",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                "⏰ Dnevna emocionalna provjera je *isključena*.",
                parse_mode="Markdown",
            )
        return

    if data == "SHOW_HISTORY":
        conv = load_conversations()
        msgs = conv.get(chat_id_str, [])
        if not msgs:
            await query.edit_message_text("🗂 Nema arhive.")
            return
        tail = msgs[-20:]
        lines = []
        for m in tail:
            ts = m.get("timestamp", "")
            role = "Ti" if m.get("role") == "user" else "Psiholog"
            tx = m.get("text", "")
            if len(tx) > 80:
                tx = tx[:80] + "…"
            lines.append(f"[{ts}] {role}: {tx}")
        txt = "🗂 *Zadnjih 20 poruka:*\n\n" + "\n".join(lines)
        await query.edit_message_text(txt, parse_mode="Markdown")
        return

    if data == "EMERGENCY_MODE":
        user["emergency_mode"] = True
        save_user(chat_id_str, user)
        crisis_text = (
            "🚨 *Hitni način uključen.*\n\n"
            "Ako si u neposrednoj opasnosti ili razmišljaš o samoozljeđivanju, "
            "odmah nazovi 112 ili lokalnu hitnu psihijatriju.\n"
            "Također, javi se osobi od povjerenja.\n\n"
            "Ovdje možeš napisati kako se osjećaš, ali imaj na umu da sam AI i "
            "ne mogu zamijeniti stručnu pomoć."
        )
        await query.edit_message_text(crisis_text, parse_mode="Markdown")
        return

    if data == "CHOOSE_THERAPIST":
        if not premium:
            await query.edit_message_text(
                "🧠 Različiti AI psiholozi dostupni su u ⭐ premium verziji."
            )
            return
        kb = [
            [InlineKeyboardButton("🤝 Empatični terapeut", callback_data="THER_standard")],
            [InlineKeyboardButton("💡 Direktni coach", callback_data="THER_coach")],
            [InlineKeyboardButton("🧘 Mindfulness psiholog", callback_data="THER_mindfulness")],
        ]
        await query.edit_message_text(
            "🧠 Odaberi stil AI psihologa:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("THER_"):
        key = data.replace("THER_", "")
        if key not in THERAPISTS:
            await query.edit_message_text("Nepoznat tip psihologa.")
            return
        user["therapist"] = key
        save_user(chat_id_str, user)
        names = {
            "standard": "empatični terapeut",
            "coach": "direktni coach",
            "mindfulness": "mindfulness psiholog",
        }
        await query.edit_message_text(
            f"🧠 Stil postavljen na: *{names.get(key, key)}*.",
            parse_mode="Markdown",
        )
        return

    if data == "CHOOSE_MODE":
        if not premium:
            await query.edit_message_text(
                "🎯 Terapijski modovi (CBT, ACT, DBT) dostupni su u ⭐ premium verziji."
            )
            return
        kb = [
            [
                InlineKeyboardButton("CBT", callback_data="MODE_CBT"),
                InlineKeyboardButton("ACT", callback_data="MODE_ACT"),
                InlineKeyboardButton("DBT", callback_data="MODE_DBT"),
            ],
            [InlineKeyboardButton("Bez moda", callback_data="MODE_NONE")],
        ]
        await query.edit_message_text(
            "🎯 Odaberi terapijski mod:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("MODE_"):
        mode = data.replace("MODE_", "")
        if mode == "NONE":
            user["therapy_mode"] = "NONE"
            save_user(chat_id_str, user)
            await query.edit_message_text(
                "🎯 Terapijski mod je isključen."
            )
            return
        if mode not in THERAPY_MODES:
            await query.edit_message_text("Nepoznat terapijski mod.")
            return
        user["therapy_mode"] = mode
        save_user(chat_id_str, user)
        await query.edit_message_text(
            f"🎯 Terapijski mod postavljen na: *{mode}*.",
            parse_mode="Markdown",
        )
        return

    if data == "PREMIUM_INFO":
        await query.edit_message_text(
            "⭐ *Premium uključuje:*\n"
            "• Analizu emocija\n"
            "• Različite AI psihologe\n"
            "• Napredne terapijske modove (CBT, ACT, DBT)\n"
            "• Dnevnu emocionalnu provjeru\n"
            "• Psihološke testove\n\n"
            "Za nadogradnju javi se administratoru."
        )
        return

    if data == "DAILY_CHALLENGE":
        prompt = (
            "Smisli jedan mali, jednostavan dnevni izazov za mentalno zdravlje "
            "(npr. kratka vježba zahvalnosti, disanja, kontakt s nekim bliskim). "
            "Odgovori kratko, 2–3 rečenice, na hrvatskom."
        )
        challenge = await ai_chat_reply(user, prompt)
        await query.edit_message_text("🎲 *Dnevni izazov:*\n\n" + challenge, parse_mode="Markdown")
        return

    if data == "TEST_MENU":
        kb = [
            [InlineKeyboardButton("PHQ-9 (depresija)", callback_data="TEST_PHQ9")],
            [InlineKeyboardButton("GAD-7 (anksioznost)", callback_data="TEST_GAD7")],
        ]
        await query.edit_message_text(
            "🧪 Odaberi psihološki test:", reply_markup=InlineKeyboardMarkup(kb)
        )
        return

    if data.startswith("TEST_"):
        test_key = data.replace("TEST_", "")
        if test_key not in TESTS:
            await query.edit_message_text("Nepoznat test.")
            return
        test = TESTS[test_key]
        start_test(user, test_key)
        save_user(chat_id_str, user)
        await query.edit_message_text(
            f"🧪 {test['title']}\n\n{test['description']}\n\n"
            f"Prvo pitanje (1/{len(test['questions'])}):\n{test['questions'][0]}\n\n"
            "Odgovori brojem 0–3."
        )
        return


# =====================================================
# 12. WEB ADMIN PANEL (Flask + login + HTTPS + superadmin)
# =====================================================

def run_admin_panel():
    from flask import Flask, request, redirect, url_for, render_template_string, session

    app = Flask(__name__)

    # Konfiguracija iz .env (s default vrijednostima za lokalni rad)
    app.secret_key = os.getenv("ADMIN_WEB_SECRET", "change_this_secret")
    app.config["SESSION_COOKIE_SECURE"] = False  # jer često radimo i bez pravog SSL-a
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    ADMIN_PANEL_USER = os.getenv("ADMIN_PANEL_USER", "admin")
    ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "admin123")

    SUPERADMIN_USER = os.getenv("SUPERADMIN_USER", "superadmin")
    SUPERADMIN_PASSWORD = os.getenv("SUPERADMIN_PASSWORD", "superadmin123")

    from functools import wraps

    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return wrapper

    TEMPLATE = """
    <!doctype html>
    <html lang="hr">
    <head>
        <meta charset="utf-8">
        <title>Psiholog Bot – Admin</title>
        <style>
            body { font-family: Arial, sans-serif; padding: 20px; }
            table { border-collapse: collapse; width: 100%; margin-bottom: 30px; }
            th, td { padding: 8px 10px; border: 1px solid #ccc; font-size: 14px; }
            th { background: #f0f0f0; }
            h1, h2 { margin-top: 0; }
            form { display: inline-block; margin: 0 5px; }
            .badge { padding: 2px 6px; border-radius: 4px; font-size: 12px; }
            .badge-ok { background: #d4edda; color: #155724; }
            .badge-wait { background: #fff3cd; color: #856404; }
            .badge-no { background: #f8d7da; color: #721c24; }
            pre { background: #f8f8f8; padding: 10px; max-height: 300px; overflow: auto; }
            a { color: #007bff; text-decoration: none; }
            a:hover { text-decoration: underline; }
            .topbar { margin-bottom: 20px; }
        </style>
    </head>
    <body>
        <div class="topbar">
            <h1>Psiholog Bot – Admin panel</h1>
            <p>
                Prijavljen kao: <strong>{{ username }}</strong>
                {% if is_superadmin %}
                    <span class="badge badge-ok">SUPERADMIN</span>
                {% endif %}
                &nbsp;|&nbsp;
                <a href="{{ url_for('logout') }}">Odjava</a>
            </p>
        </div>

        <h2>Korisnici</h2>
        <table>
            <tr>
                <th>ID</th>
                <th>Ime</th>
                <th>Odobren</th>
                <th>Na čekanju</th>
                <th>Pretplata do</th>
                <th>Premium</th>
                <th>Akcije</th>
            </tr>
            {% for uid, u in users.items() %}
            <tr>
                <td>{{ uid }}</td>
                <td>{{ u.get("name","") }}</td>
                <td>
                    {% if u.get("approved") %}
                        <span class="badge badge-ok">DA</span>
                    {% else %}
                        <span class="badge badge-no">NE</span>
                    {% endif %}
                </td>
                <td>
                    {% if u.get("waiting") %}
                        <span class="badge badge-wait">DA</span>
                    {% else %}
                        NE
                    {% endif %}
                </td>
                <td>{{ u.get("subscription_until","-") }}</td>
                <td>
                    {% if u.get("premium") %}
                        <span class="badge badge-ok">DA</span>
                    {% else %}
                        <span class="badge badge-no">NE</span>
                    {% endif %}
                </td>
                <td>
                    <form method="post" action="{{ url_for('approve_user') }}">
                        <input type="hidden" name="user_id" value="{{ uid }}">
                        <input type="number" name="days" value="7" style="width:60px">
                        <button type="submit">Approve</button>
                    </form>
                    <form method="post" action="{{ url_for('extend_user') }}">
                        <input type="hidden" name="user_id" value="{{ uid }}">
                        <input type="number" name="days" value="7" style="width:60px">
                        <button type="submit">Extend</button>
                    </form>
                    <form method="post" action="{{ url_for('toggle_premium') }}">
                        <input type="hidden" name="user_id" value="{{ uid }}">
                        <input type="hidden" name="flag" value="{{ 'off' if u.get('premium') else 'on' }}">
                        <button type="submit">{{ 'Premium OFF' if u.get('premium') else 'Premium ON' }}</button>
                    </form>
                    <form method="get" action="{{ url_for('user_history') }}">
                        <input type="hidden" name="user_id" value="{{ uid }}">
                        <button type="submit">Povijest</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
        </table>

        <h2>Korisnici na čekanju</h2>
        <ul>
            {% for uid, u in users.items() if u.get("waiting") %}
                <li>{{ u.get("name","") }} ({{ uid }})</li>
            {% else %}
                <li>Nema korisnika na čekanju.</li>
            {% endfor %}
        </ul>
    </body>
    </html>
    """

    LOGIN_TEMPLATE = """
    <!doctype html>
    <html lang="hr">
    <head>
        <meta charset="utf-8">
        <title>Psiholog Bot – Login</title>
        <style>
            body { font-family: Arial, sans-serif; background: #f5f5f5; }
            .box {
                max-width: 360px;
                margin: 80px auto;
                background: #fff;
                padding: 25px;
                border-radius: 8px;
                box-shadow: 0 0 8px rgba(0,0,0,0.1);
            }
            h1 { margin-top: 0; font-size: 20px; text-align: center; }
            label { display:block; margin-top:10px; }
            input[type=text], input[type=password] {
                width:100%; padding:8px; margin-top:5px;
                border:1px solid #ccc; border-radius:4px;
            }
            button {
                margin-top:15px; width:100%; padding:8px;
                border:none; border-radius:4px;
                background:#007bff; color:#fff; font-weight:bold;
                cursor:pointer;
            }
            button:hover { background:#0056b3; }
            .error { color:#c00; margin-top:10px; text-align:center; }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>Psiholog Bot – Admin login</h1>
            <form method="post">
                <label>Korisničko ime</label>
                <input type="text" name="username" autocomplete="username" required>
                <label>Lozinka</label>
                <input type="password" name="password" autocomplete="current-password" required>
                <button type="submit">Prijava</button>
            </form>
            {% if error %}
                <div class="error">{{ error }}</div>
            {% endif %}
        </div>
    </body>
    </html>
    """

    from functools import wraps

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            username = request.form.get("username")
            password = request.form.get("password")

            if username == SUPERADMIN_USER and password == SUPERADMIN_PASSWORD:
                session["logged_in"] = True
                session["username"] = username
                session["is_superadmin"] = True
                return redirect(url_for("index"))

            if username == ADMIN_PANEL_USER and password == ADMIN_PANEL_PASSWORD:
                session["logged_in"] = True
                session["username"] = username
                session["is_superadmin"] = False
                return redirect(url_for("index"))

            error = "Pogrešno ime ili lozinka."

        return render_template_string(LOGIN_TEMPLATE, error=error)

    @app.route("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        users = load_users()
        return render_template_string(
            TEMPLATE,
            users=users,
            username=session.get("username"),
            is_superadmin=session.get("is_superadmin", False),
        )

    @app.route("/approve", methods=["POST"])
    @login_required
    def approve_user():
        user_id = request.form.get("user_id")
        days = int(request.form.get("days") or 7)
        users = load_users()
        if user_id in users:
            until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
            u = users[user_id]
            u["approved"] = True
            u["waiting"] = False
            u["subscription_until"] = until
            users[user_id] = u
            save_users(users)
        return redirect(url_for("index"))

    @app.route("/extend", methods=["POST"])
    @login_required
    def extend_user():
        user_id = request.form.get("user_id")
        days = int(request.form.get("days") or 7)
        users = load_users()
        if user_id in users:
            cur = users[user_id].get("subscription_until")
            if not cur:
                base = datetime.now()
            else:
                base = datetime.strptime(cur, "%Y-%m-%d")
            new_exp = (base + timedelta(days=days)).strftime("%Y-%m-%d")
            users[user_id]["subscription_until"] = new_exp
            save_users(users)
        return redirect(url_for("index"))

    @app.route("/premium", methods=["POST"])
    @login_required
    def toggle_premium():
        if not session.get("is_superadmin"):
            return "Samo SUPERADMIN može mijenjati premium.", 403

        user_id = request.form.get("user_id")
        flag = request.form.get("flag", "off")
        users = load_users()
        if user_id in users:
            users[user_id]["premium"] = True if flag == "on" else False
            save_users(users)
        return redirect(url_for("index"))

    @app.route("/history")
    @login_required
    def user_history():
        if not session.get("is_superadmin"):
            return "Samo SUPERADMIN može vidjeti arhivu razgovora.", 403

        user_id = request.args.get("user_id")
        conv = load_conversations()
        msgs = conv.get(user_id, [])
        html = "<h1>Povijest razgovora</h1><pre>"
        for m in msgs[-400:]:
            html += f"[{m.get('timestamp')}] {m.get('role')}: {m.get('text')}\n"
        html += "</pre><a href='/'>← natrag</a>"
        return html

    # SIGURNI START FLASKA U THREADU – BEZ RELOADERA I BEZ DEBUGA
    try:
        print("🌐 Pokrećem HTTPS admin panel (adhoc certifikat)...")
        app.run(
            host="0.0.0.0",
            port=5000,
            debug=False,
            use_reloader=False,
            ssl_context="adhoc",
        )
    except Exception as e:
        print(f"⚠️ HTTPS nije moguće pokrenuti ({e}). Prebacujem se na HTTP...")
        app.run(
            host="0.0.0.0",
            port=5000,
            debug=False,
            use_reloader=False,
        )


# =====================================================
# 13. MAIN – POKRETANJE BOTA
# =====================================================

def main_bot():
    print("🤖 Psiholog Bot FULL pokrenut!")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # korisničke komande
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("profile", profile_cmd))
    application.add_handler(CommandHandler("menu", menu_cmd))
    application.add_handler(CommandHandler("mood", mood_cmd))
    application.add_handler(CommandHandler("history", history_cmd))
    application.add_handler(CommandHandler("weekly", weekly_cmd))
    application.add_handler(CommandHandler("tests", tests_cmd))

    # admin komande
    application.add_handler(CommandHandler("approve", approve_cmd))
    application.add_handler(CommandHandler("pending", pending_cmd))
    application.add_handler(CommandHandler("extend", extend_cmd))
    application.add_handler(CommandHandler("setpremium", setpremium_cmd))

    # inline gumbi
    application.add_handler(CallbackQueryHandler(handle_button))

    # tekst poruke
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()


if __name__ == "__main__":
    import threading

    # Modovi:
    #   python psiholog_bot.py          -> bot + admin panel
    #   python psiholog_bot.py bot      -> samo bot
    #   python psiholog_bot.py admin    -> samo admin panel

    if len(sys.argv) > 1:
        mode = sys.argv[1].lower()
        if mode == "admin":
            print("▶ Pokrećem SAMO admin web sučelje...")
            run_admin_panel()
        elif mode == "bot":
            print("▶ Pokrećem SAMO Telegram bota...")
            main_bot()
        else:
            print("⚠ Nepoznat argument. Koristi bez argumenata, ili 'bot' ili 'admin'.")
    else:
        print("▶ Pokrećem admin web sučelje u pozadini i Telegram bota u prvom planu...")

        admin_thread = threading.Thread(target=run_admin_panel, daemon=True)
        admin_thread.start()

        main_bot()
