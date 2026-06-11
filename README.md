# LoFi Studio: повне розгортання на Ubuntu

Це покрокова інструкція для встановлення LoFi Studio на чистий хмарний
сервер. Вона розрахована на Ubuntu 24.04 LTS, але також підійде для
Ubuntu 22.04 LTS.

Після встановлення працюватимуть:

- захищена вебпанель;
- безперервний YouTube/RTMP-стрім через FFmpeg;
- завантаження музики, відеофонів і анімацій;
- розклад денних і нічних відеофонів;
- текст, дата, час, локація і погода поверх відео;
- Telegram-бот для запуску, зупинки й перевірки стріму;
- Nginx і безкоштовний SSL-сертифікат Let's Encrypt.

## 1. Що потрібно підготувати

Перед початком потрібні:

1. VPS або виділений сервер з Ubuntu 24.04.
2. Мінімум 2 CPU, 4 GB RAM і 30 GB SSD. Для 1080p краще 4 CPU.
3. Публічна IPv4-адреса.
4. Домен або піддомен, наприклад `studio.example.com`.
5. SSH-доступ до сервера і користувач із правами `sudo`.
6. RTMP/RTMPS-адреса і ключ YouTube-трансляції.

У DNS-панелі домену створіть запис:

```text
Тип: A
Ім'я: studio
Значення: IP_АДРЕСА_СЕРВЕРА
TTL: Auto
```

Для кореневого домену замість `studio` зазвичай вказують `@`.
Дочекайтеся оновлення DNS. Перевірити можна на своєму комп'ютері:

```bash
nslookup studio.example.com
```

Команда повинна показати IP вашого сервера.

## 2. Підключення до сервера

На Windows відкрийте PowerShell, на macOS/Linux відкрийте Terminal:

```bash
ssh YOUR_USER@SERVER_IP
```

Замініть `YOUR_USER` та `SERVER_IP` своїми значеннями.

## 3. Оновлення Ubuntu

```bash
sudo apt update
sudo apt upgrade -y
sudo reboot
```

Після перезавантаження знову підключіться через SSH.

## 4. Встановлення програм

```bash
sudo apt install -y ffmpeg nginx python3 python3-venv python3-pip rsync ufw snapd
```

Перевірте FFmpeg:

```bash
ffmpeg -version
```

## 5. Створення окремого користувача

Застосунок не повинен працювати від `root`.

```bash
sudo useradd --create-home --shell /bin/bash lofi
sudo mkdir -p /home/lofi/app
sudo chown -R lofi:lofi /home/lofi
```

У результаті весь застосунок знаходитиметься тут:

```text
/home/lofi/app
```

## 6. Завантаження проєкту

### Варіант A: проєкт є у Git

```bash
sudo -u lofi git clone YOUR_REPOSITORY_URL /home/lofi/app
```

Якщо команда `git` відсутня:

```bash
sudo apt install -y git
```

### Варіант B: файли знаходяться на вашому комп'ютері

Виконайте на своєму комп'ютері з папки, яка містить проєкт:

```bash
scp -r ./lofi YOUR_USER@SERVER_IP:/tmp/lofi
```

Потім на сервері:

```bash
sudo rsync -a --delete /tmp/lofi/ /home/lofi/app/
sudo chown -R lofi:lofi /home/lofi/app
rm -rf /tmp/lofi
```

Після копіювання структура повинна виглядати так:

```text
/home/lofi/app/
├── bot/
│   └── bot.py
├── deploy/
│   ├── lofi-bot.service
│   ├── lofi-web.service
│   ├── lofi-worker.service
│   └── nginx.conf
├── web/
│   ├── static/
│   ├── templates/
│   ├── app.py
│   └── db.py
├── .env.example
├── README.md
├── requirements.txt
└── worker.py
```

## 7. Робочі каталоги

Великі медіафайли не потрібно додавати до Git. Створіть каталоги:

```bash
sudo -u lofi mkdir -p \
  /home/lofi/app/data \
  /home/lofi/app/music \
  /home/lofi/app/video \
  /home/lofi/app/overlays
```

Призначення:

```text
data/       SQLite-база, стан worker і службові файли
music/      завантажена музика
video/      основні відеофони
overlays/   анімації "підпишись", логотипи та інші відеошари
```

Надалі файли можна завантажувати через вебпанель.

## 8. Python-середовище

```bash
cd /home/lofi/app
sudo -u lofi python3 -m venv .venv
sudo -u lofi /home/lofi/app/.venv/bin/pip install --upgrade pip
sudo -u lofi /home/lofi/app/.venv/bin/pip install -r requirements.txt
```

## 9. Налаштування `.env`

Створіть конфігурацію:

```bash
sudo -u lofi cp /home/lofi/app/.env.example /home/lofi/app/.env
sudo nano /home/lofi/app/.env
```

Приклад:

