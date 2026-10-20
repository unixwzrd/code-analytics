import { element } from "./dom.js";
import { stateView } from "./primitives.js";
export function chartContainer(options) {
    const root = element("figure", { className: "lui-chart" });
    if (!options.dataset)
        return element("div", { className: "lui-chart" }, stateView({
            kind: "empty",
            title: "No chart data",
            ...(options.description ? { message: options.description } : {}),
        }));
    const { dataset } = options;
    if (!dataset.points.length)
        return element("div", { className: "lui-chart" }, stateView({
            kind: "empty",
            title: "No data",
            message: `${dataset.title} has no points.`,
        }));
    if (dataset.points.some((point) => typeof point.label !== "string" ||
        (point.value !== null && !Number.isFinite(point.value))))
        return element("div", { className: "lui-chart" }, stateView({
            kind: "error",
            title: "Invalid chart data",
            message: "The supplied dataset contains a non-finite value or invalid label.",
        }));
    const valid = dataset.points.filter((point) => point.value !== null);
    if (!valid.length)
        return element("div", { className: "lui-chart" }, stateView({
            kind: "empty",
            title: "No data",
            message: `${dataset.title} has no plottable points.`,
        }));
    const width = 640;
    const height = 240;
    const max = Math.max(...valid.map((point) => Math.abs(point.value)), 1);
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", options.description ?? dataset.title);
    const barWidth = Math.max(12, Math.min(72, (width - 40) / dataset.points.length - 12));
    dataset.points.forEach((point, index) => {
        if (point.value === null)
            return;
        const barHeight = (Math.abs(point.value) / max) * 170;
        const x = 24 + index * ((width - 40) / dataset.points.length);
        const rect = document.createElementNS(svg.namespaceURI, "rect");
        rect.setAttribute("x", String(x));
        rect.setAttribute("y", String(196 - barHeight));
        rect.setAttribute("width", String(barWidth));
        rect.setAttribute("height", String(barHeight));
        rect.setAttribute("class", point.value < 0
            ? "lui-chart__bar lui-chart__bar--negative"
            : "lui-chart__bar");
        const title = document.createElementNS(svg.namespaceURI, "title");
        title.textContent = `${point.label}: ${point.value}${dataset.unit ? ` ${dataset.unit}` : ""}`;
        rect.append(title);
        svg.append(rect);
    });
    root.append(element("figcaption", { text: dataset.title }), svg);
    return root;
}
