import os
import json
import re
import time
import logging
import threading
import asyncio
from datetime import datetime, timedelta
from functools import wraps
from dotenv import load_dotenv
import pytz

import gspread
from google.oauth2.service_account import Credentials
from google.auth.exceptions import GoogleAuthError, RefreshError
from googleapiclient.errors import HttpError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Импорт базы данных
from database import db, get_pending_links, mark_link_notified, get_today_links, get_subscribed_users

# ========== НАСТРОЙКА ЧАСОВОГО ПОЯСА ==========
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
UTC_TZ = pytz.UTC

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

# Глобальный кэш с блокировкой для потокобезопасности
_data_cache = {
    'data': None,
    'timestamp': 0,
    'version': 0,
    'last_successful_data': None
}
_cache_lock = threading.Lock()  # Для безопасного доступа из разных потоков

user_last_request = {}
user_state = {}

# ========== УНИФИЦИРОВАННЫЕ СООБЩЕНИЯ ==========
MESSAGES = {
    'start': "Напишите /start для перезапуска",
    'hw': "Напишите /hw для загрузки заданий",
    'links': "Напишите /links для просмотра ссылок",
    'help': "Напишите /help для помощи",
    'error': "❌ Ошибка. Напишите /start для перезапуска",
    'no_data': "📭 Данные не загружены. Напишите /hw",
    'cooldown': "⏳ Подождите {} секунд перед следующим запросом",
    'new_data': "🔔 Обнаружены новые данные! Рекомендуется обновить список",
    'update_available': "🔄 Доступно обновление",
    'api_error': "⚠️ Временно недоступно. Используются сохранённые данные",
    'data_from': "📅 Данные от {}"
}

# ========== СОСТОЯНИЯ ПОЛЬЗОВАТЕЛЕЙ ==========
USER_STATES = {
    'MAIN_MENU': '🏠 ГЛАВНОЕ МЕНЮ',
    'TASKS_LIST': '📚 СПИСОК ЗАДАНИЙ',
    'HELP_MAIN': '❓ ПОМОЩЬ (главная)',
    'HELP_TASKS': '❓ ПОМОЩЬ (задания)',
    'FILTER_TODAY': '📅 ФИЛЬТР: СЕГОДНЯ',
    'FILTER_OVERDUE': '⚠️ ФИЛЬТР: ПРОСРОЧКА',
    'LINKS': '🔗 ССЫЛКИ'
}


def set_user_state(user_id, state, page=None):
    """Устанавливает состояние пользователя с красивым логированием"""
    if page is not None and state == 'TASKS_LIST':
        user_state[user_id] = f'TASKS_LIST_PAGE_{page}'
        logger.info(f"📍 📄 СТРАНИЦА {page}")
    else:
        user_state[user_id] = state
        logger.info(f"📍 {USER_STATES.get(state, 'НЕИЗВЕСТНОЕ СОСТОЯНИЕ')}")


def get_moscow_time():
    """Возвращает текущее время в Москве"""
    return datetime.now(MOSCOW_TZ)


def format_moscow_time(dt=None):
    """Форматирует время в московский формат"""
    if dt is None:
        dt = get_moscow_time()
    return dt.strftime("%d.%m.%Y %H:%M")


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
    """Извлекает URL и текст из формулы ГИПЕРССЫЛКА"""
    if not formula or not isinstance(formula, str):
        return None, None

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
            return url, text

    try:
        url_match = re.search(r'"([^"]+)"', formula)
        if url_match:
            url = url_match.group(1)
            text_match = re.search(r'[;,]\s*"([^"]+)"', formula)
            text = text_match.group(1) if text_match else "Ссылка"
            return url, text
    except Exception as e:
        logger.debug(f"Ошибка парсинга ссылки: {e}")
        pass

    return None, None


