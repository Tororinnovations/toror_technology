const CACHE = 'toror-tech-v3';
const ASSETS = [
  '/register', '/login', '/portal', '/projects', '/chat', '/profile', '/manifest.webmanifest',
  '/static/css/style.css', '/static/js/app.js', '/static/default-logo.svg'
];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  event.respondWith(
    fetch(req).then(res => {
      const cloned = res.clone();
      caches.open(CACHE).then(cache => cache.put(req, cloned)).catch(() => {});
      return res;
    }).catch(() => caches.match(req).then(r => r || caches.match('/register')))
  );
});
