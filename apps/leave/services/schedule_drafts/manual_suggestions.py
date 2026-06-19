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


def _get_draft_schedule_for_preview(year):
    schedule = VacationSchedule.objects.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")
    return schedule


def build_schedule_draft_urgent_closure_options(*, year, employee_id):
    schedule = _get_draft_schedule_for_preview(year)
    eligible_employees = get_eligible_preference_employees(year)
    employee = next((item for item in eligible_employees if item.id == employee_id), None)
    if employee is None:
        raise ValidationError("Сотрудник не найден в текущем сборе пожеланий.")

    employee_ids = [employee.id]
    preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    draft_items = [item for item in _draft_items_for_schedule(schedule) if item.employee_id == employee.id]
    draft_items_by_employee = {employee.id: draft_items}
    planning_need = build_employee_schedule_planning_need_map(
        [employee],
        year,
        draft_items_by_employee=draft_items_by_employee,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
    )[employee.id]
    urgent_closure = detect_previous_year_closure_need(employee, year, planning_need, include_options=True)
    if urgent_closure is None:
        raise ValidationError("Срочный остаток для этого сотрудника уже не требуется.")
    return urgent_closure


def _manual_package_target_candidates(
    context,
    employee,
    current_items,
    planning_need,
    *,
    candidates_per_strategy=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
    seed_limit=None,
    include_risk_explanation=True,
):
    _, planning_end = _planning_year_bounds(context.year)
    open_required_days = planning_need["open_required_days"]
    if open_required_days <= 0:
        return []

    latest_end = planning_need.get("nearest_deadline") if planning_need.get("has_blocker") else planning_end
    latest_end = latest_end or planning_end
    urgent = bool(planning_need.get("has_blocker"))
    target_days = [
        open_required_days,
        min(open_required_days, Decimal(AUTO_DRAFT_FALLBACK_CHUNK_DAYS)),
        min(open_required_days, Decimal("21")),
        min(open_required_days, Decimal("14")),
    ]
    candidates = []
    backup_candidate = _backup_preference_candidate(
        context,
        employee,
        planning_need,
        metadata={"manual_package_seed": True},
        include_risk_explanation=include_risk_explanation,
    )
    if backup_candidate is not None:
        candidates.append(backup_candidate)
    candidates.extend(
        _build_auto_generation_candidates(
            context,
            employee,
            current_items,
            planning_need,
            candidates_per_strategy=candidates_per_strategy,
            max_candidates=max(candidates_per_strategy * 3, candidates_per_strategy),
            include_risk_explanation=include_risk_explanation,
        )
    )
    for target in target_days:
        if target <= 0:
            continue
        payloads = list(
            _iter_auto_candidate_payloads_for_need(
                employee,
                context.year,
                context.placements,
                target,
                latest_end,
                urgent=urgent,
                allow_short_parts=target < MIN_CONTINUOUS_PAID_LEAVE_DAYS,
                max_chargeable_days=target,
                limit=candidates_per_strategy,
                exclude_schedule_item_ids=context.excluded_schedule_item_ids,
                assessment_cache=context.assessment_cache,
                risk_context_cache=context.risk_context_cache,
                include_risk_explanation=include_risk_explanation,
            )
        )
        candidates.extend(
            _auto_generation_candidates_from_payloads(
                employee,
                payloads,
                kind=DRAFT_CANDIDATE_AUTO_URGENT if urgent else DRAFT_CANDIDATE_AUTO,
                comment="Предложение модуля для ручного распределения.",
                planning_need=planning_need,
                metadata={"manual_package_seed": True},
            )
        )
    ranked_candidates = _rank_generation_candidates(_dedupe_generation_candidates(candidates))
    ranked_candidates = sorted(
        ranked_candidates,
        key=lambda candidate: 1 if candidate.metadata.get("is_preference_candidate") else 0,
        reverse=True,
    )
    if seed_limit is not None:
        return ranked_candidates[:seed_limit]
    return ranked_candidates


