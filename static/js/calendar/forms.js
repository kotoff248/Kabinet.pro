(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    Calendar.createFormsController = function (context, dependencies) {
        const signal = context.signal;

        function closeVacationModal() {
            dependencies.closeCustomSelects();
            if (!context.modal) {
                return;
            }

            window.appModal.close(context.modal);
        }

        function openTransferModal(trigger) {
            if (!context.transferModal || !context.transferForm || !trigger) {
                return;
            }

            dependencies.closeCustomSelects();
            dependencies.closeDetailModal();
            context.transferForm.action = trigger.dataset.transferUrl || "";
            context.transferForm.reset();
            dependencies.syncFormNavigationFields(context.transferForm);
            if (context.transferCurrentPeriod) {
                context.transferCurrentPeriod.textContent = trigger.dataset.transferTitle || "Выбранный отпуск";
            }
            window.appModal.open(context.transferModal);
        }

        function calculateChargeableDays(start, end, vacationType) {
            if (vacationType !== "paid") {
                return 0;
            }

            return Calendar.getDateRange(start, end).filter(function (currentDate) {
                return !context.holidayDates.has(Calendar.toIsoDate(currentDate));
            }).length;
        }

        function updateVacationHint(message, isError) {
            if (!context.vacationFormHint) {
                return;
            }

            context.vacationFormHint.textContent = message || context.vacationFormHint.dataset.defaultHint || "";
            context.vacationFormHint.classList.toggle("is-error", Boolean(isError));
        }

        function calculateVacationForm() {
            if (
                !context.startDateInput ||
                !context.endDateInput ||
                !context.submitButton ||
                !context.countDays ||
                !context.remainingBalance ||
                !context.chargeableDaysNode
            ) {
                return;
            }

            const startValue = context.startDateInput.value;
            const endValue = context.endDateInput.value;
            const defaultHint = context.vacationFormHint ? context.vacationFormHint.dataset.defaultHint : "";

            if (!startValue || !endValue) {
                context.countDays.textContent = "0 д.";
                context.chargeableDaysNode.textContent = "0 д.";
                context.remainingBalance.textContent = Calendar.formatDays(context.availableBalance) + " д.";
                updateVacationHint(defaultHint, false);
                context.submitButton.disabled = true;
                return;
            }

            const start = new Date(startValue + "T00:00:00");
            const end = new Date(endValue + "T00:00:00");
            if (end < start) {
                context.countDays.textContent = "0 д.";
                context.chargeableDaysNode.textContent = "0 д.";
                context.remainingBalance.textContent = Calendar.formatDays(context.availableBalance) + " д.";
                updateVacationHint("Дата окончания не может быть раньше даты начала.", true);
                context.submitButton.disabled = true;
                return;
            }

            const vacationType = context.vacationTypeSelect ? context.vacationTypeSelect.value : "paid";
            const calendarDays = Math.floor((end - start) / (1000 * 60 * 60 * 24)) + 1;
            const chargeableDays = calculateChargeableDays(start, end, vacationType);
            const remaining = vacationType === "paid"
                ? context.availableBalance - chargeableDays
                : context.availableBalance;

            context.countDays.textContent = calendarDays + " д.";
            context.chargeableDaysNode.textContent = chargeableDays + " д.";
            context.remainingBalance.textContent = Calendar.formatDays(remaining) + " д.";

            if (vacationType !== "paid") {
                updateVacationHint("Неоплачиваемый и учебный отпуск не уменьшают оплачиваемый баланс.", false);
                context.submitButton.disabled = false;
                return;
            }

            if (remaining < 0) {
                updateVacationHint("Недостаточно доступных дней для этой заявки.", true);
                context.submitButton.disabled = true;
                return;
            }

            if (chargeableDays === 0) {
                updateVacationHint("В выбранном периоде нет дней, которые спишутся с баланса.", true);
                context.submitButton.disabled = true;
                return;
            }

            updateVacationHint(defaultHint, false);
            context.submitButton.disabled = false;
        }

        function init() {
            document.addEventListener("click", function (event) {
                const transferTrigger = event.target.closest("[data-transfer-open]");
                if (!transferTrigger) {
                    return;
                }

                event.preventDefault();
                event.stopPropagation();
                openTransferModal(transferTrigger);
            }, { signal: signal });

            if (context.modal) {
                context.modal.addEventListener("app-modal:open", function () {
                    dependencies.closeDetailModal();
                    dependencies.closeCustomSelects();
                    const vacationForm = document.getElementById("vacation-plan-form");
                    dependencies.syncFormNavigationFields(vacationForm);
                    calculateVacationForm();
                }, { signal: signal });
            }

            const vacationForm = document.getElementById("vacation-plan-form");
            if (vacationForm) {
                vacationForm.addEventListener("submit", function () {
                    dependencies.syncFormNavigationFields(vacationForm);
                }, { signal: signal });
            }
            if (context.transferForm) {
                context.transferForm.addEventListener("submit", function () {
                    dependencies.syncFormNavigationFields(context.transferForm);
                }, { signal: signal });
            }

            if (context.startDateInput) {
                context.startDateInput.addEventListener("change", calculateVacationForm, { signal: signal });
            }
            if (context.endDateInput) {
                context.endDateInput.addEventListener("change", calculateVacationForm, { signal: signal });
            }
            if (context.vacationTypeSelect) {
                context.vacationTypeSelect.addEventListener("change", calculateVacationForm, { signal: signal });
            }
        }

        return {
            init: init,
            closeVacationModal: closeVacationModal,
        };
    };
})();
