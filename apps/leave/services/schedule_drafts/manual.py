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
from apps.leave.ml.package_scoring import (
    PackageScoringResult,
    build_generation_package_features,
    score_generation_package,
    score_package_features,
)

from apps.leave.services.schedule_drafts.constants import *
from apps.leave.services.schedule_drafts.types import *


def _manual_package_periods_from_input(periods, year, *, max_periods=MANUAL_DRAFT_MAX_PACKAGE_PERIODS):
    normalized = []
    if not isinstance(periods, (list, tuple)) or not periods:
        raise ValidationError("Добавьте хотя бы один период отпуска.")
    max_periods = max(1, int(max_periods or MANUAL_DRAFT_MAX_PACKAGE_PERIODS))
    if len(periods) > max_periods:
        raise ValidationError(f"За один раз можно поставить не больше {max_periods} периодов.")

    planning_start, planning_end = _planning_year_bounds(year)
    for index, period in enumerate(periods, start=1):
        start_date = period.get("start_date") if isinstance(period, dict) else None
        end_date = period.get("end_date") if isinstance(period, dict) else None
        if not start_date or not end_date:
            raise ValidationError(f"Заполните дату начала и окончания в периоде {index}.")
        if end_date < start_date:
            raise ValidationError(f"В периоде {index} дата окончания не может быть раньше даты начала.")
        if start_date < planning_start or end_date > planning_end:
            raise ValidationError(f"Период {index} должен быть внутри {year} года.")
        normalized.append(
            {
                "order": index,
                "start_date": start_date,
                "end_date": end_date,
            }
        )

    sorted_periods = sorted(normalized, key=lambda item: (item["start_date"], item["end_date"]))
    for previous, current in zip(sorted_periods, sorted_periods[1:]):
        if _periods_overlap(previous["start_date"], previous["end_date"], current["start_date"], current["end_date"]):
            raise ValidationError("Периоды внутри одного размещения не должны пересекаться.")
    return normalized


def _manual_draft_employee_context(year, employee_id, *, for_update=False):
    schedule_queryset = VacationSchedule.objects
    if for_update:
        schedule_queryset = schedule_queryset.select_for_update()
    schedule = schedule_queryset.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    employee = next(
        (candidate for candidate in get_eligible_preference_employees(year) if candidate.id == employee_id),
        None,
    )
    if employee is None:
        raise ValidationError("Сотрудник не участвует в планировании графика за этот год.")

    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = _draft_items_by_employee(draft_items)
    planning_need = build_employee_schedule_planning_need(
        employee,
        year,
        draft_items_by_employee.get(employee.id, []),
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )
    return schedule, employee, draft_items, draft_items_by_employee, planning_need


def _risk_level_rank(risk_level):
    return RISK_LEVEL_FEATURE_WEIGHT.get(risk_level or VacationRequest.RISK_LOW, 1)


def _manual_period_preview_payload(period, assessment, remaining_after):
    risk_payload = assessment.get("risk_payload") or {
        "risk_score": 0,
        "risk_level": VacationRequest.RISK_LOW,
        "risk_explanation": {},
    }
    risk_explanation = risk_payload.get("risk_explanation") or {}
    staffing_summary = _staffing_summary_from_risk_payload(risk_payload)
    start_date = period["start_date"]
    end_date = period["end_date"]
    chargeable_days = assessment.get("chargeable_days")
    if chargeable_days is None and end_date >= start_date:
        chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid")
    chargeable_days = quantize_leave_days(chargeable_days or Decimal("0.00"))
    return {
        "order": period["order"],
        "start_date": start_date,
        "end_date": end_date,
        "start_date_iso": start_date.isoformat(),
        "end_date_iso": end_date.isoformat(),
        "period_label": _short_period_label(start_date, end_date),
        "full_period_label": _period_label(start_date, end_date),
        "calendar_days": _calendar_days(start_date, end_date),
        "chargeable_days": chargeable_days,
        "chargeable_days_label": _days_label(chargeable_days),
        "can_place": bool(assessment.get("can_place")),
        "message": _candidate_assessment_reason(assessment).get("text", ""),
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_payload.get("risk_level"), "Низкий"),
        "risk_score": int(risk_payload.get("risk_score") or 0),
        "risk_level": risk_payload.get("risk_level") or VacationRequest.RISK_LOW,
        "risk_tone": _risk_tone(risk_payload.get("risk_level"), bool(risk_explanation.get("is_conflict"))),
        "risk_short_reason": risk_explanation.get("short_reason", ""),
        "risk_recommended_action": risk_explanation.get("recommended_action", ""),
        "risk_is_conflict": bool(risk_explanation.get("is_conflict")),
        "staffing_summary": staffing_summary,
        "staffing_chips": list(staffing_summary.get("chips") or []),
        "remaining_after_period": remaining_after,
        "assessment": assessment,
    }


