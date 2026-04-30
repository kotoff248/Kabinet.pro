from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command, CommandError
from django.test import TestCase
from django.utils import timezone

from apps.employees.models import Departments, Employees
from apps.leave.models import DepartmentStaffingRule, DepartmentWorkload, VacationSchedule


class SeedEnterpriseCommandTests(TestCase):
    def test_command_requires_explicit_reset_confirmation(self):
        stale_department = Departments.objects.create(name="Old department")
        Employees.objects.create(
            login="stale_user",
            last_name="Old",
            first_name="Employee",
            middle_name="Test",
            position="Tester",
            department=stale_department,
            role=Employees.ROLE_EMPLOYEE,
        )

        with self.assertRaisesMessage(CommandError, "--confirm-reset"):
            call_command("seed_vacation_requests", seed_value=7, stdout=StringIO())

        self.assertTrue(Departments.objects.filter(name="Old department").exists())
        self.assertTrue(Employees.objects.filter(login="stale_user").exists())

    def test_command_rebuilds_enterprise_structure_and_credentials(self):
        stale_department = Departments.objects.create(name="Старый отдел")
        Employees.objects.create(
            login="stale_user",
            last_name="Старый",
            first_name="Сотрудник",
            middle_name="Тестович",
            position="Тестировщик",
            department=stale_department,
            role=Employees.ROLE_EMPLOYEE,
        )

        stdout = StringIO()
        call_command("seed_vacation_requests", seed_value=7, confirm_reset=True, stdout=stdout)

        self.assertEqual(Departments.objects.count(), 5)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_EMPLOYEE).count(), 100)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_HR).count(), 2)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_DEPARTMENT_HEAD).count(), 5)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_ENTERPRISE_HEAD).count(), 1)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_AUTHORIZED_PERSON).count(), 1)
        self.assertFalse(Employees.objects.filter(login="stale_user").exists())

        self.assertTrue(Employees.objects.filter(login="director_1", role=Employees.ROLE_ENTERPRISE_HEAD).exists())
        self.assertTrue(Employees.objects.filter(login="admin_1", role=Employees.ROLE_AUTHORIZED_PERSON).exists())
        self.assertTrue(Employees.objects.filter(login="hr_1", role=Employees.ROLE_HR).exists())
        self.assertTrue(Employees.objects.filter(login="hr_2", role=Employees.ROLE_HR).exists())
        self.assertTrue(Employees.objects.filter(login="manager_5", role=Employees.ROLE_DEPARTMENT_HEAD).exists())
        self.assertTrue(Employees.objects.filter(login="employ_100", role=Employees.ROLE_EMPLOYEE).exists())

        expected_department_counts = {
            "Производство": 30,
            "Техническое обслуживание": 24,
            "Промышленная безопасность": 12,
            "Логистика": 18,
            "Финансы и закупки": 16,
        }
        expected_rules = {
            "Производство": (23, 8, 5, "production-core"),
            "Техническое обслуживание": (18, 7, 5, "maintenance-critical"),
            "Промышленная безопасность": (10, 3, 5, "safety-control"),
            "Логистика": (14, 5, 4, "logistics-shifts"),
            "Финансы и закупки": (12, 5, 4, "finance-procurement"),
        }

        authorized_person = Employees.objects.get(login="admin_1")
        self.assertEqual(authorized_person.full_name, "")
        self.assertEqual(authorized_person.position, "")
        self.assertIsNone(authorized_person.department)
        self.assertEqual(authorized_person.user.first_name, "")
        self.assertTrue(authorized_person.user.check_password("1234"))
        self.assertTrue(Employees.objects.get(login="hr_1").user.check_password("1234"))
        self.assertTrue(Employees.objects.get(login="manager_1").user.check_password("1234"))
        self.assertTrue(Employees.objects.get(login="employ_1").user.check_password("1234"))
        current_year = timezone.localdate().year
        enterprise_start_year = current_year - 5
        expected_schedule_years = list(range(current_year - 5, current_year + 1))
        expected_department_formation_dates = {
            "Производство": date(enterprise_start_year, 1, 11),
            "Техническое обслуживание": date(enterprise_start_year, 2, 8),
            "Промышленная безопасность": date(enterprise_start_year, 3, 15),
            "Логистика": date(enterprise_start_year, 4, 12),
            "Финансы и закупки": date(enterprise_start_year, 5, 17),
        }
        self.assertEqual(
            list(VacationSchedule.objects.order_by("year").values_list("year", flat=True)),
            expected_schedule_years,
        )
        self.assertFalse(VacationSchedule.objects.filter(year__lt=current_year - 5).exists())
        self.assertEqual(VacationSchedule.objects.get(year=current_year).status, VacationSchedule.STATUS_APPROVED)
        self.assertFalse(VacationSchedule.objects.filter(year__lt=current_year).exclude(status=VacationSchedule.STATUS_ARCHIVED).exists())
        self.assertEqual(DepartmentStaffingRule.objects.count(), 5)
        self.assertEqual(DepartmentWorkload.objects.count(), 5 * len(expected_schedule_years) * 12)

        employee_last_names = list(Employees.objects.filter(role=Employees.ROLE_EMPLOYEE).values_list("last_name", flat=True))
        self.assertGreaterEqual(len(set(employee_last_names)), 90)
        most_common_last_name_count = max(employee_last_names.count(last_name) for last_name in set(employee_last_names))
        self.assertLessEqual(most_common_last_name_count, 2)
        self.assertEqual(Employees.objects.get(login="director_1").date_joined, date(enterprise_start_year, 1, 4))

        for department in Departments.objects.all():
            with self.subTest(department=department.name):
                self.assertIsNotNone(department.head)
                formation_date = expected_department_formation_dates[department.name]
                self.assertEqual(timezone.localtime(department.date_added).date(), formation_date)
                self.assertEqual(department.head.date_joined, formation_date)
                self.assertFalse(
                    department.employees.exclude(pk=department.head_id).filter(date_joined__lt=formation_date + timedelta(days=1)).exists()
                )
                self.assertEqual(
                    department.employees.filter(role=Employees.ROLE_EMPLOYEE).count(),
                    expected_department_counts[department.name],
                )
                rule = department.staffing_rule
                self.assertEqual(
                    (
                        rule.min_staff_required,
                        rule.max_absent,
                        rule.criticality_level,
                        rule.substitution_group,
                    ),
                    expected_rules[department.name],
                )
                december_workload = DepartmentWorkload.objects.get(
                    department=department,
                    year=current_year,
                    month=12,
                )
                self.assertEqual(december_workload.min_staff_required, rule.min_staff_required)
                self.assertEqual(december_workload.max_absent, rule.max_absent)

        for workload in DepartmentWorkload.objects.select_related("department", "department__staffing_rule"):
            if workload.month == 12:
                month_end = date(workload.year, 12, 31)
            else:
                month_end = date(workload.year, workload.month + 1, 1) - timedelta(days=1)
            active_count = Employees.objects.filter(
                department=workload.department,
                is_active_employee=True,
                date_joined__lte=month_end,
            ).exclude(role__in=Employees.SERVICE_ROLES).count()

            with self.subTest(department=workload.department.name, year=workload.year, month=workload.month):
                self.assertGreaterEqual(workload.min_staff_required, 1)
                self.assertGreaterEqual(workload.max_absent, 1)
                self.assertLessEqual(workload.min_staff_required, workload.department.staffing_rule.min_staff_required)
                self.assertLessEqual(workload.max_absent, workload.department.staffing_rule.max_absent)
                if active_count:
                    self.assertLessEqual(workload.min_staff_required, active_count)