def sync_windows_time():
    """Пытается синхронизировать время в Windows"""
    try:
        import platform
        import subprocess
        if platform.system() == "Windows":
            logger.info("🔄 Синхронизация времени Windows...")
            result = subprocess.run("w32tm /resync", shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                logger.info("✅ Синхронизация времени выполнена")
                return True
            else:
                logger.warning(f"⚠️ Ошибка синхронизации: {result.stderr}")
    except Exception as e:
        logger.error(f"❌ Ошибка при синхронизации времени: {e}")
    return False


@safe_api_call(default_return=[])
@timer_decorator
def get_homework_fast(force_refresh=False):
    """Загрузка данных из Google Sheets с сохранением последней успешной версии"""
    global _data_cache

    # Проверяем кэш (с блокировкой для потокобезопасности)
    with _cache_lock:
        if not force_refresh and _data_cache['data'] and (time.time() - _data_cache['timestamp']) < CACHE_TTL:
            logger.info("📦 Использую кэшированные данные")
            return _data_cache['data']

    logger.info("⚡ Загрузка из Google Sheets...")

    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)

        spreadsheet = client.open_by_key(SHEET_KEY)
        sheet = spreadsheet.worksheet(SHEET_WORKSHEET)

        all_values = sheet.get_all_values(value_render_option='FORMULA')

        if len(all_values) < 2:
            logger.warning("⚠️ В таблице только заголовки")
            with _cache_lock:
                _data_cache['timestamp'] = time.time()
            return _data_cache.get('last_successful_data', [])

        headers = all_values[0]
        records = []

        for row_idx, row in enumerate(all_values[1:], start=2):
            record = {}
            for i, header in enumerate(headers):
                if i >= len(row):
                    value = ''
                else:
                    value = row[i]

                if header == "Задание" and value and isinstance(value, str):
                    if 'HYPERLINK' in value or 'ГИПЕРССЫЛКА' in value:
                        url, text = parse_hyperlink_formula(value)
                        if url:
                            record[header] = {
                                'text': text or "Ссылка",
                                'url': url,
                                'is_hyperlink': True
                            }
                        else:
                            record[header] = {'text': value, 'is_hyperlink': False}
                    else:
                        record[header] = {'text': value, 'is_hyperlink': False}

                elif header == "Срок" and value:
                    try:
                        if isinstance(value, (int, float)) or (
                                isinstance(value, str) and value.replace('.', '').replace('-', '').isdigit()):
                            excel_date = float(value)
                            if excel_date > 0:
                                base_date = datetime(1899, 12, 30)
                                converted_date = base_date + timedelta(days=excel_date)
                                date_str = converted_date.strftime("%d.%m.%Y")
                                record[header] = date_str
                            else:
                                record[header] = str(value)
                        else:
                            record[header] = str(value)
                    except Exception as e:
                        logger.debug(f"Ошибка преобразования даты: {e}")
                        record[header] = str(value)
                else:
                    record[header] = value

            records.append(record)

        hyperlink_count = sum(1 for r in records
                              if isinstance(r.get('Задание'), dict)
                              and r['Задание'].get('is_hyperlink'))

        # Обновляем кэш (с блокировкой)
        with _cache_lock:
            _data_cache['last_successful_data'] = records
            _data_cache['data'] = records
            _data_cache['timestamp'] = time.time()
            _data_cache['version'] += 1

        logger.info(f"✅ Загружено: {len(records)} записей, {hyperlink_count} гиперссылок")
        logger.info(f"📅 Время данных: {format_moscow_time()}")

        return records

    except (GoogleAuthError, HttpError, RefreshError) as e:
        logger.error(f"❌ Ошибка авторизации Google: {e}")
        if "invalid_grant" in str(e):
            logger.warning("⚠️ Обнаружена ошибка времени, пробую синхронизировать...")
            sync_windows_time()
        with _cache_lock:
            if _data_cache.get('last_successful_data'):
                logger.info("📦 Возвращаю последние успешные данные")
                return _data_cache['last_successful_data']
        raise
    except Exception as e:
        logger.error(f"❌ Неизвестная ошибка: {e}")
        with _cache_lock:
            if _data_cache.get('last_successful_data'):
                logger.info("📦 Возвращаю последние успешные данные")
                return _data_cache['last_successful_data']
        raise


# ========== ФОНОВОЕ ОБНОВЛЕНИЕ ==========
def background_cache_updater():
    """Фоновая задача для проверки обновлений"""
    global _data_cache

    while True:
        try:
            time.sleep(CHECK_INTERVAL)
            logger.info("🔄 Проверка обновлений...")

            try:
                get_homework_fast(force_refresh=True)
            except Exception as e:
                logger.error(f"❌ Ошибка при проверке обновлений: {e}")

        except Exception as e:
            logger.error(f"❌ Критическая ошибка в фоновом обновлении: {e}")


