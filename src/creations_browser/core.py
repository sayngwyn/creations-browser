"""Free Bethesda Creations: look them up, download the PC file outside the game, and turn it into a normal zip.

Plain Python (standard library only): no Qt, no ``mobase``. Adapted from the author's own private modding tooling.

Bethesda's public catalogue (the one creations.bethesda.net itself uses) is read with a public client key that the
site's own config publishes (``https://cdn.bethesda.net/data/core``). The key is fetched once per process, sent only
to api.bethesda.net, and never logged or returned; the signed download links are never returned either.

Only free Creations are fetched: free is confirmed (``author_price`` present and empty, ``catalog_info`` empty, no other
price-like field set); anything else is ``NOT_FREE``, and a missing field is ``FREE_UNKNOWN``. Redirects are never
followed (so the key and the host checks cannot be sidestepped). The PC download is a ``.ckm`` (a ``BTAR`` container,
version 1). It is checked against the size and MD5 (the ETag) the API lists, capped in size, read with a strict reader
(only ``Data\\`` content of an allow-listed game-data type; unsafe paths, truncation and trailing bytes are refused), and
rewritten as a stored, deterministic ``.zip`` in the downloads folder with an MO2-style ``.meta`` beside it. From
there Mod Organizer 2 installs it like any other download (a zip with a ``Data`` folder).

Verified live in MO2 2.5.2 for Fallout 4 and Skyrim Special Edition, 2026-10-21.
"""

from __future__ import annotations

import hashlib
import html
import http.client
import json
import logging
import os
import re
import struct
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

VERSION = "1.1.0"
CORE_URL = "https://cdn.bethesda.net/data/core"
API = "https://api.bethesda.net/ugcmods/v1"
DOWNLOAD_HOST = "ugcmods.bethesda.net"
USER_AGENT = f"CreationsBrowser/{VERSION} (unofficial Mod Organizer 2 plugin; +https://github.com/sayngwyn/creations-browser)"
PRODUCTS = {"fallout4": "fallout4", "skyrimspecialedition": "skyrim"}  # game -> x-bnet-product and creations.bethesda.net path
GAME_LABELS = {"fallout4": "Fallout 4", "skyrimspecialedition": "Skyrim Special Edition"}  # game -> its name in words
# game -> the short name Mod Organizer 2 writes in ``gameName=`` of a download's .meta (seen in real MO2 2.5 .meta files)
MO2_GAME_NAMES = {"fallout4": "Fallout4", "skyrimspecialedition": "SkyrimSE"}
REPOSITORY = "Bethesda.net"
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Game data only: the plugin, archive and config types a Creation ships, and the loose asset types that can sit beside them.
# Everything else (executables, scripts, anything unknown) is refused.
ALLOWED_SUFFIXES = {".esp", ".esm", ".esl", ".ba2", ".bsa", ".ini", ".txt",
                    ".nif", ".dds", ".swf", ".pex", ".psc", ".wav", ".xwm", ".fuz", ".lip", ".bgsm", ".bgem",
                    ".strings", ".dlstrings", ".ilstrings",
                    ".hkx", ".tri", ".egm", ".egt", ".bik", ".seq", ".lod", ".btr", ".bto", ".btt", ".ssf", ".cdf", ".gid"}
RESERVED_NAME = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|(com|lpt)[0-9¹²³])", re.I)  # Windows device names
CHUNK = 1024 * 1024
MAX_DOWNLOAD = 4 * 1024 ** 3  # a hard cap on one .ckm
MAX_JSON = 16 * 1024 ** 2
DOWNLOAD_DEADLINE = 600.0  # seconds for a whole download, besides the socket timeout
DOWNLOAD_SOCKET_TIMEOUT = 30  # seconds one connect or read may block (bounds how long a cancel or MO2's exit can wait)
DESCRIPTION_LIMIT = 4000  # characters of a Creation's description kept for display
NET_ERRORS = (OSError, http.client.HTTPException)  # URLError, timeouts and ssl.SSLError are OSErrors

Progress = Callable[[int, "int | None"], None]
Cancel = Callable[[], bool]

_key_cache: list[str] = []  # memoised per process (tests clear it)
_key_lock = threading.Lock()  # the window's jobs run on several threads: one of them fetches the key, the rest wait for it


class CreationsError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would sidestep the host checks, could downgrade to http and would carry the request's headers (the
    client key) to another host: every redirect is refused."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CreationsError("CREATIONS_ERROR", f"refused a redirect (HTTP {code}) to {_strip_query(newurl)}")


def _open(request: urllib.request.Request, timeout: float):
    """The one place the network is touched (tests replace it)."""
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def clean_text(value) -> str:
    """Text from the catalogue as one safe line: HTML entities unescaped, then every control, format and line/paragraph
    separator character (\\n \\r \\v \\f U+0085 U+2028 U+2029 ...) removed, so it cannot start a new line in a .meta or
    meta.ini or smuggle in bidi overrides."""
    text = html.unescape(str(value or ""))
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else "" if unicodedata.category(c) == "Cf" else c for c in text)
    return re.sub(r" {2,}", " ", text).strip()


_LOG_LIMIT = 4000


def scrub(text) -> str:
    """Text that is safe to log or show: the client key, an ``x-bnet-key`` header value, a download-host link (its path and
    signature) and everything after a ``?`` are removed, whatever whitespace they hold."""
    out = str(text)
    for key in _key_cache:
        out = out.replace(key, "…")
    out = re.sub(r"(?i)x-bnet-key\W+\S+", "x-bnet-key …", out)
    out = re.sub(r"(?i)https?://ugcmods\.bethesda\.net[^\r\n]*", "https://ugcmods.bethesda.net/…", out)
    out = re.sub(r"\?.*", "?…", out, flags=re.S)
    return out if len(out) <= _LOG_LIMIT else out[:_LOG_LIMIT] + "…"


def log_line(level: int, text) -> None:
    """Log one finished, scrubbed message. Mod Organizer's Python log handler prints ``record.msg`` only (it drops ``args``
    and tracebacks), so every message is built before it reaches the logger."""
    log.log(level, scrub(text))


def log_unexpected(what: str) -> None:
    """Log an unexpected error with its (scrubbed) traceback; call it from inside an ``except`` block."""
    log.error(scrub(f"{what}\n{traceback.format_exc()}"))


def net_reason(exc: BaseException) -> str:
    """A short, safe description of a network failure: the exception type (and a URLError's inner type) and a plain reason.
    Never the exception's text, which can carry a signed link."""
    name = type(exc).__name__
    inner = exc.reason if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException) else None
    if inner is not None:
        name += f" ({type(inner).__name__})"
    probe = inner if inner is not None else exc
    if isinstance(probe, TimeoutError):
        why = "timed out"
    elif isinstance(probe, ConnectionError):
        why = "connection lost"
    elif isinstance(probe, http.client.IncompleteRead):
        why = "the connection ended early"
    elif isinstance(probe, http.client.InvalidURL):
        why = "malformed link"
    else:
        return name
    return f"{name}, {why}"


_TAG_BREAK = re.compile(r"<\s*(?:br\s*/?|/\s*p|/\s*div|/\s*li|/\s*h[1-6])\s*>", re.I)
_TAG = re.compile(r"</?[A-Za-z][^<>]*>")


