(function () {
    "use strict";

    const DEFAULT_TEXT = {
        title: "Запросить перенос отпуска",
        subtitle: "Выберите новые даты и укажите причину. Запрос уйдёт руководителю на согласование.",
        period: "Выбранный отпуск",
        hint: "Старый отпуск останется в графике, пока руководитель не согласует перенос.",
        submit: "Запросить перенос",
    };
    let returnContext = null;
    let isRestoringReturnContext = false;
    let previewController = null;
    let previewRequestId = 0;

    function getCurrentUrl() {
        return window.location.pathname + window.location.search;
    }

    function setText(node, value) {
        if (node) {
            node.textContent = value;
        }
    }

    function setFieldValue(form, name, value) {
        const field = form ? form.querySelector('[name="' + name + '"]') : null;
        if (field) {
            field.value = value || "";
        }
    }

    function getSubmitButton() {
        return document.getElementById("submit-transfer-btn");
    }

    function setSubmitEnabled(isEnabled) {
        const submitButton = getSubmitButton();
        if (submitButton) {
            submitButton.disabled = !isEnabled;
            submitButton.classList.toggle("is-disabled", !isEnabled);
        }
    }

    function formatNumber(value) {
        const numericValue = Number(value);
        if (!Number.isFinite(numericValue)) {
            return "—";
        }
        return numericValue.toLocaleString("ru-RU", {
            maximumFractionDigits: 1,
        });
    }

    function setPreviewValue(id, value) {
        setText(document.getElementById(id), formatNumber(value));
    }

    function getPreviewPanel() {
        return document.getElementById("transfer-preview-panel");
    }

    function setPreviewState(state) {
        const panel = getPreviewPanel();
        if (!panel) {
            return;
        }
        panel.classList.remove("is-idle", "is-loading", "is-ready", "is-warning", "is-error");
        panel.classList.add("is-" + state);
    }

    function setHint(message, state) {
        const hint = document.getElementById("transfer-form-hint");
        if (!hint) {
            return;
        }
        hint.textContent = message || hint.dataset.defaultText || DEFAULT_TEXT.hint;
        hint.classList.remove("is-error", "is-warning", "is-success");
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

    function resetPreview(options) {
        const form = document.getElementById("schedule-transfer-form");
        const risk = document.getElementById("transfer-preview-risk");
        abortPreviewRequest();
        setPreviewState("idle");
        setPreviewValue("transfer-preview-old-calendar", null);
        setPreviewValue("transfer-preview-old-chargeable", null);
        setPreviewValue("transfer-preview-new-calendar", null);
        setPreviewValue("transfer-preview-new-chargeable", null);
        setPreviewValue("transfer-preview-balance", null);
        setText(document.getElementById("transfer-preview-delta"), "Выберите даты");
        if (risk) {
            risk.hidden = true;
        }
        updateModulePreview(null);
        if (form) {
            form.dataset.previewCanSubmit = "false";
        }
        setSubmitEnabled(false);
        setHint(options && options.hint ? options.hint : "", "");
    }

    function updateRiskPreview(payload) {
        const risk = document.getElementById("transfer-preview-risk");
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
        setText(document.getElementById("transfer-preview-risk-label"), riskLabel || "Низкий");
        setText(document.getElementById("transfer-preview-risk-reason"), payload.risk_short_reason || "");
        setText(document.getElementById("transfer-preview-risk-action"), payload.risk_recommended_action || "");

        risk.classList.toggle("is-conflict", Boolean(payload.risk_is_conflict));
        risk.classList.toggle("is-high", payload.risk_label === "Высокий");
    }

    function moduleStatusVariant(recommendation) {
        if (recommendation === "prefer") {
            return "planned";
        }
        if (recommendation === "avoid") {
            return "medium";
        }
        if (recommendation === "blocked") {
            return "risk";
        }
        return "info";
    }

    function updateModulePreview(payload) {
        const module = document.getElementById("transfer-preview-module");
        const hasModule = Boolean(payload && payload.module_score_label);
        const recommendation = hasModule ? payload.module_recommendation || "normal" : "";
        if (module) {
            module.classList.remove(
                "schedule-transfer-preview__module--prefer",
                "schedule-transfer-preview__module--normal",
                "schedule-transfer-preview__module--avoid",
                "schedule-transfer-preview__module--blocked"
            );
            if (recommendation) {
                module.classList.add("schedule-transfer-preview__module--" + recommendation);
            }
            module.dataset.scheduleStatusVariant = moduleStatusVariant(recommendation);
            module.dataset.tooltipText = hasModule
                ? payload.module_action || payload.module_explanation || "Модуль оценил новые даты переноса."
                : "Выберите новые даты, чтобы получить подсказку нейромодуля.";
        }

        setText(
            document.getElementById("transfer-preview-module-label"),
            hasModule
                ? "Модуль " + payload.module_score_label + " · " + (payload.module_recommendation_label || "можно отправлять")
                : "Выберите даты"
        );
        setText(
            document.getElementById("transfer-preview-module-reason"),
            hasModule
                ? payload.module_explanation || "Модуль оценил новые даты переноса."
                : "Модуль оценит новые даты после проверки переноса."
        );
        setText(
            document.getElementById("transfer-preview-module-action"),
            hasModule
                ? payload.module_action || "Подсказка не заменяет жесткие правила переноса."
                : "Подсказка не заменяет жесткие правила переноса."
        );
    }

    function applyPreviewPayload(payload) {
        const form = document.getElementById("schedule-transfer-form");
        if (!form) {
            return;
        }

        setPreviewValue("transfer-preview-old-calendar", payload.old_calendar_days);
        setPreviewValue("transfer-preview-old-chargeable", payload.old_chargeable_days);
        setPreviewValue("transfer-preview-new-calendar", payload.new_calendar_days);
        setPreviewValue("transfer-preview-new-chargeable", payload.new_chargeable_days);
        setPreviewValue("transfer-preview-balance", payload.balance_after_change);
        setText(document.getElementById("transfer-preview-delta"), payload.chargeable_days_delta_label || "Без изменения");
        updateRiskPreview(payload);
        updateModulePreview(payload);

        const isWarning = Boolean(payload.risk_is_conflict) || payload.risk_label === "Высокий";
        if (payload.can_submit) {
            setPreviewState(isWarning ? "warning" : "ready");
            setHint(payload.message || "Перенос можно отправить.", isWarning ? "warning" : "success");
            form.dataset.previewCanSubmit = "true";
            setSubmitEnabled(true);
            return;
        }

        setPreviewState("error");
        setHint(payload.message || "Проверьте новые даты переноса.", "error");
        form.dataset.previewCanSubmit = "false";
        setSubmitEnabled(false);
    }

    function requestPreview() {
        const form = document.getElementById("schedule-transfer-form");
        if (!form) {
            return;
        }

        const startField = form.querySelector('[name="new_start_date"]');
        const endField = form.querySelector('[name="new_end_date"]');
        const previewUrl = form.dataset.transferPreviewUrl || "";
        const defaultHint = form.dataset.transferDefaultHint || DEFAULT_TEXT.hint;
        if (!startField || !endField) {
            return;
        }

        if (!startField.value || !endField.value) {
            resetPreview({ hint: defaultHint });
            return;
        }

        if (!previewUrl) {
            resetPreview({ hint: "Проверка переноса недоступна. Обновите страницу и попробуйте ещё раз." });
            setPreviewState("error");
            return;
        }

        abortPreviewRequest();
        const requestId = previewRequestId;
        previewController = new AbortController();
        setPreviewState("loading");
        setSubmitEnabled(false);
        setHint("Проверяем даты, баланс и риск состава...", "");

        const url = new URL(previewUrl, window.location.origin);
        url.searchParams.set("new_start_date", startField.value);
        url.searchParams.set("new_end_date", endField.value);

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
                        message: "Не удалось разобрать ответ проверки переноса.",
                    };
                }).then(function (payload) {
                    if (!response.ok && !payload.message) {
                        payload.message = "Не удалось проверить перенос.";
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
                updateModulePreview(null);
                setHint("Не удалось проверить перенос. Проверьте соединение и попробуйте ещё раз.", "error");
                if (form) {
                    form.dataset.previewCanSubmit = "false";
                }
                setSubmitEnabled(false);
            });
    }

    function captureReturnContext(trigger, transferModal) {
        const sourceModal = trigger.closest(".app-modal.is-open");
        if (!sourceModal || sourceModal === transferModal) {
            return null;
        }

        return {
            modal: sourceModal,
            scrollTargets: Array.from(sourceModal.querySelectorAll("*"))
                .concat(sourceModal)
                .filter(function (node) {
                    const style = window.getComputedStyle(node);
                    const scrollsY = /auto|scroll/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1;
                    const scrollsX = /auto|scroll/.test(style.overflowX) && node.scrollWidth > node.clientWidth + 1;
                    return node.scrollTop
                        || node.scrollLeft
                        || scrollsY
                        || scrollsX;
                })
                .map(function (node) {
                    return {
                        node: node,
                        scrollTop: node.scrollTop,
                        scrollLeft: node.scrollLeft,
                    };
                }),
        };
    }

    function restoreModalScroll(context) {
        if (!context || !Array.isArray(context.scrollTargets)) {
            return;
        }

        const applyScroll = function () {
            context.scrollTargets.forEach(function (target) {
                if (!target.node || !document.documentElement.contains(target.node)) {
                    return;
                }

                target.node.scrollTop = Math.max(0, Number(target.scrollTop) || 0);
                target.node.scrollLeft = Math.max(0, Number(target.scrollLeft) || 0);
            });
        };

        applyScroll();
        window.requestAnimationFrame(function () {
            applyScroll();
            window.requestAnimationFrame(applyScroll);
        });
    }

    function restoreReturnContext() {
        const context = returnContext;
        returnContext = null;
        if (
            !context
            || !context.modal
            || !document.documentElement.contains(context.modal)
            || !window.appModal
            || typeof window.appModal.open !== "function"
        ) {
            return;
        }

        window.appModal.open(context.modal);
        restoreModalScroll(context);
    }

    function closeTransferAndReturn() {
        const modal = document.getElementById("schedule-transfer-modal");
        isRestoringReturnContext = true;
        abortPreviewRequest();
        if (modal && window.appModal && typeof window.appModal.close === "function") {
            window.appModal.close(modal);
        }
        restoreReturnContext();
        isRestoringReturnContext = false;
    }

    function closeOtherModals(targetModal) {
        if (!window.appModal || typeof window.appModal.close !== "function") {
            return;
        }

        document.querySelectorAll(".app-modal.is-open").forEach(function (modal) {
            if (modal !== targetModal) {
                window.appModal.close(modal);
            }
        });
    }

    function openTransferModal(trigger) {
        const modal = document.getElementById("schedule-transfer-modal");
        const form = document.getElementById("schedule-transfer-form");
        if (!modal || !form || !trigger) {
            return;
        }

        const hintText = trigger.dataset.transferHint || DEFAULT_TEXT.hint;
        returnContext = captureReturnContext(trigger, modal);
        closeOtherModals(modal);
        form.action = trigger.dataset.transferUrl || "";
        form.dataset.transferPreviewUrl = trigger.dataset.transferPreviewUrl || "";
        form.dataset.transferDefaultHint = hintText;
        form.dataset.datePickerEmployeeId = trigger.dataset.transferEmployeeId || "";
        form.dataset.datePickerExcludeScheduleItem = trigger.dataset.transferExcludeScheduleItem || "";
        form.dataset.datePickerYear = trigger.dataset.transferYear || "";
        form.dataset.previewCanSubmit = "false";
        form.reset();
        setFieldValue(form, "next_url", trigger.dataset.transferNextUrl || getCurrentUrl());
        if (trigger.dataset.transferNextView) {
            setFieldValue(form, "next_view_mode", trigger.dataset.transferNextView);
        }
        if (trigger.dataset.transferNextYear) {
            setFieldValue(form, "next_year", trigger.dataset.transferNextYear);
        }
        if (trigger.dataset.transferNextMonth) {
            setFieldValue(form, "next_month", trigger.dataset.transferNextMonth);
        }

        setText(document.getElementById("transfer-current-period"), trigger.dataset.transferTitle || DEFAULT_TEXT.period);
        setText(document.getElementById("transfer-form-hint"), hintText);
        setText(getSubmitButton(), trigger.dataset.transferSubmitLabel || DEFAULT_TEXT.submit);
        setText(document.getElementById("schedule-transfer-modal-title"), trigger.dataset.transferModalTitle || DEFAULT_TEXT.title);
        setText(modal.querySelector(".app-modal__subtitle"), trigger.dataset.transferModalSubtitle || DEFAULT_TEXT.subtitle);
        resetPreview({ hint: hintText });

        if (window.appModal && typeof window.appModal.open === "function") {
            window.appModal.open(modal);
        }
    }

    document.addEventListener("click", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        const closeButton = target ? target.closest("[data-modal-close]") : null;
        if (closeButton && closeButton.closest("#schedule-transfer-modal") && returnContext) {
            event.preventDefault();
            event.stopImmediatePropagation();
            closeTransferAndReturn();
            return;
        }

        const trigger = target ? target.closest("[data-transfer-open]") : null;
        if (!trigger || event.defaultPrevented) {
            return;
        }

        event.preventDefault();
        event.stopImmediatePropagation();
        openTransferModal(trigger);
    });

    document.addEventListener("input", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !target.closest("#schedule-transfer-form")) {
            return;
        }
        if (target.matches('[name="new_start_date"], [name="new_end_date"]')) {
            requestPreview();
        }
    });

    document.addEventListener("change", function (event) {
        const target = event.target instanceof Element ? event.target : null;
        if (!target || !target.closest("#schedule-transfer-form")) {
            return;
        }
        if (target.matches('[name="new_start_date"], [name="new_end_date"]')) {
            requestPreview();
        }
    });

    document.addEventListener("submit", function (event) {
        if (!event.target || event.target.id !== "schedule-transfer-form") {
            return;
        }

        const form = event.target;
        const nextUrl = form.querySelector('[name="next_url"]');
        if (nextUrl && !nextUrl.value) {
            nextUrl.value = getCurrentUrl();
        }
        if (form.dataset.previewCanSubmit !== "true") {
            event.preventDefault();
            setHint("Сначала выберите новые даты и дождитесь успешной проверки.", "error");
            setPreviewState("error");
            setSubmitEnabled(false);
        }
    });

    document.addEventListener("app-modal:close", function (event) {
        if (event.target && event.target.id === "schedule-transfer-modal") {
            abortPreviewRequest();
        }
        if (
            event.target
            && event.target.id === "schedule-transfer-modal"
            && !isRestoringReturnContext
        ) {
            returnContext = null;
        }
    });

    document.addEventListener("keydown", function (event) {
        const modal = document.getElementById("schedule-transfer-modal");
        if (
            event.key === "Escape"
            && returnContext
            && modal
            && modal.classList.contains("is-open")
        ) {
            event.preventDefault();
            event.stopImmediatePropagation();
            closeTransferAndReturn();
        }
    });

    window.KabinetScheduleTransfer = {
        open: openTransferModal,
    };
})();
