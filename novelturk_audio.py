# -*- coding: utf-8 -*-
"""
Evrensel Novel -> TXT -> MP3 (sesli kitap)      GitHub Actions / Google Colab / yerel makine

Herhangi bir roman sitesinde ilk bölümün (veya seri sayfasının) linkinden başlar, "Sonraki"
bağlantısını takip ederek bölümleri çeker, TXT olarak kaydeder ve edge-tts ile MP3'e çevirir.

Komutlar
  scrape     Bölümleri çekip TXT kaydeder                    txt/<seri>/0001_Bolum_1.txt
  tts        TXT dosyalarını edge-tts ile MP3'e çevirir      mp3/<seri>/0001_Bolum_1.mp3
  translate  TXT bölümlerini başka dile çevirir              txt_en/<seri>/0001_Bolum_1.txt
  epub       TXT bölümlerinden tek bir e-kitap (.epub) üretir epub/<seri>.epub
  all        scrape + (istenirse translate) + --format'ta yazılanlar (varsayılan: mp3)
  plan       Paralel iş planı üretir (GitHub Actions matrix)
  probe      Bir sayfada hangi motorun çalıştığını, metnin ve "sonraki" linkin nasıl bulunduğunu gösterir
  voices     Kullanılabilir edge-tts seslerini listeler

Örnekler
  python novelturk_audio.py probe  --url https://site.com/novel/xyz/bolum-1/     # yeni sitede ÖNCE bunu deneyin
  python novelturk_audio.py scrape --url https://site.com/novel/xyz/bolum-1/ --max-chapters 10
  python novelturk_audio.py all    --url https://site.com/novel/xyz/ --max-chapters 0 --voice tr-TR-EmelNeural
  python novelturk_audio.py all    --url https://site.com/novel/xyz/ --format mp3,epub --translate-to en
  python novelturk_audio.py translate --slug xyz --translate-to en --translate-engine google
  python novelturk_audio.py epub   --slug xyz --epub-title "Kitap Adı" --epub-author "Yazar"
  python novelturk_audio.py tts    --slug xyz --start 101 --end 200

Mimari (dosya içindeki bölümler)
  1) Fetcher        curl_cffi (tarayıcı TLS parmak izi) ve Playwright (gerçek tarayıcı) motorları;
                    rastgele bekleme, User-Agent/parmak izi rotasyonu, 3 yeniden deneme, otomatik yükseltme
  2) Extractor      trafilatura (ana) + BeautifulSoup metin yoğunluğu (yedek)
  3) Navigator      "Sonraki" bağlantısı: rel=next, metin (Sonraki/Next/İleri/>/>>), class ve URL sayı artışı
  4) SeriesStore    txt/ dosyaları + _state.json (kaldığı yerden devam)
  5) Scraper        sıralı gezinti döngüsü
  6) TTS            edge-tts (parçalama, zaman aşımı, yeniden deneme, atla-varsa, eşzamanlı)
  7) Translate       deep-translator/Google (ücretsiz, hızlı) veya Argos Translate (tamamen çevrimdışı)
  8) EPUB           ebooklib ile bölümlerden tek e-kitap dosyası

Ortam değişkenleri (hepsi isteğe bağlı)
  NOVEL_URLS  NOVEL_NAME  NOVEL_MAX_CHAPTERS  NOVEL_FETCHER (auto|curl|playwright)
  NOVEL_PROXY  http://kullanici:sifre@host:port[,http://ikinci:proxy@host:port,...] veya @proxies.txt
              (veri merkezi IP'si engelliyse; virgülle birden çok verilirse siteye göre rotasyonlanır)
  NOVEL_SITE_CONFIG  site_profiles.json yolu (bkz. --site-config; sorunlu sitelere kalıcı ayar)
  NOVEL_FORMAT (mp3,epub)  NOVEL_TRANSLATE_TO (ör. en)  NOVEL_TRANSLATE_ENGINE (google|argos)

Sorunlu bir sitede kalıcı düzeltme (site_profiles.json):
  {
    "sorunlusite.com": {
      "selector": "#reading-content, .chapter-content",   // probe ile bulunan doğru kap (CSS seçici)
      "noise": ["sorunlusite\\.com", "reklami bildir"],     // bu kalıplarla eşleşen satırlar atılır
      "guess": false,                                        // 'sonraki' linki yoksa numara tahmini KAPALI
      "min_chars": 150                                       // bu sitede bölüm sayılmak için en az karakter
    }
  }
  --site-config site_profiles.json ile verilir; her alan isteğe bağlıdır.
"""
from __future__ import annotations

import sys, os, re, json, math, time, random, asyncio, hashlib, logging, argparse
import importlib, importlib.util, subprocess, threading, unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


def _import(module, pip_name=None):
    """Kütüphane yoksa pip ile kurup içe aktarır (Colab / ilk çalıştırma için)."""
    try:
        return importlib.import_module(module)
    except ImportError:
        if os.getenv("NOVEL_NO_AUTOINSTALL"):
            raise
        cmd = [sys.executable, "-m", "pip", "install", "-q", pip_name or module]
        try:
            subprocess.check_call(cmd)
        except subprocess.CalledProcessError:
            subprocess.check_call(cmd + ["--break-system-packages"])
        importlib.invalidate_caches()
        return importlib.import_module(module)


_import("bs4", "beautifulsoup4")
_import("lxml")
from bs4 import BeautifulSoup, NavigableString, Comment  # noqa: E402

log = logging.getLogger("novel_audio")

# ======================================================================================
# AYARLAR  (komut satırı ve ortam değişkenleri bunların önüne geçer)
# ======================================================================================
START_URLS = []            # Colab için: ["https://site.com/novel/xyz/bolum-1/"]
MAX_CHAPTERS = 5           # 0 = tüm seri. Yeni sitede önce küçük bir sayıyla deneyin.
DELAY = (1.5, 4.0)         # istekler arası rastgele bekleme (sn)
RETRIES = 3                # başarısız istekte yeniden deneme sayısı
MIN_CHARS = 200            # bir sayfa "bölüm" sayılması için gereken en az karakter
DEFAULT_VOICE = "tr-TR-AhmetNeural"
TTS_RETRIES = 5
BASE_DIR = Path("/content") if Path("/content").exists() else Path(".")

LANG = "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
USER_AGENTS = [   # Playwright için; Chrome sürümü çalışan Chromium'a göre otomatik uyarlanır
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
]


# ======================================================================================
# ORTAK YARDIMCILAR
# ======================================================================================
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad\u202a-\u202e]")
_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g",
                       "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u"})
_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def _env_bool(name):
    return (os.getenv(name, "") or "").strip().lower() in ("1", "true", "yes", "evet", "on")


def fold(s):
    """Büyük/küçük harf ve Türkçe karakter duyarsız karşılaştırma için: 'SONRAKİ Bölüm' -> 'sonraki bolum'."""
    return (s or "").translate(_FOLD).lower()


