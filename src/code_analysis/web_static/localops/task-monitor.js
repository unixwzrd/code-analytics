import { element } from "./dom.js";
import { button, statusBadge } from "./primitives.js";
import { assertTaskAdapter } from "./contracts.js";
function tone(snapshot) {
    if (snapshot.phase === "succeeded")
        return "success";
    if (snapshot.phase === "failed")
        return "danger";
    if (snapshot.phase === "cancelled")
        return "warning";
    if (snapshot.phase === "running" || snapshot.phase === "pending")
        return "info";
    return "neutral";
}
export class TaskMonitor {
    options;
    root;
    status = element("div", {
        className: "lui-task__status",
        "aria-live": "polite",
    });
    progress = element("progress", {
        className: "lui-task__progress",
        max: 100,
    });
    log = element("pre", {
        className: "lui-task__log",
        tabindex: 0,
        "aria-label": "Task log",
    });
    cancelButton;
    retryButton;
    subscription;
    timer;
    constructor(options) {
        this.options = options;
        assertTaskAdapter(options.adapter);
        this.cancelButton = button({
            label: options.labels?.cancel ?? "Cancel",
            onClick: () => void this.command("cancel"),
        });
        this.retryButton = button({
            label: options.labels?.retry ?? "Retry",
            onClick: () => void this.command("retry"),
        });
        this.root = element("section", { className: "lui-task", "aria-label": "Task monitor" }, this.status, this.progress, element("div", { className: "lui-task__actions" }, this.cancelButton, this.retryButton, button({ label: "Top", onClick: () => (this.log.scrollTop = 0) }), button({
            label: "Follow tail",
            onClick: () => (this.log.scrollTop = this.log.scrollHeight),
        })), this.log);
    }
    async start() {
        await this.refresh();
        if (this.options.adapter.subscribe)
            this.subscription = this.options.adapter.subscribe((snapshot) => this.update(snapshot), (error) => this.showError(error));
        else
            this.timer = window.setInterval(() => void this.refresh(), this.options.pollIntervalMs ?? 2000);
    }
    stop() {
        this.subscription?.unsubscribe();
        if (this.timer !== undefined)
            window.clearInterval(this.timer);
    }
    async refresh() {
        try {
            this.update(await this.options.adapter.getSnapshot());
        }
        catch (error) {
            this.showError(error);
        }
    }
    update(snapshot) {
        this.status.replaceChildren(element("strong", { text: snapshot.label }), statusBadge(snapshot.phase, tone(snapshot)), snapshot.message
            ? element("span", { text: snapshot.message })
            : element("span"));
        if (snapshot.progress === undefined) {
            this.progress.removeAttribute("value");
            this.progress.setAttribute("aria-label", "Progress unknown");
        }
        else {
            this.progress.value = Math.max(0, Math.min(100, snapshot.progress));
            this.progress.setAttribute("aria-label", `${this.progress.value}% complete`);
        }
        const nearBottom = this.log.scrollHeight - this.log.scrollTop - this.log.clientHeight < 48;
        const selected = window.getSelection()?.toString();
        if ((snapshot.log ?? "") !== this.log.textContent)
            this.log.textContent = snapshot.log ?? "";
        if (nearBottom && !selected)
            this.log.scrollTop = this.log.scrollHeight;
        this.cancelButton.disabled =
            !this.options.adapter.cancel ||
                !["pending", "running"].includes(snapshot.phase);
        this.retryButton.disabled =
            !this.options.adapter.retry ||
                !["failed", "cancelled"].includes(snapshot.phase);
    }
    async command(command) {
        const callback = this.options.adapter[command];
        if (!callback)
            return;
        await callback.call(this.options.adapter);
        await this.refresh();
    }
    showError(error) {
        this.status.replaceChildren(statusBadge("Update failed", "danger"), element("span", {
            text: error instanceof Error ? error.message : String(error),
        }));
    }
}
