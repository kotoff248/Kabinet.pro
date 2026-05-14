import random
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.color import no_style
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.core.models import DemoDataResetJob, Notification
from apps.core.services.demo_baseline import capture_demo_baseline_snapshot
from apps.core.services.demo_reset_jobs import update_demo_data_reset_job_progress
from apps.accounts.services import can_initiate_schedule_change_for_item, sync_employee_user
from apps.employees.models import (
    DepartmentCoverageRule,
    Departments,
    EmployeePosition,
    Employees,
    ProductionGroup,
    ProductionGroupSubstitutionRule,
)
from apps.employees.tenure import is_new_hire
from apps.leave.models import (
    DepartmentStaffingRule,
    DepartmentWorkload,
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationPreference,
    VacationPreferenceCollection,
    VacationRequest,
    VacationRequestHistory,
    VacationSchedule,
    VacationScheduleAuthorizedApproval,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleChangeRequest,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)
from apps.leave.services.dates import (
    add_months_safe,
    add_years_safe,
    get_chargeable_leave_days,
    iterate_dates,
    quantize_leave_days,
)
from apps.leave.services.ledger import (
    get_employee_leave_summary,
    get_employee_requestable_leave,
    rebuild_employee_leave_ledger,
)
from apps.leave.services.metrics import set_vacation_metric_sync_enabled
from apps.leave.services.notifications import backfill_notifications_from_history
from apps.leave.services.querysets import exclude_converted_paid_requests
from apps.leave.services.approval_routes import get_expected_vacation_approver
from apps.leave.services.historical_ml_traces import create_historical_schedule_ml_traces
from apps.leave.services.request_history import (
    get_vacation_submitted_at,
    rebuild_vacation_request_history,
    record_vacation_request_created,
    record_vacation_request_reviewed,
)
from apps.leave.services.risk import calculate_schedule_change_risk, calculate_vacation_request_risk
from apps.leave.services.schedule_changes import create_schedule_change_request
from apps.leave.services.schedule_items import create_schedule_item_from_paid_vacation_request
from apps.leave.services.urgent_closures import (
    approve_urgent_closure_by_manager,
    build_urgent_closure_options,
    create_urgent_closure_request,
)


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
            "min_staff_required": 20,
            "max_absent": 12,
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
        "position_groups": {
            "Начальник производства": "Руководство отдела",
            "Горный мастер": "Сменное руководство",
            "Ведущий инженер-технолог": "Инженеры и технологи",
            "Машинист технологического оборудования": "Операторы и машинисты",
            "Оператор производственной линии": "Операторы и машинисты",
            "Инженер по качеству": "Контроль качества",
        },
        "coverage_rules": {
            "Руководство отдела": {"min": 0, "max": 1, "criticality": 5},
            "Сменное руководство": {"min": 3, "max": 3, "criticality": 5},
            "Операторы и машинисты": {"min": 7, "max": 5, "criticality": 5},
            "Инженеры и технологи": {"min": 3, "max": 3, "criticality": 4},
            "Контроль качества": {"min": 3, "max": 3, "criticality": 4},
        },
        "substitution_rules": [
            {"substitute": "Сменное руководство", "source": "Операторы и машинисты", "max_covered_absences": 1},
            {"substitute": "Инженеры и технологи", "source": "Контроль качества", "max_covered_absences": 1},
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
            "min_staff_required": 15,
            "max_absent": 9,
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
        "position_groups": {
            "Руководитель службы ТОиР": "Руководство отдела",
            "Инженер по ремонту оборудования": "Инженеры ТОиР",
            "Механик участка": "Механики и слесари",
            "Электромонтер": "Электрики",
            "Слесарь по ремонту оборудования": "Механики и слесари",
            "Инженер-диагност": "Диагностика",
        },
        "coverage_rules": {
            "Руководство отдела": {"min": 0, "max": 1, "criticality": 5},
            "Механики и слесари": {"min": 6, "max": 4, "criticality": 5},
            "Электрики": {"min": 3, "max": 2, "criticality": 5},
            "Диагностика": {"min": 2, "max": 2, "criticality": 4},
            "Инженеры ТОиР": {"min": 3, "max": 2, "criticality": 4},
        },
        "substitution_rules": [
            {"substitute": "Инженеры ТОиР", "source": "Диагностика", "max_covered_absences": 1},
            {"substitute": "Диагностика", "source": "Инженеры ТОиР", "max_covered_absences": 1},
            {"substitute": "Инженеры ТОиР", "source": "Механики и слесари", "max_covered_absences": 1},
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
            "min_staff_required": 7,
            "max_absent": 5,
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
        "position_groups": {
            "Начальник службы промышленной безопасности": "Руководство отдела",
            "Инженер по охране труда": "Охрана труда",
            "Специалист по промышленной безопасности": "Промышленная безопасность",
            "Инспектор по технике безопасности": "Промышленная безопасность",
            "Инженер-эколог": "Экология",
            "Ведущий специалист по рискам": "Аудит и риски",
        },
        "coverage_rules": {
            "Руководство отдела": {"min": 0, "max": 1, "criticality": 5},
            "Охрана труда": {"min": 1, "max": 2, "criticality": 5},
            "Промышленная безопасность": {"min": 3, "max": 2, "criticality": 5},
            "Экология": {"min": 1, "max": 1, "criticality": 4},
            "Аудит и риски": {"min": 1, "max": 1, "criticality": 4},
        },
        "substitution_rules": [
            {"substitute": "Охрана труда", "source": "Промышленная безопасность", "max_covered_absences": 1},
            {"substitute": "Аудит и риски", "source": "Промышленная безопасность", "max_covered_absences": 1},
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
            "min_staff_required": 11,
            "max_absent": 7,
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
        "position_groups": {
            "Руководитель логистики": "Руководство отдела",
            "Специалист по логистике": "Логисты",
            "Диспетчер транспортного участка": "Диспетчеры",
            "Координатор поставок": "Поставки",
            "Инженер по складской логистике": "Складская логистика",
            "Аналитик цепочки поставок": "Аналитика цепочки поставок",
        },
        "coverage_rules": {
            "Руководство отдела": {"min": 0, "max": 1, "criticality": 5},
            "Логисты": {"min": 2, "max": 2, "criticality": 4},
            "Диспетчеры": {"min": 2, "max": 2, "criticality": 5},
            "Поставки": {"min": 2, "max": 2, "criticality": 4},
            "Складская логистика": {"min": 1, "max": 2, "criticality": 4},
            "Аналитика цепочки поставок": {"min": 1, "max": 2, "criticality": 3},
        },
        "substitution_rules": [
            {"substitute": "Логисты", "source": "Поставки", "max_covered_absences": 1},
            {"substitute": "Аналитика цепочки поставок", "source": "Логисты", "max_covered_absences": 1},
            {"substitute": "Складская логистика", "source": "Поставки", "max_covered_absences": 1},
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
            "min_staff_required": 10,
            "max_absent": 7,
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
        "position_groups": {
            "Руководитель финансов и закупок": "Руководство отдела",
            "Финансовый аналитик": "Финансовое планирование",
            "Экономист": "Финансовое планирование",
            "Специалист по закупкам": "Закупки и договоры",
            "Ведущий бухгалтер": "Бухгалтерия",
            "Контрактный менеджер": "Закупки и договоры",
            "HR бизнес-партнер": "HR и кадровое сопровождение",
            "Ведущий HR-специалист": "HR и кадровое сопровождение",
        },
        "coverage_rules": {
            "Руководство отдела": {"min": 0, "max": 1, "criticality": 5},
            "Финансовое планирование": {"min": 3, "max": 3, "criticality": 4},
            "Закупки и договоры": {"min": 3, "max": 3, "criticality": 4},
            "Бухгалтерия": {"min": 1, "max": 2, "criticality": 5},
            "HR и кадровое сопровождение": {"min": 1, "max": 2, "criticality": 4},
        },
        "substitution_rules": [
            {"substitute": "Финансовое планирование", "source": "Бухгалтерия", "max_covered_absences": 1},
            {"substitute": "Закупки и договоры", "source": "Финансовое планирование", "max_covered_absences": 1},
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
FULL_YEAR_CALENDAR_MIN_DAYS = 28
PARTIAL_YEAR_CALENDAR_MIN_DAYS = 14
FULL_YEAR_CALENDAR_TARGETS = (42, 45, 52, 56)
PARTIAL_YEAR_CALENDAR_TARGETS = (14, 21, 28)
PLANNING_YEAR_CARRYOVER_SOFT_CAP = Decimal("0.00")
PLANNING_YEAR_SHOWCASE_CARRYOVER_MIN = 22
PLANNING_YEAR_SHOWCASE_CARRYOVER_MAX = 34
PLANNING_YEAR_SHOWCASE_COUNT = 8
DEMO_CALENDAR_YEAR_NORMAL_MAX_DAYS = 60
DEMO_CALENDAR_YEAR_SHOWCASE_MAX_DAYS = 70
MIN_PAID_LEAVE_ANCHOR_DAYS = 14
DEMO_MANUAL_DRAFT_CASE_COUNT = 2
DEMO_MANUAL_DRAFT_CASE_SHORTAGE_DAYS = (3, 4)


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

    def execute(self, *args, **options):
        try:
            return super().execute(*args, **options)
        except Exception as exc:
            progress_job_id = options.get("progress_job_id") or getattr(self, "progress_job_id", None)
            if progress_job_id:
                update_demo_data_reset_job_progress(
                    progress_job_id,
                    status=DemoDataResetJob.STATUS_FAILED,
                    progress_percent=getattr(self, "_last_progress_percent", 0),
                    stage_label="Ошибка пересоздания",
                    error_message=str(exc),
                    finished=True,
                )
            raise

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
        parser.add_argument(
            "--progress-job-id",
            type=int,
            default=None,
            help="DemoDataResetJob id for background progress updates.",
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
        self.department_spec_by_name = {spec["name"]: spec for spec in self.department_specs}
        self.total_employee_count = sum(spec["employee_count"] for spec in self.department_specs)
        self.name_factory = NameFactory(self.rng)
        self.status_counts = Counter()
        self.schedule_item_counts = Counter()
        self.transfer_counts = Counter()
        self.schedule_by_year = {}
        self.department_workload = {}
        self.staffing_rules = {}
        self.position_by_department_title = {}
        self.group_by_department_name = {}
        self._paid_source_signature_by_employee = {}
        self._employees_with_saved_allocations = set()
        self._calendar_year_paid_days_cache = {}
        self._active_request_periods_by_employee = {}
        self._active_schedule_item_periods_by_employee = {}
        self.progress_job_id = options.get("progress_job_id")

        previous_sync_state = set_vacation_metric_sync_enabled(False)
        try:
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                1,
                "Подготовка",
                "Запуск полного пересоздания демо-данных.",
                started=True,
            )
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                5,
                "Очистка демо-БД",
                "Удаляются старые демо-сотрудники, графики, заявки и связанные данные.",
            )
            self._reset_demo_data()
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                12,
                "Структура предприятия",
                "Создаются отделы, сотрудники, пользователи и правила состава.",
            )
            departments = self._create_departments()
            self._create_staffing_reference_data(departments)
            enterprise_head = self._create_enterprise_head()
            authorized_person = self._create_authorized_person()
            hr_team = self._create_hr_team(departments[-1])
            department_heads = self._create_department_heads(departments)
            employees = self._create_department_employees(departments)
            self._assign_department_deputies(departments)
            self._assign_enterprise_deputy(hr_team)
            self._create_staffing_rules(departments)
            self._create_department_workload(departments)
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                24,
                "Исторические графики",
                "Создаются архивные и текущие графики отпусков.",
            )
            self._create_historical_schedules(hr_team[0], enterprise_head, authorized_person, departments)

            everyone = [enterprise_head, *hr_team, *department_heads, *employees]
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                34,
                "Пожелания и отпуска",
                "Формируются исторические пожелания и отпуска сотрудников.",
            )
            self._create_vacation_preferences(everyone)
            for index, employee in enumerate(everyone, start=1):
                self._seed_employee_vacations(employee)
                if index % 20 == 0 or index == len(everyone):
                    self._progress(
                        DemoDataResetJob.STATUS_RUNNING,
                        min(49, 34 + int(index / max(len(everyone), 1) * 15)),
                        "Пожелания и отпуска",
                        f"Обработано сотрудников: {index} из {len(everyone)}.",
                    )

            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                52,
                "Нормализация истории",
                "Выравниваются балансы, переносы и календарная история отпусков.",
            )
            self._cancel_unallocatable_paid_sources(everyone)
            self._normalize_calendar_year_leave_history(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._create_balanced_special_request_history(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._normalize_calendar_year_leave_history(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._stabilize_current_calendar_year_leave(everyone)
            self._normalize_planning_year_carryover(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._stabilize_current_calendar_year_leave(everyone)
            self._normalize_short_paid_leave_fragments(everyone)
            self._cleanup_tiny_generated_calendar_year_leaves(everyone)
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                68,
                "Переносы и ручные кейсы",
                "Создаются исторические переносы и демо-кейсы для черновика.",
            )
            self._create_historical_manager_initiated_transfers()
            self._create_pending_current_year_transfers()
            self._create_demo_manual_schedule_draft_cases(everyone)
            self._normalize_historical_schedule_risk_levels()
            self._normalize_demo_historical_staffing_pressure(everyone)
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                78,
                "ML-следы",
                "Создаются generation runs, candidates, packages и feedback для обучения.",
            )
            self.historical_ml_trace_stats = create_historical_schedule_ml_traces(
                self.rng,
                hr_team[0],
                self.schedule_end_year,
            )
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                88,
                "Уведомления",
                "Восстанавливаются уведомления из истории заявок и графиков.",
            )
            self.notification_stats = backfill_notifications_from_history(as_of_date=self.today)
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                92,
                "Начальная точка",
                "Сохраняется снимок для быстрого сброса демо-состояния.",
            )
            self.demo_baseline_snapshot = capture_demo_baseline_snapshot(
                planning_year=self.schedule_end_year + 1,
                seed_value=options["seed_value"],
            )
        finally:
            set_vacation_metric_sync_enabled(previous_sync_state)

        self._progress(
            DemoDataResetJob.STATUS_RUNNING,
            94,
            "Отпускные балансы",
            "Пересчитываются отпускные балансы сотрудников.",
        )
        for index, employee in enumerate(everyone, start=1):
            self._rebuild_employee_leave_ledger(employee)
            if index % 20 == 0 or index == len(everyone):
                self._progress(
                    DemoDataResetJob.STATUS_RUNNING,
                    min(99, 94 + int(index / max(len(everyone), 1) * 5)),
                    "Отпускные балансы",
                    f"Пересчитано сотрудников: {index} из {len(everyone)}.",
                )

        self._write_calendar_leave_audit(everyone)
        self._progress(
            DemoDataResetJob.STATUS_SUCCEEDED,
            100,
            "Готово",
            "Демо-данные пересозданы. Можно войти заново с паролем 1234.",
            finished=True,
        )

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
        self.stdout.write(
            self.style.SUCCESS(
                "Созданы переносы: "
                f"employee_history_approved={self.transfer_counts['employee_historical_approved']}, "
                f"employee_history_rejected={self.transfer_counts['employee_historical_rejected']}, "
                f"employee_current_pending={self.transfer_counts['employee_current_pending']}, "
                f"manager_history_approved={self.transfer_counts['manager_historical_approved']}, "
                f"manager_history_rejected={self.transfer_counts['manager_historical_rejected']}, "
                f"manager_current_pending={self.transfer_counts['manager_current_pending']}"
            )
        )
        if getattr(self, "manual_draft_case_stats", None):
            self.stdout.write(
                self.style.SUCCESS(
                    "Ручные кейсы черновика: "
                    f"urgent_closures={self.manual_draft_case_stats['urgent_closures']}, "
                    f"days={self.manual_draft_case_stats['days']}, "
                    f"employee_review={self.manual_draft_case_stats['employee_review']}"
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Созданы уведомления: "
                f"created={self.notification_stats['notifications_created']}, "
                f"updated={self.notification_stats['notifications_updated']}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Сохранён начальный снимок демо-состояния: "
                f"planning_year={self.demo_baseline_snapshot.planning_year}, "
                f"seed={self.demo_baseline_snapshot.seed_value}"
            )
        )

    def _progress(self, status, progress_percent, stage_label, message, *, started=False, finished=False):
        if not getattr(self, "progress_job_id", None):
            return
        self._last_progress_percent = progress_percent
        update_demo_data_reset_job_progress(
            self.progress_job_id,
            status=status,
            progress_percent=progress_percent,
            stage_label=stage_label,
            message=message,
            error_message="" if status != DemoDataResetJob.STATUS_FAILED else None,
            started=started,
            finished=finished,
        )

    def _write_calendar_leave_audit(self, employees):
        rows = self._calendar_leave_audit_rows(employees)
        adjustment_bits = []
        if getattr(self, "calendar_leave_adjustments", None):
            adjustment_bits = [
                f"добрано_дней={self.calendar_leave_adjustments['top_up_days']}",
                f"добрано_периодов={self.calendar_leave_adjustments['top_up_items']}",
                f"снято_лишних_дней={self.calendar_leave_adjustments['trimmed_days']}",
                f"снято_периодов={self.calendar_leave_adjustments['trimmed_items']}",
                f"отменено_микро={self.calendar_leave_adjustments['cancelled_tiny_items']}",
                f"отменено_коротких={self.calendar_leave_adjustments['cancelled_short_items']}",
            ]
        if getattr(self, "carryover_adjustments", None):
            adjustment_bits.extend(
                [
                    f"перенос_добрано_дней={self.carryover_adjustments['top_up_days']}",
                    f"перенос_добрано_периодов={self.carryover_adjustments['top_up_items']}",
                    f"перенос_не_размещено={self.carryover_adjustments['unplaced_employees']}",
                ]
            )
        self.stdout.write("Аудит календарных отпусков:")
        if adjustment_bits:
            self.stdout.write("  нормализация: " + ", ".join(adjustment_bits))
        for row in rows:
            self.stdout.write(
                "  "
                f"{row['year']}: "
                f"сотрудников={row['employees']}, "
                f"0={row['zero']}, "
                f"1-13={row['small_1_13']}, "
                f"<28={row['under_28']}, "
                f">=52={row['gte_52']}, "
                f">70={row['gt_70']}, "
                f"среднее={row['avg']:.1f}, "
                f"максимум={row['max']}"
            )

    def _calendar_leave_audit_rows(self, employees):
        eligible_employees = [employee for employee in employees if not employee.is_service_account]
        rows = []
        for year in range(self.schedule_start_year, self.schedule_end_year + 1):
            year_end = date(year, 12, 31)
            totals = [
                float(self._calendar_year_paid_schedule_days(employee, year))
                for employee in eligible_employees
                if employee.date_joined <= year_end
            ]
            if not totals:
                continue
            rows.append(
                {
                    "year": year,
                    "employees": len(totals),
                    "zero": sum(1 for total in totals if total == 0),
                    "small_1_13": sum(1 for total in totals if 0 < total < 14),
                    "under_28": sum(1 for total in totals if 0 < total < 28),
                    "gte_52": sum(1 for total in totals if total >= 52),
                    "gt_70": sum(1 for total in totals if total > 70),
                    "avg": sum(totals) / len(totals),
                    "max": max(totals),
                }
            )
        return rows

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

        VacationScheduleCandidateFeedback.objects.all().delete()
        VacationScheduleCandidatePackagePeriod.objects.all().delete()
        VacationScheduleCandidatePackage.objects.all().delete()
        VacationScheduleCandidate.objects.all().delete()
        VacationScheduleGenerationRun.objects.all().delete()
        VacationScheduleChangeRequest.objects.all().delete()
        VacationUrgentClosureRequest.objects.all().delete()
        VacationRequestHistory.objects.all().delete()
        VacationEntitlementAllocation.objects.all().delete()
        VacationEntitlementPeriod.objects.all().delete()
        VacationPreferenceCollection.objects.all().delete()
        VacationPreference.objects.all().delete()
        DepartmentWorkload.objects.all().delete()
        DepartmentStaffingRule.objects.all().delete()
        ProductionGroupSubstitutionRule.objects.all().delete()
        DepartmentCoverageRule.objects.all().delete()
        EmployeePosition.objects.all().delete()
        ProductionGroup.objects.all().delete()
        VacationSchedule.objects.all().delete()
        VacationRequest.objects.all().delete()
        Employees.objects.all().delete()
        Departments.objects.all().delete()

        if user_ids:
            get_user_model().objects.filter(id__in=user_ids).delete()

        self._reset_demo_sequences()

    def _reset_demo_sequences(self):
        models = [
            get_user_model(),
            Notification,
            Departments,
            EmployeePosition,
            Employees,
            ProductionGroup,
            ProductionGroupSubstitutionRule,
            DepartmentCoverageRule,
            DepartmentStaffingRule,
            DepartmentWorkload,
            VacationEntitlementAllocation,
            VacationEntitlementPeriod,
            VacationPreference,
            VacationPreferenceCollection,
            VacationRequest,
            VacationRequestHistory,
            VacationSchedule,
            VacationScheduleAuthorizedApproval,
            VacationScheduleCandidate,
            VacationScheduleCandidateFeedback,
            VacationScheduleCandidatePackage,
            VacationScheduleCandidatePackagePeriod,
            VacationScheduleChangeRequest,
            VacationScheduleDepartmentApproval,
            VacationScheduleEnterpriseApproval,
            VacationScheduleGenerationRun,
            VacationScheduleItem,
            VacationUrgentClosureRequest,
        ]
        sequence_sql = connection.ops.sequence_reset_sql(no_style(), models)
        if not sequence_sql:
            return
        with connection.cursor() as cursor:
            for sql in sequence_sql:
                cursor.execute(sql)

    def _create_departments(self):
        departments = []
        for spec in self.department_specs:
            departments.append(Departments.objects.create(name=spec["name"], date_added=self._department_formation_at(spec)))
        return departments

    def _infer_group_name_for_position(self, position):
        position_lower = position.lower()
        if "руковод" in position_lower or "начальник" in position_lower:
            return "Руководство отдела"
        if "hr" in position_lower or "кадр" in position_lower:
            return "HR и кадровое сопровождение"
        if any(keyword in position_lower for keyword in ["механик", "слесарь", "ремонт", "электромонтер", "диагност"]):
            return "Механики и ремонт"
        if any(keyword in position_lower for keyword in ["инженер", "технолог", "качество", "эколог", "рискам", "охране"]):
            return "Инженеры"
        if any(keyword in position_lower for keyword in ["логист", "диспетчер", "постав", "склад", "цепоч"]):
            return "Логистика"
        if any(keyword in position_lower for keyword in ["финанс", "эконом", "закуп", "бухгалтер", "контракт"]):
            return "Финансы и закупки"
        if any(keyword in position_lower for keyword in ["мастер", "машинист", "оператор", "линии"]):
            return "Производственная смена"
        if any(keyword in position_lower for keyword in ["безопас", "инспектор"]):
            return "Безопасность"
        return "Общая группа"

    def _group_name_for_position(self, department, position_title):
        if department is not None:
            spec = self.department_spec_by_name.get(department.name)
            if spec is not None:
                group_name = spec.get("position_groups", {}).get(position_title)
                if group_name:
                    return group_name
        return self._infer_group_name_for_position(position_title)

    def _get_or_create_production_group(self, department, group_name):
        group_key = (department.id, group_name)
        group = self.group_by_department_name.get(group_key)
        if group is not None:
            return group
        group, _ = ProductionGroup.objects.get_or_create(
            department=department,
            name=group_name,
            defaults={
                "code": group_name.lower().replace(" ", "-"),
                "description": "Создано демо-сидером для расчёта покрытия отпусков.",
            },
        )
        self.group_by_department_name[group_key] = group
        return group

    def _get_or_create_employee_position(self, department, position_title):
        if department is None:
            return None
        position_key = (department.id, position_title)
        position = self.position_by_department_title.get(position_key)
        if position is not None:
            return position
        group = self._get_or_create_production_group(department, self._group_name_for_position(department, position_title))
        position, _ = EmployeePosition.objects.get_or_create(
            department=department,
            title=position_title,
            defaults={"production_group": group},
        )
        self.position_by_department_title[position_key] = position
        return position

    def _create_staffing_reference_data(self, departments):
        for department, spec in zip(departments, self.department_specs):
            titles = [spec["head_position"], *spec["employee_positions"]]
            if department == departments[-1]:
                titles.extend(["HR бизнес-партнер", "Ведущий HR-специалист"])

            for title in titles:
                self._get_or_create_employee_position(department, title)

            positions_by_group = defaultdict(list)
            for position in EmployeePosition.objects.filter(department=department).select_related("production_group"):
                positions_by_group[position.production_group].append(position)

            for group, positions in positions_by_group.items():
                rule_spec = spec.get("coverage_rules", {}).get(group.name)
                if rule_spec is not None:
                    min_staff_required = rule_spec["min"]
                    max_absent = rule_spec["max"]
                    criticality_level = rule_spec["criticality"]
                elif group.name == "Руководство отдела":
                    min_staff_required = 0
                    max_absent = 1
                    criticality_level = 5
                else:
                    expected_count = max(1, round(spec["employee_count"] * len(positions) / max(len(spec["employee_positions"]), 1)))
                    min_staff_required = max(1, round(expected_count * 0.55))
                    max_absent = max(1, expected_count - min_staff_required)
                    criticality_level = spec["staffing_rule"]["criticality_level"]
                DepartmentCoverageRule.objects.update_or_create(
                    department=department,
                    production_group=group,
                    defaults={
                        "min_staff_required": min_staff_required,
                        "max_absent": max_absent,
                        "criticality_level": criticality_level,
                    },
                )

            groups_by_name = {group.name: group for group in positions_by_group}
            for substitution_spec in spec.get("substitution_rules", []):
                source_group = groups_by_name.get(substitution_spec["source"])
                substitute_group = groups_by_name.get(substitution_spec["substitute"])
                if source_group is None or substitute_group is None or source_group == substitute_group:
                    continue
                ProductionGroupSubstitutionRule.objects.update_or_create(
                    department=department,
                    source_group=source_group,
                    substitute_group=substitute_group,
                    defaults={
                        "max_covered_absences": max(1, int(substitution_spec.get("max_covered_absences", 1))),
                    },
                )

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
        preference_comments_by_policy = {
            VacationPreference.REMAINDER_AUTO: [
                "Предпочитает отпуск в период школьных каникул.",
                "Просит не ставить отпуск на время квартальной отчетности.",
                "Желательно совместить отпуск с семейной поездкой.",
                "Предпочитает спокойный период с низкой нагрузкой отдела.",
            ],
            VacationPreference.REMAINDER_APPROVAL: [
                "Основной период важен, остальные дни готов согласовать с руководителем.",
                "Просит отдельно согласовать остаток после оценки загрузки отдела.",
                "Готов обсудить дополнительные дни, если график отдела будет напряженным.",
            ],
            VacationPreference.REMAINDER_DEFER: [
                "Готов перенести остаток при производственной необходимости.",
                "Просит сохранить основной период, остальные дни можно поставить позже.",
                "Резервный период указан на случай конфликта графика.",
            ],
        }
        remainder_policies = [
            VacationPreference.REMAINDER_AUTO,
            VacationPreference.REMAINDER_AUTO,
            VacationPreference.REMAINDER_AUTO,
            VacationPreference.REMAINDER_APPROVAL,
            VacationPreference.REMAINDER_AUTO,
            VacationPreference.REMAINDER_DEFER,
            VacationPreference.REMAINDER_AUTO,
            VacationPreference.REMAINDER_APPROVAL,
            VacationPreference.REMAINDER_AUTO,
            VacationPreference.REMAINDER_DEFER,
        ]
        policy_index = 0
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
                        remainder_policy=VacationPreference.REMAINDER_AUTO,
                        comment=self.rng.choice(["Пожелания не указаны.", "Сотрудник готов принять даты по решению HR."]),
                        created_automatically=True,
                    )
                    continue
                remainder_policy = remainder_policies[policy_index % len(remainder_policies)]
                policy_index += 1
                primary_candidates = (
                    [14, 21, 28]
                    if remainder_policy == VacationPreference.REMAINDER_AUTO
                    else [21, 28, 35, 42]
                )
                primary_duration = self.rng.choice(primary_candidates)
                backup_duration = self.rng.choice(
                    [duration for duration in [14, 21, 28] if duration <= primary_duration] or [14]
                )
                preference_comments = preference_comments_by_policy.get(remainder_policy) or preference_comments_by_policy[
                    VacationPreference.REMAINDER_AUTO
                ]
                used_months = set()
                for priority, duration in [
                    (VacationPreference.PRIORITY_PRIMARY, primary_duration),
                    (VacationPreference.PRIORITY_BACKUP, backup_duration),
                ]:
                    high_load_months = [
                        workload.month
                        for workload in self.department_workload.values()
                        if workload.department_id == employee.department_id
                        and workload.year == year
                        and workload.load_level >= 4
                    ]
                    month_pool = high_load_months if high_load_months and self.rng.random() < 0.36 else [2, 3, 4, 6, 7, 8, 9, 10, 11]
                    month_pool = [month for month in month_pool if month not in used_months] or month_pool
                    month = self.rng.choice(month_pool)
                    used_months.add(month)
                    start_day = self.rng.randint(1, 10)
                    start_date = date(year, month, start_day)
                    end_date = min(start_date + timedelta(days=duration - 1), date(year, 12, 31))
                    VacationPreference.objects.create(
                        employee=employee,
                        year=year,
                        start_date=start_date,
                        end_date=end_date,
                        priority=priority,
                        status=VacationPreference.STATUS_FILLED,
                        remainder_policy=remainder_policy,
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
            recent_hires = min(spec.get("recent_hires", 0), spec["employee_count"])
            recent_hire_start_slot = spec["employee_count"] - recent_hires
            for slot in range(spec["employee_count"]):
                date_joined = self._recent_hire_date(employee_index) if slot >= recent_hire_start_slot else None
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

    def _assign_department_deputies(self, departments):
        for department in departments:
            candidates_query = (
                Employees.objects.filter(
                    department=department,
                    is_active_employee=True,
                )
                .exclude(role__in={Employees.ROLE_DEPARTMENT_HEAD, *Employees.SERVICE_ROLES})
                .order_by("date_joined", "last_name", "first_name")
            )
            if department.head_id:
                candidates_query = candidates_query.exclude(id=department.head_id)

            experienced_candidates = [
                employee
                for employee in candidates_query
                if not is_new_hire(employee, as_of=self.today)
            ]
            if not experienced_candidates:
                raise CommandError(
                    f"В отделе «{department.name}» нет опытного сотрудника для роли заместителя отдела "
                    "(стаж должен быть минимум 6 месяцев)."
                )

            deputy = experienced_candidates[0]
            department.deputy = deputy
            department.save(update_fields=["deputy"])

    def _assign_enterprise_deputy(self, hr_team):
        if not hr_team:
            return
        deputy = hr_team[0]
        deputy.is_enterprise_deputy = True
        deputy.save(update_fields=["is_enterprise_deputy"])

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

        employee_position = self._get_or_create_employee_position(department, position)
        employee = Employees.objects.create(
            login=login,
            last_name=last_name,
            first_name=first_name,
            middle_name=middle_name,
            position=position,
            employee_position=employee_position,
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

    def _normalize_calendar_year_leave_history(self, employees):
        self.calendar_leave_adjustments = Counter()
        for employee in employees:
            if employee.is_service_account:
                continue
            for year in range(self.schedule_start_year, self.schedule_end_year):
                self._normalize_employee_calendar_year_leave(employee, year)

    def _normalize_employee_calendar_year_leave(self, employee, year):
        schedule = self.schedule_by_year.get(year)
        if schedule is None:
            return

        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        if employee.date_joined > year_end:
            return

        eligibility_start = max(year_start, add_months_safe(employee.date_joined, 6))
        paid_days = self._calendar_year_paid_schedule_days(employee, year)
        minimum_days, target_days = self._calendar_year_leave_targets(eligibility_start, year)
        maximum_days = self._calendar_year_leave_maximum(minimum_days)

        if maximum_days and paid_days > maximum_days:
            self._trim_calendar_year_leave(employee, year, maximum_days, minimum_days)
            paid_days = self._calendar_year_paid_schedule_days(employee, year)

        if 0 < paid_days < PARTIAL_YEAR_CALENDAR_MIN_DAYS and minimum_days == 0:
            self._cancel_tiny_calendar_year_leave(employee, year)
            return

        if paid_days < minimum_days:
            self._top_up_calendar_year_leave(
                employee,
                year,
                eligibility_start,
                minimum_days,
                target_days,
                paid_days,
            )
            paid_days = self._calendar_year_paid_schedule_days(employee, year)
            if 0 < paid_days < PARTIAL_YEAR_CALENDAR_MIN_DAYS:
                self._cancel_tiny_calendar_year_leave(employee, year)
            return

        if minimum_days == FULL_YEAR_CALENDAR_MIN_DAYS and paid_days < min(target_days, 42):
            self._top_up_calendar_year_leave(
                employee,
                year,
                eligibility_start,
                minimum_days,
                target_days,
                paid_days,
            )

    def _calendar_year_leave_targets(self, eligibility_start, year):
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        if eligibility_start > year_end:
            return 0, 0
        if eligibility_start <= year_start:
            return FULL_YEAR_CALENDAR_MIN_DAYS, self.rng.choice(FULL_YEAR_CALENDAR_TARGETS)
        if eligibility_start <= date(year, 9, 30):
            return PARTIAL_YEAR_CALENDAR_MIN_DAYS, self.rng.choice(PARTIAL_YEAR_CALENDAR_TARGETS)
        return 0, 0

    def _calendar_year_leave_maximum(self, minimum_days):
        if minimum_days == FULL_YEAR_CALENDAR_MIN_DAYS:
            return max(FULL_YEAR_CALENDAR_TARGETS)
        if minimum_days == PARTIAL_YEAR_CALENDAR_MIN_DAYS:
            return max(PARTIAL_YEAR_CALENDAR_TARGETS)
        return 0

    def _calendar_year_paid_schedule_days(self, employee, year):
        cache_key = (employee.id, year)
        if cache_key in self._calendar_year_paid_days_cache:
            return self._calendar_year_paid_days_cache[cache_key]

        paid_days = sum(
            item.chargeable_days
            for item in employee.vacation_schedule_items.filter(
                schedule__year=year,
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            )
        )
        self._calendar_year_paid_days_cache[cache_key] = paid_days
        return paid_days

    def _adjust_calendar_year_paid_schedule_days(self, employee_id, year, delta_days):
        cache_key = (employee_id, year)
        if cache_key not in self._calendar_year_paid_days_cache:
            return
        self._calendar_year_paid_days_cache[cache_key] = max(
            self._calendar_year_paid_days_cache[cache_key] + delta_days,
            0,
        )

    def _add_schedule_item_to_paid_days_cache(self, item):
        if item.vacation_type != "paid" or item.status not in VacationScheduleItem.BALANCE_STATUSES:
            return
        self._adjust_calendar_year_paid_schedule_days(item.employee_id, item.start_date.year, item.chargeable_days)

    def _remove_schedule_item_from_paid_days_cache(self, item):
        if item.vacation_type != "paid":
            return
        self._adjust_calendar_year_paid_schedule_days(item.employee_id, item.start_date.year, -item.chargeable_days)

    def _active_request_periods_for_employee(self, employee):
        if employee.id not in self._active_request_periods_by_employee:
            active_requests = VacationRequest.objects.filter(
                employee=employee,
                status__in=VacationRequest.ACTIVE_STATUSES,
            )
            active_requests = exclude_converted_paid_requests(active_requests, employee_ids=[employee.id])
            self._active_request_periods_by_employee[employee.id] = list(
                active_requests.values_list("id", "start_date", "end_date")
            )
        return self._active_request_periods_by_employee[employee.id]

    def _active_schedule_item_periods_for_employee(self, employee):
        if employee.id not in self._active_schedule_item_periods_by_employee:
            self._active_schedule_item_periods_by_employee[employee.id] = list(
                VacationScheduleItem.objects.filter(
                    employee=employee,
                    status__in=VacationScheduleItem.ACTIVE_STATUSES,
                ).values_list("id", "start_date", "end_date")
            )
        return self._active_schedule_item_periods_by_employee[employee.id]

    def _add_request_to_active_period_cache(self, request_obj):
        if request_obj.status not in VacationRequest.ACTIVE_STATUSES:
            return
        if request_obj.vacation_type == "paid" and request_obj.status == VacationRequest.STATUS_APPROVED:
            return
        periods = self._active_request_periods_by_employee.get(request_obj.employee_id)
        if periods is not None:
            periods.append((request_obj.id, request_obj.start_date, request_obj.end_date))

    def _remove_request_from_active_period_cache(self, request_obj):
        periods = self._active_request_periods_by_employee.get(request_obj.employee_id)
        if periods is None:
            return
        self._active_request_periods_by_employee[request_obj.employee_id] = [
            period for period in periods if period[0] != request_obj.id
        ]

    def _add_schedule_item_to_active_period_cache(self, item):
        if item.status not in VacationScheduleItem.ACTIVE_STATUSES:
            return
        periods = self._active_schedule_item_periods_by_employee.get(item.employee_id)
        if periods is not None:
            periods.append((item.id, item.start_date, item.end_date))

    def _remove_schedule_item_from_active_period_cache(self, item):
        periods = self._active_schedule_item_periods_by_employee.get(item.employee_id)
        if periods is None:
            return
        self._active_schedule_item_periods_by_employee[item.employee_id] = [
            period for period in periods if period[0] != item.id
        ]

    def _remaining_paid_budget_for_demo(self, employee):
        try:
            ledger_available_days = int(get_employee_leave_summary(employee)["available"])
        except ValidationError:
            return 0
        if ledger_available_days <= 0:
            return 0
        requestable_days = int(get_employee_requestable_leave(employee, self.today))
        scheduled_days = sum(
            item.chargeable_days
            for item in employee.vacation_schedule_items.filter(
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            )
        )
        active_paid_requests = employee.vacation_requests.filter(
            vacation_type="paid",
            status__in=VacationRequest.ACTIVE_STATUSES,
        )
        active_paid_requests = exclude_converted_paid_requests(active_paid_requests, employee_ids=[employee.id])
        request_days = sum(
            get_chargeable_leave_days(request_obj.start_date, request_obj.end_date, request_obj.vacation_type)
            for request_obj in active_paid_requests
        )
        calculated_available_days = max(int(requestable_days - scheduled_days - request_days), 0)
        return min(ledger_available_days, calculated_available_days)

    def _top_up_calendar_year_leave(
        self,
        employee,
        year,
        eligibility_start,
        minimum_days,
        target_days,
        paid_days,
        window_end_override=None,
    ):
        available_budget = self._remaining_paid_budget_for_demo(employee)
        if available_budget < 7:
            return

        window_start = max(eligibility_start, date(year, 1, 1))
        window_end = window_end_override or min(date(year, 12, 20), self.today - timedelta(days=7))
        if window_start > window_end:
            return

        occupied_periods = self._active_periods_for_employee_window(employee, window_start, window_end)
        paid_periods = list(
            employee.vacation_schedule_items.filter(
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
                start_date__lte=window_end,
                end_date__gte=window_start,
            ).values_list("start_date", "end_date")
        )

        current_days = int(paid_days)
        max_year_days = 56 if minimum_days >= FULL_YEAR_CALENDAR_MIN_DAYS else 28
        while available_budget >= 7 and current_days < target_days and current_days < max_year_days:
            if current_days < PARTIAL_YEAR_CALENDAR_MIN_DAYS:
                duration_options = [28, 21, 14]
                min_duration = 14
            elif current_days < minimum_days:
                duration_options = [14, 10, 7]
                min_duration = 7
            else:
                duration_options = [14, 10, 7]
                min_duration = 7

            consumed = 0
            for duration in duration_options:
                if duration < min_duration or duration > available_budget:
                    continue
                if current_days + duration > max_year_days:
                    continue
                consumed = self._create_paid_leave_block(
                    employee,
                    occupied_periods,
                    paid_periods,
                    window_start,
                    window_end,
                    duration=duration,
                    min_gap_days=self.rng.randint(*PAID_OPERATIONAL_GAP_RANGE) if paid_periods else 0,
                )
                if consumed > 0:
                    break

            if consumed <= 0:
                break

            current_days += int(consumed)
            available_budget = max(available_budget - int(consumed), 0)
            self.calendar_leave_adjustments["top_up_days"] += int(consumed)
            self.calendar_leave_adjustments["top_up_items"] += 1

    def _normalize_current_calendar_year_leave(self, employees):
        year = self.schedule_end_year
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 20)
        for employee in employees:
            if employee.is_service_account or employee.date_joined > self.schedule_approval_cutoff:
                continue
            paid_days = self._calendar_year_paid_schedule_days(employee, year)
            if paid_days >= 45:
                continue
            eligibility_start = max(year_start, add_months_safe(employee.date_joined, 6))
            self._top_up_calendar_year_leave(
                employee,
                year,
                eligibility_start,
                45,
                52,
                paid_days,
                window_end_override=year_end,
            )

    def _stabilize_current_calendar_year_leave(self, employees):
        for _ in range(3):
            self._normalize_current_calendar_year_leave(employees)
            self._cancel_unallocatable_paid_sources(employees)

    def _normalize_planning_year_carryover(self, employees):
        eligible_employees = [
            employee
            for employee in employees
            if not employee.is_service_account and employee.date_joined <= self.schedule_approval_cutoff
        ]
        self.carryover_adjustments = Counter()
        carryover_rows = []
        for employee in eligible_employees:
            due_remaining = self._planning_year_due_remaining(employee)
            if due_remaining > PLANNING_YEAR_CARRYOVER_SOFT_CAP:
                carryover_rows.append((employee, due_remaining))

        if not carryover_rows:
            return

        carryover_rows.sort(key=lambda row: (row[1], row[0].date_joined, row[0].id), reverse=True)
        showcase_count = min(
            PLANNING_YEAR_SHOWCASE_COUNT,
            max(1, len(eligible_employees) // 12),
            len(carryover_rows),
        )
        showcase_employee_ids = {employee.id for employee, _due_remaining in carryover_rows[:showcase_count]}

        for employee, _due_remaining in carryover_rows:
            if employee.id in showcase_employee_ids:
                target_cap = Decimal(str(self.rng.randint(PLANNING_YEAR_SHOWCASE_CARRYOVER_MIN, PLANNING_YEAR_SHOWCASE_CARRYOVER_MAX)))
            else:
                target_cap = PLANNING_YEAR_CARRYOVER_SOFT_CAP
            self._reduce_employee_planning_carryover(employee, target_cap, employee.id in showcase_employee_ids)

    def _planning_year_due_remaining(self, employee):
        planning_deadline = date(self.schedule_end_year + 1, 12, 31)
        self._rebuild_employee_leave_ledger(employee, strict=False)
        due_remaining = Decimal("0.00")
        for period in VacationEntitlementPeriod.objects.filter(
            employee=employee,
            must_use_by__lte=planning_deadline,
        ):
            allocated_days = sum(Decimal(allocation.allocated_days) for allocation in period.allocations.all())
            due_remaining += max(Decimal(period.entitled_days) - allocated_days, Decimal("0.00"))
        return due_remaining

    def _oldest_open_planning_entitlement_start(self, employee):
        planning_deadline = date(self.schedule_end_year + 1, 12, 31)
        for period in VacationEntitlementPeriod.objects.filter(
            employee=employee,
            must_use_by__lte=planning_deadline,
        ).order_by("period_start"):
            allocated_days = sum(Decimal(allocation.allocated_days) for allocation in period.allocations.all())
            if Decimal(period.entitled_days) - allocated_days > 0:
                return period.period_start
        return None

    def _reduce_employee_planning_carryover(self, employee, target_cap, is_showcase_employee):
        for _ in range(8):
            due_remaining = self._planning_year_due_remaining(employee)
            excess_days = due_remaining - target_cap
            if excess_days <= 0:
                return

            desired_days = int(min(excess_days, Decimal("28.00")))
            consumed_days = self._create_carryover_top_up_block(
                employee,
                desired_days,
                is_showcase_employee=is_showcase_employee,
            )
            if consumed_days <= 0:
                self.carryover_adjustments["unplaced_employees"] += 1
                return

            self.carryover_adjustments["top_up_days"] += int(consumed_days)
            self.carryover_adjustments["top_up_items"] += 1

    def _create_carryover_top_up_block(self, employee, desired_days, *, is_showcase_employee):
        oldest_open_start = self._oldest_open_planning_entitlement_start(employee)
        if oldest_open_start is None:
            return 0

        year_candidates = []
        preferred_years = [self.schedule_end_year - 1, self.schedule_end_year]
        fallback_years = list(range(max(self.schedule_start_year, oldest_open_start.year), self.schedule_end_year + 1))
        for year in [*preferred_years, *reversed(fallback_years)]:
            if year not in year_candidates and self.schedule_start_year <= year <= self.schedule_end_year:
                year_candidates.append(year)

        for year in year_candidates:
            schedule = self.schedule_by_year.get(year)
            if schedule is None:
                continue

            year_cap = DEMO_CALENDAR_YEAR_SHOWCASE_MAX_DAYS if is_showcase_employee else DEMO_CALENDAR_YEAR_NORMAL_MAX_DAYS
            current_year_days = int(self._calendar_year_paid_schedule_days(employee, year))
            year_room = max(year_cap - current_year_days, 0)
            if year_room < 7:
                continue

            window_start = max(
                date(year, 1, 1),
                oldest_open_start,
                add_months_safe(employee.date_joined, 6),
            )
            if year == self.schedule_end_year:
                window_start = max(window_start, self.today + timedelta(days=21))
            window_end = date(year, 12, 20)
            if year < self.schedule_end_year:
                window_end = min(window_end, self.today - timedelta(days=7))
            if window_start > window_end:
                continue

            has_anchor = self._calendar_year_has_paid_anchor(employee, year)
            duration_options = self._carryover_top_up_duration_options(desired_days, year_room, has_anchor)
            if not duration_options:
                continue

            occupied_periods = self._active_periods_for_employee_window(employee, window_start, window_end)
            paid_periods = list(
                employee.vacation_schedule_items.filter(
                    vacation_type="paid",
                    status__in=VacationScheduleItem.BALANCE_STATUSES,
                ).values_list("start_date", "end_date")
            )
            for duration in duration_options:
                consumed_days = self._create_paid_leave_block(
                    employee,
                    occupied_periods,
                    paid_periods,
                    window_start,
                    window_end,
                    duration=duration,
                    min_gap_days=self.rng.randint(10, 24) if paid_periods else 0,
                    allow_transfer=False,
                )
                if consumed_days > 0:
                    return consumed_days
        return 0

    def _carryover_top_up_duration_options(self, desired_days, year_room, has_anchor):
        options = []
        for duration in (28, 21, 14):
            if duration <= year_room and duration <= max(desired_days + 4, MIN_PAID_LEAVE_ANCHOR_DAYS):
                options.append(duration)
        if has_anchor:
            for duration in (10, 7):
                if duration <= year_room and duration <= max(desired_days + 2, 7):
                    options.append(duration)
        return options

    def _normalize_short_paid_leave_fragments(self, employees):
        for employee in employees:
            if employee.is_service_account:
                continue
            for year in range(self.schedule_start_year, self.schedule_end_year + 1):
                short_items = self._calendar_year_short_paid_items(employee, year)
                if not short_items or self._calendar_year_has_paid_anchor(employee, year):
                    continue

                year_start = date(year, 1, 1)
                year_end = date(year, 12, 31)
                if employee.date_joined > year_end:
                    continue

                eligibility_start = max(year_start, add_months_safe(employee.date_joined, 6))
                minimum_days, target_days = self._calendar_year_leave_targets(eligibility_start, year)
                if minimum_days == 0:
                    self._cancel_short_generated_calendar_year_leaves(employee, year)
                    continue

                paid_days = self._calendar_year_paid_schedule_days(employee, year)
                self._top_up_calendar_year_leave(
                    employee,
                    year,
                    eligibility_start,
                    minimum_days,
                    max(target_days, int(paid_days) + MIN_PAID_LEAVE_ANCHOR_DAYS),
                    paid_days,
                    window_end_override=date(year, 12, 20),
                )
                if not self._calendar_year_has_paid_anchor(employee, year):
                    self._cancel_short_generated_calendar_year_leaves(employee, year)

    def _calendar_year_short_paid_items(self, employee, year):
        items = list(
            employee.vacation_schedule_items.filter(
                schedule__year=year,
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            )
            .order_by("start_date", "id")
        )
        return [item for item in items if self._schedule_item_calendar_days(item) < MIN_PAID_LEAVE_ANCHOR_DAYS]

    def _calendar_year_has_paid_anchor(self, employee, year):
        return any(
            self._schedule_item_calendar_days(item) >= MIN_PAID_LEAVE_ANCHOR_DAYS
            for item in employee.vacation_schedule_items.filter(
                schedule__year=year,
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            )
        )

    def _schedule_item_calendar_days(self, item):
        return (item.end_date - item.start_date).days + 1

    def _cancel_short_generated_calendar_year_leaves(self, employee, year):
        for item in self._calendar_year_short_paid_items(employee, year):
            if self._schedule_item_calendar_days(item) >= MIN_PAID_LEAVE_ANCHOR_DAYS:
                continue
            if (
                item.source != VacationScheduleItem.SOURCE_GENERATED
                or item.previous_item_id is not None
                or item.created_from_change_request_id is not None
                or item.change_requests.exists()
            ):
                continue
            item.status = VacationScheduleItem.STATUS_CANCELLED
            item.manager_comment = "Отменено при нормализации демо-истории отпусков."
            item.save(update_fields=["status", "manager_comment"])
            self._remove_schedule_item_from_paid_days_cache(item)
            self._remove_schedule_item_from_active_period_cache(item)
            self.calendar_leave_adjustments["cancelled_short_items"] += 1

    def _create_demo_manual_schedule_draft_cases(self, employees):
        planning_year = self.schedule_end_year + 1
        hr_actor = next((employee for employee in employees if employee.role == Employees.ROLE_HR), None)
        self.manual_draft_case_stats = Counter()
        if hr_actor is None:
            return

        candidates = sorted(
            (
                employee
                for employee in employees
                if (
                    employee.role == Employees.ROLE_EMPLOYEE
                    and not employee.is_service_account
                    and employee.is_active_employee
                    and employee.date_joined <= date(planning_year - 1, 6, 30)
                )
            ),
            key=lambda employee: (
                employee.department_id or 0,
                employee.full_name,
                employee.id,
            ),
        )
        selected_employee_ids = set()
        selected_department_ids = set()
        deadlines = [date(planning_year, 1, 3), date(planning_year, 1, 10)]

        for index, required_days in enumerate(DEMO_MANUAL_DRAFT_CASE_SHORTAGE_DAYS[:DEMO_MANUAL_DRAFT_CASE_COUNT]):
            deadline = deadlines[index % len(deadlines)]
            closure_request = self._create_one_demo_manual_schedule_draft_case(
                candidates,
                planning_year=planning_year,
                required_days=Decimal(required_days),
                deadline=deadline,
                actor=hr_actor,
                selected_employee_ids=selected_employee_ids,
                selected_department_ids=selected_department_ids,
                prefer_new_department=True,
            )
            if closure_request is None:
                closure_request = self._create_one_demo_manual_schedule_draft_case(
                    candidates,
                    planning_year=planning_year,
                    required_days=Decimal(required_days),
                    deadline=deadline,
                    actor=hr_actor,
                    selected_employee_ids=selected_employee_ids,
                    selected_department_ids=selected_department_ids,
                    prefer_new_department=False,
                )
            if closure_request is None:
                continue

            selected_employee_ids.add(closure_request.employee_id)
            if closure_request.employee.department_id:
                selected_department_ids.add(closure_request.employee.department_id)
            self.manual_draft_case_stats["urgent_closures"] += 1
            self.manual_draft_case_stats["days"] += int(closure_request.required_days)
            self.manual_draft_case_stats[f"deadline_{closure_request.deadline:%m_%d}"] += 1

            if index == 1:
                reviewer = get_expected_vacation_approver(closure_request.employee).employee
                try:
                    approve_urgent_closure_by_manager(
                        closure_request.id,
                        reviewer=reviewer,
                        comment="Демо: руководитель подтвердил период, ожидается ответ сотрудника.",
                    )
                    self.manual_draft_case_stats["employee_review"] += 1
                except ValidationError:
                    pass

    def _create_one_demo_manual_schedule_draft_case(
        self,
        candidates,
        *,
        planning_year,
        required_days,
        deadline,
        actor,
        selected_employee_ids,
        selected_department_ids,
        prefer_new_department,
    ):
        for employee in candidates:
            if employee.id in selected_employee_ids:
                continue
            if prefer_new_department and employee.department_id in selected_department_ids:
                continue
            if VacationUrgentClosureRequest.objects.filter(
                employee=employee,
                planning_year=planning_year,
                status__in=VacationUrgentClosureRequest.ACTIVE_STATUSES,
            ).exists():
                continue

            options = build_urgent_closure_options(employee, planning_year, required_days, deadline)
            safe_options = [
                option
                for option in options
                if option["can_submit"] and not option["risk_is_conflict"] and option["risk_level"] != VacationRequest.RISK_HIGH
            ]
            if not safe_options:
                safe_options = [option for option in options if option["can_submit"] and not option["risk_is_conflict"]]
            if not safe_options:
                safe_options = [option for option in options if option["can_submit"]]
            if not safe_options:
                continue

            option = safe_options[0]
            try:
                return create_urgent_closure_request(
                    employee=employee,
                    planning_year=planning_year,
                    required_days=required_days,
                    deadline=deadline,
                    start_date=option["start_date"],
                    end_date=option["end_date"],
                    actor=actor,
                    reason=(
                        "Демо-кейс: небольшой срочный остаток прошлого года нужно согласовать "
                        f"до начала графика {planning_year} года."
                    ),
                )
            except ValidationError:
                continue
        return None

    def _cancel_tiny_calendar_year_leave(self, employee, year):
        tiny_items = list(
            employee.vacation_schedule_items.filter(
                schedule__year=year,
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
                source=VacationScheduleItem.SOURCE_GENERATED,
                previous_item__isnull=True,
                created_from_change_request__isnull=True,
                change_requests__isnull=True,
            )
        )
        if not tiny_items:
            return
        for item in tiny_items:
            item.status = VacationScheduleItem.STATUS_CANCELLED
            item.manager_comment = "Отменено при нормализации демо-истории отпусков."
            item.save(update_fields=["status", "manager_comment"])
            self._remove_schedule_item_from_paid_days_cache(item)
            self._remove_schedule_item_from_active_period_cache(item)
            self.calendar_leave_adjustments["cancelled_tiny_items"] += 1

    def _cleanup_tiny_generated_calendar_year_leaves(self, employees):
        for employee in employees:
            if employee.is_service_account:
                continue
            for year in range(self.schedule_start_year, self.schedule_end_year):
                paid_days = self._calendar_year_paid_schedule_days(employee, year)
                if 0 < paid_days < PARTIAL_YEAR_CALENDAR_MIN_DAYS:
                    self._cancel_tiny_calendar_year_leave(employee, year)

    def _normalize_demo_historical_staffing_pressure(self, employees):
        eligible_employees = [employee for employee in employees if not employee.is_service_account]
        absence_dates_by_employee = {
            employee.id: self._employee_active_absence_dates(employee)
            for employee in eligible_employees
        }
        self._assign_low_conflict_department_deputies(absence_dates_by_employee)
        self._assign_low_conflict_enterprise_deputy(absence_dates_by_employee)
        self._reduce_department_leadership_overlaps()
        self._relax_staffing_limits_to_seeded_absences(eligible_employees)

    def _reduce_department_leadership_overlaps(self):
        for department in Departments.objects.select_related("head", "deputy"):
            head = department.head
            deputy = department.deputy
            if head is None or deputy is None:
                continue
            if is_new_hire(deputy, as_of=self.today):
                continue
            head_absences = self._employee_active_absence_dates(head)
            if not head_absences:
                continue
            overlap_items = list(
                deputy.vacation_schedule_items.filter(
                    schedule__year__lt=self.schedule_end_year,
                    status__in=VacationScheduleItem.BALANCE_STATUSES,
                    vacation_type="paid",
                    source=VacationScheduleItem.SOURCE_GENERATED,
                    previous_item__isnull=True,
                    created_from_change_request__isnull=True,
                    change_requests__isnull=True,
                ).order_by("-chargeable_days", "-start_date", "id")
            )
            for item in overlap_items:
                item_dates = set(iterate_dates(item.start_date, item.end_date))
                if not item_dates & head_absences:
                    continue
                minimum_days = self._calendar_year_minimum_days(deputy, item.schedule.year)
                current_days = self._calendar_year_paid_schedule_days(deputy, item.schedule.year)
                if current_days - item.chargeable_days < minimum_days:
                    continue
                item.status = VacationScheduleItem.STATUS_CANCELLED
                item.manager_comment = "Отменено при нормализации пересечений руководителя и заместителя отдела."
                item.save(update_fields=["status", "manager_comment"])
                self._remove_schedule_item_from_paid_days_cache(item)
                self._remove_schedule_item_from_active_period_cache(item)
                head_absences = self._employee_active_absence_dates(head)

    def _calendar_year_minimum_days(self, employee, year):
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        if employee.date_joined > year_end:
            return 0
        eligibility_start = max(year_start, add_months_safe(employee.date_joined, 6))
        if eligibility_start > year_end:
            return 0
        if eligibility_start <= year_start:
            return FULL_YEAR_CALENDAR_MIN_DAYS
        if eligibility_start <= date(year, 9, 30):
            return PARTIAL_YEAR_CALENDAR_MIN_DAYS
        return 0

    def _normalize_historical_schedule_risk_levels(self):
        for year in range(self.schedule_start_year, self.schedule_end_year):
            historical_items = VacationScheduleItem.objects.filter(
                schedule__year=year,
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            )
            total_count = historical_items.count()
            if total_count <= 0:
                continue
            max_high_risk_count = max(1, round(total_count * 0.05))
            high_risk_items = list(
                historical_items.filter(risk_level=VacationScheduleItem.RISK_HIGH).order_by(
                    "-risk_score",
                    "start_date",
                    "employee_id",
                    "id",
                )
            )
            for item in high_risk_items[max_high_risk_count:]:
                item.risk_score = min(item.risk_score, 62)
                item.risk_level = VacationScheduleItem.RISK_MEDIUM
                item.save(update_fields=["risk_score", "risk_level"])

    def _employee_active_absence_dates(self, employee):
        period_start = date(self.schedule_start_year, 1, 1)
        period_end = date(self.schedule_end_year, 12, 31)
        absence_dates = set()
        schedule_items = employee.vacation_schedule_items.filter(
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__lte=period_end,
            end_date__gte=period_start,
        )
        for item in schedule_items:
            clipped_start = max(item.start_date, period_start)
            clipped_end = min(item.end_date, period_end)
            absence_dates.update(iterate_dates(clipped_start, clipped_end))

        active_requests = employee.vacation_requests.filter(
            status__in=VacationRequest.ACTIVE_STATUSES,
            start_date__lte=period_end,
            end_date__gte=period_start,
        )
        active_requests = exclude_converted_paid_requests(
            active_requests,
            employee_ids=[employee.id],
            start_date=period_start,
            end_date=period_end,
        )
        for request_obj in active_requests:
            clipped_start = max(request_obj.start_date, period_start)
            clipped_end = min(request_obj.end_date, period_end)
            absence_dates.update(iterate_dates(clipped_start, clipped_end))
        return absence_dates

    def _assign_low_conflict_department_deputies(self, absence_dates_by_employee):
        for department in Departments.objects.select_related("head").all():
            head = department.head
            if head is None:
                continue
            head_absences = absence_dates_by_employee.get(head.id, set())
            candidates_query = (
                Employees.objects.filter(
                    department=department,
                    is_active_employee=True,
                )
                .exclude(id=head.id)
                .exclude(role__in={Employees.ROLE_DEPARTMENT_HEAD, *Employees.SERVICE_ROLES})
            )
            candidates = [
                employee
                for employee in candidates_query
                if not is_new_hire(employee, as_of=self.today)
            ]
            if not candidates:
                raise CommandError(
                    f"В отделе «{department.name}» нет опытного сотрудника для роли заместителя отдела "
                    "(стаж должен быть минимум 6 месяцев)."
                )
            deputy = min(
                candidates,
                key=lambda employee: (
                    len(head_absences & absence_dates_by_employee.get(employee.id, set())),
                    len(absence_dates_by_employee.get(employee.id, set())),
                    employee.date_joined,
                    employee.last_name,
                    employee.first_name,
                    employee.id,
                ),
            )
            if department.deputy_id != deputy.id:
                department.deputy = deputy
                department.save(update_fields=["deputy"])

    def _assign_low_conflict_enterprise_deputy(self, absence_dates_by_employee):
        enterprise_head = Employees.objects.filter(
            role=Employees.ROLE_ENTERPRISE_HEAD,
            is_active_employee=True,
        ).order_by("id").first()
        if enterprise_head is None:
            return

        head_absences = absence_dates_by_employee.get(enterprise_head.id, set())
        candidates = list(
            Employees.objects.filter(
                role__in=[Employees.ROLE_HR, Employees.ROLE_DEPARTMENT_HEAD],
                is_active_employee=True,
            ).exclude(id=enterprise_head.id)
        )
        if not candidates:
            return
        deputy = min(
            candidates,
            key=lambda employee: (
                len(head_absences & absence_dates_by_employee.get(employee.id, set())),
                0 if employee.role == Employees.ROLE_HR else 1,
                len(absence_dates_by_employee.get(employee.id, set())),
                employee.last_name,
                employee.first_name,
                employee.id,
            ),
        )
        Employees.objects.filter(is_enterprise_deputy=True).exclude(id=deputy.id).update(is_enterprise_deputy=False)
        if not deputy.is_enterprise_deputy:
            deputy.is_enterprise_deputy = True
            deputy.save(update_fields=["is_enterprise_deputy"])

    def _relax_staffing_limits_to_seeded_absences(self, employees):
        department_absences = defaultdict(set)
        group_absences = defaultdict(set)
        group_staff = defaultdict(set)
        employee_by_id = {employee.id: employee for employee in employees}
        period_start = date(self.schedule_start_year, 1, 1)
        period_end = date(self.schedule_end_year, 12, 31)

        for employee in employees:
            if employee.department_id is None:
                continue
            group_id = employee.employee_position.production_group_id if employee.employee_position_id else None
            if group_id is not None:
                group_staff[group_id].add(employee.id)

            schedule_items = employee.vacation_schedule_items.filter(
                status__in=VacationScheduleItem.ACTIVE_STATUSES,
                start_date__lte=period_end,
                end_date__gte=period_start,
            )
            active_requests = employee.vacation_requests.filter(
                status__in=VacationRequest.ACTIVE_STATUSES,
                start_date__lte=period_end,
                end_date__gte=period_start,
            )
            active_requests = exclude_converted_paid_requests(
                active_requests,
                employee_ids=[employee.id],
                start_date=period_start,
                end_date=period_end,
            )
            absence_periods = [(item.start_date, item.end_date) for item in schedule_items]
            absence_periods.extend((request.start_date, request.end_date) for request in active_requests)
            for start_date, end_date in absence_periods:
                clipped_start = max(start_date, period_start)
                clipped_end = min(end_date, period_end)
                for current_date in iterate_dates(clipped_start, clipped_end):
                    department_absences[(employee.department_id, current_date)].add(employee.id)
                    if group_id is not None:
                        group_absences[(group_id, current_date)].add(employee.id)

        for workload in DepartmentWorkload.objects.filter(year__gte=self.schedule_start_year, year__lte=self.schedule_end_year):
            month_start = date(workload.year, workload.month, 1)
            month_end = self._month_end_date(workload.year, workload.month)
            active_count = self._department_active_count_on(workload.department, month_end)
            peak_absent = 0
            for current_date in iterate_dates(month_start, month_end):
                peak_absent = max(peak_absent, len(department_absences.get((workload.department_id, current_date), set())))
            if active_count <= 0:
                continue
            new_max_absent = max(workload.max_absent, peak_absent)
            new_min_staff = max(1, min(workload.min_staff_required, max(active_count - peak_absent, 1)))
            staffing_rule = self.staffing_rules.get(workload.department_id) or getattr(workload.department, "staffing_rule", None)
            if staffing_rule is not None and staffing_rule.max_absent < new_max_absent:
                staffing_rule.max_absent = new_max_absent
                staffing_rule.save(update_fields=["max_absent"])
                self.staffing_rules[workload.department_id] = staffing_rule
            if workload.max_absent != new_max_absent or workload.min_staff_required != new_min_staff:
                workload.max_absent = new_max_absent
                workload.min_staff_required = new_min_staff
                workload.save(update_fields=["max_absent", "min_staff_required"])
                self.department_workload[(workload.department_id, workload.year, workload.month)] = workload

        for coverage_rule in DepartmentCoverageRule.objects.select_related("production_group"):
            staff_ids = group_staff.get(coverage_rule.production_group_id, set())
            if not staff_ids:
                continue
            peak_absent = 0
            min_present = len(staff_ids)
            for (group_id, _current_date), absent_ids in group_absences.items():
                if group_id == coverage_rule.production_group_id:
                    peak_absent = max(peak_absent, len(absent_ids))
                    active_staff_count = sum(
                        1
                        for employee_id in staff_ids
                        if employee_by_id[employee_id].date_joined <= _current_date
                    )
                    min_present = min(min_present, max(active_staff_count - len(absent_ids), 0))
            group_size = len(staff_ids)
            new_max_absent = max(coverage_rule.max_absent, min(peak_absent, group_size))
            new_min_staff = min(coverage_rule.min_staff_required, min_present)
            if coverage_rule.max_absent != new_max_absent or coverage_rule.min_staff_required != new_min_staff:
                coverage_rule.max_absent = new_max_absent
                coverage_rule.min_staff_required = new_min_staff
                coverage_rule.save(update_fields=["max_absent", "min_staff_required"])

    def _trim_calendar_year_leave(self, employee, year, maximum_days, floor_days):
        active_items = list(
            employee.vacation_schedule_items.filter(
                schedule__year=year,
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
                source=VacationScheduleItem.SOURCE_GENERATED,
                previous_item__isnull=True,
                created_from_change_request__isnull=True,
                change_requests__isnull=True,
            ).order_by("chargeable_days", "-start_date")
        )
        if not active_items:
            return

        current_days = sum(item.chargeable_days for item in active_items)
        for item in active_items:
            if current_days <= maximum_days:
                break
            if current_days - item.chargeable_days < floor_days:
                continue
            item.status = VacationScheduleItem.STATUS_CANCELLED
            item.manager_comment = "Отменено при нормализации демо-истории отпусков."
            item.save(update_fields=["status", "manager_comment"])
            self._remove_schedule_item_from_paid_days_cache(item)
            self._remove_schedule_item_from_active_period_cache(item)
            current_days -= item.chargeable_days
            self.calendar_leave_adjustments["trimmed_days"] += int(item.chargeable_days)
            self.calendar_leave_adjustments["trimmed_items"] += 1

    def _active_periods_for_employee_window(self, employee, window_start, window_end):
        schedule_items = employee.vacation_schedule_items.filter(
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__lte=window_end,
            end_date__gte=window_start,
        )
        occupied_periods = list(schedule_items.values_list("start_date", "end_date"))
        active_requests = employee.vacation_requests.filter(
            status__in=VacationRequest.ACTIVE_STATUSES,
            start_date__lte=window_end,
            end_date__gte=window_start,
        )
        active_requests = exclude_converted_paid_requests(
            active_requests,
            employee_ids=[employee.id],
            start_date=window_start,
            end_date=window_end,
        )
        occupied_periods.extend(active_requests.values_list("start_date", "end_date"))
        return occupied_periods

    def _cancel_unallocatable_paid_sources(self, employees):
        source_signatures = self._active_paid_source_signatures(employees)
        for employee in employees:
            if employee.is_service_account:
                continue
            source_signature = source_signatures.get(employee.id, ((), ()))
            if self._paid_source_signature_by_employee.get(employee.id) == source_signature:
                self._delete_saved_allocations(employee)
                continue

            self._rebuild_employee_leave_ledger(employee, strict=False)
            for item in employee.vacation_schedule_items.filter(
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            ):
                allocated_days = sum(allocation.allocated_days for allocation in item.entitlement_allocations.all())
                if allocated_days < item.chargeable_days:
                    item.status = VacationScheduleItem.STATUS_CANCELLED
                    item.manager_comment = "Отменено при сверке отпускных прав по рабочим годам."
                    item.save(update_fields=["status", "manager_comment"])
                    self._remove_schedule_item_from_paid_days_cache(item)
                    self._remove_schedule_item_from_active_period_cache(item)

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
                    self._remove_request_from_active_period_cache(request_obj)
                    rebuild_vacation_request_history(request_obj)

            self._delete_saved_allocations(employee)
            self._paid_source_signature_by_employee[employee.id] = self._active_paid_source_signature(employee)

    def _rebuild_employee_leave_ledger(self, employee, **kwargs):
        periods = rebuild_employee_leave_ledger(employee, **kwargs)
        if employee is not None:
            self._employees_with_saved_allocations.add(employee.id)
        return periods

    def _delete_saved_allocations(self, employee):
        if employee.id not in self._employees_with_saved_allocations:
            return
        VacationEntitlementAllocation.objects.filter(employee=employee).delete()
        self._employees_with_saved_allocations.discard(employee.id)

    def _active_paid_source_signatures(self, employees):
        employee_ids = [employee.id for employee in employees if not employee.is_service_account]
        signatures = {employee_id: ([], []) for employee_id in employee_ids}
        if not employee_ids:
            return {}

        schedule_rows = VacationScheduleItem.objects.filter(
            employee_id__in=employee_ids,
            vacation_type="paid",
            status__in=VacationScheduleItem.BALANCE_STATUSES,
        ).order_by("employee_id", "id").values_list(
            "employee_id",
            "id",
            "status",
            "start_date",
            "end_date",
            "chargeable_days",
        )
        for employee_id, item_id, status, start_date, end_date, chargeable_days in schedule_rows:
            signatures[employee_id][0].append((item_id, status, start_date, end_date, chargeable_days))

        active_paid_requests = VacationRequest.objects.filter(
            employee_id__in=employee_ids,
            vacation_type="paid",
            status__in=VacationRequest.ACTIVE_STATUSES,
        )
        active_paid_requests = exclude_converted_paid_requests(active_paid_requests, employee_ids=employee_ids)
        request_rows = active_paid_requests.order_by("employee_id", "id").values_list(
            "employee_id",
            "id",
            "status",
            "start_date",
            "end_date",
            "vacation_type",
        )
        for employee_id, request_id, status, start_date, end_date, vacation_type in request_rows:
            signatures[employee_id][1].append((request_id, status, start_date, end_date, vacation_type))

        return {
            employee_id: (tuple(schedule_items), tuple(requests))
            for employee_id, (schedule_items, requests) in signatures.items()
        }

    def _active_paid_source_signature(self, employee):
        schedule_items = tuple(
            employee.vacation_schedule_items.filter(
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            )
            .order_by("id")
            .values_list("id", "status", "start_date", "end_date", "chargeable_days")
        )
        active_paid_requests = employee.vacation_requests.filter(
            vacation_type="paid",
            status__in=VacationRequest.ACTIVE_STATUSES,
        )
        active_paid_requests = exclude_converted_paid_requests(active_paid_requests, employee_ids=[employee.id])
        requests = tuple(
            active_paid_requests.order_by("id").values_list("id", "status", "start_date", "end_date", "vacation_type")
        )
        return schedule_items, requests

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
        if year_budget < 7:
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
        while budget_left >= 7 and extras_created < extras_allowed:
            consumed = 0
            for extra_duration in [duration for duration in [14, 10, 7] if duration <= budget_left]:
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
        variants = [14, 10, 7]
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
        allow_transfer=True,
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
        if allow_transfer and self._should_create_transfer(employee, start_date):
            replacement_slot = self._find_transfer_slot(occupied_periods, start_date, end_date, duration)
            if replacement_slot is not None:
                new_start_date, new_end_date = replacement_slot
                original_item = self._create_schedule_item(
                    employee,
                    schedule,
                    start_date,
                    end_date,
                    VacationScheduleItem.STATUS_APPROVED,
                    VacationScheduleItem.SOURCE_GENERATED,
                    chargeable_days,
                )
                if original_item is None:
                    return 0
                status = (
                    VacationScheduleChangeRequest.STATUS_REJECTED
                    if self.rng.random() < 0.18
                    else VacationScheduleChangeRequest.STATUS_APPROVED
                )
                _, replacement_item = self._create_historical_transfer_request(
                    original_item,
                    new_start_date,
                    new_end_date,
                    requested_by=employee,
                    status=status,
                    reason_choices=[
                        "Семейные обстоятельства.",
                        "Производственная необходимость.",
                        "Корректировка графика отдела.",
                        "Перенос по согласованию сторон.",
                    ],
                )
                if replacement_item is not None:
                    occupied_periods.append((new_start_date, new_end_date))
                    paid_periods.append((new_start_date, new_end_date))
                    return replacement_item.chargeable_days
                occupied_periods.append((start_date, end_date))
                paid_periods.append((start_date, end_date))
                return chargeable_days

        item = self._create_schedule_item(
            employee,
            schedule,
            start_date,
            end_date,
            VacationScheduleItem.STATUS_APPROVED,
            VacationScheduleItem.SOURCE_GENERATED,
            chargeable_days,
        )
        if item is None:
            return 0
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

    def _schedule_load_risk_boost(self, load_level):
        return {
            1: 0,
            2: 4,
            3: 8,
            4: 14,
            5: 20,
        }.get(load_level, 8)

    def _calculate_schedule_risk(self, employee, start_date):
        if employee.department_id is None:
            base_score = 42 if employee.role == Employees.ROLE_ENTERPRISE_HEAD else 25
            return base_score, self._risk_level_for_score(base_score), None

        workload = self.department_workload.get((employee.department_id, start_date.year, start_date.month))
        load_level = workload.load_level if workload is not None else 3
        is_historical_schedule = start_date.year < self.schedule_end_year
        if is_historical_schedule:
            role_boost = 6 if employee.role == Employees.ROLE_DEPARTMENT_HEAD else 0
            random_boost = self.rng.randint(0, 8)
            demo_spike_boost = self.rng.choices([0, 8, 14], weights=[90, 8, 2], k=1)[0]
            risk_score = min(
                68,
                8
                + self._schedule_load_risk_boost(load_level)
                + role_boost
                + random_boost
                + demo_spike_boost,
            )
            if load_level >= 4 and self.rng.random() < 0.02:
                risk_score = self.rng.randint(70, 78)
        else:
            role_boost = 8 if employee.role == Employees.ROLE_DEPARTMENT_HEAD else 0
            random_boost = self.rng.randint(0, 8)
            demo_spike_boost = self.rng.choices([0, 8, 14], weights=[88, 9, 3], k=1)[0]
            risk_score = min(
                82,
                10
                + self._schedule_load_risk_boost(load_level)
                + role_boost
                + random_boost
                + demo_spike_boost,
            )
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
        if status in VacationScheduleItem.ACTIVE_STATUSES and (
            self._active_request_overlap_exists(employee, start_date, end_date)
            or self._active_schedule_item_overlap_exists(employee, start_date, end_date)
        ):
            return None

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
        self._add_schedule_item_to_paid_days_cache(item)
        self._add_schedule_item_to_active_period_cache(item)
        return item

    def _reviewer_for_employee(self, employee):
        return get_expected_vacation_approver(employee).employee

    def _historical_transfer_timeline(self, original_item):
        old_start_date = original_item.start_date
        target_created_date = old_start_date - timedelta(days=self.rng.randint(14, 42))
        if original_item.schedule.approved_at:
            schedule_floor = original_item.schedule.approved_at.date() + timedelta(days=1)
            target_created_date = max(target_created_date, schedule_floor)
        if target_created_date >= old_start_date - timedelta(days=1):
            target_created_date = old_start_date - timedelta(days=3)
        review_date = min(
            target_created_date + timedelta(days=self.rng.randint(1, 5)),
            old_start_date - timedelta(days=1),
        )
        if review_date <= target_created_date:
            review_date = target_created_date + timedelta(days=1)
        return (
            self._make_aware_datetime(target_created_date, 9, self.rng.choice([0, 15, 30])),
            self._make_aware_datetime(review_date, 15, self.rng.choice([0, 20, 40])),
        )

    def _record_transfer_count(self, change_request):
        is_manager_initiated = (
            change_request.requested_by_id is not None
            and change_request.requested_by_id != change_request.employee_id
        )
        is_current_year = change_request.schedule_item.schedule.year == self.schedule_end_year
        origin_key = "manager" if is_manager_initiated else "employee"
        period_key = "current" if is_current_year else "historical"
        status_key = change_request.status
        self.transfer_counts[f"{origin_key}_{period_key}_{status_key}"] += 1

    def _create_historical_transfer_request(
        self,
        original_item,
        new_start_date,
        new_end_date,
        *,
        requested_by,
        status,
        reason_choices,
    ):
        if not can_initiate_schedule_change_for_item(requested_by, original_item):
            return None, None
        employee = original_item.employee
        is_manager_initiated = requested_by.id != employee.id
        reviewer = employee if is_manager_initiated else self._reviewer_for_employee(employee)
        if reviewer is None:
            return None, None

        risk_payload = calculate_schedule_change_risk(original_item, new_start_date, new_end_date)
        created_at, reviewed_at = self._historical_transfer_timeline(original_item)
        if (
            status == VacationScheduleChangeRequest.STATUS_APPROVED
            and (
                self._active_request_overlap_exists(employee, new_start_date, new_end_date)
                or self._active_schedule_item_overlap_exists(
                    employee,
                    new_start_date,
                    new_end_date,
                    exclude_item=original_item,
                )
            )
        ):
            status = VacationScheduleChangeRequest.STATUS_REJECTED
        is_approved = status == VacationScheduleChangeRequest.STATUS_APPROVED
        change_request = VacationScheduleChangeRequest.objects.create(
            schedule_item=original_item,
            employee=employee,
            old_start_date=original_item.start_date,
            old_end_date=original_item.end_date,
            new_start_date=new_start_date,
            new_end_date=new_end_date,
            reason=self.rng.choice(reason_choices),
            status=status,
            requested_by=requested_by,
            reviewed_by=reviewer,
            review_comment=(
                self.rng.choice([
                    "Перенос принят сотрудником.",
                    "Предложение согласовано, новый период подходит.",
                ])
                if is_manager_initiated and is_approved
                else self.rng.choice([
                    "Предложение отклонено сотрудником.",
                    "Сотрудник оставил исходный период отпуска.",
                ])
                if is_manager_initiated
                else "Перенос согласован."
                if is_approved
                else "Период признан рискованным для отдела."
            ),
            reviewed_at=reviewed_at,
            **risk_payload,
        )
        VacationScheduleChangeRequest.objects.filter(pk=change_request.pk).update(created_at=created_at)
        change_request.created_at = created_at
        replacement_item = None

        if is_approved:
            original_item.status = VacationScheduleItem.STATUS_TRANSFERRED
            original_item.was_changed_by_manager = True
            original_item.manager_comment = (
                "Перенесено по принятому предложению руководителя."
                if is_manager_initiated
                else "Перенесено по согласованному запросу сотрудника."
            )
            original_item.save(update_fields=["status", "was_changed_by_manager", "manager_comment"])
            self._remove_schedule_item_from_paid_days_cache(original_item)
            self._remove_schedule_item_from_active_period_cache(original_item)
            self.schedule_item_counts[VacationScheduleItem.STATUS_APPROVED] -= 1
            self.schedule_item_counts[VacationScheduleItem.STATUS_TRANSFERRED] += 1

            replacement_item = VacationScheduleItem.objects.create(
                schedule=original_item.schedule,
                employee=employee,
                start_date=new_start_date,
                end_date=new_end_date,
                vacation_type=original_item.vacation_type,
                chargeable_days=get_chargeable_leave_days(new_start_date, new_end_date, original_item.vacation_type),
                status=VacationScheduleItem.STATUS_APPROVED,
                source=VacationScheduleItem.SOURCE_TRANSFER,
                risk_score=risk_payload["risk_score"],
                risk_level=risk_payload["risk_level"],
                generated_by_ai=True,
                was_changed_by_manager=True,
                manager_comment=(
                    "Создано после принятия предложения переноса."
                    if is_manager_initiated
                    else "Создано после согласования переноса."
                ),
                previous_item=original_item,
                created_from_change_request=change_request,
            )
            self.schedule_item_counts[VacationScheduleItem.STATUS_APPROVED] += 1
            self._add_schedule_item_to_paid_days_cache(replacement_item)
            self._add_schedule_item_to_active_period_cache(replacement_item)
        else:
            original_item.status = VacationScheduleItem.STATUS_APPROVED
            original_item.was_changed_by_manager = False
            original_item.manager_comment = "Историческая запись графика отпусков."
            original_item.save(update_fields=["status", "was_changed_by_manager", "manager_comment"])

        self._record_transfer_count(change_request)
        return change_request, replacement_item

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
            self.transfer_counts["employee_current_pending"] += 1
            created_count += 1
        self._create_manager_initiated_current_year_transfers(current_year)

    def _active_periods_for_employee_year(self, employee, year, exclude_item=None):
        schedule_items = VacationScheduleItem.objects.filter(
            employee=employee,
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__year=year,
        )
        if exclude_item is not None:
            schedule_items = schedule_items.exclude(pk=exclude_item.pk)
        occupied_periods = list(schedule_items.values_list("start_date", "end_date"))
        active_requests = VacationRequest.objects.filter(
            employee=employee,
            status__in=VacationRequest.ACTIVE_STATUSES,
            start_date__year=year,
        )
        active_requests = exclude_converted_paid_requests(
            active_requests,
            employee_ids=[employee.id],
            start_date=date(year, 1, 1),
            end_date=date(year, 12, 31),
        )
        occupied_periods.extend(active_requests.values_list("start_date", "end_date"))
        return occupied_periods

    def _find_transfer_slot_for_item(self, item, *, min_shift_days=21, latest_start_month_day=(10, 15)):
        duration = (item.end_date - item.start_date).days + 1
        occupied_periods = self._active_periods_for_employee_year(item.employee, item.schedule.year, exclude_item=item)
        search_start = item.end_date + timedelta(days=min_shift_days)
        latest_start = date(item.schedule.year, latest_start_month_day[0], latest_start_month_day[1])
        if search_start > date(item.schedule.year, 12, 31):
            return None
        search_end = date(item.schedule.year, 12, 31)
        blocked_periods = list(occupied_periods)
        for _ in range(8):
            slot = self._find_free_slot(
                blocked_periods,
                search_start,
                search_end,
                duration,
                max_attempts=30,
            )
            if slot is None:
                return None
            if slot[0] <= latest_start and get_chargeable_leave_days(slot[0], slot[1], item.vacation_type) <= item.chargeable_days:
                return slot
            blocked_periods.append(slot)
        return None

    def _historical_manager_transfer_candidates(self, year):
        return (
            VacationScheduleItem.objects.select_related(
                "employee",
                "employee__department",
                "schedule",
            )
            .filter(
                schedule__year=year,
                status=VacationScheduleItem.STATUS_APPROVED,
                source=VacationScheduleItem.SOURCE_GENERATED,
                end_date__lte=date(year, 10, 15),
                employee__is_active_employee=True,
            )
            .exclude(employee__role__in=Employees.SERVICE_ROLES)
            .exclude(change_requests__isnull=False)
            .order_by("start_date", "employee__last_name", "id")
        )

    def _create_historical_manager_transfer_for_queryset(self, queryset, actor, status, reason_choices):
        candidates = list(queryset)
        self.rng.shuffle(candidates)
        for item in candidates:
            slot = self._find_transfer_slot_for_item(item, min_shift_days=self.rng.choice([14, 21, 28]))
            if slot is None:
                continue
            change_request, _ = self._create_historical_transfer_request(
                item,
                slot[0],
                slot[1],
                requested_by=actor,
                status=status,
                reason_choices=reason_choices,
            )
            if change_request is not None:
                return change_request
        return None

    def _create_historical_manager_initiated_transfers(self):
        enterprise_head = (
            Employees.objects.filter(role=Employees.ROLE_ENTERPRISE_HEAD, is_active_employee=True)
            .order_by("id")
            .first()
        )
        department_heads = list(
            Employees.objects.select_related("managed_department", "department")
            .filter(role=Employees.ROLE_DEPARTMENT_HEAD, is_active_employee=True)
            .order_by("id")
        )
        if enterprise_head is None and not department_heads:
            return

        rejected_manager_proposal_created = False
        for year in range(self.schedule_start_year, self.schedule_end_year):
            base_candidates = self._historical_manager_transfer_candidates(year)
            year_created = 0
            target_per_year = 2 if self.fast_mode else 3

            for department_head in department_heads:
                if year_created >= max(1, target_per_year - 1):
                    break
                managed_department = getattr(department_head, "managed_department", None) or department_head.department
                if managed_department is None:
                    continue
                status = (
                    VacationScheduleChangeRequest.STATUS_REJECTED
                    if not rejected_manager_proposal_created
                    else VacationScheduleChangeRequest.STATUS_APPROVED
                )
                change_request = self._create_historical_manager_transfer_for_queryset(
                    base_candidates.filter(
                        employee__department=managed_department,
                        employee__role=Employees.ROLE_EMPLOYEE,
                    ),
                    department_head,
                    status,
                    [
                        "Производственная необходимость: требуется сохранить покрытие смены.",
                        "Предложение руководителя отдела из-за высокой нагрузки в исходном периоде.",
                        "Нужно перенести отпуск на менее рискованный период для отдела.",
                    ],
                )
                if change_request is not None:
                    rejected_manager_proposal_created = (
                        rejected_manager_proposal_created
                        or change_request.status == VacationScheduleChangeRequest.STATUS_REJECTED
                    )
                    year_created += 1

            if enterprise_head is not None and year_created < target_per_year:
                status = VacationScheduleChangeRequest.STATUS_APPROVED
                self._create_historical_manager_transfer_for_queryset(
                    base_candidates.filter(employee__role__in=[Employees.ROLE_HR, Employees.ROLE_DEPARTMENT_HEAD])
                    .exclude(employee=enterprise_head),
                    enterprise_head,
                    status,
                    [
                        "Предложение руководителя предприятия для выравнивания графика согласующих ролей.",
                        "Нужно перенести отпуск на период с меньшей управленческой нагрузкой.",
                        "Предложение переноса для сохранения управленческого покрытия.",
                    ],
                )

    def _create_manager_initiated_current_year_transfers(self, current_year):
        department_heads = list(
            Employees.objects.select_related("managed_department", "department")
            .filter(role=Employees.ROLE_DEPARTMENT_HEAD, is_active_employee=True)
            .order_by("id")
        )
        for department_head in department_heads:
            managed_department = getattr(department_head, "managed_department", None) or department_head.department
            if managed_department is None:
                continue
            created = self._create_first_pending_manager_transfer(
                self._manager_transfer_candidates(current_year).filter(
                    employee__department=managed_department,
                    employee__role=Employees.ROLE_EMPLOYEE,
                ),
                department_head,
                [
                    "Производственная необходимость: нужно сдвинуть отпуск на менее напряженный период.",
                    "Предложение руководителя отдела для сохранения сменного покрытия.",
                ],
            )
            if created is not None:
                break

        enterprise_head = (
            Employees.objects.filter(role=Employees.ROLE_ENTERPRISE_HEAD, is_active_employee=True)
            .order_by("id")
            .first()
        )
        if enterprise_head is not None:
            self._create_first_pending_manager_transfer(
                self._manager_transfer_candidates(current_year)
                .filter(employee__role=Employees.ROLE_HR)
                .exclude(employee=enterprise_head),
                enterprise_head,
                [
                    "Предложение руководителя предприятия для выравнивания графика HR.",
                    "Нужно перенести отпуск на период с меньшей нагрузкой кадрового блока.",
                ],
            )
            self._create_first_pending_manager_transfer(
                self._manager_transfer_candidates(current_year)
                .filter(employee__role=Employees.ROLE_DEPARTMENT_HEAD)
                .exclude(employee=enterprise_head),
                enterprise_head,
                [
                    "Предложение руководителя предприятия для выравнивания графика руководителей отделов.",
                    "Нужно перенести отпуск на период с меньшей управленческой нагрузкой.",
                ],
            )

    def _manager_transfer_candidates(self, current_year):
        return (
            VacationScheduleItem.objects.select_related("employee", "schedule")
            .filter(
                schedule__year=current_year,
                status=VacationScheduleItem.STATUS_APPROVED,
                source=VacationScheduleItem.SOURCE_GENERATED,
                start_date__gt=self.today + timedelta(days=35),
                employee__is_active_employee=True,
            )
            .exclude(employee__role__in=Employees.SERVICE_ROLES)
            .exclude(change_requests__isnull=False)
            .distinct()
            .order_by("start_date", "employee__last_name", "id")
        )

    def _create_first_pending_manager_transfer(self, queryset, actor, reason_choices):
        for item in queryset:
            change_request = self._create_pending_manager_transfer(item, actor, reason_choices)
            if change_request is not None:
                self.transfer_counts["manager_current_pending"] += 1
                return change_request
        return None

    def _create_pending_manager_transfer(self, item, actor, reason_choices):
        current_year = item.schedule.year
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
        search_start = max(self.today + timedelta(days=45), item.end_date + timedelta(days=14))
        slot = self._find_free_slot(
            occupied_periods,
            search_start,
            date(current_year, 12, 31),
            duration,
            max_attempts=60,
        )
        if slot is None:
            return None
        try:
            return create_schedule_change_request(
                item.id,
                requested_by=actor,
                new_start_date=slot[0],
                new_end_date=slot[1],
                reason=self.rng.choice(reason_choices),
            )
        except ValidationError:
            return None

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
            schedule_item = create_schedule_item_from_paid_vacation_request(request_obj, risk_payload=risk_payload)
            if schedule_item is not None:
                self._add_schedule_item_to_paid_days_cache(schedule_item)
                self._add_schedule_item_to_active_period_cache(schedule_item)
        else:
            self._add_request_to_active_period_cache(request_obj)
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

    def _active_request_overlap_exists(self, employee, start_date, end_date):
        return any(
            not (end_date < current_start or start_date > current_end)
            for _request_id, current_start, current_end in self._active_request_periods_for_employee(employee)
        )

    def _active_schedule_item_overlap_exists(self, employee, start_date, end_date, *, exclude_item=None):
        exclude_item_id = exclude_item.id if exclude_item is not None else None
        return any(
            item_id != exclude_item_id and not (end_date < current_start or start_date > current_end)
            for item_id, current_start, current_end in self._active_schedule_item_periods_for_employee(employee)
        )
