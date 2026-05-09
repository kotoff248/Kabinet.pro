(function () {
    "use strict";

    let previewController = null;
    let previewRequestId = 0;
    let urgentPreviewController = null;
    let urgentPreviewRequestId = 0;

    function setText(node, value) {
        if (node) {
            node.textContent = value || "—";
        }
    }

    function getForm() {
        return document.getElementById("schedule-draft-placement-form");
    }

    function getSubmitButton() {
        return document.getElementById("submit-draft-placement-btn");
    }

    function formatNumber(value) {
        if (value === null || value === undefined || value === "") {
            return "—";
        }
        const numericValue = Number(value);
        if (!Number.isFinite(numericValue)) {
            return "—";
        }
        return numericValue.toLocaleString("ru-RU", { maximumFractionDigits: 1 });
    }

    function setPreviewValue(id, value) {
        setText(document.getElementById(id), formatNumber(value));
    }

    function setSubmitEnabled(isEnabled) {
        const button = getSubmitButton();
        if (button) {
            button.disabled = !isEnabled;
            button.classList.toggle("is-disabled", !isEnabled);
        }
    }

    function setPreviewState(state) {
        const panel = document.getElementById("draft-placement-preview-panel");
        if (!panel) {
            return;
        }
        panel.classList.remove("is-idle", "is-loading", "is-ready", "is-warning", "is-error");
        panel.classList.add("is-" + state);
    }

    function setHint(message, state) {
        const hint = document.getElementById("draft-placement-form-hint");
        if (!hint) {
            return;
        }
        hint.textContent = message || "Выберите даты, чтобы проверить списываемые дни, остаток и риск состава.";
        hint.classList.remove("is-success", "is-warning", "is-error");
        if (state) {
            hint.classList.add("is-" + state);
        }
    }

    function abortPreviewRequest() {
        previewRequestId += 1;
        if (previewController) {
            previewController.abort();
            previewController = null;
        }
    }

    function abortUrgentPreviewRequest() {
        urgentPreviewRequestId += 1;
        if (urgentPreviewController) {
            urgentPreviewController.abort();
            urgentPreviewController = null;
        }
    }

    function openNativeDatePicker(input) {
        if (!input || input.disabled || input.readOnly || typeof input.showPicker !== "function") {
            return;
        }
        try {
            input.showPicker();
        } catch (error) {
            // Browsers can require showPicker to run directly from a user gesture.
        }
    }

    function syncDateInputVisualState(input) {
        if (!input || input.type !== "date") {
            return;
        }
        input.classList.toggle("is-empty", !input.value);
    }

    function resetPreview() {
        const form = getForm();
        const risk = document.getElementById("draft-placement-risk");
        abortPreviewRequest();
        setPreviewState("idle");
        setPreviewValue("draft-placement-calendar-days", null);
        setPreviewValue("draft-placement-chargeable-days", null);
        setPreviewValue("draft-placement-remaining-days", null);
        setPreviewValue("draft-placement-merged-days", null);
        setText(document.getElementById("draft-placement-merged-period"), "Выберите даты");
        if (risk) {
            risk.hidden = true;
        }
        if (form) {
            form.dataset.previewCanSubmit = "false";
        }
        setSubmitEnabled(false);
        setHint("", "");
    }

    function updateRisk(payload) {
        const risk = document.getElementById("draft-placement-risk");
        if (!risk) {
            return;
        }

        const hasRiskText = payload.risk_short_reason || payload.risk_recommended_action || Number(payload.risk_score) > 0;
        risk.hidden = !hasRiskText;
        if (!hasRiskText) {
            return;
        }

        const riskLabel = payload.risk_score
            ? payload.risk_label + " · " + payload.risk_score + "%"
            : payload.risk_label;
        setText(document.getElementById("draft-placement-risk-label"), riskLabel || "Низкий");
        setText(document.getElementById("draft-placement-risk-reason"), payload.risk_short_reason || "");
        setText(document.getElementById("draft-placement-risk-action"), payload.risk_recommended_action || "");
        risk.classList.toggle("is-conflict", Boolean(payload.risk_is_conflict));
        risk.classList.toggle("is-high", payload.risk_label === "Высокий");
    }

    function setUrgentSubmitEnabled(form, isEnabled) {
        const button = form ? form.querySelector("[data-urgent-submit]") : null;
        if (button) {
            button.disabled = !isEnabled;
            button.classList.toggle("is-disabled", !isEnabled);
        }
        if (form) {
            form.dataset.urgentCanSubmit = isEnabled ? "true" : "false";
        }
    }

    function setUrgentHint(form, message, state) {
        const hint = form ? form.querySelector("[data-urgent-hint]") : null;
        if (!hint) {
            return;
        }
        hint.textContent = message || "Выберите предложенный период или укажите даты вручную.";
        hint.classList.remove("is-success", "is-warning", "is-error");
        if (state) {
            hint.classList.add("is-" + state);
        }
    }

    function setUrgentPreviewState(form, state) {
        const panel = form ? form.querySelector("[data-urgent-preview]") : null;
        if (!panel) {
            return;
        }
        panel.hidden = false;
        panel.classList.remove("is-idle", "is-loading", "is-ready", "is-warning", "is-error");
        panel.classList.add("is-" + state);
    }

    function resetUrgentPreview(form, hidePanel) {
        const panel = form ? form.querySelector("[data-urgent-preview]") : null;
        const risk = form ? form.querySelector("[data-urgent-risk]") : null;
        abortUrgentPreviewRequest();
        if (panel) {
            panel.classList.remove("is-loading", "is-ready", "is-warning", "is-error");
            panel.classList.add("is-idle");
            panel.hidden = Boolean(hidePanel);
        }
        setText(form ? form.querySelector("[data-urgent-period]") : null, "Выберите даты");
        setText(form ? form.querySelector("[data-urgent-calendar-days]") : null, null);
        setText(form ? form.querySelector("[data-urgent-chargeable-days]") : null, null);
        if (risk) {
            risk.hidden = true;
            risk.classList.remove("is-high", "is-conflict");
        }
    }

    function updateUrgentRisk(form, payload) {
        const risk = form ? form.querySelector("[data-urgent-risk]") : null;
        if (!risk) {
            return;
        }
        const hasRiskText = payload.risk_short_reason || payload.risk_recommended_action || Number(payload.risk_score) > 0;
        risk.hidden = !hasRiskText;
        risk.classList.toggle("is-conflict", Boolean(payload.risk_is_conflict));
        risk.classList.toggle("is-high", !payload.risk_is_conflict && (payload.risk_label === "Высокий" || payload.risk_level === "high"));
        if (!hasRiskText) {
            return;
        }

        const riskLabel = payload.risk_score
            ? payload.risk_label + " · " + payload.risk_score + "%"
            : payload.risk_label;
        setText(form.querySelector("[data-urgent-risk-label]"), riskLabel || "Низкий");
        setText(form.querySelector("[data-urgent-risk-reason]"), payload.risk_short_reason || "");
        setText(form.querySelector("[data-urgent-risk-action]"), payload.risk_recommended_action || "");
    }

    function applyUrgentPreviewPayload(form, payload) {
        if (!form) {
            return;
        }
        setText(form.querySelector("[data-urgent-period]"), payload.period_label || "Выбранный период");
        setText(form.querySelector("[data-urgent-calendar-days]"), formatNumber(payload.calendar_days));
        setText(form.querySelector("[data-urgent-chargeable-days]"), formatNumber(payload.chargeable_days));
        updateUrgentRisk(form, payload);

        const isWarning = Boolean(payload.risk_is_conflict) || payload.risk_label === "Высокий";
        if (payload.can_submit) {
            setUrgentPreviewState(form, isWarning ? "warning" : "ready");
            setUrgentHint(form, payload.message || "Период можно отправить руководителю.", isWarning ? "warning" : "success");
            setUrgentSubmitEnabled(form, true);
            return;
        }

        setUrgentPreviewState(form, "error");
        setUrgentHint(form, payload.message || "Проверьте выбранный период.", "error");
        setUrgentSubmitEnabled(form, false);
    }

    function getUrgentDateValues(form) {
        const startField = form ? form.querySelector('[name="manual_start_date"]') : null;
        const endField = form ? form.querySelector('[name="manual_end_date"]') : null;
        return {
            startField: startField,
            endField: endField,
            startDate: startField ? startField.value : "",
            endDate: endField ? endField.value : "",
        };
    }

    function clearUrgentSystemOptions(form) {
        if (!form) {
            return;
        }
        form.querySelectorAll('input[name="selected_option"]').forEach(function (radio) {
            radio.checked = false;
        });
    }

    function clearUrgentManualDates(form) {
        if (!form) {
            return;
        }
        form.querySelectorAll('input[type="date"]').forEach(function (input) {
            input.value = "";
            syncDateInputVisualState(input);
        });
    }

    function validateUrgentManualDatesLocally(form) {
        const values = getUrgentDateValues(form);
        if (!values.startDate && !values.endDate) {
            resetUrgentPreview(form, true);
            setUrgentHint(form, "Выберите предложенный период или укажите даты вручную.", "");
            setUrgentSubmitEnabled(form, false);
            return false;
        }
        if (!values.startDate || !values.endDate) {
            resetUrgentPreview(form, false);
            setUrgentPreviewState(form, "error");
            setUrgentHint(form, "Укажите дату начала и дату окончания.", "error");
            setUrgentSubmitEnabled(form, false);
            return false;
        }
        if (values.endDate < values.startDate) {
            resetUrgentPreview(form, false);
            setUrgentPreviewState(form, "error");
            setUrgentHint(form, "Дата окончания не может быть раньше даты начала.", "error");
            setUrgentSubmitEnabled(form, false);
            return false;
        }
        return true;
    }

    function requestUrgentPreview(form) {
        if (!form || !validateUrgentManualDatesLocally(form)) {
            return;
        }

        const previewUrl = form.dataset.urgentPreviewUrl || "";
        const values = getUrgentDateValues(form);
        if (!previewUrl) {
            resetUrgentPreview(form, false);
            setUrgentPreviewState(form, "error");
            setUrgentHint(form, "Проверка недоступна. Обновите страницу и попробуйте ещё раз.", "error");
            setUrgentSubmitEnabled(form, false);
            return;
        }

        abortUrgentPreviewRequest();
        const requestId = urgentPreviewRequestId;
        urgentPreviewController = new AbortController();
        setUrgentPreviewState(form, "loading");
        setUrgentHint(form, "Проверяем дни, срок использования и риск состава...", "");
        setUrgentSubmitEnabled(form, false);

        const url = new URL(previewUrl, window.location.origin);
        url.searchParams.set("start_date", values.startDate);
        url.searchParams.set("end_date", values.endDate);
        url.searchParams.set("required_days", form.querySelector('[name="required_days"]').value || "");
        url.searchParams.set("deadline", form.querySelector('[name="deadline"]').value || "");

        fetch(url.toString(), {
            method: "GET",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
            credentials: "same-origin",
            signal: urgentPreviewController.signal,
        })
            .then(function (response) {
                return response.json().catch(function () {
                    return {
                        can_submit: false,
                        message: "Не удалось разобрать ответ проверки.",
                    };
                }).then(function (payload) {
                    if (!response.ok && !payload.message) {
                        payload.message = "Не удалось проверить период.";
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                if (requestId !== urgentPreviewRequestId) {
                    return;
                }
                urgentPreviewController = null;
                applyUrgentPreviewPayload(form, payload);
            })
            .catch(function (error) {
                if (error && error.name === "AbortError") {
                    return;
                }
                if (requestId !== urgentPreviewRequestId) {
                    return;
                }
                urgentPreviewController = null;
                resetUrgentPreview(form, false);
                setUrgentPreviewState(form, "error");
                setUrgentHint(form, "Не удалось проверить период. Попробуйте ещё раз.", "error");
                setUrgentSubmitEnabled(form, false);
            });
    }

    function applyUrgentSystemOption(target) {
        const form = target ? target.closest(".schedule-draft-urgent-closure-form") : null;
        if (!form || !target.checked) {
            return;
        }
        abortUrgentPreviewRequest();
        clearUrgentManualDates(form);
        resetUrgentPreview(form, true);
        const isWarning = target.dataset.riskConflict === "true" || target.dataset.riskHigh === "true";
        setUrgentHint(form, target.dataset.optionMessage || "Период можно отправить руководителю.", isWarning ? "warning" : "success");
        setUrgentSubmitEnabled(form, true);
    }

    function resetUrgentForm(form) {
        if (!form) {
            return;
        }
        abortUrgentPreviewRequest();
        form.reset();
        form.querySelectorAll('input[type="date"]').forEach(syncDateInputVisualState);
        resetUrgentPreview(form, true);
        setUrgentHint(form, "Выберите предложенный период или укажите даты вручную.", "");
        setUrgentSubmitEnabled(form, false);
    }

    function restoreUrgentModalFromQuery() {
        const params = new URLSearchParams(window.location.search);
        const modalId = params.get("open_modal") || "";
        if (!modalId || modalId.indexOf("urgent-closure-") !== 0) {
            return;
        }

        const modal = document.getElementById(modalId);
        if (!modal) {
            return;
        }
        const errorMessage = params.get("modal_error") || "Период не отправлен. Проверьте даты и попробуйте ещё раз.";
        window.requestAnimationFrame(function () {
            if (window.appModal && typeof window.appModal.open === "function") {
                window.appModal.open(modal);
            }
            const form = modal.querySelector(".schedule-draft-urgent-closure-form");
            setUrgentHint(form, errorMessage, "error");
            setUrgentSubmitEnabled(form, false);
        });

        params.delete("open_modal");
        params.delete("modal_error");
        const nextQuery = params.toString();
        const nextUrl = window.location.pathname + (nextQuery ? "?" + nextQuery : "") + window.location.hash;
        window.history.replaceState({}, "", nextUrl);
    }

    function applyPreviewPayload(payload) {
        const form = getForm();
        if (!form) {
            return;
        }

        setPreviewValue("draft-placement-calendar-days", payload.calendar_days);
        setPreviewValue("draft-placement-chargeable-days", payload.chargeable_days);
        setPreviewValue("draft-placement-remaining-days", payload.remaining_after_placement);
        setPreviewValue("draft-placement-merged-days", payload.merged_chargeable_days);
        setText(
            document.getElementById("draft-placement-merged-period"),
            payload.will_merge ? payload.merged_period_label : "Без объединения",
        );
        updateRisk(payload);

        const isWarning = Boolean(payload.risk_is_conflict)
            || payload.risk_label === "Высокий"
            || Boolean(payload.short_gap_warning)
            || Boolean(payload.will_merge);

        if (payload.can_submit) {
            setPreviewState(isWarning ? "warning" : "ready");
            setHint(payload.message || "Период можно поставить в черновик.", isWarning ? "warning" : "success");
            form.dataset.previewCanSubmit = "true";
            setSubmitEnabled(true);
            return;
        }

        setPreviewState("error");
        setHint(payload.message || "Проверьте выбранный период.", "error");
        form.dataset.previewCanSubmit = "false";
        setSubmitEnabled(false);
    }

    function requestPreview() {
        const form = getForm();
        if (!form) {
            return;
        }

        const startField = form.querySelector('[name="start_date"]');
        const endField = form.querySelector('[name="end_date"]');
        const previewUrl = form.dataset.previewUrl || "";
        if (!startField || !endField) {
            return;
        }

        if (!startField.value || !endField.value) {
            resetPreview();
            return;
        }

        if (!previewUrl) {
            setPreviewState("error");
            setHint("Проверка недоступна. Обновите страницу и попробуйте ещё раз.", "error");
            setSubmitEnabled(false);
            return;
        }

        abortPreviewRequest();
        const requestId = previewRequestId;
        previewController = new AbortController();
        setPreviewState("loading");
        setSubmitEnabled(false);
        setHint("Проверяем даты, дни, остаток и риск состава...", "");

        const url = new URL(previewUrl, window.location.origin);
        url.searchParams.set("start_date", startField.value);
        url.searchParams.set("end_date", endField.value);

        fetch(url.toString(), {
            method: "GET",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
            },
            credentials: "same-origin",
            signal: previewController.signal,
        })
            .then(function (response) {
                return response.json().catch(function () {
                    return {
                        can_submit: false,
                        message: "Не удалось разобрать ответ проверки.",
                    };
                }).then(function (payload) {
                    if (!response.ok && !payload.message) {
                        payload.message = "Не удалось проверить период.";
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                if (requestId !== previewRequestId) {
                    return;
                }
                previewController = null;
                applyPreviewPayload(payload);
            })
            .catch(function (error) {
                if (error && error.name === "AbortError") {
                    return;
                }
                if (requestId !== previewRequestId) {
                    return;
                }
                previewController = null;
                setPreviewState("error");
                setHint("Не удалось проверить период. Попробуйте ещё раз.", "error");
                setSubmitEnabled(false);
            });
    }

    function openPlacementModal(trigger) {
        const modal = document.getElementById("schedule-draft-manual-modal");
        const form = getForm();
        if (!modal || !form || !trigger) {
            return;
        }

        form.action = trigger.dataset.manualActionUrl || "";
        form.dataset.previewUrl = trigger.dataset.manualPreviewUrl || "";
        form.dataset.previewCanSubmit = "false";
        form.reset();
        form.querySelectorAll('input[type="date"]').forEach(syncDateInputVisualState);
        const nextField = document.getElementById("draft-placement-next-url");
        if (nextField) {
            nextField.value = trigger.dataset.manualNextUrl || window.location.pathname + window.location.search;
        }

        setText(document.getElementById("schedule-draft-manual-modal-title"), trigger.dataset.manualEmployee || "Распределить отпуск");
        setText(modal.querySelector(".app-modal__subtitle"), trigger.dataset.manualSubtitle || "Выберите период и проверьте размещение.");
        setText(document.getElementById("draft-placement-employee"), trigger.dataset.manualEmployee || "");
        setText(document.getElementById("draft-placement-subtitle"), trigger.dataset.manualSubtitle || "");
        setText(document.getElementById("draft-placement-needed"), trigger.dataset.manualNeeded || "");
        setText(document.getElementById("draft-placement-status"), trigger.dataset.manualStatus || "");
        setText(document.getElementById("draft-placement-primary"), trigger.dataset.manualPrimary || "");
        setText(document.getElementById("draft-placement-backup"), trigger.dataset.manualBackup || "");
        setText(document.getElementById("draft-placement-placed"), trigger.dataset.manualPlaced || "");
        setText(document.getElementById("draft-placement-target"), trigger.dataset.manualTarget || "");
        setText(
            document.getElementById("draft-placement-reason"),
            [trigger.dataset.manualReason, trigger.dataset.manualDetail].filter(Boolean).join(" "),
        );
        resetPreview();

        if (window.appModal && typeof window.appModal.open === "function") {
            window.appModal.open(modal);
        }
    }

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const trigger = target ? target.closest("[data-draft-manual-open]") : null;
        if (!trigger || event.defaultPrevented) {
            return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        openPlacementModal(trigger);
    });

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !target.matches("#schedule-draft-placement-form input[type='date'], .schedule-draft-urgent-closure-form input[type='date']")) {
            return;
        }
        openNativeDatePicker(target);
    });

    document.addEventListener("focusin", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !target.matches("#schedule-draft-placement-form input[type='date'], .schedule-draft-urgent-closure-form input[type='date']")) {
            return;
        }
        openNativeDatePicker(target);
    });

    document.addEventListener("input", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (target && target.matches("#schedule-draft-placement-form input[type='date']")) {
            syncDateInputVisualState(target);
            requestPreview();
        }
        if (target && target.matches(".schedule-draft-urgent-closure-form input[type='date']")) {
            const form = target.closest(".schedule-draft-urgent-closure-form");
            if (form && target.value) {
                clearUrgentSystemOptions(form);
            }
            syncDateInputVisualState(target);
            requestUrgentPreview(form);
        }
    });

    document.addEventListener("change", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (target && target.matches("#schedule-draft-placement-form input[type='date']")) {
            syncDateInputVisualState(target);
            requestPreview();
        }
        if (target && target.matches(".schedule-draft-urgent-closure-form input[type='date']")) {
            const form = target.closest(".schedule-draft-urgent-closure-form");
            if (form && target.value) {
                clearUrgentSystemOptions(form);
            }
            syncDateInputVisualState(target);
            requestUrgentPreview(form);
        }
        if (target && target.matches('.schedule-draft-urgent-closure-form input[name="selected_option"]')) {
            applyUrgentSystemOption(target);
        }
    });

    document.addEventListener("submit", function (event) {
        if (!event.target || event.target.id !== "schedule-draft-placement-form") {
            return;
        }
        const form = event.target;
        if (form.dataset.previewCanSubmit !== "true") {
            event.preventDefault();
            setPreviewState("error");
            setHint("Сначала выберите даты и дождитесь успешной проверки.", "error");
            setSubmitEnabled(false);
        }
    });

    document.addEventListener("submit", function (event) {
        const form = event.target instanceof HTMLFormElement ? event.target : null;
        if (!form || !form.classList.contains("schedule-draft-urgent-closure-form")) {
            return;
        }

        if (form.dataset.urgentCanSubmit === "true") {
            return;
        }

        event.preventDefault();
        validateUrgentManualDatesLocally(form);
        if (form.dataset.urgentCanSubmit !== "true") {
            setUrgentHint(form, "Выберите предложенный период или дождитесь успешной проверки ручных дат.", "error");
            setUrgentSubmitEnabled(form, false);
        }
    });

    document.addEventListener("app-modal:open", function (event) {
        const modal = event.target instanceof Element ? event.target : null;
        if (!modal || !modal.id || modal.id.indexOf("urgent-closure-") !== 0) {
            return;
        }
        resetUrgentForm(modal.querySelector(".schedule-draft-urgent-closure-form"));
    });

    document.addEventListener("app-modal:close", function (event) {
        if (event.target && event.target.id === "schedule-draft-manual-modal") {
            abortPreviewRequest();
        }
        if (event.target && event.target.id && event.target.id.indexOf("urgent-closure-") === 0) {
            abortUrgentPreviewRequest();
        }
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", restoreUrgentModalFromQuery, { once: true });
    } else {
        restoreUrgentModalFromQuery();
    }
})();
