from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.core.models import DemoBaselineSnapshot, DemoDataResetJob, Notification
from apps.core.services.demo_locks import try_demo_data_mutation_lock
from apps.employees.models import (
    DepartmentCoverageRule,
    Departments,
    EmployeePosition,
    Employees,
    ProductionGroup,
    ProductionGroupSubstitutionRule,
)
from apps.leave.models import (
    DepartmentStaffingRule,
    DepartmentWorkload,
    VacationPreference,
    VacationPreferenceCollection,
    VacationSchedule,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)
from apps.leave.services.notifications import (
    notify_urgent_closure_created,
    notify_urgent_closure_employee_review,
    notify_urgent_closure_hr_finalization,
)


INITIAL_DEMO_STATE_KEY = "initial_demo_state"
SNAPSHOT_SCHEMA_VERSION = 1


class DemoBaselineMissingError(Exception):
    pass


class DemoBaselineResetInProgressError(Exception):
    pass


def _serialize_date(value):
    return value.isoformat() if value else None


def _serialize_datetime(value):
    return value.isoformat() if value else None


def _parse_date(value):
    return parse_date(value) if value else None


def _parse_datetime(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _serialize_staffing_payload():
    return {
        "departments": [
            {
                "id": department.id,
                "name": department.name,
                "head_id": department.head_id,
                "deputy_id": department.deputy_id,
                "date_added": _serialize_datetime(department.date_added),
            }
            for department in Departments.objects.order_by("id")
        ],
        "employees": [
            {
                "id": employee.id,
                "department_id": employee.department_id,
                "employee_position_id": employee.employee_position_id,
                "position": employee.position,
                "is_enterprise_deputy": employee.is_enterprise_deputy,
            }
            for employee in Employees.objects.order_by("id")
        ],
        "production_groups": [
            {
                "id": group.id,
                "department_id": group.department_id,
                "name": group.name,
                "code": group.code,
                "description": group.description,
            }
            for group in ProductionGroup.objects.order_by("id")
        ],
        "employee_positions": [
            {
                "id": position.id,
                "department_id": position.department_id,
                "production_group_id": position.production_group_id,
                "title": position.title,
                "is_active": position.is_active,
            }
            for position in EmployeePosition.objects.order_by("id")
        ],
        "coverage_rules": [
            {
                "id": rule.id,
                "department_id": rule.department_id,
                "production_group_id": rule.production_group_id,
                "min_staff_required": rule.min_staff_required,
                "max_absent": rule.max_absent,
                "criticality_level": rule.criticality_level,
            }
            for rule in DepartmentCoverageRule.objects.order_by("id")
        ],
        "substitution_rules": [
            {
                "id": rule.id,
                "department_id": rule.department_id,
                "source_group_id": rule.source_group_id,
                "substitute_group_id": rule.substitute_group_id,
                "max_covered_absences": rule.max_covered_absences,
            }
            for rule in ProductionGroupSubstitutionRule.objects.order_by("id")
        ],
        "staffing_rules": [
            {
                "id": rule.id,
                "department_id": rule.department_id,
                "min_staff_required": rule.min_staff_required,
                "max_absent": rule.max_absent,
                "criticality_level": rule.criticality_level,
                "substitution_group": rule.substitution_group,
            }
            for rule in DepartmentStaffingRule.objects.order_by("id")
        ],
        "department_workload": [
            {
                "id": workload.id,
                "department_id": workload.department_id,
                "year": workload.year,
                "month": workload.month,
                "load_level": workload.load_level,
                "min_staff_required": workload.min_staff_required,
                "max_absent": workload.max_absent,
            }
            for workload in DepartmentWorkload.objects.order_by("id")
        ],
    }


def _serialize_urgent_closures(planning_year):
    return [
        {
            "id": closure.id,
            "employee_id": closure.employee_id,
            "planning_year": closure.planning_year,
            "closure_year": closure.closure_year,
            "required_days": str(closure.required_days),
            "deadline": _serialize_date(closure.deadline),
            "proposed_start_date": _serialize_date(closure.proposed_start_date),
            "proposed_end_date": _serialize_date(closure.proposed_end_date),
            "reason": closure.reason,
            "status": closure.status,
            "created_by_id": closure.created_by_id,
            "department_reviewer_id": closure.department_reviewer_id,
            "department_reviewed_at": _serialize_datetime(closure.department_reviewed_at),
            "department_comment": closure.department_comment,
            "employee_responded_at": _serialize_datetime(closure.employee_responded_at),
            "employee_comment": closure.employee_comment,
            "finalized_by_id": closure.finalized_by_id,
            "finalized_at": _serialize_datetime(closure.finalized_at),
            "final_comment": closure.final_comment,
            "rejected_by_id": closure.rejected_by_id,
            "rejected_at": _serialize_datetime(closure.rejected_at),
            "rejection_comment": closure.rejection_comment,
            "created_schedule_item_id": closure.created_schedule_item_id,
            "risk_score": closure.risk_score,
            "risk_level": closure.risk_level,
            "department_load_level": closure.department_load_level,
            "overlapping_absences_count": closure.overlapping_absences_count,
            "remaining_staff_count": closure.remaining_staff_count,
            "min_staff_required": closure.min_staff_required,
            "balance_after_closure": str(closure.balance_after_closure),
            "created_at": _serialize_datetime(closure.created_at),
            "updated_at": _serialize_datetime(closure.updated_at),
        }
        for closure in VacationUrgentClosureRequest.objects.filter(planning_year=planning_year).order_by("id")
    ]


@transaction.atomic
def capture_demo_baseline_snapshot(*, planning_year, seed_value=None):
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": _serialize_datetime(timezone.now()),
        "staffing": _serialize_staffing_payload(),
        "urgent_closures": _serialize_urgent_closures(planning_year),
    }
    snapshot, _ = DemoBaselineSnapshot.objects.update_or_create(
        key=INITIAL_DEMO_STATE_KEY,
        defaults={
            "planning_year": planning_year,
            "seed_value": seed_value,
            "payload": payload,
        },
    )
    return snapshot


