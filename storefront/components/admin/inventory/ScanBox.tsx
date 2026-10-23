"use client";

import { forwardRef, useState } from "react";

/**
 * The box the Zebra scanner types into. A scanner in its normal (keyboard) mode types the
 * code and presses Enter, which submits this form. You can also type a code by hand.
 */
const ScanBox = forwardRef<
  HTMLInputElement,
  { onScan: (code: string) => void; placeholder?: string; disabled?: boolean }
>(function ScanBox({ onScan, placeholder = "Scan a barcode here…", disabled }, ref) {
  const [value, setValue] = useState("");
  return (
    <form
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
        className="w-full rounded-lg border-2 border-gray-400 bg-white px-4 py-3 text-lg text-gray-900 focus:border-black focus:outline-none disabled:bg-gray-100"
      />
    </form>
  );
});

export default ScanBox;
