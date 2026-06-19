from django.contrib import messages
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string

from apps.accounts.services import employee_required, get_current_employee, get_user_context
from apps.core.models import Notification
from apps.core.services.notifications import (
    delete_notification,
    get_notification_filter_counts,
    get_notifications_for_employee,
    get_unread_notifications_count,
    is_managed_action_notification,
    mark_notification_active,
    mark_notification_done,
    mark_notification_read,
    mark_notification_unread,
    normalize_notification_filter,
)
from apps.employees.services import update_context_with_departments


PROJECT_LINKS = (
    {
        "label": "Демонстрационная страница",
        "title": "Страница Kabinet.pro",
        "description": "Веб-страница проекта для просмотра и демонстрации работы системы.",
        "url": "http://186.246.2.151/",
        "variant": "site",
    },
    {
        "label": "Репозиторий",
        "title": "GitHub-репозиторий",
        "description": "Исходный код и структура проекта Kabinet.pro.",
        "url": "https://github.com/kotoff248/Kabinet.pro",
        "variant": "github",
    },
    {
        "label": "Материалы ВКР",
        "title": "Яндекс.Диск",
        "description": "Пояснительная записка, изображения, презентация и дополнительные материалы.",
        "url": "https://disk.yandex.ru/d/bOAT3kEPEL7J8g",
        "variant": "disk",
    },
)


def project_links(request):
    return HttpResponse(render_to_string("project_links.html", {"project_links": PROJECT_LINKS}))


def _build_notifications_payload(request, current_employee, selected_filter):
    context = {
        "notifications": get_notifications_for_employee(current_employee, selected_filter),
        "notification_filter": selected_filter,
        "notification_counts": get_notification_filter_counts(current_employee),
    }
    return {
        "notifications_html": render_to_string(
            "includes/notifications/list.html",
            context,
            request=request,
        ),
        "counts": context["notification_counts"],
        "filter": selected_filter,
        "unread_count": get_unread_notifications_count(current_employee),
    }


@employee_required
def notifications(request):
    current_employee = get_current_employee(request)
    selected_filter = normalize_notification_filter(request.GET.get("filter"))
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    if request.method == "POST":
        notification = get_object_or_404(Notification, pk=request.POST.get("notification_id"), recipient=current_employee)
        action = request.POST.get("action")
        message_text = "Уведомление отмечено как прочитанное."
        if action == "delete":
            delete_notification(notification, employee=current_employee)
            message_text = "Уведомление удалено."
        elif action == "mark_done":
            if is_managed_action_notification(notification):
                message_text = "Задача завершится автоматически после согласования."
            else:
                mark_notification_done(notification, employee=current_employee)
                message_text = "Уведомление завершено."
        elif action == "mark_active":
            if is_managed_action_notification(notification):
                message_text = "Задача управляется статусом согласования."
            else:
                mark_notification_active(notification, employee=current_employee)
                message_text = "Уведомление снова требует действия."
        elif action == "mark_unread":
            mark_notification_unread(notification, employee=current_employee)
            message_text = "Уведомление снова отмечено как новое."
        else:
            mark_notification_read(notification, employee=current_employee)
        if is_ajax:
            return JsonResponse(_build_notifications_payload(request, current_employee, selected_filter))
        messages.success(request, message_text)
        return redirect(f"{request.path}?filter={selected_filter}")

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    context.update(
        {
            "notifications": get_notifications_for_employee(current_employee, selected_filter),
            "notification_filter": selected_filter,
            "notification_counts": get_notification_filter_counts(current_employee),
        }
    )
    if is_ajax:
        return JsonResponse(_build_notifications_payload(request, current_employee, selected_filter))
    return render(request, "notifications.html", context)
