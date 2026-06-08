# Anti-Spam Agent Platform

Telegram guruhlarini ommaviy **"Adult-Scam"** va fishing tarqatuvchi userbotlardan
himoya qiluvchi platforma. Django (dashboard) + Pyrogram (userbot dvigateli)
asosida qurilgan. Yangi userbotlar (Agentlar) **QR-kod** orqali (SMS kodsiz)
ulanadi, guruhlarga avtomatik tarqatiladi va gibrid (Baza → Regex → ChatGPT)
filtr orqali spamni real vaqtda bloklaydi.

---

## 1. Arxitektura

```
                +------------------------+         +---------------------------+
                |   Django Dashboard     |         |   Telegram Engine         |
                |   (sync, web process)  |         |   (async, worker process) |
                |------------------------|         |---------------------------|
                |  views / templates     |         |  run_agents (Pyrogram)    |
                |  QR login UI (AJAX)    |         |  message handlers         |
                +-----------+------------+         +-------------+-------------+
                            |                                    |
                            v                                    v
                     +---------------------------------------------------+
                     |          SQLite/Postgres  (single source of truth)|
                     |  UserBot · TelegramGroup · BlacklistUser ·        |
                     |  SpamContent · SecurityLog                        |
                     +---------------------------------------------------+
```

**Django (sync) va Pyrogram (async) ni xavfsiz birga ishlatish** uchun:

- Barcha umumiy holat **bazada** saqlanadi (yagona haqiqat manbai).
- Pyrogram mijozlari **alohida, doimiy asyncio event-loop** da ishlaydi
  (`agents/engine/loop.py`). Hech qachon Django so'rov oqimida (request thread)
  ishlamaydi. Bu QR-login mijozining bir nechta AJAX so'rovlari davomida tirik
  qolishini ta'minlaydi.
- Async koddan ORM ga faqat `asgiref.sync.sync_to_async` orqali murojaat qilinadi
  (`agents/engine/repository.py`).
- Spam dvigateli (`run_agents`) dashboard'dan **alohida jarayon** sifatida
  ishlaydi (systemd / supervisor / alohida konteyner).

---

## 2. Loyiha tuzilishi

```
antispam_platform/        # Django project (settings, urls, wsgi, asgi)
agents/
  models.py               # UserBot, TelegramGroup, BlacklistUser, SpamContent, SecurityLog
  views.py                # dashboard, logs, QR API endpoints
  urls.py
  admin.py
  templates/agents/       # Bootstrap 5 dashboard (dark theme + Chart.js)
  engine/                 # Pyrogram bilan ishlovchi barcha kod
    loop.py               #   doimiy fon event-loop (singleton)
    qr_login.py           #   QR-kod login (ExportLoginToken xom-API oqimi)
    filters.py            #   gibrid filtr: precheck (1+2) + ai_decide (3)
    ai_filter.py          #   ChatGPT gpt-4o-mini klassifikatori
    hashing.py            #   MD5 (matn) + pHash (rasm/GIF)
    propagation.py        #   guruhga qo'shish + admin tayinlash (FloodWait-safe)
    repository.py         #   async-xavfsiz ORM yordamchilari
    runner.py             #   AgentRunner: userbotlarni ishga tushiradi
  management/commands/
    run_agents.py         #   python manage.py run_agents
    propagate_agent.py    #   python manage.py propagate_agent ...
```

---

## 3. O'rnatish

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # va qiymatlarni to'ldiring
python manage.py migrate
python manage.py createsuperuser
```

`.env` da kerakli qiymatlar:

| O'zgaruvchi | Tavsif |
|-------------|--------|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | https://my.telegram.org/apps dan |
| `OPENAI_API_KEY` | 3-bosqich (ChatGPT) filtri uchun |
| `OPENAI_MODEL` | standart `gpt-4o-mini` |
| `QR_TOKEN_REFRESH` | QR token yangilanish oralig'i (soniya) |
| `ACTION_THROTTLE_SECONDS` | FloodWait'dan saqlanish uchun kechikish |

---

## 4. Ishga tushirish

**Ikkita jarayon** kerak:

```bash
# 1) Web dashboard
python manage.py runserver 0.0.0.0:8000

# 2) Spam dvigateli (barcha faol userbotlarni ishga tushiradi)
python manage.py run_agents
```

Dashboard: <http://127.0.0.1:8000/> (admin login orqali kiriladi).

---

## 5. Userbotni QR-kod orqali ulash

1. Dashboard → **Add Userbot** sahifasiga o'ting.
2. Backend Pyrogram orqali `auth.ExportLoginToken` chaqiradi va
   `tg://login?token=...` havolasini QR-kod (PNG) ga aylantiradi.
3. QR sahifada **jonli** ko'rsatiladi va har ~25 soniyada (token eskirishidan
   oldin) avtomatik yangilanadi (AJAX polling, har 2 soniyada `api/qr/status`).
4. Telefonda: **Telegram → Settings → Devices → Link Desktop Device** → QR ni
   skanerlang.
