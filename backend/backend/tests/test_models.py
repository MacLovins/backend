from leadradar_auth.models import auth_metadata
from leadradar_core.db.models import core_metadata


def test_core_metadata_tables_and_schemas():
    assert core_metadata.schema == "core"

    expected_core_tables = {
        "core.org",
        "core.company",
        "core.service",
        "core.signal_question",
        "core.icp_profile",
        "core.disqualification_rule",
        "core.scoring_profile",
        "core.document",
        "core.document_chunk",
        "core.extraction_state",
        "core.analysis_run",
        "core.run_event",
        "core.signal",
        "core.rejected_evidence",
        "core.lead_score",
        "core.feedback",
        "core.llm_call",
        "core.llm_cache",
        "core.domain_event",
    }

    actual_core_tables = set(core_metadata.tables.keys())
    assert expected_core_tables.issubset(actual_core_tables)


def test_auth_metadata_tables_and_schemas():
    assert auth_metadata.schema == "auth"

    expected_auth_tables = {
        "auth.user_account",
    }

    actual_auth_tables = set(auth_metadata.tables.keys())
    assert expected_auth_tables == actual_auth_tables


def test_company_table_constraints():
    company = core_metadata.tables["core.company"]
    col_names = {col.name for col in company.columns}
    assert "domain" in col_names
    assert "org_id" in col_names
    assert "is_tracked" in col_names


def test_document_chunk_has_vector_embedding():
    chunk = core_metadata.tables["core.document_chunk"]
    assert "embedding" in chunk.columns
    # pgvector embedding type check
    assert "VECTOR" in str(type(chunk.columns["embedding"].type)).upper()


def test_lead_score_table_constraints():
    lead_score = core_metadata.tables["core.lead_score"]
    col_names = {col.name for col in lead_score.columns}
    assert "priority" in col_names
    assert "tier" in col_names
    assert "why_now" in col_names
    assert "is_current" in col_names
