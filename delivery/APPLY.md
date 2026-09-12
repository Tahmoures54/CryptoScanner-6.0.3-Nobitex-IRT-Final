# مکانیک هوشمند — بستهٔ تحویل

این ایجنت اشتباهاً روی ریپوی **CryptoScanner** اجرا شد، در حالی که کار روی این دو ریپو بود:

- https://github.com/Tahmoures54/smart-mechanic-flutter
- https://github.com/Tahmoures54/smart-mec-backend

توکن این اجرا اجازهٔ push به آن ریپوها را ندارد. تغییرات کامل روی شاخه‌های محلی ساخته شده و به‌صورت پچ اینجاست.

**این PR را داخل CryptoScanner مرج نکنید.** فقط پچ‌ها را روی ریپوهای مکانیک هوشمند اعمال کنید.

## اعمال پچ فلاتر

```bash
git clone https://github.com/Tahmoures54/smart-mechanic-flutter.git
cd smart-mechanic-flutter
git checkout -b cursor/apply-branding-and-api-sync-c744
git am /path/to/smart-mechanic-flutter.patch
git push -u origin cursor/apply-branding-and-api-sync-c744
```

## اعمال پچ بک‌اند

```bash
git clone https://github.com/Tahmoures54/smart-mec-backend.git
cd smart-mec-backend
git checkout -b cursor/branding-api-alignment-c744
git am /path/to/smart-mec-backend.patch
git push -u origin cursor/branding-api-alignment-c744
```

## چه چیزی عوض شد

### برندینگ
لوگوی ریشه (`logo.png` — قفل طلایی + آچار) روی اسپلش، آیکون لانچر اندروید، صفحه ورود، AppBar و لندینگ بک‌اند اعمال شد.

### هماهنگی API
- کاتالوگ خودرو: ۲۴۳ مدل مشترک بین اپ و `diagnose`
- بسته‌های فروشگاه با `GET /api/v1/products` یکی شد
- تاریخچه: `page` + `offset`
- حذف تاریخچه: `DELETE /api/v1/diagnose/:id`
- تعمیرگاه نزدیک: صفحه جدید روی `/garages/nearby`
- اعتبارسنجی سال/توضیح با اپ یکی شد
- CORS چنددامنه‌ای + OPTIONS
- صفحه سلامت با بررسی دیتابیس

برای اجرای دوبارهٔ همین کار، ایجنت را روی خود ریپوی فلاتر یا بک‌اند بسازید تا PR مستقیم همان‌جا باز شود.
