from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField, RangeBoundary, RangeOperators
from django.db import models
from django.db.models import Func
from django.db.models.functions import Greatest, Least
from django.utils import timezone


VACATION_TYPE_CHOICES = [
    ("paid", "Оплачиваемый"),
    ("unpaid", "Неоплачиваемый"),
    ("study", "Учебный"),
]


class VacationRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    RISK_LOW = "low"
    RISK_MEDIUM = "medium"
    RISK_HIGH = "high"

    STATUS_CHOICES = [
        (STATUS_PENDING, "В ожидании"),
        (STATUS_APPROVED, "Одобрено"),
        (STATUS_REJECTED, "Отклонено"),
    ]
    RISK_CHOICES = [
        (RISK_LOW, "Низкий"),
        (RISK_MEDIUM, "Средний"),
        (RISK_HIGH, "Высокий"),
    ]
    ACTIVE_STATUSES = (STATUS_PENDING, STATUS_APPROVED)

    employee = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.CASCADE,
        related_name="vacation_requests",
        verbose_name="Сотрудник",
    )
    start_date = models.DateField(verbose_name="Дата начала")
    end_date = models.DateField(verbose_name="Дата окончания")
    vacation_type = models.CharField(
        max_length=50,
        choices=VACATION_TYPE_CHOICES,
        default="paid",
        verbose_name="Тип отпуска",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        verbose_name="Статус",
    )
    reason = models.TextField(blank=True, default="", verbose_name="Причина")
    reviewed_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_vacation_requests",
        verbose_name="Рассмотрел",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата рассмотрения")
    review_comment = models.TextField(blank=True, default="", verbose_name="Комментарий согласующего")
    risk_score = models.PositiveSmallIntegerField(default=0, verbose_name="Оценка риска")
    risk_level = models.CharField(max_length=16, choices=RISK_CHOICES, default=RISK_LOW, verbose_name="Уровень риска")
    department_load_level = models.PositiveSmallIntegerField(default=1, verbose_name="Нагрузка отдела")
    overlapping_absences_count = models.PositiveSmallIntegerField(default=0, verbose_name="Пересечения отсутствий")
    remaining_staff_count = models.PositiveSmallIntegerField(default=0, verbose_name="Останется сотрудников")
    min_staff_required = models.PositiveSmallIntegerField(default=0, verbose_name="Минимум сотрудников")
    balance_after_request = models.DecimalField(max_digits=7, decimal_places=2, default=0, verbose_name="Баланс после заявки")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Meta:
        db_table = "leave_vacationrequest"
        verbose_name = "Заявка на отпуск"
        verbose_name_plural = "Заявки на отпуск"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(start_date__lte=models.F("end_date")),
                name="vacation_request_start_before_end",
            ),
            models.CheckConstraint(
                check=models.Q(department_load_level__gte=1, department_load_level__lte=5),
                name="vacation_request_department_load_1_5",
            ),
            ExclusionConstraint(
                name="exclude_overlapping_active_vacation_requests",
                expressions=[
                    ("employee", RangeOperators.EQUAL),
                    (
                        Func(
                            Least("start_date", "end_date"),
                            Greatest("start_date", "end_date"),
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                            function="DATERANGE",
                            output_field=DateRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=models.Q(status__in=("pending", "approved")),
            ),
        ]

    def __str__(self):
        return f"Заявка {self.employee.full_name}: {self.get_status_display()} с {self.start_date} по {self.end_date}"


class VacationRequestHistory(models.Model):
    ACTION_CREATED = "created"
    ACTION_SUBMITTED = "submitted"
    ACTION_APPROVED = "approved"
    ACTION_REJECTED = "rejected"
    ACTION_DELETED = "deleted"

    ACTION_CHOICES = [
        (ACTION_CREATED, "Создана"),
        (ACTION_SUBMITTED, "Отправлена на согласование"),
        (ACTION_APPROVED, "Одобрена"),
        (ACTION_REJECTED, "Отклонена"),
        (ACTION_DELETED, "Удалена"),
    ]

    vacation_request = models.ForeignKey(
        VacationRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="history_entries",
        verbose_name="Заявка",
    )
    employee = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vacation_request_history",
        verbose_name="Сотрудник",
    )
    actor = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vacation_request_history_actions",
        verbose_name="Инициатор",
    )
    action = models.CharField(max_length=32, choices=ACTION_CHOICES, verbose_name="Действие")
    title = models.CharField(max_length=160, verbose_name="Заголовок")
    description = models.TextField(blank=True, default="", verbose_name="Описание")
    status_snapshot = models.CharField(max_length=20, blank=True, default="", verbose_name="Статус заявки")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Дата действия")

    class Meta:
        db_table = "leave_vacationrequesthistory"
        verbose_name = "История заявки на отпуск"
        verbose_name_plural = "История заявок на отпуск"
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["vacation_request", "created_at"]),
            models.Index(fields=["employee", "created_at"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        return f"{self.title}: {self.employee or 'сотрудник не указан'}"


class VacationSchedule(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_DEPARTMENT_REVIEW = "department_review"
    STATUS_APPROVED = "approved"
    STATUS_ARCHIVED = "archived"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_DEPARTMENT_REVIEW, "На согласовании"),
        (STATUS_APPROVED, "Утвержден"),
        (STATUS_ARCHIVED, "Архив"),
    ]

    year = models.PositiveIntegerField(unique=True, verbose_name="Год")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT, verbose_name="Статус")
    created_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_vacation_schedules",
        verbose_name="Создал",
    )
    approved_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_vacation_schedules",
        verbose_name="Утвердил",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    generated_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата формирования")
    approved_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата утверждения")

    class Meta:
        db_table = "leave_vacationschedule"
        verbose_name = "Годовой график отпусков"
        verbose_name_plural = "Годовые графики отпусков"
        ordering = ["-year"]

    def __str__(self):
        return f"График отпусков на {self.year} год"