def _manual_package_preview_message(can_submit, period_payloads, planning_need, post_planning_need):
    if not period_payloads:
        return "Добавьте хотя бы один период отпуска."
    first_failed = next((period for period in period_payloads if not period["can_place"]), None)
    if first_failed:
        return first_failed["message"] or "Один из периодов нельзя поставить в черновик."
    if planning_need["has_blocker"] and post_planning_need["blocking_days"] > 0:
        return _deadline_not_closed_message(planning_need)
    if post_planning_need["open_required_days"] <= 0:
        return "Периоды полностью закрывают плановую потребность."
    if can_submit:
        return "Периоды можно поставить в черновик. Часть дней останется на ручное распределение."
    return "Проверьте выбранные периоды."


def _package_preview_recommendation_label(recommendation):
    return {
        "prefer": "Предпочтительный",
        "normal": "Допустимый",
        "avoid": "С осторожностью",
        "blocked": "Заблокирован",
    }.get(recommendation or "", "Оценен")


def _manual_package_iso_signature_from_periods(periods):
    signature = []
    for period in periods:
        start_date = period.get("start_date")
        end_date = period.get("end_date")
        if not start_date or not end_date:
            continue
        signature.append(
            (
                start_date.isoformat() if hasattr(start_date, "isoformat") else str(start_date),
                end_date.isoformat() if hasattr(end_date, "isoformat") else str(end_date),
            )
        )
    return tuple(sorted(signature))


def _manual_suggestion_option_signature(option):
    periods = list(option.get("periods") or [])
    if not periods and (option.get("start_date") or option.get("end_date")):
        periods = [{"start_date": option.get("start_date"), "end_date": option.get("end_date")}]
    return tuple(
        sorted(
            (str(period.get("start_date") or ""), str(period.get("end_date") or ""))
            for period in periods
            if period.get("start_date") and period.get("end_date")
        )
    )


def _manual_package_scoring_response(result, *, alternative_count=0):
    return {
        "package_score": result.score,
        "package_score_label": _percent_label(result.score),
        "package_confidence": result.confidence,
        "package_confidence_label": _percent_label(result.confidence),
        "package_model_version": result.model_version,
        "package_recommendation": result.recommendation,
        "package_recommendation_label": _package_preview_recommendation_label(result.recommendation),
        "package_explanation": result.explanation,
        "alternative_count": int(alternative_count or 0),
    }


def _manual_package_scoring_payload_from_suggestion_option(option):
    result = PackageScoringResult(
        score=_feature_decimal(option.get("score") or option.get("package_score")),
        confidence=_feature_decimal(option.get("confidence") or option.get("package_confidence")),
        recommendation=option.get("recommendation") or option.get("package_recommendation") or "",
        explanation=(
            option.get("package_explanation")
            or option.get("explanation")
            or option.get("message")
            or ""
        ),
        model_version=option.get("model_version") or option.get("package_model_version") or "",
        scorer_kind=option.get("scorer_kind") or option.get("package_scorer_kind") or "",
    )
    return _manual_package_scoring_response(
        result,
        alternative_count=option.get("alternative_count") or 0,
    )


def _find_matching_manual_suggestion_option(schedule, employee, period_payloads):
    target_signature = _manual_package_iso_signature_from_periods(period_payloads)
    if not target_signature:
        return None

    version = int(schedule.manual_suggestion_cache_version or 0)
    if version <= 0:
        return None
    cache = VacationScheduleManualSuggestionCache.objects.filter(
        schedule=schedule,
        employee=employee,
        version=version,
    ).first()
    payload = cache.payload if cache else {}
    if int((payload or {}).get("suggestion_schema_version") or 0) != MANUAL_DRAFT_SUGGESTION_CACHE_SCHEMA_VERSION:
        return None
    return next(
        (
            option
            for option in (payload.get("options") or [])
            if _manual_suggestion_option_signature(option) == target_signature
        ),
        None,
    )


