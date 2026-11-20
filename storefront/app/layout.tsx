import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";
import "./globals.css";
import { AuthProvider } from "../components/site/AuthProvider";
import AuthModal from "../components/site/AuthModal";
import CheckoutGateModal from "../components/site/CheckoutGateModal";
import { CartProvider } from "../components/site/CartProvider";
import SiteHeader from "../components/site/SiteHeader";
import SiteFooter from "../components/site/SiteFooter";
import { SITE_URL, robotsMeta } from "../lib/site";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  // Pages that set a title get " | E&I Motorsports" added; the home page uses the default.
  title: {
    default: "E&I Motorsports | ATV, UTV, Snowmobile, Dirt Bike & Motorcycle Parts",
    template: "%s | E&I Motorsports",
  },
  description: "Powersports parts that fit your ATV, UTV, snowmobile, dirt bike, motorcycle or PWC.",
  openGraph: { siteName: "E&I Motorsports", type: "website" },
  robots: robotsMeta(true),
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <AuthProvider>
          <CartProvider>
            {/* The header reads the current URL, so it needs a Suspense boundary. */}
            <Suspense fallback={<div className="h-40 bg-black" />}>
              <SiteHeader />
            </Suspense>
            <div className="flex-1">{children}</div>
            {/* Like the header, the footer reads the current URL, so it needs a Suspense boundary. */}
            <Suspense fallback={null}>
              <SiteFooter />
            </Suspense>
            <AuthModal />
            <CheckoutGateModal />
          </CartProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
