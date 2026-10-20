import { element } from "./dom.js";
import { button } from "./primitives.js";

const encoder = new TextEncoder();
function exact(value, fields, optional = []) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        fields.some((key) => !Object.hasOwn(value, key)) ||
        Object.keys(value).some((key) => ![...fields, ...optional].includes(key)))
        throw new TypeError("invalid editor object shape");
}
function integer(value, minimum, maximum) {
    if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new TypeError("invalid editor integer");
}
function validText(value) {
    if (typeof value !== "string" || value.includes("\r") || encoder.encode(value).length > 1048576 ||
        value.split("\n").length > 20000 || [...value].some((char) => {
            const point = char.codePointAt(0); return point >= 0xd800 && point <= 0xdfff;
        })) throw new TypeError("editor requires well-formed LF text within 1 MiB / 20000 lines");
}
function boundary(text, offset) {
    integer(offset, 0, text.length);
    if (offset && offset < text.length && /[\uD800-\uDBFF]/.test(text[offset - 1]) && /[\uDC00-\uDFFF]/.test(text[offset]))
        throw new TypeError("selection splits a Unicode surrogate pair");
}
function validSelection(text, selection) {
    exact(selection, ["start","end"], ["direction"]);
    boundary(text, selection.start); boundary(text, selection.end);
    if (selection.end < selection.start || !["none","forward","backward"].includes(selection.direction ?? "none"))
        throw new TypeError("invalid editor selection");
    return {...selection, direction:selection.direction ?? "none"};
}

/** Native plain-text editor. Document text is normalized LF by the consumer.
 * @param {{id:string, label:string, onChange?:Function, onSelection?:Function,
 * onCopy?:Function, onExport?:Function, historyLimit?:{transactions:number,bytes:number}}} options
 */
