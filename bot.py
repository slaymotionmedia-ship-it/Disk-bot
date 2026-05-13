"""
Telegram-бот для виртуальной примерки дисков.

Сценарий:
1. Клиент: /start -> грузит фото машины -> оставляет телефон
2. Менеджеру в личку прилетает заявка с фото и контактом
3. Менеджер отвечает (reply) на сообщение-заявку фото диска с подписью /go
4. Бот генерит примерку через Gemini Nano Banana и отправляет клиенту
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from io import BytesIO

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image
from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- Конфиг ---
load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MANAGER_CHAT_ID = int(os.environ["MANAGER_CHAT_ID"])  # ID менеджера в Telegram
DB_PATH = os.environ.get("DB_PATH", "leads.db")

# Состояния диалога клиента
WAITING_CAR_PHOTO, WAITING_PHONE = range(2)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Gemini-клиент (Nano Banana = gemini-2.5-flash-image)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)


# --- База данных (SQLite, простая) ---
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_chat_id INTEGER NOT NULL,
            client_username TEXT,
            phone TEXT,
            car_photo_file_id TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'new'
        )
        """
    )
    conn.commit()
    conn.close()


def save_lead(chat_id: int, username: str | None, phone: str, file_id: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO leads (client_chat_id, client_username, phone, car_photo_file_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (chat_id, username, phone, file_id, datetime.utcnow().isoformat()),
    )
    lead_id = cur.lastrowid
    conn.commit()
    conn.close()
    return lead_id


def get_lead(lead_id: int) -> tuple | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, client_chat_id, client_username, phone, car_photo_file_id "
        "FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()
    conn.close()
    return row


def mark_done(lead_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE leads SET status = 'done' WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()


# --- Хэндлеры клиента ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👋 Привет! Это бот примерки дисков.\n\n"
        "Я помогу увидеть, как разные диски будут смотреться на вашей машине.\n\n"
        "📸 Пришлите, пожалуйста, фото вашего авто сбоку (чтобы было видно колёса целиком).",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WAITING_CAR_PHOTO


async def receive_car_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text(
            "Это должно быть фото 📸. Попробуйте ещё раз — отправьте именно картинку, "
            "а не файл-документ."
        )
        return WAITING_CAR_PHOTO

    # Берём самое крупное разрешение
    photo = update.message.photo[-1]
    context.user_data["car_photo_file_id"] = photo.file_id

    contact_button = KeyboardButton("📱 Отправить мой номер", request_contact=True)
    markup = ReplyKeyboardMarkup(
        [[contact_button]], resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "Отличное фото! ✅\n\n"
        "Оставьте, пожалуйста, номер телефона — менеджер свяжется и пришлёт "
        "примерки с подходящими дисками.",
        reply_markup=markup,
    )
    return WAITING_PHONE


async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text.strip()
        # Простая валидация: должна быть хоть какая-то цифра
        if not any(ch.isdigit() for ch in phone):
            await update.message.reply_text(
                "Не похоже на номер. Введите телефон или нажмите кнопку ниже."
            )
            return WAITING_PHONE

    chat = update.effective_chat
    user = update.effective_user
    file_id = context.user_data["car_photo_file_id"]

    lead_id = save_lead(chat.id, user.username, phone, file_id)

    await update.message.reply_text(
        f"Спасибо! ✅ Заявка №{lead_id} принята.\n\n"
        "Менеджер скоро пришлёт сюда фото вашей машины с примерками дисков. "
        "Обычно занимает 10–30 минут в рабочее время.",
        reply_markup=ReplyKeyboardRemove(),
    )

    # Уведомляем менеджера
    username_tag = f"@{user.username}" if user.username else "(без username)"
    caption = (
        f"🆕 Заявка №{lead_id}\n"
        f"👤 {user.full_name} {username_tag}\n"
        f"📱 {phone}\n\n"
        f"Чтобы отправить примерку — *ответьте (reply) на это сообщение* "
        f"фото диска с подписью /go"
    )
    await context.bot.send_photo(
        chat_id=MANAGER_CHAT_ID,
        photo=file_id,
        caption=caption,
        parse_mode="Markdown",
    )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Окей, отменил. Если что — /start.", reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


# --- Хэндлер менеджера: /go в ответ на заявку с фото диска ---
async def manager_generate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != MANAGER_CHAT_ID:
        return  # игнор от посторонних

    msg = update.message
    if not msg.reply_to_message:
        await msg.reply_text(
            "❗️ Отправьте /go *ответом (reply)* на сообщение с заявкой клиента, "
            "приложив фото диска.",
            parse_mode="Markdown",
        )
        return

    if not msg.photo:
        await msg.reply_text("❗️ К сообщению с /go нужно приложить фото диска.")
        return

    # Достаём lead_id из подписи к заявке
    original_caption = msg.reply_to_message.caption or ""
    lead_id = None
    for token in original_caption.split():
        if token.startswith("№"):
            try:
                lead_id = int(token.lstrip("№"))
                break
            except ValueError:
                pass

    if lead_id is None:
        await msg.reply_text("❗️ Не нашёл номер заявки в исходном сообщении.")
        return

    lead = get_lead(lead_id)
    if lead is None:
        await msg.reply_text(f"❗️ Заявка №{lead_id} не найдена в базе.")
        return

    _, client_chat_id, _, _, car_file_id = lead

    await context.bot.send_chat_action(
        chat_id=MANAGER_CHAT_ID, action=ChatAction.UPLOAD_PHOTO
    )
    status_msg = await msg.reply_text("⏳ Генерирую примерку…")

    try:
        # Скачиваем оба фото
        car_file = await context.bot.get_file(car_file_id)
        car_bytes = await car_file.download_as_bytearray()
        car_img = Image.open(BytesIO(car_bytes))

        disk_file = await context.bot.get_file(msg.photo[-1].file_id)
        disk_bytes = await disk_file.download_as_bytearray()
        disk_img = Image.open(BytesIO(disk_bytes))

        # Генерация через Nano Banana
        result_bytes = await asyncio.to_thread(
            generate_wheel_fitting, car_img, disk_img
        )

        if not result_bytes:
            await status_msg.edit_text(
                "❌ Не удалось сгенерировать изображение. Попробуйте другое фото диска."
            )
            return

        # Отправляем клиенту
        await context.bot.send_photo(
            chat_id=client_chat_id,
            photo=BytesIO(result_bytes),
            caption=(
                "Вот как ваша машина будет выглядеть с этими дисками 👇\n\n"
                "Если нравится — менеджер скоро свяжется и расскажет про наличие и цену."
            ),
        )
        # Копию менеджеру для контроля
        await context.bot.send_photo(
            chat_id=MANAGER_CHAT_ID,
            photo=BytesIO(result_bytes),
            caption=f"✅ Отправлено клиенту по заявке №{lead_id}",
        )
        mark_done(lead_id)
        await status_msg.delete()

    except Exception as e:
        logger.exception("Ошибка генерации")
        await status_msg.edit_text(f"❌ Ошибка: {e}")


def generate_wheel_fitting(car_img: Image.Image, disk_img: Image.Image) -> bytes | None:
    """Синхронный вызов Gemini 2.5 Flash Image (Nano Banana)."""
    prompt = (
        "You are an automotive photo editor. Take the first image (a car) and replace "
        "ONLY its wheels/rims with the rim design shown in the second image. "
        "Keep the car's body, color, paint, lighting, shadows, background, perspective, "
        "license plate and all other details EXACTLY the same. "
        "The new rims must match the car's wheel size, tire profile, and camera angle. "
        "Preserve realistic shadows under the wheels and reflections on the rim surface. "
        "The result must look like an unedited photograph, not a render. "
        "Output only the edited image."
    )

    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=[prompt, car_img, disk_img],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
        ),
    )

    for part in response.candidates[0].content.parts:
        if getattr(part, "inline_data", None) and part.inline_data.data:
            return part.inline_data.data
    return None


# --- Получить свой chat_id ---
async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Ваш chat_id: `{update.effective_chat.id}`\n"
        f"Скопируйте его в переменную MANAGER_CHAT_ID, если вы менеджер.",
        parse_mode="Markdown",
    )


# --- Запуск ---
def main() -> None:
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_CAR_PHOTO: [MessageHandler(filters.PHOTO | filters.ATTACHMENT, receive_car_photo)],
            WAITING_PHONE: [
                MessageHandler(filters.CONTACT, receive_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.CaptionRegex(r"^/go\b"), manager_generate
        )
    )

    logger.info("Бот запущен. Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
