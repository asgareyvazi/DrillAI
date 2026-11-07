"""Dialect contracts for shared document-derived trust predicates."""

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from drillai.db.models import ExtractedRecord
from drillai.documents.access import extracted_record_trust_condition


def test_quality_flag_membership_compiles_as_exact_json_array_membership():
    statement = select(ExtractedRecord.id).where(extracted_record_trust_condition())

    sqlite_sql = str(statement.compile(dialect=sqlite.dialect())).lower()
    postgres_sql = str(statement.compile(dialect=postgresql.dialect())).lower()

    assert "json_each(" in sqlite_sql
    assert "json_array_elements_text(" in postgres_sql
    assert " like " not in sqlite_sql
    assert " like " not in postgres_sql
