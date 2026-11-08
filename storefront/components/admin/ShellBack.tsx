"use client";

import { createContext, useContext } from "react";

/* True while the admin shell is showing its own "← Back to <page>" link above the page. A page that has its own
   "Back to ..." button reads this to hide it, so the person never sees two Back links at once. */
const Ctx = createContext(false);
export const ShellBackProvider = Ctx.Provider;
export function useShellBack(): boolean {
  return useContext(Ctx);
}
