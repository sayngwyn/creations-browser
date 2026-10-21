"""Fakes shared by the tests: Bethesda's catalogue and download host as an in-process transport (no real network)."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import urllib.error
import urllib.parse
import urllib.request

from creations_browser import core

KEY = "PUBLIC-CLIENT-KEY-1234567890abcdef"
HOST = "https://ugcmods.bethesda.net"
UUID = "11111111-2222-3333-4444-555555555555"
PLUGIN = b"TES4" + b"\x00" * 20


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def btar(entries, version=1, count=None, extra=b"", magic=b"BTAR"):
    body = b"".join(struct.pack("<H", len(p.encode())) + p.encode() + struct.pack("<Q", len(d)) + d for p, d in entries)
    return magic + struct.pack("<HH", version, len(entries) if count is None else count) + body + extra


GOOD_ENTRIES = [("data\\Fine.esp", PLUGIN), ("data\\Fine.ini", b""), ("Data\\Fine - Main.ba2", b"BTDX" + b"\x08" * 12)]


def make_stats(pc=None, total=None, likes=0, subs=0, rating=None, views=0):
    """The catalogue's ``stats``: a block per platform (and ``ALL``) plus ``totals``. ``pc`` None leaves the WINDOWS block out."""
    def block(downloads):
        out = {"downloads": downloads, "likes": likes, "views": views, "bookmarks": 0, "subscribes": subs, "enableds": 0}
        if rating is not None:
            out["rating"] = rating
        return out
    total = (pc or 0) if total is None else total
    platforms = {"ALL": block(total)}
    if pc is not None:
        platforms["WINDOWS"] = block(pc)
    return {"platforms": platforms, "totals": block(total)}


def api_item(cid=743724, uuid=UUID, title="Fine Mod: Revamped", price="", catalog="", ckm=None, version="1.04", ctime=100, **extra):
    ckm = ckm if ckm is not None else btar(GOOD_ENTRIES)
    published = [{"version_name": version, "ctime": ctime, "client": {"main": {
        "download_url": f"{HOST}/files/{cid}/{urllib.parse.quote(version)}/{cid}.ckm?sig=SIGNED-SECRET&exp=1", "etag": f'"{hashlib.md5(ckm).hexdigest()}"',
        "size": len(ckm), "ctime": ctime}}}]
    return {"content_id": cid, "content_uuid": uuid, "title": title, "author_displayname": "Someone", "author_price": price,
            "catalog_info": catalog, "hardware_platforms": ["WINDOWS"], "required_mods": [], "required_dlc": [],
            "download": [{"hardware_platform": "WINDOWS", "published": published}],
            "stats": make_stats(pc=1200, total=1500, likes=10, subs=300, rating=1), "ptime": 1700000000, "utime": 1710000000,
            "first_ptime": 1690000000, "categories": ["Quests"], **extra}


