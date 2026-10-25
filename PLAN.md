# Scrap Mechanic Dedicated Server — план

## 0. Статус

Реализовано и проверено локально (Docker Desktop, Linux x86_64).

- Локальный проект: `/home/alex/Documents/Projects/SM-dedicated-server`
- Деплой: `<remote-host>:/opt/docker/gameservers/scrap-mechanic`
- Игра: Scrap Mechanic, Steam App ID `387990`
- Хост: Linux **x86_64 без GPU**, только Docker
- Рендер: **lavapipe + Xvfb**, без `/dev/dri` и GPU-runtime
- Steam Guard: **выключен**, Guard-код не предусмотрен
- Запуск: только через Steam, с `-open`, не прямым вызовом Proton

## 1. Зафиксированные решения

1. Готового поддерживаемого Docker-образа под Scrap Mechanic нет. Собираем свой.
2. Steam обязателен: Steam-клиент запускается внутри контейнера под Xvfb.
3. Окно обязательно: используем Xvfb. Окно создаётся, но физического дисплея нет.
4. `-use_null_driver` под DXVK не работает. Не используем.
5. GPU не используем: Steam Runtime пробрасывает lavapipe ICD внутрь pressure-vessel; DXVK 3.1 выбирает `llvmpipe`.
6. Steam Guard выключен. Логин — по логину/паролю из `.env`. Guard-код и интерактивный вход не предусмотрены. Если Steam всё же запросит Guard или иное подтверждение — это вне рамок, контейнер не стартует.
7. Steam Cloud не используем и не полагаемся на него.
8. `SAVE_NAME` и `GAME_MODE` (`custom|survival|creative`) задаются в `.env`.
9. Текущий сейв: `something-test`, обычный мир. Ограничение terrain data 256×256 для этого мира не рассматриваем.
10. Запуск игры — через Steam с `-open "<windows path>"`, а не `-last_save`.
11. Бэкапы — отдельный compose-volume, только у control-plane.
12. Сейв кладётся в выделенный volume. Перед первым запуском файл должен быть **инициализированным** SQLite-сейвом, а не пустым файлом.
13. Документация минимальная: один README + `.env.example` + OpenAPI control-plane.

## 2. Архитектура

### 2.1 Контейнеры

`sm-game`
- Ubuntu 24.04 + Steam-клиент + GE-Proton + Xvfb + Mesa 25/lavapipe + openbox + DXVK 3.1.
- Запускает Steam, логинится, затем игру через `steam -applaunch`.
- Не содержит GPU-устройств, не требует `/dev/dri`.
- Работает с `network_mode: host`, чтобы не ломать Steam-сеть.
- Имеет `stop_grace_period`, healthcheck и supervisor/entrypoint.

`sm-control`
- REST API + Prometheus `/metrics`.
- Читает сейвы и логи, делает бэкапы/restore.
- Хранит метаданные в отдельной SQLite-базе.
- Не имеет доступа к Steam-кредам.
- Не монтирует Docker socket в MVP.
- Слушает `127.0.0.1:8080`; снаружи — только reverse proxy/SSH-туннель.

### 2.2 Volumes

```yaml
volumes:
  sm_steam:    # Steam-клиент, файлы игры, Proton-префикс, сессия
  sm_saves:    # .../Scrap Mechanic/User, все User_*
  sm_seed:     # исходный инициализированный something-test.db, read-only
  sm_logs:     # .../Scrap Mechanic/Logs
  sm_backups:  # только sm-control
  sm_control:  # метаданные control-plane
```

Монтирование:

```yaml
services:
  sm-game:
    volumes:
      - sm_steam:/home/steam/.local/share/Steam
      - sm_saves:/home/steam/.local/share/Steam/steamapps/compatdata/387990/pfx/drive_c/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User
      - sm_seed:/seed:ro
      - sm_logs:/home/steam/.local/share/Steam/steamapps/common/Scrap Mechanic/Logs

  sm-control:
    volumes:
      - sm_saves:/saves
      - sm_logs:/logs:ro
      - sm_backups:/backups
      - sm_control:/var/lib/sm-control
```

Правила:

