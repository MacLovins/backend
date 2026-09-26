# LeadRadar — архитектура, этапы, план

> **Единый источник правды** по архитектуре, контрактам между папками и плану на 48 часов.
> **Репозитории:** [MacLovins/backend](https://github.com/MacLovins/backend) — этот файл, `parser/`, `ai/`, `auth/`, `core/` ·
> [MacLovins/frontend](https://github.com/MacLovins/frontend) — SPA. Клонируйте оба рядом: `LeadRadar/backend`, `LeadRadar/frontend`.
> ТЗ по папкам: [parser](parser/SPEC.md) · [frontend](https://github.com/MacLovins/frontend/blob/main/SPEC.md) · [общие правила бэкенда](BACKEND.md) ·
> [core](core/SPEC.md) · [ai](ai/SPEC.md) · [auth](auth/SPEC.md)
>
> Версия 1.0 · 2026-09-25 · Документация на русском, код, UI и идентификаторы на английском.

---

## 0. Коротко

- **Что строим.** Платформу, которая превращает публичную информацию о компаниях (новости, сайты, отчёты,
  вакансии, реестры) в **проверяемые сигналы покупки** под конкретную услугу Orange Systems. На выходе:
  объяснимый скор и отранжированный список аккаунтов с ответом «почему именно сейчас».
- **Главное отличие: evidence-first.** Сигнал существует, только если у него есть дословная цитата из источника,
  и эту цитату проверил код, а не LLM. LLM извлекает факты, а ранжирует детерминированная формула. Что это даёт:
  мало ложных срабатываний (K1), мгновенный пересчёт при смене весов (K2), прозрачность для продажников (K4).
- **Конвейер повторяет процесс из Annex (A1):** `ICP → Accounts → Public Data → Signals → Interpretation → Score →
  Prioritized Accounts`. Каждому шагу соответствуют узел графа, таблица и экран.
- **Этап 1 (MVP, 48 ч, 5 человек: 3 на Python, 2 на React).** Модульный монолит: uv workspace из четырёх пакетов
  (`parser`, `ai`, `auth`, `core`) плюс React SPA. Gemini на free tier, только бесплатные источники.
  Два пресета, Intelligent Automation и Cybersecurity, рынок — Европа.
- **Этап 2.** Те же пакеты становятся сервисами (ingestion, intelligence, identity, core/BFF, integrations)
  без переписывания: границы и порты заложены в MVP. Добавляются ML-скоринг на фидбеке и исходах CRM,
  агент-исследователь, outreach, HubSpot, алерты, мультитенантность.

---

## 1. Требования

### 1.1 Каталог требований (ID используются во всех SPEC)

**Критерии жюри (K):**

| ID | Критерий | Вес | Что проверяют |
|---|---|---|---|
| K1 | Signal Relevance & Accuracy | 25% | Качество и точность сигналов, низкий false positive |
| K2 | Configurability | 20% | Свои вопросы-сигналы, категории услуг, правила скоринга, ICP |
| K3 | AI/ML Innovation | 20% | Модель скоринга, креативное использование LLM, оригинальность подхода |
| K4 | Usability & UX | 15% | Понятный дашборд для продажников без AI-экспертизы |
| K5 | Technical Execution | 10% | Надёжность пайплайна, интеграции, real-time обработка |
| K6 | Business Impact & Scalability | 10% | ROI, масштабирование на новые рынки, индустрии, вертикали |

**Объём решения из брифа (S):**

| ID | Требование |
|---|---|
| S1 | ICP: рынок, индустрия, размер компании, география |
| S2 | Пользовательские вопросы-сигналы для каждой услуги |
| S3 | Веса сигналов: high / medium / low |
| S4 | Негативные сигналы и правила дисквалификации |
| S5 | Настраиваемая логика скоринга для приоритизации |
| S6 | Данные: Crunchbase (профили, ключевые люди, корпоративные события) |
| S7 | Данные: сайты компаний, newsroom, годовые отчёты, стратегические публикации |
| S8 | Данные: Google News / NewsAPI / GDELT |
| S9 | Данные: карьерные страницы и job boards (сигналы найма: AI, RPA, process mining…) |
| S10 | LinkedIn / Sales Navigator — опционально, ручная валидация; решение **не зависит** от скрейпинга или API LinkedIn |
| S11 | Масштабное извлечение и нормализация из разнородных веб-источников |
| S12 | Динамический скоринг: готовность (readiness) и вероятность покупки |
| S13 | Детекция сигналов: инциденты, смена руководства, tech stack, compliance-события |
| S14 | (опц.) Outreach: персональные сообщения, value proposition под ситуацию, черновики для разных каналов |
| S15 | (опц.) CRM-хуки (HubSpot) |
| S16 | Ожидаемый стек: скрейпинг, PostgreSQL/MongoDB, LLM, LangChain/LangGraph, ML или rule-based скоринг с объяснимостью, React |
| S17 | Для каждого лида: почему он релевантен (объяснение) и скор |
| S18 | Мониторинг публичных источников, а не разовый поиск |
| S19 | Пользователи — SDR-команды IT-провайдеров, не технические специалисты |

**Annex (A):**

| ID | Требование |
|---|---|
| A1 | Цепочка ICP → Accounts → Public Data → Signals → Interpretation → Score → Prioritized Accounts |
| A2 | Сигналы IA: программы снижения затрат/эффективности; digital transformation; AI/RPA/Agentic AI/Process Mining проекты; релевантный найм; новые руководители; shared services / консолидация процессов; технологии в использовании; существующие технологические партнёры |
| A3 | Оценка сигнала: релевантность, свежесть, сила, позитивный/негативный |
| A4 | Лица, принимающие решения: CIO, COO, Head of Digital Transformation, Head of Automation / Process Excellence |
| A5 | Примеры Lufthansa и DHL: сильные позитивные сигналы плюс негатив из-за собственных компетенций и текущих провайдеров |
| A6 | Заменить ручной субъективный процесс стандартизированным автоматическим |

**Ограничения команды (U):**

| ID | Ограничение |
|---|---|
| U1 | Бэкенд целиком на Python, фронтенд на React |
| U2 | MVP за 48 часов, команда 5 человек: 3 Python + 2 React |
| U3 | Этап 2 вырастает из MVP без переписывания |
| U4 | LLM: Gemini через `google-genai`, free tier, бюджет $0 |
| U5 | Данные: только бесплатные источники или free trial |
| U6 | Аккаунты: CSV и ручной ввод плюс автопоиск по ICP |
| U7 | Демо-фокус: Intelligent Automation + Cybersecurity, Европа |
| U8 | Outreach, алерты и HubSpot — после ядра, как надстройки |
| U9 | Деплой на свой сервер за Cloudflare |
| U10 | Модульный монолит |
| U11 | Авторизация: email + пароль, JWT, две роли |
| U12 | LangGraph с первого дня (у команды уверенный опыт) |
| U13 | Документы: `SPEC.md` в каждой папке + `ARCHITECTURE.md` |

### 1.2 Допущения (проверить в час 0)

| # | Допущение | Если не так |
|---|---|---|
| D1 | Лимиты free tier Gemini (RPM/RPD) видны в AI Studio → Rate limits; прописываем их в `.env` | Меняем пул моделей и расписание пакетного прогона (§4.8) |
| D2 | Сервер: Linux + Docker, ≥ 4 vCPU / 8 GB RAM, доступ через Cloudflare (named tunnel или проксируемый DNS) | При 4 GB: эмбеддинги `multilingual-e5-small`, 1 worker, без браузера |
| D3 | По правилам хакатона код пишется в течение 48 ч; план, ключи и аккаунты можно готовить заранее | Если можно заранее — выполнить M0 (§4.10) до старта |
| D4 | UI на английском (международные рынки, жюри); источники мультиязычные: EN/DE/FR/NL/RO… | — |
| D5 | Демо: 40–60 европейских компаний прогнаны заранее; вживую прогоняем 1–3 новые компании | — |

---

## 2. Принципы и применённые практики

Источники собраны в §8. Каждая практика привязана к месту, где она применяется.

| # | Практика | Как применяем | Где |
|---|---|---|---|
| P1 | **Workflows before agents.** Сначала предсказуемые цепочки, агенты — только когда шаги нельзя предсказать | MVP — детерминированный граф: цепочка промптов + параллельные ветки. Автономный агент-исследователь — только на этапе 2 | ai |
| P2 | **Durable execution** | LangGraph checkpointer (Postgres) + `RetryPolicy` на сетевых и LLM-узлах: упавший прогон продолжается с последнего шага | ai, core |
| P3 | **Модульный монолит с проверяемыми границами** | uv workspace из 4 пакетов; import-linter в CI запрещает недопустимые импорты | все Python |
| P4 | **Ports & adapters** | `ai` и `parser` не знают про БД и HTTP API. `core` реализует порты. На этапе 2 порт становится сетевым клиентом | ai, parser, core |
| P5 | **Contract-first API** | OpenAPI из FastAPI → Orval генерирует типы, хуки TanStack Query и MSW-моки. Фронт работает на моках с часа 2 | core, frontend |
| P6 | **FastAPI best practices** | Доменные модули (router/schemas/models/service/repository), async I/O, Pydantic везде, dependencies для валидации, миграции `YYYY-MM-DD_slug`, тесты на httpx `AsyncClient` | core, auth |
| P7 | **Native SSE (FastAPI ≥ 0.135)** | `EventSourceResponse` с автоматическим keep-alive каждые 15 с. Клиент шлёт POST-SSE и переходит на polling, если поток буферизуется (известная проблема GET-SSE в Cloudflare quick tunnel) | core, frontend |
| P8 | **Async-очередь** | Taskiq + Redis: async-native, есть интеграция с FastAPI и планировщик. ARQ отвергнут — maintenance-only | core |
| P9 | **Auth по актуальному туториалу FastAPI** | PyJWT + `pwdlib[argon2]`, dummy-hash против timing-атак, httpOnly-cookie | auth |
| P10 | **Feature-based React (bulletproof-react)** | `features/*`, импорты только в сторону shared → features → app, правило ESLint `import/no-restricted-paths` | frontend |
| P11 | **Промптинг Gemini** | Правила, роль и формат — в system instruction. В длинном контексте сначала данные, задача в конце. Один формат разделителей (XML-теги). Few-shot в каждом промпте и одинаковой структуры. `temperature` у Gemini 3.x не трогаем. Structured output по JSON Schema из Pydantic | ai |
| P12 | **Context engineering** | Минимальный набор высокосигнальных токенов: префильтр отбирает ≤ 40 фрагментов на вызов. Вместо списка всех edge cases — несколько канонических разнообразных примеров | ai |
| P13 | **Grounded extraction** | Дословные цитаты выравниваются по исходному тексту (как в Google LangExtract) и проверяются кодом | ai |
| P14 | **Hybrid retrieval** | BM25 + эмбеддинги, слияние Reciprocal Rank Fusion (k = 60) | ai |
| P15 | **B2B lead scoring** | Два измерения: Fit (ICP) и Intent (сигналы). Маршрутизация по матрице, а не по сумме. Полураспад свой для каждого типа сигнала. Отрицательные баллы и дисквалификация | ai |
| P16 | **Извлечение основного текста** | trafilatura — лидер бенчмарков и не требует браузера. Браузер (Playwright/Crawl4AI) — только fallback | parser |
| P17 | **Вежливый краулинг** | robots.txt, узнаваемый User-Agent, лимиты на хост, HTTP-кэш. GDELT — не чаще 1 запроса в 5 с, Wikidata — по UA-policy | parser |
| P18 | **Spec-driven разработка с агентами** | Каждый SPEC самодостаточен: называет файлы и интерфейсы, говорит, что вне рамок, и заканчивается end-to-end проверкой | все SPEC |
| P19 | **Eval-driven AI** | Золотой набор, precision/recall, фидбек продажников как разметка. Для ранжирования — precision@k и PR-AUC | ai, core |
| P20 | **12-factor config** | Всё через env (pydantic-settings), один образ для dev и demo | все |

---

## 3. Предметная модель (общая для обоих этапов)

### 3.1 Конвейер Annex → система (A1)

| Шаг Annex | Что делает система | Модуль | Экран UI |
|---|---|---|---|
| ICP | Критерии рынка, индустрии, размера, географии; must-have и nice-to-have | `core.config`, `ai.scoring.fit` | Settings → ICP |
| Accounts | CSV, ручной ввод, автопоиск по ICP (Wikidata/GLEIF) | `core.accounts`, `parser.discover` | Accounts, Discovery |
| Public Data | Сбор из новостей, сайтов, отчётов, вакансий и реестров; нормализация и дедуп | `parser.collect` | Company → Sources |
| Signals | Ответы на вопросы-сигналы с дословными цитатами | `ai` prefilter → extract → verify | Company → Evidence |
| Interpretation | Полярность, сила, свежесть, надёжность источника, подтверждение несколькими источниками | `ai.verification`, `ai.scoring` | Карточки сигналов |
| Score | Fit, Intent, Risk → Priority, Tier, дисквалификация | `ai.scoring` | Score breakdown |
| Prioritized Accounts | Ранжирование, «почему сейчас», фильтры, экспорт | `core.leads` | Prospects |

### 3.2 Словарь

| Термин | Определение |
|---|---|
| **Service** | Услуга Orange Systems (Intelligent Automation, Cybersecurity…). Свой набор вопросов, ICP, правил и профиль скоринга |
| **Signal question** | Бизнес-вопрос на естественном языке с атрибутами: категория, полярность (+/−), вес (H/M/L), источники, окно свежести, ключевые слова |
| **ICP** | Критерии идеального клиента: must-have (фильтр) и nice-to-have (веса) |
| **Disqualification rule** | Правило исключения, ограничения скора или пометки: по фирмографике, сигналу или списку |
| **Scoring profile** | Версионируемые параметры формулы: веса, полураспады, пороги тиров, баланс Fit/Intent |
| **Company (account)** | Целевая компания. Ключ — домен |
| **Document** | Нормализованная единица данных из источника: новость, страница, вакансия, страница отчёта, запись реестра |
| **Snippet** | Фрагмент документа (120–220 слов) для префильтра и LLM |
| **Evidence** | Дословная цитата + ссылка + дата + сила, извлечённые LLM из snippet |
| **Signal** | Evidence, прошедший проверку кодом (verification), привязанный к вопросу |
| **Fit / Intent / Risk** | 0–100: соответствие ICP / сила позитивных сигналов с учётом свежести / сила негативных сигналов |
| **Priority / Tier** | Итоговый скор 0–100 и уровень: `hot` / `warm` / `cold` / `disqualified` |
| **Run** | Запуск анализа, автопоиска или пересчёта; имеет прогресс и события |
| **Domain event** | Факт в outbox (`signal.detected`, `lead.tier_changed`…). Точка расширения для алертов и CRM |

### 3.3 Таксономия сигналов (закрывает A2, S13)

| Категория (`category`) | Смысл | Пример вопроса | Покрывает |
|---|---|---|---|
| `cost_efficiency` | Программы снижения затрат и эффективности | Cost reduction / efficiency targets | A2 |
| `digital_transformation` | Программы цифровой трансформации | Strategy 2030 с цифровизацией процессов | A2 |
| `ai_automation` | AI / RPA / Agentic AI / IDP / Process Mining инициативы | Agentic AI use cases in operations | A2 |
| `hiring` | Релевантный найм | RPA developer, SOC analyst | A2, S9 |
| `leadership_change` | Новые руководители | New COO / CIO / CISO | A2, S13 |
| `shared_services` | Консолидация процессов, GBS, SSC | New shared service center | A2 |
| `tech_stack` | Технологии в использовании | UiPath, Celonis, SAP S/4HANA, Sentinel | A2, S13 |
| `tech_partners` | Существующие технологические партнёры (часто негатив) | Long-term MSSP / automation vendor | A2, A5 |
| `incident` | Кибер-инциденты, утечки, сбои | Ransomware attack, data breach | S13 |
| `compliance` | Регуляторные события: NIS2, DORA, ISO 27001, TISAX | NIS2 readiness program | S13 |
| `investment` | Бюджеты и инвестиции в IT и безопасность | Security budget increase | — |
| `expansion` | Выход на рынки, M&A, интеграция поглощений | Acquisition integration | — |
| `internal_capability` | Сильные собственные компетенции (негатив) | In-house automation CoE | A5 |
| `distress` | Блокеры расходов: неплатёжеспособность, заморозка найма | Hiring freeze, insolvency | S4 |

### 3.4 Модель скоринга (сводка; формулы и тесты — в [ai/SPEC.md](ai/SPEC.md) §1.7)

```
вклад evidence  v = strength × confidence × reliability(source) × 0.5^(age_days / half_life(source))
сила вопроса    s_q = 1 − Π(1 − v_e)                       # noisy-OR по историям: перепечатки одной новости — одна (V5)
Intent (0–100)  = 100 × (1 − exp(−Σ_{q+} w_q·s_q / τ))     # насыщение
Risk   (0–100)  = 100 × (1 − exp(−Σ_{q−} w_q·s_q / τ_neg))
Fit    (0–100)  = must-have ? 40 + 60 × Σ(вес совпавших nice-to-have) / Σ(весов) : 0   # nice-to-have ранжирует
Priority        = 100 × (Fit/100)^0.4 × (Intent/100)^0.8 × (1 − 0.5·Risk/100)   # нужны и Fit, и Intent
Tier            = disqualified (правило) | hot ≥ 65 и ≥ 2 вопроса с s_q ≥ 0.5 | warm ≥ 40 | cold
```

Значения по умолчанию лежат в профиле скоринга и меняются в UI: веса H/M/L = 3/2/1; сила weak/moderate/strong =
0.35/0.65/1.0; τ = 5, τ_neg = 2; полураспад: jobs 45 дн., news 120, website 240, report 365, incident 270;
сигнал без даты из датируемого источника × 0.5. Калибровка (2026-09-26): один сильный вопрос с весом H ≈ Warm (47),
два — Hot (69), смена веса одного сильного вопроса H → L сдвигает Priority на 10–13 пунктов. При τ = 3 и степени 0.6
один сигнал средней силы уже давал Warm 51, а один сильный — Hot 71, и веса почти не влияли на тир.
Readiness (готовность) = Intent: свежие триггеры весят больше. Likelihood (вероятность) видна как пара Fit × (1 − Risk).
Ранжирование основано на матрице Fit × Intent, а не на сумме (P15).

### 3.5 Пресеты демо (U7). Полные YAML — в `ai/src/leadradar_ai/presets/`

**Intelligent Automation (IA)** — ICP: EU/UK/CH/NO, ≥ 1 000 сотрудников; приоритетные индустрии: логистика и транспорт,
авиаперевозки, производство, ритейл, банки и страхование, телеком, энергетика.

| Ключ | Вопрос (кратко) | Кат. | ± | Вес | Источники | Окно |
|---|---|---|---|---|---|---|
| ia_cost | Объявлены программы снижения затрат / эффективности с целями или сроками? | cost_efficiency | + | H | news, website, report | 365 |
| ia_ai_projects | Идут или планируются AI, RPA, agentic AI, IDP, process mining инициативы? | ai_automation | + | H | news, website, report | 365 |
| ia_hiring | Нанимают RPA-разработчиков, automation engineers, process excellence, BA, AI-специалистов? | hiring | + | H | jobs | 90 |
| ia_dt | Есть программа цифровой трансформации, включающая цифровизацию процессов? | digital_transformation | + | M | website, report, news | 540 |
| ia_ssc | Консолидируют процессы в shared services / GBS, реструктурируют back-office? | shared_services | + | M | news, website, report, jobs | 365 |
| ia_leaders | Новый COO / CIO / CDO / CFO / Head of Transformation, Automation, Process Excellence за 12 мес.? | leadership_change | + | M | news, website | 365 |
| ia_erp | Идёт миграция ERP / core-систем (например, SAP S/4HANA) или крупная модернизация IT? | tech_stack | + | M | jobs, news, report | 365 |
| ia_stack | В вакансиях и материалах упоминаются UiPath, Automation Anywhere, Blue Prism, Power Automate, Celonis, ServiceNow? | tech_stack | + | L | jobs, website | 365 |
| ia_inhouse | Есть зрелый внутренний Automation / AI CoE или большая своя команда автоматизации? | internal_capability | − | M | website, report, news, jobs | 730 |
| ia_partner | Публично назван стратегический партнёр или вендор по автоматизации / AI (долгий контракт)? | tech_partners | − | M | news, website | 730 |
| ia_distress | Неплатёжеспособность, заморозка найма в IT, крупная распродажа активов? | distress | − | H | news | 365 |

Правила: `employees < 500` → exclude; компания сама вендор IT / автоматизации (индустрия `it_services`/`software`) → flag.
ЛПР для ручной проверки в LinkedIn (A4): COO, CIO, Chief Digital Officer, Head of Digital Transformation,
Head of Automation, Head of Process Excellence.

**Cybersecurity (Cyber)** — ICP: ЕС (NIS2) + UK/CH/NO, ≥ 250 сотрудников; приоритет — секторы из приложений I и II NIS2,
для финансового сектора — DORA.

| Ключ | Вопрос (кратко) | Кат. | ± | Вес | Источники | Окно |
|---|---|---|---|---|---|---|
| cy_incident | Кибер-инцидент (ransomware, утечка, сбой из-за атаки) за 18 мес.? | incident | + | H | news, incident | 540 |
| cy_compliance | Готовятся к NIS2 / DORA / ISO 27001 / TISAX / CRA, упоминают аудиты и дедлайны? | compliance | + | H | news, website, report, jobs | 540 |
| cy_hiring | Нанимают CISO, SOC-аналитиков, security engineers, IAM, GRC, пентестеров? | hiring | + | H | jobs | 90 |
| cy_leaders | Новый CISO / CIO / CTO за 12 мес.? | leadership_change | + | M | news, website | 365 |
| cy_surface | Облачная миграция, M&A-интеграция, OT/IoT — растёт поверхность атаки? | expansion | + | M | news, website, report, jobs | 365 |
| cy_budget | Объявлено увеличение бюджета или программа безопасности? | investment | + | M | news, report | 365 |
| cy_stack | В вакансиях есть SIEM, EDR, IAM, zero trust (Splunk, Sentinel, CrowdStrike, Okta…)? | tech_stack | + | L | jobs | 365 |
| cy_mssp | Публично объявлен долгосрочный контракт с MSSP / SOC-провайдером? | tech_partners | − | M | news, website | 730 |
| cy_inhouse | Крупный собственный SOC / CERT, публичная security-организация? | internal_capability | − | M | website, report | 730 |

Производный сигнал без LLM (P1): «вероятно в скоупе NIS2» — если индустрия из приложения I/II, страна в ЕС и
≥ 50 сотрудников или > €10 млн выручки; «в скоупе DORA» — финансовый сектор ЕС.
Правила: компания — вендор кибербезопасности → exclude; `employees < 200` → exclude.
ЛПР: CISO, CIO, Head of IT Security, Head of GRC.

---

## 4. Этап 1 — MVP (48 часов)

### 4.1 Цель и границы

**Цель.** Рабочий продукт, который закрывает все требования §1.1 минимальными средствами. Сквозной сценарий:
задать услугу, вопросы и ICP → добавить или найти компании → собрать публичные данные → получить проверенные
сигналы с цитатами → получить объяснимый скор и рейтинг → оценить сигналы и увидеть точность.

| Входит в MVP (P0 — без этого продукт не работает) | Не входит (P1 — если успеваем; остальное — этап 2) |
|---|---|
| Настройка услуг, вопросов, весов, полярности, источников, ICP, правил, профиля скоринга через UI | Outreach, алерты, HubSpot — надстройки после ядра, §4.13 (U8) |
| Импорт CSV, ручное добавление, автопоиск по ICP (Wikidata) | ML-модель скоринга (этап 2: нужна разметка и исходы CRM) |
| Сбор: GDELT, сайт и newsroom компании, ATS-вакансии, Wikidata | Crunchbase API: платный, в 2026 бесплатного нет — адаптер на этапе 2, в MVP заменён Wikidata/GLEIF и импортом CSV-выгрузки |
| Сбор Data Layer: Google News RSS, GDELT, NewsAPI, SerpAPI, ATS, Wikidata | Crunchbase direct API |
| Граф LangGraph: prefilter → Gemini/OpenAI extract → verify → score | Агент-исследователь с открытым поиском |
| Объяснимый скор, «почему сейчас», карточки сигналов с цитатами и ссылками | Playwright / JS-рендеринг (P2 fallback) |
| Outreach Generation: генерация персонализированных писем/InMail по сигналам | HubSpot двусторонняя синхронизация (этап 2) |
| Живой прогресс прогонов (SSE + polling fallback) | Мультитенантность (колонка `org_id` уже есть) |
| Фидбек по сигналам и метрика precision | SSO, MFA, refresh-токены |
| Вход email + пароль, роли `admin` и `sales` | i18n интерфейса |
| Деплой docker compose + Cloudflare | Kubernetes, отдельные сервисы |

P1 внутри 48 ч (по убыванию пользы): Google News RSS, GLEIF, HIBP (утечки для Cyber), PDF годовых отчётов,
производные сигналы NIS2/DORA, подсказка вопросов от AI, CSV-экспорт, страница Quality, cron-обновление, activity feed.

### 4.2 Архитектура MVP

```
          SDR / Sales (роль sales) · Head of Sales / RevOps (роль admin)
                                   │ HTTPS через Cloudflare Tunnel
┌──────────────────────────────────▼────────────────────────────────────────────┐
│ web (Caddy): статика frontend/dist + reverse proxy /api/* → api:8000           │
└───────────┬───────────────────────────────────────────────────┬───────────────┘
            │ SPA                                               │ REST /api/v1 (cookie-JWT), SSE
┌───────────▼───────────────┐              ┌────────────────────▼──────────────────────────┐
│ frontend/  React 19 SPA   │              │ api — core (leadradar-core)                   │
│ Prospects · Company ·     │              │ FastAPI: config · accounts · runs · leads ·   │
│ Accounts · Discovery ·    │              │ feedback · meta · activity                    │
│ Runs · Settings · Quality │              │ + router и dependencies из auth               │
└───────────────────────────┘              └───────┬───────────────────────┬───────────────┘
                                     enqueue (Taskiq) │                       │ SQLAlchemy async
                                    ┌─────────────────▼──┐       ┌────────────▼──────────────────┐
                                    │ Redis 7            │       │ PostgreSQL 16 + pgvector       │
                                    │ очередь, pub/sub   │       │ схемы: core · auth · langgraph │
                                    │ прогресса          │       └────────────▲──────────────────┘
                                    └─────────▲──────────┘                    │ адаптеры портов ai
                                              │                               │
                               ┌──────────────┴───────────────────────────────┴────┐
                               │ worker + scheduler (тот же образ leadradar-core)    │
                               │ исполняет граф LangGraph из ai                      │
                               └──────┬─────────────────────────────────┬───────────┘
                                      │ Collector-порт                  │ LLMClient-порт
                         ┌────────────▼────────────┐       ┌────────────▼────────────────┐
                         │ parser/ (библиотека)    │       │ ai (библиотека)             │
                         │ адаптеры источников,    │       │ граф, prefilter, extract,   │
                         │ нормализация, discovery │       │ verify, scoring, presets    │
                         └────────────┬────────────┘       └────────────┬────────────────┘
                                      │ HTTP (вежливо, с кэшем)          │ google-genai
      GDELT · сайты / newsroom / PDF · ATS (Greenhouse, Lever, Workday…) · Wikidata     Gemini API
      P1: Google News RSS · GLEIF · HIBP · NewsAPI и Adzuna (по ключу)                  (free tier)
```

**Процессы** (один Python-образ, разные команды): `api` (uvicorn), `worker` (taskiq worker), `scheduler` (taskiq scheduler).
Плюс `web` (Caddy со сборкой фронта), `postgres`, `redis`, `cloudflared`.

### 4.3 Папки: роль, связи, владелец

Репо `MacLovins/backend` содержит `parser/`, `ai/`, `auth/` и `core/` (один uv workspace) и общие правила бэкенда
в `BACKEND.md`, репо `MacLovins/frontend` — SPA.

| Папка | Пакет / артефакт | Роль | Зависит от | Кто использует | Владелец |
|---|---|---|---|---|---|
| `parser/` | `leadradar-parser` (lib + CLI `lr-parser`) | Сбор и нормализация публичных данных, автопоиск компаний, резолв компании (домен, careers, ATS), таксономия индустрий | внешние источники | core (адаптер `Collector`, discovery, meta) | P2 — Data engineer |
| `ai/` | `leadradar-ai` (lib + CLI `lr-ai`) | Граф анализа (LangGraph), LLM-шлюз Gemini, префильтр, извлечение, верификация, скоринг, пресеты, evals | внешние: Gemini, fastembed | core (worker, rescore, config) | P3 — AI engineer |
| `auth/` | `leadradar-auth` (lib + CLI `lr-auth`) | Пользователи, логин, JWT, роли, `Principal` | — | core (роутер и dependencies) | P1 |
| `core/` | `leadradar-core` (app + CLI `lr`) | REST API, БД и миграции, worker, SSE, импорт, discovery, лиды, фидбек, метрики, сиды, деплой | parser, ai, auth | frontend | P1 — Backend lead |
| `BACKEND.md`, `Dockerfile` | общие правила и образ | Workspace, процессы, владение БД, границы; один образ для api / worker / scheduler | — | — | P1 |
| `frontend/` | SPA (Vite) | Дашборд продажника и настройки администратора | OpenAPI core | пользователи | F1 (sales UX), F2 (admin UX) |

### 4.4 Правила зависимостей (проверяет import-linter в CI)

```
leadradar_core ──▶ leadradar_parser   (только публичный API из __init__)
leadradar_core ──▶ leadradar_ai       (только публичный API из __init__)
leadradar_core ──▶ leadradar_auth     (только публичный API из __init__)
leadradar_ai   ──✗ parser, core, auth  (ai получает данные через порты)
leadradar_parser ─✗ ai, core, auth     (чистая библиотека сбора)
leadradar_auth ──✗ ai, core, parser
frontend ──▶ только HTTP API core (сгенерированный клиент); никаких ручных fetch-ей мимо клиента
```

Общих «shared»-библиотек нет. У каждого пакета свои контракты (Pydantic), `core` явно преобразует их в
`core/src/leadradar_core/adapters/`. Небольшое дублирование полей — осознанная плата за то, что на этапе 2
пакеты выносятся в сервисы без переписывания.

### 4.5 Ключевые потоки

**(a) Анализ компании** (цель p50 ≤ 90 с на компанию и две услуги, при кэше — ≤ 30 с):

```
UI: Analyze ─▶ POST /api/v1/runs {company_ids, service_ids}
core: создаёт analysis_run (queued) ─▶ Taskiq: analyze_company(run_id, company_id)   ─▶ 202 {run_id}
UI: POST /api/v1/runs/{id}/events (SSE)  ◀── Redis pub/sub ◀── ProgressSink
worker: граф ai.analysis (thread_id = run_id:company_id, checkpointer = Postgres)
  resolve   ── Collector.resolve ─▶ parser.resolve_company   (домен, careers, ATS, фирмографика)
  collect   ── Collector.collect ─▶ parser.collect ─▶ core сохраняет Document (дедуп по content_hash)
  index     ── нарезка на snippets + локальные эмбеддинги (fastembed) ─▶ document_chunk
  prefilter ── по услуге: BM25 + векторы → RRF → top-k фрагментов на вопрос, фильтр сущности и свежести
  extract   ── 1 вызов Gemini на услугу: ответы на все вопросы + цитаты (structured output)
  verify    ── код: цитата есть в тексте? про ту компанию? свежая? → Signal / RejectedEvidence
  score     ── формула §3.4 → lead_score (+ история) → domain_event (signal.detected, lead.tier_changed)
  finalize  ── статистика прогона, событие company.done
UI: карточка лида обновляется (событие + refetch)
```

**(b) Смена настроек скоринга:** `PUT /services/{id}/scoring-profile` создаёт новую версию и синхронно пересчитывает
всех по уже извлечённым сигналам (чистая функция `ai.score_company`, без LLM). Цель: ≤ 2 с на 1 000 компаний.

**(c) Изменение или добавление вопроса:** версия вопроса +1 → задача `expand_question` генерирует мультиязычные ключевые
слова (дешёвая модель) → вопрос помечен stale → UI предлагает повторный анализ. При повторном прогоне по
fingerprint (версии вопросов + id фрагментов) заново извлекается только затронутая услуга.

**(d) Автопоиск:** `POST /discovery/search` → `parser.discover(ICP)` (Wikidata SPARQL) → кандидаты с Fit из
`ai.fit_score` → пользователь отмечает → `POST /discovery/accept` → компании с `origin=discovery`.

**(e) Фидбек → качество:** 👍/👎 на сигнале → `feedback` → `GET /quality` → precision по категориям и источникам,
плюс доля отклонённых верификатором цитат (метрика галлюцинаций).

**(f) Мониторинг (S18):** повторный анализ идемпотентен и инкрементален: документы дедуплицируются, LLM вызывается
только при новых фрагментах или изменённых вопросах. P1: cron в `scheduler` (по умолчанию раз в 6 ч) для
отслеживаемых компаний, бейдж NEW на сигналах за 7 дней.

### 4.6 Данные и владение

Postgres 16 + pgvector. Владелец схемы `core` — `core` (Alembic). Схема `auth` — `auth`
(свой `MetaData`, миграции подключены в тот же Alembic env). Схема `langgraph` — чекпоинты
(`AsyncPostgresSaver.setup()`). Внешних ключей между схемами нет: на этапе 2 `auth` уезжает отдельно.

| Таблица (`core`) | Суть | Ключевые поля |
|---|---|---|
| `org` | Организация (одна в MVP, заготовка мультитенантности) | id, name |
| `service` | Услуга | id, org_id, name, slug, description, value_proposition, is_active |
| `signal_question` | Вопрос-сигнал | service_id, key, text, category, polarity, weight, source_types[], recency_days, keywords jsonb, keywords_status, version, is_active |
| `icp_profile` | ICP услуги | service_id, must_have jsonb, nice_to_have jsonb, version |
| `disqualification_rule` | Правило | service_id, kind, condition jsonb, action (exclude/cap/flag), cap_value |
| `scoring_profile` | Версия параметров скоринга (неизменяемая) | service_id, version, params jsonb, is_current |
| `company` | Аккаунт | name, domain (уник. в org), aliases[], country_code, industry_ids[], employees, revenue_eur, wikidata_qid, lei, careers_url, newsroom_url, ats jsonb, linkedin_url, notes, origin, last_analyzed_at |
| `document` | Нормализованный документ | company_id, source_type, source_name, url, canonical_url, title, text, published_at, fetched_at, language, content_hash (уник. в компании), meta jsonb |
| `document_chunk` | Фрагмент + эмбеддинг | document_id, company_id, ord, text, char_start, char_end, embedding vector(384) |
| `analysis_run` | Прогон | kind, status, params, progress, stats, error, created_by |
| `run_event` | События прогона (реплей SSE, аудит) | run_id, company_id, stage, status, message, payload |
| `extraction_state` | Fingerprint последнего извлечения (пропуск LLM, если ничего не изменилось) | company_id, service_id, fingerprint, prompt_version, extracted_at |
| `signal` | Проверенный сигнал | company_id, service_id, question_id, question_version, document_id, run_id, polarity, category, strength, confidence, quote, quote_start, quote_end, summary, event_date, reliability, flags, status, model, prompt_version |
| `rejected_evidence` | Отклонено верификатором | …, reason (quote_not_found, wrong_subject, stale, below_confidence, no_evidence_for_yes) |
| `lead_score` | Скор (текущий + история) | company_id, service_id, scoring_profile_id, fit, intent, risk, priority, tier, disqualified, rule_hits, fit_details, breakdown, why_now, data_gaps (jsonb), is_current |
| `feedback` | Оценки пользователей | user_id, target_type, target_id, verdict, reason |
| `llm_call` / `llm_cache` | Учёт вызовов и кэш ответов | purpose, model, prompt_version, tokens, latency, cache_hit / key, response |
| `domain_event` | Outbox | type, payload, created_at, processed_at |
| `auth.user_account` | Пользователь | email, password_hash, full_name, role, org_id, is_active |

Во всех таблицах `core` есть `org_id`, `created_at`, `updated_at`. Детали — в SPEC `core` §1.4.

### 4.7 Контракты между папками

| Граница | Тип | Где определён | Версионирование |
|---|---|---|---|
| frontend ↔ core | REST + SSE, OpenAPI 3.1 | `core` §1.4; снимок `openapi.json` в корне репо backend, копия во frontend (`npm run sync:api`) | `/api/v1`; тест в CI backend: снимок = живой OpenAPI |
| core → parser | Python API: `resolve_company`, `collect`, `discover`, `industry_taxonomy` | `parser/SPEC.md` §1.4 | Поля Pydantic только добавляются, не удаляются |
| core → ai | Python API: `build_analysis_graph`, `score_company`, `fit_score`, `expand_question`, пресеты | `ai/SPEC.md` §1.4 | То же |
| ai → core | Порты (Protocol): `Collector`, `AnalysisStore`, `ProgressSink`, `LLMCache`, `UsageSink` | `ai/SPEC.md` §1.4 | Порт меняется только по согласию P1 и P3 |
| core → auth | `create_auth_router`, `get_current_principal`, `require_roles`, `Principal` | `auth/SPEC.md` §1.4 | JWT-claims: `sub`, `org`, `role`, `exp`, `iat` |

**Канонические перечисления** (одинаковые в Python, OpenAPI и TS):

```
source_type: news | website | jobs | report | registry | incident | derived | manual
polarity:    positive | negative            weight:   high | medium | low
strength:    weak | moderate | strong       answer:   yes | no | unclear
tier:        hot | warm | cold | disqualified
run.kind:    analyze | discover | rescore | refresh
run.status:  queued | running | succeeded | partial | failed | cancelled
stage:       resolving | collecting | indexing | prefiltering | extracting | verifying | scoring | done | failed | paused
feedback:    correct | incorrect | irrelevant (сигнал) · good_fit | bad_fit (лид)
role:        admin | sales
```

### 4.8 Gemini free tier: бюджет и защита квот (U4)

Факты (сентябрь 2026, проверить в час 0): бесплатно доступны Flash-модели семейства 3.x (например, `gemini-3.8-flash`,
`gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`). Pro на free tier нет.
Лимиты считаются **на проект, а не на ключ**, суточные сбрасываются в полночь по тихоокеанскому времени
(10:00 по Кишинёву летом). Точные RPM/RPD смотрим в AI Studio → Rate limits. Модели 2.5 для новых проектов
ограничены — берём 3.x.

| Мера | Суть |
|---|---|
| 1 вызов на услугу | Все вопросы услуги и отобранные фрагменты (≤ 40, ≤ ~25k токенов) — в одном structured-вызове |
| Без LLM, если нечего проверять | Нет кандидатов у вопроса — ответ `no` без вызова; нет новых фрагментов — извлечение пропускается (fingerprint) |
| Два класса моделей | `main` (извлечение) и `cheap` (ключевые слова, подсказки, классификация индустрии). У каждого класса упорядоченный пул fallback-моделей, у каждой модели своя квота |
| Лимитер | RPM и RPD на модель из `LLM_LIMITS_JSON`; суточный счётчик пишется в `llm_call` и переживает рестарт |
| 429 и исчерпание | Ждём retry-delay или backoff → следующая модель пула → `QuotaExhausted`: компания получает статус `paused`, продолжение с чекпоинта |
| Кэш ответов | Ключ = prompt_version + system + contents + schema. В dev повторный прогон не тратит квоту |
| Раздельные проекты | У каждого разработчика свой проект и ключ в AI Studio; у demo-сервера — отдельный |
| Пакетный прогон демо | Стартует не позже H24, повтор в окне H40. 60 компаний × 2 услуги ≈ 120–200 вызовов |
| Приватность | На бесплатном тарифе Google может использовать запросы для улучшения продуктов (проверить актуальные Terms). Отправляем только публичные данные и неконфиденциальные настройки; на этапе 2 — платный тариф |

### 4.9 Окружение и деплой (U9)

- **Локально:** оба репо клонированы рядом. В backend — `docker compose up postgres redis`, затем `uv run` для api и worker;
  во frontend — `npm run dev` (Vite проксирует `/api` → `:8000`).
- **Demo-сервер:** оба репо клонированы рядом, `docker compose up -d` в backend — сервисы `postgres`
  (`pgvector/pgvector:pg16`), `redis`, `api`, `worker`, `scheduler`, `web` (собирается из соседнего `../frontend`;
  Caddy: статика и `reverse_proxy /api/* api:8000` с `flush_interval -1` для SSE),
  `cloudflared` (named tunnel по `CF_TUNNEL_TOKEN`; quick tunnel не используем — он буферизует GET-SSE).
- **Конфигурация:** единый `.env` по `.env.example`. Секреты не коммитим. `COOKIE_SECURE=true` на сервере.
- **Резерв на демо:** `pg_dump` прогнанной БД, локальный запуск из того же compose, видео прохода демо.
- **CI (GitHub Actions) в каждом репо:** backend — ruff, pytest (Postgres как service container), import-linter,
  сверка снимка OpenAPI; frontend — `npm run lint`, `typecheck`, `test`, `build`.

Репозитории (клонировать рядом в одну папку):

```
LeadRadar/
├── backend/                    # github.com/MacLovins/backend — uv workspace + инфраструктура
│   ├── ARCHITECTURE.md · openapi.json (снимок API — источник правды для фронта)
│   ├── BACKEND.md              # общие правила бэкенда (процессы, границы, соглашения)
│   ├── pyproject.toml          # корень uv workspace (members: parser, ai, auth, core) + ruff, pytest, import-linter
│   ├── uv.lock · .env.example · docker-compose.yml · Caddyfile · .github/workflows/ci.yml
│   ├── Dockerfile              # один образ для api / worker / scheduler (контекст сборки — корень репо)
│   ├── parser/                 # leadradar-parser
│   ├── ai/                     # leadradar-ai
│   ├── auth/                   # leadradar-auth
│   └── core/                   # leadradar-core
└── frontend/                   # github.com/MacLovins/frontend — SPA: SPEC.md, openapi.json (копия), Dockerfile, CI
```

### 4.10 План на 48 часов

**Роли:** P1 — Backend lead (`core` + `auth` + деплой) · P2 — Data engineer (`parser`) ·
P3 — AI engineer (`ai`) · F1 — Frontend, сторона продаж (Prospects, Company, Runs, shell) ·
F2 — Frontend, сторона администратора (Settings, Accounts, Discovery, Quality).

| Окно | Веха | P1 | P2 | P3 | F1 | F2 |
|---|---|---|---|---|---|---|
| H0–H2 | **M0 Kickoff** | Scaffold: uv workspace, compose (pg, redis), CI, скелет core | Скелет parser, HTTP-слой, CLI | Контракты и порты ai, черновики пресетов, лимиты Gemini из AI Studio → `.env` | Шаблон уже в репо (Vite, shadcn base-nova): роутинг, providers, layout, CI | Orval + MSW, мок-данные, дизайн-токены |
| H2–H8 | **M1 Слайсы на моках** | Модели и миграции, auth, CRUD конфигурации и компаний, OpenAPI (H4) | website, gdelt, jobs_ats (Greenhouse, Lever, Workday), Wikidata; JSONL DHL и Lufthansa → P3 к H6 | LLM-шлюз (structured output, лимитер, кэш), нарезка, эмбеддинги, prefilter, промпт v1, скоринг + тесты, граф на фейках | Login, shell, Prospects (моки), каркас Company | Services и Questions (моки), Accounts |
| H8–H12 | **M2 Walking skeleton** | Taskiq worker, runs API, адаптеры портов, SSE | Встраивание в worker, resolve (careers / ATS) | Граф с реальным Gemini, verify | Prospects и Company на живом API | Runs со SSE |
| H11 | Интеграция №1 | DHL end-to-end; деплой на сервер; проверка SSE через Cloudflare (иначе — polling) | | | | |
| H12–H24 | **M3 Глубина** | Discovery API, импорт CSV, версии профиля + rescore, leads и quality API, сиды | discovery (SPARQL + таксономия), остальные ATS, P1: Google News, HIBP, PDF, GLEIF | Ключевые слова вопросов, негативы, P1: NIS2 / DORA, evals + золотой набор, итерации промпта на 10 компаниях | Company полностью: breakdown, why now, сигналы, фидбек, источники, LinkedIn-ссылки; фильтры | ICP, правила, профиль скоринга (мгновенный пересчёт), импорт CSV, Discovery |
| H24 | Интеграция №2 | **Заморозка контрактов:** дальше меняем только по согласию владельцев | | | | |
| H24–H32 | **M4 Данные и качество** | Пакетный прогон демо-аккаунтов (с P2) | Починка адаптеров по логам прогона | Разбор FP → промпт v2, метрики на золотом наборе | Фидбек-UX, состояния empty / loading / error | Quality (P1), полировка |
| — | Вся команда | Разметка ≥ 100 сигналов через UI (по 30 мин на человека) → precision | | | | |
| H32–H40 | **M5 Hardening** | Тесты, cron (P1), CSV-экспорт (P1), README, финальный деплой | Тесты адаптеров | Тесты графа, отчёт evals | E2E-проход, баги | E2E-проход, баги |
| H40 | **Code freeze ядра** | | | | | |
| H36–H44 | Надстройки (только если DoD ядра выполнен) | outreach → алерты → HubSpot (§4.13), за feature-флагами | | | | |
| H40–H48 | **M6 Демо** | Финальный прогон данных, `pg_dump`, видео, слайды, 2 репетиции, 2 ч буфера | | | | |

Правила: ветка на папку, мерж в `main` не реже раза в 3 часа, CI зелёный. P1 обновляет снимок `openapi.json`
в backend, фронт забирает его и перегенерирует клиент (`npm run sync:api && npm run gen:api`). Сон — сменами, минимум 2 блока по 3–4 ч у каждого;
P1 и P3 спят в разное время.

### 4.11 Критерии готовности MVP (сквозные)

1. Выполнены все P0 из всех SPEC, DoD каждого SPEC зелёный.
2. Живой прогон новой компании: от добавления до скора ≤ 3 мин, прогресс виден в UI.
3. ≥ 40 демо-компаний проанализированы по двум услугам. У каждого лида в топ-10 ≥ 2 проверенных сигнала с цитатой, ссылкой и датой.
4. Precision по размеченным пользователями сигналам ≥ 0.8 (≥ 100 меток), число видно в UI.
5. Смена веса или порога → рейтинг пересчитан ≤ 2 с, число вызовов LLM не изменилось.
6. Новый вопрос, написанный в UI на естественном языке, находит сигналы после повторного прогона (ключевые слова сгенерированы автоматически).
7. Продукт открывается по ссылке через Cloudflare, логин под admin и sales, роли соблюдаются.
8. Ни одного запроса к linkedin.com (проверяет тест parser).
9. Резерв готов: локальный compose, дамп БД, видео.

### 4.12 Демо-сценарий (6–7 минут)

1. **Проблема → решение (30 с).** Ручной ресёрч занимает часы на аккаунт. LeadRadar проводит цепочку из Annex автоматически.
2. **Prospects, IA (1 мин).** Топ-10 с тирами. У DHL и Lufthansa — «почему сейчас» с цитатами и ссылками.
   Видны и негативы («сильный внутренний CoE»), ровно как в Annex (A5).
3. **Переключение на Cybersecurity (30 с).** Рейтинг другой: NIS2, инциденты, найм в SOC.
4. **Настраиваемость (1.5 мин).** Вес `ia_hiring` меняем с High на Low — рейтинг пересчитан мгновенно. Добавляем свой вопрос
   («Открывает ли компания shared service center в Восточной Европе?»), ключевые слова генерируются сами.
5. **Живой прогон (1.5 мин).** Анализируем компанию, которую назовёт жюри. Прогресс по стадиям идёт в реальном времени.
6. **Точность (45 с).** Страница Quality: precision по размеченным сигналам, отклонённые верификатором цитаты,
   вендоры (SAP, Celonis) дисквалифицированы.
7. **Масштаб и ROI (45 с).** Новый рынок или индустрия — это настройка, а не код. $0 на LLM в MVP. Этап 2 и HubSpot — роадмап.

### 4.13 Надстройки после ядра (U8, S14, S15)

Подключаются без изменения ядра: каждая — новый потребитель `domain_event`, отдельный endpoint и отдельный feature-флаг.

| Надстройка | Где | Точка подключения, готовая в MVP | Оценка |
|---|---|---|---|
| Outreach-черновики (email, LinkedIn InMail) | `ai.outreach` (промпт) + `core` endpoint + модалка во frontend | Сигналы с цитатами, `service.value_proposition`, LLM-шлюз | 3–4 ч |
| Алерты (Telegram bot API / email) | `core.integrations.alerts` | Outbox `domain_event` + диспетчер потребителей в worker | 2–3 ч |
| HubSpot (push компании, свойств скора и заметки с доказательствами) | `core.integrations.hubspot` | Outbox, CSV-экспорт, формат `why_now` | 3–4 ч |

### 4.14 Риски и меры

| Риск | Вер. | Мера |
|---|---|---|
| Квота Gemini кончилась перед демо | Высокая | Кэш, 1 вызов на услугу, пул моделей, прогон с H24, pause/resume по чекпоинтам, дамп БД |
| Блокировки и 429 от источников (GDELT, сайты) | Средняя | Лимиты на хост, кэш, backoff и circuit breaker, частичные результаты; сбой одного источника не роняет сбор |
| Буферизация SSE в Cloudflare | Средняя | Named tunnel, POST-SSE, fallback на polling; проверка на H11 |
| Омонимы (Orange, Continental, Shell) | Средняя | Запросы привязаны к домену, алиасы, проверка упоминания в тексте и поле `subject` у LLM |
| Ложные срабатывания: новости вендора, маркетинговая вода | Высокая | Рубрика силы, проверка цитат, негативные вопросы, правило «вендор», фидбек и метрика |
| Не успеваем | Средняя | P0/P1/P2, заморозка контрактов на H24, code freeze на H40, надстройки только после DoD |
| Интеграция разваливается | Средняя | Contract-first, walking skeleton к H12, моки из OpenAPI, CI-тест снимка |
| Юридические и этические риски | Низкая | robots.txt, ToS, никакого LinkedIn, минимум персональных данных, атрибуция HIBP (CC BY 4.0) |
| Сеть на площадке | Средняя | Локальный compose, дамп, видео |

---

## 5. Этап 2 — полный проект

### 5.1 Цель

Коммерческая SaaS-платформа sales intelligence для IT-провайдеров: Orange Systems и дальше другие компании (S19, K6).
Тысячи аккаунтов на тенант, непрерывный мониторинг, ML-скоринг, обученный на исходах сделок, интеграция в CRM и
каналы продаж, outreach, измеряемый ROI. Всё вырастает из MVP: пакеты становятся сервисами, порты — сетевыми
клиентами, outbox — шиной событий.

### 5.2 Архитектура этапа 2

```
 Клиенты: SPA · Chrome-расширение · HubSpot / Salesforce · Slack / Teams / Telegram · Public API (ключи, webhooks)
                                    │
                         ┌──────────▼──────────┐          ┌──────────────────────────────┐
                         │ Gateway / BFF (core)│◀────────▶│ Identity (auth → OIDC: SSO,  │
                         │ REST · SSE · RBAC   │          │ MFA, refresh, API keys, SCIM)│
                         └───┬────────────┬────┘          └──────────────────────────────┘
          команды / запросы  │            │ события: outbox → NATS JetStream (Kafka при росте)
   ┌───────────────┬─────────┴─────┬──────┴────────┬────────────────┬───────────────┬──────────────┐
   ▼               ▼               ▼               ▼                ▼               ▼              ▼
 Ingestion     Intelligence    Scoring & ML    Integrations     Outreach       Notifications   Research
 (parser):     (ai): LangGraph (правила + ML,  (HubSpot, SF,    (черновики,    (правила        agent (ai):
 планировщик,  воркеры, роутер feature store,  webhooks,        шаблоны,       алертов,        deep research
 пул краулеров, моделей, судья, обучение,      CSV)             value props)   дайджесты)      по Tier A
 здоровье      batch API       реестр моделей)
 источников
   │               │               │
   ▼               ▼               ▼
 S3 / MinIO (сырые HTML / PDF) · Postgres (RLS, партиции) · pgvector / Qdrant · Redis · ClickHouse (аналитика)
 Наблюдаемость: OpenTelemetry → Grafana (Prometheus / Loki / Tempo) · Langfuse (LLM-трейсы и evals) · Sentry
```

### 5.3 Как папки становятся сервисами без переписывания (U3)

| MVP | Этап 2 | Что добавляется | Что остаётся как есть |
|---|---|---|---|
| `parser` (lib, вызывается воркером) | Ingestion service | HTTP/queue-обёртка, планировщик свежести, пул браузеров, S3, здоровье источников | Адаптеры, контракт `Document`, HTTP-слой |
| `ai` (lib + граф) | Intelligence + Scoring & ML + Research agent | Отдельные воркеры графа, роутер моделей, Batch API, судья, ML-модель | Узлы графа, промпты, порты, контракты, формула как prior |
| `auth` (lib) | Identity (свой сервис или Keycloak/ZITADEL) | OIDC SSO, MFA, refresh-ротация, API-ключи, SCIM, аудит | `Principal`, `require_roles`, JWT-claims |
| `core` (api + worker) | Gateway/BFF + доменные сервисы | Шина событий, RLS, кэш, публичный API | REST `/api/v1`, схемы, outbox, модули |
| `frontend` | SPA + Chrome-расширение | Новые features | Структура features, сгенерированный клиент |

Механика перехода: реализация порта заменяется, например `ParserCollector` (вызов функции) → `HttpCollector`
(вызов сервиса), а outbox-диспетчер начинает публиковать события в шину. Код узлов, адаптеров и UI не меняется.

### 5.4 Новые возможности (по критериям)

- **K1 Точность:** LLM-судья для сигналов с весом H, самосогласованность, калибровка уверенности (isotonic на фидбеке),
  активное обучение (очередь на ручную проверку неуверенных сигналов), precision по источникам с автоотключением
  слабых, регрессионный eval в CI при каждом изменении промпта.
- **K2 Настраиваемость:** песочница вопросов (проверить на 5 компаниях до сохранения), what-if симулятор весов,
  конструктор правил с AND/OR, шаблоны услуг и индустрий, импорт и экспорт конфигов (YAML), профили по рынкам и командам.
- **K3 AI/ML:** ML-скоринг (логистическая регрессия или GBM с монотонными ограничениями) на фидбеке и исходах CRM
  (встреча, сделка), SHAP-объяснения, смешивание с правилами по объёму данных; look-alike по выигранным клиентам;
  агент-исследователь (LangGraph + поиск + human-in-the-loop); кластеризация событий по рынку; граф компаний, людей
  и событий; outreach с цитатами.
- **K4 UX:** сохранённые представления, территории, центр уведомлений, заметки и задачи, Chrome-расширение
  (скор на сайте компании и в LinkedIn для ручной проверки), i18n (EN/RO/RU/DE), мобильная версия.
- **K5 Надёжность:** сервисы с шиной событий, DLQ, идемпотентность, SLO, трассировка, blue-green деплой, IaC.
- **K6 Бизнес:** мультитенантность (RLS), пакеты источников под рынки (DE, RO, Nordics), дашборд ROI
  (время на аккаунт, конверсия сигнал → встреча), биллинг, SLA.
- **S6/S14/S15:** Crunchbase API (при лицензии), outreach-сервис, двусторонний HubSpot и Salesforce (исходы сделок → метки ML).

### 5.5 Масштаб и надёжность: целевые показатели этапа 2

| Метрика | MVP | Этап 2 |
|---|---|---|
| Аккаунтов на тенант | ~100 | ≥ 10 000 |
| Свежесть мониторинга | вручную или раз в 6 ч | новости ≤ 1 ч, вакансии ≤ 24 ч, сайты ≤ 7 дней |
| Анализ одной компании (p95) | ≤ 3 мин | ≤ 60 с |
| Precision сигналов с весом H | ≥ 0.8 | ≥ 0.9 при recall ≥ 0.7 |
| Стоимость LLM | $0 (free tier) | ≈ $0.01–0.05 на пару компания-услуга (~25k входных токенов на платном Flash; Batch API −50%, кэш контекста; цены Flash 3.6–3.8 удваиваются с 01.01.2027) |
| Доступность | best effort | 99.5% |

### 5.6 Безопасность и соответствие

GDPR: B2B-проспектинг на основании законного интереса, минимизация персональных данных (только публичные должности
и имена из новостей), сроки хранения, процедура возражения и удаления. Соблюдение robots.txt и ToS, реестр источников
с правовым статусом, никакого скрейпинга LinkedIn. Коммерческие лицензии там, где бесплатный доступ только для
некоммерческого использования (ransomware.live, NewsAPI, Crunchbase). Секреты в vault, аудит-лог, RBAC,
SSO и MFA, шифрование на уровне хранилища. Платный тариф LLM, чтобы данные клиента не шли на обучение.

### 5.7 Дорожная карта этапа 2

| Фаза | Срок | Содержание |
|---|---|---|
| 2a Пилот | 1–2 мес. | Надстройки U8 в полном виде (outreach, алерты, HubSpot), платный Gemini + Batch, cron-мониторинг, Playwright-fallback, Crunchbase (при лицензии), 300+ аккаунтов Orange Systems, сбор исходов |
| 2b Интеллект | 2–4 мес. | ML-скоринг на исходах, LLM-судья, калибровка, агент-исследователь, песочница вопросов, what-if, Chrome-расширение |
| 2c Платформа | 4–6 мес. | Вынос сервисов, шина событий, мультитенантность, SSO, биллинг, пакеты рынков, Salesforce, SLA |

---

## 6. Матрица покрытия требований (где и как закрыт каждый)

| ID | MVP: где и как | Этап 2: где и как |
|---|---|---|
| **K1** | ai: префильтр (сущность + свежесть), 1 structured-вызов с рубрикой и few-shot, верификация цитат кодом, негативные вопросы, правило «вендор»; core: фидбек → precision; frontend: цитаты, ссылки, 👍/👎 | Судья, самосогласованность, калибровка, активное обучение, eval-гейт в CI |
| **K2** | frontend + core: услуги, вопросы на естественном языке, категории, ±, веса H/M/L, источники, окно свежести, ICP (must / nice), правила (exclude / cap / flag), профиль скоринга (веса, τ, полураспады, пороги) с версиями; ai: автогенерация ключевых слов, мгновенный пересчёт | Песочница, what-if, AND/OR-правила, шаблоны, YAML, профили по командам |
| **K3** | ai: граф LangGraph (prompt chaining + parallelization), гибридный retrieval (BM25 + e5 + RRF), grounded-извлечение, noisy-OR агрегация, полураспад по типу источника, матрица Fit × Intent, производные сигналы NIS2 / DORA (P1), подсказка вопросов (P1) | ML на исходах + SHAP, look-alike, агент-исследователь, граф знаний, outreach |
| **K4** | frontend: рейтинг с тирами и «почему сейчас» простым языком, карточки доказательств, breakdown скора, живой прогресс, пресеты из коробки, фидбек, ссылки для ручной проверки в LinkedIn | Представления, уведомления, расширение, i18n, мобильная версия |
| **K5** | core: Taskiq-очередь, идемпотентные инкрементальные прогоны, чекпоинты LangGraph, ретраи, лимиты, кэш, SSE + polling, health-checks, CI, docker compose; parser: изоляция сбоев источников | Сервисы, шина, DLQ, SLO, OpenTelemetry, автоскейл |
| **K6** | Рынки, индустрии, услуги — через конфиг, а не код; колонка `org_id` везде; реестр адаптеров; метрики использования и стоимости; $0 на MVP; CSV-экспорт (P1) | Мультитенантность, пакеты рынков, CRM, ROI-дашборд, биллинг |
| S1 | core `icp_profile` + frontend ICP-редактор + `ai.fit_score` + parser discovery по ICP | Сегменты ICP, look-alike |
| S2 | core `signal_question` на услугу + редактор | Песочница, библиотека шаблонов |
| S3 | `weight` H/M/L → числа в профиле скоринга | Веса, выученные ML, с подсказками |
| S4 | Вопросы с `polarity=negative` + `disqualification_rule` (exclude / cap / flag) | Сложные правила, списки клиентов и конкурентов из CRM |
| S5 | `scoring_profile` (версии, параметры формулы) + мгновенный пересчёт | ML + правила, A/B профилей |
| S6 | **Частично:** Wikidata (профиль, CEO, идентификаторы, включая Crunchbase ID) + GLEIF (P1) + импорт CSV-выгрузки Crunchbase (пресет маппинга); API-адаптер — интерфейс готов | Crunchbase API по лицензии: люди, события, раунды |
| S7 | parser `website` (newsroom, стратегия, о компании, IR) + `reports` PDF (P1) + RSS newsroom (P1) | Playwright / Crawl4AI, архив снапшотов, LangExtract для длинных отчётов |
| S8 | parser `gdelt` (P0) + `google_news` (P1) + `newsapi` по dev-ключу (P1, только для разработки) | Платный news API, RSSHub, near-real-time |
| S9 | parser `jobs_ats` (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Personio, Recruitee, Workday) + careers HTML fallback + Adzuna (P1) | Агрегаторы, Jobsuche API (DE), история вакансий как тренд |
| S10 | Нет скрейпинга и API LinkedIn (тест). В карточке — ссылки на поиск ЛПР по должностям из пресета, поле `linkedin_url`, заметки | Chrome-расширение для ручной проверки, импорт CSV из Sales Navigator |
| S11 | parser: единый контракт `Document` для всех источников; ai: нарезка, эмбеддинги, structured-извлечение | Ingestion service, батчи, S3, партиции |
| S12 | ai: Intent с полураспадом (readiness) × Fit и Risk (likelihood) → Priority, Tier | ML-вероятность покупки на исходах CRM |
| S13 | Категории `incident`, `leadership_change`, `tech_stack`, `compliance` в пресетах; HIBP (P1); производные NIS2 / DORA (P1) | SEC 8-K Item 1.05, ransomware.live (лицензия), Wappalyzer-подобная детекция, TED-тендеры |
| S14 | Надстройка после ядра (§4.13): точка подключения готова | Outreach-сервис: мультиканальные черновики с цитатами |
| S15 | Надстройка после ядра (§4.13) + CSV-экспорт (P1) | Двусторонний HubSpot и Salesforce |
| S16 | httpx + trafilatura (Playwright — P2 fallback), PostgreSQL + pgvector, Gemini, LangGraph, rule-based скоринг с объяснимостью, React | Playwright / Crawl4AI, ML-скоринг, SerpAPI-адаптер |
| S17 | `lead_score.why_now` + breakdown + карточки сигналов | Нарративный брифинг по аккаунту от агента |
| S18 | Идемпотентные инкрементальные прогоны + cron (P1) + бейдж NEW + activity (P1) | Непрерывный мониторинг, алерты |
| S19 | Простой язык в UI, пресеты, tooltip «как считается», никакого AI-жаргона | Онбординг-тур, обучение, i18n |
| A1 | Граф, таблицы и экраны повторяют цепочку (§3.1) | То же, в виде сервисов |
| A2 | Все 8 типов — вопросы пресета IA (§3.5) | Расширенная таксономия |
| A3 | Сила (рубрика), свежесть (полураспад), надёжность источника, полярность, подтверждение несколькими источниками | Калибровка на исходах |
| A4 | Список ЛПР в пресете → ссылки на поиск в LinkedIn для ручной проверки; сигналы `leadership_change` | Карта buying committee |
| A5 | Lufthansa и DHL — в золотом наборе и в демо; негативы `internal_capability` и `tech_partners` | Тонкие value props: «чем мы лучше их CoE» |
| A6 | Единый конвейер и формула вместо субъективной оценки; версии профиля делают результат воспроизводимым | ML на исходах |
| U1–U13 | Стек (U1), план §4.10 (U2), §5.3 (U3), §4.8 (U4), источники §4.1 (U5), accounts + discovery (U6), пресеты §3.5 (U7), §4.13 (U8), §4.9 (U9), §4.4 (U10), auth SPEC (U11), граф в ai (U12), этот набор файлов (U13) | — |

---

## 7. Как работать с этими ТЗ (люди + Claude Code)

Принципы (P18): спецификация — источник правды; агент получает самодостаточный кусок, называющий файлы и интерфейсы,
знает, что вне рамок, и имеет команду проверки.

1. **Одна фича — одна сессия.** Открыть сессию в корне, указать SPEC и номер функции. Между задачами — `/clear`.
2. **Сначала план, потом код** (plan mode) для всего, что трогает больше одного файла.
3. **Проверка обязательна:** каждая функция в SPEC имеет команду проверки; агент показывает её вывод.
4. **Чистая логика — через тесты:** скоринг, верификация, нормализация — сначала тест с конкретными числами, потом код.
5. **Параллельно:** у каждого владельца своя ветка или git worktree; контракты меняются только через `ARCHITECTURE.md` §4.7.
6. **Ревью свежим контекстом:** после фичи — `/code-review` или субагент сверяет diff с DoD SPEC.

Шаблон промпта для агента:

```text
Контекст: @ARCHITECTURE.md (§4.4, §4.7) и @parser/SPEC.md.
Задача: реализуй функцию PR-03 (адаптер GDELT) из parser/SPEC.md §1.3. Вне рамок: другие адаптеры, БД.
Интерфейс: SourceAdapter из parser/src/leadradar_parser/adapters/base.py, контракт Document из contracts.py.
Ограничения: не больше 1 запроса в 5 с, на 429 — backoff 60 с, без сети в тестах (respx-фикстуры).
Проверка: `uv run --package leadradar-parser pytest -k gdelt` зелёный, затем
`uv run lr-parser collect --domain dhl.com --name "DHL Group" --sources news --since 30d` — покажи 5 первых строк.
Сначала покажи план, код пиши после моего ок.
```

`CLAUDE.md` в корне каждого репо (создать в H0, ≤ 20 строк): команды (`uv run …` / `npm run …`, `docker compose …`),
ссылка на этот файл (во frontend — `../backend/ARCHITECTURE.md`), правила импорта (§4.4), «не менять контракты §4.7
без согласования», «тесты без сети».

---

## 8. Источники

- Anthropic — Building effective agents: https://www.anthropic.com/engineering/building-effective-agents
- Anthropic — Effective context engineering for AI agents: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Claude Code — Best practices: https://code.claude.com/docs/en/best-practices
- GitHub Spec Kit (spec-driven development): https://github.com/github/spec-kit
- Gemini API — prompting strategies: https://ai.google.dev/gemini-api/docs/prompting-strategies
- Gemini API — structured output: https://ai.google.dev/gemini-api/docs/structured-output
- Gemini API — models / pricing / rate limits: https://ai.google.dev/gemini-api/docs/models · https://ai.google.dev/gemini-api/docs/pricing · https://ai.google.dev/gemini-api/docs/rate-limits
- google-genai Python SDK: https://googleapis.github.io/python-genai/
- Google LangExtract (grounded extraction): https://github.com/google/langextract
- LangGraph — RetryPolicy / CachePolicy / fault tolerance: https://reference.langchain.com/python/langgraph/types/RetryPolicy · https://www.langchain.com/blog/fault-tolerance-in-langgraph
- FastAPI best practices: https://github.com/zhanymkanov/fastapi-best-practices
- FastAPI — Server-Sent Events: https://fastapi.tiangolo.com/tutorial/server-sent-events/
- FastAPI — OAuth2 + JWT (PyJWT, pwdlib): https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/
- Cloudflare quick tunnel: буферизация GET-SSE: https://github.com/cloudflare/cloudflared/issues/1449
- Python task queues 2026 (Taskiq, ARQ maintenance-only): https://aleksul.space/posts/choosing-python-task-queue-library/
- uv workspaces: https://docs.astral.sh/uv/concepts/projects/workspaces/
- Границы модулей: import-linter / tach: https://github.com/deadislove/fastapi-modular-monolith-template · https://pypi.org/project/tach
- bulletproof-react — структура проекта: https://github.com/alan2207/bulletproof-react/blob/master/docs/project-structure.md
- Orval и другие генераторы OpenAPI → TS: https://orval.dev/ · https://dev.to/nyaomaru/which-openapi-codegen-should-you-choose-openapi-typescript-vs-hey-api-vs-orval-vs-kubb-100p
- Hybrid search в Postgres (pgvector + tsvector + RRF): https://www.paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual
- B2B lead scoring (fit + intent, decay, negative scoring): https://breadcrumbs.io/blog/lead-scoring-best-practices/
- trafilatura — бенчмарки: https://trafilatura.readthedocs.io/en/latest/evaluation.html
- GDELT DOC 2.0 API: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/ (лимит 1 запрос / 5 с на IP: https://github.com/alex9smith/gdelt-doc-api/issues/22)
- NewsAPI (dev-план: 100 запросов/сутки, задержка 24 ч, только разработка): https://newsapi.org/pricing
- Crunchbase API в 2026 (бесплатного тарифа нет): https://dev.to/agenthustler/crunchbase-api-in-2026-free-tier-gone-what-startup-data-hunters-do-now-1177
- Публичные JSON-эндпоинты ATS: https://apify.com/zon4a/ats-jobs-api (обзор), Adzuna API: https://developer.adzuna.com/
- Google News RSS — декодирование ссылок: https://github.com/mohamidi74/gnews-decoder
- Wikidata Query Service — лимиты и UA-policy: https://www.mediawiki.org/wiki/Wikidata_Query_Service/User_Manual
- GLEIF API: https://www.gleif.org/en/lei-data/gleif-api
- Have I Been Pwned API (каталог утечек без ключа): https://haveibeenpwned.com/api/v3
- NIS2 — сфера действия и пороги: https://www.arthurcox.com/knowledge/nis2-sme-guidelines-how-do-they-apply-and-thresholds/
- HubSpot — private apps и бесплатный CRM API: https://developers.hubspot.com/docs/apps/legacy-apps/private-apps/overview