def clean_line(text):
    """Unicode normalize eder (süslü harfleri düzler), görünmez karakterleri atar, boşlukları sadeleştirir."""
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def slugify(text, max_len=50):
    text = unicodedata.normalize("NFKD", (text or "").translate(_TR_MAP)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")[:max_len].strip("_")


def _as_list(v):
    return v if isinstance(v, list) else ([v] if v else [])


def _host(u):
    return urlsplit(u).netloc.lower().removeprefix("www.")


def norm_url(u):
    """Karşılaştırma anahtarı: şema, www, sondaki '/' ve #parça yok sayılır."""
    p = urlsplit(u)
    return f"{_host(u)}{p.path.rstrip('/') or '/'}{'?' + p.query if p.query else ''}"


def warn(msg):
    log.warning(msg)
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::warning::{msg}", flush=True)


def in_clean_thread(fn, *a):
    """Jupyter/Colab'da çalışan bir asyncio döngüsü varsa (Playwright sync ve asyncio.run çakışır) ayrı iş parçacığında çalıştırır."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*a)
    box = {}

    def worker():
        try:
            box["r"] = fn(*a)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    if "e" in box:
        raise box["e"]
    return box["r"]


def progress(**kw):
    """tqdm varsa ilerleme çubuğu; yoksa (veya TTY yoksa) sessiz."""
    try:
        return _import("tqdm.auto", "tqdm").tqdm(disable=None, **kw)
    except Exception:  # noqa: BLE001
        class _Dummy:
            def update(self, *_): pass
            def close(self): pass
        return _Dummy()


# ======================================================================================
# 1) FETCHER  — HTTP motorları (curl_cffi / Playwright) + hız sınırı + yeniden deneme
# ======================================================================================
def parse_proxies(spec):
    """'--proxy' değerini liste haline getirir: virgülle ayrılmış birden çok proxy, ya da '@dosya.txt'
    (satır satır proxy; '#' ile başlayan satırlar yok sayılır). Tek proxy verilirse tek elemanlı liste döner."""
    spec = (spec or "").strip()
    if not spec:
        return []
    if spec.startswith("@"):
        path = Path(spec[1:])
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            warn(f"Proxy dosyası okunamadı ({path}): {e}")
            return []
        return [x.strip() for x in lines if x.strip() and not x.strip().startswith("#")]
    return [x.strip() for x in spec.split(",") if x.strip()]


class FetchError(Exception):
    def __init__(self, msg, status=None, permanent=False, fatal=False, wait=0):
        super().__init__(msg)
        self.status = status
        self.permanent = permanent   # 404/410: tekrar denemek anlamsız
        self.fatal = fatal           # motor kullanılamıyor (kurulu değil vb.)
        self.wait = wait             # Retry-After (sn)


_CHALLENGE = ("just a moment", "cf-chl", "challenge-platform", "attention required", "cf_chl_opt",
              "checking your browser", "enable javascript and cookies", "ddos protection")


def is_challenge(status, headers, text):
    """Cloudflare / benzeri bot doğrulama ekranı mı?"""
    if (headers or {}).get("cf-mitigated") == "challenge":
        return True
    head = (text or "")[:6000].lower()
    if "<title>just a moment" in head or "<title>attention required" in head or "cf_chl_opt" in head:
        return True
    # 'challenge-platform' normal Cloudflare sayfalarında da bulunur; yalnızca hata koduyla birlikte say
    return status in (403, 429, 503) and any(m in head for m in _CHALLENGE)


def decode_html(raw, content_type=""):
    """Baytları doğru kodlamayla metne çevirir (UTF-8, bildirilen charset, eski Türkçe siteler için cp1254)."""
    cands = ["utf-8"]
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        cands.append(m.group(1))
    m = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", raw[:4096], re.I)
    if m:
        cands.append(m.group(1).decode("ascii", "ignore"))
    cands.append("cp1254")
    for enc in cands:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


class CurlFetcher:
    """curl_cffi: gerçek tarayıcı TLS/HTTP2 parmak izini taklit eder. Hızlıdır, JavaScript çalıştırmaz.
    User-Agent rotasyonu = tarayıcı profili rotasyonu: her profil kendi UA/sec-ch-ua başlıklarıyla
    gelir; elle yazılmış UA, TLS parmak iziyle çeliştiği için engellenme ihtimalini artırır."""
    name = "curl"
    PREFERRED = ["chrome136", "chrome133a", "chrome131", "chrome124", "chrome120", "chrome119",
                 "chrome116", "chrome110", "edge101", "safari18_0", "safari17_0", "firefox135", "firefox133"]

    def __init__(self, proxies=None):
        _import("curl_cffi")
        from curl_cffi import requests as cffi
        self.cffi = cffi
        self.proxies = list(proxies or [])
        self.proxy_i = random.randrange(len(self.proxies)) if self.proxies else 0
        try:
            from curl_cffi.requests import BrowserType
            have = {b.value for b in BrowserType}
        except Exception:  # noqa: BLE001
            have = set()
        self.profiles = [p for p in self.PREFERRED if p in have] or ["chrome120", "chrome110", "chrome"]
        self.i = random.randrange(len(self.profiles))
        self.s = None
        self._new()

    @property
    def proxy(self):
        return self.proxies[self.proxy_i % len(self.proxies)] if self.proxies else None

    def _new(self):
        proxy = self.proxy
        proxies = {"http": proxy, "https": proxy} if proxy else None
        for _ in range(len(self.profiles)):
            prof = self.profiles[self.i % len(self.profiles)]
            try:
                self.s = self.cffi.Session(impersonate=prof, proxies=proxies)
                self.profile, self._warm, self._left = prof, set(), random.randint(15, 30)
                return
            except Exception:  # noqa: BLE001
                self.i += 1
        self.s = self.cffi.Session(impersonate="chrome", proxies=proxies)
        self.profile, self._warm, self._left = "chrome", set(), 20

    def rotate(self):
        self.i += 1
        if self.proxies:      # birden çok proxy varsa parmak iziyle birlikte proxy'yi de değiştir
            self.proxy_i += 1
        self._new()

    def _warm_up(self, url):
        """Gerçek kullanıcı gibi önce ana sayfaya girip çerezleri al."""
        p = urlsplit(url)
        self._warm.add(p.netloc)
        try:
            self.s.get(f"{p.scheme}://{p.netloc}/", timeout=20, headers={"Accept-Language": LANG})
            time.sleep(random.uniform(0.5, 1.2))
        except Exception:  # noqa: BLE001
            pass

    def fetch(self, url, referer=None):
        self._left -= 1
        if self._left <= 0:          # periyodik rotasyon: yeni profil + temiz oturum
            self.rotate()
        if urlsplit(url).netloc not in self._warm:
            self._warm_up(url)
        headers = {"Accept-Language": LANG}
        if referer:
            headers["Referer"] = referer
            same = _host(url) == _host(referer)
            headers["Sec-Fetch-Site"] = "same-origin" if same else "cross-site"
        try:
            r = self.s.get(url, headers=headers, timeout=30, allow_redirects=True)
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"{type(e).__name__}: {str(e)[:150]}") from e
        text = decode_html(r.content, r.headers.get("content-type", ""))
        st = r.status_code
        if is_challenge(st, r.headers, text):
            raise FetchError(f"bot koruması (HTTP {st}) [{self.profile}]", st)
        if st in (404, 410):
            raise FetchError(f"HTTP {st}", st, permanent=True)
        if st >= 400:
            ra = r.headers.get("retry-after", "")
            raise FetchError(f"HTTP {st} [{self.profile}]", st, wait=min(int(ra), 60) if ra.isdigit() else 0)
        return text

    def close(self):
        pass


def _pw_proxy(proxy):
    p = urlsplit(proxy)
    d = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        d.update(username=p.username, password=p.password or "")
    return d


def _block_heavy(route):
    """Görsel/medya/font yüklemesini kes: metin için gereksiz, sayfayı hızlandırır."""
    if route.request.resource_type in ("image", "media", "font"):
        route.abort()
    else:
        route.continue_()


class PlaywrightFetcher:
    """Playwright (Chromium): JavaScript ile yüklenen (React/Vue) ve Cloudflare doğrulamalı siteler için.
    Yavaştır; 'auto' modda yalnızca curl yetmediğinde devreye girer.
    Kurulum: pip install playwright && python -m playwright install chromium"""
    name = "playwright"

    _STEALTH_JS = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['tr-TR', 'tr', 'en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        window.chrome = window.chrome || {runtime: {}};
        const _q = window.navigator.permissions && window.navigator.permissions.query;
        if (_q) {
            window.navigator.permissions.query = (p) => (
                p && p.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : _q(p)
            );
        }
    """

    def __init__(self, proxies=None):
        self.proxies = list(proxies or [])
        self.proxy_i = random.randrange(len(self.proxies)) if self.proxies else 0
        self._pw = self._browser = self._ctx = None

    @property
    def proxy(self):
        return self.proxies[self.proxy_i % len(self.proxies)] if self.proxies else None

    def _launch_browser(self):
        if self._browser:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        opts = {"headless": True, "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"]}
        if self.proxy:
            opts["proxy"] = _pw_proxy(self.proxy)
        self._browser = self._pw.chromium.launch(**opts)

    def _start(self):
        if self._ctx:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise FetchError("Playwright kurulu değil: pip install playwright && "
                             "python -m playwright install chromium", fatal=True)
        try:
            if not self._pw:
                self._pw = sync_playwright().start()
            if not self._browser:
                self._launch_browser()
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"Playwright başlatılamadı ({str(e)[:120]}). "
                             "'python -m playwright install chromium' çalıştırın.", fatal=True)
        self._new_context()

    def _new_context(self):
        if self._ctx:
            try:
                self._ctx.close()
            except Exception:  # noqa: BLE001
                pass
        major = (self._browser.version or "136").split(".")[0]
        ua = re.sub(r"Chrome/\d+", f"Chrome/{major}", random.choice(USER_AGENTS))
        self._ctx = self._browser.new_context(
            user_agent=ua, locale="tr-TR", extra_http_headers={"Accept-Language": LANG},
            viewport={"width": random.choice([1366, 1440, 1536, 1920]), "height": random.choice([768, 864, 900, 1080])})
        self._ctx.add_init_script(self._STEALTH_JS)
        self._ctx.route("**/*", _block_heavy)

    def rotate(self):
        if not self._browser:
            return
        if len(self.proxies) > 1:        # birden çok proxy varsa tarayıcıyı yeni proxy ile yeniden başlat
            self.proxy_i += 1
            self._launch_browser()
        self._new_context()      # yeni UA + temiz çerezler

    def fetch(self, url, referer=None):
        self._start()
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000, referer=referer)
            for _ in range(20):      # Cloudflare "Just a moment" ekranı genelde birkaç sn'de kendiliğinden geçer
                title = (page.title() or "").lower()
                if not any(k in title for k in ("just a moment", "attention required", "bir dakika")):
                    break
                page.wait_for_timeout(1000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            try:                     # tembel yüklenen içerik için sayfayı sonuna kaydır
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(600)
            except Exception:  # noqa: BLE001
                pass
            html, status = page.content(), (resp.status if resp else 0)
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"{type(e).__name__}: {str(e)[:150]}") from e
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass
        if status in (404, 410):
            raise FetchError(f"HTTP {status}", status, permanent=True)
        if is_challenge(200, {}, html):
            raise FetchError("bot doğrulaması aşılamadı [playwright]", status)
        return html

    def close(self):
        for obj in (self._ctx, self._browser):
            try:
                obj and obj.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._pw and self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = self._ctx = None


class Fetcher:
    """Motorları yöneten üst katman: hız sınırı, yeniden deneme (RETRIES kez), otomatik yükseltme.
    mode: 'curl' | 'playwright' | 'auto' (önce curl, engel/boş sayfa olursa tarayıcı)."""

    HOST_PENALTY_STEP = 8.0     # her bot-koruması/429 sonrası bu siteye eklenen ek bekleme (sn)
    HOST_PENALTY_MAX = 45.0

    def __init__(self, mode="auto", delay=DELAY, retries=RETRIES, proxy=None):
        self.mode, self.delay, self.retries = mode, delay, retries
        self.proxies = proxy if isinstance(proxy, list) else parse_proxies(proxy)
        self.prefer_browser = mode == "playwright"
        self.last_engine = None
        self._engines, self._last = {}, 0.0
        self._host_penalty = {}     # host -> ek bekleme (sn); engellenen sitede otomatik yavaşlama

    @property
    def can_render(self):
        return self.mode in ("auto", "playwright") and importlib.util.find_spec("playwright") is not None

    def _engine(self, name):
        if name not in self._engines:
            self._engines[name] = CurlFetcher(self.proxies) if name == "curl" else PlaywrightFetcher(self.proxies)
        return self._engines[name]

    def _throttle(self, host=None):
        gap = random.uniform(*self.delay) + self._host_penalty.get(host, 0.0)
        wait = self._last + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _penalize(self, host, e):
        """Bot koruması/hız sınırı görülen siteyi kalıcı olarak (bu çalıştırma boyunca) yavaşlatır."""
        if e.status in (403, 429, 503):
            self._host_penalty[host] = min(self.HOST_PENALTY_MAX, self._host_penalty.get(host, 0.0) + self.HOST_PENALTY_STEP)

    def _relax(self, host):
        cur = self._host_penalty.get(host)
        if cur:
            self._host_penalty[host] = cur / 2 if cur > 1.0 else 0.0     # başarılı istekte cezayı yarıya indir

    def _run(self, name, url, referer):
        host, eng, last = _host(url), self._engine(name), None
        for attempt in range(self.retries + 1):
            if attempt == 0:
                self._throttle(host)
            try:
                html = eng.fetch(url, referer)
                self._last, self.last_engine = time.monotonic(), name
                self._relax(host)
                return html
            except FetchError as e:
                self._last = time.monotonic()
                self._penalize(host, e)
                if e.permanent or e.fatal:
                    raise
                last = e
                log.debug("%s deneme %d/%d başarısız: %s", name, attempt + 1, self.retries + 1, e)
                if attempt < self.retries:
                    eng.rotate()     # farklı parmak izi / UA (ve varsa proxy) ile tekrar dene
                    time.sleep(max(min(2 ** (attempt + 1), 20) + random.random(), e.wait))
        raise FetchError(f"{url} alınamadı ({self.retries + 1} deneme, {name}): {last}", last.status if last else None)

    def fetch(self, url, referer=None):
        if self.mode == "curl":
            order = ["curl"]
        elif self.mode == "playwright" or self.prefer_browser:
            order = ["playwright"]
        else:
            order = ["curl", "playwright"]
        first = None
        for name in order:
            try:
                html = self._run(name, url, referer)
            except FetchError as e:
                if e.permanent:
                    raise
                if e.fatal and first:      # tarayıcı yedeği de yok: asıl hatayı tarayıcı ipucuyla bildir
                    raise FetchError(f"{first} | tarayıcı yedeği kullanılamadı: {e}", first.status)
                first = first or e
                continue
            if name == "playwright" and len(order) > 1:
                self.prefer_browser = True
                log.info("Curl engellendi; bu siteden sonra tarayıcı (Playwright) kullanılacak.")
            return html
        raise first

    def render(self, url, referer=None):
        """Sayfayı doğrudan tarayıcıda (JavaScript çalıştırarak) getirir."""
        return self._run("playwright", url, referer)

    def close(self):
        for e in self._engines.values():
            e.close()


# ======================================================================================
# 2) EXTRACTOR  — trafilatura (ana) + BeautifulSoup metin yoğunluğu (yedek)
# ======================================================================================
_CHAP_WORD = r"(?:bolum|chapter|chap|ch|episode|ep|bab|kisim|part)"
_CHAP_TXT = re.compile(rf"(?<![a-z]){_CHAP_WORD}(?![a-z])\W*\d+")
_CHAP_URL = re.compile(rf"(?<![a-z]){_CHAP_WORD}(?![a-z])[-_/ .]*(\d+)")