def clean_paragraphs(value, limit: int = DESCRIPTION_LIMIT) -> str:
    """Longer catalogue text (a description) as safe plain text that keeps its line breaks as ``\\n``. HTML tags are
    removed (``<br>`` and closing block tags become line breaks), entities are unescaped, and every control and format
    character is removed except ``\\n`` (tabs become spaces; \\r\\n, \\r, U+0085, U+2028 and U+2029 become ``\\n``).
    Runs of three or more line breaks collapse to a blank line, and the result is capped at ``limit`` characters."""
    text = _TAG.sub("", _TAG_BREAK.sub("\n", str(value or "")))
    text = html.unescape(text).replace("\r\n", "\n")
    out = []
    for c in text:
        cat = unicodedata.category(c)
        if c == "\n" or c in "\r\x85" or cat in ("Zl", "Zp"):
            out.append("\n")
        elif cat == "Cf":
            continue
        elif cat == "Cc" or c == "\t":
            out.append(" ")
        else:
            out.append(c)
    text = "".join(out)
    text = "\n".join(re.sub(r" {2,}", " ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:max(limit - 1, 0)].rstrip() + "…"
    return text


# ---------------------------------------------------------------- the API

def norm_game(game) -> str:
    low = str(game or "").lower().replace(" ", "")
    if low in ("skyrimse", "skyrim", "skyrimspecialedition"):
        return "skyrimspecialedition"
    if low == "fallout4":
        return "fallout4"
    raise CreationsError("INVALID_ARGUMENT", f"game must be 'fallout4' or 'skyrimspecialedition', got {game!r}")


def game_alias(name: str) -> str:
    """A ``gameName`` from a .meta (MO2's ``SkyrimSE`` / ``Fallout4`` or the long lower-case form) as the long form."""
    low = name.lower().replace(" ", "")
    return {"skyrimse": "skyrimspecialedition", "skyrimspecialedition": "skyrimspecialedition",
            "fallout4": "fallout4"}.get(low, low)


def _http_json(url: str, headers: dict[str, str], what: str):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if urllib.parse.urlsplit(url).scheme != "https" or (host not in ("api.bethesda.net", "cdn.bethesda.net")):
        raise CreationsError("UNEXPECTED_HOST", f"{what}: refusing to call {host!r}")
    if "x-bnet-key" in headers and host != "api.bethesda.net":
        raise CreationsError("UNEXPECTED_HOST", f"{what}: the client key is only sent to api.bethesda.net")
    request = urllib.request.Request(url, headers=headers)
    try:
        with _open(request, 60) as response:
            raw = response.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise CreationsError("CREATIONS_ERROR", f"{what}: the answer is larger than {MAX_JSON} bytes")
        return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = clean_text(exc.read(200).decode("utf-8", errors="replace"))
        except NET_ERRORS:
            body = ""
        status = "NOT_FOUND" if exc.code == 404 else "RATE_LIMITED" if exc.code == 429 else "CREATIONS_ERROR"
        raise CreationsError(status, f"{what}: HTTP {exc.code} {body}".strip()) from None
    except NET_ERRORS as exc:
        raise CreationsError("UNREACHABLE", f"{what}: unreachable ({net_reason(exc)})") from None
    except ValueError:
        raise CreationsError("CREATIONS_ERROR", f"{what}: the answer was not JSON") from None


def client_key() -> str:
    with _key_lock:
        if not _key_cache:
            core = _http_json(CORE_URL, {"User-Agent": USER_AGENT, "Accept": "application/json"}, "Bethesda config")
            key = core.get("ugc", {}).get("bnetKey") if isinstance(core, dict) else None
            if not isinstance(key, str) or not key:
                raise CreationsError("CREATIONS_ERROR", "Bethesda's public config no longer lists the Creations client key")
            _key_cache.append(key)
        return _key_cache[0]


def _api_raw(game: str, path: str, params: dict | None, what: str):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
               "Content-Type": "application/json", "x-bnet-product": PRODUCTS[game], "x-bnet-key": client_key()}
    url = f"{API}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    return _http_json(url, headers, what)


def _api(game: str, path: str, params: dict | None, what: str) -> dict:
    data = _api_raw(game, path, params, what)
    try:
        return data["platform"]["response"]
    except (KeyError, TypeError):
        raise CreationsError("CREATIONS_ERROR", f"{what}: the answer has an unexpected shape") from None


# What the catalogue's ``content`` call may be asked (every value is checked against these before anything is sent).
SORT_KEYS = frozenset({"subscriptions", "ptime", "utime", "ctime", "title", "rating"})
ORDERS = frozenset({"asc", "desc"})
TIME_PERIODS = frozenset({"all_time", "monthly", "weekly", "daily"})
PLATFORM_FILTERS = frozenset({"WINDOWS"})  # the only platform filter this plugin sends: PC
MAX_PAGE_SIZE = 100
MAX_PAGE = 1000
MAX_QUERY = 200  # characters of search text
MAX_NAME = 80  # characters of a category or author name
TEXT_CAP = 500  # the most results one text search keeps (at most TEXT_CAP / MAX_PAGE_SIZE requests)
BROWSE_CAP = 1000  # the most Creations one browse collects (at most BROWSE_CAP / MAX_PAGE_SIZE requests)

# The window's sort choices. A browse (no search words) is sorted by Bethesda (sort, order, time_period), except Most
# downloaded and Most liked, which the server has no sort for: the window collects Bethesda's most popular Creations and sorts
# them here (see ``lookup_browse_collect`` and ``sort_rows``). Bethesda ignores every sort when there are search words, so a
# text search is sorted here too, and the server-sorted choices are not offered with words.
SERVER_SORTS = {"popular": ("subscriptions", "desc", "all_time"), "monthly": ("subscriptions", "desc", "monthly"),
                "weekly": ("subscriptions", "desc", "weekly"), "newest": ("ptime", "desc", None),
                "updated": ("utime", "desc", None), "title": ("title", "asc", None), "rating": ("rating", "desc", None)}
POPULARITY_ORDER = ("subscriptions", "desc", "all_time")  # the order a browse collects in for the locally sorted choices
LOCAL_SORTS = ("downloads", "likes")  # no server sort exists: sorted here, in a browse from the collected Creations
TEXT_ONLY_SORTS = ("match",)  # Bethesda's own text ranking: only with search words
WORDLESS_SORTS = ("weekly", "monthly", "popular")  # Bethesda's popularity sorts: ignored when there are search words
PERIOD_SORTS = ("weekly", "monthly")  # only Bethesda can order by these: no per-Creation figure for a week or a month
SORT_MODES = ("match", "downloads", "weekly", "monthly", "popular", "likes", "rating", "newest", "updated", "title")


def _refuse(message: str):
    raise CreationsError("INVALID_ARGUMENT", message)


def _whole(value, low: int, high: int, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        _refuse(f"{what} must be a whole number from {low} to {high}")
    return value


def _safe_name(value, what: str) -> str:
    """A category or author name that can go into a request: one plain line of 1..MAX_NAME characters, no control,
    format or line-separator characters and no padding spaces."""
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > MAX_NAME
            or any(unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in value)):
        _refuse(f"{what} must be plain text of 1 to {MAX_NAME} characters")
    return value


def valid_name(value) -> bool:
    """True when ``value`` can be sent as a category or author name (see ``_safe_name``)."""
    try:
        _safe_name(value, "name")
    except CreationsError:
        return False
    return True


MAX_CONTENT_IDS = 100  # the most Creations one ``content_ids`` request names
_ID_DIGITS = re.compile(r"[0-9]{1,9}")


def _content_ids(value) -> str:
    """``content_ids`` as the comma list the catalogue takes: a non-empty list of at most MAX_CONTENT_IDS references, each a
    Creation UUID or a number of one to nine digits (a whole number or its ASCII digits), UUIDs in lower case. Anything else is
    an INVALID_ARGUMENT and nothing is sent."""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        _refuse("content_ids must be a list of Creation ids or UUIDs")
    out: list[str] = []
    for ref in value:
        text = str(ref) if isinstance(ref, int) and not isinstance(ref, bool) else ref
        if not isinstance(text, str) or not (_ID_DIGITS.fullmatch(text) or UUID_RE.fullmatch(text)):
            _refuse("every Creation reference must be a UUID or a number of one to nine digits")
        out.append(text.lower())
    if not 1 <= len(out) <= MAX_CONTENT_IDS:
        _refuse(f"content_ids must name from 1 to {MAX_CONTENT_IDS} Creations")
    return ",".join(out)


def _content_params(game: str, text=None, *, page=1, size=10, sort=None, order=None, time_period=None,
                    platforms=None, marketplace=None, category=None, author=None, content_ids=None) -> dict:
    """The validated query for ``content``. ``platforms`` may only be "WINDOWS", ``marketplace`` only False (leave store
    items out), ``content_ids`` a list of Creation ids and UUIDs (see ``_content_ids``); a value outside the allow-lists is
    an INVALID_ARGUMENT and nothing is sent."""
    params: dict = {"product": PRODUCTS[game], "page": _whole(page, 1, MAX_PAGE, "page"), "size": _whole(size, 1, MAX_PAGE_SIZE, "size")}
    if text is not None:
        if not isinstance(text, str) or len(text.strip()) > MAX_QUERY:
            _refuse(f"the search text must be at most {MAX_QUERY} characters")
        if text.strip():
            params["text"] = text.strip()
    for key, value, allowed in (("sort", sort, SORT_KEYS), ("order", order, ORDERS), ("time_period", time_period, TIME_PERIODS)):
        if value is not None:
            if value not in allowed:
                _refuse(f"{key} must be one of {sorted(allowed)}")
            params[key] = value
    if platforms is not None:
        if platforms not in PLATFORM_FILTERS:
            _refuse(f"platforms must be one of {sorted(PLATFORM_FILTERS)}")
        params["hardware_platforms"] = platforms
    if marketplace is not None:
        if marketplace is not False:
            _refuse("marketplace may only be False (leave store items out)")
        params["marketplace"] = "false"
    if category is not None:
        params["categories"] = _safe_name(category, "the category")
    if author is not None:
        params["author_displayname"] = _safe_name(author, "the author")
    if content_ids is not None:
        params["content_ids"] = _content_ids(content_ids)
    return params


def search(game: str, text: str | None = None, content_ids: list[str] | None = None, size: int = 10) -> list[dict]:
    game = norm_game(game)
    params: dict = {"product": PRODUCTS[game], "size": max(1, min(int(size), 50))}
    if text:
        params["text"] = text
    if content_ids:
        params["content_ids"] = _content_ids(content_ids)
    data = _api(game, "content", params, "Creations search").get("data")
    return [_clean(d) for d in (data or []) if isinstance(d, dict)]


def search_page(game: str, text: str | None = None, **options) -> tuple[list[dict], int]:
    """One page of the catalogue: ``(items, total)`` where ``total`` is how many Creations match in all. ``options`` are
    page, size, sort, order, time_period, platforms, marketplace, category, author and content_ids (see ``_content_params``)."""
    game = norm_game(game)
    response = _api(game, "content", _content_params(game, text, **options), "Creations search")
    data = response.get("data")
    items = [_clean(d) for d in (data if isinstance(data, list) else []) if isinstance(d, dict)]
    total = _num(response.get("total"))
    return items, max(total if total is not None else len(items), len(items))


def categories(game: str) -> list[str]:
    """The category names the catalogue lists for ``game``, cleaned, without repeats, sorted A to Z."""
    game = norm_game(game)
    raw = _api_raw(game, "categories", {"product": PRODUCTS[game]}, "Creations categories")
    found = raw.get("categories") if isinstance(raw, dict) else None
    if found is None and isinstance(raw, dict):  # the answer may also come wrapped like the other calls
        inner = (raw.get("platform") or {}).get("response") if isinstance(raw.get("platform"), dict) else None
        found = inner.get("categories") if isinstance(inner, dict) else None
    if not isinstance(found, list):
        raise CreationsError("CREATIONS_ERROR", "Creations categories: the answer has an unexpected shape")
    names: dict[str, str] = {}
    for entry in found[:500]:
        name = clean_text(entry.get("name") if isinstance(entry, dict) else entry)
        if name and len(name) <= MAX_NAME:
            names.setdefault(name.casefold(), name)
    return sorted(names.values(), key=str.casefold)


def _clean(item: dict) -> dict:
    """The catalogue stores titles HTML-escaped (``&amp;``) and its text is untrusted: title and author become one safe
    line (see clean_text), and a content_uuid that is not exactly a UUID is dropped."""
    out = dict(item)
    for key in ("title", "author_displayname"):
        if isinstance(out.get(key), str):
            out[key] = clean_text(out[key])
    uid = out.get("content_uuid")
    out["content_uuid"] = uid if isinstance(uid, str) and UUID_RE.fullmatch(uid) else None
    return out


def detail(game: str, content_id: int) -> dict:
    game = norm_game(game)
    return _clean(_api(game, f"content/{int(content_id)}", None, f"Creation {content_id}"))


def resolve(game: str, ref) -> tuple[int, str | None]:
    """(numeric content id, uuid or None) from a numeric id, a UUID or a creations.bethesda.net link."""
    game = norm_game(game)
    if isinstance(ref, bool):
        raise CreationsError("INVALID_ARGUMENT", "content_id must be a number, a UUID or a creations.bethesda.net link")
    cid = numeric_id(ref)
    if cid is not None:
        if cid <= 0:
            raise CreationsError("INVALID_ARGUMENT", "content_id must be positive")
        return cid, None
    text = str(ref or "").strip()
    uid = None
    if "://" in text or text.lower().startswith("creations.bethesda.net"):
        parsed = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
        if (parsed.hostname or "").lower() != "creations.bethesda.net":
            raise CreationsError("INVALID_ARGUMENT", "Only creations.bethesda.net links are understood.")
        m = UUID_RE.search(parsed.path)
        if not m:
            raise CreationsError("INVALID_ARGUMENT", "That link doesn't contain a Creation id.")
        segments = [s.lower() for s in parsed.path.split("/") if s]
        for seg in segments:
            if seg in ("fallout4", "skyrim", "skyrimspecialedition") and norm_game(seg) != game:
                raise CreationsError("INVALID_ARGUMENT", f"That link is for {GAME_LABELS[norm_game(seg)]}.")
        uid = m.group(0)
    elif UUID_RE.fullmatch(text):
        uid = text
    else:
        raise CreationsError("INVALID_ARGUMENT", "content_id must be a number, a UUID or a creations.bethesda.net link")
    for item in search(game, content_ids=[uid.lower()], size=5):
        if str(item.get("content_uuid") or "").lower() == uid.lower() and _int(item.get("content_id")) > 0:
            return _int(item["content_id"]), item["content_uuid"]
    raise CreationsError("NOT_FOUND", f"no Creation with the id {uid} for {game}")


def link_game(ref) -> str | None:
    """The game a pasted creations.bethesda.net link names in its path (``fallout4``, ``skyrim`` or ``skyrimspecialedition``),
    as a game key; None when ``ref`` is not such a link or names no game. Nothing is requested."""
    text = str(ref or "").strip()
    if "://" not in text and not text.lower().startswith("creations.bethesda.net"):
        return None
    try:
        parsed = urllib.parse.urlsplit(text if "://" in text else "https://" + text)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if host != "creations.bethesda.net":
        return None
    for seg in (s.lower() for s in parsed.path.split("/") if s):
        if seg in ("fallout4", "skyrim", "skyrimspecialedition"):
            return norm_game(seg)
    return None


def numeric_id(ref) -> int | None:
    """The number in ``ref`` when it is a whole number or plain ASCII digits (not '²' and the like), else None."""
    if isinstance(ref, int) and not isinstance(ref, bool):
        return ref
    if isinstance(ref, str) and re.fullmatch(r"[0-9]+", ref.strip()):
        return int(ref.strip())
    return None


# ---------------------------------------------------------------- facts about one Creation

PAID_KEY = re.compile(r"price|catalog|entitle|purchase|sku|marketplace", re.I)


def _zero(value) -> bool:
    if value is None or value is False or value == [] or value == {} or value == "":
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    return isinstance(value, str) and value.strip() in ("0", "0.0", "0.00")


def _paid_fields(node, depth: int = 0) -> list[str]:
    """Keys that mention price, catalogue, entitlement, purchase, sku or marketplace and hold a value (the download tree is
    skipped). ``marketplace: true`` is Bethesda's flag for a store listing."""
    found: list[str] = []
    if isinstance(node, dict) and depth < 4:
        for key, value in node.items():
            if key == "download":
                continue
            if PAID_KEY.search(str(key)) and not _zero(value):
                found.append(str(key))
            found += _paid_fields(value, depth + 1)
    elif isinstance(node, list) and depth < 4:
        for value in node[:50]:
            found += _paid_fields(value, depth + 1)
    return found


def free_status(item: dict) -> str:
    """'free' only when it is confirmed: author_price and catalog_info are present and empty (None, 0, empty) and no
    price-like field (including ``marketplace``) holds a value. 'paid' when any such value is set, 'unknown' when a field is missing."""
    if _paid_fields(item):
        return "paid"
    if "author_price" not in item or "catalog_info" not in item:
        return "unknown"
    return "free"


def is_free(item: dict) -> bool:
    return free_status(item) == "free"


WITHDRAWN_KEYS = ("deleted", "blocklisted", "restricted")
WITHDRAWN_REASON = "Withdrawn or restricted by Bethesda: not available here"


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return not _zero(value)


def is_withdrawn(item: dict) -> bool:
    """True when the catalogue marks the Creation deleted, block-listed or restricted."""
    return any(_truthy(item.get(k)) for k in WITHDRAWN_KEYS)


def page_url(game: str, content_uuid: str | None) -> str | None:
    return f"https://creations.bethesda.net/en/{PRODUCTS[norm_game(game)]}/details/{content_uuid}" if content_uuid else None


def _strip_query(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _num(value) -> int | None:
    """A non-negative whole number from a count the catalogue gave as a number or digits; None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if 0 <= value < 1e15 else None  # also false for nan and inf
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,15}", value.strip()):
        return int(value.strip())
    return None


def _stat_block(item: dict, platform: str) -> dict:
    """``stats.platforms[platform]`` (the platform key matched ignoring case), or {} when it is missing or odd."""
    stats = item.get("stats")
    platforms = stats.get("platforms") if isinstance(stats, dict) else None
    if isinstance(platforms, dict):
        for key, block in platforms.items():
            if isinstance(key, str) and key.upper() == platform and isinstance(block, dict):
                return block
    return {}


def _totals(item: dict) -> dict:
    stats = item.get("stats")
    totals = stats.get("totals") if isinstance(stats, dict) else None
    return totals if isinstance(totals, dict) else {}


def _total_stat(item: dict, key: str) -> int | None:
    """A figure for all platforms together: ``stats.totals``, else the ``ALL`` block."""
    value = _num(_totals(item).get(key))
    return value if value is not None else _num(_stat_block(item, "ALL").get(key))


def pc_downloads(item: dict) -> int | None:
    """Downloads of the PC (WINDOWS) version, or None when the catalogue lists no PC figure."""
    return _num(_stat_block(item, "WINDOWS").get("downloads"))


def all_downloads(item: dict) -> int | None:
    """Total downloads on every platform: what rows show and "Most downloaded" sorts by."""
    return _total_stat(item, "downloads")


def like_count(item: dict) -> int | None:
    return _total_stat(item, "likes")


def subscribe_count(item: dict) -> int | None:
    return _total_stat(item, "subscribes")


RATING_NAMES = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def rating_level(item: dict) -> int | None:
    """Bethesda's rating as 0 (LOW), 1 (MEDIUM) or 2 (HIGH), or None when the catalogue gives none."""
    raw = _totals(item).get("rating")
    if raw is None:
        raw = _stat_block(item, "ALL").get("rating")
    if isinstance(raw, str) and raw.strip().upper() in RATING_NAMES:
        return RATING_NAMES[raw.strip().upper()]
    value = _num(raw)
    return value if value in (0, 1, 2) else None


def _epoch(value) -> int | None:
    """Seconds since 1970 from a catalogue time (milliseconds are folded down); None when missing, zero or odd."""
    seconds = _num(value)
    if seconds is None or seconds == 0:
        return None
    return seconds // 1000 if seconds > 10 ** 11 else seconds


def published_time(item: dict) -> int | None:
    value = _epoch(item.get("ptime"))
    return value if value is not None else _epoch(item.get("first_ptime"))


def updated_time(item: dict) -> int | None:
    return _epoch(item.get("utime"))


def category_names(item: dict) -> list[str]:
    """The Creation's category names as safe one-line text (at most 20)."""
    found = item.get("categories")
    out = [clean_text(c) for c in (found if isinstance(found, list) else []) if isinstance(c, str)]
    return [c for c in out if c and len(c) <= MAX_NAME][:20]


def pc_files(item: dict) -> list[dict]:
    """Every PC (.ckm, WINDOWS) file the detail lists, newest first. Each has version, ctime, size, etag, url (a signed
    link: internal, never returned) and name. Raises NO_PC_FILE / UNEXPECTED_HOST."""
    found: list[dict] = []
    saw_windows = False
    for group in item.get("download") or []:
        if not isinstance(group, dict) or group.get("hardware_platform") != "WINDOWS":
            continue
        saw_windows = True
        for pub in group.get("published") or []:
            for key, meta in ((pub.get("client") or {}).items() if isinstance(pub, dict) else ()):
                if not isinstance(meta, dict) or not meta.get("download_url"):
                    continue
                if re.search(r"[\s\x00-\x1f\x7f]", str(meta["download_url"])):
                    raise CreationsError("UNEXPECTED_HOST", "the PC file link holds spaces or control characters; not downloaded")
                parsed = urllib.parse.urlsplit(str(meta["download_url"]))
                if not parsed.path.lower().endswith(".ckm"):
                    continue
                if parsed.scheme != "https" or (parsed.hostname or "").lower() != DOWNLOAD_HOST:
                    raise CreationsError("UNEXPECTED_HOST", f"the PC file is offered from {parsed.hostname!r}, not {DOWNLOAD_HOST}; not downloaded")
                found.append({"version": clean_text(pub.get("version_name")), "ctime": _int(pub.get("ctime") or meta.get("ctime")),
                              "size": _int(meta.get("size"), -1) if _int(meta.get("size"), -1) >= 0 else None,
                              "etag": str(meta.get("etag") or "").strip('"'), "url": str(meta["download_url"]),
                              "name": Path(parsed.path).name})
    if not found:
        raise CreationsError("NO_PC_FILE", "this Creation has no PC (.ckm) download" if saw_windows else "this Creation has no Windows download")
    return sorted(found, key=lambda f: f["ctime"], reverse=True)


def pc_file(item: dict, version: str | None = None) -> dict:
    """The latest PC file, or the one of ``version``."""
    files = pc_files(item)
    if version is None or not str(version).strip():
        return files[0]
    for f in files:
        if f["version"] == clean_text(version):
            return f
    raise CreationsError("NOT_FOUND", f"no PC file of version {version!r}; listed: {sorted({f['version'] for f in files})}")


@dataclass
class Plan:
    game: str
    content_id: int
    uuid: str | None
    title: str
    author: str | None
    version: str
    size: int | None
    etag: str
    url: str = field(repr=False)  # signed: internal

    def public(self) -> dict:
        return {"game": self.game, "content_id": self.content_id, "uuid": self.uuid, "title": self.title, "author": self.author,
                "version": self.version, "bytes": self.size, "free": True, "page": page_url(self.game, self.uuid)}


def plan(game: str, ref, version: str | None = None) -> Plan:
    """What would be downloaded for ``ref``: resolves it, checks it is free and has a PC file. Network reads only."""
    game = norm_game(game)
    cid, uid = resolve(game, ref)
    item = detail(game, cid)
    status = free_status(item)
    title = item.get("title") or f"Creation {cid}"
    if is_withdrawn(item):
        raise CreationsError("UNAVAILABLE", f"{title} has been withdrawn or restricted by Bethesda; it is not downloaded")
    if status == "paid":
        raise CreationsError("NOT_FREE", f"{title} is not free (price-like fields: {sorted(set(_paid_fields(item)))}); "
                                         "paid Creations are not downloaded")
    if status != "free":
        raise CreationsError("FREE_UNKNOWN", f"{title}: the catalogue does not say whether it is free (author_price or catalog_info is "
                                             "missing); it is not downloaded")
    uuid_ = item.get("content_uuid") or uid
    if not (isinstance(uuid_, str) and UUID_RE.fullmatch(uuid_)):
        raise CreationsError("CREATIONS_ERROR", f"{title}: the catalogue gave no valid Creation UUID")
    f = pc_file(item, version)
    return Plan(game, cid, uuid_, str(title), item.get("author_displayname"), f["version"], f["size"], f["etag"], f["url"])


# ---------------------------------------------------------------- downloading and converting

def download_ckm(url: str, dest: Path, size: int | None, etag: str,
                 progress: Progress | None = None, cancel: Cancel | None = None) -> str:
    """Stream the .ckm to ``dest`` (https from the Bethesda download host only, no redirects); the byte count must equal
    ``size`` and the MD5 the ETag. Reading stops as soon as more than the listed size (or, with no size, MAX_DOWNLOAD)
    has arrived, or DOWNLOAD_DEADLINE seconds have passed. Returns the md5.

    ``progress(done_bytes, total_or_None)`` is called after every chunk. ``cancel()`` is asked before the request and
    before every chunk; when it returns True the partial ``dest`` is removed and ``CreationsError("CANCELLED")`` is raised."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != DOWNLOAD_HOST:
        raise CreationsError("UNEXPECTED_HOST", f"refusing to download from {parsed.hostname!r}")
    if size is not None and size > MAX_DOWNLOAD:
        raise CreationsError("TOO_BIG", f"the API lists {size} bytes, more than the {MAX_DOWNLOAD} byte limit")
    if cancel is not None and cancel():
        raise CreationsError("CANCELLED", "the download was cancelled")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h, count, limit = hashlib.md5(), 0, (size if size is not None else MAX_DOWNLOAD)
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    cancelled = False
    try:
        response = _open(request, DOWNLOAD_SOCKET_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise CreationsError("RATE_LIMITED" if exc.code == 429 else "CREATIONS_ERROR", f"download failed: HTTP {exc.code}") from None
    except NET_ERRORS as exc:  # only the type and a plain reason: the text of these errors can hold the signed link
        raise CreationsError("UNREACHABLE", f"download failed: {net_reason(exc)}") from None
    with response, dest.open("wb") as out:
        while True:
            if cancel is not None and cancel():
                cancelled = True
                break
            try:
                chunk = response.read(min(CHUNK, limit - count + 1))
            except NET_ERRORS as exc:
                raise CreationsError("UNREACHABLE", f"download failed: {net_reason(exc)}") from None
            if not chunk:
                break
            count += len(chunk)
            if count > limit:
                if size is not None:
                    raise CreationsError("DOWNLOAD_MISMATCH", f"more than the {size} bytes the API lists arrived; stopped")
                raise CreationsError("TOO_BIG", f"the download exceeds the {MAX_DOWNLOAD} byte limit; stopped")
            if time.monotonic() > deadline:
                raise CreationsError("CREATIONS_ERROR", f"the download took longer than {DOWNLOAD_DEADLINE:.0f} s; stopped")
            out.write(chunk)
            h.update(chunk)
            if progress is not None:
                progress(count, size)
    if cancelled:
        dest.unlink(missing_ok=True)
        raise CreationsError("CANCELLED", "the download was cancelled")
    md5 = h.hexdigest()
    hex_etag = etag.lower() if re.fullmatch(r"[0-9a-fA-F]{32}", etag or "") else None  # a multipart etag is not an md5
    if size is None and hex_etag is None:
        raise CreationsError("DOWNLOAD_MISMATCH", "the API lists neither a size nor an md5 for this file, so it cannot be verified")
    if size is not None and count != size:
        raise CreationsError("DOWNLOAD_MISMATCH", f"downloaded {count} bytes, the API lists {size}")
    if hex_etag is not None and md5 != hex_etag:
        raise CreationsError("DOWNLOAD_MISMATCH", "the downloaded file's md5 does not match the API's ETag")
    return md5


def member_label(raw, limit: int = 60) -> str:
    """A package member's path as one short, printable line fit for the status line: cleaned (see ``clean_text``), written
    with forward slashes, any unprintable character shown as ``?``, and the middle elided past ``limit`` characters."""
    text = clean_text(str(raw or "").replace("\\", "/"))
    text = "".join(c if c.isprintable() else "?" for c in text)
    if len(text) > limit:
        tail = (limit - 1) * 2 // 3
        text = text[:limit - 1 - tail] + "…" + text[-tail:]
    return text or "(unnamed)"


def safe_member(raw: str) -> str:
    """A BTAR member path as a zip name (forward slashes, the ``data`` folder spelt ``Data``). A Creation is game data: the
    path must lie inside ``data`` (so never in Root or the game folder). Refuses absolute, drive, ``..``, empty,
    Windows-invalid and Windows device-name parts."""
    if not raw or "\x00" in raw:
        raise CreationsError("UNSAFE_CKM", f"{member_label(raw)} has an unsafe file path")
    parts = raw.replace("/", "\\").split("\\")  # an empty part is a leading, trailing or doubled separator (absolute and UNC paths too)
    for part in parts:
        if part in ("", ".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', part) or part != part.strip(" ") or part.endswith("."):
            raise CreationsError("UNSAFE_CKM", f"{member_label(raw)} has an unsafe file path")
        for form in (part, unicodedata.normalize("NFKC", part)):  # NFKC folds superscript digits and full-width letters
            if RESERVED_NAME.fullmatch(form.split(".")[0].rstrip(" ")):
                raise CreationsError("UNSAFE_CKM", f"{member_label(raw)} uses a Windows device name")
    if len(parts) < 2 or parts[0].lower() != "data":
        raise CreationsError("UNSAFE_CKM", f"{member_label(raw)} isn't inside the Data folder")
    return "/".join(("Data", *parts[1:]))


def scan_btar(fh, total: int) -> list[tuple[str, int, int]]:
    """Validate a BTAR container (version 1) and return (raw path, data offset, size) per entry without reading the data.
    The file must be consumed exactly."""
    def need(n: int, what: str) -> bytes:
        b = fh.read(n)
        if len(b) != n:
            raise CreationsError("INVALID_CKM", f"the .ckm is truncated (in {what})")
        return b

    head = fh.read(8)
    if len(head) < 8 or head[:4] != b"BTAR":
        raise CreationsError("INVALID_CKM", "not a BTAR container (bad magic)")
    version, count = struct.unpack("<HH", head[4:])
    if version != 1:
        raise CreationsError("UNSUPPORTED_CKM", f"BTAR version {version} is not supported (only version 1)")
    if count == 0:
        raise CreationsError("INVALID_CKM", "the .ckm holds no files")
    pos, members = 8, []
    for n in range(count):
        (plen,) = struct.unpack("<H", need(2, f"entry {n + 1} path length"))
        if plen == 0:
            raise CreationsError("UNSAFE_CKM", f"entry {n + 1} has an empty path")
        try:
            raw = need(plen, f"entry {n + 1} path").decode("utf-8")
        except UnicodeDecodeError:
            raise CreationsError("INVALID_CKM", f"entry {n + 1} has a path that is not UTF-8") from None
        (size,) = struct.unpack("<Q", need(8, f"entry {n + 1} size"))
        offset = pos + 2 + plen + 8
        if offset + size > total:
            raise CreationsError("INVALID_CKM", f"the .ckm is truncated (entry {raw!r} claims {size} bytes)")
        fh.seek(offset + size)
        pos = offset + size
        members.append((raw, offset, size))
    if pos != total:
        raise CreationsError("INVALID_CKM", f"the .ckm has {total - pos} trailing bytes after its last entry")
    return members


def ckm_to_zip(ckm: Path, dest: Path) -> dict:
    """Rewrite a .ckm as a stored zip (fixed timestamps and attributes, entries in ckm order, so the same input always
    gives the same bytes). Only ``Data`` members of an ALLOWED_SUFFIXES type are accepted (``UNSAFE_CKM`` otherwise).
    Returns {files}."""
    total = ckm.stat().st_size
    with ckm.open("rb") as fh:
        members = scan_btar(fh, total)
        names, seen, folders = [], set(), set()
        for raw, _, _ in members:
            name = safe_member(raw)
            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise CreationsError("UNSAFE_CKM", f"{member_label(name)} isn't a type this plugin accepts")
            key = name.casefold()
            if key in seen or key in folders:
                raise CreationsError("UNSAFE_CKM", f"{member_label(name)} is listed twice (or as both a file and a folder)")
            seen.add(key)
            parts = key.split("/")
            for i in range(1, len(parts)):
                folder = "/".join(parts[:i])
                if folder in seen:
                    raise CreationsError("UNSAFE_CKM", f"{member_label(name)} sits inside a path that is also a file")
                folders.add(folder)
            names.append(name)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
            for name, (_, offset, size) in zip(names, members):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 0
                info.external_attr = 0o100644 << 16
                info.file_size = size
                fh.seek(offset)
                with zf.open(info, "w") as out:
                    left = size
                    while left:
                        chunk = fh.read(min(CHUNK, left))
                        if not chunk:
                            raise CreationsError("INVALID_CKM", "the .ckm ended while it was being unpacked")
                        out.write(chunk)
                        left -= len(chunk)
    return {"files": names}


_ID_LIKE_HYPHEN = re.compile(r"-(?=[0-9]*-)")  # a hyphen that opens "-<digits>-" or "--"


def _no_nexus_id(text: str, filler: str) -> str:
    """``text`` with every hyphen that could open MO2's ``-<mod id>-`` pattern replaced by ``filler`` ("Pre-war" keeps its
    hyphen; "Mod -123- Pack" does not), so no Nexus mod id can be read out of it."""
    return _ID_LIKE_HYPHEN.sub(filler, text)


def zip_name(title: str, content_id: int, version: str) -> str:
    """``<title> (Creation <id>) v<version>.zip``, built so that Mod Organizer 2 cannot read a Nexus mod id from it.

    MO2 (``NexusInterface::interpretNexusFileName``) takes ``-<digits>-`` out of an archive's name as a Nexus mod id, so
    ``<title>-<id>-<version>.zip`` would make every installed Creation look like an unrelated Nexus mod. This form has no
    such pattern: a hyphen that could start one (in the title or the version) is replaced by a space (title) or ``_``
    (version). When cleaning changed the version (so two different versions could clean to the same text) a short hash
    of the raw version is added in brackets."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", clean_text(title))
    clean = re.sub(r"\s+", " ", clean).strip(" .")[:80].strip(" .")
    clean = re.sub(r" {2,}", " ", _no_nexus_id(clean, " ")).strip(" .") or "Creation"
    ver = re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "_", version or "").strip("._") or "0"
    ver = re.sub(r"^[vV](?=[0-9])", "", ver) or "0"  # the name adds its own "v"
    ver = _no_nexus_id(ver, "_")
    tail = ""
    if ver != (version or ""):
        tail = " [" + hashlib.sha256((version or "").encode("utf-8")).hexdigest()[:6] + "]"
    return f"{clean} (Creation {content_id}) v{ver}{tail}.zip"


