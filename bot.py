import os
import json
import re
import time
import logging
import threading
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
SHEET_KEY = os.environ.get("SHEET_KEY")
SHEET_WORKSHEET = os.environ.get("SHEET_WORKSHEET", "1 вариант")

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не найден в .env файле!")
    exit(1)

if not GOOGLE_CREDS_JSON:
    logger.error("❌ GOOGLE_CREDENTIALS не найден в .env файле!")
    exit(1)

if not SHEET_KEY:
    logger.error("❌ SHEET_KEY не найден в .env файле!")
    exit(1)

# ========== КОНСТАНТЫ ==========
ITEMS_PER_PAGE = 5
REQUEST_COOLDOWN = 5
CACHE_TTL = 60  # Кэш на 1 минуту

# Глобальный кэш
_data_cache = {
    'data': None,
    'timestamp': 0
}

user_last_request = {}
user_state = {}

# ========== УНИФИЦИРОВАННЫЕ СООБЩЕНИЯ ==========
MESSAGES = {
    'start': "Напишите /start для перезапуска",
    'hw': "Напишите /hw для загрузки заданий",
    'help': "Напишите /help для помощи",
    'error': "❌ Ошибка. Напишите /start для перезапуска",
    'no_data': "📭 Данные не загружены. Напишите /hw",
    'cooldown': "⏳ Подождите {} секунд перед следующим запросом"
}

# ========== СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ ==========
USER_STATES = {
    'main_menu': '🏠 ГЛАВНОЕ МЕНЮ',
    'tasks_list': '📚 СПИСОК ЗАДАНИЙ',
    'help_main': '❓ ПОМОЩЬ (главная)',
    'help_tasks': '❓ ПОМОЩЬ (задания)',
    'filter_today': '📅 ФИЛЬТР: СЕГОДНЯ',
    'filter_overdue': '⚠️ ФИЛЬТР: ПРОСРОЧКА',
}


def set_user_state(user_id, state, page=None):
    """Устанавливает состояние пользователя с красивым логированием"""
    if page is not None and state == 'page':
        state_key = f'page_{page}'
        user_state[user_id] = state_key
        logger.info(f"📍 📄 СТРАНИЦА {page}")
    else:
        user_state[user_id] = state
        logger.info(f"📍 {USER_STATES.get(state, 'НЕИЗВЕСТНОЕ СОСТОЯНИЕ')}")


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
    """Извлекает URL и текст из формулы ГИПЕРССЫЛКА (поддержка EN и RU)"""
    if not formula or not isinstance(formula, str):
        return None, None

    # Отладочный вывод для понимания, что приходит
    if formula and ('HYPERLINK' in formula or 'ГИПЕРССЫЛКА' in formula):
        logger.debug(f"🔍 Парсинг формулы: {formula[:100]}...")

    # Паттерны для разных вариантов
    patterns = [
        # Русский вариант с точкой с запятой
        r'=ГИПЕРССЫЛКА\("([^"]+)";\s*"([^"]*)"\)',
        # Английский вариант с точкой с запятой
        r'=HYPERLINK\("([^"]+)";\s*"([^"]*)"\)',
        # Английский вариант с запятой
        r'=HYPERLINK\("([^"]+)",\s*"([^"]*)"\)',
        # Без кавычек для текста (редкий случай)
        r'=HYPERLINK\("([^"]+)",\s*([^)]+)\)',
    ]

    for pattern in patterns:
        match = re.search(pattern, formula)
        if match:
            url = match.group(1)
            text = match.group(2)
            # Если текст без кавычек, убираем лишние пробелы
            if text and not text.startswith('"'):
                text = text.strip()
            logger.debug(f"✅ Найдена ссылка: {url[:30]}... -> {text[:30]}...")
            return url, text

    # Упрощенный парсинг на случай, если паттерны не сработали
    try:
        # Ищем URL в кавычках
        url_match = re.search(r'"([^"]+)"', formula)
        if url_match:
            url = url_match.group(1)
            # Ищем текст после разделителя
            text_match = re.search(r'[;,]\s*"([^"]+)"', formula)
            text = text_match.group(1) if text_match else "Ссылка"
            logger.debug(f"✅ Упрощенный парсинг: {url[:30]}...")
            return url, text
    except:
        pass

    logger.warning(f"⚠️ Не удалось распарсить формулу: {formula[:50]}...")
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
        spreadsheet = client.open_by_key(SHEET_KEY)
        sheet = spreadsheet.worksheet(SHEET_WORKSHEET)

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
                if header == "Задание" and value and ('HYPERLINK' in value or 'ГИПЕРССЫЛКА' in value):
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


