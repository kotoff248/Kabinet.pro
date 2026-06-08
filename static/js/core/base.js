(function () {
    window.KabinetSegmented = window.KabinetSegmented || {};
    window.KabinetSegmented.sync = function (control, activeItem) {
        if (!control) {
            return;
        }

        const items = Array.from(control.querySelectorAll(".segmented-control__item"));
        const currentItem = activeItem || items.find(function (item) {
            return item.classList.contains("active") || item.classList.contains("is-active");
        });
        const index = Math.max(0, items.indexOf(currentItem));
        control.dataset.segmentedIndex = String(index);
    };
}());

document.addEventListener("DOMContentLoaded", function () {
    const CORE_STYLE_MATCHERS = [
        "css/base/reset.css",
        "css/base/foundation.css",
        "css/layout/app-shell.css",
        "css/components/page-hero.css",
        "css/components/modals.css",
        "css/components/schedule-transfer-modal.css",
        "css/components/date-picker.css",
        "css/components/panels.css",
        "css/components/messages.css",
        "css/layout/sidebar-shell.css",
        "css/components/segmented-control.css",
        "css/layout/sidebar-nav.css",
        "css/pages/profile.css",
        "css/components/vacation-request-card.css",
        "css/pages/vacation-detail.css",
        "css/components/buttons.css",
        "css/components/radii.css",
        "css/layout/responsive.css",
        "css/pages/profile-sections.css",
        "css/components/reduced-motion.css",
    ];
    const CORE_SCRIPT_MATCHERS = ["js/components/date-picker.js", "js/core/base.js"];
    const PAGE_STATE_CLASSES = ["is-calendar-page"];
    const PAGE_TRANSITION_CLASS = "is-page-transitioning";
    const CALENDAR_ROOT_SELECTOR = "#calendar-filters-form";
    const CALENDAR_ACTIVE_PREFERENCES_URL_KEY = "calendar:active-preferences-url";
    const PLANNING_ACTIVE_URL_KEY = "schedule-planning:last-active-href";
    const NAVIGATION_PREFETCH_TTL_MS = 120000;
    const NAVIGATION_PREFETCH_MAX_ENTRIES = 10;
    const NAVIGATION_IDLE_PREFETCH_DELAY_MS = 700;
    const NAVIGATION_IDLE_PREFETCH_STEP_MS = 900;
    const NAVIGATION_IDLE_PREFETCH_KEYS = [];
    const NAVIGATION_STYLE_LOAD_TIMEOUT_MS = 1800;
    const SCROLL_PERFORMANCE_SELECTOR = [
        ".applications-cards-shell",
        ".employee-cards-shell",
        ".department-cards-shell",
        ".department-detail-shell",
        ".notifications-list",
        ".preference-readiness-panel__scroll",
        ".schedule-planning-panel__scroll",
        ".schedule-draft-panel__scroll",
        ".staffing-board",
        ".calendar-board-scroll",
    ].join(",");
    const PLANNING_SCROLL_MEMORY_CONFIGS = [
        {
            pageSelector: ".preference-readiness-page",
            scrollSelector: ".preference-readiness-panel__scroll",
            storageKey: "planning-scroll:preference-readiness",
        },
        {
            pageSelector: ".schedule-planning-page",
            scrollSelector: ".schedule-planning-panel__scroll",
            storageKey: "planning-scroll:schedule-planning",
        },
        {
            pageSelector: ".schedule-draft-page",
            scrollSelector: ".schedule-draft-panel__scroll",
            storageKey: "planning-scroll:schedule-draft",
        },
    ];
    const PLANNING_SCROLL_MEMORY_TTL_MS = 10 * 60 * 1000;
    const SECTION_MEMORY = {
        profile: {
            listPath: "/main/",
        },
        calendar: {
            listPath: "/calendar/",
        },
        applications: {
            storageKey: "applications:last-detail-href",
            listStorageKey: "applications:last-list-href",
            listPath: "/applications/",
            detailPattern: /^\/applications\/(?:\d+|transfers\/\d+|urgent-closures\/\d+)\/$/,
        },
        employees: {
            storageKey: "employees:last-detail-href",
            listStorageKey: "employees:last-list-href",
            listPath: "/employees/",
            detailPattern: /^\/employee\/\d+\/$/,
        },
        departments: {
            storageKey: "departments:last-detail-href",
            listStorageKey: "departments:last-list-href",
            listPath: "/departments/",
            detailPattern: /^\/departments\/\d+\/$/,
        },
        analytics: {
            listPath: "/analytics/",
        },
        staffing: {
            listPath: "/staffing/",
        },
        notifications: {
            listStorageKey: "notifications:last-list-href",
            listPath: "/notifications/",
        },
    };
    const SESSION_MEMORY_KEYS = [
        "applications:list-scroll-state",
        "employees:list-scroll-state",
        "departments:list-scroll-state",
        "departments:detail-scroll-state",
        "calendar:path",
        "calendar:last-url",
        CALENDAR_ACTIVE_PREFERENCES_URL_KEY,
        "calendar:board-scroll-state",
        PLANNING_ACTIVE_URL_KEY,
        "schedule-planning:calendar-path",
        "schedule-planning:calendar-last-url",
    ];
    const SESSION_MEMORY_PREFIXES = [
        "profile-sections:",
        "profile-schedule-filters:",
        "calendar:preferences-draft:",
        "planning-scroll:",
    ];

    const navigationState = {
        isNavigating: false,
        targetHref: null,
        pendingPopstateHref: null,
    };
    let sidebarIndicatorFrame = 0;
    let sidebarIndicatorSettledTimer = 0;
    const navigationPrefetchCache = new Map();
    const navigationPrefetchInFlight = new Set();
    const navigationPrefetchedAssets = new Set();
    let navigationPrefetchGeneration = 0;
    let navigationIdlePrefetchTimer = 0;

    function getNowMs() {
        return window.performance && typeof window.performance.now === "function"
            ? window.performance.now()
            : Date.now();
    }

    function assetMatches(url, matchers) {
        return matchers.some(function (matcher) {
            return url.indexOf(matcher) !== -1;
        });
    }

    function getCurrentPath() {
        return window.location.pathname + window.location.search + window.location.hash;
    }

    function getPathFromHref(href) {
        try {
            const url = new URL(href, window.location.href);
            return url.pathname + url.search + url.hash;
        } catch (error) {
            return "";
        }
    }

    function isLogoutUrl(url) {
        return /\/logout\/?$/.test(url.pathname);
    }

    function clearKabinetSessionMemory() {
        try {
            Object.keys(SECTION_MEMORY).forEach(function (sectionKey) {
                const section = SECTION_MEMORY[sectionKey];
                if (section.storageKey) {
                    sessionStorage.removeItem(section.storageKey);
                }
                if (section.listStorageKey) {
                    sessionStorage.removeItem(section.listStorageKey);
                }
            });

            SESSION_MEMORY_KEYS.forEach(function (key) {
                sessionStorage.removeItem(key);
            });

            for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
                const key = sessionStorage.key(index);
                if (!key) {
                    continue;
                }

                if (SESSION_MEMORY_PREFIXES.some(function (prefix) {
                    return key.indexOf(prefix) === 0;
                })) {
                    sessionStorage.removeItem(key);
                }
            }
        } catch (error) {
        }
    }

    function toSameOriginUrl(href) {
        try {
            const url = new URL(href, window.location.href);
            if (url.origin !== window.location.origin) {
                return null;
            }
            return url;
        } catch (error) {
            return null;
        }
    }

    function isVacationPreferencesUrl(url) {
        return Boolean(url && (
            /^\/preferences\/\d+\/(?:readiness\/)?$/.test(url.pathname)
            || /^\/calendar\/drafts\/\d+\/$/.test(url.pathname)
        ));
    }

    function isSchedulePlanningHubUrl(url) {
        return Boolean(url && /^\/calendar\/planning\/(?:\d+\/)?$/.test(url.pathname));
    }

    function isSchedulePlanningNestedTargetUrl(url) {
        return Boolean(url && (
            url.pathname === SECTION_MEMORY.calendar.listPath
            || /^\/preferences\/\d+\/readiness\/$/.test(url.pathname)
            || /^\/calendar\/drafts\/\d+\/$/.test(url.pathname)
        ));
    }

    function isSchedulePlanningWorkspaceUrl(url) {
        if (!url) {
            return false;
        }
        if (isSchedulePlanningHubUrl(url)) {
            return true;
        }
        return url.searchParams.get("from") === "schedule_planning" && isSchedulePlanningNestedTargetUrl(url);
    }

    function isPlanningContextCalendarUrl(url) {
        return Boolean(
            url
            && url.pathname === SECTION_MEMORY.calendar.listPath
            && url.searchParams.get("from") === "schedule_planning"
        );
    }

    function stripPlanningContextParams(url) {
        url.searchParams.delete("from");
        url.searchParams.delete("back_url");
        url.searchParams.delete("back_label");
        return url;
    }

    function stripNavigationContextParams(url) {
        url.searchParams.delete("from");
        url.searchParams.delete("back_url");
        url.searchParams.delete("back_label");
        return url;
    }

    function getMemorySafePlanningUrl(href) {
        const url = toSameOriginUrl(href || window.location.href);
        if (!isSchedulePlanningWorkspaceUrl(url)) {
            return null;
        }

        url.searchParams.delete("open_modal");
        url.searchParams.delete("modal_error");
        url.searchParams.delete("calendar_modal");
        url.searchParams.delete("calendar_month");
        url.searchParams.delete("calendar_modal_focus");
        url.searchParams.delete("calendar_modal_scroll");
        return url;
    }

    function rememberActivePlanningHref(href) {
        const url = getMemorySafePlanningUrl(href || window.location.href);
        if (!url) {
            return false;
        }

        try {
            sessionStorage.setItem(PLANNING_ACTIVE_URL_KEY, url.href);
            return true;
        } catch (error) {
            return false;
        }
    }

    function getActivePlanningHref() {
        try {
            const rememberedUrl = getMemorySafePlanningUrl(sessionStorage.getItem(PLANNING_ACTIVE_URL_KEY));
            return rememberedUrl ? rememberedUrl.href : "";
        } catch (error) {
            return "";
        }
    }

    function getRememberedCalendarHref(fallbackHref) {
        const fallbackUrl = toSameOriginUrl(fallbackHref) || new URL(SECTION_MEMORY.calendar.listPath, window.location.origin);

        try {
            const rememberedPath = sessionStorage.getItem("calendar:path");
            const rememberedHref = sessionStorage.getItem("calendar:last-url");
            const rememberedUrl = toSameOriginUrl(rememberedHref);

            if (
                rememberedPath === SECTION_MEMORY.calendar.listPath
                && rememberedUrl
                && rememberedUrl.pathname === rememberedPath
                && rememberedUrl.searchParams.get("from") !== "schedule_planning"
            ) {
                if (rememberedUrl.searchParams.has("from")) {
                    stripNavigationContextParams(rememberedUrl);
                }
                return rememberedUrl.href;
            }
        } catch (error) {
        }

        return fallbackUrl.href;
    }

    function getCalendarSidebarHref(fallbackHref) {
        const rememberedHref = getRememberedCalendarHref(fallbackHref);
        const rememberedUrl = toSameOriginUrl(rememberedHref);
        if (isPlanningContextCalendarUrl(rememberedUrl)) {
            return stripPlanningContextParams(rememberedUrl).href;
        }
        if (rememberedUrl && rememberedUrl.pathname === SECTION_MEMORY.calendar.listPath) {
            return stripNavigationContextParams(rememberedUrl).href;
        }
        return rememberedHref;
    }

    function getActiveCalendarPreferenceHref() {
        try {
            const rememberedUrl = toSameOriginUrl(sessionStorage.getItem(CALENDAR_ACTIVE_PREFERENCES_URL_KEY));
            if (isVacationPreferencesUrl(rememberedUrl) && !isSchedulePlanningWorkspaceUrl(rememberedUrl)) {
                return rememberedUrl.href;
            }
        } catch (error) {
        }

        return "";
    }

    function rememberActiveCalendarPreferenceHref(href) {
        const url = toSameOriginUrl(href || window.location.href);
        if (!isVacationPreferencesUrl(url)) {
            return false;
        }

        try {
            sessionStorage.setItem(CALENDAR_ACTIVE_PREFERENCES_URL_KEY, url.href);
            return true;
        } catch (error) {
            return false;
        }
    }

    function clearActiveCalendarPreferenceHref() {
        try {
            sessionStorage.removeItem(CALENDAR_ACTIVE_PREFERENCES_URL_KEY);
        } catch (error) {
        }
    }

    function syncCalendarReturnLinks(root) {
        const scope = root || document;
        scope.querySelectorAll("[data-calendar-return-link]").forEach(function (link) {
            if (link.href) {
                link.href = getRememberedCalendarHref(link.href);
            }
        });
    }

    function isSectionDetailUrl(url, sectionKey) {
        const section = SECTION_MEMORY[sectionKey];
        if (!section || !url) {
            return false;
        }

        const contextualSectionKey = getContextualSectionKey(url);
        if (contextualSectionKey) {
            return contextualSectionKey === sectionKey;
        }

        return Boolean(section.detailPattern && section.detailPattern.test(url.pathname));
    }

    function isContextualDetailUrl(url) {
        if (!url) {
            return "";
        }

        return /^\/employee\/\d+\/$/.test(url.pathname)
            || /^\/applications\/(?:\d+|transfers\/\d+|urgent-closures\/\d+)\/$/.test(url.pathname);
    }

    function getContextualSectionKey(url) {
        if (!isContextualDetailUrl(url)) {
            return "";
        }

        const source = url.searchParams.get("from") || "";
        return source && SECTION_MEMORY[source] ? source : "";
    }

    function getSectionKeyFromDetailUrl(url) {
        return Object.keys(SECTION_MEMORY).find(function (sectionKey) {
            return isSectionDetailUrl(url, sectionKey);
        }) || "";
    }

    function isSectionListUrl(url, sectionKey) {
        const section = SECTION_MEMORY[sectionKey];
        return Boolean(section && url && url.pathname === section.listPath);
    }

    function rememberSectionDetailHref(href) {
        const url = toSameOriginUrl(href);
        if (!url) {
            return;
        }

        Object.keys(SECTION_MEMORY).forEach(function (sectionKey) {
            const section = SECTION_MEMORY[sectionKey];
            if (!section.storageKey || !isSectionDetailUrl(url, sectionKey)) {
                return;
            }

            try {
                sessionStorage.setItem(section.storageKey, url.href);
            } catch (error) {
            }
        });
    }

    function clearSectionMemory(sectionKey) {
        const section = SECTION_MEMORY[sectionKey];
        if (!section || !section.storageKey) {
            return;
        }

        try {
            sessionStorage.removeItem(section.storageKey);
        } catch (error) {
        }
    }

    function clearSectionListMemory(sectionKey) {
        const section = SECTION_MEMORY[sectionKey];
        if (!section || !section.listStorageKey) {
            return;
        }

        try {
            sessionStorage.removeItem(section.listStorageKey);
        } catch (error) {
        }
    }

    function getSectionListHref(sectionKey) {
        const section = SECTION_MEMORY[sectionKey];
        if (!section) {
            return "";
        }
        return new URL(section.listPath, window.location.origin).href;
    }

    function rememberSectionListHref(sectionKey, href) {
        const section = SECTION_MEMORY[sectionKey];
        const url = toSameOriginUrl(href || window.location.href);
        if (!section || !section.listStorageKey || !isSectionListUrl(url, sectionKey)) {
            return;
        }

        try {
            sessionStorage.setItem(section.listStorageKey, url.href);
        } catch (error) {
        }
    }

    function getRememberedSectionListHref(sectionKey) {
        const section = SECTION_MEMORY[sectionKey];
        if (!section || !section.listStorageKey) {
            return getSectionListHref(sectionKey);
        }

        try {
            const remembered = sessionStorage.getItem(section.listStorageKey);
            const rememberedUrl = toSameOriginUrl(remembered);
            if (isSectionListUrl(rememberedUrl, sectionKey)) {
                return rememberedUrl.href;
            }
        } catch (error) {
        }

        return getSectionListHref(sectionKey);
    }

    function syncSectionBackLinks(root) {
        const scope = root || document;
        scope.querySelectorAll("[data-section-back-link]").forEach(function (link) {
            const sectionKey = link.dataset.sectionBackLink;
            if (!sectionKey || !SECTION_MEMORY[sectionKey]) {
                return;
            }

            const href = getRememberedSectionListHref(sectionKey);
            if (href) {
                link.href = href;
            }
        });
        syncCalendarReturnLinks(scope);
    }

    function getRememberedSectionHref(sectionKey) {
        const section = SECTION_MEMORY[sectionKey];
        if (!section || !section.storageKey) {
            return "";
        }

        try {
            const remembered = sessionStorage.getItem(section.storageKey);
            const rememberedUrl = toSameOriginUrl(remembered);
            if (isSectionDetailUrl(rememberedUrl, sectionKey)) {
                return rememberedUrl.href;
            }
        } catch (error) {
        }

        return "";
    }

    function getSectionKeyFromLink(link) {
        const sectionKey = link ? link.dataset.sidebarKey : "";
        if (sectionKey === "schedule-planning") {
            return sectionKey;
        }
        return sectionKey && SECTION_MEMORY[sectionKey] ? sectionKey : "";
    }

    function getActiveSidebarSectionKey() {
        const activeLink = document.querySelector("[data-sidebar-link][data-sidebar-key][aria-current='page']")
            || document.querySelector("[data-sidebar-link][data-sidebar-key].active");
        return getSectionKeyFromLink(activeLink);
    }

    function getBackLabelForCurrentPage() {
        const currentUrl = toSameOriginUrl(window.location.href);
        if (!currentUrl) {
            return "";
        }

        if (/^\/employee\/\d+\/$/.test(currentUrl.pathname)) {
            return "К сотруднику";
        }

        if (/^\/applications\/\d+\/$/.test(currentUrl.pathname)) {
            return "К заявке";
        }

        if (/^\/applications\/transfers\/\d+\/$/.test(currentUrl.pathname)) {
            return "К переносу";
        }

        if (/^\/departments\/\d+\/$/.test(currentUrl.pathname)) {
            return "К группам";
        }

        if (currentUrl.pathname === "/applications/") {
            return "К заявкам";
        }

        if (currentUrl.pathname === "/employees/") {
            return "К сотрудникам";
        }

        if (currentUrl.pathname === "/departments/") {
            return "К отделам";
        }

        if (currentUrl.pathname === "/calendar/") {
            return "К графику";
        }

        if (isSchedulePlanningHubUrl(currentUrl)) {
            return "К планированию";
        }

        if (/^\/preferences\/\d+\/(?:readiness\/)?$/.test(currentUrl.pathname)) {
            return "К сбору";
        }

        if (/^\/calendar\/drafts\/\d+\/$/.test(currentUrl.pathname)) {
            return "К черновику";
        }

        if (currentUrl.pathname === "/main/") {
            return "К профилю";
        }

        if (currentUrl.pathname === "/analytics/") {
            return "К аналитике";
        }

        if (currentUrl.pathname === "/staffing/") {
            return "К правилам состава";
        }

        if (currentUrl.pathname === "/notifications/") {
            return "К уведомлениям";
        }

        return "";
    }

    function getCurrentRelativeHref() {
        return window.location.pathname + window.location.search + window.location.hash;
    }

    function withActivePlanningContext(href, options) {
        const nextOptions = options || {};
        const url = toSameOriginUrl(href);
        if (
            !url
            || getActiveSidebarSectionKey() !== "schedule-planning"
            || !isSchedulePlanningNestedTargetUrl(url)
        ) {
            return href;
        }

        if (!url.searchParams.has("from")) {
            url.searchParams.set("from", "schedule_planning");
        }

        if (!nextOptions.skipBackLink && !url.searchParams.has("back_url")) {
            const backLabel = getBackLabelForCurrentPage();
            if (backLabel) {
                url.searchParams.set("back_url", getCurrentRelativeHref());
                url.searchParams.set("back_label", backLabel);
            }
        }

        return url.href;
    }

    function withActiveSectionContext(href, options) {
        const nextOptions = options || {};
        const planningHref = withActivePlanningContext(href, nextOptions);
        const url = toSameOriginUrl(planningHref);
        if (!url) {
            return planningHref;
        }

        const sectionKey = getActiveSidebarSectionKey();
        const canUseActiveSection = Boolean(sectionKey && SECTION_MEMORY[sectionKey]);
        const currentSource = url.searchParams.get("from") || "";
        const canReplaceSource = !currentSource || currentSource === "calendar" || Boolean(SECTION_MEMORY[currentSource]);
        if (
            url.pathname === SECTION_MEMORY.calendar.listPath
            && canUseActiveSection
            && sectionKey !== "calendar"
        ) {
            if (canReplaceSource) {
                url.searchParams.set("from", sectionKey);
            }

            if (!nextOptions.skipBackLink && !url.searchParams.has("back_url")) {
                const backLabel = getBackLabelForCurrentPage();
                if (backLabel) {
                    url.searchParams.set("back_url", getCurrentRelativeHref());
                    url.searchParams.set("back_label", backLabel);
                }
            }

            return url.href;
        }

        if (!isContextualDetailUrl(url)) {
            return planningHref;
        }

        if (canUseActiveSection && canReplaceSource) {
            url.searchParams.set("from", sectionKey);
        }

        if (!nextOptions.skipBackLink && !url.searchParams.has("back_url")) {
            const backLabel = getBackLabelForCurrentPage();
            if (backLabel) {
                url.searchParams.set("back_url", getCurrentRelativeHref());
                url.searchParams.set("back_label", backLabel);
            }
        }

        return url.href;
    }

    function scrollToTop(element, preserveLeft) {
        if (!element) {
            return;
        }

        const left = preserveLeft ? element.scrollLeft : 0;
        if (typeof element.scrollTo === "function") {
            element.scrollTo({ top: 0, left: left, behavior: "smooth" });
            return;
        }

        element.scrollTop = 0;
        if (!preserveLeft) {
            element.scrollLeft = 0;
        }
    }

    function getNormalizedPlanningScrollPath() {
        const url = new URL(window.location.href);
        url.searchParams.delete("open_modal");
        url.searchParams.delete("modal_error");
        const query = url.searchParams.toString();
        return url.pathname + (query ? "?" + query : "");
    }

    function getActivePlanningScrollConfig() {
        return PLANNING_SCROLL_MEMORY_CONFIGS.find(function (config) {
            return Boolean(document.querySelector(config.pageSelector));
        }) || null;
    }

    function getPlanningScrollRoot(config) {
        return config ? document.querySelector(config.scrollSelector) : null;
    }

    function savePlanningScrollState() {
        const config = getActivePlanningScrollConfig();
        const scrollRoot = getPlanningScrollRoot(config);
        if (!config || !scrollRoot) {
            return;
        }

        try {
            sessionStorage.setItem(config.storageKey, JSON.stringify({
                path: getNormalizedPlanningScrollPath(),
                top: scrollRoot.scrollTop || 0,
                left: scrollRoot.scrollLeft || 0,
                timestamp: Date.now(),
            }));
        } catch (error) {
        }
    }

    function restorePlanningScrollState() {
        const config = getActivePlanningScrollConfig();
        const scrollRoot = getPlanningScrollRoot(config);
        if (!config || !scrollRoot) {
            return;
        }

        let state = null;
        try {
            state = JSON.parse(sessionStorage.getItem(config.storageKey) || "null");
        } catch (error) {
            state = null;
        }

        if (!state || state.path !== getNormalizedPlanningScrollPath() || Date.now() - Number(state.timestamp || 0) > PLANNING_SCROLL_MEMORY_TTL_MS) {
            try {
                sessionStorage.removeItem(config.storageKey);
            } catch (error) {
            }
            return;
        }

        try {
            sessionStorage.removeItem(config.storageKey);
        } catch (error) {
        }

        window.requestAnimationFrame(function () {
            scrollRoot.scrollTop = Number(state.top) || 0;
            scrollRoot.scrollLeft = Number(state.left) || 0;
        });
    }

    function resetSectionedPageToOverview(root) {
        if (!root) {
            return false;
        }

        root.dataset.activeSection = "overview";
        root.querySelectorAll("[data-profile-section]").forEach(function (section) {
            const isOverview = section.dataset.profileSection === "overview";
            section.classList.toggle("is-active", isOverview);
            section.setAttribute("aria-hidden", isOverview ? "false" : "true");
        });

        scrollToTop(document.scrollingElement || document.documentElement, false);
        root.querySelectorAll("[data-profile-overview-scroll], [data-profile-schedule-scroll], [data-profile-requests-scroll], [data-entitlement-scroll]").forEach(function (scrollRoot) {
            scrollToTop(scrollRoot, false);
        });
        return true;
    }

    function scrollCalendarBoardToTop() {
        const boardScroll = document.querySelector("[data-calendar-grid-body]") || document.querySelector(".calendar-board-scroll");
        if (!boardScroll) {
            return false;
        }

        scrollToTop(boardScroll, true);

        const gridHead = document.querySelector("[data-calendar-grid-head]");
        if (gridHead) {
            gridHead.scrollLeft = boardScroll.scrollLeft;
        }
        return true;
    }

    function handleLocalSectionRepeat(sectionKey) {
        if (sectionKey === "profile") {
            return resetSectionedPageToOverview(document.querySelector("[data-profile-sections]:not([data-applications-page])"));
        }

        if (sectionKey === "applications") {
            return resetSectionedPageToOverview(document.querySelector("[data-applications-page]"));
        }

        if (sectionKey === "calendar") {
            return scrollCalendarBoardToTop();
        }

        return false;
    }

    function applyRememberedSectionHref(link, sectionKey) {
        if (!link) {
            return;
        }

        const currentUrl = toSameOriginUrl(window.location.href);
        if (isSectionListUrl(currentUrl, sectionKey)) {
            clearSectionMemory(sectionKey);
            rememberSectionListHref(sectionKey, currentUrl.href);
            link.href = getSectionListHref(sectionKey);
            return;
        }

        if (isSectionDetailUrl(currentUrl, sectionKey)) {
            rememberSectionDetailHref(currentUrl.href);
            link.href = getRememberedSectionListHref(sectionKey);
            return;
        }

        link.href = getRememberedSectionHref(sectionKey) || getRememberedSectionListHref(sectionKey);
    }

    function handleSectionListRepeatClick(event, nav, link) {
        const sectionKey = getSectionKeyFromLink(link);
        const currentUrl = toSameOriginUrl(window.location.href);
        if (!sectionKey || !isSectionListUrl(currentUrl, sectionKey)) {
            return false;
        }

        const linkUrl = toSameOriginUrl(link.href);
        if (
            sectionKey === "calendar"
            && isPlanningContextCalendarUrl(currentUrl)
            && linkUrl
            && !isPlanningContextCalendarUrl(linkUrl)
            && !isCurrentPageUrl(linkUrl.href)
        ) {
            return false;
        }

        event.preventDefault();
        clearSectionMemory(sectionKey);
        clearSectionListMemory(sectionKey);

        const defaultHref = getSectionListHref(sectionKey);
        const resetEvent = new CustomEvent("app:section-sidebar-repeat", {
            cancelable: true,
            detail: {
                sectionKey: sectionKey,
                defaultHref: defaultHref,
            },
        });
        const wasHandled = !document.dispatchEvent(resetEvent);
        syncSidebarRememberedHrefs(nav);

        if (wasHandled) {
            return true;
        }

        const wasHandledLocally = handleLocalSectionRepeat(sectionKey);
        if (wasHandledLocally) {
            return true;
        }

        if (defaultHref && !isCurrentPageUrl(defaultHref)) {
            navigateWithFetch(defaultHref, true);
            return true;
        }

        window.scrollTo({ top: 0, left: 0, behavior: "smooth" });
        return true;
    }

    function dispatchNavigationEvent(url) {
        const nextUrl = url || new URL(window.location.href);
        document.dispatchEvent(new CustomEvent("app:navigation", {
            detail: {
                pathname: nextUrl.pathname,
                url: nextUrl.href,
            },
        }));
    }

    function suppressPageEntryMotion() {
        document.documentElement.classList.add(PAGE_TRANSITION_CLASS);
    }

    function releasePageEntryMotion() {
        window.requestAnimationFrame(function () {
            window.requestAnimationFrame(function () {
                document.documentElement.classList.remove(PAGE_TRANSITION_CLASS);
            });
        });
    }

    function getStylesheetHrefs(targetDocument) {
        return Array.from(targetDocument.querySelectorAll("link[rel='stylesheet'][href]"))
            .map(function (styleNode) {
                return styleNode.href;
            })
            .filter(function (href) {
                return href && !assetMatches(href, CORE_STYLE_MATCHERS);
            });
    }

    function findCurrentStylesheet(href) {
        return Array.from(document.querySelectorAll("link[rel='stylesheet'][href]")).find(function (styleNode) {
            return styleNode.href === href;
        });
    }

    function isStylesheetReady(styleNode) {
        return Boolean(styleNode && styleNode.sheet);
    }

    function waitForStylesheetReady(styleNode) {
        if (!styleNode || isStylesheetReady(styleNode)) {
            return Promise.resolve();
        }

        return new Promise(function (resolve) {
            let settled = false;
            const finish = function () {
                if (settled) {
                    return;
                }
                settled = true;
                window.clearTimeout(timer);
                styleNode.removeEventListener("load", finish);
                styleNode.removeEventListener("error", finish);
                resolve();
            };
            const timer = window.setTimeout(finish, NAVIGATION_STYLE_LOAD_TIMEOUT_MS);

            styleNode.addEventListener("load", finish);
            styleNode.addEventListener("error", finish);
        });
    }

    function createPreparedStylesheet(nextDocument, href) {
        const nextStyle = Array.from(nextDocument.querySelectorAll("link[rel='stylesheet'][href]")).find(function (styleNode) {
            return styleNode.href === href;
        });

        if (!nextStyle) {
            return null;
        }

        const style = nextStyle.cloneNode(true);
        const originalMedia = style.getAttribute("media") || "";
        style.href = href;
        style.dataset.kabinetNavigationPendingStyle = "true";
        style.dataset.kabinetNavigationOriginalMedia = originalMedia;
        style.media = "print";
        document.head.appendChild(style);
        return style;
    }

    function activatePreparedStylesheets(nextStyleHrefs) {
        nextStyleHrefs.forEach(function (href) {
            const styleNode = findCurrentStylesheet(href);
            if (!styleNode || styleNode.dataset.kabinetNavigationPendingStyle !== "true") {
                return;
            }

            const originalMedia = styleNode.dataset.kabinetNavigationOriginalMedia || "";
            if (originalMedia) {
                styleNode.media = originalMedia;
            } else {
                styleNode.removeAttribute("media");
            }
            delete styleNode.dataset.kabinetNavigationPendingStyle;
            delete styleNode.dataset.kabinetNavigationOriginalMedia;
        });
    }

    function removeStaleDocumentStyles(nextStyleHrefs) {
        const nextStyleSet = new Set(nextStyleHrefs);

        Array.from(document.querySelectorAll("link[rel='stylesheet'][href]")).forEach(function (styleNode) {
            const href = styleNode.href;
            if (!href || assetMatches(href, CORE_STYLE_MATCHERS)) {
                return;
            }
            if (!nextStyleSet.has(href)) {
                styleNode.remove();
            }
        });
    }

    async function syncDocumentStyles(nextDocument) {
        const nextStyleHrefs = getStylesheetHrefs(nextDocument);
        const pendingStyles = [];

        nextStyleHrefs.forEach(function (href) {
            let styleNode = findCurrentStylesheet(href);
            if (!styleNode) {
                styleNode = createPreparedStylesheet(nextDocument, href);
            }
            if (styleNode) {
                pendingStyles.push(waitForStylesheetReady(styleNode));
            }
        });

        await Promise.all(pendingStyles);
        activatePreparedStylesheets(nextStyleHrefs);
        removeStaleDocumentStyles(nextStyleHrefs);
    }

    async function syncDocumentScripts(nextDocument) {
        const nextScripts = Array.from(nextDocument.querySelectorAll("script[src]"));

        for (const scriptNode of nextScripts) {
            const src = scriptNode.src;
            if (!src || assetMatches(src, CORE_SCRIPT_MATCHERS)) {
                continue;
            }
            if (Array.from(document.querySelectorAll("script[src]")).some(function (currentScript) {
                return currentScript.src === src;
            })) {
                continue;
            }

            await new Promise(function (resolve, reject) {
                const script = document.createElement("script");
                script.src = src;
                script.defer = true;
                script.onload = resolve;
                script.onerror = reject;
                document.body.appendChild(script);
            });
        }
    }

    function syncBodyClass(nextDocument) {
        if (!nextDocument.body) {
            return;
        }

        const nextBodyClass = nextDocument.body.getAttribute("class");
        if (nextBodyClass) {
            document.body.setAttribute("class", nextBodyClass);
        } else {
            document.body.removeAttribute("class");
        }
    }

    function syncKnownPageClasses(nextDocument) {
        const isCalendarPage = Boolean(nextDocument.querySelector(CALENDAR_ROOT_SELECTOR));

        PAGE_STATE_CLASSES.forEach(function (className) {
            document.documentElement.classList.remove(className);
            document.body.classList.remove(className);
        });

        if (isCalendarPage) {
            document.documentElement.classList.add("is-calendar-page");
            document.body.classList.add("is-calendar-page");
        }
    }

    function syncMessages(nextDocument) {
        const currentMessages = document.querySelector(".messages-wrapper");
        const nextMessages = nextDocument.querySelector(".messages-wrapper");
        const appContainer = document.querySelector("[data-app-container]");

        if (currentMessages && nextMessages) {
            currentMessages.replaceWith(nextMessages.cloneNode(true));
            return;
        }

        if (currentMessages && !nextMessages) {
            currentMessages.remove();
            return;
        }

        if (!currentMessages && nextMessages && appContainer) {
            appContainer.parentNode.insertBefore(nextMessages.cloneNode(true), appContainer);
        }
    }

    function updateSidebarIndicator(nav, activeLink) {
        if (!nav) {
            return;
        }

        const currentActive = activeLink || nav.querySelector("[data-sidebar-link].active, [data-sidebar-link][aria-current='page']");
        if (!currentActive) {
            nav.dataset.sidebarHasActive = "false";
            return;
        }

        const navRect = nav.getBoundingClientRect();
        const activeRect = currentActive.getBoundingClientRect();
        const activeTop = activeRect.top - navRect.top;

        if (!Number.isFinite(activeTop) || !Number.isFinite(activeRect.height) || activeRect.height <= 0) {
            nav.dataset.sidebarHasActive = "false";
            return;
        }

        nav.style.setProperty("--sidebar-active-y", activeTop + "px");
        nav.style.setProperty("--sidebar-active-h", activeRect.height + "px");
        nav.dataset.sidebarHasActive = "true";
    }

    function setSidebarActiveLink(nav, activeLink) {
        if (!nav) {
            return;
        }

        Array.from(nav.querySelectorAll("[data-sidebar-link]")).forEach(function (link) {
            const isActive = link === activeLink;
            link.classList.toggle("active", isActive);
            if (isActive) {
                link.setAttribute("aria-current", "page");
            } else {
                link.removeAttribute("aria-current");
            }
        });

        updateSidebarIndicator(nav, activeLink);
    }

    function primeSidebarIndicator(nav) {
        if (!nav) {
            return;
        }

        const indicator = nav.querySelector(".sidebar__active-indicator");
        if (!indicator) {
            updateSidebarIndicator(nav);
            nav.classList.add("is-ready");
            return;
        }

        indicator.style.transition = "none";
        updateSidebarIndicator(nav);
        nav.classList.add("is-ready");
        indicator.getBoundingClientRect();

        window.requestAnimationFrame(function () {
            indicator.style.transition = "";
        });
    }

    function syncSidebarLink(currentLink, nextLink) {
        currentLink.href = nextLink.href;
        currentLink.classList.toggle("active", nextLink.classList.contains("active"));

        if (nextLink.hasAttribute("aria-current")) {
            currentLink.setAttribute("aria-current", nextLink.getAttribute("aria-current") || "page");
        } else {
            currentLink.removeAttribute("aria-current");
        }

        const currentSide = currentLink.querySelector(".sidebar__link-side");
        const nextSide = nextLink.querySelector(".sidebar__link-side");

        if (nextLink.dataset.sidebarDefaultHref) {
            currentLink.dataset.sidebarDefaultHref = nextLink.dataset.sidebarDefaultHref;
        } else {
            delete currentLink.dataset.sidebarDefaultHref;
        }

        if (currentSide && nextSide) {
            currentSide.replaceWith(nextSide.cloneNode(true));
        } else if (nextSide) {
            currentLink.appendChild(nextSide.cloneNode(true));
        } else if (currentSide) {
            currentSide.remove();
        }
    }

    function applyRememberedCalendarHref(link) {
        if (!link || !link.href) {
            return;
        }

        const defaultHref = link.dataset.sidebarDefaultHref || link.href;
        const currentUrl = toSameOriginUrl(window.location.href);
        if (currentUrl && currentUrl.pathname === SECTION_MEMORY.calendar.listPath) {
            clearActiveCalendarPreferenceHref();
        }
        if (!isVacationPreferencesUrl(currentUrl)) {
            const activePreferencesHref = getActiveCalendarPreferenceHref();
            if (activePreferencesHref) {
                link.href = activePreferencesHref;
                return;
            }
        }

        link.href = getCalendarSidebarHref(defaultHref);
    }

    function applyRememberedPlanningHref(link) {
        if (!link || !link.href) {
            return;
        }

        const defaultHref = link.dataset.sidebarDefaultHref || link.href;
        const currentUrl = toSameOriginUrl(window.location.href);
        if (isSchedulePlanningWorkspaceUrl(currentUrl)) {
            rememberActivePlanningHref(currentUrl.href);
            link.href = defaultHref;
            return;
        }

        const activePlanningHref = getActivePlanningHref();
        if (activePlanningHref) {
            link.href = activePlanningHref;
        }
    }

    function syncSidebarNavigation(nextDocument) {
        const currentNav = document.querySelector("[data-sidebar-nav]");
        const nextNav = nextDocument.querySelector("[data-sidebar-nav]");

        if (!currentNav || !nextNav) {
            return;
        }

        const nextLinks = Array.from(nextNav.querySelectorAll("[data-sidebar-link][data-sidebar-key]"));
        Array.from(currentNav.querySelectorAll("[data-sidebar-link][data-sidebar-key]")).forEach(function (currentLink) {
            const key = currentLink.dataset.sidebarKey;
            const nextLink = nextLinks.find(function (link) {
                return link.dataset.sidebarKey === key;
            });

            if (nextLink) {
                syncSidebarLink(currentLink, nextLink);
            }
        });

        syncSidebarRememberedHrefs(currentNav);
        syncSectionBackLinks();
        updateSidebarIndicator(currentNav);
    }

    function syncSidebarRememberedHrefs(nav) {
        if (!nav) {
            return;
        }

        applyRememberedCalendarHref(nav.querySelector('[data-sidebar-key="calendar"]'));
        applyRememberedPlanningHref(nav.querySelector('[data-sidebar-key="schedule-planning"]'));
        applyRememberedSectionHref(nav.querySelector('[data-sidebar-key="applications"]'), "applications");
        applyRememberedSectionHref(nav.querySelector('[data-sidebar-key="employees"]'), "employees");
        applyRememberedSectionHref(nav.querySelector('[data-sidebar-key="departments"]'), "departments");
        applyRememberedSectionHref(nav.querySelector('[data-sidebar-key="notifications"]'), "notifications");
    }

    function replacePageMain(nextDocument) {
        const currentMain = document.querySelector(".page-main");
        const nextMain = nextDocument.querySelector(".page-main");

        if (!currentMain || !nextMain) {
            return false;
        }

        closeAllModals();
        currentMain.replaceWith(nextMain);
        document.title = nextDocument.title;
        syncBodyClass(nextDocument);
        syncKnownPageClasses(nextDocument);
        syncMessages(nextDocument);
        syncSidebarNavigation(nextDocument);
        initScrollPerformanceHints(nextMain);
        return true;
    }

    function isPlainLeftClick(event, link) {
        if (
            !link
            || !link.href
            || event.defaultPrevented
            || event.button !== 0
            || event.detail === 0
            || event.metaKey
            || event.ctrlKey
            || event.shiftKey
            || event.altKey
            || (link.target && link.target !== "_self")
            || link.hasAttribute("download")
        ) {
            return false;
        }

        return true;
    }

    function isSidebarRepeatClick(event, link) {
        return Boolean(
            link
            && link.href
            && !event.defaultPrevented
            && event.button === 0
            && !event.metaKey
            && !event.ctrlKey
            && !event.shiftKey
            && !event.altKey
            && (!link.target || link.target === "_self")
            && !link.hasAttribute("download")
        );
    }

    function canNavigateWithFetch(targetUrl) {
        try {
            const url = new URL(targetUrl, window.location.href);
            return (
                url.origin === window.location.origin
                && !isLogoutUrl(url)
                && (url.pathname + url.search + url.hash) !== getCurrentPath()
            );
        } catch (error) {
            return false;
        }
    }

    function isCurrentPageUrl(targetUrl) {
        try {
            const url = new URL(targetUrl, window.location.href);
            return url.origin === window.location.origin && (url.pathname + url.search + url.hash) === getCurrentPath();
        } catch (error) {
            return false;
        }
    }

    function clearNavigationPrefetchCache() {
        navigationPrefetchGeneration += 1;
        navigationPrefetchCache.clear();
        navigationPrefetchInFlight.clear();
    }

    function pruneNavigationPrefetchCache() {
        const now = getNowMs();
        Array.from(navigationPrefetchCache.keys()).forEach(function (key) {
            const entry = navigationPrefetchCache.get(key);
            if (!entry || now - entry.timestamp > NAVIGATION_PREFETCH_TTL_MS) {
                navigationPrefetchCache.delete(key);
            }
        });

        while (navigationPrefetchCache.size > NAVIGATION_PREFETCH_MAX_ENTRIES) {
            const firstKey = navigationPrefetchCache.keys().next().value;
            navigationPrefetchCache.delete(firstKey);
        }
    }

    function storeNavigationPrefetch(key, entry) {
        if (!key || !entry || !entry.html) {
            return;
        }
        navigationPrefetchCache.set(key, {
            finalHref: entry.finalHref || key,
            html: entry.html,
            timestamp: entry.timestamp || getNowMs(),
        });
        pruneNavigationPrefetchCache();
    }

    function prefetchNavigationAsset(href, assetType) {
        const url = toSameOriginUrl(href);
        if (!url || navigationPrefetchedAssets.has(url.href)) {
            return;
        }

        navigationPrefetchedAssets.add(url.href);
        const link = document.createElement("link");
        link.rel = "prefetch";
        link.href = url.href;
        if (assetType) {
            link.as = assetType;
        }
        document.head.appendChild(link);
    }

    function warmNavigationAssets(html) {
        if (!html) {
            return;
        }

        let parsedDocument = null;
        try {
            parsedDocument = new DOMParser().parseFromString(html, "text/html");
        } catch (error) {
            return;
        }

        getStylesheetHrefs(parsedDocument).forEach(function (href) {
            if (!findCurrentStylesheet(href)) {
                prefetchNavigationAsset(href, "style");
            }
        });

        Array.from(parsedDocument.querySelectorAll("script[src]")).forEach(function (scriptNode) {
            const src = scriptNode.src;
            if (!src || assetMatches(src, CORE_SCRIPT_MATCHERS)) {
                return;
            }
            if (Array.from(document.querySelectorAll("script[src]")).some(function (currentScript) {
                return currentScript.src === src;
            })) {
                return;
            }
            prefetchNavigationAsset(src, "script");
        });
    }

    function prefetchNavigationHref(href) {
        const url = toSameOriginUrl(href);
        if (!url || !canNavigateWithFetch(url.href)) {
            return;
        }
        if (
            isSchedulePlanningWorkspaceUrl(url)
            || url.pathname === SECTION_MEMORY.calendar.listPath
            || url.pathname === "/staffing/"
        ) {
            return;
        }

        const key = url.href;
        if (navigationPrefetchInFlight.has(key)) {
            return;
        }
        const existing = navigationPrefetchCache.get(key);
        if (existing && getNowMs() - existing.timestamp <= NAVIGATION_PREFETCH_TTL_MS) {
            return;
        }

        const generation = navigationPrefetchGeneration;
        navigationPrefetchInFlight.add(key);
        fetch(key, {
            headers: {
                "X-Kabinet-Prefetch": "1",
            },
        })
            .then(function (response) {
                if (!response.ok) {
                    return null;
                }
                return response.text().then(function (html) {
                    warmNavigationAssets(html);
                    return {
                        finalHref: (toSameOriginUrl(response.url) || url).href,
                        html: html,
                        timestamp: getNowMs(),
                    };
                });
            })
            .then(function (entry) {
                if (!entry || generation !== navigationPrefetchGeneration) {
                    return;
                }
                storeNavigationPrefetch(key, entry);
            })
            .catch(function () {
            })
            .finally(function () {
                navigationPrefetchInFlight.delete(key);
            });
    }

    function takeNavigationPrefetch(targetHref) {
        pruneNavigationPrefetchCache();
        const entry = navigationPrefetchCache.get(targetHref);
        if (!entry) {
            return null;
        }

        navigationPrefetchCache.delete(targetHref);
        if (getNowMs() - entry.timestamp > NAVIGATION_PREFETCH_TTL_MS) {
            return null;
        }
        return entry;
    }

    function rememberCurrentNavigationDocument() {
        const currentUrl = toSameOriginUrl(window.location.href);
        if (!currentUrl || !canNavigateWithFetch(currentUrl.href)) {
            return;
        }

        try {
            storeNavigationPrefetch(currentUrl.href, {
                finalHref: currentUrl.href,
                html: "<!doctype html>\n" + document.documentElement.outerHTML,
                timestamp: getNowMs(),
            });
        } catch (error) {
        }
    }

    function dispatchBeforeNavigation(targetHref) {
        savePlanningScrollState();
        rememberActivePlanningHref(window.location.href);
        document.dispatchEvent(new CustomEvent("app:before-navigation", {
            detail: {
                href: targetHref,
            },
        }));
    }

    function shouldHandleLinkNavigation(event, link) {
        return isPlainLeftClick(event, link) && canNavigateWithFetch(link.href);
    }

    function setNavigationBusy(isBusy, targetHref) {
        navigationState.isNavigating = isBusy;
        navigationState.targetHref = isBusy ? targetHref : null;

        const nav = document.querySelector("[data-sidebar-nav]");
        if (nav) {
            nav.classList.toggle("is-navigating", isBusy);
            if (isBusy) {
                nav.classList.add("is-ready");
                suppressPageEntryMotion();
            }
        }
    }

    function scheduleSidebarIndicatorUpdate() {
        if (sidebarIndicatorFrame) {
            window.cancelAnimationFrame(sidebarIndicatorFrame);
        }

        sidebarIndicatorFrame = window.requestAnimationFrame(function () {
            sidebarIndicatorFrame = 0;
            updateSidebarIndicator(document.querySelector("[data-sidebar-nav]"));
            window.requestAnimationFrame(function () {
                updateSidebarIndicator(document.querySelector("[data-sidebar-nav]"));
            });
        });

        window.clearTimeout(sidebarIndicatorSettledTimer);
        sidebarIndicatorSettledTimer = window.setTimeout(function () {
            sidebarIndicatorSettledTimer = 0;
            updateSidebarIndicator(document.querySelector("[data-sidebar-nav]"));
        }, 420);
    }

    function initScrollPerformanceHints(root) {
        const scope = root || document;
        scope.querySelectorAll(SCROLL_PERFORMANCE_SELECTOR).forEach(function (scrollRoot) {
            if (scrollRoot.dataset.scrollPerformanceBound === "true") {
                return;
            }

            scrollRoot.dataset.scrollPerformanceBound = "true";
            let scrollTimer = 0;
            scrollRoot.addEventListener("scroll", function () {
                scrollRoot.classList.add("is-scrolling");
                if (scrollTimer) {
                    window.clearTimeout(scrollTimer);
                }
                scrollTimer = window.setTimeout(function () {
                    scrollTimer = 0;
                    scrollRoot.classList.remove("is-scrolling");
                }, 140);
            }, { passive: true });
        });
    }

    function initNavigationPrefetch() {
        document.addEventListener("pointerover", function (event) {
            const link = event.target instanceof Element
                ? event.target.closest("a[data-app-link], [data-sidebar-link], [data-section-back-link], [data-calendar-return-link]")
                : null;
            if (link && link.href) {
                prefetchNavigationHref(link.href);
            }
        }, { passive: true });

        document.addEventListener("focusin", function (event) {
            const link = event.target instanceof Element
                ? event.target.closest("a[data-app-link], [data-sidebar-link], [data-section-back-link], [data-calendar-return-link]")
                : null;
            if (link && link.href) {
                prefetchNavigationHref(link.href);
            }
        });

        document.addEventListener("submit", clearNavigationPrefetchCache, { capture: true });
    }

    function getIdlePrefetchLinks() {
        const nav = document.querySelector("[data-sidebar-nav]");
        if (!nav) {
            return [];
        }

        return NAVIGATION_IDLE_PREFETCH_KEYS
            .map(function (key) {
                return nav.querySelector('[data-sidebar-link][data-sidebar-key="' + key + '"]');
            })
            .filter(function (link) {
                if (!link || !link.href || isCurrentPageUrl(link.href)) {
                    return false;
                }
                const url = toSameOriginUrl(link.href);
                return Boolean(url && canNavigateWithFetch(url.href));
            });
    }

    function scheduleIdleNavigationPrefetch() {
        if (navigationIdlePrefetchTimer) {
            window.clearTimeout(navigationIdlePrefetchTimer);
        }

        navigationIdlePrefetchTimer = window.setTimeout(function () {
            navigationIdlePrefetchTimer = 0;
            if (navigationState.isNavigating) {
                scheduleIdleNavigationPrefetch();
                return;
            }

            const links = getIdlePrefetchLinks();
            let index = 0;
            const prefetchNext = function () {
                if (index >= links.length || navigationState.isNavigating) {
                    return;
                }
                prefetchNavigationHref(links[index].href);
                index += 1;
                if (index < links.length) {
                    window.setTimeout(prefetchNext, NAVIGATION_IDLE_PREFETCH_STEP_MS);
                }
            };
            prefetchNext();
        }, NAVIGATION_IDLE_PREFETCH_DELAY_MS);
    }

    async function navigateWithFetch(targetUrl, pushState) {
        const target = new URL(targetUrl, window.location.href);
        const targetHref = target.href;

        if (navigationState.isNavigating) {
            return;
        }

        dispatchBeforeNavigation(targetHref);
        rememberSectionDetailHref(targetHref);
        rememberCurrentNavigationDocument();
        setNavigationBusy(true, targetHref);

        try {
            const prefetched = takeNavigationPrefetch(targetHref);
            let finalUrl = prefetched ? toSameOriginUrl(prefetched.finalHref) || target : null;
            let html = prefetched ? prefetched.html : "";

            if (!prefetched) {
                const response = await fetch(targetHref);
                if (!response.ok) {
                    const staleSectionKey = response.status === 404 ? getSectionKeyFromDetailUrl(target) : "";
                    if (staleSectionKey) {
                        clearSectionMemory(staleSectionKey);
                        setNavigationBusy(false, null);
                        navigateWithFetch(getRememberedSectionListHref(staleSectionKey), true);
                        return;
                    }
                    throw new Error("Navigation failed");
                }

                finalUrl = toSameOriginUrl(response.url) || target;
                html = await response.text();
            }

            const finalHref = finalUrl.href;
            storeNavigationPrefetch(finalHref, {
                finalHref: finalHref,
                html: html,
                timestamp: getNowMs(),
            });
            const nextDocument = new DOMParser().parseFromString(html, "text/html");

            await syncDocumentStyles(nextDocument);
            if (!replacePageMain(nextDocument)) {
                throw new Error("Navigation shell mismatch");
            }

            if (pushState) {
                window.history.pushState({}, "", finalHref);
            }

            window.scrollTo({ top: 0, left: 0, behavior: "auto" });
            await syncDocumentScripts(nextDocument);
            initSidebarNavigation();
            initDateFields();
            setNavigationBusy(false, null);
            const pendingPopstateHref = navigationState.pendingPopstateHref;
            navigationState.pendingPopstateHref = null;
            if (pendingPopstateHref && pendingPopstateHref !== finalHref) {
                navigateWithFetch(pendingPopstateHref, false);
                return;
            }
            dispatchNavigationEvent(finalUrl);
            releasePageEntryMotion();
        } catch (error) {
            document.documentElement.classList.remove(PAGE_TRANSITION_CLASS);
            window.location.href = targetHref;
        }
    }

    function initSidebarNavigation() {
        const nav = document.querySelector("[data-sidebar-nav]");
        if (!nav) {
            return;
        }

        rememberSectionDetailHref(window.location.href);
        const currentUrl = toSameOriginUrl(window.location.href);
        if (isSchedulePlanningWorkspaceUrl(currentUrl)) {
            rememberActivePlanningHref(currentUrl.href);
        }
        if (isVacationPreferencesUrl(currentUrl) && !isSchedulePlanningWorkspaceUrl(currentUrl)) {
            rememberActiveCalendarPreferenceHref(currentUrl.href);
        }
        syncSidebarRememberedHrefs(nav);
        syncSectionBackLinks();

        const links = Array.from(nav.querySelectorAll("[data-sidebar-link]"));
        const shouldPrimeIndicator = nav.dataset.sidebarInitialized !== "true";
        const previousController = window.__sidebarNavigationController;
        if (previousController) {
            previousController.abort();
        }

        const controller = new AbortController();
        const signal = controller.signal;
        window.__sidebarNavigationController = controller;

        nav.addEventListener("click", function (event) {
            const target = event.target instanceof Element ? event.target : null;
            const link = target ? target.closest("[data-sidebar-link]") : null;
            if (!link || !nav.contains(link)) {
                return;
            }

            syncSidebarRememberedHrefs(nav);
            if (isLogoutUrl(new URL(link.href, window.location.href))) {
                clearKabinetSessionMemory();
                return;
            }
            if (isSidebarRepeatClick(event, link) && handleSectionListRepeatClick(event, nav, link)) {
                event.stopPropagation();
            }
        }, { capture: true, signal: signal });

        links.forEach(function (link) {
            if (!link.href) {
                return;
            }

            link.addEventListener("click", function (event) {
                syncSidebarRememberedHrefs(nav);

                if (isLogoutUrl(new URL(link.href, window.location.href))) {
                    clearKabinetSessionMemory();
                    return;
                }

                if (isSidebarRepeatClick(event, link) && handleSectionListRepeatClick(event, nav, link)) {
                    return;
                }

                if (isPlainLeftClick(event, link) && isCurrentPageUrl(link.href)) {
                    event.preventDefault();
                    return;
                }

                if (navigationState.isNavigating) {
                    event.preventDefault();
                    return;
                }

                if (!shouldHandleLinkNavigation(event, link)) {
                    return;
                }

                event.preventDefault();
                nav.classList.add("is-ready");
                setSidebarActiveLink(nav, link);
                navigateWithFetch(link.href, true);
            }, { signal: signal });
        });

        setNavigationBusy(false, null);
        if (shouldPrimeIndicator) {
            primeSidebarIndicator(nav);
            nav.dataset.sidebarInitialized = "true";
        } else {
            updateSidebarIndicator(nav);
            nav.classList.add("is-ready");
        }

        window.addEventListener("pageshow", function () {
            setNavigationBusy(false, null);
            updateSidebarIndicator(nav);
            nav.classList.add("is-ready");
        }, { signal: signal });

        window.addEventListener("resize", scheduleSidebarIndicatorUpdate, { signal: signal });

        nav.addEventListener("transitionend", function (event) {
            const target = event.target instanceof Element ? event.target : null;
            if (!target || target.closest(".sidebar__active-indicator")) {
                return;
            }

            scheduleSidebarIndicatorUpdate();
        }, { signal: signal });

        if ("ResizeObserver" in window) {
            const sidebarResizeObserver = new ResizeObserver(scheduleSidebarIndicatorUpdate);
            sidebarResizeObserver.observe(nav);
            links.forEach(function (link) {
                sidebarResizeObserver.observe(link);
            });
            signal.addEventListener("abort", function () {
                sidebarResizeObserver.disconnect();
            }, { once: true });
        }

        window.addEventListener("popstate", function () {
            const url = new URL(window.location.href);
            if (url.origin !== window.location.origin || isLogoutUrl(url)) {
                return;
            }
            if (navigationState.isNavigating) {
                navigationState.pendingPopstateHref = url.href;
                return;
            }
            navigateWithFetch(url.href, false);
        }, { signal: signal });
    }

    window.KabinetNavigation = Object.assign(window.KabinetNavigation || {}, {
        rememberSectionListHref: rememberSectionListHref,
        getRememberedSectionListHref: getRememberedSectionListHref,
        rememberSectionDetailHref: rememberSectionDetailHref,
        clearSectionMemory: clearSectionMemory,
        clearSectionListMemory: clearSectionListMemory,
        getSectionListHref: getSectionListHref,
        syncSectionBackLinks: syncSectionBackLinks,
        getRememberedCalendarHref: getRememberedCalendarHref,
        rememberActivePlanningHref: rememberActivePlanningHref,
        getActivePlanningHref: getActivePlanningHref,
        rememberActiveCalendarPreferenceHref: rememberActiveCalendarPreferenceHref,
        getActiveCalendarPreferenceHref: getActiveCalendarPreferenceHref,
        clearActiveCalendarPreferenceHref: clearActiveCalendarPreferenceHref,
        navigate: function (targetUrl, pushState) {
            if (canNavigateWithFetch(targetUrl)) {
                navigateWithFetch(targetUrl, pushState !== false);
                return true;
            }
            return false;
        },
    });

    function hasTextSelection() {
        const selection = window.getSelection ? window.getSelection().toString().trim() : "";
        return Boolean(selection);
    }

    function requestDatePicker(input) {
        if (!input || input.disabled || input.readOnly) {
            return;
        }
        if (window.KabinetDatePicker && typeof window.KabinetDatePicker.open === "function") {
            window.KabinetDatePicker.open(input);
        }
    }

    function syncDateInputState(input) {
        if (!input || input.type !== "date") {
            return;
        }

        input.classList.toggle("is-empty", !input.value);
    }

    function initDateFields() {
        if (window.KabinetDatePicker && typeof window.KabinetDatePicker.init === "function") {
            window.KabinetDatePicker.init();
        }
        document.querySelectorAll("[data-date-field] input[type='date']").forEach(function (input) {
            if (input.dataset.dateFieldBound === "true") {
                syncDateInputState(input);
                return;
            }

            const field = input.closest("[data-date-field]");
            input.dataset.dateFieldBound = "true";
            syncDateInputState(input);

            if (field) {
                field.addEventListener("click", function (event) {
                    if (event.target.closest("button, select, textarea")) {
                        return;
                    }
                    requestDatePicker(input);
                });
            }

            input.addEventListener("focus", function () {
                requestDatePicker(input);
            });

            input.addEventListener("change", function () {
                syncDateInputState(input);
            });

            input.addEventListener("input", function () {
                syncDateInputState(input);
            });
        });
    }

    function resolveModal(target) {
        if (!target) {
            return null;
        }

        if (typeof target === "string") {
            return document.getElementById(target);
        }

        return target;
    }

    function setModalState(target, isOpen) {
        const modal = resolveModal(target);
        if (!modal) {
            return;
        }

        const wasOpen = modal.classList.contains("is-open");
        modal.classList.toggle("is-open", isOpen);
        modal.setAttribute("aria-hidden", isOpen ? "false" : "true");

        if (wasOpen === isOpen) {
            return;
        }

        modal.dispatchEvent(new CustomEvent(isOpen ? "app-modal:open" : "app-modal:close", {
            bubbles: true,
            detail: { modalId: modal.id },
        }));
    }

    function closeAllModals() {
        document.querySelectorAll(".app-modal.is-open").forEach(function (modal) {
            setModalState(modal, false);
        });
    }

    function openClickableTarget(href) {
        const targetHref = withActiveSectionContext(href);
        if (!targetHref) {
            return;
        }

        rememberSectionDetailHref(targetHref);
        if (canNavigateWithFetch(targetHref)) {
            navigateWithFetch(targetHref, true);
            return;
        }

        window.location.href = targetHref;
    }

    function handleAppLinkNavigation(event) {
        const target = event.target instanceof Element ? event.target : null;
        const link = target ? target.closest("a[data-app-link], a.calendar-drawer__entry-action--link") : null;
        if (!link || !isPlainLeftClick(event, link)) {
            return false;
        }

        if (navigationState.isNavigating) {
            event.preventDefault();
            return true;
        }

        const targetHref = withActiveSectionContext(link.href, {
            skipBackLink: link.classList.contains("page-hero__back-link"),
        });

        if (isCurrentPageUrl(targetHref)) {
            event.preventDefault();
            return true;
        }

        if (!canNavigateWithFetch(targetHref)) {
            return false;
        }

        event.preventDefault();
        if (link.closest(".app-modal")) {
            closeAllModals();
        }
        navigateWithFetch(targetHref, true);
        return true;
    }

    window.appModal = {
        open: function (target) {
            setModalState(target, true);
        },
        close: function (target) {
            setModalState(target, false);
        },
    };

    initNavigationPrefetch();
    initScrollPerformanceHints(document);
    restorePlanningScrollState();
    initSidebarNavigation();
    initDateFields();
    scheduleIdleNavigationPrefetch();

    document.addEventListener("app:navigation", initDateFields);
    document.addEventListener("app:navigation", scheduleIdleNavigationPrefetch);
    document.addEventListener("app:navigation", restorePlanningScrollState);

    window.addEventListener("pagehide", function () {
        savePlanningScrollState();
        rememberActivePlanningHref(window.location.href);
    });

    document.addEventListener("submit", function (event) {
        const form = event.target instanceof HTMLFormElement ? event.target : null;
        if (!form || !form.dataset.clearSectionMemory) {
            return;
        }

        clearSectionMemory(form.dataset.clearSectionMemory);
    });

    document.addEventListener("submit", function (event) {
        const form = event.target instanceof HTMLFormElement ? event.target : null;
        if (!form || (form.method || "").toLowerCase() !== "post") {
            return;
        }

        savePlanningScrollState();
    });

    document.addEventListener("click", function (event) {
        if (handleAppLinkNavigation(event)) {
            return;
        }

        const openButton = event.target.closest("[data-modal-open]");
        if (openButton) {
            setModalState(openButton.dataset.modalOpen, true);
            return;
        }

        const closeButton = event.target.closest("[data-modal-close]");
        if (closeButton) {
            setModalState(closeButton.closest(".app-modal"), false);
            return;
        }

        const clickableRow = event.target.closest("[data-href]");
        if (!clickableRow) {
            return;
        }

        if (
            navigationState.isNavigating
            || hasTextSelection()
            || event.target.closest("a, button, input, select, textarea, label, form")
        ) {
            return;
        }

        event.preventDefault();
        openClickableTarget(clickableRow.dataset.href);
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            closeAllModals();
        }

        if (event.key !== "Enter" && event.key !== " ") {
            return;
        }

        const clickableRow = event.target.closest("[data-href]");
        if (!clickableRow || navigationState.isNavigating) {
            return;
        }

        event.preventDefault();
        openClickableTarget(clickableRow.dataset.href);
    });
});