class VacationScheduleItem(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_PLANNED = "planned"
    STATUS_APPROVED = "approved"
    STATUS_TRANSFERRED = "transferred"
    STATUS_CANCELLED = "cancelled"

    SOURCE_GENERATED = "generated"
    SOURCE_MANUAL = "manual"
    SOURCE_TRANSFER = "transfer"

    RISK_LOW = "low"
    RISK_MEDIUM = "medium"
    RISK_HIGH = "high"

    ACTIVE_STATUSES = (STATUS_PLANNED, STATUS_APPROVED)
    BALANCE_STATUSES = (STATUS_PLANNED, STATUS_APPROVED)

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Черновик"),
        (STATUS_PLANNED, "Запланирован"),
        (STATUS_APPROVED, "Утвержден"),
        (STATUS_TRANSFERRED, "Перенесен"),
        (STATUS_CANCELLED, "Отменен"),
    ]
    SOURCE_CHOICES = [
        (SOURCE_GENERATED, "Сформирован системой"),
        (SOURCE_MANUAL, "Внесен вручную"),
        (SOURCE_TRANSFER, "Создан переносом"),
    ]
    RISK_CHOICES = [
        (RISK_LOW, "Низкий"),
        (RISK_MEDIUM, "Средний"),
        (RISK_HIGH, "Высокий"),
    ]

    schedule = models.ForeignKey(
        VacationSchedule,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name="График",
    )
    employee = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.CASCADE,
        related_name="vacation_schedule_items",
        verbose_name="Сотрудник",
    )
    start_date = models.DateField(verbose_name="Дата начала")
    end_date = models.DateField(verbose_name="Дата окончания")
    vacation_type = models.CharField(max_length=50, choices=VACATION_TYPE_CHOICES, default="paid", verbose_name="Тип отпуска")
    chargeable_days = models.PositiveIntegerField(default=0, verbose_name="Списываемые дни")
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_DRAFT, verbose_name="Статус")
    source = models.CharField(max_length=32, choices=SOURCE_CHOICES, default=SOURCE_GENERATED, verbose_name="Источник")
    risk_score = models.PositiveSmallIntegerField(default=0, verbose_name="Оценка риска")
    risk_level = models.CharField(max_length=16, choices=RISK_CHOICES, default=RISK_LOW, verbose_name="Уровень риска")
    generated_by_ai = models.BooleanField(default=False, verbose_name="Сформировано ИИ")
    was_changed_by_manager = models.BooleanField(default=False, verbose_name="Изменено руководителем")
    manager_comment = models.TextField(blank=True, default="", verbose_name="Комментарий руководителя")
    previous_item = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replacement_items",
        verbose_name="Предыдущий пункт графика",
    )
    created_from_change_request = models.ForeignKey(
        "leave.VacationScheduleChangeRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_schedule_items",
        verbose_name="Создан из запроса переноса",
    )
    created_from_vacation_request = models.ForeignKey(
        "leave.VacationRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_schedule_items",
        verbose_name="Создан из оплачиваемой заявки",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Meta:
        db_table = "leave_vacationscheduleitem"
        verbose_name = "Пункт графика отпусков"
        verbose_name_plural = "Пункты графика отпусков"
        ordering = ["start_date", "employee__last_name", "employee__first_name"]
        indexes = [
            models.Index(fields=["employee", "start_date", "end_date"]),
            models.Index(fields=["schedule", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(start_date__lte=models.F("end_date")),
                name="schedule_item_start_before_end",
            ),
            models.UniqueConstraint(
                fields=["created_from_change_request"],
                condition=models.Q(created_from_change_request__isnull=False),
                name="unique_schedule_item_change_request_source",
            ),
            models.UniqueConstraint(
                fields=["created_from_vacation_request"],
                condition=models.Q(created_from_vacation_request__isnull=False),
                name="unique_schedule_item_vacation_request_source",
            ),
            ExclusionConstraint(
                name="exclude_overlapping_active_schedule_items",
                expressions=[
                    ("employee", RangeOperators.EQUAL),
                    (
                        Func(
                            Least("start_date", "end_date"),
                            Greatest("start_date", "end_date"),
                            RangeBoundary(inclusive_lower=True, inclusive_upper=True),
                            function="DATERANGE",
                            output_field=DateRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ],
                condition=models.Q(status__in=("planned", "approved")),
            ),
        ]

    def __str__(self):
        return f"{self.employee}: {self.start_date} - {self.end_date}"


class VacationEntitlementPeriod(models.Model):
    employee = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.CASCADE,
        related_name="vacation_entitlement_periods",
        verbose_name="Сотрудник",
    )
    working_year_number = models.PositiveIntegerField(verbose_name="Номер рабочего года")
    period_start = models.DateField(verbose_name="Начало рабочего года")
    period_end = models.DateField(verbose_name="Окончание рабочего года")
    entitled_days = models.DecimalField(max_digits=7, decimal_places=2, default=0, verbose_name="Право на отпуск")
    available_from = models.DateField(verbose_name="Доступно с")
    must_use_by = models.DateField(verbose_name="Использовать до")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        db_table = "leave_vacationentitlementperiod"
        verbose_name = "Рабочий год для отпуска"
        verbose_name_plural = "Рабочие годы для отпусков"
        ordering = ["employee_id", "period_start"]
        constraints = [
            models.UniqueConstraint(fields=["employee", "working_year_number"], name="unique_employee_working_year"),
            models.UniqueConstraint(fields=["employee", "period_start", "period_end"], name="unique_employee_working_period"),
            models.CheckConstraint(
                check=models.Q(period_start__lte=models.F("period_end")),
                name="entitlement_period_start_before_end",
            ),
            models.CheckConstraint(
                check=models.Q(available_from__lte=models.F("must_use_by")),
                name="entitlement_period_available_before_deadline",
            ),
            models.CheckConstraint(
                check=models.Q(entitled_days__gte=0),
                name="entitlement_period_non_negative_days",
            ),
        ]
        indexes = [
            models.Index(fields=["employee", "period_start", "period_end"]),
            models.Index(fields=["employee", "available_from"]),
        ]

    def __str__(self):
        return f"{self.employee}: {self.period_start} - {self.period_end}"


class VacationEntitlementAllocation(models.Model):
    SOURCE_REQUEST = "request"
    SOURCE_SCHEDULE = "schedule"
    STATE_USED = "used"
    STATE_RESERVED = "reserved"

    SOURCE_CHOICES = [
        (SOURCE_REQUEST, "Заявка"),
        (SOURCE_SCHEDULE, "Годовой график"),
    ]
    STATE_CHOICES = [
        (STATE_USED, "Использовано"),
        (STATE_RESERVED, "В резерве"),
    ]

    employee = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.CASCADE,
        related_name="vacation_entitlement_allocations",
        verbose_name="Сотрудник",
    )
    entitlement_period = models.ForeignKey(
        VacationEntitlementPeriod,
        on_delete=models.CASCADE,
        related_name="allocations",
        verbose_name="Рабочий год",
    )
    vacation_request = models.ForeignKey(
        VacationRequest,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="entitlement_allocations",
        verbose_name="Заявка",
    )
    schedule_item = models.ForeignKey(
        VacationScheduleItem,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="entitlement_allocations",
        verbose_name="Пункт графика",
    )
    source_kind = models.CharField(max_length=16, choices=SOURCE_CHOICES, verbose_name="Источник")
    state = models.CharField(max_length=16, choices=STATE_CHOICES, verbose_name="Состояние")
    allocated_days = models.DecimalField(max_digits=7, decimal_places=2, default=0, verbose_name="Распределено дней")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")

    class Meta:
        db_table = "leave_vacationentitlementallocation"
        verbose_name = "Распределение отпускных дней"
        verbose_name_plural = "Распределения отпускных дней"
        ordering = ["employee_id", "entitlement_period__period_start", "created_at"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(vacation_request__isnull=False, schedule_item__isnull=True)
                    | models.Q(vacation_request__isnull=True, schedule_item__isnull=False)
                ),
                name="entitlement_allocation_single_source",
            ),
            models.UniqueConstraint(
                fields=["entitlement_period", "vacation_request"],
                condition=models.Q(vacation_request__isnull=False),
                name="unique_entitlement_request_alloc",
            ),
            models.UniqueConstraint(
                fields=["entitlement_period", "schedule_item"],
                condition=models.Q(schedule_item__isnull=False),
                name="unique_entitlement_schedule_alloc",
            ),
            models.CheckConstraint(
                check=models.Q(allocated_days__gt=0),
                name="entitlement_allocation_positive_days",
            ),
        ]
        indexes = [
            models.Index(fields=["employee", "state"]),
            models.Index(fields=["source_kind", "state"]),
        ]

    def __str__(self):
        return f"{self.employee}: {self.allocated_days} д. ({self.get_source_kind_display()})"


