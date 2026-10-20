import { element, uniqueId } from "./dom.js";
import { iconButton } from "./primitives.js";
/** Return the existing shell HTMLElement with an explicit closeNavigation operation.
 * closeNavigation({focusTarget?}) returns true only when expanded state was closed.
 * Consumers invoke it after accepting navigation; no route or callback auto-closes it.
 */
export function operatorShell(options) {
    const navId = uniqueId("navigation");
    const nav = element("aside", { id: navId, className: "lui-shell__sidebar" }, element("nav", { "aria-label": options.navigationLabel ?? "Primary" }, ...options.navigation.map((item) => element("a", {
        className: "lui-shell__nav-link",
        href: item.href ?? `#${item.id}`,
        onclick: item.onSelect
            ? (event) => {
                event.preventDefault();
                item.onSelect?.(item.id);
            }
            : undefined,
        text: item.label,
    }))));
    const toggle = iconButton({
        label: "Toggle navigation",
        icon: "☰",
        onClick: () => {
            const expanded = nav.classList.toggle("lui-shell__sidebar--open");
            toggle.setAttribute("aria-expanded", String(expanded));
        },
    });
    toggle.classList.add("lui-shell__menu");
    toggle.setAttribute("aria-controls", navId);
    toggle.setAttribute("aria-expanded", "false");
    const content = element("main", {
        id: "main-content",
        className: "lui-shell__content",
        tabindex: -1,
        "data-layout-pane": "",
    }, options.content);
    const root = element("div", { className: "lui-shell" }, element("header", { className: "lui-shell__topbar" }, toggle, element("h1", { className: "lui-shell__title", text: options.title }), element("div", { className: "lui-shell__actions" }, ...(options.topBarActions ?? []))), nav, content);
    /** @param {{focusTarget?: HTMLElement}} closeOptions */
    root.closeNavigation = (closeOptions = {}) => {
        const target = closeOptions.focusTarget;
        if (target !== undefined && (!(target instanceof HTMLElement) || !content.contains(target) ||
            !target.isConnected || !target.getClientRects().length || target.matches(":disabled") ||
            target.closest("[hidden], [inert]") || getComputedStyle(target).visibility === "hidden" ||
            (target.tabIndex < 0 && !target.hasAttribute("tabindex"))))
            throw new TypeError("shell focusTarget must be visible and focusable within its content pane");
        if (!nav.classList.contains("lui-shell__sidebar--open")) return false;
        const focusedNavigation = nav.contains(document.activeElement);
        // The toggle is displayed only when the CSS switches to an overlay drawer.
        const narrow = getComputedStyle(toggle).display !== "none";
        nav.classList.remove("lui-shell__sidebar--open");
        toggle.setAttribute("aria-expanded", "false");
        if (narrow && (target || focusedNavigation)) (target ?? content).focus({preventScroll: true});
        return true;
    };
    return root;
}