def _delete_notifications_by_prefixes(prefixes):
    query = Q()
    has_prefixes = False
    for prefix in prefixes:
        if not prefix:
            continue
        query |= Q(dedupe_key__startswith=prefix)
        has_prefixes = True
    if has_prefixes:
        Notification.objects.filter(query).delete()


def _clear_planning_year_workflow(planning_year):
    schedule_ids = list(VacationSchedule.objects.filter(year=planning_year).values_list("id", flat=True))
    item_ids = list(
        VacationScheduleItem.objects.filter(schedule_id__in=schedule_ids).values_list("id", flat=True)
    )
    change_request_ids = list(
        VacationScheduleChangeRequest.objects.filter(schedule_item_id__in=item_ids).values_list("id", flat=True)
    )
    urgent_closure_ids = list(
        VacationUrgentClosureRequest.objects.filter(planning_year=planning_year).values_list("id", flat=True)
    )

    prefixes = [f"{Notification.TYPE_PREFERENCES_COLLECTION_STARTED}:{planning_year}:"]
    for schedule_id in schedule_ids:
        prefixes.extend(
            [
                f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:department:{schedule_id}:",
                f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:enterprise:{schedule_id}:",
                f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:authorized:{schedule_id}:",
            ]
        )
    for item_id in item_ids:
        prefixes.append(f"{Notification.TYPE_SCHEDULE_ITEM_CHANGED_BY_MANAGER}:{item_id}:")
    for change_request_id in change_request_ids:
        prefixes.extend(
            [
                f"{Notification.TYPE_SCHEDULE_CHANGE_CREATED}:{change_request_id}:",
                f"{Notification.TYPE_SCHEDULE_CHANGE_APPROVED}:{change_request_id}:",
                f"{Notification.TYPE_SCHEDULE_CHANGE_REJECTED}:{change_request_id}:",
            ]
        )
    for closure_id in urgent_closure_ids:
        prefixes.append(f"urgent_closure:{closure_id}:")
    _delete_notifications_by_prefixes(prefixes)

    VacationPreference.objects.filter(year=planning_year).delete()
    VacationPreferenceCollection.objects.filter(year=planning_year).delete()
    VacationUrgentClosureRequest.objects.filter(planning_year=planning_year).delete()
    VacationSchedule.objects.filter(year=planning_year).delete()

    return {
        "deleted_schedules": len(schedule_ids),
        "deleted_schedule_items": len(item_ids),
        "deleted_urgent_closures": len(urgent_closure_ids),
    }


def _restore_departments(department_rows):
    department_ids = {row["id"] for row in department_rows}

    Departments.objects.update(head=None, deputy=None)
    Employees.objects.filter(department_id__isnull=False).exclude(department_id__in=department_ids).update(
        department=None
    )
    Departments.objects.exclude(id__in=department_ids).delete()

    for row in department_rows:
        Departments.objects.update_or_create(
            id=row["id"],
            defaults={
                "name": row["name"],
                "date_added": _parse_datetime(row.get("date_added")) or timezone.now(),
            },
        )

    employee_ids = set(Employees.objects.values_list("id", flat=True))
    for row in department_rows:
        Departments.objects.filter(id=row["id"]).update(
            head_id=row["head_id"] if row.get("head_id") in employee_ids else None,
            deputy_id=row["deputy_id"] if row.get("deputy_id") in employee_ids else None,
        )

    return department_ids


