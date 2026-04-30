import random
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import sync_employee_user
from apps.employees.models import Departments, Employees
from apps.leave.models import (
    DepartmentStaffingRule,
    DepartmentWorkload,
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationPreference,
    VacationRequest,
    VacationRequestHistory,
    VacationSchedule,
    VacationScheduleAuthorizedApproval,
    VacationScheduleChangeRequest,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleItem,
)
from apps.leave.services.dates import add_months_safe, add_years_safe, get_chargeable_leave_days
from apps.leave.services.ledger import get_employee_requestable_leave, rebuild_employee_leave_ledger
from apps.leave.services.metrics import set_vacation_metric_sync_enabled
from apps.leave.services.querysets import exclude_converted_paid_requests
from apps.leave.services.approval_routes import get_expected_vacation_approver
from apps.leave.services.request_history import (
    get_vacation_submitted_at,
    rebuild_vacation_request_history,
    record_vacation_request_created,
    record_vacation_request_reviewed,
)
from apps.leave.services.risk import calculate_vacation_request_risk
from apps.leave.services.schedule_changes import create_schedule_change_request
from apps.leave.services.schedule_items import create_schedule_item_from_paid_vacation_request


DEPARTMENT_SPECS = [
    {
        "name": "Производство",
        "formation_month": 1,
        "formation_day": 11,
        "formation_hour": 9,
        "formation_minute": 0,
        "employee_count": 30,
        "recent_hires": 3,
        "head_position": "Начальник производства",
        "staffing_rule": {
            "min_staff_required": 23,
            "max_absent": 8,
            "criticality_level": 5,
            "substitution_group": "production-core",
        },
        "workload_profile": [5, 5, 4, 3, 3, 4, 4, 4, 3, 4, 5, 5],
        "employee_positions": [
            "Горный мастер",
            "Ведущий инженер-технолог",
            "Машинист технологического оборудования",
            "Оператор производственной линии",
            "Инженер по качеству",
        ],
    },
    {
        "name": "Техническое обслуживание",
        "formation_month": 2,
        "formation_day": 8,
        "formation_hour": 9,
        "formation_minute": 30,
        "employee_count": 24,
        "recent_hires": 2,
        "head_position": "Руководитель службы ТОиР",
        "staffing_rule": {
            "min_staff_required": 18,
            "max_absent": 7,
            "criticality_level": 5,
            "substitution_group": "maintenance-critical",
        },
        "workload_profile": [5, 5, 5, 4, 3, 3, 3, 3, 4, 4, 5, 5],
        "employee_positions": [
            "Инженер по ремонту оборудования",
            "Механик участка",
            "Электромонтер",
            "Слесарь по ремонту оборудования",
            "Инженер-диагност",
        ],
    },
    {
        "name": "Промышленная безопасность",
        "formation_month": 3,
        "formation_day": 15,
        "formation_hour": 10,
        "formation_minute": 0,
        "employee_count": 12,
        "recent_hires": 1,
        "head_position": "Начальник службы промышленной безопасности",
        "staffing_rule": {
            "min_staff_required": 10,
            "max_absent": 3,
            "criticality_level": 5,
            "substitution_group": "safety-control",
        },
        "workload_profile": [4, 4, 4, 5, 5, 4, 3, 3, 5, 4, 4, 4],
        "employee_positions": [
            "Инженер по охране труда",
            "Специалист по промышленной безопасности",
            "Инспектор по технике безопасности",
            "Инженер-эколог",
            "Ведущий специалист по рискам",
        ],
    },
    {
        "name": "Логистика",
        "formation_month": 4,
        "formation_day": 12,
        "formation_hour": 9,
        "formation_minute": 15,
        "employee_count": 18,
        "recent_hires": 1,
        "head_position": "Руководитель логистики",
        "staffing_rule": {
            "min_staff_required": 14,
            "max_absent": 5,
            "criticality_level": 4,
            "substitution_group": "logistics-shifts",
        },
        "workload_profile": [3, 3, 4, 4, 4, 5, 5, 5, 5, 4, 3, 4],
        "employee_positions": [
            "Специалист по логистике",
            "Диспетчер транспортного участка",
            "Координатор поставок",
            "Инженер по складской логистике",
            "Аналитик цепочки поставок",
        ],
    },
    {
        "name": "Финансы и закупки",
        "formation_month": 5,
        "formation_day": 17,
        "formation_hour": 10,
        "formation_minute": 30,
        "employee_count": 16,
        "recent_hires": 1,
        "head_position": "Руководитель финансов и закупок",
        "staffing_rule": {
            "min_staff_required": 12,
            "max_absent": 5,
            "criticality_level": 4,
            "substitution_group": "finance-procurement",
        },
        "workload_profile": [4, 3, 5, 4, 3, 5, 2, 2, 3, 4, 4, 5],
        "employee_positions": [
            "Финансовый аналитик",
            "Экономист",
            "Специалист по закупкам",
            "Ведущий бухгалтер",
            "Контрактный менеджер",
        ],
    },
]

MALE_FIRST_NAMES = [
    "Алексей",
    "Дмитрий",
    "Иван",
    "Павел",
    "Егор",
    "Николай",
    "Сергей",
    "Максим",
    "Андрей",
    "Константин",
    "Олег",
    "Роман",
    "Виктор",
    "Артем",
    "Георгий",
]
FEMALE_FIRST_NAMES = [
    "Анна",
    "Мария",
    "Елена",
    "Ирина",
    "Ольга",
    "Наталья",
    "Светлана",
    "Татьяна",
    "Дарья",
    "Юлия",
    "Евгения",
    "Виктория",
]
MALE_LAST_NAMES = [
    "Иванов",
    "Петров",
    "Сидоров",
    "Федоров",
    "Кузнецов",
    "Смирнов",
    "Попов",
    "Соколов",
    "Лебедев",
    "Козлов",
    "Новиков",
    "Морозов",
    "Павлов",
    "Семенов",
    "Громов",
]
FEMALE_LAST_NAMES = [
    "Иванова",
    "Петрова",
    "Сидорова",
    "Федорова",
    "Кузнецова",
    "Смирнова",
    "Попова",
    "Соколова",
    "Лебедева",
    "Козлова",
    "Новикова",
    "Морозова",
    "Павлова",
    "Семенова",
    "Громова",
]
MALE_MIDDLE_NAMES = [
    "Александрович",
    "Дмитриевич",
    "Иванович",
    "Павлович",
    "Егорович",
    "Николаевич",
    "Сергеевич",
    "Максимович",
    "Андреевич",
    "Константинович",
    "Олегович",
    "Романович",
]
FEMALE_MIDDLE_NAMES = [
    "Александровна",
    "Дмитриевна",
    "Ивановна",
    "Павловна",
    "Егоровна",
    "Николаевна",
    "Сергеевна",
    "Максимовна",
    "Андреевна",
    "Константиновна",
    "Олеговна",
    "Романовна",
]
SURNAME_PAIRS = [
    ("Абрамов", "Абрамова"),
    ("Агеев", "Агеева"),
    ("Акимов", "Акимова"),
    ("Александров", "Александрова"),
    ("Андреев", "Андреева"),
    ("Антонов", "Антонова"),
    ("Артамонов", "Артамонова"),
    ("Афанасьев", "Афанасьева"),
    ("Баранов", "Баранова"),
    ("Белов", "Белова"),
    ("Белозеров", "Белозерова"),
    ("Беляев", "Беляева"),
    ("Блинов", "Блинова"),
    ("Богданов", "Богданова"),
    ("Большаков", "Большакова"),
    ("Борисов", "Борисова"),
    ("Васильев", "Васильева"),
    ("Виноградов", "Виноградова"),
    ("Власов", "Власова"),
    ("Волков", "Волкова"),
    ("Воробьев", "Воробьева"),
    ("Гаврилов", "Гаврилова"),
    ("Герасимов", "Герасимова"),
    ("Голубев", "Голубева"),
    ("Гончаров", "Гончарова"),
    ("Горбунов", "Горбунова"),
    ("Гордеев", "Гордеева"),
    ("Григорьев", "Григорьева"),
    ("Громов", "Громова"),
    ("Давыдов", "Давыдова"),
    ("Данилов", "Данилова"),
    ("Дементьев", "Дементьева"),
    ("Денисов", "Денисова"),
    ("Дмитриев", "Дмитриева"),
    ("Дорофеев", "Дорофеева"),
    ("Егоров", "Егорова"),
    ("Елисеев", "Елисеева"),
    ("Емельянов", "Емельянова"),
    ("Ершов", "Ершова"),
    ("Ефимов", "Ефимова"),
    ("Жданов", "Жданова"),
    ("Жуков", "Жукова"),
    ("Зайцев", "Зайцева"),
    ("Захаров", "Захарова"),
    ("Зимин", "Зимина"),
    ("Зиновьев", "Зиновьева"),
    ("Иванов", "Иванова"),
    ("Игнатьев", "Игнатьева"),
    ("Ильин", "Ильина"),
    ("Калинин", "Калинина"),
    ("Карпов", "Карпова"),
    ("Кириллов", "Кириллова"),
    ("Киселев", "Киселева"),
    ("Князев", "Князева"),
    ("Ковалев", "Ковалева"),
    ("Колесников", "Колесникова"),
    ("Комаров", "Комарова"),
    ("Кондратьев", "Кондратьева"),
    ("Корнилов", "Корнилова"),
    ("Коротков", "Короткова"),
    ("Крылов", "Крылова"),
    ("Кудрявцев", "Кудрявцева"),
    ("Кузнецов", "Кузнецова"),
    ("Лапин", "Лапина"),
    ("Ларионов", "Ларионова"),
    ("Лебедев", "Лебедева"),
    ("Логинов", "Логинова"),
    ("Макаров", "Макарова"),
    ("Мартынов", "Мартынова"),
    ("Медведев", "Медведева"),
    ("Мельников", "Мельникова"),
    ("Миронов", "Миронова"),
    ("Михайлов", "Михайлова"),
    ("Морозов", "Морозова"),
    ("Назаров", "Назарова"),
    ("Нестеров", "Нестерова"),
    ("Никитин", "Никитина"),
    ("Новиков", "Новикова"),
    ("Орлов", "Орлова"),
    ("Осипов", "Осипова"),
    ("Павлов", "Павлова"),
    ("Панфилов", "Панфилова"),
    ("Петров", "Петрова"),
    ("Поляков", "Полякова"),
    ("Попов", "Попова"),
    ("Прохоров", "Прохорова"),
    ("Родионов", "Родионова"),
    ("Романов", "Романова"),
    ("Румянцев", "Румянцева"),
    ("Савельев", "Савельева"),
    ("Сафонов", "Сафонова"),
    ("Селезнев", "Селезнева"),
    ("Семенов", "Семенова"),
    ("Сергеев", "Сергеева"),
    ("Сидоров", "Сидорова"),
    ("Смирнов", "Смирнова"),
    ("Соболев", "Соболева"),
    ("Соколов", "Соколова"),
    ("Соловьев", "Соловьева"),
    ("Степанов", "Степанова"),
    ("Суворов", "Суворова"),
    ("Тарасов", "Тарасова"),
    ("Терентьев", "Терентьева"),
    ("Тимофеев", "Тимофеева"),
    ("Титов", "Титова"),
    ("Фадеев", "Фадеева"),
    ("Федоров", "Федорова"),
    ("Филатов", "Филатова"),
    ("Фомин", "Фомина"),
    ("Фролов", "Фролова"),
    ("Харитонов", "Харитонова"),
    ("Цветков", "Цветкова"),
    ("Чернов", "Чернова"),
    ("Шаповалов", "Шаповалова"),
    ("Шестаков", "Шестакова"),
    ("Щербаков", "Щербакова"),
    ("Юдин", "Юдина"),
    ("Яковлев", "Яковлева"),
]

