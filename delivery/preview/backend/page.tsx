import Link from 'next/link';
import Image from 'next/image';

const endpoints = [
  ['POST /api/v1/account', 'ورود OTP و سیستم معرفی'],
  ['GET /api/v1/account/credits', 'پروفایل، اعتبار و سهمیه رایگان'],
  ['POST /api/v1/diagnose', 'عیب‌یابی متنی با AI'],
  ['POST /api/v1/diagnose/audio', 'عیب‌یابی از صدای موتور'],
  ['GET /api/v1/diagnose?history=true', 'تاریخچه عیب‌یابی'],
  ['DELETE /api/v1/diagnose/:id', 'حذف یک رکورد تاریخچه'],
  ['GET /api/v1/garages/nearby', 'تعمیرگاه‌های نزدیک'],
  ['GET /api/v1/products', 'بسته‌های اعتبار و اشتراک'],
  ['POST /api/v1/purchase', 'ایجاد لینک پرداخت'],
  ['GET /api/v1/cars یا /cars.json', 'لیست خودروها'],
];

export default function Home() {
  return (
    <div className="page" dir="rtl">
      <header className="hero">
        <Image src="/logo.png" alt="مکانیک هوشمند" width={96} height={96} priority />
        <h1>مکانیک هوشمند</h1>
        <p className="slogan">عیب‌یابی هوشمند خودرو</p>
        <p className="lede">
          API اپلیکیشن فلاتر: احراز هویت OTP، عیب‌یابی AI، تحلیل صدا، نقشه تعمیرگاه،
          اعتبار و اشتراک طلایی.
        </p>
      </header>

      <section className="grid">
        <article className="card">
          <h2>نقاط پایانی (نسخه v1)</h2>
          <ul>
            {endpoints.map(([path, desc]) => (
              <li key={path}>
                <code>{path}</code>
                <span>{desc}</span>
              </li>
            ))}
          </ul>
        </article>

        <article className="card">
          <h2>وضعیت</h2>
          <p className="status">
            <span className="dot" /> آماده سرویس‌دهی
          </p>
          <div className="links">
            <Link href="/api/health">بررسی سلامت</Link>
            <Link href="/api/v1/health">سلامت v1</Link>
            <Link href="/api/v1/products">بسته‌ها</Link>
            <Link href="/admin">پنل مدیریت</Link>
          </div>
        </article>
      </section>
    </div>
  );
}
