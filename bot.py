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
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Импорт базы данных
from database import (
    db, add_user, is_authorized, authorize_user, is_in_whitelist,
    get_user_subscription, toggle_subscription, get_subscribed_users,
    get_pending_links, mark_link_notified, get_today_links, add_log,
    add_to_whitelist, get_whitelist, remove_from_whitelist
)

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

# 👑 ID администратора из переменных окружения
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не найден в .env файле!")
    exit(1)

if not GOOGLE_CREDS_JSON:
    logger.error("❌ GOOGLE_CREDENTIALS не найден в .env файле!")
    exit(1)

if not SHEET_KEY:
    logger.error("❌ SHEET_KEY не найден в .env файле!")
    exit(1)

if ADMIN_ID == 0:
    logger.error("❌ ADMIN_ID не найден или некорректен в .env файле!")
    logger.error("💡 Добавьте в .env: ADMIN_ID=ваш_telegram_id")
    exit(1)

# ========== КОНСТАНТЫ ==========
ITEMS_PER_PAGE = 5
REQUEST_COOLDOWN = 5
CACHE_TTL = 300  # Кэш на 5 минут
CHECK_INTERVAL = 60  # Проверка обновлений каждую минуту

# Глобальный кэш с блокировкой для потокобезопасности
_data_cache = {
    'data': None,
    'previous_data': None,  # Предыдущая версия данных для сравнения
    'timestamp': 0,
    'version': 0,
    'last_successful_data': None
}
_cache_lock = threading.Lock()

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
    'LINKS': '🔗 ССЫЛКИ',
    'SETTINGS': '⚙️ НАСТРОЙКИ',
    'ADMIN': '👑 АДМИН ПАНЕЛЬ'
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


