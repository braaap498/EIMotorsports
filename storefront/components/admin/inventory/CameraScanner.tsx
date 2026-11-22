"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Reads a barcode with the phone (or any device) camera. It only ADDS a way to enter a code: whatever it reads is handed
 * to the same place a typed or Zebra-scanned code goes (see ScanBox), so nothing else about scanning changes.
 *
 * How it reads, in this order:
 *   1. The browser's own barcode reader (BarcodeDetector: Chrome on Android and some desktops) when it can read
 *      Data Matrix, QR and Code 128, the three label types made on the Barcodes & labels page.
 *   2. Otherwise the ZXing library, loaded from the address below the first time the camera is opened (iPhone Safari
 *      has no built-in reader). To host that file yourself instead, put it in the storefront's public folder and change
 *      ZXING_URL, e.g. to "/vendor/zxing-library.min.js". A site that restricts which script addresses may load
 *      (a Content-Security-Policy) must allow whichever address is used.
 * The camera needs the site to be on https (it is) and the person to allow camera access once.
 */
const ZXING_URL = "https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js";

// Label codes first, then the common product (UPC / EAN) codes for the box and part labels.
const NATIVE_FORMATS = ["data_matrix", "qr_code", "code_128", "ean_13", "ean_8", "upc_a", "upc_e", "code_39", "itf"];
const MUST_READ = ["data_matrix", "qr_code", "code_128"];

type Decoder = (video: HTMLVideoElement, canvas: HTMLCanvasElement) => Promise<string | null>;

let zxingLoading: Promise<any> | null = null;
function loadZXing(): Promise<any> {
  const w = window as any;
  if (w.ZXing) return Promise.resolve(w.ZXing);
  if (!zxingLoading) {
    zxingLoading = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = ZXING_URL;
      s.async = true;
      s.onload = () => (w.ZXing ? resolve(w.ZXing) : reject(new Error("The reader did not start.")));
      s.onerror = () => {
        zxingLoading = null; // allow another try next time
        reject(new Error("The barcode reader could not be loaded."));
      };
      document.head.appendChild(s);
    });
  }
  return zxingLoading;
}

async function makeDecoder(): Promise<Decoder> {
  const BD = (window as any).BarcodeDetector;
  let nativeFormats: string[] = [];
  if (BD) {
    try {
      const supported: string[] = await BD.getSupportedFormats();
      nativeFormats = NATIVE_FORMATS.filter((f) => supported.includes(f));
    } catch {
      nativeFormats = [];
    }
  }
  const native = (): Decoder => {
    const det = new BD({ formats: nativeFormats });
    return async (video) => {
      const found = await det.detect(video);
      return found.length ? String(found[0].rawValue) : null;
    };
  };
  // The browser's reader is used only when it reads all three label types.
  if (nativeFormats.length && MUST_READ.every((f) => nativeFormats.includes(f))) return native();

  try {
    const Z = await loadZXing();
    const formats = [
      Z.BarcodeFormat.DATA_MATRIX,
      Z.BarcodeFormat.QR_CODE,
      Z.BarcodeFormat.CODE_128,
      Z.BarcodeFormat.EAN_13,
      Z.BarcodeFormat.EAN_8,
      Z.BarcodeFormat.UPC_A,
      Z.BarcodeFormat.UPC_E,
      Z.BarcodeFormat.CODE_39,
      Z.BarcodeFormat.ITF,
    ];
    const hints = new Map();
    hints.set(Z.DecodeHintType.POSSIBLE_FORMATS, formats);
    hints.set(Z.DecodeHintType.TRY_HARDER, true);
    const reader = new Z.MultiFormatReader();
    reader.setHints(hints);
    return async (video, canvas) => {
      const vw = video.videoWidth;
      const vh = video.videoHeight;
      if (!vw || !vh) return null;
      const scale = Math.min(1, 1280 / vw);
      canvas.width = Math.round(vw * scale);
      canvas.height = Math.round(vh * scale);
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      if (!ctx) return null;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      try {
        const bitmap = new Z.BinaryBitmap(new Z.HybridBinarizer(new Z.HTMLCanvasElementLuminanceSource(canvas)));
        return String(reader.decode(bitmap).getText());
      } catch {
        return null; // nothing readable in this picture: try the next one
      }
    };
  } catch (e) {
    // The library could not load: use the browser's reader for whatever it can read, if it has one.
    if (nativeFormats.length) return native();
    throw e;
  }
}