# ---------------------------------------------------------------- the download's .meta
#
# MO2 reads and rewrites a download's .meta with QSettings (IniFormat), so values are written the way Qt's own writer
# writes them and read back the way Qt's reader reads them: a value with ";" "," or "=" (or a leading or trailing space)
# is quoted, "\" and '"' are backslash-escaped, control characters are escaped, and a leading "@" (which Qt reads as a
# variant) is doubled. Without this a comma makes MO2 read a list (and lose the value) and a semicolon truncates it.

_INI_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\a": "\\a", "\b": "\\b",
                "\f": "\\f", "\v": "\\v", "\0": "\\0"}
_INI_UNESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", '"': '"', "?": "?",
                  "'": "'", "\\": "\\"}
_HEX = "0123456789abcdefABCDEF"


def ini_quote(value) -> str:
    """``value`` as QSettings' IniFormat writer would write it."""
    text = str(value)
    out: list[str] = []
    after_hex = False
    for c in text:
        if after_hex and c in _HEX:
            out.append(f"\\x{ord(c):x}")
            continue
        after_hex = False
        if c in _INI_ESCAPES:
            out.append(_INI_ESCAPES[c])
        elif ord(c) < 0x20 or ord(c) == 0x7F:
            out.append(f"\\x{ord(c):x}")
            after_hex = True
        else:
            out.append(c)
    body = "".join(out)
    if text.startswith("@"):
        body = "@" + body
    if any(c in text for c in ";,=") or text[:1] == " " or text[-1:] == " ":
        body = '"' + body + '"'
    return body