def _manual_preview_package_from_periods(employee, period_payloads, planning_need, post_planning_need, year):
    candidates = [
        _manual_candidate_from_period_preview(
            employee,
            period,
            planning_need,
            year,
            order=index,
        )
        for index, period in enumerate(period_payloads, start=1)
    ]
    total_days = sum((_feature_decimal(period["chargeable_days"]) for period in period_payloads), Decimal("0.00"))
    return DraftGenerationCandidatePackage(
        employee=employee,
        candidates=candidates,
        source=VacationScheduleItem.SOURCE_MANUAL,
        explanation="Оценка выбранного пакета.",
        metadata={
            "package_kind": "manual_preview",
            "total_chargeable_days": total_days,
            "periods_count": len(candidates),
            "package_target_days": planning_need["open_required_days"],
            "remaining_after_package": post_planning_need["open_required_days"],
            "package_closes_need": post_planning_need["open_required_days"] <= 0,
        },
    )


def _manual_package_preview_scoring_payload(
    schedule,
    employee,
    period_payloads,
    planning_need,
    post_planning_need,
    *,
    can_submit,
):
    if not period_payloads:
        return {
            "package_score": None,
            "package_score_label": "",
            "package_confidence": None,
            "package_confidence_label": "",
            "package_model_version": "",
            "package_recommendation": "",
            "package_recommendation_label": "",
            "package_explanation": "",
            "alternative_count": 0,
        }

    if can_submit:
        matching_option = _find_matching_manual_suggestion_option(schedule, employee, period_payloads)
        if matching_option is not None:
            return _manual_package_scoring_payload_from_suggestion_option(matching_option)

    preview_package = _manual_preview_package_from_periods(
        employee,
        period_payloads,
        planning_need,
        post_planning_need,
        schedule.year,
    )
    result = score_generation_package(
        preview_package,
        use_neural=True,
    )
    if not (bool(can_submit) and all(period["can_place"] for period in period_payloads)):
        result = score_package_features(
            build_generation_package_features(preview_package),
            passed_hard_rules=False,
        )
    return _manual_package_scoring_response(result)


def _merged_paid_leave_segments(items):
    periods = sorted(
        [
            (item.start_date, item.end_date)
            for item in items
            if getattr(item, "vacation_type", "paid") == "paid"
            and getattr(item, "status", VacationScheduleItem.STATUS_DRAFT) != VacationScheduleItem.STATUS_CANCELLED
            and item.start_date
            and item.end_date
        ],
        key=lambda period: (period[0], period[1]),
    )
    if not periods:
        return []

    segments = []
    current_start, current_end = periods[0]
    for start_date, end_date in periods[1:]:
        if start_date <= current_end + timedelta(days=1):
            current_end = max(current_end, end_date)
            continue
        segments.append((current_start, current_end))
        current_start, current_end = start_date, end_date
    segments.append((current_start, current_end))
    return segments


def _has_required_continuous_paid_leave_part(items):
    return any(
        get_chargeable_leave_days(start_date, end_date, "paid") >= MIN_CONTINUOUS_PAID_LEAVE_DAYS
        for start_date, end_date in _merged_paid_leave_segments(items)
    )


def _requires_required_continuous_paid_leave_part(planning_need):
    return _feature_decimal(planning_need.get("target_days")) >= Decimal(MIN_CONTINUOUS_PAID_LEAVE_DAYS)


def _required_continuous_paid_leave_message():
    return (
        "После размещения в графике должна быть хотя бы одна часть оплачиваемого отпуска "
        f"не меньше {MIN_CONTINUOUS_PAID_LEAVE_DAYS} дней."
    )


