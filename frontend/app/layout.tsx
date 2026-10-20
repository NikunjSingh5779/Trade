import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Trade — Personal AI Trading Agent",
  description: "Local-first AI trading research and paper-trading dashboard."
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
