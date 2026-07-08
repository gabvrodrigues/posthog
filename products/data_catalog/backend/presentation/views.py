"""DRF views for data_catalog.

Thin: validate via the serializer, call the facade, serialize the result. Domain invariants
(name reservation, upsert, validation, drift, approval) live in the logic layer behind the facade.
"""

from django.db.models import QuerySet

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from posthog.api.routing import TeamAndOrgViewSetMixin
from posthog.api.utils import action
from posthog.utils import refresh_requested_by_client

from ..facade import api
from ..facade.enums import CreatedSource
from ..facade.models import Metric, RelationshipProposal, TableCertification
from .serializers import (
    CertificationCreateSerializer,
    CertificationSerializer,
    MetricRunRequestSerializer,
    MetricRunResponseSerializer,
    MetricSerializer,
    RelationshipProposalSerializer,
    RelationshipRejectSerializer,
)


class MetricViewSet(TeamAndOrgViewSetMixin, viewsets.ModelViewSet):
    """CRUD for catalog metrics, addressed by their reserved ``name`` (e.g. /metrics/mrr/)."""

    scope_object = "data_catalog"
    lookup_field = "name"
    serializer_class = MetricSerializer
    queryset = Metric.objects.unscoped()

    def safely_get_queryset(self, queryset: QuerySet[Metric]) -> QuerySet[Metric]:
        return queryset.filter(team_id=self.team_id, deleted=False).order_by("-created_at")

    @extend_schema(description="Create a metric, or refine the one already holding this name for the team.")
    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        metric = api.upsert_metric(
            team=self.team,
            user=request.user,
            name=data["name"],
            description=data["description"],
            display_name=data.get("display_name", ""),
            unit=data.get("unit", ""),
            definition=data.get("definition"),
            source_insight_short_id=data.get("source_insight_short_id"),
            created_source=data.get("created_source", CreatedSource.USER),
            ai_model=data.get("ai_model", ""),
            confidence=data.get("confidence"),
            reasoning=data.get("reasoning", ""),
        )
        return Response(self.get_serializer(metric).data, status=status.HTTP_201_CREATED)

    def update(self, request: Request, *args, **kwargs) -> Response:
        partial = kwargs.pop("partial", False)
        metric = self.get_object()
        serializer = self.get_serializer(metric, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        fields = dict(serializer.validated_data)
        if "name" in fields and fields["name"] != metric.name:
            raise ValidationError({"name": "Metric name is write-once and cannot be changed."})
        fields.pop("name", None)
        metric = api.update_metric(metric, team=self.team, user=request.user, **fields)
        return Response(self.get_serializer(metric).data)

    def perform_destroy(self, instance: Metric) -> None:
        api.soft_delete_metric(instance, self.request.user)

    @action(
        detail=True,
        methods=["POST"],
        required_scopes=["data_catalog_approval:write"],
        request=None,
        responses={200: MetricSerializer},
    )
    def approve(self, request: Request, **kwargs) -> Response:
        """Bless a metric as canonical. Returns 409 while the metric is drifted from its insight."""
        metric = api.approve_metric(self.get_object(), request.user)
        return Response(self.get_serializer(metric).data)

    @action(
        detail=True,
        methods=["POST"],
        url_path="refresh_from_insight",
        required_scopes=["data_catalog:write"],
        request=None,
        responses={200: MetricSerializer},
    )
    def refresh_from_insight(self, request: Request, **kwargs) -> Response:
        """Re-snapshot the linked insight's current query into the definition."""
        metric = api.refresh_metric_from_insight(self.get_object(), request.user)
        return Response(self.get_serializer(metric).data)

    @action(
        detail=True,
        methods=["POST"],
        required_scopes=["data_catalog:read", "query:read"],
        request=MetricRunRequestSerializer,
        responses={200: MetricRunResponseSerializer},
    )
    def run(self, request: Request, **kwargs) -> Response:
        """Execute the metric's definition and return the normalized result envelope."""
        envelope = api.run_metric(
            team=self.team,
            metric=self.get_object(),
            user=request.user,
            refresh=refresh_requested_by_client(request),
            date_from=request.data.get("date_from"),
            date_to=request.data.get("date_to"),
            interval=request.data.get("interval"),
            query_id=request.data.get("query_id"),
        )
        return Response(envelope)


class CertificationViewSet(TeamAndOrgViewSetMixin, viewsets.ModelViewSet):
    """Trust marks on warehouse tables and views. Reads exclude soft-deleted targets."""

    scope_object = "data_catalog"
    serializer_class = CertificationSerializer
    queryset = TableCertification.objects.unscoped()

    def safely_get_queryset(self, queryset: QuerySet[TableCertification]) -> QuerySet[TableCertification]:
        return api.certifications_for_team(self.team)

    @extend_schema(request=CertificationCreateSerializer, responses={201: CertificationSerializer})
    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = CertificationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cert = api.propose_certification(team=self.team, user=request.user, **serializer.validated_data)
        return Response(CertificationSerializer(cert).data, status=status.HTTP_201_CREATED)

    def perform_destroy(self, instance: TableCertification) -> None:
        api.revoke_certification(instance, self.request.user)

    @action(
        detail=True,
        methods=["POST"],
        required_scopes=["data_catalog_approval:write"],
        request=None,
        responses={200: CertificationSerializer},
    )
    def certify(self, request: Request, **kwargs) -> Response:
        """Mark the target as certified (prefer this source)."""
        cert = api.certify(self.get_object(), request.user)
        return Response(CertificationSerializer(cert).data)

    @action(
        detail=True,
        methods=["POST"],
        required_scopes=["data_catalog_approval:write"],
        request=None,
        responses={200: CertificationSerializer},
    )
    def deprecate(self, request: Request, **kwargs) -> Response:
        """Mark the target as deprecated (avoid this source)."""
        cert = api.deprecate(self.get_object(), request.user)
        return Response(CertificationSerializer(cert).data)


class RelationshipProposalViewSet(
    TeamAndOrgViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Reviewed join facts. Accepting one promotes it to a real DataWarehouseJoin; rejections persist."""

    scope_object = "data_catalog"
    serializer_class = RelationshipProposalSerializer
    queryset = RelationshipProposal.objects.unscoped()

    def safely_get_queryset(self, queryset: QuerySet[RelationshipProposal]) -> QuerySet[RelationshipProposal]:
        proposals = api.relationships_for_team(self.team)
        status_filter = self.request.query_params.get("status")
        return proposals.filter(status=status_filter) if status_filter else proposals

    @extend_schema(
        parameters=[OpenApiParameter("status", OpenApiTypes.STR, description="Filter by proposed/accepted/rejected.")]
    )
    def list(self, request: Request, *args, **kwargs) -> Response:
        return super().list(request, *args, **kwargs)

    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        proposal = api.propose_relationship(
            team=self.team,
            user=request.user,
            source_table_name=data["source_table_name"],
            source_table_key=data["source_table_key"],
            joining_table_name=data["joining_table_name"],
            joining_table_key=data["joining_table_key"],
            field_name=data["field_name"],
            configuration=data.get("configuration"),
            confidence=data.get("confidence"),
            reasoning=data.get("reasoning", ""),
            evidence=data.get("evidence"),
        )
        return Response(self.get_serializer(proposal).data, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["POST"],
        required_scopes=["data_catalog_approval:write"],
        request=None,
        responses={200: RelationshipProposalSerializer},
    )
    def accept(self, request: Request, **kwargs) -> Response:
        """Promote the proposal to a real warehouse join after re-validating and probing it."""
        proposal = api.accept_proposal(self.get_object(), request.user)
        return Response(self.get_serializer(proposal).data)

    @extend_schema(request=RelationshipRejectSerializer, responses={200: RelationshipProposalSerializer})
    @action(detail=True, methods=["POST"], required_scopes=["data_catalog_approval:write"])
    def reject(self, request: Request, **kwargs) -> Response:
        """Reject the proposal. Persists forever so the pair is never re-proposed."""
        body = RelationshipRejectSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        proposal = api.reject_proposal(self.get_object(), request.user, body.validated_data.get("rejection_reason", ""))
        return Response(self.get_serializer(proposal).data)
