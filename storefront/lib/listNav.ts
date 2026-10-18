// Remembers the list the admin was looking at (the current page of products),
// so the edit screen can step to the row above or below it.
// Kept in sessionStorage: it lives for this browser tab only.

export type NavState = {
  kind: string; // "products"
  query: string; // the filters, without limit/offset, e.g. "q=piston&published=true"
  limit: number;
  offset: number;
  total: number;
  ids: number[]; // ids on the current page, in the order shown
};

const key = (kind: string) => `adminListNav:${kind}`;

export function saveNav(s: NavState): void {
  try {
    sessionStorage.setItem(key(s.kind), JSON.stringify(s));
  } catch {
    /* storage unavailable: the arrows simply stay disabled */
  }
}

export function loadNav(kind: string): NavState | null {
  try {
    const raw = sessionStorage.getItem(key(kind));
    return raw ? (JSON.parse(raw) as NavState) : null;
  } catch {
    return null;
  }
}
