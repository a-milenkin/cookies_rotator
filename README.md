# mnogo-rotator

FastAPI-ротатор для A/B-теста лендингов MnogoReels на платном трафике (PropellerAds и любой другой).

Роуты:
- `/go` - редирект 302 на следующий лендинг по кругу с учётом весов, прокидывает `subid`, `zone`, `utm_*` и добавляет `lp=<id варианта>`
- `/postback?subid=...&token=...` - засчитать конверсию (регистрация/оплата) за вариант
- `/stats?token=...` - клики, конверсии и CR по каждому варианту

Редиректит только на домены из `ALLOWED_HOSTS` (по умолчанию mnogo-reels.ru).

## Деплой на Vercel

1. Залей папку в GitHub-репо, в Vercel жмёшь Add New -> Project -> Import. Или из папки: `npx vercel --prod`.
2. Storage -> Marketplace -> Upstash Redis (бесплатный план), подключи к проекту. Переменные `KV_REST_API_URL` / `KV_REST_API_TOKEN` пропишутся сами. Без Redis ротатор работает на взвешенном рандоме и статистику не пишет.
3. Settings -> Environment Variables:

```
VARIANTS=[{"id":"a","url":"https://mnogo-reels.ru/","weight":1},{"id":"b","url":"https://mnogo-reels.ru/podcasts","weight":1}]
ALLOWED_HOSTS=mnogo-reels.ru
STATS_TOKEN=<любой длинный секрет>
```

4. Redeploy.

## Подключение к Propeller

Target URL кампании:

```
https://<проект>.vercel.app/go?subid=${SUBID}&zone=${ZONEID}&utm_source=propeller
```

Точные названия макросов сверь в кабинете Propeller (раздел Tracking / Macros).

На стороне mnogo-reels.ru сохраняй `subid` и `lp` из URL при регистрации, а после регистрации или оплаты дёргай:

```
GET https://<проект>.vercel.app/postback?subid=<subid>&token=<STATS_TOKEN>
```

Если хочешь передавать конверсии и в сам Propeller для автооптимизации, добавь их S2S postback URL отдельно.
