"use client";

import { useEffect, useState } from "react";
import { api } from "../../lib/adminApi";

export type AmazonAccount = {
  account: string;
  display_name: string | null;
  seller_id: string | null;
  authorized: boolean;
  authorized_at: string | null;
  live_pushes_enabled: boolean;
  sales_polling_enabled: boolean;
  sales_poll_minutes: number;
  sales_qty_push_enabled: boolean;
  marketplaces: string | null;
  dropship_policy_ack: boolean;
  referral_fee_pct: number | null;
  target_margin_pct: number | null;
  ship_cost_est: number | null;
  stand_down_enabled: boolean;
};

/** The Amazon accounts, loaded once. `account` is the one picked in the filter ("" = all). */
export function useAmazonAccounts() {
  const [accounts, setAccounts] = useState<AmazonAccount[]>([]);
  const [account, setAccount] = useState("");
  useEffect(() => {
    api<{ accounts: AmazonAccount[] }>("/amazon-connect/accounts")
      .then((r) => setAccounts(r.accounts))
      .catch(() => {});
  }, []);
  return { accounts, account, setAccount };
}

export const money = (v: number | string | null | undefined) =>
  v === null || v === undefined || v === "" ? "—" : `$${Number(v).toFixed(2)}`;
