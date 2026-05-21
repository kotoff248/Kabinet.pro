from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.shortcuts import redirect

from apps.employees.models import Departments, Employees
from apps.employees.role_presentation import get_employee_role_card_meta


MANAGERS_GROUP_NAME = "Managers"
HR_GROUP_NAME = "HR"
DEPARTMENT_HEADS_GROUP_NAME = "DepartmentHeads"
ENTERPRISE_HEADS_GROUP_NAME = "EnterpriseHeads"
AUTHORIZED_PERSONS_GROUP_NAME = "AuthorizedPersons"
MANAGEMENT_GROUP_NAMES = (
    HR_GROUP_NAME,
    DEPARTMENT_HEADS_GROUP_NAME,
    ENTERPRISE_HEADS_GROUP_NAME,
    AUTHORIZED_PERSONS_GROUP_NAME,
)
EMPLOYEE_CONTEXT_SELECT_RELATED = (
    "department",
    "managed_department",
    "deputy_department",
    "employee_position",
    "employee_position__production_group",
)
ROLE_LABELS = {
    Employees.ROLE_EMPLOYEE: "сотрудник",
    Employees.ROLE_HR: "HR",
    Employees.ROLE_DEPARTMENT_HEAD: "руководитель отдела",
    Employees.ROLE_ENTERPRISE_HEAD: "руководитель предприятия",
    Employees.ROLE_AUTHORIZED_PERSON: "уполномоченное лицо",
}


def normalize_employee_login(value):
    return (value or "").strip()


def default_employee_login(employee_id):
    return f"employee_{employee_id}"


def get_role_group_name(role):
    return {
        Employees.ROLE_HR: HR_GROUP_NAME,
        Employees.ROLE_DEPARTMENT_HEAD: DEPARTMENT_HEADS_GROUP_NAME,
        Employees.ROLE_ENTERPRISE_HEAD: ENTERPRISE_HEADS_GROUP_NAME,
        Employees.ROLE_AUTHORIZED_PERSON: AUTHORIZED_PERSONS_GROUP_NAME,
    }.get(role)


def get_or_create_group(name):
    group, _ = Group.objects.get_or_create(name=name)
    return group


def get_employee_for_user(user):
    if not getattr(user, "is_authenticated", False):
        return None

    employee = getattr(user, "employee_profile", None)
    if employee is None:
        employee = Employees.objects.select_related(*EMPLOYEE_CONTEXT_SELECT_RELATED).filter(user=user).first()
    return employee


def get_current_employee(request):
    if hasattr(request, "_kabinet_current_employee"):
        return request._kabinet_current_employee

    employee = get_employee_for_user(request.user)
    request._kabinet_current_employee = employee
    return employee


def is_hr_employee(employee):
    return employee is not None and employee.role == Employees.ROLE_HR


def is_department_head_employee(employee):
    return employee is not None and employee.role == Employees.ROLE_DEPARTMENT_HEAD


def is_enterprise_head_employee(employee):
    return employee is not None and employee.role == Employees.ROLE_ENTERPRISE_HEAD


def is_authorized_person_employee(employee):
    return employee is not None and employee.role == Employees.ROLE_AUTHORIZED_PERSON


def is_management_employee(employee):
    return employee is not None and employee.role in Employees.MANAGEMENT_ROLES


def can_use_management_login(employee):
    return is_management_employee(employee) or is_authorized_person_employee(employee)


def is_hr_user(user):
    return is_hr_employee(get_employee_for_user(user))


def is_department_head_user(user):
    return is_department_head_employee(get_employee_for_user(user))


def is_enterprise_head_user(user):
    return is_enterprise_head_employee(get_employee_for_user(user))


def is_management_user(user):
    employee = get_employee_for_user(user)
    return can_use_management_login(employee)


def is_manager_user(user):
    return is_management_user(user)


def get_managed_department_id(employee):
    if not is_department_head_employee(employee):
        return None
    managed_department = getattr(employee, "managed_department", None)
    if managed_department is not None:
        return managed_department.id
    return employee.department_id


def can_edit_employee_data(employee):
    return is_hr_employee(employee)


def can_delete_employee(actor, target):
    if actor is None or target is None:
        return False
    if not is_hr_employee(actor):
        return False
    if actor.id == target.id:
        return False
    if not getattr(target, "is_active_employee", True):
        return False
    if target.role in {Employees.ROLE_ENTERPRISE_HEAD, Employees.ROLE_AUTHORIZED_PERSON}:
        return False
    return True


