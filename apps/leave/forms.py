from django import forms

from apps.leave.models import VacationPreference, VacationRequest

from .services.dates import get_chargeable_leave_days, quantize_leave_days
from .services.ledger import get_employee_available_balance
from .services.preferences import get_paid_leave_available_from
from .services.validation import (
    MIN_CONTINUOUS_PAID_LEAVE_DAYS,
    validate_schedule_change_request,
    validate_vacation_request_for_employee,
)


class VacationRequestCreateForm(forms.ModelForm):
    class Meta:
        model = VacationRequest
        fields = ["vacation_type", "start_date", "end_date", "reason"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "reason": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data

        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        vacation_type = cleaned_data.get("vacation_type")
        if self.employee and start_date and end_date and vacation_type:
            validate_vacation_request_for_employee(
                employee=self.employee,
                start_date=start_date,
                end_date=end_date,
                vacation_type=vacation_type,
            )
        return cleaned_data


class ScheduleChangeRequestCreateForm(forms.Form):
    new_start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    new_end_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, schedule_item=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.schedule_item = schedule_item

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data

        new_start_date = cleaned_data.get("new_start_date")
        new_end_date = cleaned_data.get("new_end_date")
        if self.schedule_item and new_start_date and new_end_date:
            validate_schedule_change_request(self.schedule_item, new_start_date, new_end_date)
        return cleaned_data


class VacationPreferenceResponseForm(forms.Form):
    REMAINDER_POLICY_CHOICES = VacationPreference.REMAINDER_POLICY_CHOICES

    primary_start_date = forms.DateField(
        required=False,
        label="Дата начала основного отпуска",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    primary_end_date = forms.DateField(
        required=False,
        label="Дата окончания основного отпуска",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    backup_start_date = forms.DateField(
        required=False,
        label="Дата начала запасного отпуска",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    backup_end_date = forms.DateField(
        required=False,
        label="Дата окончания запасного отпуска",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    comment = forms.CharField(required=False, label="Комментарий", widget=forms.Textarea(attrs={"rows": 3}))
    no_preferences = forms.BooleanField(required=False, label="Нет пожеланий")
    remainder_policy = forms.ChoiceField(
        required=False,
        choices=REMAINDER_POLICY_CHOICES,
        initial=VacationPreference.REMAINDER_AUTO,
        label="Что делать с остатком дней",
        widget=forms.RadioSelect,
    )

    def __init__(self, *args, employee=None, collection=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        self.collection = collection
        self.available_balance = None
        if employee is not None and collection is not None:
            self.available_balance = quantize_leave_days(
                get_employee_available_balance(employee, f"{collection.year}-12-31")
            )

    def _clean_period(self, cleaned_data, start_field, end_field, label):
        start_date = cleaned_data.get(start_field)
        end_date = cleaned_data.get(end_field)
        if not start_date or not end_date:
            raise forms.ValidationError(f"Укажите даты для блока «{label}».")
        if end_date < start_date:
            raise forms.ValidationError(f"В блоке «{label}» дата окончания не может быть раньше даты начала.")

        year = self.collection.year
        if start_date.year != year or end_date.year != year:
            raise forms.ValidationError(f"В блоке «{label}» даты должны быть в пределах {year} года.")

        available_from = get_paid_leave_available_from(self.employee)
        if start_date < available_from:
            raise forms.ValidationError(
                f"В блоке «{label}» оплачиваемый отпуск доступен с {available_from:%d.%m.%Y}."
            )
        chargeable_days = quantize_leave_days(get_chargeable_leave_days(start_date, end_date, "paid"))
        calendar_days = (end_date - start_date).days + 1
        if (
            self.available_balance is not None
            and self.available_balance >= MIN_CONTINUOUS_PAID_LEAVE_DAYS
            and calendar_days < MIN_CONTINUOUS_PAID_LEAVE_DAYS
        ):
            raise forms.ValidationError(
                (
                    f"В блоке «{label}» выбрано {calendar_days:g} д. "
                    f"Укажите не меньше {MIN_CONTINUOUS_PAID_LEAVE_DAYS} д., "
                    "чтобы в графике была нормальная непрерывная часть отпуска."
                )
            )
        if self.available_balance is not None and chargeable_days > self.available_balance:
            raise forms.ValidationError(
                (
                    f"В блоке «{label}» выбрано {chargeable_days:g} д., "
                    f"а доступно к планированию {self.available_balance:g} д."
                )
            )
        cleaned_data[f"{start_field}_chargeable_days"] = chargeable_days

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data
        if cleaned_data.get("no_preferences"):
            cleaned_data["remainder_policy"] = VacationPreference.REMAINDER_AUTO
            return cleaned_data

        cleaned_data["remainder_policy"] = cleaned_data.get("remainder_policy") or VacationPreference.REMAINDER_AUTO

        self._clean_period(
            cleaned_data,
            "primary_start_date",
            "primary_end_date",
            "Основной отпуск",
        )
        self._clean_period(
            cleaned_data,
            "backup_start_date",
            "backup_end_date",
            "Запасной отпуск",
        )
        return cleaned_data