_DROP_TAGS = ("script", "style", "noscript", "iframe", "svg", "nav", "header", "footer",
              "aside", "button", "select", "ins", "template", "canvas", "video", "audio")
_BOILER = {"comments", "comment", "comment-list", "comment-respond", "respond", "share", "sharedaddy",
           "social", "related", "related-posts", "sidebar", "widget", "advertisement", "adsbygoogle",
           "ads", "ad", "breadcrumb", "breadcrumbs", "cookie", "popup", "modal", "navigation", "nav",
           "menu", "pagination", "chapter-nav", "nextprev", "nav-links", "footer", "header"}
_BLOCK = {"p", "div", "section", "article", "main", "center", "blockquote", "ul", "ol", "li", "table",
          "tr", "pre", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt"}
_NAV_LINE = re.compile(
    r"^[\W_]*(?:(?:bir )?(?:sonraki|onceki|ileri|geri|next|prev|previous|back)"
    r"(?: (?:bolum|chapter|chap|ch|part|sayfa|page))?|bolum listesi|chapter list|index|"
    r"table of contents|icindekiler|ana sayfa|home|paylas|share|report|hata bildir|"
    r"yorumlar?|comments?|reklam|advertisement)[\W_]*$")


@dataclass
class Extracted:
    title: str
    lines: list
    method: str
    min_chars: int = MIN_CHARS
    back: bool = False      # sayfa bir önceki bölüme geri bağlantı veriyor mu (tahmin doğrulaması için)

    @property
    def chars(self):
        return sum(len(x) for x in self.lines)

    @property
    def ok(self):
        return self.chars >= self.min_chars


def page_title(soup):
    """Bölüm başlığı: 'bölüm/chapter N' içeren aday (h1, og:title, <title>) tercih edilir."""
    cands = []
    if soup.find("h1"):
        cands.append(soup.find("h1").get_text(" ", strip=True))
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        cands.append(og["content"])
    if soup.title and soup.title.get_text(strip=True):
        cands.append(soup.title.get_text(strip=True))
    # "Bölüm 5 | Site Adı" -> "Bölüm 5"
    cands = [c for c in (clean_line(c.split("|")[0]) for c in cands if c) if c]
    for c in cands:
        if _CHAP_TXT.search(fold(c)):
            return c[:120]
    return cands[0][:120] if cands else ""


def _lines_of(node):
    """Bir HTML düğümünü blok/<br> sınırlarına göre metin satırlarına böler (satır içi etiketler bölünmez)."""
    lines, buf = [], []

    def flush():
        if buf:
            lines.append("".join(buf))
            buf.clear()

    def walk(n):
        for ch in n.children:
            if isinstance(ch, Comment):
                continue
            if isinstance(ch, NavigableString):
                buf.append(str(ch))
            elif ch.name in _DROP_TAGS:
                continue
            elif ch.name == "br":
                flush()
            elif ch.name in _BLOCK:
                flush()
                walk(ch)
                flush()
            else:
                walk(ch)

    walk(node)
    flush()
    return lines


def density_extract(soup):
    """Yedek yöntem (Readability tarzı): her metin bloğu kendi kabına puan verir, en yüksek puanlı,
    bağlantı yoğunluğu düşük kap ana içerik seçilir. <p> etiketi olmayan (<br> ile ayrılmış) sayfaları da çözer."""
    for t in soup(list(_DROP_TAGS)):
        t.extract()
    for f in soup.find_all("form"):      # arama/yorum formları; içeriği saran büyük formlara (ASP.NET) dokunma
        if len(f.get_text()) < 500:
            f.extract()
    for t in soup.find_all(True):
        if t.name in ("html", "body"):
            continue
        toks = {x.lower() for x in _as_list(t.get("class")) + _as_list(t.get("id"))}
        if toks & _BOILER:
            t.extract()
    scores, nodes = {}, {}

    def add(node, val):
        if node is None or node.name in ("[document]", "html"):
            return
        scores[id(node)] = scores.get(id(node), 0) + val
        nodes[id(node)] = node

    for el in soup.find_all(True):
        if el.name == "p":
            own = el.get_text("")
        elif el.name in ("div", "td", "section", "article", "main", "center", "body", "font"):
            own = "".join(str(c) for c in el.children if type(c) is NavigableString)
        else:
            continue
        text = clean_line(own)
        if len(text) < 25:
            continue
        val = 1 + text.count(",") + min(len(text) // 100, 3)
        if el.name == "p":
            add(el.parent, val)
            add(el.parent.parent if el.parent else None, val / 2)
        else:
            add(el, val)
            add(el.parent, val / 2)
    if not nodes:
        return []

    def link_density(n):
        return sum(len(a.get_text("")) for a in n.find_all("a")) / (len(n.get_text("")) or 1)

    best = max(nodes.values(), key=lambda n: scores[id(n)] * (1 - min(link_density(n), 1.0)))
    return _lines_of(best)


_TRAF = []


def _trafilatura():
    if not _TRAF:
        try:
            _TRAF.append(_import("trafilatura"))
        except Exception as e:  # noqa: BLE001
            warn(f"trafilatura kullanılamıyor ({e}); yalnızca yedek yöntem çalışacak.")
            _TRAF.append(None)
    return _TRAF[0]


def trafilatura_lines(html, url):
    traf = _trafilatura()
    if not traf:
        return []
    kw = dict(url=url, include_comments=False, include_tables=False, include_images=False,
              include_links=False, output_format="txt")
    for extra in ({"favor_recall": True}, {}):     # favor_recall eski sürümlerde yok
        try:
            return (traf.extract(html, **kw, **extra) or "").splitlines()
        except TypeError:
            continue
        except Exception as e:  # noqa: BLE001
            log.debug("trafilatura hata: %s", e)
            return []
    return []


def clean_lines(raw, title="", noise=()):
    """Satırları temizler: menü/gezinti metinleri, kullanıcı gürültü kalıpları, tekrarlar ve gövdedeki başlık atılır."""
    out, prev = [], None
    for ln in raw:
        ln = clean_line(ln)
        if not ln or not re.search(r"\w", ln):
            continue
        if len(ln) <= 40 and _NAV_LINE.match(fold(ln)):
            continue
        if any(rx.search(ln) for rx in noise):
            continue
        if ln == prev and len(ln) >= 40:     # yinelenen uzun satırları at; "Bum! Bum!" gibi kısa tekrarlar kalsın
            continue
        out.append(ln)
        prev = ln
    if out and title:                # TTS başlığı zaten okuyor; gövdede tekrar etmesin
        ft, f0 = fold(title), fold(out[0])
        if len(f0) >= 4 and (f0 == ft or (len(f0) < 80 and f0 in ft)):
            out.pop(0)
    return out


def selector_extract(soup, selector):
    """Site profilinde verilen CSS seçiciyle içerik kabını doğrudan hedefler (en güvenilir yöntem;
    'probe' ile bulunup site_profiles.json'a yazılan seçici için). Birden çok kap eşleşirse en uzun metinli seçilir."""
    if not selector:
        return []
    try:
        nodes = soup.select(selector)
    except Exception:  # noqa: BLE001 - geçersiz CSS seçici
        return []
    if not nodes:
        return []
    best = max(nodes, key=lambda n: len(n.get_text()))
    return _lines_of(best)


def extract_page(html, url, noise=(), min_chars=MIN_CHARS, selector=None):
    """Sırasıyla dener: (1) site profilindeki CSS seçici (varsa), (2) trafilatura, (3) yoğunluk tabanlı yedek."""
    soup = BeautifulSoup(html, "lxml")
    title = page_title(soup)
    lines, method = [], ""
    if selector:
        lines = clean_lines(selector_extract(soup, selector), title, noise)
        method = "seçici"
    if sum(map(len, lines)) < min_chars:
        traf = clean_lines(trafilatura_lines(html, url), title, noise)
        if sum(map(len, traf)) > sum(map(len, lines)):
            lines, method = traf, "trafilatura"
    if sum(map(len, lines)) < min_chars:
        alt = clean_lines(density_extract(soup), title, noise)
        if sum(map(len, alt)) > sum(map(len, lines)):
            lines, method = alt, "yoğunluk"
    return Extracted(title, lines, method, min_chars)


# ======================================================================================
# 3) NAVIGATOR  — "Sonraki bölüm" bulma (heuristic)
# ======================================================================================
_NEXT_SYM, _PREV_SYM = set(">»›→⟩▶►➡⇒⏩"), set("<«‹←⟨◀◄⬅⇐⏪")
_UNIT = r"(?: (?:bolum|chapter|chap|ch|part|sayfa|page|episode|ep))?"
_NEXT_RE = re.compile(rf"(?:bir )?(?:sonraki|ileri|next|forward){_UNIT}")
_PREV_RE = re.compile(rf"(?:bir )?(?:onceki|geri|prev|previous|back){_UNIT}")
_DENY_PATH = re.compile(r"(?:^|/)(?:category|tag|author|genres?|feed|comment-page-\d+|wp-admin|wp-login)(?:/|$)", re.I)
_FIRST_RE = re.compile(r"ilk bolum|first chapter|read first|start reading|okumaya basla|bastan oku|ilk bolumu oku")
MIN_NEXT_SCORE = 40


@dataclass
class Link:
    url: str
    score: int
    why: str


def link_kind(text):
    """Bağlantı metnini sınıflar -> ('next'|'prev'|None, puan). Büyük/küçük harf ve Türkçe karakter duyarsız."""
    t = re.sub(r"\s+", " ", fold(text)).strip()
    if not t or len(t) > 40:
        return None, 0
    core = t.strip("".join(_NEXT_SYM | _PREV_SYM) + " |.:-–—•·")
    if not core:                                        # yalnızca simge: ">", ">>", "»" ...
        chars = set(t) - {" ", "|"}
        if chars <= _NEXT_SYM:
            return "next", 40
        if chars <= _PREV_SYM:
            return "prev", 0
        return None, 0
    if _NEXT_RE.fullmatch(core):
        return "next", 60
    if _PREV_RE.fullmatch(core):
        return "prev", 0
    return None, 0


def chap_num(url):
    """URL'deki bölüm numarası -> (numaradan önceki yol, numara). '/bolum-12', '/chapter-12-baslik' gibi."""
    path = fold(urlsplit(url).path)
    ms = list(_CHAP_URL.finditer(path))
    return (path[:ms[-1].start()], int(ms[-1].group(1))) if ms else None


def _template(url):
    p = urlsplit(url)
    s = p.path + (("?" + p.query) if re.search(r"\d", p.query) else "")
    parts = re.split(r"(\d+)", s)
    return tuple(parts[0::2]), [int(x) for x in parts[1::2]]


def url_delta(cur, cand):
    """Aynı bölüm serisinde numara farkı (ör. bolum-12 -> bolum-13 = 1). Karşılaştırılamazsa None."""
    a, b = chap_num(cur), chap_num(cand)
    if a and b and a[0] == b[0]:
        return b[1] - a[1]
    (ta, na), (tb, nb) = _template(cur), _template(cand)
    if ta == tb and na and len(na) == len(nb):
        diff = [y - x for x, y in zip(na, nb) if x != y]
        return diff[0] if len(diff) == 1 else None
    return None


def _class_tokens(el):
    raw = " ".join(_as_list(el.get("class")) + _as_list(el.get("id")))
    return set(re.split(r"[\s_\-]+", fold(raw)))


def find_next(html, cur_url):
    """Sayfadaki bağlantıları puanlar; en yüksek puanlıyı (>= MIN_NEXT_SCORE) döndürür.
       rel=next +100 | 'Sonraki/Next/İleri' metni +60 | '>' '>>' '»' +40 | class/id 'next' +30 |
       URL'de bölüm no. tam +1 artıyor +40 (+2/+3 ise +15) | numara geriliyorsa aday elenir."""
    soup = BeautifulSoup(html, "lxml")
    base = soup.find("base", href=True)
    base_url = urljoin(cur_url, base["href"]) if base else cur_url
    cur_key, host = norm_url(cur_url), _host(cur_url)
    best = {}
    for el in soup.find_all(["a", "link"], href=True):
        href = el["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        url = urljoin(base_url, href)
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or _host(url) != host or norm_url(url) == cur_key:
            continue
        if _DENY_PATH.search(p.path):
            continue
        rel = {r.lower() for r in _as_list(el.get("rel"))}
        if rel & {"prev", "previous"}:
            continue
        score, why = 0, []
        if "next" in rel:
            score += 100
            why.append("rel=next")
        if el.name == "a":
            labels = [el.get_text(" ", strip=True), el.get("aria-label") or "", el.get("title") or ""]
            labels += [im.get("alt") or "" for im in el.find_all("img")]
            kinds = [link_kind(x) for x in labels if x]
            if any(k == "prev" for k, _ in kinds):
                continue
            pts = max([s for k, s in kinds if k == "next"], default=0)
            if pts:
                score += pts
                why.append("metin")
            toks = _class_tokens(el) | (_class_tokens(el.parent) if el.parent is not None and el.parent.name else set())
            if toks & {"prev", "previous", "prv", "onceki"} and not toks & {"next", "nxt", "sonraki"}:
                continue
            if toks & {"next", "nxt", "sonraki"} and not toks & {"prev", "previous", "prv", "onceki"}:
                score += 30
                why.append("class")
            d = url_delta(cur_url, url)
            if d is not None and d < 0:
                continue
            if d == 1:
                score += 40
                why.append("no+1")
            elif d in (2, 3):
                score += 15
                why.append(f"no+{d}")
            if score and p.path.rsplit("/", 1)[0] == urlsplit(cur_url).path.rsplit("/", 1)[0]:
                score += 5
        if score > best.get(url, (0, ""))[0]:
            best[url] = (score, "+".join(why))
    if not best:
        return None
    url, (score, why) = max(best.items(), key=lambda kv: kv[1][0])
    return Link(url, score, why) if score >= MIN_NEXT_SCORE else None


def guess_next(url):
    """Sayfada 'sonraki' bağlantısı yoksa son çare: URL'deki bölüm numarasını 1 artır (bolum-12 -> bolum-13)."""
    p = urlsplit(url)
    ms = list(_CHAP_URL.finditer(fold(p.path)))
    if ms:
        a, b = ms[-1].span(1)
    else:
        ds = list(re.finditer(r"\d+", p.path))
        if not ds:
            return None
        a, b = ds[-1].span()
    old = p.path[a:b]
    return urlunsplit((p.scheme, p.netloc, p.path[:a] + str(int(old) + 1).zfill(len(old)) + p.path[b:], p.query, ""))


def find_first_chapter(html, url):
    """Seri/içindekiler sayfasında ilk bölüm linkini bulur ('İLK BÖLÜM' düğmesi ya da en küçük numaralı bölüm)."""
    soup = BeautifulSoup(html, "lxml")
    host, groups = _host(url), {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        u = urljoin(url, href)
        if urlsplit(u).scheme not in ("http", "https") or _host(u) != host:
            continue
        label = fold(a.get_text(" ", strip=True))
        if len(label) <= 60 and _FIRST_RE.search(label):
            return u
        cn = chap_num(u)
        if cn:
            groups.setdefault(cn[0], []).append((cn[1], u))
    if groups:
        items = max(groups.values(), key=len)
        if len(items) >= 3:
            return min(items)[1]
    return None


# ======================================================================================
# 4) SERIESSTORE  — TXT dosyaları + kaldığı yerden devam (_state.json)
# ======================================================================================
def derive_name(url):
    """Linkten seri klasör adı: .../solo-farming-bolum-1/ -> solo-farming"""
    p = urlsplit(url)
    segs = [re.sub(r"\.(html?|php|aspx?)$", "", s, flags=re.I) for s in p.path.split("/") if s]
    for seg in reversed(segs):
        f = fold(seg)
        m = (re.match(rf"^(.*?)[-_]?(?<![a-z]){_CHAP_WORD}(?![a-z])[-_ ]*\d+.*$", f)
             or re.match(r"^(.*?)[-_]\d+$", f))
        base = m.group(1) if m else f
        base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
        if base and not base.isdigit():
            return base
    return re.sub(r"[^a-z0-9]+", "-", _host(url)).strip("-") or "novel"


_TXT_RE = re.compile(r"^(\d+)_.+\.txt$")


def chapter_files(d):
    """[(bölüm_no, yol), ...] numaraya göre sıralı."""
    out = [(int(m.group(1)), f) for f in d.glob("*.txt") if (m := _TXT_RE.match(f.name))]
    return sorted(out)


def series_dirs(root):
    return sorted(d for d in root.iterdir() if d.is_dir() and chapter_files(d))


class SeriesStore:
    """txt/<seri>/0001_Baslik.txt dosyalarını ve _state.json'u yönetir.
    _state.json: her bölümün adresi, dosyası ve 'sonraki' linki. Betik yeniden çalışınca kaldığı
    bölümden devam eder; zaten indirilmiş bölümler tekrar istenmez (resume)."""

    def __init__(self, txt_root, name, flat=False):
        self.dir = Path(txt_root) if flat else Path(txt_root) / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "_state.json"
        self.state = {"start_url": "", "chapters": [], "finished": False}
        if self.path.exists():
            try:
                self.state.update(json.loads(self.path.read_text(encoding="utf-8")))
            except ValueError:
                warn(f"{self.path} bozuk; sıfırdan başlanıyor.")
        ok = []
        for c in self.state["chapters"]:      # dosyası silinmiş bölümden itibaren yeniden indir
            if c.get("file") and not (self.dir / c["file"]).exists():
                break
            ok.append(c)
        if len(ok) != len(self.state["chapters"]):
            warn(f"{len(self.state['chapters']) - len(ok)} bölümün TXT dosyası eksik; {len(ok) + 1}. bölümden devam edilecek.")
            self.state["finished"] = False
        self.state["chapters"] = ok

    @property
    def chapters(self):
        return self.state["chapters"]

    def reset(self):
        self.state.update(chapters=[], finished=False)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, idx, url, title, lines, nxt, digest):
        """Bölümü 0001_Bolum_1.txt olarak yazar: ilk satır başlık, sonra boş satırla ayrılmış paragraflar."""
        name = f"{idx:04d}_{slugify(title) or 'Bolum'}.txt"
        for old in self.dir.glob(f"{idx:04d}_*.txt"):    # başlık değiştiyse eski dosya kalmasın
            if old.name != name:
                old.unlink()
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(f"{title}\n\n" + "\n\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(self.dir / name)
        self.chapters.append({"i": idx, "url": url, "file": name, "next": nxt, "hash": digest})
        self.save()

    def skip(self, idx, url, nxt):
        """İçeriği alınamayan bölümü atla ama numarayı koru (sıra kaymasın)."""
        self.chapters.append({"i": idx, "url": url, "file": None, "next": nxt, "hash": None})
        self.save()


# ======================================================================================
# 5) SCRAPER  — sıralı gezinti döngüsü
# ======================================================================================
def load_site_profiles(path):
    """--site-config dosyasını okur: {"site.com": {"selector": "...", "noise": [...], "guess": bool,
    "min_chars": int}}. 'probe' ile bulunan doğru içerik seçicisini/gürültü kalıplarını sorunlu bir site
    için kalıcı hale getirmek içindir. Dosya yoksa/bozuksa boş sözlük döner (sessizce yok sayılır)."""
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        warn(f"Site ayar dosyası bulunamadı: {path}")
        return {}
    except ValueError as e:
        warn(f"Site ayar dosyası ({path}) geçerli JSON değil: {e}")
        return {}
    if not isinstance(data, dict):
        warn(f"Site ayar dosyası ({path}) bir sözlük (obje) olmalı; yok sayılıyor.")
        return {}
    return {str(k).lower().removeprefix("www."): (v or {}) for k, v in data.items()}


@dataclass
class SiteSettings:
    noise: list
    guess: bool
    min_chars: int
    selector: str | None = None


def site_settings(args, host):
    """Genel CLI ayarlarını, varsa o site için site_profiles.json'daki override'larla birleştirir."""
    prof = (getattr(args, "_site_profiles", None) or {}).get(host, {})
    noise = list(args.noise) + [re.compile(x, re.I) for x in prof.get("noise", [])]
    return SiteSettings(noise=noise,
                        guess=bool(args.guess and prof.get("guess", True)),
                        min_chars=int(prof.get("min_chars", args.min_chars)),
                        selector=prof.get("selector") or None)


def content_digest(lines):
    """Bölüm içeriği için karşılaştırma özeti: büyük/küçük harf, Türkçe karakter ve boşluk farklarını yok
    sayar (fold + boşluk sadeleştirme), böylece aynı bölümün ufak farklarla tekrar yayınlanmış hali de
    'aynı içerik' olarak yakalanır."""
    norm = re.sub(r"\s+", " ", fold(" ".join(lines))).strip()
    return hashlib.sha1(norm.encode()).hexdigest()


def linked_back(html, url, ref):
    """Sayfada önceki bölüme (ref) giden bir bağlantı var mı?"""
    if not ref:
        return False
    key = norm_url(ref)
    soup = BeautifulSoup(html, "lxml")
    return any(norm_url(urljoin(url, a["href"])) == key for a in soup.find_all("a", href=True)
               if not a["href"].startswith(("#", "javascript:")))


def load_page(fx, url, ref, args, pre, settings=None):
    """Sayfayı getirip (metin, sonraki_url, sonraki_tahmin_mi) döndürür.
    curl yetersizse (metin yok / sonraki link yok) sayfa Playwright ile bir kez daha denenir."""
    st = settings or site_settings(args, _host(url))
    html = pre.pop(norm_url(url), None) or fx.fetch(url, ref)
    page = extract_page(html, url, st.noise, st.min_chars, st.selector)
    nxt = find_next(html, url)
    if fx.can_render and fx.last_engine != "playwright" and (not page.ok or not nxt):
        try:
            html2 = fx.render(url, ref)
        except FetchError as e:
            log.debug("Tarayıcıyla yeniden deneme başarısız: %s", e)
        else:
            page2 = extract_page(html2, url, st.noise, st.min_chars, st.selector)
            nxt2, better = find_next(html2, url), False
            if page2.ok and not page.ok:
                page, html, better = page2, html2, True
            if nxt2 and not nxt:
                nxt, better = nxt2, True
            if better and fx.mode == "auto":
                fx.prefer_browser = True
                log.info("Sayfa JavaScript ile yükleniyor; bu siteden sonra tarayıcı (Playwright) kullanılacak.")
    page.back = linked_back(html, url, ref)
    if nxt:
        return page, nxt.url, False
    # Yalnızca sayfa GERÇEKTEN çıkarılabildiyse URL desenine göre tahmin et: aksi halde (örn. geçici
    # engelleme/rate-limit yüzünden içerik boş dönmüşse) yanlış bir tahmin "serinin sonu" sanılabilir.
    g = guess_next(url) if (st.guess and page.ok) else None
    return page, g, bool(g)


def resolve_start(fx, url, args, pre):
    """Seri/içindekiler sayfası verildiyse ilk bölümü bulur; bölüm linki verildiyse olduğu gibi kullanır."""
    if chap_num(url) is not None:
        return url
    html = fx.fetch(url)
    pre[norm_url(url)] = html
    first = find_first_chapter(html, url)
    if first and norm_url(first) != norm_url(url):
        log.info("Seri sayfası algılandı; ilk bölüm: %s", first)
        return first
    return url


def scrape_series(url, name, args, fx):
    store = SeriesStore(args.txt_dir, name, args.flat)
    if store.chapters and store.state.get("start_url") not in ("", url):
        warn(f"'{name}' klasörü başka bir başlangıç adresiyle indirilmişti; kayıt sıfırlanıyor.")
        store.reset()
    store.state["start_url"] = url
    limit = args.max_chapters or None
    settings = site_settings(args, _host(url))
    if settings.selector or not settings.guess or settings.min_chars != args.min_chars:
        log.info("Site profili uygulanıyor (%s): seçici=%s guess=%s min_chars=%d",
                  _host(url), settings.selector or "-", settings.guess, settings.min_chars)
    log.info("=== %s === %s | sınır: %s", name, url, limit or "yok (tüm seri)")

    chs, pre = store.chapters, {}
    seen_urls = {norm_url(c["url"]) for c in chs}
    seen_hash = {c["hash"] for c in chs if c.get("hash")}
    cur = ref = None
    guessed = False
    try:
        if not chs:
            cur = resolve_start(fx, url, args, pre)
        elif chs[-1].get("next"):
            cur, ref = chs[-1]["next"], chs[-1]["url"]
        elif not (limit and len(chs) >= limit):
            # Son bölümün 'sonraki' linki yoktu: site yeni bölüm eklemiş olabilir, bir kez yeniden bak
            _, cur, guessed = load_page(fx, chs[-1]["url"], None, args, pre, settings)
            ref = chs[-1]["url"]
        if chs:
            log.info("Kayıtlı %d bölüm atlandı (devam ediliyor).", len(chs))

        idx, bar, added = len(chs) + 1, progress(total=limit, initial=len(chs), desc=name[:25]), 0
        while cur:
            if limit and idx > limit:
                break
            if norm_url(cur) in seen_urls:
                warn(f"Döngü algılandı ({cur}); duruyorum.")
                cur = None
                break
            try:
                page, nxt, nxt_guess = load_page(fx, cur, ref, args, pre, settings)
            except FetchError as e:
                if guessed:
                    log.info("Sonraki bölüm yok (%s); seri sonu kabul edildi.", e)
                    cur = None
                else:
                    warn(f"{cur} alınamadı: {e}  (yeniden çalıştırınca buradan devam eder)")
                break
            digest = content_digest(page.lines)
            # URL'yi biz tahmin ettiysek sayfa gerçekten bölüm mü? (soft-404 / ana sayfaya yönlendirme koruması)
            if guessed and page.ok and not (_CHAP_TXT.search(fold(page.title)) or page.back):
                log.info("Tahmin edilen sayfa bölüm gibi görünmüyor (%s); seri sonu kabul edildi.", cur)
                cur = None
                break
            if not page.ok and not guessed:
                # Gerçek bir 'Sonraki' bağlantısını takip ettik ama içerik çıkarılamadı: bu genelde
                # sitenin geçici olarak sizi kısıtlaması (rate-limit) ya da bir WAF/bot sayfası demektir,
                # gerçekten serinin bittiği anlamına GELMEZ. Backoff ile birkaç kez daha dene.
                ok_retry = False
                for attempt in range(1, 4):
                    wait = min(2 ** (attempt + 2), 60) + random.uniform(0, 3)
                    log.warning("[%d] içerik çıkarılamadı (muhtemel geçici engel/rate-limit); %.0fsn sonra "
                                "tekrar denenecek (%d/3): %s", idx, wait, attempt, cur)
                    time.sleep(wait)
                    try:
                        eng = fx._engine(fx.last_engine or "curl")
                        if hasattr(eng, "rotate"):
                            eng.rotate()      # farklı parmak izi/UA ile tekrar dene
                    except Exception:  # noqa: BLE001
                        pass
                    pre.pop(norm_url(cur), None)
                    try:
                        page, nxt, nxt_guess = load_page(fx, cur, ref, args, pre, settings)
                    except FetchError:
                        continue
                    if page.ok:
                        digest = content_digest(page.lines)
                        ok_retry = True
                        break
                if not ok_retry:
                    warn(f"{idx}. bölüm birkaç denemeden sonra hâlâ alınamadı (muhtemel geçici engel/rate-limit): "
                         f"{cur}\n  Duraklatıldı (seri BİTMEDİ) — yeniden çalıştırınca tam buradan devam eder. "
                         "Yardımcı olabilir: --delay-min/--delay-max değerini artırın, --fetcher playwright "
                         "deneyin, ya da --proxy kullanın.")
                    break      # cur hâlâ bu URL'de; son kaydedilen bölümün 'next' alanı zaten buraya
                               # işaret ediyor, o yüzden yeniden çalıştırınca tam buradan devam eder
            if not page.ok or digest in seen_hash:
                if guessed:                # tahmin ettiğimiz sayfa bölüm değil -> seri bitti
                    cur = None
                    break
                warn(f"{idx}. bölümde içerik alınamadı/tekrar ({cur}); atlandı.")
                store.skip(idx, cur, nxt)
            else:
                store.add(idx, cur, page.title or f"Bolum {idx}", page.lines, nxt, digest)
                seen_hash.add(digest)
                added += 1
                log.info("[%d] %s — %d karakter (%s)", idx, page.title[:50], page.chars, page.method)
            seen_urls.add(norm_url(cur))
            bar.update(1)
            idx, ref, cur, guessed = idx + 1, cur, nxt, nxt_guess
        bar.close()
        store.state["finished"] = cur is None
        store.save()
    finally:
        store.save()
    log.info("✓ %s: toplam %d bölüm (%d yeni) -> %s", name, len(store.chapters), added, store.dir)
    return len(store.chapters)


def url_list(args):
    urls = [u for u in re.split(r"[,\s]+", args.url or "") if u]
    return [u if u.startswith("http") else "https://" + u for u in urls]


def run_scrape(args):
    """Tüm URL'leri sırayla çeker. Playwright aynı iş parçacığında açılıp kapansın diye tek fonksiyonda."""
    urls = url_list(args)
    fx = Fetcher(args.fetcher, (args.delay_min, args.delay_max), args.retries, args.proxy)
    done = []
    try:
        for u in urls:
            name = args.name if len(urls) == 1 and args.name else derive_name(u)
            try:
                done.append((name, scrape_series(u, name, args, fx)))
            except Exception as e:  # noqa: BLE001
                log.error("✗ %s atlandı: %r", u, e)
                done.append((name, 0))
    finally:
        fx.close()
    return done


def cmd_scrape(args):
    if not url_list(args):
        log.error("Adres verilmedi. --url https://site.com/novel/xyz/bolum-1/ (veya NOVEL_URLS / START_URLS)")
        return 2
    done = in_clean_thread(run_scrape, args)
    total = sum(n for _, n in done)
    for name, n in done:
        if not n:
            warn(f"Hiç bölüm alınamayan seri: {name}")
    log.info("Toplam %d bölüm TXT klasöründe: %s", total, Path(args.txt_dir).resolve())
    return 0 if total else 1


# ======================================================================================
# 6) TTS  — edge-tts ile TXT -> MP3
# ======================================================================================
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def read_txt(path):
    """(başlık, gövde) döndürür; yalnızca sembol içeren satırlar atılır."""
    lines = [x for x in (clean_line(x) for x in path.read_text(encoding="utf-8").splitlines()) if x]
    return (lines[0] if lines else ""), "\n".join(x for x in lines[1:] if re.search(r"\w", x))


def read_chapter(path):
    """(başlık, [paragraf, paragraf, ...]) döndürür; store.add ile yazılan boş-satır ayraçlı biçimi çözer.
    translate ve epub komutları paragraf yapısını korumak için read_txt yerine bunu kullanır."""
    raw = path.read_text(encoding="utf-8")
    parts = raw.split("\n\n")
    title = clean_line(parts[0]) if parts else ""
    paras = [p for p in (clean_line(x.replace("\n", " ")) for x in parts[1:]) if p]
    return title, paras


def write_chapter(path, title, paras):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"{title}\n\n" + "\n\n".join(paras) + "\n", encoding="utf-8")
    tmp.replace(path)


def _hard_split(sentence, max_chars):
    parts, cur = [], ""
    for word in sentence.split(" "):
        if len(word) > max_chars:                # patolojik: boşluksuz dev kelime
            if cur:
                parts.append(cur)
                cur = ""
            parts.extend(word[i:i + max_chars] for i in range(0, len(word), max_chars))
        elif cur and len(cur) + 1 + len(word) > max_chars:
            parts.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        parts.append(cur)
    return parts


def split_text(text, max_chars):
    """Metni paragraf/cümle sınırlarını koruyarak en fazla max_chars'lık parçalara böler."""
    chunks, buf = [], ""

    def flush():
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for paragraph in text.split("\n"):
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            for piece in ([sentence] if len(sentence) <= max_chars else _hard_split(sentence, max_chars)):
                if buf and len(buf) + 1 + len(piece) > max_chars:
                    flush()
                buf = f"{buf}{'' if (not buf or buf.endswith(chr(10))) else ' '}{piece}"
        if buf and not buf.endswith("\n"):
            buf += "\n"                          # paragraf sonu
    flush()
    return [c for c in chunks if re.search(r"\w", c)]


def spoken_text(title, body):
    """Başlığı (sonuna nokta koyup doğal duraklama için) gövdenin önüne ekler."""
    if not title:
        return body
    return f"{title if title[-1] in '.!?…:;' else title + '.'}\n{body}"


async def _stream_audio(text, args):
    import edge_tts
    comm = edge_tts.Communicate(text, args.voice, rate=args.rate, volume=args.volume, pitch=args.pitch)
    buf = bytearray()
    async for msg in comm.stream():
        if msg["type"] == "audio":
            buf.extend(msg["data"])
    if not buf:
        raise RuntimeError("Servisten ses verisi alınamadı")
    return bytes(buf)


async def synth_chunk(text, args, label):
    """Tek parçayı zaman aşımı + üstel geri çekilmeli yeniden deneme ile sentezler."""
    last = None
    for attempt in range(1, args.tts_retries + 1):
        try:
            return await asyncio.wait_for(_stream_audio(text, args), timeout=args.chunk_timeout or None)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < args.tts_retries:
                delay = min(2 ** attempt, 30)
                log.warning("%s: deneme %d/%d başarısız (%s: %s); %d sn sonra tekrar", label, attempt,
                            args.tts_retries, type(exc).__name__, exc, delay)
                await asyncio.sleep(delay)
    raise RuntimeError(f"{label}: {args.tts_retries} denemede sentezlenemedi") from last


async def convert_file(idx, txt_path, out_path, total, args, sem):
    """Bir TXT dosyasını MP3'e çevirir. Başarılı ya da atlandıysa True (resume: var olan MP3 atlanır)."""
    if out_path.exists() and out_path.stat().st_size > 0 and not args.overwrite:
        log.info("[%d/%d] Zaten var, atlandı: %s", idx, total, out_path.name)
        return True
    title, body = read_txt(txt_path)
    if not body:
        log.warning("[%d/%d] Okunacak metin yok, atlandı: %s", idx, total, txt_path.name)
        return True
    async with sem:
        text = spoken_text(title, body)
        chunks = split_text(text, args.max_chars)
        log.info("[%d/%d] %s (%d karakter, %d parça)", idx, total, out_path.name, len(text), len(chunks))
        tmp = out_path.with_name(out_path.name + ".part")
        try:
            with tmp.open("wb") as fh:
                for i, chunk in enumerate(chunks, 1):
                    fh.write(await synth_chunk(chunk, args, f"{out_path.name} parça {i}/{len(chunks)}"))
            tmp.replace(out_path)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("[%d/%d] BAŞARISIZ: %s -> %s", idx, total, out_path.name, exc)
            tmp.unlink(missing_ok=True)
            return False


async def tts_series(series_dir, out_dir, args):
    files = chapter_files(series_dir)
    if not files:
        log.error("TXT dosyası bulunamadı: %s", series_dir)
        return 0, 1
    total = files[-1][0]
    end = args.end or total
    files = [(i, p) for i, p in files if args.start <= i <= end]
    if args.limit:
        files = files[:args.limit]
    if not files:
        log.warning("%s: %d-%d aralığında bölüm yok (toplam %d)", series_dir.name, args.start, end, total)
        return 0, 0
    log.info("Seri: %s | bölüm %d-%d (toplam %d) | ses: %s", series_dir.name, files[0][0], files[-1][0], total, args.voice)
    out_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.concurrency)
    res = await asyncio.gather(*(convert_file(i, p, out_dir / f"{p.stem}.mp3", total, args, sem) for i, p in files))
    ok = sum(res)
    return ok, len(res) - ok


def check_tts_args(args):
    """rate/pitch/volume/voice biçimini baştan doğrular; hata mesajını döndürür (yoksa None)."""
    if args.max_chars < 200:
        return "--max-chars en az 200 olmalı"
    if args.start < 1:
        return "--start en az 1 olmalı"
    if args.end < 0 or (args.end and args.end < args.start):
        return "--end, --start değerinden küçük olamaz (0 = sonuna kadar)"
    args.concurrency, args.tts_retries = max(1, args.concurrency), max(1, args.tts_retries)
    try:
        import edge_tts
        edge_tts.Communicate("test", args.voice, rate=args.rate, volume=args.volume, pitch=args.pitch)
    except (ValueError, TypeError) as exc:
        return f"Geçersiz ses ayarı: {exc}"
    return None


def cmd_tts(args):
    _import("edge_tts", "edge-tts")
    err = check_tts_args(args)
    if err:
        log.error(err)
        return 2
    root, out_root = Path(args.txt_dir), Path(args.mp3_dir)
    if not root.is_dir():
        log.error("TXT klasörü yok: %s", root)
        return 1
    if args.flat:
        dirs = [root]
    elif args.slug:
        dirs = [root / args.slug]
    else:
        dirs = series_dirs(root)
    if not dirs or not all(d.is_dir() for d in dirs):
        log.error("Seslendirilecek seri klasörü bulunamadı (%s)", args.slug or root)
        return 1
    ok = fail = 0
    for d in dirs:
        o, f = in_clean_thread(asyncio.run, tts_series(d, out_root if args.flat else out_root / d.name, args))
        ok, fail = ok + o, fail + f
    log.info("Bitti: %d bölüm başarılı, %d başarısız. Çıktı: %s", ok, fail, out_root.resolve())
    return 0 if fail == 0 else 1


# ======================================================================================
# 7) TRANSLATE  — TXT bölümlerini başka dile çevirir
#    google: deep-translator üzerinden Google Translate (ücretsiz, key gerekmez, internet ister)
#    argos : Argos Translate (tamamen çevrimdışı; ilk çalıştırmada dil paketini indirir)
# ======================================================================================
_ARGOS_LOCK = threading.Lock()
_ARGOS_READY = set()


# MyMemory (deep_translator'ın Google engellendiğinde düştüğü yedek), kısa dil kodu değil "locale"
# ister (ör. 'tr' değil 'tr-TR'). Kullanıcı --translate-to ile kısa kod verdiğinde (en yaygın kullanım)
# önceden bu eşleşmeden dolayı "No support for the provided language" hatasıyla sessizce başarısız
# oluyordu; bu tablo en sık kullanılan dilleri doğru locale'e çevirir.
_MYMEMORY_LOCALE = {
    "tr": "tr-TR", "en": "en-GB", "de": "de-DE", "fr": "fr-FR", "es": "es-ES", "it": "it-IT",
    "pt": "pt-PT", "pt-br": "pt-BR", "ru": "ru-RU", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN",
    "zh-cn": "zh-CN", "zh-tw": "zh-TW", "ar": "ar-SA", "nl": "nl-NL", "pl": "pl-PL", "sv": "sv-SE",
    "fi": "fi-FI", "da": "da-DK", "no": "no-NO", "cs": "cs-CZ", "el": "el-GR", "he": "he-IL",
    "hi": "hi-IN", "id": "id-ID", "th": "th-TH", "vi": "vi-VN", "uk": "uk-UA", "ro": "ro-RO",
    "hu": "hu-HU", "bg": "bg-BG", "sk": "sk-SK", "hr": "hr-HR", "sr": "sr-RS", "fa": "fa-IR",
    "ur": "ur-PK", "bn": "bn-IN", "ta": "ta-IN", "te": "te-IN", "mr": "mr-IN", "ml": "ml-IN",
}


def mymemory_locale(code):
    """Kısa bir dil kodunu ('tr', 'en') MyMemory'nin beklediği locale biçimine çevirir ('tr-TR', 'en-GB').
    Zaten 'xx-YY' biçimindeyse yalnızca büyük/küçük harfi düzeltir. Tabloda yoksa 'xx-XX' tahmini yapılır
    (her dil için doğru olmayabilir; MyMemory hâlâ reddederse --translate-engine argos deneyin)."""
    c = (code or "").strip()
    if not c or c.lower() in ("auto", "autodetect"):
        return "autodetect"
    if "-" in c:
        lang, _, region = c.partition("-")
        return f"{lang.lower()}-{region.upper()}"
    key = c.lower()
    return _MYMEMORY_LOCALE.get(key, f"{key}-{key.upper()}")


def _translate_google_one(text, target, source):
    from deep_translator import GoogleTranslator, MyMemoryTranslator
    out = []
    for chunk in split_text(text, 4500):     # Google'ın tek istekte kabul ettiği karakter sınırının altında
        try:
            t = GoogleTranslator(source=source or "auto", target=target).translate(chunk)
        except Exception as e:  # noqa: BLE001
            # Google'ın ücretsiz arka ucu bazı veri merkezi IP'lerinden (GitHub Actions dahil) engellenebilir;
            # MyMemory'e düş (günlük karakter kotası vardır ama farklı bir uçtur, çoğu zaman çalışır).
            # MyMemory kısa kod değil locale ister ('tr' değil 'tr-TR') — aksi halde bu da başarısız olur.
            log.debug("GoogleTranslator başarısız (%s), MyMemory deneniyor: %s", type(e).__name__, e)
            t = MyMemoryTranslator(source=mymemory_locale(source), target=mymemory_locale(target)).translate(chunk)
        out.append(t or "")
    return " ".join(out).strip()


def _argos_pair(source, target):
    """Gerekli Argos dil paketini bir kez indirip kurar (thread-safe); (from_lang, to_lang) döndürür."""
    import argostranslate.package as apkg
    import argostranslate.translate as atr
    key = (source, target)
    with _ARGOS_LOCK:
        if key not in _ARGOS_READY:
            langs = atr.get_installed_languages()
            src = next((l for l in langs if l.code == source), None)
            dst = next((l for l in langs if l.code == target), None)
            if not (src and dst and src.get_translation(dst)):
                apkg.update_package_index()
                match = next((p for p in apkg.get_available_packages()
                              if p.from_code == source and p.to_code == target), None)
                if not match:
                    raise RuntimeError(f"Argos: {source}->{target} dil paketi bulunamadı")
                apkg.install_from_path(match.download())
            _ARGOS_READY.add(key)
        langs = atr.get_installed_languages()
        src = next(l for l in langs if l.code == source)
        dst = next(l for l in langs if l.code == target)
        return src, dst


def _translate_argos_one(text, target, source):
    src, dst = _argos_pair(source or "tr", target)
    tr = src.get_translation(dst)
    with _ARGOS_LOCK:          # ctranslate2 modeli aynı anda tek çeviri güvenlidir; offline olduğu için zaten hızlı
        return " ".join(tr.translate(chunk) for chunk in split_text(text, 2000)).strip()


def translate_paragraphs(paras, target, source, engine, concurrency, retries=3):
    """Paragraf listesini çevirir; google için eşzamanlı (ThreadPoolExecutor), argos için sıralı çalışır."""
    fn = _translate_google_one if engine == "google" else _translate_argos_one
    workers = 1 if engine == "argos" else max(1, concurrency)

    def work(text):
        last = None
        for attempt in range(retries):
            try:
                return fn(text, target, source)
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"çeviri başarısız: {last}")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(work, paras))