def _backup_preference_candidate(context, employee, planning_need, *, metadata=None, include_risk_explanation=True):
    pair = context.preference_pair_by_employee.get(employee.id) or {}
    preference = pair.get(VacationPreference.PRIORITY_BACKUP)
    if not preference or not preference.start_date or not preference.end_date:
        return None

    open_required_days = _decimal_to_whole_days(planning_need.get("open_required_days"))
    if open_required_days <= 0:
        return None

    backup_days = _decimal_to_whole_days(get_chargeable_leave_days(preference.start_date, preference.end_date, "paid"))
    if backup_days <= 0:
        return None

    target_days = min(open_required_days, backup_days)
    start_date = preference.start_date
    end_date = preference.end_date
    preference_match = "backup"
    preference_match_label = "Запасное пожелание"
    if target_days < backup_days:
        partial_end = _end_date_for_chargeable_days(start_date, target_days, preference.end_date)
        if partial_end is None:
            return None
        end_date = partial_end
        preference_match = "backup_partial"
        preference_match_label = "Часть запасного пожелания"

    candidate = DraftGenerationCandidate(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        kind=DRAFT_CANDIDATE_BACKUP_PREFERENCE,
        source=VacationScheduleItem.SOURCE_GENERATED,
        comment=f"Предложение модуля: {preference_match_label.lower()}.",
        preference=preference,
        metadata={
            "priority": VacationPreference.PRIORITY_BACKUP,
            "target_chargeable_days": Decimal(target_days),
            "preference_match": preference_match,
            "preference_match_label": preference_match_label,
            "is_preference_candidate": True,
            **(metadata or {}),
        },
    )
    _apply_planning_need_metadata(candidate, planning_need, context.year)
    return _assess_generation_candidate_hard_rules(
        candidate,
        context.year,
        context.placements,
        max_chargeable_days=Decimal(target_days),
        exclude_schedule_item_ids=context.excluded_schedule_item_ids,
        assessment_cache=context.assessment_cache,
        risk_context_cache=context.risk_context_cache,
        include_risk_explanation=include_risk_explanation,
    )


def _manual_package_context_after_candidate(context, employee, current_items, candidate):
    next_context = DraftGenerationContext(
        year=context.year,
        schedule=context.schedule,
        eligible_employees=context.eligible_employees,
        draft_items_by_employee={key: list(value) for key, value in context.draft_items_by_employee.items()},
        preference_pair_by_employee=context.preference_pair_by_employee,
        preference_state_by_employee=context.preference_state_by_employee,
        placements=list(context.placements),
        planning_need_by_employee=dict(context.planning_need_by_employee),
        excluded_schedule_item_ids=set(context.excluded_schedule_item_ids),
        planning_static_by_employee=context.planning_static_by_employee,
        assessment_cache=context.assessment_cache,
        risk_context_cache=context.risk_context_cache,
    )
    next_items = list(current_items)
    chargeable_days = candidate.metadata.get("chargeable_days") or candidate.assessment.get("chargeable_days") or 0
    next_items.append(
        _virtual_draft_item(
            employee,
            candidate.start_date,
            candidate.end_date,
            chargeable_days,
            source=candidate.source,
            selected_candidate=candidate,
        )
    )
    next_context.draft_items_by_employee[employee.id] = next_items
    next_context.placements.append(DraftPlacement(employee.id, candidate.start_date, candidate.end_date, None))
    next_context.planning_need_by_employee[employee.id] = _current_employee_planning_need(next_context, employee)
    return next_context, next_items


