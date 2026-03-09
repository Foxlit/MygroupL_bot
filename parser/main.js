const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();
const { datesAndNames } = require("./shedule.json");

// Загружаем переменные окружения
require('dotenv').config();

// Проверяем наличие логина и пароля
const LOGIN = process.env.LOGIN;
const PASSWORD = process.env.PASSWORD;

if (!LOGIN || !PASSWORD) {
    console.error('❌ Ошибка: LOGIN и PASSWORD должны быть заданы в .env или переменных окружения');
    process.exit(1);
}

console.log('✅ Переменные окружения загружены');

// Функция для "пинга" - чтобы приложение не засыпало
function startPing() {
    setInterval(() => {
        console.log('.');
    }, 5 * 60 * 1000); // Каждые 5 минут
}

async function extractAndSaveLink(parTitle) {
    if (!parTitle) {
        console.log("❌ Предмет не указан.");
        return null;
    }

    console.log(`🚀 Запуск парсера для: "${parTitle}"`);

    const browser = await puppeteer.launch({
        headless: true, // В GitHub Actions должен быть true
        slowMo: 5,
        args: ['--no-sandbox', '--disable-setuid-sandbox']
    });

    const page = await browser.newPage();
    await page.setViewport({ width: 1920, height: 1080 });

    browser.on('targetcreated', async (target) => {
        if (target.type() === 'page') {
            const newPage = await target.page();
            console.log('📌 Новая вкладка открыта!');
            try {
                await newPage.waitForNavigation({ waitUntil: 'networkidle2', timeout: 10000 });
                console.log('URL новой вкладки:', newPage.url());
            } catch (e) {
                console.log('⚠️ Новая вкладка не загрузилась полностью');
            }
        }
    });

    try {
        console.log('1️⃣ Переход на страницу авторизации...');
        await page.goto('https://elearn.mmu.ru/login/index.php/', {
            waitUntil: 'domcontentloaded',
            timeout: 30000
        });

        // Ждём появления капчи
        await new Promise(resolve => setTimeout(resolve, 1600));

        // Решаем пример
        const math = await page.evaluate(() => {
            const element = document.querySelector('div.form-group:has(input[name="answer"])');
            return element?.textContent?.trim();
        });

        if (!math) {
            throw new Error('Не удалось найти математический пример');
        }

        const result = answerMath(math);
        console.log(`🧮 Пример: ${math} = ${result}`);

        // Заполняем форму
        await page.type('input[name=username]', LOGIN);
        await page.type('input[name=password]', PASSWORD);
        await page.type('input[name=answer]', result.toString());

        await page.click('button[type=submit]');
        await new Promise(resolve => setTimeout(resolve, 175));

        console.log('2️⃣ Переход на мои дисциплины...');
        await page.goto('https://elearn.mmu.ru/blocks/course_summary/index.php', {
            waitUntil: 'domcontentloaded',
            timeout: 30000
        });

        // Получаем список дисциплин
        const disciplines = await page.evaluate(() => {
            let assObj = {};
            const discElements = document.querySelectorAll("a[title*='часть']");

            for (let elem of discElements) {
                const fullName = elem.textContent;
                // Очищаем название (убираем всё до скобок)
                const cleanName = fullName.substring(0, fullName.indexOf("(") - 1).trim();
                assObj[cleanName] = elem.href;
            }
            return assObj;
        });

        console.log(`3️⃣ Ищем дисциплину "${parTitle}"...`);

        // Проверяем, есть ли такая дисциплина
        let disciplineUrl = disciplines[parTitle];

        // Если нет - пробуем частичное совпадение
        if (!disciplineUrl) {
            for (let [name, url] of Object.entries(disciplines)) {
                if (name.includes(parTitle) || parTitle.includes(name)) {
                    disciplineUrl = url;
                    console.log(`✅ Найдено частичное совпадение: "${name}"`);
                    break;
                }
            }
        }

        if (!disciplineUrl) {
            console.log('❌ Дисциплина не найдена. Доступны:');
            Object.keys(disciplines).slice(0, 5).forEach(name => console.log(`   - ${name}`));
            await browser.close();
            return null;
        }

        console.log(`4️⃣ Переход на страницу дисциплины...`);
        await page.goto(disciplineUrl, {
            waitUntil: 'domcontentloaded',
            timeout: 30000
        });

        // Ищем ссылку на занятие
        const link = await page.evaluate(() => {
            // Пробуем разные селекторы
            const selectors = [
                '[data-activityname*="Лекционные занятия"] a.aalink',
                '[data-activityname*="Семинарские занятия"] a.aalink',
                '.activityinstance a.aalink',
                'a[href*="mod/url"]'
            ];

            for (let selector of selectors) {
                const element = document.querySelector(selector);
                if (element) return element.href;
            }
            return null;
        });

        if (!link) {
            console.log('❌ Ссылка на занятие не найдена');
            await browser.close();
            return null;
        }

        console.log(`5️⃣ Переход по ссылке: ${link}`);
        await page.click(`a[href='${link}']`);
        await new Promise(resolve => setTimeout(resolve, 2000));

        // Ждём финального URL
        const finalUrl = page.url();
        console.log(`✅ Финальный URL: ${finalUrl}`);

        // Сохраняем в файл (резервное копирование)
        fs.writeFileSync(path.join(__dirname, 'link.txt'), finalUrl, 'utf-8');
        console.log('📁 Ссылка сохранена в link.txt');

        // Сохраняем в БД
        try {
            const db = new sqlite3.Database('../shared-data/bot_data.db');
            db.run(
                "INSERT INTO links (par_name, link) VALUES (?, ?)",
                [parTitle, finalUrl],
                function(err) {
                    if (err) {
                        console.error('❌ Ошибка сохранения в БД:', err);
                    } else {
                        console.log('✅ Ссылка сохранена в БД (ID: ' + this.lastID + ')');
                    }
                }
            );
            db.close();
        } catch (dbErr) {
            console.error('❌ Ошибка подключения к БД:', dbErr);
        }

        return finalUrl;

    } catch (error) {
        console.error('❌ Ошибка:', error);
        console.error('Стек ошибки:', error.stack);
        return null;
    } finally {
        await browser.close();
        console.log('🏁 Браузер закрыт');
    }
}

