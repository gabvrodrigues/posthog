from posthog.test.base import APIBaseTest, ClickhouseTestMixin
from unittest.mock import patch

from posthog.hogql.query import execute_hogql_query

from posthog.models.team import Team

from products.data_catalog.backend.logic import relationships
from products.data_catalog.backend.logic.certifications import certify, propose_certification
from products.data_catalog.backend.logic.metrics import upsert_metric
from products.data_catalog.backend.logic.relationships import accept_proposal, propose_relationship
from products.product_analytics.backend.models.insight import Insight
from products.warehouse_sources.backend.facade.models import DataWarehouseTable

_HOGQL = {"kind": "HogQLQuery", "query": "select count() from events"}
_COLUMNS = {"id": {"hogql": "StringDatabaseField", "clickhouse": "Nullable(String)", "schema_valid": True}}


class TestInformationSchemaMetrics(ClickhouseTestMixin, APIBaseTest):
    def _rows(self, where: str = "") -> dict:
        response = execute_hogql_query(
            f"SELECT name, description, status, is_drifted, definition_kind FROM system.information_schema.metrics {where}",
            team=self.team,
        )
        return {row[0]: row for row in response.results}

    def test_metric_is_discoverable_via_ilike(self) -> None:
        upsert_metric(
            team=self.team, user=self.user, name="mrr", description="Monthly recurring revenue", definition=_HOGQL
        )
        rows = self._rows("WHERE name ILIKE '%mrr%'")
        assert "mrr" in rows
        assert rows["mrr"][1] == "Monthly recurring revenue"
        assert rows["mrr"][2] == "proposed"
        assert rows["mrr"][4] == "HogQLQuery"

    def test_is_drifted_reflects_source_insight(self) -> None:
        insight = Insight.objects.create(team=self.team, created_by=self.user, query=_HOGQL)
        upsert_metric(
            team=self.team, user=self.user, name="active", description="d", source_insight_short_id=insight.short_id
        )
        assert self._rows("WHERE name = 'active'")["active"][3] in (False, 0)

        Insight.objects.filter(pk=insight.pk).update(
            query={"kind": "HogQLQuery", "query": "select count() from persons"}
        )
        assert self._rows("WHERE name = 'active'")["active"][3] in (True, 1)

    def test_team_isolation(self) -> None:
        other = Team.objects.create_with_data(organization=self.organization, initiating_user=self.user, name="Other")
        upsert_metric(team=other, user=self.user, name="theirs", description="d", definition=_HOGQL)
        assert "theirs" not in self._rows()


class TestInformationSchemaCertificationsAndRelationships(ClickhouseTestMixin, APIBaseTest):
    def test_certification_column_on_tables(self) -> None:
        table = DataWarehouseTable.objects.create(
            name="revenue", format="Parquet", team=self.team, url_pattern="s3://bucket/x", columns=_COLUMNS
        )
        certify(propose_certification(team=self.team, user=self.user, table_id=str(table.id)), self.user)

        response = execute_hogql_query(
            "SELECT table_name, certification FROM system.information_schema.tables WHERE table_name = 'revenue'",
            team=self.team,
        )
        assert {row[0]: row[1] for row in response.results}.get("revenue") == "certified"

    def test_proposed_join_appears_in_relationships(self) -> None:
        propose_relationship(
            team=self.team,
            user=self.user,
            source_table_name="events",
            source_table_key="distinct_id",
            joining_table_name="persons",
            joining_table_key="id",
            field_name="linked_person",
        )
        response = execute_hogql_query(
            "SELECT relationship_kind, status FROM system.information_schema.relationships "
            "WHERE source_table = 'events' AND relationship_kind = 'proposed_join'",
            team=self.team,
        )
        assert any(row[1] == "proposed" for row in response.results)

    def test_accepted_join_is_enriched_with_reviewed_provenance(self) -> None:
        proposal = propose_relationship(
            team=self.team,
            user=self.user,
            source_table_name="events",
            source_table_key="distinct_id",
            joining_table_name="persons",
            joining_table_key="id",
            field_name="reviewed_person",
            confidence=0.9,
            reasoning="97% key match",
        )
        with patch.object(relationships, "execute_hogql_query"):  # mock the ClickHouse probe boundary
            accept_proposal(proposal, self.user)

        response = execute_hogql_query(
            "SELECT confidence, reasoning FROM system.information_schema.relationships "
            "WHERE source_table = 'events' AND relationship_kind = 'lazy_join' AND reasoning IS NOT NULL",
            team=self.team,
        )
        assert [row[1] for row in response.results] == ["97% key match"]
        assert response.results[0][0] is not None