class Net:
    """The fake transport behind core._open. ``items`` is keyed by numeric id; ``files`` by download path."""

    def __init__(self):
        self.items: dict[int, dict] = {}
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.category_names = {"fallout4": ["Weapons", "Armor", "Quests", "Q&A Things"], "skyrim": ["Armor", "Quests"]}
        self.categories_wrapped = False  # whether the categories answer comes wrapped as platform.response
        self.categories_fail = False

    def add(self, item, ckm=None):
        self.items[int(item["content_id"])] = item
        for group in item.get("download", []):
            for pub in group.get("published", []):
                for meta in pub.get("client", {}).values():
                    self.files[urllib.request.urlsplit(meta["download_url"]).path] = ckm if ckm is not None else btar(GOOD_ENTRIES)
        return item

    def __call__(self, request, timeout):
        url, headers = request.full_url, {k.lower(): v for k, v in request.header_items()}
        self.calls.append((url, headers))
        parts = urllib.request.urlsplit(url)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
        if url == core.CORE_URL:
            return Response(json.dumps({"ugc": {"bnetKey": KEY}}).encode())
        if parts.netloc == "api.bethesda.net":
            assert headers["x-bnet-key"] == KEY and headers["x-bnet-product"] in ("fallout4", "skyrim")
            path = parts.path.removeprefix("/ugcmods/v1/")
            if path == "content":
                return Response(json.dumps({"platform": {"response": self._content(q)}}).encode())
            if path == "categories":
                if self.categories_fail:
                    raise urllib.error.HTTPError(url, 500, "Server Error", {}, io.BytesIO(b"{}"))
                names = self.category_names[q["product"]]
                body = {"categories": [{"id": i + 1, "name": n, "product": q["product"], "ctime": 0} for i, n in enumerate(names)]}
                return Response(json.dumps({"platform": {"response": body}} if self.categories_wrapped else body).encode())
            cid = int(path.rsplit("/", 1)[1])
            if cid not in self.items:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b'{"message":"Content does not exist"}'))
            return Response(json.dumps({"platform": {"response": self.items[cid]}}).encode())
        if parts.netloc == "ugcmods.bethesda.net":
            assert "x-bnet-key" not in headers, "the client key must never go to the download host"
            return Response(self.files[parts.path])
        raise AssertionError(f"unexpected request {url}")

    SORTS = {"subscriptions": lambda i: i["stats"]["totals"]["subscribes"], "ptime": lambda i: i.get("ptime", 0),
             "utime": lambda i: i.get("utime", 0), "ctime": lambda i: i.get("ctime", 0),
             "title": lambda i: i["title"].casefold(), "rating": lambda i: i["stats"]["totals"].get("rating", -1)}

    def _content(self, q):
        """The content endpoint: filters, then (like the real one) a sort only when there are no search words, then paging."""
        found = list(self.items.values())
        if "content_ids" in q:
            wanted = q["content_ids"].split(",")
            assert 1 <= len(wanted) <= 100, "a content_ids request names at most 100 Creations"
            found = [i for i in found if i["content_uuid"] in wanted or str(i["content_id"]) in wanted]
        if "text" in q:
            found = [i for i in found if q["text"].lower() in i["title"].lower()]
        elif "sort" in q:
            found.sort(key=self.SORTS[q["sort"]], reverse=q.get("order", "desc") == "desc")
        if "hardware_platforms" in q:
            found = [i for i in found if q["hardware_platforms"] in (i.get("hardware_platforms") or [])]
        if q.get("marketplace") == "false":
            found = [i for i in found if not i.get("marketplace")]
        if "categories" in q:
            found = [i for i in found if q["categories"] in (i.get("categories") or [])]
        if "author_displayname" in q:
            found = [i for i in found if i.get("author_displayname") == q["author_displayname"]]
        out = {"total": len(found)}
        if "size" in q:
            page, size = int(q.get("page", 1)), int(q["size"])
            start = (page - 1) * size
            out.update(page=page, size=size, **{"from": start + 1, "to": min(start + size, len(found))})
            found = found[start:start + size]
        out["data"] = [self._search(i) for i in found]
        return out

    @staticmethod
    def _search(item):
        return {k: v for k, v in item.items() if k != "download"}

    def downloads(self):
        return [u for u, _ in self.calls if "ugcmods.bethesda.net" in u]


def secrets_absent(*objs):
    text = json.dumps(objs)
    assert KEY not in text and "SIGNED-SECRET" not in text and "sig=" not in text and "x-bnet" not in text.lower()


def install(monkeypatch) -> "Net":
    """Replace urlopen itself, the real seam's opener and core._open; return the fake. No test reaches the real network."""
    def no_network(*a, **k):
        raise AssertionError("a test reached the real network")
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", no_network)  # the real seam builds its own opener
    fake = Net()
    monkeypatch.setattr(core, "_open", fake)
    monkeypatch.setattr(core, "_key_cache", [])
    return fake