def build_manual_schedule_draft_package_preview(*, year, employee_id, periods):
    normalized_periods = _manual_package_periods_from_input(periods, year)
    schedule, employee, draft_items, draft_items_by_employee, planning_need = _manual_draft_employee_context(year, employee_id)
    if not planning_need["needs_manual_attention"]:
        return {
            "can_submit": False,
            "message": "По сотруднику уже закрыта плановая потребность.",
            "periods": [],
            "calendar_days": 0,
            "chargeable_days": Decimal("0.00"),
            "remaining_after_placement": planning_need["open_required_days"],
            "risk_label": "Низкий",
            "risk_score": 0,
            "risk_level": VacationRequest.RISK_LOW,
            "risk_tone": "low",
            "risk_short_reason": "",
            "risk_recommended_action": "",
            "risk_is_conflict": False,
            "staffing_summary": {},
            "staffing_chips": [],
            "planning_need": planning_need,
            "post_planning_need": planning_need,
        }

    placements = _current_placements_from_items(draft_items)
    simulated_items = list(draft_items_by_employee.get(employee.id, []))
    remaining_days = planning_need["open_required_days"]
    period_payloads = []
    total_chargeable_days = Decimal("0.00")
    total_calendar_days = 0
    can_submit = True

    for period in normalized_periods:
        assessment = assess_schedule_draft_candidate(
            employee,
            period["start_date"],
            period["end_date"],
            year,
            placements,
            max_chargeable_days=remaining_days,
        )
        chargeable_days = quantize_leave_days(assessment.get("chargeable_days") or Decimal("0.00"))
        if assessment.get("can_place"):
            total_chargeable_days += chargeable_days
            total_calendar_days += _calendar_days(period["start_date"], period["end_date"])
            simulated_items.append(_virtual_draft_item(employee, period["start_date"], period["end_date"], chargeable_days))
            placements.append(DraftPlacement(employee.id, period["start_date"], period["end_date"], None))
            post_planning_need = build_employee_schedule_planning_need(
                employee,
                year,
                simulated_items,
                preference_pair=get_employee_preference_pair(employee, year),
                preference_state=get_employee_preference_state(employee, year),
            )
            remaining_days = post_planning_need["open_required_days"]
        else:
            can_submit = False
            post_planning_need = build_employee_schedule_planning_need(
                employee,
                year,
                simulated_items,
                preference_pair=get_employee_preference_pair(employee, year),
                preference_state=get_employee_preference_state(employee, year),
            )
        period_payloads.append(_manual_period_preview_payload(period, assessment, remaining_days))
        if not assessment.get("can_place"):
            break

    post_planning_need = build_employee_schedule_planning_need(
        employee,
        year,
        simulated_items,
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )
    if planning_need["has_blocker"] and post_planning_need["blocking_days"] > 0:
        can_submit = False
    continuous_part_missing = (
        _requires_required_continuous_paid_leave_part(planning_need)
        and not _has_required_continuous_paid_leave_part(simulated_items)
    )
    if can_submit and continuous_part_missing:
        can_submit = False
    show_continuous_part_message = (
        continuous_part_missing
        and all(period["can_place"] for period in period_payloads)
        and not (planning_need["has_blocker"] and post_planning_need["blocking_days"] > 0)
    )

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
    staffing_summary, staffing_chips = _worst_staffing_summary_from_payloads(period_payloads)
    package_scoring = _manual_package_preview_scoring_payload(
        schedule,
        employee,
        period_payloads,
        planning_need,
        post_planning_need,
        can_submit=can_submit,
    )

    return {
        "can_submit": can_submit,
        "message": (
            _required_continuous_paid_leave_message()
            if show_continuous_part_message
            else _manual_package_preview_message(can_submit, period_payloads, planning_need, post_planning_need)
        ),
        "periods": period_payloads,
        "calendar_days": total_calendar_days,
        "chargeable_days": total_chargeable_days,
        "remaining_after_placement": post_planning_need["open_required_days"],
        "target_days": planning_need["target_days"],
        "placed_days": planning_need["placed_days"],
        "open_required_days": planning_need["open_required_days"],
        "blocking_after_placement": post_planning_need["blocking_days"],
        "annual_remaining_after_placement": post_planning_need["annual_remaining_days"],
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, "Низкий"),
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_tone": _risk_tone(risk_level, risk_is_conflict),
        "risk_short_reason": risk_short_reason,
        "risk_recommended_action": risk_recommended_action,
        "risk_is_conflict": risk_is_conflict,
        "staffing_summary": staffing_summary,
        "staffing_chips": staffing_chips,
        "planning_need": planning_need,
        "post_planning_need": post_planning_need,
        **package_scoring,
    }


