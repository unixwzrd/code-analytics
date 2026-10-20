import { element, uniqueId } from "./dom.js";
import { button } from "./primitives.js";
function focusable(root) {
    return Array.from(root.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')).filter((node) => !node.hidden);
}
function openDialog(kind, options, defaultValue = "") {
    const previous = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const titleId = uniqueId("dialog-title");
    const descriptionId = uniqueId("dialog-description");
    const overlay = element("div", { className: "lui-dialog-overlay" });
    const dialog = element("div", {
        className: "lui-dialog",
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": titleId,
        "aria-describedby": options.description ? descriptionId : undefined,
    });
    dialog.append(element("h2", {
        id: titleId,
        className: "lui-dialog__title",
        text: options.title,
    }));
    if (options.description)
        dialog.append(element("p", {
            id: descriptionId,
            className: "lui-dialog__description",
            text: options.description,
        }));
    const input = kind === "prompt"
        ? element("input", {
            className: "lui-dialog__input",
            value: defaultValue,
            "aria-label": options.title,
        })
        : null;
    if (input)
        dialog.append(input);
    const actions = element("div", { className: "lui-dialog__actions" });
    dialog.append(actions);
    overlay.append(dialog);
    document.body.append(overlay);
    return new Promise((resolve) => {
        const close = (value) => {
            overlay.remove();
            previous?.focus();
            resolve(value);
        };
        if (kind !== "alert")
            actions.append(button({
                label: options.cancelLabel ?? "Cancel",
                onClick: () => close(kind === "prompt" ? null : false),
            }));
        const confirm = button({
            label: options.confirmLabel ?? "OK",
            variant: options.destructive ? "danger" : "primary",
            onClick: () => close(kind === "prompt" ? input.value : true),
        });
        actions.append(confirm);
        overlay.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && options.closeOnEscape !== false) {
                event.preventDefault();
                close(kind === "prompt" ? null : false);
                return;
            }
            if (event.key !== "Tab")
                return;
            const candidates = focusable(dialog);
            if (!candidates.length)
                return;
            const first = candidates[0];
            const last = candidates[candidates.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            }
            else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        });
        queueMicrotask(() => (input ?? confirm).focus());
    });
}
export function alertDialog(options) {
    return openDialog("alert", options);
}
export function confirmDialog(options) {
    return openDialog("confirm", options);
}
export function promptDialog(options) {
    return openDialog("prompt", options, options.defaultValue ?? "");
}
