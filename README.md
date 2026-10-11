# cookies_rotator

Редиректор на FastAPI для Vercel. Человек заходит на основной домен, ротатор строго по кругу отправляет его на `x1.com`, `x2.com`, `x3.com` и проверяет, что трафик реально дошёл до целевого сайта. 900 кликов = ровно 300/300/300.

## Как работает

```
пользователь -> https://<rotator>/?utm_source=...
   -> Redis (атомарный Lua): INCR счётчика, выбор домена n % 3, запись клика с cid
   -> 302 на https://x1.com/?utm_source=...&cid=<cid>
x1.com: <script src="https://<rotator>/t.js" async></script>
   -> пиксель /t/arrive?cid=<cid>&host=x1.com -> клик отмечен как "дошёл"
/stats -> отправлено / дошло / % доставки / задержка / приход не с того домена
```

Счётчик живёт в Upstash Redis, потому что на Vercel каждый запрос может попасть в новый инстанс и память или SQLite между ними не шарятся. INCR атомарный, поэтому распределение ровное при любой параллельности.

## Роуты

- `/` - редирект 302 на следующий домен по кругу. Все query-параметры прокидываются, добавляется `cid`. Боты и превью мессенджеров редиректятся на первый домен и в ротацию не попадают, HEAD отдаёт 200
- `/t.js` - сниппет для целевых сайтов
- `/t/arrive?cid=...&host=...` - пиксель подтверждения, засчитывается только первый приход
- `/stats?token=...` - статистика по доменам. `distribution_spread` 0 или 1 значит идеально ровно
- `POST /reset?token=...` - обнулить счётчики перед новым тестом
- `/health` - healthcheck и проверка Redis

Если Redis недоступен, редирект всё равно работает (рандомом) и отдаёт заголовок `X-Rotator-Fallback: random`.

## Деплой на Vercel

1. Storage -> Marketplace -> Upstash Redis -> Connect to project. Переменные `KV_REST_API_URL` и `KV_REST_API_TOKEN` пропишутся сами (подойдёт любой префикс).
2. Settings -> Environment Variables:

```
TARGETS=https://x1.com,https://x2.com,https://x3.com
STATS_TOKEN=<длинный секрет>
```

3. Redeploy.

## На целевых сайтах

В `<head>` каждого из x1/x2/x3:

```html
<script src="https://<rotator>/t.js" async></script>
```

## Проверка 900 кликов

```bash
curl -X POST "https://<rotator>/reset?token=$TOKEN"
for i in $(seq 1 900); do curl -s -o /dev/null -A "Mozilla/5.0" "https://<rotator>/?test=$i"; done
curl "https://<rotator>/stats?token=$TOKEN"
```

curl не исполняет JS, поэтому `arrived` после такого теста будет 0, зато `sent` покажет 300/300/300. Доставку проверяй живыми переходами из браузера.

Локальный тест с фейковым Redis: `pip install fastapi httpx "fakeredis[lua]" && python tests/test_rotator.py`

## Файлы

- `api/index.py` - всё приложение
- `tests/test_rotator.py` - 900 параллельных кликов, пиксели, дубли, боты, падение Redis
- `requirements.txt` - fastapi, httpx
