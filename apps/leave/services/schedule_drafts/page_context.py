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


def build_manual_schedule_draft_preview(*, year, employee_id, start_date, end_date):
    schedule = VacationSchedule.objects.filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    employee = next(
        (candidate for candidate in get_eligible_preference_employees(year) if candidate.id == employee_id),
        None,
    )
    if employee is None:
        raise ValidationError("Сотрудник не участвует в планировании графика за этот год.")

    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)

    planning_need = build_employee_schedule_planning_need(
        employee,
        year,
        draft_items_by_employee.get(employee.id, []),
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )
    calendar_days = (end_date - start_date).days + 1 if end_date >= start_date else 0
    chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid") if calendar_days else 0
    placements = _current_placements_from_items(draft_items)
    current_items = draft_items_by_employee.get(employee.id, [])
    adjacent_items = _adjacent_employee_items(current_items, start_date, end_date)
    adjacent_ids = {item.id for item in adjacent_items if item.id is not None}
    merged_start = min([start_date, *(item.start_date for item in adjacent_items)])
    merged_end = max([end_date, *(item.end_date for item in adjacent_items)])
    merged_chargeable_days = get_chargeable_leave_days(merged_start, merged_end, "paid") if calendar_days else 0
    has_short_gap = _has_short_gap_to_employee_placement(
        placements,
        employee.id,
        start_date,
        end_date,
        exclude_item_ids=adjacent_ids,
    )

    if not planning_need["needs_manual_attention"]:
        return {
            "can_submit": False,
            "message": "По сотруднику уже закрыта плановая потребность.",
            "calendar_days": calendar_days,
            "chargeable_days": chargeable_days,
            "merged_calendar_days": (merged_end - merged_start).days + 1 if calendar_days else 0,
            "merged_chargeable_days": merged_chargeable_days,
            "remaining_after_placement": planning_need["open_required_days"],
            "risk_label": "Низкий",
            "risk_score": 0,
            "risk_short_reason": "",
            "risk_recommended_action": "",
            "risk_is_conflict": False,
            "will_merge": bool(adjacent_items),
            "merged_period_label": _period_label(merged_start, merged_end) if calendar_days else "",
            "short_gap_warning": has_short_gap,
            "planning_need": planning_need,
        }

    assessment = assess_schedule_draft_candidate(
        employee,
        start_date,
        end_date,
        year,
        placements,
        max_chargeable_days=planning_need["open_required_days"],
    )
    can_submit = bool(assessment["can_place"])
    message = "Период можно поставить в черновик."
    risk_payload = assessment.get("risk_payload")
    risk_explanation = (risk_payload or {}).get("risk_explanation") or {}
    post_planning_need = planning_need

    if can_submit:
        post_planning_need = _planning_need_after_candidate(
            employee,
            year,
            current_items,
            start_date,
            end_date,
            assessment["chargeable_days"],
        )
        other_placements = [
            placement
            for placement in placements
            if placement.employee_id != employee.id or placement.item_id not in adjacent_ids
        ]
        risk_payload = calculate_vacation_request_risk_with_explanation(
            employee=employee,
            start_date=merged_start,
            end_date=merged_end,
            vacation_type="paid",
            extra_absent_employee_ids=_extra_absent_ids_for_period(
                other_placements,
                merged_start,
                merged_end,
                exclude_employee_id=employee.id,
            ),
        )
        risk_explanation = risk_payload.get("risk_explanation") or {}
        if adjacent_items:
            message = "Период будет объединён с соседней частью в один непрерывный отпуск."
        if has_short_gap:
            message = (
                "Поставить можно, но рядом уже есть другой отпуск с коротким разрывом. "
                "Проверьте, что такое разделение согласовано с сотрудником."
            )
        if risk_explanation.get("is_conflict"):
            message = "Поставить можно, но после размещения будет конфликт состава."
        elif risk_payload.get("risk_level") == VacationRequest.RISK_HIGH:
            message = "Поставить можно, но риск состава высокий."
        if planning_need["has_blocker"] and post_planning_need["blocking_days"] > 0:
            can_submit = False
            message = _deadline_not_closed_message(planning_need)
        if can_submit and _requires_required_continuous_paid_leave_part(planning_need):
            simulated_items = [
                *list(current_items or []),
                _virtual_draft_item(employee, start_date, end_date, assessment["chargeable_days"]),
            ]
            if not _has_required_continuous_paid_leave_part(simulated_items):
                can_submit = False
                message = _required_continuous_paid_leave_message()
    else:
        message = assessment["reason"]["text"]
        if risk_payload is None:
            risk_payload = {
                "risk_score": 0,
                "risk_level": VacationRequest.RISK_LOW,
                "balance_after_request": get_employee_available_balance(employee),
            }

    risk_label = dict(VacationRequest.RISK_CHOICES).get(risk_payload["risk_level"], "Низкий")
    remaining_after_placement = post_planning_need["open_required_days"]
    return {
        "can_submit": can_submit,
        "message": message,
        "calendar_days": calendar_days,
        "chargeable_days": chargeable_days,
        "merged_calendar_days": (merged_end - merged_start).days + 1 if calendar_days else 0,
        "merged_chargeable_days": merged_chargeable_days,
        "remaining_after_placement": remaining_after_placement,
        "risk_label": risk_label,
        "risk_score": risk_payload["risk_score"],
        "risk_short_reason": risk_explanation.get("short_reason", ""),
        "risk_recommended_action": risk_explanation.get("recommended_action", ""),
        "risk_is_conflict": risk_explanation.get("is_conflict", False),
        "will_merge": bool(adjacent_items),
        "merged_period_label": _period_label(merged_start, merged_end) if calendar_days else "",
        "short_gap_warning": has_short_gap,
        "blocking_after_placement": post_planning_need["blocking_days"],
        "annual_remaining_after_placement": post_planning_need["annual_remaining_days"],
        "planning_need": planning_need,
    }


def _source_label_for_item(item, pair):
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    backup = pair.get(VacationPreference.PRIORITY_BACKUP)
    if primary and item.start_date == primary.start_date and item.end_date == primary.end_date:
        return "Основное пожелание"
    if backup and item.start_date == backup.start_date and item.end_date == backup.end_date:
        return "Запасной период"
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "Вручную"
    return "Сформировано системой"