```dotenv
LOFI_BASE=/home/lofi/app
FLASK_SECRET=ДУЖЕ_ДОВГИЙ_ВИПАДКОВИЙ_РЯДОК
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=ДУЖЕ_НАДІЙНИЙ_ПАРОЛЬ_МІНІМУМ_12_СИМВОЛІВ
COOKIE_SECURE=1
MAX_UPLOAD_MB=2048
PUBLIC_URL=https://studio.example.com

SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=mailer@example.com
SMTP_PASSWORD=SMTP_PASSWORD
SMTP_FROM=mailer@example.com

YT_URL=rtmps://a.rtmps.youtube.com/live2
YT_KEY=YOUR_YOUTUBE_STREAM_KEY

TG_TOKEN=
TG_USER_ID=
```

Згенерувати `FLASK_SECRET` можна командою:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

Пояснення:

- `ADMIN_EMAIL` і `ADMIN_PASSWORD` створюють першого адміністратора.
- `PUBLIC_URL` повинен містити ваш справжній HTTPS-домен.
- SMTP потрібен лише для відновлення пароля поштою.
- YouTube і Telegram можна налаштувати пізніше через панель.
- Не додавайте `.env` до Git і нікому не надсилайте його.

Захистіть файл:

```bash
sudo chown lofi:lofi /home/lofi/app/.env
sudo chmod 600 /home/lofi/app/.env
```

## 10. Встановлення systemd-сервісів

```bash
sudo cp /home/lofi/app/deploy/lofi-web.service /etc/systemd/system/
sudo cp /home/lofi/app/deploy/lofi-worker.service /etc/systemd/system/
sudo cp /home/lofi/app/deploy/lofi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable lofi-web lofi-worker lofi-bot
sudo systemctl start lofi-web lofi-worker lofi-bot
```

Перевірте:

```bash
sudo systemctl status lofi-web --no-pager
sudo systemctl status lofi-worker --no-pager
sudo systemctl status lofi-bot --no-pager
```

Статус `active (running)` означає, що сервіс працює.

## 11. Налаштування Nginx

Спочатку замініть домен у шаблоні:

```bash
sudo cp /home/lofi/app/deploy/nginx.conf /etc/nginx/sites-available/lofi
sudo nano /etc/nginx/sites-available/lofi
```

Змініть:

```nginx
server_name studio.example.com;
```

на свій домен. Потім увімкніть сайт:

```bash
sudo ln -s /etc/nginx/sites-available/lofi /etc/nginx/sites-enabled/lofi
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl reload nginx
```

Команда `nginx -t` повинна показати:

```text
syntax is ok
test is successful
```

Тепер перевірте в браузері:

```text
http://studio.example.com
```

До встановлення сертифіката це буде звичайний HTTP.

## 12. Firewall

Спочатку обов'язково дозвольте SSH, щоб не заблокувати собі сервер:

```bash
sudo ufw allow OpenSSH
sudo ufw allow "Nginx Full"
sudo ufw enable
sudo ufw status
```

Повинні бути відкриті:

```text
22/tcp   SSH
80/tcp   HTTP і ACME-перевірка
443/tcp  HTTPS
```

Якщо хмарний провайдер має окремий firewall або Security Group, відкрийте
там ті самі порти.

## 13. SSL через ACME і Let's Encrypt

Для HTTP-01 перевірки домен уже повинен вести на сервер, Nginx повинен
відповідати на HTTP, а порт `80` має бути доступний з інтернету.

Встановіть офіційний Certbot через Snap:

```bash
sudo apt remove -y certbot
sudo snap install core
sudo snap refresh core
sudo snap install --classic certbot
sudo ln -sf /snap/bin/certbot /usr/local/bin/certbot
```

Випустіть сертифікат:

```bash
sudo certbot --nginx -d studio.example.com
```

Certbot попросить:

1. Ввести email для повідомлень.
2. Прийняти умови Let's Encrypt.
3. За бажанням погодитися або відмовитися від розсилки.

Certbot автоматично:

- виконає ACME-перевірку домену;
- отримає сертифікат;
- додасть HTTPS до Nginx;
- налаштує перенаправлення HTTP на HTTPS.

