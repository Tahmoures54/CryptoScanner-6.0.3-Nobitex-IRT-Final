// ═══════════════════════════════════════════════════════════
// Middleware - Smart-MEC
// ═══════════════════════════════════════════════════════════

import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { jwtVerify } from 'jose';
import { applyCors, corsPreflight } from '@/lib/cors';

/** مسیرهای محافظت‌شده (با و بدون پیشوند /v1) */
const protectedPrefixes = [
  '/api/diagnose',
  '/api/v1/diagnose',
  '/api/purchase',
  '/api/v1/purchase',
  '/api/account/credits',
  '/api/v1/account/credits',
  '/api/account/withdraw',
  '/api/v1/account/withdraw',
  '/api/admin',
  '/api/v1/admin',
];

export async function middleware(request: NextRequest) {
  if (request.method === 'OPTIONS') {
    return corsPreflight(request);
  }

  const { pathname } = request.nextUrl;

  const isProtected = protectedPrefixes.some(
    (route) => pathname === route || pathname.startsWith(`${route}/`)
  );

  if (isProtected) {
    const authHeader = request.headers.get('authorization');

    if (!authHeader || !authHeader.startsWith('Bearer ')) {
      return applyCors(
        NextResponse.json(
          { success: false, error: 'توکن احراز هویت یافت نشد' },
          { status: 401 }
        ),
        request
      );
    }

    const token = authHeader.split(' ')[1];
    const systemToken = process.env.ADMIN_SYSTEM_TOKEN;

    if (systemToken && token === systemToken) {
      return applyCors(NextResponse.next(), request);
    }

    const secret = process.env.JWT_SECRET;
    if (!secret) {
      return applyCors(
        NextResponse.json(
          { success: false, error: 'پیکربندی سرور ناقص است' },
          { status: 500 }
        ),
        request
      );
    }

    try {
      await jwtVerify(token, new TextEncoder().encode(secret));
      return applyCors(NextResponse.next(), request);
    } catch {
      return applyCors(
        NextResponse.json(
          { success: false, error: 'توکن نامعتبر یا منقضی شده است' },
          { status: 401 }
        ),
        request
      );
    }
  }

  return applyCors(NextResponse.next(), request);
}

export const config = {
  matcher: ['/api/:path*'],
};
