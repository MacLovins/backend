# leadradar-ai

AI-движок LeadRadar: из публичных документов о компании — **проверенные сигналы с дословными цитатами** и
**объяснимый скор** (Fit × Intent × (1 − Risk) → Priority, Tier, «почему сейчас»).
ТЗ: [SPEC.md](SPEC.md) · архитектура: [ARCHITECTURE.md](../../ARCHITECTURE.md) §3.3–3.5, §4.8.

```
документы ─▶ index ─▶ prefilter ─▶ extract (1 вызов Gemini на услугу) ─▶ verify (код) ─▶ score (формула)
```

LLM только извлекает факты с цитатами. Цитату проверяет код, скор считает детерминированная формула.

## Быстрый старт

```bash
cd backend/ai
uv sync                                   # до появления корневого workspace — из папки пакета
uv run pytest                             # без сети, ~2 с
uv run pytest --live                      # + живые тесты (скачивают модель e5, ~0.5 ГБ)
uvx ruff check --line-length 110 src tests
```

Ключ и модели — в `.env` (пример: [../../.env.example](../../.env.example)):
`GEMINI_API_KEY` (или `GOOGLE_API_KEY`), `LLM_MAIN_MODELS`, `LLM_CHEAP_MODELS`, `LLM_LIMITS_JSON` — реальные RPM/RPD
из AI Studio → Rate limits (лимиты считаются на проект, не на ключ).

## CLI `lr-ai`

| Команда | Что делает |
|---|---|
| `lr-ai presets [ключ]` | Пресеты услуг и их вопросы/правила |
| `lr-ai analyze --fixture dhl.jsonl --domain dhl.com --name "DHL Group" --live` | Полный граф на JSONL из `lr-parser collect --out`: скор, «почему сейчас», цитаты со ссылками, отклонённое верификатором, число вызовов LLM. `--json out.json` — сохранить результат |
| `lr-ai expand --preset intelligent_automation --question ia_hiring --live` | Мультиязычные ключевые слова, должности и стоп-слова для вопроса |
| `lr-ai eval --fixtures-dir tests/fixtures [--live] [--fail-under 0.8]` | Precision / recall на золотом наборе, отчёт в `evals/reports/` |

Без `--live` в сеть ничего не уходит: ответы берутся из локального кэша `.cache/lr-ai/llm`, промах — ошибка услуги
с подсказкой. Повторный `--live`-прогон стоит 0 вызовов. Учёт вызовов — `.cache/lr-ai/usage.jsonl`.

У `analyze` для фикстур с вакансиями добавляйте `--own-domain <хост ATS>`, иначе фильтр сущности может отсеять вакансии
без названия компании в тексте. `--fake-embeddings` — без загрузки модели (для быстрых проверок).

## Структура

```
src/leadradar_ai/
├── __init__.py          # публичный API — единственное, что импортирует core
├── contracts.py · ports.py · errors.py · settings.py
├── llm/                 # GeminiClient: пулы main/cheap, лимитер RPM/RPD, 429/5xx, починка JSON, кэш, учёт
├── retrieval/           # нарезка, e5-эмбеддинги, BM25 + косинус → RRF, фильтр сущности, бюджет, fingerprint
├── prompts/<имя>/<версия>/  system.md · user.jinja · examples.json
├── extraction/          # extract_signals@v1: один вызов на услугу
├── verification/        # V1–V4: цитата, субъект, свежесть, согласованность
├── scoring/             # fit, правила, decay, noisy-OR, why_now, производные NIS2/DORA
├── pipeline/            # граф LangGraph
├── config_assist/       # expand_question
├── presets/             # intelligent_automation.yaml · cybersecurity.yaml
├── evals/               # золотой набор, раннер, метрики, отчёт
├── local.py             # файловый кэш LLM и учёт для CLI, загрузка фикстур parser
├── testing/             # фейки портов, FakeLLM, фабрики — для тестов ai и core
└── cli.py
```