async def daily_update_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневное обновление данных в 00:00 МСК с уведомлением пользователей"""
    moscow_time = get_moscow_time()
    logger.info(f"📅 Запуск ежедневного обновления в {format_moscow_time(moscow_time)}")

    try:
        old_version = _data_cache.get('version', 0)
        new_data = get_homework_fast(force_refresh=True)
        new_version = _data_cache.get('version', 0)

        if new_data:
            if new_version > old_version:
                logger.info(f"✅ Данные обновлены: версия {old_version} -> {new_version}")

                # Получаем всех пользователей
                users = get_subscribed_users()

                if users:
                    update_message = (
                        "🔄 <b>Ежедневное обновление</b>\n\n"
                        f"📅 Данные обновлены на {format_moscow_time()}\n"
                        "Напишите /hw для просмотра"
                    )

                    sent_count = 0
                    for user_id in users:
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=update_message,
                                parse_mode="HTML"
                            )
                            sent_count += 1
                            await asyncio.sleep(0.05)
                        except Exception as e:
                            logger.error(f"❌ Ошибка отправки уведомления {user_id}: {e}")

                    logger.info(f"📨 Уведомления отправлены {sent_count} пользователям")
            else:
                logger.info("📅 Данные не изменились")
        else:
            logger.warning("⚠️ Не удалось получить новые данные")

    except Exception as e:
        logger.error(f"❌ Ошибка ежедневного обновления: {e}")


# ========== ФУНКЦИИ ФОРМАТИРОВАНИЯ ==========

def check_for_updates(context, user_id):
    """Проверяет, есть ли обновления для пользователя"""
    user_version = context.user_data.get('data_version', 0)
    current_version = _data_cache.get('version', 0)
    return current_version > user_version


def format_homework_page(records, page=0, show_update_notice=False, current_filter=None):
    """Форматирует одну страницу с заданиями"""
    if not records:
        return "📭 На сегодня заданий нет. Можно отдыхать!", []

    def get_date(item):
        date_str = item.get('Срок', '')
        if date_str:
            try:
                return datetime.strptime(date_str, "%d.%m.%Y")
            except Exception as e:
                logger.debug(f"Ошибка парсинга даты: {e}")
                pass
        return datetime.max

    sorted_records = sorted(records, key=get_date)

    total_pages = max(1, (len(sorted_records) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, len(sorted_records))
    current_page_records = sorted_records[start_idx:end_idx]

    message = ""
    if show_update_notice:
        message += f"{MESSAGES['new_data']}\n\n"

    if _data_cache['timestamp'] > 0:
        update_time = datetime.fromtimestamp(_data_cache['timestamp']).strftime("%d.%m.%Y %H:%M")
        message += f"{MESSAGES['data_from'].format(update_time)}\n"

    filter_indicator = ""
    if current_filter == 'today':
        filter_indicator = " [Фильтр: сегодня]"

    message += f"📚 <b>Домашнее задание{filter_indicator}</b> (страница {page + 1}/{total_pages})\n\n"

    for idx, item in enumerate(current_page_records, start=start_idx + 1):
        subject = item.get('Предмет', 'Без предмета')
        due_date = item.get('Срок', '')
        task_data = item.get('Задание', 'Нет описания')

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
            except Exception as e:
                logger.debug(f"Ошибка вычисления статуса: {e}")
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

    keyboard = []

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    filter_buttons = []
    refresh_emoji = "🔄" + ("❗" if show_update_notice else "")
    filter_buttons.append(InlineKeyboardButton(f"{refresh_emoji} Обновить", callback_data="refresh_data"))

    if current_filter != 'today':
        filter_buttons.append(InlineKeyboardButton("📅 Сегодня", callback_data="filter_today"))

    if current_filter:
        filter_buttons.append(InlineKeyboardButton("◀️ К списку", callback_data="back_to_tasks"))

    if filter_buttons:
        for i in range(0, len(filter_buttons), 2):
            keyboard.append(filter_buttons[i:i + 2])

    keyboard.append([
        InlineKeyboardButton("❓ Помощь", callback_data="help_tasks"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
    ])

    return message, keyboard


# ========== ФУНКЦИЯ ПРОВЕРКИ ССЫЛОК ==========
async def check_links_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача для проверки новых ссылок"""
    logger.info("🔍 Проверяю новые ссылки...")

    try:
        pending = get_pending_links()

        if pending:
            logger.info(f"📬 Найдено {len(pending)} новых ссылок")
            users = get_subscribed_users()

            if not users:
                logger.warning("⚠️ Нет подписанных пользователей")
                return

            for link in pending:
                message = (
                    f"🔔 <b>Новая ссылка!</b>\n\n"
                    f"📚 {link['par_name']}\n"
                    f"🔗 <a href='{link['link']}'>Перейти</a>\n"
                )

                sent_count = 0
                for user_id in users:
                    try:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=message,
                            parse_mode="HTML",
                            disable_web_page_preview=False
                        )
                        sent_count += 1
                        await asyncio.sleep(0.05)
                    except Exception as e:
                        logger.error(f"❌ Ошибка отправки пользователю {user_id}: {e}")

                mark_link_notified(link['id'])
                logger.info(f"✅ Отправлено {sent_count} пользователям, ссылка ID {link['id']}")
        else:
            logger.info("📭 Новых ссылок нет")

    except Exception as e:
        logger.error(f"❌ Ошибка в check_links_job: {e}", exc_info=True)