def _build_manual_candidate_package_from_seed(
    context,
    employee,
    seed_candidate,
    *,
    candidates_per_strategy=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
    include_risk_explanation=True,
):
    candidates = [seed_candidate]
    current_context = context
    current_items = list(context.draft_items_by_employee.get(employee.id, []))

    if not _candidate_passed_hard_rules(seed_candidate):
        return DraftGenerationCandidatePackage(
            employee=employee,
            candidates=candidates,
            source=VacationScheduleItem.SOURCE_GENERATED,
            explanation="Пакет заблокирован жесткими правилами.",
            metadata={"package_kind": "manual_suggestion"},
        )

    current_context, current_items = _manual_package_context_after_candidate(current_context, employee, current_items, seed_candidate)
    while len(candidates) < MANUAL_DRAFT_MAX_PACKAGE_PERIODS:
        planning_need = _current_employee_planning_need(current_context, employee)
        if not planning_need["needs_manual_attention"]:
            break
        _, planning_end = _planning_year_bounds(current_context.year)
        latest_end = planning_need.get("nearest_deadline") if planning_need.get("has_blocker") else planning_end
        latest_end = latest_end or planning_end
        target_days = min(planning_need["open_required_days"], Decimal(AUTO_DRAFT_FALLBACK_CHUNK_DAYS))
        payloads = list(
            _iter_auto_candidate_payloads_for_need(
                employee,
                current_context.year,
                current_context.placements,
                target_days,
                latest_end,
                urgent=bool(planning_need.get("has_blocker")),
                allow_short_parts=True,
                max_chargeable_days=planning_need["open_required_days"],
                limit=candidates_per_strategy,
                exclude_schedule_item_ids=current_context.excluded_schedule_item_ids,
                assessment_cache=current_context.assessment_cache,
                risk_context_cache=current_context.risk_context_cache,
                include_risk_explanation=include_risk_explanation,
            )
        )
        next_candidates = _rank_generation_candidates(
            _auto_generation_candidates_from_payloads(
                employee,
                payloads,
                kind=DRAFT_CANDIDATE_AUTO_URGENT if planning_need.get("has_blocker") else DRAFT_CANDIDATE_AUTO,
                comment="Предложение модуля: следующая часть отпуска.",
                planning_need=planning_need,
                metadata={"manual_package_continuation": True},
            )
        )
        next_candidate = _select_first_passed_generation_candidate(next_candidates)
        if next_candidate is None:
            break
        candidates.append(next_candidate)
        current_context, current_items = _manual_package_context_after_candidate(
            current_context,
            employee,
            current_items,
            next_candidate,
        )

    total_days = sum((_feature_decimal(candidate.metadata.get("chargeable_days")) for candidate in candidates), Decimal("0.00"))
    remaining_need = _current_employee_planning_need(current_context, employee)
    explanation = (
        "Пакет полностью закрывает плановую потребность."
        if not remaining_need["needs_manual_attention"]
        else "Пакет закрывает часть дней, остаток останется вручную."
    )
    return DraftGenerationCandidatePackage(
        employee=employee,
        candidates=candidates,
        source=VacationScheduleItem.SOURCE_GENERATED,
        explanation=explanation,
        metadata={
            "package_kind": "manual_suggestion",
            "total_chargeable_days": total_days,
            "periods_count": len(candidates),
            "remaining_after_package": remaining_need["open_required_days"],
            "package_closes_need": not remaining_need["needs_manual_attention"],
        },
    )


def _manual_package_key(package):
    return tuple((candidate.start_date, candidate.end_date) for candidate in package.candidates)


def _rank_manual_candidate_packages(packages):
    def package_rank(package):
        candidates = package.candidates
        has_preference_candidate = any(candidate.metadata.get("is_preference_candidate") for candidate in candidates)
        has_exact_preference = any(candidate.metadata.get("preference_match") == "backup" for candidate in candidates)
        total_days = sum((_feature_decimal(candidate.metadata.get("chargeable_days")) for candidate in candidates), Decimal("0.00"))
        max_risk = max((int(candidate.metadata.get("risk_score") or 0) for candidate in candidates), default=0)
        closes_need = bool(package.metadata.get("package_closes_need"))
        return (
            1 if has_preference_candidate else 0,
            1 if has_exact_preference else 0,
            1 if closes_need else 0,
            _manual_package_quality_score(package),
            total_days,
            Decimal("100.00") - Decimal(max_risk),
            -len(candidates),
        )

    return sorted(packages, key=package_rank, reverse=True)