## Подключение в core (чек-лист P1)

**Worker, при старте** (`leadradar_core/worker/deps.py`):

```python
from leadradar_ai import (AnalysisDeps, AISettings, FastEmbedder, GeminiClient, LLMSettings,
                          build_analysis_graph, run_analysis, AnalysisPaused)

embedder = FastEmbedder(AISettings().embed_model, cache_dir=AISettings().embed_cache_dir)
embedder.warm_up()                                          # в Docker — на этапе сборки образа
llm = GeminiClient.from_settings(LLMSettings(), usage=SqlUsageSink(...), cache=SqlLLMCache(...))
graph = build_analysis_graph(AnalysisDeps(
    collector=ParserCollector(...), store=SqlAnalysisStore(...), progress=RedisProgressSink(...),
    llm=llm, embedder=embedder, checkpointer=AsyncPostgresSaver(...),
))
```

**Задача `analyze_company`:**

```python
try:
    output = await run_analysis(graph, AnalysisInput(run_id=..., company=..., services=[...], now=...))
except AnalysisPaused as e:        # подкласс QuotaExhausted; готовые услуги уже сохранены, частичный итог — e.output
    ...                            # компания → paused; повторный вызов с тем же входом дорабатывает только паузу
```

`thread_id = "{run_id}:{company_id}"`. Упавший прогон (kill worker) продолжается с чекпоинта тем же вызовом
`run_analysis`. Выход: `scores`, `stats` (`RunStats`), `errors` (`StepError`), `outcomes` (по услугам, со статусом
`done | failed | paused`).

**Порты**, которые реализует core (`ports.py`):

| Порт | Важное |
|---|---|
| `Collector` | `collect` сохраняет документы с дедупом и возвращает документы окна |
| `AnalysisStore` | `save_extraction` переводит старые активные сигналы услуги в `superseded` и сохраняет fingerprint атомарно; `load_signals` — только `active` |
| `ProgressSink` | события стадий; `data` — счётчики для UI |
| `LLMCache` / `UsageSink` | `used_today` считает вызовы за сутки по тихоокеанскому времени, **без** `cache_hit` и `rate_limited` |
| `Embedder` | `FastEmbedder` из пакета |

**Ошибки → статусы:** `AnalysisPaused` / `QuotaExhausted` → paused · `LLMUnavailable` → retry позже ·
`LLMBadRequest`, `ExtractionFailed` → услуга failed (уже внутри графа) · `LLMInputTooLarge` не выходит из графа
(извлечение само делится на части).

**Прочее:**
- Пересчёт при смене настроек: `score_company(company, bundle, active_signals, now)` — чистая функция, ≤ 1 с на
  1 000 компаний. Производные сигналы NIS2/DORA добавляются внутри неё (в БД их нет); для карточки лида —
  `derived_signals(company, bundle, now)`.
- Discovery: `fit_score(company, icp)`.
- Ключевые слова вопроса: `expand_question(llm, bundle, question)` → сохранить `keywords`, `job_titles`,
  `negative_terms`, `keywords_status=ready`; при `QuotaExhausted` оставить `pending` и повторить.
- Сид: `list_presets()`, `load_preset(key)` — шаблон без id (подписи вопросов `label`, `decision_makers`).
  `SIGNAL_CATEGORIES` — подписи категорий для UI.
- Чекпоинтер: если включить `LANGGRAPH_STRICT_MSGPACK`, добавьте модули `leadradar_ai` в разрешённые.

## Промпты и качество

- Любая правка `prompts/*` → новая папка версии (`v2`) и новый `PROMPT_VERSION`: меняются ключ кэша и fingerprint,
  версия пишется в каждый сигнал. Тесты-снимки (`tests/snapshots`) падают при правке без новой версии.
