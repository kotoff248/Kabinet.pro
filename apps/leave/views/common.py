import json
from datetime import date
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)

from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from apps.leave.services.schedule_planning import (
    get_schedule_planning_year,
    schedule_planning_url,
)


def _form_errors_to_messages(form):
    errors = []
    for field_errors in form.errors.values():
        errors.extend(field_errors)
    return " ".join(str(error) for error in errors)


def _validation_error_message(exc):
    return " ".join(exc.messages) if getattr(exc, "messages", None) else str(exc)


def _request_wants_json(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )


def _urgent_closure_create_success_message(demo_result):
    if not demo_result or not demo_result.get("manager_approved"):
        return "Согласование срочного остатка отправлено руководителю отдела."
    if demo_result.get("employee_proposed"):
        return "Демо-ответы применены: сотрудник предложил другой период, задача снова у руководителя."
    if demo_result.get("employee_accepted"):
        return "Демо-ответы применены: руководитель и сотрудник подтвердили период, ожидается финализация HR."
    return "Демо-ответ применен: руководитель подтвердил период, ожидается ответ сотрудника."


def _json_number(value):
    return float(value or 0)


def _serialize_entitlement_source_preview(preview):
    return {
        "entitlement_source_label": preview["label"],
        "entitlement_allocations": [
            {
                "working_year_number": row["working_year_number"],
                "period_label": row["period_label"],
                "period_start": row["period_start"].isoformat(),
                "period_end": row["period_end"].isoformat(),
                "days": _json_number(row["days"]),
                "balance_before": _json_number(row["balance_before"]),
                "balance_after": _json_number(row["balance_after"]),
            }
            for row in preview["allocations"]
        ],
    }


def _serialize_vacation_request_ai_period(period):
    return {
        "start_date": period["start_date"].isoformat(),
        "end_date": period["end_date"].isoformat(),
        "period_label": period["period_label"],
        "calendar_days": period["calendar_days"],
        "chargeable_days": _json_number(period["chargeable_days"]),
        "module_score": _json_number(period["module_score"]),
        "module_score_label": period["module_score_label"],
        "module_confidence": _json_number(period["module_confidence"]),
        "module_confidence_label": period["module_confidence_label"],
        "module_model_version": period["module_model_version"],
        "module_recommendation": period["module_recommendation"],
        "module_recommendation_label": period["module_recommendation_label"],
        "module_action": period["module_action"],
        "module_explanation": period["module_explanation"],
        "module_scorer_kind": period["module_scorer_kind"],
        "risk_label": period["risk_label"],
        "risk_score": period["risk_score"],
        "risk_level": period["risk_level"],
        "risk_is_conflict": period["risk_is_conflict"],
    }


def _serialize_vacation_request_ai_support(ai_support):
    payload = _serialize_vacation_request_ai_period(ai_support)
    payload["module_alternatives"] = [
        _serialize_vacation_request_ai_period(option)
        for option in ai_support.get("module_alternatives", [])
    ]
    return payload


def _parse_preview_date(value, field_label):
    if not value:
        raise ValidationError(f"Выберите поле «{field_label}».")
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Некорректная дата в поле «{field_label}».")


def _parse_manual_periods_payload(raw_periods):
    if not raw_periods:
        raise ValidationError("Добавьте хотя бы один период отпуска.")
    if isinstance(raw_periods, str):
        try:
            raw_periods = json.loads(raw_periods)
        except json.JSONDecodeError:
            raise ValidationError("Не удалось разобрать список периодов.")
    if not isinstance(raw_periods, list):
        raise ValidationError("Список периодов должен быть массивом.")

    periods = []
    for index, period in enumerate(raw_periods, start=1):
        if not isinstance(period, dict):
            raise ValidationError(f"Период {index} заполнен некорректно.")
        periods.append(
            {
                "start_date": _parse_preview_date(period.get("start_date"), f"Дата начала {index}"),
                "end_date": _parse_preview_date(period.get("end_date"), f"Дата окончания {index}"),
            }
        )
    return periods


def _manual_package_preview_json(preview):
    return {
        "can_submit": preview["can_submit"],
        "message": preview["message"],
        "calendar_days": preview["calendar_days"],
        "chargeable_days": _json_number(preview["chargeable_days"]),
        "remaining_after_placement": _json_number(preview["remaining_after_placement"]),
        "target_days": _json_number(preview["planning_need"]["target_days"]),
        "placed_days": _json_number(preview["planning_need"]["placed_days"]),
        "open_required_days": _json_number(preview["planning_need"]["open_required_days"]),
        "blocking_after_placement": _json_number(preview.get("blocking_after_placement", 0)),
        "annual_remaining_after_placement": _json_number(preview.get("annual_remaining_after_placement", 0)),
        "risk_label": preview["risk_label"],
        "risk_score": preview["risk_score"],
        "risk_level": preview["risk_level"],
        "risk_tone": preview["risk_tone"],
        "risk_short_reason": preview["risk_short_reason"],
        "risk_recommended_action": preview["risk_recommended_action"],
        "risk_is_conflict": preview["risk_is_conflict"],
        "staffing_summary": preview.get("staffing_summary") or {},
        "staffing_chips": preview.get("staffing_chips") or [],
        "package_score": _json_number(preview.get("package_score", 0)),
        "package_score_label": preview.get("package_score_label", ""),
        "package_confidence": _json_number(preview.get("package_confidence", 0)),
        "package_confidence_label": preview.get("package_confidence_label", ""),
        "package_model_version": preview.get("package_model_version", ""),
        "package_recommendation": preview.get("package_recommendation", ""),
        "package_recommendation_label": preview.get("package_recommendation_label", ""),
        "package_explanation": preview.get("package_explanation", ""),
        "alternative_count": preview.get("alternative_count", 0),
        "periods": [
            {
                "order": period["order"],
                "start_date": period["start_date_iso"],
                "end_date": period["end_date_iso"],
                "period_label": period["period_label"],
                "full_period_label": period["full_period_label"],
                "calendar_days": period["calendar_days"],
                "chargeable_days": _json_number(period["chargeable_days"]),
                "chargeable_days_label": period["chargeable_days_label"],
                "can_place": period["can_place"],
                "message": period["message"],
                "risk_label": period["risk_label"],
                "risk_score": period["risk_score"],
                "risk_level": period["risk_level"],
                "risk_tone": period["risk_tone"],
                "risk_short_reason": period["risk_short_reason"],
                "risk_recommended_action": period["risk_recommended_action"],
                "risk_is_conflict": period["risk_is_conflict"],
                "staffing_summary": period.get("staffing_summary") or {},
                "staffing_chips": period.get("staffing_chips") or [],
                "remaining_after_period": _json_number(period["remaining_after_period"]),
            }
            for period in preview["periods"]
        ],
    }


