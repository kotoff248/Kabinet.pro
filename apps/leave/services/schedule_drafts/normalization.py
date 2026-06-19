import calendar
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from math import ceil
from types import SimpleNamespace
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Avg
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from apps.accounts.services import get_managed_department_id, is_department_head_employee, is_hr_employee
from apps.leave.models import (
    DepartmentWorkload,
    VacationPreference,
    VacationPreferenceCollection,
    VacationRequest,
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleDepartmentApproval,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
    VacationScheduleManualSuggestionCache,
)
from apps.leave.services.candidate_feedback import build_schedule_candidate_feedback_context
from apps.leave.services.dates import format_period_label, get_chargeable_leave_days, quantize_leave_days
from apps.leave.services.employee_presentation import get_employee_identity_presentation
from apps.leave.services.ledger import (
    get_employee_available_balance,
    get_employee_entitlement_rows,
    get_employee_entitlement_rows_bulk,
    get_employee_list_leave_summaries,
)
from apps.leave.ml.scoring import ACTIVE_CANDIDATE_SCORER_VERSION, score_candidate_features
from apps.leave.services.preferences import (
    get_eligible_preference_employees,
    get_employee_preference_pair_map,
    get_employee_preference_pair,
    get_employee_preference_state_map,
    get_employee_preference_state,
    get_paid_leave_available_from,
)
from apps.leave.services.planning_cycles import is_active_planning_year
from apps.leave.services.risk import calculate_vacation_request_risk_with_explanation
from apps.leave.services.schedule_auto_place_jobs import get_active_schedule_auto_place_job, schedule_auto_place_job_page_payload
from apps.leave.services.staffing import format_staff_count
from apps.leave.services.urgent_closures import detect_previous_year_closure_need, get_active_urgent_closure_payload_map
from apps.leave.services.validation import MIN_CONTINUOUS_PAID_LEAVE_DAYS, get_overlapping_requests, get_overlapping_schedule_items

from apps.leave.services.schedule_drafts.constants import *
from apps.leave.services.schedule_drafts.types import *


def _replace_employee_placements(placements, employee_id, items):
    placements[:] = [
        placement
        for placement in placements
        if placement.employee_id != employee_id
    ]
    placements.extend(
        DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id)
        for item in items
    )


def _create_draft_item_from_assessment(
    schedule,
    employee,
    start_date,
    end_date,
    assessment,
    *,
    source,
    comment,
    generation_run=None,
    selected_candidate_record=None,
    status=VacationScheduleItem.STATUS_DRAFT,
):
    risk_payload = assessment["risk_payload"]
    generated_by_ai = bool(
        generation_run
        and generation_run.mode
        in (VacationScheduleGenerationRun.MODE_NEURAL, VacationScheduleGenerationRun.MODE_HYBRID)
    )
    return VacationScheduleItem.objects.create(
        schedule=schedule,
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        vacation_type="paid",
        chargeable_days=assessment["chargeable_days"],
        status=status,
        source=source,
        risk_score=risk_payload["risk_score"],
        risk_level=risk_payload["risk_level"],
        generated_by_ai=generated_by_ai,
        generation_run=generation_run,
        selected_candidate=selected_candidate_record,
        ai_score=selected_candidate_record.score if generated_by_ai and selected_candidate_record else None,
        ai_confidence=selected_candidate_record.confidence if generated_by_ai and selected_candidate_record else None,
        ai_model_version=selected_candidate_record.model_version if generated_by_ai and selected_candidate_record else "",
        ai_explanation=selected_candidate_record.explanation if generated_by_ai and selected_candidate_record else "",
        was_changed_by_manager=source == VacationScheduleItem.SOURCE_MANUAL,
        manager_comment=comment,
    )


def _create_draft_item_from_generation_candidate(schedule, candidate, *, generation_run=None, selected_candidate_record=None):
    if candidate.assessment is None or not _candidate_passed_hard_rules(candidate):
        raise ValidationError("Нельзя создать пункт черновика из неподходящего кандидата.")
    return _create_draft_item_from_assessment(
        schedule,
        candidate.employee,
        candidate.start_date,
        candidate.end_date,
        candidate.assessment,
        source=candidate.source,
        comment=candidate.comment,
        generation_run=generation_run,
        selected_candidate_record=selected_candidate_record or candidate.stored_candidate,
    )


def _register_draft_item_in_generation_context(context, item):
    context.draft_items_by_employee.setdefault(item.employee_id, []).append(item)
    context.placements.append(DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id))


def _virtual_draft_item(
    employee,
    start_date,
    end_date,
    chargeable_days,
    *,
    source=VacationScheduleItem.SOURCE_MANUAL,
    selected_candidate=None,
):
    return SimpleNamespace(
        id=None,
        employee=employee,
        employee_id=employee.id,
        start_date=start_date,
        end_date=end_date,
        vacation_type="paid",
        chargeable_days=quantize_leave_days(chargeable_days),
        status=VacationScheduleItem.STATUS_DRAFT,
        source=source,
        selected_candidate=selected_candidate,
    )


def _planning_need_after_candidate(employee, year, current_items, start_date, end_date, chargeable_days):
    return build_employee_schedule_planning_need(
        employee,
        year,
        [*list(current_items or []), _virtual_draft_item(employee, start_date, end_date, chargeable_days)],
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )


def _deadline_not_closed_message(planning_need):
    return (
        "Период не закрывает срочный остаток до "
        f"{planning_need['nearest_deadline_label']}. "
        "Выберите даты, которые заканчиваются до срока использования."
    )


