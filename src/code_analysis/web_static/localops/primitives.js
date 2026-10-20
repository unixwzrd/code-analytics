import { element } from "./dom.js";
export function button(options) {
    return element("button", {
        type: "button",
        className: `lui-button lui-button--${options.variant ?? "default"}`,
        text: options.label,
        disabled: options.disabled ?? false,
        title: options.title,
        onclick: options.onClick,
    });
}
export function iconButton(options) {
    const control = button({ ...options, label: "" });
    control.classList.add("lui-icon-button");
    control.setAttribute("aria-label", options.label);
    control.append(options.icon instanceof Node
        ? options.icon
        : element("span", { "aria-hidden": "true", text: options.icon }));
    return control;
}
export function copyControl(options) {
    const normalLabel = options.label ?? "Copy";
    const control = button({ label: normalLabel, variant: "quiet" });
    control.addEventListener("click", async () => {
        const write = options.write ??
            ((value) => navigator.clipboard.writeText(value));
        await write(options.text());
        control.textContent = options.copiedLabel ?? "Copied";
        window.setTimeout(() => (control.textContent = normalLabel), 1200);
    });
    return control;
}
export function statusBadge(label, tone = "neutral") {
    return element("span", {
        className: `lui-badge lui-badge--${tone}`,
        text: label,
    });
}
export function stateView(options) {
    const root = element("section", {
        className: `lui-state lui-state--${options.kind}`,
        role: options.kind === "error" ? "alert" : "status",
    });
    root.append(element("h3", { text: options.title }));
    if (options.message)
        root.append(element("p", { text: options.message }));
    if (options.action)
        root.append(options.action);
    return root;
}