- `sm_backups` не монтируется в `sm-game`.
- `sm_saves` монтируется в control-plane read-write, но запись разрешена только при остановленном сервере.
- `sm_logs` — read-only.
- `sm_seed` — read-only, только для первичного копирования сейва.
- В game-контейнере нет `/dev/dri`.

### 2.3 Пути внутри контейнера

```text
Steam:        /home/steam/.local/share/Steam
Prefix:       .../steamapps/compatdata/387990/pfx
User dir:     .../pfx/drive_c/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User
Saves:        .../User/User_<steamid>/Save
Custom saves: .../User/User_<steamid>/Save/Custom
Logs:         .../steamapps/common/Scrap Mechanic/Logs
```

Control-plane видит:

```text
/saves/User_*/Save/<SAVE_NAME>.db
/saves/User_*/Save/Custom/<SAVE_NAME>.db
/logs/game-*.log
/backups
```

## 3. Steam-логин

### 3.1 Креды

Источник: `/home/alex/Documents/SMServer_creds.txt`.

В файле только:

```text
SERVER_STEAM_USER_LOGIN
SERVER_STEAM_USER_PASSWORD
```

Guard-кода нет. Файл сейчас `644`; рекомендуется `600`.

В `.env` переносим те же имена:

```dotenv
SERVER_STEAM_USER_LOGIN=...
SERVER_STEAM_USER_PASSWORD=...
```

Требования:

- `.env` не коммитить;
- `.env` не копировать в Docker-образ;
- не передавать креды через build args;
- `.dockerignore` исключает `.env`, `*.creds`, `SMServer_creds.txt`, `data/`, `backups/`;
- `.env` на сервере — `chmod 600`;
- Steam-сессия хранится в `sm_steam`, не в образе;
- `SM_API_TOKEN` генерируется отдельно.

### 3.2 Вход

- Steam Guard должен быть выключен. Это обязательное условие.
- Вход выполняется через Steam-клиент под Xvfb с логином/паролем из `.env`.
- Интерактивный вход, Guard-код и дополнительные подтверждения не предусмотрены.
- Если Steam запросит Guard или иное подтверждение — контейнер завершается с понятной ошибкой; это вне рамок.
- После успешного входа сессия сохраняется в `sm_steam`, последующие запуски используют её.

## 4. Сейв и первичное заполнение volume

### 4.1 Параметры

```dotenv
SAVE_NAME=something-test
GAME_MODE=survival
```

Правило:

```text
GAME_MODE=survival → /saves/User_*/Save/Survival/<SAVE_NAME>.db
GAME_MODE=custom → /saves/User_*/Save/Custom/<SAVE_NAME>.db
GAME_MODE=creative → /saves/User_*/Save/<SAVE_NAME>.db
```

- Если `SAVE_NAME` уже содержит `.db`, второй раз не добавляем.
- Если `User_*` несколько, выбираем тот, где лежит файл; при неоднозначности — ошибка.
- `Survival/` пока не поддерживаем. При необходимости добавим `SAVE_SUBDIR`.

### 4.2 Требование к сейву

**Важно:** в `sm_seed`/`sm_saves` должен попасть уже инициализированный сейв Scrap Mechanic, а не пустой файл и не заглушка.

Минимальные проверки:

- файл существует;
- это SQLite 3;
- `PRAGMA quick_check` возвращает `ok`;
- есть таблица `Game`;
- читается `Game.gametick`/`Game.savegameversion`;
- размер больше нуля.

Если проверка не проходит — контейнер не запускает игру и завершается с ошибкой.

### 4.3 Первичное копирование

Entrypoint `sm-game` после логина Steam:

1. Определяет SteamID:
   - из `config/loginusers.vdf`, или
   - из существующего `User_*`, или
   - из опционального `STEAM_ID` в `.env`.
2. Формирует `USER_DIR=.../User/User_<steamid>`.
3. Формирует target:
   - `USER_DIR/Save/Survival/<SAVE_NAME>.db`, если `GAME_MODE=survival`;
   - `USER_DIR/Save/Custom/<SAVE_NAME>.db`, если `GAME_MODE=custom`;
   - `USER_DIR/Save/<SAVE_NAME>.db`, если `GAME_MODE=creative`.
4. Если target отсутствует:
   - копирует `/seed/<SAVE_NAME>.db` в target;
   - проверяет целостность;
   - выставляет права.
