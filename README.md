# cookies_rotator

Редиректор на FastAPI для Vercel. Раскидывает трафик с основного домена по ссылкам из `TARGETS` строго по кругу и проверяет, что посетитель дошёл до целевого сайта. 900 кликов на 3 ссылки = 300/300/300.

```
клик -> /?subid=...  -> Redis: INCR, выбор ссылки, запись клика с cid -> 302 на ссылку + cid
целевой сайт: t.js   -> /t/arrive?cid=...&host=... -> клик отмечен как дошедший
/stats               -> отправлено, дошло, % доставки, задержка, приход не с того домена
```

Счётчик хранится в Upstash Redis: инстансы Vercel не делят память, а INCR атомарный.

## Роуты

| Роут | Что делает |
|---|---|
| `GET /` | 302 на следующую ссылку. Входящие параметры прокидываются, добавляется `cid`. Боты идут на первую ссылку мимо ротации |
| `GET /t.js` | сниппет для целевых сайтов |
| `GET /t/arrive` | пиксель доставки, засчитывается один раз на `cid` |
| `GET /stats?token=` | статистика по ссылкам |
| `POST /reset?token=` | обнулить счётчики |
| `GET /health` | статус и подключение Redis |

Если Redis недоступен, редирект идёт рандомом с заголовком `X-Rotator-Fallback: random`.

## Настройка

1. Vercel -> Storage -> Marketplace -> Upstash Redis -> Connect to project.
2. Settings -> Environment Variables:

```
TARGETS=https://x1.com,https://x2.com,https://x3.com
STATS_TOKEN=<секрет>
CLICK_TTL_DAYS=30
```

Ссылки можно указывать с путём и параметрами: `https://site.com/page?q=текст`. Параметры ссылки главнее входящих. Если в ссылках есть запятые, пиши по одной на строку.

3. На каждом целевом сайте в `<head>`:

```html
<script src="https://<rotator>/t.js" async></script>
```

## Проверка

```bash
curl -X POST "https://<rotator>/reset?token=$TOKEN"
for i in $(seq 1 900); do curl -s -o /dev/null -A "Mozilla/5.0" "https://<rotator>/?test=$i"; done
curl "https://<rotator>/stats?token=$TOKEN"
```

curl не исполняет JS, поэтому `arrived` останется 0. Доставку проверяй переходами из браузера.

Локально: `pip install fastapi httpx "fakeredis[lua]" && python tests/test_rotator.py`
