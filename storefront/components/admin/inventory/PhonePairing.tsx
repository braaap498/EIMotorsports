"use client";

import { useEffect, useRef, useState } from "react";
import { api, errMsg } from "../../../lib/adminApi";
import { btn2Cls } from "../ui";

/**
 * Scan with a phone while working on the computer. It sits under a scan box (see ScanBox):
 *   1. it asks the server for a short-lived pairing and shows its link as a QR code;
 *   2. the phone scans the QR with its normal camera app, opens the link, and reads barcodes with its camera;
 *   3. this panel asks the server every second for new codes and hands each one to `onCode`, which is the same place a typed or
 *      Zebra-scanned code goes. So a scan lands on the screen (and the step) of the scan box that made the QR.
 * Each scan box makes its own pairing, so two boxes on one screen never get each other's scans. Closing the panel ends the pairing.
 *
 * The QR picture is drawn with the small qrcode-generator library, loaded from the address below the first time (the barcode reader
 * is loaded from the same host). If it cannot load, the link is shown as text to open on the phone by hand.
 */
const QR_URL = "https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js";
const POLL_MS = 1000;

let qrLoading: Promise<any> | null = null;
function loadQr(): Promise<any> {
  const w = window as any;
  if (typeof w.qrcode === "function") return Promise.resolve(w.qrcode);
  if (!qrLoading) {
    qrLoading = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = QR_URL;
      s.async = true;
      s.onload = () => (typeof w.qrcode === "function" ? resolve(w.qrcode) : reject(new Error("The QR maker did not start.")));
      s.onerror = () => {
        qrLoading = null;
        reject(new Error("The QR maker could not be loaded."));
      };
      document.head.appendChild(s);
    });
  }
  return qrLoading;
}

/** The QR as an SVG path (one square per dark module) plus its size with a 4-module white border. */
async function makeQr(text: string): Promise<{ size: number; path: string }> {
  const qrcode = await loadQr();
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const n: number = qr.getModuleCount();
  const quiet = 4;
  let path = "";
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (qr.isDark(r, c)) path += `M${c + quiet} ${r + quiet}h1v1h-1z`;
    }
  }
  return { size: n + quiet * 2, path };
}

type Poll = { scans: { id: number; code: string }[]; phone_connected: boolean };

export default function PhonePairing({ onCode, onClose }: { onCode: (code: string) => void; onClose: () => void }) {
  const [link, setLink] = useState("");
  const [qr, setQr] = useState<{ size: number; path: string } | null>(null);
  const [qrFailed, setQrFailed] = useState(false);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(false);
  const [last, setLast] = useState("");
  const [count, setCount] = useState(0);
  const [copied, setCopied] = useState(false);

  // The handler is read through a ref so the pairing is made once, not every time the parent redraws.
  const onCodeRef = useRef(onCode);
  onCodeRef.current = onCode;

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let token = "";
    let after = 0;

    (async () => {
      try {
        const r = await api<{ token: string }>("/scan-pair", { method: "POST" });
        if (stopped) {
          void api(`/scan-pair/${r.token}`, { method: "DELETE" }).catch(() => {});
          return;
        }
        token = r.token;
      } catch (e) {
        if (!stopped) setError(errMsg(e));
        return;
      }
      const url = `${window.location.origin}/scan/${token}`;
      setLink(url);
      makeQr(url)
        .then((q) => !stopped && setQr(q))
        .catch(() => !stopped && setQrFailed(true));

      const tick = async () => {
        if (stopped) return;
        try {
          const p = await api<Poll>(`/scan-pair/${token}?after=${after}`);
          if (stopped) return;
          setConnected(p.phone_connected);
          for (const s of p.scans) {
            after = Math.max(after, s.id);
            setLast(s.code);
            setCount((n) => n + 1);
            onCodeRef.current(s.code);
          }
        } catch (e) {
          if (stopped) return;
          const status = (e as { status?: number })?.status;
          if (status === 404 || status === 401) {
            setError(status === 404 ? "This phone link has ended. Close this and press Phone again." : errMsg(e));
            return; // no more polling
          }
          // a hiccup (network): try again next round
        }
        timer = setTimeout(tick, POLL_MS);
      };
      void tick();
    })();

    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (token) void api(`/scan-pair/${token}`, { method: "DELETE" }).catch(() => {});
    };
  }, []);

  async function copy() {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* the link is shown, it can be selected by hand */
    }
  }

  return (
    <div className="mt-2 flex flex-wrap items-start gap-4 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-gray-800">
      <div className="shrink-0">
        {qr ? (
          <svg viewBox={`0 0 ${qr.size} ${qr.size}`} className="h-40 w-40 rounded bg-white" shapeRendering="crispEdges" role="img" aria-label="QR code to open the scanner on your phone">
            <rect width={qr.size} height={qr.size} fill="#fff" />
            <path d={qr.path} fill="#000" />
          </svg>
        ) : (
          <div className="flex h-40 w-40 items-center justify-center rounded bg-white text-xs text-gray-500">
            {error ? "No QR" : qrFailed ? "QR could not load" : "Making QR…"}
          </div>
        )}
      </div>
      <div className="min-w-0 flex-1 space-y-1">
        <p className="font-semibold">Scan with your phone</p>
        {error ? (
          <p className="text-red-700">{error}</p>
        ) : (
          <>
            <p>
              1. Point your phone&rsquo;s camera at the QR code and open the link.
              <br />
              2. Tap <b>Start camera</b>, then scan barcodes. Each one lands here, in this box.
            </p>
            <p className={connected ? "font-medium text-green-800" : "text-gray-600"}>
              {connected ? "Phone connected." : "Waiting for the phone…"}
              {count > 0 ? ` ${count} code${count === 1 ? "" : "s"} received, last: ${last}` : ""}
            </p>
            {(qrFailed || !qr) && link && (
              <p className="break-all text-xs text-gray-600">
                Or type this address on the phone: <span className="font-mono">{link}</span>
              </p>
            )}
          </>
        )}
        <div className="flex flex-wrap gap-2 pt-1">
          {link && !error && (
            <button type="button" className={`${btn2Cls} px-3 py-1 text-sm`} onClick={() => void copy()}>
              {copied ? "Copied" : "Copy link"}
            </button>
          )}
          <button type="button" className={`${btn2Cls} px-3 py-1 text-sm`} onClick={onClose}>
            {error ? "Close" : "Done with phone"}
          </button>
        </div>
        <p className="text-xs text-gray-500">The link works only for this scan box and stops when you press Done, or about an hour after the phone camera was last on.</p>
      </div>
    </div>
  );
}