5. Pyrogram sessiyani yakunlaydi, `StringSession` ni oladi va `UserBot` ni
   `active` statusida bazaga yozadi.

> DC migratsiyasi (`LoginTokenMigrateTo`) avtomatik hal qilinadi.

**2FA (cloud parol):** agar akkauntda ikki bosqichli parol yoqilgan bo'lsa, QR
skanerlangach sahifada **parol maydoni** paydo bo'ladi. Operator parolni kiritadi
(`api/qr/password`), backend `client.check_password()` orqali kirishni yakunlaydi.
Noto'g'ri parolda qayta urinish mumkin. Parol hech qachon saqlanmaydi.

---

## 6. Guruhga tarqatish va admin tayinlash (Dashboard orqali)

**Propagation** sahifasida: *Admin userbot* (allaqachon guruhda admin), *yangi
userbot* va *chat id* tanlanadi → **Queue job** bosiladi. Bu `PropagationJob`
yaratadi.

Ishni **engine jarayoni** (`run_agents`) bajaradi — chunki u admin botning *jonli
sessiyasi*ga ega. Bu bir sessiyani ikki jarayonda baravar ishlatishdan (Telegram
buni sessiya o'g'irlash deb hisoblab bekor qilishi mumkin) saqlaydi. Engine har
5 soniyada navbatdagi ishlarni oladi:

1. Userbot A → Userbot B ni guruhga qo'shadi (`add_chat_members`).
2. Userbot A → B ni *delete + ban* huquqlari bilan admin qiladi.

Ish holati (pending/running/done/failed) sahifada real-time ko'rinadi. Barcha
chaqiruvlar `FloodWait`-safe (`try/except` + retry + `asyncio.sleep`).

> CLI varianti ham mavjud:
> `python manage.py propagate_agent --admin <A_id> --new <B_id> --chat <chat_id>`

---

## 7. Gibrid filtr oqimi va boshqariladigan qoidalar

| Bosqich | Tekshiruv | Natija |
|---------|-----------|--------|
| **1. Tezkor baza** | foydalanuvchi `BlacklistUser` da yoki xabar xeshi `SpamContent` da | Darhol: o'chirish + ban |
| **2. Regex / Emoji** | dashboard'da boshqariladigan qoidalar (kalit so'z / emoji / regex) | Shubhali → 3-bosqichga |
| **3. ChatGPT 4o-mini** | matn + bio + profil rasmi yuboriladi, system-prompt: *faqat `SPAM_BOT` yoki `SAFE`* | `SPAM_BOT` → ban + qora ro'yxat + xesh keshlash |

**2-bosqich qoidalari Dashboard'dan boshqariladi** (`Filter Rules` sahifasi):
kalit so'z, emoji yoki regex qoidalarini qo'shish/o'chirish/yoqib-o'chirish mumkin.
Qoidalar `FilterRule` jadvalida saqlanadi; engine ularni qisqa **TTL kesh** (15s)
bilan o'qiydi, shuning uchun o'zgarishlar web va engine jarayonlari o'rtasida bir
necha soniyada qo'llanadi. Regex qoidalar saqlashdan oldin tekshiriladi (buzuq
pattern engine'ga tushmaydi). Jadval bo'sh bo'lsa, ichki standart qoidalar ishlaydi
(ular `migrate` paytida seed qilinadi).

AI "SPAM_BOT" deganda: foydalanuvchi ID `BlacklistUser` ga, yuborilgan
matn/GIF/rasm xeshi `SpamContent` ga yoziladi — keyingi safar bu kontent
**AI'siz**, 1-bosqichda darhol bloklanadi.

---

## 8. Dashboard sahifalari

- **Dashboard** — jami guruhlar, faol agentlar, bugun bloklangan botlar; Chart.js
  gibrid grafiklari (bar + line + doughnut).
- **Agents** — userbotlar ro'yxati, statusi va monitoring qilinayotgan guruhlari.
- **Security Logs** — har 2 soniyada yangilanuvchi real-time jadval: qaysi
  guruhda, qaysi agent tomonidan, kim, qaysi bosqichda bloklangani.
- **Filter Rules** — 2-bosqich kalit so'z / emoji / regex qoidalarini boshqarish.
- **Propagation** — guruhga tarqatish ishlarini yaratish va real-time kuzatish.
- **Add Userbot** — jonli QR-kod login ekrani (2FA parol bosqichi bilan).

---

## 9. Production eslatmalari

- Bazani **PostgreSQL** ga o'zgartiring (`DATABASES`), `DEBUG=False`.
- `run_agents` ni systemd/supervisor ostida ishga tushiring; web va worker
  alohida jarayonlar bo'lsin. Propagation ishlari faqat engine ishlаб turganda
  bajariladi (admin bot `active` bo'lishi shart).
- Telegram **2FA (cloud parol)** qo'llab-quvvatlanadi (QR oqimidagi parol bosqichi).
- Userbotlardan foydalanish Telegram ToS doirasida, faqat o'zingiz egasi/admin
  bo'lgan guruhlarni himoya qilish uchun amalga oshirilsin.
