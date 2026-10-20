export function element(tag, attributes = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attributes)) {
        if (value === null || value === undefined || value === false)
            continue;
        if (key === "className")
            node.className = String(value);
        else if (key === "text")
            node.textContent = String(value);
        else if (key === "dataset" && typeof value === "object") {
            for (const [dataKey, dataValue] of Object.entries(value)) {
                node.dataset[dataKey] = String(dataValue);
            }
        }
        else if (key.startsWith("on") && typeof value === "function") {
            node.addEventListener(key.slice(2).toLowerCase(), value);
        }
        else if (key in node && !key.startsWith("aria")) {
            Reflect.set(node, key, value);
        }
        else
            node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
        if (child === null || child === undefined || child === false)
            continue;
        node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
}
export function uniqueId(prefix = "localops") {
    return `${prefix}-${crypto.randomUUID()}`;
}
export function replace(host, child) {
    host.replaceChildren(child);
}