5. Если target уже есть:
   - не перезаписывает;
   - проверяет целостность; при ошибке — fail.
6. Если target отсутствует и seed отсутствует/невалиден — fail, не создавать новый мир.

Ручной вариант заполнения (документировать):

```bash
# положить инициализированный something-test.db в seed-volume
docker run --rm \
  -v sm_seed:/seed \
  -v "$PWD/something-test.db":/src/something-test.db:ro \
  alpine cp /src/something-test.db /seed/
```

Для удобства будет `scripts/seed-save.sh`.

## 5. Запуск игры

Основной вариант:

```bash
steam -applaunch 387990 \
  -dedicated_server \
  -console \
  -open "C:/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User/User_<steamid>/Save/something-test.db"
```

Требования:

- Steam-клиент уже запущен и залогинен.
- `DISPLAY=:99` (Xvfb).
- Путь для `-open` — Windows-путь внутри Proton-префикса. При необходимости конвертировать через `winepath -w`.
- Для custom-сейва путь указывает на `Save/Custom/<SAVE_NAME>.db`.
- `-last_save` не используем.
- После запуска control-plane парсит лог и проверяет, что активный сейв совпадает с `SAVE_NAME`.
- Если совпадения нет — статус `error`, бэкапы блокируются.

Остановка:

- `docker compose stop` с `stop_grace_period`.
- Внутри — попытка корректного закрытия окна/процесса, затем fallback.
- Консольные команды не считаем надёжным способом: в текущем логе `-console` пишет `Unable to allocate debug console`.
- Механизм graceful stop нужно проверить на практике.

## 6. Control-plane

### 6.1 Стек

По умолчанию: Python + FastAPI + `prometheus_client` + стандартный `sqlite3` + `psutil`.

Причины: быстро, хорошо для SQLite/HTTP/метрик, автоматический OpenAPI.

### 6.2 API

- `GET /healthz`
- `GET /readyz`
- `GET /api/v1/status`
- `GET /api/v1/players`
- `GET /api/v1/logs?tail=...`
- `GET /api/v1/logs/stream` (SSE)
- `GET /api/v1/backups`
- `POST /api/v1/backups`
- `GET /api/v1/backups/{id}`
- `GET /api/v1/backups/{id}/download`
- `POST /api/v1/backups/{id}/restore`
- `DELETE /api/v1/backups/{id}`
- `GET /api/v1/config`
- `PATCH /api/v1/config`
- `GET /api/v1/operations/{id}`
- `GET /metrics`

Lifecycle (`start/stop/restart/update`) — следующий этап. В MVP control-plane не монтирует Docker socket; при необходимости вернёт `409`/`501` с понятным сообщением.

### 6.3 Метрики

- `sm_server_up`
- `sm_server_state{state=...}`
- `sm_uptime_seconds`
- `sm_players_online`
- `sm_game_info{version=...}`
- `sm_save_size_bytes`
- `sm_save_gametick`
- `sm_savegameversion`
- `sm_save_match`
- `sm_backup_last_success_timestamp_seconds`
- `sm_backup_duration_seconds`
- `sm_backup_size_bytes`
- `sm_backup_failures_total`
- `sm_backup_total`
- `sm_restore_total`
- `sm_log_errors_total`
- `sm_log_warnings_total`
- `sm_steam_update_available` (позже)
- `sm_control_build_info`

CPU/RAM контейнера — позже через cAdvisor/Docker API, чтобы не дублировать.

### 6.4 Бэкапы

- SQLite Online Backup API, не `cp` живого `.db`.
- `busy_timeout`, повторные попытки.
- После копии: `PRAGMA integrity_check`, `select count(*) from Game`, чтение `gametick`.
- Манифест: `timestamp`, `save_name`, `game_mode`, `size`, `sha256`, `gametick`, `savegameversion`, `trigger`, `duration`.
- Атомарная запись: temp + fsync + rename.
- Lock: одновременно только один backup/restore.
- Расписание: онлайн каждые 15 минут при изменениях/игроках; daily full; pre-update/pre-restore.
- Retention: например, 7 daily / 4 weekly; последний бэкап не удалять.
- Бэкапить только активный мир; другие миры — опционально.
- Моды: манифест `Game.mods`; при необходимости — workshop-каталог.
- Бэкапы пишутся только в `sm_backups`.