def _manual_candidate_from_period_preview(employee, period_payload, planning_need, year, *, actor=None, order=1):
    comment = f"Вручную размещено HR: {actor.full_name if actor else 'HR'}."
    preference = None
    preference_match = ""
    preference_match_label = ""
    preference_pair = get_employee_preference_pair(employee, year)
    for priority, label in (
        (VacationPreference.PRIORITY_PRIMARY, "Основное пожелание"),
        (VacationPreference.PRIORITY_BACKUP, "Запасное пожелание"),
    ):
        current_preference = preference_pair.get(priority)
        if not current_preference or not current_preference.start_date or not current_preference.end_date:
            continue
        if (
            period_payload["start_date"] == current_preference.start_date
            and period_payload["end_date"] == current_preference.end_date
        ):
            preference = current_preference
            preference_match = priority
            preference_match_label = label
            break
        if (
            period_payload["start_date"] == current_preference.start_date
            and current_preference.start_date <= period_payload["end_date"] <= current_preference.end_date
        ):
            preference = current_preference
            preference_match = f"{priority}_partial"
            preference_match_label = f"Часть {label.lower()}"
            break

    candidate = DraftGenerationCandidate(
        employee=employee,
        start_date=period_payload["start_date"],
        end_date=period_payload["end_date"],
        kind=VacationScheduleCandidate.KIND_MANUAL,
        source=VacationScheduleItem.SOURCE_MANUAL,
        comment=comment,
        preference=preference,
        metadata={
            "manual_package_selected": True,
            "manual_package_period_order": order,
            "target_chargeable_days": period_payload["chargeable_days"],
            "preference_match": preference_match,
            "preference_match_label": preference_match_label,
            "is_preference_candidate": bool(preference_match),
        },
    )
    _apply_planning_need_metadata(candidate, planning_need, year)
    _apply_hard_rule_assessment(candidate, period_payload["assessment"])
    return _apply_candidate_scoring(candidate)


def _package_risk_payload(candidates):
    highest = max(
        candidates,
        key=lambda candidate: (_risk_level_rank(candidate.metadata.get("risk_level")), int(candidate.metadata.get("risk_score") or 0)),
        default=None,
    )
    if highest is None:
        return VacationScheduleItem.RISK_LOW, 0
    return highest.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW, int(highest.metadata.get("risk_score") or 0)


def _package_total_days(candidates):
    return sum((_feature_decimal(candidate.metadata.get("chargeable_days")) for candidate in candidates), Decimal("0.00"))


def _package_average(candidates, key):
    if not candidates:
        return None
    return sum((_candidate_scoring_decimal(candidate, key) for candidate in candidates), Decimal("0.00")) / Decimal(len(candidates))


def _percent_decimal(value):
    return max(Decimal("0.00"), min(Decimal("100.00"), Decimal(str(value)))).quantize(Decimal("0.01"))


def _manual_package_quality_score(package):
    if "package_quality_score" in package.metadata:
        return _feature_decimal(package.metadata.get("package_quality_score"))
    candidates = [_apply_candidate_scoring(candidate) for candidate in package.candidates]
    if not candidates:
        return Decimal("0.00")

    result = score_generation_package(package)
    package.metadata["package_quality_score"] = result.score
    return package.metadata["package_quality_score"]


def _package_features(package):
    candidates = package.candidates
    return _json_safe_generation_value(
        {
            **(package.metadata or {}),
            **build_generation_package_features(package),
            "periods_count": len(candidates),
            "periods": [
                {
                    "start_date": candidate.start_date,
                    "end_date": candidate.end_date,
                    "chargeable_days": candidate.metadata.get("chargeable_days"),
                    "score": candidate.metadata.get("scoring_score"),
                    "confidence": candidate.metadata.get("scoring_confidence"),
                    "risk_score": candidate.metadata.get("risk_score"),
                    "risk_level": candidate.metadata.get("risk_level"),
                    "passed_hard_rules": _candidate_passed_hard_rules(candidate),
                }
                for candidate in candidates
            ],
        }
    )


