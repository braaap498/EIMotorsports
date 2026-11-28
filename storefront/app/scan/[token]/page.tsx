import { Suspense } from "react";
import type { Metadata } from "next";
import PhoneScanPage from "../../../components/admin/inventory/PhoneScanPage";

// The phone page behind the QR code on the computer: keep it out of search engines.
export const metadata: Metadata = {
  title: "Scan",
  robots: { index: false, follow: false },
};

// params is request-time data, so it is read inside a Suspense boundary (the page shell can still be prerendered).
async function ScanFromParams({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <PhoneScanPage token={token} />;
}

export default function Page({ params }: { params: Promise<{ token: string }> }) {
  return (
    <Suspense fallback={<p className="p-6 text-center text-gray-600">Loading...</p>}>
      <ScanFromParams params={params} />
    </Suspense>
  );
}