class VacationScheduleDepartmentApproval(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"

    STATUS_CHOICES = [
        (STATUS_PENDING, "В ожидании"),
        (STATUS_APPROVED, "Утверждено"),
        (STATUS_REJECTED, "Отклонено"),
    ]

    schedule = models.ForeignKey(VacationSchedule, on_delete=models.CASCADE, related_name="department_approvals")
    department = models.ForeignKey(to="employees.Departments", on_delete=models.CASCADE, related_name="schedule_approvals")
    department_head = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="department_schedule_approvals",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    comment = models.TextField(blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "leave_vacationschedule_departmentapproval"
        constraints = [
            models.UniqueConstraint(fields=["schedule", "department"], name="unique_schedule_department_approval"),
        ]


class VacationScheduleEnterpriseApproval(models.Model):
    STATUS_PENDING = VacationScheduleDepartmentApproval.STATUS_PENDING
    STATUS_APPROVED = VacationScheduleDepartmentApproval.STATUS_APPROVED
    STATUS_REJECTED = VacationScheduleDepartmentApproval.STATUS_REJECTED
    STATUS_CHOICES = VacationScheduleDepartmentApproval.STATUS_CHOICES

    schedule = models.ForeignKey(VacationSchedule, on_delete=models.CASCADE, related_name="enterprise_approvals")
    enterprise_head = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="enterprise_schedule_approvals",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    comment = models.TextField(blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "leave_vacationschedule_enterpriseapproval"
        constraints = [
            models.UniqueConstraint(fields=["schedule"], name="unique_schedule_enterprise_approval"),
        ]


class VacationScheduleAuthorizedApproval(models.Model):
    STATUS_PENDING = VacationScheduleDepartmentApproval.STATUS_PENDING
    STATUS_APPROVED = VacationScheduleDepartmentApproval.STATUS_APPROVED
    STATUS_REJECTED = VacationScheduleDepartmentApproval.STATUS_REJECTED
    STATUS_CHOICES = VacationScheduleDepartmentApproval.STATUS_CHOICES

    schedule = models.ForeignKey(VacationSchedule, on_delete=models.CASCADE, related_name="authorized_approvals")
    authorized_person = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authorized_schedule_approvals",
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    comment = models.TextField(blank=True, default="")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "leave_vacationschedule_authorizedapproval"
        constraints = [
            models.UniqueConstraint(fields=["schedule"], name="unique_schedule_authorized_approval"),
        ]


class VacationScheduleChangeRequest(models.Model):
    STATUS_PENDING = VacationScheduleDepartmentApproval.STATUS_PENDING
    STATUS_APPROVED = VacationScheduleDepartmentApproval.STATUS_APPROVED
    STATUS_REJECTED = VacationScheduleDepartmentApproval.STATUS_REJECTED
    STATUS_CHOICES = VacationScheduleDepartmentApproval.STATUS_CHOICES

    schedule_item = models.ForeignKey(VacationScheduleItem, on_delete=models.CASCADE, related_name="change_requests")
    employee = models.ForeignKey(to="employees.Employees", on_delete=models.CASCADE, related_name="vacation_schedule_change_requests")
    old_start_date = models.DateField()
    old_end_date = models.DateField()
    new_start_date = models.DateField()
    new_end_date = models.DateField()
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    requested_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requested_vacation_schedule_changes",
    )
    reviewed_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_vacation_schedule_changes",
    )
    review_comment = models.TextField(blank=True, default="")
    risk_score = models.PositiveSmallIntegerField(default=0)
    risk_level = models.CharField(max_length=16, choices=VacationScheduleItem.RISK_CHOICES, default=VacationScheduleItem.RISK_LOW)
    department_load_level = models.PositiveSmallIntegerField(default=1)
    overlapping_absences_count = models.PositiveSmallIntegerField(default=0)
    remaining_staff_count = models.PositiveSmallIntegerField(default=0)
    min_staff_required = models.PositiveSmallIntegerField(default=0)
    balance_after_change = models.DecimalField(max_digits=7, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "leave_vacationschedule_changerequest"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(old_start_date__lte=models.F("old_end_date")),
                name="schedule_change_old_start_before_end",
            ),
            models.CheckConstraint(
                check=models.Q(new_start_date__lte=models.F("new_end_date")),
                name="schedule_change_new_start_before_end",
            ),
            models.CheckConstraint(
                check=models.Q(department_load_level__gte=1, department_load_level__lte=5),
                name="schedule_change_department_load_1_5",
            ),
        ]


