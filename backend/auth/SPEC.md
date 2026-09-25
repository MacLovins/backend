# backend/auth — ТЗ (`leadradar-auth`)

> **Роль:** пользователи, вход по email и паролю, JWT в httpOnly-cookie (и Bearer для API-клиентов), роли
> `admin` и `sales`, объект `Principal` для остальных модулей.
> **Владелец:** P1 (≈ 3 ч в окне H2–H8) · **Потребитель:** `backend/backend` (роутер + dependencies)
> **Связано:** [ARCHITECTURE.md](../../ARCHITECTURE.md) §4.3, §4.7 · [backend/SPEC.md](../SPEC.md)
> **Закрывает:** U11 · K4 (роли: продажник не видит лишнего) · K5 (безопасность) · K6 (заготовка под SSO и мультитенантность)

---

## 0. Как пользоваться этим файлом

Пакет маленький и самодостаточный. Остальные модули знают только `Principal`, `get_current_principal` и
`require_roles(...)`: ни модели пользователя, ни JWT они не видят. Поэтому на этапе 2 auth можно заменить внешним
IdP без правок в других модулях.

---

## 1. Этап 1 — MVP

### 1.1 Цель

Безопасный вход для двух ролей за минимальное время: без refresh-токенов, SSO и MFA, но по современным практикам
(Argon2, PyJWT, httpOnly-cookie, защита от timing-атак).

### 1.2 Границы

| Входит | Не входит (этап 2) |
|---|---|
| Модель пользователя (схема `auth`), логин, логаут, `me`, роли, CLI создания пользователя, управление пользователями (P1) | SSO (OIDC), MFA, refresh-токены, отзыв токенов, API-ключи, SCIM, аудит |
| Stateless-проверка JWT без обращения к БД на каждый запрос | Тонкие права (permissions), территории, команды |
| Одна организация (`org_id` в токене) | Переключение между организациями |

### 1.3 Функции

| ID | Функция | Пр. | Проверка |
|---|---|---|---|
| AU-01 | `AuthSettings` (префикс `AUTH_`): секрет, алгоритм, TTL, параметры cookie | P0 | `pytest -k settings` |
| AU-02 | Модель `auth.user_account` на своём `MetaData(schema="auth")`; миграция в общем Alembic core | P0 | `alembic upgrade head` |
| AU-03 | Хеширование: `pwdlib.PasswordHash.recommended()` (Argon2); `DUMMY_HASH` — проверка при несуществующем email против timing-атак | P0 | `pytest -k password` |
| AU-04 | JWT (PyJWT, HS256): claims `sub`, `org`, `role`, `email`, `name`, `iat`, `exp`, `iss`, `aud`, `jti` | P0 | `pytest -k jwt` (истёкший, чужая подпись, не тот aud) |
| AU-05 | `POST /auth/login` (JSON) → `Set-Cookie: lr_session` + `{user}`; `POST /auth/logout`; `GET /auth/me` | P0 | `pytest -k login` |
| AU-06 | `get_current_principal`: cookie, иначе `Authorization: Bearer` → `Principal` или 401 | P0 | `pytest -k principal` |
| AU-07 | `require_roles(*roles)` → 403 | P0 | `pytest -k roles` |
| AU-08 | CLI `lr-auth create-user --email --role --full-name` (пароль из stdin или env) + сервисная функция для сидов core | P0 | Ручной запуск |
| AU-09 | `POST /auth/token` (OAuth2 password form → Bearer) для Swagger и скриптов | P1 | `pytest -k token` |
| AU-10 | Admin API: `GET/POST /auth/users`, `PATCH /auth/users/{id}` (роль, активность, сброс пароля) | P1 | `pytest -k users_admin` |
| AU-11 | Лимит попыток входа: 10 за 5 мин на пару IP + email (в памяти процесса) → 429 | P1 | `pytest -k rate_limit` |

### 1.4 Входы и выходы

#### 1.4.1 Публичный API пакета

```python
from leadradar_auth import (
    AuthSettings, Role, Principal,
    create_auth_router,        # (get_session: Callable[[], AsyncIterator[AsyncSession]]) -> APIRouter
    get_current_principal,     # FastAPI dependency: -> Principal
    require_roles,             # (*roles: Role) -> dependency
    auth_metadata,             # MetaData(schema="auth") для Alembic core
    AuthService,               # create_user, authenticate, list_users, update_user
)

Role = Literal["admin", "sales"]

class Principal(BaseModel):
    user_id: UUID; org_id: UUID; email: EmailStr; role: Role; full_name: str | None = None
```

Подключение в core:

```python
app.include_router(create_auth_router(get_session=get_db_session), prefix="/api/v1")
api_v1 = APIRouter(prefix="/api/v1", dependencies=[Depends(get_current_principal)])
config_router = APIRouter(dependencies=[Depends(require_roles("admin"))])   # запись конфигурации
```