def ini_unquote(raw: str) -> str:
    """The string QSettings' IniFormat reader makes of one ``key=value`` value (quotes and backslash escapes resolved; a
    comma stays a comma)."""
    s = raw.strip(" \t\r\n")
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            i += 1
        elif c == "\\" and i + 1 < n:
            nxt = s[i + 1]
            if nxt in _INI_UNESCAPES:
                out.append(_INI_UNESCAPES[nxt])
                i += 2
            elif nxt == "x":
                j = i + 2
                while j < n and j < i + 6 and s[j] in _HEX:
                    j += 1
                out.append(chr(int(s[i + 2:j], 16)) if j > i + 2 else "x")
                i = max(j, i + 2)
            elif nxt in "01234567":
                j = i + 1
                while j < n and j < i + 4 and s[j] in "01234567":
                    j += 1
                out.append(chr(int(s[i + 1:j], 8)))
                i = j
            else:
                out.append(nxt)
                i += 2
        else:
            out.append(c)
            i += 1
    text = "".join(out)
    return text[1:] if text.startswith("@@") else text


def read_meta(archive: Path) -> dict[str, str]:
    """The ``[General]`` keys of the ``.meta`` beside an archive (lower-cased keys, values unquoted the way QSettings reads
    them); empty when absent."""
    meta = Path(str(archive) + ".meta")
    out: dict[str, str] = {}
    try:
        text = meta.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return out
    section = ""
    for line in re.split(r"\r\n|\r|\n", text):
        line = line.strip(" \t")
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].lower()
        elif section == "general" and "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(k.strip().lower(), ini_unquote(v))
    return out


