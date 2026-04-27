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

    function assetMatches(url, matchers) {
        return matchers.some(function (matcher) {
            return url.indexOf(matcher) !== -1;
        });
    }

    function dispatchNavigationEvent(pathname) {
        document.dispatchEvent(new CustomEvent("app:navigation", {
            detail: { pathname: pathname || window.location.pathname },
        }));
    }

    function ensureDocumentStyles(nextDocument) {
        const nextStyles = Array.from(nextDocument.querySelectorAll("link[rel='stylesheet'][href]"));

        nextStyles.forEach(function (styleNode) {
            const href = styleNode.href;
            if (!href || assetMatches(href, CORE_STYLE_MATCHERS)) {
                return;
            }

            if (!document.querySelector("link[rel='stylesheet'][href='" + href + "']")) {
                const clone = styleNode.cloneNode(true);
                document.head.appendChild(clone);
            }
        });
    }

    async function ensureDocumentScripts(nextDocument) {
        const nextScripts = Array.from(nextDocument.querySelectorAll("script[src]"));

        const pendingScripts = nextScripts
            .map(function (scriptNode) {
                return scriptNode.src;
            })
            .filter(function (src) {
                return src && !assetMatches(src, CORE_SCRIPT_MATCHERS) && !document.querySelector("script[src='" + src + "']");
            })
            .map(function (src) {
                return new Promise(function (resolve, reject) {
                    const script = document.createElement("script");
                    script.src = src;
                    script.defer = true;
                    script.onload = resolve;
                    script.onerror = reject;
                    document.body.appendChild(script);
                });
            });

        if (pendingScripts.length) {
            await Promise.all(pendingScripts);
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

        currentMain.replaceWith(nextMain);
        document.title = nextDocument.title;
        syncBodyClass(nextDocument);
        syncSidebarNavigation(nextDocument);
        return true;
    }

    function getPathFromHref(href) {
        try {
            const url = new URL(href, window.location.href);
            return url.pathname + url.search + url.hash;
        } catch (error) {
            return "";
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

    function canNavigateWithFetch(targetUrl) {
        try {
            const url = new URL(targetUrl, window.location.href);
            const currentPath = window.location.pathname + window.location.search + window.location.hash;
            const targetPath = url.pathname + url.search + url.hash;

            return url.origin === window.location.origin && targetPath !== currentPath;
        } catch (error) {
            return false;
        }
    }

    async function navigateWithFetch(targetUrl, pushState) {
        try {
            const response = await fetch(targetUrl);

            if (!response.ok) {
                throw new Error("Navigation failed");
            }

            const html = await response.text();
            const parser = new DOMParser();
            const nextDocument = parser.parseFromString(html, "text/html");

            ensureDocumentStyles(nextDocument);

            if (!replacePageMain(nextDocument)) {
                throw new Error("Navigation shell mismatch");
            }

            if (pushState) {
                window.history.pushState({}, "", targetUrl);
            }

            window.scrollTo({ top: 0, left: 0, behavior: "auto" });
            await ensureDocumentScripts(nextDocument);
            initSidebarNavigation();
            dispatchNavigationEvent(new URL(targetUrl, window.location.href).pathname);
        } catch (error) {
            window.location.href = targetUrl;
        }
    }

    function initSidebarNavigation() {
        const nav = document.querySelector("[data-sidebar-nav]");
        if (!nav) {
            return;
        }

        const links = Array.from(nav.querySelectorAll("[data-sidebar-link]"));
        let navigationController = window.__sidebarNavigationController;

        if (navigationController) {
            navigationController.abort();
        }

        navigationController = new AbortController();
        window.__sidebarNavigationController = navigationController;
        const signal = navigationController.signal;

        if (!links.length) {
            return;
        }

        function resetNavigationState() {
            nav.classList.remove("is-navigating");
        }

        function scheduleSidebarIndicatorUpdate() {
            window.requestAnimationFrame(function () {
                updateSidebarIndicator(nav);
            });
        }

        function getTargetPath(link) {
            return getPathFromHref(link.href);
        }

        function getCurrentPath() {
            return window.location.pathname + window.location.search + window.location.hash;
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

        function shouldHandleNavigation(event, link) {
            if (!isPlainLeftClick(event, link)) {
                return false;
            }

            try {
                const url = new URL(link.href, window.location.href);
                if (url.origin !== window.location.origin) {
                    return false;
                }

                return getTargetPath(link) !== getCurrentPath();
            } catch (error) {
                return false;
            }
        }

        links.forEach(function (link) {
            if (!link.href) {
                return;
            }

            link.addEventListener("click", function (event) {
                if (nav.classList.contains("is-navigating")) {
                    event.preventDefault();
                    return;
                }

                applyRememberedCalendarHref(link);

                if (!shouldHandleNavigation(event, link)) {
                    return;
                }

                event.preventDefault();
                resetNavigationState();
                nav.classList.add("is-ready", "is-navigating");
                setSidebarActiveLink(nav, link);

                navigateWithFetch(link.href, true);
            }, { signal: signal });
        });

        resetNavigationState();
        updateSidebarIndicator(nav);
        nav.classList.add("is-ready");

        window.addEventListener("pageshow", function () {
            resetNavigationState();
            updateSidebarIndicator(nav);
            nav.classList.add("is-ready");
        }, { signal: signal });

        window.addEventListener("resize", scheduleSidebarIndicatorUpdate, { signal: signal });

        window.addEventListener("popstate", function () {
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

    window.appModal = {
        open: function (target) {
            setModalState(target, true);
        },
        close: function (target) {
            setModalState(target, false);
        },
    };

    initSidebarNavigation();

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
            hasTextSelection()
            || event.target.closest("a, button, input, select, textarea, label, form")
        ) {
            return;
        }

        const href = clickableRow.dataset.href;
        if (!href) {
            return;
        }

        if (canNavigateWithFetch(href)) {
            event.preventDefault();
            navigateWithFetch(href, true);
        } else {
            window.location.href = href;
        }
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            closeAllModals();
        }

        if (event.key !== "Enter" && event.key !== " ") {
            return;
        }

        const clickableRow = event.target.closest("[data-href]");
        if (!clickableRow) {
            return;
        }

        event.preventDefault();
        const href = clickableRow.dataset.href;
        if (!href) {
            return;
        }

        if (canNavigateWithFetch(href)) {
            navigateWithFetch(href, true);
        } else {
            window.location.href = href;
        }
    });
});