def can_access_applications(employee):
    return (
        is_hr_employee(employee)
        or is_department_head_employee(employee)
        or is_enterprise_head_employee(employee)
        or is_authorized_person_employee(employee)
    )


def can_view_department(viewer, department):
    if viewer is None or department is None:
        return False
    if is_hr_employee(viewer) or is_enterprise_head_employee(viewer):
        return True
    if is_department_head_employee(viewer):
        return get_managed_department_id(viewer) == department.id
    return viewer.department_id == department.id


def can_view_employee(viewer, target):
    if viewer is None or target is None:
        return False
    if target.role in Employees.SERVICE_ROLES:
        return viewer.id == target.id
    if viewer.id == target.id:
        return True
    if is_hr_employee(viewer) or is_enterprise_head_employee(viewer):
        return True
    if is_authorized_person_employee(viewer):
        return target.role == Employees.ROLE_ENTERPRISE_HEAD
    if is_department_head_employee(viewer):
        return target.department_id is not None and target.department_id == get_managed_department_id(viewer)
    return False


def can_approve_leave_for_employee(viewer, target):
    if viewer is None or target is None or viewer.id == target.id:
        return False

    if is_department_head_employee(viewer):
        managed_department_id = get_managed_department_id(viewer)
        return (
            managed_department_id is not None
            and target.role == Employees.ROLE_EMPLOYEE
            and target.department_id == managed_department_id
        )

    if is_enterprise_head_employee(viewer):
        return target.role in {Employees.ROLE_DEPARTMENT_HEAD, Employees.ROLE_HR}

    if is_authorized_person_employee(viewer):
        return target.role == Employees.ROLE_ENTERPRISE_HEAD

    return False


def can_initiate_schedule_change_for_employee(actor, target):
    if actor is None or target is None:
        return False
    if not getattr(actor, "is_active_employee", True) or not getattr(target, "is_active_employee", True):
        return False
    if target.role in Employees.SERVICE_ROLES:
        return False
    if actor.id == target.id:
        return actor.role not in Employees.SERVICE_ROLES
    if actor.role in Employees.SERVICE_ROLES:
        return False
    return can_approve_leave_for_employee(actor, target)


def can_initiate_schedule_change_for_item(actor, schedule_item):
    target = getattr(schedule_item, "employee", None)
    if target is None:
        return False
    return can_initiate_schedule_change_for_employee(actor, target)


def can_review_schedule_change_request(actor, change_request):
    if actor is None or change_request is None:
        return False
    requested_by_id = getattr(change_request, "requested_by_id", None)
    employee_id = getattr(change_request, "employee_id", None)
    if requested_by_id and requested_by_id != employee_id:
        return actor.id == employee_id and actor.id != requested_by_id
    return can_approve_leave_for_employee(actor, getattr(change_request, "employee", None))


def can_access_departments_page(employee):
    return is_hr_employee(employee) or is_enterprise_head_employee(employee) or is_department_head_employee(employee)


def can_access_staffing_page(employee):
    return is_hr_employee(employee) or is_enterprise_head_employee(employee) or is_department_head_employee(employee)


def can_edit_staffing_rules(employee):
    return is_hr_employee(employee) or is_enterprise_head_employee(employee)


def can_access_analytics(employee):
    return is_hr_employee(employee) or is_department_head_employee(employee) or is_enterprise_head_employee(employee)


def get_accessible_departments(employee):
    queryset = Departments.objects.select_related("head").order_by("name")
    if employee is None:
        return queryset.none()
    if is_hr_employee(employee) or is_enterprise_head_employee(employee):
        return queryset
    if is_department_head_employee(employee):
        managed_department_id = get_managed_department_id(employee)
        return queryset.filter(id=managed_department_id) if managed_department_id else queryset.none()
    if employee.department_id:
        return queryset.filter(id=employee.department_id)
    return queryset.none()


def sync_department_head_assignment(employee):
    Departments.objects.filter(head=employee).exclude(id=employee.department_id).update(head=None)
    if employee.role == Employees.ROLE_DEPARTMENT_HEAD and employee.department_id:
        Departments.objects.filter(id=employee.department_id).update(head=employee)
    else:
        Departments.objects.filter(head=employee).update(head=None)


