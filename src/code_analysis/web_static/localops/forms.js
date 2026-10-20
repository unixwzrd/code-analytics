import { element, uniqueId } from "./dom.js";
import { button, statusBadge } from "./primitives.js";
import { assertPathAdapter } from "./contracts.js";
function clamp(value, min, max) {
    let result = Number.isFinite(value) ? value : (min ?? 0);
    if (min !== undefined)
        result = Math.max(min, result);
    if (max !== undefined)
        result = Math.min(max, result);
    return result;
}
export function numberField(options) {
    const id = uniqueId("number");
    const input = element("input", {
        id,
        type: "number",
        value: options.value ?? 0,
        min: options.min,
        max: options.max,
        step: options.step ?? 1,
    });
    const normalize = () => {
        input.value = String(clamp(input.valueAsNumber, options.min, options.max));
        options.onChange?.(input.valueAsNumber);
    };
    input.addEventListener("change", normalize);
    input.addEventListener("blur", normalize);
    const root = element("label", { className: "lui-field", htmlFor: id }, element("span", { className: "lui-field__label", text: options.label }), element("span", { className: "lui-number" }, input, options.suffix
        ? element("span", {
            className: "lui-number__suffix",
            text: options.suffix,
        })
        : null));
    return {
        root,
        input,
        value: () => input.valueAsNumber,
        setValue: (value) => (input.value = String(clamp(value, options.min, options.max))),
    };
}
export function sliderField(options) {
    const numeric = numberField(options);
    const slider = element("input", {
        type: "range",
        min: options.min,
        max: options.max,
        step: options.step ?? 1,
        value: options.value ?? options.min,
        "aria-label": `${options.label} slider`,
    });
    slider.addEventListener("input", () => {
        numeric.setValue(slider.valueAsNumber);
        options.onChange?.(slider.valueAsNumber);
    });
    numeric.input.addEventListener("input", () => {
        if (Number.isFinite(numeric.input.valueAsNumber))
            slider.value = String(clamp(numeric.input.valueAsNumber, options.min, options.max));
    });
    numeric.root.classList.add("lui-slider-field");
    numeric.root.querySelector(".lui-number")?.before(slider);
    return { ...numeric, slider };
}
export function pathField(options) {
    assertPathAdapter(options.adapter);
    const kind = options.kind ?? "any";
    const id = uniqueId("path");
    const input = element("input", {
        id,
        type: "text",
        value: options.value ?? "",
        autocomplete: "off",
        spellcheck: false,
    });
    const detail = element("div", {
        className: "lui-path__detail",
        "aria-live": "polite",
    });
    const actions = element("div", { className: "lui-path__actions" });
    let request = 0;
    const resolve = async () => {
        const current = ++request;
        detail.replaceChildren(statusBadge("Resolving", "info"));
        try {
            const resolution = await options.adapter.resolve(input.value, kind);
            if (current !== request)
                return undefined;
            detail.replaceChildren(statusBadge(resolution.valid
                ? resolution.exists
                    ? "Available"
                    : "Valid"
                : "Invalid", resolution.valid ? "success" : "danger"), element("span", {
                className: "lui-path__resolved",
                text: resolution.resolved,
            }));
            options.onChange?.(input.value, resolution);
            return resolution;
        }
        catch (error) {
            if (current !== request)
                return undefined;
            detail.replaceChildren(statusBadge("Error", "danger"), element("span", { text: String(error) }));
            return undefined;
        }
    };
    input.addEventListener("change", () => void resolve());
    actions.append(button({ label: "Resolve", onClick: () => void resolve() }));
    if (options.adapter.browse)
        actions.append(button({
            label: "Browse",
            onClick: async () => {
                const listing = await options.adapter.browse?.(input.value, kind);
                if (listing) {
                    input.value = listing.path;
                    await resolve();
                }
            },
        }));
    if (options.adapter.create && kind !== "any")
        actions.append(button({
            label: "Create",
            onClick: async () => {
                const resolution = await options.adapter.create?.(input.value, kind);
                if (resolution) {
                    detail.replaceChildren(statusBadge(resolution.valid ? "Created" : "Invalid", resolution.valid ? "success" : "danger"), element("span", { text: resolution.resolved }));
                    options.onChange?.(input.value, resolution);
                }
            },
        }));
    const root = element("div", { className: "lui-field lui-path" }, element("label", {
        className: "lui-field__label",
        htmlFor: id,
        text: options.label,
    }), element("div", { className: "lui-path__row" }, input, actions), detail);
    return { root, input, resolve };
}
