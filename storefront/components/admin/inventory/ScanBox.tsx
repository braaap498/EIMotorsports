"use client";

import { forwardRef, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { btn2Cls, btnCls } from "../ui";
import CameraScanner from "./CameraScanner";
import PhonePairing from "./PhonePairing";

/**
 * The box the Zebra scanner types into. A scanner in its normal (keyboard) mode types the
 * code and presses Enter, which submits this form. You can also type a code by hand and
 * press the Enter button next to the box. Where the device has a camera (a phone), a Camera button next to them reads
 * the barcode with it and enters the code the same way; `camera={false}` leaves that button out.
 * On a computer (no touch screen) a Phone button also shows: it makes a QR code, the phone opens it and scans with ITS camera, and
 * each code arrives in this same box, exactly as if it had been typed here (see PhonePairing).
 */
const ScanBox = forwardRef<
  HTMLInputElement,
  { onScan: (code: string) => void; placeholder?: string; disabled?: boolean; buttonLabel?: string; hint?: string | false; camera?: boolean }
>(function ScanBox(
  { onScan, placeholder = "Scan a barcode here…", disabled, buttonLabel = "Enter", hint, camera = true },
  ref
) {
  const [value, setValue] = useState("");
  const [camOpen, setCamOpen] = useState(false);
  const [phoneOpen, setPhoneOpen] = useState(false);
  // A computer (mouse, no touch screen) offers the phone; a phone or tablet already has its own camera.
  const [isComputer, setIsComputer] = useState(false);
  // Known only in the browser (so the first draw matches the server's): the button shows where a camera can be opened.
  const [canCamera, setCanCamera] = useState(false);
  useEffect(() => {
    setCanCamera(typeof navigator !== "undefined" && !!navigator.mediaDevices?.getUserMedia);
    setIsComputer(typeof window !== "undefined" && !window.matchMedia("(pointer: coarse)").matches);
  }, []);
  return (
    <div>
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          const code = value.trim();
          if (!code) return;
          setValue("");
          onScan(code);
        }}
      >
        <input
          ref={ref}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={disabled}
          placeholder={placeholder}
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          className="min-w-0 flex-1 rounded-lg border-2 border-gray-400 bg-white px-4 py-3 text-lg text-gray-900 focus:border-black focus:outline-none disabled:bg-gray-100"
        />
        {camera && isComputer && (
          <button
            type="button"
            className={`${btn2Cls} px-4 text-base`}
            disabled={disabled}
            onClick={() => setPhoneOpen((v) => !v)}
            title="Scan with your phone's camera: a QR code connects it to this box"
          >
            Phone
          </button>
        )}
        {camera && canCamera && (
          <button
            type="button"
            className={`${btn2Cls} px-4 text-base`}
            disabled={disabled}
            onClick={() => setCamOpen(true)}
            title="Read the barcode with the camera"
          >
            Camera
          </button>
        )}
        <button type="submit" className={`${btnCls} px-5 text-base`} disabled={disabled || !value.trim()}>
          {buttonLabel}
        </button>
      </form>
      {phoneOpen && <PhonePairing onCode={(code) => onScan(code)} onClose={() => setPhoneOpen(false)} />}
      {camOpen &&
        createPortal(
          <CameraScanner
            onClose={() => setCamOpen(false)}
            onCode={(code) => {
              setCamOpen(false);
              onScan(code);
            }}
          />,
          document.body
        )}
      {hint !== false && (
        <p className="mt-1 text-xs text-gray-500">{hint ?? "Scan it, or type it and press the Enter button."}</p>
      )}
    </div>
  );
});

export default ScanBox;
