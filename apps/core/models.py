from django.db import models


class Notification(models.Model):
    STATUS_NEW = "new"
    STATUS_READ = "read"
    STATUS_DONE = "done"

    STATUS_CHOICES = [
        (STATUS_NEW, "Новое"),
        (STATUS_READ, "Прочитано"),
        (STATUS_DONE, "Завершено"),
    ]

    PRIORITY_LOW = 1
    PRIORITY_NORMAL = 2
    PRIORITY_HIGH = 3

    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Низкий"),
        (PRIORITY_NORMAL, "Обычный"),
        (PRIORITY_HIGH, "Высокий"),
    ]

    TYPE_VACATION_REQUEST_CREATED = "vacation_request_created"
    TYPE_VACATION_REQUEST_APPROVED = "vacation_request_approved"
    TYPE_VACATION_REQUEST_REJECTED = "vacation_request_rejected"
    TYPE_SCHEDULE_CHANGE_CREATED = "schedule_change_created"
    TYPE_SCHEDULE_CHANGE_APPROVED = "schedule_change_approved"
    TYPE_SCHEDULE_CHANGE_REJECTED = "schedule_change_rejected"
    TYPE_PREFERENCES_COLLECTION_STARTED = "preferences_collection_started"
    TYPE_SCHEDULE_REVIEW_REQUESTED = "schedule_review_requested"
    TYPE_SCHEDULE_ITEM_CHANGED_BY_MANAGER = "schedule_item_changed_by_manager"
    TYPE_UPCOMING_VACATION_REMINDER = "upcoming_vacation_reminder"
    TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW = "urgent_closure_department_review"
    TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW = "urgent_closure_employee_review"
    TYPE_URGENT_CLOSURE_HR_FINALIZATION = "urgent_closure_hr_finalization"
    TYPE_URGENT_CLOSURE_STATUS = "urgent_closure_status"

    MANAGED_ACTION_EVENT_TYPES = {
        TYPE_VACATION_REQUEST_CREATED,
        TYPE_SCHEDULE_CHANGE_CREATED,
        TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW,
        TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW,
        TYPE_URGENT_CLOSURE_HR_FINALIZATION,
    }

    EVENT_TYPE_CHOICES = [
        (TYPE_VACATION_REQUEST_CREATED, "Создана заявка на отпуск"),
        (TYPE_VACATION_REQUEST_APPROVED, "Заявка на отпуск одобрена"),
        (TYPE_VACATION_REQUEST_REJECTED, "Заявка на отпуск отклонена"),
        (TYPE_SCHEDULE_CHANGE_CREATED, "Создан запрос переноса"),
        (TYPE_SCHEDULE_CHANGE_APPROVED, "Перенос одобрен"),
        (TYPE_SCHEDULE_CHANGE_REJECTED, "Перенос отклонён"),
        (TYPE_PREFERENCES_COLLECTION_STARTED, "Начат сбор пожеланий"),
        (TYPE_SCHEDULE_REVIEW_REQUESTED, "Запрошено согласование графика"),
        (TYPE_SCHEDULE_ITEM_CHANGED_BY_MANAGER, "График отпуска изменён руководителем"),
        (TYPE_UPCOMING_VACATION_REMINDER, "Скоро отпуск"),
        (TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW, "Закрытие остатка у руководителя"),
        (TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW, "Закрытие остатка у сотрудника"),
        (TYPE_URGENT_CLOSURE_HR_FINALIZATION, "Закрытие остатка у HR"),
        (TYPE_URGENT_CLOSURE_STATUS, "Статус закрытия остатка"),
    ]

    recipient = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="Получатель",
    )
    actor = models.ForeignKey(
        to="employees.Employees",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_notifications",
        verbose_name="Инициатор",
    )
    event_type = models.CharField(max_length=64, choices=EVENT_TYPE_CHOICES, verbose_name="Тип события")
    title = models.CharField(max_length=180, verbose_name="Заголовок")
    message = models.TextField(verbose_name="Текст")
    action_url = models.CharField(max_length=255, blank=True, default="", verbose_name="Ссылка действия")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_NEW, verbose_name="Статус")
    priority = models.PositiveSmallIntegerField(
        choices=PRIORITY_CHOICES,
        default=PRIORITY_NORMAL,
        verbose_name="Приоритет",
    )
    requires_action = models.BooleanField(default=False, verbose_name="Требует действия")
    dedupe_key = models.CharField(
        max_length=180,
        unique=True,
        null=True,
        blank=True,
        verbose_name="Ключ уникальности",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    read_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата прочтения")
    done_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата завершения")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        db_table = "core_notification"
        verbose_name = "Уведомление"
        verbose_name_plural = "Уведомления"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["recipient", "status", "-created_at"], name="core_notifi_recipie_77f260_idx"),
            models.Index(fields=["recipient", "requires_action", "status"], name="core_notifi_recipie_4acc57_idx"),
            models.Index(fields=["event_type", "created_at"], name="core_notifi_event_t_d934c3_idx"),
        ]

    def __str__(self):
        return f"{self.recipient}: {self.title}"

    @property
    def is_unread(self):
        return self.status == self.STATUS_NEW

    @property
    def is_active_task(self):
        return self.requires_action and self.status != self.STATUS_DONE

    @property
    def is_managed_action_task(self):
        return self.requires_action and self.event_type in self.MANAGED_ACTION_EVENT_TYPES

    @property
    def visual_kind(self):
        if self.event_type.startswith("vacation_request_"):
            return "vacation"
        if self.event_type.startswith("schedule_change_"):
            return "transfer"
        if self.event_type == self.TYPE_SCHEDULE_ITEM_CHANGED_BY_MANAGER:
            return "schedule"
        if self.event_type == self.TYPE_UPCOMING_VACATION_REMINDER:
            return "reminder"
        if self.event_type.startswith("urgent_closure_"):
            return "urgent_closure"
        if self.event_type in {self.TYPE_PREFERENCES_COLLECTION_STARTED, self.TYPE_SCHEDULE_REVIEW_REQUESTED}:
            return "planning"
        return "system"

    @property
    def visual_icon(self):
        if self.is_active_task:
            return "task_alt"
        if self.status == self.STATUS_DONE:
            return "done_all"
        return {
            "vacation": "event_available",
            "transfer": "sync_alt",
            "schedule": "edit_calendar",
            "reminder": "notification_important",
            "urgent_closure": "event_busy",
            "planning": "event_note",
        }.get(self.visual_kind, "notifications")