class VacationUrgentClosureRequest(models.Model):
    STATUS_DEPARTMENT_REVIEW = "department_review"
    STATUS_EMPLOYEE_REVIEW = "employee_review"
    STATUS_HR_FINALIZATION = "hr_finalization"
    STATUS_COMPLETED = "completed"
    STATUS_REJECTED = "rejected"

    ACTIVE_STATUSES = (
        STATUS_DEPARTMENT_REVIEW,
        STATUS_EMPLOYEE_REVIEW,
        STATUS_HR_FINALIZATION,
    )
    TERMINAL_STATUSES = (
        STATUS_COMPLETED,
        STATUS_REJECTED,
    )
    STATUS_CHOICES = [
        (STATUS_DEPARTMENT_REVIEW, "У руководителя отдела"),
        (STATUS_EMPLOYEE_REVIEW, "У сотрудника"),
        (STATUS_HR_FINALIZATION, "Финализация HR"),
        (STATUS_COMPLETED, "Завершено"),
        (STATUS_REJECTED, "Отклонено"),
    ]

    employee = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.CASCADE,
        related_name="urgent_closure_requests",
        verbose_name="Сотрудник",
    )
    planning_year = models.PositiveIntegerField(verbose_name="Год черновика")
    closure_year = models.PositiveIntegerField(verbose_name="Год закрытия остатка")
    required_days = models.DecimalField(max_digits=7, decimal_places=2, verbose_name="Нужно закрыть дней")
    deadline = models.DateField(verbose_name="Использовать до")
    proposed_start_date = models.DateField(verbose_name="Предложенная дата начала")
    proposed_end_date = models.DateField(verbose_name="Предложенная дата окончания")
    reason = models.TextField(blank=True, default="", verbose_name="Комментарий HR")
    status = models.CharField(
        max_length=32,
        choices=STATUS_CHOICES,
        default=STATUS_DEPARTMENT_REVIEW,
        verbose_name="Статус",
    )
    created_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_urgent_closure_requests",
        verbose_name="Создал",
    )
    department_reviewer = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="department_reviewed_urgent_closures",
        verbose_name="Рассмотрел руководитель отдела",
    )
    department_reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата решения руководителя")
    department_comment = models.TextField(blank=True, default="", verbose_name="Комментарий руководителя")
    employee_responded_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата ответа сотрудника")
    employee_comment = models.TextField(blank=True, default="", verbose_name="Комментарий сотрудника")
    finalized_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finalized_urgent_closure_requests",
        verbose_name="Финализировал HR",
    )
    finalized_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата финализации")
    final_comment = models.TextField(blank=True, default="", verbose_name="Комментарий финализации")
    rejected_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_urgent_closure_requests",
        verbose_name="Отклонил",
    )
    rejected_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата отклонения")
    rejection_comment = models.TextField(blank=True, default="", verbose_name="Причина отклонения")
    created_schedule_item = models.OneToOneField(
        VacationScheduleItem,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="urgent_closure_request",
        verbose_name="Созданный пункт графика",
    )
    risk_score = models.PositiveSmallIntegerField(default=0, verbose_name="Оценка риска")
    risk_level = models.CharField(
        max_length=16,
        choices=VacationScheduleItem.RISK_CHOICES,
        default=VacationScheduleItem.RISK_LOW,
        verbose_name="Уровень риска",
    )
    department_load_level = models.PositiveSmallIntegerField(default=1, verbose_name="Нагрузка отдела")
    overlapping_absences_count = models.PositiveSmallIntegerField(default=0, verbose_name="Пересечения отсутствий")
    remaining_staff_count = models.PositiveSmallIntegerField(default=0, verbose_name="Останется сотрудников")
    min_staff_required = models.PositiveSmallIntegerField(default=0, verbose_name="Минимум сотрудников")
    balance_after_closure = models.DecimalField(
        max_digits=7,
        decimal_places=2,
        default=0,
        verbose_name="Баланс после закрытия",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        db_table = "leave_vacationurgentclosurerequest"
        verbose_name = "Закрытие срочного остатка"
        verbose_name_plural = "Закрытия срочных остатков"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["employee", "planning_year", "status"], name="urgent_close_emp_plan_idx"),
            models.Index(fields=["status", "-created_at"], name="urgent_close_status_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(proposed_start_date__lte=models.F("proposed_end_date")),
                name="urgent_closure_start_before_end",
            ),
            models.CheckConstraint(
                check=models.Q(required_days__gt=0),
                name="urgent_closure_required_days_positive",
            ),
            models.CheckConstraint(
                check=models.Q(department_load_level__gte=1, department_load_level__lte=5),
                name="urgent_closure_department_load_1_5",
            ),
            models.UniqueConstraint(
                fields=["employee", "planning_year", "deadline"],
                condition=models.Q(
                    status__in=(
                        "department_review",
                        "employee_review",
                        "hr_finalization",
                    )
                ),
                name="unique_active_urgent_closure",
            ),
        ]

    def __str__(self):
        return f"{self.employee}: закрытие {self.required_days} д. до {self.deadline}"


