"""initial_schema

Revision ID: 2026_09_25_init
Revises: 
Create Date: 2026-09-25 18:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

# revision identifiers, used by Alembic.
revision: str = "2026_09_25_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0. Extensions & Schemas
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE SCHEMA IF NOT EXISTS core;")
    op.execute("CREATE SCHEMA IF NOT EXISTS auth;")
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph;")

    # --- Step 1: org & auth.user_account ---
    op.create_table(
        "org",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )

    op.create_table(
        "user_account",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("role", sa.String(32), server_default="sales", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'sales')", name="ck_user_account_role"),
        schema="auth",
    )
    op.create_index("ix_auth_user_account_email", "user_account", ["email"], unique=True, schema="auth")

    # --- Step 2: service, signal_question, icp_profile, disqualification_rule, scoring_profile ---
    op.create_table(
        "service",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("value_proposition", sa.Text(), server_default="", nullable=False),
        sa.Column("decision_makers", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "slug", name="uq_service_org_slug"),
        schema="core",
    )

    op.create_table(
        "signal_question",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("polarity", sa.String(16), server_default="positive", nullable=False),
        sa.Column("weight", sa.String(16), server_default="medium", nullable=False),
        sa.Column("source_types", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("recency_days", sa.Integer(), server_default="180", nullable=False),
        sa.Column("keywords", JSONB(), nullable=True),
        sa.Column("job_titles", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("negative_terms", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("keywords_status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("service_id", "key", name="uq_signal_question_service_key"),
        schema="core",
    )
    op.create_index(
        "idx_signal_question_service_active",
        "signal_question",
        ["service_id", "is_active"],
        schema="core",
    )

    op.create_table(
        "icp_profile",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("countries", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("industries_any", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("employees_min", sa.Integer(), nullable=True),
        sa.Column("employees_max", sa.Integer(), nullable=True),
        sa.Column("revenue_min_eur", sa.Numeric(15, 2), nullable=True),
        sa.Column("nice_to_have", JSONB(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("service_id", name="uq_icp_profile_service"),
        schema="core",
    )

    op.create_table(
        "disqualification_rule",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("condition", JSONB(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("cap_value", sa.Numeric(10, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index("idx_disqualification_rule_service", "disqualification_rule", ["service_id"], schema="core")

    op.create_table(
        "scoring_profile",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("service_id", "version", name="uq_scoring_profile_service_version"),
        schema="core",
    )
    op.create_index(
        "idx_scoring_profile_current",
        "scoring_profile",
        ["service_id"],
        postgresql_where=sa.text("is_current = true"),
        schema="core",
    )

    # --- Step 3: company ---
    op.create_table(
        "company",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("aliases", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("own_domains", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("country_code", sa.String(2), nullable=True),
        sa.Column("industry_ids", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("employees", sa.Integer(), nullable=True),
        sa.Column("revenue_eur", sa.Numeric(15, 2), nullable=True),
        sa.Column("hq_city", sa.String(255), nullable=True),
        sa.Column("wikidata_qid", sa.String(64), nullable=True),
        sa.Column("lei", sa.String(64), nullable=True),
        sa.Column("crunchbase_id", sa.String(128), nullable=True),
        sa.Column("homepage_url", sa.Text(), nullable=True),
        sa.Column("careers_url", sa.Text(), nullable=True),
        sa.Column("newsroom_url", sa.Text(), nullable=True),
        sa.Column("ats", JSONB(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("tags", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("origin", sa.String(32), server_default="manual", nullable=False),
        sa.Column("is_tracked", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "domain", name="uq_company_org_domain"),
        schema="core",
    )
    op.create_index("idx_company_org_tracked", "company", ["org_id", "is_tracked"], schema="core")

    # --- Step 4: document, document_chunk, extraction_state ---
    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("meta", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("company_id", "content_hash", name="uq_document_company_content_hash"),
        schema="core",
    )
    op.create_index(
        "idx_document_company_source_pub",
        "document",
        ["company_id", "source_type", "published_at"],
        schema="core",
    )

    op.create_table(
        "document_chunk",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("core.document.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index("idx_document_chunk_company", "document_chunk", ["company_id"], schema="core")

    op.create_table(
        "extraction_state",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("company_id", "service_id", name="uq_extraction_state_company_service"),
        schema="core",
    )

    # --- Step 5: analysis_run & run_event ---
    op.create_table(
        "analysis_run",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), server_default="analyze", nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("params", JSONB(), server_default="{}", nullable=False),
        sa.Column("progress", JSONB(), server_default='{"done": 0, "total": 0, "failed": 0, "paused": 0}', nullable=False),
        sa.Column("stats", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index("idx_analysis_run_org_created", "analysis_run", ["org_id", "created_at"], schema="core")

    op.create_table(
        "run_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(start=1), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("core.analysis_run.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("core.company.id", ondelete="SET NULL"), nullable=True),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="SET NULL"), nullable=True),
        sa.Column("stage", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("payload", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index("idx_run_event_run_id", "run_event", ["run_id", "id"], schema="core")

    # --- Step 6: signal, rejected_evidence, lead_score ---
    op.create_table(
        "signal",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("core.signal_question.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_key", sa.String(128), nullable=False),
        sa.Column("question_version", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("core.document.id", ondelete="SET NULL"), nullable=True),
        sa.Column("chunk_id", sa.Uuid(), sa.ForeignKey("core.document_chunk.id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("core.analysis_run.id", ondelete="SET NULL"), nullable=True),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("polarity", sa.String(16), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_start", sa.Integer(), nullable=True),
        sa.Column("quote_end", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("strength", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("reliability", sa.Numeric(5, 4), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(128), nullable=False),
        sa.Column("flags", ARRAY(sa.String()), server_default="{}", nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("prompt_version", sa.String(32), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index(
        "idx_signal_company_service_status",
        "signal",
        ["company_id", "service_id", "status"],
        schema="core",
    )

    op.create_table(
        "rejected_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("core.signal_question.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("core.analysis_run.id", ondelete="SET NULL"), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index(
        "idx_rejected_evidence_service_reason",
        "rejected_evidence",
        ["service_id", "reason"],
        schema="core",
    )

    op.create_table(
        "lead_score",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), sa.ForeignKey("core.company.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scoring_profile_id", sa.Uuid(), sa.ForeignKey("core.scoring_profile.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fit", sa.Numeric(5, 2), nullable=False),
        sa.Column("intent", sa.Numeric(5, 2), nullable=False),
        sa.Column("risk", sa.Numeric(5, 2), nullable=False),
        sa.Column("priority", sa.Numeric(5, 2), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("disqualified", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("rule_hits", JSONB(), server_default="[]", nullable=False),
        sa.Column("fit_details", JSONB(), server_default="{}", nullable=False),
        sa.Column("breakdown", JSONB(), server_default="[]", nullable=False),
        sa.Column("why_now", JSONB(), server_default="[]", nullable=False),
        sa.Column("data_gaps", JSONB(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("is_current", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index(
        "idx_lead_score_service_priority_current",
        "lead_score",
        ["org_id", "service_id", "priority"],
        postgresql_where=sa.text("is_current = true"),
        schema="core",
    )
    op.create_index(
        "idx_lead_score_company_service",
        "lead_score",
        ["company_id", "service_id"],
        schema="core",
    )

    # --- Step 7: feedback, llm_call, llm_cache, domain_event ---
    op.create_table(
        "feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), sa.ForeignKey("core.service.id", ondelete="CASCADE"), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "target_type", "target_id", name="uq_feedback_user_target"),
        schema="core",
    )
    op.create_index("idx_feedback_service_verdict", "feedback", ["service_id", "verdict"], schema="core")

    op.create_table(
        "llm_call",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("core.analysis_run.id", ondelete="SET NULL"), nullable=True),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(32), server_default="success", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index("idx_llm_call_model_created", "llm_call", ["model", "created_at"], schema="core")

    op.create_table(
        "llm_cache",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("response", JSONB(), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )

    op.create_table(
        "domain_event",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(128), nullable=False),
        sa.Column("payload", JSONB(), server_default="{}", nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="core",
    )
    op.create_index(
        "idx_domain_event_unprocessed",
        "domain_event",
        ["created_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
        schema="core",
    )


def downgrade() -> None:
    # Drop tables in reverse order
    op.drop_table("domain_event", schema="core")
    op.drop_table("llm_cache", schema="core")
    op.drop_table("llm_call", schema="core")
    op.drop_table("feedback", schema="core")
    op.drop_table("lead_score", schema="core")
    op.drop_table("rejected_evidence", schema="core")
    op.drop_table("signal", schema="core")
    op.drop_table("run_event", schema="core")
    op.drop_table("analysis_run", schema="core")
    op.drop_table("extraction_state", schema="core")
    op.drop_table("document_chunk", schema="core")
    op.drop_table("document", schema="core")
    op.drop_table("company", schema="core")
    op.drop_table("scoring_profile", schema="core")
    op.drop_table("disqualification_rule", schema="core")
    op.drop_table("icp_profile", schema="core")
    op.drop_table("signal_question", schema="core")
    op.drop_table("service", schema="core")
    op.drop_table("user_account", schema="auth")
    op.drop_table("org", schema="core")
    op.execute("DROP SCHEMA IF EXISTS auth CASCADE;")
    op.execute("DROP SCHEMA IF EXISTS core CASCADE;")
