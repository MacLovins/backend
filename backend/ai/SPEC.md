# backend/ai — ТЗ (`leadradar-ai`)

> **Роль:** AI-движок: граф анализа компании (LangGraph), LLM-шлюз Gemini, префильтр, извлечение сигналов
> с цитатами, их проверка кодом, объяснимый скоринг, пресеты услуг, оценка качества.
> **Владелец:** P3 — AI engineer · **Потребитель:** `backend/backend` (worker, пересчёт, конфигурация)
> **Связано:** [ARCHITECTURE.md](../../ARCHITECTURE.md) §3.3–3.5 (сигналы, скоринг, пресеты), §4.5 (потоки), §4.7 (контракты), §4.8 (квоты Gemini)
> **Закрывает:** K1, K2, K3, K5 · S2–S5, S11–S13, S16, S17 · A1–A6

---

## 0. Как пользоваться этим файлом

- Для человека: §1.3 — что делать, §1.9 — как понять, что сделано. Формулы скоринга — §1.7.5, они же в тестах.
- Для агента (Claude Code): давай ему одну функцию из §1.3 плюс ссылки на §1.4 (контракты) и §1.7 (алгоритм).
  Проверка — команда из колонки «Проверка». Пакет **не импортирует** `leadradar_parser`, `leadradar_core` и
  `leadradar_auth` (import-linter). Всё внешнее приходит через порты (§1.4.3).

---

## 1. Этап 1 — MVP

### 1.1 Цель

По компании и набору услуг получить **проверенные сигналы с дословными цитатами** и **объяснимый скор**:
Fit, Intent, Risk → Priority, Tier, «почему сейчас». Не больше 1–2 вызовов Gemini на пару компания-услуга
в рамках бесплатного тарифа. Скоринг — чистая функция: пересчитывается мгновенно при смене настроек.

### 1.2 Границы

| Входит | Не входит (кто делает / когда) |
|---|---|
| Граф анализа, LLM-шлюз, префильтр, извлечение, верификация, скоринг, пресеты, ключевые слова вопросов, evals, CLI | Хранение в БД — core через порт `AnalysisStore` |
| Контракты входа и выхода, порты | Сбор данных из сети — parser через порт `Collector` (реализует core) |
| Локальные эмбеддинги (CPU) | HTTP API, очередь, SSE — core |
| P1: подтверждение несколькими источниками, производные NIS2 / DORA, подсказка вопросов, классификация индустрии | Outreach — надстройка после ядра (AI-19); агент-исследователь, ML-скоринг — этап 2 |

### 1.3 Функции

| ID | Функция | Пр. | Проверка |
|---|---|---|---|
| AI-01 | Контракты (`contracts.py`) и порты (`ports.py`) по §1.4 | P0 | `pytest tests/test_contracts.py` |
| AI-02 | LLM-шлюз `GeminiClient`: structured output по Pydantic-схеме, пулы моделей `main`/`cheap`, лимитер RPM/RPD, ретраи, fallback, кэш, учёт вызовов, одна попытка починки JSON (§1.7.6) | P0 | `pytest -k llm_gateway` (фейковый транспорт: 429 → fallback, кэш-хит) |
| AI-03 | Нарезка документов на snippets 120–220 слов с перекрытием в 1 предложение; у новостей и вакансий заголовок идёт префиксом | P0 | `pytest -k chunking` |
| AI-04 | `FastEmbedder`: `intfloat/multilingual-e5-small` (384-d, CPU), префиксы `query:` и `passage:`, батчи | P0 | `pytest -k embed` (кэш модели в CI) |
| AI-05 | Префильтр: BM25 + косинус → RRF(k=60), фильтры источника, свежести и сущности, разнообразие, бюджет ≤ 40 фрагментов и ≤ 25k токенов на услугу (§1.7.2) | P0 | `pytest -k prefilter` на фикстурах DHL |
| AI-06 | Промпт извлечения `extract_signals@v1`: system (роль, правила, рубрика, формат, 3 примера) + user (контекст → вопросы → задача), схема `ExtractionOutput` (§1.7.3) | P0 | Снимок промпта в тесте, `lr-ai analyze --live` |
| AI-07 | Узел `extract`: 1 вызов на услугу (2, если бюджет превышен); пропуск, если fingerprint не изменился; вопрос без кандидатов получает `no` без LLM | P0 | `pytest -k extract_node` (FakeLLM считает вызовы) |
| AI-08 | Верификация V1–V4: цитата, субъект, свежесть, согласованность; кап — топ-3 evidence на вопрос (§1.7.4) | P0 | `pytest -k verification` (≥ 15 кейсов) |
| AI-09 | Скоринг: `fit_score`, `evaluate_rules`, `score_company` → `LeadScore` с breakdown и `why_now` (§1.7.5) | P0 | `pytest -k scoring` (золотые числа + свойства монотонности) |
| AI-10 | Граф LangGraph `analysis`: узлы, fan-out по услугам через `Send`, `RetryPolicy`, чекпоинтер из DI, события прогресса (§1.7.1) | P0 | `pytest -k graph_e2e` (фейки + `MemorySaver`) |
| AI-11 | `expand_question`: мультиязычные ключевые слова, должности, стоп-термины (модель `cheap`) (§1.7.7) | P0 | `pytest -k expand` (FakeLLM), `lr-ai expand --live` |
| AI-12 | Пресеты `intelligent_automation.yaml`, `cybersecurity.yaml` (ARCHITECTURE §3.5) + загрузчик и валидация | P0 | `pytest -k presets` |
| AI-13 | Evals: формат золотого набора, раннер, метрики precision / recall / F1, доля галлюцинаций, доля воздержаний (§1.7.8) | P0 | `lr-ai eval --cache-only` выдаёт отчёт |
| AI-14 | CLI `lr-ai`: `analyze` (по JSONL-фикстуре parser), `eval`, `expand`, `presets` | P0 | `lr-ai --help` |
| AI-15 | Подтверждение несколькими источниками (V5): кластеризация одинаковых событий, флаг `corroborated` | P1 | `pytest -k corroboration` |
| AI-16 | Производные сигналы без LLM: вероятный скоуп NIS2 и DORA по фирмографике | P1 | `pytest -k derived` |
| AI-17 | `suggest_questions`: 8–12 черновиков вопросов, 2 негатива и 2 правила из описания услуги и ICP | P1 | `lr-ai suggest --live` |
| AI-18 | `classify_industry`: id индустрий по тексту сайта для компаний без индустрии (модель `cheap`) | P1 | `pytest -k classify` |
| AI-19 | `generate_outreach`: черновики email и LinkedIn InMail по сигналам и value proposition, каждое утверждение со ссылкой на сигнал | Надстройка | После DoD ядра (ARCHITECTURE §4.13) |
| AI-20 | LLM-судья для сигналов с весом H — только при запасе квоты | P2 | — |

