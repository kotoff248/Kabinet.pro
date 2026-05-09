from apps.accounts.services import (
    can_access_applications,
    get_current_employee,
    get_managed_department_id,
    is_authorized_person_employee,
    is_department_head_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.employees.models import Employees
from apps.leave.models import VacationRequest, VacationScheduleChangeRequest
from apps.leave.services.schedule_planning import (
    can_access_schedule_planning,
    get_schedule_planning_year,
    schedule_planning_url,
)


def pending_requests_count(request):
    current_employee = get_current_employee(request)
    planning_access = can_access_schedule_planning(current_employee)
    planning_year = get_schedule_planning_year()
    planning_context = {
        "can_access_schedule_planning": planning_access,
        "schedule_planning_sidebar_url": schedule_planning_url(planning_year) if planning_access else "",
    }
    if current_employee is None or not can_access_applications(current_employee):
        return {"pending_requests_count": 0, **planning_context}

    queryset = VacationRequest.objects.filter(status=VacationRequest.STATUS_PENDING).exclude(
        employee__role__in=Employees.SERVICE_ROLES
    )
    change_queryset = VacationScheduleChangeRequest.objects.filter(status=VacationScheduleChangeRequest.STATUS_PENDING).exclude(
        employee__role__in=Employees.SERVICE_ROLES
    )

    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        if not managed_department_id:
            return {"pending_requests_count": 0, **planning_context}
        queryset = queryset.filter(
            employee__department_id=managed_department_id,
            employee__role=Employees.ROLE_EMPLOYEE,
        )
        change_queryset = change_queryset.filter(
            employee__department_id=managed_department_id,
            employee__role=Employees.ROLE_EMPLOYEE,
        )
    elif is_enterprise_head_employee(current_employee):
        pass
    elif is_authorized_person_employee(current_employee):
        queryset = queryset.filter(employee__role=Employees.ROLE_ENTERPRISE_HEAD)
        change_queryset = change_queryset.filter(employee__role=Employees.ROLE_ENTERPRISE_HEAD)
    elif is_hr_employee(current_employee):
        pass

    return {"pending_requests_count": queryset.count() + change_queryset.count(), **planning_context}
