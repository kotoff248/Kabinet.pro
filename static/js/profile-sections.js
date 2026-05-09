function initProfileSectionsPage() {
    const previousController = window.__profileSectionsPageController;
    if (previousController) {
        previousController.abort();
    }

    const pageMain = document.querySelector(".page-main");
    const root = document.querySelector("[data-profile-sections]");
    if (!root) {
        if (pageMain) {
            pageMain.classList.remove("is-profile-sections");
        }
        return;
    }

    const controller = new AbortController();
    const signal = controller.signal;
    window.__profileSectionsPageController = controller;

    const viewport = root.querySelector("[data-profile-viewport]");
    const sections = Array.from(root.querySelectorAll("[data-profile-section]"));
    const sectionNames = sections
        .map(function (section) {
            return section.dataset.profileSection;
        })
        .filter(Boolean);
    const sectionScrolls = buildSectionScrollMap();
    const requestsScroll = getSectionScrolls("requests")[0] || null;
    const mobileSectionsMedia = window.matchMedia("(max-width: 992px)");
    const preserveRequestsScrollOnSectionSwitch = root.hasAttribute("data-applications-page");
    const shouldLockSectionScrollRoots = root.hasAttribute("data-profile-lock-scroll-roots");
    const shouldUseGenericScrollMemory = !root.hasAttribute("data-applications-page");
    const sectionStorageKey = "profile-sections:" + window.location.pathname;
    const storedState = readSectionState();
    let activeSection = getInitialActiveSection();
    let hasRestoredStoredScroll = false;
    const wheelSwitchGestureIdleMs = 140;
    const wheelSwitchGestureMaxMs = 420;
    const wheelSwitchGestureInterruptMs = 90;
    const wheelSwitchGestureIntentDelta = 18;
    let wheelSwitchGestureLocked = false;
    let wheelSwitchGestureTimer = 0;
    let wheelSwitchGestureMaxTimer = 0;
    let wheelSwitchGestureDirection = 0;
    let wheelSwitchGestureStartedAt = 0;
    let sectionStateTimer = 0;

    bindModeChange();
    signal.addEventListener("abort", clearWheelSwitchGestureTimers, { once: true });
    signal.addEventListener("abort", function () {
        if (sectionStateTimer) {
            window.clearTimeout(sectionStateTimer);
        }
    }, { once: true });

    if (mobileSectionsMedia.matches) {
        setupMobileSections();
        return;
    }

    if (pageMain) {
        pageMain.classList.add("is-profile-sections");
    }
    scrollDocumentToTop({ behavior: "auto" });

    if (viewport && activeSection !== sectionNames[0]) {
        viewport.style.transition = "none";
    }

    root.classList.add("is-enhanced");

    function bindModeChange() {
        const handleModeChange = function () {
            initProfileSectionsPage();
        };

        if (typeof mobileSectionsMedia.addEventListener === "function") {
            mobileSectionsMedia.addEventListener("change", handleModeChange, { signal: signal });
            return;
        }

        if (typeof mobileSectionsMedia.addListener === "function") {
            mobileSectionsMedia.addListener(handleModeChange);
            signal.addEventListener("abort", function () {
                mobileSectionsMedia.removeListener(handleModeChange);
            }, { once: true });
        }
    }

    function buildSectionScrollMap() {
        return sectionNames.reduce(function (scrolls, sectionName) {
            const selector = sectionName === "overview"
                ? "[data-profile-overview-scroll], [data-entitlement-scroll]"
                : "[data-profile-" + sectionName + "-scroll]";
            scrolls[sectionName] = Array.from(root.querySelectorAll(selector));
            return scrolls;
        }, {});
    }

    function setupMobileSections() {
        if (pageMain) {
            pageMain.classList.remove("is-profile-sections");
        }

        root.classList.remove("is-enhanced");

        if (viewport) {
            viewport.style.transition = "";
        }

        sections.forEach(function (section) {
            section.classList.add("is-active");
            section.setAttribute("aria-hidden", "false");
        });

        document.addEventListener("app:section-sidebar-repeat", function (event) {
            if (!shouldHandleSidebarRepeat(event)) {
                return;
            }

            event.preventDefault();
            if (requestFilterReset(event.detail.sectionKey)) {
                return;
            }
            scrollDocumentToTop();
            scrollAllSectionRootsToTop();
        }, { signal: signal });
    }

    function readSectionState() {
        try {
            return JSON.parse(sessionStorage.getItem(sectionStorageKey) || "null");
        } catch (error) {
            return null;
        }
    }

    function writeSectionState() {
        const state = {
            activeSection: activeSection,
        };

        if (shouldUseGenericScrollMemory) {
            if (!hasRestoredStoredScroll && storedState) {
                if (storedState.scrollTops) {
                    state.scrollTops = storedState.scrollTops;
                }
                if (storedState.requestsTop !== undefined) {
                    state.requestsTop = Number(storedState.requestsTop) || 0;
                }
            } else {
                state.scrollTops = {};
                Object.keys(sectionScrolls).forEach(function (sectionName) {
                    state.scrollTops[sectionName] = getSectionScrolls(sectionName).map(function (scrollRoot) {
                        return scrollRoot ? scrollRoot.scrollTop : 0;
                    });
                });
                if (requestsScroll) {
                    state.requestsTop = requestsScroll.scrollTop;
                }
            }
        }

        try {
            sessionStorage.setItem(sectionStorageKey, JSON.stringify(state));
        } catch (error) {
        }
    }

    function flushSectionState() {
        if (sectionStateTimer) {
            window.clearTimeout(sectionStateTimer);
            sectionStateTimer = 0;
        }
        writeSectionState();
    }

    function scheduleSectionStateWrite() {
        if (sectionStateTimer) {
            window.clearTimeout(sectionStateTimer);
        }
        sectionStateTimer = window.setTimeout(function () {
            sectionStateTimer = 0;
            writeSectionState();
        }, 140);
    }

    function getInitialActiveSection() {
        if (sectionNames.indexOf(root.dataset.activeSection) !== -1) {
            return root.dataset.activeSection;
        }

        if (storedState && sectionNames.indexOf(storedState.activeSection) !== -1) {
            return storedState.activeSection;
        }

        return sectionNames[0] || "overview";
    }

    function restoreStoredScroll() {
        if (!shouldUseGenericScrollMemory || !storedState) {
            return;
        }

        requestAnimationFrame(function () {
            Object.keys(sectionScrolls).forEach(function (sectionName) {
                getSectionScrolls(sectionName).forEach(function (scrollRoot, index) {
                    if (!scrollRoot) {
                        return;
                    }

                    const sectionTops = storedState.scrollTops && storedState.scrollTops[sectionName];
                    let savedTop = Array.isArray(sectionTops) ? sectionTops[index] : undefined;
                    if (savedTop === undefined && sectionName === "requests" && index === 0) {
                        savedTop = storedState.requestsTop;
                    }

                    if (savedTop !== undefined) {
                        scrollRoot.scrollTop = Number(savedTop) || 0;
                    }
                });
            });
            hasRestoredStoredScroll = true;
            flushSectionState();
        });
    }

    function syncSectionState() {
        root.dataset.activeSection = activeSection;
        root.style.setProperty("--profile-active-index", String(getActiveSectionIndex()));
        sections.forEach(function (section) {
            const isActive = section.dataset.profileSection === activeSection;
            section.classList.toggle("is-active", isActive);
            section.setAttribute("aria-hidden", isActive ? "false" : "true");
        });
        flushSectionState();
    }

    function activateSection(sectionName, options) {
        const nextOptions = options || {};
        if (sectionNames.indexOf(sectionName) === -1) {
            return;
        }

        if (sectionName === activeSection && !nextOptions.force) {
            return;
        }

        activeSection = sectionName;
        if (sectionName === "requests" && requestsScroll && nextOptions.resetScroll) {
            requestsScroll.scrollTop = 0;
        }

        syncSectionState();
    }

    function getSectionScrolls(sectionName) {
        return sectionScrolls[sectionName] || [];
    }

    function getActiveSectionIndex() {
        return Math.max(0, sectionNames.indexOf(activeSection));
    }

    function getAdjacentSection(direction) {
        const currentIndex = getActiveSectionIndex();
        const nextIndex = currentIndex + direction;
        if (nextIndex < 0 || nextIndex >= sectionNames.length) {
            return "";
        }
        return sectionNames[nextIndex];
    }

    function scrollRootsToTop(scrollRoots) {
        scrollRoots.forEach(function (scrollRoot) {
            if (!scrollRoot) {
                return;
            }

            if (typeof scrollRoot.scrollTo === "function") {
                scrollRoot.scrollTo({ top: 0, left: 0, behavior: "smooth" });
                return;
            }

            scrollRoot.scrollTop = 0;
        });
    }

    function scrollAllSectionRootsToTop() {
        Object.keys(sectionScrolls).forEach(function (sectionName) {
            scrollRootsToTop(sectionScrolls[sectionName]);
        });
    }

    function scrollDocumentToTop(options) {
        const behavior = options && options.behavior ? options.behavior : "smooth";
        const scrollingElement = document.scrollingElement || document.documentElement;
        if (scrollingElement && typeof scrollingElement.scrollTo === "function") {
            scrollingElement.scrollTo({ top: 0, left: 0, behavior: behavior });
            return;
        }

        window.scrollTo({ top: 0, left: 0, behavior: behavior });
    }

    function requestFilterReset(sectionKey) {
        const resetFiltersEvent = new CustomEvent("app:section-filters-reset", {
            cancelable: true,
            detail: {
                sectionKey: sectionKey,
            },
        });
        return !document.dispatchEvent(resetFiltersEvent);
    }

    function shouldHandleSidebarRepeat(event) {
        if (!event.detail) {
            return false;
        }

        return (
            (
                event.detail.sectionKey === "applications"
                && root.hasAttribute("data-applications-page")
            )
            || (
                event.detail.sectionKey === "profile"
                && window.location.pathname === "/main/"
                && !root.hasAttribute("data-applications-page")
            )
        );
    }

    function getEventElement(event) {
        return event.target instanceof Element ? event.target : null;
    }

    function isVerticalWheel(event) {
        return Math.abs(event.deltaY) > Math.abs(event.deltaX || 0);
    }

    function getNow() {
        if (window.performance && typeof window.performance.now === "function") {
            return window.performance.now();
        }

        return Date.now();
    }

    function clearWheelSwitchGestureTimers() {
        if (wheelSwitchGestureTimer) {
            window.clearTimeout(wheelSwitchGestureTimer);
            wheelSwitchGestureTimer = 0;
        }

        if (wheelSwitchGestureMaxTimer) {
            window.clearTimeout(wheelSwitchGestureMaxTimer);
            wheelSwitchGestureMaxTimer = 0;
        }
    }

    function unlockWheelSwitchGesture() {
        wheelSwitchGestureLocked = false;
        wheelSwitchGestureDirection = 0;
        wheelSwitchGestureStartedAt = 0;
        clearWheelSwitchGestureTimers();
    }

    function scheduleWheelSwitchGestureIdleUnlock() {
        if (wheelSwitchGestureTimer) {
            window.clearTimeout(wheelSwitchGestureTimer);
        }

        wheelSwitchGestureTimer = window.setTimeout(unlockWheelSwitchGesture, wheelSwitchGestureIdleMs);
    }

    function lockWheelSwitchGesture(direction) {
        wheelSwitchGestureLocked = true;
        wheelSwitchGestureDirection = direction;
        wheelSwitchGestureStartedAt = getNow();
        scheduleWheelSwitchGestureIdleUnlock();
        if (wheelSwitchGestureMaxTimer) {
            window.clearTimeout(wheelSwitchGestureMaxTimer);
        }
        wheelSwitchGestureMaxTimer = window.setTimeout(unlockWheelSwitchGesture, wheelSwitchGestureMaxMs);
    }

    function consumeWheelDuringSwitchGesture(event) {
        if (!wheelSwitchGestureLocked || !isVerticalWheel(event)) {
            return false;
        }

        const wheelDirection = Math.sign(event.deltaY);
        const elapsedMs = getNow() - wheelSwitchGestureStartedAt;
        const isDeliberateReverseGesture = (
            wheelDirection !== 0
            && wheelDirection !== wheelSwitchGestureDirection
            && Math.abs(event.deltaY) >= wheelSwitchGestureIntentDelta
            && elapsedMs >= wheelSwitchGestureInterruptMs
        );

        if (isDeliberateReverseGesture || elapsedMs >= wheelSwitchGestureMaxMs) {
            unlockWheelSwitchGesture();
            return false;
        }

        event.preventDefault();
        scheduleWheelSwitchGestureIdleUnlock();
        return true;
    }

    function hasVerticalOverflow(element) {
        if (!element) {
            return false;
        }

        return element.scrollHeight - element.clientHeight > 1;
    }

    function canScrollElementVertically(element, deltaY, deltaX) {
        if (!element || Math.abs(deltaY) <= Math.abs(deltaX || 0)) {
            return false;
        }

        const maxScrollTop = element.scrollHeight - element.clientHeight;
        if (maxScrollTop <= 1) {
            return false;
        }

        if (deltaY > 0) {
            return element.scrollTop < maxScrollTop - 1;
        }

        if (deltaY < 0) {
            return element.scrollTop > 1;
        }

        return false;
    }

    function findContainingScrollRoot(event, scrollRoots) {
        const target = getEventElement(event);
        if (!target) {
            return null;
        }

        return scrollRoots.filter(function (candidate) {
            return candidate.contains(target);
        }).reduce(function (closest, candidate) {
            if (!closest || closest.contains(candidate)) {
                return candidate;
            }
            return closest;
        }, null);
    }

    function findScrollableDescendant(event, boundaryElement) {
        const target = getEventElement(event);
        if (!target || !boundaryElement) {
            return null;
        }

        let currentElement = target;
        while (currentElement && currentElement !== boundaryElement) {
            if (canScrollElementVertically(currentElement, event.deltaY, event.deltaX)) {
                return currentElement;
            }
            currentElement = currentElement.parentElement;
        }
        return null;
    }

    function keepWheelInsideElement(element, event, options) {
        const nextOptions = options || {};
        if (!element || !isVerticalWheel(event)) {
            return false;
        }

        if (!hasVerticalOverflow(element)) {
            if (nextOptions.lockWhenPresent) {
                event.preventDefault();
            }
            return Boolean(nextOptions.lockWhenPresent);
        }

        if (!canScrollElementVertically(element, event.deltaY, event.deltaX)) {
            event.preventDefault();
            return Boolean(nextOptions.lockAtBoundary);
        }

        return true;
    }

    function shouldLeaveWheelAlone(event) {
        const target = getEventElement(event);
        if (!target) {
            return false;
        }

        const dropdownMenu = target.closest(
            ".employee-select__menu--floating, [data-employee-select-menu], " +
            "[data-profile-schedule-year-menu], [data-select-menu], .calendar-select__menu"
        );
        return keepWheelInsideElement(dropdownMenu, event, {
            lockAtBoundary: true,
            lockWhenPresent: true,
        });
    }

    document.addEventListener("wheel", function (event) {
        const activeScrolls = getSectionScrolls(activeSection);
        const nextSection = getAdjacentSection(1);
        const previousSection = getAdjacentSection(-1);

        if (shouldLeaveWheelAlone(event)) {
            return;
        }

        if (consumeWheelDuringSwitchGesture(event)) {
            return;
        }

        const activeScrollRoot = findContainingScrollRoot(event, activeScrolls);
        if (activeScrollRoot) {
            if (findScrollableDescendant(event, activeScrollRoot)) {
                return;
            }

            const scrollRootOptions = shouldLockSectionScrollRoots
                ? { lockAtBoundary: true, lockWhenPresent: true }
                : undefined;
            if (keepWheelInsideElement(activeScrollRoot, event, scrollRootOptions)) {
                return;
            }
        }

        if (event.deltaY > 12 && nextSection) {
            event.preventDefault();
            activateSection(nextSection, {
                resetScroll: nextSection === "requests" && !preserveRequestsScrollOnSectionSwitch,
            });
            lockWheelSwitchGesture(1);
            return;
        }

        if (event.deltaY < -12 && previousSection) {
            event.preventDefault();
            activateSection(previousSection);
            lockWheelSwitchGesture(-1);
        }
    }, { passive: false, signal: signal });

    if (shouldUseGenericScrollMemory) {
        Object.keys(sectionScrolls).forEach(function (sectionName) {
            getSectionScrolls(sectionName).forEach(function (scrollRoot) {
                if (!scrollRoot) {
                    return;
                }

                scrollRoot.addEventListener("scroll", function () {
                    scheduleSectionStateWrite();
                }, { passive: true, signal: signal });
            });
        });
    }

    document.addEventListener("app:before-navigation", flushSectionState, { signal: signal });

    document.addEventListener("app:section-sidebar-repeat", function (event) {
        if (!shouldHandleSidebarRepeat(event)) {
            return;
        }

        event.preventDefault();
        if (requestFilterReset(event.detail.sectionKey)) {
            return;
        }
        activateSection(sectionNames[0] || "overview", { force: true });
        scrollDocumentToTop();
        scrollAllSectionRootsToTop();
    }, { signal: signal });

    syncSectionState();
    restoreStoredScroll();

    if (viewport && viewport.style.transition === "none") {
        requestAnimationFrame(function () {
            requestAnimationFrame(function () {
                viewport.style.transition = "";
            });
        });
    }
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initProfileSectionsPage, { once: true });
} else {
    initProfileSectionsPage();
}

document.addEventListener("app:navigation", initProfileSectionsPage);
