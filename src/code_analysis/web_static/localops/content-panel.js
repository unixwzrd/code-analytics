import { element } from "./dom.js";
import { button } from "./primitives.js";

function exact(value, required, optional = []) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        required.some((key) => !Object.hasOwn(value, key)) ||
        Object.keys(value).some((key) => ![...required, ...optional].includes(key)))
        throw new TypeError("invalid ContentPanel options");
}
function visible(node) {
    return node instanceof HTMLElement && node.isConnected && !node.closest("[hidden], [inert]") &&
        !node.matches(":disabled") && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
}
function focusable(node) {return visible(node) && (node.tabIndex >= 0 || node.hasAttribute("tabindex"));}
function suppliedContent(content) {
    if (typeof content !== "string" && (!(content instanceof Node) || ![1, 3, 11].includes(content.nodeType)))
        throw new TypeError("ContentPanel content must be trusted DOM or literal text");
}

/** Modal content surface. DOM must be consumer-trusted; strings are literal text.
 * @param {{id:string,title:string,content:Node|string,description?:string,closeLabel?:string,
 * expandLabel?:string,restoreLabel?:string,fallbackFocus?:HTMLElement,
 * onOpenChange?:(open:boolean)=>void,onExpandedChange?:(expanded:boolean)=>void}} options
 */