def _employee_org_payload(employee):
    position = employee.employee_position
    group = position.production_group if position and position.production_group_id else None
    return {
        "department_name": employee.department.name if employee.department_id else "Без отдела",
        "group_name": group.name if group else "Без группы",
        "position": employee.position,
    }


def _employee_identity_payload(employee):
    identity = get_employee_identity_presentation(employee)
    return {
        "role_icon": identity["employee_role_icon"],
        "role_icon_type": identity["employee_role_icon_type"],
        "role_variant": identity["employee_role_variant"],
        "role_label": identity["employee_role_label"],
        "management_badges": identity["employee_management_badges"],
        "new_hire_badge": identity["employee_new_hire_badge"],
    }


def _profile_url(employee, year):
    params = urlencode(
        {
            "from": "preferences",
            "back_url": schedule_draft_url(year),
            "back_label": "К черновику",
        }
    )
    return f"{reverse('employee_profile', args=[employee.id])}?{params}"


def _calendar_employee_url(employee, year, *, focus_start=None, focus_end=None):
    params = urlencode(
        {
            "view": "year",
            "year": year,
            "employee": employee.id,
            "calendar_modal": "employee_detail",
            "calendar_employee": employee.id,
        }
    )
    if focus_start and focus_end:
        params = urlencode(
            {
                "view": "year",
                "year": year,
                "employee": employee.id,
                "calendar_modal": "employee_detail",
                "calendar_employee": employee.id,
                "calendar_focus_employee": employee.id,
                "calendar_focus_start": focus_start.isoformat(),
                "calendar_focus_end": focus_end.isoformat(),
            }
        )
    return f"{reverse('calendar')}?{params}"


def _placement_source_hint(item, pair):
    source_label = _source_label_for_item(item, pair)
    if source_label == "Основное пожелание":
        return "Совпало с основным пожеланием"
    if source_label == "Запасной период":
        return "Совпало с запасным периодом"
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "Поставлено вручную HR"
    if item.selected_candidate and item.selected_candidate.kind in {
        VacationScheduleCandidate.KIND_AUTO,
        VacationScheduleCandidate.KIND_AUTO_URGENT,
        VacationScheduleCandidate.KIND_AUTO_TOPUP,
    }:
        return "Подобрано модулем"
    return "Сформировано системой"


def _draft_view_item_statuses(schedule):
    if schedule is None:
        return (VacationScheduleItem.STATUS_DRAFT,)
    if schedule.status == VacationSchedule.STATUS_DEPARTMENT_REVIEW:
        return (VacationScheduleItem.STATUS_PLANNED,)
    if schedule.status == VacationSchedule.STATUS_APPROVED:
        return (VacationScheduleItem.STATUS_APPROVED,)
    return (VacationScheduleItem.STATUS_DRAFT,)


def _draft_items_for_schedule(schedule):
    if schedule is None:
        return []

    return list(
        schedule.items.select_related(
            "employee",
            "employee__department",
            "employee__employee_position",
            "employee__employee_position__production_group",
            "generation_run",
            "selected_candidate",
        )
        .filter(status__in=_draft_view_item_statuses(schedule))
        .order_by("start_date", "employee__last_name", "employee__first_name", "employee__middle_name")
    )


@transaction.atomic


def _generic_risk_summary(risk_level):
    if risk_level == VacationRequest.RISK_HIGH:
        return "Высокий риск сохранен при создании черновика."
    if risk_level == VacationRequest.RISK_MEDIUM:
        return "Средний риск сохранен при создании черновика."
    return "Критичных пересечений не найдено."


def _percent_label(value):
    if value is None:
        return ""
    value = Decimal(value).quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return f"{int(value)}%"
    return f"{str(value).replace('.', ',')}%"


def _candidate_recommendation_label(recommendation):
    return {
        "prefer": "Предпочтительный",
        "normal": "Допустимый",
        "avoid": "С осторожностью",
        "blocked": "Заблокирован",
    }.get(recommendation or "", "Оценен")


def _draft_item_ai_context(item):
    if not item.generated_by_ai or item.ai_score is None:
        return None

    selected_candidate = item.selected_candidate
    recommendation = ""
    decision_rank = None
    if selected_candidate is not None:
        recommendation = (selected_candidate.features or {}).get("scoring_recommendation", "")
        decision_rank = selected_candidate.decision_rank

    return {
        "score": item.ai_score,
        "score_label": _percent_label(item.ai_score),
        "confidence": item.ai_confidence,
        "confidence_label": _percent_label(item.ai_confidence),
        "model_version": item.ai_model_version,
        "explanation": item.ai_explanation,
        "recommendation": recommendation,
        "recommendation_label": _candidate_recommendation_label(recommendation),
        "decision_rank": decision_rank,
    }


def _package_recommendation_label(package):
    features = package.features or {}
    recommendation = features.get("package_recommendation")
    if not recommendation and package.score is not None:
        if package.score >= Decimal("80.00"):
            recommendation = "prefer"
        elif package.score >= Decimal("55.00"):
            recommendation = "normal"
        else:
            recommendation = "avoid"
    return _candidate_recommendation_label(recommendation)


def _package_preference_label(features):
    if features.get("has_primary_preference_period"):
        return "Учтено основное пожелание"
    if features.get("has_backup_preference_period"):
        return "Учтено запасное пожелание"
    if features.get("has_preference_period"):
        return "Учтено пожелание"
    return "Пожелание не использовано"


def _package_period_payload(period):
    features = period.features if isinstance(period.features, dict) else {}
    risk_level = period.risk_level or VacationScheduleItem.RISK_LOW
    return {
        "period_label": _short_period_label(period.start_date, period.end_date),
        "full_period_label": _period_label(period.start_date, period.end_date),
        "chargeable_days_label": _days_label(period.chargeable_days),
        "risk_score": period.risk_score,
        "risk_level": risk_level,
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, "Низкий"),
        "risk_tone": _risk_tone(risk_level, bool(features.get("risk_is_conflict"))),
        "hard_rules_label": "Правила пройдены" if period.passed_hard_rules else "Есть блокировка",
        "block_reason": period.block_reason,
    }


