import { NextResponse } from 'next/server';

import { ensureDbReady } from '@/db';

export const dynamic = 'force-dynamic';

export async function GET() {
  let dbOk = false;
  try {
    await ensureDbReady();
    dbOk = true;
  } catch {
    dbOk = false;
  }

  return NextResponse.json({
    success: dbOk,
    status: dbOk ? 'ok' : 'degraded',
    service: 'smart-mec-backend',
    brand: 'مکانیک هوشمند',
    timestamp: new Date().toISOString(),
    db: dbOk,
  });
}