Перевірте:

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo certbot certificates
sudo certbot renew --dry-run
```

`renew --dry-run` перевіряє автоматичне поновлення. Сертифікати
Let's Encrypt надалі поновлюються таймером Certbot.

Відкрийте:

```text
https://studio.example.com
```

У браузері має з'явитися значок захищеного з'єднання.

## 14. Перший вхід

Увійдіть за `ADMIN_EMAIL` і `ADMIN_PASSWORD` із `.env`.

Після успішного першого входу видаліть початковий пароль із `.env`:

```bash
sudo nano /home/lofi/app/.env
```

Видаліть рядок `ADMIN_PASSWORD=...`, збережіть файл і виконайте:

```bash
sudo systemctl restart lofi-web lofi-worker lofi-bot
```

Хеш пароля вже зберігається в `/home/lofi/app/data/lofi.db`.

## 15. Початкове налаштування панелі

Рекомендований порядок:

1. Відкрийте розділ трансляції та введіть повну RTMPS-адресу.
2. Завантажте хоча б один музичний файл.
3. Завантажте хоча б один відеофон або додайте RTSP/HTTP-джерело.
4. За потреби створіть денний і нічний розклад.
5. Додайте текст, дату, погоду або анімацію.
6. Натисніть `Запустити`.

Без музики, відеоджерела або RTMP-адреси worker покаже помилку й не
запустить FFmpeg.

## 16. Telegram-бот

1. Відкрийте Telegram і знайдіть `@BotFather`.
2. Виконайте `/newbot`.
3. Скопіюйте отриманий token.
4. Дізнайтеся свій числовий Telegram user ID через надійного ID-бота
   або Telegram API.
5. У панелі введіть token і user ID.
6. Увімкніть Telegram-керування та збережіть.
7. Відкрийте свого бота й натисніть `/start`.

Бот приймає команди лише від указаного user ID.

## 17. Де зберігаються дані

```text
/home/lofi/app/.env                 секрети й початкові параметри
/home/lofi/app/data/lofi.db        користувачі, плейлист і налаштування
/home/lofi/app/data/worker-status.json
/home/lofi/app/music/               музика
/home/lofi/app/video/               відеофони
/home/lofi/app/overlays/            анімації поверх відео
/etc/systemd/system/lofi-*.service  системні сервіси
/etc/nginx/sites-available/lofi     конфігурація Nginx
/etc/letsencrypt/                   SSL-сертифікати
```

## 18. Перегляд журналів

Вебпанель:

```bash
sudo journalctl -u lofi-web -f
```

FFmpeg worker:

```bash
sudo journalctl -u lofi-worker -f
```

Telegram:

```bash
sudo journalctl -u lofi-bot -f
```

Nginx:

```bash
sudo tail -f /var/log/nginx/error.log
```

Вихід із перегляду журналу: `Ctrl+C`.

## 19. Перезапуск

Після зміни коду:

```bash
sudo chown -R lofi:lofi /home/lofi/app
sudo systemctl restart lofi-web lofi-worker lofi-bot
sudo systemctl reload nginx
```

Перезапуск усього сервера:

```bash
sudo reboot
```

Усі сервіси запустяться автоматично.

## 20. Оновлення проєкту

Якщо використовується Git:

```bash
cd /home/lofi/app
sudo -u lofi git pull
sudo -u lofi /home/lofi/app/.venv/bin/pip install -r requirements.txt
sudo cp deploy/lofi-web.service deploy/lofi-worker.service deploy/lofi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart lofi-web lofi-worker lofi-bot
sudo nginx -t
sudo systemctl reload nginx
```

Перед оновленням зробіть резервну копію `data/`, `.env`, `music/`,
`video/` та `overlays/`.

## 21. Резервна копія

```bash
sudo systemctl stop lofi-web lofi-worker lofi-bot
sudo tar -czf /tmp/lofi-backup-$(date +%F).tar.gz \
  /home/lofi/app/.env \
  /home/lofi/app/data \
  /home/lofi/app/music \
  /home/lofi/app/video \
  /home/lofi/app/overlays
sudo systemctl start lofi-web lofi-worker lofi-bot
```

Скопіюйте архів із сервера на свій комп'ютер:

```bash
scp YOUR_USER@SERVER_IP:/tmp/lofi-backup-DATE.tar.gz .
```

## 22. Типові проблеми

### Домен не відкривається

```bash
nslookup studio.example.com
sudo ufw status
sudo systemctl status nginx --no-pager
```

Переконайтеся, що DNS показує правильний IP, а порти 80/443 відкриті.

### Certbot не може підтвердити домен

- домен ще не оновився в DNS;
- порт 80 закритий у UFW або firewall провайдера;
- `server_name` у Nginx не відповідає домену;
- Nginx не запущений.

Перевірте:

```bash
sudo nginx -t
curl -I http://studio.example.com
```

### Панель показує 502 Bad Gateway

```bash
sudo systemctl status lofi-web --no-pager
sudo journalctl -u lofi-web -n 100 --no-pager
```

### Стрім не запускається

```bash
sudo journalctl -u lofi-worker -n 200 --no-pager
ffmpeg -version
```

Перевірте музику, відеоджерело, RTMP-адресу та вільне місце на диску:

```bash
df -h
```

### Відновлення пароля не надсилає лист

Перевірте параметри `SMTP_*` у `.env` і журнал:

```bash
sudo journalctl -u lofi-web -n 100 --no-pager
```

## 23. Важливі правила безпеки

- Не запускайте застосунок від `root`.
- Не відкривайте порти `5000` або `8000` в інтернет.
- Не публікуйте `.env`, SQLite-базу, RTMP і Telegram-ключі.
- Використовуйте пароль щонайменше із 12-16 символів.
- Залишайте `COOKIE_SECURE=1` на production-сервері.
- Регулярно встановлюйте оновлення Ubuntu.
- Регулярно створюйте резервні копії.

## Офіційні довідки

- Certbot для Nginx: https://certbot.eff.org/instructions?ws=nginx&os=snap
- Let's Encrypt HTTP-01: https://letsencrypt.org/docs/challenge-types/
- Nginx reverse proxy: https://nginx.org/en/docs/http/ngx_http_proxy_module.html