def _package_payload(package, *, alternatives_count=None):
    features = package.features or {}
    periods = [_package_period_payload(period) for period in package.periods.all()]
    risk_level = package.risk_level or VacationScheduleItem.RISK_LOW
    hard_rules_label = "Жесткие правила пройдены" if package.passed_hard_rules else "Есть блокировка правил"
    package_days_label = _days_label(package.total_chargeable_days)
    preference_label = _package_preference_label(features)
    stats = [
        {"label": "Закрыто дней", "value": package_days_label},
        {"label": "Пожелания", "value": preference_label},
        {"label": "Правила", "value": hard_rules_label},
        {
            "label": "Максимальный риск",
            "value": f"{dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, 'Низкий')} · {package.risk_score}%",
        },
    ]
    if package.model_version:
        stats.append({"label": "Модель", "value": package.model_version})
    if package.confidence is not None:
        stats.append({"label": "Уверенность", "value": _percent_label(package.confidence)})

    factors = [
        f"пакет закрывает {package_days_label}",
        preference_label.lower(),
        hard_rules_label.lower(),
        f"максимальный риск: {dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, 'Низкий').lower()}",
    ]
    if alternatives_count:
        factors.append(f"сравнено альтернатив: {alternatives_count}")

    return {
        "id": package.id,
        "periods_count": package.periods_count,
        "total_chargeable_days": package.total_chargeable_days,
        "total_chargeable_days_label": package_days_label,
        "score": package.score,
        "score_label": _percent_label(package.score),
        "confidence": package.confidence,
        "confidence_label": _percent_label(package.confidence),
        "model_version": package.model_version,
        "recommendation_label": _package_recommendation_label(package),
        "explanation": package.explanation,
        "decision": package.decision,
        "decision_label": dict(VacationScheduleCandidatePackage.DECISION_CHOICES).get(package.decision, "Не выбран"),
        "decision_rank": package.decision_rank,
        "risk_score": package.risk_score,
        "risk_level": risk_level,
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, "Низкий"),
        "risk_tone": _risk_tone(risk_level),
        "passed_hard_rules": package.passed_hard_rules,
        "hard_rules_label": hard_rules_label,
        "preference_label": preference_label,
        "stats": stats,
        "factors": factors,
        "periods": periods,
    }


def _selected_package_for_item(item):
    package = (
        VacationScheduleCandidatePackage.objects.filter(
            schedule=item.schedule,
            employee=item.employee,
            decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
            periods__schedule_item=item,
        )
        .prefetch_related("periods")
        .order_by("decision_rank", "-score", "-confidence", "id")
        .first()
    )
    if package is not None:
        return package
    if item.generation_run_id:
        return (
            VacationScheduleCandidatePackage.objects.filter(
                schedule=item.schedule,
                employee=item.employee,
                generation_run_id=item.generation_run_id,
                decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
            )
            .prefetch_related("periods")
            .order_by("decision_rank", "-selected_at", "-score", "id")
            .first()
        )
    return None


def _package_report_for_item(item):
    selected_package = _selected_package_for_item(item)
    if selected_package is None:
        return None

    alternatives_queryset = VacationScheduleCandidatePackage.objects.filter(
        schedule=item.schedule,
        employee=item.employee,
    ).exclude(pk=selected_package.pk)
    if selected_package.generation_run_id:
        alternatives_queryset = alternatives_queryset.filter(generation_run_id=selected_package.generation_run_id)
    alternatives_queryset = alternatives_queryset.prefetch_related("periods").order_by(
        "decision_rank",
        "-score",
        "-confidence",
        "id",
    )
    alternatives_count = alternatives_queryset.count()
    alternatives = [
        _package_payload(package)
        for package in alternatives_queryset[:3]
    ]
    selected_payload = _package_payload(selected_package, alternatives_count=alternatives_count)
    selected_payload["alternatives"] = alternatives
    selected_payload["alternatives_count"] = alternatives_count
    selected_payload["comparison_note"] = (
        f"Показаны ближайшие альтернативы: {len(alternatives)} из {alternatives_count}."
        if alternatives_count
        else "Для этого пункта не сохранены альтернативные пакеты."
    )
    return selected_payload


def _candidate_kind_label(kind):
    return dict(VacationScheduleCandidate.KIND_CHOICES).get(kind, "Кандидат")


def _candidate_decision_label(decision):
    return dict(VacationScheduleCandidate.DECISION_CHOICES).get(decision, "Не выбран")


def _candidate_decision_tone(decision):
    return {
        VacationScheduleCandidate.DECISION_SELECTED: "selected",
        VacationScheduleCandidate.DECISION_REJECTED: "rejected",
        VacationScheduleCandidate.DECISION_BLOCKED: "blocked",
    }.get(decision, "pending")


def _risk_tone(risk_level, is_conflict=False):
    if is_conflict:
        return "conflict"
    if risk_level == VacationRequest.RISK_HIGH:
        return "high"
    if risk_level == VacationRequest.RISK_MEDIUM:
        return "medium"
    return "low"


def _stored_candidate_payload(candidate):
    features = candidate.features or {}
    return {
        "id": candidate.id,
        "period_label": _short_period_label(candidate.start_date, candidate.end_date),
        "full_period_label": _period_label(candidate.start_date, candidate.end_date),
        "start_date": candidate.start_date.isoformat() if candidate.start_date else "",
        "end_date": candidate.end_date.isoformat() if candidate.end_date else "",
        "chargeable_days": candidate.chargeable_days,
        "chargeable_days_label": _days_label(candidate.chargeable_days),
        "kind": candidate.kind,
        "kind_label": _candidate_kind_label(candidate.kind),
        "decision": candidate.decision,
        "decision_label": _candidate_decision_label(candidate.decision),
        "decision_tone": _candidate_decision_tone(candidate.decision),
        "decision_rank": candidate.decision_rank,
        "passed_hard_rules": candidate.passed_hard_rules,
        "hard_rules_label": "Прошел жесткие правила" if candidate.passed_hard_rules else "Заблокирован жесткими правилами",
        "block_reason": candidate.block_reason,
        "risk_score": candidate.risk_score,
        "risk_level": candidate.risk_level,
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(candidate.risk_level, "Низкий"),
        "risk_tone": _risk_tone(candidate.risk_level, bool(features.get("risk_is_conflict"))),
        "score": candidate.score,
        "score_label": _percent_label(candidate.score),
        "confidence": candidate.confidence,
        "confidence_label": _percent_label(candidate.confidence),
        "model_version": candidate.model_version,
        "recommendation_label": _candidate_recommendation_label(features.get("scoring_recommendation")),
        "explanation": candidate.explanation,
        "is_selected": candidate.decision == VacationScheduleCandidate.DECISION_SELECTED,
    }


