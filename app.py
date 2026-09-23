from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from smart_terms import build_boolean_query, build_search_plan

APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
if os.environ.get("HUNTER_TRUST_PROXY", "1") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

WEB_VERSION = "2.1.0-beta-render-free"

# Web Beta 2.1 Render Free: lightweight server-side aggregation. Browser automation is disabled on the free tier by default.
# Native adapters return far more results with proper thumbnails in one request.
SOURCES = {
    "printables": {
        "name": "Printables", "domain": "printables.com", "path_hint": "/model/",
        "search": "https://www.printables.com/search/models?q={q}", "mark": "P", "mode": "native",
    },
    "makerworld": {
        "name": "MakerWorld", "domain": "makerworld.com", "path_hint": "/models/",
        "search": "https://makerworld.com/en/search/models?keyword={q}", "mark": "MW", "mode": "native",
    },
    "makeronline": {
        "name": "MakerOnline", "domain": "makeronline.com", "path_hint": "/model/",
        "search": "https://makeronline.com/en/search/model/?keyword={q}", "mark": "MO", "mode": "native",
    },
    "nexprint": {
        "name": "Nexprint", "domain": "nexprint.com", "path_hint": "/models/",
        "search": "https://www.nexprint.com/en/models?keyword={q}", "mark": "NX", "mode": "native",
    },
    "thingiverse": {
        "name": "Thingiverse", "domain": "thingiverse.com", "path_hint": "/thing:",
        "search": "https://www.thingiverse.com/search?q={q}&page=1&type=things&sort=relevant", "mark": "TV", "mode": "index",
    },
    "cults": {
        "name": "Cults3D", "domain": "cults3d.com", "path_hint": "/3d-model/",
        "search": "https://cults3d.com/en/search?q={q}", "mark": "C", "mode": "index",
    },
    "myminifactory": {
        "name": "MyMiniFactory", "domain": "myminifactory.com", "path_hint": "/object/3d-print-",
        "search": "https://www.myminifactory.com/search/?query={q}", "mark": "MMF", "mode": "index",
    },
    "thangs": {
        "name": "Thangs", "domain": "thangs.com", "path_hint": "/designer/",
        "search": "https://thangs.com/search/{q}?scope=all&view=grid", "mark": "T", "mode": "index",
    },
}

ALLOWED_DOMAINS = tuple(cfg["domain"] for cfg in SOURCES.values())
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/152 Safari/537.36"

# DDGS tends to rate-limit when several source threads hit it at exactly the same time.
_DDGS_SEM = threading.Semaphore(2)
_MW_BROWSER_SEM = threading.Semaphore(1)
_SOURCE_UNION_LOCK = threading.Lock()
_SOURCE_UNION_CACHE: dict[tuple, dict[str, dict]] = {}
SUPPORTED_FORMATS = {"stl", "3mf", "step", "obj"}

_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[str, list[float]] = {}

def _client_ip() -> str:
    return str(request.remote_addr or "unknown")

def _basic_auth_ok() -> bool:
    user = os.environ.get("HUNTER_BETA_USER", "").strip()
    password = os.environ.get("HUNTER_BETA_PASSWORD", "")
    if not user or not password:
        return True
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        got_user, got_password = raw.split(":", 1)
        return got_user == user and got_password == password
    except Exception:
        return False

def _rate_allowed(bucket: str, max_hits: int, window_seconds: int) -> bool:
    now = time.time()
    key = f"{bucket}:{_client_ip()}"
    with _RATE_LOCK:
        hits = [x for x in _RATE_BUCKETS.get(key, []) if now - x < window_seconds]
        if len(hits) >= max_hits:
            _RATE_BUCKETS[key] = hits
            return False
        hits.append(now)
        _RATE_BUCKETS[key] = hits
        if len(_RATE_BUCKETS) > 5000:
            stale = [k for k, v in _RATE_BUCKETS.items() if not v or now - v[-1] > max(window_seconds, 3600)]
            for k in stale[:1000]:
                _RATE_BUCKETS.pop(k, None)
    return True

@app.before_request
def _web_guard():
    # Health must stay public for container/orchestrator checks.
    if request.path == "/api/health":
        return None
    if not _basic_auth_ok():
        return Response("3D Model Hunter Web Beta", 401, {"WWW-Authenticate": 'Basic realm="3D Model Hunter Beta"'})
    if request.path == "/api/search" and not _rate_allowed("search", int(os.environ.get("HUNTER_SEARCHES_PER_10MIN", "40")), 600):
        return jsonify({"error": "Za dużo wyszukiwań z tego adresu. Odczekaj chwilę i spróbuj ponownie."}), 429
    return None

@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    return response

def _norm_format(value: str) -> str:
    v = str(value or "").lower().strip().lstrip(".")
    if v == "stp":
        return "step"
    return v

def _filename_format(name: str) -> str:
    n = str(name or "").lower().split("?", 1)[0].split("#", 1)[0]
    if "." not in n:
        return ""
    return _norm_format(n.rsplit(".", 1)[-1])

def _collect_formats(value: Any) -> list[str]:
    """Best-effort recursive format extraction from public API metadata.

    We deliberately only return formats that are evidenced by a filename/type field;
    we do not guess based on the platform name.
    """
    found: list[str] = []
    def add(v):
        if v in SUPPORTED_FORMATS and v not in found:
            found.append(v)
    def walk(x, key=""):
        if isinstance(x, dict):
            for k, v in x.items():
                kl = str(k).lower()
                if isinstance(v, str):
                    # filename-like fields and explicit type/extension fields
                    if any(t in kl for t in ("file", "name", "type", "format", "extension")):
                        ext = _filename_format(v)
                        add(ext)
                        vv = _norm_format(v)
                        add(vv)
                walk(v, kl)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and any(t in key for t in ("file", "name", "type", "format", "extension")):
            add(_filename_format(x)); add(_norm_format(x))
    walk(value)
    return found

def _format_match(item: dict, wanted: list[str]) -> bool:
    if not wanted:
        return True
    available = {_norm_format(x) for x in (item.get("formats") or [])}
    return bool(available & {_norm_format(x) for x in wanted})