### 1.4 Входы и выходы

#### 1.4.1 Публичный API (`leadradar_ai/__init__.py`) — единственное, что импортирует core

```python
from leadradar_ai import (
    # граф анализа
    build_analysis_graph, AnalysisDeps, AnalysisInput, QuotaExhausted,
    # скоринг — чистые функции
    score_company, fit_score, evaluate_rules,
    # LLM-помощники конфигурации
    expand_question, suggest_questions, classify_industry,
    # инфраструктура
    GeminiClient, LLMSettings, AISettings, FastEmbedder,
    # пресеты и evals
    list_presets, load_preset, run_eval,
    # контракты (реэкспорт)
    CompanyProfile, ServiceBundle, QuestionConfig, ICPConfig, Criterion, RuleConfig, ScoringProfile,
    AnalysisDocument, Snippet, ChunkIn, VerifiedSignal, StoredSignal, RejectedEvidence,
    Contribution, Reason, LeadScore, ScoreChange, ProgressEvent, LLMCallRecord, CollectRequest,
    # порты (реэкспорт) — их реализует core в leadradar_core/adapters/
    Collector, AnalysisStore, ProgressSink, LLMCache, UsageSink, Embedder,
)
```

#### 1.4.2 Контракты (`contracts.py`, сокращённо; поля только добавляются)

```python
SourceType = Literal["news", "website", "jobs", "report", "registry", "incident", "derived", "manual"]
Weight = Literal["high", "medium", "low"]; Polarity = Literal["positive", "negative"]
Strength = Literal["weak", "moderate", "strong"]; Tier = Literal["hot", "warm", "cold", "disqualified"]

class CompanyProfile(BaseModel):
    id: UUID; name: str; domain: str; aliases: list[str] = []
    own_domains: list[str] = []            # домены компании и её ATS — для фильтра сущности
    country_code: str | None = None; industry_ids: list[str] = []
    employees: int | None = None; revenue_eur: int | None = None; tags: list[str] = []

class QuestionConfig(BaseModel):
    id: UUID; key: str; version: int; text: str; category: str
    polarity: Polarity; weight: Weight; source_types: set[SourceType]; recency_days: int
    keywords: dict[str, list[str]] = {}    # {"en": [...], "de": [...]}
    job_titles: list[str] = []; negative_terms: list[str] = []

class Criterion(BaseModel):                # nice-to-have ICP
    kind: Literal["country_in", "industry_in", "employees_between", "revenue_at_least", "tag_in"]
    values: list[str | int]; weight: float = 1.0

class ICPConfig(BaseModel):
    countries: list[str] = []; industries_any: list[str] = []
    employees_min: int | None = None; employees_max: int | None = None; revenue_min_eur: int | None = None
    nice_to_have: list[Criterion] = []

class RuleConfig(BaseModel):
    id: UUID; name: str; kind: Literal["firmographic", "signal", "list"]
    condition: dict                        # §1.7.5, «Правила»
    action: Literal["exclude", "cap", "flag"]; cap_value: float | None = None

class ScoringProfile(BaseModel):           # значения по умолчанию — ARCHITECTURE §3.4
    id: UUID; version: int
    weights: dict[Weight, float] = {"high": 3, "medium": 2, "low": 1}
    strength_values: dict[Strength, float] = {"weak": 0.35, "moderate": 0.65, "strong": 1.0}
    reliability: dict[str, float] = {"website": 1.0, "report": 1.0, "jobs": 0.9, "incident": 0.9,
                                     "registry": 0.9, "news": 0.8, "derived": 0.7, "headline_only": 0.6}
    half_life_days: dict[str, int | None] = {"jobs": 45, "news": 120, "website": 240, "report": 365,
                                             "incident": 270, "registry": None, "derived": None}
    tau_intent: float = 3.0; tau_risk: float = 2.0
    fit_exponent: float = 0.4; intent_exponent: float = 0.6; risk_penalty: float = 0.5
    tiers: dict[str, float] = {"hot": 65, "warm": 40}; min_confidence: float = 0.5
    max_evidence_per_question: int = 3

class ServiceBundle(BaseModel):
    service_id: UUID; key: str; name: str; description: str; value_proposition: str = ""
    questions: list[QuestionConfig]; icp: ICPConfig; rules: list[RuleConfig]; scoring: ScoringProfile

class AnalysisDocument(BaseModel):
    id: UUID; source_type: SourceType; source_name: str; url: str; title: str | None
    text: str; published_at: datetime | None; fetched_at: datetime; language: str | None; meta: dict = {}

class Snippet(BaseModel):
    id: str                                # короткий id для промпта: "S12"
    chunk_id: UUID; document_id: UUID; text: str; char_start: int; char_end: int
    source_type: SourceType; source_name: str; url: str; title: str | None
    published_at: datetime | None; language: str | None; embedding: list[float] | None = None

class VerifiedSignal(BaseModel):
    question_id: UUID; question_key: str; question_version: int; category: str; polarity: Polarity
    document_id: UUID; chunk_id: UUID | None; url: str; source_type: SourceType; source_name: str
    quote: str; quote_start: int | None; quote_end: int | None   # смещения в document.text
    summary: str; strength: Strength; confidence: float; reliability: float
    event_date: date | None; published_at: datetime | None
    flags: set[str] = set()                # fuzzy_quote | headline_only | undated | corroborated | derived
    model: str | None; prompt_version: str | None

class StoredSignal(VerifiedSignal):
    id: UUID; detected_at: datetime; status: Literal["active", "rejected_by_user"] = "active"

class RejectedEvidence(BaseModel):
    question_id: UUID; snippet_id: str; quote: str
    reason: Literal["quote_not_found", "wrong_subject", "stale", "below_confidence", "no_evidence_for_yes"]

class Contribution(BaseModel):
    question_id: UUID; key: str; label: str; polarity: Polarity; weight: float
    strength: float; points: float; signal_ids: list[UUID]

class Reason(BaseModel):
    text: str; polarity: Polarity | Literal["fit", "data_gap"]
    signal_id: UUID | None = None; source_name: str | None = None; url: str | None = None; date: date | None = None

class LeadScore(BaseModel):
    company_id: UUID; service_id: UUID; scoring_profile_id: UUID
    fit: float; intent: float; risk: float; priority: float; tier: Tier; disqualified: bool
    rule_hits: list[dict]; fit_details: list[dict]; breakdown: list[Contribution]
    why_now: list[Reason]; data_gaps: list[str]; computed_at: datetime

class ProgressEvent(BaseModel):
    run_id: UUID; company_id: UUID; service_id: UUID | None = None
    stage: Literal["resolving", "collecting", "indexing", "prefiltering", "extracting",
                   "verifying", "scoring", "done", "failed", "paused"]
    status: Literal["started", "progress", "done", "failed", "paused"]; message: str; data: dict = {}
```

