/**
 * @typedef {"file" | "directory" | "any"} PathKind
 * @typedef {{input: string, resolved: string, exists: boolean, valid: boolean, kind?: PathKind, message?: string}} PathResolution
 * @typedef {{name: string, path: string, kind: "file" | "directory"}} PathEntry
 * @typedef {{path: string, parent?: string, entries: readonly PathEntry[]}} PathListing
 * @typedef {{resolve(path: string, kind: PathKind): Promise<PathResolution>, browse?(path: string, kind: PathKind): Promise<PathListing>, create?(path: string, kind: "file" | "directory"): Promise<PathResolution>}} PathAdapter
 * @typedef {{load(key: string): Promise<unknown> | unknown, save(key: string, value: unknown): Promise<void> | void}} PreferenceStore
 * @typedef {"idle" | "pending" | "running" | "succeeded" | "failed" | "cancelled"} TaskPhase
 * @typedef {{id: string, phase: TaskPhase, label: string, progress?: number, message?: string, log?: string, updatedAt?: string}} TaskSnapshot
 * @typedef {{unsubscribe(): void}} TaskSubscription
 * @typedef {{getSnapshot(): Promise<TaskSnapshot>, subscribe?(onUpdate: (snapshot: TaskSnapshot) => void, onError: (error: unknown) => void): TaskSubscription, cancel?(): Promise<void>, retry?(): Promise<void>}} TaskAdapter
 * @typedef {{id: string, label: string, mediaType?: string, size?: number}} ArtifactDescriptor
 * @typedef {{list(): Promise<readonly ArtifactDescriptor[]>, open(id: string): Promise<Blob | string>}} ArtifactProvider
 * @typedef {{schema_version: "1", result?: unknown, update: object}} UITransportResponse
 * @typedef {{load(): Promise<object>, send(event: object): Promise<UITransportResponse>, subscribe?(onUpdate: (update: object) => void, onError: (error: unknown) => void): TaskSubscription}} ManifestTransport
 */

function assertObject(value, name) {
    if (!value || typeof value !== "object")
        throw new TypeError(`${name} must be an object`);
    return value;
}

function assertFunction(value, name) {
    if (typeof value !== "function")
        throw new TypeError(`${name} must be a function`);
}

function assertOptionalFunction(value, name) {
    if (value !== undefined)
        assertFunction(value, name);
}

/** @param {unknown} adapter @returns {PathAdapter} */
export function assertPathAdapter(adapter) {
    const candidate = assertObject(adapter, "PathAdapter");
    assertFunction(candidate.resolve, "PathAdapter.resolve");
    assertOptionalFunction(candidate.browse, "PathAdapter.browse");
    assertOptionalFunction(candidate.create, "PathAdapter.create");
    return candidate;
}

/** @param {unknown} store @returns {PreferenceStore} */
export function assertPreferenceStore(store) {
    const candidate = assertObject(store, "PreferenceStore");
    assertFunction(candidate.load, "PreferenceStore.load");
    assertFunction(candidate.save, "PreferenceStore.save");
    return candidate;
}

/** @param {unknown} adapter @returns {TaskAdapter} */
export function assertTaskAdapter(adapter) {
    const candidate = assertObject(adapter, "TaskAdapter");
    assertFunction(candidate.getSnapshot, "TaskAdapter.getSnapshot");
    assertOptionalFunction(candidate.subscribe, "TaskAdapter.subscribe");
    assertOptionalFunction(candidate.cancel, "TaskAdapter.cancel");
    assertOptionalFunction(candidate.retry, "TaskAdapter.retry");
    return candidate;
}

/** @param {unknown} provider @returns {ArtifactProvider} */
export function assertArtifactProvider(provider) {
    const candidate = assertObject(provider, "ArtifactProvider");
    assertFunction(candidate.list, "ArtifactProvider.list");
    assertFunction(candidate.open, "ArtifactProvider.open");
    return candidate;
}

/** @param {unknown} transport @returns {ManifestTransport} */
export function assertManifestTransport(transport) {
    const candidate = assertObject(transport, "ManifestTransport");
    assertFunction(candidate.load, "ManifestTransport.load");
    assertFunction(candidate.send, "ManifestTransport.send");
    assertOptionalFunction(candidate.subscribe, "ManifestTransport.subscribe");
    return candidate;
}
