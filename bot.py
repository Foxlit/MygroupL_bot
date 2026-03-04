import os
import json
import re
import time
import logging
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('homework_bot')
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ==========
load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS")

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не найден в .env файле!")
    exit(1)

if not GOOGLE_CREDS_JSON:
    logger.error("❌ GOOGLE_CREDENTIALS не найден в .env файле!")
    exit(1)

# ========== КОНСТАНТЫ ==========
ITEMS_PER_PAGE = 5
REQUEST_COOLDOWN = 5
CACHE_TTL = 60  # Кэш на 1 минуту, чтобы не грузить часто

# Глобальный кэш для данных (чтобы не грузить с таблицы каждому пользователю)
_data_cache = {
    'data': None,
    'timestamp': 0
}

user_last_request = {}
user_state = {}  # Словарь для отслеживания состояния пользователя


# ========== ДЕКОРАТОРЫ ==========
def timer_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        duration = time.time() - start
        logger.info(f"⏱️ {func.__name__} выполнилась за {duration:.2f} секунд")
        return result

    return wrapper


def async_timer_decorator(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.time()
        result = await func(*args, **kwargs)
        duration = time.time() - start
        logger.info(f"⏱️ Асинхронная {func.__name__} выполнилась за {duration:.2f} секунд")
        return result

    return wrapper


# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С GOOGLE SHEETS ==========

def parse_hyperlink_formula(formula):
    """Извлекает URL и текст из формулы ГИПЕРССЫЛКА"""
    if not formula or not isinstance(formula, str):
        return None, None

    pattern = r'=HYPERLINK\("([^"]+)";\s*"([^"]*)"\)'
    match = re.search(pattern, formula)

    if match:
        return match.group(1), match.group(2)
    return None, None


@timer_decorator
def get_homework_fast(force_refresh=False):
    """МАКСИМАЛЬНО ОПТИМИЗИРОВАННАЯ функция загрузки"""
    global _data_cache

    # Проверяем кэш
    if not force_refresh and _data_cache['data'] and (time.time() - _data_cache['timestamp']) < CACHE_TTL:
        logger.info("📦 Использую кэшированные данные")
        return _data_cache['data']

    logger.info("⚡ Загрузка из Google Sheets...")

    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)

        # Открываем таблицу
        spreadsheet = client.open_by_key("1vIxyMIs08MWVOtmuz9dh4O2CVwRINXmxaQnJBObgOFY")
        sheet = spreadsheet.worksheet("1 вариант")

        # ВСЕГО 1 ЗАПРОС! Получаем всё одним махом
        all_values = sheet.get_all_values()

        if len(all_values) < 2:
            _data_cache['data'] = []
            _data_cache['timestamp'] = time.time()
            return []

        headers = all_values[0]

        # Обрабатываем данные в памяти
        records = []

        for row in all_values[1:]:
            record = {}
            for i, header in enumerate(headers):
                if i >= len(row):
                    value = ''
                else:
                    value = row[i]

                # Проверяем, не является ли значение формулой гиперссылки
                if header == "Задание" and value and 'HYPERLINK' in value:
                    url, text = parse_hyperlink_formula(value)
                    if url:
                        record[header] = {
                            'text': text or value,
                            'url': url,
                            'is_hyperlink': True
                        }
                    else:
                        record[header] = {'text': value, 'is_hyperlink': False}
                else:
                    record[header] = value

            records.append(record)

        # Сохраняем в кэш
        _data_cache['data'] = records
        _data_cache['timestamp'] = time.time()

        hyperlink_count = sum(1 for r in records
                              if isinstance(r.get('Задание'), dict)
                              and r['Задание'].get('is_hyperlink'))

        logger.info(f"✅ Загружено: {len(records)} записей, {hyperlink_count} гиперссылок")
        return records

    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
        return []


# ========== ФУНКЦИИ ФОРМАТИРОВАНИЯ ==========