_MO2_STATE_KEYS = ("installed", "uninstalled", "removed", "paused")


def write_meta(archive: Path, p: Plan, ckm_md5: str) -> None:
    """The download ``.meta`` in the form MO2 writes it (``gameName`` is MO2's short name), with the Creation's
    provenance in [General].

    ``modID=-1``: MO2 only guesses a Nexus mod id out of the file name when the .meta says 0 (``InstallationManager::install``:
    ``if (modID == 0 && guessedModID != -1)``), and -1 is MO2's own "no Nexus id" value (``ModInfoRegular`` reads a missing
    ``modid`` as -1, and every Nexus feature tests ``> 0``). ``modName`` is what the install dialog proposes (MO2 ranks it
    above the file-name guess; ``name`` ranks lower and is read as HTML). When a .meta already exists for this archive its
    ``installed``/``uninstalled``/``removed``/``paused`` values are kept, never reset."""
    title, version = clean_text(p.title), clean_text(p.version)
    old = read_meta(archive)
    state = {k: (old[k].lower() if old.get(k, "").lower() in ("true", "false") else "false") for k in _MO2_STATE_KEYS}
    fields = [("gameName", MO2_GAME_NAMES[p.game]), ("modID", "-1"), ("fileID", "0"), ("name", title), ("modName", title),
              ("version", version), ("repository", REPOSITORY), ("url", page_url(p.game, p.uuid) or ""),
              ("installed", state["installed"]), ("uninstalled", state["uninstalled"]), ("paused", state["paused"]),
              ("removed", state["removed"]), ("creationsContentId", str(p.content_id)), ("creationsUuid", p.uuid or ""),
              ("creationsVersion", version), ("creationsCkmMd5", ckm_md5)]
    lines = ["[General]"] + [f"{k}={ini_quote(v)}" for k, v in fields]
    Path(str(archive) + ".meta").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8-sig", newline="")


