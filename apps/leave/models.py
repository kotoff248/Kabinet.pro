from django.db import models


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

    def __str__(self):
        return f"Заявка {self.employee.full_name}: {self.get_status_display()} с {self.start_date} по {self.end_date}"


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


class VacationPreference(models.Model):
    STATUS_PENDING = "pending"
    STATUS_FILLED = "filled"
    STATUS_SKIPPED = "skipped"

    PRIORITY_PRIMARY = "primary"
    PRIORITY_BACKUP = "backup"

    STATUS_CHOICES = [
        (STATUS_PENDING, "В ожидании"),
        (STATUS_FILLED, "Заполнено"),
        (STATUS_SKIPPED, "Пропущено"),
    ]
    PRIORITY_CHOICES = [
        (PRIORITY_PRIMARY, "Основное"),
        (PRIORITY_BACKUP, "Запасное"),
    ]

    employee = models.ForeignKey(to="employees.Employees", on_delete=models.CASCADE, related_name="vacation_preferences")
    year = models.PositiveIntegerField()
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=16, choices=PRIORITY_CHOICES, default=PRIORITY_PRIMARY)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    comment = models.TextField(blank=True, default="")
    created_automatically = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "leave_vacationpreference"
        ordering = ["year", "employee_id", "priority", "start_date"]
        indexes = [
            models.Index(fields=["employee", "year"]),
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
        ]


class DepartmentStaffingRule(models.Model):
    department = models.OneToOneField(to="employees.Departments", on_delete=models.CASCADE, related_name="staffing_rule")
    min_staff_required = models.PositiveSmallIntegerField(default=1)
    max_absent = models.PositiveSmallIntegerField(default=1)
    criticality_level = models.PositiveSmallIntegerField(default=3)
    substitution_group = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        db_table = "leave_departmentstaffingrule"
