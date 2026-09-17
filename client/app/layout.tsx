import type { Metadata } from "next";
import { Analytics } from "@vercel/analytics/next";
import "./globals.css";

export const metadata: Metadata = {
  title: 'GeoContextualize',
  description: 'Discover geographical context and insights by analyzing any area on Earth with advanced geospatial tools.',
  keywords: 'geography, geospatial, mapping, satellite imagery, geographic context, earth analysis',
  icons: {
    icon: '/icon8.png',
  }
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased">
        {children}
        <Analytics />
      </body>
    </html>
  );
}