class DemoBaselineSnapshot(models.Model):
    key = models.CharField(max_length=80, unique=True, verbose_name="Ключ снимка")
    planning_year = models.PositiveIntegerField(verbose_name="Год планирования")
    seed_value = models.IntegerField(null=True, blank=True, verbose_name="Seed")
    payload = models.JSONField(default=dict, blank=True, verbose_name="Данные снимка")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        db_table = "core_demo_baseline_snapshot"
        verbose_name = "Начальный снимок демо-данных"
        verbose_name_plural = "Начальные снимки демо-данных"
        ordering = ["key"]

    def __str__(self):
        return f"{self.key}: {self.planning_year}"


class DemoDataResetJob(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "В очереди"),
        (STATUS_RUNNING, "Выполняется"),
        (STATUS_SUCCEEDED, "Завершено"),
        (STATUS_FAILED, "Ошибка"),
    ]

    token = models.CharField(max_length=96, unique=True, verbose_name="Токен статуса")
    status = models.CharField(max_length=24, choices=STATUS_CHOICES, default=STATUS_QUEUED, verbose_name="Статус")
    seed_value = models.PositiveIntegerField(verbose_name="Seed")
    progress_percent = models.PositiveSmallIntegerField(default=0, verbose_name="Прогресс")
    stage_label = models.CharField(max_length=180, blank=True, default="", verbose_name="Текущий этап")
    message = models.TextField(blank=True, default="", verbose_name="Сообщение")
    error_message = models.TextField(blank=True, default="", verbose_name="Ошибка")
    process_id = models.PositiveIntegerField(null=True, blank=True, verbose_name="PID процесса")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата запуска")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="Дата завершения")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="Дата обновления")

    class Meta:
        db_table = "core_demo_data_reset_job"
        verbose_name = "Задание пересоздания демо-данных"
        verbose_name_plural = "Задания пересоздания демо-данных"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="core_demo_job_status_idx"),
        ]

    def __str__(self):
        return f"Reset job #{self.id}: {self.get_status_display()}"
