"use client";

import { useCallback, useEffect, useState } from "react";
import CameraScanner from "./CameraScanner";

/**
 * The page a phone opens from the QR code on the computer (/scan/<secret>). It reads barcodes with the phone camera and sends each one to
 * the computer's scan box (see PhonePairing and app/scan_pair.py). After each scan it shows what was sent and gets ready for the next, so
 * a whole stack of parts can be scanned without touching the computer. No sign-in: the secret link is the permission.
 */
const API = process.env.NEXT_PUBLIC_API_URL ?? "";

type Phase = "checking" | "ready" | "scanning" | "sent" | "ended";

export default function PhoneScanPage({ token }: { token: string }) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [last, setLast] = useState("");
  const [sentCount, setSentCount] = useState(0);
  const [error, setError] = useState("");
  const [cameraKey, setCameraKey] = useState(0);

  useEffect(() => {
    let alive = true;
    fetch(`${API}/scan-pair/${encodeURIComponent(token)}`)
      .then((r) => {
        if (!alive) return;
        setPhase(r.ok ? "ready" : "ended");
      })
      .catch(() => {
        if (!alive) return;
        setError("Could not reach the server. Check the phone's connection and reload this page.");
        setPhase("ready");
      });
    return () => {
      alive = false;
    };
  }, [token]);

  const onCode = useCallback(
    async (code: string) => {
      setLast(code);
      setError("");
      try {
        const r = await fetch(`${API}/scan-pair/${encodeURIComponent(token)}/scan`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });
        if (r.status === 410) {
          setPhase("ended");
          return;
        }
        if (!r.ok) {
          const j = await r.json().catch(() => null);
          setError(typeof j?.detail === "string" ? j.detail : `The computer did not accept that code (${r.status}).`);
          setPhase("ready");
          return;
        }
        setSentCount((n) => n + 1);
        setPhase("sent");
      } catch {
        setError("The code could not be sent. Check the phone's connection and scan it again.");
        setPhase("ready");
      }
    },
    [token]
  );

  // After "sent" show the code for a moment, then open the camera again for the next one.
  useEffect(() => {
    if (phase !== "sent") return;
    const t = setTimeout(() => {
      setCameraKey((k) => k + 1);
      setPhase("scanning");
    }, 1300);
    return () => clearTimeout(t);
  }, [phase]);

  if (phase === "scanning") {
    return <CameraScanner key={cameraKey} onCode={(c) => void onCode(c)} onClose={() => setPhase("ready")} />;
  }

  return (
    <div className="fixed inset-0 z-[100] flex flex-col items-center justify-center gap-5 bg-white p-6 text-center text-gray-900">
      {phase === "checking" && <p className="text-lg">Checking the link…</p>}
      {phase === "ended" && (
        <>
          <h1 className="text-2xl font-semibold">This link has ended</h1>
          <p className="max-w-sm text-base text-gray-700">Go back to the computer and press the Phone button again to get a new QR code.</p>
        </>
      )}
      {phase === "sent" && (
        <>
          <h1 className="text-2xl font-semibold text-green-700">Sent to the computer</h1>
          <p className="max-w-sm break-all font-mono text-lg">{last}</p>
          <p className="text-sm text-gray-600">Getting ready for the next one…</p>
        </>
      )}
      {phase === "ready" && (
        <>
          <h1 className="text-2xl font-semibold">Scan for the computer</h1>
          <p className="max-w-sm text-base text-gray-700">
            Each barcode you scan here shows up in the scan box on the computer, as if it had been typed there.
          </p>
          {sentCount > 0 && <p className="text-sm text-green-700">{sentCount} sent{last ? `, last: ${last}` : ""}</p>}
          {error && <p className="max-w-sm text-base text-red-700">{error}</p>}
          <button
            type="button"
            className="rounded-lg bg-black px-8 py-4 text-lg font-semibold text-white"
            onClick={() => {
              setCameraKey((k) => k + 1);
              setPhase("scanning");
            }}
          >
            {sentCount > 0 ? "Scan next" : "Start camera"}
          </button>
        </>
      )}
    </div>
  );
}
