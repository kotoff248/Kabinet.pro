(function () {
    "use strict";

    const DAY_MS = 24 * 60 * 60 * 1000;

    function parseIsoDate(value) {
        if (!value) {
            return null;
        }
        const parts = value.split("-").map(Number);
        if (parts.length !== 3 || parts.some(Number.isNaN)) {
            return null;
        }
        return new Date(parts[0], parts[1] - 1, parts[2]);
    }

    function formatRuDate(date) {
        if (!date) {
            return "";
        }
        return date.toLocaleDateString("ru-RU");
    }

    function inclusiveDays(start, end) {
        return Math.round((end.getTime() - start.getTime()) / DAY_MS) + 1;
    }

    function setAlert(alert, message, tone) {
        if (!alert) {
            return;
        }
        alert.textContent = message || "";
        alert.hidden = !message;
        alert.classList.remove(
            "preferences-live-alert--success",
            "preferences-live-alert--warning",
            "preferences-live-alert--error",
        );
        if (message && tone) {
            alert.classList.add(`preferences-live-alert--${tone}`);
        }
    }

    function setText(element, text) {
        if (element) {
            element.textContent = text;
        }
    }

    function getNavigation() {
        return window.KabinetNavigation || {};
    }

    function getDraftKey(collectionYear) {
        if (!Number.isFinite(collectionYear)) {
            return "";
        }
        return `calendar:preferences-draft:${collectionYear}`;
    }

    function readDraft(draftKey) {
        if (!draftKey) {
            return null;
        }

        try {
            return JSON.parse(sessionStorage.getItem(draftKey) || "null");
        } catch (error) {
            return null;
        }
    }

    function writeDraft(draftKey, draft) {
        if (!draftKey || !draft) {
            return;
        }

        try {
            sessionStorage.setItem(draftKey, JSON.stringify(draft));
        } catch (error) {
        }
    }

    function clearDraft(draftKey) {
        if (!draftKey) {
            return;
        }

        try {
            sessionStorage.removeItem(draftKey);
        } catch (error) {
        }
    }

    function samePreferencePath(firstHref, secondHref) {
        try {
            const first = new URL(firstHref, window.location.href);
            const second = new URL(secondHref, window.location.href);
            return first.origin === second.origin && first.pathname === second.pathname;
        } catch (error) {
            return false;
        }
    }

    function clearCurrentActivePreference() {
        const navigation = getNavigation();
        if (
            typeof navigation.getActiveCalendarPreferenceHref === "function"
            && typeof navigation.clearActiveCalendarPreferenceHref === "function"
            && samePreferencePath(navigation.getActiveCalendarPreferenceHref(), window.location.href)
        ) {
            navigation.clearActiveCalendarPreferenceHref();
        }
    }

    function rememberCurrentPreference() {
        const navigation = getNavigation();
        if (typeof navigation.rememberActiveCalendarPreferenceHref === "function") {
            navigation.rememberActiveCalendarPreferenceHref(window.location.href);
        }
    }

    function syncCalendarReturnLinks(collectionYear) {
        const navigation = getNavigation();
        const fallbackHref = `/calendar/?view=year&year=${collectionYear}`;
        const href = typeof navigation.getRememberedCalendarHref === "function"
            ? navigation.getRememberedCalendarHref(fallbackHref)
            : fallbackHref;

        document.querySelectorAll("[data-calendar-return-link]").forEach((link) => {
            link.href = href;
        });
    }

    function initPreferencesForm(form) {
        if (!form || form.dataset.preferencesInitialized === "true") {
            return;
        }
        form.dataset.preferencesInitialized = "true";

        const collectionYear = Number(form.dataset.collectionYear);
        const planningYear = Number(form.dataset.planningYear);
        const editable = form.dataset.editable === "true";
        const preferenceState = form.dataset.preferenceState || "";
        const paidLeaveAvailableFrom = parseIsoDate(form.dataset.paidLeaveAvailableFrom);
        const availableBalance = Number(form.dataset.availableBalance) || 0;
        const minContinuousDays = Number(form.dataset.minContinuousDays) || 14;
        const noPreferences = form.querySelector('input[name="no_preferences"]');
        const remainderInputs = Array.from(form.querySelectorAll('input[name="remainder_policy"]'));
        const submitButton = form.querySelector("[data-preferences-submit]");
        const alert = form.querySelector("[data-preferences-alert]");
        const totalHint = form.querySelector("[data-preferences-total-hint]");
        const comment = form.querySelector('[name="comment"]');
        const aiPreviewUrl = form.dataset.aiPreviewUrl || "";
        const aiPanel = form.querySelector("[data-preferences-ai]");
        const aiTitle = form.querySelector("[data-preferences-ai-title]");
        const aiSubtitle = form.querySelector("[data-preferences-ai-subtitle]");
        const aiDetail = form.querySelector("[data-preferences-ai-detail]");
        const aiOptionElements = {
            primary: form.querySelector('[data-preferences-ai-option="primary"]'),
            backup: form.querySelector('[data-preferences-ai-option="backup"]'),
        };
        const aiScores = {
            primary: form.querySelector('[data-preferences-ai-score="primary"]'),
            backup: form.querySelector('[data-preferences-ai-score="backup"]'),
        };
        const aiMessages = {
            primary: form.querySelector('[data-preferences-ai-message="primary"]'),
            backup: form.querySelector('[data-preferences-ai-message="backup"]'),
        };
        let aiPreviewTimer = null;
        let aiPreviewRequestId = 0;
        const draftKey = getDraftKey(collectionYear);
        const isActiveEditableCollection = editable
            && collectionYear === planningYear
            && preferenceState !== "filled"
            && preferenceState !== "skipped";
        const periods = {
            primary: {
                label: "Основной отпуск",
                start: form.querySelector('input[name="primary_start_date"]'),
                end: form.querySelector('input[name="primary_end_date"]'),
                fieldset: form.querySelector('[data-preference-period="primary"]'),
                feedback: form.querySelector('[data-period-feedback="primary"]'),
                days: form.querySelector('[data-preference-days="primary"]'),
                message: form.querySelector('[data-preference-message="primary"]'),
            },
            backup: {
                label: "Запасной отпуск",
                start: form.querySelector('input[name="backup_start_date"]'),
                end: form.querySelector('input[name="backup_end_date"]'),
                fieldset: form.querySelector('[data-preference-period="backup"]'),
                feedback: form.querySelector('[data-period-feedback="backup"]'),
                days: form.querySelector('[data-preference-days="backup"]'),
                message: form.querySelector('[data-preference-message="backup"]'),
            },
        };
        const totalDays = form.querySelector('[data-preference-days="total"]');
        const dateInputs = Object.values(periods).flatMap((period) => [period.start, period.end]).filter(Boolean);

        syncCalendarReturnLinks(collectionYear);

        function collectDraft() {
            return {
                noPreferences: Boolean(noPreferences && noPreferences.checked),
                primaryStart: periods.primary.start ? periods.primary.start.value : "",
                primaryEnd: periods.primary.end ? periods.primary.end.value : "",
                backupStart: periods.backup.start ? periods.backup.start.value : "",
                backupEnd: periods.backup.end ? periods.backup.end.value : "",
                remainderPolicy: (remainderInputs.find((input) => input.checked) || {}).value || "auto",
                comment: comment ? comment.value : "",
            };
        }

        function applyDraft(draft) {
            if (!draft || !isActiveEditableCollection) {
                return;
            }

            if (noPreferences) {
                noPreferences.checked = Boolean(draft.noPreferences);
            }
            if (periods.primary.start) {
                periods.primary.start.value = draft.primaryStart || "";
            }
            if (periods.primary.end) {
                periods.primary.end.value = draft.primaryEnd || "";
            }
            if (periods.backup.start) {
                periods.backup.start.value = draft.backupStart || "";
            }
            if (periods.backup.end) {
                periods.backup.end.value = draft.backupEnd || "";
            }
            if (draft.remainderPolicy) {
                remainderInputs.forEach((input) => {
                    input.checked = input.value === draft.remainderPolicy;
                });
            }
            if (comment) {
                comment.value = draft.comment || "";
            }
        }

        function saveDraft() {
            if (!isActiveEditableCollection) {
                return;
            }

            writeDraft(draftKey, collectDraft());
            rememberCurrentPreference();
        }

        if (isActiveEditableCollection) {
            applyDraft(readDraft(draftKey));
            rememberCurrentPreference();
        } else {
            clearDraft(draftKey);
            clearCurrentActivePreference();
        }

        function openNativeDatePicker(input) {
            if (!input || input.disabled || input.readOnly) {
                return;
            }
            if (window.KabinetDatePicker && typeof window.KabinetDatePicker.open === "function") {
                window.KabinetDatePicker.open(input);
            }
        }

        function setPeriodState(period, state, message, days) {
            if (period.fieldset) {
                period.fieldset.dataset.validationState = state || "";
            }
            setText(period.feedback, message || "");
            setText(period.days, days > 0 ? `${days} д.` : "0 д.");
            setText(period.message, message || "Выберите даты");
        }

        function setAiTone(tone) {
            if (!aiPanel) {
                return;
            }
            aiPanel.classList.remove(
                "preferences-ai--idle",
                "preferences-ai--loading",
                "preferences-ai--prefer",
                "preferences-ai--normal",
                "preferences-ai--avoid",
                "preferences-ai--blocked",
                "preferences-ai--error",
            );
            aiPanel.classList.add(`preferences-ai--${tone || "idle"}`);
        }

        function resetAiPreview(title, detail) {
            aiPreviewRequestId += 1;
            if (aiPreviewTimer) {
                window.clearTimeout(aiPreviewTimer);
                aiPreviewTimer = null;
            }
            setAiTone("idle");
            setText(aiTitle, title || "Выберите основной и запасной период");
            setText(aiSubtitle, "Модуль сравнит, какой вариант удобнее для будущего графика.");
            setText(aiDetail, detail || "Оценка появится после выбора дат.");
            setText(aiScores.primary, "-");
            setText(aiScores.backup, "-");
            setText(aiMessages.primary, "Выберите даты");
            setText(aiMessages.backup, "Выберите даты");
            Object.values(aiOptionElements).forEach((element) => {
                if (element) {
                    element.dataset.aiOptionState = "";
                }
            });
        }

        function setAiLoading() {
            setAiTone("loading");
            setText(aiTitle, "Модуль проверяет варианты");
            setText(aiSubtitle, "Оценка обновится автоматически.");
            setText(aiDetail, "Сравниваем основной и запасной период.");
        }

        function renderAiOption(key, option, winner) {
            const score = option && (option.module_score_label || option.score_label);
            const message = option && (option.module_recommendation_label || option.block_reason || option.module_action);
            setText(aiScores[key], score || "-");
            setText(aiMessages[key], message || "Нет оценки");
            const element = aiOptionElements[key];
            if (element) {
                element.dataset.aiOptionState = winner === key ? "winner" : "";
            }
        }

        function renderAiPreview(payload) {
            if (!payload || payload.ok === false) {
                setAiTone("error");
                setText(aiTitle, "Оценка временно недоступна");
                setText(aiSubtitle, "");
                setText(aiDetail, (payload && payload.message) || "Попробуйте изменить даты.");
                return;
            }
            setAiTone(payload.tone || "normal");
            setText(aiTitle, payload.summary || payload.winner_label || "Оценка модуля готова");
            setText(aiSubtitle, payload.winner_label || "");
            setText(aiDetail, payload.detail || "");
            renderAiOption("primary", payload.primary || {}, payload.winner);
            renderAiOption("backup", payload.backup || {}, payload.winner);
        }

        function scheduleAiPreview(primaryState, backupState) {
            if (!aiPanel || !aiPreviewUrl) {
                return;
            }
            if (!primaryState.complete || !backupState.complete) {
                resetAiPreview();
                return;
            }
            if (aiPreviewTimer) {
                window.clearTimeout(aiPreviewTimer);
            }
            const requestId = aiPreviewRequestId + 1;
            aiPreviewRequestId = requestId;
            setAiLoading();
            aiPreviewTimer = window.setTimeout(() => {
                const params = new URLSearchParams({
                    primary_start_date: periods.primary.start ? periods.primary.start.value : "",
                    primary_end_date: periods.primary.end ? periods.primary.end.value : "",
                    backup_start_date: periods.backup.start ? periods.backup.start.value : "",
                    backup_end_date: periods.backup.end ? periods.backup.end.value : "",
                    remainder_policy: (remainderInputs.find((input) => input.checked) || {}).value || "auto",
                });
                fetch(`${aiPreviewUrl}?${params.toString()}`, {
                    method: "GET",
                    credentials: "same-origin",
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json",
                    },
                })
                    .then((response) => response.json())
                    .then((payload) => {
                        if (requestId === aiPreviewRequestId) {
                            renderAiPreview(payload);
                        }
                    })
                    .catch(() => {
                        if (requestId !== aiPreviewRequestId) {
                            return;
                        }
                        setAiTone("error");
                        setText(aiTitle, "Оценка временно недоступна");
                        setText(aiSubtitle, "");
                        setText(aiDetail, "Пожелания можно сохранить, если даты проходят обычную проверку.");
                    });
            }, 350);
        }

        function validatePeriod(period) {
            const start = parseIsoDate(period.start && period.start.value);
            const end = parseIsoDate(period.end && period.end.value);

            if (!start && !end) {
                return {
                    valid: false,
                    complete: false,
                    days: 0,
                    state: "warning",
                    message: "Выберите дату начала и окончания.",
                    firstInvalid: period.start,
                };
            }
            if (!start || !end) {
                return {
                    valid: false,
                    complete: false,
                    days: 0,
                    state: "warning",
                    message: start ? "Выберите дату окончания." : "Выберите дату начала.",
                    firstInvalid: start ? period.end : period.start,
                };
            }
            if (start.getFullYear() !== collectionYear || end.getFullYear() !== collectionYear) {
                return {
                    valid: false,
                    complete: true,
                    days: 0,
                    state: "error",
                    message: `Даты должны быть в пределах ${collectionYear} года.`,
                    firstInvalid: start.getFullYear() !== collectionYear ? period.start : period.end,
                };
            }
            if (end < start) {
                return {
                    valid: false,
                    complete: true,
                    days: 0,
                    state: "error",
                    message: "Дата окончания не может быть раньше даты начала.",
                    firstInvalid: period.end,
                };
            }
            if (paidLeaveAvailableFrom && start < paidLeaveAvailableFrom) {
                return {
                    valid: false,
                    complete: true,
                    days: inclusiveDays(start, end),
                    state: "error",
                    message: `Оплачиваемый отпуск доступен с ${formatRuDate(paidLeaveAvailableFrom)}.`,
                    firstInvalid: period.start,
                };
            }
            const days = inclusiveDays(start, end);
            if (availableBalance >= minContinuousDays && days < minContinuousDays) {
                return {
                    valid: false,
                    complete: true,
                    days,
                    state: "error",
                    message: `Выберите не меньше ${minContinuousDays} дн. подряд.`,
                    firstInvalid: period.end,
                };
            }
            return {
                valid: true,
                complete: true,
                days,
                state: "success",
                message: `${days} календарн. дн.`,
                firstInvalid: null,
            };
        }

        function updateSummary(options) {
            const fromSubmit = options && options.fromSubmit;
            const isPlanningYear = collectionYear === planningYear;

            dateInputs.forEach((input) => {
                input.disabled = !editable || (noPreferences && noPreferences.checked);
            });
            remainderInputs.forEach((input) => {
                input.disabled = !editable || (noPreferences && noPreferences.checked);
            });

            if (!isPlanningYear) {
                resetAiPreview(`${collectionYear} год заполнять не нужно.`, `Сейчас сбор пожеланий ведётся на ${planningYear} год.`);
                Object.values(periods).forEach((period) => {
                    setPeriodState(period, "warning", `${collectionYear} год заполнять не нужно.`, 0);
                });
                setText(totalDays, "0 д.");
                setText(totalHint, `Сейчас сбор пожеланий ведётся на ${planningYear} год.`);
                setAlert(
                    alert,
                    `Этот сбор относится к ${collectionYear} году. Сейчас нужно заполнять пожелания на ${planningYear} год.`,
                    "warning",
                );
                if (submitButton) {
                    submitButton.disabled = true;
                }
                return { valid: false, firstInvalid: null };
            }

            if (!editable) {
                resetAiPreview("Сбор закрыт для редактирования.", "Ответ можно просматривать, но нельзя изменить.");
                Object.values(periods).forEach((period) => {
                    setPeriodState(period, "warning", "Сбор закрыт для редактирования.", 0);
                });
                setText(totalDays, "0 д.");
                setText(totalHint, "Ответ можно просматривать, но нельзя изменить.");
                if (submitButton) {
                    submitButton.disabled = true;
                }
                return { valid: false, firstInvalid: null };
            }

            if (noPreferences && noPreferences.checked) {
                resetAiPreview("Оценка не нужна", "Вы выбрали вариант без пожеланий.");
                Object.values(periods).forEach((period) => {
                    setPeriodState(period, "", "Даты не нужны: выбран вариант без пожеланий.", 0);
                });
                setText(totalDays, "0 д.");
                setText(totalHint, "HR и руководитель поставят даты по производственной необходимости.");
                setAlert(alert, "Вы выбрали вариант «Нет пожеланий». Можно сохранить ответ.", "success");
                if (submitButton) {
                    submitButton.disabled = false;
                }
                return { valid: true, firstInvalid: null };
            }

            const primary = validatePeriod(periods.primary);
            const backup = validatePeriod(periods.backup);
            const remainderPolicy = (remainderInputs.find((input) => input.checked) || {}).value || "auto";
            setPeriodState(periods.primary, primary.state, primary.message, primary.days);
            setPeriodState(periods.backup, backup.state, backup.message, backup.days);
            scheduleAiPreview(primary, backup);

            const selectedDays = primary.days;
            setText(totalDays, selectedDays > 0 ? `${selectedDays} д.` : "0 д.");
            if (remainderPolicy === "approval") {
                setText(totalHint, "Остаток не попадёт в черновик без отдельного согласования.");
            } else if (remainderPolicy === "defer") {
                setText(totalHint, "Сверх основного периода система ничего не добавит.");
            } else {
                setText(totalHint, "Система сможет добрать остаток безопасными периодами.");
            }

            if (primary.valid && backup.valid) {
                setAlert(
                    alert,
                    `Можно сохранить: к планированию ${primary.days} д., запасной вариант ${backup.days} д.`,
                    "success",
                );
                if (submitButton) {
                    submitButton.disabled = false;
                }
                return { valid: true, firstInvalid: null };
            }

            const firstInvalid = primary.firstInvalid || backup.firstInvalid;
            const message = fromSubmit
                ? "Исправьте даты или отметьте «Нет пожеланий», чтобы сохранить ответ."
                : "Заполните основной и запасной период. Ошибки появятся здесь сразу.";
            setAlert(alert, message, primary.complete || backup.complete ? "error" : "warning");
            if (submitButton) {
                submitButton.disabled = false;
            }
            return { valid: false, firstInvalid };
        }

        dateInputs.forEach((input) => {
            input.addEventListener("click", () => {
                openNativeDatePicker(input);
            });
            input.addEventListener("focus", () => {
                openNativeDatePicker(input);
            });
            input.addEventListener("input", () => {
                updateSummary();
                saveDraft();
            });
            input.addEventListener("change", () => {
                updateSummary();
                saveDraft();
            });
        });
        if (noPreferences) {
            noPreferences.addEventListener("change", () => {
                updateSummary();
                saveDraft();
            });
        }
        remainderInputs.forEach((input) => {
            input.addEventListener("change", () => {
                updateSummary();
                saveDraft();
            });
        });
        if (comment) {
            comment.addEventListener("input", saveDraft);
            comment.addEventListener("change", saveDraft);
        }
        form.addEventListener("submit", (event) => {
            const result = updateSummary({ fromSubmit: true });
            if (!result.valid) {
                event.preventDefault();
                event.stopPropagation();
                if (result.firstInvalid && typeof result.firstInvalid.focus === "function") {
                    result.firstInvalid.focus();
                }
                saveDraft();
            }
        });

        updateSummary();
    }

    function initPage() {
        document.querySelectorAll("[data-preferences-form]").forEach(initPreferencesForm);
    }

    document.addEventListener("DOMContentLoaded", initPage, { once: true });
    document.addEventListener("app:navigation", initPage);
})();
