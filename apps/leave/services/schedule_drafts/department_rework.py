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


def get_schedule_department_rework_approval(*, year, approval_id, actor=None):
    if actor is not None and not is_hr_employee(actor):
        raise ValidationError("Доработать возвращённый отдел может только HR.")
    approval = (
        VacationScheduleDepartmentApproval.objects.select_related("schedule", "department", "department_head")
        .filter(id=approval_id, schedule__year=year)
        .first()
    )
    if approval is None:
        raise ValidationError("Согласование отдела не найдено.")
    if approval.schedule.status != VacationSchedule.STATUS_DEPARTMENT_REVIEW:
        raise ValidationError("График сейчас не находится на проверке отделов.")
    if approval.status != VacationScheduleDepartmentApproval.STATUS_REJECTED:
        raise ValidationError("Доработать можно только отдел, который руководитель вернул.")
    return approval


def _department_rework_current_items(schedule, employee):
    return list(
        VacationScheduleItem.objects.select_related("employee")
        .filter(schedule=schedule, employee=employee, status=VacationScheduleItem.STATUS_PLANNED)
        .order_by("start_date", "end_date", "id")
    )


def _department_rework_target_days(current_items):
    return quantize_leave_days(sum((_draft_item_days(item) for item in current_items), Decimal("0.00")))


def _department_rework_planning_need(employee, year, target_days, *, placed_days=Decimal("0.00")):
    target_days = quantize_leave_days(target_days)
    placed_days = quantize_leave_days(placed_days)
    remaining_days = max(target_days - placed_days, Decimal("0.00"))
    zero_days = Decimal("0.00")
    return {
        "available_days": target_days,
        "available_days_label": _days_label(target_days),
        "plan_available_days": target_days,
        "plan_available_days_label": _days_label(target_days),
        "future_available_days": zero_days,
        "future_available_days_label": _days_label(zero_days),
        "mandatory_days": zero_days,
        "mandatory_days_label": _days_label(zero_days),
        "base_target_days": target_days,
        "base_target_days_label": _days_label(target_days),
        "annual_target_days": target_days,
        "annual_target_days_label": _days_label(target_days),
        "optional_annual_days": zero_days,
        "optional_annual_days_label": _days_label(zero_days),
        "requested_preference_days": zero_days,
        "requested_preference_days_label": _days_label(zero_days),
        "remainder_policy": VacationPreference.REMAINDER_AUTO,
        "remainder_policy_label": _remainder_policy_label(VacationPreference.REMAINDER_AUTO),
        "auto_remainder_days": zero_days,
        "auto_remainder_days_label": _days_label(zero_days),
        "remainder_approval_days": zero_days,
        "remainder_approval_days_label": _days_label(zero_days),
        "employee_deferred_days": zero_days,
        "employee_deferred_days_label": _days_label(zero_days),
        "planning_basis": "department_rework",
        "target_days": target_days,
        "target_days_label": _days_label(target_days),
        "placed_days": placed_days,
        "placed_days_label": _days_label(placed_days),
        "open_required_days": remaining_days,
        "open_required_days_label": _days_label(remaining_days),
        "deadline_blocking_days": zero_days,
        "deadline_blocking_days_label": _days_label(zero_days),
        "annual_remaining_days": remaining_days,
        "annual_remaining_days_label": _days_label(remaining_days),
        "manual_task_label": _days_label(target_days),
        "blocking_days": zero_days,
        "blocking_days_label": _days_label(zero_days),
        "deferred_days": zero_days,
        "deferred_days_label": _days_label(zero_days),
        "nearest_deadline": None,
        "nearest_deadline_label": "",
        "status": {
            "key": "rework",
            "label": "Доработка отдела",
            "icon": "edit_calendar",
            "tone": "warning",
        },
        "action_text": f"Нужно заменить пакет отпуска на те же {_days_label(target_days)}.",
        "has_blocker": False,
        "needs_manual_attention": remaining_days > 0,
        "plan_breakdown": [{"label": "Текущий пакет", "value": _days_label(target_days), "tone": "warning"}],
        "mandatory_rows": [],
        "entitlement_rows": [],
    }


