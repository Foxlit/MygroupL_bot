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

# Создаем логгер для нашего бота
logger = logging.getLogger('homework_bot')
# Убираем лишние логи от библиотек
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ==========
load_dotenv()

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_CREDENTIALS")

# Проверка наличия токена
if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_BOT_TOKEN не найден в .env файле!")
    exit(1)

if not GOOGLE_CREDS_JSON:
    logger.error("❌ GOOGLE_CREDENTIALS не найден в .env файле!")
    exit(1)

# ========== КОНСТАНТЫ ==========
ITEMS_PER_PAGE = 5  # Сколько заданий показывать на одной странице
CACHE_TTL = 300  # Время жизни кэша в секундах (5 минут)
REQUEST_COOLDOWN = 5  # Секунд между запросами от одного пользователя

# Словарь для хранения времени последнего запроса пользователей
user_last_request = {}


# ========== ДЕКОРАТОРЫ ДЛЯ ЗАМЕРА ВРЕМЕНИ ==========
def timer_decorator(func):
    """Декоратор для замера времени выполнения синхронных функций"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        end = time.time()
        logger.info(f"⏱️ {func.__name__} выполнилась за {end - start:.3f} секунд")
        return result

    return wrapper


def async_timer_decorator(func):
    """Декоратор для замера времени выполнения асинхронных функций"""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.time()
        result = await func(*args, **kwargs)
        end = time.time()
        logger.info(f"⏱️ Асинхронная {func.__name__} выполнилась за {end - start:.3f} секунд")
        return result

    return wrapper


# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С GOOGLE SHEETS ==========

def parse_hyperlink_formula(formula):
    """Извлекает URL и текст из формулы ГИПЕРССЫЛКА"""
    if not formula:
        return None, None

    # Паттерн для русской версии с точкой с запятой
    pattern_ru = r'=HYPERLINK\("([^"]+)";\s*"([^"]*)"\)'

    match = re.search(pattern_ru, formula)
    if match:
        url = match.group(1)
        text = match.group(2)
        return url, text

    # Запасной вариант для других форматов
    try:
        # Простой парсинг: ищем URL в кавычках
        url_match = re.search(r'"([^"]+)"', formula)
        if url_match:
            url = url_match.group(1)
            # Ищем текст после точки с запятой
            text_match = re.search(r';\s*"([^"]+)"', formula)
            text = text_match.group(1) if text_match else "Ссылка"
            return url, text
    except:
        pass

    return None, None


@timer_decorator
def get_homework_from_sheets():
    """Функция получения данных из Google Sheets с поддержкой гиперссылок"""
    logger.info("📊 Начинаю загрузку данных из Google Sheets...")

    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly"
        ]

        logger.debug("Аутентификация в Google Sheets...")
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)

        logger.info("📂 Открываю таблицу по ключу...")
        # ⚠️ ЗАМЕНИ НА СВОЙ КЛЮЧ ТАБЛИЦЫ!
        spreadsheet = client.open_by_key("1vIxyMIs08MWVOtmuz9dh4O2CVwRINXmxaQnJBObgOFY")
        sheet = spreadsheet.worksheet("1 вариант")

        logger.info("📥 Получаю все значения из таблицы...")
        all_values = sheet.get_all_values()
        logger.info(f"✅ Получено {len(all_values)} строк")

        if len(all_values) < 2:
            logger.warning("⚠️ В таблице только заголовки или она пуста")
            return []

        headers = all_values[0]
        logger.info(f"📋 Заголовки: {headers}")

        records = []

        # Проходим по всем строкам, начиная со второй
        for row_idx in range(2, len(all_values) + 1):
            record = {}
            for col_idx, header in enumerate(headers, start=1):
                if not header:
                    continue

                # Получаем обычное значение
                cell = sheet.cell(row_idx, col_idx)
                value = cell.value if cell else ''

                if header == "Задание":
                    # Получаем формулу
                    formula_cell = sheet.cell(row_idx, col_idx, value_render_option='FORMULA')
                    formula = formula_cell.value if formula_cell else None

                    if formula and ('HYPERLINK' in formula):
                        url, text = parse_hyperlink_formula(formula)
                        if url:
                            if row_idx == 2:  # Логируем только первую найденную
                                logger.info(f"🔗 Найдена гиперссылка: {url[:50]}...")
                            record[header] = {
                                'text': text or value,
                                'url': url,
                                'is_hyperlink': True
                            }
                        else:
                            record[header] = {'text': value, 'is_hyperlink': False}
                    else:
                        record[header] = {'text': value, 'is_hyperlink': False}
                else:
                    record[header] = value

            records.append(record)

        hyperlink_count = sum(1 for r in records
                              if isinstance(r.get('Задание'), dict)
                              and r['Задание'].get('is_hyperlink'))

        logger.info(f"📊 Статистика: {len(records)} записей, {hyperlink_count} гиперссылок")

        return records

    except Exception as e:
        logger.error(f"❌ Ошибка при доступе к таблице: {e}", exc_info=True)
        return []


# ========== ФУНКЦИИ ФОРМАТИРОВАНИЯ ==========

def format_homework_page(records, page=0):
    """Форматирует одну страницу с заданиями"""
    if not records:
        return "📭 На сегодня заданий нет. Можно отдыхать!", []

    # Сортируем по сроку (самые срочные первыми)
    def get_date(item):
        date_str = item.get('Срок', '')
        if date_str:
            try:
                return datetime.strptime(date_str, "%d.%m.%Y")
            except:
                pass
        return datetime.max

    sorted_records = sorted(records, key=get_date)

    # Разбиваем на страницы
    total_pages = (len(sorted_records) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start_idx = page * ITEMS_PER_PAGE
    end_idx = min(start_idx + ITEMS_PER_PAGE, len(sorted_records))
    current_page_records = sorted_records[start_idx:end_idx]

    # Форматируем заголовок
    message = f"📚 <b>Домашнее задание</b> (страница {page + 1}/{total_pages})\n\n"

    # Добавляем задания
    for idx, item in enumerate(current_page_records, start=start_idx + 1):
        subject = item.get('Предмет', 'Без предмета')
        due_date = item.get('Срок', '')
        task_data = item.get('Задание', 'Нет описания')

        # Проверка на просрочку
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

        # Обработка задания (может быть словарем со ссылкой или простым текстом)
        if isinstance(task_data, dict):
            if task_data.get('is_hyperlink') and task_data.get('url'):
                # Это гиперссылка - делаем кликабельной
                url = task_data['url']
                text = task_data.get('text', 'Ссылка')
                task_display = f'<a href="{url}">{text}</a>'
            else:
                # Обычный текст из словаря
                task_display = task_data.get('text', '')
                # Экранируем HTML
                task_display = task_display.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        else:
            # Это просто строка
            task_display = str(task_data)
            # Проверяем, не является ли строка ссылкой
            if task_display.startswith(('http://', 'https://')):
                task_display = f'<a href="{task_display}">🔗 ссылка</a>'
            else:
                task_display = task_display.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        message += f"{idx}. <b>{subject}</b>\n"
        message += f"   📌 {task_display}\n"
        message += f"   📅 Срок: {due_date} {status_emoji}\n\n"

    # Создаем кнопки для навигации
    keyboard = []

    # Кнопки навигации по страницам
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️ Назад", callback_data=f"page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперед ▶️", callback_data=f"page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    # Кнопки фильтров
    filter_buttons = [
        InlineKeyboardButton("🔄 Обновить", callback_data="refresh"),
        InlineKeyboardButton("📅 Сегодня", callback_data="filter_today"),
        InlineKeyboardButton("⚠️ Просрочка", callback_data="filter_overdue")
    ]
    keyboard.append(filter_buttons)

    # Кнопка помощи
    keyboard.append([InlineKeyboardButton("❓ Помощь", callback_data="help")])

    return message, keyboard


# ========== ОБРАБОТЧИКИ КОМАНД ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    user = update.effective_user
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} вызвал команду /start")

    # Приветственное сообщение с кнопками
    keyboard = [
        [InlineKeyboardButton("📚 Показать задания", callback_data="show_hw")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я бот для отслеживания домашних заданий.\n"
        "Нажми кнопку ниже, чтобы увидеть актуальные задания:",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    user = update.effective_user
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} вызвал команду /help")

    help_text = (
        "📚 <b>Помощь по боту</b>\n\n"
        "<b>Команды:</b>\n"
        "/hw - показать домашнее задание\n"
        "/help - показать это сообщение\n"
        "/start - перезапустить бота\n\n"
        "<b>Кнопки навигации:</b>\n"
        "◀️ Назад / Вперед ▶️ - листать страницы\n\n"
        "<b>Фильтры:</b>\n"
        "🔄 Обновить - загрузить свежие данные\n"
        "📅 Сегодня - показать задания на сегодня\n"
        "⚠️ Просрочка - показать просроченные задания\n\n"
        "Данные берутся из Google Sheets таблицы группы."
    )

    await update.message.reply_text(help_text, parse_mode="HTML")


@async_timer_decorator
async def homework_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /hw"""
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} (ID: {user_id}) вызвал команду /hw")

    # Проверка на спам (cooldown)
    current_time = time.time()
    if user_id in user_last_request:
        time_diff = current_time - user_last_request[user_id]
        if time_diff < REQUEST_COOLDOWN:
            logger.warning(f"⚠️ Пользователь {username} слишком часто запрашивает данные")
            await update.message.reply_text(
                f"⏳ Подожди {REQUEST_COOLDOWN - time_diff:.0f} секунд перед следующим запросом"
            )
            return

    user_last_request[user_id] = current_time

    # Отправляем "печатает..." чтобы пользователь знал, что бот работает
    await update.message.chat.send_action(action="typing")

    loading_msg = await update.message.reply_text("🔍 Загружаю данные из таблицы...")

    try:
        # Проверяем кэш
        cache_key = "homework_cache"
        cache_time_key = "homework_cache_time"

        if context.bot_data.get(cache_time_key) and time.time() - context.bot_data[cache_time_key] < CACHE_TTL:
            logger.info("📦 Использую кэшированные данные")
            homework_data = context.bot_data[cache_key]
        else:
            logger.info("🔄 Загружаю свежие данные из таблицы")
            homework_data = get_homework_from_sheets()
            context.bot_data[cache_key] = homework_data
            context.bot_data[cache_time_key] = time.time()

        # Сохраняем данные в user_data
        context.user_data['homework_data'] = homework_data
        context.user_data['current_page'] = 0

        message, keyboard = format_homework_page(homework_data, 0)
        reply_markup = InlineKeyboardMarkup(keyboard)

        await loading_msg.delete()

        await update.message.reply_text(
            message,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

        logger.info(f"✅ Ответ пользователю {username} отправлен")

    except Exception as e:
        logger.error(f"❌ Ошибка для пользователя {username}: {e}", exc_info=True)
        await loading_msg.edit_text(f"❌ Ошибка при загрузке данных: {str(e)[:100]}")


@async_timer_decorator
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на инлайн-кнопки"""
    query = update.callback_query
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name

    logger.info(f"👤 Пользователь @{username} нажал кнопку: {query.data}")

    await query.answer()

    data = query.data
    homework_data = context.user_data.get('homework_data', [])
    current_page = context.user_data.get('current_page', 0)

    try:
        if data == "help":
            help_text = (
                "📚 <b>Помощь по боту</b>\n\n"
                "<b>Кнопки навигации:</b>\n"
                "◀️ Назад / Вперед ▶️ - листать страницы\n\n"
                "<b>Фильтры:</b>\n"
                "🔄 Обновить - загрузить свежие данные\n"
                "📅 Сегодня - показать задания на сегодня\n"
                "⚠️ Просрочка - показать просроченные задания\n\n"
                "<b>Команды:</b>\n"
                "/hw - показать задания\n"
                "/start - перезапустить бота"
            )

            keyboard = [[InlineKeyboardButton("◀️ Назад к заданиям", callback_data=f"page_{current_page}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                help_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data == "show_hw" or data == "refresh":
            await query.edit_message_text("🔍 Обновляю данные...")

            # Проверяем кэш при обновлении
            cache_key = "homework_cache"
            cache_time_key = "homework_cache_time"

            if context.bot_data.get(cache_time_key) and time.time() - context.bot_data[cache_time_key] < CACHE_TTL:
                logger.info("📦 Использую кэшированные данные для обновления")
                homework_data = context.bot_data[cache_key]
            else:
                logger.info("🔄 Загружаю свежие данные для обновления")
                homework_data = get_homework_from_sheets()
                context.bot_data[cache_key] = homework_data
                context.bot_data[cache_time_key] = time.time()

            context.user_data['homework_data'] = homework_data
            context.user_data['current_page'] = 0

            message, keyboard = format_homework_page(homework_data, 0)
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data.startswith("page_"):
            page = int(data.split("_")[1])
            context.user_data['current_page'] = page

            message, keyboard = format_homework_page(homework_data, page)
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data == "filter_today":
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

            logger.info(f"📅 Найдено {len(filtered)} заданий на сегодня")

            if filtered:
                message, keyboard = format_homework_page(filtered, 0)
                keyboard.append([InlineKeyboardButton("◀️ Все задания", callback_data="page_0")])
            else:
                message = "📭 На сегодня заданий нет!"
                keyboard = [[InlineKeyboardButton("◀️ Все задания", callback_data="page_0")]]

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

        elif data == "filter_overdue":
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

            logger.info(f"⚠️ Найдено {len(filtered)} просроченных заданий")

            if filtered:
                message, keyboard = format_homework_page(filtered, 0)
                keyboard.append([InlineKeyboardButton("◀️ Все задания", callback_data="page_0")])
            else:
                message = "✅ Просроченных заданий нет!"
                keyboard = [[InlineKeyboardButton("◀️ Все задания", callback_data="page_0")]]

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                message,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

    except Exception as e:
        logger.error(f"❌ Ошибка при обработке кнопки {data}: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ Произошла ошибка. Попробуйте еще раз или нажмите /hw",
            disable_web_page_preview=True
        )


# ========== ЗАПУСК БОТА ==========

def main():
    """Главная функция запуска бота"""
    logger.info("=" * 60)
    logger.info("🚀 ЗАПУСК БОТА")
    logger.info("=" * 60)

    logger.info(f"📊 Настройки:")
    logger.info(f"  • Токен: {TELEGRAM_TOKEN[:10]}... (скрыт)")
    logger.info(f"  • Google Sheets: подключен")
    logger.info(f"  • Логирование: включено")
    logger.info(f"  • Элементов на странице: {ITEMS_PER_PAGE}")
    logger.info(f"  • Время жизни кэша: {CACHE_TTL} сек")
    logger.info(f"  • Защита от спама: {REQUEST_COOLDOWN} сек")

    # Создаём приложение
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Добавляем обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("hw", homework_command))

    # Добавляем обработчик нажатий на кнопки
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("✅ Обработчики команд зарегистрированы")
    logger.info("🤖 Бот готов к работе. Ожидаю команды...")
    logger.info("=" * 60)

    # Запускаем бота (будет работать постоянно)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
