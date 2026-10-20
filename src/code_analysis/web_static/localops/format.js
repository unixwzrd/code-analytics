export function formatDuration(milliseconds, options = {}) {
    const value = Number(milliseconds);
    if (!Number.isFinite(value) || value < 0)
        return "n/a";
    const seconds = value / 1000;
    let human;
    if (seconds < 60)
        human = `${seconds < 10 ? seconds.toFixed(2) : seconds.toFixed(1)}s`;
    else if (seconds < 3600)
        human = `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
    else {
        const remainder = Math.round(seconds % 60);
        human = `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m${remainder ? ` ${remainder}s` : ""}`;
    }
    return options.includeMilliseconds === false
        ? human
        : `${human} (${Math.round(value)} ms)`;
}
export function formatNumber(value, options = {}) {
    return typeof value === "number" && Number.isFinite(value)
        ? new Intl.NumberFormat(undefined, options).format(value)
        : "n/a";
}
