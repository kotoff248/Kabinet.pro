from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta

from django.utils import timezone

from apps.accounts.services import sync_employee_user
from apps.core.services.demo_seed.constants import DEFAULT_PASSWORD, DEPARTMENT_SPECS, FAST_EMPLOYEE_COUNTS, HR_COUNT
from apps.core.services.demo_urgent_closure_cases import demo_urgent_closure_join_date
from apps.employees.tenure import is_new_hire
from apps.employees.models import (
    DepartmentCoverageRule,
    Departments,
    EmployeePosition,
    Employees,
    ProductionGroup,
    ProductionGroupSubstitutionRule,
)
from apps.leave.models import (
    DepartmentStaffingRule,
    DepartmentWorkload,
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationPlanningCycle,
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
from apps.leave.services.dates import add_months_safe


class DemoSeedEnterpriseMixin:
    def _build_department_specs(self):
        specs = deepcopy(DEPARTMENT_SPECS)
        employee_counts = getattr(self, "demo_employee_counts", None)
        if employee_counts is None and self.fast_mode:
            employee_counts = FAST_EMPLOYEE_COUNTS
        if employee_counts is None:
            return specs

        for spec, employee_count in zip(specs, employee_counts):
            spec["employee_count"] = employee_count
            spec["recent_hires"] = 1 if employee_count >= 4 else 0
        return specs

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

    def _staffing_demo_reserve(self, staff_count):
        staff_count = int(staff_count or 0)
        if staff_count <= 1:
            return 0
        if staff_count <= 4:
            return 1
        return max(2, round(staff_count * 0.25))

    def _balanced_demo_staffing_limits(self, staff_count, min_staff_required, max_absent, *, allow_zero_min=False):
        staff_count = int(staff_count or 0)
        if staff_count <= 0:
            return 0, 0

        desired_min = max(0, int(min_staff_required or 0))
        desired_max_absent = max(1, int(max_absent or 1))
        reserve = min(staff_count, self._staffing_demo_reserve(staff_count))
        feasible_min = max(staff_count - reserve, 0)
        balanced_min = min(desired_min, feasible_min)
        if desired_min > 0 and not allow_zero_min:
            balanced_min = max(1, balanced_min)
        allowed_absent = max(staff_count - balanced_min, 1 if staff_count > 1 else 0)
        balanced_max_absent = min(desired_max_absent, allowed_absent)
        return balanced_min, balanced_max_absent

    def _department_group_active_count_on(self, department, production_group, as_of_date):
        return Employees.objects.filter(
            department=department,
            employee_position__production_group=production_group,
            is_active_employee=True,
            date_joined__lte=as_of_date,
        ).exclude(role__in=Employees.SERVICE_ROLES).count()

    def _normalize_current_coverage_rules(self, departments):
        as_of_date = date(self.schedule_end_year, 12, 31)
        for department in departments:
            coverage_rules = DepartmentCoverageRule.objects.filter(department=department).select_related("production_group")
            for coverage_rule in coverage_rules:
                group_count = self._department_group_active_count_on(
                    department,
                    coverage_rule.production_group,
                    as_of_date,
                )
                min_staff_required, max_absent = self._balanced_demo_staffing_limits(
                    group_count,
                    coverage_rule.min_staff_required,
                    coverage_rule.max_absent,
                    allow_zero_min=coverage_rule.min_staff_required == 0,
                )
                if (
                    coverage_rule.min_staff_required != min_staff_required
                    or coverage_rule.max_absent != max_absent
                ):
                    coverage_rule.min_staff_required = min_staff_required
                    coverage_rule.max_absent = max_absent
                    coverage_rule.save(update_fields=["min_staff_required", "max_absent"])

    def _create_staffing_rules(self, departments):
        self._normalize_current_coverage_rules(departments)
        for department, spec in zip(departments, self.department_specs):
            rule_spec = spec["staffing_rule"]
            active_count = self._department_active_count_on(department, date(self.schedule_end_year, 12, 31))
            min_staff_required, max_absent = self._balanced_demo_staffing_limits(
                active_count,
                rule_spec["min_staff_required"],
                rule_spec["max_absent"],
            )
            self.staffing_rules[department.id] = DepartmentStaffingRule.objects.create(
                department=department,
                min_staff_required=min_staff_required,
                max_absent=max_absent,
                criticality_level=rule_spec["criticality_level"],
                substitution_group=rule_spec["substitution_group"],
            )

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
        return self._balanced_demo_staffing_limits(active_count, min_staff_required, max_absent)

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
                eligible_start = max(date(year, 1, 1), add_months_safe(employee.date_joined, 6))
                eligible_end = date(year, 12, 20)
                if eligible_start > eligible_end:
                    continue
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
                    [14, 14, 21, 21, 28]
                    if remainder_policy == VacationPreference.REMAINDER_AUTO
                    else [14, 21, 21, 28, 28, 35]
                )
                primary_duration = self.rng.choice(primary_candidates)
                backup_duration = self.rng.choice(
                    [duration for duration in [14, 21, 28] if duration <= primary_duration] or [14]
                )
                preference_comments = preference_comments_by_policy.get(remainder_policy) or preference_comments_by_policy[
                    VacationPreference.REMAINDER_AUTO
                ]
                department_months = [
                    workload
                    for workload in self.department_workload.values()
                    if workload.department_id == employee.department_id and workload.year == year
                ]
                eligible_months = [
                    month
                    for month in range(eligible_start.month, eligible_end.month + 1)
                    if date(year, month, 1) <= eligible_end and self._month_end_date(year, month) >= eligible_start
                ]
                safe_months = [
                    workload.month
                    for workload in department_months
                    if workload.load_level <= 3 and workload.month in eligible_months
                ] or [month for month in [2, 3, 4, 6, 9, 10, 11] if month in eligible_months] or eligible_months
                quiet_months = [
                    workload.month
                    for workload in department_months
                    if workload.load_level <= 2 and workload.month in eligible_months
                ] or safe_months
                conflict_months = [
                    workload.month
                    for workload in department_months
                    if workload.load_level >= 4 and workload.month in eligible_months
                ]
                ordinary_months = [month for month in [2, 3, 4, 6, 7, 8, 9, 10, 11] if month in eligible_months] or eligible_months
                used_months = set()
                for priority, duration in [
                    (VacationPreference.PRIORITY_PRIMARY, primary_duration),
                    (VacationPreference.PRIORITY_BACKUP, backup_duration),
                ]:
                    if priority == VacationPreference.PRIORITY_PRIMARY:
                        if conflict_months and self.rng.random() < 0.18:
                            month_pool = conflict_months
                        elif self.rng.random() < 0.72:
                            month_pool = safe_months
                        else:
                            month_pool = ordinary_months
                    else:
                        if self.rng.random() < 0.76:
                            month_pool = quiet_months
                        elif self.rng.random() < 0.90:
                            month_pool = safe_months
                        else:
                            month_pool = ordinary_months
                    month_pool = [month for month in month_pool if month not in used_months] or month_pool
                    month = self.rng.choice(month_pool)
                    month_first = date(year, month, 1)
                    month_last = self._month_end_date(year, month)
                    valid_start = max(month_first, eligible_start)
                    valid_end = min(month_last, eligible_end)
                    if valid_start > valid_end:
                        continue
                    latest_start_for_full_duration = eligible_end - timedelta(days=duration - 1)
                    latest_start_for_min_duration = eligible_end - timedelta(days=13)
                    if valid_start <= latest_start_for_full_duration:
                        latest_valid_start = min(valid_end, latest_start_for_full_duration)
                    else:
                        latest_valid_start = min(valid_end, latest_start_for_min_duration)
                    if valid_start > latest_valid_start:
                        continue
                    earliest_day = valid_start.day
                    latest_day = min(10, latest_valid_start.day)
                    if earliest_day > latest_day:
                        latest_day = latest_valid_start.day
                    if earliest_day > latest_day:
                        continue
                    used_months.add(month)
                    start_day = self.rng.randint(earliest_day, latest_day)
                    start_date = date(year, month, start_day)
                    end_date = min(start_date + timedelta(days=duration - 1), eligible_end)
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
                date_joined = demo_urgent_closure_join_date(self.schedule_end_year, employee_index)
                if date_joined is None and slot >= recent_hire_start_slot:
                    date_joined = self._recent_hire_date(employee_index)
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