export class ContentPanel {
    constructor(options) {
        exact(options, ["id", "title", "content"], ["description", "closeLabel", "expandLabel", "restoreLabel",
            "fallbackFocus", "onOpenChange", "onExpandedChange"]);
        if (typeof options.id !== "string" || options.id.length > 128 ||
            !/^[A-Za-z0-9](?:[A-Za-z0-9._:-]*[A-Za-z0-9])?$/.test(options.id)) throw new TypeError("invalid panel identity");
        for (const key of ["title", "description", "closeLabel", "expandLabel", "restoreLabel"])
            if ((key === "title" || options[key] !== undefined) &&
                (typeof options[key] !== "string" || !options[key].trim())) throw new TypeError("panel labels must be nonempty");
        for (const key of ["onOpenChange", "onExpandedChange"])
            if (options[key] !== undefined && typeof options[key] !== "function") throw new TypeError("invalid panel callback");
        if (options.fallbackFocus !== undefined && !(options.fallbackFocus instanceof HTMLElement))
            throw new TypeError("invalid panel fallbackFocus");
        suppliedContent(options.content);
        this.options = {...options}; this.expanded = false; this.opened = false;
        this.title = element("h2", {id:`${options.id}-title`, text:options.title, tabindex:-1});
        this.closeButton = button({label:options.closeLabel ?? "Close"});
        this.expandButton = button({label:options.expandLabel ?? "Expand"});
        this.expandButton.setAttribute("aria-pressed", "false");
        this.content = element("div", {className:"lui-content-panel__body", "data-layout-pane":"", tabindex:0,
            role:"region", "aria-label":options.title}, options.content);
        this.root = element("dialog", {id:options.id, className:"lui-content-panel", role:"dialog", "aria-modal":"true",
            "aria-labelledby":this.title.id, "aria-describedby":options.description ? `${options.id}-description` : undefined},
            element("header", {className:"lui-content-panel__header"}, this.title, this.expandButton, this.closeButton));
        if (options.description) this.root.append(element("p", {id:`${options.id}-description`,
            className:"lui-content-panel__description", text:options.description}));
        this.root.append(this.content);
        this.closeClick = () => this.close(); this.expandClick = () => this.setExpanded(!this.expanded);
        this.compositionStart = (event) => {this.compositionTarget = event.target;};
        this.compositionEnd = () => {this.compositionTarget = null;};
        this.composing = () => Boolean(this.compositionTarget && this.root.contains(this.compositionTarget));
        this.cancel = (event) => {event.preventDefault(); if (!this.composing()) this.close();};
        this.closed = () => {if (!this.root.open) this.finishClose();};
        this.escape = (event) => {
            if (!this.root.open || event.key !== "Escape" || event.altKey || event.ctrlKey || event.metaKey ||
                event.shiftKey || event.isComposing || event.keyCode === 229 || this.composing()) return;
            // Only the innermost native dialog owns this key, including DOM-nested panels.
            const dialog = event.composedPath().find((node) => node instanceof HTMLDialogElement && node.open);
            if (dialog !== this.root) return;
            event.preventDefault(); event.stopPropagation(); this.close();
        };
        this.keydown = (event) => {
            if (event.key !== "Tab") return;
            const nodes = [...this.root.querySelectorAll("button,input,select,textarea,a[href],[tabindex]")]
                .filter((node) => focusable(node) && node.tabIndex >= 0)
                .sort((a, b) => (a.tabIndex || Number.MAX_SAFE_INTEGER) - (b.tabIndex || Number.MAX_SAFE_INTEGER));
            if (!nodes.length) {event.preventDefault(); this.title.focus(); return;}
            if (event.shiftKey && (document.activeElement === nodes[0] || !nodes.includes(document.activeElement))) {
                event.preventDefault(); nodes.at(-1).focus();
            } else if (!event.shiftKey && (document.activeElement === nodes.at(-1) || !nodes.includes(document.activeElement))) {
                event.preventDefault(); nodes[0].focus();
            }
        };
        this.closeButton.addEventListener("click", this.closeClick); this.expandButton.addEventListener("click", this.expandClick);
        this.root.addEventListener("cancel", this.cancel); this.root.addEventListener("close", this.closed);
        this.root.addEventListener("keydown", this.keydown);
        this.root.addEventListener("keydown", this.escape, true);
        this.root.addEventListener("compositionstart", this.compositionStart, true);
        this.root.addEventListener("compositionend", this.compositionEnd, true);
    }
    live() {if (this.destroyed) throw new Error("ContentPanel is destroyed");}
    open(options = {}) {
        this.live(); exact(options, [], ["opener", "initialFocus"]);
        if (options.opener !== undefined && (!focusable(options.opener) || this.root.contains(options.opener)))
            throw new TypeError("panel opener must be a visible external focus target");
        if (options.initialFocus !== undefined && (!(options.initialFocus instanceof HTMLElement) ||
            !this.root.contains(options.initialFocus) || options.initialFocus.matches(":disabled") ||
            options.initialFocus.closest("[hidden],[inert]") ||
            (options.initialFocus.tabIndex < 0 && !options.initialFocus.hasAttribute("tabindex"))))
            throw new TypeError("invalid panel initialFocus");
        if (this.root.open) return false;
        this.opener = options.opener ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
        if (!this.root.isConnected) document.body.append(this.root);
        this.root.showModal();
        if (options.initialFocus && !focusable(options.initialFocus)) {
            this.root.close(); this.opener = null; throw new TypeError("panel initialFocus is not visible");
        }
        this.opened = true; (options.initialFocus ?? this.title).focus({preventScroll:true});
        this.options.onOpenChange?.(true); return true;
    }
    finishClose() {
        if (!this.opened) return;
        this.opened = false;
        this.compositionTarget = null;
        const fallback = this.options.fallbackFocus;
        let target = focusable(this.opener) ? this.opener : focusable(fallback) ? fallback : document.querySelector("main");
        if (!visible(target)) target = document.body;
        const previous = target.getAttribute("tabindex");
        if (!focusable(target)) target.setAttribute("tabindex", "-1");
        target.focus({preventScroll:true});
        if (previous === null) target.removeAttribute("tabindex");
        this.opener = null; this.options.onOpenChange?.(false);
    }
    close() {
        this.live(); if (!this.root.open) return false;
        this.root.close(); this.finishClose(); return true;
    }
    setExpanded(expanded) {
        this.live(); if (typeof expanded !== "boolean") throw new TypeError("expanded must be boolean");
        if (this.expanded === expanded) return false;
        this.expanded = expanded; this.root.dataset.expanded = String(expanded);
        this.expandButton.setAttribute("aria-pressed", String(expanded));
        this.expandButton.textContent = expanded ? (this.options.restoreLabel ?? "Restore") : (this.options.expandLabel ?? "Expand");
        this.options.onExpandedChange?.(expanded); return true;
    }
    setContent(content) {
        this.live(); suppliedContent(content);
        if (content instanceof Node && (content === this.root || content.contains(this.root) || content === this.content))
            throw new TypeError("panel content must not contain the panel");
        if (content === this.content.firstChild && this.content.childNodes.length === 1) return;
        const focused = this.content.contains(document.activeElement);
        this.content.replaceChildren(typeof content === "string" ? document.createTextNode(content) : content);
        if (!this.composing()) this.compositionTarget = null;
        if (focused && this.root.open) this.title.focus({preventScroll:true});
    }
    getState() {this.live(); return Object.freeze({open:this.root.open, expanded:this.expanded});}
    destroy() {
        this.live(); this.close();
        this.closeButton.removeEventListener("click", this.closeClick); this.expandButton.removeEventListener("click", this.expandClick);
        this.root.removeEventListener("cancel", this.cancel); this.root.removeEventListener("close", this.closed);
        this.root.removeEventListener("keydown", this.escape, true);
        this.root.removeEventListener("compositionstart", this.compositionStart, true);
        this.root.removeEventListener("compositionend", this.compositionEnd, true);
        this.root.removeEventListener("keydown", this.keydown); this.root.remove(); this.destroyed = true;
    }
}
