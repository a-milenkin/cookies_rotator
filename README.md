# cookies_rotator

Ротатор платного трафика для A/B-теста лендингов [MnogoReels](https://mnogo-reels.ru). Это FastAPI-приложение на Vercel.

## Зачем

Мы закупаем трафик (попандеры/пуши в PropellerAds) и хотим понять, какой лендинг MnogoReels лучше конвертит: главная, посадочная под подкасты, под вебинары и так далее. Если гнать весь трафик на одну страницу, сравнивать не с чем. Если делать отдельную кампанию под каждый лендинг, бюджет размазывается, а аудитории получаются разные.

Ротатор решает это одной ссылкой. В рекламной кампании стоит одна ссылка, ротатор по очереди (с учётом весов) раскидывает клики по вариантам, а потом показывает, сколько кликов и конверсий принёс каждый вариант. Победителю отдаём больше трафика, проигравших выключаем.

## Как работает

```
Propeller -> /go?subid=123&zone=9 -> ротатор выбирает вариант (a, b, c...) -> 302 на mnogo-reels.ru/...?lp=a&subid=123
                                         |
                              Redis: клики по вариантам, subid -> вариант
                                         |
mnogo-reels.ru (регистрация/оплата) -> /postback?subid=123 -> +1 конверсия варианту a
                                         |
                                  /stats -> клики, конверсии, CR
```

Роуты:
- `/go` - редирект 302 на следующий лендинг по кругу с учётом весов. Прокидывает `subid`, `zone`, `utm_*` и добавляет `lp=<id варианта>`
- `/postback?subid=...&token=...` - засчитать конверсию варианту
- `/stats?token=...` - клики, конверсии и CR по каждому варианту
- `/` - healthcheck

Редиректит только на домены из `ALLOWED_HOSTS` (по умолчанию mnogo-reels.ru), чтобы ссылку нельзя было использовать как открытый редирект.

## План работ

1. ~~Код ротатора, локальные тесты (веса 2:1 -> 2006/994 на 3000 кликов)~~
2. ~~Залить код в этот репозиторий~~
3. Импортировать репо в Vercel (Hobby для теста, Pro перед боевым трафиком)
4. Подключить Upstash Redis через Vercel Marketplace, иначе вместо строгой очереди будет рандом и статистика не пишется
5. Прописать переменные окружения: `VARIANTS`, `ALLOWED_HOSTS`, `STATS_TOKEN`
6. Проверить `/go` и `/stats` на проде
7. На mnogo-reels.ru сохранять `subid` и `lp` при регистрации и дёргать `/postback`
8. Запустить кампанию в Propeller с Target URL ротатора, тестовый бюджет
9. Через 1-2 тысячи кликов на вариант смотреть `/stats`, перекидывать веса на победителя

## Деплой на Vercel

1. Vercel -> Add New -> Project -> Import этого репо. Фреймворк определится сам (Python, `api/index.py`).
2. Storage -> Marketplace -> Upstash Redis (бесплатный план) -> Connect to project. `KV_REST_API_URL` и `KV_REST_API_TOKEN` пропишутся сами.
3. Settings -> Environment Variables:

```
VARIANTS=[{"id":"a","url":"https://mnogo-reels.ru/","weight":1},{"id":"b","url":"https://mnogo-reels.ru/podcasts","weight":1}]
ALLOWED_HOSTS=mnogo-reels.ru
STATS_TOKEN=<длинный секрет>
```

4. Redeploy.

## Подключение к Propeller

Target URL кампании:

```
https://<проект>.vercel.app/go?subid=${SUBID}&zone=${ZONEID}&utm_source=propeller
```

Точные названия макросов сверь в кабинете Propeller (Tracking / Macros).

После регистрации или оплаты на mnogo-reels.ru:

```
GET https://<проект>.vercel.app/postback?subid=<subid>&token=<STATS_TOKEN>
```

Чтобы Propeller сам оптимизировал кампанию по конверсиям, дополнительно настрой его S2S postback.

## Файлы

- `api/index.py` - всё приложение
- `requirements.txt` - fastapi, httpx