def translate_series_dir(src_dir, out_dir, args):
    """Bir seri klasöründeki tüm TXT bölümlerini çevirir (resume: mevcut dosya atlanır)."""
    files = chapter_files(src_dir)
    if not files:
        return 0, 0
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    bar = progress(total=len(files), desc=f"çeviri {src_dir.name[:20]}")
    for idx, path in files:
        out_path = out_dir / path.name
        if out_path.exists() and out_path.stat().st_size > 0 and not args.overwrite:
            ok += 1
            bar.update(1)
            continue
        title, paras = read_chapter(path)
        try:
            t_title = translate_paragraphs([title], args.translate_to, args.translate_source,
                                            args.translate_engine, 1)[0] if title else title
            t_paras = translate_paragraphs(paras, args.translate_to, args.translate_source,
                                            args.translate_engine, args.translate_concurrency)
            write_chapter(out_path, t_title, t_paras)
            ok += 1
        except Exception as e:  # noqa: BLE001
            log.error("[%d] çeviri başarısız: %s -> %s", idx, path.name, e)
            fail += 1
        bar.update(1)
    bar.close()
    return ok, fail


def _warn_if_translate_broken(ok, fail, engine):
    """Çoğu/tüm bölüm çevrilemediyse olası nedeni açıkça söyler (sessizce başarısız olmasın)."""
    if fail and fail >= max(1, ok):
        if engine == "google":
            log.error("Çevirinin çoğu/tamamı başarısız oldu. Muhtemel neden: bu ağ/IP'den (ör. GitHub Actions, "
                       "bazı VPS'ler) ücretsiz Google Translate arka ucu engelleniyor olabilir; yedek olarak "
                       "denenen MyMemory de --translate-to/--translate-source için tahmin edilen locale kodunu "
                       "(ör. 'tr' -> 'tr-TR') desteklemiyor olabilir. Çözümler: --translate-engine argos "
                       "(tamamen çevrimdışı) deneyin, ya da --proxy ile ev/rezidansiyel bir proxy kullanın. "
                       "Yukarıdaki '[N] çeviri başarısız: ...' satırları asıl hatayı gösterir.")
        else:
            log.error("Çevirinin çoğu/tamamı başarısız oldu. Argos dil paketi indirilememiş olabilir "
                       "(ilk çalıştırmada internet gerekir) ya da --translate-source/--translate-to kodu yanlış "
                       "olabilir. Yukarıdaki '[N] çeviri başarısız: ...' satırları asıl hatayı gösterir.")