def _creation_downloads(downloads: Path, game: str, content_id: int | None = None):
    """(archive, meta dict) for every zip in ``downloads`` whose ``.meta`` names a Creation of ``game`` (only the Creation
    ``content_id`` when given; any version)."""
    if not downloads.is_dir():
        return
    for meta in sorted(downloads.glob("*.meta")):
        archive = meta.with_suffix("")
        if not archive.is_file():
            continue
        m = read_meta(archive)
        if (m.get("repository", "").lower() == REPOSITORY.lower() and m.get("creationscontentid", "") != ""
                and (content_id is None or m.get("creationscontentid") == str(content_id))
                and game_alias(m.get("gamename", "")) in (game, "")):
            yield archive, m


def find_downloaded(downloads: Path, game: str, content_id: int, version: str) -> Path | None:
    """A zip in the downloads folder whose ``.meta`` names this Creation and version."""
    for archive, m in _creation_downloads(downloads, game, content_id):
        if m.get("creationsversion") == version:
            return archive
    return None


def _status_of(rows: list, latest_version: str | None) -> dict:
    """The local-status answer for one Creation from its ``(archive, meta)`` rows (see ``local_status``)."""
    latest = clean_text(latest_version)
    same, other = [], []
    for archive, m in rows:
        version = m.get("creationsversion", "")
        (same if (not latest or version == latest) else other).append((archive, version, m))

    def pick(found):
        archive, version, m = max(found, key=lambda r: r[0].stat().st_mtime)
        return {"archive": str(archive), "version": version, "meta_installed": m.get("installed", "").lower() == "true"}

    if same:
        return {"state": "downloaded", **pick(same)}
    if other:
        return {"state": "update", **pick(other)}
    return {"state": "none", "archive": None, "version": None, "meta_installed": False}


def local_status(downloads: Path, game: str, content_id: int, latest_version: str | None) -> dict:
    """What the downloads folder holds for one Creation, from the ``.meta`` files beside the zips.

    ``state``: ``"downloaded"`` when a download of ``latest_version`` exists, ``"update"`` when downloads exist only for
    other versions, ``"none"`` otherwise (with no ``latest_version`` given, any download counts as ``"downloaded"``).
    ``archive`` is the path of that zip (the newest when several match), ``version`` its version, ``meta_installed``
    whether MO2 marked it ``installed=true``."""
    game, cid = norm_game(game), _int(content_id)
    return _status_of(list(_creation_downloads(Path(downloads), game, cid)), latest_version)


def local_statuses(downloads: Path, game: str, latest: dict) -> dict:
    """``local_status`` for many Creations in one pass over the downloads folder (one read of each ``.meta``, however
    many Creations are asked about). ``latest`` maps a content id to its latest version (or None); the result maps the
    same ids to the same answers ``local_status`` gives."""
    game = norm_game(game)
    wanted = {_int(k): v for k, v in latest.items()}
    rows: dict[int, list] = {cid: [] for cid in wanted}
    for archive, m in _creation_downloads(Path(downloads), game):
        cid = _int(m.get("creationscontentid"), -1)
        if cid in rows:
            rows[cid].append((archive, m))
    return {cid: _status_of(found, wanted[cid]) for cid, found in rows.items()}


def nudge_downloads_watcher(downloads: Path) -> None:
    """Make Mod Organizer 2 re-read its Downloads folder: create and remove an empty marker file in it.

    MO2 (``DownloadManager::directoryChanged``) refreshes its list when the folder changes, but it ignores changes that
    arrive while a refresh is running, and the end of a refresh handles queued events while still counting as running.
    A download makes several changes in a row (temp files, the zip, its .meta), so the last of them can be dropped and
    the new zip not listed. One change made after things settle starts a fresh refresh. MO2 lists only archives, so the
    marker never shows. Never raises."""
    marker = Path(downloads) / f"~creations-browser-{uuidlib.uuid4().hex[:8]}.tmp"
    try:
        marker.touch(exist_ok=False)
    except OSError:
        return
    try:
        marker.unlink()
    except OSError:
        pass


def fetch(downloads: Path, p: Plan, progress: Progress | None = None, cancel: Cancel | None = None) -> tuple[Path, dict, bool]:
    """Get one Creation into ``downloads`` as a zip: reuse the one the ``.meta`` files name, else download the .ckm,
    verify it, convert it (create-only, via temp names) and write the ``.meta``. Returns (zip, info, reused).
    ``progress`` and ``cancel`` are passed to ``download_ckm``; a cancel leaves no file behind."""
    found = find_downloaded(downloads, p.game, p.content_id, p.version)
    if found:
        return found, {**p.public(), "zip_bytes": found.stat().st_size}, True
    downloads.mkdir(parents=True, exist_ok=True)
    dest = downloads / zip_name(p.title, p.content_id, p.version)
    tag = uuidlib.uuid4().hex[:8]
    ckm_tmp, zip_tmp = dest.with_name(f"{dest.name}.{tag}.ckm.part"), dest.with_name(f"{dest.name}.{tag}.zip.part")
    try:
        md5 = download_ckm(p.url, ckm_tmp, p.size, p.etag, progress, cancel)
        if cancel is not None and cancel():
            raise CreationsError("CANCELLED", "the download was cancelled")
        converted = ckm_to_zip(ckm_tmp, zip_tmp)
        if cancel is not None and cancel():  # the last look before anything becomes visible: "nothing was saved" stays true
            raise CreationsError("CANCELLED", "the download was cancelled")
        try:
            _rename_new(zip_tmp, dest)
        except FileExistsError:  # the zip is deterministic: the same bytes mean the same download whose .meta went missing
            if _sha256(dest) != _sha256(zip_tmp):
                raise CreationsError("TARGET_EXISTS", f"{dest} already exists with other content and no matching .meta; it was not overwritten") from None
    finally:
        for tmp in (ckm_tmp, zip_tmp):
            if tmp.exists():
                tmp.unlink()
    write_meta(dest, p, md5)
    return dest, {**p.public(), "zip_bytes": dest.stat().st_size, "files": converted["files"]}, False


def _rename_new(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst`` only if ``dst`` does not exist (one atomic step; ``FileExistsError`` otherwise)."""
    if os.name == "nt":
        os.rename(src, dst)  # on Windows rename fails when the target exists
    else:
        os.link(src, dst)  # elsewhere rename would replace, link would not
        os.unlink(src)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- lookups for the UI

def _description(item: dict) -> str:
    for key in ("description", "overview"):
        if isinstance(item.get(key), str) and item[key].strip():
            return clean_paragraphs(item[key])
    return ""


NO_PC_REASON = "No PC version: not available here"


PC_STORES = ("STEAM", "EPIC", "MICROSOFTSTORE", "GOG")  # the PC stores a Creation's price is listed for, in order of preference


def store_price(item: dict) -> dict | None:
    """What Bethesda's in-game Creations store asks for this Creation on PC, in Creation Credits (display only: a Creation
    with a store listing is never downloaded here). ``catalog_info`` lists one entry per store, each with ``prices`` in
    the store's virtual currency; the first PC store in PC_STORES that lists a credits price wins. Returns
    ``{"credits": n, "was": n or None, "store": name}`` (``was`` when the store marks a sale), or None."""
    entries = item.get("catalog_info")
    if not isinstance(entries, list):
        return None
    found: dict[str, dict] = {}
    for entry in entries[:20]:
        if not isinstance(entry, dict):
            continue
        store = str(entry.get("platform") or "").strip().upper()
        if store not in PC_STORES or store in found or not isinstance(entry.get("prices"), list):
            continue
        for p in entry["prices"][:10]:
            if not isinstance(p, dict) or str(p.get("currency_type") or "").strip().upper() != "VIRTUAL":
                continue
            credits, original = _num(p.get("amount")), _num(p.get("original_amount"))
            if credits is None or credits > 1_000_000:
                continue
            was = original if original is not None and original > credits and original <= 1_000_000 else None
            found[store] = {"credits": credits, "was": was, "store": store}
            break
    return next((found[s] for s in PC_STORES if s in found), None)


def _reason(status: str, withdrawn: bool = False, no_pc: bool = False) -> str | None:
    if withdrawn:
        return WITHDRAWN_REASON
    if status != "free":
        return "Paid Creation: get it in game" if status == "paid" else "Not clearly marked free: get it in game"
    return NO_PC_REASON if no_pc else None


def _no_pc_version(item: dict) -> bool:
    """True when the catalogue lists the platforms a Creation is on and Windows is not among them (a console-only Creation).
    A search result has no download tree, so this is how it is known before the Creation is opened."""
    platforms = item.get("hardware_platforms")
    return isinstance(platforms, list) and bool(platforms) and "WINDOWS" not in {str(x).strip().upper() for x in platforms}


def _summary(game: str, item: dict) -> dict:
    uid = item.get("content_uuid")
    status = free_status(item)
    withdrawn = is_withdrawn(item)
    no_pc = _no_pc_version(item)
    try:
        latest = pc_files(item)[0]
    except CreationsError:
        latest = None
    return {"content_id": item.get("content_id"), "uuid": uid, "title": item.get("title"), "author": item.get("author_displayname"),
            "author_verified": _truthy(item.get("author_verified")), "author_official": _truthy(item.get("author_official")),
            "free": status == "free", "free_status": status, "withdrawn": withdrawn,
            "downloadable": status == "free" and not withdrawn and not no_pc, "no_pc_version": no_pc,
            "get_in_game_reason": _reason(status, withdrawn, no_pc),
            "author_price": item.get("author_price") or None, "price": store_price(item),
            "in_creation_club": bool(item.get("catalog_info")), "platforms": item.get("hardware_platforms"),
            "description": _description(item),
            "downloads": all_downloads(item), "downloads_pc": pc_downloads(item), "downloads_all": all_downloads(item),
            "likes": like_count(item), "subscribes": subscribe_count(item), "rating": rating_level(item),
            "published": published_time(item), "updated": updated_time(item), "categories": category_names(item),
            "version": latest["version"] if latest else None, "bytes": latest["size"] if latest else None,
            "required_mods": [m for m in (item.get("required_mods") or []) if isinstance(m, (int, str))],
            "required_dlc": [clean_text(d) if isinstance(d, str) else d for d in (item.get("required_dlc") or [])],
            "page": page_url(game, uid)}


def _pc_summary(item: dict) -> dict:
    try:
        files = pc_files(item)
    except CreationsError as exc:
        return {"pc_file": None, "pc_file_problem": str(exc)}
    f = files[0]
    return {"pc_file": {"version": f["version"], "bytes": f["size"], "file": f["name"], "ctime": f["ctime"]},
            "versions": [x["version"] for x in files]}


RELEASE_NOTES_LIMIT = 20


def pc_release_notes(item: dict) -> list[dict]:
    """The release notes the catalogue lists for the PC (WINDOWS) build, as ``[{"version", "note"}]`` in the catalogue's
    order, at most RELEASE_NOTES_LIMIT, each note cleaned to safe plain text (see clean_paragraphs). The catalogue's shape is
    a list of ``{hardware_platform, release_notes: [{note, version_name, ...}]}``; anything else yields fewer or no notes."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    groups = item.get("release_notes")
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict) or group.get("hardware_platform") != "WINDOWS":
            continue
        notes = group.get("release_notes")
        for entry in notes if isinstance(notes, list) else []:
            if isinstance(entry, str):
                version, text = "", entry
            elif isinstance(entry, dict):
                version = entry.get("version_name") or entry.get("version") or ""
                text = next((entry[k] for k in ("note", "notes", "text", "description") if isinstance(entry.get(k), str)), "")
            else:
                continue
            note = clean_paragraphs(text)
            if note and (clean_text(version), note) not in seen:  # the live catalogue repeats identical entries
                seen.add((clean_text(version), note))
                out.append({"version": clean_text(version), "note": note})
            if len(out) >= RELEASE_NOTES_LIMIT:
                return out
    return out


