import calendar
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Avg
from django.utils import timezone

from apps.employees.models import Employees
from apps.leave.models import (
    VacationPreference,
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleChangeRequest,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
)
from apps.leave.services.dates import get_chargeable_leave_days, quantize_leave_days
from apps.leave.services.schedule_drafts.candidate_generation import (
    _apply_candidate_scoring,
    _apply_hard_rule_assessment,
    _candidate_passed_hard_rules,
    _candidate_scoring_decimal,
    _generation_candidate_features,
    _json_safe_generation_value,
)
from apps.leave.services.schedule_drafts.candidate_rules import (
    assess_schedule_draft_candidate,
)
from apps.leave.services.schedule_drafts.types import (
    DraftGenerationCandidate,
    DraftPlacement,
)
from apps.leave.ml.scoring import ACTIVE_CANDIDATE_SCORER_VERSION
from apps.leave.services.staffing import (
    build_department_staffing_context_map,
    get_department_staffing_rule,
    get_weighted_department_workload,
)


TRACE_SCHEDULE_STATUSES = (VacationSchedule.STATUS_ARCHIVED, VacationSchedule.STATUS_APPROVED)
TRACE_ITEM_STATUSES = (VacationScheduleItem.STATUS_APPROVED, VacationScheduleItem.STATUS_TRANSFERRED)
TRACE_PACKAGE_MAX_PERIODS = 3
TRACE_FEATURE_SCHEMA_VERSION = 1
TRACE_MODEL_SOURCE = "seed_historical_ml_trace"


def create_historical_schedule_ml_traces(rng, actor, current_year):
    builder = _HistoricalScheduleMLTraceBuilder(rng=rng, actor=actor, current_year=current_year)
    return builder.create()