def _persist_manual_candidate_package(generation_run, schedule, package, *, decision, decision_rank):
    candidates = [_apply_candidate_scoring(candidate) for candidate in package.candidates]
    package_score = _manual_package_quality_score(package)
    selected_at = timezone.now() if decision == VacationScheduleCandidatePackage.DECISION_SELECTED else None
    candidate_records = []
    blocked_reason = ""
    blocked_key = ""

    for order, candidate in enumerate(candidates, start=1):
        passed = _candidate_passed_hard_rules(candidate)
        if not passed:
            candidate_decision = VacationScheduleCandidate.DECISION_BLOCKED
            reason = _candidate_assessment_reason(candidate.assessment or {})
            blocked_key = blocked_key or reason.get("kind", "")
            blocked_reason = blocked_reason or reason.get("text", "")
        elif decision == VacationScheduleCandidatePackage.DECISION_SELECTED:
            candidate_decision = VacationScheduleCandidate.DECISION_SELECTED
        else:
            candidate_decision = VacationScheduleCandidate.DECISION_REJECTED

        candidate_records.append(
            VacationScheduleCandidate(
                generation_run=generation_run,
                schedule=schedule,
                employee=candidate.employee,
                start_date=candidate.start_date,
                end_date=candidate.end_date,
                vacation_type="paid",
                chargeable_days=int(candidate.metadata.get("chargeable_days") or 0),
                kind=candidate.kind,
                source=candidate.source,
                passed_hard_rules=passed,
                block_reason_key=(candidate.metadata.get("block_reason_key") or blocked_key or "")[:80],
                block_reason=candidate.metadata.get("block_reason") or blocked_reason,
                risk_score=int(candidate.metadata.get("risk_score") or 0),
                risk_level=candidate.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW,
                features=_generation_candidate_features(candidate),
                score=_candidate_scoring_decimal(candidate, "scoring_score"),
                confidence=_candidate_scoring_decimal(candidate, "scoring_confidence"),
                model_version=candidate.metadata.get("scoring_model_version") or DRAFT_GENERATION_HYBRID_MODEL_VERSION,
                explanation=candidate.metadata.get("scoring_explanation") or _candidate_explanation(candidate, candidate_decision),
                decision=candidate_decision,
                decision_rank=(decision_rank * 10) + order,
                selected_at=selected_at if candidate_decision == VacationScheduleCandidate.DECISION_SELECTED else None,
            )
        )

    candidate_records = VacationScheduleCandidate.objects.bulk_create(candidate_records)
    for candidate, stored_candidate in zip(candidates, candidate_records):
        candidate.stored_candidate = stored_candidate

    passed_package = bool(candidates) and all(_candidate_passed_hard_rules(candidate) for candidate in candidates)
    if not passed_package:
        decision = VacationScheduleCandidatePackage.DECISION_BLOCKED
    risk_level, risk_score = _package_risk_payload(candidates)
    package_confidence = package.metadata.get("package_confidence") or _package_average(candidates, "scoring_confidence")
    package_model_version = package.metadata.get("package_model_version") or DRAFT_GENERATION_HYBRID_MODEL_VERSION
    package_explanation = package.metadata.get("package_scoring_explanation") or package.explanation
    package_record = VacationScheduleCandidatePackage.objects.create(
        generation_run=generation_run,
        schedule=schedule,
        employee=package.employee,
        periods_count=len(candidates),
        total_chargeable_days=int(_package_total_days(candidates)),
        source=package.source,
        passed_hard_rules=passed_package,
        block_reason_key=(blocked_key or "")[:80],
        block_reason=blocked_reason,
        risk_score=risk_score,
        risk_level=risk_level,
        features=_package_features(package),
        score=package_score,
        confidence=package_confidence,
        model_version=package_model_version,
        explanation=package_explanation,
        decision=decision,
        decision_rank=decision_rank,
        selected_at=selected_at if decision == VacationScheduleCandidatePackage.DECISION_SELECTED else None,
    )
    period_records = []
    for order, (candidate, candidate_record) in enumerate(zip(candidates, candidate_records), start=1):
        period_records.append(
            VacationScheduleCandidatePackagePeriod(
                candidate_package=package_record,
                candidate=candidate_record,
                start_date=candidate.start_date,
                end_date=candidate.end_date,
                chargeable_days=int(candidate.metadata.get("chargeable_days") or 0),
                passed_hard_rules=_candidate_passed_hard_rules(candidate),
                block_reason_key=(candidate.metadata.get("block_reason_key") or "")[:80],
                block_reason=candidate.metadata.get("block_reason") or "",
                risk_score=int(candidate.metadata.get("risk_score") or 0),
                risk_level=candidate.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW,
                features=_generation_candidate_features(candidate),
                order=order,
            )
        )
    period_records = VacationScheduleCandidatePackagePeriod.objects.bulk_create(period_records)
    package.stored_package = package_record
    return package_record, candidate_records, period_records


def _find_covering_draft_item(items, start_date, end_date):
    return next(
        (
            item
            for item in items
            if item.start_date <= start_date and item.end_date >= end_date
        ),
        None,
    )


