"""The words the window uses, kept apart from Qt so they can be checked and reused. Plain Python, no Qt, no ``mobase``."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import core

NOTICE = ("Unofficial; not affiliated with Bethesda or ZeniMax. Only free Creations are downloaded, straight from Bethesda's "
          "servers. You are responsible for following Bethesda's terms; each Creation's author keeps all rights.")
TOOL_DESCRIPTION = ("Browse Bethesda Creations for Fallout 4 and Skyrim Special Edition, see what paid ones cost and which "
                    "ones your game already has, and download the free ones as ordinary downloads that Mod Organizer 2 "
                    "installs as usual. Unofficial.")
TOOL_TOOLTIP = "Browse Bethesda Creations and download the free ones (unofficial)"
SEARCH_PLACEHOLDER = "Search Creations, or paste a creations.bethesda.net link or ID"
GET_IN_GAME = "Get it in game"

# The sort choices, by the keys ``core.SORT_MODES`` uses, in the order the list shows them.
SORT_LABELS = {"match": "Best match", "downloads": "Most downloaded", "weekly": "Trending this week",
               "monthly": "Popular this month", "popular": "Most popular (all time)", "likes": "Most liked",
               "rating": "Best rated", "newest": "Newest", "updated": "Recently updated", "title": "Title A–Z"}
ALL_CATEGORIES = "All categories"
SORT_TIP_WORDLESS = "Not available with search words"
SORT_TIP_PERIOD = "Not available for the Creations in your game: Bethesda gives no weekly or monthly figures per Creation"
SORT_TIP_MATCH = "Best match needs search words"
VERIFIED_LABEL = "Verified creators only"
VERIFIED_TIP = "Only Creations by Bethesda's Verified Creators"
OFFICIAL_LABEL = "Official creations only"
OFFICIAL_TIP = "Only Creations published by Bethesda"
VERIFIED_BADGE = "✓ Verified"
OFFICIAL_BADGE = "✓ Official"
OWNED_LABEL = "In my game only"
OWNED_TIP = "Creations your game downloaded through its Creations menu (read from the game's own list on this PC)"
OWNED_MARKER = "In your game"
OWNED_DETAIL = "In your game: you have this through the game's Creations menu."
OWNED_NOT_DOWNLOADABLE = "It's a store Creation, so your game's Creations menu looks after it."
OWNED_NONE = ("Your game hasn't downloaded any Creations through its Creations menu yet (or this PC's game list wasn't "
              "found).")
OWNED_NO_MATCH = "None of the Creations in your game match those words."
OWNED_NO_DETAILS = "Bethesda's catalogue has no details for the Creations your game lists."
OWNED_GREY_TIP = "Not used while only the Creations in your game are shown"
BROWSE_HINT = ("Search for a Creation, or paste a creations.bethesda.net link or ID. Leave the box empty and press Browse "
               "to see everything. Pick one from the list to see its details.")

GAME_LABELS = core.GAME_LABELS

# What each failure means for the person using the window. The catalogue's own wording is not shown for these.
_ERRORS = {
    "NOT_FREE": "This Creation isn't free, so it isn't downloaded here. Get it in game.",
    "FREE_UNKNOWN": "Bethesda's listing doesn't clearly say this Creation is free, so it isn't downloaded here. Get it in game.",
    "UNAVAILABLE": "Bethesda has withdrawn or restricted this Creation, so it isn't available here.",
    "NO_PC_FILE": "This Creation has no PC download listed, so there is nothing to download here.",
    "NOT_FOUND": "That Creation wasn't found for this game. Check the link or ID, and that the right game is picked.",
    "DOWNLOAD_MISMATCH": "The downloaded file didn't match what Bethesda lists, so it was thrown away and nothing was saved. "
                         "Try again.",
    "TOO_BIG": "That download is larger than this plugin's safety limit, so it was stopped and nothing was saved.",
    "UNSAFE_CKM": "The download holds files that aren't plain game data, so it was refused and nothing was saved.",
    "INVALID_CKM": "The download was damaged or in a layout this plugin doesn't understand, so nothing was saved.",
    "UNSUPPORTED_CKM": "The download uses a newer package format than this plugin understands, so nothing was saved. "
                       "This plugin may need an update.",
    "UNEXPECTED_HOST": "Bethesda's listing pointed somewhere unexpected, so nothing was downloaded.",
    "CANCELLED": "Download cancelled. Nothing was saved.",
    "TARGET_EXISTS": "A file with that name is already in your Downloads folder with different content, so it was not "
                     "overwritten. Move or rename it, then try again.",
    "UNREACHABLE": "Couldn't reach Bethesda's servers. Check your internet connection and try again.",
    "RATE_LIMITED": "Bethesda's service asked us to slow down. Wait a minute and try again.",
    "CREATIONS_ERROR": "Bethesda's service didn't answer as expected; it may be down or may have changed. Try again later; "
                       "if it keeps happening, report it.",
}


def describe_error(status: str, message: str = "", refused: str = "") -> str:
    """A plain-words explanation of a ``CreationsError`` status. Never includes the catalogue key or a signed link.
    ``refused`` is the reason core gives for refusing a package member (the ``UNSAFE_CKM`` message, which holds only the
    member's cleaned, shortened name): when given, the explanation names the file."""
    if status == "INVALID_ARGUMENT":
        return f"Check what you typed: {message}" if message else "Check what you typed."
    if status == "UNSAFE_CKM" and refused.strip():
        return f"The download was refused: {core.clean_text(refused).rstrip('.')}. Nothing was saved."
    return _ERRORS.get(status) or _ERRORS["CREATIONS_ERROR"]


def human_bytes(n: int | None) -> str:
    if n is None or n < 0:
        return "unknown size"
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} bytes"


