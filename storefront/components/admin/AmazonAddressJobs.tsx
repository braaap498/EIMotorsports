"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errMsg } from "../../lib/adminApi";
import { Notice } from "./ui";
import AmazonAddressForm, { needsAddress } from "./AmazonAddressForm";

/* Self-contained panel for the Drop ship page: lists Amazon orders that stopped for lack of a ship-to address and shows the address
   form under each one. Drop <AmazonAddressJobs /> anywhere on that page. It shows nothing when there is nothing to fix.
   Uses GET /dropship/jobs?stage=problem, which already returns channel, problem, buyer_name and ebay_order_id ("amazon:<order>"). */

type Job = { id: number; ebay_order_id: string; channel: string | null; problem: string | null; stage: string };

export default function AmazonAddressJobs({ onChanged }: { onChanged?: () => void }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await api<{ jobs: Job[] }>("/dropship/jobs?stage=problem");
      setJobs((r.jobs || []).filter((j) => j.channel === "amazon" && needsAddress(j.problem)));
      setErr(null);
    } catch (e) {
      setErr(errMsg(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (err) return <Notice kind="error">{err}</Notice>;
  if (!jobs.length) return null;

  return (
    <section className="space-y-3 rounded border border-amber-200 bg-amber-50 p-3">
      <div className="text-sm font-semibold text-amber-900">Amazon orders waiting for a ship-to address ({jobs.length})</div>
      {jobs.map((j) => (
        <AmazonAddressForm
          key={j.id}
          jobId={j.id}
          orderRef={j.ebay_order_id.replace(/^amazon:/, "")}
          onDone={() => {
            void load();
            onChanged?.();
          }}
        />
      ))}
    </section>
  );
}