def _request_json(url: str, payload: dict | None = None, timeout: float = 9.0, headers: dict | None = None):
    body = None
    merged = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }
    if headers:
        merged.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        merged["Content-Type"] = "application/json"

    last_exc = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, headers=merged, method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(4_000_000)
                enc = response.headers.get_content_charset() or "utf-8"
            return json.loads(raw.decode(enc, errors="replace"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= 2:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = min(2.5, max(0.25, float(retry_after))) if retry_after else 0.45 * (attempt + 1)
            except Exception:
                delay = 0.45 * (attempt + 1)
            time.sleep(delay)
        except (TimeoutError, urllib.error.URLError) as exc:
            last_exc = exc
            if attempt >= 1:
                raise
            time.sleep(0.35)
    if last_exc:
        raise last_exc
    raise RuntimeError("request failed")


@lru_cache(maxsize=256)
def _translator(query: str) -> tuple[str, str]:
    """Translate, but never let an upstream error page become a search query."""
    q = re.sub(r"\s+", " ", str(query or "").strip())

    def valid(text: Any) -> str:
        t = re.sub(r"\s+", " ", str(text or "").strip())
        low = t.lower()
        bad = (
            "server error", "error 500", "that's an error", "thats an error",
            "please try again later", "all we know", "<html", "<!doctype",
            "service unavailable", "bad gateway", "internal server error",
        )
        if not t or any(x in low for x in bad):
            return ""
        # A normal translation should not explode into a whole error document.
        if len(t) > max(220, len(q) * 6):
            return ""
        if not re.search(r"[A-Za-zÀ-ž]", t):
            return ""
        return t

    # Primary translator. deep-translator occasionally returns Google's HTML error text
    # as if it were a successful translation, hence the strict validation above.
    try:
        from deep_translator import GoogleTranslator
        text = valid(GoogleTranslator(source="auto", target="en").translate(q))
        if text:
            return text, "online"
    except Exception:
        pass

    # Independent fallback supplied by the same installed package.
    try:
        from deep_translator import MyMemoryTranslator
        text = valid(MyMemoryTranslator(source="pl-PL", target="en-GB").translate(q))
        if text:
            return text, "online-fallback"
    except Exception:
        pass

    # Local workshop dictionary. Longer phrases MUST run before single words.
    replacements = {
        "suszarki do ubrań": "drying rack", "suszarka do ubrań": "drying rack",
        "suszarki na pranie": "drying rack", "suszarka na pranie": "drying rack",
        "uchwyt": "holder", "mocowanie": "mount", "wspornik": "bracket", "osłona": "cover",
        "obudowa": "housing", "zaślepka": "cap", "zębatka": "gear", "tuleja": "bushing",
        "dystans": "spacer", "kierunkowskaz": "turn signal", "błotnik": "fender", "owiewka": "fairing",
        "lusterko": "mirror", "silniczek": "motor", "przewód": "cable", "wtyczka": "connector",
        "uszczelka": "gasket", "łożysko": "bearing", "śruba": "bolt", "nakrętka": "nut",
        "pokrętło": "knob", "stojak": "stand", "wieszak": "hanger", "prowadnica": "guide",
        "czujnik": "sensor", "dysza": "nozzle", "szpula": "spool", "drukarki": "printer",
        "drukarka": "printer", "motocykla": "motorcycle", "motocykl": "motorcycle",
        "zawias": "hinge", "ubrań": "clothes",
    }
    out = q
    for pl, en in sorted(replacements.items(), key=lambda kv: len(kv[0]), reverse=True):
        out = re.sub(rf"\b{re.escape(pl)}\b", en, out, flags=re.IGNORECASE)
    return valid(out) or q, "offline-fallback"

def _ddgs_text(search_query: str, max_results: int):
    try:
        from ddgs import DDGS
        with _DDGS_SEM:
            with DDGS() as d:
                return list(d.text(search_query, max_results=max_results, safesearch="moderate"))
    except Exception:
        try:
            from duckduckgo_search import DDGS
            with _DDGS_SEM:
                with DDGS() as d:
                    return list(d.text(search_query, max_results=max_results, safesearch="moderate"))
        except Exception as exc:
            raise RuntimeError(str(exc))


def _ddgs_images(search_query: str, max_results: int):
    try:
        from ddgs import DDGS
        with _DDGS_SEM:
            with DDGS() as d:
                return list(d.images(search_query, max_results=max_results, safesearch="moderate"))
    except Exception:
        try:
            from duckduckgo_search import DDGS
            with _DDGS_SEM:
                with DDGS() as d:
                    return list(d.images(search_query, max_results=max_results, safesearch="moderate"))
        except Exception:
            return []


def _strip_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _canonical_key(url: str, source_id: str = "") -> str:
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower().removeprefix("www.")
        path = urllib.parse.unquote(p.path).rstrip("/").lower()
        patterns = {
            "printables": r"/model/(\d+)",
            "makerworld": r"/models/(\d+)",
            "thingiverse": r"/thing:(\d+)",
            "nexprint": r"/models/([^/?#]+)",
            "makeronline": r"/(\d+)\.html$",
        }
        pat = patterns.get(source_id)
        if pat and (m := re.search(pat, path)):
            return f"{source_id}:{m.group(1)}"
        return host + path
    except Exception:
        return url.split("?", 1)[0].rstrip("/").lower()


def _normalize_text(s: str) -> str:
    s = unescape(str(s or "")).lower().replace("–", "-").replace("—", "-")
    s = re.sub(r"[^a-z0-9à-ž+_.\-/ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _term_in(term: str, hay: str) -> bool:
    t = _normalize_text(term)
    h = _normalize_text(hay)
    if not t:
        return False
    # Treat hyphens/spaces as equivalent for model codes such as YZF-R125 / YZF R125.
    variants = {t, t.replace("-", " "), t.replace(" ", "-")}
    return any(v in h or v.replace("-", " ") in h.replace("-", " ") for v in variants)


def _family_terms(concept: dict) -> list[str]:
    vals = list(concept.get("synonyms") or [])
    # Literal/machine-translation aliases are valid evidence in result text even when we avoid using them as primary queries.
    term = str(concept.get("term") or "")
    if "suszark" in term.lower() or any("drying rack" in str(v).lower() for v in vals):
        vals += ["clothes dryer", "laundry airer"]
    return list(dict.fromkeys(vals))


def _relevance(item: dict, plan: dict, source_cfg: dict) -> tuple[float, dict]:
    title = str(item.get("title") or "")
    desc = str(item.get("description") or "")
    url = str(item.get("url") or "")
    all_text = f"{title} {desc} {url}"
    title_norm = _normalize_text(title)

    score = 0.0
    anchors = list(plan.get("anchors") or [])
    base_terms = list(plan.get("base_terms") or [])
    concepts = list(plan.get("concepts") or [])

    anchor_hits = 0
    for anchor in anchors:
        if _term_in(anchor, title):
            score += 11.0
            anchor_hits += 1
        elif _term_in(anchor, all_text):
            score += 6.0
            anchor_hits += 1
        else:
            score -= 10.0 if any(ch.isdigit() for ch in anchor) else 6.0

    soft_hits = 0
    for term in base_terms:
        if term.lower() in {x.lower() for x in anchors}:
            continue
        if _term_in(term, title):
            score += 3.2
            soft_hits += 1
        elif _term_in(term, all_text):
            score += 1.4
            soft_hits += 1

    concept_hits = 0
    concept_detail = []
    for c in concepts:
        family = _family_terms(c)
        title_hit = next((s for s in family if _term_in(s, title)), None)
        body_hit = title_hit or next((s for s in family if _term_in(s, all_text)), None)
        if title_hit:
            score += 8.5
            concept_hits += 1
            concept_detail.append(title_hit)
        elif body_hit:
            score += 4.5
            concept_hits += 1
            concept_detail.append(body_hit)
        else:
            score -= 2.2

    if anchors and anchor_hits == len(anchors):
        score += 6.0
    if concepts and concept_hits == len(concepts):
        score += 11.0
    if anchors and anchor_hits and concepts and concept_hits:
        score += 5.0

    low_title = title_norm
    if any(word in low_title for word in ("search results", "models for 3d printer", "free 3d models", "collection of")):
        score -= 5.0
    if any(word in _normalize_text(all_text) for word in ("replacement", "repair", "spare part", "fix", "joint", "hinge", "bracket", "adapter")):
        score += 1.2
    if source_cfg.get("path_hint", "").lower() in url.lower():
        score += 2.0

    # Public UI should not pretend that the internal ranking score is a probability.
    # Report transparent keyword/group coverage instead. Soft terms are the leftover
    # meaningful words after extracting brand/model anchors and synonym concepts.
    soft_total = len([t for t in base_terms if t.lower() not in {x.lower() for x in anchors}])
    keyword_hits = anchor_hits + concept_hits + soft_hits
    keyword_total = len(anchors) + len(concepts) + soft_total

    if score >= 28:
        quality = "exact"
    elif score >= 16:
        quality = "good"
    else:
        quality = "broad"
    return round(score, 2), {
        "anchors": anchor_hits, "anchor_total": len(anchors),
        "concepts": concept_hits, "concept_total": len(concepts),
        "soft": soft_hits, "soft_total": soft_total,
        "keyword_hits": keyword_hits, "keyword_total": keyword_total,
        "matched_terms": concept_detail[:4], "quality": quality,
    }


def _passes_precision(item: dict, plan: dict, precision: str) -> bool:
    meta = item.get("match") or {}
    a, at = int(meta.get("anchors") or 0), int(meta.get("anchor_total") or 0)
    c, ct = int(meta.get("concepts") or 0), int(meta.get("concept_total") or 0)
    score = float(item.get("score") or 0)

    if precision == "wide":
        return score >= 1.5
    if precision == "strict":
        anchors_ok = (not at) or a == at
        concepts_ok = (not ct) or c == ct
        return anchors_ok and concepts_ok and score >= 10

    # Balanced default: brand/model/OEM must be present; with several semantic concepts at least one must match.
    if at and a == 0:
        return False
    if ct and c == 0:
        return False
    return score >= 6.0


def _finalize_item(item: dict, plan: dict, source_id: str, precision: str, force_keep: bool = False) -> dict | None:
    cfg = SOURCES[source_id]
    score, meta = _relevance(item, plan, cfg)
    item["score"] = score
    item["match"] = meta
    item["match_quality"] = meta["quality"]
    # Kept for old clients only; this is keyword coverage, not probability/confidence.
    total = int(meta.get("keyword_total") or 0)
    hits = int(meta.get("keyword_hits") or 0)
    item["match_pct"] = round((hits / total) * 100) if total else 0
    return item if (force_keep or _passes_precision(item, plan, precision)) else None


def _base_item(source_id: str, *, title: str, url: str, description: str = "", image: str = "", **extra) -> dict:
    cfg = SOURCES[source_id]
    clean = url.split("#", 1)[0] if source_id != "makerworld" else url
    out = {
        "id": hashlib.sha1(clean.encode("utf-8", "ignore")).hexdigest()[:12],
        "source": source_id, "source_name": cfg["name"], "mark": cfg["mark"],
        "title": _strip_html(title) or "Model 3D", "url": url, "description": _strip_html(description),
        "image": image or "", "image_origin": extra.pop("image_origin", ""),
        "author": extra.pop("author", "") or "", "downloads": extra.pop("downloads", None),
        "likes": extra.pop("likes", None), "rating": extra.pop("rating", None),
        "published": extra.pop("published", "") or "", "license": extra.pop("license", "") or "",
        "kind": "result",
        "formats": extra.pop("formats", []) or [],
        "format_verified": bool(extra.pop("format_verified", False)),
    }
    out.update(extra)
    return out


def _deep_lists(value: Any) -> Iterable[list[dict]]:
    if isinstance(value, list):
        if value and all(isinstance(x, dict) for x in value):
            yield value
        for x in value:
            yield from _deep_lists(x)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _deep_lists(v)


def _best_object_list(data: Any) -> list[dict]:
    best: list[dict] = []
    best_score = -1
    desired = {
        "id", "designId", "modelId", "name", "title", "modelName", "model_name",
        "coverUrl", "cover", "thumbnail", "image", "downloadCount", "likeCount"
    }
    for xs in _deep_lists(data):
        sample = xs[: min(5, len(xs))]
        keys = set().union(*(set(x.keys()) for x in sample)) if sample else set()
        score = len(xs) + 8 * len(keys & desired)
        if score > best_score:
            best, best_score = xs, score
    return best


def _first(obj: dict, *keys, default=None):
    for key in keys:
        if key in obj and obj.get(key) not in (None, "", []):
            return obj.get(key)
    return default


def _first_http(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith(("http://", "https://")) else ""
    if isinstance(value, list):
        for x in value:
            got = _first_http(x)
            if got:
                return got
    if isinstance(value, dict):
        for key in ("url", "coverUrl", "imageUrl", "src", "path", "thumbnail", "preview"):
            got = _first_http(value.get(key))
            if got:
                return got
        for x in value.values():
            got = _first_http(x)
            if got:
                return got
    return ""


def _author_name(obj: dict) -> str:
    for key in ("designCreator", "creator", "user", "author", "publisher", "owner"):
        v = obj.get(key)
        if isinstance(v, dict):
            for k in ("name", "publicUsername", "handle", "nick", "nickname", "username"):
                if v.get(k):
                    return str(v[k])
        elif isinstance(v, str) and v.strip():
            return v.strip()
    return str(_first(obj, "authorName", "userName", "nickname", "creatorName", default="") or "")


def _num(obj: dict, *keys):
    v = _first(obj, *keys, default=None)
    if isinstance(v, dict):
        v = _first(v, "count", "value", default=None)
    return v


PRINTABLES_FORMAT_BATCH_FIELDS = "filesType stls { name } gcodes { name } slas { name } otherFiles { name }"

def _printables_batch_formats(ids: list[str]) -> dict[str, list[str]]:
    ids = [str(x) for x in ids if str(x).isdigit()]
    if not ids:
        return {}
    out: dict[str, list[str]] = {}
    for start in range(0, len(ids), 35):
        chunk = ids[start:start + 35]
        fields = " ".join(f'p{i}: print(id: "{mid}") {{ id {PRINTABLES_FORMAT_BATCH_FIELDS} }}' for i, mid in enumerate(chunk))
        data = _request_json("https://api.printables.com/graphql/", payload={"query": "query FormatBatch { " + fields + " }"},
                             timeout=10.0, headers={"Referer": "https://www.printables.com/"})
        root = (data or {}).get("data") or {}
        for obj in root.values():
            if not isinstance(obj, dict) or not obj.get("id"):
                continue
            fmts = _collect_formats(obj)
            ft = _norm_format(str(obj.get("filesType") or ""))
            if ft in SUPPORTED_FORMATS and ft not in fmts:
                fmts.append(ft)
            out[str(obj["id"])] = fmts
    return out

@lru_cache(maxsize=1024)
def _makeronline_formats(mid: str) -> tuple[str, ...]:
    data = _request_json(f"https://makeronline.com/api/mold/detail?id={urllib.parse.quote(str(mid))}", timeout=7.5,
                         headers={"Referer": "https://makeronline.com/"})
    return tuple(_collect_formats(data))

@lru_cache(maxsize=1024)
def _nexprint_formats(mid: str) -> tuple[str, ...]:
    params = urllib.parse.urlencode({"id": str(mid)})
    data = _request_json(f"https://www.nexprint.com/gateway/api/v1/model-library-server/model-base-info/get?{params}", timeout=7.5,
                         headers={"Referer": "https://www.nexprint.com/"})
    return tuple(_collect_formats(data))

def _hydrate_detail_formats(items: list[dict], source_id: str, wanted: list[str]) -> list[dict]:
    if not wanted or not items:
        return items
    getter = _makeronline_formats if source_id == "makeronline" else _nexprint_formats
    # File metadata calls are only made when the user explicitly asks for a format.
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {pool.submit(getter, str(it.get("external_id") or "")): it for it in items if it.get("external_id")}
        for fut in as_completed(jobs):
            it = jobs[fut]
            try:
                it["formats"] = list(fut.result())
                it["format_verified"] = True
            except Exception:
                it["formats"] = []
                it["format_verified"] = False
    verified = sum(1 for it in items if it.get("format_verified"))
    if items and verified == 0:
        raise RuntimeError(f"{source_id}: nie udało się zweryfikować formatów plików")
    return [it for it in items if _format_match(it, wanted)]

PRINTABLES_SEARCH = """
query SearchModels($query: String!, $limit: Int, $offset: Int, $ordering: SearchChoicesEnum) {
  result: searchPrints2(query: $query, printType: print, limit: $limit, offset: $offset, ordering: $ordering) {
    items {
      id name slug ratingAvg likesCount downloadCount datePublished
      user { publicUsername handle }
      image { filePath }
      license { name }
    }
    totalCount
  }
}
"""


def _search_printables(plan: dict, limit: int, precision: str, formats: list[str]) -> list[dict]:
    out: dict[str, dict] = {}
    queries = plan["search_queries"][:4]
    page_size = 30
    scan_budget = min(max(limit + 180, 300), 1400)
    errors = []
    scanned = 0

    def fetch_page(q: str, offset: int) -> tuple[int, int | None]:
        nonlocal scanned
        payload = {
            "operationName": "SearchModels", "query": PRINTABLES_SEARCH,
            "variables": {"query": q, "limit": page_size, "offset": offset, "ordering": "best_match"},
        }
        data = _request_json("https://api.printables.com/graphql/", payload=payload, timeout=8.5, headers={"Referer": "https://www.printables.com/"})
        if data.get("errors"):
            raise RuntimeError(str(data["errors"][0].get("message") or "GraphQL error"))
        root = (((data or {}).get("data") or {}).get("result") or {})
        rows = root.get("items") or []
        total = root.get("totalCount")
        scanned += len(rows)
        for m in rows:
            mid = str(m.get("id") or "").strip()
            if not mid:
                continue
            slug = str(m.get("slug") or "").strip()
            url = f"https://www.printables.com/model/{mid}" + (f"-{slug}" if slug else "")
            image_info = m.get("image") or {}
            fp = image_info.get("filePath") if isinstance(image_info, dict) else ""
            image = f"https://media.printables.com/{str(fp).lstrip('/')}" if fp else ""
            author = m.get("user") if isinstance(m.get("user"), dict) else {}
            lic = m.get("license") if isinstance(m.get("license"), dict) else {}
            item = _base_item(
                "printables", title=m.get("name") or "Model 3D", url=url, image=image,
                image_origin="native-search" if image else "", author=author.get("publicUsername") or author.get("handle") or "",
                downloads=m.get("downloadCount"), likes=m.get("likesCount"), rating=m.get("ratingAvg"),
                published=m.get("datePublished") or "", license=lic.get("name") or "", matched_query=q, external_id=mid,
            )
            item = _finalize_item(item, plan, "printables", precision, force_keep=(q == queries[0] and precision != "strict"))
            if item:
                key = _canonical_key(url, "printables")
                if key not in out or item["score"] > out[key]["score"]:
                    out[key] = item
        return len(rows), int(total) if isinstance(total, int) else None

    # First page of several keyword variants improves recall.
    primary_got, primary_total = 0, None
    if queries:
        try:
            primary_got, primary_total = fetch_page(queries[0], 0)
        except Exception as exc:
            errors.append(str(exc))
        for q in queries[1:]:
            try:
                fetch_page(q, 0)
            except Exception as exc:
                errors.append(str(exc))

    # Paginate by the ACTUAL number returned, not the requested page size.
    # Some services cap a page below our requested limit; treating that as EOF
    # was the main reason BETA 1.1 stopped after only a few dozen results.
    if queries and primary_got > 0:
        offset = primary_got
        stalls = 0
        while len(out) < limit and scanned < scan_budget:
            before = len(out)
            try:
                got, total = fetch_page(queries[0], offset)
            except Exception as exc:
                errors.append(str(exc)); break
            if got <= 0:
                break
            offset += got
            stalls = stalls + 1 if len(out) == before else 0
            if stalls >= 2:
                break
            known_total = total if total is not None else primary_total
            if known_total is not None and offset >= known_total:
                break

    if not out and errors:
        raise RuntimeError("; ".join(errors[:2]))
    items = sorted(out.values(), key=lambda x: -x["score"])[:limit]
    if formats and items:
        try:
            mapping = _printables_batch_formats([str(x.get("external_id") or "") for x in items])
            for it in items:
                it["formats"] = mapping.get(str(it.get("external_id") or ""), [])
                it["format_verified"] = True
            items = [it for it in items if _format_match(it, formats)]
        except Exception as exc:
            raise RuntimeError(f"Printables format verification failed: {exc}")
    return items


def _makerworld_browser_collect(plan: dict, limit: int, precision: str) -> list[dict]:
    """Collect MakerWorld cards from the public search page with a server-side headless browser.

    In Docker/Linux this uses Chromium. On a Windows development machine it can use Edge.
    No model files are downloaded.
    """
    query = str((plan.get("search_queries") or [plan.get("translated") or plan.get("original") or ""])[0]).strip()
    if not query:
        return []
    browser_kind = os.environ.get("HUNTER_BROWSER", "chromium" if os.name != "nt" else "edge").lower().strip()
    if browser_kind in {"off", "disabled", "none", "0", "false"}:
        return []
    try:
        from selenium import webdriver
        from selenium.webdriver.support.ui import WebDriverWait
    except Exception as exc:
        raise RuntimeError(f"MakerWorld WWW: brak Selenium ({exc})")

    target = max(1, min(int(limit), 1000))
    url = "https://makerworld.com/en/search/models?" + urllib.parse.urlencode({"keyword": query})
    out: dict[str, dict] = {}
    driver = None
    with _MW_BROWSER_SEM:
        try:
            if browser_kind in {"chrome", "chromium"}:
                from selenium.webdriver.chrome.options import Options as ChromeOptions
                from selenium.webdriver.chrome.service import Service as ChromeService
                opts = ChromeOptions()
                chrome_bin = os.environ.get("CHROME_BIN", "").strip()
                if chrome_bin:
                    opts.binary_location = chrome_bin
                for arg in (
                    "--headless=new", "--disable-gpu", "--window-size=1600,1200", "--lang=en-US",
                    "--disable-notifications", "--no-first-run", "--no-sandbox", "--disable-dev-shm-usage",
                    "--disable-extensions", "--disable-background-networking",
                ):
                    opts.add_argument(arg)
                driver_path = os.environ.get("CHROMEDRIVER_PATH", "").strip()
                driver = webdriver.Chrome(service=ChromeService(driver_path), options=opts) if driver_path else webdriver.Chrome(options=opts)
            else:
                from selenium.webdriver.edge.options import Options as EdgeOptions
                opts = EdgeOptions()
                for arg in ("--headless=new", "--disable-gpu", "--window-size=1600,1200", "--lang=en-US", "--disable-notifications", "--no-first-run"):
                    opts.add_argument(arg)
                driver = webdriver.Edge(options=opts)
            driver.set_page_load_timeout(35)
            driver.get(url)
            WebDriverWait(driver, 20).until(lambda d: d.execute_script("return document.readyState") in ("interactive", "complete"))
            time.sleep(1.0)

            blocked = str(driver.title or "").lower()
            body_head = str(driver.execute_script("return (document.body && document.body.innerText || '').slice(0,1200)") or "").lower()
            if any(x in (blocked + " " + body_head) for x in ("just a moment", "verify you are human", "access denied", "captcha")):
                raise RuntimeError("MakerWorld WWW: strona zażądała weryfikacji przeglądarki")

            js = r"""
            const rows = [];
            const seen = new Set();
            const anchors = Array.from(document.querySelectorAll('a[href*="/models/"]'));
            for (const a of anchors) {
              const href = a.href || '';
              const m = href.match(/\/models\/(\d+)(?:[-/?#]|$)/);
              if (!m || seen.has(m[1])) continue;
              seen.add(m[1]);
              let card = a;
              let best = null;
              for (let i=0; i<8 && card; i++, card=card.parentElement) {
                const txt = (card.innerText || '').trim();
                if (txt.length >= 2 && txt.length <= 900) {
                  const ml = card.querySelectorAll ? card.querySelectorAll('a[href*="/models/"]').length : 0;
                  if (ml <= 6) best = card;
                }
              }
              card = best || a;
              const same = Array.from(card.querySelectorAll ? card.querySelectorAll('a[href*="/models/"]') : [a])
                .filter(x => (x.href || '').match(/\/models\/(\d+)(?:[-/?#]|$)/)?.[1] === m[1]);
              const labels = same.map(x => (x.getAttribute('title') || x.getAttribute('aria-label') || x.innerText || '').trim())
                .filter(x => x && x.length < 240);
              labels.sort((x,y) => y.length - x.length);
              let title = labels[0] || '';
              if (!title) {
                const slug = href.split('/models/')[1].split(/[?#]/)[0].replace(/^\d+-?/, '').replace(/-/g, ' ');
                title = slug || ('Model ' + m[1]);
              }
              const imgs = Array.from(card.querySelectorAll ? card.querySelectorAll('img') : []);
              imgs.sort((x,y) => ((y.clientWidth||0)*(y.clientHeight||0)) - ((x.clientWidth||0)*(x.clientHeight||0)));
              let image = '';
              for (const img of imgs) {
                image = img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original') || '';
                if (image && /^https?:/.test(image)) break;
              }
              rows.push({id:m[1], href, title, image, text:(card.innerText||'').trim().slice(0,800)});
            }
            return rows;
            """

            stalls = 0
            rounds = 0
            max_rounds = 90 if target >= 1000 else 42
            while len(out) < target and stalls < 8 and rounds < max_rounds:
                rounds += 1
                rows = driver.execute_script(js) or []
                before = len(out)
                for r in rows:
                    did = str(r.get("id") or "").strip()
                    if not did:
                        continue
                    href = str(r.get("href") or f"https://makerworld.com/en/models/{did}")
                    title = str(r.get("title") or f"Model {did}").strip()
                    desc = str(r.get("text") or "").strip()
                    image = str(r.get("image") or "").strip()
                    item = _base_item(
                        "makerworld", title=title, url=href, description=desc, image=image,
                        image_origin="makerworld-web" if image else "", matched_query=query,
                        external_id=did, format_verified=False, retrieval="web",
                    )
                    item = _finalize_item(item, plan, "makerworld", precision, force_keep=True)
                    key = _canonical_key(href, "makerworld")
                    if key not in out or float(item.get("score") or 0) > float(out[key].get("score") or 0):
                        out[key] = item
                stalls = stalls + 1 if len(out) == before else 0
                if len(out) >= target:
                    break
                no_more = bool(driver.execute_script("return /no more data/i.test((document.body && document.body.innerText)||'')"))
                if no_more:
                    break
                driver.execute_script("window.scrollTo(0, Math.max(document.body.scrollHeight - 900, 0));")
                time.sleep(0.45)
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.65)

            return sorted(out.values(), key=lambda x: -float(x.get("score") or 0))[:target]
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

def _search_makerworld(plan: dict, limit: int, precision: str, formats: list[str]) -> list[dict]:
    out: dict[str, dict] = {}
    errors = []
    queries = plan["search_queries"][:4]
    page_size = 30
    scan_budget = min(max(limit + 180, 300), 1400)
    scanned = 0

    def fetch_page(q: str, offset: int) -> int:
        nonlocal scanned
        params = urllib.parse.urlencode({"keyword": q, "limit": page_size, "offset": offset})
        data = _request_json(f"https://api.bambulab.com/v1/search-service/select/design2?{params}", timeout=8.5, headers={"Referer": "https://makerworld.com/"})
        models = _best_object_list(data)
        scanned += len(models)
        for m in models:
            did = _first(m, "id", "designId", "design_id")
            title = _first(m, "title", "name", "designTitle", "modelName", default="Model 3D")
            raw_url = _first(m, "url", "link", "designUrl", default="")
            if raw_url and str(raw_url).startswith("http"):
                url = str(raw_url)
            elif did is not None:
                url = f"https://makerworld.com/en/models/{did}"
            else:
                continue
            image = _first_http(_first(m, "coverUrl", "cover", "images", "image", "thumbnail", "pictures", default=""))
            fmts = _collect_formats(m.get("designExtension") or m)
            item = _base_item(
                "makerworld", title=title, url=url,
                description=_first(m, "summary", "description", "desc", default=""),
                image=image, image_origin="native-search" if image else "", author=_author_name(m),
                downloads=_num(m, "downloadCount", "downloads", "download_count"),
                likes=_num(m, "likeCount", "likes", "like_count"), rating=_num(m, "rating", "ratingAvg", "score"),
                published=_first(m, "createdAt", "createTime", "datePublished", "publishedAt", default=""),
                license=_first(m, "license", "licenseName", default="") if isinstance(_first(m, "license", "licenseName", default=""), str) else "",
                matched_query=q, external_id=str(did), formats=fmts, format_verified=bool(fmts), retrieval="api",
            )
            item = _finalize_item(item, plan, "makerworld", precision, force_keep=(q == queries[0] and precision != "strict"))
            if item:
                key = _canonical_key(url, "makerworld")
                if key not in out or item["score"] > out[key]["score"]:
                    out[key] = item
        return len(models)

    primary_got = 0
    if queries:
        try:
            primary_got = fetch_page(queries[0], 0)
        except Exception as exc:
            errors.append(str(exc))
        for q in queries[1:]:
            try:
                fetch_page(q, 0)
            except Exception as exc:
                errors.append(str(exc))

    if queries and primary_got > 0:
        offset = primary_got
        stalls = 0
        while len(out) < limit and scanned < scan_budget:
            before = len(out)
            try:
                got = fetch_page(queries[0], offset)
            except Exception as exc:
                errors.append(str(exc)); break
            if got <= 0:
                break
            offset += got
            stalls = stalls + 1 if len(out) == before else 0
            if stalls >= 2:
                break

    if not out and errors:
        raise RuntimeError("; ".join(errors[:2]))
    # Standard and Full modes can mirror the real MakerWorld web search via a server-side headless browser.
    # This supplements the public API, which may expose only a small subset for some queries.
    if limit >= 300 and not formats:
        try:
            web_items = _makerworld_browser_collect(plan, limit, precision)
            for it in web_items:
                key = _canonical_key(it["url"], "makerworld")
                if key not in out or float(it.get("score") or 0) > float(out[key].get("score") or 0):
                    out[key] = it
        except Exception:
            pass

    items = sorted(out.values(), key=lambda x: -float(x.get("score") or 0))[:limit]
    if formats:
        if items and not any(it.get("format_verified") for it in items):
            raise RuntimeError("MakerWorld: brak metadanych formatów w odpowiedzi wyszukiwania")
        items = [it for it in items if _format_match(it, formats)]
    return items


def _search_makeronline(plan: dict, limit: int, precision: str, formats: list[str]) -> list[dict]:
    out: dict[str, dict] = {}
    errors = []
    queries = plan["search_queries"][:3]
    page_size = 30
    scan_budget = min(max(limit + 180, 300), 1400)
    scanned = 0

    def fetch_page(q: str, page: int) -> int:
        nonlocal scanned
        payload = {"keyword": q, "page": page, "page_size": page_size, "print_type": 0, "search": 1}
        data = _request_json("https://makeronline.com/api/search/model", payload=payload, timeout=8.5,
                             headers={"Referer": "https://makeronline.com/"})
        models = _best_object_list(data)
        scanned += len(models)
        for m in models:
            mid = _first(m, "id", "model_id", "modelId", "mold_id")
            title = str(_first(m, "name", "title", "model_name", "modelName", default="Model 3D"))
            raw_url = _first(m, "url", "link", "detail_url", default="")
            if raw_url and str(raw_url).startswith("http"):
                url = str(raw_url)
            elif mid is not None:
                url = f"https://makeronline.com/en/model/{urllib.parse.quote(title, safe='')}/{mid}.html"
            else:
                continue
            image = _first_http(_first(m, "cover", "cover_url", "coverUrl", "image", "images", "thumbnail", "pic", default=""))
            item = _base_item(
                "makeronline", title=title, url=url,
                description=_first(m, "description", "desc", "summary", default=""), image=image,
                image_origin="native-search" if image else "", author=_author_name(m),
                downloads=_num(m, "download_count", "downloadCount", "downloads", "download_num"),
                likes=_num(m, "like_count", "likeCount", "likes", "collect_count"),
                rating=_num(m, "rating", "score"), published=_first(m, "created_at", "createdAt", "upload_time", "publish_time", default=""),
                license=str(_first(m, "license_name", "license", default="") or ""), matched_query=q, external_id=str(mid),
            )
            item = _finalize_item(item, plan, "makeronline", precision, force_keep=(q == queries[0] and precision != "strict"))
            if item:
                key = _canonical_key(url, "makeronline")
                if key not in out or item["score"] > out[key]["score"]:
                    out[key] = item
        return len(models)

    primary_got = 0
    if queries:
        try:
            primary_got = fetch_page(queries[0], 1)
        except Exception as exc:
            errors.append(str(exc))
        for q in queries[1:]:
            try:
                fetch_page(q, 1)
            except Exception as exc:
                errors.append(str(exc))
    if queries and primary_got > 0:
        page = 2
        stalls = 0
        while len(out) < limit and scanned < scan_budget:
            before = len(out)
            try:
                got = fetch_page(queries[0], page)
            except Exception as exc:
                errors.append(str(exc)); break
            if got <= 0:
                break
            page += 1
            stalls = stalls + 1 if len(out) == before else 0
            if stalls >= 2:
                break

    if not out and errors:
        raise RuntimeError("; ".join(errors[:2]))
    items = sorted(out.values(), key=lambda x: -x["score"])[:limit]
    return _hydrate_detail_formats(items, "makeronline", formats) if formats else items


def _search_nexprint(plan: dict, limit: int, precision: str, formats: list[str]) -> list[dict]:
    out: dict[str, dict] = {}
    errors = []
    queries = plan["search_queries"][:3]
    page_size = 30
    scan_budget = min(max(limit + 180, 300), 1400)
    scanned = 0

    def fetch_page(q: str, page_no: int) -> int:
        nonlocal scanned
        params = urllib.parse.urlencode({"keyword": q, "pageNo": page_no, "pageSize": page_size})
        data = _request_json(f"https://www.nexprint.com/gateway/api/v1/model-library-server/model-base-info/search?{params}", timeout=8.5,
                             headers={"Referer": "https://www.nexprint.com/"})
        models = _best_object_list(data)
        scanned += len(models)
        for m in models:
            mid = _first(m, "id", "modelId", "model_id", "modelCode", "modelNo")
            title = str(_first(m, "name", "title", "modelName", "model_name", default="Model 3D"))
            raw_url = _first(m, "url", "link", "detailUrl", default="")
            if raw_url and str(raw_url).startswith("http"):
                url = str(raw_url)
            elif mid is not None:
                url = f"https://www.nexprint.com/en/models/{mid}"
            else:
                continue
            image = _first_http(_first(m, "coverUrl", "cover", "coverImage", "image", "images", "thumbnailUrl", "thumbnail", default=""))
            item = _base_item(
                "nexprint", title=title, url=url,
                description=_first(m, "description", "desc", "summary", "brief", default=""), image=image,
                image_origin="native-search" if image else "", author=_author_name(m),
                downloads=_num(m, "downloadCount", "downloads", "downloadNum", "download_count"),
                likes=_num(m, "likeCount", "likes", "likeNum", "collectCount"),
                rating=_num(m, "rating", "score"), published=_first(m, "publishTime", "createdAt", "createTime", default=""),
                license=str(_first(m, "licenseName", "licenseType", "license", default="") or ""), matched_query=q, external_id=str(mid),
            )
            item = _finalize_item(item, plan, "nexprint", precision, force_keep=(q == queries[0] and precision != "strict"))
            if item:
                key = _canonical_key(url, "nexprint")
                if key not in out or item["score"] > out[key]["score"]:
                    out[key] = item
        return len(models)

    primary_got = 0
    if queries:
        try:
            primary_got = fetch_page(queries[0], 1)
        except Exception as exc:
            errors.append(str(exc))
        for q in queries[1:]:
            try:
                fetch_page(q, 1)
            except Exception as exc:
                errors.append(str(exc))
    if queries and primary_got > 0:
        page_no = 2
        stalls = 0
        while len(out) < limit and scanned < scan_budget:
            before = len(out)
            try:
                got = fetch_page(queries[0], page_no)
            except Exception as exc:
                errors.append(str(exc)); break
            if got <= 0:
                break
            page_no += 1
            stalls = stalls + 1 if len(out) == before else 0
            if stalls >= 2:
                break

    if not out and errors:
        raise RuntimeError("; ".join(errors[:2]))
    items = sorted(out.values(), key=lambda x: -x["score"])[:limit]
    return _hydrate_detail_formats(items, "nexprint", formats) if formats else items


def _title_words(text: str) -> set[str]:
    stop = {"model", "models", "download", "free", "printable", "printables", "thingiverse", "makerworld", "cults3d", "stl", "3d"}
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_-]+", unescape(text).lower()) if len(w) >= 3 and w not in stop}


def _looks_generic_image(candidate: dict, source_name: str) -> bool:
    title = str(candidate.get("title") or "").strip().lower()
    image = str(candidate.get("image") or candidate.get("thumbnail") or "").lower()
    if title in {source_name.lower(), f"{source_name.lower()} - digital designs for physical objects"}:
        return True
    return any(x in image for x in ("logo", "favicon", "brandmark", "default-og", "social-share", "thingiverse-logo"))


def _assign_index_images(items: list[dict], source_id: str, visual_query: str):
    if not items:
        return
    # Thingiverse currently exposes a generic blue social card so often that it is
    # more misleading than a clean placeholder. Do not call it a model thumbnail.
    if source_id == "thingiverse":
        return
    cfg = SOURCES[source_id]
    candidates = _ddgs_images(f"site:{cfg['domain']} {visual_query}", max_results=max(16, min(36, len(items) * 3)))
    if not candidates:
        return

    by_key: dict[str, dict] = {}
    cleaned = []
    for c in candidates:
        if _looks_generic_image(c, cfg["name"]):
            continue
        page = c.get("url") or ""
        key = _canonical_key(page, source_id)
        if key:
            by_key.setdefault(key, c)
        cleaned.append(c)

    for item in items:
        if item.get("image"):
            continue
        exact = by_key.get(_canonical_key(item["url"], source_id))
        if exact:
            item["image"] = exact.get("thumbnail") or exact.get("image") or ""
            item["image_origin"] = "index-exact"
            continue
        iw = _title_words(item.get("title", ""))
        if not iw:
            continue
        best, best_score = None, 0.0
        for c in cleaned:
            cw = _title_words(c.get("title", ""))
            if not cw:
                continue
            overlap = len(iw & cw) / max(1, len(iw | cw))
            seq = difflib.SequenceMatcher(None, " ".join(sorted(iw)), " ".join(sorted(cw))).ratio()
            score = overlap * 0.72 + seq * 0.28
            if score > best_score:
                best_score, best = score, c
        if best and best_score >= 0.55:
            item["image"] = best.get("thumbnail") or best.get("image") or ""
            item["image_origin"] = "index-title"


def _search_index_source(source_id: str, plan: dict, limit: int, precision: str, formats: list[str]) -> list[dict]:
    cfg = SOURCES[source_id]
    # Index fallbacks cannot be paged reliably like native APIs; keep them bounded.
    limit = min(limit, 60)
    queries = plan["search_queries"][:3]
    if formats:
        # Index-only sources do not expose structured file metadata anonymously.
        # Probe each selected extension and only keep results that explicitly mention it.
        queries = [f"{q} {fmt}" for q in queries for fmt in formats][:max(3, min(8, len(queries) * len(formats)))]
    out: dict[str, dict] = {}
    failures = []

    # Fan-out plain natural-language variants instead of one giant Boolean query.
    # Search engines rank these much better and we deduplicate afterward.
    for q in queries:
        try:
            raw = _ddgs_text(f"site:{cfg['domain']} {q}", max_results=max(10, min(16, limit)))
        except Exception as exc:
            failures.append(str(exc))
            continue
        for r in raw:
            href = r.get("href") or r.get("url") or ""
            if not href or cfg["domain"] not in href.lower():
                continue
            # Only actual model pages enter the normal result grid.
            if cfg.get("path_hint") and cfg["path_hint"].lower() not in href.lower():
                continue
            desc = re.sub(r"\s+", " ", (r.get("body") or "").strip())
            evidence = _collect_formats({"title": r.get("title") or "", "description": desc, "url": href})
            item = _base_item(
                source_id, title=(r.get("title") or "Model 3D").strip(), url=href,
                description=desc, matched_query=q, formats=evidence, format_verified=bool(evidence),
            )
            if formats and not _format_match(item, formats):
                continue
            item = _finalize_item(item, plan, source_id, precision)
            if not item:
                continue
            key = _canonical_key(href, source_id)
            if key not in out or item["score"] > out[key]["score"]:
                out[key] = item
        if len(out) >= limit:
            break

    items = sorted(out.values(), key=lambda x: -x["score"])[:limit]
    if not items and failures:
        raise RuntimeError("; ".join(failures[:2]))
    if items:
        try:
            _assign_index_images(items, source_id, queries[0])
        except Exception:
            pass
    return items


@lru_cache(maxsize=384)
def _search_source_cached(source_id: str, plan_json: str, limit: int, precision: str, formats_json: str):
    plan = json.loads(plan_json)
    formats = json.loads(formats_json)
    if source_id == "printables":
        try:
            return json.dumps(_search_printables(plan, limit, precision, formats), ensure_ascii=False)
        except Exception:
            # Fallback keeps the program useful if the unofficial GraphQL schema changes.
            return json.dumps(_search_index_source(source_id, plan, limit, precision, formats), ensure_ascii=False)
    if source_id == "makerworld":
        try:
            return json.dumps(_search_makerworld(plan, limit, precision, formats), ensure_ascii=False)
        except Exception:
            return json.dumps(_search_index_source(source_id, plan, limit, precision, formats), ensure_ascii=False)
    if source_id == "makeronline":
        try:
            return json.dumps(_search_makeronline(plan, limit, precision, formats), ensure_ascii=False)
        except Exception:
            return json.dumps(_search_index_source(source_id, plan, limit, precision, formats), ensure_ascii=False)
    if source_id == "nexprint":
        try:
            return json.dumps(_search_nexprint(plan, limit, precision, formats), ensure_ascii=False)
        except Exception:
            return json.dumps(_search_index_source(source_id, plan, limit, precision, formats), ensure_ascii=False)
    return json.dumps(_search_index_source(source_id, plan, limit, precision, formats), ensure_ascii=False)


def _search_source(source_id: str, plan: dict, limit: int, precision: str, formats: list[str]):
    compact_plan = {
        "original": plan.get("original", ""), "translated": plan.get("translated", ""),
        "anchors": plan.get("anchors", []), "base_terms": plan.get("base_terms", []),
        "concepts": plan.get("concepts", []), "search_queries": plan.get("search_queries", [])[:12],
    }
    fresh = json.loads(_search_source_cached(source_id, json.dumps(compact_plan, ensure_ascii=False, sort_keys=True), limit, precision, json.dumps(sorted(formats))))

    cache_key = (source_id, str(compact_plan.get("original") or "").casefold(), str(compact_plan.get("translated") or "").casefold(), precision, tuple(sorted(formats)))
    with _SOURCE_UNION_LOCK:
        bucket = _SOURCE_UNION_CACHE.setdefault(cache_key, {})
        for item in fresh:
            key = _canonical_key(item.get("url") or "", source_id)
            if key not in bucket or float(item.get("score") or 0) > float(bucket[key].get("score") or 0):
                bucket[key] = item
        merged = list(bucket.values())
    merged.sort(key=lambda x: (-float(x.get("score") or 0), -(int(x.get("downloads") or 0) if str(x.get("downloads") or "").isdigit() else 0), str(x.get("title") or "").casefold()))
    return merged[:limit]


def _direct_links(query: str, selected_sources: list[str]):
    q = urllib.parse.quote_plus(query)
    path_q = urllib.parse.quote(query, safe="")
    links = []
    for sid in selected_sources:
        cfg = SOURCES[sid]
        template = cfg["search"]
        value = path_q if sid == "thangs" else q
        links.append({"source": sid, "source_name": cfg["name"], "mark": cfg["mark"], "url": template.format(q=value)})
    return links


class _MetaImageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        attrs = {str(k).lower(): v for k, v in attrs if k}
        if tag.lower() == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"} and attrs.get("content"):
                self.candidates.append(attrs["content"])
        if tag.lower() == "link" and (attrs.get("rel") or "").lower() == "image_src" and attrs.get("href"):
            self.candidates.append(attrs["href"])


def _is_allowed_page_url(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower().removeprefix("www.")
        return p.scheme in {"http", "https"} and any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)
    except Exception:
        return False


@lru_cache(maxsize=1024)
def _extract_thumbnail(page_url: str) -> str:
    if not _is_allowed_page_url(page_url):
        return ""
    try:
        host = (urllib.parse.urlsplit(page_url).hostname or "").lower()
        if host.endswith("thingiverse.com"):
            return ""
    except Exception:
        pass
    try:
        req = urllib.request.Request(page_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        with urllib.request.urlopen(req, timeout=4.5) as response:
            ctype = (response.headers.get("Content-Type") or "").lower()
            if "text/html" not in ctype and "application/xhtml" not in ctype:
                return ""
            raw = response.read(520_000)
            enc = response.headers.get_content_charset() or "utf-8"
        parser = _MetaImageParser()
        parser.feed(raw.decode(enc, errors="ignore"))
        for candidate in parser.candidates:
            candidate = candidate.strip()
            if not candidate or candidate.startswith("data:"):
                continue
            url = urllib.parse.urljoin(page_url, candidate)
            low = url.lower()
            if any(x in low for x in ("logo", "favicon", "default-og", "social-share", "thingiverse-logo")):
                continue
            if url.startswith(("http://", "https://")):
                return url
    except Exception:
        return ""
    return ""


@app.get("/")
def index():
    return render_template("index.html", sources=SOURCES)


@app.get("/api/expand")
def expand():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Brak zapytania"}), 400
    translated, mode = _translator(q)
    data = build_search_plan(q, translated)
    data["translation_mode"] = mode
    return jsonify(data)


@app.post("/api/search")
def api_search():
    payload = request.get_json(silent=True) or {}
    q = str(payload.get("q") or "").strip()
    if not q:
        return jsonify({"error": "Wpisz czego szukasz."}), 400

    selected = [s for s in payload.get("sources", []) if s in SOURCES]
    if not selected:
        selected = list(SOURCES.keys())
    formats = [_norm_format(x) for x in payload.get("formats", []) if _norm_format(x) in SUPPORTED_FORMATS]
    limit = max(20, min(int(payload.get("limit", 300)), 1000))
    smart = bool(payload.get("smart", True))
    precision = str(payload.get("precision") or "balanced")
    if precision not in {"strict", "balanced", "wide"}:
        precision = "balanced"

    translated, mode = _translator(q) if smart else (q, "disabled")
    if smart:
        plan = build_search_plan(q, translated, max_queries=12)
        boolean_query, _ = build_boolean_query(q, translated, formats=formats)
    else:
        plan = {"original": q, "translated": q, "concepts": [], "anchors": [], "base_terms": q.split(), "soft_terms": q.split(), "variants": [q], "search_queries": [q]}
        boolean_query = q

    primary_query = plan["search_queries"][0] if plan.get("search_queries") else translated
    start = time.time()
    results: list[dict] = []
    errors = []
    responded_sources: list[str] = []

    # Sources are independent. Native APIs + index sources execute in parallel.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(selected)))) as pool:
        futures = {pool.submit(_search_source, sid, plan, limit, precision, formats): sid for sid in selected}
        for fut in as_completed(futures):
            sid = futures[fut]
            try:
                results.extend(fut.result())
                responded_sources.append(sid)
            except Exception as exc:
                errors.append({"source": sid, "message": str(exc)[:220]})

    unique: dict[str, dict] = {}
    for item in results:
        key = _canonical_key(item["url"], item["source"])
        if key not in unique or float(item.get("score") or 0) > float(unique[key].get("score") or 0):
            unique[key] = item
    results = sorted(unique.values(), key=lambda x: (-float(x.get("score") or 0), -(int(x.get("downloads") or 0) if str(x.get("downloads") or "").isdigit() else 0), x["source_name"].lower()))

    source_counts = {sid: 0 for sid in selected}
    image_count = 0
    quality_counts = {"exact": 0, "good": 0, "broad": 0}
    format_verified_count = 0
    retrieval_counts = {"makerworld_api": 0, "makerworld_web": 0}
    for item in results:
        source_counts[item["source"]] = source_counts.get(item["source"], 0) + 1
        if item.get("image"):
            image_count += 1
        if item.get("format_verified"):
            format_verified_count += 1
        if item.get("source") == "makerworld":
            if item.get("retrieval") == "web":
                retrieval_counts["makerworld_web"] += 1
            elif item.get("retrieval") == "api":
                retrieval_counts["makerworld_api"] += 1
        ql = item.get("match_quality") or "broad"
        quality_counts[ql] = quality_counts.get(ql, 0) + 1

    return jsonify({
        "query": q, "translated": translated, "translation_mode": mode,
        "boolean_query": boolean_query, "visual_query": primary_query,
        "terms": plan, "results": results, "source_counts": source_counts,
        "image_count": image_count, "quality_counts": quality_counts,
        "direct_links": _direct_links(primary_query, selected), "errors": errors,
        "source_status": {
            "selected": selected,
            "responded": sorted(set(responded_sources)),
            "failed": [e["source"] for e in errors],
        },
        "precision": precision, "formats": formats, "format_verified_count": format_verified_count, "limit": limit,
        "retrieval_counts": retrieval_counts,
        "elapsed_ms": int((time.time() - start) * 1000),
    })


@app.get("/api/thumbnail")
def api_thumbnail():
    page_url = (request.args.get("url") or "").strip()
    if not page_url or not _is_allowed_page_url(page_url):
        return jsonify({"image": ""}), 400
    return jsonify({"image": _extract_thumbnail(page_url)})


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "product": "3D Model Hunter", "version": WEB_VERSION, "mode": "web", "sources": list(SOURCES)})



if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True, use_reloader=False)
