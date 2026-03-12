const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const sqlite3 = require('sqlite3').verbose();
const { datesAndNames } = require("./shedule.json");
require("dotenv").config();

// Функция для паузы
const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

async function ensureDatabase() {
    """Проверяет существование БД и создаёт таблицу если нужно"""
    const dbPath = path.join(__dirname, '..', 'shared-data', 'bot_data.db');
    console.log(`📁 Путь к БД: ${dbPath}`);

    // Проверяем существование папки
    const dbDir = path.dirname(dbPath);
    if (!fs.existsSync(dbDir)) {
        console.log(`📁 Создаю папку: ${dbDir}`);
        fs.mkdirSync(dbDir, { recursive: true });
    }

    // Проверяем существование файла БД
    if (!fs.existsSync(dbPath)) {
        console.log(`⚠️ База данных не найдена, будет создана при первом подключении`);
    }

    return dbPath;
}

async function extractAndSaveLink(parTitle) {
    if (!parTitle) {
        console.log("❌ Предмет не указан.");
        return;
    }

    console.log(`🚀 Запуск парсера для: "${parTitle}"`);
    console.log(`🔧 Режим: ${process.env.GITHUB_ACTIONS ? 'GitHub Actions' : 'Локальный'}`);

    // Проверяем БД перед запуском
    const dbPath = await ensureDatabase();

    const browser = await puppeteer.launch({
        headless: true,  // В GitHub Actions всегда true
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--window-size=1920,1080',
            '--disable-dev-shm-usage',
            '--disable-gpu'
        ]
    });

    const page = await browser.newPage();
    await page.setViewport({ width: 1920, height: 1080 });

    // Переменная для хранения URL вебинара
    let webinarUrl = null;

    // Отслеживаем новые вкладки
    browser.on('targetcreated', async (target) => {
        if (target.type() === 'page') {
            const newPage = await target.page();
            console.log('📌 Новая вкладка открыта!');

            try {
                await newPage.waitForNavigation({ waitUntil: 'networkidle2', timeout: 30000 });
                const newUrl = newPage.url();
                console.log('URL новой вкладки:', newUrl);

                // Проверяем, что это ссылка на вебинар
                if (newUrl.includes('mts-link') || newUrl.includes('stream')) {
                    console.log('✅ Найдена ссылка на вебинар!');
                    webinarUrl = newUrl;
                    await newPage.screenshot({ path: 'webinar-page.png' });
                }
            } catch (e) {
                console.log('⚠️ Новая вкладка не загрузилась полностью');
            }
        }
    });

    try {
        console.log('1️⃣ Переход на страницу авторизации...');
        await page.goto('https://elearn.mmu.ru/login/index.php/', {
            waitUntil: 'domcontentloaded',
            timeout: 60000
        });

        await sleep(2000);

        // Решение капчи
        const math = await page.evaluate(() => {
            const element = document.querySelector('div.form-group:has(input[name="answer"])');
            return element?.textContent?.trim();
        });

        if (!math) {
            throw new Error('Не удалось найти математический пример');
        }

        const mathResult = answerMath(math);
        console.log("🧮 Пример:", math, "=", mathResult);

        // Заполнение формы
        await page.type('input[name=username]', process.env.LOGIN);
        await page.type('input[name=password]', process.env.PASSWORD);
        await page.type('input[name=answer]', String(mathResult));
        await page.click('button[type=submit]');

        await sleep(1000);

        console.log('2️⃣ Переход на мои дисциплины...');
        await page.goto('https://elearn.mmu.ru/blocks/course_summary/index.php', {
            waitUntil: 'networkidle2',
            timeout: 30000
        });

        // Парсинг дисциплин
        const disciplines = await page.evaluate(() => {
            const assObj = {};
            const discElements = document.querySelectorAll("a[title*='часть']");
            for (const elem of discElements) {
                const fullTitle = elem.textContent.trim();
                const bracketIndex = fullTitle.indexOf('(');
                if (bracketIndex === -1) continue;
                const cleanTitle = fullTitle.substring(0, bracketIndex).trim();
                assObj[cleanTitle] = elem.href;
            }
            return assObj;
        });

        console.log(`3️⃣ Ищем дисциплину "${parTitle}"...`);

        if (!disciplines[parTitle]) {
            console.log('❌ Дисциплина не найдена. Доступны:');
            Object.keys(disciplines).slice(0, 10).forEach(name => console.log(`   - ${name}`));
            await browser.close();
            return;
        }

        console.log(`4️⃣ Переход на страницу дисциплины...`);
        await page.goto(disciplines[parTitle], {
            waitUntil: 'networkidle2',
            timeout: 30000
        });

        // Ищем ссылку на занятие
        const linkInfo = await page.evaluate(() => {
            // Сначала ищем элемент занятия
            const activity = document.querySelector(
                '[data-activityname*="Лекционные занятия"], [data-activityname*="Семинарские занятия"]'
            );

            if (activity) {
                const linkEl = activity.querySelector('a.aalink');
                if (linkEl) {
                    return { type: 'activity', url: linkEl.href };
                }
            }

            // Если не нашли, ищем любую ссылку на занятие
            const urlLinks = document.querySelectorAll('a[href*="mod/url"]');
            if (urlLinks.length > 0) {
                return { type: 'url', url: urlLinks[0].href };
            }

            return { type: 'not_found', url: null };
        });

        if (!linkInfo.url) {
            console.log("❌ Ссылка на занятие не найдена");
            await page.screenshot({ path: 'no-link.png', fullPage: true });
            await browser.close();
            return;
        }

        console.log(`5️⃣ Переход по ссылке: ${linkInfo.url}`);

        // Переходим на страницу с описанием
        await page.goto(linkInfo.url, {
            waitUntil: 'networkidle2',
            timeout: 30000
        });

        // Ждём появления ссылки на вебинар
        await page.waitForSelector('div.urlworkaround a', { timeout: 15000 })
            .catch(() => console.log('⚠️ Селектор urlworkaround не найден'));

        // Ищем ссылку на вебинар
        const webinarLinkInfo = await page.evaluate(() => {
            // Пробуем разные селекторы
            const selectors = [
                'div.urlworkaround a',
                'a[href*="mts-link"]',
                'a[href*="stream"]',
                'a[href*="webinar"]',
                '.resourceworkaround a'
            ];

            for (const selector of selectors) {
                const element = document.querySelector(selector);
                if (element && element.href) {
                    return {
                        found: true,
                        url: element.href,
                        text: element.textContent?.trim()
                    };
                }
            }

            // Если не нашли по селекторам, ищем любую ссылку, которая может быть вебинаром
            const allLinks = document.querySelectorAll('a[href*="http"]');
            for (const link of allLinks) {
                if (link.href.includes('mts-link') || link.href.includes('stream')) {
                    return {
                        found: true,
                        url: link.href,
                        text: link.textContent?.trim()
                    };
                }
            }

            return { found: false };
        });

        if (!webinarLinkInfo.found) {
            console.log('❌ Ссылка на вебинар не найдена');
            await page.screenshot({ path: 'no-webinar-link.png', fullPage: true });

            // Временно используем текущий URL
            webinarLinkInfo.url = page.url();
            console.log(`⚠️ Использую текущий URL: ${webinarLinkInfo.url}`);
        } else {
            console.log(`✅ Найдена ссылка на вебинар: ${webinarLinkInfo.url}`);

            // Кликаем по ссылке
            await page.evaluate((url) => {
                const link = document.querySelector(`a[href="${url}"]`);
                if (link) {
                    link.click();
                }
            }, webinarLinkInfo.url);

            await sleep(5000);
        }

        // Определяем финальный URL
        const finalUrl = webinarUrl || webinarLinkInfo.url || page.url();
        console.log(`✅ Финальный URL: ${finalUrl}`);

        // Сохраняем в файл
        fs.writeFileSync(path.join(__dirname, 'link.txt'), finalUrl, 'utf-8');
        console.log('📁 Ссылка сохранена в link.txt');

        // Сохраняем в БД
        try {
            const db = new sqlite3.Database(dbPath);

            // Проверяем существование таблицы и создаём если нет
            db.run(`
                CREATE TABLE IF NOT EXISTS links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    par_name TEXT NOT NULL,
                    link TEXT NOT NULL,
                    parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    notified BOOLEAN DEFAULT 0
                )
            `);

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

    } catch (error) {
        console.error('❌ Ошибка:', error);
        console.error('📚 Стек ошибки:', error.stack);
        await page.screenshot({ path: 'error.png', fullPage: true });
    } finally {
        await browser.close();
        console.log('🏁 Браузер закрыт');
    }
}

// Вычисление математического примера
function answerMath(math) {
    const expression = math.replace(/[^\d+\-*/]/g, '');
    return eval(expression);
}

// Поиск сегодняшней пары
function findTodayParName() {
    const today = new Date();
    const day = String(today.getDate()).padStart(2, '0');
    const month = String(today.getMonth() + 1).padStart(2, '0');
    const year = today.getFullYear();
    const todayString = `${day}.${month}.${year}`;

    console.log(`📅 Сегодня: ${todayString}`);

    for (const elem of datesAndNames) {
        if (elem.date === todayString) {
            console.log(`✅ Найдена пара: ${elem.name}`);
            return elem.name;
        }
    }
    console.log(`📭 На ${todayString} пар нет`);
    return null;
}

// Запуск
if (require.main === module) {
    const parName = process.argv[2] || findTodayParName();
    if (parName) {
        extractAndSaveLink(parName);
    } else {
        console.log('❌ Не удалось определить предмет для парсинга');
        console.log('💡 Использование: node main.js "Название предмета"');
    }
}

module.exports = { extractAndSaveLink, findTodayParName };