class VacationPreferenceCollection(models.Model):
    STATUS_OPEN = "open"
    STATUS_FINISHED = "finished"

    STATUS_CHOICES = [
        (STATUS_OPEN, "Открыт"),
        (STATUS_FINISHED, "Завершён"),
    ]

    year = models.PositiveIntegerField(unique=True, verbose_name="Год")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN, verbose_name="Статус")
    deadline = models.DateField(verbose_name="Срок заполнения")
    started_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="started_vacation_preference_collections",
        verbose_name="Запустил",
    )
    finished_by = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="finished_vacation_preference_collections",
        verbose_name="Завершил",
    )
    started_at = models.DateTimeField(default=timezone.now, verbose_name="Дата запуска")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата завершения")
    demo_autofill_enabled = models.BooleanField(default=False, verbose_name="Демо-автозаполнение")

    class Meta:
        db_table = "leave_vacationpreference_collection"
        verbose_name = "Сбор пожеланий по отпуску"
        verbose_name_plural = "Сборы пожеланий по отпуску"
        ordering = ["-year"]
        indexes = [
            models.Index(fields=["status", "year"]),
        ]

    def __str__(self):
        return f"Сбор пожеланий на {self.year} год"


class VacationPreference(models.Model):
    STATUS_PENDING = "pending"
    STATUS_FILLED = "filled"
    STATUS_SKIPPED = "skipped"

    PRIORITY_PRIMARY = "primary"
    PRIORITY_BACKUP = "backup"

    REMAINDER_AUTO = "auto"
    REMAINDER_APPROVAL = "approval"
    REMAINDER_DEFER = "defer"

    STATUS_CHOICES = [
        (STATUS_PENDING, "В ожидании"),
        (STATUS_FILLED, "Заполнено"),
        (STATUS_SKIPPED, "Пропущено"),
    ]
    PRIORITY_CHOICES = [
        (PRIORITY_PRIMARY, "Основное"),
        (PRIORITY_BACKUP, "Запасное"),
    ]
    REMAINDER_POLICY_CHOICES = [
        (REMAINDER_AUTO, "Можно распределить автоматически"),
        (REMAINDER_APPROVAL, "Сначала согласовать со мной"),
        (REMAINDER_DEFER, "Не планировать сверх указанного периода"),
    ]

    employee = models.ForeignKey(to="employees.Employees", on_delete=models.CASCADE, related_name="vacation_preferences")
    year = models.PositiveIntegerField()
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=16, choices=PRIORITY_CHOICES, default=PRIORITY_PRIMARY)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    remainder_policy = models.CharField(
        max_length=24,
        choices=REMAINDER_POLICY_CHOICES,
        default=REMAINDER_AUTO,
        verbose_name="Решение по остатку",
    )
    comment = models.TextField(blank=True, default="")
    created_automatically = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "leave_vacationpreference"
        ordering = ["year", "employee_id", "priority", "start_date"]
        indexes = [
            models.Index(fields=["employee", "year"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(start_date__isnull=True, end_date__isnull=True)
                    | models.Q(start_date__isnull=False, end_date__isnull=False, start_date__lte=models.F("end_date"))
                ),
                name="vacation_preference_date_range_valid",
            ),
        ]