// Расчёт математического примера
function answerMath(math) {
    const expression = math.replace(/[^\d+\-*/]/g, '');
    // Безопасное вычисление (только простые операции)
    try {
        return eval(expression);
    } catch (e) {
        console.error('❌ Ошибка вычисления:', e);
        return 0;
    }
}

// Поиск сегодняшнего предмета по расписанию
function findTodayParName() {
    const today = new Date();
    const day = String(today.getDate()).padStart(2, '0');
    const month = String(today.getMonth() + 1).padStart(2, '0');
    const year = today.getFullYear();
    const todayString = `${day}.${month}.${year}`;

    console.log(`📅 Сегодня: ${todayString}`);

    for (let elem of datesAndNames) {
        if (elem.date === todayString) {
            console.log(`✅ Найден предмет: ${elem.name}`);
            return elem.name;
        }
    }
    console.log(`📭 На ${todayString} пар нет в расписании`);
    return null;
}

// Запуск пинга (чтобы приложение не засыпало)
startPing();

// Экспортируем функции для использования в GitHub Actions
module.exports = { extractAndSaveLink, findTodayParName };

// Если файл запущен напрямую - проверяем аргументы
if (require.main === module) {
    const parName = process.argv[2] || findTodayParName();
    if (parName) {
        extractAndSaveLink(parName);
    } else {
        console.log('❌ Не удалось определить предмет для парсинга');
        console.log('💡 Использование: node main.js "Название предмета"');
    }
}