### 6.5 Restore

- Требует остановленного сервера.
- Перед restore — safety backup.
- Проверка checksum и SQLite integrity.
- Атомарная замена target-файла.
- Восстановление прав.
- Запуск сервера и проверка лога.
- Асинхронная операция с `operation_id`.

### 6.6 Парсер логов

Из текущих логов можно получить:

- `Game version 1.0.6.889`;
- `Fetching game info for: '.../<save>.db'`;
- `Opening save file: '...'`;
- `Server started`;
- `Connection Status Changed` + `Connection handle` + `State: ... -> ...`;
- `ERROR:` / `WARNING:`.

План:

- следить за новым `Logs/game-*.log`;
- при ротации начинать с начала нового файла и сбрасывать состояние;
- определять активный сейв и сравнивать с `SAVE_NAME`;
- считать игроков по переходам в `Connected`/`None`;
- исключать SteamID серверного аккаунта из player count;
- считать ошибки/предупреждения для метрик.

### 6.7 Безопасность

- Bearer token (`SM_API_TOKEN`).
- Bind `127.0.0.1`.
- Снаружи — reverse proxy/TLS или SSH-туннель.
- Не логировать пароли/токены.
- Валидация path traversal, backup id, restore target.
- Restore только для известного `SAVE_NAME` и известного volume.
- Бэкапы можно шифровать/выгружать через restic/rclone позже.
- Docker socket в control-plane не монтировать в MVP.

## 7. Compose (эскиз)

```yaml
services:
  sm-game:
    build:
      context: ./docker/game
      target: runtime
    env_file: .env
    network_mode: host
    shm_size: "1gb"
    stop_grace_period: 60s
    restart: unless-stopped
    volumes:
      - sm_steam:/home/steam/.local/share/Steam
      - sm_saves:/home/steam/.local/share/Steam/steamapps/compatdata/387990/pfx/drive_c/users/steamuser/AppData/Roaming/Axolot Games/Scrap Mechanic/User
      - sm_seed:/seed:ro
      - sm_logs:/home/steam/.local/share/Steam/steamapps/common/Scrap Mechanic/Logs
    # никаких /dev/dri и GPU-runtime

  sm-control:
    build:
      context: ./control
    env_file: .env
    restart: unless-stopped
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - sm_saves:/saves
      - sm_logs:/logs:ro
      - sm_backups:/backups
      - sm_control:/var/lib/sm-control

volumes:
  sm_steam:
  sm_saves:
  sm_seed:
  sm_logs:
  sm_backups:
  sm_control:
```

## 8. Переменные `.env`

```dotenv
# Steam
SERVER_STEAM_USER_LOGIN=...
SERVER_STEAM_USER_PASSWORD=...

# Save
SAVE_NAME=something-test
GAME_MODE=survival

# API
SM_API_TOKEN=change-me
SM_API_PORT=8080

# Runtime
TZ=UTC
PUID=1000
PGID=1000
XVFB_RESOLUTION=1280x720x24
DXVK_FPS_LIMIT=30
LP_NUM_THREADS=4

# Backups
BACKUP_INTERVAL_MINUTES=15
BACKUP_RETENTION_DAYS=7
BACKUP_RETENTION_WEEKS=4
```

Пути `/saves`, `/logs`, `/backups`, `/var/lib/sm-control` — контейнерные. Для локального запуска control-plane без Docker они переопределяются.

## 9. Структура репозитория

```text
SM-dedicated-server/
  compose.yaml
  .env.example
  .gitignore
  .dockerignore
  README.md
  PLAN.md
  control/
    Dockerfile
    pyproject.toml
    app/
      main.py
      config.py
      saves.py
      backups.py
      logs.py
      metrics.py
      api.py
    tests/
  docker/
    game/
      Dockerfile
      entrypoint.sh
      supervisord.conf
      dxvk.conf
  scripts/
    import-creds.sh
    seed-save.sh
    deploy.sh
```

## 10. Деплой