#### 1.4.3 Порты (`ports.py`) — реализует core в `leadradar_core/adapters/`

```python
class Collector(Protocol):
    async def resolve(self, company: CompanyProfile) -> CompanyProfile: ...
    async def collect(self, company: CompanyProfile, request: CollectRequest) -> list[AnalysisDocument]: ...
    # core: parser.collect → сохранить с дедупом → вернуть документы окна (новые + собранные ранее)

class AnalysisStore(Protocol):
    async def documents_without_chunks(self, company_id: UUID) -> list[AnalysisDocument]: ...
    async def save_chunks(self, chunks: list[ChunkIn]) -> None: ...
    async def load_snippets(self, company_id: UUID, since: datetime, source_types: set[SourceType]) -> list[Snippet]: ...
    async def get_fingerprint(self, company_id: UUID, service_id: UUID) -> str | None: ...
    async def save_extraction(self, run_id: UUID, company_id: UUID, service_id: UUID, fingerprint: str,
                              signals: list[VerifiedSignal], rejected: list[RejectedEvidence]) -> None: ...
    async def load_signals(self, company_id: UUID, service_id: UUID) -> list[StoredSignal]: ...
    async def save_score(self, run_id: UUID | None, score: LeadScore) -> ScoreChange: ...  # tier до/после

class ProgressSink(Protocol):
    async def emit(self, event: ProgressEvent) -> None: ...

class LLMCache(Protocol):
    async def get(self, key: str) -> dict | None: ...
    async def set(self, key: str, value: dict, meta: dict) -> None: ...

class UsageSink(Protocol):
    async def record(self, call: LLMCallRecord) -> None: ...
    async def used_today(self, model: str) -> int: ...   # сутки — по тихоокеанскому времени (как квоты Gemini)

class Embedder(Protocol):
    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...

class CollectRequest(BaseModel):
    source_types: set[SourceType]; since: datetime
    news_topics: list[str] = []            # ключевые слова позитивных вопросов → фокус запросов новостей
    job_keywords: list[str] = []           # должности и навыки → поиск в ATS (Workday, Adzuna)
    max_items_per_source: int = 50
```

#### 1.4.4 Вход и выход графа

```python
class AnalysisInput(BaseModel):
    run_id: UUID; company: CompanyProfile; services: list[ServiceBundle]
    mode: Literal["full", "incremental"] = "incremental"   # incremental: без LLM, если нечего проверять
    now: datetime

# выход: dict с ключами scores: list[LeadScore], stats: RunStats, errors: list[StepError]
# побочные эффекты — только через порты (store, progress, cache, usage)
```

### 1.5 Зависимости

| Тип | Что |
|---|---|
| Python | `langgraph` (≥ 1.0), `langgraph-checkpoint-postgres` (чекпоинтер создаёт core), `google-genai`, `pydantic` v2, `fastembed`, `rank-bm25`, `rapidfuzz`, `numpy`, `unidecode`, `pyyaml`, `jinja2`, `typer` |
| Внешние | Gemini API (free tier): `GEMINI_API_KEY`; модель эмбеддингов скачивается при первом запуске (~120 МБ), в Docker — прогрев на этапе сборки |
| От других папок | Ничего. Фикстуры документов для разработки — JSONL из `lr-parser collect` (P2 отдаёт к H6) |
| Для других папок | core: публичный API §1.4.1; frontend: ключи и подписи категорий через OpenAPI core |

Настройки (`AISettings`, `LLMSettings`, префикс `AI_` / `LLM_`):

```dotenv
GEMINI_API_KEY=
LLM_MAIN_MODELS=gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash     # порядок = fallback; проверить в AI Studio (H0)
LLM_CHEAP_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite
LLM_LIMITS_JSON={"gemini-3.8-flash":{"rpm":10,"rpd":250},"gemini-3.5-flash-lite":{"rpm":15,"rpd":1000}}  # ПРИМЕР — заменить реальными
LLM_TIMEOUT_S=90
LLM_MAX_INPUT_TOKENS=30000
LLM_CACHE_ENABLED=true
AI_EMBED_MODEL=intfloat/multilingual-e5-small
AI_TOPK_PER_QUESTION=5
AI_MAX_SNIPPETS_PER_SERVICE=40
LANGSMITH_TRACING=false                                               # опционально: трейсы графа (бесплатный Developer-план)
```

