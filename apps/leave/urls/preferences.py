from django.urls import path

from apps.leave.views import preferences


urlpatterns = [
    path(
        "calendar/preferences/start/",
        preferences.start_vacation_preferences_collection,
        name="preferences_collection_start",
    ),
    path(
        "calendar/preferences/<int:year>/finish/",
        preferences.finish_vacation_preferences_collection,
        name="preferences_collection_finish",
    ),
    path(
        "preferences/<int:year>/readiness/",
        preferences.preference_collection_readiness,
        name="preference_collection_readiness",
    ),
    path(
        "preferences/<int:year>/ai-preview/",
        preferences.vacation_preferences_ai_preview,
        name="vacation_preferences_ai_preview",
    ),
    path("preferences/<int:year>/", preferences.vacation_preferences, name="vacation_preferences"),
]