def _merge_comment_for_items(items):
    has_manual = any(item.was_changed_by_manager or item.source == VacationScheduleItem.SOURCE_MANUAL for item in items)
    if has_manual:
        return "Соседние части объединены HR в один непрерывный отпуск."
    return "Соседние части объединены системой в один непрерывный отпуск."


def _selected_candidate_kind(item):
    selected_candidate = getattr(item, "selected_candidate", None)
    return getattr(selected_candidate, "kind", "") or ""


def _contains_employee_preference_item(items):
    preference_kinds = {
        VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE,
        VacationScheduleCandidate.KIND_BACKUP_PREFERENCE,
    }
    return any(_selected_candidate_kind(item) in preference_kinds for item in items)


def _should_merge_adjacent_draft_group(items):
    preference_kinds = {
        VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE,
        VacationScheduleCandidate.KIND_BACKUP_PREFERENCE,
    }
    if not _contains_employee_preference_item(items):
        return True
    return all(_selected_candidate_kind(item) in preference_kinds for item in items)


def _merge_adjacent_employee_draft_items(schedule, employee, draft_items_by_employee, placements):
    items = sorted(
        [
            item
            for item in draft_items_by_employee.get(employee.id, [])
            if item.status == VacationScheduleItem.STATUS_DRAFT
        ],
        key=lambda item: (item.start_date, item.end_date, item.id or 0),
    )
    if len(items) < 2:
        return 0

    merged_items = []
    deleted_ids = []
    index = 0

    while index < len(items):
        group = [items[index]]
        group_start = items[index].start_date
        group_end = items[index].end_date
        cursor = index + 1

        while cursor < len(items) and items[cursor].start_date <= group_end + timedelta(days=1):
            group.append(items[cursor])
            group_start = min(group_start, items[cursor].start_date)
            group_end = max(group_end, items[cursor].end_date)
            cursor += 1

        if len(group) == 1:
            merged_items.append(group[0])
            index = cursor
            continue

        if not _should_merge_adjacent_draft_group(group):
            merged_items.extend(group)
            index = cursor
            continue

        keep_item = group[0]
        group_ids = {item.id for item in group if item.id is not None}
        other_placements = [
            placement
            for placement in placements
            if placement.employee_id != employee.id or placement.item_id not in group_ids
        ]
        chargeable_days = get_chargeable_leave_days(group_start, group_end, "paid")
        risk_payload = calculate_vacation_request_risk_with_explanation(
            employee=employee,
            start_date=group_start,
            end_date=group_end,
            vacation_type="paid",
            exclude_schedule_item_id=keep_item.id,
            extra_absent_employee_ids=_extra_absent_ids_for_period(
                other_placements,
                group_start,
                group_end,
                exclude_employee_id=employee.id,
            ),
        )
        has_manual = any(item.was_changed_by_manager or item.source == VacationScheduleItem.SOURCE_MANUAL for item in group)

        keep_item.start_date = group_start
        keep_item.end_date = group_end
        keep_item.chargeable_days = chargeable_days
        keep_item.risk_score = risk_payload["risk_score"]
        keep_item.risk_level = risk_payload["risk_level"]
        keep_item.source = VacationScheduleItem.SOURCE_MANUAL if has_manual else VacationScheduleItem.SOURCE_GENERATED
        keep_item.was_changed_by_manager = has_manual
        keep_item.manager_comment = _merge_comment_for_items(group)
        keep_item.save(
            update_fields=[
                "start_date",
                "end_date",
                "chargeable_days",
                "risk_score",
                "risk_level",
                "source",
                "was_changed_by_manager",
                "manager_comment",
            ]
        )

        remove_ids = [item.id for item in group[1:] if item.id is not None]
        if remove_ids:
            VacationScheduleItem.objects.filter(pk__in=remove_ids).delete()
            deleted_ids.extend(remove_ids)
        merged_items.append(keep_item)
        index = cursor

    if deleted_ids:
        draft_items_by_employee[employee.id] = sorted(
            merged_items,
            key=lambda item: (item.start_date, item.end_date, item.id or 0),
        )
        _replace_employee_placements(placements, employee.id, draft_items_by_employee[employee.id])

    return len(deleted_ids)


def _remove_conflicting_generated_draft_items(schedule):
    removed_count = 0
    while True:
        draft_items = _draft_items_for_schedule(schedule)
        placements = _current_placements_from_items(draft_items)
        conflict_ids = [
            item.id
            for item in draft_items
            if item.id is not None
            and item.source == VacationScheduleItem.SOURCE_GENERATED
            and not item.was_changed_by_manager
            and _candidate_assessment_reason(
                assess_schedule_draft_candidate(
                    item.employee,
                    item.start_date,
                    item.end_date,
                    schedule.year,
                    placements,
                    exclude_schedule_item_id=item.id,
                )
            ).get("kind") == "staffing_conflict"
        ]
        if not conflict_ids:
            return removed_count

        deleted_count, _ = VacationScheduleItem.objects.filter(
            schedule=schedule,
            id__in=conflict_ids,
            source=VacationScheduleItem.SOURCE_GENERATED,
            was_changed_by_manager=False,
        ).delete()
        if deleted_count <= 0:
            return removed_count
        removed_count += deleted_count


@transaction.atomic
def normalize_schedule_draft_adjacent_items(year):
    schedule = VacationSchedule.objects.select_for_update().filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        return 0

    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)

    placements = _current_placements_from_items(draft_items)
    merged_count = 0
    for items in list(draft_items_by_employee.values()):
        if len(items) < 2:
            continue
        employee = items[0].employee
        merged_count += _merge_adjacent_employee_draft_items(
            schedule,
            employee,
            draft_items_by_employee,
            placements,
        )
    return merged_count