class _HistoricalScheduleMLTraceBuilder:
    def __init__(self, *, rng, actor, current_year):
        self.rng = rng
        self.actor = actor
        self.current_year = current_year
        self.enterprise_head = (
            Employees.objects.filter(role=Employees.ROLE_ENTERPRISE_HEAD, is_active_employee=True)
            .order_by("id")
            .first()
        )
        self.stats = Counter()
        self._staffing_context_cache = {}
        self._staffing_rule_cache = {}
        self._workload_cache = {}

    @transaction.atomic
    def create(self):
        schedules = list(
            VacationSchedule.objects.filter(
                year__lte=self.current_year,
                status__in=TRACE_SCHEDULE_STATUSES,
            ).order_by("year", "id")
        )
        for schedule in schedules:
            self._reset_schedule_trace_data(schedule)
            self._create_schedule_trace(schedule)
        return dict(self.stats)

    def _reset_schedule_trace_data(self, schedule):
        schedule_items = VacationScheduleItem.objects.filter(schedule=schedule)
        VacationScheduleCandidateFeedback.objects.filter(schedule_item__schedule=schedule).delete()
        schedule_items.update(
            generation_run=None,
            selected_candidate=None,
            ai_score=None,
            ai_confidence=None,
            ai_model_version="",
            ai_explanation="",
        )
        VacationScheduleCandidatePackage.objects.filter(schedule=schedule).delete()
        VacationScheduleCandidate.objects.filter(schedule=schedule).delete()
        VacationScheduleGenerationRun.objects.filter(schedule=schedule).delete()

    def _create_schedule_trace(self, schedule):
        items = self._trace_items(schedule)
        if not items:
            return
        self._prepare_schedule_risk_cache(schedule, items)

        run = VacationScheduleGenerationRun.objects.create(
            schedule=schedule,
            year=schedule.year,
            mode=VacationScheduleGenerationRun.MODE_HYBRID,
            status=VacationScheduleGenerationRun.STATUS_RUNNING,
            actor=self.actor,
            model_version=ACTIVE_CANDIDATE_SCORER_VERSION,
            started_at=_historical_timestamp(schedule.year, days=0),
        )

        preferences = self._preference_map(schedule, items)
        active_placements = _placements_from_items(
            schedule.items.filter(
                vacation_type="paid",
                status__in=VacationScheduleItem.ACTIVE_STATUSES,
            )
        )
        change_requests = self._change_request_map(items)
        items_by_employee = _items_by_employee(items)
        selected_by_item = {}
        candidates_by_employee = defaultdict(list)

        for employee_id, employee_items in sorted(items_by_employee.items()):
            placed_days = Decimal("0.00")
            total_days = _group_trace_days(employee_items)
            for item in employee_items:
                pair = preferences.get((employee_id, schedule.year), {})
                selected_candidate = self._create_selected_candidate(
                    run,
                    schedule,
                    item,
                    pair,
                    placements=active_placements,
                    placed_days=placed_days,
                    total_days=total_days,
                )
                selected_by_item[item.id] = selected_candidate
                candidates_by_employee[employee_id].append(selected_candidate)
                placed_days = quantize_leave_days(placed_days + Decimal(item.chargeable_days or 0))

                for alternative in self._create_item_alternatives(
                    run,
                    schedule,
                    item,
                    pair,
                    active_placements,
                    change_requests.get(item.id, []),
                    total_days=total_days,
                ):
                    candidates_by_employee[employee_id].append(alternative)

                self._write_item_ai_fields(item, selected_candidate, run)

        for employee_id, employee_items in sorted(items_by_employee.items()):
            self._create_candidate_packages(
                run,
                schedule,
                employee_items,
                [selected_by_item[item.id] for item in employee_items if item.id in selected_by_item],
                candidates_by_employee[employee_id],
            )

        self._create_feedback(items, selected_by_item)
        self._finish_generation_run(run)
        self.stats["generation_runs"] += 1

    def _trace_items(self, schedule):
        return list(
            schedule.items.select_related(
                "employee",
                "employee__department",
                "employee__department__head",
                "employee__employee_position",
                "employee__employee_position__production_group",
            )
            .filter(
                vacation_type="paid",
                status__in=TRACE_ITEM_STATUSES,
            )
            .exclude(employee__role__in=Employees.SERVICE_ROLES)
            .order_by("employee_id", "start_date", "end_date", "id")
        )

    def _preference_map(self, schedule, items):
        employee_ids = sorted({item.employee_id for item in items})
        preferences = defaultdict(dict)
        for preference in VacationPreference.objects.filter(
            employee_id__in=employee_ids,
            year=schedule.year,
            status=VacationPreference.STATUS_FILLED,
            start_date__isnull=False,
            end_date__isnull=False,
        ).order_by("employee_id", "priority", "id"):
            preferences[(preference.employee_id, preference.year)][preference.priority] = preference
        return preferences

    def _change_request_map(self, items):
        item_ids = [item.id for item in items]
        grouped = defaultdict(list)
        if not item_ids:
            return grouped
        for change_request in VacationScheduleChangeRequest.objects.filter(schedule_item_id__in=item_ids).order_by(
            "schedule_item_id",
            "created_at",
            "id",
        ):
            grouped[change_request.schedule_item_id].append(change_request)
        return grouped

    def _create_selected_candidate(self, run, schedule, item, pair, *, placements, placed_days, total_days):
        kind, preference, preference_match_label = _selected_candidate_kind(item, pair)
        candidate = DraftGenerationCandidate(
            employee=item.employee,
            start_date=item.start_date,
            end_date=item.end_date,
            kind=kind,
            source=item.source,
            comment=_selected_candidate_comment(item, preference_match_label),
            preference=preference,
            metadata=_candidate_metadata(
                schedule,
                item.employee,
                item,
                total_days=total_days,
                placed_days=placed_days,
                outcome=_selected_outcome(item),
                preference_match_label=preference_match_label,
            ),
        )
        candidate.assessment = _selected_assessment(
            item,
            schedule.year,
            placements,
            risk_context=self._risk_context(schedule, item.employee, item.start_date, item.end_date),
        )
        _apply_selected_assessment_metadata(candidate, item)
        candidate = _apply_candidate_scoring(candidate)
        stored = _store_candidate(
            run,
            schedule,
            candidate,
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            decision_rank=1,
        )
        self.stats["selected_candidates"] += 1
        return stored

    def _create_item_alternatives(
        self,
        run,
        schedule,
        item,
        pair,
        placements,
        change_requests,
        *,
        total_days,
    ):
        alternatives = []
        rank = 2

        for preference in _alternative_preferences(item, pair):
            stored = self._create_preference_alternative(
                run,
                schedule,
                item,
                preference,
                placements,
                total_days=total_days,
                decision_rank=rank,
            )
            if stored is not None:
                alternatives.append(stored)
                rank += 1

        auto_candidate = self._create_auto_alternative(
            run,
            schedule,
            item,
            placements,
            total_days=total_days,
            decision_rank=rank,
        )
        if auto_candidate is not None:
            alternatives.append(auto_candidate)
            rank += 1

        for change_request in change_requests:
            if change_request.status != VacationScheduleChangeRequest.STATUS_REJECTED:
                continue
            stored = self._create_rejected_transfer_candidate(
                run,
                schedule,
                item,
                change_request,
                placements,
                total_days=total_days,
                decision_rank=rank,
            )
            if stored is not None:
                alternatives.append(stored)
                rank += 1

        blocked_candidate = self._create_blocked_candidate(
            run,
            schedule,
            item,
            placements,
            total_days=total_days,
            decision_rank=rank,
        )
        if blocked_candidate is not None:
            alternatives.append(blocked_candidate)

        return alternatives

    def _create_preference_alternative(
        self,
        run,
        schedule,
        item,
        preference,
        placements,
        *,
        total_days,
        decision_rank,
    ):
        if not preference or not preference.start_date or not preference.end_date:
            return None
        if preference.start_date == item.start_date and preference.end_date == item.end_date:
            return None

        candidate = DraftGenerationCandidate(
            employee=item.employee,
            start_date=preference.start_date,
            end_date=preference.end_date,
            kind=_preference_candidate_kind(preference),
            source=VacationScheduleItem.SOURCE_GENERATED,
            comment="Исторический альтернативный вариант из пожеланий сотрудника.",
            preference=preference,
            metadata=_candidate_metadata(
                schedule,
                item.employee,
                item,
                total_days=total_days,
                placed_days=Decimal("0.00"),
                outcome="rejected_preference_alternative",
                preference_match_label=_preference_label(preference),
            ),
        )
        assessment = assess_schedule_draft_candidate(
            item.employee,
            preference.start_date,
            preference.end_date,
            schedule.year,
            placements,
            max_chargeable_days=_alternative_max_chargeable_days(item, total_days, preference.start_date, preference.end_date),
            exclude_schedule_item_id=item.id,
            risk_context=self._risk_context(schedule, item.employee, preference.start_date, preference.end_date),
        )
        _apply_hard_rule_assessment(candidate, assessment)
        return self._store_scored_alternative(run, schedule, candidate, decision_rank)

    def _create_auto_alternative(self, run, schedule, item, placements, *, total_days, decision_rank):
        for start_date, end_date in _iter_auto_alternative_periods(self.rng, item, schedule.year):
            assessment = assess_schedule_draft_candidate(
                item.employee,
                start_date,
                end_date,
                schedule.year,
                placements,
                max_chargeable_days=_alternative_max_chargeable_days(item, total_days, start_date, end_date),
                exclude_schedule_item_id=item.id,
                risk_context=self._risk_context(schedule, item.employee, start_date, end_date),
            )
            if not assessment.get("can_place"):
                continue
            candidate = DraftGenerationCandidate(
                employee=item.employee,
                start_date=start_date,
                end_date=end_date,
                kind=VacationScheduleCandidate.KIND_AUTO,
                source=VacationScheduleItem.SOURCE_GENERATED,
                comment="Исторический альтернативный автоподбор: прошел правила, но выбран другой период.",
                metadata=_candidate_metadata(
                    schedule,
                    item.employee,
                    item,
                    total_days=total_days,
                    placed_days=Decimal("0.00"),
                    outcome="rejected_auto_alternative",
                    preference_match_label="Автоподбор",
                ),
            )
            _apply_hard_rule_assessment(candidate, assessment)
            return self._store_scored_alternative(run, schedule, candidate, decision_rank)
        return None

    def _create_rejected_transfer_candidate(
        self,
        run,
        schedule,
        item,
        change_request,
        placements,
        *,
        total_days,
        decision_rank,
    ):
        candidate = DraftGenerationCandidate(
            employee=item.employee,
            start_date=change_request.new_start_date,
            end_date=change_request.new_end_date,
            kind=VacationScheduleCandidate.KIND_MANUAL,
            source=VacationScheduleItem.SOURCE_TRANSFER,
            comment="Исторически отклоненный вариант переноса.",
            metadata=_candidate_metadata(
                schedule,
                item.employee,
                item,
                total_days=total_days,
                placed_days=Decimal("0.00"),
                outcome="rejected_transfer_alternative",
                preference_match_label="Отклоненный перенос",
            ),
        )
        assessment = assess_schedule_draft_candidate(
            item.employee,
            change_request.new_start_date,
            change_request.new_end_date,
            schedule.year,
            placements,
            max_chargeable_days=_alternative_max_chargeable_days(
                item,
                total_days,
                change_request.new_start_date,
                change_request.new_end_date,
            ),
            exclude_schedule_item_id=item.id,
            risk_context=self._risk_context(schedule, item.employee, change_request.new_start_date, change_request.new_end_date),
        )
        _apply_hard_rule_assessment(candidate, assessment)
        return self._store_scored_alternative(run, schedule, candidate, decision_rank)

    def _create_blocked_candidate(self, run, schedule, item, placements, *, total_days, decision_rank):
        max_days = max(Decimal(item.chargeable_days or 0) - Decimal("1.00"), Decimal("0.00"))
        if max_days <= 0:
            max_days = Decimal("1.00")
        assessment = assess_schedule_draft_candidate(
            item.employee,
            item.start_date,
            item.end_date,
            schedule.year,
            placements,
            max_chargeable_days=max_days,
            exclude_schedule_item_id=item.id,
            risk_context=self._risk_context(schedule, item.employee, item.start_date, item.end_date),
        )
        if assessment.get("can_place"):
            assessment = _blocked_overlap_assessment(item)
        candidate = DraftGenerationCandidate(
            employee=item.employee,
            start_date=item.start_date,
            end_date=item.end_date,
            kind=VacationScheduleCandidate.KIND_AUTO_URGENT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            comment="Исторический заблокированный вариант: нарушает жесткие правила.",
            metadata=_candidate_metadata(
                schedule,
                item.employee,
                item,
                total_days=total_days,
                placed_days=Decimal("0.00"),
                outcome="blocked_hard_rule",
                preference_match_label="Заблокированный вариант",
            ),
        )
        _apply_hard_rule_assessment(candidate, assessment)
        return self._store_scored_alternative(run, schedule, candidate, decision_rank)

    def _store_scored_alternative(self, run, schedule, candidate, decision_rank):
        candidate = _apply_candidate_scoring(candidate)
        decision = (
            VacationScheduleCandidate.DECISION_REJECTED
            if _candidate_passed_hard_rules(candidate)
            else VacationScheduleCandidate.DECISION_BLOCKED
        )
        stored = _store_candidate(run, schedule, candidate, decision=decision, decision_rank=decision_rank)
        if decision == VacationScheduleCandidate.DECISION_REJECTED:
            self.stats["rejected_candidates"] += 1
        else:
            self.stats["blocked_candidates"] += 1
        return stored

    def _write_item_ai_fields(self, item, candidate, run):
        VacationScheduleItem.objects.filter(pk=item.pk).update(
            generation_run=run,
            selected_candidate=candidate,
            ai_score=candidate.score,
            ai_confidence=candidate.confidence,
            ai_model_version=candidate.model_version,
            ai_explanation=candidate.explanation,
        )
        item.generation_run = run
        item.selected_candidate = candidate
        item.ai_score = candidate.score
        item.ai_confidence = candidate.confidence
        item.ai_model_version = candidate.model_version
        item.ai_explanation = candidate.explanation

    def _create_candidate_packages(self, run, schedule, employee_items, selected_candidates, employee_candidates):
        if len(employee_items) < 2 or len(selected_candidates) < 2:
            return

        for chunk_start in range(0, len(selected_candidates), TRACE_PACKAGE_MAX_PERIODS):
            chunk = selected_candidates[chunk_start : chunk_start + TRACE_PACKAGE_MAX_PERIODS]
            item_chunk = employee_items[chunk_start : chunk_start + TRACE_PACKAGE_MAX_PERIODS]
            if not chunk:
                continue
            selected_package = _create_candidate_package(
                run,
                schedule,
                item_chunk[0].employee,
                chunk,
                decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
                decision_rank=(chunk_start // TRACE_PACKAGE_MAX_PERIODS) + 1,
                schedule_items=item_chunk,
                package_kind="historical_selected_package",
            )
            self.stats["selected_packages"] += 1

            chunk_event_keys = {_historical_decision_event_key(schedule, item_chunk[0].employee, item) for item in item_chunk}
            rejected = [
                candidate
                for candidate in employee_candidates
                if candidate.decision == VacationScheduleCandidate.DECISION_REJECTED
                and _stored_candidate_event_key(candidate) in chunk_event_keys
            ][: len(chunk)]
            if rejected:
                _create_candidate_package(
                    run,
                    schedule,
                    item_chunk[0].employee,
                    rejected,
                    decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
                    decision_rank=selected_package.decision_rank + 20,
                    package_kind="historical_rejected_package",
                )
                self.stats["rejected_packages"] += 1

            blocked = [
                candidate
                for candidate in employee_candidates
                if candidate.decision == VacationScheduleCandidate.DECISION_BLOCKED
                and _stored_candidate_event_key(candidate) in chunk_event_keys
            ][: len(chunk)]
            if blocked:
                _create_candidate_package(
                    run,
                    schedule,
                    item_chunk[0].employee,
                    blocked,
                    decision=VacationScheduleCandidatePackage.DECISION_BLOCKED,
                    decision_rank=selected_package.decision_rank + 40,
                    package_kind="historical_blocked_package",
                )
                self.stats["blocked_packages"] += 1

    def _create_feedback(self, items, selected_by_item):
        for index, item in enumerate(items, start=1):
            selected_candidate = selected_by_item.get(item.id)
            if selected_candidate is None:
                continue
            decision = _feedback_decision_for_item(item, index)
            comment = _feedback_comment(item, decision)
            for reviewer_index, (reviewer, role) in enumerate(
                _feedback_reviewers(item, self.actor, self.enterprise_head),
                start=1,
            ):
                feedback = VacationScheduleCandidateFeedback.objects.create(
                    schedule_item=item,
                    candidate=selected_candidate,
                    generation_run=selected_candidate.generation_run,
                    reviewer=reviewer,
                    reviewer_role=role,
                    decision=decision,
                    comment=comment,
                    score_snapshot=selected_candidate.score,
                    confidence_snapshot=selected_candidate.confidence,
                    model_version_snapshot=selected_candidate.model_version,
                    explanation_snapshot=selected_candidate.explanation,
                )
                timestamp = selected_candidate.generation_run.started_at + timedelta(
                    minutes=180 + index * 3 + reviewer_index
                )
                VacationScheduleCandidateFeedback.objects.filter(pk=feedback.pk).update(
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                self.stats["feedback"] += 1

    def _finish_generation_run(self, run):
        candidates = run.candidates.all()
        selected_count = candidates.filter(decision=VacationScheduleCandidate.DECISION_SELECTED).count()
        candidates_count = candidates.count()
        average_score = candidates.aggregate(value=Avg("score"))["value"]
        run.status = VacationScheduleGenerationRun.STATUS_COMPLETED
        run.candidates_count = candidates_count
        run.selected_count = selected_count
        run.rejected_count = max(candidates_count - selected_count, 0)
        run.manual_count = candidates.filter(kind=VacationScheduleCandidate.KIND_MANUAL).count()
        run.average_score = average_score
        run.finished_at = run.started_at + timedelta(minutes=12)
        run.error_message = ""
        run.save(
            update_fields=[
                "status",
                "candidates_count",
                "selected_count",
                "rejected_count",
                "manual_count",
                "average_score",
                "finished_at",
                "error_message",
            ]
        )

    def _prepare_schedule_risk_cache(self, schedule, items):
        departments = {
            item.employee.department_id: item.employee.department
            for item in items
            if item.employee_id
            and getattr(item.employee, "department_id", None)
            and getattr(item.employee, "department", None)
        }
        if not departments:
            return

        as_of_date = date(schedule.year, 12, 31)
        contexts = build_department_staffing_context_map(departments.values(), as_of_date)
        for department_id, department in departments.items():
            self._staffing_context_cache[(schedule.year, department_id)] = contexts.get(department_id)
            if department_id not in self._staffing_rule_cache:
                self._staffing_rule_cache[department_id] = get_department_staffing_rule(department)

    def _risk_context(self, schedule, employee, start_date, end_date):
        department = getattr(employee, "department", None)
        department_id = getattr(employee, "department_id", None)
        if department is None or not department_id:
            return {}

        staffing_rule = self._staffing_rule_cache.get(department_id)
        workload_key = (department_id, start_date, end_date)
        if workload_key not in self._workload_cache:
            self._workload_cache[workload_key] = get_weighted_department_workload(
                department,
                start_date,
                end_date,
                staffing_rule,
            )

        return {
            "staffing_context": self._staffing_context_cache.get((schedule.year, department_id)),
            "staffing_rule": staffing_rule,
            "weighted_workload": self._workload_cache[workload_key],
        }


def _historical_timestamp(year, *, days):
    naive = datetime(year - 1, 12, 1, 9, 0, 0) + timedelta(days=days)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _placements_from_items(items):
    return [DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id) for item in items]


def _items_by_employee(items):
    grouped = defaultdict(list)
    for item in items:
        grouped[item.employee_id].append(item)
    return {
        employee_id: sorted(group, key=lambda item: (item.start_date, item.end_date, item.id))
        for employee_id, group in grouped.items()
    }


def _group_trace_days(items):
    active_days = sum(
        (Decimal(item.chargeable_days or 0) for item in items if item.status in VacationScheduleItem.BALANCE_STATUSES),
        Decimal("0.00"),
    )
    if active_days > 0:
        return quantize_leave_days(active_days)
    return quantize_leave_days(sum((Decimal(item.chargeable_days or 0) for item in items), Decimal("0.00")))


def _historical_decision_event_key(schedule, employee, item):
    return f"{schedule.id}:{employee.id}:{item.id}"


def _stored_candidate_event_key(candidate):
    features = candidate.features if isinstance(candidate.features, dict) else {}
    return features.get("historical_decision_event_key") or ""


def _alternative_max_chargeable_days(item, total_days, start_date, end_date):
    alternative_days = Decimal(get_chargeable_leave_days(start_date, end_date, "paid") or 0)
    return max(
        quantize_leave_days(total_days or Decimal("0.00")),
        Decimal(item.chargeable_days or 0),
        alternative_days,
        Decimal("1.00"),
    )


def _selected_candidate_kind(item, pair):
    if item.source in {VacationScheduleItem.SOURCE_MANUAL, VacationScheduleItem.SOURCE_TRANSFER}:
        return VacationScheduleCandidate.KIND_MANUAL, None, "Ручное решение"
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    if _matches_preference(item, primary):
        return VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE, primary, "Основное пожелание"
    backup = pair.get(VacationPreference.PRIORITY_BACKUP)
    if _matches_preference(item, backup):
        return VacationScheduleCandidate.KIND_BACKUP_PREFERENCE, backup, "Запасное пожелание"
    if item.chargeable_days and item.chargeable_days < 14:
        return VacationScheduleCandidate.KIND_AUTO_TOPUP, None, "Автодобор"
    return VacationScheduleCandidate.KIND_AUTO, None, "Автоподбор"


def _matches_preference(item, preference):
    return bool(
        preference
        and preference.start_date
        and preference.end_date
        and item.start_date == preference.start_date
        and item.end_date == preference.end_date
    )


def _selected_candidate_comment(item, preference_match_label):
    if item.status == VacationScheduleItem.STATUS_TRANSFERRED:
        return "Исторически выбранный вариант позже был отправлен на перенос."
    if item.source == VacationScheduleItem.SOURCE_TRANSFER:
        return "Исторически выбранный вариант после согласованного переноса."
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "Исторически внесенный HR вариант графика."
    return f"Исторически выбранный вариант: {preference_match_label.lower()}."


def _selected_outcome(item):
    if item.status == VacationScheduleItem.STATUS_TRANSFERRED:
        return "later_transferred"
    if item.source == VacationScheduleItem.SOURCE_TRANSFER or item.previous_item_id:
        return "transfer_replacement"
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "manual_approved"
    if item.risk_level == VacationScheduleItem.RISK_HIGH:
        return "approved_high_risk"
    return "approved"


def _candidate_metadata(
    schedule,
    employee,
    item,
    *,
    total_days,
    placed_days,
    outcome,
    preference_match_label,
):
    chargeable_days = Decimal(item.chargeable_days or 0)
    total_days = quantize_leave_days(total_days or chargeable_days)
    placed_days = quantize_leave_days(placed_days or Decimal("0.00"))
    open_required_days = max(chargeable_days, Decimal("1.00"))
    target_days = max(total_days, open_required_days)
    return {
        "historical_seed_trace": True,
        "historical_decision_source": TRACE_MODEL_SOURCE,
        "historical_decision_event_key": _historical_decision_event_key(schedule, employee, item),
        "historical_schedule_item_id": item.id,
        "historical_outcome": outcome,
        "planning_year": schedule.year,
        "available_days": target_days,
        "plan_available_days": target_days,
        "target_days": target_days,
        "placed_days": placed_days,
        "open_required_days": open_required_days,
        "blocking_days": Decimal("0.00"),
        "deadline_blocking_days": Decimal("0.00"),
        "annual_remaining_days": max(target_days - placed_days, Decimal("0.00")),
        "mandatory_days": min(target_days, Decimal("28.00")),
        "requested_preference_days": chargeable_days,
        "planning_basis": "historical_schedule",
        "remainder_policy": VacationPreference.REMAINDER_AUTO,
        "has_blocker": False,
        "needs_manual_attention": False,
        "nearest_deadline": None,
        "mandatory_rows_count": 0,
        "target_chargeable_days": chargeable_days,
        "chargeable_days": chargeable_days,
        "risk_score": item.risk_score,
        "risk_level": item.risk_level,
        "passed_hard_rules": True,
        "preference_match_label": preference_match_label,
        "employee_role_snapshot": employee.role,
        "schedule_item_id": item.id,
    }


def _selected_assessment(item, year, placements, *, risk_context=None):
    assessment = assess_schedule_draft_candidate(
        item.employee,
        item.start_date,
        item.end_date,
        year,
        placements,
        exclude_schedule_item_id=item.id,
        risk_context=risk_context,
    )
    if assessment.get("risk_payload"):
        reason = assessment.get("reason") or {}
        return {
            **assessment,
            "can_place": True,
            "has_conflict": bool(assessment.get("has_conflict")),
            "chargeable_days": assessment.get("chargeable_days") or item.chargeable_days,
            "reason": {"kind": "historical_selected", "text": "Исторически выбранный вариант."},
            "historical_assessment_can_place": bool(assessment.get("can_place")),
            "historical_assessment_reason_key": reason.get("kind", ""),
        }

    risk_payload = {
        "risk_score": item.risk_score,
        "risk_level": item.risk_level,
        "balance_after_request": Decimal("0.00"),
        "risk_explanation": {
            "is_conflict": False,
            "details": [],
            "short_reason": "Исторически принятое решение графика.",
        },
    }
    return {
        "can_place": True,
        "has_conflict": False,
        "chargeable_days": item.chargeable_days,
        "risk_payload": risk_payload,
        "reason": {"kind": "historical_selected", "text": "Исторически выбранный вариант."},
        "historical_assessment_can_place": True,
        "historical_assessment_reason_key": "historical_selected",
    }


def _apply_selected_assessment_metadata(candidate, item):
    assessment = candidate.assessment or {}
    risk_payload = assessment.get("risk_payload") or {}
    risk_score = risk_payload.get("risk_score")
    risk_level = risk_payload.get("risk_level")
    candidate.metadata.update(
        {
            "passed_hard_rules": True,
            "block_reason_key": "",
            "block_reason": "",
            "chargeable_days": assessment.get("chargeable_days") or item.chargeable_days,
            "risk_score": risk_score if risk_score is not None else item.risk_score,
            "risk_level": risk_level or item.risk_level,
            "historical_assessment_can_place": assessment.get("historical_assessment_can_place", True),
            "historical_assessment_reason_key": assessment.get("historical_assessment_reason_key", ""),
        }
    )
    return candidate


def _alternative_preferences(item, pair):
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    backup = pair.get(VacationPreference.PRIORITY_BACKUP)
    preferences = []
    for preference in (primary, backup):
        if preference and not _matches_preference(item, preference):
            preferences.append(preference)
    return preferences


def _preference_candidate_kind(preference):
    if preference.priority == VacationPreference.PRIORITY_PRIMARY:
        return VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE
    return VacationScheduleCandidate.KIND_BACKUP_PREFERENCE


def _preference_label(preference):
    if preference.priority == VacationPreference.PRIORITY_PRIMARY:
        return "Основное пожелание"
    return "Запасное пожелание"


def _iter_auto_alternative_periods(rng, item, year):
    target_days = max(int(item.chargeable_days or 0), 1)
    offsets = [31, 45, 62, 75, 93, 120, -31, -45, -62, -75, -93]
    rng.shuffle(offsets)
    for offset in offsets:
        start_date = item.start_date + timedelta(days=offset)
        if start_date.year != year:
            start_date = _same_year_date(start_date, year)
        start_date = start_date.replace(day=min(start_date.day, 20))
        end_date = _end_date_for_chargeable_days(start_date, target_days, year)
        if end_date is None:
            continue
        if start_date == item.start_date and end_date == item.end_date:
            continue
        yield start_date, end_date


def _same_year_date(value, year):
    last_day = calendar.monthrange(year, value.month)[1]
    return value.replace(year=year, day=min(value.day, last_day))


def _end_date_for_chargeable_days(start_date, target_days, year):
    cursor = start_date
    latest = start_date.replace(year=year, month=12, day=31)
    while cursor <= latest:
        if get_chargeable_leave_days(start_date, cursor, "paid") >= target_days:
            return cursor
        cursor += timedelta(days=1)
    return None


def _blocked_overlap_assessment(item):
    risk_payload = {
        "risk_score": max(int(item.risk_score or 0), 80),
        "risk_level": VacationScheduleItem.RISK_HIGH,
        "balance_after_request": Decimal("0.00"),
        "risk_explanation": {
            "is_conflict": True,
            "details": [{"kind": "employee_overlap", "text": "У сотрудника уже есть отпуск на эти даты."}],
            "short_reason": "Период пересекается с уже выбранным отпуском.",
        },
    }
    return {
        "can_place": False,
        "has_conflict": True,
        "chargeable_days": item.chargeable_days,
        "risk_payload": risk_payload,
        "reason": {"kind": "employee_overlap", "text": "У сотрудника уже есть отпуск на эти даты."},
    }


def _store_candidate(run, schedule, candidate, *, decision, decision_rank):
    features = _generation_candidate_features(candidate)
    stored = VacationScheduleCandidate.objects.create(
        generation_run=run,
        schedule=schedule,
        employee=candidate.employee,
        start_date=candidate.start_date,
        end_date=candidate.end_date,
        vacation_type="paid",
        chargeable_days=int(candidate.metadata.get("chargeable_days") or 0),
        kind=candidate.kind,
        source=candidate.source,
        passed_hard_rules=_candidate_passed_hard_rules(candidate),
        block_reason_key=(candidate.metadata.get("block_reason_key") or "")[:80],
        block_reason=candidate.metadata.get("block_reason") or "",
        risk_score=int(candidate.metadata.get("risk_score") or 0),
        risk_level=candidate.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW,
        features=features,
        score=_candidate_scoring_decimal(candidate, "scoring_score"),
        confidence=_candidate_scoring_decimal(candidate, "scoring_confidence"),
        model_version=candidate.metadata.get("scoring_model_version") or ACTIVE_CANDIDATE_SCORER_VERSION,
        explanation=candidate.metadata.get("scoring_explanation") or candidate.comment,
        decision=decision,
        decision_rank=decision_rank,
        selected_at=(
            run.started_at + timedelta(minutes=decision_rank)
            if decision == VacationScheduleCandidate.DECISION_SELECTED
            else None
        ),
    )
    candidate.stored_candidate = stored
    return stored


def _create_candidate_package(
    run,
    schedule,
    employee,
    candidates,
    *,
    decision,
    decision_rank,
    package_kind,
    schedule_items=None,
):
    candidates = list(candidates)
    schedule_items = list(schedule_items or [])
    risk_level, risk_score = _package_risk(candidates)
    total_days = sum((Decimal(candidate.chargeable_days or 0) for candidate in candidates), Decimal("0.00"))
    package = VacationScheduleCandidatePackage.objects.create(
        generation_run=run,
        schedule=schedule,
        employee=employee,
        periods_count=len(candidates),
        total_chargeable_days=int(total_days),
        source=_package_source(candidates),
        passed_hard_rules=all(candidate.passed_hard_rules for candidate in candidates),
        block_reason_key=_first_non_empty(candidate.block_reason_key for candidate in candidates)[:80],
        block_reason=_first_non_empty(candidate.block_reason for candidate in candidates),
        risk_score=risk_score,
        risk_level=risk_level,
        features=_package_features(candidates, package_kind),
        score=_average_decimal(candidate.score for candidate in candidates),
        confidence=_average_decimal(candidate.confidence for candidate in candidates),
        model_version=ACTIVE_CANDIDATE_SCORER_VERSION,
        explanation=_package_explanation(decision),
        decision=decision,
        decision_rank=decision_rank,
        selected_at=(
            run.started_at + timedelta(minutes=decision_rank + 10)
            if decision == VacationScheduleCandidatePackage.DECISION_SELECTED
            else None
        ),
    )
    for order, candidate in enumerate(candidates, start=1):
        schedule_item = schedule_items[order - 1] if order <= len(schedule_items) else None
        VacationScheduleCandidatePackagePeriod.objects.create(
            candidate_package=package,
            candidate=candidate,
            schedule_item=schedule_item if decision == VacationScheduleCandidatePackage.DECISION_SELECTED else None,
            start_date=candidate.start_date,
            end_date=candidate.end_date,
            chargeable_days=candidate.chargeable_days,
            passed_hard_rules=candidate.passed_hard_rules,
            block_reason_key=candidate.block_reason_key,
            block_reason=candidate.block_reason,
            risk_score=candidate.risk_score,
            risk_level=candidate.risk_level,
            features=candidate.features,
            order=order,
        )
    return package


def _package_risk(candidates):
    rank = {
        VacationScheduleItem.RISK_LOW: 0,
        VacationScheduleItem.RISK_MEDIUM: 1,
        VacationScheduleItem.RISK_HIGH: 2,
    }
    highest = max(candidates, key=lambda candidate: (rank.get(candidate.risk_level, 0), candidate.risk_score), default=None)
    if highest is None:
        return VacationScheduleItem.RISK_LOW, 0
    return highest.risk_level, highest.risk_score


def _package_source(candidates):
    if any(candidate.source == VacationScheduleItem.SOURCE_MANUAL for candidate in candidates):
        return VacationScheduleItem.SOURCE_MANUAL
    if any(candidate.source == VacationScheduleItem.SOURCE_TRANSFER for candidate in candidates):
        return VacationScheduleItem.SOURCE_TRANSFER
    return VacationScheduleItem.SOURCE_GENERATED


def _package_features(candidates, package_kind):
    event_keys = sorted(
        key
        for candidate in candidates
        for key in [_stored_candidate_event_key(candidate)]
        if key
    )
    return _json_safe_generation_value(
        {
            "feature_schema_version": TRACE_FEATURE_SCHEMA_VERSION,
            "historical_seed_trace": True,
            "historical_package_event_key": "|".join(event_keys),
            "package_kind": package_kind,
            "periods_count": len(candidates),
            "periods": [
                {
                    "candidate_id": candidate.id,
                    "start_date": candidate.start_date,
                    "end_date": candidate.end_date,
                    "chargeable_days": candidate.chargeable_days,
                    "score": candidate.score,
                    "confidence": candidate.confidence,
                    "risk_score": candidate.risk_score,
                    "risk_level": candidate.risk_level,
                    "passed_hard_rules": candidate.passed_hard_rules,
                    "decision": candidate.decision,
                }
                for candidate in candidates
            ],
        }
    )


def _average_decimal(values):
    values = [Decimal(value) for value in values if value is not None]
    if not values:
        return None
    return quantize_leave_days(sum(values, Decimal("0.00")) / Decimal(len(values)))


def _first_non_empty(values):
    for value in values:
        if value:
            return value
    return ""


def _package_explanation(decision):
    if decision == VacationScheduleCandidatePackage.DECISION_SELECTED:
        return "Исторический пакет выбранных периодов графика."
    if decision == VacationScheduleCandidatePackage.DECISION_BLOCKED:
        return "Исторический пакет заблокирован жесткими правилами."
    return "Исторический пакет прошел правила, но выбран другой набор периодов."


def _feedback_reviewers(item, actor, enterprise_head):
    reviewers = []
    if actor is not None and actor.id != item.employee_id:
        reviewers.append((actor, VacationScheduleCandidateFeedback.ROLE_HR))

    department_head = getattr(getattr(item.employee, "department", None), "head", None)
    if (
        department_head is not None
        and department_head.id != item.employee_id
        and department_head.id not in {reviewer.id for reviewer, _ in reviewers if reviewer is not None}
    ):
        reviewers.append((department_head, VacationScheduleCandidateFeedback.ROLE_DEPARTMENT_HEAD))

    if item.employee.role in Employees.MANAGEMENT_ROLES or item.risk_level == VacationScheduleItem.RISK_HIGH:
        if (
            enterprise_head is not None
            and enterprise_head.id != item.employee_id
            and enterprise_head.id not in {reviewer.id for reviewer, _ in reviewers if reviewer is not None}
        ):
            reviewers.append((enterprise_head, VacationScheduleCandidateFeedback.ROLE_ENTERPRISE_HEAD))

    return reviewers[:3]


def _feedback_decision_for_item(item, index):
    if item.status == VacationScheduleItem.STATUS_TRANSFERRED:
        if item.was_changed_by_manager:
            return VacationScheduleCandidateFeedback.DECISION_REJECT
        return (
            VacationScheduleCandidateFeedback.DECISION_REJECT
            if index % 3 == 0 or item.risk_level == VacationScheduleItem.RISK_HIGH
            else VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE
        )
    if item.source == VacationScheduleItem.SOURCE_TRANSFER or item.previous_item_id:
        return VacationScheduleCandidateFeedback.DECISION_AGREE
    if item.risk_level == VacationScheduleItem.RISK_HIGH:
        return VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE
    return VacationScheduleCandidateFeedback.DECISION_AGREE


def _feedback_comment(item, decision):
    if decision == VacationScheduleCandidateFeedback.DECISION_REJECT:
        return "Историческое решение: период вернули, после чего отпуск был перенесен."
    if decision == VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE:
        if item.status == VacationScheduleItem.STATUS_TRANSFERRED:
            return "Историческое решение: потребовалась правка дат и перенос."
        return "Историческое решение: согласовано с замечанием из-за повышенной нагрузки."
    return "Историческое решение: вариант принят без правок."