def cmd_translate(args):
    if not args.translate_to:
        log.error("--translate-to ile hedef dil kodu verin (ör. en, de, es, fr, ja)")
        return 2
    if args.translate_engine == "google":
        _import("deep_translator", "deep-translator")
    else:
        _import("argostranslate", "argostranslate")
    root = Path(args.txt_dir)
    if not root.is_dir():
        log.error("TXT klasörü yok: %s", root)
        return 1
    if args.flat:
        dirs = [root]
    elif args.slug:
        dirs = [root / args.slug]
    else:
        dirs = series_dirs(root)
    if not dirs or not all(d.is_dir() for d in dirs):
        log.error("Çevrilecek seri klasörü bulunamadı (%s)", args.slug or root)
        return 1
    out_root = Path(args.translate_out) if args.translate_out else Path(f"{args.txt_dir}_{args.translate_to}")
    ok = fail = 0
    for d in dirs:
        o, f = translate_series_dir(d, out_root if args.flat else out_root / d.name, args)
        ok, fail = ok + o, fail + f
    log.info("Çeviri bitti: %d bölüm başarılı, %d başarısız -> %s", ok, fail, out_root.resolve())
    _warn_if_translate_broken(ok, fail, args.translate_engine)
    return 0 if fail == 0 else 1


