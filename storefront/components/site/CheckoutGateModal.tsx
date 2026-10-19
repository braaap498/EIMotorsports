"use client";

import { useAuth } from "./AuthProvider";

// "How would you like to check out?" Shown when a customer who is not signed in
// starts to pay. Open it with requireCheckoutChoice() from useAuth().
export default function CheckoutGateModal() {
  const { gateOpen, startGateAuth, chooseGuest, cancelGate } = useAuth();
  if (!gateOpen) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-start justify-center overflow-y-auto bg-black/60 p-4">
      <div className="my-12 w-full max-w-md rounded-lg bg-white p-6 text-gray-900 shadow-xl" role="dialog" aria-modal="true" aria-labelledby="gate-title">
        <div className="mb-1 flex items-start justify-between">
          <h2 id="gate-title" className="text-xl font-bold">How would you like to check out?</h2>
          <button type="button" onClick={cancelGate} aria-label="Close" className="text-2xl leading-none text-gray-400 hover:text-black">
            ×
          </button>
        </div>
        <p className="mb-4 text-sm text-gray-600">Your cart is saved. Pick one to continue.</p>

        <div className="space-y-3">
          <button
            type="button"
            onClick={() => startGateAuth("signin")}
            className="w-full rounded bg-[#06bde7] px-4 py-3 text-left text-[#04202b] hover:brightness-110"
          >
            <span className="block text-sm font-bold">Sign in</span>
            <span className="block text-xs">I already have an account. Use my saved addresses.</span>
          </button>
          <button
            type="button"
            onClick={() => startGateAuth("signup")}
            className="w-full rounded border-2 border-[#06bde7] px-4 py-3 text-left hover:bg-[#06bde7]/10"
          >
            <span className="block text-sm font-bold">Create an account</span>
            <span className="block text-xs text-gray-600">Save your addresses, track orders and check out faster next time.</span>
          </button>
          <button
            type="button"
            onClick={chooseGuest}
            className="w-full rounded border border-gray-300 px-4 py-3 text-left hover:bg-gray-50"
          >
            <span className="block text-sm font-bold">Continue as guest</span>
            <span className="block text-xs text-gray-600">No account needed. You will enter your shipping address and payment at checkout.</span>
          </button>
        </div>
      </div>
    </div>
  );
}