# ========== ФОНОВОЕ ОБНОВЛЕНИЕ ==========
def background_cache_updater():
    """Фоновая задача для обновления кэша в отдельном потоке"""
    global _data_cache

    while True:
        try:
            time.sleep(60)  # Ждём минуту
            logger.info("🔄 Фоновое обновление кэша (поток)...")

            new_data = get_homework_fast(force_refresh=True)
            if new_data:
                _data_cache['data'] = new_data
                _data_cache['timestamp'] = time.time()
                logger.info(f"✅ Кэш обновлён: {len(new_data)} записей")
            else:
                logger.warning("⚠️ Не удалось обновить кэш")
        except Exception as e:
            logger.error(f"❌ Ошибка фонового обновления: {e}")


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

    # Кнопки действий (без просрочки)
    filter_buttons = [
        InlineKeyboardButton("🔄 Обновить", callback_data="refresh_data"),
        InlineKeyboardButton("📅 Сегодня", callback_data="filter_today")
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
    set_user_state(user_id, 'main_menu')

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
    """Обработчик команды /help - показывает подробную помощь"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} вызвал команду /help")

    help_text = (
        "📚 <b>Помощь по боту</b>\n\n"
        "<b>Основные команды:</b>\n"
        "• /hw - показать домашнее задание\n"
        "• /help - показать это сообщение\n"
        "• /start - главное меню\n"
        "• /overdue - показать просроченные задания\n\n"

        "<b>Как пользоваться:</b>\n"
        "1. Напишите /hw или нажмите кнопку '📚 Показать задания'\n"
        "2. Листайте страницы кнопками ◀️ Назад / Вперед ▶️\n"
        "3. Используйте фильтры для быстрого поиска:\n"
        "   • 📅 Сегодня - задания на сегодня\n"
        "   • 🔄 Обновить - загрузить свежие данные\n\n"

        "<b>Что означают статусы:</b>\n"
        "• 🔥 СЕГОДНЯ! - сдать сегодня\n"
        "• ⚠️ ЗАВТРА! - сдать завтра\n"
        "• ⏰ N дн. - осталось N дней\n"
        "• ❗️ ПРОСРОЧЕНО - задание просрочено\n\n"

        "<b>Дополнительно:</b>\n"
        "• Ссылки в заданиях кликабельны (синий текст)\n"
        "• Данные обновляются автоматически каждую минуту\n"
        "• Если что-то не работает - напишите /start для перезапуска"
    )

    keyboard = [[InlineKeyboardButton("🏠 В главное меню", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        help_text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


async def overdue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /overdue - показать просроченные задания"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} вызвал команду /overdue")

    homework_data = get_homework_fast(force_refresh=False)

    if not homework_data:
        await update.message.reply_text(f"📭 Нет данных о заданиях. {MESSAGES['hw']}")
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
        set_user_state(user_id, 'filter_overdue')
        message, keyboard = format_homework_page(filtered, 0)
        keyboard.append([InlineKeyboardButton("◀️ Все задания", callback_data="show_hw")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"⚠️ <b>Просроченные задания ({len(filtered)}):</b>\n\n{message}",
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    else:
        await update.message.reply_text("✅ Просроченных заданий нет!")


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
                MESSAGES['cooldown'].format(REQUEST_COOLDOWN - time_diff)
            )
            return

    user_last_request[user_id] = current_time

    await update.message.chat.send_action(action="typing")

    try:
        # Загружаем данные
        homework_data = get_homework_fast(force_refresh=True)

        if homework_data:
            set_user_state(user_id, 'tasks_list')

            message, keyboard = format_homework_page(homework_data, 0)
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

            context.user_data['homework_data'] = homework_data
            context.user_data['current_page'] = 0
            context.user_data['last_update'] = time.time()
        else:
            await update.message.reply_text("📭 В таблице нет заданий!")

    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Ошибка. {MESSAGES['start']}")


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
            set_user_state(user_id, 'help_main')

            help_text = (
                "📚 <b>Помощь по боту</b>\n\n"
                "<b>Что я умею:</b>\n"
                "• Показывать домашние задания из таблицы\n"
                "• Отображать гиперссылки в заданиях\n"
                "• Фильтровать по срокам\n\n"
                "<b>Команды:</b>\n"
                "/hw - сразу показать задания\n"
                "/help - подробная помощь\n"
                "/start - главное меню\n"
                "/overdue - просроченные задания\n\n"
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
            logger.info(f"🏠 Пользователь {username} вернулся в главное меню")
            set_user_state(user_id, 'main_menu')

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

            homework_data = get_homework_fast(force_refresh=True)

            if homework_data:
                set_user_state(user_id, 'tasks_list')

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
                context.user_data['last_update'] = time.time()
            else:
                await query.edit_message_text("📭 В таблице нет заданий!")

        elif data == "help_tasks":
            set_user_state(user_id, 'help_tasks')

            help_text = (
                "📚 <b>Работа с заданиями</b>\n\n"
                "<b>Кнопки:</b>\n"
                "◀️ Назад / Вперед ▶️ - листать страницы\n"
                "🔄 Обновить - загрузить свежие данные\n"
                "📅 Сегодня - показать задания на сегодня\n"
                "🏠 Главное меню - вернуться в начало\n\n"
                "<b>Статусы:</b>\n"
                "🔥 СЕГОДНЯ! - сдать сегодня\n"
                "⚠️ ЗАВТРА! - сдать завтра\n"
                "⏰ N дн. - осталось N дней\n"
                "❗️ ПРОСРОЧЕНО - задание просрочено\n\n"
                f"{MESSAGES['help']}"
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
            logger.info(f"◀️ Пользователь {username} вернулся к заданиям")

            # Проверяем, не устарели ли данные
            homework_data = context.user_data.get('homework_data', [])
            current_page = context.user_data.get('current_page', 0)
            last_update = context.user_data.get('last_update', 0)

            # Если данные старше 5 минут или их нет - загружаем свежие
            if not homework_data or (time.time() - last_update) > 300:  # 5 минут
                logger.info("📦 Данные устарели, загружаю свежие...")
                await query.edit_message_text("🔄 Обновляю данные...")
                homework_data = get_homework_fast(force_refresh=True)
                if homework_data:
                    context.user_data['homework_data'] = homework_data
                    context.user_data['last_update'] = time.time()
                    context.user_data['current_page'] = 0
                    current_page = 0

            if homework_data:
                set_user_state(user_id, 'tasks_list')
                message, keyboard = format_homework_page(homework_data, current_page)
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            else:
                await query.edit_message_text(
                    f"📭 Данные не загружены. {MESSAGES['hw']}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚 Загрузить задания", callback_data="show_hw")
                    ]])
                )

        elif data == "refresh_data":
            logger.info(f"🔄 Пользователь {username} обновляет данные")

            await query.edit_message_text("🔄 Обновляю данные из таблицы...")

            new_data = get_homework_fast(force_refresh=True)

            if new_data:
                set_user_state(user_id, 'tasks_list')

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
                context.user_data['last_update'] = time.time()

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
                    f"📭 Сначала загрузите данные. {MESSAGES['hw']}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚 Загрузить задания", callback_data="show_hw")
                    ]])
                )
                return

            context.user_data['current_page'] = page
            set_user_state(user_id, 'page', page=page)

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
                    f"📭 Сначала загрузите данные. {MESSAGES['hw']}",
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
                set_user_state(user_id, 'filter_today')
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

    except Exception as e:
        logger.error(f"❌ Ошибка в кнопке {data}: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ Произошла ошибка. {MESSAGES['start']}",
                disable_web_page_preview=True
            )
        except:
            pass


# ========== ЗАПУСК БОТА ==========

def main():
    """Главная функция запуска бота"""
    logger.info("=" * 60)
    logger.info("🚀 ЗАПУСК ФИНАЛЬНОЙ ВЕРСИИ БОТА")
    logger.info("=" * 60)

    logger.info(f"📊 Настройки:")
    logger.info(f"  • Токен: {TELEGRAM_TOKEN[:10]}... (скрыт)")
    logger.info(f"  • Google Sheets: подключен")
    logger.info(f"  • Ключ таблицы: {SHEET_KEY[:10]}... (скрыт)")
    logger.info(f"  • Лист: {SHEET_WORKSHEET}")
    logger.info(f"  • Элементов на странице: {ITEMS_PER_PAGE}")
    logger.info(f"  • Время жизни кэша: {CACHE_TTL} сек")
    logger.info(f"  • Защита от спама: {REQUEST_COOLDOWN} сек")

    # Запускаем фоновое обновление в отдельном потоке
    updater_thread = threading.Thread(target=background_cache_updater, daemon=True)
    updater_thread.start()
    logger.info("✅ Фоновое обновление запущено в отдельном потоке")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавляем обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("hw", homework_command))
    app.add_handler(CommandHandler("overdue", overdue_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("✅ Обработчики команд зарегистрированы")
    logger.info("📍 Будет вестись лог состояний пользователей")
    logger.info("🤖 Бот готов к работе. Ожидаю команды...")
    logger.info("=" * 60)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