# ========== ДЕКОРАТОР АВТОРИЗАЦИИ ==========
def authorized_only(func):
    """Декоратор для проверки авторизации пользователя"""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user:
            return

        user_id = user.id
        username = user.username or user.first_name

        if not is_authorized(user_id):
            if is_in_whitelist(user_id):
                authorize_user(user_id)
                add_log(user_id, "auth", "INFO", f"Автоматическая авторизация по белому списку")
                logger.info(f"✅ Пользователь @{username} (ID: {user_id}) автоматически авторизован")
            else:
                logger.info(f"⛔ Пользователь @{username} (ID: {user_id}) попытался доступ без авторизации")

                keyboard = [[InlineKeyboardButton("🔑 Запросить доступ", callback_data="request_access")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                if update.message:
                    await update.message.reply_text(
                        "🔒 <b>Доступ ограничен</b>\n\n"
                        "Этот бот предназначен только для студентов группы.\n"
                        "Если вы из нашей группы, нажмите кнопку ниже для запроса доступа.\n\n"
                        "После проверки администратор добавит вас в белый список.",
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )
                elif update.callback_query:
                    await update.callback_query.edit_message_text(
                        "🔒 <b>Доступ ограничен</b>\n\n"
                        "Этот бот предназначен только для студентов группы.\n"
                        "Если вы из нашей группы, нажмите кнопку ниже для запроса доступа.\n\n"
                        "После проверки администратор добавит вас в белый список.",
                        parse_mode="HTML",
                        reply_markup=reply_markup
                    )

                add_log(user_id, "auth", "WARNING", "Попытка доступа без авторизации")
                return
        return await func(update, context, *args, **kwargs)

    return wrapper


# ========== ДЕКОРАТОР АДМИНА ==========
def admin_only(func):
    """Декоратор для проверки прав администратора"""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user:
            return

        if user.id != ADMIN_ID:
            if update.message:
                await update.message.reply_text("❌ У вас нет прав для выполнения этой команды.")
            elif update.callback_query:
                await update.callback_query.edit_message_text("❌ У вас нет прав для выполнения этого действия.")
            return

        return await func(update, context, *args, **kwargs)

    return wrapper


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


def is_record_changed(old_record, new_record):
    """Проверяет, изменилась ли запись значимо для плашки НОВОЕ"""
    if not old_record or not new_record:
        return True

    # Проверяем все поля, которые могут измениться
    important_fields = ['Предмет', 'Задание', 'Срок']

    for field in important_fields:
        old_value = old_record.get(field)
        new_value = new_record.get(field)

        # Нормализуем значения для сравнения
        old_str = str(old_value) if old_value is not None else ''
        new_str = str(new_value) if new_value is not None else ''

        # Если поле изменилось
        if old_str != new_str:
            # Для поля "Срок" специальная обработка
            if field == 'Срок':
                # Если было значение, а стало прочерком - это изменение
                if old_str and old_str != '-' and (new_str == '-' or new_str == ''):
                    logger.debug(f"📅 Дата удалена: {old_str} -> {new_str}")
                    return True

                # Если было прочерк, а появилась дата - это изменение
                if (old_str == '-' or old_str == '') and new_str and new_str != '-':
                    logger.debug(f"📅 Дата появилась: {old_str} -> {new_str}")
                    return True

                # Если обе даты - сравниваем их
                if old_str and new_str and old_str != '-' and new_str != '-':
                    try:
                        old_date = datetime.strptime(old_str, "%d.%m.%Y")
                        new_date = datetime.strptime(new_str, "%d.%m.%Y")
                        if old_date != new_date:
                            logger.debug(f"📅 Дата изменилась: {old_str} -> {new_str}")
                            return True
                    except:
                        return True
            else:
                # Для других полей любое изменение - важно
                logger.debug(f"✏️ Поле {field} изменилось: {old_str} -> {new_str}")
                return True

    return False


@safe_api_call(default_return=[])
@timer_decorator
def get_homework_fast(force_refresh=False):
    """Загрузка данных из Google Sheets с сохранением последней успешной версии"""
    global _data_cache

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

        with _cache_lock:
            # Сохраняем предыдущую версию перед обновлением
            if _data_cache['data'] is not None:
                _data_cache['previous_data'] = _data_cache['data'].copy()

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


# ========== ФУНКЦИИ ФОРМАТИРОВАНИЯ ==========

def check_for_updates(context, user_id):
    """Проверяет, есть ли реальные изменения в данных для пользователя"""
    current_data = _data_cache.get('data', [])
    previous_data = _data_cache.get('previous_data', [])
    seen_records = context.user_data.get('seen_records', {})

    if not current_data:
        return False

    # Если пользователь только что загрузил данные (есть seen_records) - проверяем изменения
    if seen_records:
        # 1. Проверяем, есть ли новые записи
        for item in current_data:
            subject = item.get('Предмет')
            due_date = item.get('Срок')

            if due_date == '-' or due_date == '':
                continue

            record_id = f"{subject}_{due_date}"

            if record_id not in seen_records:
                logger.debug(f"🆕 Новая запись: {record_id}")
                return True

        # 2. Проверяем, изменились ли существующие записи
        if previous_data:
            for i, item in enumerate(current_data):
                if i < len(previous_data):
                    if is_record_changed(previous_data[i], item):
                        subject = item.get('Предмет')
                        due_date = item.get('Срок')
                        if due_date != '-' and due_date != '':
                            record_id = f"{subject}_{due_date}"
                            if record_id not in seen_records:
                                logger.debug(f"✏️ Изменилась запись: {record_id}")
                                return True

        # 3. Проверяем, не удалились ли записи
        current_records = set()
        for item in current_data:
            subject = item.get('Предмет')
            due_date = item.get('Срок')
            if due_date != '-' and due_date != '':
                record_id = f"{subject}_{due_date}"
                current_records.add(record_id)

        for record_id in seen_records.keys():
            if record_id not in current_records:
                logger.debug(f"🗑️ Удалена запись: {record_id}")
                return True

    # Если пользователь ещё не видел никаких записей - считаем что всё новое
    # Но при первом просмотре не показываем уведомление
    return False


def format_homework_page(records, page=0, show_update_notice=False, current_filter=None, context=None):
    """Форматирует одну страницу с заданиями и плашкой НОВОЕ"""
    if not records:
        return "📭 На сегодня заданий нет. Можно отдыхать!", []

    def get_date(item):
        date_str = item.get('Срок')
        if date_str:
            try:
                return datetime.strptime(date_str, "%d.%m.%Y")
            except Exception as E:
                logger.debug(f"Ошибка парсинга даты: {E}")
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

    # Получаем просмотренные записи пользователя
    seen_records = context.user_data.get('seen_records', {}) if context else {}
    previous_data = _data_cache.get('previous_data', [])

    for idx, item in enumerate(current_page_records, start=start_idx + 1):
        subject = item.get('Предмет')
        due_date = item.get('Срок')
        task_data = item.get('Задание')

        # Проверка на прочерк в дате
        if due_date == '-' or due_date == '':
            is_new = False
        else:
            # Создаём уникальный идентификатор записи
            record_id = f"{subject}_{due_date}"

            # Проверяем, видел ли пользователь эту запись
            if record_id in seen_records:
                is_new = False
            else:
                # Проверяем, изменилась ли запись по сравнению с предыдущей версией
                if previous_data and idx - 1 < len(previous_data):
                    old_record = previous_data[idx - 1]
                    if old_record:
                        is_new = is_record_changed(old_record, item)
                    else:
                        is_new = True
                else:
                    is_new = True

        status_emoji = ""
        if due_date and due_date != '-':
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

        # Добавляем плашку НОВОЕ
        new_badge = " 🆕 <b>НОВОЕ</b>" if is_new else ""
        message += f"{idx}. <b>{subject}</b>{new_badge}\n"
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
@authorized_only
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


# ========== НАСТРОЙКИ ==========
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню настроек"""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id

    await query.answer()

    subscribed = get_user_subscription(user_id)
    sub_status = "✅ Включена" if subscribed else "❌ Отключена"
    sub_button = "🔕 Отключить рассылку" if subscribed else "🔔 Включить рассылку"

    message = (
        "⚙️ <b>Настройки</b>\n\n"
        f"👤 Ваш ID: <code>{user_id}</code>\n"
        f"📢 Рассылка ссылок: {sub_status}\n\n"
        "<b>О рассылке:</b>\n"
        "• Приходит автоматически при появлении новой ссылки\n"
        "• Только для подписанных пользователей\n"
        "• Можно отключить в любой момент"
    )

    keyboard = [
        [InlineKeyboardButton(sub_button, callback_data="toggle_subscription")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")]
    ]

    if user_id == ADMIN_ID:
        keyboard.insert(1, [InlineKeyboardButton("👑 Админ панель", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        message,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


async def toggle_subscription_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает подписку пользователя"""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id

    await query.answer()

    new_status = toggle_subscription(user_id)
    status_text = "включена" if new_status else "отключена"

    add_log(user_id, "settings", "INFO", f"Рассылка {status_text}")

    await query.edit_message_text(
        f"✅ Рассылка успешно {status_text}!\n\n"
        "Настройки обновлены.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад в настройки", callback_data="settings")
        ]])
    )