DEFAULT_PASSWORD = "1234"
TOTAL_EMPLOYEE_COUNT = 100
HR_COUNT = 2
ENTERPRISE_HEAD_COUNT = 1
ACTIVE_STATUSES = {VacationRequest.STATUS_APPROVED, VacationRequest.STATUS_PENDING}
DEFAULT_SCHEDULE_HISTORY_YEARS = 5
FAST_SCHEDULE_HISTORY_YEARS = 2
FAST_EMPLOYEE_COUNTS = [8, 6, 4, 4, 3]
MAX_REALISTIC_AVAILABLE_DAYS = 104
PAID_OPERATIONAL_GAP_RANGE = (14, 30)
SPECIAL_REQUEST_TARGET_RANGE = (18, 28)
SPECIAL_REQUEST_REJECTION_SHARE_RANGE = (0.10, 0.18)
SPECIAL_REQUEST_TYPES = ("unpaid", "study")


class NameFactory:
    def __init__(self, rng):
        self.rng = rng
        self._surname_pairs = list(SURNAME_PAIRS)
        self.rng.shuffle(self._surname_pairs)
        self._counters = {"male": 0, "female": 0}
        self._global_counter = 0

    def next_name(self, gender):
        counter = self._counters[gender]
        self._counters[gender] += 1
        global_counter = self._global_counter
        self._global_counter += 1

        if gender == "female":
            first_names = FEMALE_FIRST_NAMES
            middle_names = FEMALE_MIDDLE_NAMES
            last_name = self._surname_pairs[global_counter % len(self._surname_pairs)][1]
        else:
            first_names = MALE_FIRST_NAMES
            middle_names = MALE_MIDDLE_NAMES
            last_name = self._surname_pairs[global_counter % len(self._surname_pairs)][0]

        return (
            last_name,
            first_names[counter % len(first_names)],
            middle_names[((counter // (len(first_names) * len(self._surname_pairs))) + counter) % len(middle_names)],
        )


class Command(BaseCommand):
    help = "Reset demo enterprise data and create realistic departments, employees, logins, and vacation history"

    def add_arguments(self, parser):
        parser.add_argument("--seed-value", type=int, default=42)
        parser.add_argument(
            "--history-years",
            type=int,
            default=DEFAULT_SCHEDULE_HISTORY_YEARS,
            help="How many full years before the current year to include in vacation history.",
        )
        parser.add_argument(
            "--fast",
            action="store_true",
            help="Create a smaller but structurally complete dataset for tests and quick checks.",
        )
        parser.add_argument(
            "--confirm-reset",
            action="store_true",
            help="Confirm deleting existing demo data before rebuilding the demo enterprise dataset.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not options["confirm_reset"]:
            raise CommandError(
                "seed_vacation_requests deletes existing demo employees, departments, vacation requests, "
                "schedules, and linked users. Run again with --confirm-reset to rebuild demo data."
            )

        self.rng = random.Random(options["seed_value"])
        self.today = timezone.localdate()
        self.fast_mode = options["fast"]
        history_years = FAST_SCHEDULE_HISTORY_YEARS if self.fast_mode else max(1, options["history_years"])
        self.schedule_end_year = self.today.year
        self.schedule_start_year = self.schedule_end_year - history_years
        self.enterprise_start_year = self.schedule_end_year - DEFAULT_SCHEDULE_HISTORY_YEARS
        self.schedule_approval_cutoff = date(self.schedule_end_year - 1, 12, 31)
        self.department_specs = self._build_department_specs()
        self.total_employee_count = sum(spec["employee_count"] for spec in self.department_specs)
        self.name_factory = NameFactory(self.rng)
        self.status_counts = Counter()
        self.schedule_item_counts = Counter()
        self.schedule_by_year = {}
        self.department_workload = {}
        self.staffing_rules = {}

        previous_sync_state = set_vacation_metric_sync_enabled(False)
        try:
            self._reset_demo_data()
            departments = self._create_departments()
            enterprise_head = self._create_enterprise_head()
            authorized_person = self._create_authorized_person()
            hr_team = self._create_hr_team(departments[-1])
            department_heads = self._create_department_heads(departments)
            employees = self._create_department_employees(departments)
            self._create_staffing_rules(departments)
            self._create_department_workload(departments)
            self._create_historical_schedules(hr_team[0], enterprise_head, authorized_person, departments)

            everyone = [enterprise_head, *hr_team, *department_heads, *employees]
            self._create_vacation_preferences(everyone)
            for employee in everyone:
                self._seed_employee_vacations(employee)

            self._cancel_unallocatable_paid_sources(everyone)
            self._create_balanced_special_request_history(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._create_pending_current_year_transfers()
        finally:
            set_vacation_metric_sync_enabled(previous_sync_state)

        for employee in everyone:
            rebuild_employee_leave_ledger(employee)

        self.stdout.write(
            self.style.SUCCESS(
                "Демо-база предприятия создана: "
                f"отделы={len(departments)}, "
                f"руководители_отделов={len(department_heads)}, "
                f"hr={len(hr_team)}, "
                f"директора={ENTERPRISE_HEAD_COUNT}, "
                f"сотрудники={len(employees)}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Созданы заявки: "
                f"approved={self.status_counts[VacationRequest.STATUS_APPROVED]}, "
                f"pending={self.status_counts[VacationRequest.STATUS_PENDING]}, "
                f"rejected={self.status_counts[VacationRequest.STATUS_REJECTED]}"
            )
        )

    def _build_department_specs(self):
        specs = deepcopy(DEPARTMENT_SPECS)
        if not self.fast_mode:
            return specs

        for spec, employee_count in zip(specs, FAST_EMPLOYEE_COUNTS):
            spec["employee_count"] = employee_count
            spec["recent_hires"] = 1 if employee_count >= 4 else 0
        return specs

    def _reset_demo_data(self):
        user_ids = list(Employees.objects.exclude(user_id=None).values_list("user_id", flat=True))

        VacationScheduleChangeRequest.objects.all().delete()
        VacationRequestHistory.objects.all().delete()
        VacationEntitlementAllocation.objects.all().delete()
        VacationEntitlementPeriod.objects.all().delete()
        VacationPreference.objects.all().delete()
        DepartmentWorkload.objects.all().delete()
        DepartmentStaffingRule.objects.all().delete()
        VacationSchedule.objects.all().delete()
        VacationRequest.objects.all().delete()
        Employees.objects.all().delete()
        Departments.objects.all().delete()

        if user_ids:
            get_user_model().objects.filter(id__in=user_ids).delete()

    def _create_departments(self):
        departments = []
        for spec in self.department_specs:
            departments.append(Departments.objects.create(name=spec["name"], date_added=self._department_formation_at(spec)))
        return departments

    def _department_formation_at(self, spec):
        return timezone.make_aware(
            datetime(
                self.enterprise_start_year,
                spec["formation_month"],
                spec["formation_day"],
                spec["formation_hour"],
                spec["formation_minute"],
            ),
            timezone.get_current_timezone(),
        )

    def _create_staffing_rules(self, departments):
        for department, spec in zip(departments, self.department_specs):
            rule_spec = spec["staffing_rule"]
            self.staffing_rules[department.id] = DepartmentStaffingRule.objects.create(
                department=department,
                min_staff_required=rule_spec["min_staff_required"],
                max_absent=rule_spec["max_absent"],
                criticality_level=rule_spec["criticality_level"],
                substitution_group=rule_spec["substitution_group"],
            )

    def _month_end_date(self, year, month):
        if month == 12:
            return date(year, 12, 31)
        return date(year, month + 1, 1) - timedelta(days=1)

    def _department_active_count_on(self, department, as_of_date):
        return Employees.objects.filter(
            department=department,
            is_active_employee=True,
            date_joined__lte=as_of_date,
        ).exclude(role__in=Employees.SERVICE_ROLES).count()

    def _scale_staffing_metric(self, final_value, active_count, final_count, *, minimum=1):
        if final_count <= 0 or active_count <= 0:
            return minimum

        scaled_value = round(final_value * active_count / final_count)
        return min(final_value, max(minimum, scaled_value))

    def _historical_staffing_for_month(self, department, rule, year, month):
        month_end = self._month_end_date(year, month)
        final_count = self._department_active_count_on(department, date(self.schedule_end_year, 12, 31))
        active_count = self._department_active_count_on(department, month_end)
        if final_count <= 0 or active_count <= 0:
            return 1, 1

        min_staff_required = self._scale_staffing_metric(
            rule.min_staff_required,
            active_count,
            final_count,
            minimum=1,
        )
        min_staff_required = min(min_staff_required, active_count)
        max_absent = self._scale_staffing_metric(
            rule.max_absent,
            active_count,
            final_count,
            minimum=1,
        )
        return min_staff_required, max_absent

    def _create_department_workload(self, departments):
        for department, spec in zip(departments, self.department_specs):
            rule = self.staffing_rules[department.id]
            profile = spec["workload_profile"]
            for year in range(self.schedule_start_year, self.schedule_end_year + 1):
                yearly_spike_month = self.rng.choice(range(1, 13)) if self.rng.random() < 0.18 else None
                for month in range(1, 13):
                    load_level = profile[month - 1]
                    if month == yearly_spike_month:
                        load_level = min(5, load_level + 1)
                    elif self.rng.random() < 0.06:
                        load_level = min(5, load_level + 1)
                    elif self.rng.random() < 0.04:
                        load_level = max(1, load_level - 1)
                    min_staff_required, max_absent = self._historical_staffing_for_month(
                        department,
                        rule,
                        year,
                        month,
                    )
                    workload = DepartmentWorkload.objects.create(
                        department=department,
                        year=year,
                        month=month,
                        load_level=load_level,
                        min_staff_required=min_staff_required,
                        max_absent=max_absent,
                    )
                    self.department_workload[(department.id, year, month)] = workload

    def _create_historical_schedules(self, hr_employee, enterprise_head, authorized_person, departments):
        generated_base_month = 12
        for year in range(self.schedule_start_year, self.schedule_end_year + 1):
            generated_at = timezone.make_aware(datetime(year - 1, generated_base_month, self.rng.randint(1, 14), 10, 0))
            approved_at = timezone.make_aware(datetime(year - 1, generated_base_month, self.rng.randint(15, 25), 16, 0))
            schedule = VacationSchedule.objects.create(
                year=year,
                status=VacationSchedule.STATUS_ARCHIVED if year < self.schedule_end_year else VacationSchedule.STATUS_APPROVED,
                created_by=hr_employee,
                approved_by=enterprise_head,
                generated_at=generated_at,
                approved_at=approved_at,
            )
            self.schedule_by_year[year] = schedule
            for department in departments:
                VacationScheduleDepartmentApproval.objects.create(
                    schedule=schedule,
                    department=department,
                    department_head=department.head,
                    status=VacationScheduleDepartmentApproval.STATUS_APPROVED,
                    comment="Согласовано по историческому графику.",
                    approved_at=approved_at,
                )
            VacationScheduleEnterpriseApproval.objects.create(
                schedule=schedule,
                enterprise_head=enterprise_head,
                status=VacationScheduleEnterpriseApproval.STATUS_APPROVED,
                comment="График руководителей отделов утвержден.",
                approved_at=approved_at,
            )
            VacationScheduleAuthorizedApproval.objects.create(
                schedule=schedule,
                authorized_person=authorized_person,
                status=VacationScheduleAuthorizedApproval.STATUS_APPROVED,
                comment="Отпуск руководителя предприятия согласован уполномоченным лицом.",
                approved_at=approved_at,
            )

    def _create_vacation_preferences(self, employees):
        preference_comments = [
            "Предпочитает отпуск в период школьных каникул.",
            "Просит не ставить отпуск на время квартальной отчетности.",
            "Готов перенести даты при производственной необходимости.",
            "Желательно совместить отпуск с семейной поездкой.",
            "Просит первую половину отпуска летом.",
            "Предпочитает спокойный период с низкой нагрузкой отдела.",
            "Резервный период указан на случай конфликта графика.",
        ]
        for employee in employees:
            if employee.is_service_account:
                continue
            start_year = max(self.schedule_start_year, employee.date_joined.year)
            for year in range(start_year, self.schedule_end_year + 1):
                if self.rng.random() < 0.22:
                    VacationPreference.objects.create(
                        employee=employee,
                        year=year,
                        status=VacationPreference.STATUS_SKIPPED,
                        comment=self.rng.choice(["Пожелания не указаны.", "Сотрудник готов принять даты по решению HR."]),
                        created_automatically=True,
                    )
                    continue
                for priority, duration in [
                    (VacationPreference.PRIORITY_PRIMARY, self.rng.choice([14, 21, 28])),
                    (VacationPreference.PRIORITY_BACKUP, self.rng.choice([10, 14, 24])),
                ]:
                    high_load_months = [
                        workload.month
                        for workload in self.department_workload.values()
                        if workload.department_id == employee.department_id
                        and workload.year == year
                        and workload.load_level >= 4
                    ]
                    month_pool = high_load_months if high_load_months and self.rng.random() < 0.36 else [2, 3, 4, 6, 7, 8, 9, 10, 11]
                    month = self.rng.choice(month_pool)
                    start_day = self.rng.randint(1, 10)
                    start_date = date(year, month, start_day)
                    end_date = min(start_date + timedelta(days=duration - 1), date(year, month, 28))
                    VacationPreference.objects.create(
                        employee=employee,
                        year=year,
                        start_date=start_date,
                        end_date=end_date,
                        priority=priority,
                        status=VacationPreference.STATUS_FILLED,
                        comment=self.rng.choice(preference_comments),
                        created_automatically=True,
                    )

    def _create_enterprise_head(self):
        return self._create_employee(
            login="director_1",
            role=Employees.ROLE_ENTERPRISE_HEAD,
            position="Директор предприятия",
            department=None,
            gender="male",
            min_years=8,
            max_years=12,
            date_joined=date(self.enterprise_start_year, 1, 4),
        )

    def _create_authorized_person(self):
        employee = Employees.objects.create(
            login="admin_1",
            role=Employees.ROLE_AUTHORIZED_PERSON,
            position="Уполномоченное лицо",
            department=None,
            date_joined=date(self.enterprise_start_year, 1, 4),
        )
        sync_employee_user(employee, raw_password=DEFAULT_PASSWORD)
        return employee

    def _create_hr_team(self, department):
        positions = ["HR бизнес-партнер", "Ведущий HR-специалист"]
        genders = ["female", "female"]
        hr_team = []
        hr_min_join_date = date(self.enterprise_start_year, 6, 1)
        for index in range(HR_COUNT):
            hr_team.append(
                self._create_employee(
                    login=f"hr_{index + 1}",
                    role=Employees.ROLE_HR,
                    position=positions[index % len(positions)],
                    department=department,
                    gender=genders[index % len(genders)],
                    min_years=4,
                    max_years=9,
                    min_join_date=hr_min_join_date,
                )
            )
        return hr_team

    def _create_department_heads(self, departments):
        heads = []
        for index, (department, spec) in enumerate(zip(departments, self.department_specs), start=1):
            heads.append(
                self._create_employee(
                    login=f"manager_{index}",
                    role=Employees.ROLE_DEPARTMENT_HEAD,
                    position=spec["head_position"],
                    department=department,
                    gender="male" if index % 2 else "female",
                    min_years=6,
                    max_years=11,
                    date_joined=timezone.localtime(department.date_added).date(),
                )
            )
        return heads

    def _create_department_employees(self, departments):
        employees = []
        employee_index = 1
        for department, spec in zip(departments, self.department_specs):
            min_join_date = timezone.localtime(department.date_added).date() + timedelta(days=1)
            for slot in range(spec["employee_count"]):
                date_joined = self._recent_hire_date(employee_index) if slot < spec.get("recent_hires", 0) else None
                employees.append(
                    self._create_employee(
                        login=f"employ_{employee_index}",
                        role=Employees.ROLE_EMPLOYEE,
                        position=spec["employee_positions"][slot % len(spec["employee_positions"])],
                        department=department,
                        gender="female" if (employee_index + slot) % 4 == 0 else "male",
                        min_years=0 if date_joined else 1,
                        max_years=10,
                        date_joined=date_joined,
                        min_join_date=min_join_date,
                    )
                )
                employee_index += 1
        return employees

    def _recent_hire_date(self, employee_index):
        base_date = date(self.schedule_end_year, 1, 10)
        latest_date = min(self.today - timedelta(days=14), date(self.schedule_end_year, 3, 20))
        if latest_date < base_date:
            latest_date = base_date
        return base_date + timedelta(days=(employee_index * 11) % ((latest_date - base_date).days + 1))

    def _create_employee(self, login, role, position, department, gender, min_years, max_years, date_joined=None, min_join_date=None):
        last_name, first_name, middle_name = self.name_factory.next_name(gender)
        if date_joined is None:
            start_days = self.rng.randint(min_years * 365, max_years * 365)
            date_joined = self.today - timedelta(days=start_days)
            earliest_join_date = date(self.schedule_start_year, 1, 10)
            if date_joined < earliest_join_date:
                latest_join_date = min(self.schedule_approval_cutoff, self.today - timedelta(days=365))
                if latest_join_date < earliest_join_date:
                    latest_join_date = earliest_join_date
                date_joined = earliest_join_date + timedelta(days=self.rng.randint(0, (latest_join_date - earliest_join_date).days))
        if min_join_date is not None and date_joined < min_join_date:
            latest_join_date = min(self.schedule_approval_cutoff, self.today - timedelta(days=365))
            if latest_join_date < min_join_date:
                date_joined = min_join_date
            else:
                date_joined = min_join_date + timedelta(days=self.rng.randint(0, (latest_join_date - min_join_date).days))

        employee = Employees.objects.create(
            login=login,
            last_name=last_name,
            first_name=first_name,
            middle_name=middle_name,
            position=position,
            role=role,
            date_joined=date_joined,
            annual_paid_leave_days=52,
            department=department,
        )
        sync_employee_user(employee, raw_password=DEFAULT_PASSWORD)
        return employee

    def _seed_employee_vacations(self, employee):
        if employee.is_service_account:
            return

        occupied_periods = []
        paid_periods = []
        tenure_days = max((self.today - employee.date_joined).days, 0)
        requestable_days = int(get_employee_requestable_leave(employee, self.today))
        target_reserved_days = self._target_reserved_days(tenure_days, requestable_days)
        target_available_days = self._target_available_balance(tenure_days, requestable_days, target_reserved_days)
        target_used_paid_days = max(requestable_days - target_available_days - target_reserved_days, 0)
        working_years = self._build_working_year_windows(employee)
        year_budgets = self._allocate_paid_budget_by_working_year(employee, working_years, target_used_paid_days)

        remaining_paid_budget = 0
        for year_window, year_budget in zip(working_years, year_budgets):
            remaining_paid_budget += self._seed_paid_history_for_working_year(
                employee,
                occupied_periods,
                paid_periods,
                year_window,
                year_budget,
            )
            self._maybe_create_historical_special_leave(
                employee,
                occupied_periods,
                paid_periods,
                year_window,
                tenure_days,
            )

        if remaining_paid_budget >= 5:
            remaining_paid_budget = self._backfill_paid_budget(
                employee,
                occupied_periods,
                paid_periods,
                working_years,
                remaining_paid_budget,
            )

        if employee.date_joined <= self.schedule_approval_cutoff:
            self._seed_current_calendar_year_schedule(employee, occupied_periods, paid_periods)
        else:
            self._create_new_hire_paid_exception(employee, occupied_periods, paid_periods)

    def _create_new_hire_paid_exception(self, employee, occupied_periods, paid_periods):
        earliest_start = add_months_safe(employee.date_joined, 6)
        window_start = max(earliest_start, self.today + timedelta(days=30))
        window_end = date(self.schedule_end_year, 12, 15)
        if window_start > window_end or self.rng.random() > 0.62:
            return

        duration = self._pick_duration(window_start, window_end, [14, 10, 7])
        if duration is None:
            return

        slot = self._find_free_slot(
            occupied_periods,
            window_start,
            window_end,
            duration,
            gap_periods=paid_periods,
            min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE),
        )
        if slot is None:
            return

        start_date, end_date = slot
        status = self.rng.choice([VacationRequest.STATUS_PENDING, VacationRequest.STATUS_APPROVED])
        self._create_request(
            employee,
            start_date,
            end_date,
            "paid",
            status,
            reason="Оплачиваемый отпуск вне графика для сотрудника, принятого после утверждения годового графика.",
        )
        occupied_periods.append((start_date, end_date))
        paid_periods.append((start_date, end_date))

    def _cancel_unallocatable_paid_sources(self, employees):
        for employee in employees:
            if employee.is_service_account:
                continue
            rebuild_employee_leave_ledger(employee, strict=False)
            for item in employee.vacation_schedule_items.filter(
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            ):
                allocated_days = sum(allocation.allocated_days for allocation in item.entitlement_allocations.all())
                if allocated_days < item.chargeable_days:
                    item.status = VacationScheduleItem.STATUS_CANCELLED
                    item.manager_comment = "Отменено при сверке отпускных прав по рабочим годам."
                    item.save(update_fields=["status", "manager_comment"])

            active_paid_requests = employee.vacation_requests.filter(
                vacation_type="paid",
                status__in=VacationRequest.ACTIVE_STATUSES,
            )
            active_paid_requests = exclude_converted_paid_requests(active_paid_requests, employee_ids=[employee.id])
            for request_obj in active_paid_requests:
                requested_days = get_chargeable_leave_days(request_obj.start_date, request_obj.end_date, request_obj.vacation_type)
                allocated_days = sum(allocation.allocated_days for allocation in request_obj.entitlement_allocations.all())
                if allocated_days < requested_days:
                    request_obj.status = VacationRequest.STATUS_REJECTED
                    request_obj.reviewed_by = self._reviewer_for_employee(employee)
                    request_obj.review_comment = "Отклонено при сверке отпускных прав по рабочим годам."
                    request_obj.reviewed_at = request_obj.reviewed_at or timezone.now()
                    request_obj.save(update_fields=["status", "reviewed_by", "review_comment", "reviewed_at"])
                    rebuild_vacation_request_history(request_obj)

            VacationEntitlementAllocation.objects.filter(employee=employee).delete()

    def _create_balanced_special_request_history(self, employees):
        eligible_employees = [employee for employee in employees if not employee.is_service_account]
        for year in range(self.schedule_start_year, self.schedule_end_year + 1):
            year_end = date(year, 12, 31)
            active_employees = [employee for employee in eligible_employees if employee.date_joined <= year_end]
            if len(active_employees) < 8:
                continue

            target_total = self._target_special_request_count(len(active_employees))
            target_rejected = self._target_special_rejection_count(target_total, len(active_employees))
            existing_total = VacationRequest.objects.filter(
                vacation_type__in=SPECIAL_REQUEST_TYPES,
                start_date__year=year,
            ).count()
            existing_rejected = VacationRequest.objects.filter(
                vacation_type__in=SPECIAL_REQUEST_TYPES,
                status=VacationRequest.STATUS_REJECTED,
                start_date__year=year,
            ).count()

            rejected_to_create = max(0, target_rejected - existing_rejected)
            approved_or_pending_to_create = max(0, target_total - existing_total - rejected_to_create)

            for _ in range(rejected_to_create):
                self._create_balanced_special_request(
                    active_employees,
                    year,
                    VacationRequest.STATUS_REJECTED,
                    prefer_high_load=True,
                )

            for _ in range(approved_or_pending_to_create):
                status = VacationRequest.STATUS_PENDING if year == self.schedule_end_year else VacationRequest.STATUS_APPROVED
                self._create_balanced_special_request(
                    active_employees,
                    year,
                    status,
                    prefer_high_load=False,
                )

    def _target_special_request_count(self, active_employee_count):
        full_company_target = self.rng.randint(*SPECIAL_REQUEST_TARGET_RANGE)
        scaled_target = round(full_company_target * active_employee_count / self.total_employee_count)
        if active_employee_count >= 80:
            return max(18, scaled_target)
        if active_employee_count >= 40:
            return max(8, scaled_target)
        return max(2, scaled_target)

    def _target_special_rejection_count(self, target_total, active_employee_count):
        if target_total <= 0:
            return 0
        rejection_share = self.rng.uniform(*SPECIAL_REQUEST_REJECTION_SHARE_RANGE)
        minimum = 2 if active_employee_count >= 50 else 1
        return max(minimum, round(target_total * rejection_share))

    def _create_balanced_special_request(self, employees, year, status, prefer_high_load=False):
        for _ in range(140):
            employee = self.rng.choice(employees)
            vacation_type = self._pick_special_request_type(employee, year)
            if vacation_type is None:
                continue

            month = self._pick_special_request_month(employee, year, prefer_high_load)
            window_start, window_end = self._special_request_window(employee, year, status, month)
            durations = [7, 10, 14, 21] if vacation_type == "study" else [2, 3, 5, 7, 10]
            duration = self._pick_duration(window_start, window_end, durations)
            if duration is None:
                continue

            slot = self._find_free_request_slot(employee, window_start, window_end, duration)
            if slot is None:
                continue

            start_date, end_date = slot
            self._create_request(employee, start_date, end_date, vacation_type, status)
            return True
        return False

    def _pick_special_request_type(self, employee, year):
        study_allowed = (date(year, 12, 31) - employee.date_joined).days >= 365
        if not study_allowed:
            return "unpaid"
        return self.rng.choices(
            population=["unpaid", "study"],
            weights=[70, 30],
            k=1,
        )[0]

    def _pick_special_request_month(self, employee, year, prefer_high_load=False):
        if employee.department_id is None:
            return self.rng.randint(1, 12)

        workloads = [
            workload
            for key, workload in self.department_workload.items()
            if key[0] == employee.department_id and key[1] == year
        ]
        if not workloads:
            return self.rng.randint(1, 12)

        if prefer_high_load:
            months = [workload.month for workload in workloads if workload.load_level >= 4]
            if months:
                return self.rng.choice(months)

        weights = [max(1, 6 - workload.load_level) for workload in workloads]
        return self.rng.choices([workload.month for workload in workloads], weights=weights, k=1)[0]

    def _special_request_window(self, employee, year, status, month):
        month_start = date(year, month, 1)
        month_end = date(year, month, 28)
        window_start = max(month_start, employee.date_joined + timedelta(days=30))
        window_end = month_end

        if year == self.schedule_end_year:
            if status == VacationRequest.STATUS_PENDING:
                window_start = max(window_start, self.today + timedelta(days=20))
                window_end = date(year, 12, 20)
            else:
                window_end = min(window_end, max(self.today + timedelta(days=120), self.today))

        return window_start, window_end

    def _find_free_request_slot(self, employee, window_start, window_end, duration):
        latest_start = window_end - timedelta(days=duration - 1)
        if latest_start < window_start:
            return None

        for _ in range(80):
            offset = self.rng.randint(0, (latest_start - window_start).days)
            start_date = window_start + timedelta(days=offset)
            end_date = start_date + timedelta(days=duration - 1)
            if not self._request_period_conflicts(employee, start_date, end_date):
                return start_date, end_date

        cursor = window_start
        while cursor <= latest_start:
            end_date = cursor + timedelta(days=duration - 1)
            if not self._request_period_conflicts(employee, cursor, end_date):
                return cursor, end_date
            cursor += timedelta(days=1)
        return None

    def _request_period_conflicts(self, employee, start_date, end_date):
        conflicting_requests = VacationRequest.objects.filter(
            employee=employee,
            start_date__lte=end_date,
            end_date__gte=start_date,
        )
        conflicting_requests = exclude_converted_paid_requests(
            conflicting_requests,
            employee_ids=[employee.id],
            start_date=start_date,
            end_date=end_date,
        )
        return (
            conflicting_requests.exists()
            or VacationScheduleItem.objects.filter(
                employee=employee,
                status__in=VacationScheduleItem.ACTIVE_STATUSES,
                start_date__lte=end_date,
                end_date__gte=start_date,
            ).exists()
        )

    def _build_working_year_windows(self, employee):
        windows = []
        cursor = employee.date_joined
        while cursor <= self.today:
            window_end = add_years_safe(cursor, 1) - timedelta(days=1)
            windows.append(
                {
                    "start": cursor,
                    "end": window_end,
                    "completed": window_end < self.today,
                    "is_current": cursor <= self.today <= window_end,
                }
            )
            cursor = window_end + timedelta(days=1)
        return windows

    def _allocate_paid_budget_by_working_year(self, employee, working_years, target_used_paid_days):
        budgets = [0] * len(working_years)
        for index, window in enumerate(working_years):
            if (
                window["completed"]
                and window["start"].year <= self.schedule_end_year
                and window["end"].year < self.schedule_end_year
            ):
                budgets[index] = 52
        return budgets

    def _seed_current_calendar_year_schedule(self, employee, occupied_periods, paid_periods):
        schedule = self.schedule_by_year.get(self.schedule_end_year)
        if schedule is None:
            return

        year_start = date(self.schedule_end_year, 1, 1)
        year_end = date(self.schedule_end_year, 12, 20)
        if employee.date_joined > year_start:
            year_start = employee.date_joined
        year_start = max(year_start, add_months_safe(employee.date_joined, 6))
        if year_start > year_end:
            return

        target_days = 52
        consumed_days = 0
        split_variants = [
            [28, 24],
            [28, 14, 10],
            [21, 17, 14],
            [14, 14, 14, 10],
        ]
        durations = self.rng.choice(split_variants)
        for duration in durations:
            consumed = self._create_paid_leave_block(
                employee,
                occupied_periods,
                paid_periods,
                year_start,
                year_end,
                duration=duration,
                min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE) if paid_periods else 0,
            )
            consumed_days += consumed

        attempts = 0
        while target_days - consumed_days >= 5 and attempts < 4:
            remaining = target_days - consumed_days
            duration = self._pick_paid_extra_duration(remaining)
            if duration is None:
                break
            consumed = self._create_paid_leave_block(
                employee,
                occupied_periods,
                paid_periods,
                year_start,
                year_end,
                duration=duration,
                min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE),
            )
            if consumed <= 0:
                break
            consumed_days += consumed
            attempts += 1

    def _seed_paid_history_for_working_year(self, employee, occupied_periods, paid_periods, year_window, year_budget):
        if year_budget < 5:
            return year_budget

        budget_left = year_budget
        period_start = year_window["start"]
        if year_window["start"] == employee.date_joined:
            period_start = max(period_start, add_months_safe(employee.date_joined, 6))
        if year_window["is_current"]:
            period_start = max(period_start, self.today + timedelta(days=14))
            period_end = min(year_window["end"], date(self.schedule_end_year, 12, 31))
        else:
            period_end = min(year_window["end"], self.today - timedelta(days=7), date(self.schedule_end_year, 12, 31))
        if period_start > period_end:
            return year_budget

        if (year_window["completed"] or year_window["is_current"]) and budget_left >= 14:
            main_duration = self._pick_paid_main_duration(budget_left)
            budget_left -= self._create_paid_leave_block(
                employee,
                occupied_periods,
                paid_periods,
                period_start,
                period_end,
                duration=main_duration,
                min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE) if paid_periods else 0,
            )

        extras_allowed = 3 if year_window["completed"] else 2
        extras_created = 0
        while budget_left >= 5 and extras_created < extras_allowed:
            consumed = 0
            for extra_duration in [duration for duration in [14, 10, 7, 5] if duration <= budget_left]:
                consumed = self._create_paid_leave_block(
                    employee,
                    occupied_periods,
                    paid_periods,
                    period_start,
                    period_end,
                    duration=extra_duration,
                    min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE),
                )
                if consumed > 0:
                    break

            if consumed <= 0:
                break
            budget_left -= consumed
            extras_created += 1

        return max(budget_left, 0)

    def _pick_paid_main_duration(self, available_budget):
        if available_budget >= 42:
            variants = [28, 21, 14]
        elif available_budget >= 28:
            variants = [21, 14]
        else:
            variants = [14]
        return self.rng.choice([duration for duration in variants if duration <= available_budget] or [14])

    def _pick_paid_extra_duration(self, available_budget):
        variants = [14, 10, 7, 5]
        eligible = [duration for duration in variants if duration <= available_budget]
        if not eligible:
            return None
        return self.rng.choice(eligible)

    def _create_paid_leave_block(
        self,
        employee,
        occupied_periods,
        paid_periods,
        window_start,
        window_end,
        duration,
        min_gap_days=0,
    ):
        slot = self._find_free_slot(
            occupied_periods,
            window_start,
            window_end,
            duration,
            gap_periods=paid_periods,
            min_gap_days=min_gap_days,
        )
        if slot is None:
            return 0

        start_date, end_date = slot
        schedule = self.schedule_by_year.get(start_date.year)
        if schedule is None:
            return 0

        chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid")
        if self._should_create_transfer(employee, start_date):
            replacement_slot = self._find_transfer_slot(occupied_periods, start_date, end_date, duration)
            if replacement_slot is not None:
                new_start_date, new_end_date = replacement_slot
                original_item = self._create_schedule_item(
                    employee,
                    schedule,
                    start_date,
                    end_date,
                    VacationScheduleItem.STATUS_TRANSFERRED,
                    VacationScheduleItem.SOURCE_GENERATED,
                    chargeable_days,
                    was_changed_by_manager=True,
                )
                new_chargeable_days = get_chargeable_leave_days(new_start_date, new_end_date, "paid")
                replacement_item = self._create_schedule_item(
                    employee,
                    schedule,
                    new_start_date,
                    new_end_date,
                    VacationScheduleItem.STATUS_APPROVED,
                    VacationScheduleItem.SOURCE_TRANSFER,
                    new_chargeable_days,
                    previous_item=original_item,
                    was_changed_by_manager=True,
                )
                transfer_approved = self._create_change_request(original_item, replacement_item)
                if transfer_approved:
                    occupied_periods.append((new_start_date, new_end_date))
                    paid_periods.append((new_start_date, new_end_date))
                    return new_chargeable_days
                occupied_periods.append((start_date, end_date))
                paid_periods.append((start_date, end_date))
                return chargeable_days

        self._create_schedule_item(
            employee,
            schedule,
            start_date,
            end_date,
            VacationScheduleItem.STATUS_APPROVED,
            VacationScheduleItem.SOURCE_GENERATED,
            chargeable_days,
        )
        occupied_periods.append((start_date, end_date))
        paid_periods.append((start_date, end_date))
        return chargeable_days

    def _should_create_transfer(self, employee, start_date):
        if start_date.year >= self.schedule_end_year:
            return False
        if employee.role == Employees.ROLE_HR:
            return self.rng.random() < 0.07
        if employee.role in {Employees.ROLE_DEPARTMENT_HEAD, Employees.ROLE_ENTERPRISE_HEAD}:
            return self.rng.random() < 0.12
        return self.rng.random() < 0.10

    def _find_transfer_slot(self, occupied_periods, start_date, end_date, duration):
        year_end = date(start_date.year, 12, 31)
        search_start = min(start_date + timedelta(days=self.rng.choice([21, 28, 35, 42])), year_end)
        if search_start > year_end:
            return None
        return self._find_free_slot(
            [*occupied_periods, (start_date, end_date)],
            search_start,
            year_end,
            duration,
            max_attempts=30,
        )

    def _risk_level_for_score(self, risk_score):
        if risk_score >= 70:
            return VacationScheduleItem.RISK_HIGH
        if risk_score >= 40:
            return VacationScheduleItem.RISK_MEDIUM
        return VacationScheduleItem.RISK_LOW

    def _calculate_schedule_risk(self, employee, start_date):
        if employee.department_id is None:
            base_score = 58 if employee.role == Employees.ROLE_ENTERPRISE_HEAD else 35
            return base_score, self._risk_level_for_score(base_score), None

        workload = self.department_workload.get((employee.department_id, start_date.year, start_date.month))
        load_level = workload.load_level if workload is not None else 3
        role_boost = 18 if employee.role == Employees.ROLE_DEPARTMENT_HEAD else 0
        random_boost = self.rng.randint(0, 18)
        risk_score = min(95, 10 + load_level * 12 + role_boost + random_boost)
        return risk_score, self._risk_level_for_score(risk_score), workload

    def _create_schedule_item(
        self,
        employee,
        schedule,
        start_date,
        end_date,
        status,
        source,
        chargeable_days,
        previous_item=None,
        was_changed_by_manager=False,
    ):
        risk_score, risk_level, _ = self._calculate_schedule_risk(employee, start_date)
        item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=chargeable_days,
            status=status,
            source=source,
            risk_score=risk_score,
            risk_level=risk_level,
            generated_by_ai=True,
            was_changed_by_manager=was_changed_by_manager,
            manager_comment="Историческая запись графика отпусков." if not was_changed_by_manager else "Перенесено при согласовании.",
            previous_item=previous_item,
        )
        self.schedule_item_counts[status] += 1
        return item

    def _reviewer_for_employee(self, employee):
        return get_expected_vacation_approver(employee).employee

    def _create_change_request(self, original_item, replacement_item):
        employee = original_item.employee
        reviewer = self._reviewer_for_employee(employee)
        risk_score, risk_level, workload = self._calculate_schedule_risk(employee, replacement_item.start_date)
        min_staff_required = workload.min_staff_required if workload is not None else 1
        remaining_staff_count = max(min_staff_required, min_staff_required + self.rng.randint(-1, 4))
        status = (
            VacationScheduleChangeRequest.STATUS_REJECTED
            if self.rng.random() < 0.18
            else VacationScheduleChangeRequest.STATUS_APPROVED
        )
        if status == VacationScheduleChangeRequest.STATUS_REJECTED:
            original_item.status = VacationScheduleItem.STATUS_APPROVED
            original_item.save(update_fields=["status"])
            replacement_item.status = VacationScheduleItem.STATUS_CANCELLED
            replacement_item.save(update_fields=["status"])
            self.schedule_item_counts[VacationScheduleItem.STATUS_CANCELLED] += 1
        VacationScheduleChangeRequest.objects.create(
            schedule_item=original_item,
            employee=employee,
            old_start_date=original_item.start_date,
            old_end_date=original_item.end_date,
            new_start_date=replacement_item.start_date,
            new_end_date=replacement_item.end_date,
            reason=self.rng.choice([
                "Семейные обстоятельства.",
                "Производственная необходимость.",
                "Корректировка графика отдела.",
                "Перенос по согласованию сторон.",
            ]),
            status=status,
            requested_by=employee,
            reviewed_by=reviewer,
            review_comment="Перенос согласован." if status == VacationScheduleChangeRequest.STATUS_APPROVED else "Период признан рискованным для отдела.",
            risk_score=risk_score,
            risk_level=risk_level,
            department_load_level=workload.load_level if workload is not None else 3,
            overlapping_absences_count=self.rng.randint(0, 4),
            remaining_staff_count=remaining_staff_count,
            min_staff_required=min_staff_required,
            balance_after_change=0,
            reviewed_at=original_item.schedule.approved_at,
        )
        return status == VacationScheduleChangeRequest.STATUS_APPROVED

    def _create_pending_current_year_transfers(self):
        current_year = self.schedule_end_year
        candidates = list(
            VacationScheduleItem.objects.select_related("employee", "schedule")
            .filter(
                schedule__year=current_year,
                status=VacationScheduleItem.STATUS_APPROVED,
                source=VacationScheduleItem.SOURCE_GENERATED,
                start_date__gt=self.today + timedelta(days=21),
                employee__is_active_employee=True,
            )
            .exclude(employee__role__in=Employees.SERVICE_ROLES)
            .order_by("start_date", "employee__last_name")
        )
        self.rng.shuffle(candidates)
        created_count = 0
        target_count = 6 if not self.fast_mode else 2
        for item in candidates:
            if created_count >= target_count:
                break
            occupied_periods = list(
                VacationScheduleItem.objects.filter(
                    employee=item.employee,
                    status__in=VacationScheduleItem.ACTIVE_STATUSES,
                    start_date__year=current_year,
                )
                .exclude(pk=item.pk)
                .values_list("start_date", "end_date")
            )
            active_requests = VacationRequest.objects.filter(
                employee=item.employee,
                status__in=VacationRequest.ACTIVE_STATUSES,
                start_date__year=current_year,
            )
            active_requests = exclude_converted_paid_requests(
                active_requests,
                employee_ids=[item.employee_id],
                start_date=date(current_year, 1, 1),
                end_date=date(current_year, 12, 31),
            )
            occupied_periods.extend(active_requests.values_list("start_date", "end_date"))
            duration = (item.end_date - item.start_date).days + 1
            search_start = max(self.today + timedelta(days=30), item.end_date + timedelta(days=21))
            slot = self._find_free_slot(
                occupied_periods,
                search_start,
                date(current_year, 12, 31),
                duration,
                max_attempts=50,
            )
            if slot is None:
                continue
            try:
                create_schedule_change_request(
                    item.id,
                    requested_by=item.employee,
                    new_start_date=slot[0],
                    new_end_date=slot[1],
                    reason=self.rng.choice(
                        [
                            "Семейные обстоятельства.",
                            "Нужно перенести отпуск на более поздний период.",
                            "Перенос по согласованию с руководителем.",
                        ]
                    ),
                )
            except ValidationError:
                continue
            created_count += 1

    def _backfill_paid_budget(self, employee, occupied_periods, paid_periods, working_years, remaining_budget):
        for year_window in reversed(working_years):
            if remaining_budget < 5:
                break
            if not year_window["completed"]:
                continue
            existing_window_days = self._paid_days_in_window(paid_periods, year_window["start"], year_window["end"])
            window_capacity = max(52 - existing_window_days, 0)
            if window_capacity < 5:
                continue
            extra_duration = self._pick_paid_extra_duration(remaining_budget)
            if extra_duration is None:
                break
            extra_duration = min(extra_duration, window_capacity)
            if extra_duration < 5:
                continue
            window_start = year_window["start"]
            if year_window["start"] == employee.date_joined:
                window_start = max(window_start, add_months_safe(employee.date_joined, 6))
            consumed = self._create_paid_leave_block(
                employee,
                occupied_periods,
                paid_periods,
                window_start,
                min(year_window["end"], self.today - timedelta(days=7), date(self.schedule_end_year, 12, 31)),
                duration=extra_duration,
                min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE),
            )
            if consumed <= 0:
                continue
            remaining_budget -= consumed
        return max(remaining_budget, 0)

    def _paid_days_in_window(self, paid_periods, window_start, window_end):
        total_days = 0
        for period_start, period_end in paid_periods:
            clipped_start = max(period_start, window_start)
            clipped_end = min(period_end, window_end)
            if clipped_start <= clipped_end:
                total_days += get_chargeable_leave_days(clipped_start, clipped_end, "paid")
        return total_days

    def _maybe_create_historical_special_leave(self, employee, occupied_periods, paid_periods, year_window, tenure_days):
        if not year_window["completed"]:
            return

        vacation_type = None
        if self.rng.random() < 0.16:
            vacation_type = "unpaid"
        elif tenure_days > 365 and self.rng.random() < 0.09:
            vacation_type = "study"

        if vacation_type is None:
            return

        self._create_special_leave(
            employee,
            occupied_periods,
            paid_periods,
            year_window["start"],
            min(year_window["end"], self.today - timedelta(days=14)),
            vacation_type,
            VacationRequest.STATUS_APPROVED,
        )

    def _create_special_leave(self, employee, occupied_periods, paid_periods, window_start, window_end, vacation_type, status):
        duration = self._pick_duration(window_start, window_end, [3, 5, 7, 10])
        if duration is None:
            return False

        slot = self._find_free_slot(
            occupied_periods,
            window_start,
            window_end,
            duration,
            gap_periods=paid_periods if vacation_type == "paid" else None,
            min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE) if vacation_type == "paid" else 0,
        )
        if slot is None:
            return False

        start_date, end_date = slot
        self._create_request(employee, start_date, end_date, vacation_type, status)
        occupied_periods.append((start_date, end_date))
        if vacation_type == "paid":
            paid_periods.append((start_date, end_date))
        return True

    def _consume_remaining_paid_budget(self, employee, occupied_periods, earliest_paid_start, remaining_paid_budget):
        if remaining_paid_budget < 5:
            return remaining_paid_budget

        year_windows = []
        for year_cursor in range(max(earliest_paid_start.year, self.today.year - 9), self.today.year + 1):
            window_start = max(date(year_cursor, 1, 1), earliest_paid_start)
            window_end = min(date(year_cursor, 12, 31), self.today - timedelta(days=21))
            if window_start <= window_end:
                year_windows.append((window_start, window_end))

        for pass_index in range(3):
            if remaining_paid_budget < 5:
                break

            made_progress = False
            for window_start, window_end in year_windows:
                duration = self._pick_duration(
                    window_start,
                    window_end,
                    [35, 28, 21, 14, 10, 7] if pass_index == 0 else [21, 14, 10, 7],
                )
                if duration is None:
                    continue

                duration = min(duration, remaining_paid_budget)
                if duration < 5:
                    continue

                slot = self._find_free_slot(occupied_periods, window_start, window_end, duration)
                if slot is None:
                    continue

                start_date, end_date = slot
                self._create_request(employee, start_date, end_date, "paid", VacationRequest.STATUS_APPROVED)
                occupied_periods.append((start_date, end_date))
                remaining_paid_budget -= get_chargeable_leave_days(start_date, end_date, "paid")
                made_progress = True

                if remaining_paid_budget < 5:
                    break

            if not made_progress:
                break

        return max(remaining_paid_budget, 0)

    def _target_available_balance(self, tenure_days, requestable_days, target_reserved_days):
        tenure_years = tenure_days / 365
        available_limit = max(requestable_days - target_reserved_days, 0)

        if tenure_years >= 8:
            if self.rng.random() < 0.12:
                target = self.rng.randint(55, 85)
            else:
                target = self.rng.randint(16, 42)
        elif tenure_years >= 5:
            if self.rng.random() < 0.10:
                target = self.rng.randint(45, 70)
            else:
                target = self.rng.randint(14, 36)
        elif tenure_years >= 2:
            target = self.rng.randint(8, 28)
        else:
            target = self.rng.randint(3, 18)

        return min(target, available_limit, MAX_REALISTIC_AVAILABLE_DAYS)

    def _target_reserved_days(self, tenure_days, requestable_days):
        if tenure_days <= 220 or requestable_days < 5:
            return 0

        if self.rng.random() > 0.42:
            return 0

        variants = [7, 10, 14]
        if requestable_days >= 21 and self.rng.random() < 0.25:
            variants.append(21)
        return min(self.rng.choice(variants), requestable_days)

    def _approved_probability(self, tenure_days, year_cursor):
        tenure_years = tenure_days / 365
        if year_cursor == self.today.year:
            return 0.38 if tenure_years < 3 else 0.62
        if tenure_years >= 7:
            return 0.95
        if tenure_years >= 4:
            return 0.84
        return 0.68

    def _create_approved_leave(self, employee, occupied_periods, window_start, window_end, remaining_paid_budget):
        vacation_type = self.rng.choices(
            population=["paid", "paid", "paid", "unpaid", "study"],
            weights=[55, 20, 10, 10, 5],
            k=1,
        )[0]
        durations = [7, 10, 14, 21, 28] if vacation_type == "paid" else [3, 5, 7, 10]
        duration = self._pick_duration(window_start, window_end, durations)
        if duration is None:
            return remaining_paid_budget

        if vacation_type == "paid":
            duration = min(duration, remaining_paid_budget if remaining_paid_budget > 0 else duration)
            if duration < 5:
                return remaining_paid_budget

        slot = self._find_free_slot(occupied_periods, window_start, window_end, duration)
        if slot is None:
            return remaining_paid_budget

        start_date, end_date = slot
        self._create_request(employee, start_date, end_date, vacation_type, VacationRequest.STATUS_APPROVED)
        occupied_periods.append((start_date, end_date))

        if vacation_type == "paid":
            remaining_paid_budget -= get_chargeable_leave_days(start_date, end_date, vacation_type)
        return max(remaining_paid_budget, 0)

    def _create_secondary_past_leave(self, employee, occupied_periods, window_start, window_end, remaining_paid_budget):
        vacation_type = self.rng.choices(
            population=["paid", "unpaid", "study"],
            weights=[45, 35, 20],
            k=1,
        )[0]
        duration = self._pick_duration(window_start, window_end, [3, 5, 7])
        if duration is None:
            return remaining_paid_budget

        if vacation_type == "paid":
            duration = min(duration, remaining_paid_budget)
            if duration < 3:
                return remaining_paid_budget

        slot = self._find_free_slot(occupied_periods, window_start, window_end, duration)
        if slot is None:
            return remaining_paid_budget

        start_date, end_date = slot
        self._create_request(employee, start_date, end_date, vacation_type, VacationRequest.STATUS_APPROVED)
        occupied_periods.append((start_date, end_date))
        if vacation_type == "paid":
            remaining_paid_budget -= get_chargeable_leave_days(start_date, end_date, vacation_type)
        return max(remaining_paid_budget, 0)

    def _create_current_approved_leave(self, employee, occupied_periods, remaining_paid_budget):
        duration = min(self.rng.choice([7, 10, 14]), remaining_paid_budget)
        if duration < 5:
            return remaining_paid_budget

        start_date = self.today - timedelta(days=self.rng.randint(0, min(duration - 1, 5)))
        end_date = start_date + timedelta(days=duration - 1)
        if self._period_overlaps(occupied_periods, start_date, end_date):
            return remaining_paid_budget

        self._create_request(employee, start_date, end_date, "paid", VacationRequest.STATUS_APPROVED)
        occupied_periods.append((start_date, end_date))
        remaining_paid_budget -= get_chargeable_leave_days(start_date, end_date, "paid")
        return max(remaining_paid_budget, 0)

    def _create_future_pending_leave(self, employee, occupied_periods, paid_periods, remaining_paid_budget):
        duration = self._pick_duration(
            self.today + timedelta(days=20),
            self.today + timedelta(days=210),
            [21, 14, 10, 7],
        )
        if duration is None:
            return

        duration = min(duration, remaining_paid_budget)
        if duration < 5:
            return

        slot = self._find_free_slot(
            occupied_periods,
            self.today + timedelta(days=20),
            self.today + timedelta(days=210),
            duration,
            gap_periods=paid_periods,
            min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE),
        )
        if slot is None:
            return

        start_date, end_date = slot
        self._create_request(employee, start_date, end_date, "paid", VacationRequest.STATUS_PENDING)
        occupied_periods.append((start_date, end_date))
        paid_periods.append((start_date, end_date))

    def _create_future_special_leave(self, employee, occupied_periods, paid_periods):
        vacation_type = self.rng.choice(["unpaid", "study"])
        self._create_special_leave(
            employee,
            occupied_periods,
            paid_periods,
            self.today + timedelta(days=20),
            self.today + timedelta(days=210),
            vacation_type,
            VacationRequest.STATUS_PENDING,
        )

    def _create_rejected_leave(self, employee, occupied_periods, paid_periods):
        window_start = self.today - timedelta(days=120)
        window_end = self.today + timedelta(days=160)
        vacation_types = ["unpaid", "study"]
        vacation_weights = [62, 38]
        if employee.date_joined > self.schedule_approval_cutoff:
            vacation_types.append("paid")
            vacation_weights.append(18)
        vacation_type = self.rng.choices(vacation_types, weights=vacation_weights, k=1)[0]
        if vacation_type == "paid":
            window_start = max(window_start, add_months_safe(employee.date_joined, 6))
        duration_options = [7, 10, 14] if vacation_type == "paid" else [3, 5, 7, 10]
        duration = self._pick_duration(window_start, window_end, duration_options)
        if duration is None:
            return

        slot = self._find_free_slot(
            occupied_periods,
            window_start,
            window_end,
            duration,
            gap_periods=paid_periods if vacation_type == "paid" else None,
            min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE) if vacation_type == "paid" else 0,
        )
        if slot is None:
            return

        start_date, end_date = slot
        self._create_request(employee, start_date, end_date, vacation_type, VacationRequest.STATUS_REJECTED)
        occupied_periods.append((start_date, end_date))
        if vacation_type == "paid":
            paid_periods.append((start_date, end_date))

    def _request_reason(self, vacation_type, status):
        if vacation_type == "paid":
            return self.rng.choice(
                [
                    "Оплачиваемый отпуск вне графика после появления права на отпуск.",
                    "Корректировка утвержденного графика по личным обстоятельствам.",
                    "Внеплановый оплачиваемый отпуск с учетом текущего баланса.",
                ]
            )
        if vacation_type == "study":
            return self.rng.choice(
                [
                    "Учебная сессия и подтверждающие документы от образовательной организации.",
                    "Подготовка и сдача экзаменов.",
                    "Защита учебного проекта по графику обучения.",
                    "Промежуточная аттестация по программе повышения квалификации.",
                    "Справка-вызов на период очной сессии.",
                ]
            )
        return self.rng.choice(
            [
                "Семейные обстоятельства.",
                "Личные обстоятельства, не связанные с ежегодным оплачиваемым отпуском.",
                "Краткосрочный отпуск без сохранения заработной платы.",
                "Переезд и оформление бытовых вопросов.",
                "Регистрация брака близкого родственника.",
                "Медицинские вопросы в семье.",
                "Необходимость сопровождения родственника.",
            ]
        )

    def _review_comment(self, status, risk_payload):
        if status == VacationRequest.STATUS_APPROVED:
            if risk_payload["risk_level"] == VacationRequest.RISK_HIGH:
                return self.rng.choice(
                    [
                        "Согласовано при высоком риске, требуется контроль замещения.",
                        "Согласовано после проверки графика отдела и доступности замены.",
                    ]
                )
            return self.rng.choice(
                [
                    "Согласовано, критичных ограничений по отделу не выявлено.",
                    "Согласовано: минимальный состав отдела сохраняется.",
                    "Согласовано, пересечений с критичными отсутствиями нет.",
                ]
            )
        if risk_payload["risk_level"] == VacationRequest.RISK_HIGH:
            return self.rng.choice(
                [
                    "Отклонено из-за высокой нагрузки отдела и риска нехватки сотрудников.",
                    "Отклонено: в периоде уже есть критичные отсутствия в отделе.",
                    "Отклонено, так как отдел опускается ниже минимального состава.",
                ]
            )
        return self.rng.choice(
            [
                "Отклонено, предложено выбрать другой период.",
                "Отклонено после проверки графика, рекомендован резервный период.",
                "Отклонено из-за пересечения с плановыми отсутствиями коллег.",
            ]
        )

    def _make_aware_datetime(self, value, hour, minute=0):
        return timezone.make_aware(datetime(value.year, value.month, value.day, hour, minute))

    def _request_timeline(self, start_date, reviewed_by):
        if reviewed_by is not None:
            review_date = min(start_date - timedelta(days=self.rng.randint(4, 14)), self.today)
            reviewed_at = self._make_aware_datetime(review_date, 15, 0)
            created_date = review_date - timedelta(days=self.rng.randint(1, 5))
        else:
            created_date = min(start_date - timedelta(days=self.rng.randint(7, 24)), self.today)
            reviewed_at = None

        created_at = self._make_aware_datetime(created_date, 9, self.rng.choice([0, 10, 20, 30]))
        submitted_at = get_vacation_submitted_at(created_at, reviewed_at)
        return created_at, submitted_at, reviewed_at

    def _create_request(self, employee, start_date, end_date, vacation_type, status, reason=""):
        risk_payload = calculate_vacation_request_risk(employee, start_date, end_date, vacation_type)
        reviewed_by = self._reviewer_for_employee(employee) if status != VacationRequest.STATUS_PENDING else None
        if status != VacationRequest.STATUS_PENDING and reviewed_by is None:
            status = VacationRequest.STATUS_PENDING
        created_at, submitted_at, reviewed_at = self._request_timeline(start_date, reviewed_by)
        request_obj = VacationRequest.objects.create(
            employee=employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type=vacation_type,
            status=status,
            reason=reason or self._request_reason(vacation_type, status),
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            review_comment=self._review_comment(status, risk_payload) if reviewed_by is not None else "",
            **risk_payload,
        )
        VacationRequest.objects.filter(pk=request_obj.pk).update(created_at=created_at)
        request_obj.created_at = created_at
        record_vacation_request_created(request_obj, created_at=created_at, submitted_at=submitted_at)
        if status != VacationRequest.STATUS_PENDING:
            record_vacation_request_reviewed(request_obj)
        if vacation_type == "paid" and status == VacationRequest.STATUS_APPROVED:
            create_schedule_item_from_paid_vacation_request(request_obj, risk_payload=risk_payload)
        self.status_counts[status] += 1
        return request_obj

    def _pick_duration(self, window_start, window_end, variants):
        if window_start > window_end:
            return None

        max_duration = (window_end - window_start).days + 1
        eligible = [variant for variant in variants if variant <= max_duration]
        if not eligible:
            return None
        return self.rng.choice(eligible)

    def _find_free_slot(
        self,
        occupied_periods,
        window_start,
        window_end,
        duration,
        gap_periods=None,
        min_gap_days=0,
        max_attempts=80,
    ):
        if window_start > window_end:
            return None

        latest_start = window_end - timedelta(days=duration - 1)
        if latest_start < window_start:
            return None

        for _ in range(max_attempts):
            offset = self.rng.randint(0, (latest_start - window_start).days)
            start_date = window_start + timedelta(days=offset)
            end_date = start_date + timedelta(days=duration - 1)
            if not self._period_overlaps(occupied_periods, start_date, end_date) and not self._period_overlaps_with_gap(
                gap_periods or [],
                start_date,
                end_date,
                min_gap_days,
            ):
                return start_date, end_date

        cursor = window_start
        while cursor <= latest_start:
            end_date = cursor + timedelta(days=duration - 1)
            if not self._period_overlaps(occupied_periods, cursor, end_date) and not self._period_overlaps_with_gap(
                gap_periods or [],
                cursor,
                end_date,
                min_gap_days,
            ):
                return cursor, end_date
            cursor += timedelta(days=1)
        return None

    def _period_overlaps(self, occupied_periods, start_date, end_date):
        return any(not (end_date < current_start or start_date > current_end) for current_start, current_end in occupied_periods)

    def _period_overlaps_with_gap(self, occupied_periods, start_date, end_date, min_gap_days):
        if min_gap_days <= 0:
            return False

        padded_start = start_date - timedelta(days=min_gap_days)
        padded_end = end_date + timedelta(days=min_gap_days)
        return any(not (padded_end < current_start or padded_start > current_end) for current_start, current_end in occupied_periods)
