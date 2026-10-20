import { element, uniqueId } from "./dom.js";
export function tabs(options) {
    if (!options.tabs.length)
        throw new Error("tabs requires at least one tab");
    const base = uniqueId("tabs");
    const root = element("div", { className: "lui-tabs" });
    const tablist = element("div", {
        className: "lui-tabs__list",
        role: "tablist",
        "aria-label": options.ariaLabel ?? "Sections",
    });
    const controls = new Map();
    const panels = new Map();
    let selected = options.initialId &&
        options.tabs.some((item) => item.id === options.initialId)
        ? options.initialId
        : options.tabs[0].id;
    const select = (id) => {
        if (!controls.has(id))
            return;
        selected = id;
        for (const [tabId, control] of controls) {
            const active = tabId === id;
            control.setAttribute("aria-selected", String(active));
            control.tabIndex = active ? 0 : -1;
            panels.get(tabId).hidden = !active;
        }
        options.onChange?.(id);
    };
    options.tabs.forEach((definition, index) => {
        const tabId = `${base}-tab-${index}`;
        const panelId = `${base}-panel-${index}`;
        const control = element("button", {
            id: tabId,
            type: "button",
            role: "tab",
            className: "lui-tabs__tab",
            "aria-controls": panelId,
            "aria-selected": "false",
            tabindex: -1,
            text: definition.label,
        });
        const panel = element("section", {
            id: panelId,
            role: "tabpanel",
            className: "lui-tabs__panel",
            "aria-labelledby": tabId,
            tabindex: 0,
        }, typeof definition.content === "function"
            ? definition.content()
            : definition.content);
        control.addEventListener("click", () => select(definition.id));
        control.addEventListener("keydown", (event) => {
            const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
            if (!keys.includes(event.key))
                return;
            event.preventDefault();
            const current = options.tabs.findIndex((item) => item.id === selected);
            const next = event.key === "Home"
                ? 0
                : event.key === "End"
                    ? options.tabs.length - 1
                    : (current +
                        (event.key === "ArrowRight" ? 1 : -1) +
                        options.tabs.length) %
                        options.tabs.length;
            const nextId = options.tabs[next].id;
            select(nextId);
            controls.get(nextId)?.focus();
        });
        controls.set(definition.id, control);
        panels.set(definition.id, panel);
        tablist.append(control);
        root.append(panel);
    });
    root.prepend(tablist);
    select(selected);
    return { root, select, selectedId: () => selected };
}
export function disclosure(options) {
    return element("details", { className: "lui-disclosure", open: options.open ?? false }, element("summary", { text: options.title }), element("div", { className: "lui-disclosure__body" }, options.content));
}
