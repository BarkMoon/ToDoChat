// ToDoChat サービスワーカー。
// 目的は「ホーム画面追加(インストール)要件を満たすこと」と「アプリシェルを
// キャッシュして電波が弱い/オフラインでも起動画面が出ること」。会話や設定など
// の API(/api/*)は絶対にキャッシュせず常にネットワークへ通す。
const CACHE = 'todochat-v1';
const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  // 旧バージョンのキャッシュを掃除して即座に制御を握る。
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                 // POST 等(API 変更系)は素通し
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;  // 外部オリジンは触らない
  if (url.pathname.startsWith('/api/')) return;     // API は常にネットワーク

  if (req.mode === 'navigate') {
    // アプリシェルはネットワーク優先。更新を確実に取り込みつつ、失敗時のみ
    // キャッシュにフォールバックしてオフラインでも起動画面を出す。
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put('/', copy));
          return res;
        })
        .catch(() => caches.match('/').then((r) => r || caches.match('/index.html')))
    );
    return;
  }

  // アイコン等の静的資産はキャッシュ優先(高速・オフライン耐性)。
  e.respondWith(caches.match(req).then((r) => r || fetch(req)));
});