def _manual_candidate_packages(
    context,
    employee,
    limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS,
    *,
    planning_need=None,
    current_items=None,
    candidates_per_strategy=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
    seed_multiplier=None,
    include_risk_explanation=True,
):
    current_items = list(context.draft_items_by_employee.get(employee.id, []) if current_items is None else current_items)
    planning_need = planning_need or _current_employee_planning_need(context, employee)
    if seed_multiplier is None:
        seed_limit = None
        package_build_limit = int(limit or 1) * 2
    else:
        seed_limit = max(1, min(MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS, int(limit or 1) * max(1, int(seed_multiplier or 1))))
        package_build_limit = seed_limit
    seeds = [
        candidate
        for candidate in _manual_package_target_candidates(
            context,
            employee,
            current_items,
            planning_need,
            candidates_per_strategy=candidates_per_strategy,
            seed_limit=seed_limit,
            include_risk_explanation=include_risk_explanation,
        )
        if _candidate_passed_hard_rules(candidate)
    ]
    packages = []
    seen = set()
    for seed in seeds:
        package = _build_manual_candidate_package_from_seed(
            context,
            employee,
            seed,
            candidates_per_strategy=candidates_per_strategy,
            include_risk_explanation=include_risk_explanation,
        )
        key = _manual_package_key(package)
        if not key or key in seen:
            continue
        seen.add(key)
        packages.append(package)
        if len(packages) >= package_build_limit:
            break
    return _rank_manual_candidate_packages(packages)[:limit]


def _manual_package_payload(package, *, rank=None, alternative_count=0):
    candidates = [_apply_candidate_scoring(candidate) for candidate in package.candidates]
    period_payloads = [_generation_candidate_payload(candidate, rank=index) for index, candidate in enumerate(candidates, start=1)]
    preference_payload = next((payload for payload in period_payloads if payload.get("is_preference_candidate")), None)
    total_days = sum((_feature_decimal(payload["chargeable_days"]) for payload in period_payloads), Decimal("0.00"))
    package_score = _manual_package_quality_score(package)
    period_avg_confidence = (
        sum((_feature_decimal(payload["confidence"]) for payload in period_payloads), Decimal("0.00"))
        / Decimal(len(period_payloads))
        if period_payloads
        else Decimal("0.00")
    )
    package_confidence = package.metadata.get("package_confidence") or period_avg_confidence
    highest_risk = max(
        period_payloads,
        key=lambda payload: (_risk_level_rank(payload["risk_level"]), int(payload["risk_score"] or 0)),
        default={},
    )
    first_period = period_payloads[0] if period_payloads else {}
    staffing_summary, staffing_chips = _worst_staffing_summary_from_payloads(period_payloads)
    package_explanation = package.metadata.get("package_scoring_explanation") or package.explanation
    package_recommendation = package.metadata.get("package_recommendation", "")
    return {
        "rank": rank,
        "periods_count": len(period_payloads),
        "periods": period_payloads,
        "period_label": " + ".join(payload["period_label"] for payload in period_payloads),
        "full_period_label": " + ".join(payload["full_period_label"] for payload in period_payloads),
        "start_date": first_period.get("start_date", ""),
        "end_date": first_period.get("end_date", ""),
        "chargeable_days": int(total_days),
        "chargeable_days_label": _days_label(total_days),
        "kind": "manual_package",
        "kind_label": f"{len(period_payloads)} период(а)",
        "passed_hard_rules": all(payload["passed_hard_rules"] for payload in period_payloads),
        "can_apply": all(payload["can_apply"] for payload in period_payloads),
        "preference_match": preference_payload.get("preference_match", "") if preference_payload else "",
        "preference_match_label": preference_payload.get("preference_match_label", "") if preference_payload else "",
        "is_preference_candidate": preference_payload is not None,
        "risk_score": int(highest_risk.get("risk_score") or 0),
        "risk_level": highest_risk.get("risk_level") or VacationScheduleItem.RISK_LOW,
        "risk_label": highest_risk.get("risk_label") or "Низкий",
        "risk_tone": highest_risk.get("risk_tone") or "low",
        "staffing_summary": staffing_summary,
        "staffing_chips": staffing_chips,
        "score": package_score,
        "score_label": _percent_label(package_score),
        "confidence": package_confidence,
        "confidence_label": _percent_label(package_confidence),
        "period_confidence": period_avg_confidence,
        "period_confidence_label": _percent_label(period_avg_confidence),
        "model_version": package.metadata.get("package_model_version", ""),
        "scorer_kind": package.metadata.get("package_scorer_kind", ""),
        "recommendation": package_recommendation,
        "recommendation_label": _package_preview_recommendation_label(package_recommendation),
        "explanation": package_explanation,
        "package_explanation": package_explanation,
        "alternative_count": int(alternative_count or 0),
        "message": package_explanation,
    }


