from django.urls import path

from . import views


urlpatterns = [
    path("main/", views.main, name="main"),
    path("employees/", views.employees, name="employees"),
    path("employee/<int:employee_id>/", views.employee_profile, name="employee_profile"),
    path("employee/<int:employee_id>/update/", views.update_employee, name="update_employee"),
    path("employee/<int:employee_id>/delete/", views.delete_employee, name="delete_employee"),
    path("departments/", views.departments, name="departments"),
    path("departments/<int:department_id>/", views.department_detail, name="department_detail"),
    path("staffing/", views.staffing_rules, name="staffing_rules"),
    path("staffing/demo-data/reset/", views.reset_demo_data, name="reset_demo_data"),
    path(
        "staffing/demo-data/reset/<int:job_id>/status/",
        views.reset_demo_data_status,
        name="reset_demo_data_status",
    ),
    path("staffing/demo-data/restore-initial/", views.restore_demo_initial_state, name="restore_demo_initial_state"),
]