def _department_rework_employee_context(*, year, approval_id, employee_id, actor=None, for_update=False):
    approval = get_schedule_department_rework_approval(year=year, approval_id=approval_id, actor=actor)
    schedule = approval.schedule
    if for_update:
        schedule = VacationSchedule.objects.select_for_update().get(pk=schedule.pk)
        approval.schedule = schedule

    employee = next(
        (
            candidate
            for candidate in get_eligible_preference_employees(year)
            if candidate.id == employee_id and candidate.department_id == approval.department_id
        ),
        None,
    )
    if employee is None:
        raise ValidationError("Сотрудник не найден в возвращённом отделе.")

    current_items = _department_rework_current_items(schedule, employee)
    if not current_items:
        raise ValidationError("У сотрудника нет запланированных пунктов графика для доработки.")

    target_days = _department_rework_target_days(current_items)
    if target_days <= 0:
        raise ValidationError("У текущего пакета сотрудника нет списываемых дней.")

    all_items = _draft_items_for_schedule(schedule)
    current_item_ids = {item.id for item in current_items}
    other_items = [item for item in all_items if item.id not in current_item_ids]
    planning_need = _department_rework_planning_need(employee, year, target_days)
    return SimpleNamespace(
        approval=approval,
        schedule=schedule,
        employee=employee,
        current_items=current_items,
        current_item_ids=current_item_ids,
        other_items=other_items,
        placements=_current_placements_from_items(other_items),
        target_days=target_days,
        planning_need=planning_need,
    )


def _department_rework_context_for_suggestions(rework):
    context = _build_draft_generation_context(rework.schedule.year, rework.schedule)
    current_item_ids = set(rework.current_item_ids)
    context.draft_items_by_employee[rework.employee.id] = []
    context.placements = [
        placement
        for placement in context.placements
        if placement.item_id not in current_item_ids and placement.employee_id != rework.employee.id
    ]
    context.planning_need_by_employee[rework.employee.id] = rework.planning_need
    context.excluded_schedule_item_ids = current_item_ids
    return context


def _department_rework_max_package_periods(current_items):
    return max(MANUAL_DRAFT_MAX_PACKAGE_PERIODS, len(current_items or []))


def _department_rework_package_preview_message(can_submit, period_payloads, planning_need, total_days):
    if not period_payloads:
        return "Добавьте хотя бы один период отпуска."
    first_failed = next((period for period in period_payloads if not period["can_place"]), None)
    if first_failed:
        return first_failed["message"] or "Один из периодов нельзя поставить в график."
    if total_days != planning_need["target_days"]:
        return f"Нужно выбрать ровно {planning_need['target_days_label']}."
    if can_submit:
        return "Пакет можно сохранить как доработку отдела."
    return "Проверьте выбранные периоды."