### 1.6 Структура папки

```
backend/ai/
├── SPEC.md
├── pyproject.toml                    # name = "leadradar-ai"; scripts: lr-ai = "leadradar_ai.cli:app"
├── src/leadradar_ai/
│   ├── __init__.py                   # публичный API §1.4.1
│   ├── settings.py                   # AISettings, LLMSettings (pydantic-settings)
│   ├── contracts.py                  # §1.4.2
│   ├── ports.py                      # §1.4.3
│   ├── errors.py                     # QuotaExhausted, ExtractionFailed
│   ├── llm/        gemini.py · limiter.py · cache_key.py · records.py
│   ├── prompts/    loader.py
│   │   ├── extract_signals/v1/  system.md · user.jinja · examples.json
│   │   ├── expand_question/v1/  system.md · user.jinja
│   │   └── suggest_questions/v1/ … (P1)
│   ├── pipeline/   graph.py · state.py · nodes/{resolve,collect,index,prefilter,extract,verify,score,finalize}.py
│   ├── retrieval/  chunking.py · embed.py · bm25.py · hybrid.py · entity.py · budget.py
│   ├── verification/ quotes.py · subject.py · recency.py · consistency.py · corroboration.py (P1)
│   ├── scoring/    engine.py · fit.py · rules.py · decay.py · explain.py · derived.py (P1)
│   ├── config_assist/ expand.py · suggest.py (P1) · classify.py (P1)
│   ├── presets/    intelligent_automation.yaml · cybersecurity.yaml · loader.py
│   ├── evals/      golden/mvp.jsonl · runner.py · metrics.py · report.py
│   ├── testing/    fakes.py (FakeLLM, InMemoryStore, FakeCollector, ListProgressSink)
│   └── cli.py
└── tests/
    ├── fixtures/   dhl.jsonl · lufthansa.jsonl · sap.jsonl (ловушка «вендор») · orange.jsonl (ловушка омонима)
    └── test_*.py
```

### 1.7 Алгоритмы и ключевые решения

#### 1.7.1 Граф `analysis` (LangGraph, паттерны prompt chaining + parallelization, P1 и P2 из ARCHITECTURE §2)

```
START → resolve → collect → index ──Send(по каждой услуге)──▶ prefilter → extract → verify → score ──▶ finalize → END
                                                                  │
                            fingerprint не изменился (incremental) └──────────────▶ score (на старых сигналах)
```

| Узел | Делает | Политики |
|---|---|---|
| `resolve` | `Collector.resolve`: домен, own_domains, careers/ATS, фирмографика (если не заполнены) | `RetryPolicy(max_attempts=3)` на сетевых ошибках |
| `collect` | `CollectRequest` из вопросов всех услуг (объединение источников, самое длинное окно, `news_topics`, `job_keywords`) → `Collector.collect` | `RetryPolicy(max_attempts=2)`; частичный результат — не ошибка |
| `index` | Документы без фрагментов → нарезка → эмбеддинги → `store.save_chunks` | CPU; батчи по 64 |
| `prefilter` | По услуге: `store.load_snippets` → кандидаты по вопросам (§1.7.2) → fingerprint | — |
| `extract` | 0–2 вызова LLM на услугу (§1.7.3) | Ретраи внутри шлюза; `QuotaExhausted` → стадия `paused` |
| `verify` | V1–V4 (+ V5 в P1) → `VerifiedSignal[]` + `RejectedEvidence[]` → `store.save_extraction` | — |
| `score` | `store.load_signals` + производные (P1) → `score_company` → `store.save_score` | Чистая функция |
| `finalize` | Статистика: документы по источникам, вызовы LLM, длительности → `ProgressEvent(done)` | — |

- **Состояние маленькое:** id, счётчики, кандидаты-фрагменты (≤ 40 на услугу). Тексты документов в состояние не кладём:
  чекпоинты не раздуваются.
- **Чекпоинтер** передаёт core (`AsyncPostgresSaver`), `thread_id = f"{run_id}:{company_id}"`. Сохраняем после
  каждого шага, чтобы продолжить после падения worker.
- **Прогресс:** каждый узел вызывает `ProgressSink.emit` на старте и в конце (плюс промежуточные события
  `collect`: «новостей 23, страниц 12, вакансий 41»).
- Ошибка в одной услуге не роняет другие: `StepError` пишется в выход, стадия услуги — `failed`.

#### 1.7.2 Префильтр — «минимум высокосигнальных токенов» (P12, P14)

1. Для каждого вопроса оставляем фрагменты, у которых `source_type ∈ question.source_types` и дата
   (`published_at`, иначе `fetched_at` с флагом `undated`) ≥ `now − recency_days`.
2. **Фильтр сущности.** Если документ со стороннего домена (новости, чужие отчёты), в заголовке или в окне ±300 символов
   вокруг фрагмента должно быть имя или алиас компании (граница слова, без учёта регистра и диакритики).
   Домены из `own_domains`, ATS компании и записи HIBP по домену проходят без проверки.
3. **Запрос вопроса:** `"query: " + text + " " + ключевые слова` (языки рынков ICP).
   BM25 по токенам фрагментов (casefold + unidecode) и косинус к эмбеддингу запроса.
4. **RRF:** `score = Σ 1/(60 + rank)`, по 20 лучших из каждого списка → top-k (`AI_TOPK_PER_QUESTION = 5`).
   Отсекаем, если BM25 = 0 и косинус < `min_cosine` (по умолчанию 0.78, подбирается на фикстурах).
5. **Разнообразие и бюджет:** ≤ 2 фрагмента из одного документа на вопрос; объединение по услуге ≤ 40 фрагментов и
   ≤ 25k токенов (оценка: 4 символа ≈ 1 токен). Не влезло → выкидываем фрагменты с наименьшим RRF, а вопросы с весом H
   защищены минимумом в 2 фрагмента.