# ========== АДМИН ПАНЕЛЬ ==========
@admin_only
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ панель"""
    query = update.callback_query
    await query.answer()

    whitelist = get_whitelist()
    authorized_users = db.get_authorized_users()

    message = (
        "👑 <b>Админ панель</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• В белом списке: {len(whitelist)}\n"
        f"• Авторизовано: {len(authorized_users)}\n\n"
        "<b>Команды:</b>\n"
        "• <code>/whitelist</code> - список доступа\n"
        "• <code>/adduser &lt;id&gt; [комментарий]</code> - добавить пользователя\n"
        "• <code>/removeuser &lt;id&gt;</code> - удалить пользователя\n"
        "• <code>/broadcast &lt;текст&gt;</code> - массовая рассылка авторизованным пользователям\n\n"
        "<b>Навигация:</b>"
    )

    keyboard = [
        [InlineKeyboardButton("📋 Белый список", callback_data="admin_whitelist")],
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton("◀️ Назад", callback_data="settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        message,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


@admin_only
async def admin_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр белого списка с красивым форматированием даты"""
    query = update.callback_query
    await query.answer()

    whitelist = get_whitelist()

    if not whitelist:
        await query.edit_message_text(
            "📭 Белый список пуст.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")
            ]])
        )
        return

    message = "📋 <b>Белый список:</b>\n\n"
    for item in whitelist:
        added_at = db.format_date(item['added_at'])

        message += (
            f"👤 ID: <code>{item['user_id']}</code>\n"
            f"📝 {item['comment'] or 'без комментария'}\n"
            f"📅 Добавлен: {added_at}\n\n"
        )

    if len(message) > 4000:
        message = "📋 <b>Белый список (последние 10):</b>\n\n"
        for item in whitelist[-10:]:
            added_at = db.format_date(item['added_at'])
            message += f"• <code>{item['user_id']}</code> - {item['comment'] or '?'}\n"
            message += f"  🕐 {added_at}\n\n"

    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        message,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )


# ========== КОМАНДЫ АДМИНА ==========
@admin_only
async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для добавления пользователя в белый список"""
    try:
        args = context.args
        if len(args) < 1:
            await update.message.reply_text(
                "❌ Использование: /adduser <user_id> [комментарий]\n"
                "Пример: /adduser 123456789 Иванов Иван"
            )
            return

        user_id = int(args[0])
        comment = " ".join(args[1:]) if len(args) > 1 else ""

        add_to_whitelist(user_id, update.effective_user.id, comment)

        if not is_authorized(user_id):
            authorize_user(user_id)

        add_log(update.effective_user.id, "admin", "INFO", f"Добавил {user_id} в белый список")

        await update.message.reply_text(
            f"✅ Пользователь {user_id} добавлен в белый список!\n"
            f"Комментарий: {comment or 'не указан'}"
        )

    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID. ID должен быть числом.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


@admin_only
async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для удаления пользователя из белого списка"""
    try:
        args = context.args
        if len(args) < 1:
            await update.message.reply_text(
                "❌ Использование: /removeuser <user_id>\n"
                "Пример: /removeuser 123456789"
            )
            return

        user_id = int(args[0])

        if not is_in_whitelist(user_id):
            await update.message.reply_text(f"❌ Пользователь {user_id} не найден в белом списке.")
            return

        remove_from_whitelist(user_id)

        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET is_authorized = 0 WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()

        add_log(update.effective_user.id, "admin", "INFO", f"Удалил {user_id} из белого списка")

        await update.message.reply_text(
            f"✅ Пользователь {user_id} удалён из белого списка!\n"
            f"Он больше не имеет доступа к боту."
        )

    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID. ID должен быть числом.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