def build_schedule_department_rework_package_preview(*, year, approval_id, employee_id, periods, actor=None):
    rework = _department_rework_employee_context(
        year=year,
        approval_id=approval_id,
        employee_id=employee_id,
        actor=actor,
    )
    normalized_periods = _manual_package_periods_from_input(
        periods,
        year,
        max_periods=_department_rework_max_package_periods(rework.current_items),
    )
    planning_need = rework.planning_need
    placements = list(rework.placements)
    simulated_items = []
    remaining_days = rework.target_days
    period_payloads = []
    total_chargeable_days = Decimal("0.00")
    total_calendar_days = 0
    can_submit = True

    for period in normalized_periods:
        assessment = assess_schedule_draft_candidate(
            rework.employee,
            period["start_date"],
            period["end_date"],
            year,
            placements,
            max_chargeable_days=remaining_days,
            exclude_schedule_item_ids=rework.current_item_ids,
        )
        chargeable_days = quantize_leave_days(assessment.get("chargeable_days") or Decimal("0.00"))
        if assessment.get("can_place"):
            total_chargeable_days += chargeable_days
            total_calendar_days += _calendar_days(period["start_date"], period["end_date"])
            simulated_items.append(_virtual_draft_item(rework.employee, period["start_date"], period["end_date"], chargeable_days))
            placements.append(DraftPlacement(rework.employee.id, period["start_date"], period["end_date"], None))
            remaining_days = max(rework.target_days - total_chargeable_days, Decimal("0.00"))
        else:
            can_submit = False
        period_payloads.append(_manual_period_preview_payload(period, assessment, remaining_days))
        if not assessment.get("can_place"):
            break

    if total_chargeable_days != rework.target_days:
        can_submit = False
    continuous_part_missing = (
        _requires_required_continuous_paid_leave_part(planning_need)
        and not _has_required_continuous_paid_leave_part(simulated_items)
    )
    if can_submit and continuous_part_missing:
        can_submit = False

    highest_risk = max(
        period_payloads,
        key=lambda period: (_risk_level_rank(period["risk_level"]), period["risk_score"]),
        default=None,
    )
    risk_level = highest_risk["risk_level"] if highest_risk else VacationRequest.RISK_LOW
    risk_score = highest_risk["risk_score"] if highest_risk else 0
    risk_is_conflict = any(period["risk_is_conflict"] for period in period_payloads)
    risk_short_reason = next((period["risk_short_reason"] for period in period_payloads if period["risk_short_reason"]), "")
    risk_recommended_action = next(
        (period["risk_recommended_action"] for period in period_payloads if period["risk_recommended_action"]),
        "",
    )
    post_planning_need = _department_rework_planning_need(
        rework.employee,
        year,
        rework.target_days,
        placed_days=total_chargeable_days,
    )
    message = _department_rework_package_preview_message(can_submit, period_payloads, planning_need, total_chargeable_days)
    if (
        continuous_part_missing
        and total_chargeable_days == rework.target_days
        and all(period["can_place"] for period in period_payloads)
    ):
        message = _required_continuous_paid_leave_message()
    return {
        "can_submit": can_submit,
        "message": message,
        "periods": period_payloads,
        "calendar_days": total_calendar_days,
        "chargeable_days": total_chargeable_days,
        "remaining_after_placement": max(rework.target_days - total_chargeable_days, Decimal("0.00")),
        "target_days": planning_need["target_days"],
        "placed_days": planning_need["placed_days"],
        "open_required_days": planning_need["open_required_days"],
        "blocking_after_placement": Decimal("0.00"),
        "annual_remaining_after_placement": post_planning_need["annual_remaining_days"],
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, "Низкий"),
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_tone": _risk_tone(risk_level, risk_is_conflict),
        "risk_short_reason": risk_short_reason,
        "risk_recommended_action": risk_recommended_action,
        "risk_is_conflict": risk_is_conflict,
        "planning_need": planning_need,
        "post_planning_need": post_planning_need,
    }


def _department_rework_context_with_candidate(context, employee, candidate, remaining_days):
    next_context = SimpleNamespace(**context.__dict__)
    next_context.placements = list(context.placements)
    next_context.draft_items_by_employee = {key: list(value) for key, value in context.draft_items_by_employee.items()}
    chargeable_days = _feature_decimal(candidate.metadata.get("chargeable_days"))
    next_context.placements.append(DraftPlacement(employee.id, candidate.start_date, candidate.end_date, None))
    next_context.draft_items_by_employee.setdefault(employee.id, []).append(
        _virtual_draft_item(employee, candidate.start_date, candidate.end_date, chargeable_days)
    )
    next_context.planning_need_by_employee = dict(context.planning_need_by_employee)
    next_context.planning_need_by_employee[employee.id] = _department_rework_planning_need(
        employee,
        context.year,
        remaining_days,
    )
    return next_context


