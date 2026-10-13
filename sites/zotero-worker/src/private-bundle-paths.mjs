/** Deny private deployment bytes before every public/static/framework fallback.
 * Archive query strings remain untouched. Requests containing ambiguous path
 * encodings are conservatively rejected rather than delegated to normalization. */
export const PRIVATE_BUNDLE_PREFIX = '/__zotero_private_bundles/';
export function denyPrivateBundleRequest(request) {
  const path = new URL(request.url).pathname;
  return /[%\\\u0000-\u0020\u007f]/.test(path) || path.includes('//')
    || path.toLowerCase().includes(PRIVATE_BUNDLE_PREFIX.slice(1, -1));
}
export function privateBundleDeniedResponse() {
  return new Response('Not found', { status: 404, headers: { 'Cache-Control': 'no-store, private',
    'Content-Type': 'text/plain; charset=utf-8', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer' } });
}
