// Remembers what an admin list screen was showing (filters, page, results), so coming back to it
// from another admin page does not start over. Kept in sessionStorage: this browser tab only.

const k = (key: string) => `adminSaved:${key}`;

export function loadSaved<T>(key: string): T | null {
  try {
    const raw = sessionStorage.getItem(k(key));
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

export function saveSaved(key: string, value: unknown): void {
  try {
    sessionStorage.setItem(k(key), JSON.stringify(value));
  } catch {
    /* storage full or unavailable: the screen just starts fresh next time */
  }
}