def build_schedule_draft_item_review_context(item, *, actor=None):
    selected_candidate = item.selected_candidate
    candidate_filters = {
        "schedule": item.schedule,
        "employee": item.employee,
    }
    if selected_candidate and selected_candidate.generation_run_id:
        candidate_filters["generation_run_id"] = selected_candidate.generation_run_id
    elif item.generation_run_id:
        candidate_filters["generation_run_id"] = item.generation_run_id

    candidates = list(
        VacationScheduleCandidate.objects.filter(**candidate_filters)
        .select_related("generation_run")
        .order_by("decision_rank", "-score", "-confidence", "start_date", "id")
    )
    feedback_context = build_schedule_candidate_feedback_context([item], actor=actor).get(
        item.id,
        {
            "summary": {"total": 0, "items": [], "counts": {}},
            "current": None,
            "can_submit": False,
        },
    )
    feedback_context = {
        **feedback_context,
        "action_url": reverse("schedule_draft_candidate_feedback", args=[item.schedule.year, item.id]),
    }
    pair = get_employee_preference_pair(item.employee, item.schedule.year)
    return {
        "item": item,
        "employee": item.employee,
        "employee_name": item.employee.full_name,
        "period_label": _period_label(item.start_date, item.end_date),
        "short_period_label": _short_period_label(item.start_date, item.end_date),
        "chargeable_days_label": _days_label(item.chargeable_days),
        "source_hint": _placement_source_hint(item, pair),
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(item.risk_level, "Низкий"),
        "risk_score": item.risk_score,
        "risk_tone": _risk_tone(item.risk_level),
        "ai_decision": _draft_item_ai_context(item),
        "package_report": _package_report_for_item(item),
        "feedback": feedback_context,
        "candidates": [_stored_candidate_payload(candidate) for candidate in candidates],
        "calendar_url": _calendar_employee_url(
            item.employee,
            item.schedule.year,
            focus_start=item.start_date,
            focus_end=item.end_date,
        ),
    }


def _generation_candidate_payload(candidate, *, rank=None):
    risk_level = candidate.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW
    risk_score = int(candidate.metadata.get("risk_score") or 0)
    is_conflict = bool((candidate.assessment or {}).get("risk_payload", {}).get("risk_explanation", {}).get("is_conflict"))
    staffing_summary = _staffing_summary_from_candidate(candidate)
    preference_match = candidate.metadata.get("preference_match") or ""
    preference_match_label = candidate.metadata.get("preference_match_label") or ""
    passed_hard_rules = _candidate_passed_hard_rules(candidate)
    return {
        "rank": rank,
        "period_label": _short_period_label(candidate.start_date, candidate.end_date),
        "full_period_label": _period_label(candidate.start_date, candidate.end_date),
        "start_date": candidate.start_date.isoformat() if candidate.start_date else "",
        "end_date": candidate.end_date.isoformat() if candidate.end_date else "",
        "chargeable_days": int(candidate.metadata.get("chargeable_days") or 0),
        "chargeable_days_label": _days_label(candidate.metadata.get("chargeable_days") or 0),
        "kind": candidate.kind,
        "kind_label": _candidate_kind_label(candidate.kind),
        "passed_hard_rules": passed_hard_rules,
        "can_apply": passed_hard_rules,
        "preference_match": preference_match,
        "preference_match_label": preference_match_label,
        "is_preference_candidate": bool(candidate.metadata.get("is_preference_candidate") or preference_match),
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, "Низкий"),
        "risk_tone": _risk_tone(risk_level, is_conflict),
        "staffing_summary": staffing_summary,
        "staffing_chips": list(staffing_summary.get("chips") or []),
        "score": _candidate_scoring_decimal(candidate, "scoring_score"),
        "score_label": _percent_label(_candidate_scoring_decimal(candidate, "scoring_score")),
        "confidence": _candidate_scoring_decimal(candidate, "scoring_confidence"),
        "confidence_label": _percent_label(_candidate_scoring_decimal(candidate, "scoring_confidence")),
        "recommendation_label": _candidate_recommendation_label(candidate.metadata.get("scoring_recommendation")),
        "explanation": candidate.metadata.get("scoring_explanation") or candidate.comment,
        "message": candidate.comment,
    }


def _draft_item_rows(schedule, year, items, planning_need_by_employee, preference_pair_by_employee=None, actor=None):
    if schedule is None:
        return []

    placements = [DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id) for item in items]
    preference_pair_by_employee = preference_pair_by_employee or {}
    feedback_context_by_item = build_schedule_candidate_feedback_context(items, actor=actor)
    rows = []
    for item in items:
        employee = item.employee
        pair = preference_pair_by_employee.get(employee.id) or get_employee_preference_pair(employee, year)
        primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
        backup = pair.get(VacationPreference.PRIORITY_BACKUP)
        risk_score = int(item.risk_score or 0)
        risk_level = item.risk_level or VacationRequest.RISK_LOW
        explanation = {}
        risk_summary = _generic_risk_summary(risk_level)

        if risk_level == VacationRequest.RISK_HIGH:
            extra_absent_ids = _extra_absent_ids_for_period(
                placements,
                item.start_date,
                item.end_date,
                exclude_employee_id=employee.id,
            )
            risk_payload = calculate_vacation_request_risk_with_explanation(
                employee=employee,
                start_date=item.start_date,
                end_date=item.end_date,
                vacation_type=item.vacation_type,
                exclude_schedule_item_id=item.id,
                extra_absent_employee_ids=extra_absent_ids,
            )
            risk_score = risk_payload["risk_score"]
            risk_level = risk_payload["risk_level"]
            explanation = risk_payload.get("risk_explanation") or {}
            risk_summary = explanation.get("short_reason") or risk_summary

        has_conflict = bool(explanation.get("is_conflict"))
        has_high_risk = risk_level == VacationRequest.RISK_HIGH
        org = _employee_org_payload(employee)
        identity = _employee_identity_payload(employee)
        source_label = _source_label_for_item(item, pair)
        period_label = _short_period_label(item.start_date, item.end_date)
        chargeable_days_label = _days_label(item.chargeable_days)
        feedback_context = feedback_context_by_item.get(
            item.id,
            {
                "summary": {"total": 0, "items": [], "counts": {}},
                "current": None,
                "can_submit": False,
            },
        )
        feedback_context = {
            **feedback_context,
            "action_url": reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
        }
        rows.append(
            {
                "item": item,
                "employee": employee,
                "employee_name": employee.full_name,
                "department_name": org["department_name"],
                "group_name": org["group_name"],
                "position": org["position"],
                "period_label": period_label,
                "full_period_label": _period_label(item.start_date, item.end_date),
                "assigned_label": f"Назначено: {period_label}",
                "source_label": source_label,
                "source_hint": _placement_source_hint(item, pair),
                "primary_label": _period_label(primary.start_date if primary else None, primary.end_date if primary else None),
                "backup_label": _period_label(backup.start_date if backup else None, backup.end_date if backup else None),
                "chargeable_days": item.chargeable_days,
                "chargeable_days_label": chargeable_days_label,
                "risk_score": risk_score,
                "risk_level": risk_level,
                "risk_label": dict(VacationRequest.RISK_CHOICES).get(risk_level, "Низкий"),
                "has_conflict": has_conflict,
                "has_high_risk": has_high_risk,
                "issue_label": "Конфликт" if has_conflict else ("Высокий риск" if has_high_risk else "Без проблем"),
                "issue_icon": "warning" if has_conflict else ("bolt" if has_high_risk else "verified"),
                "risk_summary": risk_summary,
                "risk_details": list(explanation.get("details") or [])[:3],
                "ai_decision": _draft_item_ai_context(item),
                "feedback": feedback_context,
                "profile_url": _profile_url(employee, year),
                "calendar_url": _calendar_employee_url(
                    employee,
                    year,
                    focus_start=item.start_date,
                    focus_end=item.end_date,
                ),
                "review_url": reverse("schedule_draft_item_review", args=[year, item.id]),
                "day_calculation_url": reverse("schedule_draft_day_calculation", args=[year, employee.id]),
                "manual_anchor": f"draft-manual-{employee.id}",
                "planning_need": planning_need_by_employee.get(employee.id),
                **identity,
            }
        )
    return rows


