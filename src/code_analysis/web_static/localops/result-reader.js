import { element } from "./dom.js";
import { button, stateView } from "./primitives.js";

const idPattern = /^[A-Za-z0-9](?:[A-Za-z0-9._:-]*[A-Za-z0-9])?$/;
function id(value) {
    if (typeof value !== "string" || value.length > 128 || !idPattern.test(value))
        throw new TypeError("invalid reader identity");
}
function text(value) {
    if (typeof value !== "string") throw new TypeError("reader text must be a string");
}
function shape(value, required, optional = []) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        required.some((key) => !Object.hasOwn(value, key)) ||
        Object.keys(value).some((key) => ![...required, ...optional].includes(key)))
        throw new TypeError("invalid reader object shape");
}
function revision(value) {
    if (!Number.isSafeInteger(value) || value < 0) throw new TypeError("invalid reader revision");
}
function validate(snapshot) {
    shape(snapshot, ["revision", "results", "selectedResultId", "reader"]);
    revision(snapshot.revision);
    if (!Array.isArray(snapshot.results)) throw new TypeError("results must be an array");
    const ids = new Set();
    for (const item of snapshot.results) {
        shape(item, ["id", "title", "excerpt"]);
        id(item.id); text(item.title); text(item.excerpt);
        if (!item.title.trim()) throw new TypeError("result title must name its control");
        if (ids.has(item.id)) throw new TypeError("duplicate result identity");
        ids.add(item.id);
    }
    if (snapshot.selectedResultId !== null && !ids.has(snapshot.selectedResultId))
        throw new TypeError("selected result is missing");
    const reader = snapshot.reader;
    if (reader?.kind === "unselected") {
        shape(reader, ["kind"]);
        if (snapshot.selectedResultId !== null) throw new TypeError("selected result needs an owned reader state");
    } else {
        const optional = reader?.kind === "ready" ? ["activeBlockId"] : [];
        const fields = reader?.kind === "ready"
            ? ["kind", "resultId", "id", "contentRevision", "title", "blocks"]
            : reader?.kind === "loading" ? ["kind", "resultId", "message"]
            : ["kind", "resultId", "message", "retryAvailable"];
        shape(reader, fields, optional);
        if (!["ready", "loading", "error", "unavailable"].includes(reader.kind) ||
            reader.resultId !== snapshot.selectedResultId || snapshot.selectedResultId === null)
            throw new TypeError("reader does not own the selected result");
        if (reader.kind === "ready") {
            id(reader.id); revision(reader.contentRevision); text(reader.title);
            if (!reader.title.trim()) throw new TypeError("document title must be nonempty");
            if (!Array.isArray(reader.blocks)) throw new TypeError("blocks must be an array");
            const blocks = new Set();
            for (const block of reader.blocks) {
                shape(block, ["id", "text"]); id(block.id); text(block.text);
                if (blocks.has(block.id)) throw new TypeError("duplicate block identity");
                blocks.add(block.id);
            }
            if (reader.activeBlockId !== undefined && !blocks.has(reader.activeBlockId))
                throw new TypeError("active block is missing");
        } else {
            text(reader.message);
            if (reader.kind !== "loading" && typeof reader.retryAvailable !== "boolean")
                throw new TypeError("retryAvailable must be boolean");
        }
    }
    return structuredClone(snapshot);
}

/** Native results/reader presentation. Application callbacks own retrieval and state.
 * @param {{id: string, resultsLabel: string, readerLabel: string,
 * openLabel: string, returnLabel: string, emptyLabel: string, unselectedLabel: string,
 * loadingLabel: string, errorLabel: string, unavailableLabel: string, retryLabel: string,
 * onSelect: (resultId: string) => void, onViewChange?: (view: string) => void,
 * onRetry?: (resultId: string) => void}} options
 */
