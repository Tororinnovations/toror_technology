const CACHE = 'toror-tech-v6';
const ASSETS = [
  '/', '/about', '/services', '/work', '/faq', '/contact', '/privacy', '/terms', '/verify',
  '/manifest.webmanifest', '/qr/home.png', '/static/css/style.css?v=6', '/static/js/app.js', '/static/default-logo.svg'
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
  if (new URL(req.url).pathname.startsWith('/admin') || new URL(req.url).pathname.startsWith('/promise212324')) return;
  event.respondWith(
    fetch(req).then(res => {
      const cloned = res.clone();
      caches.open(CACHE).then(cache => cache.put(req, cloned)).catch(() => {});
      return res;
    }).catch(() => caches.match(req).then(r => r || caches.match('/')))
  );
});
