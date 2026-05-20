(function () {
    "use strict";

    const POLL_INTERVAL_MS = 1500;
    let pollTimer = null;
    let neuralPollTimer = null;

    function clearPollTimer() {
        if (pollTimer) {
            window.clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    function clearNeuralPollTimer() {
        if (neuralPollTimer) {
            window.clearTimeout(neuralPollTimer);
            neuralPollTimer = null;
        }
    }

    function clampPercent(value) {
        const numericValue = Number(value);
        if (!Number.isFinite(numericValue)) {
            return 0;
        }
        return Math.max(0, Math.min(100, Math.round(numericValue)));
    }

    function setText(node, value) {
        if (node) {
            node.textContent = value === undefined || value === null || value === "" ? "—" : String(value);
        }
    }

    function updateJobClass(job, status) {
        job.classList.remove(
            "schedule-planning-auto-job--queued",
            "schedule-planning-auto-job--running",
            "schedule-planning-auto-job--succeeded",
            "schedule-planning-auto-job--failed",
        );
        job.classList.add("schedule-planning-auto-job--" + (status || "running"));
        job.dataset.status = status || "running";
    }

    function renderJob(job, payload) {
        const status = payload.status || "running";
        const percent = clampPercent(payload.progress_percent);
        updateJobClass(job, status);
        setText(job.querySelector("[data-planning-auto-job-stage]"), payload.stage_label || "Добрать незакрытые дни");
        setText(job.querySelector("[data-planning-auto-job-percent]"), percent + "%");
        setText(
            job.querySelector("[data-planning-auto-job-message]"),
            payload.error_message || payload.message || "Система добирает незакрытые дни и проверяет ограничения состава.",
        );
        setText(
            job.querySelector("[data-planning-auto-job-processed]"),
            (payload.processed_employees || 0) + " / " + (payload.total_employees || 0),
        );
        setText(job.querySelector("[data-planning-auto-job-placed]"), payload.placed_count || 0);
        setText(job.querySelector("[data-planning-auto-job-unresolved]"), payload.unresolved_count || 0);
        const bar = job.querySelector("[data-planning-auto-job-bar]");
        if (bar) {
            bar.style.width = percent + "%";
        }
    }

    function fetchStatus(statusUrl) {
        return fetch(statusUrl, {
            method: "GET",
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            },
        }).then(function (response) {
            return response.json().then(function (payload) {
                if (!response.ok || payload.ok === false) {
                    throw new Error(payload.message || payload.error_message || "Не удалось получить статус добора.");
                }
                return payload;
            });
        });
    }

    function reloadPlanningPage() {
        window.setTimeout(function () {
            window.location.reload();
        }, 1200);
    }

    function schedulePoll(job, statusUrl, delayMs) {
        clearPollTimer();
        pollTimer = window.setTimeout(function () {
            fetchStatus(statusUrl)
                .then(function (payload) {
                    renderJob(job, payload);
                    if (payload.status === "succeeded") {
                        setText(job.querySelector("[data-planning-auto-job-message]"), "Готово. Обновляю показатели черновика.");
                        clearPollTimer();
                        reloadPlanningPage();
                        return;
                    }
                    if (payload.status === "failed") {
                        clearPollTimer();
                        return;
                    }
                    schedulePoll(job, statusUrl, POLL_INTERVAL_MS);
                })
                .catch(function (error) {
                    renderJob(job, {
                        status: "failed",
                        progress_percent: 0,
                        stage_label: "Ошибка статуса",
                        error_message: error.message || "Не удалось получить статус добора.",
                    });
                    clearPollTimer();
                });
        }, delayMs);
    }

    function initPlanningAutoJob() {
        const previousController = window.__schedulePlanningAutoJobController;
        if (previousController) {
            previousController.abort();
            window.__schedulePlanningAutoJobController = null;
        }
        clearPollTimer();

        const root = document.querySelector("[data-page='schedule-planning']");
        const job = root ? root.querySelector("[data-planning-auto-job]") : null;
        if (!job) {
            return;
        }

        const controller = new AbortController();
        window.__schedulePlanningAutoJobController = controller;
        controller.signal.addEventListener("abort", clearPollTimer, { once: true });

        const statusUrl = job.dataset.statusUrl || "";
        const status = job.dataset.status || "";
        if (!statusUrl || status === "failed") {
            return;
        }
        if (status === "succeeded") {
            reloadPlanningPage();
            return;
        }
        schedulePoll(job, statusUrl, 250);
    }

    function csrfTokenFromForm(form) {
        const input = form ? form.querySelector("input[name='csrfmiddlewaretoken']") : null;
        return input ? input.value : "";
    }

    function updateNeuralClass(card, status) {
        card.classList.remove(
            "schedule-planning-neural-card--ok",
            "schedule-planning-neural-card--warning",
            "schedule-planning-neural-card--muted",
            "schedule-planning-neural-card--info",
            "schedule-planning-neural-card--queued",
            "schedule-planning-neural-card--running",
            "schedule-planning-neural-card--succeeded",
            "schedule-planning-neural-card--failed",
        );
        if (status) {
            card.classList.add("schedule-planning-neural-card--" + status);
        }
        card.dataset.status = status || "";
    }

    function renderNeuralJob(card, payload) {
        const status = payload.status || "running";
        const percent = clampPercent(payload.progress_percent);
        updateNeuralClass(card, status);
        setText(card.querySelector("[data-neural-training-status-label]"), payload.stage_label || payload.message || status);
        setText(
            card.querySelector("[data-neural-training-message]"),
            payload.error_message || payload.message || "Переобучаю нейромодуль.",
        );
        const bar = card.querySelector("[data-neural-training-bar]");
        if (bar) {
            bar.style.width = percent + "%";
        }
        const metrics = payload.metrics_payload || {};
        const source = metrics.source || {};
        const candidate = metrics.candidate || {};
        const packageMetrics = metrics.package || {};
        if (source.years) {
            setText(card.querySelector("[data-neural-training-years]"), source.years.join(", "));
        }
        if (candidate.examples_count !== undefined) {
            setText(card.querySelector("[data-neural-training-candidate-count]"), candidate.examples_count);
        }
        if (packageMetrics.examples_count !== undefined) {
            setText(card.querySelector("[data-neural-training-package-count]"), packageMetrics.examples_count);
        }
        const formButton = card.querySelector("[data-neural-training-form] button");
        if (formButton) {
            formButton.disabled = status === "queued" || status === "running";
        }
    }

    function scheduleNeuralPoll(card, statusUrl, delayMs) {
        clearNeuralPollTimer();
        neuralPollTimer = window.setTimeout(function () {
            fetchStatus(statusUrl)
                .then(function (payload) {
                    renderNeuralJob(card, payload);
                    if (payload.status === "succeeded") {
                        setText(card.querySelector("[data-neural-training-message]"), "Готово. Обновляю статус модели.");
                        clearNeuralPollTimer();
                        reloadPlanningPage();
                        return;
                    }
                    if (payload.status === "failed") {
                        clearNeuralPollTimer();
                        return;
                    }
                    scheduleNeuralPoll(card, statusUrl, POLL_INTERVAL_MS);
                })
                .catch(function (error) {
                    renderNeuralJob(card, {
                        status: "failed",
                        progress_percent: 0,
                        stage_label: "Ошибка статуса",
                        error_message: error.message || "Не удалось получить статус обучения.",
                    });
                    clearNeuralPollTimer();
                });
        }, delayMs);
    }

    function startNeuralTraining(card, form) {
        const button = form.querySelector("button");
        if (button) {
            button.disabled = true;
        }
        renderNeuralJob(card, {
            status: "queued",
            progress_percent: 0,
            stage_label: "Запускаю обучение",
            message: "Создаю фоновую задачу переобучения.",
        });
        fetch(form.action, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": csrfTokenFromForm(form),
            },
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || payload.ok === false) {
                        throw new Error(payload.message || payload.error_message || "Не удалось запустить обучение.");
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                renderNeuralJob(card, payload);
                if (payload.status_url) {
                    card.dataset.statusUrl = payload.status_url;
                    scheduleNeuralPoll(card, payload.status_url, 500);
                }
            })
            .catch(function (error) {
                renderNeuralJob(card, {
                    status: "failed",
                    progress_percent: 0,
                    stage_label: "Ошибка запуска",
                    error_message: error.message || "Не удалось запустить обучение.",
                });
                if (button) {
                    button.disabled = false;
                }
            });
    }

    function initNeuralTraining() {
        const previousController = window.__schedulePlanningNeuralTrainingController;
        if (previousController) {
            previousController.abort();
            window.__schedulePlanningNeuralTrainingController = null;
        }
        clearNeuralPollTimer();

        const root = document.querySelector("[data-page='schedule-planning']");
        const card = root ? root.querySelector("[data-neural-training]") : null;
        if (!card) {
            return;
        }

        const controller = new AbortController();
        window.__schedulePlanningNeuralTrainingController = controller;
        controller.signal.addEventListener("abort", clearNeuralPollTimer, { once: true });

        const form = card.querySelector("[data-neural-training-form]");
        if (form) {
            form.addEventListener("submit", function (event) {
                event.preventDefault();
                if (form.querySelector("button:disabled")) {
                    return;
                }
                startNeuralTraining(card, form);
            });
        }

        const statusUrl = card.dataset.statusUrl || "";
        const status = card.dataset.status || "";
        if (statusUrl && (status === "queued" || status === "running")) {
            scheduleNeuralPoll(card, statusUrl, 250);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
            initPlanningAutoJob();
            initNeuralTraining();
        }, { once: true });
    } else {
        initPlanningAutoJob();
        initNeuralTraining();
    }

    document.addEventListener("app:navigation", function () {
        initPlanningAutoJob();
        initNeuralTraining();
    });
})();