def _restore_staffing(payload):
    staffing = payload.get("staffing") or {}
    department_rows = staffing.get("departments") or []
    if not department_rows:
        raise ValueError("Начальный снимок демо-данных не содержит отделы.")

    employee_rows = staffing.get("employees") or []
    snapshot_employee_ids = {row["id"] for row in employee_rows}

    Employees.objects.update(employee_position=None, is_enterprise_deputy=False)
    Employees.objects.exclude(id__in=snapshot_employee_ids).update(department=None)

    DepartmentWorkload.objects.all().delete()
    DepartmentStaffingRule.objects.all().delete()
    DepartmentCoverageRule.objects.all().delete()
    ProductionGroupSubstitutionRule.objects.all().delete()
    EmployeePosition.objects.all().delete()
    ProductionGroup.objects.all().delete()

    department_ids = _restore_departments(department_rows)

    group_rows = [
        row for row in staffing.get("production_groups", []) if row.get("department_id") in department_ids
    ]
    ProductionGroup.objects.bulk_create(
        [
            ProductionGroup(
                id=row["id"],
                department_id=row["department_id"],
                name=row["name"],
                code=row.get("code") or "",
                description=row.get("description") or "",
            )
            for row in group_rows
        ]
    )
    group_ids = {row["id"] for row in group_rows}

    position_rows = [
        row
        for row in staffing.get("employee_positions", [])
        if row.get("department_id") in department_ids and row.get("production_group_id") in group_ids
    ]
    EmployeePosition.objects.bulk_create(
        [
            EmployeePosition(
                id=row["id"],
                department_id=row["department_id"],
                production_group_id=row["production_group_id"],
                title=row["title"],
                is_active=row.get("is_active", True),
            )
            for row in position_rows
        ]
    )
    position_ids = {row["id"] for row in position_rows}

    coverage_rows = [
        row
        for row in staffing.get("coverage_rules", [])
        if row.get("department_id") in department_ids and row.get("production_group_id") in group_ids
    ]
    DepartmentCoverageRule.objects.bulk_create(
        [
            DepartmentCoverageRule(
                id=row["id"],
                department_id=row["department_id"],
                production_group_id=row["production_group_id"],
                min_staff_required=row["min_staff_required"],
                max_absent=row["max_absent"],
                criticality_level=row["criticality_level"],
            )
            for row in coverage_rows
        ]
    )

    substitution_rows = [
        row
        for row in staffing.get("substitution_rules", [])
        if (
            row.get("department_id") in department_ids
            and row.get("source_group_id") in group_ids
            and row.get("substitute_group_id") in group_ids
        )
    ]
    ProductionGroupSubstitutionRule.objects.bulk_create(
        [
            ProductionGroupSubstitutionRule(
                id=row["id"],
                department_id=row["department_id"],
                source_group_id=row["source_group_id"],
                substitute_group_id=row["substitute_group_id"],
                max_covered_absences=row["max_covered_absences"],
            )
            for row in substitution_rows
        ]
    )

    staffing_rule_rows = [
        row for row in staffing.get("staffing_rules", []) if row.get("department_id") in department_ids
    ]
    DepartmentStaffingRule.objects.bulk_create(
        [
            DepartmentStaffingRule(
                id=row["id"],
                department_id=row["department_id"],
                min_staff_required=row["min_staff_required"],
                max_absent=row["max_absent"],
                criticality_level=row["criticality_level"],
                substitution_group=row.get("substitution_group") or "",
            )
            for row in staffing_rule_rows
        ]
    )

    workload_rows = [
        row for row in staffing.get("department_workload", []) if row.get("department_id") in department_ids
    ]
    DepartmentWorkload.objects.bulk_create(
        [
            DepartmentWorkload(
                id=row["id"],
                department_id=row["department_id"],
                year=row["year"],
                month=row["month"],
                load_level=row["load_level"],
                min_staff_required=row["min_staff_required"],
                max_absent=row["max_absent"],
            )
            for row in workload_rows
        ]
    )

    for row in employee_rows:
        department_id = row.get("department_id") if row.get("department_id") in department_ids else None
        position_id = row.get("employee_position_id") if row.get("employee_position_id") in position_ids else None
        Employees.objects.filter(id=row["id"]).update(
            department_id=department_id,
            employee_position_id=position_id,
            position=row.get("position") or "",
            is_enterprise_deputy=bool(row.get("is_enterprise_deputy")),
        )

    return {
        "departments": len(department_rows),
        "groups": len(group_rows),
        "positions": len(position_rows),
        "workload_months": len(workload_rows),
    }


