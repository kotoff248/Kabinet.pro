from apps.employees.models import Employees
from apps.employees.role_presentation import get_employee_role_card_meta


def _get_employee_department_deputy(employee):
    department = getattr(employee, "department", None)
    if department is not None and getattr(department, "deputy_id", None) == getattr(employee, "id", None):
        return department
    if department is not None:
        return None
    return getattr(employee, "deputy_department", None)


def _get_employee_production_group(employee):
    employee_position = getattr(employee, "employee_position", None)
    if not getattr(employee, "employee_position_id", None) or employee_position is None:
        return None
    return employee_position.production_group


def _get_application_employee_role_meta(employee, department_deputy=None):
    role_meta = get_employee_role_card_meta(employee).copy()
    if employee.role == Employees.ROLE_EMPLOYEE and department_deputy is not None:
        role_meta.update(
            {
                "icon": "supervisor_account",
                "icon_type": "material",
                "label": "Заместитель отдела",
                "variant": "department-deputy",
            }
        )
    elif employee.role == Employees.ROLE_EMPLOYEE and employee.is_enterprise_deputy:
        role_meta.update(
            {
                "icon": "workspace_premium",
                "icon_type": "material",
                "label": "Заместитель предприятия",
                "variant": "enterprise-deputy",
            }
        )
    return role_meta


def get_application_employee_management_badges(employee, department_deputy=None):
    badges = []
    role_meta = get_employee_role_card_meta(employee)
    if employee.role in {
        Employees.ROLE_HR,
        Employees.ROLE_DEPARTMENT_HEAD,
        Employees.ROLE_ENTERPRISE_HEAD,
    }:
        badges.append(
            {
                "label": "Руководитель отдела" if employee.role == Employees.ROLE_DEPARTMENT_HEAD else role_meta["label"],
                "icon": role_meta["icon"],
                "icon_type": role_meta["icon_type"],
                "variant": role_meta["variant"],
            }
        )
    if department_deputy is not None:
        badges.append(
            {
                "label": "Заместитель отдела",
                "icon": "supervisor_account",
                "icon_type": "material",
                "variant": "department-deputy",
            }
        )
    if employee.is_enterprise_deputy:
        badges.append(
            {
                "label": "Заместитель предприятия",
                "icon": "workspace_premium",
                "icon_type": "material",
                "variant": "enterprise-deputy",
            }
        )
    return badges


def get_application_employee_secondary_label(employee):
    if getattr(employee, "role", Employees.ROLE_EMPLOYEE) == Employees.ROLE_DEPARTMENT_HEAD:
        return employee.position or "Не указан"

    department = getattr(employee, "department", None)
    if department is not None and department.name:
        return department.name
    return "Не указан"


def get_employee_identity_presentation(employee):
    department_deputy = _get_employee_department_deputy(employee)
    production_group = _get_employee_production_group(employee)
    role_meta = _get_application_employee_role_meta(employee, department_deputy=department_deputy)
    return {
        "employee_role_icon": role_meta["icon"],
        "employee_role_icon_type": role_meta["icon_type"],
        "employee_role_variant": role_meta["variant"],
        "employee_role_label": role_meta["label"],
        "employee_secondary_label": get_application_employee_secondary_label(employee),
        "employee_position_label": employee.position or "Должность не указана",
        "employee_department_label": employee.department.name if employee.department else "Не указан",
        "employee_production_group_label": production_group.name if production_group else "Не указана",
        "employee_management_badges": get_application_employee_management_badges(
            employee,
            department_deputy=department_deputy,
        ),
    }


def enrich_application_employee_presentation(target):
    identity = get_employee_identity_presentation(target.employee)
    target.employee_role_icon = identity["employee_role_icon"]
    target.employee_role_icon_type = identity["employee_role_icon_type"]
    target.employee_role_variant = identity["employee_role_variant"]
    target.employee_role_label = identity["employee_role_label"]
    target.employee_secondary_label = identity["employee_secondary_label"]
    target.employee_position_label = identity["employee_position_label"]
    target.employee_department_label = identity["employee_department_label"]
    target.employee_production_group_label = identity["employee_production_group_label"]
    target.employee_management_badges = identity["employee_management_badges"]
    return target


def serialize_application_employee_presentation(target):
    enrich_application_employee_presentation(target)
    return {
        "employee_role_icon": target.employee_role_icon,
        "employee_role_icon_type": target.employee_role_icon_type,
        "employee_role_variant": target.employee_role_variant,
        "employee_role_label": target.employee_role_label,
        "employee_secondary_label": target.employee_secondary_label,
        "employee_position_label": target.employee_position_label,
        "employee_department_label": target.employee_department_label,
        "employee_production_group_label": target.employee_production_group_label,
        "employee_management_badges": target.employee_management_badges,
    }