def looks_like_reference(text: str) -> bool:
    """True when ``text`` is a Creation id (digits), a Creation UUID or a creations.bethesda.net link rather than search words."""
    t = text.strip()
    return bool(t) and (re.fullmatch(r"[0-9]+", t) is not None or core.UUID_RE.fullmatch(t) is not None
                        or "creations.bethesda.net" in t.lower())


def instance_game_key(short_name: str | None) -> str | None:
    """The game key for MO2's ``gameShortName()`` ("Fallout4", "SkyrimSE"), or None for a game Creations Browser does not handle."""
    if not short_name:
        return None
    key = core.game_alias(short_name)
    return key if key in core.PRODUCTS else None


def state_label(state: dict | None) -> str:
    """How a Creation sits in the Downloads folder: '', 'Downloaded', 'Installed' or 'Update available'."""
    if not state or state.get("state") == "none":
        return ""
    if state["state"] == "update":
        return "Update available"
    return "Installed" if state.get("meta_installed") else "Downloaded"


def marker(summary: dict, owned: bool = False) -> str:
    """The clear free / not-free marker for a result row; "In your game" for a Creation the game itself lists (``owned``)."""
    if owned:
        return OWNED_MARKER
    if summary.get("withdrawn"):
        return "Withdrawn"
    if summary.get("get_in_game_reason") == core.NO_PC_REASON:
        return "No PC version"
    if summary.get("downloadable"):
        return "Free"
    return price_text(summary.get("price"), short=True) or GET_IN_GAME


def price_text(price: dict | None, short: bool = False) -> str:
    """A store price in words: "400 credits" (short) or "400 Creation Credits", with "on sale, was 500" when the store
    marks a sale; a price of 0 is a store item that is free to claim in game. "" when there is no price."""
    if not isinstance(price, dict) or not isinstance(price.get("credits"), int):
        return ""
    credits = price["credits"]
    if credits == 0:
        return "Claim in game (free)" if short else "0 Creation Credits (claim it in game)"
    text = f"{credits:,} credits" if short else f"{credits:,} Creation Credits"
    if isinstance(price.get("was"), int):
        text += f" (sale, was {price['was']:,})" if short else f", on sale (was {price['was']:,})"
    return text


def availability(summary: dict, owned: bool = False) -> str:
    """The details panel's one line on whether a Creation can be downloaded here, and if not, why and where to get it (a
    Creation the game already lists, ``owned``, is not sent back to the game's Creations menu)."""
    if summary.get("downloadable"):
        return "Free: can be downloaded here."
    if owned and not summary.get("withdrawn") and summary.get("get_in_game_reason") != core.NO_PC_REASON:
        return OWNED_NOT_DOWNLOADABLE
    price = summary.get("price")
    if price_text(price) and not summary.get("withdrawn"):
        where = ", free to claim" if price["credits"] == 0 else f" for {price_text(price)}"
        return f"In Bethesda's Creations store{where}. Get it in the game's Creations menu."
    reason = summary.get("get_in_game_reason") or GET_IN_GAME
    return reason[0].upper() + reason[1:] + ("" if reason.endswith(".") else ".")


# ---------------------------------------------------------------- numbers, dates and the stats lines

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
RATING_WORDS = {2: "Very positive", 1: "Positive"}  # LOW and no rating say nothing


def compact_number(n: int | None) -> str:
    """A count in a few characters: 950, 1.2K, 41.5K, 3.4M, 1.2B (one decimal, a trailing .0 dropped)."""
    if n is None or n < 0:
        return ""
    if n < 1000:
        return str(int(n))
    value, unit = float(n), ""
    for step in ("K", "M", "B"):
        value /= 1000
        unit = step
        if round(value, 1) < 1000 or step == "B":
            break
    text = f"{value:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + unit


def short_date(epoch: int | None) -> str:
    """'8 Mar 2025' (UTC, English month abbreviations, so it reads the same everywhere); '' when there is no date."""
    if not epoch:
        return ""
    try:
        d = datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _count(n: int | None, singular: str, plural: str | None = None) -> str:
    if n is None:
        return ""
    return f"{compact_number(n)} {singular if n == 1 else (plural or singular + 's')}"