# ======================================================================================
# 8) EPUB  — TXT bölümlerinden tek e-kitap dosyası (ebooklib)
# ======================================================================================
from html import escape as _esc  # noqa: E402


def build_epub(series_dir, out_path, title, author, lang, cover_path=None):
    _import("ebooklib")
    from ebooklib import epub
    files = chapter_files(series_dir)
    if not files:
        raise RuntimeError(f"TXT bölümü yok: {series_dir}")
    book = epub.EpubBook()
    book.set_identifier(f"novelturk-{slugify(title) or series_dir.name}")
    book.set_title(title)
    book.set_language(lang)
    if author:
        book.add_author(author)
    if cover_path and Path(cover_path).is_file():
        book.set_cover("cover.jpg", Path(cover_path).read_bytes())
    chapters = []
    for idx, path in files:
        ch_title, paras = read_chapter(path)
        ch_title = ch_title or f"Bölüm {idx}"
        c = epub.EpubHtml(title=ch_title, file_name=f"chap_{idx:04d}.xhtml", lang=lang)
        body = "".join(f"<p>{_esc(p)}</p>\n" for p in paras) or "<p></p>"
        c.content = f"<h1>{_esc(ch_title)}</h1>\n{body}"
        book.add_item(c)
        chapters.append(c)
    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    css = epub.EpubItem(uid="style_nav", file_name="style/nav.css", media_type="text/css",
                        content="body{font-family:serif;line-height:1.5;margin:1em;} h1{text-align:center;}")
    book.add_item(css)
    book.spine = ["nav"] + chapters
    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return len(chapters)