- Локально: `/home/alex/Documents/Projects/SM-dedicated-server`
- Remote: `<remote-host>:/opt/docker/gameservers/scrap-mechanic`
- Предварительно: x86_64, Docker + Compose, без GPU.
- `.env` создаётся на remote из локального `SMServer_creds.txt` или копируется по SSH; права `600`.
- `sm_seed` заполняется инициализированным `something-test.db`.
- `scripts/deploy.sh`:
  - `rsync` репозитория на remote;
  - проверка `.env`;
  - `docker compose build`;
  - `docker compose up -d`;
  - smoke-check `/healthz`, `/metrics`, лог игры.
- Деплой выполняется только после локальной проверки control-plane и game-образа.

## 11. Документация (минимум)

`README.md`:

- что это и ограничения (нет официального dedicated server, Steam обязателен, Guard должен быть выключен, CPU-рендер);
- `.env` и таблица переменных;
- volumes и где лежат сейвы/бэкапы;
- **важно:** сейв в volume должен быть инициализированным SQLite-сейвом; пустой файл/заглушка не подходят;
- первый запуск, логин, seed, проверка;
- команды `docker compose`;
- API endpoints и примеры `curl`;
- backup/restore;
- деплой на `<remote-host>`;
- troubleshooting: Steam login, lavapipe, Xvfb, `-open`, пустой сейв, несовпадение активного сейва;
- Steam Cloud не используем.

OpenAPI — автоматически у control-plane (`/docs`, `/openapi.json`).

## 12. Этапы

1. **Репозиторий + документация-скелет**
   - `compose.yaml`, `.env.example`, `.gitignore`, `.dockerignore`, `README.md`, `PLAN.md`.
2. **Control-plane MVP**
   - конфиг, резолвер сейва, SQLite backup/restore, API, метрики, тесты на копии.
3. **Game image**
   - Debian + Steam + GE-Proton + Xvfb + lavapipe + openbox;
   - entrypoint: Steam login, seed save, `steam -applaunch ... -open ...`;
   - проверка без `/dev/dri`, `vulkaninfo` → llvmpipe, отсутствие GPU-нагрузки.
4. **Compose-интеграция**
   - volumes, healthcheck, control-plane в контейнере, backup/restore.
5. **Деплой на `<remote-host>`**
   - `.env`, seed, первый запуск, smoke-test, бэкап/restore.
6. **Эксплуатация**
   - обновления/rollback, алерты, панель, при необходимости — lifecycle через Docker API.

## 13. Критерии готовности

- Контейнер запускается на x86_64 без GPU и без `/dev/dri`.
- `vulkaninfo`/DXVK используют llvmpipe/lavapipe; GPU не задействован.
- Steam-логин проходит при выключенном Guard.
- Игра запускается через `steam -applaunch 387990 -dedicated_server -console -open "<path>"`.
- В логе активный сейв — `something-test.db`.
- `sm_saves` изначально содержит валидный инициализированный сейв; новый мир не создаётся.
- Онлайн-бэкап SQLite проходит `integrity_check`.
- Restore на копию/стенд работает.
- `/metrics` и REST API доступны с токеном.
- Бэкапы сохраняются в `sm_backups` и переживают перезапуск контейнеров.
- README содержит предупреждения про Guard, seed и volumes.

## 14. Риски и ограничения

- Steam-клиент может потребовать интерактивный вход даже при выключенном Guard — проверить.
- `-open` с Windows-путём и пробелами — проверить quoting/`winepath`.
- Lavapipe может дать высокую CPU-нагрузку — тюнить разрешение, FPS cap, `LP_NUM_THREADS`.
- Обновления игры/Proton могут ломать запуск — фиксировать версии, делать pre-update backup.
- Steam-сеть и host network — проверить на реальном клиенте.
- Graceful stop — проверить; fallback может не сохранить состояние.
- Docker socket для lifecycle — отдельный этап, не в MVP.
- `Survival/` не поддержан; только root/Custom.
- Моды/workshop — нужен манифест или отдельный volume.
- Секреты в `.env` видны в `docker inspect`; для MVP допустимо, для продакшена — secrets.

## 15. Открытые пункты

- Точное поведение `steam -login` без Guard.
- Способ graceful stop (Alt+F4 через xdotool / SIGTERM / wineserver).
- Формат seed-volume: named volume vs bind mount.
- Нужен ли Prometheus/Grafana в compose или только `/metrics`.
- Нужен ли lifecycle API в MVP или позже.
- Поддержка `Survival/`.
- `TZ` по умолчанию.