# ========== КОМАНДА ДЛЯ ПРОСМОТРА ССЫЛОК ==========
async def links_command(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Показывает сегодняшние ссылки"""
    user = update.effective_user
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} запросил ссылки (/links)")

    try:
        today_links = get_today_links()

        if today_links:
            message = "🔗 <b>Сегодняшние ссылки:</b>\n\n"
            for w in today_links:
                status = "✅ Отправлено" if w['notified'] else "⏳ Ожидает"
                message += f"• <b>{w['par_name']}</b>\n"
                message += f"  🔗 {w['link']}\n"
                message += f"  {status}\n\n"
        else:
            message = "📭 На сегодня ссылок нет"

        await update.message.reply_text(message, parse_mode="HTML")

    except Exception as e:
        logger.error(f"❌ Ошибка в links_command: {e}")
        await update.message.reply_text("❌ Не удалось получить список ссылок")


# ========== ОБРАБОТЧИКИ КОМАНД ==========

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start - главное меню"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    # Добавляем пользователя в БД
    db.add_user(user.id, user.username, user.first_name)
    logger.info(f"👤 Пользователь {user.id} сохранен в БД")
    logger.info(f"👤 Пользователь @{username} (ID: {user_id}) вызвал команду /start")
    set_user_state(user_id, 'MAIN_MENU')

    keyboard = [
        [InlineKeyboardButton("📚 Показать задания", callback_data="show_hw")],
        [InlineKeyboardButton("🔗 Ссылки сегодня", callback_data="links_today")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я бот для отслеживания домашних заданий и ссылок.\n"
        "Выбери действие:",
        reply_markup=reply_markup
    )


async def help_command(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user = update.effective_user
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} вызвал команду /help")

    help_text = (
        "📚 <b>Помощь по боту</b>\n\n"
        "<b>Основные команды:</b>\n"
        "• /hw - показать домашнее задание\n"
        "• /links - показать ссылки\n"
        "• /help - показать это сообщение\n"
        "• /start - главное меню\n\n"

        "<b>Как пользоваться:</b>\n"
        "1. Напишите /hw для просмотра заданий\n"
        "2. Напишите /links для просмотра ссылок\n"
        "3. Используйте кнопки для навигации\n\n"

        "<b>Статусы заданий:</b>\n"
        "• 🔥 СЕГОДНЯ! - сдать сегодня\n"
        "• ⚠️ ЗАВТРА! - сдать завтра\n"
        "• ⏰ N дн. - осталось N дней\n"
        "• ❗️ ПРОСРОЧЕНО - задание просрочено\n\n"

        "<b>Обновление данных:</b>\n"
        "• Задания обновляются ежедневно в 00:00 МСК\n"
        "• Дата последнего обновления показывается сверху\n"
        "• При ошибке показываются предыдущие данные\n\n"
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
    """Обработчик команды /hw"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} (ID: {user_id}) вызвал команду /hw")

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
                "• Присылать ссылки\n"
                "• Фильтровать по срокам\n\n"

                "<b>Команды:</b>\n"
                "/hw - показать задания\n"
                "/links - показать ссылки\n"
                "/start - главное меню\n"
                "/help - подробная справка\n\n"
                
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
                [InlineKeyboardButton("🔗 Ссылки сегодня", callback_data="links_today")],
                [InlineKeyboardButton("❓ Помощь", callback_data="help_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                "👋 Главное меню:",
                reply_markup=reply_markup
            )

        elif data == "links_today":
            set_user_state(user_id, 'LINKS')

            today_links = get_today_links()

            if today_links:
                message = "🔗 <b>Ссылки на сегодня:</b>\n\n"
                for link in today_links:
                    status = "✅" if link['notified'] else "⏳"
                    message += f"• {link['par_name']}\n"
                    message += f"  🔗 {link['link']}\n"
                    message += f"  {status}\n\n"
            else:
                message = "📭 На сегодня ссылок нет"

            keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
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
                "📚 <b>Помощь по боту</b>\n\n"
                "<b>Что я умею:</b>\n"
                "• Показывать домашние задания из таблицы\n"
                "• Отображать гиперссылки в заданиях\n"
                "• Присылать ссылки\n"
                "• Фильтровать по срокам\n\n"

                "<b>Команды:</b>\n"
                "/hw - показать задания\n"
                "/links - показать ссылки\n"
                "/start - главное меню\n"
                "/help - подробная справка"
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
                date_str = item.get('Срок', '')
                if date_str:
                    try:
                        due = datetime.strptime(date_str, "%d.%m.%Y").date()
                        if due == today:
                            filtered.append(item)
                    except Exception as e:
                        logger.debug(f"Ошибка парсинга даты: {e}")
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
        except Exception as e:
            logger.debug(f"Ошибка при отправке сообщения об ошибке: {e}")
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
    logger.info(f"  • Часовой пояс: Europe/Moscow (UTC+3)")
    logger.info(f"  • Текущее время МСК: {format_moscow_time()}")

    # Запускаем фоновую проверку обновлений заданий
    updater_thread = threading.Thread(target=background_cache_updater, daemon=True)
    updater_thread.start()
    logger.info("✅ Фоновая проверка обновлений заданий запущена")

    # Создаём приложение с настройками для многозадачности
    app = Application.builder() \
        .token(TELEGRAM_TOKEN) \
        .concurrent_updates(True) \
        .build()

    # Увеличиваем количество worker-ов для обработки
    if hasattr(app, 'dispatcher'):
        app.dispatcher.workers = 8
        logger.info("✅ Количество worker-ов увеличено до 8")

    # Добавляем фоновые задачи
    job_queue = app.job_queue

    # Проверка ссылок каждые 5 минут
    job_queue.run_repeating(check_links_job, interval=300, first=10)
    logger.info("✅ Фоновая проверка ссылок запущена (интервал 5 минут)")

    # Ежедневное обновление в 00:00 МСК
    try:
        # Получаем текущее время в Москве
        now_moscow = get_moscow_time()

        # Вычисляем следующую полночь по Москве
        next_midnight = now_moscow.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

        # Конвертируем в UTC для планировщика (он работает в UTC)
        next_midnight_utc = next_midnight.astimezone(UTC_TZ)
        now_utc = datetime.now(UTC_TZ)

        # Секунды до следующей полуночи по Москве
        seconds_until_midnight = (next_midnight_utc - now_utc).total_seconds()

        # Запускаем ежедневное обновление
        job_queue.run_repeating(
            daily_update_job,
            interval=86400,  # 24 часа
            first=seconds_until_midnight
        )
        logger.info(f"✅ Ежедневное обновление запланировано на {next_midnight.strftime('%H:%M')} МСК")

    except Exception as e:
        logger.error(f"⚠️ Ошибка при планировании обновления: {e}")
        # Запасной вариант - обновление каждые 6 часов
        job_queue.run_repeating(daily_update_job, interval=21600, first=3600)
        logger.info("✅ Использую запасной вариант: обновление каждые 6 часов")

    # Добавляем обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("hw", homework_command))
    app.add_handler(CommandHandler("links", links_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("✅ Обработчики команд зарегистрированы")
    logger.info("📍 Будет вестись лог состояний пользователей")
    logger.info("🤖 Бот готов к работе. Ожидаю команды...")
    logger.info("=" * 60)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