6. Вопрос без кандидатов сразу получает `no` (без LLM). Если кандидатов нет ни у одного вопроса — извлечения нет вообще.
7. **Fingerprint** = sha256(отсортированные `question.id@version` + id фрагментов-кандидатов + `PROMPT_VERSION`).
   В режиме `incremental` совпадение с сохранённым fingerprint → пропускаем `extract` и `verify`.

#### 1.7.3 Промпт извлечения `extract_signals@v1` (P11, P13)

Структура по рекомендациям Google для Gemini 3.x: роль, правила и формат — в system instruction;
длинный контекст — первым в user; задача — в самом конце; единый формат разделителей (XML-теги);
few-shot той же структуры, что и ответ. `temperature`, `top_p`, `top_k` **не задаём** (у Gemini 3.x это вызывает зацикливание).

`system.md` (каркас, текст промпта — английский):

```text
<role>You are a senior B2B sales research analyst at an IT services provider. You judge public evidence
about ONE target company for ONE service line.</role>
<rules>
1. Use ONLY the snippets inside <snippets>. Do not use outside knowledge.
2. Every answer "yes" needs at least one evidence item: snippet_id + a quote copied VERBATIM from that snippet
   (max 300 characters, original language, no paraphrasing, no "...").
3. Evidence must be about the target company itself (subject = target_company). Customers, partners, competitors,
   the industry in general, or a different company with a similar name are not evidence.
4. If the target company SELLS such services or products (it is a vendor), that is not a buying signal.
5. "no" = snippets show the opposite; "unclear" = indirect or insufficient evidence. Prefer "unclear" to guessing.
6. strength: strong = explicit and specific (named program, budget, target, date, open roles, appointed person);
   moderate = stated plan or intent without specifics; weak = generic or marketing language.
7. event_date: date of the event if stated, else the snippet's published date, else null.
   A future target year ("by 2030") is not an event date.
8. summary: one plain-English sentence a salesperson understands. No jargon.
9. Questions with polarity="negative" describe blockers; answer "yes" when the blocker is present.
</rules>
<output_format>JSON that matches the provided schema. Exactly one answer per question id.</output_format>
<examples>
  <!-- 3 примера с одинаковой структурой: (1) полное попадание в стиле DHL (Strategy 2030, agentic AI в обработке RFQ)
       + негатив tech_partners («использует и внутреннюю разработку, и сторонние AI-решения»);
       (2) ловушка «вендор»: пресс-релиз, где целевая компания продаёт RPA → unclear;
       (3) ловушка омонима: новость про другую «Orange» → evidence subject=other_company, ответ unclear. -->
</examples>
```

`user.jinja` (порядок важен — контекст, потом вопросы, потом задача):

```text
<company>{{ name }} · {{ domain }} · aliases: {{ aliases }} · {{ country }} · {{ industries }} · employees: {{ employees }}</company>
<service>{{ service.name }}: {{ service.description }}</service>
<snippets>
{% for s in snippets %}<snippet id="{{ s.id }}" source_type="{{ s.source_type }}" source="{{ s.source_name }}"
 published="{{ s.published_at or 'unknown' }}" title="{{ s.title }}">{{ s.text }}</snippet>
{% endfor %}</snippets>
<questions>
{% for q in questions %}<question id="{{ q.pid }}" polarity="{{ q.polarity }}" category="{{ q.category }}">{{ q.text }}</question>
{% endfor %}</questions>
Based on the snippets above, answer every question in <questions> following the rules. Return JSON only.
```

Схема ответа (`response_schema`):

```python
class Evidence(BaseModel):
    snippet_id: str
    quote: str = Field(max_length=400, description="Verbatim substring of the snippet")
    subject: Literal["target_company", "other_company", "industry_general"]
    event_date: date | None
    strength: Literal["weak", "moderate", "strong"]
    summary: str = Field(max_length=240)

class Answer(BaseModel):
    question_id: str                       # "Q1".."Qn" — короткие id, маппинг на UUID в коде
    answer: Literal["yes", "no", "unclear"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list, max_length=3)
    rationale: str = Field(max_length=300)

class ExtractionOutput(BaseModel):
    answers: list[Answer]
```

Правила версионирования: любая правка промпта или примеров → новая версия (`v2`), `PROMPT_VERSION` меняется,
кэш и fingerprint инвалидируются, evals перезапускаются. Версия пишется в каждый сигнал.

#### 1.7.4 Верификация (P13) — решает код, а не LLM

| Шаг | Правило | Иначе |
|---|---|---|
| V1 Цитата | Нормализация (NFKC, casefold, пробелы, кавычки, тире, многоточия) → точное вхождение во фрагмент → смещения в `document.text`. Если нет — `rapidfuzz.fuzz.partial_ratio ≥ 90` с выравниванием → флаг `fuzzy_quote`, confidence × 0.9 | `quote_not_found` |
| V2 Субъект | `subject == target_company`. Для сторонних документов имя или алиас компании должны быть в заголовке или в окне ±300 символов | `wrong_subject` |
| V3 Свежесть | `event_date` (если > сегодня + 7 дн. → берём `published_at`) или `published_at` / `fetched_at` в пределах `recency_days` | `stale` |
| V4 Согласованность | `yes` без валидного evidence → `unclear`. Evidence у `no` / `unclear` игнорируется | `no_evidence_for_yes` |
| Порог | `confidence ≥ profile.min_confidence` | `below_confidence` |
| Кап | На вопрос берём ≤ `max_evidence_per_question` (3) evidence с наибольшим вкладом — против накрутки одним событием | — |
| V5 (P1) | Кластер «одно событие»: косинус summary ≥ 0.85 и даты ±14 дн. → главный сигнал + `corroborated`; в noisy-OR кластер идёт одним evidence с confidence = 1 − Π(1 − cᵢ), не выше 0.98 | — |

