import os
import json
import re
import time
import logging
import threading
from datetime import datetime, timedelta
from functools import wraps
from dotenv import load_dotenv

import gspread
from google.oauth2.service_account import Credentials
from google.auth.exceptions import GoogleAuthError
from google.api_core.exceptions import GoogleAPIError
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
logging.getLogger('googleapiclient').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

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
CACHE_TTL = 300  # Кэш на 5 минут
CHECK_INTERVAL = 60  # Проверка обновлений каждую минуту

# Глобальный кэш
_data_cache = {
    'data': None,
    'timestamp': 0,
    'version': 0
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
    'cooldown': "⏳ Подождите {} секунд перед следующим запросом",
    'new_data': "🔔 Обнаружены новые данные! Рекомендуется обновить список",
    'update_available': "🔄 Доступно обновление",
    'api_error': "⚠️ Временно недоступно. Используются сохранённые данные"
}

# ========== СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ ==========
USER_STATES = {
    'MAIN_MENU': '🏠 ГЛАВНОЕ МЕНЮ',
    'TASKS_LIST': '📚 СПИСОК ЗАДАНИЙ',
    'HELP_MAIN': '❓ ПОМОЩЬ (главная)',
    'HELP_TASKS': '❓ ПОМОЩЬ (задания)',
    'FILTER_TODAY': '📅 ФИЛЬТР: СЕГОДНЯ',
}


def set_user_state(user_id, state, page=None):
    """Устанавливает состояние пользователя с красивым логированием"""
    if page is not None and state == 'TASKS_LIST':
        user_state[user_id] = f'TASKS_LIST_PAGE_{page+1}'
        logger.info(f"📍 📄 СТРАНИЦА {page+1}")
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


