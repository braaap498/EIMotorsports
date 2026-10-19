"use client";

import { useState } from "react";

const input =
  "w-full rounded border border-gray-300 bg-white px-3 py-2 pr-10 text-sm text-gray-900 outline-none focus:border-[#06bde7] focus:ring-2 focus:ring-[#06bde7]/30";

// A password box with an eye button that shows or hides what was typed.
export default function PasswordField({
  value,
  onChange,
  autoComplete,
  required = true,
}: {
  value: string;
  onChange: (v: string) => void;
  autoComplete: string;
  required?: boolean;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="relative mt-1">
      <input
        className={input}
        type={visible ? "text" : "password"}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoComplete={autoComplete}
        required={required}
        spellCheck={false}
        autoCapitalize="none"
      />
      <button
        type="button"
        onClick={() => setVisible((v) => !v)}
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
        title={visible ? "Hide password" : "Show password"}
        className="absolute inset-y-0 right-0 flex w-10 items-center justify-center text-gray-500 hover:text-gray-900"
      >
        {visible ? (
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M3 3l18 18" />
            <path d="M10.6 6.1A10.9 10.9 0 0 1 12 6c5 0 8.5 4.2 9.7 6a11.6 11.6 0 0 1-2.6 3M6.2 7.6A11.5 11.5 0 0 0 2.3 12C3.5 13.8 7 18 12 18a10 10 0 0 0 3.8-.7" />
            <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M2.3 12C3.5 10.2 7 6 12 6s8.5 4.2 9.7 6c-1.2 1.8-4.7 6-9.7 6s-8.5-4.2-9.7-6z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
        )}
      </button>
    </div>
  );
}
