from django.db.models import Q

from apps.accounts.services import (
    get_managed_department_id,
    is_authorized_person_employee,
    is_department_head_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.employees.models import Employees


def get_visible_employee_ids(current_employee):
    if current_employee is None:
        return []

    if is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee):
        return list(Employees.objects.exclude(role__in=Employees.SERVICE_ROLES).values_list("id", flat=True))

    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        if managed_department_id:
            return list(
                Employees.objects.filter(department_id=managed_department_id).values_list("id", flat=True)
            )

    return [current_employee.id]


def restrict_requests_queryset_for_employee(queryset, current_employee):
    if current_employee is None:
        return queryset.none()

    if is_hr_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        if managed_department_id:
            return queryset.filter(
                employee__department_id=managed_department_id,
                employee__role=Employees.ROLE_EMPLOYEE,
            )
        return queryset.none()

    if is_enterprise_head_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_authorized_person_employee(current_employee):
        return queryset.filter(employee__role=Employees.ROLE_ENTERPRISE_HEAD)

    return queryset.filter(employee=current_employee)


def restrict_change_requests_queryset_for_employee(queryset, current_employee):
    if current_employee is None:
        return queryset.none()

    if is_hr_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        if managed_department_id:
            return queryset.filter(
                employee__department_id=managed_department_id,
                employee__role=Employees.ROLE_EMPLOYEE,
            )
        return queryset.none()

    if is_enterprise_head_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_authorized_person_employee(current_employee):
        return queryset.filter(employee__role=Employees.ROLE_ENTERPRISE_HEAD)

    return queryset.filter(employee=current_employee)


def restrict_urgent_closure_requests_queryset_for_employee(queryset, current_employee):
    if current_employee is None:
        return queryset.none()

    if is_hr_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        if managed_department_id:
            return queryset.filter(
                employee__department_id=managed_department_id,
                employee__role=Employees.ROLE_EMPLOYEE,
            )
        return queryset.none()

    if is_enterprise_head_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_authorized_person_employee(current_employee):
        return queryset.filter(employee__role=Employees.ROLE_ENTERPRISE_HEAD)

    return queryset.filter(employee=current_employee)


def normalize_employee_search_query(value):
    return " ".join((value or "").split())


def filter_by_employee_name(queryset, search_query):
    for token in search_query.split():
        queryset = queryset.filter(
            Q(employee__last_name__icontains=token)
            | Q(employee__first_name__icontains=token)
            | Q(employee__middle_name__icontains=token)
        )
    return queryset