Отклонённое сохраняется (`RejectedEvidence`): это метрика галлюцинаций и материал для разбора FP.
В UI отклонённое не показывается.

#### 1.7.5 Скоринг (P15) — чистая детерминированная функция

```
v_e   = strength_values[e.strength] × e.confidence × rel(e) × decay(e)
rel(e)= reliability[e.source_type] (или reliability["headline_only"] при флаге headline_only)
decay = 0.5 ** (age_days / half_life_days[source_type])      # None → 1.0; age от event_date, иначе published_at
s_q   = 1 − Π_{e ∈ top3(q)} (1 − v_e)                         # noisy-OR
points_q = weights[q.weight] × s_q
Intent   = 100 × (1 − exp(−Σ_{q:+} points_q / tau_intent))
Risk     = 100 × (1 − exp(−Σ_{q:−} points_q / tau_risk))
Fit      = 0, если провален must-have; иначе 100 × Σ(вес совпавших nice-to-have) / Σ(весов)   (неизвестно → 0.5 веса + data_gap)
           без nice-to-have → 100
Priority = 100 × (Fit/100)^fit_exponent × (Intent/100)^intent_exponent × (1 − risk_penalty × Risk/100)
Правила: exclude → tier=disqualified, priority=0 · cap → priority=min(priority, cap_value) · flag → предупреждение
Tier     = disqualified | hot (≥ tiers.hot) | warm (≥ tiers.warm) | cold
Сортировка: priority ↓, intent ↓, fit ↓, name ↑.  Округление до 0.1.
```

**Must-have** (`ICPConfig`): `countries` (пусто — любые), `industries_any`, `employees_min/max`, `revenue_min_eur`.
Неизвестное значение must-have → проходит, но попадает в `data_gaps`.

**Правила** (`RuleConfig.condition`):
- `firmographic`: `{"field": "employees" | "country_code" | "industry_ids" | "domain" | "tags", "op": "lt" | "gt" | "eq" | "in" | "not_in" | "intersects", "value": ...}`
- `signal`: `{"question_key": "ia_distress", "min_strength": 0.5}` — срабатывает по `s_q`
- `list`: `{"domains": ["client1.com", "competitor.com"]}` — существующие клиенты и конкуренты

**Почему сейчас** (`why_now`): топ-3 позитивных вклада по `points` → сильнейший сигнал каждого → `Reason(text=summary,
source, date, url)`. Плюс главный негатив, если Risk ≥ 20. Плюс заметки по Fit (не прошёл must-have, пробелы в данных).

Пример `breakdown` (DHL, IA; числа иллюстративные):

```json
{"fit": 92.0, "intent": 81.3, "risk": 38.1, "priority": 69.2, "tier": "hot",
 "breakdown": [
  {"key": "ia_ai_projects", "polarity": "positive", "weight": 3, "strength": 0.83, "points": 2.49},
  {"key": "ia_dt",          "polarity": "positive", "weight": 2, "strength": 0.61, "points": 1.22},
  {"key": "ia_hiring",      "polarity": "positive", "weight": 3, "strength": 0.44, "points": 1.32},
  {"key": "ia_inhouse",     "polarity": "negative", "weight": 2, "strength": 0.48, "points": 0.96}],
 "why_now": [
  {"text": "Uses agentic AI to process customer RFQs as part of Strategy 2030.", "source_name": "dhl.com", "date": "2026-06-18"},
  {"text": "Hiring 6 automation and AI engineers in Germany and Czechia.", "source_name": "Workday", "date": "2026-09-10"},
  {"text": "Risk: large in-house automation capability and third-party AI partners.", "polarity": "negative"}]}
```

Обязательные тесты-свойства: больше силы или уверенности позитива → Priority не падает; негатив → не растёт;
старее → вклад не растёт; Fit = 0 → Priority = 0; `exclude` → `disqualified`; пересчёт 1 000 компаний × 11 вопросов × 3
сигнала ≤ 1 с.

#### 1.7.6 LLM-шлюз `GeminiClient` (ARCHITECTURE §4.8)

- Вызов: `await client.aio.models.generate_content(model=m, contents=user, config=types.GenerateContentConfig(
  system_instruction=system, response_mime_type="application/json", response_schema=OutputModel))`.
  Результат — `response.parsed` (или `OutputModel.model_validate_json(response.text)`), токены — `response.usage_metadata`.
  Если в актуальном SDK рекомендован другой способ (например, Interactions API с `response_format`), меняем только
  этот адаптер: интерфейс `LLMClient.generate(request) -> LLMResult[T]` остаётся прежним.
- **Пулы:** `main` (извлечение, подсказки) и `cheap` (ключевые слова, классификация). Модель выбирается первой из пула,
  у которой есть бюджет RPM и RPD.
- **Лимитер:** скользящее окно RPM в процессе (worker один) + `UsageSink.used_today(model)` для RPD.
- **Ошибки:** `google.genai.errors.APIError`: 429 → ждём retry-delay или backoff 5 → 15 → 45 с, потом следующая модель;
  5xx / 503 → 3 ретрая; 400 → без ретрая, в лог. Пул исчерпан → `QuotaExhausted`.
  `finish_reason` SAFETY / RECITATION → все ответы вызова считаются `unclear`, событие в лог.
- **Починка JSON:** при `ValidationError` — одна повторная попытка с текстом ошибки валидации.
- **Кэш:** ключ `sha256(prompt_version | system | contents | schema_json)` → `LLMCache`. В записи — модель и токены.
- **Учёт:** каждый вызов → `UsageSink.record(purpose, model, prompt_version, input_tokens, output_tokens, latency_ms,
  cache_hit, status)`.
- **Бюджет входа:** ≤ `LLM_MAX_INPUT_TOKENS`. Больше → префильтр делит вопросы услуги на 2 вызова.
- Thinking: для `cheap`-задач — минимальный уровень рассуждений (если модель поддерживает параметр),
  для извлечения — по умолчанию.