def rating_word(summary: dict) -> str:
    return RATING_WORDS.get(summary.get("rating"), "")


def row_stats(summary: dict) -> str:
    """The compact stats a result row shows after its marker: '41.5K downloads  ·  120 likes  ·  updated 8 Mar 2025'."""
    updated = short_date(summary.get("updated"))
    downloads = None if _uncounted(summary) else summary.get("downloads")
    parts = [_count(downloads, "download"), _count(_likes(summary), "like"), f"updated {updated}" if updated else ""]
    return "  ·  ".join(p for p in parts if p)


def _uncounted(summary: dict) -> bool:
    """A store Creation's downloads are not counted in the catalogue (it says 0), so 0 is left out rather than shown."""
    return bool(summary.get("in_creation_club")) and not summary.get("downloads") and not summary.get("downloads_pc")


def _likes(summary: dict) -> int | None:
    """The likes count, except a 0 on a store Creation (Bethesda's own store items often carry no counts at all)."""
    likes = summary.get("likes")
    return None if summary.get("in_creation_club") and not likes else likes


def detail_stats(summary: dict) -> str:
    """The numbers line of the details panel: downloads (once when the PC and all-platform figures are the same, else both),
    likes and a rating word."""
    pc, every = summary.get("downloads_pc"), summary.get("downloads_all")
    if _uncounted(summary):
        downloads = []
    elif pc is None or pc == every:
        downloads = [_count(every if every is not None else pc, "download")]
    else:
        downloads = [_count(pc, "PC download"), _count(every, "download on all platforms", "downloads on all platforms")]
    parts = [*downloads, _count(_likes(summary), "like"), rating_word(summary)]
    return "  ·  ".join(p for p in parts if p)


def detail_dates(summary: dict) -> str:
    """Published and updated dates and the categories, for the details panel."""
    published, updated = short_date(summary.get("published")), short_date(summary.get("updated"))
    cats = summary.get("categories") or []
    parts = [f"Published {published}" if published else "", f"Updated {updated}" if updated else "",
             "Categories: " + ", ".join(cats) if cats else ""]
    return "  ·  ".join(p for p in parts if p)


def filter_summary(pc_only: bool, free_only: bool, category: str | None = None, author: str | None = None,
                   verified: bool = False, official: bool = False) -> str:
    """'PC, free, Armor, by Someone, verified' for the status line ('' when no filter is on)."""
    parts = ["PC" if pc_only else "", "free" if free_only else "", category or "", f"by {author}" if author else "",
             "verified" if verified else "", "official" if official else ""]
    return ", ".join(p for p in parts if p)


def badges(summary: dict) -> str:
    """'✓ Verified', '✓ Official', both or '' for the creator marks a Creation's author has (shown after the author's name)."""
    return " ".join(b for b, on in ((VERIFIED_BADGE, summary.get("author_verified")), (OFFICIAL_BADGE, summary.get("author_official")))
                    if on)


def author_marks(summary: dict) -> str:
    """The details panel's words after the author's name: ' (verified author)', ' (official)', both or ''."""
    marks = [w for w, on in (("verified author", summary.get("author_verified")), ("published by Bethesda", summary.get("author_official")))
             if on]
    return f" ({', '.join(marks)})" if marks else ""


def loading_text(sort: str, done: int = 0, total: int = 0) -> str:
    """The status line while a browse collects its Creations: 'Loading the most downloaded… (3 of 10)'."""
    what = f"the {SORT_LABELS[sort].lower()}" if sort in core.LOCAL_SORTS else "Creations"
    return f"Loading {what}…" + (f" ({done} of {total})" if total else "")


def collected_head(sort: str, count: int, capped: bool) -> str:
    """The start of the status line for a browse that was collected and is ordered here (see ``loading_text``)."""
    label = SORT_LABELS[sort]
    if sort in core.LOCAL_SORTS:
        return f"{label} among Bethesda's {count:,} most popular"
    return f"{label}, among the first {count:,} Creations Bethesda lists" if capped else label


# ---------------------------------------------------------------- the In my game view

def title_matches(title, words: str) -> bool:
    """True when every word of ``words`` (split on spaces, any case) is in ``title``; no words match everything."""
    low = str(title or "").casefold()
    return all(w in low for w in words.casefold().split())


def owned_loading_text(done: int = 0, total: int = 0) -> str:
    """The status line while the Creations in the game's list are looked up: 'Looking up the Creations in your game… (1 of 3)'."""
    return "Looking up the Creations in your game…" + (f" ({done} of {total})" if total > 1 else "")


def owned_count_text(count: int, listed: int | None = None, capped_at: int | None = None) -> str:
    """'12 Creations in your game.' With ``capped_at`` (only that many of the ``listed`` Creations in the game's list were looked
    up) it also says so."""
    text = f"{count:,} Creation{'s' if count != 1 else ''} in your game."
    if capped_at is not None and listed is not None and listed > capped_at:
        text += f" Your game lists {listed:,}; only the first {capped_at:,} are looked up."
    return text