#### 1.4.2 HTTP

| Метод и путь | Вход | Выход | Ошибки |
|---|---|---|---|
| `POST /api/v1/auth/login` | `{"email", "password"}` | 200 `{"user": UserOut}` + cookie | 401 `invalid_credentials` (одно сообщение для любых причин), 429 (P1) |
| `POST /api/v1/auth/logout` | — | 204, cookie удалена | — |
| `GET /api/v1/auth/me` | cookie или Bearer | `UserOut` | 401 |
| `POST /api/v1/auth/token` (P1) | form `username`, `password` | `{"access_token", "token_type": "bearer"}` | 401 |
| `GET/POST /api/v1/auth/users`, `PATCH /api/v1/auth/users/{id}` (P1) | admin | `UserOut` | 403, 409 (email занят) |

`UserOut = {id, email, full_name, role, is_active, last_login_at}`.
Cookie: `lr_session=<jwt>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=<TTL>`. `Secure` выключается только
при `AUTH_COOKIE_SECURE=false` для локального http.

#### 1.4.3 Матрица прав MVP

| Действие | admin | sales |
|---|---|---|
| Смотреть лиды, компании, прогоны, качество | ✓ | ✓ |
| Запускать анализ, импорт, discovery, добавлять и редактировать компании | ✓ | ✓ |
| Оценивать сигналы и лиды, экспорт CSV | ✓ | ✓ |
| Менять услуги, вопросы, ICP, правила, профиль скоринга, применять пресеты | ✓ | — |
| Удалять компании, управлять пользователями | ✓ | — |

### 1.5 Зависимости

| Тип | Что |
|---|---|
| Python | `fastapi`, `pyjwt`, `pwdlib[argon2]`, `sqlalchemy[asyncio]`, `pydantic[email]`, `pydantic-settings`, `typer` |
| От других папок | Ничего. Сессию БД передаёт core через `create_auth_router(get_session=...)` |
| Для других папок | core — публичный API §1.4.1 |

```dotenv
AUTH_JWT_SECRET=            # openssl rand -hex 32; разный для dev и demo
AUTH_JWT_ALG=HS256
AUTH_ACCESS_TTL_MIN=720     # 12 ч на время хакатона
AUTH_COOKIE_NAME=lr_session
AUTH_COOKIE_SECURE=true     # false только для локального http
AUTH_ISSUER=leadradar
AUTH_AUDIENCE=leadradar-web
AUTH_DEFAULT_ORG_ID=        # единственная организация MVP (создаётся сидом core)
```

### 1.6 Структура папки

```
backend/auth/
├── SPEC.md
├── pyproject.toml              # name = "leadradar-auth"; scripts: lr-auth = "leadradar_auth.cli:app"
├── src/leadradar_auth/
│   ├── __init__.py             # публичный API §1.4.1
│   ├── settings.py             # AuthSettings
│   ├── models.py               # auth_metadata, UserAccount
│   ├── schemas.py              # LoginIn, UserOut, UserCreate, UserUpdate, Principal
│   ├── security.py             # PasswordHash.recommended(), DUMMY_HASH, encode/decode JWT
│   ├── repository.py · service.py
│   ├── dependencies.py         # get_current_principal, require_roles
│   ├── router.py               # create_auth_router(get_session)
│   ├── rate_limit.py           # P1
│   └── cli.py
└── tests/
```

### 1.7 Ключевые решения

- **Stateless-проверка.** `get_current_principal` проверяет подпись и срок JWT без запроса к БД. Цена — деактивированный
  пользователь работает до конца TTL (≤ 12 ч). Для хакатона это приемлемо, отзыв токенов — на этапе 2.
- **Почему cookie, а не localStorage.** Фронт и API — один origin (Caddy). Cookie `HttpOnly` недоступна JavaScript
  (защита от XSS-кражи токена). SSE работает без ручной передачи заголовков. От CSRF защищают `SameSite=Lax` и
  проверка `Origin` в core (P1).
- **Одинаковый ответ** на «нет пользователя» и «неверный пароль», проверка против `DUMMY_HASH` —
  защита от перебора email по времени ответа.
- **Email** нормализуется (trim, lowercase), уникальный индекс по `lower(email)`. Пароль ≥ 10 символов.
- Таблица `auth.user_account`: `id uuid pk`, `org_id uuid`, `email`, `password_hash`, `full_name`,
  `role` (CHECK: admin | sales), `is_active`, `last_login_at`, `created_at`, `updated_at`.
  На неё нет внешних ключей из `core` — только `user_id` (uuid).

### 1.8 План работ (P1, H2–H8, ≈ 3 ч)

AU-01 … AU-04 (1 ч) → AU-05 … AU-07 с тестами (1 ч) → AU-08 и подключение в core (0.5 ч) → сид двух пользователей и
проверка из фронта (0.5 ч). P1-функции (AU-09 … AU-11) — в окне H32–H40.

