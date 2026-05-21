from django.contrib import messages
from django.shortcuts import (
    redirect,
    render,
)

from apps.accounts.services import (
    can_access_analytics,
    employee_required,
    get_current_employee,
    get_user_context,
)
from apps.employees.services import update_context_with_departments
from apps.leave.services.page_contexts import build_analytics_page_context


@employee_required
def analytics(request):
    current_employee = get_current_employee(request)
    if not can_access_analytics(current_employee):
        messages.error(request, "Раздел аналитики доступен только HR и руководителям.")
        return redirect("main")

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    context.update(build_analytics_page_context(current_employee, request.GET))
    return render(request, "analytics.html", context)