def _manual_row_for_employee(
    employee,
    year,
    placed_employee_ids,
    placed_rows,
    planning_need,
    preference_state_by_employee=None,
    preference_pair_by_employee=None,
    active_urgent_closure_by_employee=None,
):
    preference_state_by_employee = preference_state_by_employee or {}
    preference_pair_by_employee = preference_pair_by_employee or {}
    active_urgent_closure_by_employee = active_urgent_closure_by_employee or {}
    state = preference_state_by_employee.get(employee.id)
    if state is None:
        state = get_employee_preference_state(employee, year)
    pair = preference_pair_by_employee.get(employee.id)
    if pair is None:
        pair = get_employee_preference_pair(employee, year)
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    backup = pair.get(VacationPreference.PRIORITY_BACKUP)
    has_draft_item = employee.id in placed_employee_ids
    reason = None
    active_urgent_closure = active_urgent_closure_by_employee.get(employee.id)

    if not planning_need["needs_manual_attention"] and active_urgent_closure is None:
        return None

    if active_urgent_closure is not None and not planning_need["needs_manual_attention"]:
        reason = _manual_reason(
            "deadline_blocker",
            f"Открыто срочное закрытие {active_urgent_closure['required_days_label']}",
            (
                f"Период уже отправлен на согласование до "
                f"{active_urgent_closure['deadline_label']}; HR нужно проконтролировать решение."
            ),
        )
    elif planning_need["has_blocker"]:
        detail = (
            f"Сначала нужно закрыть {_days_label(planning_need['blocking_days'])} "
            f"до {planning_need['nearest_deadline_label']}."
        )
        if planning_need["annual_remaining_days"] > 0:
            remaining_label = (
                "добора незакрытых дней"
                if planning_need["remainder_policy"] == VacationPreference.REMAINDER_AUTO
                else "по пожеланию"
            )
            detail = (
                f"{detail} После срочного остатка останется "
                f"{_days_label(planning_need['annual_remaining_days'])} {remaining_label}."
            )
        reason = _manual_reason(
            "deadline_blocker",
            f"Срочно закрыть {_days_label(planning_need['blocking_days'])}",
            detail,
        )
    elif state in {VacationPreference.STATUS_PENDING, "missing"}:
        reason = _manual_reason("pending", "Не ответил на сбор.", "HR сможет выбрать даты вручную на следующем этапе.")
    elif state == VacationPreference.STATUS_SKIPPED:
        reason = _manual_reason("skipped", "Без пожеланий.", "HR закрывает только обязательный остаток.")
    elif not has_draft_item:
        placements = [
            DraftPlacement(row["employee"].id, row["item"].start_date, row["item"].end_date, row["item"].id)
            for row in placed_rows
        ]
        primary_assessment = assess_preference_candidate(employee, primary, year, placements)
        backup_assessment = assess_preference_candidate(employee, backup, year, placements)
        if primary_assessment["has_conflict"] and backup_assessment["has_conflict"]:
            reason = _manual_reason(
                "staffing_conflict",
                "Основной и запасной периоды не прошли проверку.",
                "Нужно подобрать другой период с учетом правил состава.",
            )
        else:
            reason = _manual_reason(
                "not_placed",
                "Нужно проверить вручную.",
                "Пожелания есть, но черновой пункт не найден.",
            )
    elif planning_need["needs_manual_attention"]:
        reason = _manual_reason(
            "remaining_plan",
            f"Осталось добрать {_days_label(planning_need['open_required_days'])}",
            "Пожелание или обязательный остаток еще не закрыты полностью.",
        )

    if reason is None:
        return None

    org = _employee_org_payload(employee)
    identity = _employee_identity_payload(employee)
    urgent_closure = (
        detect_previous_year_closure_need(employee, year, planning_need, include_options=False)
        or active_urgent_closure
    )
    return {
        "employee": employee,
        "employee_name": employee.full_name,
        "department_name": org["department_name"],
        "group_name": org["group_name"],
        "position": org["position"],
        "status": state,
        "reason": reason,
        "primary_label": _period_label(primary.start_date if primary else None, primary.end_date if primary else None),
        "backup_label": _period_label(backup.start_date if backup else None, backup.end_date if backup else None),
        "profile_url": _profile_url(employee, year),
        "calendar_url": _calendar_employee_url(employee, year),
        "manual_place_url": reverse("schedule_draft_manual_place", args=[year, employee.id]),
        "manual_preview_url": reverse("schedule_draft_manual_preview", args=[year, employee.id]),
        "manual_package_preview_url": reverse("schedule_draft_manual_package_preview", args=[year, employee.id]),
        "manual_suggestions_url": reverse("schedule_draft_manual_suggestions", args=[year, employee.id]),
        "day_calculation_url": reverse("schedule_draft_day_calculation", args=[year, employee.id]),
        "manual_anchor": f"draft-manual-{employee.id}",
        "planning_need": planning_need,
        "urgent_closure": urgent_closure,
        **identity,
    }