def _manual_preference_option_payload(candidate):
    if candidate is None:
        return None

    candidate = _apply_candidate_scoring(candidate)
    payload = _generation_candidate_payload(candidate)
    can_apply = _candidate_passed_hard_rules(candidate)
    block_reason = candidate.metadata.get("block_reason") or candidate.metadata.get("block_reason_detail") or ""
    payload.update(
        {
            "can_apply": can_apply,
            "status_label": "Учтено в предложениях" if can_apply else "Не подходит",
            "status_tone": "ready" if can_apply else "blocked",
            "reason": (
                "Запасной период прошел проверки и добавлен в список предложений."
                if can_apply
                else block_reason or payload.get("explanation") or "Запасной период не прошел проверку."
            ),
        }
    )
    return payload


def _json_safe_manual_suggestion_payload(payload):
    return json.loads(json.dumps(payload, cls=DjangoJSONEncoder))


def _limit_manual_suggestion_payload(payload, limit):
    limit = max(1, min(int(limit or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS), MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS))
    payload = dict(payload or {})
    all_options = list(payload.get("options") or [])
    visible_options = all_options[:limit]
    total_candidates = int(payload.get("total_candidates") or len(all_options))
    payload["visible_limit"] = limit
    payload["options"] = visible_options
    payload["shown_candidates"] = len(visible_options)
    payload["has_more_options"] = total_candidates > len(visible_options)
    return payload


def _manual_suggestion_payload_from_context(
    context,
    employee,
    *,
    limit=MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS,
    package_limit=None,
    planning_need=None,
):
    limit = max(1, min(int(limit or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS), MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS))
    package_limit = max(
        1,
        min(int(package_limit or limit), MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS),
    )
    planning_need = planning_need or _current_employee_planning_need(context, employee)
    preference_option = _manual_preference_option_payload(
        _backup_preference_candidate(
            context,
            employee,
            planning_need,
            metadata={"manual_package_seed": True},
            include_risk_explanation=False,
        )
    )
    packages = _manual_candidate_packages(
        context,
        employee,
        limit=package_limit,
        planning_need=planning_need,
        candidates_per_strategy=MANUAL_DRAFT_SUGGESTION_CANDIDATES_PER_STRATEGY,
        seed_multiplier=MANUAL_DRAFT_SUGGESTION_SEED_MULTIPLIER,
        include_risk_explanation=False,
    )
    visible_packages = packages[:limit]
    return {
        "suggestion_schema_version": MANUAL_DRAFT_SUGGESTION_CACHE_SCHEMA_VERSION,
        "employee_id": employee.id,
        "employee_name": employee.full_name,
        "needed_label": planning_need["manual_task_label"],
        "status_label": planning_need["status"]["label"],
        "target_days_label": planning_need["target_days_label"],
        "placed_days_label": planning_need["placed_days_label"],
        "planning_year": context.year,
        "date_min": date(context.year, 1, 1).isoformat(),
        "date_max": date(context.year, 12, 31).isoformat(),
        "preference_option": preference_option,
        "visible_limit": limit,
        "options": [
            _manual_package_payload(package, rank=index, alternative_count=max(len(packages) - 1, 0))
            for index, package in enumerate(visible_packages, start=1)
        ],
        "total_candidates": len(packages),
        "safe_candidates": len(packages),
        "shown_candidates": len(visible_packages),
        "has_more_options": len(packages) > len(visible_packages),
        "cache_package_limit": package_limit,
        "cache_complete": len(packages) < package_limit,
    }


