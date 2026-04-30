(function () {
    "use strict";

    const Calendar = window.KabinetCalendar || {};
    window.KabinetCalendar = Calendar;

    Calendar.createFormsController = function (context, dependencies) {
        const signal = context.signal;
        let previewRequestId = 0;
        let previewAbortController = null;
        let latestPreviewCanSubmit = false;

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

        function updateVacationHint(message, isError, isLoading) {
            if (!context.vacationFormHint) {
                return;
            }

            context.vacationFormHint.textContent = message || context.vacationFormHint.dataset.defaultHint || "";
            context.vacationFormHint.classList.toggle("is-error", Boolean(isError));
            context.vacationFormHint.classList.toggle("is-loading", Boolean(isLoading));
        }

        function formatDaysValue(value) {
            return Calendar.formatDays(Calendar.parseNumber(value, 0)) + " д.";
        }

        function updateEntitlementSource(payload) {
            const label = payload && payload.entitlement_source_label
                ? payload.entitlement_source_label
                : "Выберите даты, чтобы определить рабочий год списания.";
            const allocations = payload && Array.isArray(payload.entitlement_allocations)
                ? payload.entitlement_allocations
                : [];

            if (context.entitlementSourceLabel) {
                context.entitlementSourceLabel.textContent = label;
            }
            if (!context.entitlementSourceList) {
                return;
            }

            context.entitlementSourceList.replaceChildren();
            if (!allocations.length) {
                context.entitlementSourceList.hidden = true;
                return;
            }

            allocations.forEach(function (allocation) {
                const item = document.createElement("li");
                const period = document.createElement("span");
                const days = document.createElement("strong");

                period.textContent = allocation.period_label || "";
                days.textContent = "Списывается: " + formatDaysValue(allocation.days);
                item.append(period, days);
                context.entitlementSourceList.appendChild(item);
            });
            context.entitlementSourceList.hidden = false;
        }

        function setSubmitState(canSubmit) {
            latestPreviewCanSubmit = Boolean(canSubmit);
            if (context.submitButton) {
                context.submitButton.disabled = !latestPreviewCanSubmit;
            }
        }

        function setPreviewValues(payload) {
            const vacationType = context.vacationTypeSelect ? context.vacationTypeSelect.value : "paid";

            if (context.countDays) {
                context.countDays.textContent = (payload.calendar_days || 0) + " д.";
            }
            if (context.chargeableDaysNode) {
                context.chargeableDaysNode.textContent = vacationType === "paid"
                    ? (payload.chargeable_days || 0) + " д."
                    : "Не списывается";
            }
            if (context.availableOnStart) {
                context.availableOnStart.textContent = formatDaysValue(payload.available_on_start);
            }
            if (context.remainingBalance) {
                context.remainingBalance.textContent = formatDaysValue(payload.remaining_after_request);
            }
            if (context.balanceNode && payload.balance_today !== undefined) {
                context.availableBalance = Calendar.parseNumber(payload.balance_today, context.availableBalance || 0);
                context.balanceNode.dataset.balance = String(context.availableBalance);
                context.balanceNode.textContent = formatDaysValue(context.availableBalance);
            }
            updateEntitlementSource(payload);
        }

        function resetVacationPreview(message, isError) {
            if (
                !context.startDateInput ||
                !context.endDateInput ||
                !context.countDays ||
                !context.remainingBalance ||
                !context.chargeableDaysNode ||
                !context.availableOnStart
            ) {
                return;
            }

            const defaultHint = context.vacationFormHint ? context.vacationFormHint.dataset.defaultHint : "";

            context.countDays.textContent = "0 д.";
            context.chargeableDaysNode.textContent = "0 д.";
            context.availableOnStart.textContent = "0 д.";
            context.remainingBalance.textContent = formatDaysValue(context.availableBalance);
            updateEntitlementSource(null);
            updateVacationHint(message || defaultHint, Boolean(isError), false);
            setSubmitState(false);
        }

        function calculateVacationForm() {
            if (!context.startDateInput || !context.endDateInput || !context.previewUrl) {
                resetVacationPreview("Не удалось найти адрес проверки заявки.", true);
                return;
            }

            const startValue = context.startDateInput.value;
            const endValue = context.endDateInput.value;

            if (!startValue || !endValue) {
                resetVacationPreview("", false);
                return;
            }

            const start = new Date(startValue + "T00:00:00");
            const end = new Date(endValue + "T00:00:00");
            if (end < start) {
                resetVacationPreview("Дата окончания не может быть раньше даты начала.", true);
                return;
            }

            const vacationType = context.vacationTypeSelect ? context.vacationTypeSelect.value : "paid";
            const requestId = previewRequestId + 1;
            previewRequestId = requestId;
            setSubmitState(false);
            updateVacationHint("Проверяем данные...", false, true);

            if (previewAbortController) {
                previewAbortController.abort();
            }
            previewAbortController = new AbortController();

            const params = new URLSearchParams({
                start_date: startValue,
                end_date: endValue,
                vacation_type: vacationType,
            });

            fetch(context.previewUrl + "?" + params.toString(), {
                credentials: "same-origin",
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
                signal: previewAbortController.signal,
            })
                .then(function (response) {
                    return response.json().catch(function () {
                        return {
                            can_submit: false,
                            message: "Не удалось прочитать ответ проверки заявки.",
                        };
                    }).then(function (payload) {
                        return { ok: response.ok, payload: payload };
                    });
                })
                .then(function (result) {
                    if (signal.aborted || requestId !== previewRequestId) {
                        return;
                    }

                    const payload = result.payload || {};
                    setPreviewValues(payload);
                    setSubmitState(result.ok && payload.can_submit);
                    updateVacationHint(
                        payload.message || "Проверка завершена.",
                        !(result.ok && payload.can_submit),
                        false
                    );
                })
                .catch(function (error) {
                    if (error && error.name === "AbortError") {
                        return;
                    }
                    if (signal.aborted || requestId !== previewRequestId) {
                        return;
                    }
                    resetVacationPreview("Не удалось проверить заявку. Попробуйте ещё раз.", true);
                });
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
                    const vacationForm = context.vacationForm || document.getElementById("vacation-plan-form");
                    dependencies.syncFormNavigationFields(vacationForm);
                    calculateVacationForm();
                }, { signal: signal });
            }

            const vacationForm = context.vacationForm || document.getElementById("vacation-plan-form");
            if (vacationForm) {
                vacationForm.addEventListener("submit", function (event) {
                    if (!latestPreviewCanSubmit) {
                        event.preventDefault();
                        calculateVacationForm();
                        return;
                    }
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
