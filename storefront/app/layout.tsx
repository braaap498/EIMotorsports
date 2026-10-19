import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { Suspense } from "react";
import "./globals.css";
import { AuthProvider } from "../components/site/AuthProvider";
import AuthModal from "../components/site/AuthModal";
import CheckoutGateModal from "../components/site/CheckoutGateModal";
import SiteHeader from "../components/site/SiteHeader";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "E&I Motorsports",
  description: "Powersports parts that fit your ATV, UTV, snowmobile, dirt bike, motorcycle or PWC.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col">
        <AuthProvider>
          {/* The header reads the current URL, so it needs a Suspense boundary. */}
          <Suspense fallback={<div className="h-40 bg-black" />}>
            <SiteHeader />
          </Suspense>
          <div className="flex-1">{children}</div>
          <AuthModal />
          <CheckoutGateModal />
        </AuthProvider>
      </body>
    </html>
  );
}