def _get_current_manual_suggestion_cache(schedule, employee_id):
    version = int(schedule.manual_suggestion_cache_version or 0)
    if version <= 0:
        return None
    cache = VacationScheduleManualSuggestionCache.objects.filter(
        schedule=schedule,
        employee_id=employee_id,
        version=version,
    ).first()
    if cache is None:
        return None
    if int((cache.payload or {}).get("suggestion_schema_version") or 0) != MANUAL_DRAFT_SUGGESTION_CACHE_SCHEMA_VERSION:
        cache.delete()
        return None
    return cache


def _rebuild_schedule_draft_manual_suggestion_cache(schedule):
    if schedule is None:
        return 0

    context = _build_draft_generation_context(schedule.year, schedule)
    next_version = int(schedule.manual_suggestion_cache_version or 0) + 1
    rebuilt_at = timezone.now()
    cache_rows = []

    schedule.manual_suggestion_cache_version = next_version
    schedule.manual_suggestion_cache_rebuilt_at = rebuilt_at
    schedule.save(update_fields=["manual_suggestion_cache_version", "manual_suggestion_cache_rebuilt_at"])

    for employee in context.eligible_employees:
        planning_need = _current_employee_planning_need(context, employee)
        context.planning_need_by_employee[employee.id] = planning_need
        if not planning_need["needs_manual_attention"]:
            continue

        payload = _manual_suggestion_payload_from_context(
            context,
            employee,
            limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS,
            planning_need=planning_need,
        )
        cache_rows.append(
            VacationScheduleManualSuggestionCache(
                schedule=schedule,
                employee=employee,
                version=next_version,
                payload=_json_safe_manual_suggestion_payload(payload),
                created_at=rebuilt_at,
                updated_at=rebuilt_at,
            )
        )

    VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).delete()
    if cache_rows:
        VacationScheduleManualSuggestionCache.objects.bulk_create(cache_rows)
    return len(cache_rows)


def _invalidate_schedule_draft_manual_suggestion_cache(schedule):
    if schedule is None:
        return 0

    next_version = int(schedule.manual_suggestion_cache_version or 0) + 1
    invalidated_at = timezone.now()
    schedule.manual_suggestion_cache_version = next_version
    schedule.manual_suggestion_cache_rebuilt_at = invalidated_at
    schedule.save(update_fields=["manual_suggestion_cache_version", "manual_suggestion_cache_rebuilt_at"])
    deleted_count, _ = VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).delete()
    return deleted_count


def _ensure_manual_suggestion_cache_version(schedule):
    version = int(schedule.manual_suggestion_cache_version or 0)
    if version > 0:
        return version

    schedule.manual_suggestion_cache_version = 1
    schedule.manual_suggestion_cache_rebuilt_at = timezone.now()
    schedule.save(update_fields=["manual_suggestion_cache_version", "manual_suggestion_cache_rebuilt_at"])
    return schedule.manual_suggestion_cache_version


