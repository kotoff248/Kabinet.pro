(function () {
    const STORAGE_KEY = "kabinet:demo-reset-job";
    const POLL_INTERVAL_MS = 1500;
    const DONE_STATUSES = ["succeeded", "failed"];

    let pollTimer = 0;
    let banner = null;

    function readStoredJob() {
        try {
            const raw = window.localStorage.getItem(STORAGE_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (error) {
            return null;
        }
    }

    function storeJob(payload) {
        if (!payload || !payload.status_url) {
            return;
        }
        try {
            window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
                job_id: payload.job_id,
                token: payload.token,
                status_url: payload.status_url,
            }));
        } catch (error) {
        }
    }

    function clearStoredJob() {
        try {
            window.localStorage.removeItem(STORAGE_KEY);
        } catch (error) {
        }
    }

    function formatDuration(seconds) {
        const value = Number(seconds);
        if (!Number.isFinite(value) || value <= 0) {
            return "";
        }
        const rounded = Math.max(1, Math.round(value));
        if (rounded < 60) {
            return `${rounded} сек.`;
        }
        const minutes = Math.round(rounded / 60);
        if (minutes < 60) {
            return `${minutes} мин.`;
        }
        const hours = Math.floor(minutes / 60);
        const tailMinutes = minutes % 60;
        return tailMinutes ? `${hours} ч ${tailMinutes} мин.` : `${hours} ч`;
    }

    function setHidden(element, hidden) {
        if (element) {
            element.hidden = Boolean(hidden);
        }
    }

    function titleForPayload(payload) {
        const label = payload.preset_label || "Демо-база";
        const percent = Math.max(0, Math.min(100, Number(payload.progress_percent || 0)));
        if (payload.status === "succeeded") {
            return `${label} пересоздана`;
        }
        if (payload.status === "failed") {
            return `${label}: ошибка пересоздания`;
        }
        return `${label}: пересоздание ${Math.round(percent)}%`;
    }

    function messageForPayload(payload) {
        if (payload.status === "succeeded") {
            const elapsed = formatDuration(payload.elapsed_seconds);
            return elapsed ? `Готово за ${elapsed}. Можно войти заново с паролем 1234.` : "Готово. Можно войти заново с паролем 1234.";
        }
        if (payload.status === "failed") {
            return payload.error_message || "Пересоздание завершилось с ошибкой.";
        }

        const pieces = [];
        if (payload.stage_label) {
            pieces.push(payload.stage_label);
        }
        if (payload.estimated_remaining_seconds !== null && payload.estimated_remaining_seconds !== undefined) {
            const remaining = formatDuration(payload.estimated_remaining_seconds);
            if (remaining) {
                pieces.push(`осталось примерно ${remaining}`);
            }
        } else {
            pieces.push("примерное время появится после первых этапов");
        }
        return pieces.join(" · ");
    }

    function render(payload) {
        if (!banner || !payload) {
            return;
        }

        const percent = Math.max(0, Math.min(100, Number(payload.progress_percent || 0)));
        const title = banner.querySelector("[data-demo-reset-banner-title]");
        const message = banner.querySelector("[data-demo-reset-banner-message]");
        const percentElement = banner.querySelector("[data-demo-reset-banner-percent]");
        const bar = banner.querySelector("[data-demo-reset-banner-bar]");
        const login = banner.querySelector("[data-demo-reset-banner-login]");
        const close = banner.querySelector("[data-demo-reset-banner-close]");

        banner.classList.toggle("is-error", payload.status === "failed");
        banner.classList.toggle("is-done", payload.status === "succeeded");
        setHidden(banner, false);

        if (title) {
            title.textContent = titleForPayload(payload);
        }
        if (message) {
            message.textContent = messageForPayload(payload);
        }
        if (percentElement) {
            percentElement.textContent = `${Math.round(percent)}%`;
        }
        if (bar) {
            bar.style.width = `${percent}%`;
        }
        if (login) {
            login.href = payload.login_url || login.href;
            setHidden(login, payload.status !== "succeeded");
        }
        setHidden(close, DONE_STATUSES.indexOf(payload.status) === -1);
    }

    function stopPolling() {
        if (pollTimer) {
            window.clearTimeout(pollTimer);
            pollTimer = 0;
        }
    }

    function schedulePoll(statusUrl) {
        stopPolling();
        pollTimer = window.setTimeout(function () {
            poll(statusUrl);
        }, POLL_INTERVAL_MS);
    }

    function poll(statusUrl) {
        if (!statusUrl) {
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
                        throw new Error(payload.message || payload.error_message || "Не удалось получить статус пересоздания.");
                    }
                    return payload;
                });
            })
            .then(function (payload) {
                render(payload);
                if (DONE_STATUSES.indexOf(payload.status) !== -1) {
                    stopPolling();
                    return;
                }
                schedulePoll(statusUrl);
            })
            .catch(function () {
                clearStoredJob();
                stopPolling();
                setHidden(banner, true);
            });
    }

    function track(payload) {
        if (!payload || !payload.status_url) {
            return;
        }
        storeJob(payload);
        render(payload);
        if (DONE_STATUSES.indexOf(payload.status) === -1) {
            schedulePoll(payload.status_url);
        }
    }

    document.addEventListener("DOMContentLoaded", function () {
        banner = document.querySelector("[data-demo-reset-banner]");
        if (!banner) {
            return;
        }

        const close = banner.querySelector("[data-demo-reset-banner-close]");
        if (close) {
            close.addEventListener("click", function () {
                clearStoredJob();
                stopPolling();
                setHidden(banner, true);
            });
        }

        const stored = readStoredJob();
        if (stored && stored.status_url) {
            poll(stored.status_url);
        }
    });

    window.KabinetDemoResetStatus = {
        track: track,
        clear: clearStoredJob,
    };
}());
