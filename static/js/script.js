const container = document.getElementById("container");
const registerBtn = document.getElementById("register");
const loginBtn = document.getElementById("login");

function clearKabinetSessionMemoryAfterLogout() {
    const params = new URLSearchParams(window.location.search);
    if (params.get("signed_out") !== "1") {
        return;
    }

    const exactKeys = [
        "applications:last-detail-href",
        "applications:last-list-href",
        "applications:list-scroll-state",
        "employees:last-detail-href",
        "employees:last-list-href",
        "employees:list-scroll-state",
        "notifications:last-list-href",
        "calendar:path",
        "calendar:last-url",
        "calendar:board-scroll-state",
    ];
    const prefixes = ["profile-sections:", "profile-schedule-filters:"];

    try {
        exactKeys.forEach(function (key) {
            sessionStorage.removeItem(key);
        });

        for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
            const key = sessionStorage.key(index);
            if (!key) {
                continue;
            }

            if (prefixes.some(function (prefix) {
                return key.indexOf(prefix) === 0;
            })) {
                sessionStorage.removeItem(key);
            }
        }
    } catch (error) {
    }

    params.delete("signed_out");
    const cleanQuery = params.toString();
    const cleanUrl = window.location.pathname + (cleanQuery ? "?" + cleanQuery : "") + window.location.hash;
    window.history.replaceState({}, document.title, cleanUrl);
}

clearKabinetSessionMemoryAfterLogout();

if (container && registerBtn && loginBtn) {
    registerBtn.addEventListener("click", function () {
        container.classList.add("active");
    });

    loginBtn.addEventListener("click", function () {
        container.classList.remove("active");
    });
}