def format_homework_page(records, page=0):
    """Форматирует одну страницу с заданиями"""
    if not records:
        return "📭 На сегодня заданий нет. Можно отдыхать!", []

    # Сортируем по сроку
    def get_date(item):
        date_str = item.get('Срок', '')
        if date_str:
            try:
                return datetime.strptime(date_str, "%d.%m.%Y")
            except:
                pass
        return datetime.max

    sorted_records = sorted(records, key=get_date)

    total_pages = max(1, (len(sorted_records) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, len(sorted_records))
    current_page_records = sorted_records[start_idx:end_idx]

    message = f"📚 <b>Домашнее задание</b> (страница {page + 1}/{total_pages})\n\n"

    for idx, item in enumerate(current_page_records, start=start_idx + 1):
        subject = item.get('Предмет', 'Без предмета')
        due_date = item.get('Срок', '')
        task_data = item.get('Задание', 'Нет описания')

        status_emoji = "⏳"
        if due_date:
            try:
                due = datetime.strptime(due_date, "%d.%m.%Y")
                days_left = (due.date() - datetime.now().date()).days
                if days_left < 0:
                    status_emoji = f"❗️ ПРОСРОЧЕНО ({-days_left} дн.)"
                elif days_left == 0:
                    status_emoji = "🔥 СЕГОДНЯ!"
                elif days_left == 1:
                    status_emoji = "⚠️ ЗАВТРА!"
                elif days_left <= 3:
                    status_emoji = f"⏰ {days_left} дн."
            except:
                pass

        if isinstance(task_data, dict):
            if task_data.get('is_hyperlink') and task_data.get('url'):
                url = task_data['url']
                text = task_data.get('text', 'Ссылка')
                task_display = f'<a href="{url}">{text}</a>'
            else:
                task_display = task_data.get('text', '')
                task_display = task_display.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        else:
            task_display = str(task_data)
            if task_display.startswith(('http://', 'https://')):
                task_display = f'<a href="{task_display}">🔗 ссылка</a>'
            else:
                task_display = task_display.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        message += f"{idx}. <b>{subject}</b>\n"
        message += f"   📌 {task_display}\n"
        message += f"   📅 Срок: {due_date} {status_emoji}\n\n"

    # Кнопки навигации
    keyboard = []

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    # Кнопки действий
    filter_buttons = [
        InlineKeyboardButton("🔄 Обновить", callback_data="refresh_data"),
        InlineKeyboardButton("📅 Сегодня", callback_data="filter_today"),
        InlineKeyboardButton("⚠️ Просрочка", callback_data="filter_overdue")
    ]
    keyboard.append(filter_buttons)

    # Кнопка помощи и главное меню
    keyboard.append([
        InlineKeyboardButton("❓ Помощь", callback_data="help_tasks"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
    ])

    return message, keyboard


# ========== ОБРАБОТЧИКИ КОМАНД ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start - главное меню"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} (ID: {user_id}) вызвал команду /start")

    # Записываем состояние
    user_state[user_id] = 'main_menu'
    logger.info(f"📍 Состояние пользователя {username}: ГЛАВНОЕ МЕНЮ")

    # Главное меню
    keyboard = [
        [InlineKeyboardButton("📚 Показать задания", callback_data="show_hw")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я бот для отслеживания домашних заданий.\n"
        "Выбери действие:",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    await start(update, context)


@async_timer_decorator
async def homework_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /hw - быстрая загрузка заданий"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} (ID: {user_id}) вызвал команду /hw")

    # Проверка на спам
    current_time = time.time()
    if user_id in user_last_request:
        time_diff = current_time - user_last_request[user_id]
        if time_diff < REQUEST_COOLDOWN:
            await update.message.reply_text(
                f"⏳ Подожди {REQUEST_COOLDOWN - time_diff:.0f} секунд"
            )
            return

    user_last_request[user_id] = current_time

    await update.message.chat.send_action(action="typing")

    try:
        # Загружаем данные (force_refresh=True чтобы получить свежие)
        homework_data = get_homework_fast(force_refresh=True)

        if homework_data:
            # Записываем состояние
            user_state[user_id] = 'tasks_list'
            logger.info(f"📍 Состояние пользователя {username}: СПИСОК ЗАДАНИЙ")

            message, keyboard = format_homework_page(homework_data, 0)
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

            # Сохраняем данные
            context.user_data['homework_data'] = homework_data
            context.user_data['current_page'] = 0
        else:
            await update.message.reply_text("📭 В таблице нет заданий!")

    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка: {str(e)[:100]}")


# ========== ОБРАБОТЧИК КНОПОК ==========

@async_timer_decorator
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на инлайн-кнопки"""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} нажал кнопку: {query.data}")
    logger.info(f"📍 Текущее состояние: {user_state.get(user_id, 'НЕ ИЗВЕСТНО')}")

    await query.answer()

    data = query.data

    try:
        # ===== КНОПКИ ГЛАВНОГО МЕНЮ =====
        if data == "help_main":
            logger.info(f"ℹ️ Пользователь {username} открыл помощь (главное меню)")

            help_text = (
                "📚 <b>Помощь по боту</b>\n\n"
                "<b>Что я умею:</b>\n"
                "• Показывать домашние задания из таблицы\n"
                "• Отображать гиперссылки в заданиях\n"
                "• Фильтровать по срокам\n\n"
                "<b>Команды:</b>\n"
                "/hw - сразу показать задания\n"
                "/start - главное меню\n\n"
                "<b>Навигация:</b>\n"
                "🏠 Главное меню - вернуться сюда"
            )

            keyboard = [[InlineKeyboardButton("🏠 В главное меню", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                help_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data == "main_menu":
            # Возврат в главное меню
            logger.info(f"🏠 Пользователь {username} вернулся в главное меню")
            user_state[user_id] = 'main_menu'

            keyboard = [
                [InlineKeyboardButton("📚 Показать задания", callback_data="show_hw")],
                [InlineKeyboardButton("❓ Помощь", callback_data="help_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "👋 Главное меню:",
                reply_markup=reply_markup
            )

        # ===== КНОПКИ СПИСКА ЗАДАНИЙ =====
        elif data == "show_hw":
            logger.info(f"📚 Пользователь {username} запросил задания")

            await query.edit_message_text("⚡ Загружаю данные...")

            # Загружаем свежие данные
            homework_data = get_homework_fast(force_refresh=True)

            if homework_data:
                user_state[user_id] = 'tasks_list'
                logger.info(f"📍 Новое состояние: СПИСОК ЗАДАНИЙ")

                message, keyboard = format_homework_page(homework_data, 0)
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )

                context.user_data['homework_data'] = homework_data
                context.user_data['current_page'] = 0
            else:
                await query.edit_message_text("📭 В таблице нет заданий!")

        elif data == "help_tasks":
            logger.info(f"ℹ️ Пользователь {username} открыл помощь (список заданий)")

            help_text = (
                "📚 <b>Работа с заданиями</b>\n\n"
                "<b>Кнопки:</b>\n"
                "◀️ Назад / Вперед ▶️ - листать страницы\n"
                "🔄 Обновить - загрузить свежие данные\n"
                "📅 Сегодня - показать задания на сегодня\n"
                "⚠️ Просрочка - показать просроченные\n"
                "🏠 Главное меню - вернуться в начало\n\n"
                "<b>Статусы:</b>\n"
                "🔥 СЕГОДНЯ! - сдать сегодня\n"
                "⚠️ ЗАВТРА! - сдать завтра\n"
                "⏰ N дн. - осталось N дней\n"
                "❗️ ПРОСРОЧЕНО - задание просрочено"
            )

            keyboard = [[InlineKeyboardButton("◀️ Назад к заданиям", callback_data="back_to_tasks")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                help_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data == "back_to_tasks":
            # Возврат к списку заданий
            logger.info(f"◀️ Пользователь {username} вернулся к заданиям")

            homework_data = context.user_data.get('homework_data', [])
            current_page = context.user_data.get('current_page', 0)

            if homework_data:
                user_state[user_id] = 'tasks_list'
                message, keyboard = format_homework_page(homework_data, current_page)
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            else:
                # Если данных нет, предлагаем загрузить
                await query.edit_message_text(
                    "📭 Данные не загружены. Нажмите кнопку ниже:",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚 Загрузить задания", callback_data="show_hw")
                    ]])
                )

        elif data == "refresh_data":
            logger.info(f"🔄 Пользователь {username} обновляет данные")

            await query.edit_message_text("🔄 Обновляю данные из таблицы...")

            # Принудительно загружаем свежие данные
            new_data = get_homework_fast(force_refresh=True)

            if new_data:
                message, keyboard = format_homework_page(new_data, 0)
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )

                context.user_data['homework_data'] = new_data
                context.user_data['current_page'] = 0

                logger.info(f"✅ Данные обновлены для {username}")
            else:
                await query.edit_message_text(
                    "📭 В таблице нет заданий!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 Попробовать снова", callback_data="refresh_data")
                    ]])
                )

        elif data.startswith("page_"):
            page = int(data.split("_")[1])
            logger.info(f"📄 Пользователь {username} перешел на страницу {page}")

            homework_data = context.user_data.get('homework_data', [])

            if not homework_data:
                await query.edit_message_text(
                    "📭 Сначала загрузите данные:",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚 Загрузить задания", callback_data="show_hw")
                    ]])
                )
                return

            context.user_data['current_page'] = page
            user_state[user_id] = 'tasks_list'

            message, keyboard = format_homework_page(homework_data, page)
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data == "filter_today":
            logger.info(f"📅 Пользователь {username} фильтрует: сегодня")

            homework_data = context.user_data.get('homework_data', [])

            if not homework_data:
                await query.edit_message_text(
                    "📭 Сначала загрузите данные:",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚 Загрузить задания", callback_data="show_hw")
                    ]])
                )
                return

            today = datetime.now().date()
            filtered = []
            for item in homework_data:
                date_str = item.get('Срок', '')
                if date_str:
                    try:
                        due = datetime.strptime(date_str, "%d.%m.%Y").date()
                        if due == today:
                            filtered.append(item)
                    except:
                        pass

            if filtered:
                message, keyboard = format_homework_page(filtered, 0)
                current_page = context.user_data.get('current_page', 0)
                keyboard.append([InlineKeyboardButton("◀️ Все задания", callback_data=f"page_{current_page}")])
            else:
                message = "📭 На сегодня заданий нет!"
                current_page = context.user_data.get('current_page', 0)
                keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data=f"page_{current_page}")]]

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data == "filter_overdue":
            logger.info(f"⚠️ Пользователь {username} фильтрует: просрочка")

            homework_data = context.user_data.get('homework_data', [])

            if not homework_data:
                await query.edit_message_text(
                    "📭 Сначала загрузите данные:",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚 Загрузить задания", callback_data="show_hw")
                    ]])
                )
                return

            today = datetime.now().date()
            filtered = []
            for item in homework_data:
                date_str = item.get('Срок', '')
                if date_str:
                    try:
                        due = datetime.strptime(date_str, "%d.%m.%Y").date()
                        if due < today:
                            filtered.append(item)
                    except:
                        pass

            if filtered:
                message, keyboard = format_homework_page(filtered, 0)
                current_page = context.user_data.get('current_page', 0)
                keyboard.append([InlineKeyboardButton("◀️ Все задания", callback_data=f"page_{current_page}")])
            else:
                message = "✅ Просроченных заданий нет!"
                current_page = context.user_data.get('current_page', 0)
                keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data=f"page_{current_page}")]]

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

    except Exception as e:
        logger.error(f"❌ Ошибка в кнопке {data}: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                "❌ Произошла ошибка. Напишите /start",
                disable_web_page_preview=True
            )
        except:
            pass


# ========== ЗАПУСК БОТА ==========

def main():
    """Главная функция запуска бота"""
    logger.info("=" * 60)
    logger.info("🚀 ЗАПУСК БОТА С ОТСЛЕЖИВАНИЕМ СОСТОЯНИЙ")
    logger.info("=" * 60)

    logger.info(f"📊 Настройки:")
    logger.info(f"  • Токен: {TELEGRAM_TOKEN[:10]}... (скрыт)")
    logger.info(f"  • Google Sheets: подключен")
    logger.info(f"  • Элементов на странице: {ITEMS_PER_PAGE}")
    logger.info(f"  • Время жизни кэша: {CACHE_TTL} сек")
    logger.info(f"  • Защита от спама: {REQUEST_COOLDOWN} сек")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("hw", homework_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("✅ Обработчики команд зарегистрированы")
    logger.info("📍 Будет вестись лог состояний пользователей")
    logger.info("🤖 Бот готов к работе. Ожидаю команды...")
    logger.info("=" * 60)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
