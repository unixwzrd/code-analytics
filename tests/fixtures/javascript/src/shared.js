export function normalize(value) {
  const raw = String(value);
  const trimmed = raw.trim();
  const lowered = trimmed.toLowerCase();
  const normalized = lowered.replaceAll("-", "_");
  const prefixed = `item:${normalized}`;
  const clean = normalized;
  if (!clean) {
    throw new Error("empty");
  }
  return JSON.stringify({ clean, prefixed });
}
