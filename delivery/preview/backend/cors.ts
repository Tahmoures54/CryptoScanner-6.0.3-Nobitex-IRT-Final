import { NextRequest, NextResponse } from 'next/server';

export function allowedOrigins(): string[] {
  return (
    process.env.ALLOWED_ORIGINS ||
    'https://smart-mec.ir,https://www.smart-mec.ir,https://smart-mec-backend-zeta.vercel.app,http://localhost:3000'
  )
    .split(',')
    .map((o) => o.trim())
    .filter(Boolean);
}

export function applyCors(response: NextResponse, request: NextRequest): NextResponse {
  const origin = request.headers.get('origin');
  const allowed = allowedOrigins();

  if (origin && allowed.includes(origin)) {
    response.headers.set('Access-Control-Allow-Origin', origin);
    response.headers.set('Vary', 'Origin');
    response.headers.set('Access-Control-Allow-Credentials', 'true');
  }

  response.headers.set(
    'Access-Control-Allow-Methods',
    'GET, POST, PUT, PATCH, DELETE, OPTIONS'
  );
  response.headers.set(
    'Access-Control-Allow-Headers',
    'Authorization, Content-Type, Accept, Accept-Version, X-Api-Version'
  );
  response.headers.set('Access-Control-Max-Age', '86400');
  return response;
}

export function corsPreflight(request: NextRequest): NextResponse {
  return applyCors(new NextResponse(null, { status: 204 }), request);
}
