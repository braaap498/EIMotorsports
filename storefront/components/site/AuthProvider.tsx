"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { User } from "@supabase/supabase-js";
import { getSupabase } from "../../lib/supabase";
import { storeApi } from "../../lib/storeApi";

export type GarageItem = {
  id: number;
  year: number;
  model_id: number;
  make_id: number;
  make: string;
  model: string;
  type: string | null;
};
export type Machine = { year: number; make_id: number; model_id: number };
export type AuthMode = "signin" | "signup" | "reset" | "newpassword";

type Ctx = {
  user: User | null;
  ready: boolean;
  isStaff: boolean;
  garage: GarageItem[];
  modal: AuthMode | null;
  openAuth: (mode?: AuthMode) => void;
  closeAuth: () => void;
  signOut: () => Promise<void>;
  addToGarage: (year: number, modelId: number) => Promise<void>;
  removeFromGarage: (id: number) => Promise<void>;
  // A machine picked from the garage; the vehicle selector applies it.
  pending: Machine | null;
  selectMachine: (m: Machine) => void;
  clearPending: () => void;
};

const AuthCtx = createContext<Ctx | null>(null);

export function useAuth(): Ctx {
  const c = useContext(AuthCtx);
  if (!c) throw new Error("useAuth must be used inside <AuthProvider>");
  return c;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [garage, setGarage] = useState<GarageItem[]>([]);
  const [modal, setModal] = useState<AuthMode | null>(null);
  const [pending, setPending] = useState<Machine | null>(null);

  useEffect(() => {
    let sb: ReturnType<typeof getSupabase>;
    try {
      sb = getSupabase();
    } catch {
      setReady(true); // settings missing: the store still works, just no accounts
      return;
    }
    const { data } = sb.auth.onAuthStateChange((event, session) => {
      setUser(session?.user ?? null);
      setReady(true);
      if (event === "PASSWORD_RECOVERY") setModal("newpassword");
    });
    return () => data.subscription.unsubscribe();
  }, []);

  const uid = user?.id ?? null;
  useEffect(() => {
    if (!uid) {
      setGarage([]);
      return;
    }
    let live = true;
    storeApi<GarageItem[]>("/garage")
      .then((g) => live && setGarage(g))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [uid]);

  const addToGarage = useCallback(async (year: number, modelId: number) => {
    const item = await storeApi<GarageItem>("/garage", {
      method: "POST",
      body: { year, model_id: modelId },
    });
    setGarage((g) => (g.some((x) => x.id === item.id) ? g : [item, ...g]));
  }, []);

  const removeFromGarage = useCallback(async (id: number) => {
    await storeApi(`/garage/${id}`, { method: "DELETE" });
    setGarage((g) => g.filter((x) => x.id !== id));
  }, []);

  const signOut = useCallback(async () => {
    await getSupabase().auth.signOut();
  }, []);

  const role = (user?.app_metadata as { role?: string } | undefined)?.role;

  const value = useMemo<Ctx>(
    () => ({
      user,
      ready,
      isStaff: role === "admin" || role === "owner",
      garage,
      modal,
      openAuth: (mode: AuthMode = "signin") => setModal(mode),
      closeAuth: () => setModal(null),
      signOut,
      addToGarage,
      removeFromGarage,
      pending,
      selectMachine: (m: Machine) => setPending(m),
      clearPending: () => setPending(null),
    }),
    [user, ready, role, garage, modal, signOut, addToGarage, removeFromGarage, pending]
  );

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}