- В few-shot — вымышленные компании; все цитаты в примерах дословные (это проверяет тест).
- Цикл качества: `lr-ai eval --live` → разбор FP (цитаты в отчёте) и FN (причина: нет кандидатов или
  `unclear` модели) → правка промпта или настроек префильтра → новая версия → снова eval.
- Золотой набор: `src/leadradar_ai/evals/golden/mvp.jsonl` (метки) + `companies.yaml` (профили и фикстуры).
  Метки ставим **по содержимому фикстуры**. Сейчас 9 меток из Annex (DHL, Lufthansa); цель — ≥ 100 на ≥ 10 компаниях,
  включая ловушки SAP (вендор) и Orange (омоним).

## Отличия от SPEC и решения, принятые при реализации

| Где | Решение | Почему |
|---|---|---|
| Контракты | Добавлены `Snippet.fetched_at`, `Snippet.meta`, `ChunkIn.id`; описаны `ScoreChange`, `LLMCallRecord`, `RunStats`, `StepError`, `FitResult`; форма `RuleConfig.condition` проверяется по `kind` (+ поле `revenue_eur` для lt/gt) | Свежесть недатированных документов, `headline_only`; кривое правило отклоняется при сохранении, а не роняет пересчёт |
| Скоринг | Пример DHL из §1.7.5 по формуле даёт priority **69.1** (в тексте 69.2) | Intent 81.3 и Risk 38.1 совпадают; округление примера |
| Скоринг | Надёжность источника — из профиля, сигналы — по `question_id` любой версии | Смена настроек применяется при пересчёте |
| LLM-шлюз | `response_json_schema` вместо `response_schema`; лимиты длины — в коде, не в схеме | Полная JSON Schema с `$defs`; лишний термин не тратит вызов на починку |
| LLM-шлюз | Ошибки `LLMUnavailable`, `LLMBadRequest`, `LLMInputTooLarge`; 429 «в сутки» → модель пропускается до полуночи PT | — |
| Эмбеддинги | `multilingual-e5-small` подключена как custom-модель fastembed (встроенной нет); `AI_EMBED_CACHE_DIR` | Встроенная только large (1024-d), а в БД `vector(384)` |
| Префильтр | BM25 свой (без `rank-bm25`) со стоп-словами en/de/fr; окно ±300 символов для фильтра сущности — из соседних фрагментов (порт не менялся); при нехватке бюджета защищён лучший фрагмент каждого вопроса (H — два) | Без стоп-слов «the/is» делали любой фрагмент кандидатом |
| Префильтр | `min_cosine = 0.78` почти на границе у e5 (0.74–0.82 на примерах) | Подобрать на фикстурах P2 |
| Верификация | Нечёткая цитата должна содержать все числа исходной; цитата ≥ 12 символов; цитата из заголовка — флаг `headline_only`; V2 — только поле `subject` (упоминание имени уже проверил префильтр) | «cut 4,000 jobs» не должно пройти по «3,000» |
| Промпт | Без сегодняшней даты | Иначе ключ кэша менялся бы каждый день |
| Граф | Квота кончилась → пауза **только этой услуги**, остальные доделываются; `run_analysis` бросает `AnalysisPaused` | Исключение отменяло параллельные ветки вместе с уже оплаченным вызовом LLM |
| Граф | `resolve` / `collect` после повторов деградируют (исходный профиль, ранее собранные документы); первый узел сбрасывает накопительные каналы | Сбой GDELT не должен останавливать анализ; повторный прогон на том же thread |
| Пресеты | Вендор кибербезопасности — по тегу `cybersecurity_vendor`; в IA добавлено правило «distress → priority ≤ 35» | Отдельной индустрии в таксономии нет |
| expand | Не больше 6 языков по ICP (en всегда); остальные — через `languages=` | ICP на всю Европу — ~25 языков |
| Производные | NIS2 — weak, DORA — moderate; считаются в `score_company`, не хранятся | Фирмографика — контекст, а не триггер покупки |