def _calendar_year_redirect(year):
    return f"{reverse('calendar')}?view=year&year={year}"


def _safe_next_url(request, fallback):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return next_url
    return fallback


def _relative_return_path(url):
    split_url = urlsplit(url)
    if split_url.scheme or split_url.netloc:
        return urlunsplit(("", "", split_url.path or "/", split_url.query, split_url.fragment))
    return url


def _schedule_draft_return_source(return_url):
    query = dict(parse_qsl(urlsplit(return_url).query, keep_blank_values=True))
    return "schedule_planning" if query.get("from") == "schedule_planning" else "calendar"


def _url_with_query_params(url, **params):
    split_url = urlsplit(url)
    query = dict(parse_qsl(split_url.query, keep_blank_values=True))
    for key, value in params.items():
        if value in (None, ""):
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunsplit(
        (
            split_url.scheme,
            split_url.netloc,
            split_url.path,
            urlencode(query),
            split_url.fragment,
        )
    )


def _url_with_fragment(url, fragment):
    split_url = urlsplit(url)
    return urlunsplit(
        (
            split_url.scheme,
            split_url.netloc,
            split_url.path,
            split_url.query,
            fragment,
        )
    )


def _planning_nested_url(url, year, stage):
    return _url_with_query_params(
        url,
        **{
            "from": "schedule_planning",
            "back_url": schedule_planning_url(year, stage),
            "back_label": "К планированию",
        },
    )


def _inactive_planning_year_message(year):
    return f"Действия доступны только для активного планового года. Сейчас активен {get_schedule_planning_year()} год."


def _empty_urgent_closure_preview_payload(message):
    return {
        "can_submit": False,
        "message": message,
        "calendar_days": 0,
        "chargeable_days": 0,
        "period_label": "",
        "risk_label": "Низкий",
        "risk_score": 0,
        "risk_short_reason": "",
        "risk_recommended_action": "",
        "risk_is_conflict": False,
        "module_score": 0,
        "module_score_label": "",
        "module_confidence": 0,
        "module_confidence_label": "",
        "module_model_version": "",
        "module_recommendation": "",
        "module_explanation": "",
    }


def _urgent_closure_option_json(option):
    return {
        "start_date": option["start_date"].isoformat(),
        "end_date": option["end_date"].isoformat(),
        "period_label": option["period_label"],
        "calendar_days": option["calendar_days"],
        "chargeable_days": _json_number(option["chargeable_days"]),
        "chargeable_days_label": option["chargeable_days_label"],
        "can_submit": option["can_submit"],
        "message": option["message"],
        "risk_label": option["risk_label"],
        "risk_score": option["risk_score"],
        "risk_level": option["risk_level"],
        "risk_is_conflict": option["risk_is_conflict"],
        "module_score": _json_number(option.get("module_score") or 0),
        "module_score_label": option.get("module_score_label") or "",
        "module_confidence": _json_number(option.get("module_confidence") or 0),
        "module_confidence_label": option.get("module_confidence_label") or "",
        "module_model_version": option.get("module_model_version") or "",
        "module_recommendation": option.get("module_recommendation") or "",
        "module_explanation": option.get("module_explanation") or "",
    }


def _urgent_closure_preview_json(preview):
    return {
        "can_submit": preview["can_submit"],
        "message": preview["message"],
        "calendar_days": preview["calendar_days"],
        "chargeable_days": _json_number(preview["chargeable_days"]),
        "period_label": preview["period_label"],
        "risk_label": preview["risk_label"],
        "risk_score": preview["risk_score"],
        "risk_short_reason": preview["risk_short_reason"],
        "risk_recommended_action": preview["risk_recommended_action"],
        "risk_is_conflict": preview["risk_is_conflict"],
        "module_score": _json_number(preview.get("module_score") or 0),
        "module_score_label": preview.get("module_score_label") or "",
        "module_confidence": _json_number(preview.get("module_confidence") or 0),
        "module_confidence_label": preview.get("module_confidence_label") or "",
        "module_model_version": preview.get("module_model_version") or "",
        "module_recommendation": preview.get("module_recommendation") or "",
        "module_explanation": preview.get("module_explanation") or "",
    }
