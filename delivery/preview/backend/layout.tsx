import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import './globals.css';

export const metadata: Metadata = {
  title: 'مکانیک هوشمند — Smart Mechanic API',
  description: 'بک‌اند عیب‌یابی هوشمند خودرو با هوش مصنوعی، OTP، پرداخت و نقشه تعمیرگاه‌ها',
  icons: { icon: '/logo.png' },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="fa" dir="rtl">
      <body>{children}</body>
    </html>
  );
}