export default function CameraScanner({ onCode, onClose }: { onCode: (code: string) => void; onClose: () => void }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const trackRef = useRef<MediaStreamTrack | null>(null);
  const [status, setStatus] = useState("Starting the camera…");
  const [error, setError] = useState("");
  const [torchAvailable, setTorchAvailable] = useState(false);
  const [torchOn, setTorchOn] = useState(false);

  // The callbacks are read through refs so the camera is started once, not every time the parent redraws.
  const onCodeRef = useRef(onCode);
  onCodeRef.current = onCode;

  useEffect(() => {
    let stopped = false;
    let stream: MediaStream | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const stop = () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      stream?.getTracks().forEach((t) => t.stop());
      trackRef.current = null;
    };

    (async () => {
      if (!navigator.mediaDevices?.getUserMedia) {
        setError("This browser can't open the camera here. Type the code or use the scanner.");
        return;
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } },
          audio: false,
        });
      } catch (e) {
        const name = (e as { name?: string })?.name;
        setError(
          name === "NotAllowedError" || name === "SecurityError"
            ? "Camera access was blocked. Allow the camera for this site in the browser settings, then try again."
            : "The camera could not be opened. Another app may be using it."
        );
        return;
      }
      if (stopped) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      const video = videoRef.current;
      if (!video) return;
      video.srcObject = stream;
      await video.play().catch(() => {});
      const track = stream.getVideoTracks()[0] ?? null;
      trackRef.current = track;
      try {
        const caps = (track?.getCapabilities?.() ?? {}) as unknown as { torch?: boolean };
        setTorchAvailable(!!caps.torch);
      } catch {
        /* no torch control on this device */
      }

      let decode: Decoder;
      try {
        decode = await makeDecoder();
      } catch (e) {
        setError(`${e instanceof Error ? e.message : "The barcode reader could not start."} Type the code or use the scanner.`);
        stop();
        return;
      }
      if (stopped) return;
      setStatus("Point the camera at the barcode");

      const canvas = document.createElement("canvas");
      const tick = async () => {
        if (stopped) return;
        let code: string | null = null;
        try {
          if (video.readyState >= 2) code = await decode(video, canvas);
        } catch {
          code = null;
        }
        if (stopped) return;
        if (code && code.trim()) {
          stop();
          try {
            navigator.vibrate?.(80);
          } catch {
            /* not everywhere */
          }
          onCodeRef.current(code.trim());
          return;
        }
        timer = setTimeout(tick, 120);
      };
      void tick();
    })();

    return stop;
  }, []);

  async function toggleTorch() {
    const track = trackRef.current;
    if (!track) return;
    const next = !torchOn;
    try {
      await track.applyConstraints({ advanced: [{ torch: next }] } as unknown as MediaTrackConstraints);
      setTorchOn(next);
    } catch {
      setTorchAvailable(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex flex-col bg-black text-white" role="dialog" aria-label="Scan with the camera">
      <div className="flex items-center justify-between gap-3 px-4 py-3">
        <p className="text-sm">{error ? "Camera" : status}</p>
        <button type="button" onClick={onClose} className="rounded border border-white/60 px-4 py-2 text-sm font-semibold">
          Close
        </button>
      </div>
      <div className="relative min-h-0 flex-1">
        <video ref={videoRef} playsInline muted autoPlay className="absolute inset-0 h-full w-full object-cover" />
        {!error && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <div className="h-[55%] w-[75%] max-w-sm rounded-lg border-2 border-white/80" />
          </div>
        )}
        {error && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/80 p-6">
            <p className="max-w-sm text-center text-base">{error}</p>
          </div>
        )}
      </div>
      <div className="flex items-center justify-between gap-3 px-4 py-3">
        <p className="text-xs text-white/70">Hold steady, with the whole code inside the box and good light.</p>
        {torchAvailable && (
          <button type="button" onClick={() => void toggleTorch()} className="rounded border border-white/60 px-4 py-2 text-sm font-semibold">
            {torchOn ? "Light off" : "Light on"}
          </button>
        )}
      </div>
    </div>
  );
}