export class CodeEditor {
    constructor(options) {
        exact(options, ["id","label"], ["onChange","onSelection","onCopy","onExport","historyLimit"]);
        if (!options || typeof options.id !== "string" || options.id.length > 128 ||
            !/^[A-Za-z0-9](?:[A-Za-z0-9._:-]*[A-Za-z0-9])?$/.test(options.id) ||
            typeof options.label !== "string" || !options.label.trim()) throw new TypeError("editor needs identity and label");
        for (const key of ["onChange", "onSelection", "onCopy", "onExport"])
            if (options[key] !== undefined && typeof options[key] !== "function") throw new TypeError("invalid editor callback");
        this.limit = {...(options.historyLimit ?? {transactions:100, bytes:8388608})};
        exact(this.limit, ["transactions", "bytes"]);
        integer(this.limit.transactions, 1, 100); integer(this.limit.bytes, 1, 8388608);
        this.options = options; this.undoStack = []; this.redoStack = []; this.discarded = 0;
        this.listeners = []; this.state = {kind:"loading", message:"No document supplied"};
        this.input = element("textarea", {id:options.id, "aria-label":options.label, spellcheck:false, wrap:"soft", readOnly:true});
        this.gutter = element("div", {className:"lui-editor__gutter", "aria-hidden":"true"});
        this.measure = element("div", {className:"lui-editor__measure", "aria-hidden":"true"});
        this.status = element("p", {role:"status", "aria-live":"polite", className:"lui-editor__status"});
        this.line = element("input", {type:"number", min:1, value:1, required:true, "aria-label":"Line number"});
        this.search = element("input", {type:"search", "aria-label":"Literal search"});
        this.replacement = element("input", {type:"text", "aria-label":"Replacement text"});
        this.controls = new Map();
        const control = (label, operation) => {
            const node = button({label}); this.listen(node, "click", operation); this.controls.set(label, node); return node;
        };
        const wrap = element("input", {type:"checkbox", checked:true});
        this.listen(wrap, "change", () => {this.input.wrap = wrap.checked ? "soft" : "off"; this.layout();});
        this.root = element("section", {className:"lui-editor", "aria-label":options.label},
            element("div", {className:"lui-editor__toolbar"},
                control("Undo", () => this.undo()), control("Redo", () => this.redo()),
                control("Revert", () => this.revert()), control("Indent", () => this.indent("  ")),
                control("Outdent", () => this.indent("  ", true)),
                control("Copy", () => options.onCopy?.(this.getSnapshot())),
                control("Export", () => options.onExport?.(this.getSnapshot())),
                element("label", {}, wrap, "Wrap lines")),
            element("div", {className:"lui-editor__toolbar"}, this.line,
                control("Go to line", () => {if (this.line.reportValidity()) this.revealLines({startLine:Number(this.line.value), endLine:Number(this.line.value)}, {focus:true});}),
                this.search, this.replacement, control("Find next", () => this.find(this.search.value)),
                control("Replace", () => this.replaceSelection(this.search.value, this.replacement.value)),
                control("Replace all", () => this.replaceAll(this.search.value, this.replacement.value))),
            this.status, element("div", {className:"lui-editor__viewport", "data-layout-pane":""},
                this.gutter, this.input, this.measure));
        this.listen(this.input, "scroll", () => {this.gutter.scrollTop = this.input.scrollTop;});
        this.listen(this.input, "compositionstart", () => {this.composing = true; this.compositionBefore = this.capture();});
        this.listen(this.input, "compositionend", () => {
            this.composing = false; this.acceptInput(this.compositionBefore); this.compositionBefore = null;
        });
        this.listen(this.input, "beforeinput", (event) => {
            if (["historyUndo", "historyRedo"].includes(event.inputType)) {
                event.preventDefault(); if (!this.input.readOnly && !this.composing) event.inputType === "historyUndo" ? this.undo() : this.redo();
            } else this.before = this.capture();
        });
        this.listen(this.input, "input", () => {if (!this.composing) this.acceptInput(this.before ?? this.current);});
        this.listen(this.input, "keydown", (event) => {
            if (!this.composing && (event.ctrlKey || event.metaKey) && ["z", "y"].includes(event.key.toLowerCase())) {
                event.preventDefault();
                if (this.input.readOnly) return;
                if (event.key.toLowerCase() === "y" || event.shiftKey) this.redo(); else this.undo();
            }
        });
        const selection = () => {if (this.document && !this.composing) this.options.onSelection?.(this.getSnapshot());};
        this.listen(this.input, "select", selection); this.listen(this.input, "keyup", selection);
        this.listen(this.input, "pointerup", selection);
        this.resize = new ResizeObserver(() => this.layout()); this.resize.observe(this.input);
        this.refresh();
    }
    listen(node, kind, callback) {node.addEventListener(kind, callback); this.listeners.push([node,kind,callback]);}
    live() {if (this.destroyed) throw new Error("CodeEditor is destroyed");}
    editable() {this.live(); if (!this.document || this.input.readOnly || this.composing) throw new Error("editor is not editable");}
    capture() {return {text:this.input.value, selection:{start:this.input.selectionStart, end:this.input.selectionEnd, direction:this.input.selectionDirection}};}
    bytes(record) {return encoder.encode(record.before.text).length + encoder.encode(record.after.text).length;}
    historyBytes() {return [...this.undoStack, ...this.redoStack].reduce((sum, record) => sum + this.bytes(record), 0);}
    record(before, after) {
        if (before.text === after.text) return;
        this.discarded += this.redoStack.length; this.redoStack = [];
        this.undoStack.push({before:structuredClone(before), after:structuredClone(after)});
        while (this.undoStack.length > this.limit.transactions || this.historyBytes() > this.limit.bytes) {
            this.undoStack.shift(); this.discarded++;
        }
    }
    acceptInput(before) {
        if (!this.document) return;
        const after = this.capture();
        try {validText(after.text);} catch {
            this.restore(before ?? this.current); this.notice = "Edit rejected: text exceeds document bounds or is invalid";
            this.refresh(); return;
        }
        if (this.input.readOnly) {this.restore(this.current); return;}
        this.record(before ?? this.current, after); this.current = after; this.before = null;
        this.refresh(); if (before?.text !== after.text) this.options.onChange?.(this.getSnapshot());
    }
    restore(value) {
        this.input.value = value.text;
        this.input.setSelectionRange(value.selection.start, value.selection.end, value.selection.direction);
        this.current = structuredClone(value);
    }
    /** Passive same-document updates preserve dirty text if the supplied baseline is unchanged.
     * Switching/changing a dirty baseline requires {discardChanges:true} after consumer approval.
     */
    setDocument(document, decision = {}) {
        this.live(); exact(document, ["id","revision","text","readOnly"]); exact(decision, [], ["discardChanges"]);
        if (typeof document.id !== "string" || document.id.length > 128 ||
            !/^[A-Za-z0-9](?:[A-Za-z0-9._:-]*[A-Za-z0-9])?$/.test(document.id) || typeof document.readOnly !== "boolean" ||
            (decision.discardChanges !== undefined && typeof decision.discardChanges !== "boolean")) throw new TypeError("invalid editor document");
        integer(document.revision, 0, Number.MAX_SAFE_INTEGER); validText(document.text);
        if (this.composing) throw new Error("cannot replace a document during composition");
        const same = this.document?.id === document.id;
        if (same && document.revision <= this.document.revision) throw new Error("stale editor document revision");
        const changed = !same || this.document.text !== document.text;
        if (this.document && this.input.value !== this.document.text && changed && !decision.discardChanges)
            throw new Error("dirty document requires an explicit discard decision");
        this.document = structuredClone(document); this.state = {kind:"ready"};
        if (changed) {
            this.restore({text:document.text, selection:{start:0,end:0,direction:"none"}});
            this.undoStack = []; this.redoStack = []; this.discarded = 0; this.notice = "";
        }
        this.refresh();
    }
    setState(state) {
        this.live(); exact(state, ["kind"], ["message"]);
        if (!["ready","loading","unavailable","error"].includes(state.kind) ||
            (state.message !== undefined && typeof state.message !== "string")) throw new TypeError("invalid editor state");
        if (this.composing) throw new Error("cannot change state during composition");
        this.state = structuredClone(state); this.refresh();
    }
    getSnapshot() {
        this.live(); if (!this.document) throw new Error("editor has no document");
        return structuredClone({documentId:this.document.id, revision:this.document.revision, ...this.capture(),
            dirty:this.input.value !== this.document.text, readOnly:this.input.readOnly, state:this.state,
            history:{undo:this.undoStack.length, redo:this.redoStack.length, bytes:this.historyBytes(), discardedTransactions:this.discarded}});
    }
    setSelection(selection) {
        this.live();
        if (this.composing) throw new Error("cannot select during composition");
        const next = validSelection(this.input.value, selection);
        this.input.setSelectionRange(next.start, next.end, next.direction);
    }
    /** Explicitly hydrate a consumer-held draft against the current original baseline. */
    setDraft(draft) {
        this.editable(); exact(draft, ["documentId","revision","text","selection"]);
        if (draft.documentId !== this.document.id || draft.revision !== this.document.revision)
            throw new Error("draft does not belong to the current baseline");
        this.change(draft.text, draft.selection);
    }
    change(text, selection) {
        this.editable(); validText(text); const before = this.capture();
        const after = {text, selection:validSelection(text,selection)}; this.restore(after); this.record(before, after); this.refresh();
        if (before.text !== text) this.options.onChange?.(this.getSnapshot());
    }
    undo() {this.editable(); const record = this.undoStack.pop(); if (!record) return false;
        this.redoStack.push(record); this.restore(record.before); this.refresh(); this.options.onChange?.(this.getSnapshot()); return true;}
    redo() {this.editable(); const record = this.redoStack.pop(); if (!record) return false;
        this.undoStack.push(record); this.restore(record.after); this.refresh(); this.options.onChange?.(this.getSnapshot()); return true;}
    revert() {this.editable(); this.change(this.document.text, {start:0,end:0,direction:"none"});}
    find(query) {
        this.live(); if (typeof query !== "string" || !query) return false;
        validText(query);
        const index = this.input.value.indexOf(query, this.input.selectionEnd);
        const found = index < 0 ? this.input.value.indexOf(query) : index;
        if (found < 0) return false;
        this.setSelection({start:found,end:found+query.length}); this.input.focus(); return true;
    }
    replaceSelection(query, replacement) {
        this.editable(); if (typeof query !== "string" || !query) return false; validText(replacement);
        const {text,selection} = this.capture();
        if (text.slice(selection.start, selection.end) !== query) return this.find(query);
        this.change(text.slice(0,selection.start)+replacement+text.slice(selection.end),
            {start:selection.start,end:selection.start+replacement.length,direction:"none"}); return true;
    }
    replaceAll(query, replacement) {
        this.editable(); if (typeof query !== "string" || !query) return 0; validText(replacement);
        const chunks = this.input.value.split(query); const count = chunks.length - 1;
        if (count) this.change(chunks.join(replacement), {start:0,end:0,direction:"none"}); return count;
    }
    indent(unit = "  ", outdent = false) {
        this.editable(); if (typeof unit !== "string" || !/^[ \t]{1,8}$/.test(unit) || typeof outdent !== "boolean")
            throw new TypeError("invalid indentation");
        const {text,selection} = this.capture(); const start = selection.start ? text.lastIndexOf("\n", selection.start-1)+1 : 0;
        const final = selection.end > selection.start && text[selection.end-1] === "\n" ? selection.end-1 : selection.end;
        const end = text.indexOf("\n",final); const stop = end < 0 ? text.length : end;
        const lines = text.slice(start,stop).split("\n").map((line) => outdent ? (line.startsWith(unit) ? line.slice(unit.length) : line) : unit+line);
        const result = lines.join("\n"); this.change(text.slice(0,start)+result+text.slice(stop),
            {start,end:start+result.length,direction:"none"});
    }
    revealLines(range, options = {}) {
        this.live(); exact(range, ["startLine","endLine"]); exact(options, [], ["focus"]);
        if (this.composing) throw new Error("cannot reveal during composition");
        const count = this.input.value.split("\n").length;
        integer(range.startLine,1,count); integer(range.endLine,range.startLine,count);
        if (options.focus !== undefined && typeof options.focus !== "boolean") throw new TypeError("invalid reveal focus");
        this.layout(); const line = this.gutter.children[range.startLine-1];
        this.input.scrollTop = line.offsetTop - this.gutter.firstChild.offsetTop;
        this.gutter.scrollTop = this.input.scrollTop;
        if (options.focus) {
            const lines = this.input.value.split("\n"); const start = lines.slice(0,range.startLine-1).reduce((n,line)=>n+line.length+1,0);
            const end = start + lines.slice(range.startLine-1,range.endLine).join("\n").length;
            this.setSelection({start,end}); this.input.focus();
        }
    }
    layout() {
        if (this.destroyed) return;
        const lines = this.input.value.split("\n");
        this.line.max = lines.length;
        this.measure.style.width = `${Math.max(1,this.input.clientWidth-24)}px`;
        this.measure.style.whiteSpace = this.input.wrap === "off" ? "pre" : "pre-wrap";
        this.measure.replaceChildren(...lines.map((line)=>element("div",{text:line || "\u200b"})));
        this.gutter.replaceChildren(...lines.map((_,i)=>{
            const node = element("div",{text:i+1}); node.style.height = `${this.measure.children[i].getBoundingClientRect().height}px`; return node;
        }));
        this.gutter.scrollTop = this.input.scrollTop;
    }
    refresh() {
        this.input.readOnly = !this.document || this.document.readOnly || this.state.kind !== "ready";
        this.status.textContent = [this.state.kind, this.document?.readOnly ? "Read-only" : "", this.state.message ?? "",
            this.notice ?? "", this.discarded ? `${this.discarded} history transactions discarded` : ""].filter(Boolean).join(" · ");
        for (const [label,node] of this.controls) node.disabled = !this.document ||
            (["Undo","Redo","Revert","Indent","Outdent","Replace","Replace all"].includes(label) && this.input.readOnly) ||
            (label === "Undo" && !this.undoStack.length) || (label === "Redo" && !this.redoStack.length) ||
            (label === "Copy" && !this.options.onCopy) || (label === "Export" && !this.options.onExport);
        this.layout();
    }
    destroy() {this.resize.disconnect(); for(const [node,kind,callback] of this.listeners) node.removeEventListener(kind,callback);
        this.listeners = []; this.input.readOnly = true; this.destroyed = true;}
}