def _filter_draft_scope_employees(eligible_employees, actor=None):
    if actor is None or not is_department_head_employee(actor):
        return eligible_employees

    managed_department_id = get_managed_department_id(actor)
    if managed_department_id is None:
        return []
    return [employee for employee in eligible_employees if employee.department_id == managed_department_id]


def _normalize_schedule_draft_search_query(query_params):
    return ((query_params or {}).get("q") or "").strip()


def _draft_row_matches_search(row, normalized_query):
    if not normalized_query:
        return True
    employee = row.get("employee")
    search_text = " ".join(
        str(value)
        for value in [
            row.get("employee_name"),
            getattr(employee, "login", ""),
            row.get("position"),
            row.get("department_name"),
            row.get("group_name"),
        ]
        if value
    ).casefold()
    return normalized_query.casefold() in search_text


def _filter_schedule_draft_rows(rows, query):
    normalized_query = query.casefold()
    if not normalized_query:
        return rows
    return [row for row in rows if _draft_row_matches_search(row, normalized_query)]


def _schedule_draft_visible_count_label(visible_count, total_count, unit_label):
    if visible_count == total_count:
        return f"{visible_count} {unit_label}"
    return f"{visible_count} из {total_count} {unit_label}"


def _empty_draft_summary():
    return {
        "placed": 0,
        "manual": 0,
        "blocking": 0,
        "open_required_days": Decimal("0.00"),
        "open_required_days_label": _days_label(Decimal("0.00")),
        "remaining_plan_days": Decimal("0.00"),
        "remaining_plan_days_label": _days_label(Decimal("0.00")),
        "blocking_days": Decimal("0.00"),
        "blocking_days_label": _days_label(Decimal("0.00")),
        "high_risk": 0,
        "conflicts": 0,
        "departments": 0,
        "total": 0,
    }


def _draft_item_has_stored_conflict(item):
    selected_candidate = getattr(item, "selected_candidate", None)
    if selected_candidate is None:
        return False
    return bool((selected_candidate.features or {}).get("risk_is_conflict"))


def has_department_schedule_hard_conflicts(schedule, department_id):
    if schedule is None or not department_id:
        return False
    items = (
        VacationScheduleItem.objects.select_related("selected_candidate", "employee__department")
        .filter(
            schedule=schedule,
            status=VacationScheduleItem.STATUS_PLANNED,
            employee__department_id=department_id,
        )
    )
    return any(_draft_item_has_stored_conflict(item) for item in items)


def _build_draft_summary_from_parts(draft_items, manual_planning_needs, department_names):
    manual_planning_needs = list(manual_planning_needs or [])
    blocking_needs = [need for need in manual_planning_needs if need["has_blocker"]]
    total_open_required_days = quantize_leave_days(
        sum((need["open_required_days"] for need in manual_planning_needs), Decimal("0.00"))
    )
    total_blocking_days = quantize_leave_days(
        sum((need["blocking_days"] for need in blocking_needs), Decimal("0.00"))
    )
    total_remaining_plan_days = quantize_leave_days(
        sum(
            (
                max(
                    need["open_required_days"] - need["blocking_days"],
                    Decimal("0.00"),
                )
                for need in manual_planning_needs
            ),
            Decimal("0.00"),
        )
    )
    conflict_count = sum(1 for item in draft_items if _draft_item_has_stored_conflict(item))
    high_risk_count = sum(
        1
        for item in draft_items
        if item.risk_level == VacationScheduleItem.RISK_HIGH and not _draft_item_has_stored_conflict(item)
    )
    return {
        "placed": len(draft_items),
        "manual": len(manual_planning_needs),
        "blocking": len(blocking_needs),
        "open_required_days": total_open_required_days,
        "open_required_days_label": _days_label(total_open_required_days),
        "remaining_plan_days": total_remaining_plan_days,
        "remaining_plan_days_label": _days_label(total_remaining_plan_days),
        "blocking_days": total_blocking_days,
        "blocking_days_label": _days_label(total_blocking_days),
        "high_risk": high_risk_count,
        "conflicts": conflict_count,
        "departments": len(department_names),
        "total": len(draft_items) + len(manual_planning_needs),
    }


def _readonly_planning_need_from_items(employee, year, items):
    placed_days = quantize_leave_days(sum((_draft_item_days(item) for item in items or []), Decimal("0.00")))
    zero_days = Decimal("0.00")
    return {
        "available_days": placed_days,
        "available_days_label": _days_label(placed_days),
        "plan_available_days": placed_days,
        "plan_available_days_label": _days_label(placed_days),
        "future_available_days": zero_days,
        "future_available_days_label": _days_label(zero_days),
        "mandatory_days": zero_days,
        "mandatory_days_label": _days_label(zero_days),
        "base_target_days": placed_days,
        "base_target_days_label": _days_label(placed_days),
        "annual_target_days": placed_days,
        "annual_target_days_label": _days_label(placed_days),
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
        "planning_basis": "department_review",
        "target_days": placed_days,
        "target_days_label": _days_label(placed_days),
        "placed_days": placed_days,
        "placed_days_label": _days_label(placed_days),
        "open_required_days": zero_days,
        "open_required_days_label": _days_label(zero_days),
        "deadline_blocking_days": zero_days,
        "deadline_blocking_days_label": _days_label(zero_days),
        "annual_remaining_days": zero_days,
        "annual_remaining_days_label": _days_label(zero_days),
        "manual_task_label": _days_label(zero_days),
        "blocking_days": zero_days,
        "blocking_days_label": _days_label(zero_days),
        "deferred_days": zero_days,
        "deferred_days_label": _days_label(zero_days),
        "nearest_deadline": None,
        "nearest_deadline_label": "",
        "status": {
            "key": "covered",
            "label": "Отправлен на проверку",
            "icon": "groups",
            "tone": "ok",
        },
        "action_text": f"Черновик на {year} год отправлен руководителям отделов.",
        "has_blocker": False,
        "needs_manual_attention": False,
        "plan_breakdown": [],
        "mandatory_rows": [],
        "entitlement_rows": [],
    }