### 1.9 Критерии готовности (DoD)

- [ ] `uv run --package leadradar-auth pytest` зелёный: логин успешный и неуспешный (одинаковый ответ), вызов
      `DUMMY_HASH` при несуществующем email, флаги cookie, Bearer работает, истёкший и подделанный токен → 401,
      `require_roles("admin")` для sales → 403, деактивированный пользователь не входит.
- [ ] Из фронта: вход admin и sales, `GET /auth/me` возвращает роль; logout удаляет cookie.
- [ ] На сервере cookie с `Secure`; секрет не в репозитории.
- [ ] `lint-imports`: пакет не импортирует другие workspace-пакеты.

### 1.10 Какие критерии закрывает модуль (MVP)

| ID | Как |
|---|---|
| U11 | Email и пароль, JWT, роли admin и sales |
| K4 | Продажник видит рабочие экраны, настройки — у администратора |
| K5 | Argon2, PyJWT, httpOnly-cookie, защита от timing-атак, 401 и 403 по стандарту |
| K6 | `org_id` в токене и в `Principal` — заготовка мультитенантности; интерфейс готов к SSO |

---

## 2. Этап 2 — Identity

### 2.1 Цель

Корпоративный вход и управление доступом для многих организаций: SSO, MFA, тонкие права, API-ключи для интеграций,
аудит. Остальные модули по-прежнему работают только с `Principal`.

### 2.2 Функции

| Область | Функции |
|---|---|
| Вход | OIDC SSO (Microsoft Entra ID, Google Workspace) через Authlib, либо внешний IdP (Keycloak / ZITADEL) — core принимает его JWT |
| Сессии | Короткий access (15 мин) + refresh (30 дней, httpOnly, ротация с детекцией повторного использования), отзыв по `jti` (Redis denylist), управление сессиями в UI |
| Безопасность | MFA (TOTP, WebAuthn), лимиты входа в Redis, проверка паролей по Pwned Passwords (k-anonymity), политика паролей |
| Доступ | RBAC с permissions (`config:write`, `leads:export`, `users:manage` …), территории (рынки → пользователи), команды |
| Организации | Мультитенантность, переключатель организации, приглашения, SCIM-провижининг |
| Интеграции | API-ключи (хранится только хеш, скоупы, срок действия) для публичного API, HubSpot-webhooks и Zapier |
| Аудит | Журнал входов и действий администраторов, экспорт |

### 2.3 Входы и выходы (изменения)

`Principal` получает поля `permissions: set[str]`, `territories: list[str]`, `auth_method` — только добавление.
`require_roles` остаётся, рядом появляется `require_permissions`. Новые эндпоинты: `/auth/oidc/*`, `/auth/refresh`,
`/auth/mfa/*`, `/auth/api-keys`, `/auth/sessions`, `/auth/audit`.

### 2.4 Зависимости

IdP (Entra ID, Google) или Keycloak / ZITADEL, Redis, секрет-хранилище.

### 2.5 Критерии готовности этапа 2

SSO работает с Entra ID и Google; MFA обязательна для admin; отзыв сессии действует ≤ 1 мин; тесты изоляции тенантов;
аудит покрывает 100% административных действий.

### 2.6 Критерии, которые усиливает этап 2

K5 (безопасность корпоративного уровня), K6 (продажа платформы другим IT-провайдерам: SSO и мультитенантность — обязательные требования).

---

## 3. Рост и развитие: что заложено в MVP и как расширять

| Заложено в MVP | Зачем | Как растёт на этапе 2 |
|---|---|---|
| `Principal` + `get_current_principal` + `require_roles` как единственный интерфейс | Модули не знают, откуда берётся пользователь | Источник меняется (OIDC, внешний IdP) — модули не трогаем |
| `org_id` в токене | Одна организация сейчас | Мультитенантность и RLS в core |
| `jti` в каждом токене | Не нужен в MVP | Отзыв токенов и учёт сессий |
| Своя схема `auth` без внешних ключей из `core` | Независимость данных | Вынос в отдельный сервис или замена IdP без миграции core |
| Cookie + Bearer | SPA и скрипты | Bearer для публичного API с ключами, cookie для SPA |
| Роли как `Literal` | Простота | Роли → наборы permissions без изменения вызовов `require_roles` |

---

## 4. Риски и анти-паттерны

- **Не хранить токен в localStorage** и не отдавать его в JS — только httpOnly-cookie для SPA.
- **Не использовать `python-jose` и `passlib`** (устарели): по текущему туториалу FastAPI — PyJWT и pwdlib.
- Не различать в ответе «нет такого email» и «неверный пароль».
- Не создавать внешние ключи из `core` на `auth.user_account` — иначе auth не вынести.
- Не коммитить `AUTH_JWT_SECRET`; для demo — отдельный секрет.