#### 1.7.7 Ключевые слова вопроса `expand_question@v1`

Вход: текст вопроса, категория, услуга, языки рынков ICP (`en` всегда + языки стран ICP; словарь страна → язык в коде).
Выход (схема): `keywords: {lang: [≤ 12 терминов]}`, `job_titles: [≤ 12]`, `negative_terms: [≤ 8]`.
Затравка — `keywords_seed` из пресета. Модель `cheap`, результат кэшируется. Используется в префильтре (BM25, запрос)
и в `CollectRequest` (`news_topics`, `job_keywords`).

#### 1.7.8 Evals (P19)

- **Золотой набор** `evals/golden/mvp.jsonl`, одна строка — одно решение:
  `{"company_domain": "dhl.com", "service": "intelligent_automation", "question_key": "ia_ai_projects",
  "expected": "yes", "evidence_hint": "agentic AI RFQ processing", "source_url": null}`.
  Состав MVP: ≥ 10 компаний × ключевые вопросы (≥ 100 меток). Обязательны Lufthansa и DHL (A5), ловушки:
  вендор (SAP / Celonis / UiPath), омоним (Orange SA против других «Orange»), компания без сигналов.
- **Метрики:** на уровне решения по вопросу — TP / FP / FN / TN → precision, recall, F1 (всего и по категориям);
  доля галлюцинаций = `quote_not_found` / все evidence; доля воздержаний (`unclear`); вызовов LLM на компанию.
- **Режимы:** `--cache-only` (без сети, на сохранённых документах и ответах — для CI) и `--live`.
- **Отчёт:** `evals/reports/<date>-<prompt_version>.md` + JSON. Precision по фидбеку пользователей считает core.

### 1.8 План работ P3 (часы от старта)

| Окно | Задачи |
|---|---|
| H0–H2 | AI-01, каркас пресетов (AI-12), лимиты Gemini из AI Studio → `.env`, договорённость о портах с P1 |
| H2–H8 | AI-02, AI-03, AI-04, AI-05, AI-06, AI-09 (с тестами), AI-10 на фейках; с H6 — фикстуры DHL и Lufthansa от P2 |
| H8–H12 | Реальный Gemini на фикстурах, AI-07, AI-08, интеграция с адаптерами core (вместе с P1) |
| H12–H24 | AI-11, негативные вопросы, AI-13 + золотой набор, итерации промпта на 10 компаниях, P1: AI-16, AI-17 |
| H24–H32 | Разбор FP → `extract_signals@v2`, P1: AI-15, AI-18; помощь с пакетным прогоном |
| H32–H40 | Тесты, отчёт evals для демо, README пакета |
| H36–H44 | Надстройка AI-19 (outreach), если ядро готово |

### 1.9 Критерии готовности (DoD)

- [ ] `uv run --package leadradar-ai pytest` зелёный, **без сети** (FakeLLM, фикстуры, `MemorySaver`).
- [ ] `uv run lr-ai analyze --fixture tests/fixtures/dhl.jsonl --service intelligent_automation --live` печатает сигналы и скор:
      ≥ 3 проверенных позитивных сигнала (AI / автоматизация / цифровизация), ≥ 1 негатив (собственные компетенции или
      сторонние AI-решения), 0 непроверенных цитат среди показанных, ≤ 2 вызова LLM.
- [ ] Повторный запуск той же команды → 0 новых вызовов LLM (кэш).
- [ ] На фикстуре `sap.jsonl` компания помечена правилом «вендор». На `orange.jsonl` чужие новости отсеяны (`wrong_subject`).
- [ ] Тест 429: берётся fallback-модель. Все модели исчерпаны → стадия `paused`, продолжение с чекпоинта работает.
- [ ] `lr-ai eval --golden evals/golden/mvp.jsonl --cache-only` выдаёт отчёт. Цель — precision ≥ 0.8 по `yes`;
      если не достигнута — в отчёте список FP с причинами.
- [ ] Тест производительности пересчёта (1 000 компаний) ≤ 1 с.
- [ ] Контракт импорта: `lint-imports` зелёный (пакет не импортирует workspace-пакеты).
- [ ] Пресеты IA и Cyber валидны и загружаются core-сидом.

### 1.10 Какие критерии закрывает модуль (MVP)

| ID | Как |
|---|---|
| K1 | Фильтр сущности и свежести, рубрика силы, few-shot с ловушками, проверка цитат кодом, негативы, кап evidence, evals + метрика галлюцинаций |
| K2 | Всё управляется конфигом (`QuestionConfig`, `ICPConfig`, `RuleConfig`, `ScoringProfile`); автогенерация ключевых слов; мгновенный пересчёт |
| K3 | LangGraph-воркфлоу, гибридный retrieval, grounded-извлечение, noisy-OR, полураспад по типу источника, матрица Fit × Intent, производные NIS2 / DORA, подсказка вопросов |
| K5 | Чекпоинты, `RetryPolicy`, лимитер и fallback, кэш, идемпотентный инкрементальный режим |
| S2–S5 | Вопросы, веса, негативы, правила, профиль скоринга — входы функций |
| S11, S12, S13, S17 | Нарезка и извлечение из любых `Document`; Intent (readiness) × Fit и Risk (likelihood); категории incident / leadership / tech_stack / compliance; `why_now` + breakdown |
| S16 | LangGraph, LLM, rule-based скоринг с объяснимостью |
| A1–A6 | Узлы графа = шаги Annex; категории A2; атрибуты A3; ЛПР в пресетах (A4); Lufthansa и DHL в evals (A5); формула вместо субъективной оценки (A6) |

---

## 2. Этап 2 — Intelligence + Scoring & ML + Research agent

### 2.1 Цель

Масштабируемый интеллект: тысячи компаний в сутки, точность ≥ 0.9 на сигналах с весом H, вероятность покупки,
обученная на реальных исходах, агентное исследование для топ-аккаунтов, outreach. Всё это на тех же узлах и контрактах.

### 2.2 Функции