def _readonly_planning_need_map(employees, year, draft_items_by_employee):
    return {
        employee.id: _readonly_planning_need_from_items(
            employee,
            year,
            draft_items_by_employee.get(employee.id, []),
        )
        for employee in employees
    }


def build_schedule_draft_summary_context(year, actor=None):
    schedule = VacationSchedule.objects.filter(year=year, status__in=DRAFT_VIEW_SCHEDULE_STATUSES).first()
    if schedule is None:
        return {
            "schedule": None,
            "draft_summary": _empty_draft_summary(),
            "approval_blocked": False,
        }

    eligible_employees = _filter_draft_scope_employees(get_eligible_preference_employees(year), actor=actor)
    employee_ids = [employee.id for employee in eligible_employees]
    employee_id_set = set(employee_ids)
    preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    active_urgent_closure_by_employee = get_active_urgent_closure_payload_map(employee_ids, year)
    draft_items = [item for item in _draft_items_for_schedule(schedule) if item.employee_id in employee_id_set]
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)

    if schedule.status != VacationSchedule.STATUS_DRAFT:
        department_names = {
            item.employee.department.name if item.employee.department_id else "Без отдела"
            for item in draft_items
        }
        draft_summary = _build_draft_summary_from_parts(draft_items, [], department_names)
        draft_summary["high_risk"] = sum(
            1
            for item in draft_items
            if item.risk_level == VacationScheduleItem.RISK_HIGH and not _draft_item_has_stored_conflict(item)
        )
        draft_summary["conflicts"] = sum(1 for item in draft_items if _draft_item_has_stored_conflict(item))
        return {
            "schedule": schedule,
            "draft_summary": draft_summary,
            "approval_blocked": False,
        }

    planning_need_by_employee = build_employee_schedule_planning_need_map(
        eligible_employees,
        year,
        draft_items_by_employee=draft_items_by_employee,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
    )

    manual_employee_ids = {
        employee.id
        for employee in eligible_employees
        if planning_need_by_employee[employee.id]["needs_manual_attention"]
        or active_urgent_closure_by_employee.get(employee.id) is not None
    }
    manual_planning_needs = [
        planning_need_by_employee[employee_id]
        for employee_id in manual_employee_ids
    ]
    department_names = {
        item.employee.department.name if item.employee.department_id else "Без отдела"
        for item in draft_items
    }
    department_names.update(
        employee.department.name if employee.department_id else "Без отдела"
        for employee in eligible_employees
        if employee.id in manual_employee_ids
    )
    draft_summary = _build_draft_summary_from_parts(draft_items, manual_planning_needs, department_names)
    return {
        "schedule": schedule,
        "draft_summary": draft_summary,
        "approval_blocked": draft_summary["blocking"] > 0,
    }


def _department_rework_package_context_payload(current_items):
    current_items = sorted(
        list(current_items or []),
        key=lambda item: (item.start_date, item.end_date, item.id or 0),
    )
    target_days = _department_rework_target_days(current_items)
    max_periods = _department_rework_max_package_periods(current_items)
    period_labels = [
        f"{_short_period_label(item.start_date, item.end_date)} ({_days_label(item.chargeable_days)})"
        for item in current_items
    ]
    return {
        "current_count": len(current_items),
        "current_summary": f"{len(current_items)} период(а) · {_days_label(target_days)}",
        "current_detail": " · ".join(period_labels) if period_labels else "Текущий пакет не найден.",
        "notice": "Вы заменяете весь пакет отпусков сотрудника за год.",
        "max_periods": max_periods,
        "max_periods_label": f"до {max_periods} периодов",
    }


