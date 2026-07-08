"""DRF views for data_catalog.

Thin: validate via the serializer, call the facade, serialize the result. Domain invariants
(name reservation, upsert, validation, drift, approval) live in the logic layer behind the facade.
"""

from django.db.models import QuerySet

from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from posthog.api.routing import TeamAndOrgViewSetMixin
from posthog.api.utils import action

from ..facade import api
from ..facade.enums import CreatedSource
from ..facade.models import Metric
from .serializers import MetricSerializer


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
