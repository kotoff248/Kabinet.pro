function initStaffingPage() {
    const previousController = window.__staffingPageController;
    if (previousController) {
        previousController.abort();
    }

    const root = document.querySelector(".staffing-page");
    if (!root) {
        return;
    }

    const controller = new AbortController();
    const signal = controller.signal;
    window.__staffingPageController = controller;

    root.querySelectorAll("[data-staffing-workload-year-select]").forEach(function (select) {
        const form = select.closest("[data-staffing-workload-year-form]");
        if (!form) {
            return;
        }

        select.addEventListener("change", function () {
            if (typeof form.requestSubmit === "function") {
                form.requestSubmit();
                return;
            }
            form.submit();
        }, { signal: signal });
    });

    initDemoDataReset(signal);
    initDemoInitialRestore(signal);
}

function initDemoDataReset(signal) {
    const form = document.querySelector("[data-demo-reset-form]");
    if (!form) {
        return;
    }

    const modal = form.closest(".app-modal");
    const submitButton = form.querySelector("[data-demo-reset-submit]");
    const cancelButton = form.querySelector("[data-demo-reset-cancel]");
    const progress = document.querySelector("[data-demo-reset-progress]");
    const progressStage = document.querySelector("[data-demo-reset-stage]");
    const progressPercent = document.querySelector("[data-demo-reset-percent]");
    const progressBar = document.querySelector("[data-demo-reset-bar]");
    const progressMessage = document.querySelector("[data-demo-reset-message]");
    const result = document.querySelector("[data-demo-reset-result]");
    const resultMessage = document.querySelector("[data-demo-reset-result-message]");
    const loginLink = document.querySelector("[data-demo-reset-login]");
    let pollTimer = null;
    let running = false;

    function setHidden(element, hidden) {
        if (!element) {
            return;
        }
        element.hidden = Boolean(hidden);
    }

    function setDisabled(disabled) {
        [submitButton, cancelButton].forEach(function (button) {
            if (button) {
                button.disabled = Boolean(disabled);
            }
        });
        if (modal) {
            modal.classList.toggle("is-demo-reset-running", Boolean(disabled));
        }
    }

    function updateProgress(payload) {
        const percent = Math.max(0, Math.min(100, Number(payload.progress_percent || 0)));
        setHidden(progress, false);
        setHidden(result, true);
        if (progressStage) {
            progressStage.textContent = payload.stage_label || "Пересоздание демо-данных";
        }
        if (progressPercent) {
            progressPercent.textContent = Math.round(percent) + "%";
        }
        if (progressBar) {
            progressBar.style.width = percent + "%";
        }
        if (progressMessage) {
            progressMessage.textContent = payload.message || "Пересоздание выполняется в фоне.";
        }
    }

    function showResult(message, options) {
        const settings = options || {};
        setHidden(progress, true);
        setHidden(result, false);
        if (result) {
            result.classList.toggle("is-error", Boolean(settings.isError));
        }
        if (resultMessage) {
            resultMessage.textContent = message;
        }
        if (loginLink) {
            if (settings.loginUrl) {
                loginLink.href = settings.loginUrl;
                setHidden(loginLink, false);
            } else {
                setHidden(loginLink, true);
            }
        }
    }

    function stopPolling() {
        if (pollTimer) {
            window.clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    function schedulePoll(statusUrl) {
        stopPolling();
        pollTimer = window.setTimeout(function () {
            pollStatus(statusUrl);
        }, 1500);
    }

    function pollStatus(statusUrl) {
        if (!statusUrl || signal.aborted) {
            return;
        }

        fetch(statusUrl, {
            method: "GET",
            credentials: "same-origin",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || payload.ok === false) {
                        throw new Error(payload.message || payload.error_message || "Не удалось получить статус.");
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                updateProgress(payload);
                if (payload.status === "succeeded") {
                    running = false;
                    stopPolling();
                    showResult(
                        "Демо-данные пересозданы. Войдите заново с паролем 1234.",
                        { loginUrl: payload.login_url }
                    );
                    return;
                }
                if (payload.status === "failed") {
                    running = false;
                    stopPolling();
                    setDisabled(false);
                    showResult(
                        payload.error_message || "Пересоздание завершилось с ошибкой.",
                        { isError: true, loginUrl: payload.login_url }
                    );
                    return;
                }
                schedulePoll(statusUrl);
            })
            .catch(function (error) {
                running = false;
                stopPolling();
                setDisabled(false);
                showResult(error.message || "Не удалось получить статус пересоздания.", { isError: true });
            });
    }

    signal.addEventListener("abort", stopPolling, { once: true });

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (running) {
            return;
        }

        running = true;
        setDisabled(true);
        updateProgress({
            progress_percent: 0,
            stage_label: "Запуск",
            message: "Запускаем фоновое пересоздание демо-данных.",
        });

        fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            credentials: "same-origin",
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
        })
            .then(function (response) {
                return response.json().then(function (payload) {
                    if (!response.ok || payload.ok === false) {
                        throw new Error(payload.message || "Не удалось запустить пересоздание демо-данных.");
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                updateProgress(payload);
                schedulePoll(payload.status_url);
            })
            .catch(function (error) {
                running = false;
                stopPolling();
                setDisabled(false);
                showResult(error.message || "Не удалось запустить пересоздание демо-данных.", { isError: true });
            });
    }, { signal: signal });
}

function initDemoInitialRestore(signal) {
    const form = document.querySelector("[data-demo-restore-form]");
    if (!form) {
        return;
    }

    const submitButton = form.querySelector("[data-demo-restore-submit]");
    const cancelButton = form.querySelector("[data-demo-restore-cancel]");
    let running = false;

    form.addEventListener("submit", function (event) {
        if (running) {
            event.preventDefault();
            return;
        }
        running = true;
        [submitButton, cancelButton].forEach(function (button) {
            if (button) {
                button.disabled = true;
            }
        });
        if (submitButton) {
            submitButton.textContent = "Сбрасываю...";
        }
    }, { signal: signal });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initStaffingPage, { once: true });
} else {
    initStaffingPage();
}

document.addEventListener("app:navigation", initStaffingPage);