def safe_api_call(default_return=None):
    """Декоратор для безопасных вызовов API"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"❌ API ошибка в {func.__name__}: {e}", exc_info=True)
                return default_return

        return wrapper

    return decorator


# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С GOOGLE SHEETS ==========

def parse_hyperlink_formula(formula):
    """Извлекает URL и текст из формулы ГИПЕРССЫЛКА (поддержка EN и RU)"""
    if not formula or not isinstance(formula, str):
        return None, None

    # Паттерны для разных вариантов
    patterns = [
        r'=ГИПЕРССЫЛКА\("([^"]+)";\s*"([^"]*)"\)',
        r'=HYPERLINK\("([^"]+)";\s*"([^"]*)"\)',
        r'=HYPERLINK\("([^"]+)",\s*"([^"]*)"\)',
        r'=HYPERLINK\("([^"]+)",\s*([^)]+)\)',
    ]

    for i, pattern in enumerate(patterns):
        match = re.search(pattern, formula)
        if match:
            url = match.group(1)
            text = match.group(2)
            if text and not text.startswith('"'):
                text = text.strip()
            logger.debug(f"✅ Паттерн {i + 1} сработал: URL={url[:30]}..., ТЕКСТ={text[:30]}...")
            return url, text

    # Упрощенный парсинг на случай, если паттерны не сработали
    try:
        url_match = re.search(r'"([^"]+)"', formula)
        if url_match:
            url = url_match.group(1)
            text_match = re.search(r'[;,]\s*"([^"]+)"', formula)
            text = text_match.group(1) if text_match else "Ссылка"
            logger.debug(f"✅ Упрощенный парсинг: URL={url[:30]}...")
            return url, text
    except:
        pass

    logger.warning(f"⚠️ Не удалось распарсить формулу: {formula[:100]}...")
    return None, None


@safe_api_call(default_return=[])
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

        # Получаем значения с формулами
        all_values = sheet.get_all_values(value_render_option='FORMULA')

        if len(all_values) < 2:
            _data_cache['data'] = []
            _data_cache['timestamp'] = time.time()
            _data_cache['version'] += 1
            return []

        headers = all_values[0]

        # Обрабатываем данные в памяти
        records = []

        for row_idx, row in enumerate(all_values[1:], start=2):
            record = {}
            for i, header in enumerate(headers):
                if i >= len(row):
                    value = ''
                else:
                    value = row[i]

                # Обработка разных типов колонок
                if header == "Задание" and value and isinstance(value, str):
                    if 'HYPERLINK' in value or 'ГИПЕРССЫЛКА' in value:
                        logger.debug(f"🔍 Строка {row_idx}: найдена формула: {value[:100]}...")
                        url, text = parse_hyperlink_formula(value)
                        if url:
                            logger.info(f"✅ Строка {row_idx}: успешно распознана ссылка: {url[:50]}...")
                            record[header] = {
                                'text': text or "Ссылка",
                                'url': url,
                                'is_hyperlink': True
                            }
                        else:
                            logger.warning(f"⚠️ Строка {row_idx}: не удалось распознать формулу: {value[:100]}...")
                            record[header] = {'text': value, 'is_hyperlink': False}
                    else:
                        record[header] = {'text': value, 'is_hyperlink': False}

                elif header == "Срок" and value:
                    # Конвертируем Excel-дату в нормальный формат
                    try:
                        if isinstance(value, (int, float)) or (
                                isinstance(value, str) and value.replace('.', '').replace('-', '').isdigit()):
                            excel_date = float(value)
                            if excel_date > 0:
                                base_date = datetime(1899, 12, 30)
                                converted_date = base_date + timedelta(days=excel_date)
                                date_str = converted_date.strftime("%d.%m.%Y")
                                logger.debug(f"📅 Строка {row_idx}: преобразовано {value} -> {date_str}")
                                record[header] = date_str
                            else:
                                record[header] = str(value)
                        else:
                            record[header] = str(value)
                    except Exception as e:
                        logger.warning(f"⚠️ Ошибка преобразования даты '{value}': {e}")
                        record[header] = str(value)
                else:
                    record[header] = value

            records.append(record)

        # Проверяем, изменились ли данные
        old_data = _data_cache['data']
        new_version = _data_cache['version'] + 1 if records != old_data else _data_cache['version']

        # Сохраняем в кэш
        _data_cache['data'] = records
        _data_cache['timestamp'] = time.time()
        _data_cache['version'] = new_version

        hyperlink_count = sum(1 for r in records
                              if isinstance(r.get('Задание'), dict)
                              and r['Задание'].get('is_hyperlink'))

        logger.info(f"✅ Загружено: {len(records)} записей, {hyperlink_count} гиперссылок")

        return records

    except (GoogleAuthError, GoogleAPIError, gspread.exceptions.APIError) as e:
        logger.error(f"❌ Ошибка Google API: {e}")
        if _data_cache['data']:
            logger.info("📦 Возвращаю кэшированные данные из-за ошибки API")
            return _data_cache['data']
        raise
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка: {e}", exc_info=True)
        if _data_cache['data']:
            logger.info("📦 Возвращаю кэшированные данные из-за ошибки")
            return _data_cache['data']
        raise


# ========== ФОНОВОЕ ОБНОВЛЕНИЕ ==========
def background_cache_updater():
    """Фоновая задача для проверки обновлений"""
    global _data_cache

    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            logger.info("🔄 Проверка обновлений...")

            old_version = _data_cache['version']

            try:
                new_data = get_homework_fast(force_refresh=True)
                if new_data:
                    new_version = _data_cache['version']
                    if new_version > old_version:
                        logger.info(f"🆕 Обнаружены изменения! Версия {old_version} -> {new_version}")
                    else:
                        logger.info("✅ Изменений не обнаружено")
            except Exception as e:
                logger.error(f"❌ Ошибка при проверке обновлений: {e}")

        except Exception as e:
            logger.error(f"❌ Критическая ошибка в фоновом обновлении: {e}")


# ========== ФУНКЦИИ ФОРМАТИРОВАНИЯ ==========

def check_for_updates(context, user_id):
    """Проверяет, есть ли обновления для пользователя"""
    user_version = context.user_data.get('data_version', 0)
    current_version = _data_cache.get('version', 0)

    return current_version > user_version


def format_homework_page(records, page=0, show_update_notice=False, current_filter=None):
    """Форматирует одну страницу с заданиями

    Args:
        records: список заданий
        page: текущая страница
        show_update_notice: показывать ли уведомление об обновлении
        current_filter: текущий активный фильтр (None, 'today')
    """
    if not records:
        return "📭 В таблице нет данных, можете отдохнуть или связаться с администратором", []

    # Сортируем по сроку
    def get_date(item):
        date_str = item.get('Срок')
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

    # Добавляем уведомление об обновлении, если нужно
    message = ""
    if show_update_notice:
        message += f"{MESSAGES['new_data']}\n\n"

    # Добавляем индикатор активного фильтра
    filter_indicator = ""
    if current_filter == 'today':
        filter_indicator = " [Фильтр: сегодня]"

    message += f"📚 <b>Домашнее задание{filter_indicator}</b> (страница {page + 1}/{total_pages})\n\n"

    for idx, item in enumerate(current_page_records, start=start_idx + 1):
        subject = item.get('Предмет')
        due_date = item.get('Срок')
        task_data = item.get('Задание')

        status_emoji = ""
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

    # Кнопки действий - УМНЫЕ КНОПКИ
    filter_buttons = []

    # Кнопка обновления (всегда доступна)
    refresh_emoji = "🔄" + ("❗" if show_update_notice else "")
    filter_buttons.append(InlineKeyboardButton(f"{refresh_emoji} Обновить", callback_data="refresh_data"))

    # Кнопка "Сегодня" - показываем только если не в этом фильтре
    if current_filter != 'today':
        filter_buttons.append(InlineKeyboardButton("📅 Сегодня", callback_data="filter_today"))

    # Кнопка "Назад к списку" - если мы в фильтре
    if current_filter:
        filter_buttons.append(InlineKeyboardButton("◀️ К списку", callback_data="back_to_tasks"))

    if filter_buttons:
        # Разбиваем на ряды по 2 кнопки
        for i in range(0, len(filter_buttons), 2):
            keyboard.append(filter_buttons[i:i + 2])

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
    set_user_state(user_id, 'MAIN_MENU')

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
        "• /start - перезапуск бота\n\n"

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
        "• ❗️ ПРОСРОЧЕНО - задание просрочено (для отладки)\n\n"

        "<b>Дополнительно:</b>\n"
        "• Ссылки в заданиях кликабельны (синий текст)\n"
        "• Данные проверяются автоматически каждую минуту, но обновляете их отображение Вы вручную\n"
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
        homework_data = get_homework_fast(force_refresh=True)

        if homework_data:
            set_user_state(user_id, 'TASKS_LIST')

            message, keyboard = format_homework_page(homework_data, 0, current_filter=None)
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
            context.user_data['data_version'] = _data_cache.get('version', 0)
        else:
            await update.message.reply_text("📭 В таблице нет заданий!")

    except Exception as e:
        logger.error(f"❌ Ошибка в /hw: {e}")
        await update.message.reply_text(f"⚠️ Временно недоступно. {MESSAGES['start']}")


# ========== ОБРАБОТЧИК КНОПОК ==========

@async_timer_decorator
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на инлайн-кнопки"""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} нажал кнопку: {query.data}")
    logger.info(f"📍 Текущее состояние: {user_state.get(user_id, 'НЕИЗВЕСТНО')}")

    await query.answer()

    data = query.data

    try:
        # ===== КНОПКИ ГЛАВНОГО МЕНЮ =====
        if data == "help_main":
            set_user_state(user_id, 'HELP_MAIN')

            help_text = (
                "📚 <b>Помощь по боту</b>\n\n"
                "<b>Что я умею:</b>\n"
                "• Показывать домашние задания из таблицы\n"
                "• Отображать гиперссылки в заданиях\n"
                "• Фильтровать задания по срокам\n\n"
                "<b>Команды:</b>\n"
                "/hw - сразу показать задания\n"
                "/help - подробная помощь\n"
                "/start - перезапуск бота\n\n"
                "<b>Навигация:</b>\n"
                "🏠 Главное меню - вернуться на главную"
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
            set_user_state(user_id, 'MAIN_MENU')

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

            try:
                homework_data = get_homework_fast(force_refresh=True)

                if homework_data:
                    set_user_state(user_id, 'TASKS_LIST')

                    message, keyboard = format_homework_page(homework_data, 0, current_filter=None)
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
                    context.user_data['data_version'] = _data_cache.get('version', 0)
                else:
                    await query.edit_message_text("📭 В таблице нет заданий!")
            except Exception as e:
                logger.error(f"❌ Ошибка при загрузке: {e}")
                await query.edit_message_text(
                    f"⚠️ Временно недоступно. {MESSAGES['start']}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🏠 В главное меню", callback_data="main_menu")
                    ]])
                )

        elif data == "help_tasks":
            set_user_state(user_id, 'HELP_TASKS')

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
                "❗️ ПРОСРОЧЕНО - задание просрочено (для отладки)\n\n"
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

            has_updates = check_for_updates(context, user_id)
            homework_data = context.user_data.get('homework_data', [])
            current_page = context.user_data.get('current_page', 0)

            if homework_data:
                set_user_state(user_id, 'TASKS_LIST', page=current_page)
                message, keyboard = format_homework_page(
                    homework_data,
                    current_page,
                    show_update_notice=has_updates,
                    current_filter=None
                )
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

            try:
                new_data = get_homework_fast(force_refresh=True)

                if new_data:
                    set_user_state(user_id, 'TASKS_LIST')

                    message, keyboard = format_homework_page(new_data, 0, current_filter=None)
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
                    context.user_data['data_version'] = _data_cache.get('version', 0)

                    logger.info(f"✅ Данные обновлены для {username}")
                else:
                    await query.edit_message_text(
                        "📭 В таблице нет заданий!",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 Попробовать снова", callback_data="refresh_data")
                        ]])
                    )
            except Exception as e:
                logger.error(f"❌ Ошибка при обновлении: {e}")
                await query.edit_message_text(
                    f"⚠️ Временно недоступно. Используются сохранённые данные.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Назад к заданиям", callback_data="back_to_tasks")
                    ]])
                )

        elif data.startswith("page_"):
            page = int(data.split("_")[1])
            logger.info(f"📄 Пользователь {username} перешел на страницу {page+1}")

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
            set_user_state(user_id, 'TASKS_LIST', page=page)

            has_updates = check_for_updates(context, user_id)

            message, keyboard = format_homework_page(
                homework_data,
                page,
                show_update_notice=has_updates,
                current_filter=None
            )
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data == "filter_today":
            logger.info(f"📅 Пользователь {username} фильтрует: сегодня")

            # Проверяем, не в фильтре ли мы уже
            if user_state.get(user_id) == 'FILTER_TODAY':
                logger.info("⚠️ Уже в фильтре сегодня, пропускаем")
                return

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
                date_str = item.get('Срок')
                if date_str:
                    try:
                        due = datetime.strptime(date_str, "%d.%m.%Y").date()
                        if due == today:
                            filtered.append(item)
                    except:
                        pass

            if filtered:
                set_user_state(user_id, 'FILTER_TODAY')
                message, keyboard = format_homework_page(
                    filtered,
                    0,
                    current_filter='today'
                )
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    message,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                    disable_web_page_preview=True
                )
            else:
                message = "📭 На сегодня заданий нет!"
                keyboard = [[InlineKeyboardButton("◀️ К списку", callback_data="back_to_tasks")]]
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
    logger.info(f"  • Интервал проверки: {CHECK_INTERVAL} сек")
    logger.info(f"  • Защита от спама: {REQUEST_COOLDOWN} сек")

    # Запускаем фоновую проверку обновлений
    updater_thread = threading.Thread(target=background_cache_updater, daemon=True)
    updater_thread.start()
    logger.info("✅ Фоновая проверка обновлений запущена")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавляем обработчики команд
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
