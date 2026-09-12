import { NextResponse } from 'next/server';

import { PRODUCTS } from '@/types';

export async function GET() {
  return NextResponse.json({
    success: true,
    data: Object.values(PRODUCTS),
  });
}
