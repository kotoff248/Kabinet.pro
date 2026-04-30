from apps.employees.models import Employees
from apps.employees.role_presentation import get_employee_role_card_meta


def get_application_employee_secondary_label(employee):
    if getattr(employee, "role", Employees.ROLE_EMPLOYEE) == Employees.ROLE_DEPARTMENT_HEAD:
        return employee.position or "Не указан"

    department = getattr(employee, "department", None)
    if department is not None and department.name:
        return department.name
    return "Не указан"


def enrich_application_employee_presentation(target):
    employee = target.employee
    role_meta = get_employee_role_card_meta(employee)
    target.employee_role_icon = role_meta["icon"]
    target.employee_role_icon_type = role_meta["icon_type"]
    target.employee_role_variant = role_meta["variant"]
    target.employee_role_label = role_meta["label"]
    target.employee_secondary_label = get_application_employee_secondary_label(employee)
    return target


def serialize_application_employee_presentation(target):
    enrich_application_employee_presentation(target)
    return {
        "employee_role_icon": target.employee_role_icon,
        "employee_role_icon_type": target.employee_role_icon_type,
        "employee_role_variant": target.employee_role_variant,
        "employee_role_label": target.employee_role_label,
        "employee_secondary_label": target.employee_secondary_label,
    }
