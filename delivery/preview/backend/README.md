# مکانیک هوشمند — بک‌اند

API اپلیکیشن [smart-mechanic-flutter](https://github.com/Tahmoures54/smart-mechanic-flutter).

- هویت: **مکانیک هوشمند** / Smart Mechanic
- شعار: عیب‌یابی هوشمند خودرو
- نسخهٔ عمومی API: `/api/v1/*` (با rewrite به `/api/*`)
- استقرار فعلی: `https://smart-mec-backend-zeta.vercel.app`

## اجرای محلی

```bash
cp .env.example .env
npm install
npm run dev
```

`SHOW_OTP_IN_DEV=true` کد OTP را در پاسخ `send` برمی‌گرداند (فقط توسعه).

## هماهنگی با اپ فلاتر

| کلاینت | سرور |
|--------|------|
| `API_BASE_URL/.../v1` | rewrite `/api/v1/:path*` → `/api/:path*` |
| `POST /account` action send/verify | `/api/account` |
| `POST /diagnose` | carId + year + description |
| `POST /diagnose/audio` | multipart `audio` + carId + year |
| `GET /diagnose?history=true&page=&limit=` | offset از page محاسبه می‌شود |
| `DELETE /diagnose/:id` | حذف رکورد همان کاربر |
| `GET /garages/nearby?lat=&lng=` | تعمیرگاه‌های دیتابیس خودمان |
| `GET /products` | همان `PRODUCTS` فروشگاه |
| `/cars.json` و `GET /api/v1/cars` | یک کاتالوگ ۲۴۳ خودرو |

## اسکریپت‌ها

```bash
npm run lint
npm run typecheck
npm run db:push
```
