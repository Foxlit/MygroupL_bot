const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');
const { setTimeout } = require('timers');
const { parTimes, datesAndNames } = require("./schedule.json")

// ⭐ ЗАГРУЖАЕМ .env СРАЗУ В НАЧАЛЕ
if (process.env.NODE_ENV !== 'production') {
    require('dotenv').config();
    console.log('✅ Файл .env загружен');
}

// ⭐ ЧИТАЕМ ПЕРЕМЕННЫЕ
const LOGIN = process.env.LOGIN;
const PASSWORD = process.env.PASSWORD;

// ⭐ ПРОВЕРЯЕМ
if (!LOGIN || !PASSWORD) {
    console.error('❌ Ошибка: не заданы логин/пароль в переменных окружения');
    process.exit(1);
}

console.log('✅ Логин и пароль загружены успешно');

// Функция для поиска сегодняшнего предмета
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
    console.log(`❌ На ${todayString} вебинаров нет в расписании`);
    return null;
}

// Определяем название предмета
let parName = process.argv[2];

if (!parName) {
    console.log('🔍 Аргумент не указан, ищу сегодняшний предмет по расписанию...');
    parName = findTodayParName();
}

if (!parName) {
    console.error('❌ Ошибка: не удалось определить название пары');
    console.log('💡 Использование: node main.js "Название предмета"');
    console.log('   или: npm run dev "Название предмета"');
    console.log('   или: npm run dev');
    process.exit(1);
}

console.log(`🚀 Запуск парсера для предмета: "${parName}"`);


async function extractAndSaveLink(parTitle) {
    const browser = await puppeteer.launch({
        headless: false,
        slowMo: 5,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--window-size=1920,1080']
    });

    const page = await browser.newPage();
    await page.setViewport({ width: 1920, height: 1080 });

    browser.on('targetcreated', async (target) => {
        if (target.type() === 'page') {
            const newPage = await target.page();
            console.log('Новая вкладка открыта!');
            await newPage.waitForNavigation({ waitUntil: 'networkidle2' });
            console.log('URL новой вкладки:', newPage.url());
            await newPage.screenshot({ path: 'new-tab.png' });
        }
    });

    try {
        console.log('Перехожу на страницу авторизации...');
        await page.goto('https://elearn.mmu.ru/login/index.php/', {
            waitUntil: 'domcontentloaded',
            timeout: 30000
        });

        await new Promise(resolve => setTimeout(resolve, 1600));

        const math = await page.evaluate(() => {
            const element = document.querySelector('div.form-group:has(input[name="answer"])');
            return element?.textContent?.trim();
        });

        console.log("Результат математики: " + answerMath(math));

        // Используем логин и пароль из переменных окружения
        await page.type('input[name=username]', LOGIN);
        await page.type('input[name=password]', PASSWORD);
        await page.type('input[name=answer]', `${answerMath(math)}`);

        await page.click('button[type=submit]');
        await new Promise(resolve => setTimeout(resolve, 175));

        console.log('Переход на мои дисциплины...');
        await page.goto('https://elearn.mmu.ru/blocks/course_summary/index.php', {
            waitUntil: 'domcontentloaded',
            timeout: 30000
        });

        const disciplines = await page.evaluate(() => {
            let assObj = {};
            const discElements = document.querySelectorAll("a[title*='часть']");

            for (elem of discElements) {
                const ueban = elem.textContent;
                const horoshiy = ueban.substring(0, ueban.indexOf("(") - 1);
                assObj[horoshiy] = elem.href;
            }

            return assObj
        });

        console.log(`Переход на ${parTitle}...`);
        await page.goto(disciplines[parTitle], {
            waitUntil: 'domcontentloaded',
            timeout: 30000
        });

        const linkAAA = await page.evaluate(() => {
            const activity = document.querySelector('[data-activityname*="Лекционные занятия"], [data-activityname*="Семинар"]');
            const link = activity.querySelector('a.aalink');
            if (activity) {
                if (link) {
                    console.log('Ссылка на занятие:', link.href);
                } else {
                    console.log('Ссылка не найдена внутри элемента');
                }
            } else {
                console.log('Занятие не найдено');
            }
            return link.href;
        });
        console.log("Ссылка на занятие: " + linkAAA)

        await page.click(`a[href='${linkAAA}']`);
        console.log("Ссылка на занятие: " + linkAAA)
        await new Promise(resolve => setTimeout(resolve, 1000));

        await page.reload({ waitUntil: 'networkidle2' });
        console.log('Финальный URL:', page.url());

        fs.writeFileSync(path.join(__dirname, 'link.txt'), page.url() + "", 'utf-8');
        console.log('Ссылка сохранена в link.txt');
        console.log('Финальный URL:', page.url());

    } catch (error) {
        console.error('Ошибка:', error);
        console.error('Стек ошибки:', error.stack);
    } finally {
        console.log('Браузер закрыт');
        // await browser.close();
    }
}

function answerMath(math) {
    const expression = math.replace(/[^\d+\-*/]/g, '');
    const result = eval(expression);
    return result;
}

extractAndSaveLink(parName);