| Область | Функции |
|---|---|
| Деплой | Отдельный сервис-воркер графа (очередь `analysis.requested` → события `signals.extracted`, `score.updated`); порты становятся сетевыми клиентами |
| Модели | Роутер: `cheap` для триажа → `main` для извлечения → судья для H-сигналов; платный тариф, Batch API (−50%) для ночных прогонов, кэш контекста для system и примеров |
| Точность | LLM-судья и самосогласованность (2 из 3) для H-сигналов; isotonic-калибровка уверенности на фидбеке; активное обучение (очередь неуверенных сигналов на ручную проверку); precision по источникам с автоотключением слабых |
| ML-скоринг | Признаки: `s_q` по вопросам, критерии Fit, свежесть, разнообразие источников, подтверждения → логистическая регрессия или GBM с монотонными ограничениями → P(встреча / сделка). SHAP-объяснения. Смешивание с правилами `λ = n / (n + 200)`. Метрики: PR-AUC, precision@k, калибровка. Еженедельное переобучение, мониторинг дрейфа, реестр моделей |
| Подсказки весов | «Вопрос X подтверждается в 40% случаев — снизить вес или переписать?» по фидбеку |
| Research agent | Агент LangGraph: инструменты `search` (grounding с Google Search / Brave), `fetch_url`, `jobs_search`, `company_graph`. Бюджет шагов, human-in-the-loop (interrupt), итог — брифинг по аккаунту с цитатами |
| Outreach | Черновики email, LinkedIn InMail, скрипта звонка; value props, привязанные к сигналам; каждое утверждение ссылается на сигнал; язык и тон; A/B-варианты |
| Знания | Кластеризация событий по рынку («волна найма под NIS2 в логистике DE»), граф компания–люди–события, look-alike по выигранным клиентам (pgvector) |
| Длинные документы | LangExtract (несколько проходов) для годовых отчётов |
| Качество | Золотой набор ≥ 500 меток, eval-гейт в CI (precision не падает больше чем на 2 п.п.), датасеты в Langfuse или LangSmith, реестр промптов с A/B |

### 2.3 Входы и выходы (изменения)

- Новые контракты: `ResearchBrief`, `OutreachDraft`, `MLScore` (probability, shap_values, model_version).
- `LeadScore` получает `ml_probability` и `blend_lambda` — поля только добавляются.
- События вместо прямых вызовов: `analysis.requested` → `signals.extracted` → `score.updated`.

### 2.4 Зависимости

Платный Gemini (или несколько провайдеров через тот же `LLMClient`), Langfuse, MLflow (реестр моделей),
feature store (таблицы Postgres), источник исходов — CRM через Integrations service.

### 2.5 Критерии готовности этапа 2

- Precision ≥ 0.9 на сигналах с весом H при recall ≥ 0.7 на золотом наборе ≥ 500 меток.
- ML-скоринг даёт PR-AUC и precision@20 лучше правил на отложенных исходах, калибровка (ECE) ≤ 0.05.
- p95 анализа компании ≤ 60 с; ≥ 10 000 компаний в сутки на Batch API.
- Research agent укладывается в бюджет шагов и отдаёт брифинг, где у каждого утверждения есть цитата.

### 2.6 Критерии, которые усиливает этап 2

K1 (судья, калибровка, активное обучение), K3 (ML, агент, граф знаний, look-alike), K6 (Batch API, стоимость),
S12 (вероятность покупки), S14 (outreach).

---

## 3. Рост и развитие: что заложено в MVP и как расширять

| Заложено в MVP | Зачем | Как растёт на этапе 2 |
|---|---|---|
| Порты `Collector`, `AnalysisStore`, `ProgressSink`, `LLMCache`, `UsageSink`, `Embedder` | ai не знает об инфраструктуре | Реализация порта → сетевой клиент; код узлов не меняется |
| `LLMClient` + пулы `main` / `cheap` + лимитер | Жить в free tier | Добавляются роутер моделей, другие провайдеры, Batch API, судья — это новые реализации того же интерфейса |
| Версии промптов в файлах + `prompt_version` в каждом сигнале | Воспроизводимость, кэш | Реестр промптов, A/B, eval-гейт в CI |
| Скоринг — чистая функция с версионируемым `ScoringProfile` | Мгновенный пересчёт, объяснимость | Правила остаются prior и cold-start; поверх — ML с `blend_lambda` |
| `breakdown` и `why_now` в `LeadScore` | Объяснимость | Добавляются SHAP-вклады ML с тем же форматом |
| `RejectedEvidence` + фидбек в core | Метрики и разбор FP | Калибровка, активное обучение, обучение судьи |
| Граф из узлов с чёткими входами и выходами | Прозрачность | Новые узлы (судья, исследование, outreach) и условные рёбра — без переписывания существующих |
| Категории сигналов как данные | Новые услуги без кода | Новые категории и пресеты под рынки и вертикали |
| Эмбеддинги фрагментов в pgvector | Префильтр | Семантический поиск по всем компаниям, look-alike, кластеризация событий |
| Золотой набор + раннер evals | Измеримость | Непрерывная оценка, датасеты, регрессионный гейт |

---

## 4. Риски и анти-паттерны

- **Не давать LLM считать скор.** Скор, посчитанный моделью, невоспроизводим и не объясним. LLM только извлекает факты.
- **Не отправлять документы целиком и не делать вызов на каждый вопрос** — сгорит квота free tier.
- **Не ставить `temperature=0` для Gemini 3.x** — Google предупреждает о зацикливании. Детерминизм обеспечивают схема, верификация и кэш.
- **Не доверять `confidence` модели без проверки кодом.** Цитата не найдена — сигнала нет.
- **Не класть бизнес-правила в промпт, если их может выполнить код:** даты, пороги, математика, списки клиентов.
- **Не хранить большие тексты в состоянии графа:** раздуваются чекпоинты.
- **Не менять контракты §1.4 молча** — только через ARCHITECTURE §4.7 и по согласию P1.