export class ResultReader {
    constructor(options) {
        id(options.id);
        for (const key of ["resultsLabel", "readerLabel", "openLabel", "returnLabel", "emptyLabel",
            "unselectedLabel", "loadingLabel", "errorLabel", "unavailableLabel", "retryLabel"]) {
            text(options[key]);
            if (!options[key].trim()) throw new TypeError("reader labels must be nonempty");
        }
        if (typeof options.onSelect !== "function" ||
            ["onViewChange", "onRetry"].some((key) => options[key] !== undefined && typeof options[key] !== "function"))
            throw new TypeError("invalid reader callbacks");
        this.options = options;
        this.nodes = new Map(); this.blocks = new Map(); this.view = "results";
        this.resultsHeading = element("h2", {id: `${options.id}-results-title`, text: options.resultsLabel, tabindex: -1});
        this.readerHeading = element("h2", {id: `${options.id}-reader-title`, text: options.readerLabel, tabindex: -1});
        this.list = element("ul", {className: "lui-result-reader__list"});
        this.empty = stateView({kind: "empty", title: options.emptyLabel});
        this.resultsScroll = element("div", {className: "lui-result-reader__scroll", "data-layout-pane": "", tabindex: 0,
            role: "region", "aria-label": options.resultsLabel}, this.empty, this.list);
        this.readerScroll = element("div", {className: "lui-result-reader__scroll", "data-layout-pane": "", tabindex: 0,
            role: "region", "aria-label": options.readerLabel});
        this.onOpen = () => this.openReader(); this.onBack = () => this.returnToResults();
        this.open = button({label: options.openLabel, onClick: this.onOpen});
        this.back = button({label: options.returnLabel, onClick: this.onBack});
        this.resultsPane = element("section", {className: "lui-result-reader__pane", "aria-labelledby": this.resultsHeading.id},
            element("div", {className: "lui-result-reader__header"}, this.resultsHeading, this.open), this.resultsScroll);
        this.readerPane = element("section", {className: "lui-result-reader__pane", "aria-labelledby": this.readerHeading.id},
            element("div", {className: "lui-result-reader__header"}, this.readerHeading, this.back), this.readerScroll);
        this.root = element("div", {id: options.id, className: "lui-result-reader"}, this.resultsPane, this.readerPane);
        this.resize = new ResizeObserver((entries) => {
            this.narrow = entries[0].contentRect.width < 700;
            if (this.narrow && this.root.contains(document.activeElement))
                this.view = this.readerPane.contains(document.activeElement) ? "reader" : "results";
            this.updateVisibility();
        });
        this.resize.observe(this.root);
        this.updateVisibility();
    }
    updateVisibility() {
        if (!this.resultsPane.hidden) this.resultPosition = this.resultsScroll.scrollTop;
        if (!this.readerPane.hidden) this.readerPosition = this.readerScroll.scrollTop;
        this.root.dataset.narrow = String(Boolean(this.narrow));
        const resultsHidden = this.resultsPane.hidden;
        const readerHidden = this.readerPane.hidden;
        this.resultsPane.hidden = Boolean(this.narrow && this.view === "reader");
        this.readerPane.hidden = Boolean(this.narrow && this.view === "results");
        if (resultsHidden && !this.resultsPane.hidden) this.resultsScroll.scrollTop = this.resultPosition ?? 0;
        if (readerHidden && !this.readerPane.hidden) this.readerScroll.scrollTop = this.readerPosition ?? 0;
        if (!this.readerPane.hidden && this.pendingBlock) {
            const blockId = this.pendingBlock; this.pendingBlock = null; this.scrollBlock(blockId);
        }
    }
    ensureLive() {
        if (this.destroyed) throw new Error("ResultReader is destroyed");
    }
    /** Apply passive state atomically; equal/stale revisions fail before DOM changes. */
    setSnapshot(value) {
        this.ensureLive();
        const next = validate(value);
        if (next.reader.retryAvailable && !this.options.onRetry) throw new TypeError("retry requires an injected callback");
        if (this.snapshot && next.revision <= this.snapshot.revision) throw new Error("stale reader snapshot");
        const ready = next.reader.kind === "ready";
        const prior = this.snapshot?.reader;
        const same = ready && this.renderedDocument?.id === next.reader.id &&
            this.renderedDocument.contentRevision === next.reader.contentRevision;
        if (same && (this.renderedDocument.title !== next.reader.title ||
            JSON.stringify(this.renderedDocument.blocks) !== JSON.stringify(next.reader.blocks)))
            throw new TypeError("content changed without a contentRevision change");
        const focusedId = [...this.nodes].find(([, node]) => node.control === document.activeElement)?.[0];
        const oldIds = this.snapshot?.results.map((item) => item.id) ?? [];
        const scroll = this.resultsPane.hidden ? this.resultPosition : this.resultsScroll.scrollTop;
        this.snapshot = next;
        const keep = new Set(next.results.map((item) => item.id));
        for (const [key, node] of this.nodes) if (!keep.has(key)) {node.root.remove(); this.nodes.delete(key);}
        next.results.forEach((item, index) => {
            let node = this.nodes.get(item.id);
            if (!node) {
                const title = element("span", {className: "lui-result-reader__result-title"});
                const excerpt = element("span");
                const activate = () => {
                    if (this.destroyed) return;
                    if (this.narrow) this.openReader();
                    this.options.onSelect(item.id);
                };
                const control = button({label: "", onClick: activate});
                control.append(title, excerpt); control.dataset.resultId = item.id;
                node = {root: element("li", {}, control), control, title, excerpt, activate}; this.nodes.set(item.id, node);
            }
            if (node.title.textContent !== item.title) node.title.textContent = item.title;
            if (node.excerpt.textContent !== item.excerpt) node.excerpt.textContent = item.excerpt;
            if (next.selectedResultId === item.id) node.control.setAttribute("aria-current", "true");
            else node.control.removeAttribute("aria-current");
            if (this.list.children[index] !== node.root) this.list.insertBefore(node.root, this.list.children[index] ?? null);
        });
        this.empty.hidden = next.results.length !== 0;
        this.resultPosition = scroll;
        if (!this.resultsPane.hidden) this.resultsScroll.scrollTop = scroll;
        if (focusedId && !keep.has(focusedId)) {
            const index = Math.min(oldIds.indexOf(focusedId), next.results.length - 1);
            (this.nodes.get(next.results[index]?.id)?.control ?? this.resultsHeading).focus({preventScroll: true});
        } else if (focusedId && document.activeElement !== this.nodes.get(focusedId).control) {
            this.nodes.get(focusedId).control.focus({preventScroll: true});
        }
        if (ready) {
            if (!same) {
                this.pendingBlock = null;
                this.blocks.clear();
                const article = element("article", {className: "lui-result-reader__document", "aria-label": next.reader.title});
                for (const block of next.reader.blocks) {
                    const node = element("p", {text: block.text, dataset: {blockId: block.id}});
                    this.blocks.set(block.id, node); article.append(node);
                }
                this.readerScroll.replaceChildren(article);
                this.renderedDocument = next.reader;
                this.readerPosition = 0;
                this.readerScroll.scrollTop = 0;
                if (next.reader.activeBlockId) this.scrollBlock(next.reader.activeBlockId);
            } else if (prior?.kind !== "ready") {
                this.readerScroll.replaceChildren(this.savedArticle);
                this.readerScroll.scrollTop = this.savedScroll;
                this.readerPosition = this.savedScroll;
            }
            this.savedArticle = this.readerScroll.firstChild;
        } else {
            if (prior?.kind === "ready")
                this.savedScroll = this.readerPane.hidden ? this.readerPosition : this.readerScroll.scrollTop;
            const kind = next.reader.kind;
            const action = next.reader.retryAvailable && this.options.onRetry
                ? button({label: this.options.retryLabel, onClick: () => {
                    if (!this.destroyed) this.options.onRetry(this.snapshot.selectedResultId);
                }}) : undefined;
            this.readerScroll.replaceChildren(stateView({kind: kind === "error" ? "error" : kind === "loading" ? "loading" : "empty",
                title: this.options[`${kind}Label`], message: next.reader.message, action}));
        }
    }
    scrollBlock(blockId) {
        if (this.readerPane.hidden) {this.pendingBlock = blockId; return;}
        const node = this.blocks.get(blockId);
        const pane = this.readerScroll;
        pane.scrollTop += node.getBoundingClientRect().top - pane.getBoundingClientRect().top - 12;
    }
    /** Deliberate same-document navigation; validates the displayed content identity.
     * @param {{documentId: string, contentRevision: number, blockId: string}} target
     * @param {{focus?: boolean}} options Focus defaults false; caller decides whether to open reader.
     */
    revealBlock(target, options = {}) {
        this.ensureLive(); shape(target, ["documentId", "contentRevision", "blockId"]);
        id(target.documentId); id(target.blockId); revision(target.contentRevision);
        shape(options, [], ["focus"]);
        if (options.focus !== undefined && typeof options.focus !== "boolean") throw new TypeError("invalid reveal focus");
        const reader = this.snapshot?.reader;
        if (reader?.kind !== "ready" || reader.id !== target.documentId ||
            reader.contentRevision !== target.contentRevision || !this.blocks.has(target.blockId))
            throw new TypeError("reveal target is not in the current document");
        this.scrollBlock(target.blockId);
        if (options.focus) this.openReader();
    }
    openReader() {
        this.ensureLive(); this.view = "reader"; this.updateVisibility();
        this.readerHeading.focus({preventScroll: true}); this.options.onViewChange?.("reader");
    }
    returnToResults() {
        this.ensureLive(); this.view = "results"; this.updateVisibility();
        (this.nodes.get(this.snapshot?.selectedResultId)?.control ?? this.resultsHeading).focus({preventScroll: true});
        this.options.onViewChange?.("results");
    }
    destroy() {
        this.resize.disconnect(); this.destroyed = true;
        this.open.removeEventListener("click", this.onOpen); this.back.removeEventListener("click", this.onBack);
        for (const node of this.nodes.values()) node.control.removeEventListener("click", node.activate);
    }
}
