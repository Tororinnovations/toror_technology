const CACHE = 'toror-tech-v10';
const ASSETS = [
  '/', '/about', '/services', '/work', '/faq', '/contact', '/privacy', '/terms', '/verify',
  '/manifest.webmanifest', '/static/css/style.css?v=10', '/static/js/app.js?v=10', '/static/default-logo.svg'
];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;
  // Never cache admin pages, private downloads, order/access links, API responses or APK files.
  if (path.startsWith('/admin') || path.startsWith('/promise212324') || path.startsWith('/api/') ||
      path.startsWith('/store/download/') || path.startsWith('/store/access/') || path.endsWith('.apk')) return;
  // Only cache known public shell assets; do not cache arbitrary pages or user-specific responses.
  if (!path.startsWith('/static/') && !['/', '/about', '/services', '/work', '/faq', '/contact', '/privacy', '/terms', '/verify', '/store'].includes(path)) return;
  event.respondWith(fetch(req).then(res => {
    if (res.ok && res.type === 'basic' && !res.headers.get('Cache-Control')?.includes('no-store')) {
      const copy = res.clone(); caches.open(CACHE).then(cache => cache.put(req, copy)).catch(() => {});
    }
    return res;
  }).catch(() => caches.match(req).then(r => r || caches.match('/'))));
});