@transaction.atomic
def place_manual_schedule_draft_items(*, year, employee_id, periods, actor):
    normalized_periods = _manual_package_periods_from_input(periods, year)
    schedule, employee, draft_items, draft_items_by_employee, planning_need = _manual_draft_employee_context(
        year,
        employee_id,
        for_update=True,
    )
    if not planning_need["needs_manual_attention"]:
        raise ValidationError("По сотруднику уже закрыта плановая потребность.")

    preview = build_manual_schedule_draft_package_preview(
        year=year,
        employee_id=employee_id,
        periods=normalized_periods,
    )
    if not preview["can_submit"]:
        raise ValidationError(preview["message"])

    context = _build_draft_generation_context(year, schedule)
    context_employee = next((candidate for candidate in context.eligible_employees if candidate.id == employee.id), employee)
    suggestion_packages = _manual_candidate_packages(
        context,
        context_employee,
        limit=MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS,
    )
    selected_key = tuple((period["start_date"], period["end_date"]) for period in preview["periods"])
    selected_package = next(
        (
            package
            for package in suggestion_packages
            if _manual_package_key(package) == selected_key
        ),
        None,
    )
    if selected_package is None:
        selected_candidates = [
            _manual_candidate_from_period_preview(
                employee,
                period_payload,
                planning_need,
                year,
                actor=actor,
                order=index,
            )
            for index, period_payload in enumerate(preview["periods"], start=1)
        ]
        selected_package = DraftGenerationCandidatePackage(
            employee=employee,
            candidates=selected_candidates,
            source=VacationScheduleItem.SOURCE_MANUAL,
            explanation=preview["message"],
            metadata={
                "package_kind": "manual_selected",
                "total_chargeable_days": preview["chargeable_days"],
                "periods_count": len(selected_candidates),
                "package_target_days": planning_need["open_required_days"],
                "remaining_after_package": preview["remaining_after_placement"],
                "package_closes_need": preview["remaining_after_placement"] <= 0,
            },
        )

    generation_run = _start_schedule_generation_run(schedule, actor)
    _, selected_candidate_records, selected_period_records = _persist_manual_candidate_package(
        generation_run,
        schedule,
        selected_package,
        decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
        decision_rank=1,
    )
    selected_key = _manual_package_key(selected_package)
    rejected_rank = 2
    for package in suggestion_packages[:3]:
        if _manual_package_key(package) == selected_key:
            continue
        _persist_manual_candidate_package(
            generation_run,
            schedule,
            package,
            decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
            decision_rank=rejected_rank,
        )
        rejected_rank += 1

    created_items = []
    placements = _current_placements_from_items(draft_items)
    current_items = draft_items_by_employee.setdefault(employee.id, [])
    for period_payload, candidate_record in zip(preview["periods"], selected_candidate_records):
        item = _create_draft_item_from_assessment(
            schedule,
            employee,
            period_payload["start_date"],
            period_payload["end_date"],
            period_payload["assessment"],
            source=VacationScheduleItem.SOURCE_MANUAL,
            comment=f"Вручную размещено HR: {actor.full_name if actor else 'HR'}.",
            generation_run=generation_run,
            selected_candidate_record=candidate_record,
        )
        current_items.append(item)
        placements.append(DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id))
        created_items.append(item)

    _merge_adjacent_employee_draft_items(schedule, employee, draft_items_by_employee, placements)
    refreshed_items = list(
        VacationScheduleItem.objects.filter(
            schedule=schedule,
            employee=employee,
            status=VacationScheduleItem.STATUS_DRAFT,
        ).order_by("start_date", "end_date", "id")
    )
    for period_record in selected_period_records:
        covering_item = _find_covering_draft_item(refreshed_items, period_record.start_date, period_record.end_date)
        if covering_item is not None:
            period_record.schedule_item = covering_item
            period_record.save(update_fields=["schedule_item"])

    unresolved_count = build_schedule_draft_summary_context(year)["draft_summary"]["manual"]
    _finish_schedule_generation_run(generation_run, manual_count=unresolved_count)
    _invalidate_schedule_draft_manual_suggestion_cache(schedule)
    return {
        "schedule": schedule,
        "items": created_items,
        "generation_run": generation_run,
    }


@transaction.atomic
def place_manual_schedule_draft_item(*, year, employee_id, start_date, end_date, actor):
    result = place_manual_schedule_draft_items(
        year=year,
        employee_id=employee_id,
        periods=[
            {
                "start_date": start_date,
                "end_date": end_date,
            }
        ],
        actor=actor,
    )
    return {
        "schedule": result["schedule"],
        "item": result["items"][0] if result["items"] else None,
    }
