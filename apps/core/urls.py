from django.urls import path

from . import views


urlpatterns = [
    path("links/", views.project_links, name="project_links"),
    path("notifications/", views.notifications, name="notifications"),
]
