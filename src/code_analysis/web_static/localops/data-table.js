import { element } from "./dom.js";
import { button } from "./primitives.js";
import { assertPreferenceStore } from "./contracts.js";
export class DataTable {
    options;
    root;
    table;
    body;
    columns;
    rows;
    layout;
    sort;
    status;
    constructor(options) {
        this.options = options;
        if (options.preferenceStore)
            assertPreferenceStore(options.preferenceStore);
        if (!Array.isArray(options.columns) || !options.columns.length)
            throw new TypeError("DataTable.columns must be a non-empty array");
        if (new Set(options.columns.map((column) => column.id)).size !== options.columns.length)
            throw new TypeError("DataTable column IDs must be unique");
        if (options.clientSort && !options.sortLocale)
            throw new TypeError("DataTable.clientSort requires sortLocale");
        if (options.clientSort && options.onSort)
            throw new TypeError("DataTable.clientSort and onSort are mutually exclusive");
        this.columns = new Map(options.columns.map((column) => [column.id, column]));
        this.rows = [];
        this.layout = {
            order: options.columns.map((column) => column.id),
            visible: options.columns
                .filter((column) => column.visible !== false)
                .map((column) => column.id),
            widths: Object.fromEntries(options.columns
                .filter((column) => column.width)
                .map((column) => [column.id, column.width])),
        };
        this.table = element("table", { className: "lui-data-table" });
        this.table.append(element("caption", {
            className: "lui-visually-hidden",
            text: options.caption,
        }));
        this.body = element("tbody");
        this.table.append(this.body);
        const scroll = element("div", {
            className: "lui-data-table__scroll",
            tabindex: 0,
            role: "region",
            "aria-label": `${options.caption} table`,
        }, this.table);
        const columnsPanel = element("details", { className: "lui-data-table__columns" }, element("summary", { text: "Columns" }));
        this.status = element("p", { className: "lui-data-table__status", "aria-live": "polite", hidden: true });
        this.root = element("section", { className: "lui-data-table-wrap" }, element("div", { className: "lui-data-table__toolbar" }, columnsPanel), this.status, scroll);
        this.setRows(options.rows ?? []);
        void this.initialize();
    }
    setRows(rows) {
        const error = this.validateRows(rows);
        if (error) {
            this.rows = [];
            this.setStatus(error, true);
        }
        else {
            this.rows = [...rows];
            this.setStatus();
        }
        this.renderBody();
    }
    validateRows(rows) {
        if (!Array.isArray(rows)) return "Invalid table data: rows must be an array.";
        const keys = new Set();
        for (const row of rows) {
            if (!row || typeof row !== "object" || Array.isArray(row)) return "Invalid table data: every row must be an object.";
            const key = String(this.options.rowKey(row) ?? "");
            if (!key || keys.has(key)) return "Invalid table data: row keys must be unique and non-empty.";
            keys.add(key);
        }
        return null;
    }
    setStatus(message, error = false) {
        this.status.hidden = !message;
        this.status.textContent = message ?? "";
        this.status.setAttribute("role", error ? "alert" : "status");
    }
    getLayout() {
        return {
            order: [...this.layout.order],
            visible: [...this.layout.visible],
            widths: { ...this.layout.widths },
        };
    }
    async loadLayout() {
        if (!this.options.preferenceStore || !this.options.preferenceKey)
            return;
        const saved = await this.options.preferenceStore.load(this.options.preferenceKey);
        if (!saved)
            return;
        if (!saved || typeof saved !== "object" || Array.isArray(saved) ||
            Object.keys(saved).some((key) => !["order", "visible", "widths"].includes(key)) ||
            !Array.isArray(saved.order) || !Array.isArray(saved.visible) ||
            !saved.widths || typeof saved.widths !== "object" || Array.isArray(saved.widths) ||
            saved.order.some((id) => typeof id !== "string") || saved.visible.some((id) => typeof id !== "string") ||
            new Set(saved.order).size !== saved.order.length || new Set(saved.visible).size !== saved.visible.length ||
            Object.values(saved.widths).some((width) => !Number.isFinite(width) || width < 80))
            throw new TypeError("saved table layout has an invalid shape");
        const known = new Set(this.columns.keys());
        const savedIds = [...saved.order, ...saved.visible, ...Object.keys(saved.widths)];
        if (savedIds.some((id) => !known.has(id)))
            throw new TypeError("saved table layout references retired columns");
        if (saved.visible.length === 0)
            throw new TypeError("saved table layout must keep at least one column visible");
        const order = saved.order.filter((id) => known.has(id));
        for (const id of known)
            if (!order.includes(id))
                order.push(id);
        this.layout = {
            order,
            visible: saved.visible.filter((id) => known.has(id)),
            widths: Object.fromEntries(Object.entries(saved.widths).filter(([id]) => known.has(id))),
        };
    }
    persist() {
        if (this.options.preferenceStore && this.options.preferenceKey)
            void Promise.resolve(this.options.preferenceStore.save(this.options.preferenceKey, this.getLayout()))
                .catch(() => this.setStatus("Table preferences could not be saved.", true));
    }
    async initialize() {
        try {
            await this.loadLayout();
        }
        catch {
            this.setStatus("Saved table preferences were invalid and were ignored.", true);
        }
        this.render();
    }
    visibleColumns() {
        return this.layout.order
            .filter((id) => this.layout.visible.includes(id))
            .map((id) => this.columns.get(id))
            .filter((column) => Boolean(column));
    }
    render() {
        this.renderColumnChooser();
        this.renderHead();
        this.renderBody();
    }
    renderColumnChooser() {
        const panel = this.root.querySelector(".lui-data-table__columns");
        panel
            .querySelectorAll("label, .lui-data-table__order")
            .forEach((node) => node.remove());
        for (const id of this.layout.order) {
            const column = this.columns.get(id);
            const checkbox = element("input", {
                type: "checkbox",
                checked: this.layout.visible.includes(id),
            });
            checkbox.addEventListener("change", () => {
                this.layout.visible = checkbox.checked
                    ? [...this.layout.visible, id]
                    : this.layout.visible.filter((candidate) => candidate !== id);
                this.persist();
                this.renderHead();
                this.renderBody();
            });
            const order = element("span", { className: "lui-data-table__order" }, button({
                label: "↑",
                title: `Move ${column.label} left`,
                onClick: () => this.move(id, -1),
            }), button({
                label: "↓",
                title: `Move ${column.label} right`,
                onClick: () => this.move(id, 1),
            }));
            panel.append(element("label", {}, checkbox, element("span", { text: column.label }), order));
        }
    }
    move(id, delta) {
        const from = this.layout.order.indexOf(id);
        const to = Math.max(0, Math.min(this.layout.order.length - 1, from + delta));
        if (from === to)
            return;
        this.layout.order.splice(from, 1);
        this.layout.order.splice(to, 0, id);
        this.persist();
        this.render();
    }
    renderHead() {
        this.table.tHead?.remove();
        const row = element("tr");
        for (const column of this.visibleColumns()) {
            const header = element("th", {
                scope: "col",
                draggable: true,
                "data-column-id": column.id,
                style: this.layout.widths[column.id]
                    ? `width:${this.layout.widths[column.id]}px`
                    : undefined,
            });
            const sortButton = element("button", {
                type: "button",
                className: "lui-data-table__sort",
                text: column.label,
                disabled: column.sortable !== true || (!this.options.clientSort && !this.options.onSort),
                "aria-label": `Sort by ${column.label}`,
            });
            if (this.sort?.id === column.id)
                header.setAttribute("aria-sort", this.sort.direction === 1 ? "ascending" : "descending");
            else
                header.setAttribute("aria-sort", "none");
            sortButton.addEventListener("click", () => {
                this.sort = {
                    id: column.id,
                    direction: this.sort?.id === column.id && this.sort.direction === 1 ? -1 : 1,
                };
                this.renderHead();
                if (this.options.onSort)
                    void Promise.resolve(this.options.onSort({ columnId: column.id, direction: this.sort.direction === 1 ? "ascending" : "descending" }))
                        .catch(() => this.setStatus("The requested sort could not be applied.", true));
                else
                    this.renderBody();
            });
            const resizer = element("span", {
                className: "lui-data-table__resizer",
                role: "separator",
                tabindex: 0,
                "aria-label": `Resize ${column.label}`,
                "aria-orientation": "vertical",
            });
            const resize = (delta) => {
                this.layout.widths[column.id] = Math.max(80, (this.layout.widths[column.id] ??
                    header.getBoundingClientRect().width) + delta);
                this.persist();
                this.renderHead();
            };
            resizer.addEventListener("keydown", (event) => {
                if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                    event.preventDefault();
                    resize(event.key === "ArrowLeft" ? -10 : 10);
                }
            });
            resizer.addEventListener("pointerdown", (event) => {
                const start = event.clientX;
                const width = header.getBoundingClientRect().width;
                resizer.setPointerCapture(event.pointerId);
                const move = (moveEvent) => {
                    this.layout.widths[column.id] = Math.max(80, width + moveEvent.clientX - start);
                    header.style.width = `${this.layout.widths[column.id]}px`;
                };
                const up = () => {
                    resizer.removeEventListener("pointermove", move);
                    this.persist();
                };
                resizer.addEventListener("pointermove", move);
                resizer.addEventListener("pointerup", up, { once: true });
            });
            header.addEventListener("dragstart", (event) => event.dataTransfer?.setData("text/plain", column.id));
            header.addEventListener("dragover", (event) => event.preventDefault());
            header.addEventListener("drop", (event) => {
                event.preventDefault();
                const source = event.dataTransfer?.getData("text/plain");
                if (!source || source === column.id)
                    return;
                const from = this.layout.order.indexOf(source);
                const to = this.layout.order.indexOf(column.id);
                this.layout.order.splice(from, 1);
                this.layout.order.splice(to, 0, source);
                this.persist();
                this.render();
            });
            header.append(sortButton, resizer);
            row.append(header);
        }
        this.table.prepend(element("thead", {}, row));
    }
    renderBody() {
        this.body.replaceChildren();
        const columns = this.visibleColumns();
        const rows = [...this.rows];
        if (this.sort && this.options.clientSort) {
            const column = this.columns.get(this.sort.id);
            if (column)
                rows.sort((left, right) => new Intl.Collator(this.options.sortLocale, { numeric: true }).compare(String(column.value(left) ?? ""), String(column.value(right) ?? "")) * this.sort.direction);
        }
        if (!rows.length) {
            this.body.append(element("tr", {}, element("td", {
                colspan: Math.max(columns.length, 1),
                text: this.options.emptyMessage ?? "No data.",
            })));
            return;
        }
        for (const row of rows) {
            const tr = element("tr", {
                dataset: { rowKey: this.options.rowKey(row) },
            });
            for (const column of columns) {
                const value = column.value(row);
                const rendered = column.render?.(value, row) ?? String(value ?? "");
                tr.append(element("td", {
                    className: column.align ? `lui-align-${column.align}` : undefined,
                }, rendered));
            }
            this.body.append(tr);
        }
    }
}
