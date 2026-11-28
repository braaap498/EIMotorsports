import type { Metadata } from "next";
import PhoneScanPage from "../../../components/admin/inventory/PhoneScanPage";

// The phone page behind the QR code on the computer: keep it out of search engines.
export const metadata: Metadata = {
  title: "Scan",
  robots: { index: false, follow: false },
};

export default async function Page({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return <PhoneScanPage token={token} />;
}