@admin_only
async def whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для просмотра белого списка с форматированием даты"""
    whitelist = get_whitelist()

    if not whitelist:
        await update.message.reply_text("📭 Белый список пуст.")
        return

    message = "📋 <b>Белый список:</b>\n\n"

    for item in whitelist:
        added_at = db.format_date(item['added_at'])

        message += (
            f"👤 ID: <code>{item['user_id']}</code>\n"
            f"📝 {item['comment'] or 'нет'}\n"
            f"📅 Добавлен: {added_at}\n\n"
        )

    try:
        if len(message) > 4000:
            message = "📋 <b>Белый список (последние 20):</b>\n\n"
            for item in whitelist[-20:]:
                added_at = db.format_date(item['added_at'])
                message += f"• <code>{item['user_id']}</code> - {item['comment'] or '?'}\n"
                message += f"  🕐 {added_at}\n\n"
            await update.message.reply_text(message, parse_mode="HTML")
        else:
            await update.message.reply_text(message, parse_mode="HTML")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке списка: {e}")
        await update.message.reply_text(
            "❌ Произошла ошибка при отправке списка.\n"
            f"Детали: {e}"
        )


@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для массовой рассылки сообщения всем пользователям"""
    try:
        args = context.args
        if len(args) < 1:
            await update.message.reply_text(
                "❌ Использование: /broadcast <текст сообщения>\n"
                "Пример: /broadcast 📢 Важное объявление!"
            )
            return

        message_text = " ".join(args)

        status_msg = await update.message.reply_text("📨 Начинаю рассылку...")

        users = db.get_authorized_users()

        if not users:
            await status_msg.edit_text("📭 Нет авторизованных пользователей для рассылки.")
            return

        success_count = 0
        fail_count = 0

        for user_id in users:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📢 <b>Рассылка</b>\n\n{message_text}",
                    parse_mode="HTML"
                )
                success_count += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"❌ Ошибка отправки пользователю {user_id}: {e}")
                fail_count += 1

        result_message = (
            f"✅ Рассылка завершена!\n\n"
            f"📊 Статистика:\n"
            f"• Успешно: {success_count}\n"
            f"• Ошибок: {fail_count}\n"
            f"• Всего: {len(users)}"
        )
        await status_msg.edit_text(result_message)

        add_log(update.effective_user.id, "admin", "INFO",
                f"Сделал рассылку: {success_count}/{len(users)} успешно")

    except Exception as e:
        logger.error(f"❌ Ошибка в broadcast_command: {e}")
        await update.message.reply_text(f"❌ Ошибка при рассылке: {e}")


@admin_only
async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для быстрого доступа к админ-панели (/ap и /adminpanel)"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👑 Админ @{username} (ID: {user_id}) вызвал команду /ap")

    whitelist = get_whitelist()
    authorized_users = db.get_authorized_users()

    message = (
        "👑 <b>Админ панель</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• В белом списке: {len(whitelist)}\n"
        f"• Авторизовано: {len(authorized_users)}\n\n"
        "<b>Команды (полные / сокращённые):</b>\n"
        "• /adminpanel или /ap - эта панель\n"
        "• /whitelist или /wl - список доступа\n"
        "• <code>/adduser &lt;id&gt; [комментарий]</code> или <code>/au &lt;id&gt; [комментарий]</code>- добавить пользователя\n"
        "• <code>/removeuser &lt;id&gt;</code> или <code>/ru &lt;id&gt;</code>- удалить пользователя\n"
        "• <code>/broadcast &lt;текст&gt;</code> или <code>/bc &lt;текст&gt;</code> - массовая рассылка авторизованным пользователям\n\n"
        "<b>Навигация:</b>"
    )

    keyboard = [
        [InlineKeyboardButton("📋 Белый список", callback_data="admin_whitelist")],
        [InlineKeyboardButton("📢 Сделать рассылку", callback_data="admin_broadcast")],
        [InlineKeyboardButton("◀️ В главное меню", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.message:
        await update.message.reply_text(
            message,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
    elif update.callback_query:
        await update.callback_query.edit_message_text(
            message,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )


# Сокращённые команды
@admin_only
async def adduser_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сокращение для /adduser - /au"""
    await add_user_command(update, context)


@admin_only
async def removeuser_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сокращение для /removeuser - /ru"""
    await remove_user_command(update, context)


@admin_only
async def whitelist_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сокращение для /whitelist - /wl"""
    await whitelist_command(update, context)


@admin_only
async def broadcast_shortcut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сокращение для /broadcast - /bc"""
    await broadcast_command(update, context)


@admin_only
async def admin_panel_full_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Полная версия команды для доступа к админ-панели (/adminpanel)"""
    await admin_panel_command(update, context)


# ========== ЗАПРОС ДОСТУПА ==========
async def request_access_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик запроса доступа"""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    username = user.username or "нет"
    first_name = user.first_name or "нет"

    await query.answer()

    if is_authorized(user_id):
        await query.edit_message_text(
            "✅ <b>Вы уже авторизованы!</b>\n\n"
            "У вас есть полный доступ ко всем функциям бота.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 В главное меню", callback_data="main_menu")
            ]])
        )
        return

    if is_in_whitelist(user_id):
        await query.edit_message_text(
            "⏳ <b>Запрос уже отправлен</b>\n\n"
            "Ваш запрос на доступ уже рассматривается администратором.\n"
            "Пожалуйста, ожидайте.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 На главную", callback_data="main_menu")
            ]])
        )
        return

    add_log(user_id, "auth", "INFO", "Запрос доступа")

    try:
        admin_message = (
            f"🔔 <b>Новый запрос доступа</b>\n\n"
            f"👤 ID: <code>{user_id}</code>\n"
            f"📝 Имя: {first_name}\n"
            f"📱 Username: @{username}\n\n"
            f"<b>Действия:</b>\n"
            f"✅ Добавить: <code>/adduser {user_id} {first_name}</code>\n"
            f"❌ Отклонить: игнорировать"
        )
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_message,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"❌ Не удалось отправить уведомление админу: {e}")

    await query.edit_message_text(
        "✅ <b>Запрос отправлен!</b>\n\n"
        "Администратор рассмотрит вашу заявку в ближайшее время.\n"
        "После подтверждения вы получите доступ к боту.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 На главную", callback_data="main_menu")
        ]])
    )


