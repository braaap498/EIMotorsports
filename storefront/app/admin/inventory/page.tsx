"use client";

import { useEffect, useState } from "react";
import { useAccess } from "../../../components/admin/AdminAccess";
import { Notice } from "../../../components/admin/ui";
import BarcodePanel from "../../../components/admin/inventory/BarcodePanel";
import HistoryPanel from "../../../components/admin/inventory/HistoryPanel";
import ScanSession from "../../../components/admin/inventory/ScanSession";
import WebOrderShip from "../../../components/admin/inventory/WebOrderShip";

const ANY = ["inventory.receive", "inventory.ship", "inventory.barcodes", "inventory.adjust"];
const TABS = [
  { key: "receive", label: "Receive (scan in)", perms: ["inventory.receive"] },
  { key: "ship", label: "Ship (scan out)", perms: ["inventory.ship"] },
  { key: "barcodes", label: "Barcodes & labels", perms: ["inventory.barcodes"] },
  { key: "history", label: "History", perms: ANY },
];

export default function InventoryPage() {
  const { can } = useAccess();
  const tabs = TABS.filter((t) => can(...t.perms));
  const [tab, setTab] = useState("");
  const [shipMode, setShipMode] = useState<"web" | "other">("web");

  // Open on ?tab=... if given, otherwise the first tab this person may use.
  useEffect(() => {
    const want = new URLSearchParams(window.location.search).get("tab");
    setTab(tabs.find((t) => t.key === want)?.key ?? tabs[0]?.key ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs.map((t) => t.key).join(",")]);

  function pick(key: string) {
    setTab(key);
    window.history.replaceState(null, "", `/admin/inventory?tab=${key}`);
  }

  if (tabs.length === 0) return <Notice kind="error">Your account doesn’t have access to inventory.</Notice>;

  // Every allowed tab stays mounted (just hidden), so a half-finished scan list isn't lost by switching tabs.
  return (
    <div className="space-y-5">
      <h1 className="text-2xl font-semibold">Inventory</h1>
      <div className="flex gap-1 overflow-x-auto border-b border-gray-200">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => pick(t.key)}
            className={`whitespace-nowrap px-4 py-2 text-sm font-medium ${
              tab === t.key ? "border-b-2 border-black text-black" : "text-gray-500 hover:text-black"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {can("inventory.receive") && (
        <div className={tab === "receive" ? "" : "hidden"}>
          <ScanSession mode="receive" active={tab === "receive"} />
        </div>
      )}

      {can("inventory.ship") && (
        <div className={tab === "ship" ? "space-y-4" : "hidden"}>
          <div className="inline-flex overflow-hidden rounded border border-gray-300 text-sm">
            <button
              className={`px-4 py-2 ${shipMode === "web" ? "bg-black text-white" : "bg-white text-gray-700"}`}
              onClick={() => setShipMode("web")}
            >
              Website order
            </button>
            <button
              className={`px-4 py-2 ${shipMode === "other" ? "bg-black text-white" : "bg-white text-gray-700"}`}
              onClick={() => setShipMode("other")}
            >
              eBay / other sale
            </button>
          </div>
          <div className={shipMode === "web" ? "" : "hidden"}>
            <WebOrderShip active={tab === "ship" && shipMode === "web"} />
          </div>
          <div className={shipMode === "other" ? "" : "hidden"}>
            <ScanSession mode="ship-other" active={tab === "ship" && shipMode === "other"} />
          </div>
        </div>
      )}

      {can("inventory.barcodes") && (
        <div className={tab === "barcodes" ? "" : "hidden"}>
          <BarcodePanel canEdit active={tab === "barcodes"} />
        </div>
      )}

      <div className={tab === "history" ? "" : "hidden"}>
        <HistoryPanel active={tab === "history"} />
      </div>
    </div>
  );
}