def cmd_epub(args):
    root = Path(args.txt_dir)
    if not root.is_dir():
        log.error("TXT klasörü yok: %s", root)
        return 1
    dirs = [root / args.slug] if args.slug else series_dirs(root)
    if not dirs or not all(d.is_dir() for d in dirs):
        log.error("EPUB için seri klasörü bulunamadı (%s)", args.slug or root)
        return 1
    out_root = Path(args.epub_out)
    out_root.mkdir(parents=True, exist_ok=True)
    ok = 0
    for d in dirs:
        out_path = out_root / f"{d.name}.epub"
        try:
            n = build_epub(d, out_path, args.epub_title or d.name, args.epub_author, args.epub_lang, args.epub_cover)
            log.info("✓ %s: %d bölüm -> %s", d.name, n, out_path)
            ok += 1
        except Exception as e:  # noqa: BLE001
            log.error("✗ %s: %s", d.name, e)
    return 0 if ok == len(dirs) else 1


# ======================================================================================
# 9) OCR  — webtoon/görsel tabanlı bölümlerden pytesseract ile metin çıkarır
#    Not: sistemde 'tesseract-ocr' kurulu olmalı (pip paketi yalnızca Python sarmalayıcısıdır).
#    Ubuntu/Debian: sudo apt install tesseract-ocr tesseract-ocr-tur tesseract-ocr-eng
# ======================================================================================
_IMG_SKIP = re.compile(r"(logo|icon|avatar|sprite|button|arrow|loading|spinner|placeholder|favicon|"
                       r"banner|advert|\bad[-_]|social|share)", re.I)
_IMG_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-lazy", "data-srcset", "src")


def extract_images(html, url):
    """Sayfadaki görsel URL'lerini DOM sırasına göre döndürür (webtoon'lar genelde tembel yüklenir,
    bu yüzden önce data-src/data-original gibi öznitelikler denenir, sonra düz src)."""
    soup = BeautifulSoup(html, "lxml")
    urls, seen = [], set()
    for img in soup.find_all("img"):
        src = ""
        for attr in _IMG_ATTRS:
            v = (img.get(attr) or "").strip()
            if v:
                src = v.split(",")[0].split(" ")[0]     # srcset ise ilk aday
                break
        if not src or src.startswith("data:"):
            continue
        u = urljoin(url, src)
        if _IMG_SKIP.search(u) or norm_url(u) in seen:
            continue
        seen.add(norm_url(u))
        urls.append(u)
    return urls


