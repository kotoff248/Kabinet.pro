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
    const CORE_STYLE_MATCHERS = ["css/reset.css", "css/main.css"];
    const CORE_SCRIPT_MATCHERS = ["js/base.js"];
    const PAGE_STATE_CLASSES = ["is-calendar-page", "is-calendar-sizing"];
    const CALENDAR_ROOT_SELECTOR = "#calendar-filters-form";

    const navigationState = {
        isNavigating: false,
        targetHref: null,
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

    function dispatchNavigationEvent(url) {
        const nextUrl = url || new URL(window.location.href);
        document.dispatchEvent(new CustomEvent("app:navigation", {
            detail: {
                pathname: nextUrl.pathname,
                url: nextUrl.href,
            },
        }));
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

        applyRememberedCalendarHref(currentNav.querySelector('[data-sidebar-key="calendar"]'));
        updateSidebarIndicator(currentNav);
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

        setNavigationBusy(true, targetHref);

        try {
            const response = await fetch(targetHref);
            if (!response.ok) {
                throw new Error("Navigation failed");
            }

            const html = await response.text();
            const nextDocument = new DOMParser().parseFromString(html, "text/html");

            syncDocumentStyles(nextDocument);
            if (!replacePageMain(nextDocument)) {
                throw new Error("Navigation shell mismatch");
            }

            if (pushState) {
                window.history.pushState({}, "", targetHref);
            }

            window.scrollTo({ top: 0, left: 0, behavior: "auto" });
            await syncDocumentScripts(nextDocument);
            initSidebarNavigation();
            initDateFields();
            setNavigationBusy(false, null);
            dispatchNavigationEvent(target);
        } catch (error) {
            window.location.href = targetHref;
        }
    }

    function initSidebarNavigation() {
        const nav = document.querySelector("[data-sidebar-nav]");
        if (!nav) {
            return;
        }

        const links = Array.from(nav.querySelectorAll("[data-sidebar-link]"));
        const shouldPrimeIndicator = nav.dataset.sidebarInitialized !== "true";
        const previousController = window.__sidebarNavigationController;
        if (previousController) {
            previousController.abort();
        }

        const controller = new AbortController();
        const signal = controller.signal;
        window.__sidebarNavigationController = controller;

        links.forEach(function (link) {
            if (!link.href) {
                return;
            }

            link.addEventListener("click", function (event) {
                applyRememberedCalendarHref(link);

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
            if (!canNavigateWithFetch(window.location.href)) {
                return;
            }
            navigateWithFetch(window.location.href, false);
        }, { signal: signal });
    }

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

        if (canNavigateWithFetch(href)) {
            navigateWithFetch(href, true);
            return;
        }

        window.location.href = href;
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

    document.addEventListener("click", function (event) {
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