class DepartmentWorkload(models.Model):
    department = models.ForeignKey(to="employees.Departments", on_delete=models.CASCADE, related_name="workload_months")
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()
    load_level = models.PositiveSmallIntegerField(default=3)
    min_staff_required = models.PositiveSmallIntegerField(default=1)
    max_absent = models.PositiveSmallIntegerField(default=1)

    class Meta:
        db_table = "leave_departmentworkload"
        ordering = ["year", "department_id", "month"]
        constraints = [
            models.UniqueConstraint(fields=["department", "year", "month"], name="unique_department_workload_month"),
            models.CheckConstraint(
                check=models.Q(month__gte=1, month__lte=12),
                name="department_workload_month_1_12",
            ),
            models.CheckConstraint(
                check=models.Q(load_level__gte=1, load_level__lte=5),
                name="department_workload_level_1_5",
            ),
        ]


class DepartmentStaffingRule(models.Model):
    department = models.OneToOneField(to="employees.Departments", on_delete=models.CASCADE, related_name="staffing_rule")
    min_staff_required = models.PositiveSmallIntegerField(default=1)
    max_absent = models.PositiveSmallIntegerField(default=1)
    criticality_level = models.PositiveSmallIntegerField(default=3)
    substitution_group = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        db_table = "leave_departmentstaffingrule"
        constraints = [
            models.CheckConstraint(
                check=models.Q(criticality_level__gte=1, criticality_level__lte=5),
                name="department_staffing_criticality_1_5",
            ),
        ]
