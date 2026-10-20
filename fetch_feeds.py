#!/usr/bin/env python3
"""公開RSS / Atom / YouTube公式RSS / Misskey公開API / HTMLページから記事を集めて
data/feed.json を作る収集スクリプト。

- APIキー・パスワード・Cookie は使いません（認証が必要な情報源は対象外です）。
- 1つの情報源が失敗しても、ほかの情報源の処理は続けます。
- robots.txt で許可されていないURLにはアクセスしません。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
import traceback
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

warnings.filterwarnings("ignore", module="bs4")

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "sources.json"
FEED_PATH = ROOT / "data" / "feed.json"
STATE_PATH = ROOT / "data" / "state.json"

UTC = timezone.utc
JST = timezone(timedelta(hours=9))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

ROBOT_TOKEN = "MyFeedReader"
VALID_TYPES = ("rss", "youtube", "misskey", "html")
VALID_CATEGORIES = ("news", "blog", "video", "sns")
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_REDIRECTS = 5
MAX_ERRORS_SHOWN = 50
REWRITE_AFTER = timedelta(hours=2)   # 変化がなくても、これより古ければ書き直す
INTERVAL_TOLERANCE_MIN = 10          # 実行時刻のずれを吸収する余裕（分）

TRACKING_PARAMS = {
    "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid",
    "igshid", "mc_cid", "mc_eid", "ref_src",
}

DEFAULT_SETTINGS = {
    "timeout_seconds": 20,
    "max_response_bytes": 5_000_000,
    "default_max_items": 20,
    "default_interval_minutes": 60,
    "request_delay_seconds": 1.0,
    "keep_per_source": 100,
    "retention_days": 30,
    "max_total_items": 600,
    "max_runtime_seconds": 480,
    "user_agent": "",
}


class FetchError(Exception):
    """利用者に見せてよい、短い日本語メッセージを持つエラー。"""


# ---------------------------------------------------------------- 基本ユーティリティ

def num(value, default, low, high):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, low), high)


def to_iso(dt: datetime) -> str:
    return dt.astimezone(JST).isoformat(timespec="seconds")


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(UTC)


def parse_feed_date(text):
    """RSS(RFC 822)とAtom/Dublin Core(ISO 8601)の日付を読む。"""
    text = (text or "").strip()
    if not text:
        return None
    dt = parse_iso(text)
    if dt:
        return dt
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_date_text(text, fmt=None):
    """HTML抽出用。日本語の日付表記も読む。時刻の記載がなければJSTとみなす。"""
    text = (text or "").strip()
    if not text:
        return None
    if fmt:
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=JST)
            return dt.astimezone(UTC)
        except ValueError:
            pass
    dt = parse_iso(text)
    if dt:
        return dt
    m = re.search(r"(\d{4})\s*[./年-]\s*(\d{1,2})\s*[./月-]\s*(\d{1,2})", text)
    if not m:
        return None
    hh = mm = 0
    t = re.search(r"(\d{1,2}):(\d{2})", text)
    if t:
        hh, mm = int(t.group(1)), int(t.group(2))
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hh, mm, tzinfo=JST).astimezone(UTC)
    except ValueError:
        return None


def clean_text(value, limit, sep=" "):
    """HTMLタグを取り除き、空白をまとめ、長ければ省略する。"""
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(sep)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def is_tracking(key: str) -> bool:
    k = key.lower()
    return k in TRACKING_PARAMS or k.startswith("utm_")


def clean_url(url) -> str:
    """表示用URL：http/httpsのみ。フラグメントと追跡用パラメータを除く。"""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url or len(url) > 2000:
        return ""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host or parts.username or parts.password:
        return ""
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    kept = [(k, v) for k, v in pairs if not is_tracking(k)]
    query = parts.query if len(kept) == len(pairs) else urlencode(kept)
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def dedupe_key(url: str) -> str:
    """重複判定用：http/https、www有無、末尾スラッシュ、パラメータの順序を無視する。"""
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{host}{path}?{query}"


def https_image(url, base=None) -> str:
    if not url or not isinstance(url, str):
        return ""
    full = urljoin(base, url.strip()) if base else url.strip()
    cleaned = clean_url(full)
    if cleaned.startswith("http://"):
        cleaned = "https://" + cleaned[len("http://"):]
    return cleaned


def header_safe(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 200 and re.fullmatch(r"[\x20-\x7e]+", value) is not None


def check_public_url(url: str) -> None:
    """http/https以外や、社内・ローカルのアドレスへのアクセスを防ぐ。"""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        raise FetchError("URLの形式が不正です")
    if parts.scheme not in ("http", "https") or not host:
        raise FetchError("http/https 以外のURLは扱えません")
    if parts.username or parts.password:
        raise FetchError("認証情報つきのURLは扱えません")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise FetchError("ホスト名を解決できません")
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not address.is_global:
            raise FetchError("内部ネットワークのアドレスにはアクセスしません")


# ---------------------------------------------------------------- 通信

class Fetcher:
    def __init__(self, settings):
        self.timeout = settings["timeout_seconds"]
        self.max_bytes = settings["max_response_bytes"]
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self._user_agent(settings),
            "Accept-Language": "ja,en;q=0.8",
        })
        self._robots = {}

    @staticmethod
    def _user_agent(settings) -> str:
        ua = settings.get("user_agent")
        if isinstance(ua, str) and ua.strip():
            return ua.strip()[:200]
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        contact = f" (+https://github.com/{repo})" if repo else ""
        return f"{ROBOT_TOKEN}/1.0{contact}"

    # robots.txt -------------------------------------------------
    def robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        parser = self._robots[origin]
        if parser is None:
            return False  # 取得できない・アクセス制限されている場合は安全側に倒す
        return parser.can_fetch(ROBOT_TOKEN, url)

    def _load_robots(self, origin: str):
        try:
            status, _, body = self._send("GET", origin + "/robots.txt", max_bytes=512_000, robots=False)
        except FetchError:
            return None
        parser = robotparser.RobotFileParser()
        if status in (401, 403) or status >= 500:
            return None
        if 400 <= status < 500:
            parser.allow_all = True  # robots.txt が存在しない
            return parser
        parser.parse(body.decode("utf-8", errors="replace").splitlines())
        parser.modified()
        return parser

    # 低レベルの送信 -----------------------------------------------
    def _send(self, method, url, headers=None, json_body=None, max_bytes=None, robots=True):
        max_bytes = max_bytes or self.max_bytes
        deadline = time.monotonic() + self.timeout * 2
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_public_url(current)
            if robots and not self.robots_allowed(current):
                raise FetchError("robots.txt により取得が許可されていません")
            try:
                resp = self.session.request(
                    method, current, headers=headers, json=json_body,
                    timeout=(5, self.timeout), allow_redirects=False, stream=True,
                )
            except requests.exceptions.Timeout:
                raise FetchError("タイムアウトしました")
            except requests.exceptions.RequestException as exc:
                raise FetchError(f"接続に失敗しました（{type(exc).__name__}）")
            with resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location or method != "GET":
                        raise FetchError("リダイレクトを処理できません")
                    current = urljoin(current, location)
                    continue
                return resp.status_code, resp.headers, self._read_body(resp, max_bytes, deadline)
        raise FetchError("リダイレクトが多すぎます")

    @staticmethod
    def _read_body(resp, max_bytes, deadline) -> bytes:
        chunks, total = [], 0
        try:
            for chunk in resp.iter_content(65536):
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("応答が大きすぎます")
                if time.monotonic() > deadline:
                    raise FetchError("タイムアウトしました")
                chunks.append(chunk)
        except requests.exceptions.RequestException as exc:
            raise FetchError(f"受信に失敗しました（{type(exc).__name__}）")
        return b"".join(chunks)

    def get(self, url, headers=None):
        return self._send("GET", url, headers=headers)

    def post_json(self, url, payload):
        status, _, body = self._send("POST", url, headers={"Accept": "application/json"}, json_body=payload)
        check_status(status)
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise FetchError("JSONとして解析できません")


def check_status(status: int) -> None:
    if status in (401, 403):
        raise FetchError(f"アクセスが制限されています（HTTP {status}）")
    if status == 429:
        raise FetchError("アクセス回数の制限を受けました（HTTP 429）")
    if status != 200:
        raise FetchError(f"HTTP {status} が返されました")


# ---------------------------------------------------------------- RSS / Atom の解析

def local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def parse_entry(el) -> dict:
    data = {"title": "", "link": "", "summary": "", "content": "",
            "published": None, "updated": None, "thumbs": [], "video_id": ""}

    def walk(parent):
        for c in parent:
            name = local(c.tag)
            text = "".join(c.itertext()).strip()
            if name == "group":                      # YouTube: media:group の中を読む
                walk(c)
            elif name == "title":
                data["title"] = data["title"] or text
            elif name == "link":
                href = (c.get("href") or "").strip()
                if href:                              # Atom
                    rel = c.get("rel", "alternate")
                    if rel == "alternate" and not data["link"]:
                        data["link"] = href
                    elif rel == "enclosure" and (c.get("type") or "").startswith("image/"):
                        data["thumbs"].append(href)
                elif text and not data["link"]:       # RSS
                    data["link"] = text
            elif name in ("description", "summary"):
                data["summary"] = data["summary"] or text
            elif name in ("encoded", "content"):
                if c.get("url"):                      # media:content
                    if c.get("medium") == "image" or (c.get("type") or "").startswith("image/"):
                        data["thumbs"].append(c.get("url"))
                else:
                    data["content"] = data["content"] or text
            elif name == "thumbnail" and c.get("url"):
                data["thumbs"].append(c.get("url"))
            elif name == "enclosure" and c.get("url") and (c.get("type") or "").startswith("image/"):
                data["thumbs"].append(c.get("url"))
            elif name == "videoId":
                data["video_id"] = text
            elif name in ("pubDate", "published", "date", "issued"):
                data["published"] = data["published"] or parse_feed_date(text)
            elif name in ("updated", "modified"):
                data["updated"] = data["updated"] or parse_feed_date(text)

    walk(el)
    data["date"] = data["published"] or data["updated"]
    return data


def parse_feed_xml(body: bytes) -> list:
    if re.search(rb"<!ENTITY", body[:50000], re.IGNORECASE):
        raise FetchError("安全のため、エンティティ定義を含むXMLは読み込みません")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise FetchError("RSS/Atomとして解析できませんでした")
    kind = local(root.tag)
    if kind not in ("feed", "rss", "RDF"):
        raise FetchError("RSS/Atom以外の形式です")
    wanted = "entry" if kind == "feed" else "item"
    return [parse_entry(e) for e in root.iter() if local(e.tag) == wanted]


def entry_thumbnail(entry: dict, base: str) -> str:
    vid = entry.get("video_id") or ""
    if re.fullmatch(r"[\w-]{11}", vid):
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    for raw in entry["thumbs"]:
        url = https_image(raw, base)
        if url:
            return url
    for html_part in (entry.get("content"), entry.get("summary")):
        if html_part and "<img" in html_part.lower():
            img = BeautifulSoup(html_part, "html.parser").find("img")
            if img:
                url = https_image(img.get("src") or img.get("data-src"), base)
                if url:
                    return url
    return ""


# ---------------------------------------------------------------- 記事データの組み立て

def make_item(source, url, title, summary, thumbnail, published, known_dates, now):
    clean = clean_url(url)
    if not clean or not title:
        return None
    item_id = hashlib.sha1(dedupe_key(clean).encode("utf-8")).hexdigest()[:16]
    when = published or parse_iso(known_dates.get(item_id)) or now
    if when > now + timedelta(hours=1):
        when = now
    if summary == title:
        summary = ""
    return {
        "id": item_id,
        "url": clean,
        "title": title,
        "summary": summary,
        "thumbnail": thumbnail or "",
        "source": source["name"],
        "source_id": source["id"],
        "category": source["category"],
        "published": to_iso(when),
    }


def limit_items(items, source):
    items.sort(key=lambda i: parse_iso(i["published"]) or EPOCH, reverse=True)
    return items[: source["max_items"]]


# ---------------------------------------------------------------- 情報源の種類ごとの取得

def fetch_feed(fetcher, source, prev, known_dates, now):
    if source["type"] == "youtube":
        parts = urlsplit(source["url"])
        if parts.hostname not in ("www.youtube.com", "youtube.com") or not parts.path.startswith("/feeds/videos.xml"):
            raise FetchError("YouTubeは公式RSS（https://www.youtube.com/feeds/videos.xml?channel_id=...）のURLを指定してください")
    headers = {"Accept": "application/atom+xml, application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"}
    if header_safe(prev.get("etag")):
        headers["If-None-Match"] = prev["etag"]
    if header_safe(prev.get("last_modified")):
        headers["If-Modified-Since"] = prev["last_modified"]

    status, resp_headers, body = fetcher.get(source["url"], headers)
    if status == 304:
        return None, {"etag": prev.get("etag", ""), "last_modified": prev.get("last_modified", "")}
    check_status(status)

    items = {}
    for entry in parse_feed_xml(body):
        url = urljoin(source["url"], entry["link"]) if entry["link"] else ""
        item = make_item(
            source, url,
            clean_text(entry["title"], 200, ""),
            clean_text(entry["summary"] or entry["content"], 220),
            entry_thumbnail(entry, source["url"]),
            entry["date"], known_dates, now,
        )
        if item:
            items.setdefault(item["id"], item)
    new_state = {
        "etag": (resp_headers.get("ETag") or "")[:200],
        "last_modified": (resp_headers.get("Last-Modified") or "")[:100],
    }
    return limit_items(list(items.values()), source), new_state


def select(node, selector):
    if not selector or not isinstance(selector, str):
        return None
    try:
        return node.select_one(selector)
    except Exception:
        raise FetchError(f"CSSセレクタが不正です: {selector[:60]}")


def fetch_html(fetcher, source, known_dates, now):
    rule = source.get("html")
    if not isinstance(rule, dict) or not isinstance(rule.get("item_selector"), str) or not rule["item_selector"].strip():
        raise FetchError("HTML抽出ルール（html.item_selector）が設定されていません")
    status, _, body = fetcher.get(source["url"], {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"})
    check_status(status)
    soup = BeautifulSoup(body, "html.parser")
    try:
        nodes = soup.select(rule["item_selector"])
    except Exception:
        raise FetchError("item_selector が不正です")
    if not nodes:
        raise FetchError("item_selector に一致する要素がありません（サイトの構造が変わった可能性があります）")

    items = {}
    for node in nodes[: source["max_items"] * 2]:
        if rule.get("link_selector"):
            link_node = select(node, rule["link_selector"])
        else:
            link_node = node if node.name == "a" and node.get("href") else node.find("a", href=True)
        href = link_node.get("href") if link_node else None
        if not href:
            continue
        title_node = select(node, rule.get("title_selector")) if rule.get("title_selector") else link_node
        summary_node = select(node, rule.get("summary_selector"))
        img = select(node, rule.get("thumbnail_selector"))
        thumb = ""
        if img:
            thumb = https_image(img.get("src") or img.get("data-src") or img.get("data-original"), source["url"])
        published = None
        date_node = select(node, rule.get("date_selector"))
        if date_node:
            attr = rule.get("date_attribute")
            if isinstance(attr, str) and attr:
                raw = date_node.get(attr)
            else:
                raw = date_node.get("datetime") or date_node.get_text(" ")
            fmt = rule.get("date_format") if isinstance(rule.get("date_format"), str) else None
            published = parse_date_text(str(raw) if raw else "", fmt)
        item = make_item(
            source, urljoin(source["url"], href),
            clean_text(title_node.get_text("") if title_node else "", 200, ""),
            clean_text(summary_node.get_text(" ") if summary_node else "", 220),
            thumb, published, known_dates, now,
        )
        if item:
            items.setdefault(item["id"], item)
    return limit_items(list(items.values()), source), {}


def fetch_misskey(fetcher, source, known_dates, now):
    parts = urlsplit(source["url"])
    origin = f"{parts.scheme}://{parts.netloc}"
    limit = min(source["max_items"], 100)
    user = (source.get("user") or "").strip().lstrip("@")
    if user:
        name, _, host = user.partition("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,128}", name) or (host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host)):
            raise FetchError("Misskeyの user は「ユーザー名」または「ユーザー名@ホスト名」で指定してください")
        profile = fetcher.post_json(origin + "/api/users/show", {"username": name, "host": host or None})
        user_id = profile.get("id") if isinstance(profile, dict) else None
        if not isinstance(user_id, str) or not re.fullmatch(r"[A-Za-z0-9]+", user_id):
            raise FetchError("Misskeyのユーザーを特定できませんでした")
        notes = fetcher.post_json(origin + "/api/users/notes", {"userId": user_id, "limit": limit})
    else:
        notes = fetcher.post_json(origin + "/api/notes/local-timeline", {"limit": limit})
    if not isinstance(notes, list):
        raise FetchError("Misskeyの応答が想定と異なります")

    items = {}
    for note in notes[:limit]:
        if not isinstance(note, dict) or note.get("visibility") != "public":
            continue
        if note.get("replyId") or (note.get("renoteId") and not note.get("text")):
            continue
        note_id = note.get("id")
        if not isinstance(note_id, str) or not re.fullmatch(r"[A-Za-z0-9]+", note_id):
            continue
        files = [f for f in (note.get("files") or []) if isinstance(f, dict)]
        thumb = ""
        for f in files:
            if str(f.get("type", "")).startswith("image/") and not f.get("isSensitive"):
                thumb = https_image(f.get("thumbnailUrl") or f.get("url"))
                if thumb:
                    break
        raw_text = (note.get("text") or "").strip()
        cw = clean_text(note.get("cw"), 120)
        if cw:                                    # 閲覧注意（CW）の本文は表示しない
            title, summary = cw, ""
        elif raw_text:
            title = clean_text(raw_text.splitlines()[0], 80)
            summary = clean_text(raw_text, 220)
        elif files:
            title, summary = "（画像の投稿）", ""
        else:
            continue
        item = make_item(
            source, f"{origin}/notes/{note_id}", title, summary, thumb,
            parse_iso(note.get("createdAt")), known_dates, now,
        )
        if item:
            items.setdefault(item["id"], item)
    return limit_items(list(items.values()), source), {}


def fetch_source(fetcher, source, prev, known_dates, now):
    """戻り値: (記事のリスト or None[変更なし], 保存する状態)"""
    if source["type"] in ("rss", "youtube"):
        return fetch_feed(fetcher, source, prev, known_dates, now)
    if source["type"] == "html":
        return fetch_html(fetcher, source, known_dates, now)
    return fetch_misskey(fetcher, source, known_dates, now)


# ---------------------------------------------------------------- 設定・状態

def build_settings(raw) -> dict:
    s = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        s.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    s["timeout_seconds"] = num(s["timeout_seconds"], 20, 5, 60)
    s["max_response_bytes"] = int(num(s["max_response_bytes"], 5_000_000, 100_000, 20_000_000))
    s["default_max_items"] = int(num(s["default_max_items"], 20, 1, 100))
    s["default_interval_minutes"] = int(num(s["default_interval_minutes"], 60, 30, 1440))
    s["request_delay_seconds"] = num(s["request_delay_seconds"], 1.0, 0, 30)
    s["keep_per_source"] = int(num(s["keep_per_source"], 100, 10, 500))
    s["retention_days"] = int(num(s["retention_days"], 30, 1, 365))
    s["max_total_items"] = int(num(s["max_total_items"], 600, 50, 3000))
    s["max_runtime_seconds"] = int(num(s["max_runtime_seconds"], 480, 60, 840))
    return s


def validate_sources(raw, settings):
    sources, problems, seen = [], [], set()
    if not isinstance(raw, list):
        return sources, [("sources.json", "\"sources\" が配列（[ ]）になっていません")]
    for index, item in enumerate(raw, start=1):
        label = f"{index}番目の情報源"
        if not isinstance(item, dict):
            problems.append((label, "設定が { } の形式ではありません"))
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            label = name.strip()[:60]
        sid = item.get("id")
        if not isinstance(sid, str) or not SOURCE_ID_RE.match(sid):
            problems.append((label, "id は英数字・-・_ の64文字以内で指定してください"))
            continue
        if sid in seen:
            problems.append((label, f"id「{sid}」が重複しています"))
            continue
        if not isinstance(name, str) or not name.strip():
            problems.append((label, "name が未設定です"))
            continue
        if item.get("type") not in VALID_TYPES:
            problems.append((label, "type は rss / youtube / misskey / html のどれかにしてください"))
            continue
        if item.get("category") not in VALID_CATEGORIES:
            problems.append((label, "category は news / blog / video / sns のどれかにしてください"))
            continue
        url = clean_url(item.get("url"))
        if not url:
            problems.append((label, "url は http:// または https:// で始まる形式にしてください"))
            continue
        seen.add(sid)
        sources.append({
            "id": sid,
            "name": name.strip()[:60],
            "type": item["type"],
            "category": item["category"],
            "url": url,
            "enabled": item.get("enabled", True) is not False,
            "max_items": int(num(item.get("max_items"), settings["default_max_items"], 1, 100)),
            "interval_minutes": int(num(item.get("interval_minutes"), settings["default_interval_minutes"], 30, 1440)),
            "user": item.get("user") if isinstance(item.get("user"), str) else "",
            "html": item.get("html") if isinstance(item.get("html"), dict) else None,
        })
    return sources, problems


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def valid_old_item(item) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("title"), str)
        and bool(clean_url(item.get("url")))
        and parse_iso(item.get("published")) is not None
        and isinstance(item.get("source_id"), str)
        and item.get("category") in VALID_CATEGORIES
    )


def make_error(label: str, message: str, now: datetime) -> dict:
    return {"source": label, "message": message[:200], "at": to_iso(now)}


def is_due(prev: dict, source: dict, now: datetime) -> bool:
    last = parse_iso(prev.get("last_checked"))
    if last is None:
        return True
    return now - last >= timedelta(minutes=source["interval_minutes"] - INTERVAL_TOLERANCE_MIN)


def signature(feed: dict):
    errors = [(e.get("source"), e.get("message")) for e in feed.get("errors", []) if isinstance(e, dict)]
    return feed.get("items"), errors


# ---------------------------------------------------------------- メイン処理

def main() -> int:
    started = time.monotonic()
    now = datetime.now(UTC).replace(microsecond=0)

    config = load_json(SOURCES_PATH, None)
    if not isinstance(config, dict):
        print("sources.json を読み込めません。カンマや引用符などJSONの書式を確認してください。", file=sys.stderr)
        return 1

    settings = build_settings(config.get("settings"))
    sources, problems = validate_sources(config.get("sources"), settings)
    errors = [make_error(label, msg, now) for label, msg in problems]

    old_feed = load_json(FEED_PATH, {})
    if not isinstance(old_feed, dict):
        old_feed = {}
    raw_old_items = old_feed.get("items")
    old_items = [i for i in raw_old_items if valid_old_item(i)] if isinstance(raw_old_items, list) else []
    old_state = load_json(STATE_PATH, {})
    old_states = old_state.get("sources") if isinstance(old_state, dict) else None
    old_states = old_states if isinstance(old_states, dict) else {}
    known_dates = {i["id"]: i["published"] for i in old_items}

    force = os.environ.get("FORCE_FETCH", "").strip().lower() in ("1", "true", "yes")
    fetcher = Fetcher(settings)
    active = [s for s in sources if s["enabled"]]

    new_items, new_states = [], {}
    fetched_any = False
    for source in active:
        sid, name = source["id"], source["name"]
        prev = old_states.get(sid) if isinstance(old_states.get(sid), dict) else {}

        if time.monotonic() - started > settings["max_runtime_seconds"]:
            errors.append(make_error(name, "実行時間の上限に達したため、今回は取得を見送りました", now))
            new_states[sid] = prev
            continue
        if not force and not is_due(prev, source, now):
            print(f"[skip] {name}: 更新間隔に達していません")
            new_states[sid] = prev
            continue

        if fetched_any:
            time.sleep(settings["request_delay_seconds"])
        fetched_any = True

        try:
            items, extra = fetch_source(fetcher, source, prev, known_dates, now)
        except FetchError as exc:
            print(f"[error] {name}: {exc}")
            errors.append(make_error(name, str(exc), now))
            new_states[sid] = prev
            continue
        except Exception as exc:  # 想定外の失敗でも、ほかの情報源は続ける
            traceback.print_exc()
            errors.append(make_error(name, f"想定外のエラーが発生しました（{type(exc).__name__}）", now))
            new_states[sid] = prev
            continue

        new_states[sid] = {"last_checked": to_iso(now), **extra}
        if items is None:
            print(f"[ok] {name}: 変更なし（304）")
        else:
            print(f"[ok] {name}: {len(items)}件")
            new_items.extend(items)

    # 新しい記事と、これまでの記事をまとめる（URLの正規化で重複を除く）
    configured = {s["id"]: s for s in active}
    merged = {}
    for item in new_items:
        merged.setdefault(dedupe_key(item["url"]), item)
    for item in old_items:
        src = configured.get(item["source_id"])
        if not src:
            continue  # 削除・無効化された情報源の記事は残さない
        item = dict(item)
        item["source"], item["category"] = src["name"], src["category"]
        merged.setdefault(dedupe_key(item["url"]), item)

    cutoff = now - timedelta(days=settings["retention_days"])
    per_source, final = {}, []
    ordered = sorted(merged.values(), key=lambda i: parse_iso(i["published"]) or EPOCH, reverse=True)
    for item in ordered:
        if (parse_iso(item["published"]) or EPOCH) < cutoff:
            continue
        count = per_source.get(item["source_id"], 0)
        if count >= settings["keep_per_source"]:
            continue
        per_source[item["source_id"]] = count + 1
        final.append(item)
    final = final[: settings["max_total_items"]]

    new_feed = {
        "schema_version": 1,
        "generated_at": to_iso(now),
        "errors": errors[:MAX_ERRORS_SHOWN],
        "items": final,
    }

    # 変化がなく、前回の書き込みから間もない場合は、コミットを増やさないよう書き込まない
    previous_time = parse_iso(old_feed.get("generated_at"))
    unchanged = bool(old_feed) and signature(old_feed) == signature(new_feed)
    if unchanged and previous_time and now - previous_time < REWRITE_AFTER:
        print("変更がないため、書き込みを省略しました")
        return 0

    write_json(FEED_PATH, new_feed)
    write_json(STATE_PATH, {"updated_at": to_iso(now), "sources": new_states})
    print(f"data/feed.json を更新しました（記事{len(final)}件、エラー{len(errors)}件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