# ---------------------------------------------------------------- the preview image

PREVIEW_HOST = DOWNLOAD_HOST  # the s3bucket the catalogue names must be exactly this
PREVIEW_MAX_BYTES = 5 * 1024 ** 2
PREVIEW_MAX_SIDE = 10000  # pixels, when the catalogue lists a size
_IMAGE_MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "jpeg": b"\xff\xd8\xff", "webp": b"RIFF"}
log = logging.getLogger("creations_browser")


class _ValidatedPreview(dict):
    """The reduced ``{file_type, s3key}`` preview that ``lookup_detail`` builds from a checked ``preview_image``. Only this
    class (which catalogue JSON can never produce) is accepted as the reduced form, so a raw item cannot skip the bucket check."""


def _preview_media(item: dict) -> dict | None:
    """The preview's ``{file_type, s3key}``, validated, or None. Reads ``preview_image`` (the catalogue's media dict, whose
    ``s3bucket`` must be the download host) or, from ``lookup_detail`` output only, its reduced ``preview``."""
    media = item.get("preview_image")
    reduced = False
    if not isinstance(media, dict):
        media, reduced = item.get("preview"), True
        if not isinstance(media, _ValidatedPreview):
            return None
    key = media.get("s3key")
    file_type = str(media.get("file_type") or "").strip().lower().rsplit("/", 1)[-1].lstrip(".")
    if not reduced and media.get("s3bucket") != PREVIEW_HOST:
        return None
    if file_type not in _IMAGE_MAGIC or not isinstance(key, str) or not key or len(key) > 512:
        return None
    if key.startswith("/") or "\\" in key or any(ord(c) < 32 or c in "?#" for c in key):
        return None
    if any(part in ("", ".", "..") for part in key.split("/")):
        return None
    for dim in ("width", "height"):
        if dim in media and not (isinstance(media[dim], int) and not isinstance(media[dim], bool) and 0 < media[dim] <= PREVIEW_MAX_SIDE):
            return None
    return {"file_type": file_type, "s3key": key}


def preview_image_bytes(item: dict, max_bytes: int = PREVIEW_MAX_BYTES) -> bytes | None:
    """The Creation's preview picture (png, jpg or webp, at most ``max_bytes``) from ``https://ugcmods.bethesda.net/<s3key>``,
    or None when there is none or anything is wrong. Never raises. No key header is sent and no redirect is followed. Call it
    only when the user has selected the Creation. ``item`` is the catalogue item or a ``lookup_detail`` result."""
    try:
        media = _preview_media(item)
        if media is None:
            return None
        url = f"https://{PREVIEW_HOST}/{urllib.parse.quote(media['s3key'], safe='/')}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with _open(request, 30) as response:
            data = response.read(max_bytes + 1)
        if not data or len(data) > max_bytes or not data.startswith(_IMAGE_MAGIC[media["file_type"]]):
            return None
        if media["file_type"] == "webp" and data[8:12] != b"WEBP":
            return None
        return data
    except Exception as exc:  # the picture is optional: never let it disturb the window
        log_line(logging.DEBUG, f"preview image skipped ({type(exc).__name__})")
        return None


# ---------------------------------------------------------------- lookups

def lookup_search(game: str, query: str, limit: int = 20) -> list[dict]:
    """Search the public catalogue; one summary dict per Creation (see ``_summary``). Reads the network, writes nothing.
    Neither the client key nor a signed download link is ever part of the result. Raises CreationsError."""
    g = norm_game(game)
    if not isinstance(query, str) or not query.strip():
        raise CreationsError("INVALID_ARGUMENT", "give some text to search for")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
        raise CreationsError("INVALID_ARGUMENT", "limit must be a whole number from 1 to 50")
    return [_summary(g, i) for i in search(g, text=query.strip(), size=limit)]


def lookup_page(game: str, text: str | None = None, **options) -> dict:
    """One page of the catalogue as summaries: ``{"rows", "total", "page", "size"}``. ``text`` may be empty (a browse).
    ``options`` are page, size, sort, order, time_period, platforms, marketplace, category and author; every value is
    checked first (INVALID_ARGUMENT). Neither the client key nor a signed link is part of the result. Raises CreationsError."""
    g = norm_game(game)
    items, total = search_page(g, text, **options)
    return {"rows": [_summary(g, i) for i in items], "total": total, "page": options.get("page", 1),
            "size": options.get("size", 10)}


