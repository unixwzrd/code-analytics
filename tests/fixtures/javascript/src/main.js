import { normalize } from "./shared.js";

export function transform(value) {
  const raw = String(value);
  const trimmed = raw.trim();
  const lowered = trimmed.toLowerCase();
  const normalized = lowered.replaceAll("-", "_");
  const prefixed = `item:${normalized}`;
  const clean = normalize(value);
  if (!clean) {
    throw new Error("empty");
  }
  return JSON.stringify({ clean, prefixed });
}