def _build_department_rework_package_from_seed(context, employee, seed_candidate, target_days, *, max_periods=None):
    max_periods = max(1, int(max_periods or MANUAL_DRAFT_MAX_PACKAGE_PERIODS))
    candidates = [seed_candidate]
    total_days = _feature_decimal(seed_candidate.metadata.get("chargeable_days"))
    remaining_days = max(target_days - total_days, Decimal("0.00"))
    current_context = _department_rework_context_with_candidate(context, employee, seed_candidate, remaining_days)
    _, planning_end = _planning_year_bounds(context.year)

    while remaining_days > 0 and len(candidates) < max_periods:
        planning_need = _department_rework_planning_need(employee, context.year, remaining_days)
        payloads = list(
            _iter_auto_candidate_payloads_for_need(
                employee,
                context.year,
                current_context.placements,
                remaining_days,
                planning_end,
                urgent=False,
                allow_short_parts=remaining_days < MIN_CONTINUOUS_PAID_LEAVE_DAYS,
                max_chargeable_days=remaining_days,
                limit=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
                exclude_schedule_item_ids=current_context.excluded_schedule_item_ids,
                preference_pair=current_context.preference_pair_by_employee.get(employee.id),
            )
        )
        next_candidates = _rank_generation_candidates(
            _auto_generation_candidates_from_payloads(
                employee,
                payloads,
                kind=DRAFT_CANDIDATE_AUTO,
                comment="Предложение модуля: доработка следующей части отпуска.",
                planning_need=planning_need,
                metadata={"department_rework_continuation": True},
            )
        )
        next_candidate = _select_first_passed_generation_candidate(next_candidates)
        if next_candidate is None:
            break
        candidates.append(next_candidate)
        total_days += _feature_decimal(next_candidate.metadata.get("chargeable_days"))
        remaining_days = max(target_days - total_days, Decimal("0.00"))
        current_context = _department_rework_context_with_candidate(current_context, employee, next_candidate, remaining_days)

    return DraftGenerationCandidatePackage(
        employee=employee,
        candidates=candidates,
        source=VacationScheduleItem.SOURCE_MANUAL,
        explanation=(
            "Пакет полностью заменяет текущий отпуск сотрудника."
            if total_days == target_days
            else "Пакет не закрывает полный объём текущего отпуска."
        ),
        metadata={
            "package_kind": "department_rework_suggestion",
            "total_chargeable_days": total_days,
            "periods_count": len(candidates),
            "remaining_after_package": remaining_days,
            "package_closes_need": total_days == target_days,
        },
    )


def _department_rework_candidate_packages(rework, *, limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS):
    context = _department_rework_context_for_suggestions(rework)
    max_periods = _department_rework_max_package_periods(rework.current_items)
    seeds = [
        candidate
        for candidate in _manual_package_target_candidates(context, rework.employee, [], rework.planning_need)
        if _candidate_passed_hard_rules(candidate)
    ]
    packages = []
    seen = set()
    for seed in seeds:
        package = _build_department_rework_package_from_seed(
            context,
            rework.employee,
            seed,
            rework.target_days,
            max_periods=max_periods,
        )
        if _package_total_days(package.candidates) != rework.target_days:
            continue
        if not all(_candidate_passed_hard_rules(candidate) for candidate in package.candidates):
            continue
        key = _manual_package_key(package)
        if not key or key in seen:
            continue
        seen.add(key)
        packages.append(package)
        if len(packages) >= limit * 2:
            break
    return _rank_manual_candidate_packages(packages)[:limit]


def build_schedule_department_rework_suggestions(*, year, approval_id, employee_id, actor=None, limit=MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS):
    limit = max(1, min(int(limit or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS), MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS))
    rework = _department_rework_employee_context(
        year=year,
        approval_id=approval_id,
        employee_id=employee_id,
        actor=actor,
    )
    packages = _department_rework_candidate_packages(rework, limit=limit)
    visible_packages = packages[:limit]
    max_periods = _department_rework_max_package_periods(rework.current_items)
    return {
        "suggestion_schema_version": MANUAL_DRAFT_SUGGESTION_CACHE_SCHEMA_VERSION,
        "employee_id": rework.employee.id,
        "employee_name": rework.employee.full_name,
        "needed_label": rework.planning_need["target_days_label"],
        "status_label": "Доработка отдела",
        "target_days_label": rework.planning_need["target_days_label"],
        "placed_days_label": _days_label(rework.target_days),
        "planning_year": year,
        "date_min": date(year, 1, 1).isoformat(),
        "date_max": date(year, 12, 31).isoformat(),
        "preference_option": None,
        "max_periods": max_periods,
        "max_periods_label": f"до {max_periods} периодов",
        "visible_limit": limit,
        "options": [_manual_package_payload(package, rank=index) for index, package in enumerate(visible_packages, start=1)],
        "total_candidates": len(packages),
        "safe_candidates": len(packages),
        "shown_candidates": len(visible_packages),
        "has_more_options": len(packages) > len(visible_packages),
    }