def _collect(game: str, text: str | None, cap: int, cancel: Cancel | None, progress: Callable[[int, int], None] | None,
             options: dict) -> dict:
    """Pages of ``MAX_PAGE_SIZE``, one request after the other, until ``cap`` Creations are kept or the catalogue runs out.
    ``cancel()`` is asked before every request. ``progress(pages_done, pages_expected)`` is called after each page."""
    max_pages = -(-cap // MAX_PAGE_SIZE)
    rows: list[dict] = []
    seen: set = set()
    total = 0
    for page in range(1, max_pages + 1):
        if cancel is not None and cancel():
            raise CreationsError("CANCELLED", "the search was replaced by a newer one")
        items, total = search_page(game, text, page=page, size=MAX_PAGE_SIZE, **options)
        for item in items:
            key = item.get("content_id")
            if key not in seen:
                seen.add(key)
                rows.append(_summary(game, item))
        if progress is not None:
            progress(page, max(1, min(max_pages, -(-total // MAX_PAGE_SIZE))))
        if not items or len(rows) >= min(cap, total):
            break
    return {"rows": rows[:cap], "total": max(total, len(rows[:cap])), "capped": total > cap}


def lookup_collect(game: str, text: str, *, cap: int = TEXT_CAP, cancel: Cancel | None = None, **filters) -> dict:
    """Every match for the search words, page by page (``MAX_PAGE_SIZE`` per request, one request after the other), up to
    ``cap`` Creations, so at most ``ceil(cap / MAX_PAGE_SIZE)`` requests. ``filters`` are platforms, marketplace, category
    and author. Returns ``{"rows", "total", "capped"}``: the rows in Bethesda's order, how many Creations match in all, and
    whether that is more than were kept. ``cancel()`` is asked before every request; when it is True the search stops with
    ``CreationsError("CANCELLED")``."""
    g = norm_game(game)
    if not isinstance(text, str) or not text.strip():
        _refuse("give some text to search for")
    cap = _whole(cap, 1, TEXT_CAP, "cap")
    return _collect(g, text, cap, cancel, None, filters)


def lookup_browse_collect(game: str, *, sort: str = POPULARITY_ORDER[0], order: str = POPULARITY_ORDER[1],
                          time_period: str | None = POPULARITY_ORDER[2], cap: int = BROWSE_CAP, cancel: Cancel | None = None,
                          progress: Callable[[int, int], None] | None = None, **filters) -> dict:
    """The first ``cap`` Creations (at most ``BROWSE_CAP``) of a browse in Bethesda's ``sort``/``order``/``time_period``
    (Bethesda's popularity order by default), page by page, one request after the other: at most ``ceil(cap / MAX_PAGE_SIZE)``
    requests (ten for a thousand). ``filters`` are platforms, marketplace, category and author. Returns ``{"rows", "total",
    "capped"}`` as ``lookup_collect`` does. ``cancel()`` is asked before every request (``CreationsError("CANCELLED")`` when it
    is True); ``progress(pages_done, pages_expected)`` is called after each page."""
    g = norm_game(game)
    cap = _whole(cap, 1, BROWSE_CAP, "cap")
    return _collect(g, None, cap, cancel, progress, {"sort": sort, "order": order, "time_period": time_period, **filters})


def sort_rows(rows: list[dict], mode: str) -> list[dict]:
    """``rows`` (summaries) in the order of a window sort choice, as a new list; equal rows keep Bethesda's order.
    ``match`` is that order itself. Most downloaded and Most liked have no server sort, so only this can order them."""
    if mode not in SORT_MODES:
        _refuse(f"sort must be one of {list(SORT_MODES)}")
    if mode == "match":
        return list(rows)
    if mode == "title":
        return sorted(rows, key=lambda r: str(r.get("title") or "").casefold())
    keys = {"popular": lambda r: r.get("subscribes") or 0, "weekly": lambda r: r.get("subscribes") or 0,
            "monthly": lambda r: r.get("subscribes") or 0, "downloads": lambda r: r.get("downloads") or 0,
            "likes": lambda r: r.get("likes") or 0,
            "rating": lambda r: (-1 if r.get("rating") is None else r["rating"], r.get("likes") or 0),
            "newest": lambda r: r.get("published") or 0, "updated": lambda r: r.get("updated") or 0}
    return sorted(rows, key=keys[mode], reverse=True)


# ---------------------------------------------------------------- the Creations the game itself lists

# Each game keeps a JSON record of the Creations it downloaded through its in-game Creations menu, at
# ``%LOCALAPPDATA%\<folder>\ContentCatalog.txt`` (Skyrim also under the GOG and Epic folder names). It is only read, never written,
# and it is treated as untrusted: the read is capped, a broken file falls back to a key scan, and nothing here raises.
CATALOG_FOLDERS = {"fallout4": ("Fallout4",),
                   "skyrimspecialedition": ("Skyrim Special Edition", "Skyrim Special Edition GOG", "Skyrim Special Edition EPIC")}
CATALOG_FILE = "ContentCatalog.txt"
CATALOG_MAX_BYTES = 5 * 1024 ** 2  # the most of the file that is read
CATALOG_MAX_ENTRIES = 2000  # the most Creations kept from one file
_CATALOG_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_CATALOG_KEY = re.compile(rf"[A-Z0-9]{{1,8}}_(?:({_CATALOG_UUID})|([0-9]{{1,9}}))")  # e.g. CSV2_<uuid> or CSV2_5615
_CATALOG_KEY_SCAN = re.compile(rf'"([A-Z0-9]{{1,8}}_(?:{_CATALOG_UUID}|[0-9]{{1,9}}))"\s*:\s*\{{')
OWNED_REQUEST_CAP = 300  # the most Creations of the game's list that are looked up (three requests of 100)


def catalog_paths(game: str, base=None) -> list[Path]:
    """The ``ContentCatalog.txt`` files that exist for ``game`` under ``base`` (``%LOCALAPPDATA%`` by default): one per game
    edition folder (Skyrim: Steam, GOG and Epic)."""
    g = norm_game(game)
    root = base if base is not None else os.environ.get("LOCALAPPDATA")
    if not root:
        return []
    found = []
    for folder in CATALOG_FOLDERS[g]:
        path = Path(root) / folder / CATALOG_FILE
        try:
            if path.is_file():
                found.append(path)
        except OSError:
            continue
    return found


def _catalog_keys(path: Path) -> list[str]:
    """The Creation keys (``CSV2_...``) a catalogue file names, in file order, at most CATALOG_MAX_ENTRIES. The read is capped at
    CATALOG_MAX_BYTES; a file that is not valid JSON is scanned for ``"KEY": {`` instead. Returns [] for anything unreadable."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(CATALOG_MAX_BYTES + 1)
    except OSError:
        return []
    text = raw[:CATALOG_MAX_BYTES].decode("utf-8-sig", errors="replace")
    keys: list[str] = []
    try:
        data = json.loads(text) if len(raw) <= CATALOG_MAX_BYTES else None
    except (ValueError, RecursionError):
        data = None
    if isinstance(data, dict):
        candidates = (k for k, v in data.items() if isinstance(v, dict))
    elif data is None:  # too big (cut off), or not JSON
        candidates = (m.group(1) for m in _CATALOG_KEY_SCAN.finditer(text))
    else:
        candidates = iter(())
    for key in candidates:
        if isinstance(key, str) and _CATALOG_KEY.fullmatch(key):
            keys.append(key)
            if len(keys) >= CATALOG_MAX_ENTRIES:
                break
    return keys


def game_catalog(game: str, base=None) -> dict:
    """The Creations ``game`` lists as downloaded through its in-game Creations menu, merged over every edition folder that has
    a list: ``{"ids": set of numeric content ids, "uuids": set of lower-case UUIDs, "count": how many Creations}``. A Fallout 4
    entry is keyed by UUID and a Skyrim one by number; either may be either. Other keys, titles and versions are ignored. Reads
    local files only (``base`` is where they are looked for, ``%LOCALAPPDATA%`` by default), changes nothing, and never raises
    for what the files hold (a missing or broken file is an empty list)."""
    ids: set[int] = set()
    uuids: set[str] = set()
    try:
        paths = catalog_paths(game, base)
    except OSError:
        paths = []
    for path in paths:
        try:
            for key in _catalog_keys(path):
                m = _CATALOG_KEY.fullmatch(key)
                if m is None:
                    continue
                if m.group(1):
                    uuids.add(m.group(1).lower())
                else:
                    ids.add(int(m.group(2)))
        except Exception as exc:  # the game's file is not ours: whatever is in it must not disturb the window
            log_line(logging.DEBUG, f"game catalogue skipped ({type(exc).__name__})")
    return {"ids": ids, "uuids": uuids, "count": len(ids) + len(uuids)}


def catalog_refs(catalog: dict) -> list:
    """A game catalogue's references in one fixed order for lookups: the numeric ids ascending, then the UUIDs A to Z."""
    return sorted(catalog.get("ids") or ()) + sorted(catalog.get("uuids") or ())


def lookup_ids(game: str, refs, *, cancel: Cancel | None = None, progress: Callable[[int, int], None] | None = None) -> dict:
    """Summaries of the Creations named by ``refs`` (numeric ids and UUIDs, mixed), asked for ``MAX_CONTENT_IDS`` at a time, one
    request after the other, for at most ``OWNED_REQUEST_CAP`` of them: at most three requests. Returns ``{"rows", "total",
    "capped"}``: the summaries (without repeats, in the order Bethesda answered), how many distinct references were given, and
    whether that is more than were looked up. ``cancel()`` is asked before every request (``CreationsError("CANCELLED")`` when it
    is True); ``progress(requests_done, requests_expected)`` is called after each. No references means no request."""
    g = norm_game(game)
    if isinstance(refs, (str, bytes)):
        _refuse("refs must be a list of Creation ids or UUIDs")
    wanted: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        one = _content_ids([ref])  # checks one reference and writes it the way it is sent
        if one not in seen:
            seen.add(one)
            wanted.append(one)
    use = wanted[:OWNED_REQUEST_CAP]
    batches = [use[i:i + MAX_CONTENT_IDS] for i in range(0, len(use), MAX_CONTENT_IDS)]
    rows: list[dict] = []
    kept: set = set()
    for n, batch in enumerate(batches, start=1):
        if cancel is not None and cancel():
            raise CreationsError("CANCELLED", "the lookup was replaced by a newer one")
        items, _ = search_page(g, None, content_ids=batch, page=1, size=MAX_PAGE_SIZE)
        for item in items:
            key = item.get("content_id")
            if key not in kept:
                kept.add(key)
                rows.append(_summary(g, item))
        if progress is not None:
            progress(n, len(batches))
    return {"rows": rows, "total": len(wanted), "capped": len(wanted) > OWNED_REQUEST_CAP}


def lookup_detail(game: str, ref) -> dict:
    """One Creation (``ref``: numeric id, UUID or creations.bethesda.net link): its summary, its latest PC file and
    versions, its PC release notes, the reduced ``preview`` (``{file_type, s3key}``, for ``preview_image_bytes``) and
    ``required_mods`` resolved to titles. Reads the network, writes nothing. Raises CreationsError."""
    g = norm_game(game)
    cid, _ = resolve(g, ref)
    item = detail(g, cid)
    out = {"game": g, **_summary(g, item), **_pc_summary(item)}
    if out["downloadable"] and out["pc_file"] is None:
        out["downloadable"] = False
        out["get_in_game_reason"] = "No PC download is listed for this Creation: get it in game"
    out["release_notes"] = pc_release_notes(item)
    media = _preview_media(item)
    out["preview"] = _ValidatedPreview(media) if media else None
    mods = []
    for rid in out["required_mods"][:10]:
        rnum = numeric_id(rid)
        try:
            if rnum is None or rnum <= 0:
                raise CreationsError("INVALID_ARGUMENT", "not an id")
            req = detail(g, rnum)
            mods.append({"content_id": rnum, "title": req.get("title"), "uuid": req.get("content_uuid"), "free": is_free(req),
                         "downloadable": is_free(req) and not is_withdrawn(req), "price": store_price(req)})
        except CreationsError:
            mods.append({"content_id": rid, "title": None})
    out["required_mods"] = mods
    return out