def _build_manual_suggestion_context_for_employee(schedule, employee_id):
    eligible_employees = list(get_eligible_preference_employees(schedule.year))
    employee = next((candidate for candidate in eligible_employees if candidate.id == employee_id), None)
    if employee is None:
        raise ValidationError("Сотрудник не найден в сборе пожеланий за этот год.")

    draft_items = _draft_items_for_schedule(schedule)
    grouped_items = _draft_items_by_employee(draft_items)
    preference_pair_by_employee = get_employee_preference_pair_map([employee.id], schedule.year)
    preference_state_by_employee = get_employee_preference_state_map([employee.id], schedule.year)
    planning_need_by_employee = build_employee_schedule_planning_need_map(
        [employee],
        schedule.year,
        draft_items_by_employee=grouped_items,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
    )
    planning_static_by_employee = {
        current_employee_id: {
            "available_days": planning_need["available_days"],
            "plan_available_days": planning_need["plan_available_days"],
            "entitlement_rows": planning_need["entitlement_rows"],
        }
        for current_employee_id, planning_need in planning_need_by_employee.items()
    }
    context = DraftGenerationContext(
        year=schedule.year,
        schedule=schedule,
        eligible_employees=[employee],
        draft_items_by_employee=grouped_items,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
        placements=_current_placements_from_items(draft_items),
        planning_need_by_employee=planning_need_by_employee,
        planning_static_by_employee=planning_static_by_employee,
    )
    return context, employee, planning_need_by_employee[employee.id]


def _build_schedule_draft_manual_suggestion_cache_for_employee(schedule, employee_id, *, package_limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS):
    if schedule is None:
        return None

    context, employee, planning_need = _build_manual_suggestion_context_for_employee(schedule, employee_id)
    employee = next((candidate for candidate in context.eligible_employees if candidate.id == employee_id), None)
    if employee is None:
        raise ValidationError("Сотрудник не найден в сборе пожеланий за этот год.")

    planning_need = _current_employee_planning_need(context, employee)
    context.planning_need_by_employee[employee.id] = planning_need
    if not planning_need["needs_manual_attention"]:
        return None

    version = _ensure_manual_suggestion_cache_version(schedule)
    payload = _manual_suggestion_payload_from_context(
        context,
        employee,
        limit=package_limit,
        package_limit=package_limit,
        planning_need=planning_need,
    )
    cache, _ = VacationScheduleManualSuggestionCache.objects.update_or_create(
        schedule=schedule,
        employee=employee,
        defaults={
            "version": version,
            "payload": _json_safe_manual_suggestion_payload(payload),
        },
    )
    return cache


def _manual_suggestion_cache_payload(cache, limit):
    payload = _limit_manual_suggestion_payload(cache.payload, limit)
    payload.update(
        {
            "from_cache": True,
            "cache_version": cache.version,
            "cached_at": cache.updated_at.isoformat() if cache.updated_at else "",
        }
    )
    return payload


def _manual_suggestion_cache_covers_limit(cache, limit):
    payload = cache.payload or {}
    if payload.get("cache_complete"):
        return True
    return int(payload.get("cache_package_limit") or 0) >= int(limit or 0)


def build_schedule_draft_manual_suggestions(*, year, employee_id, limit=MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS):
    limit = max(1, min(int(limit or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS), MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS))
    schedule = VacationSchedule.objects.filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    cache = _get_current_manual_suggestion_cache(schedule, employee_id)
    if cache is not None and not _manual_suggestion_cache_covers_limit(cache, limit):
        cache.delete()
        cache = None
    if cache is None:
        cache = _build_schedule_draft_manual_suggestion_cache_for_employee(
            schedule,
            employee_id,
            package_limit=limit,
        )
    if cache is None:
        context, employee, planning_need = _build_manual_suggestion_context_for_employee(schedule, employee_id)
        employee = next((candidate for candidate in context.eligible_employees if candidate.id == employee_id), None)
        if employee is None:
            raise ValidationError("Сотрудник не найден в сборе пожеланий за этот год.")

        payload = _manual_suggestion_payload_from_context(context, employee, limit=limit)
        payload.update(
            {
                "from_cache": False,
                "cache_version": schedule.manual_suggestion_cache_version,
                "cached_at": "",
            }
        )
        return payload
    if cache.version != int(schedule.manual_suggestion_cache_version or 0):
        cache = _get_current_manual_suggestion_cache(schedule, employee_id)
    if cache is not None:
        return _manual_suggestion_cache_payload(cache, limit)