# ========== ОБРАБОТЧИКИ КОМАНД ==========

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    add_user(user.id, user.username, user.first_name)
    logger.info(f"👤 Пользователь @{username} (ID: {user_id}) вызвал команду /start")

    if is_authorized(user_id):
        set_user_state(user_id, 'MAIN_MENU')

        subscribed = get_user_subscription(user_id)
        sub_status = "✅ Включена" if subscribed else "❌ Отключена"

        keyboard = [
            [InlineKeyboardButton("📚 Показать задания", callback_data="show_hw")],
            [InlineKeyboardButton("🔗 Ссылки сегодня", callback_data="links_today")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
            [InlineKeyboardButton("❓ Помощь", callback_data="help_main")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"👋 С возвращением, {user.first_name}!\n\n"
            f"📊 Статус подписки: {sub_status}\n"
            "Выбери действие:",
            reply_markup=reply_markup
        )
    else:
        if is_in_whitelist(user_id):
            authorize_user(user_id)
            add_log(user_id, "auth", "INFO", "Автоматическая авторизация")
            logger.info(f"✅ Пользователь @{username} автоматически авторизован")
            await start(update, _)
        else:
            keyboard = [[InlineKeyboardButton("🔑 Запросить доступ", callback_data="request_access")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "🔒 <b>Доступ ограничен</b>\n\n"
                "Этот бот предназначен только для студентов группы.\n"
                "Если вы из нашей группы, нажмите кнопку ниже для запроса доступа.\n\n"
                "После проверки администратор добавит вас в белый список.",
                parse_mode="HTML",
                reply_markup=reply_markup
            )


async def help_command(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user = update.effective_user
    user_id = user.id
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
        "• ⏰ N дн. - сдать через N дней\n"
        "• ❗️ ПРОСРОЧЕНО - задание просрочено (для отладки)\n\n"

        "<b>Рассылка ссылок:</b>\n"
        "• В настройках можно включить/отключить автоматическую рассылку\n"
        "• При появлении новой ссылки она придёт всем подписанным пользователям\n"
        "• Рассылка приходит только по подписке, можно отключить в любой момент\n\n"

        "<b>Обновление данных:</b>\n"
        "• Дата последнего обновления показывается сверху\n"
        "• При ошибке показываются предыдущие данные\n\n"
    )

    if user_id == ADMIN_ID:
        help_text += (
            "\n\n👑 <b>Команды администратора:</b>\n"
            "• /adminpanel или /ap - админ панель\n"
            "• /adduser или /au <id> [комментарий] - добавить пользователя\n"
            "• /removeuser или /ru <id> - удалить пользователя\n"
            "• /whitelist или /wl - список доступа\n"
            "• /broadcast или /bc <текст> - массовая рассылка авторизованным пользователям\n"
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
@authorized_only
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

            message, keyboard = format_homework_page(
                homework_data, 0,
                current_filter=None,
                context=context
            )
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

            # Сохраняем просмотренные записи
            seen_records = {}
            for item in homework_data:
                subject = item.get('Предмет')
                due_date = item.get('Срок')
                if subject and due_date and due_date != '-':
                    record_id = f"{subject}_{due_date}"
                    seen_records[record_id] = time.time()

            context.user_data['homework_data'] = homework_data
            context.user_data['current_page'] = 0
            context.user_data['last_update'] = time.time()
            context.user_data['data_version'] = _data_cache.get('version', 0)
            context.user_data['seen_records'] = seen_records

            # Важно: при первом просмотре не показываем уведомление
            show_update_notice = False
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

    if data in ["request_access", "main_menu", "help_main"]:
        pass
    else:
        if not is_authorized(user_id):
            if is_in_whitelist(user_id):
                authorize_user(user_id)
                add_log(user_id, "auth", "INFO", "Автоматическая авторизация")
                logger.info(f"✅ Пользователь @{username} автоматически авторизован")
            else:
                keyboard = [[InlineKeyboardButton("🔑 Запросить доступ", callback_data="request_access")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    "🔒 <b>Доступ ограничен</b>\n\n"
                    "Этот бот предназначен только для студентов группы.\n"
                    "Если вы из нашей группы, нажмите кнопку ниже для запроса доступа.",
                    parse_mode="HTML",
                    reply_markup=reply_markup
                )
                return

    try:
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
                "/help - подробная справка\n"
                "/start - главное меню\n\n"
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

            subscribed = get_user_subscription(user_id)
            sub_status = "✅ Включена" if subscribed else "❌ Отключена"

            keyboard = [
                [InlineKeyboardButton("📚 Показать задания", callback_data="show_hw")],
                [InlineKeyboardButton("🔗 Ссылки сегодня", callback_data="links_today")],
                [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
                [InlineKeyboardButton("❓ Помощь", callback_data="help_main")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"👋 Главное меню\n\n📊 Статус подписки: {sub_status}",
                reply_markup=reply_markup
            )

        elif data == "settings":
            set_user_state(user_id, 'SETTINGS')
            await settings_menu(update, context)

        elif data == "toggle_subscription":
            await toggle_subscription_handler(update, context)

        elif data == "admin_panel":
            await admin_panel(update, context)

        elif data == "admin_whitelist":
            await admin_whitelist(update, context)

        elif data == "admin_broadcast":
            context.user_data['awaiting_broadcast'] = True
            context.user_data['broadcast_step'] = 'waiting_message'

            await query.edit_message_text(
                "📢 <b>Создание рассылки - Шаг 1/2</b>\n\n"
                "✍️ <b>Введите текст рассылки</b>\n\n"
                "Вы можете использовать HTML-разметку:\n"
                f"• <code>&lt;b&gt;текст&lt;/b&gt;</code> - жирный\n"
                f"• <code>&lt;i&gt;текст&lt;/i&gt;</code> - курсив\n"
                f"• <code>&lt;u&gt;текст&lt;/u&gt;</code> - подчёркнутый\n"
                f"• <code>&lt;s&gt;текст&lt;/s&gt;</code> - зачёркнутый\n"
                f"• <code>&lt;a href=\"ссылка\"&gt;текст&lt;/a&gt;</code> - ссылка\n"
                f"• <code>&lt;code&gt;текст&lt;/code&gt;</code> - моноширинный\n"
                f"• <code>&lt;pre&gt;текст&lt;/pre&gt;</code> - блок кода\n\n"
                "📝 Просто напишите сообщение в этот чат.\n"
                "Я его запомню и покажу предпросмотр.\n\n"
                "Для отмены напишите /cancel или нажмите кнопку ниже",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Отменить", callback_data="admin_panel")
                ]])
            )

        elif data == "broadcast_edit":
            context.user_data['broadcast_step'] = 'editing'

            current_text = context.user_data.get('broadcast_message', '')
            escaped_text = current_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            await query.edit_message_text(
                f"✏️ <b>Изменение рассылки</b>\n\n"
                f"📝 <b>Текущий текст:</b>\n"
                f"{escaped_text}\n\n"
                f"{'―' * 20}\n"
                f"Отправьте новый текст сообщения.\n\n"
                f"После отправки вы вернётесь к предпросмотру.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Вернуться к предпросмотру", callback_data="broadcast_preview")],
                    [InlineKeyboardButton("❌ Отменить рассылку", callback_data="admin_panel")]
                ])
            )

        elif data == "broadcast_preview":
            message_text = context.user_data.get('broadcast_message', '')
            context.user_data['broadcast_step'] = 'confirm'

            escaped_text = message_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

            preview = (
                f"📢 <b>Предпросмотр рассылки</b>\n\n"
                f"<b>🔍 КАК БУДЕТ ВЫГЛЯДЕТЬ:</b>\n"
                f"{message_text}\n\n"
                f"<b>📋 ИСХОДНЫЙ КОД (с тегами):</b>\n"
                f"<code>{escaped_text}</code>\n\n"
                f"{'―' * 30}\n"
                f"✅ <b>Подтверждение</b>\n\n"
                f"Отправить это сообщение всем пользователям?"
            )

            keyboard = [
                [
                    InlineKeyboardButton("✅ Отправить", callback_data="broadcast_confirm"),
                    InlineKeyboardButton("✏️ Изменить", callback_data="broadcast_edit")
                ],
                [InlineKeyboardButton("❌ Отменить", callback_data="admin_panel")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                preview,
                parse_mode="HTML",
                reply_markup=reply_markup
            )

        elif data == "broadcast_confirm":
            await query.edit_message_text("📨 Начинаю рассылку...")

            message_text = context.user_data.get('broadcast_message')

            users = db.get_authorized_users()

            if not users:
                await query.edit_message_text(
                    "📭 Нет авторизованных пользователей для рассылки.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")
                    ]])
                )
                context.user_data.pop('awaiting_broadcast', None)
                context.user_data.pop('broadcast_step', None)
                context.user_data.pop('broadcast_message', None)
                return

            success_count = 0
            fail_count = 0

            for user_id in users:
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"📢 <b>Рассылка</b>\n\n{message_text}",
                        parse_mode="HTML"
                    )
                    success_count += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки пользователю {user_id}: {e}")
                    fail_count += 1

            result = (
                f"✅ <b>Рассылка завершена!</b>\n\n"
                f"📊 Статистика:\n"
                f"• Успешно: {success_count}\n"
                f"• Ошибок: {fail_count}\n"
                f"• Всего: {len(users)}"
            )

            await query.edit_message_text(
                result,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад в админку", callback_data="admin_panel")
                ]])
            )

            add_log(user_id, "admin", "INFO", f"Рассылка: {success_count}/{len(users)} успешно")

            context.user_data.pop('awaiting_broadcast', None)
            context.user_data.pop('broadcast_step', None)
            context.user_data.pop('broadcast_message', None)

        elif data == "request_access":
            await request_access_handler(update, context)

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

        elif data == "show_hw":
            logger.info(f"📚 Пользователь {username} запросил задания")

            await query.edit_message_text("⚡ Загружаю данные...")

            try:
                homework_data = get_homework_fast(force_refresh=True)

                if homework_data:
                    set_user_state(user_id, 'TASKS_LIST')

                    message, keyboard = format_homework_page(
                        homework_data, 0,
                        current_filter=None,
                        context=context
                    )
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await query.edit_message_text(
                        message,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                        disable_web_page_preview=True
                    )

                    # Сохраняем просмотренные записи
                    seen_records = {}
                    for item in homework_data:
                        subject = item.get('Предмет')
                        due_date = item.get('Срок')
                        if subject and due_date and due_date != '-':
                            record_id = f"{subject}_{due_date}"
                            seen_records[record_id] = time.time()

                    context.user_data['homework_data'] = homework_data
                    context.user_data['current_page'] = 0
                    context.user_data['last_update'] = time.time()
                    context.user_data['data_version'] = _data_cache.get('version', 0)
                    context.user_data['seen_records'] = seen_records

                    # Важно: при первом просмотре не показываем уведомление
                    show_update_notice = False
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
                "🏠 Главное меню - вернуться на главную\n\n"
                "<b>Статусы:</b>\n"
                "🔥 СЕГОДНЯ! - сдать сегодня\n"
                "⚠️ ЗАВТРА! - сдать завтра\n"
                "⏰ N дн. - сдать через N дней\n"
                "❗️ ПРОСРОЧЕНО - задание просрочено (для отладки)"
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
                    current_filter=None,
                    context=context
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

                    message, keyboard = format_homework_page(
                        new_data, 0,
                        current_filter=None,
                        context=context
                    )
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await query.edit_message_text(
                        message,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                        disable_web_page_preview=True
                    )

                    # Сохраняем просмотренные записи
                    seen_records = {}
                    for item in new_data:
                        subject = item.get('Предмет')
                        due_date = item.get('Срок')
                        if subject and due_date and due_date != '-':
                            record_id = f"{subject}_{due_date}"
                            seen_records[record_id] = time.time()

                    context.user_data['homework_data'] = new_data
                    context.user_data['current_page'] = 0
                    context.user_data['last_update'] = time.time()
                    context.user_data['data_version'] = _data_cache.get('version', 0)
                    context.user_data['seen_records'] = seen_records

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
                current_filter=None,
                context=context
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
                date_str = item.get('Срок')
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
                    current_filter='today',
                    context=context
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
        logger.error(f"❌ Ошибка в кнопке: {data}: {e}", exc_info=True)
        try:
            await query.edit_message_text(
                f"❌ Произошла ошибка. {MESSAGES['start']}",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.debug(f"Ошибка при отправке сообщения об ошибке: {e}")
            pass


# ========== ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ ==========
async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовые сообщения для создания рассылки"""
    user = update.effective_user
    user_id = user.id

    if user_id != ADMIN_ID or not context.user_data.get('awaiting_broadcast'):
        return

    step = context.user_data.get('broadcast_step')
    message_text = update.message.text

    allowed_tags = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'a', 'code', 'pre']

    import re
    forbidden_tags = re.findall(r'<(\w+)[^>]*>', message_text)
    for tag in forbidden_tags:
        if tag.lower() not in allowed_tags and not tag.startswith('/'):
            await update.message.reply_text(
                f"❌ <b>Недопустимый HTML-тег</b>\n\n"
                f"Тег <code>&lt;{tag}&gt;</code> не поддерживается Telegram.\n\n"
                f"Поддерживаются только:\n"
                f"• <code>&lt;b&gt;</code> - жирный\n"
                f"• <code>&lt;i&gt;</code> - курсив\n"
                f"• <code>&lt;u&gt;</code> - подчёркнутый\n"
                f"• <code>&lt;s&gt;</code> - зачёркнутый\n"
                f"• <code>&lt;a href=\"\"&gt;</code> - ссылка\n"
                f"• <code>&lt;code&gt;</code> - моноширинный\n"
                f"• <code>&lt;pre&gt;</code> - блок кода\n\n"
                f"Пожалуйста, уберите этот тег и отправьте сообщение снова.",
                parse_mode="HTML"
            )
            return

    context.user_data['broadcast_message'] = message_text
    context.user_data['broadcast_step'] = 'confirm'

    escaped_text = message_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    preview = (
        f"📢 <b>Предпросмотр рассылки</b>\n\n"
        f"<b>🔍 КАК БУДЕТ ВЫГЛЯДЕТЬ:</b>\n"
        f"{message_text}\n\n"
        f"<b>📋 ИСХОДНЫЙ КОД (с тегами):</b>\n"
        f"<code>{escaped_text}</code>\n\n"
        f"{'―' * 30}\n"
        f"✅ <b>Подтверждение</b>\n\n"
        f"Отправить это сообщение всем пользователям?"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Отправить", callback_data="broadcast_confirm"),
            InlineKeyboardButton("✏️ Изменить", callback_data="broadcast_edit")
        ],
        [InlineKeyboardButton("❌ Отменить", callback_data="admin_panel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        preview,
        parse_mode="HTML",
        reply_markup=reply_markup
    )


# ========== ОБРАБОТЧИК КОМАНДЫ /CANCEL ==========
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    user = update.effective_user

    if user.id != ADMIN_ID:
        await update.message.reply_text("❌ У вас нет прав для этой команды.")
        return

    if context.user_data.get('awaiting_broadcast'):
        keyboard = [
            [
                InlineKeyboardButton("✅ Да, отменить", callback_data="admin_panel"),
                InlineKeyboardButton("❌ Нет, продолжить", callback_data="broadcast_preview")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "❓ <b>Отмена рассылки</b>\n\n"
            "Вы уверены, что хотите отменить создание рассылки?\n"
            "Все введённые данные будут потеряны.",
            parse_mode="HTML",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text("Нет активных действий для отмены.")


# ========== ЗАПУСК БОТА ==========

def main():
    """Главная функция запуска бота"""
    logger.info("=" * 60)
    logger.info("🚀 ЗАПУСК БОТА")
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
    logger.info(f"  • Администратор ID: {ADMIN_ID}")

    updater_thread = threading.Thread(target=background_cache_updater, daemon=True)
    updater_thread.start()
    logger.info("✅ Фоновая проверка обновлений заданий запущена")

    app = Application.builder() \
        .token(TELEGRAM_TOKEN) \
        .concurrent_updates(True) \
        .build()

    if hasattr(app, 'dispatcher'):
        app.dispatcher.workers = 8
        logger.info("✅ Количество worker-ов увеличено до 8")

    job_queue = app.job_queue

    job_queue.run_repeating(check_links_job, interval=300, first=10)
    logger.info("✅ Фоновая проверка ссылок запущена (интервал 5 минут)")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("hw", homework_command))
    app.add_handler(CommandHandler("links", links_command))

    # Админ-команды
    app.add_handler(CommandHandler("adduser", add_user_command))
    app.add_handler(CommandHandler("removeuser", remove_user_command))
    app.add_handler(CommandHandler("whitelist", whitelist_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("ap", admin_panel_command))
    app.add_handler(CommandHandler("adminpanel", admin_panel_full_command))

    # Сокращённые админ-команды
    app.add_handler(CommandHandler("au", adduser_shortcut))
    app.add_handler(CommandHandler("ru", removeuser_shortcut))
    app.add_handler(CommandHandler("wl", whitelist_shortcut))
    app.add_handler(CommandHandler("bc", broadcast_shortcut))

    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast_message))

    logger.info("✅ Обработчики команд зарегистрированы")
    logger.info("📍 Будет вестись лог состояний пользователей")
    logger.info("🤖 Бот готов к работе. Ожидаю команды...")
    logger.info("=" * 60)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