def build_schedule_draft_page_context(year, actor=None, query_params=None, department_rework_approval=None):
    query = _normalize_schedule_draft_search_query(query_params)
    collection = VacationPreferenceCollection.objects.filter(year=year).first()
    schedule = (
        department_rework_approval.schedule
        if department_rework_approval is not None
        else VacationSchedule.objects.filter(year=year, status__in=DRAFT_VIEW_SCHEDULE_STATUSES).first()
    )
    draft_rework_mode = department_rework_approval is not None
    draft_is_editable = schedule is not None and schedule.status == VacationSchedule.STATUS_DRAFT
    draft_sent_to_review = schedule is not None and schedule.status == VacationSchedule.STATUS_DEPARTMENT_REVIEW
    draft_approved = schedule is not None and schedule.status == VacationSchedule.STATUS_APPROVED
    active_auto_place_job = (
        get_active_schedule_auto_place_job(year=year, schedule=schedule)
        if draft_is_editable
        else None
    )
    if draft_is_editable and active_auto_place_job is None:
        normalize_schedule_draft_adjacent_items(year)
        schedule.refresh_from_db()
    draft_auto_place_job = (
        schedule_auto_place_job_page_payload(active_auto_place_job)
        if active_auto_place_job is not None
        else None
    )
    if schedule is None:
        return {
            "year": year,
            "collection": collection,
            "schedule": None,
            "draft_exists": False,
            "draft_is_editable": False,
            "draft_sent_to_review": False,
            "draft_approved": False,
            "draft_rework_mode": False,
            "can_rework_department": False,
            "draft_url": schedule_draft_url(year),
            "draft_create_url": schedule_draft_create_url(year),
            "draft_auto_place_url": reverse("schedule_draft_auto_place", args=[year]),
            "draft_auto_place_preview_url": reverse("schedule_draft_auto_place_preview", args=[year]),
            "draft_auto_place_next_url": schedule_draft_url(year),
            "readiness_url": reverse("preference_collection_readiness", args=[year]),
            "placed_rows": [],
            "manual_rows": [],
            "query": query,
            "result_count": 0,
            "visible_placed_count": 0,
            "visible_manual_count": 0,
            "placed_count_label": _schedule_draft_visible_count_label(0, 0, "записей"),
            "manual_count_label": format_staff_count(0),
            "planning_need_by_employee": {},
            "draft_summary": _empty_draft_summary(),
            "draft_auto_place_job": None,
            "draft_status": {
                "label": "Черновик не создан",
                "icon": "pending_actions",
            },
            "approval_blocked": False,
        }
    eligible_employees = _filter_draft_scope_employees(get_eligible_preference_employees(year), actor=actor)
    if department_rework_approval is not None:
        eligible_employees = [
            employee
            for employee in eligible_employees
            if employee.department_id == department_rework_approval.department_id
        ]
    employee_ids = [employee.id for employee in eligible_employees]
    preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    active_urgent_closure_by_employee = get_active_urgent_closure_payload_map(employee_ids, year)
    draft_items = [item for item in _draft_items_for_schedule(schedule) if item.employee_id in set(employee_ids)]
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)
    if draft_rework_mode:
        planning_need_by_employee = {
            employee.id: _department_rework_planning_need(
                employee,
                year,
                _department_rework_target_days(draft_items_by_employee.get(employee.id, [])),
                placed_days=_department_rework_target_days(draft_items_by_employee.get(employee.id, [])),
            )
            for employee in eligible_employees
        }
    else:
        planning_need_by_employee = (
            build_employee_schedule_planning_need_map(
                eligible_employees,
                year,
                draft_items_by_employee=draft_items_by_employee,
                preference_pair_by_employee=preference_pair_by_employee,
                preference_state_by_employee=preference_state_by_employee,
            )
            if draft_is_editable
            else _readonly_planning_need_map(eligible_employees, year, draft_items_by_employee)
        )
    all_placed_rows = _draft_item_rows(
        schedule,
        year,
        draft_items,
        planning_need_by_employee,
        preference_pair_by_employee=preference_pair_by_employee,
        actor=actor,
    )
    if draft_rework_mode:
        rework_context_by_employee = {
            employee_id: _department_rework_package_context_payload(items)
            for employee_id, items in draft_items_by_employee.items()
        }
        for row in all_placed_rows:
            employee = row["employee"]
            rework_package_context = rework_context_by_employee.get(employee.id) or _department_rework_package_context_payload([])
            row.update(
                {
                    "can_rework_package": rework_package_context["current_count"] > 0,
                    "rework_package_context": rework_package_context,
                    "rework_action_url": reverse(
                        "schedule_department_review_rework_place",
                        args=[year, department_rework_approval.id, employee.id],
                    ),
                    "rework_package_preview_url": reverse(
                        "schedule_department_review_rework_package_preview",
                        args=[year, department_rework_approval.id, employee.id],
                    ),
                    "rework_suggestions_url": reverse(
                        "schedule_department_review_rework_suggestions",
                        args=[year, department_rework_approval.id, employee.id],
                    ),
                    "rework_needed_label": planning_need_by_employee[employee.id]["target_days_label"],
                    "rework_status_label": "Доработка отдела",
                    "rework_reason": department_rework_approval.comment,
                }
            )
    placed_employee_ids = {row["employee"].id for row in all_placed_rows}
    all_manual_rows = (
        [
            row
            for employee in eligible_employees
            for row in [
                _manual_row_for_employee(
                    employee,
                    year,
                    placed_employee_ids,
                    all_placed_rows,
                    planning_need_by_employee[employee.id],
                    preference_state_by_employee=preference_state_by_employee,
                    preference_pair_by_employee=preference_pair_by_employee,
                    active_urgent_closure_by_employee=active_urgent_closure_by_employee,
                )
            ]
            if row is not None
        ]
        if draft_is_editable
        else []
    )
    placed_rows = _filter_schedule_draft_rows(all_placed_rows, query)
    manual_rows = _filter_schedule_draft_rows(all_manual_rows, query)
    conflict_count = sum(1 for row in all_placed_rows if row["has_conflict"])
    high_risk_count = sum(1 for row in all_placed_rows if row["has_high_risk"] and not row["has_conflict"])
    departments = sorted({row["department_name"] for row in all_placed_rows + all_manual_rows})
    blocking_rows = [row for row in all_manual_rows if row["planning_need"]["has_blocker"]]
    draft_summary = _build_draft_summary_from_parts(
        draft_items,
        [row["planning_need"] for row in all_manual_rows],
        departments,
    )
    draft_summary["high_risk"] = high_risk_count
    draft_summary["conflicts"] = conflict_count
    return {
        "year": year,
        "collection": collection,
        "schedule": schedule,
        "draft_exists": schedule is not None,
        "draft_is_editable": draft_is_editable,
        "draft_sent_to_review": draft_sent_to_review,
        "draft_approved": draft_approved,
        "draft_rework_mode": draft_rework_mode,
        "can_rework_department": draft_rework_mode,
        "department_rework_approval": department_rework_approval,
        "department_rework_resubmit_url": (
            reverse("schedule_department_review_resubmit", args=[year, department_rework_approval.id])
            if department_rework_approval is not None
            else ""
        ),
        "department_rework_name": (
            department_rework_approval.department.name
            if department_rework_approval is not None and department_rework_approval.department_id
            else ""
        ),
        "department_rework_comment": department_rework_approval.comment if department_rework_approval is not None else "",
        "draft_url": schedule_draft_url(year),
        "draft_create_url": schedule_draft_create_url(year),
        "draft_auto_place_url": reverse("schedule_draft_auto_place", args=[year]),
        "draft_auto_place_preview_url": reverse("schedule_draft_auto_place_preview", args=[year]),
        "draft_auto_place_next_url": schedule_draft_url(year),
        "readiness_url": reverse("preference_collection_readiness", args=[year]),
        "placed_rows": placed_rows,
        "manual_rows": manual_rows,
        "query": query,
        "result_count": len(placed_rows) + len(manual_rows),
        "visible_placed_count": len(placed_rows),
        "visible_manual_count": len(manual_rows),
        "placed_count_label": _schedule_draft_visible_count_label(len(placed_rows), len(all_placed_rows), "записей"),
        "manual_count_label": (
            f"{format_staff_count(len(manual_rows))} из {format_staff_count(len(all_manual_rows))}"
            if len(manual_rows) != len(all_manual_rows)
            else format_staff_count(len(all_manual_rows))
        ),
        "planning_need_by_employee": planning_need_by_employee,
        "draft_summary": draft_summary,
        "draft_auto_place_job": draft_auto_place_job,
        "draft_status": {
            "label": (
                "Доработка отдела"
                if draft_rework_mode
                else (
                    "График утверждён"
                    if draft_approved
                    else ("Черновик отправлен" if draft_sent_to_review else "Черновик создан")
                )
            ),
            "icon": (
                "edit_note"
                if draft_rework_mode
                else ("verified" if draft_approved else ("fact_check" if draft_sent_to_review else "edit_calendar"))
            ),
        },
        "approval_blocked": bool(blocking_rows),
    }
