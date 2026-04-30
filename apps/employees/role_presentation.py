from apps.employees.models import Employees


ROLE_CARD_META = {
    Employees.ROLE_EMPLOYEE: {
        "icon": "person",
        "icon_type": "material",
        "label": "Сотрудник",
        "variant": "employee",
    },
    Employees.ROLE_HR: {
        "icon": "manage_accounts",
        "icon_type": "material",
        "label": "HR",
        "variant": "hr",
    },
    Employees.ROLE_DEPARTMENT_HEAD: {
        "icon": "admin_panel_settings",
        "icon_type": "material",
        "label": "Руководитель отдела",
        "variant": "department-head",
    },
    Employees.ROLE_ENTERPRISE_HEAD: {
        "icon": "♛",
        "icon_type": "symbol",
        "label": "Руководитель предприятия",
        "variant": "enterprise-head",
    },
    Employees.ROLE_AUTHORIZED_PERSON: {
        "icon": "verified_user",
        "icon_type": "material",
        "label": "Уполномоченное лицо",
        "variant": "authorized-person",
    },
}


def get_employee_role_card_meta(employee):
    role = getattr(employee, "role", Employees.ROLE_EMPLOYEE)
    return ROLE_CARD_META.get(role, ROLE_CARD_META[Employees.ROLE_EMPLOYEE])