def build_schedule_draft_auto_place_preview(*, year, limit=8):
    schedule = _get_draft_schedule_for_preview(year)
    context = _build_draft_generation_context(year, schedule)
    preview_options = []
    placed_count = 0
    blocked_count = 0
    high_risk_count = 0
    auto_employees = _sort_auto_place_employees(context.eligible_employees, context.planning_need_by_employee)
    initial_unresolved_count = len(auto_employees)
    processed_employee_ids = set()
    max_preview_employees = max(int(limit or 0) * 3, 8)

    for employee in auto_employees:
        if len(preview_options) >= limit:
            break
        if preview_options and len(processed_employee_ids) >= max_preview_employees:
            break
        processed_employee_ids.add(employee.id)
        chunks_count = 0
        while chunks_count < AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE:
            if len(preview_options) >= limit:
                break
            planning_need = _current_employee_planning_need(context, employee)
            if not planning_need["needs_manual_attention"]:
                context.planning_need_by_employee[employee.id] = planning_need
                break

            packages, selected_package = _auto_candidate_packages_for_employee(
                context,
                employee,
                package_limit=AUTO_DRAFT_PREVIEW_PACKAGE_SUGGESTIONS,
            )
            if selected_package is None:
                blocked_count += 1
                context.planning_need_by_employee[employee.id] = planning_need
                break

            package_periods_count = len(selected_package.candidates)
            placed_count += package_periods_count
            chunks_count += package_periods_count
            high_risk_count += sum(
                1
                for candidate in selected_package.candidates
                if candidate.metadata.get("risk_level") == VacationScheduleItem.RISK_HIGH
            )
            if len(preview_options) < limit:
                day_calculation = build_schedule_day_calculation_payload(employee, year, planning_need)
                package_payload = _manual_package_payload(selected_package, rank=len(preview_options) + 1)
                proposal_text = (
                    package_payload.get("preference_match_label")
                    or package_payload.get("kind_label")
                    or "пакет периодов"
                )
                preview_options.append(
                    {
                        "employee_id": employee.id,
                        "employee_name": employee.full_name,
                        "department_name": _employee_org_payload(employee)["department_name"],
                        "day_calculation": day_calculation,
                        "calculation_note": (
                            f"Осталось {day_calculation['open_required_days_label']}. "
                            f"{day_calculation['short_reason']}"
                        ),
                        "proposal_note": f"Предложение: {proposal_text.lower()}.",
                        **package_payload,
                    }
                )

            _register_virtual_candidate_package(context, selected_package)

        if chunks_count >= AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE:
            planning_need = _current_employee_planning_need(context, employee)
            if planning_need["needs_manual_attention"]:
                blocked_count += 1

    processed_unresolved_after = sum(
        1
        for employee in context.eligible_employees
        if employee.id in processed_employee_ids
        and _current_employee_planning_need(context, employee)["needs_manual_attention"]
    )
    unresolved_after = (initial_unresolved_count - len(processed_employee_ids)) + processed_unresolved_after
    return {
        "placed_count": placed_count,
        "unresolved_count": unresolved_after,
        "blocked_count": blocked_count,
        "high_risk_count": high_risk_count,
        "options": preview_options,
        "has_more_options": len(processed_employee_ids) < initial_unresolved_count or placed_count > len(preview_options),
    }
