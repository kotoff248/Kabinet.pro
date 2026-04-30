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
        "css/reset.css",
        "css/base/foundation.css",
        "css/layout/app-shell.css",
        "css/components/page-hero.css",
        "css/components/modals.css",
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
    const CORE_SCRIPT_MATCHERS = ["js/base.js"];
    const PAGE_STATE_CLASSES = ["is-calendar-page", "is-calendar-sizing"];
    const PAGE_TRANSITION_CLASS = "is-page-transitioning";
    const CALENDAR_ROOT_SELECTOR = "#calendar-filters-form";
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
            detailPattern: /^\/applications\/\d+\/$/,
        },
        employees: {
            storageKey: "employees:last-detail-href",
            listStorageKey: "employees:last-list-href",
            listPath: "/employees/",
            detailPattern: /^\/employee\/\d+\/$/,
        },
        notifications: {
            listStorageKey: "notifications:last-list-href",
            listPath: "/notifications/",
        },
    };
    const SESSION_MEMORY_KEYS = [
        "applications:list-scroll-state",
        "employees:list-scroll-state",
        "calendar:path",
        "calendar:last-url",
        "calendar:board-scroll-state",
    ];
    const SESSION_MEMORY_PREFIXES = [
        "profile-sections:",
    ];

    const navigationState = {
        isNavigating: false,
        targetHref: null,
        pendingPopstateHref: null,
    };

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

    function isSectionDetailUrl(url, sectionKey) {
        const section = SECTION_MEMORY[sectionKey];
        return Boolean(section && url && section.detailPattern && section.detailPattern.test(url.pathname));
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
            if (!section.detailPattern || !section.detailPattern.test(url.pathname)) {
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
        return sectionKey && SECTION_MEMORY[sectionKey] ? sectionKey : "";
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
        root.querySelectorAll("[data-profile-overview-scroll], [data-profile-requests-scroll], [data-entitlement-scroll]").forEach(function (scrollRoot) {
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

        event.preventDefault();
        clearSectionMemory(sectionKey);
        clearSectionListMemory(sectionKey);
        const wasHandledLocally = handleLocalSectionRepeat(sectionKey);

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

        if (wasHandled || wasHandledLocally) {
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

    function syncDocumentStyles(nextDocument) {
        const nextStyleHrefs = getStylesheetHrefs(nextDocument);
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

        nextStyleHrefs.forEach(function (href) {
            if (findCurrentStylesheet(href)) {
                return;
            }

            const nextStyle = Array.from(nextDocument.querySelectorAll("link[rel='stylesheet'][href]")).find(function (styleNode) {
                return styleNode.href === href;
            });
            if (nextStyle) {
                document.head.appendChild(nextStyle.cloneNode(true));
            }
        });
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
            document.documentElement.classList.add("is-calendar-page", "is-calendar-sizing");
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

        try {
            const rememberedPath = sessionStorage.getItem("calendar:path");
            const rememberedUrl = sessionStorage.getItem("calendar:last-url");
            if (!rememberedPath || !rememberedUrl) {
                return;
            }

            const linkUrl = new URL(link.href, window.location.href);
            const restoredUrl = new URL(rememberedUrl, window.location.href);

            if (
                linkUrl.origin === window.location.origin
                && restoredUrl.origin === window.location.origin
                && linkUrl.pathname === rememberedPath
                && restoredUrl.pathname === rememberedPath
            ) {
                link.href = restoredUrl.href;
            }
        } catch (error) {
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
        updateSidebarIndicator(currentNav);
    }

    function syncSidebarRememberedHrefs(nav) {
        if (!nav) {
            return;
        }

        applyRememberedCalendarHref(nav.querySelector('[data-sidebar-key="calendar"]'));
        applyRememberedSectionHref(nav.querySelector('[data-sidebar-key="applications"]'), "applications");
        applyRememberedSectionHref(nav.querySelector('[data-sidebar-key="employees"]'), "employees");
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
        window.requestAnimationFrame(function () {
            updateSidebarIndicator(document.querySelector("[data-sidebar-nav]"));
        });
    }

    async function navigateWithFetch(targetUrl, pushState) {
        const target = new URL(targetUrl, window.location.href);
        const targetHref = target.href;

        if (navigationState.isNavigating) {
            return;
        }

        rememberSectionDetailHref(targetHref);
        setNavigationBusy(true, targetHref);

        try {
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

            const finalUrl = toSameOriginUrl(response.url) || target;
            const finalHref = finalUrl.href;
            const html = await response.text();
            const nextDocument = new DOMParser().parseFromString(html, "text/html");

            syncDocumentStyles(nextDocument);
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
        syncSidebarRememberedHrefs(nav);

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
        clearSectionMemory: clearSectionMemory,
        clearSectionListMemory: clearSectionListMemory,
        getSectionListHref: getSectionListHref,
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
        if (!input || input.disabled || typeof input.showPicker !== "function") {
            return;
        }

        try {
            input.showPicker();
        } catch (error) {
        }
    }

    function syncDateInputState(input) {
        if (!input || input.type !== "date") {
            return;
        }

        input.classList.toggle("is-empty", !input.value);
    }

    function initDateFields() {
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
        if (!href) {
            return;
        }

        rememberSectionDetailHref(href);
        if (canNavigateWithFetch(href)) {
            navigateWithFetch(href, true);
            return;
        }

        window.location.href = href;
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

        if (isCurrentPageUrl(link.href)) {
            event.preventDefault();
            return true;
        }

        if (!canNavigateWithFetch(link.href)) {
            return false;
        }

        event.preventDefault();
        if (link.closest(".app-modal")) {
            closeAllModals();
        }
        navigateWithFetch(link.href, true);
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

    initSidebarNavigation();
    initDateFields();

    document.addEventListener("app:navigation", initDateFields);

    document.addEventListener("submit", function (event) {
        const form = event.target instanceof HTMLFormElement ? event.target : null;
        if (!form || !form.dataset.clearSectionMemory) {
            return;
        }

        clearSectionMemory(form.dataset.clearSectionMemory);
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