def _download_bytes(fx, url, referer=None):
    eng = fx._engine("curl")
    headers = {"Referer": referer} if referer else {}
    r = eng.s.get(url, headers=headers, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.content


def ocr_image(data, lang):
    import pytesseract
    from PIL import Image
    from io import BytesIO
    img = Image.open(BytesIO(data))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    raw = pytesseract.image_to_string(img, lang=lang)
    return clean_lines(raw.splitlines())


def cmd_ocr(args):
    _import("pytesseract")
    _import("PIL", "Pillow")
    urls = url_list(args)
    if not urls:
        log.error("Adres verilmedi. --url https://site.com/webtoon/bolum-1/ (virgülle birden çok bölüm)")
        return 2
    fx = Fetcher(args.fetcher, (args.delay_min, args.delay_max), args.retries, args.proxy)
    ok = fail = 0
    try:
        for u in urls:
            name = args.name if len(urls) == 1 and args.name else derive_name(u)
            try:
                html = fx.fetch(u)
            except FetchError as e:
                log.error("%s alınamadı: %s", u, e)
                fail += 1
                continue
            imgs = extract_images(html, u)
            if fx.can_render and not imgs:      # görsellerin JS ile yüklendiği ihtimaline karşı tarayıcıyla dene
                try:
                    html = fx.render(u)
                    imgs = extract_images(html, u)
                except FetchError:
                    pass
            if not imgs:
                log.warning("%s: hiç görsel bulunamadı (--fetcher playwright deneyin)", u)
                fail += 1
                continue
            log.info("%s: %d görsel bulundu, OCR başlıyor (dil: %s)", u, len(imgs), args.ocr_lang)
            lines, bar = [], progress(total=len(imgs), desc=f"OCR {name[:20]}")
            for iu in imgs:
                try:
                    data = _download_bytes(fx, iu, referer=u)
                    lines += ocr_image(data, args.ocr_lang)
                except Exception as e:  # noqa: BLE001
                    log.debug("OCR görsel başarısız (%s): %s", iu, e)
                bar.update(1)
            bar.close()
            if not lines:
                log.warning("%s: OCR ile metin çıkarılamadı (görseller çok küçük/bulanık ya da yanlış dil paketi olabilir)", u)
                fail += 1
                continue
            store = SeriesStore(args.txt_dir, name, args.flat)
            idx = len(store.chapters) + 1
            title = args.ocr_title or page_title(BeautifulSoup(html, "lxml")) or f"Bolum {idx}"
            digest = content_digest(lines)
            store.add(idx, u, title, lines, None, digest)
            log.info("✓ %s: %d satır OCR ile kaydedildi -> %s", name, len(lines), store.dir)
            ok += 1
    finally:
        fx.close()
    log.info("OCR bitti: %d bölüm başarılı, %d başarısız", ok, fail)
    return 0 if fail == 0 else 1


# ======================================================================================
# Yardımcı komutlar: plan / probe / voices / all
# ======================================================================================
def cmd_plan(args):
    """GitHub Actions matrix: bölüm aralıklarını paralel job'lara böler."""
    root, per = Path(args.txt_dir), max(1, args.per_job)
    urls = url_list(args)
    wanted = {args.name} if (len(urls) == 1 and args.name) else {derive_name(u) for u in urls}
    include, notes = [], []
    for d in (series_dirs(root) if root.is_dir() else []):
        if wanted and d.name not in wanted:      # önbellekten gelen eski seriler planlanmasın
            continue
        total = chapter_files(d)[-1][0]
        n = math.ceil(total / per)
        size = math.ceil(total / n)              # dengeli böl (890 bölüm, 100 -> 9 x 99)
        for k in range(n):
            s, e = k * size + 1, min((k + 1) * size, total)
            if s <= total:
                include.append({"shard": len(include) + 1, "slug": d.name, "start": s, "end": e})
        notes.append(f"- `{d.name}`: **{total}** bölüm → {n} paralel parça (parça başına ~{size} bölüm)")
    if not include:
        print("::error::Planlanacak TXT bölümü bulunamadı.", file=sys.stderr)
        return 1
    if len(include) > 256:
        print(f"::error::{len(include)} parça, GitHub matrix sınırı olan 256'yı aşıyor; chapters_per_job değerini büyütün.",
              file=sys.stderr)
        return 1
    matrix = json.dumps({"include": include}, separators=(",", ":"))
    print(matrix)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"matrix={matrix}\nshards={len(include)}\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write("### Plan\n" + "\n".join(notes) + f"\n\nToplam paralel job: **{len(include)}**\n")
    print(f"Plan: {len(include)} parça", file=sys.stderr)
    return 0


def run_probe(args):
    urls = url_list(args)
    url = urls[0]
    settings = site_settings(args, _host(url))
    fx = Fetcher(args.fetcher, (0, 0), 1, args.proxy)
    engines = ["curl", "playwright"] if args.fetcher == "auto" else [args.fetcher]
    proxy_n = len(fx.proxies)
    print(f"Adres: {url} | fetcher: {args.fetcher} | proxy: {proxy_n or 'yok'}"
          f"{f' ({proxy_n} adet, rotasyonlu)' if proxy_n > 1 else ''}")
    if settings.selector or not settings.guess or settings.min_chars != args.min_chars:
        print(f"Site profili (--site-config): seçici={settings.selector or '-'} guess={settings.guess} "
              f"min_chars={settings.min_chars}")
    try:
        for eng in engines:
            t0 = time.time()
            try:
                html = fx._run(eng, url, None)
            except FetchError as e:
                print(f"\n[{eng}] HATA: {e}")
                continue
            page = extract_page(html, url, settings.noise, settings.min_chars, settings.selector)
            nxt = find_next(html, url)
            print(f"\n[{eng}] {time.time() - t0:.1f} sn | HTML {len(html):,} bayt")
            print(f"  başlık : {page.title!r}")
            hint = "" if page.ok else "  <-- YETERSİZ (JS ile yükleniyor olabilir; --site-config ile bir CSS seçici tanımlamayı deneyin)"
            print(f"  metin  : {page.chars:,} karakter, {len(page.lines)} paragraf ({page.method}){hint}")
            if page.lines:
                print(f"  önizleme: {' / '.join(page.lines[:2])[:220]!r}")
            print(f"  sonraki: {nxt.url + f'  [puan {nxt.score}: {nxt.why}]' if nxt else 'BULUNAMADI'}")
            if not nxt and settings.guess:
                print(f"  tahmin : {guess_next(url)} (numara +1)")
            if chap_num(url) is None:
                print(f"  ilk bölüm (seri sayfası ise): {find_first_chapter(html, url) or 'bulunamadı'}")
    finally:
        fx.close()
    return 0


def cmd_probe(args):
    if not url_list(args):
        log.error("--url verin")
        return 2
    return in_clean_thread(run_probe, args)


def cmd_voices(args):
    async def go():
        import edge_tts
        for v in sorted(await edge_tts.list_voices(), key=lambda v: v["ShortName"]):
            if v["Locale"].lower().startswith(args.locale.lower()):
                print(f'{v["ShortName"]:<28} {v["Gender"]}')
    _import("edge_tts", "edge-tts")
    in_clean_thread(asyncio.run, go())
    return 0


def cmd_all(args):
    if not url_list(args):
        log.error("Adres verilmedi. --url https://site.com/novel/xyz/bolum-1/")
        return 2
    fmts = {f.strip().lower() for f in (args.format or "mp3").split(",") if f.strip()}
    bad = fmts - {"txt", "mp3", "epub"}
    if bad:
        log.warning("--format içinde tanınmayan değer(ler) yok sayıldı: %s", ", ".join(sorted(bad)))
    done = in_clean_thread(run_scrape, args)
    names = [n for n, c in done if c]
    if not names:
        return 1
    rc = 0
    src_root = Path(args.txt_dir)
    if args.translate:
        if not args.translate_to:
            log.error("--translate açık ama --translate-to (hedef dil) verilmedi; çeviri atlanıyor.")
        else:
            if args.translate_engine == "google":
                _import("deep_translator", "deep-translator")
            else:
                _import("argostranslate", "argostranslate")
            out_root = Path(args.translate_out) if args.translate_out else Path(f"{args.txt_dir}_{args.translate_to}")
            t_ok = t_fail = 0
            for n in names:
                o, f = translate_series_dir(Path(args.txt_dir) / n, out_root / n, args)
                log.info("Çeviri [%s]: %d bölüm başarılı, %d başarısız", n, o, f)
                t_ok, t_fail, rc = t_ok + o, t_fail + f, rc | (0 if f == 0 else 1)
            _warn_if_translate_broken(t_ok, t_fail, args.translate_engine)
            src_root = out_root       # sonraki adımlar (mp3/epub) çevrilmiş metinden üretilir
    orig_txt_dir = args.txt_dir
    args.txt_dir = str(src_root)
    if "mp3" in fmts:
        for n in names:
            args.slug = n
            rc |= cmd_tts(args)
    if "epub" in fmts:
        for n in names:
            args.slug = n
            rc |= cmd_epub(args)
    args.txt_dir = orig_txt_dir
    return rc


# ======================================================================================
# CLI
# ======================================================================================
def _normalize_argv(argv):
    """'--rate -15%' argparse'ta seçenek sanılır; '--rate=-15%' biçimine çevirir."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] in ("--rate", "--volume", "--pitch") and i + 1 < len(argv):
            out.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Evrensel novel sitesi -> TXT -> MP3 (edge-tts)")
    p.add_argument("command", nargs="?", default="all",
                   choices=["all", "scrape", "tts", "translate", "epub", "ocr", "plan", "probe", "voices"])
    # -- kazıma
    p.add_argument("--url", default=os.getenv("NOVEL_URLS") or ",".join(START_URLS),
                   help="İlk bölüm (veya seri sayfası) linki; birden çoksa virgülle ayırın")
    p.add_argument("--name", default=os.getenv("NOVEL_NAME", ""), help="Seri klasör adı (bos = linkten otomatik)")
    p.add_argument("--max-chapters", default=os.getenv("NOVEL_MAX_CHAPTERS") or str(MAX_CHAPTERS),
                   help="Seri başına bölüm sınırı (0 veya all = tüm seri)")
    p.add_argument("--fetcher", choices=["auto", "curl", "playwright"], default=os.getenv("NOVEL_FETCHER") or "auto",
                   help="auto: önce curl_cffi, gerekirse Playwright")
    p.add_argument("--proxy", default=os.getenv("NOVEL_PROXY") or os.getenv("NOVELTURK_PROXY") or None,
                   help="http://kullanici:sifre@host:port; birden çoksa virgülle ayırın ya da @dosya.txt verin "
                        "(siteye göre otomatik rotasyonlanır)")
    p.add_argument("--site-config", default=os.getenv("NOVEL_SITE_CONFIG", ""), metavar="JSON",
                   help="Site başına kalıcı ayar dosyası (CSS seçici, gürültü kalıpları, guess aç/kapa, "
                        "min_chars). Biçim için dosyanın en üstündeki docstring'e bakın.")
    p.add_argument("--delay-min", type=float, default=DELAY[0], help="istekler arası en az bekleme (sn)")
    p.add_argument("--delay-max", type=float, default=DELAY[1], help="istekler arası en çok bekleme (sn)")
    p.add_argument("--retries", type=int, default=RETRIES, help="başarısız istekte yeniden deneme sayısı")
    p.add_argument("--min-chars", type=int, default=MIN_CHARS, help="bölüm sayılmak için en az karakter")
    p.add_argument("--noise", action="append", default=[], metavar="REGEX",
                   help="Bu kalıpla eşleşen satırları at (tekrarlanabilir), ör. --noise 'siteadi\\.com'")
    p.add_argument("--no-guess", dest="guess", action="store_false",
                   help="'Sonraki' linki yoksa URL numarasını +1 artırarak tahmin etme")
    # -- klasörler
    p.add_argument("--txt-dir", default=str(BASE_DIR / "txt"))
    p.add_argument("--mp3-dir", "-o", default=str(BASE_DIR / "mp3"))
    p.add_argument("--flat", action="store_true", help="Seri alt klasörü açma: txt/0001_X.txt, mp3/0001_X.mp3")
    p.add_argument("--format", default=os.getenv("NOVEL_FORMAT", "mp3"),
                   help="'all' komutunda scrape sonrası üretilecek çıktılar, virgülle ayrılmış: mp3,epub (ör. --format mp3,epub)")
    # -- çeviri (translate komutu ve all'da --translate ile açılır)
    p.add_argument("--translate", action="store_true", default=_env_bool("NOVEL_TRANSLATE"),
                   help="'all' komutunda çeviriyi AÇAR (--translate-to ile birlikte kullanın). "
                        "translate komutunu doğrudan çalıştırıyorsanız bu bayrağa gerek yok.")
    p.add_argument("--translate-to", default=os.getenv("NOVEL_TRANSLATE_TO", ""), metavar="DİL",
                   help="Hedef dil kodu (ör. en, de, es, fr, ja); boş = çeviri yapılmaz")
    p.add_argument("--translate-source", default=os.getenv("NOVEL_TRANSLATE_SOURCE", "tr"), metavar="DİL",
                   help="Kaynak dil kodu (varsayılan tr; Argos için 'auto' desteklenmez, google için desteklenir)")
    p.add_argument("--translate-engine", choices=["google", "argos"],
                   default=os.getenv("NOVEL_TRANSLATE_ENGINE", "google"),
                   help="google: deep-translator/Google, ücretsiz+key yok, internet ister (varsayılan) | "
                        "argos: Argos Translate, tamamen çevrimdışı, ilk seferde dil paketini indirir")
    p.add_argument("--translate-out", default="", help="Çevrilmiş TXT çıktı klasörü (boş = <txt-dir>_<dil>)")
    p.add_argument("--translate-concurrency", type=int, default=4,
                   help="google motorunda aynı anda çevrilecek paragraf sayısı (argos sıralı çalışır)")
    # -- epub
    p.add_argument("--epub-out", default=str(BASE_DIR / "epub"), help="EPUB çıktı klasörü")
    p.add_argument("--epub-title", default="", help="Kitap başlığı (boş = seri klasör adı)")
    p.add_argument("--epub-author", default="", help="Yazar adı")
    p.add_argument("--epub-lang", default="tr", help="EPUB dil kodu (--translate-to kullanıyorsanız onunla eşleştirin)")
    p.add_argument("--epub-cover", default="", help="Kapak görseli yolu (jpg), isteğe bağlı")
    # -- ocr (webtoon/görsel tabanlı bölümler)
    p.add_argument("--ocr-lang", default=os.getenv("NOVEL_OCR_LANG", "eng+tur"),
                   help="tesseract dil kodu/kodları, '+' ile birleştirilir (ör. eng+tur, kor). "
                        "Sistemde kurulu olmalı: apt install tesseract-ocr-<kod>")
    p.add_argument("--ocr-title", default="", help="OCR bölümü için sabit başlık (boş = sayfa başlığından tahmin)")
    # -- tts
    p.add_argument("--slug", default="", help="tts: yalnızca bu seri klasörünü seslendir")
    p.add_argument("--start", type=int, default=1, help="tts: ilk bölüm no (dahil)")
    p.add_argument("--end", type=int, default=0, help="tts: son bölüm no (dahil, 0 = sonuna kadar)")
    p.add_argument("--limit", type=int, default=0, help="tts: seçilen aralıktan yalnızca ilk N bölüm (test)")
    p.add_argument("--voice", "-v", default=DEFAULT_VOICE)
    p.add_argument("--rate", default="+0%", help="Konuşma hızı, ör. +10%% veya -15%%")
    p.add_argument("--volume", default="+0%")
    p.add_argument("--pitch", default="+0Hz")
    p.add_argument("--max-chars", type=int, default=3000, help="Tek seferde sentezlenecek en fazla karakter")
    p.add_argument("--tts-retries", type=int, default=TTS_RETRIES)
    p.add_argument("--chunk-timeout", type=float, default=120.0)
    p.add_argument("--concurrency", type=int, default=5,
                   help="Aynı anda işlenecek bölüm sayısı (hız için artırılabilir; edge-tts'i çok zorlarsa düşürün)")
    p.add_argument("--overwrite", action="store_true", help="Var olan MP3'lerin üzerine yaz")
    # -- plan / voices / genel
    p.add_argument("--per-job", type=int, default=100, help="plan: paralel job başına en fazla bölüm")
    p.add_argument("--locale", default="tr-TR", help="voices: listelenecek dil")
    p.add_argument("--verbose", action="store_true")
    raw = sys.argv[1:] if argv is None else list(argv)
    # Colab/Jupyter kendi argümanlarını (-f kernel.json) geçirir; onları yok say
    args, _ = p.parse_known_args([] if "google.colab" in sys.modules else _normalize_argv(raw))
    v = str(args.max_chapters).strip().lower()
    args.max_chapters = 0 if v in ("all", "none", "") else int(v)
    args.noise = [re.compile(x, re.I) for x in args.noise]
    args.delay_min, args.delay_max = sorted((max(0.0, args.delay_min), max(0.0, args.delay_max)))
    args.retries = max(0, args.retries)
    return args


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    args._site_profiles = load_site_profiles(args.site_config)
    cmds = {"all": cmd_all, "scrape": cmd_scrape, "tts": cmd_tts, "translate": cmd_translate,
            "epub": cmd_epub, "ocr": cmd_ocr, "plan": cmd_plan, "probe": cmd_probe, "voices": cmd_voices}
    return cmds[args.command](args)


if __name__ == "__main__":
    rc = main()
    if rc:
        sys.exit(rc)