def _restore_urgent_closures(planning_year, payload):
    rows = payload.get("urgent_closures") or []
    snapshot_ids = [row["id"] for row in rows if row.get("planning_year") == planning_year]
    _delete_notifications_by_prefixes([f"urgent_closure:{closure_id}:" for closure_id in snapshot_ids])

    employee_ids = set(Employees.objects.values_list("id", flat=True))
    schedule_item_ids = set(VacationScheduleItem.objects.values_list("id", flat=True))
    restored_ids = []
    for row in rows:
        if row.get("planning_year") != planning_year or row.get("employee_id") not in employee_ids:
            continue
        created_schedule_item_id = row.get("created_schedule_item_id")
        if created_schedule_item_id not in schedule_item_ids:
            created_schedule_item_id = None
        closure = VacationUrgentClosureRequest.objects.create(
            id=row["id"],
            employee_id=row["employee_id"],
            planning_year=row["planning_year"],
            closure_year=row["closure_year"],
            required_days=Decimal(row["required_days"]),
            deadline=_parse_date(row["deadline"]),
            proposed_start_date=_parse_date(row["proposed_start_date"]),
            proposed_end_date=_parse_date(row["proposed_end_date"]),
            reason=row.get("reason") or "",
            status=row["status"],
            created_by_id=row.get("created_by_id") if row.get("created_by_id") in employee_ids else None,
            department_reviewer_id=(
                row.get("department_reviewer_id") if row.get("department_reviewer_id") in employee_ids else None
            ),
            department_reviewed_at=_parse_datetime(row.get("department_reviewed_at")),
            department_comment=row.get("department_comment") or "",
            employee_responded_at=_parse_datetime(row.get("employee_responded_at")),
            employee_comment=row.get("employee_comment") or "",
            finalized_by_id=row.get("finalized_by_id") if row.get("finalized_by_id") in employee_ids else None,
            finalized_at=_parse_datetime(row.get("finalized_at")),
            final_comment=row.get("final_comment") or "",
            rejected_by_id=row.get("rejected_by_id") if row.get("rejected_by_id") in employee_ids else None,
            rejected_at=_parse_datetime(row.get("rejected_at")),
            rejection_comment=row.get("rejection_comment") or "",
            created_schedule_item_id=created_schedule_item_id,
            risk_score=row.get("risk_score") or 0,
            risk_level=row.get("risk_level") or VacationScheduleItem.RISK_LOW,
            department_load_level=row.get("department_load_level") or 1,
            overlapping_absences_count=row.get("overlapping_absences_count") or 0,
            remaining_staff_count=row.get("remaining_staff_count") or 0,
            min_staff_required=row.get("min_staff_required") or 0,
            balance_after_closure=Decimal(row.get("balance_after_closure") or "0"),
        )
        updates = {}
        created_at = _parse_datetime(row.get("created_at"))
        updated_at = _parse_datetime(row.get("updated_at"))
        if created_at:
            updates["created_at"] = created_at
        if updated_at:
            updates["updated_at"] = updated_at
        if updates:
            VacationUrgentClosureRequest.objects.filter(id=closure.id).update(**updates)
        restored_ids.append(closure.id)

    for closure in VacationUrgentClosureRequest.objects.filter(id__in=restored_ids).select_related(
        "employee",
        "created_by",
        "department_reviewer",
    ):
        if closure.status == VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW:
            notify_urgent_closure_created(closure)
        elif closure.status == VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW:
            notify_urgent_closure_employee_review(closure)
        elif closure.status == VacationUrgentClosureRequest.STATUS_HR_FINALIZATION:
            notify_urgent_closure_hr_finalization(closure)

    return {"urgent_closures": len(restored_ids)}


@transaction.atomic
def reset_demo_to_baseline(*, actor=None):
    if not try_demo_data_mutation_lock():
        raise DemoBaselineResetInProgressError
    if DemoDataResetJob.objects.filter(
        status__in=[DemoDataResetJob.STATUS_QUEUED, DemoDataResetJob.STATUS_RUNNING]
    ).exists():
        raise DemoBaselineResetInProgressError

    try:
        snapshot = DemoBaselineSnapshot.objects.select_for_update().get(key=INITIAL_DEMO_STATE_KEY)
    except DemoBaselineSnapshot.DoesNotExist as exc:
        raise DemoBaselineMissingError from exc

    planning_year = snapshot.planning_year
    payload = snapshot.payload or {}
    workflow_stats = _clear_planning_year_workflow(planning_year)
    staffing_stats = _restore_staffing(payload)
    urgent_stats = _restore_urgent_closures(planning_year, payload)

    return {
        "planning_year": planning_year,
        "actor_id": getattr(actor, "id", None),
        **workflow_stats,
        **staffing_stats,
        **urgent_stats,
    }
