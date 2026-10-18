"use client";

import { createContext, useContext, type ReactNode } from "react";

type Access = {
  role: string;
  permissions: string[];
  /** Owners and admins can do everything; employees only what they were granted. */
  can: (...keys: string[]) => boolean; // true if they hold ANY of the keys
  isAdmin: boolean;
};

const none: Access = { role: "", permissions: [], can: () => false, isAdmin: false };
const Ctx = createContext<Access>(none);

export function AdminAccessProvider({
  role,
  permissions,
  children,
}: {
  role: string;
  permissions: string[];
  children: ReactNode;
}) {
  const isAdmin = role === "admin" || role === "owner";
  const value: Access = {
    role,
    permissions,
    isAdmin,
    can: (...keys) => isAdmin || keys.some((k) => permissions.includes(k)),
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAccess(): Access {
  return useContext(Ctx);
}