def sync_employee_user(employee, raw_password=None):
    username = normalize_employee_login(employee.login)
    if not username:
        if employee.pk is None:
            raise ValueError("Employee must be saved before syncing user data.")
        username = default_employee_login(employee.pk)

    User = get_user_model()
    existing_user = employee.user
    if existing_user is None:
        existing_user = User.objects.filter(username=username).first()
        if existing_user is None:
            existing_user = User(username=username, is_active=True)
        employee.user = existing_user

    existing_user.username = username
    existing_user.first_name = employee.full_name[:150]
    existing_user.last_name = ""
    existing_user.is_active = True
    existing_user.is_staff = can_use_management_login(employee)

    if raw_password is not None and raw_password != "":
        existing_user.set_password(raw_password)
    elif not existing_user.password:
        existing_user.set_unusable_password()

    existing_user.save()

    legacy_manager_group = get_or_create_group(MANAGERS_GROUP_NAME)
    role_groups = {name: get_or_create_group(name) for name in MANAGEMENT_GROUP_NAMES}
    existing_user.groups.remove(legacy_manager_group, *role_groups.values())

    role_group_name = get_role_group_name(employee.role)
    if role_group_name is not None:
        existing_user.groups.add(role_groups[role_group_name], legacy_manager_group)

    updates = []
    if employee.user_id != existing_user.id:
        employee.user = existing_user
        updates.append("user")
    if employee.login != username:
        employee.login = username
        updates.append("login")
    if employee.is_manager != is_management_employee(employee):
        employee.is_manager = is_management_employee(employee)
        updates.append("is_manager")
    if updates:
        employee.save(update_fields=updates)

    sync_department_head_assignment(employee)
    return existing_user


def get_user_context(request):
    employee = get_current_employee(request)
    is_authorized_person = is_authorized_person_employee(employee)
    role_meta = get_employee_role_card_meta(employee)
    if employee is not None:
        if is_authorized_person:
            employee_name = "Уполномоченное лицо"
            last_name = "Служебный"
            initials = "УЛ"
            role = "Уполномоченное лицо"
        else:
            employee_name = employee.full_name
            last_name = employee.last_name
            initials = "".join(f"{part[0].upper()}." for part in [employee.first_name, employee.middle_name] if part)
            role = ROLE_LABELS.get(employee.role, "сотрудник")
    else:
        employee_name = request.user.get_username()
        name_parts = employee_name.split()
        last_name = name_parts[0] if name_parts else ""
        initials = "".join(f"{name[0].upper()}." for name in name_parts[1:])
        role = "сотрудник"

    managed_department_id = get_managed_department_id(employee)
    return {
        "employee_name": employee_name,
        "last_name": last_name,
        "initials": initials,
        "role": role,
        "is_manager": is_management_employee(employee),
        "is_management": is_management_employee(employee),
        "is_hr": is_hr_employee(employee),
        "is_department_head": is_department_head_employee(employee),
        "is_enterprise_head": is_enterprise_head_employee(employee),
        "is_authorized_person": is_authorized_person,
        "can_access_applications": can_access_applications(employee),
        "can_access_analytics": can_access_analytics(employee),
        "can_access_notifications": employee is not None,
        "can_access_calendar": not is_authorized_person,
        "can_access_employees": not is_authorized_person,
        "can_access_staffing": can_access_staffing_page(employee),
        "can_access_profile": not is_authorized_person,
        "session_card_name": f"{last_name} {initials}".strip() if not is_authorized_person else "Служебный доступ",
        "session_card_hint": "" if not is_authorized_person else "Согласование отпуска руководителя предприятия",
        "session_role_icon": role_meta["icon"],
        "session_role_icon_type": role_meta["icon_type"],
        "session_role_label": role_meta["label"],
        "session_role_variant": role_meta["variant"],
        "managed_department_id": managed_department_id,
    }


def management_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if is_management_user(request.user):
            return view_func(request, *args, **kwargs)

        messages.error(request, "У вас нет прав для доступа к этой странице.")
        return redirect("main")

    return _wrapped_view


def manager_required(view_func):
    return management_required(view_func)


def employee_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        employee = get_current_employee(request)
        if request.user.is_authenticated and employee is not None:
            return view_func(request, *args, **kwargs)

        return redirect("login")

    return _wrapped_view
