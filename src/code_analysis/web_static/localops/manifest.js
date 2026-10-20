import { chartContainer } from "./chart.js";
import { DataTable, } from "./data-table.js";
import { alertDialog, confirmDialog, promptDialog } from "./dialogs.js";
import { element } from "./dom.js";
import { numberField, pathField, sliderField } from "./forms.js";
import { disclosure, tabs } from "./navigation.js";
import { button, copyControl, iconButton, stateView, statusBadge, } from "./primitives.js";
import { operatorShell } from "./shell.js";
import { TaskMonitor } from "./task-monitor.js";
import { assertManifestTransport } from "./contracts.js";
import { validateResponse, validateSnapshot, validateUpdate } from "./manifest-validation.js";
function isRecord(value) {
    return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
export class ManifestRuntime {
    root;
    transport;
    snapshot;
    subscription;
    eventSequence = 0;
    cleanups = [];
    constructor(root, transport) {
        this.root = root;
        this.transport = assertManifestTransport(transport);
    }
    async start() {
        this.snapshot = structuredClone(validateSnapshot(await this.transport.load()));
        this.render();
        this.subscription = this.transport.subscribe?.((update) => this.apply(update), (error) => this.showError(error));
    }
    stop() {
        this.subscription?.unsubscribe();
        this.cleanups.splice(0).forEach((cleanup) => cleanup());
    }
    apply(update) {
        update = structuredClone(validateUpdate(update));
        if ("spec" in update) {
            if (this.snapshot && update.revision <= this.snapshot.revision)
                throw new Error("stale or out-of-order state snapshot");
            this.snapshot = update;
        }
        else {
            if (!this.snapshot || update.base_revision !== this.snapshot.revision || update.revision <= update.base_revision)
                throw new Error("stale or out-of-order state patch");
            Object.assign(this.snapshot.state, update.changes);
            for (const key of update.removed ?? [])
                delete this.snapshot.state[key];
            this.snapshot.revision = update.revision;
        }
        this.render();
    }
    async event(componentId, kind, value, commandId, stateKey) {
        if (!this.snapshot)
            throw new Error("manifest runtime has not started");
        const event = {
            schema_version: "1",
            event_id: `web-${++this.eventSequence}`,
            component_id: componentId,
            kind,
            revision: this.snapshot.revision,
            ...(value !== undefined ? { value } : {}),
            ...(commandId ? { command_id: commandId } : {}),
            ...(stateKey ? { state_key: stateKey } : {}),
        };
        const response = validateResponse(await this.transport.send(event));
        this.apply(response.update);
        return response.result ?? null;
    }
    render() {
        if (!this.snapshot)
            return;
        this.cleanups.splice(0).forEach((cleanup) => cleanup());
        const pageId = this.snapshot.spec.initial_page_id ?? this.snapshot.spec.pages[0]?.id;
        const page = this.snapshot.spec.pages.find((candidate) => candidate.id === pageId);
        if (!page)
            throw new Error("manifest has no renderable page");
        const content = this.renderNode(page.body);
        const shell = operatorShell({
            title: page.title,
            navigation: (page.navigation ?? []).map((item) => ({
                id: item.id,
                label: item.label,
                ...(item.command_id
                    ? {
                        onSelect: () => void this.event(item.id, "command", undefined, item.command_id),
                    }
                    : {}),
            })),
            content,
        });
        this.root.replaceChildren(shell);
    }
    renderNode(node) {
        const state = this.snapshot.state;
        switch (node.type) {
            case "text":
                return element(node.style === "heading" ? "h2" : node.style === "code" ? "pre" : "p", {
                    className: `lui-manifest-text lui-manifest-text--${node.style ?? "body"}`,
                    text: node.text,
                });
            case "status_badge":
                return statusBadge(node.label, node.tone);
            case "state_view":
                return stateView({
                    kind: node.kind,
                    title: node.title,
                    ...(node.message ? { message: node.message } : {}),
                    ...(node.action_id && node.action_label
                        ? {
                            action: button({
                                label: node.action_label,
                                onClick: () => void this.event(node.id, "command", undefined, node.action_id),
                            }),
                        }
                        : {}),
                });
            case "button":
                return node.icon
                    ? iconButton({
                        label: node.label,
                        icon: node.icon,
                        variant: node.variant ?? "default",
                        disabled: node.disabled ?? false,
                        onClick: () => void this.event(node.id, "command", undefined, node.command_id),
                    })
                    : button({
                        label: node.label,
                        variant: node.variant ?? "default",
                        disabled: node.disabled ?? false,
                        onClick: () => void this.event(node.id, "command", undefined, node.command_id),
                    });
            case "dialog":
                return button({
                    label: node.trigger_label,
                    variant: node.destructive ? "danger" : "default",
                    onClick: async () => {
                        const options = {
                            title: node.title,
                            ...(node.description ? { description: node.description } : {}),
                            destructive: node.destructive ?? false,
                        };
                        const result = node.kind === "alert"
                            ? await alertDialog(options)
                            : node.kind === "confirm"
                                ? await confirmDialog(options)
                                : await promptDialog({
                                    ...options,
                                    defaultValue: String(state[node.default_value_key ?? ""] ?? ""),
                                });
                        await this.event(node.id, "dialog_result", result, node.command_id, node.result_key);
                    },
                });
            case "copy_control":
                return copyControl({
                    label: node.label ?? "Copy",
                    text: () => String(state[node.value_key] ?? ""),
                });
            case "number_field": {
                const control = numberField({
                    label: node.label,
                    value: Number(state[node.value_key] ?? node.minimum ?? 0),
                    ...(node.minimum !== undefined ? { min: node.minimum } : {}),
                    ...(node.maximum !== undefined ? { max: node.maximum } : {}),
                    ...(node.step !== undefined ? { step: node.step } : {}),
                    ...(node.suffix !== undefined ? { suffix: node.suffix } : {}),
                    onChange: (value) => void this.event(node.id, "value_changed", value, undefined, node.value_key),
                });
                return control.root;
            }
            case "slider_field": {
                const control = sliderField({
                    label: node.label,
                    value: Number(state[node.value_key] ?? node.minimum ?? 0),
                    min: node.minimum ?? 0,
                    max: node.maximum ?? 100,
                    ...(node.step !== undefined ? { step: node.step } : {}),
                    ...(node.suffix !== undefined ? { suffix: node.suffix } : {}),
                    onChange: (value) => void this.event(node.id, "value_changed", value, undefined, node.value_key),
                });
                return control.root;
            }
            case "path_field": {
                const adapter = {
                    resolve: async (path, kind) => (await this.event(node.id, "command", { path, kind }, `${node.adapter_id}:resolve`)),
                    ...(node.allow_browse
                        ? {
                            browse: async (path, kind) => (await this.event(node.id, "command", { path, kind }, `${node.adapter_id}:browse`)),
                        }
                        : {}),
                    ...(node.allow_create && node.kind !== "any"
                        ? {
                            create: async (path, kind) => (await this.event(node.id, "command", { path, kind }, `${node.adapter_id}:create`)),
                        }
                        : {}),
                };
                return pathField({
                    label: node.label,
                    kind: node.kind ?? "any",
                    value: String(state[node.value_key] ?? ""),
                    adapter,
                }).root;
            }
            case "data_table": {
                const columns = node.columns.map((column) => ({
                    id: column.id,
                    label: column.label,
                    value: (row) => row[column.value_key],
                    sortable: column.sortable ?? false,
                    visible: column.visible ?? true,
                    ...(column.width !== undefined ? { width: column.width } : {}),
                    align: column.align ?? "start",
                }));
                const rows = state[node.rows_key];
                if (!Array.isArray(rows) || rows.some((row) => !isRecord(row) || !String(row[node.row_key] ?? "")))
                    return stateView({ kind: "error", title: "Invalid table data", message: "Rows must be objects with unique, non-empty row keys." });
                const rowKeys = rows.map((row) => String(row[node.row_key]));
                if (new Set(rowKeys).size !== rowKeys.length)
                    return stateView({ kind: "error", title: "Invalid table data", message: "Rows must be objects with unique, non-empty row keys." });
                return new DataTable({
                    caption: node.caption,
                    columns,
                    rows,
                    rowKey: (row) => String(row[node.row_key] ?? ""),
                    emptyMessage: node.empty_message ?? "No rows",
                    clientSort: node.client_sort ?? false,
                    ...(node.sort_locale ? { sortLocale: node.sort_locale } : {}),
                    ...(node.sort_command_id
                        ? { onSort: ({ columnId, direction }) => this.event(node.id, "layout_changed", { column_id: columnId, direction }, node.sort_command_id) }
                        : {}),
                    ...(node.preference_key
                        ? {
                            preferenceKey: node.preference_key,
                            preferenceStore: {
                                load: async (key) => (await this.event(node.id, "layout_changed", key, "preferences:load")),
                                save: async (key, value) => {
                                    await this.event(node.id, "layout_changed", { key, value: value }, "preferences:save");
                                },
                            },
                        }
                        : {}),
                }).root;
            }
            case "task_monitor": {
                const current = () => (state[node.id] ?? {
                    id: node.id,
                    phase: "idle",
                    label: "Task",
                });
                const monitor = new TaskMonitor({
                    pollIntervalMs: node.poll_interval_ms ?? 2000,
                    adapter: {
                        getSnapshot: async () => current(),
                        ...(node.cancel_command_id
                            ? {
                                cancel: async () => {
                                    await this.event(node.id, "command", undefined, node.cancel_command_id);
                                },
                            }
                            : {}),
                        ...(node.retry_command_id
                            ? {
                                retry: async () => {
                                    await this.event(node.id, "command", undefined, node.retry_command_id);
                                },
                            }
                            : {}),
                    },
                });
                void monitor.start();
                this.cleanups.push(() => monitor.stop());
                return monitor.root;
            }
            case "chart": {
                const dataset = state[node.dataset_key];
                return chartContainer({
                    dataset: isRecord(dataset)
                        ? dataset
                        : null,
                    ...(node.description ? { description: node.description } : {}),
                });
            }
            case "tabs": {
                let ready = false;
                const control = tabs({
                    ariaLabel: node.aria_label ?? "Sections",
                    ...(node.selected_key
                        ? { initialId: String(state[node.selected_key] ?? "") }
                        : {}),
                    tabs: node.tabs.map((tab) => ({
                        id: tab.id,
                        label: tab.label,
                        content: element("div", {}, ...tab.children.map((child) => this.renderNode(child))),
                    })),
                    onChange: (value) => {
                        if (ready)
                            void this.event(node.id, "selection_changed", value, undefined, node.selected_key);
                    },
                });
                ready = true;
                return control.root;
            }
            case "disclosure": {
                const control = disclosure({
                    title: node.title,
                    open: node.open_key
                        ? Boolean(state[node.open_key])
                        : (node.initially_open ?? false),
                    content: element("div", {}, ...node.children.map((child) => this.renderNode(child))),
                });
                if (node.open_key)
                    control.addEventListener("toggle", () => void this.event(node.id, "value_changed", control.open, undefined, node.open_key));
                return control;
            }
            case "stack":
            case "row":
            case "grid":
            case "panel": {
                const root = element("section", {
                    className: `lui-manifest-${node.type} lui-manifest-gap--${node.gap ?? "medium"}`,
                });
                if (node.type === "grid")
                    root.style.setProperty("--lui-manifest-grid-columns", String(node.columns ?? 2));
                if (node.title)
                    root.append(element("h2", { text: node.title }));
                root.append(...node.children.map((child) => this.renderNode(child)));
                return root;
            }
        }
    }
    showError(error) {
        this.root.replaceChildren(stateView({
            kind: "error",
            title: "UI update failed",
            message: String(error),
        }));
    }
}