@transaction.atomic
def replace_department_rework_employee_package(*, year, approval_id, employee_id, periods, actor):
    rework = _department_rework_employee_context(
        year=year,
        approval_id=approval_id,
        employee_id=employee_id,
        actor=actor,
        for_update=True,
    )
    preview = build_schedule_department_rework_package_preview(
        year=year,
        approval_id=approval_id,
        employee_id=employee_id,
        periods=periods,
        actor=actor,
    )
    if not preview["can_submit"]:
        raise ValidationError(preview["message"])

    suggestion_packages = _department_rework_candidate_packages(rework, limit=3)
    selected_candidates = [
        _manual_candidate_from_period_preview(
            rework.employee,
            period_payload,
            rework.planning_need,
            year,
            actor=actor,
            order=index,
        )
        for index, period_payload in enumerate(preview["periods"], start=1)
    ]
    selected_package = DraftGenerationCandidatePackage(
        employee=rework.employee,
        candidates=selected_candidates,
        source=VacationScheduleItem.SOURCE_MANUAL,
        explanation=preview["message"],
        metadata={
            "package_kind": "department_rework_selected",
            "total_chargeable_days": preview["chargeable_days"],
            "remaining_after_placement": preview["remaining_after_placement"],
            "approval_id": rework.approval.id,
        },
    )

    generation_run = _start_schedule_generation_run(rework.schedule, actor)
    _, selected_candidate_records, selected_period_records = _persist_manual_candidate_package(
        generation_run,
        rework.schedule,
        selected_package,
        decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
        decision_rank=1,
    )
    selected_key = _manual_package_key(selected_package)
    rejected_rank = 2
    for package in suggestion_packages:
        if _manual_package_key(package) == selected_key:
            continue
        _persist_manual_candidate_package(
            generation_run,
            rework.schedule,
            package,
            decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
            decision_rank=rejected_rank,
        )
        rejected_rank += 1

    old_item_ids = [item.id for item in rework.current_items]
    VacationScheduleItem.objects.filter(pk__in=old_item_ids).update(
        status=VacationScheduleItem.STATUS_CANCELLED,
        manager_comment=f"Заменено при доработке отдела: {actor.full_name if actor else 'HR'}.",
    )

    created_items = []
    for period_payload, candidate_record in zip(preview["periods"], selected_candidate_records):
        created_items.append(
            _create_draft_item_from_assessment(
                rework.schedule,
                rework.employee,
                period_payload["start_date"],
                period_payload["end_date"],
                period_payload["assessment"],
                source=VacationScheduleItem.SOURCE_MANUAL,
                comment=f"Доработано HR после возврата отдела: {actor.full_name if actor else 'HR'}.",
                generation_run=generation_run,
                selected_candidate_record=candidate_record,
                status=VacationScheduleItem.STATUS_PLANNED,
            )
        )

    for period_record in selected_period_records:
        covering_item = _find_covering_draft_item(created_items, period_record.start_date, period_record.end_date)
        if covering_item is not None:
            period_record.schedule_item = covering_item
            period_record.save(update_fields=["schedule_item"])

    _finish_schedule_generation_run(generation_run, manual_count=0)
    _invalidate_schedule_draft_manual_suggestion_cache(rework.schedule)
    return {
        "schedule": rework.schedule,
        "approval": rework.approval,
        "old_items_count": len(old_item_ids),
        "items": created_items,
        "generation_run": generation_run,
    }
