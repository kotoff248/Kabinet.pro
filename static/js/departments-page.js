function initDepartmentsPage() {
    const existingController = window.__departmentsPageController;
    if (existingController) {
        existingController.abort();
    }

    const controller = new AbortController();
    const signal = controller.signal;
    window.__departmentsPageController = controller;

    const listShell = document.querySelector(".department-cards-shell");
    const listRoot = document.querySelector(".department-cards-list");
    const detailShell = document.querySelector(".department-detail-shell");
    const detailRoot = document.querySelector(".profile-page[data-page='department-detail']");
    const departmentSwitch = document.querySelector("[data-department-detail-switch]");
    const listScrollStorageKey = "departments:list-scroll-state";
    const detailScrollStorageKey = "departments:detail-scroll-state";
    let groupFilterRequest = null;
    let listScrollStateTimer = 0;
    let detailScrollStateTimer = 0;

    function isDepartmentsListPage() {
        return Boolean(listShell && listRoot);
    }

    function isDepartmentDetailPage() {
        return Boolean(detailShell && detailRoot);
    }

    function getCurrentPath() {
        return window.location.pathname + window.location.search;
    }

    function rememberListHref() {
        if (
            window.KabinetNavigation
            && typeof window.KabinetNavigation.rememberSectionListHref === "function"
        ) {
            window.KabinetNavigation.rememberSectionListHref("departments", window.location.href);
        }
    }

    function rememberDetailHref() {
        if (
            window.KabinetNavigation
            && typeof window.KabinetNavigation.rememberSectionDetailHref === "function"
        ) {
            window.KabinetNavigation.rememberSectionDetailHref(window.location.href);
        }
    }

    function readState(storageKey) {
        try {
            return JSON.parse(sessionStorage.getItem(storageKey) || "null");
        } catch (error) {
            return null;
        }
    }

    function writeState(storageKey, state) {
        try {
            sessionStorage.setItem(storageKey, JSON.stringify(state));
        } catch (error) {
        }
    }

    function clearState(storageKey) {
        try {
            sessionStorage.removeItem(storageKey);
        } catch (error) {
        }
    }

    function writeListScrollState(selectedDepartmentId) {
        if (!isDepartmentsListPage()) {
            return;
        }

        const state = {
            path: window.location.pathname,
            scrollTop: listShell.scrollTop,
        };
        if (selectedDepartmentId) {
            state.selectedDepartmentId = selectedDepartmentId;
        }
        writeState(listScrollStorageKey, state);
    }

    function restoreListScrollState() {
        if (!isDepartmentsListPage()) {
            return;
        }

        const savedState = readState(listScrollStorageKey);
        if (!savedState || savedState.path !== window.location.pathname) {
            return;
        }

        requestAnimationFrame(function () {
            listShell.scrollTop = Number(savedState.scrollTop) || 0;

            if (!savedState.selectedDepartmentId) {
                return;
            }

            const selectedCard = listRoot.querySelector('[data-department-id="' + savedState.selectedDepartmentId + '"]');
            if (!selectedCard) {
                return;
            }

            const shellBounds = listShell.getBoundingClientRect();
            const cardBounds = selectedCard.getBoundingClientRect();
            if (cardBounds.top < shellBounds.top || cardBounds.bottom > shellBounds.bottom) {
                selectedCard.scrollIntoView({ block: "center", behavior: "auto" });
            }
        });
    }

    function writeDetailScrollState() {
        if (!isDepartmentDetailPage()) {
            return;
        }

        writeState(detailScrollStorageKey, {
            path: getCurrentPath(),
            scrollTop: detailShell.scrollTop,
        });
    }

    function flushListScrollState(selectedDepartmentId) {
        if (listScrollStateTimer) {
            window.clearTimeout(listScrollStateTimer);
            listScrollStateTimer = 0;
        }
        writeListScrollState(selectedDepartmentId);
    }

    function scheduleListScrollStateWrite() {
        if (listScrollStateTimer) {
            window.clearTimeout(listScrollStateTimer);
        }
        listScrollStateTimer = window.setTimeout(function () {
            listScrollStateTimer = 0;
            writeListScrollState();
        }, 140);
    }

    function flushDetailScrollState() {
        if (detailScrollStateTimer) {
            window.clearTimeout(detailScrollStateTimer);
            detailScrollStateTimer = 0;
        }
        writeDetailScrollState();
    }

    function scheduleDetailScrollStateWrite() {
        if (detailScrollStateTimer) {
            window.clearTimeout(detailScrollStateTimer);
        }
        detailScrollStateTimer = window.setTimeout(function () {
            detailScrollStateTimer = 0;
            writeDetailScrollState();
        }, 140);
    }

    function restoreDetailScrollState() {
        if (!isDepartmentDetailPage()) {
            return;
        }

        const savedState = readState(detailScrollStorageKey);
        if (!savedState || savedState.path !== getCurrentPath()) {
            return;
        }

        requestAnimationFrame(function () {
            detailShell.scrollTop = Number(savedState.scrollTop) || 0;
        });
    }

    function clearDepartmentScrollMemory() {
        if (listScrollStateTimer) {
            window.clearTimeout(listScrollStateTimer);
            listScrollStateTimer = 0;
        }
        if (detailScrollStateTimer) {
            window.clearTimeout(detailScrollStateTimer);
            detailScrollStateTimer = 0;
        }
        clearState(listScrollStorageKey);
        clearState(detailScrollStorageKey);
    }

    function resolveGroupFilterTarget(target) {
        const element = target instanceof Element ? target.closest("[data-department-group-filter]") : null;
        if (!element || !isDepartmentDetailPage() || !detailShell.contains(element)) {
            return null;
        }

        const href = element.getAttribute("href") || element.dataset.href || "";
        if (!href) {
            return null;
        }

        try {
            const url = new URL(href, window.location.href);
            return url.origin === window.location.origin ? url : null;
        } catch (error) {
            return null;
        }
    }

    function replaceElementFromDocument(currentSelector, nextDocument) {
        const currentNode = document.querySelector(currentSelector);
        const nextNode = nextDocument.querySelector(currentSelector);
        if (!currentNode || !nextNode) {
            return false;
        }

        currentNode.replaceWith(nextNode);
        return true;
    }

    function syncGroupFilterFromDocument(nextDocument) {
        const replacedGroups = replaceElementFromDocument(".department-detail-section:not(.department-detail-section--employees)", nextDocument);
        const replacedEmployees = replaceElementFromDocument(".department-detail-section--employees", nextDocument);
        return replacedGroups && replacedEmployees;
    }

    function updateGroupFilterUrl(url) {
        window.history.replaceState({}, "", url.pathname + url.search + url.hash);
        rememberDetailHref();
        writeDetailScrollState();
    }

    function applyGroupFilter(targetUrl) {
        const preservedScrollTop = detailShell.scrollTop;
        if (groupFilterRequest) {
            groupFilterRequest.abort();
        }

        const requestController = new AbortController();
        groupFilterRequest = requestController;
        fetch(targetUrl.href, {
            headers: {
                "X-Requested-With": "XMLHttpRequest",
            },
            signal: requestController.signal,
        })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Не удалось обновить группу отдела.");
                }
                return response.text();
            })
            .then(function (html) {
                const nextDocument = new DOMParser().parseFromString(html, "text/html");
                if (!syncGroupFilterFromDocument(nextDocument)) {
                    throw new Error("Не удалось найти блок группы в ответе.");
                }

                updateGroupFilterUrl(targetUrl);
                requestAnimationFrame(function () {
                    detailShell.scrollTop = preservedScrollTop;
                    writeDetailScrollState();
                });
            })
            .catch(function (error) {
                if (error.name === "AbortError") {
                    return;
                }
                console.error("Error filtering department group:", error);
            })
            .finally(function () {
                if (groupFilterRequest === requestController) {
                    groupFilterRequest = null;
                }
            });
    }

    function openDepartmentFromSwitch() {
        if (!departmentSwitch || !departmentSwitch.value) {
            return;
        }

        const targetUrl = new URL(departmentSwitch.value, window.location.href);
        if (targetUrl.pathname === window.location.pathname && !targetUrl.search) {
            return;
        }

        flushDetailScrollState();
        if (
            window.KabinetNavigation
            && typeof window.KabinetNavigation.navigate === "function"
            && window.KabinetNavigation.navigate(targetUrl.href, true)
        ) {
            return;
        }

        window.location.href = targetUrl.href;
    }

    if (isDepartmentsListPage()) {
        rememberListHref();
        restoreListScrollState();

        listShell.addEventListener("scroll", scheduleListScrollStateWrite, { passive: true, signal: signal });

        listRoot.addEventListener("click", function (event) {
            const card = event.target.closest("[data-department-id]");
            if (card && listRoot.contains(card)) {
                flushListScrollState(card.dataset.departmentId);
            }
        }, { capture: true, signal: signal });

        listRoot.addEventListener("keydown", function (event) {
            if (event.key !== "Enter" && event.key !== " ") {
                return;
            }

            const card = event.target.closest("[data-department-id]");
            if (card && listRoot.contains(card)) {
                flushListScrollState(card.dataset.departmentId);
            }
        }, { capture: true, signal: signal });
    }

    if (isDepartmentDetailPage()) {
        rememberDetailHref();
        restoreDetailScrollState();

        if (departmentSwitch) {
            departmentSwitch.addEventListener("change", openDepartmentFromSwitch, { signal: signal });
        }

        detailShell.addEventListener("scroll", scheduleDetailScrollStateWrite, { passive: true, signal: signal });

        detailShell.addEventListener("click", function (event) {
            const groupFilterUrl = resolveGroupFilterTarget(event.target);
            if (groupFilterUrl) {
                event.preventDefault();
                event.stopPropagation();
                flushDetailScrollState();
                applyGroupFilter(groupFilterUrl);
                return;
            }

            if (event.target.closest("[data-href]")) {
                flushDetailScrollState();
            }
        }, { capture: true, signal: signal });

        detailShell.addEventListener("keydown", function (event) {
            if (event.key !== "Enter" && event.key !== " ") {
                return;
            }

            const groupFilterUrl = resolveGroupFilterTarget(event.target);
            if (groupFilterUrl) {
                event.preventDefault();
                event.stopPropagation();
                flushDetailScrollState();
                applyGroupFilter(groupFilterUrl);
                return;
            }

            if (event.target.closest("[data-href]")) {
                flushDetailScrollState();
            }
        }, { capture: true, signal: signal });
    }

    document.addEventListener("app:section-sidebar-repeat", function (event) {
        if (!event.detail || event.detail.sectionKey !== "departments") {
            return;
        }

        event.preventDefault();
        clearDepartmentScrollMemory();
        if (isDepartmentsListPage()) {
            listShell.scrollTop = 0;
        }
    }, { signal: signal });

    document.addEventListener("app:before-navigation", function () {
        flushListScrollState();
        flushDetailScrollState();
    }, { signal: signal });

    signal.addEventListener("abort", function () {
        if (listScrollStateTimer) {
            window.clearTimeout(listScrollStateTimer);
        }
        if (detailScrollStateTimer) {
            window.clearTimeout(detailScrollStateTimer);
        }
    }, { once: true });
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDepartmentsPage, { once: true });
} else {
    initDepartmentsPage();
}

document.addEventListener("app:navigation", initDepartmentsPage);
