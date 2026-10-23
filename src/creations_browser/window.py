"""The Creations Browser window (PyQt6, as bundled with Mod Organizer 2 2.5.x, Qt 6.7).

Nothing here talks to Mod Organizer: the plugin hands in the instance's game and Downloads folder. All network work runs
on a small thread pool (never the UI thread) and starts only when the window is first shown (it opens on a list), or the user
searches, changes the game, a filter or the sort, selects a Creation or downloads. The one local read is the game's own list
of Creations it downloaded (``ContentCatalog.txt``), which marks those Creations "In your game" and feeds the In my game view.
Colours come from the window's theme (``theme.py``: sayngwyn's own colours by default, or MO2's own theme); sizes
come from the font, so it follows the display scaling.
"""

from __future__ import annotations

import functools
import logging
import threading
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QBuffer, QByteArray, QObject, QRunnable, QSize, Qt, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QImage, QImageReader, QPalette, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFrame, QGraphicsDropShadowEffect, QGraphicsOpacityEffect,
                             QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
                             QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter, QStackedWidget,
                             QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTabWidget, QVBoxLayout, QWidget)

from . import core, texts, theme
from .theme import contrast_ratio

log = logging.getLogger("creations_browser")

BROWSE_PAGE = 50  # Creations per page when browsing (no search words) in a server sort, and per "Show more"
SHOW_STEP = 50  # rows shown at first, and added by each "Show more", when a result set is sorted here (text search, collected browse)
CACHE_LIMIT = 16  # collected browses kept for the window's lifetime (a few hundred KB each); the oldest goes first
LOCAL_MODES = ("text", "collect", "owned")  # result modes whose rows are all here: the window orders, filters and reveals them itself
PREVIEW_MAX_PIXELS = 16_000_000  # refuse to decode anything larger than this (a decompression-bomb guard: at most ~64 MB)
PAGE_PREFIX = "https://creations.bethesda.net/"
ROW_DATA = Qt.ItemDataRole.UserRole
NUDGE_DELAY_MS = 1500  # after a download, when to make MO2 re-read its Downloads folder (refresh_mo2_downloads)
MO2_REFRESH_BUTTON = "btnRefreshDownloads"  # the Refresh button of MO2's Downloads tab (mainwindow.ui, MO2 2.5.2)


def refresh_mo2_downloads(main_window: QWidget | None, downloads: Path) -> None:
    """Make MO2 re-read its Downloads folder so a new download is listed. MO2 watches the folder, but it drops changes that
    arrive while it is refreshing, and in the first session of a new instance it watches a folder that did not exist yet
    (it sets the watch before it creates its folders), so a new zip can stay unlisted. Its own Downloads-tab Refresh button
    re-reads the folder; where that button is not found (another MO2 version), an empty marker file nudges the watcher.
    Never raises."""
    button = None
    try:
        if main_window is not None:
            button = main_window.findChild(QPushButton, MO2_REFRESH_BUTTON)
        if button is not None and button.isEnabled():
            button.click()
            return
    except RuntimeError:  # MO2's window is gone (shutting down)
        return
    core.nudge_downloads_watcher(downloads)


# ---------------------------------------------------------------- background work

class _Signals(QObject):
    """One per window; jobs on other threads emit through it and Qt queues each call onto the UI thread."""
    done = pyqtSignal(int, str, object)  # token, kind, result
    failed = pyqtSignal(int, str, str, str)  # token, kind, CreationsError status ("INTERNAL" for anything else), message
    progress = pyqtSignal(int, object, object)  # token, bytes done, bytes total or None
    collecting = pyqtSignal(int, int, int)  # token, pages read, pages expected (a browse collecting its Creations)


class _Job(QRunnable):
    def __init__(self, signals: _Signals, token: int, kind: str, fn: Callable[[], object]):
        super().__init__()
        self._signals, self._token, self._kind, self._fn = signals, token, kind, fn

    def run(self):
        try:
            result = self._fn()
        except core.CreationsError as exc:
            self._emit(self._signals.failed, self._token, self._kind, exc.status, str(exc))
        except Exception as exc:  # a bug must show up as a message, not as a dead window
            core.log_unexpected(f"{self._kind} failed unexpectedly: {type(exc).__name__}: {exc}")
            self._emit(self._signals.failed, self._token, self._kind, "INTERNAL", type(exc).__name__)
        else:
            self._emit(self._signals.done, self._token, self._kind, result)

    @staticmethod
    def _emit(signal, *args):
        try:
            signal.emit(*args)
        except RuntimeError:  # the window is already gone (the application is closing)
            pass


def _states(downloads: Path | None, game: str, latest: dict) -> dict:
    return core.local_statuses(downloads, game, latest) if downloads is not None else {}


def _row_states(rows: list[dict], downloads: Path | None, game: str) -> dict:
    latest = {core.numeric_id(r["content_id"]): r["version"] for r in rows if core.numeric_id(r["content_id"]) is not None}
    return _states(downloads, game, latest)


def _filters(spec: dict) -> dict:
    return {"platforms": "WINDOWS" if spec["pc"] else None, "marketplace": False if spec["free"] else None,
            "category": spec["category"], "author": spec["author"]}


def _server_order(spec: dict) -> tuple:
    """(sort, order, time_period) a browse in this sort reads in: Bethesda's own for a server sort, and for Most downloaded and
    Most liked (which the server cannot sort) its popularity order, which the window then sorts itself."""
    if spec["sort"] in core.LOCAL_SORTS:
        return core.POPULARITY_ORDER
    return core.SERVER_SORTS.get(spec["sort"], core.SERVER_SORTS["popular"])


def _server_sort(spec: dict) -> dict:
    sort, order, period = _server_order(spec)
    return {"sort": sort, "order": order, "time_period": period}


def browse_collects(spec: dict) -> bool:
    """True when a browse is collected (up to ``BROWSE_CAP`` Creations, then ordered and filtered here) rather than read a
    page at a time: Most downloaded and Most liked have no server sort, and the creator filters are not server filters."""
    return spec["sort"] in core.LOCAL_SORTS or bool(spec.get("verified") or spec.get("official"))


def collect_key(spec: dict) -> tuple:
    """What a collected browse is cached under: the game, the PC and free filters, the category and the author, and the order
    it was read in (Bethesda's popularity order for every locally sorted choice, so Most downloaded and Most liked share one)."""
    return (spec["game"], spec["pc"], spec["free"], spec["category"], spec["author"], _server_order(spec))


def _replaced_search(spec: dict) -> dict:
    """What a search that was replaced before it asked anything returns; the window drops it, so it only has to be well formed."""
    return {"spec": spec, "mode": "text" if spec["text"] else "browse", "rows": [], "total": 0, "capped": False, "direct": None,
            "states": {}, "key": None}


def _owned_key(game: str, refs: list) -> tuple:
    """What the In my game view is cached under: the game and the exact list of Creations its own list names."""
    return ("owned", game, tuple(refs))


def _remember(cache: dict | None, key: tuple, got: dict) -> None:
    if cache is not None:
        cache[key] = got
        while len(cache) > CACHE_LIMIT:
            cache.pop(next(iter(cache)), None)


def _owned_job(spec: dict, refs: list, downloads: Path | None, is_current: Callable[[], bool], cache: dict | None,
               progress: Callable[[int, int], None] | None) -> dict:
    """The Creations the game's own list names (``refs``: numeric ids and UUIDs), looked up ``MAX_CONTENT_IDS`` at a time (at
    most three requests, kept in ``cache`` so showing the view again makes none). No references means no request. A lookup that
    was replaced while it waited in the queue makes no request at all."""
    game, key = spec["game"], _owned_key(spec["game"], refs)
    got = cache.get(key) if cache is not None else None
    if got is None:
        if not refs:
            got = {"rows": [], "total": 0, "capped": False}
        else:
            if not is_current():
                return _replaced_search(spec)
            got = core.lookup_ids(game, refs, cancel=lambda: not is_current(), progress=progress)
            _remember(cache, key, got)
    return {"spec": spec, "mode": "owned", "rows": got["rows"], "total": got["total"], "capped": got["capped"], "direct": None,
            "states": _row_states(got["rows"], downloads, game), "key": key}


def _search_job(spec: dict, downloads: Path | None, is_current: Callable[[], bool], cache: dict | None = None,
                progress: Callable[[int, int], None] | None = None, refs: list | None = None) -> dict:
    """What a search needs. Search words: one Creation when they are an id, a UUID or a creations.bethesda.net link; else
    every match up to the cap, which the window sorts itself. No words: a page of the catalogue sorted by Bethesda, or, for
    Most downloaded, Most liked and the creator filters, the first thousand Creations (read once and kept in ``cache``; a
    key seen before makes no request). A search that was replaced while it waited in the queue makes no request at all.
    With ``spec["owned"]`` (the In my game view) it is the Creations in ``refs`` instead (see ``_owned_job``)."""
    if spec.get("owned"):
        return _owned_job(spec, refs or [], downloads, is_current, cache, progress)
    game, text = spec["game"], spec["text"]
    if text and texts.looks_like_reference(text):
        if not is_current():
            return _replaced_search(spec)
        try:
            one = core.lookup_detail(game, text)
        except core.CreationsError as exc:
            if not (exc.status == "NOT_FOUND" and core.numeric_id(text) is not None):
                raise  # a number that is no Creation may still be a word to search for (a title like "2077")
        else:
            cid = core.numeric_id(one["content_id"])
            state, required = _detail_states(game, cid, one, downloads)
            return {"spec": spec, "mode": "text", "rows": [one], "total": 1, "capped": False, "direct": one,
                    "states": {cid: state} if state is not None else {}, "required": required, "key": None}
    if text:
        got = core.lookup_collect(game, text, cancel=lambda: not is_current(), **_filters(spec))
        return {"spec": spec, "mode": "text", "rows": got["rows"], "total": got["total"], "capped": got["capped"], "direct": None,
                "states": _row_states(got["rows"], downloads, game), "key": None}
    if browse_collects(spec):
        key = collect_key(spec)
        got = cache.get(key) if cache is not None else None
        if got is None:
            if not is_current():
                return _replaced_search(spec)
            sort, order, period = _server_order(spec)
            got = core.lookup_browse_collect(game, sort=sort, order=order, time_period=period, cancel=lambda: not is_current(),
                                             progress=progress, **_filters(spec))
            _remember(cache, key, got)
        return {"spec": spec, "mode": "collect", "rows": got["rows"], "total": got["total"], "capped": got["capped"],
                "direct": None, "states": _row_states(got["rows"], downloads, game), "key": key}
    if not is_current():
        return _replaced_search(spec)
    got = core.lookup_page(game, None, page=1, size=BROWSE_PAGE, **_server_sort(spec), **_filters(spec))
    return {"spec": spec, "mode": "browse", "rows": got["rows"], "total": got["total"], "capped": False, "direct": None,
            "states": _row_states(got["rows"], downloads, game), "key": None}


def _more_job(spec: dict, page: int, downloads: Path | None, is_current: Callable[[], bool]) -> dict:
    """The next page of a browse. A page that was replaced while it waited in the queue makes no request."""
    if not is_current():
        return {"rows": [], "total": 0, "page": page, "states": {}}
    got = core.lookup_page(spec["game"], None, page=page, size=BROWSE_PAGE, **_server_sort(spec), **_filters(spec))
    return {"rows": got["rows"], "total": got["total"], "page": page, "states": _row_states(got["rows"], downloads, spec["game"])}


def _categories_job(game: str) -> dict:
    """The game's category names, or None for them when Bethesda can't be asked (the picker then offers only 'All')."""
    try:
        return {"game": game, "names": core.categories(game)}
    except core.CreationsError as exc:
        core.log_line(logging.INFO, f"categories unavailable: {exc.status} {exc}")
        return {"game": game, "names": None}


def _detail_states(game: str, cid: int, one: dict, downloads: Path | None) -> tuple[dict | None, dict]:
    """(the Creation's own local state, the local states of the Creations it requires) for a looked-up detail."""
    wanted = {cid: one["version"]}
    wanted.update({m["content_id"]: None for m in one["required_mods"] if isinstance(m.get("content_id"), int)})
    states = _states(downloads, game, wanted)
    return states.get(cid), {k: v for k, v in states.items() if k != cid}


def _detail_job(game: str, cid: int, downloads: Path | None) -> dict:
    one = core.lookup_detail(game, cid)
    state, required = _detail_states(game, cid, one, downloads)
    return {"detail": one, "state": state, "required": required}


def _preview_job(detail: dict, box: QSize, is_current: Callable[[], bool]) -> QImage | None:
    """The preview as a decoded image no larger than ``box``, or None. Decoding happens here, off the UI thread, with a
    pixel cap, straight to the display size, and not at all once the user has moved on to another Creation."""
    if not is_current():
        return None
    data = core.preview_image_bytes(detail)
    if data is None or not is_current():  # the user moved on while the picture downloaded: do not spend time decoding it
        return None
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QBuffer.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    size = reader.size()  # from the header: nothing is allocated yet
    if not size.isValid() or size.width() * size.height() > PREVIEW_MAX_PIXELS:
        return None
    if box.isValid() and (size.width() > box.width() or size.height() > box.height()):
        reader.setScaledSize(size.scaled(box, Qt.AspectRatioMode.KeepAspectRatio))
    image = reader.read()
    return None if image.isNull() else image


def _download_job(game: str, cid: int, downloads: Path, progress: Callable, cancel: Callable[[], bool]) -> dict:
    plan = core.plan(game, cid)
    path, _, reused = core.fetch(downloads, plan, progress=progress, cancel=cancel)
    return {"content_id": cid, "title": plan.title, "version": plan.version, "name": path.name, "reused": reused,
            "state": core.local_status(downloads, game, cid, plan.version)}


# ---------------------------------------------------------------- the result rows

MIN_TEXT_CONTRAST = 3.0  # below this contrast ratio the palette's selected-row text is not used (WCAG large-text level)


def colour_group(state: QStyle.StateFlag) -> QPalette.ColorGroup:
    """The palette group a style draws an item in, chosen as QCommonStyle does: Disabled when the item is not enabled,
    Inactive when its window is not the active one, otherwise Active."""
    if not state & QStyle.StateFlag.State_Enabled:
        return QPalette.ColorGroup.Disabled
    if not state & QStyle.StateFlag.State_Active:
        return QPalette.ColorGroup.Inactive
    return QPalette.ColorGroup.Active


def row_text_colour(palette: QPalette, state: QStyle.StateFlag) -> QColor:
    """The colour of a result row's text. A selected row uses the palette's HighlightedText for the row's colour group, but
    where that group's HighlightedText is hard to read on its Highlight (on Windows an inactive selection is pale grey under
    white text) the better of Text and WindowText is used instead. Either way it is lifted to 4.5:1 on the row's background,
    keeping its hue, so a theme ("Match MO2" can be any MO2 theme) can't leave the rows unreadable."""
    group = colour_group(state)
    role = QPalette.ColorRole
    if not state & QStyle.StateFlag.State_Selected:
        backs = [palette.color(group, r) for r in (role.Base, role.AlternateBase)]
        return theme.readable(palette.color(group, role.Text), backs)
    back, colour = palette.color(group, role.Highlight), palette.color(group, role.HighlightedText)
    if group != QPalette.ColorGroup.Active and contrast_ratio(colour, back) < MIN_TEXT_CONTRAST:
        colour = max((palette.color(group, r) for r in (role.Text, role.WindowText)), key=lambda c: contrast_ratio(c, back))
    return theme.readable(colour, [back])


class _RowDelegate(QStyledItemDelegate):
    """Title in bold; under it the Free / Get it in game marker in bold, then 'by author' (with a '✓ Verified' / '✓ Official'
    mark when the author has one), the local state and a few stats. In a theme of ours the text takes that theme's colour
    (Qt keeps a style sheet's palette from before a theme switch, so the list's palette can't be trusted for it); under
    "Match MO2" it takes the palette's, lifted to 4.5:1."""

    def __init__(self, parent, scheme: Callable[[], theme.Scheme | None]):
        super().__init__(parent)
        self._scheme = scheme

    def _lines(self, index) -> tuple[str, str, str]:
        """(title, marker, the rest of the second line: 'by author  ·  state  ·  stats')."""
        data = index.data(ROW_DATA) or {}
        by = f"by {data['author']}" + (f" {data['badges']}" if data.get("badges") else "") if data.get("author") else ""
        parts = [by, data.get("state", ""), data.get("stats", "")]
        return data.get("title", ""), data.get("marker", ""), "  ·  ".join(p for p in parts if p)

    def sizeHint(self, option, index):
        fm = option.fontMetrics
        return QSize(fm.height() * 12, fm.height() * 2 + fm.height() // 2 + fm.height() // 3)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget
        (widget.style() if widget else QApplication.style()).drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        title, marker, rest = self._lines(index)
        scheme = self._scheme()
        colour = QColor(scheme.text) if scheme is not None else row_text_colour(opt.palette, opt.state)
        pad = opt.fontMetrics.height() // 4
        rect = opt.rect.adjusted(pad * 2, pad, -pad * 2, -pad)
        painter.save()
        painter.setPen(colour)
        bold = QFont(opt.font)
        bold.setBold(True)
        painter.setFont(bold)
        fm = painter.fontMetrics()
        painter.drawText(rect.left(), rect.top() + fm.ascent(), fm.elidedText(title, Qt.TextElideMode.ElideRight, rect.width()))
        baseline = rect.bottom() - fm.descent()
        shown = fm.elidedText(marker, Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect.left(), baseline, shown)
        left = rect.left() + fm.horizontalAdvance(shown)
        painter.setFont(opt.font)
        fm = painter.fontMetrics()
        tail = ("  ·  " if marker and rest else "") + rest
        painter.drawText(left, baseline, fm.elidedText(tail, Qt.TextElideMode.ElideRight, max(rect.right() - left, 0)))
        painter.restore()


class _LazyCombo(QComboBox):
    """A combo box that says so just before its list opens (the category list is fetched then)."""
    opening = pyqtSignal()

    def showPopup(self):
        self.opening.emit()
        super().showPopup()


# ---------------------------------------------------------------- the window

class CreationsWindow(QDialog):
    """Search, read and download free Creations. A QDialog (shown non-modal, as an ordinary resizable window) because MO2's
    stylesheets style ``QMainWindow, QDialog``: a plain top-level QWidget gets no themed background, which left light or
    dark text on the wrong background under a dark theme. ``instance_game`` is the game key ('fallout4', 'skyrimspecialedition') the
    Mod Organizer 2 instance manages, or None; ``downloads_dir`` is that instance's Downloads folder; ``catalog_base`` is the
    folder holding the games' own lists of downloaded Creations (``%LOCALAPPDATA%`` when None; tests pass a temporary one)."""

    DETAIL_DELAY_MS = 250  # a pause before a selected Creation is fetched, so arrowing down the list is not a burst of requests
    SEARCH_DELAY_MS = 300  # a pause after a filter or sort change before it searches, so scrolling a combo is one request
    AUTO_BROWSE = True  # the first time the window is shown it browses at once (tests switch it off)

    def __init__(self, parent: QWidget | None = None, instance_game: str | None = None, downloads_dir: str | Path | None = None,
                 catalog_base: str | Path | None = None, theme_mode: str = theme.DEFAULT_MODE,
                 on_theme_change: Callable[[str], None] | None = None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint)
        self.setModal(False)
        self.setWindowTitle("Creations Browser for Mod Organizer 2")
        self.setObjectName(theme.OBJECT_NAME)  # scopes the sayngwyn style sheet to this window
        self._on_theme_change = on_theme_change  # the plugin saves the choice (MO2's plugin settings)
        self._theme_mode: str | None = None
        self._context_menu: QMenu | None = None  # the open right-click menu, kept alive while it shows
        self._screen_hooked = False
        self._instance: str | None = None
        self._downloads: Path | None = None
        self.catalog_base = catalog_base  # where the games' own Creations lists are looked for (None: %LOCALAPPDATA%)
        self._catalog: dict | None = None  # the picked game's own list of Creations (core.game_catalog), read when first needed
        self._loading_owned = False  # the search on its way is the In my game view
        self._configured = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._signals = _Signals(self)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._signals.progress.connect(self._on_progress)
        self._signals.collecting.connect(self._on_collecting)
        self._tokens: dict[str, int] = {}
        self._pending = 0
        self._search_key: tuple | None = None  # the request of the search still on its way
        self._searching = False
        self._searched = False  # a search has been run since the window opened or the game changed: filter changes re-run it
        self._mode = "browse"  # "browse" (no search words) or "text": what the sort choices offer
        self._loading_sort = "downloads"  # the sort of the search on its way (named in the status line while it collects)
        self._sort_touched = False  # the user picked a sort (so a change between browsing and searching keeps it)
        self._collect_cache: dict[tuple, dict] = {}  # collected browses (see ``collect_key``), kept while the window lives
        self._result_mode: str | None = None  # the kind of search the rows on show came from: "browse", "collect" or "text"
        self._result_key: tuple | None = None  # the cache key of the rows on show when they are a collected browse
        self._spec: dict | None = None  # the search the rows on show came from
        self._results: list[dict] = []  # text/collect: every row kept, in Bethesda's order; browse: the pages so far
        self._total = 0
        self._capped = False
        self._direct = False
        self._shown = 0  # text/collect: how many of the sorted, filtered results are in the list
        self._switched_to: str | None = None  # the game a pasted link switched the picker to (named in the status line)
        self._page = 1  # browse: the last page fetched
        self._more_busy = False
        self._author: str | None = None
        self._categories: dict[str, list[str]] = {}
        self._categories_state: dict[str, str] = {}  # game -> "loading" | "done" | "failed" (asked once per game)
        self._rows: dict[int, QListWidgetItem] = {}
        self._states: dict[int, dict] = {}
        self._detail: dict | None = None
        self._required_states: dict[int, dict] = {}
        self._queue: list[tuple[int, str]] = []
        self._current: tuple[int, str] | None = None
        self._cancel = threading.Event()
        self._batch = {"total": 0, "saved": 0, "reused": 0}
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.timeout.connect(self._fetch_selected_detail)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self._launch)
        self._build()
        for button in self.findChildren(QPushButton):  # a dialog would press its default button on Enter; Enter searches here
            button.setAutoDefault(False)
            button.setDefault(False)
        self.apply_theme(theme_mode, save=False)
        self.configure(instance_game, downloads_dir)
        QApplication.instance().aboutToQuit.connect(self.shutdown)

    # ---- construction

    SCREEN_MARGIN = 40  # pixels kept free around the window on its screen

    def _fitted(self, size: QSize) -> QSize:
        """``size`` cut down, where needed, to fit the available area of the screen the window opens on (the parent's screen
        when it has a parent) less a margin; never below the window's minimum size."""
        parent = self.parentWidget()
        screen = (parent.screen() if parent is not None else None) or self.screen() or QApplication.primaryScreen()
        if screen is None:
            return size
        room = screen.availableGeometry()
        return QSize(max(min(size.width(), room.width() - self.SCREEN_MARGIN), self.minimumWidth()),
                     max(min(size.height(), room.height() - self.SCREEN_MARGIN), self.minimumHeight()))

    def _build(self):
        fm = self.fontMetrics()
        em = fm.height()
        self.setMinimumSize(em * 44, em * 30)  # the list and the details each scroll (the description in its own tab)
        self.resize(self._fitted(QSize(em * 64, em * 42)))

        self._game = QComboBox()
        for key, label in texts.GAME_LABELS.items():
            self._game.addItem(label, key)
        self._game.currentIndexChanged.connect(self._on_game_changed)
        game_label = QLabel("&Game:")
        game_label.setBuddy(self._game)
        self._query = QLineEdit()
        self._query.setPlaceholderText(texts.SEARCH_PLACEHOLDER)
        self._query.setClearButtonEnabled(True)
        self._query.setAccessibleName("Search Creations")
        self._query.returnPressed.connect(self.start_search)
        self._theme_context_menu(self._query)
        self._search_button = QPushButton("&Browse")  # says Search once there are words to search for (see _on_query_edited)
        self._search_button.clicked.connect(self.start_search)
        self._query.textChanged.connect(self._on_query_edited)
        top = QHBoxLayout()
        top.addWidget(game_label)
        top.addWidget(self._game)
        top.addWidget(self._query, 1)
        top.addWidget(self._search_button)

        self._pc_only = QCheckBox("PC &versions only")
        self._pc_only.setChecked(True)
        self._pc_only.setToolTip("Leave out Creations that are only for consoles")
        self._free_only = QCheckBox("&Free only")
        self._free_only.setChecked(True)
        self._free_only.setToolTip("Leave out Creations from Bethesda's store")
        self._verified_only = QCheckBox(texts.VERIFIED_LABEL)  # a local filter: Bethesda has no such filter to ask
        self._verified_only.setToolTip(texts.VERIFIED_TIP)
        self._official_only = QCheckBox(texts.OFFICIAL_LABEL)
        self._official_only.setToolTip(texts.OFFICIAL_TIP)
        self._owned_only = QCheckBox(texts.OWNED_LABEL)  # shows exactly the Creations the game's own list names
        self._owned_only.setToolTip(texts.OWNED_TIP)
        self._category = _LazyCombo()
        self._category.setAccessibleName("Category")
        self._category.setMinimumContentsLength(12)
        self._category.setToolTip("Only Creations in this category")
        self._category.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._category.addItem(texts.ALL_CATEGORIES, None)
        filters = QHBoxLayout()
        filters.addWidget(self._pc_only)
        filters.addWidget(self._free_only)
        filters.addWidget(self._category)
        filters.addWidget(self._owned_only)
        filters.addStretch(1)

        self._sort = QComboBox()
        self._sort.setAccessibleName("Sort by")
        for key in core.SORT_MODES:
            self._sort.addItem(texts.SORT_LABELS[key], key)
        self._sort.setCurrentIndex(self._sort.findData("downloads"))
        sort_label = QLabel("S&ort by:")
        sort_label.setBuddy(self._sort)
        self._author_label = QLabel()
        self._author_label.setWordWrap(True)
        self._author_label.setTextFormat(Qt.TextFormat.PlainText)
        self._author_label.setAccessibleName("Author filter")
        self._author_clear = QPushButton("Show all authors")
        self._author_clear.clicked.connect(self.clear_author)
        order = QHBoxLayout()
        order.addWidget(sort_label)
        order.addWidget(self._sort)
        order.addSpacing(em)
        order.addWidget(self._verified_only)  # the creator filters sit with the other list controls, on the roomier row
        order.addWidget(self._official_only)
        order.addStretch(1)
        self._theme = QComboBox()
        self._theme.setAccessibleName("Window colours")
        self._theme.setToolTip(texts.THEME_TIP)
        for key in theme.MODES:
            self._theme.addItem(theme.MODE_LABELS[key], key)
        theme_label = QLabel("&Theme:")
        theme_label.setBuddy(self._theme)
        order.addWidget(theme_label)
        order.addWidget(self._theme)
        by_author = QHBoxLayout()
        by_author.addWidget(self._author_label, 1)
        by_author.addWidget(self._author_clear)
        self._pc_only.toggled.connect(self._on_filter_changed)
        self._free_only.toggled.connect(self._on_filter_changed)
        self._verified_only.toggled.connect(self._on_local_filter_changed)
        self._official_only.toggled.connect(self._on_local_filter_changed)
        self._category.currentIndexChanged.connect(self._on_filter_changed)
        self._category.opening.connect(self._ensure_categories)
        self._owned_only.toggled.connect(self._on_owned_toggled)
        self._tips = {w: w.toolTip() for w in (self._pc_only, self._free_only, self._verified_only, self._official_only,
                                               self._category)}
        self._sort.currentIndexChanged.connect(self._on_sort_changed)
        self._theme.currentIndexChanged.connect(lambda _i: self.apply_theme(self._theme.currentData()))
        self._sync_sort_controls()
        self._show_author(None)

        self._list = QListWidget()
        self._list.setAccessibleName("Search results")
        self._list.setItemDelegate(_RowDelegate(self._list, lambda: theme.SCHEMES.get(self.theme_mode())))
        self._list.setUniformItemSizes(True)
        self._list.currentItemChanged.connect(self._on_row_changed)
        self._list.setMinimumWidth(em * 14)
        self._stack_min = em * 26

        self._stack = QStackedWidget()
        self._message = QLabel()
        self._message.setWordWrap(True)
        self._message.setTextFormat(Qt.TextFormat.PlainText)
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message_rest = True  # the message is a calm one (a hint, loading, nothing found), not a problem
        self._mark = QLabel()  # the sayngwyn mark, faded, above calm messages (sayngwyn theme only)
        self._mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._mark.hide()
        empty = QWidget()
        empty.setObjectName("emptyState")
        empty_column = QVBoxLayout(empty)
        empty_column.addStretch(1)
        empty_column.addWidget(self._mark)
        empty_column.addSpacing(em // 2)
        empty_column.addWidget(self._message)
        empty_column.addStretch(1)
        self._stack.setMinimumWidth(self._stack_min)
        self._stack.addWidget(empty)
        self._details_scroll = QScrollArea()  # at the smallest window the facts scroll instead of squeezing the rows around them
        self._details_scroll.setWidgetResizable(True)
        self._details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._details_scroll.setWidget(self._build_detail_page(em))
        self._stack.addWidget(self._details_scroll)
        self._show_message(texts.BROWSE_HINT)

        self._more_button = QPushButton("Show &more")
        self._more_button.clicked.connect(self.show_more)
        self._more_button.hide()
        left = QWidget()
        left_column = QVBoxLayout(left)
        left_column.setContentsMargins(0, 0, 0, 0)
        left_column.addWidget(self._list, 1)
        left_column.addWidget(self._more_button)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(left)
        splitter.addWidget(self._stack)
        splitter.setStretchFactor(0, 1)  # the rows' stats line (downloads, likes, date) needs about half the width
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([em * 28, em * 32])

        self._download_button = QPushButton("&Download")
        self._download_button.setProperty("primary", True)  # the window's one main action: red in the sayngwyn theme
        self._download_button.clicked.connect(self.download_selected)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)  # a percentage only while there is a download
        self._progress.setAccessibleName("Download progress")
        self._cancel_button = QPushButton("&Cancel")
        self._cancel_button.clicked.connect(self.cancel_download)
        for w in (self._progress, self._cancel_button):  # shown only while a download runs; the row keeps its height
            policy = w.sizePolicy()
            policy.setRetainSizeWhenHidden(True)
            w.setSizePolicy(policy)
            w.hide()
        buttons = QHBoxLayout()
        buttons.addWidget(self._download_button)
        buttons.addWidget(self._progress, 1)
        buttons.addWidget(self._cancel_button)

        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setTextFormat(Qt.TextFormat.PlainText)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._status.setAccessibleName("Status")
        self._status_sign = QLabel(theme.WARNING_SIGN)  # shown beside the status line only when it reports a problem
        self._status_sign.setProperty("tone", "error")
        self._status_sign.setAccessibleName("Problem")
        self._status_sign.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._status_sign.hide()
        status_row = QHBoxLayout()
        status_row.addWidget(self._status_sign)
        status_row.addWidget(self._status, 1)
        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFrameShadow(QFrame.Shadow.Sunken)
        self._notice = QLabel(texts.NOTICE)
        self._notice.setObjectName("notice")
        self._notice.setWordWrap(True)
        self._notice.setTextFormat(Qt.TextFormat.PlainText)
        self._notice.setAccessibleName("Notice")

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(filters)
        layout.addLayout(order)
        layout.addLayout(by_author)
        layout.addWidget(splitter, 1)
        layout.addLayout(buttons)
        layout.addWidget(self._note)
        layout.addLayout(status_row)
        layout.addWidget(rule)
        layout.addWidget(self._notice)

        self.setTabOrder(self._game, self._query)
        self.setTabOrder(self._query, self._search_button)
        self.setTabOrder(self._search_button, self._pc_only)
        self.setTabOrder(self._pc_only, self._free_only)
        self.setTabOrder(self._free_only, self._category)
        self.setTabOrder(self._category, self._owned_only)
        self.setTabOrder(self._owned_only, self._sort)
        self.setTabOrder(self._sort, self._verified_only)
        self.setTabOrder(self._verified_only, self._official_only)
        self.setTabOrder(self._official_only, self._theme)
        self.setTabOrder(self._theme, self._author_clear)
        self.setTabOrder(self._author_clear, self._list)
        self.setTabOrder(self._list, self._more_button)
        self.setTabOrder(self._more_button, self._req_button)
        self.setTabOrder(self._req_button, self._author_button)
        self.setTabOrder(self._author_button, self._page_button)
        self.setTabOrder(self._page_button, self._tabs)
        self.setTabOrder(self._tabs, self._download_button)
        self.setTabOrder(self._download_button, self._cancel_button)

    def _build_detail_page(self, em: int) -> QWidget:
        def label(wrap=True, selectable=True) -> QLabel:
            lab = QLabel()
            lab.setWordWrap(wrap)
            lab.setTextFormat(Qt.TextFormat.PlainText)  # catalogue text is untrusted: never rich text
            if selectable:
                lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            return lab

        self._d_title = label()
        font = QFont(self.font())
        font.setBold(True)
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * 1.3)
        self._d_title.setFont(font)
        self._d_author = label()
        self._d_facts = label()
        self._d_stats = label()
        self._d_dates = label()
        self._d_avail = label()
        self._d_owned = label()
        self._d_local = label()
        self._preview = QLabel()
        self._preview.setAccessibleName("Preview image")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._preview.setFixedSize(em * 12, em * 7)
        self._preview.hide()
        info = QVBoxLayout()
        for w in (self._d_title, self._d_author, self._d_facts, self._d_stats, self._d_dates, self._d_owned, self._d_avail, self._d_local):
            info.addWidget(w)
        info.addStretch(1)
        head = QHBoxLayout()
        head.addWidget(self._preview, 0, Qt.AlignmentFlag.AlignTop)
        head.addLayout(info, 1)

        self._d_dlc = label()
        self._d_required = label()
        self._req_button = QPushButton("Download these too")
        self._req_button.clicked.connect(self.download_required)
        self._author_button = QPushButton("More by this &author")
        self._author_button.clicked.connect(self.more_by_author)
        self._page_button = QPushButton("Open Creations page")
        self._page_button.clicked.connect(self._open_page)
        links = QHBoxLayout()
        links.addWidget(self._req_button)
        links.addWidget(self._author_button)
        links.addWidget(self._page_button)
        links.addStretch(1)

        self._description = QPlainTextEdit()
        self._description.setReadOnly(True)
        self._description.setAccessibleName("Description")
        self._notes = QPlainTextEdit()
        self._notes.setReadOnly(True)
        self._notes.setAccessibleName("Release notes for the PC version")
        for box in (self._description, self._notes):
            self._theme_context_menu(box)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._description, "Description")
        self._tabs.addTab(self._notes, "Release notes")
        self._tabs.setMinimumHeight(em * 8)

        page = QWidget()
        page.setObjectName("detailPage")
        column = QVBoxLayout(page)
        column.setContentsMargins(em // 2, 0, 0, 0)
        column.addLayout(head)
        column.addWidget(self._d_dlc)
        column.addWidget(self._d_required)
        column.addLayout(links)
        column.addWidget(self._tabs, 1)  # the description takes whatever height is left, and scrolls itself
        return page

    # ---- setup from the plugin

    def configure(self, instance_game: str | None, downloads_dir: str | Path | None):
        """The game the MO2 instance manages and its Downloads folder (called again each time the window is opened)."""
        first = not self._configured
        self._configured = True
        self._instance = instance_game if instance_game in core.PRODUCTS else None
        self._downloads = Path(downloads_dir) if downloads_dir else None
        if first and self._instance is not None:  # the first time only: later the user's own pick stands
            self._game.blockSignals(True)
            self._game.setCurrentIndex(max(self._game.findData(self._instance), 0))
            self._game.blockSignals(False)
        if not first:  # reopened: the game may have downloaded more Creations since (a cheap local read)
            self._reload_catalog()
        self._refresh_controls()

    def picked_game(self) -> str:
        return self._game.currentData()

    def busy(self) -> bool:
        """True while any request is in flight or a selected Creation is waiting to be fetched."""
        return self._pending > 0 or self._detail_timer.isActive() or self._search_timer.isActive()

    # ---- helpers

    def _next(self, kind: str) -> int:
        self._tokens[kind] = self._tokens.get(kind, 0) + 1
        return self._tokens[kind]

    def _submit(self, kind: str, fn: Callable[[], object], token: int | None = None) -> int:
        token = self._next(kind) if token is None else token
        self._pending += 1
        self._pool.start(_Job(self._signals, token, kind, fn))
        return token

    def _current_token(self, kind: str, token: int) -> bool:
        return self._tokens.get(kind) == token

    def _local_downloads(self) -> Path | None:
        """The Downloads folder when the picked game is the instance's game (it is the only game that folder belongs to)."""
        return self._downloads if (self._instance is not None and self.picked_game() == self._instance) else None

    def _catalog_now(self) -> dict:
        """The picked game's own list of downloaded Creations: read once (a local file, never changed) and kept until the game
        changes or the window is reopened. A list that can't be read is an empty one."""
        if self._catalog is None:
            try:
                self._catalog = core.game_catalog(self.picked_game(), self.catalog_base)
            except Exception as exc:
                core.log_line(logging.INFO, f"the game's list of Creations could not be read ({type(exc).__name__})")
                self._catalog = {"ids": set(), "uuids": set(), "count": 0}
        return self._catalog

    def _is_owned(self, item: dict | None) -> bool:
        """True when the game's own list names this Creation (a result row or a looked-up detail), by id or by UUID."""
        if not item:
            return False
        catalog = self._catalog_now()
        cid, uid = core.numeric_id(item.get("content_id")), str(item.get("uuid") or "").lower()
        return (cid is not None and cid in catalog["ids"]) or (bool(uid) and uid in catalog["uuids"])

    def _reload_catalog(self):
        """Read the game's list again: the markers on the rows and in the details follow it, and the In my game view is run
        again when its list changed."""
        self._catalog = None
        refs = core.catalog_refs(self._catalog_now())
        for cid in list(self._rows):
            self._update_row(cid)
        self._show_owned_line()
        if (self._owned_only.isChecked() and self._searched and not self._searching and self._result_mode == "owned"
                and self._result_key != _owned_key(self.picked_game(), refs)):
            self._launch()

    def _show_owned_line(self):
        owned = self._is_owned(self._detail)
        self._d_owned.setText(texts.OWNED_DETAIL if owned else "")
        self._d_owned.setVisible(owned)
        if self._detail:
            self._d_avail.setText(texts.availability(self._detail, owned=owned))

    def _set_status(self, text: str, log_it: bool = True, problem: bool = False):
        """Show ``text`` on the status line (and log it, except for the frequent progress updates). A ``problem`` is shown
        in the alarm colour beside a warning sign, so it never reads as an ordinary update."""
        self._status.setText(text)
        self._status.setProperty("tone", "error" if problem else "")
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)
        self._status_sign.setVisible(problem and bool(text))
        if log_it:
            core.log_line(logging.INFO, f"status: {text}")

    # ---- the theme

    def _theme_context_menu(self, box: QLineEdit | QPlainTextEdit):
        """``box``'s right-click menu is Qt's own (Undo, Copy, Paste…), shown by the window so that in a theme of ours its
        icons take the theme's text colour."""
        box.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        box.customContextMenuRequested.connect(lambda pos, b=box: self._show_context_menu(b, pos))

    def _show_context_menu(self, box: QLineEdit | QPlainTextEdit, pos):
        try:
            menu = box.createStandardContextMenu()
            if menu is None:
                return
            scheme = theme.SCHEMES.get(self.theme_mode())  # "Match MO2" leaves the icons as MO2's theme has them
            if scheme is not None:
                style = self.style()
                size = style.pixelMetric(QStyle.PixelMetric.PM_SmallIconSize) if style is not None else 16
                for action in menu.actions():
                    if not action.icon().isNull():
                        action.setIcon(theme.tinted(action.icon(), scheme.text, scheme.muted, size, self.devicePixelRatioF()))
            menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._context_menu = menu  # Qt gives it no parent: without this, Python would delete it before it showed
            origin = box.viewport() if isinstance(box, QPlainTextEdit) else box  # a text box's position is in its viewport
            menu.popup((origin or box).mapToGlobal(pos))
        except Exception as exc:  # a slot: a failure here must not leave anything half-done
            core.log_line(logging.WARNING, f"could not show the right-click menu ({type(exc).__name__})")

    def theme_mode(self) -> str:
        return self._theme_mode or theme.DEFAULT_MODE

    def apply_theme(self, mode: str | None, save: bool = True):
        """Colour the window with ``mode`` ("sayngwyn" or "mo2"; anything else is the default) and, when the user picked it,
        hand it to the plugin to save."""
        mode = mode if mode in theme.MODES else theme.DEFAULT_MODE
        if mode == self._theme_mode:
            return
        self._theme_mode = mode
        # The old sheet goes first: removing a sheet puts back the palette the window had when it was set, which would
        # undo the palette below.
        self.setStyleSheet("")
        if mode in theme.SCHEMES:
            self.setPalette(theme.palette_for(mode, QApplication.palette(self)))
        else:
            self.setPalette(QPalette())  # nothing resolved: the window takes MO2's palette again
        self.setStyleSheet(theme.stylesheet(mode))
        # Qt polishes a window lazily, at its first grab or show, and that first polish would rebuild the palette from the
        # sheet after the one set below; polished now, it has nothing left to do then.
        self.ensurePolished()
        # A widget already polished under the old sheet keeps that sheet's rules (and palette) until it is polished again,
        # which Qt does not do for a window that hasn't been shown yet: a switch then left the list's selection in the old
        # theme's colour. So the window and every widget inside are polished again from the new sheet, and the window then
        # gets its palette back (polishing it rebuilds its palette from the sheet).
        for widget in [self, *self.findChildren(QWidget)]:
            style = widget.style()
            if style is not None:
                style.unpolish(widget)
                style.polish(widget)
        if mode in theme.SCHEMES:
            self.setPalette(theme.palette_for(mode, QApplication.palette(self)))
        viewport = self._list.viewport()
        if viewport is not None:
            viewport.update()  # the rows' painter takes its text colour from the theme too (see _RowDelegate)
        self._dress_message()
        self._theme.blockSignals(True)
        self._theme.setCurrentIndex(max(self._theme.findData(mode), 0))
        self._theme.blockSignals(False)
        if save and self._on_theme_change is not None:
            try:
                self._on_theme_change(mode)
            except Exception as exc:  # the colours still change; only remembering them failed
                core.log_line(logging.WARNING, f"could not save the theme choice ({type(exc).__name__})")

    def _show_message(self, text: str, rest: bool = True):
        """Show ``text`` in the details pane. A calm message (``rest``) gets the brand in the sayngwyn theme; a problem never
        does, so it can't read as decoration."""
        self._message.setText(text)
        self._message_rest = rest
        self._dress_message()
        self._stack.setCurrentIndex(0)

    def _dress_message(self):
        """The empty details pane in the sayngwyn theme: the mark, faded, above the message in Sayngwyn Serif with the red
        offset shadow. Otherwise the plain message in the window's own font."""
        brand = self._theme_mode == "sayngwyn" and self._message_rest
        family = theme.brand_family() if brand else None
        pixmap = theme.mark(self.fontMetrics().height() * 6, self.devicePixelRatioF()) if brand else None
        if pixmap is not None:
            self._mark.setPixmap(pixmap)
            fade = QGraphicsOpacityEffect(self._mark)
            fade.setOpacity(theme.MARK_OPACITY)
            self._mark.setGraphicsEffect(fade)
        self._mark.setVisible(pixmap is not None)
        if family:
            font = QFont(family)
            font.setPixelSize(theme.BRAND_TEXT_PX)
            self._message.setFont(font)
            shadow = QGraphicsDropShadowEffect(self._message)
            shadow.setBlurRadius(0)
            step = theme.BRAND_TEXT_PX * theme.SHADOW_SHARE
            shadow.setOffset(step, step)
            shadow.setColor(QColor(theme.SHADOW))
            self._message.setGraphicsEffect(shadow)
        else:
            self._message.setFont(QFont())  # nothing set: it inherits the window's font again
            self._message.setGraphicsEffect(None)

    def _blocker(self) -> str | None:
        """Why downloading is off for the picked game (None when it is allowed)."""
        if self._instance is None:
            return ("Downloading is off: this Mod Organizer 2 instance doesn't manage Fallout 4 or Skyrim Special Edition. "
                    "You can still browse.")
        if self.picked_game() != self._instance:
            have = texts.GAME_LABELS[self._instance]
            return (f"Downloading is off for {texts.GAME_LABELS[self.picked_game()]}: this Mod Organizer 2 instance manages "
                    f"{have}, and its Downloads folder belongs to that game. Pick {have} above to download.")
        if self._downloads is None:
            return "Downloading is off: Mod Organizer 2 did not give a Downloads folder."
        return None

    def _pending_required(self) -> list[tuple[int, str]]:
        """Required Creations that are free, available and not yet downloaded."""
        if not self._detail:
            return []
        out = []
        for m in self._detail["required_mods"]:
            cid = m.get("content_id")
            if isinstance(cid, int) and m.get("downloadable") and (self._required_states.get(cid) or {}).get("state", "none") == "none":
                out.append((cid, m.get("title") or f"Creation {cid}"))
        return out

    def _refresh_controls(self):
        downloading = self._current is not None
        blocker = self._blocker()
        d = self._detail
        self._note.setText(blocker or "")
        self._note.setVisible(bool(blocker))
        self._download_button.setEnabled(bool(d and d["downloadable"] and blocker is None and not downloading))
        self._req_button.setVisible(bool(blocker is None and self._pending_required()))
        self._req_button.setEnabled(not downloading)
        self._cancel_button.setEnabled(downloading)
        self._progress.setVisible(downloading)
        self._cancel_button.setVisible(downloading)
        self._game.setEnabled(not downloading)

    # ---- searching

    def search(self, text: str):
        """Search programmatically (what the Search button does with the box's text)."""
        self._query.setText(text)
        self.start_search()

    def start_search(self):
        """The Search button: a new search by the box's words (or a browse when the box is empty). It forgets an author pick."""
        self._author = None
        self._show_author(None)
        self._switched_to = None
        self._follow_link_game()
        self._launch()

    def _follow_link_game(self):
        """A pasted creations.bethesda.net link that names the other game switches the Game picker to that game, so the link
        opens (downloading stays tied to the instance's game; see ``_blocker``). Not while a download runs: the game picker is
        locked then, and the rest of a batch is downloaded for the game it started in."""
        game = core.link_game(self._query.text())
        if game is None or game == self.picked_game() or self._current is not None:
            return
        self._game.setCurrentIndex(self._game.findData(game))  # clears the old results (see _on_game_changed)
        self._switched_to = game

    def _on_query_edited(self, text: str):
        """The search button says Browse while the box is empty (it then lists everything) and Search otherwise."""
        self._search_button.setText("&Search" if text.strip() else "&Browse")

    def clear_author(self):
        self._author = None
        self._show_author(None)
        self._launch()

    def more_by_author(self):
        """List the picked Creation's author's Creations (the filters still apply), in place of a search by words."""
        author = (self._detail or {}).get("author")
        if not core.valid_name(author):
            return
        self._owned_only.setChecked(False)  # an author's Creations are not the game's own list
        self._author = author
        self._query.clear()
        self._show_author(author)
        self._launch()

    def _show_author(self, author: str | None):
        self._author_label.setText(f"Author: {author}" if author else "")
        self._author_label.setVisible(bool(author))
        self._author_clear.setVisible(bool(author))

    def _on_filter_changed(self, *_):
        """A filter changed: run the current search again (nothing happens before the first search). The search starts after
        a short pause that each further change restarts, so stepping through a combo box asks Bethesda once. A collected
        browse that was read before is shown again from the cache, without a request."""
        if self._searched:
            self._search_timer.start(self.SEARCH_DELAY_MS)

    def _on_local_filter_changed(self, *_):
        """Verified creators only / Official creations only: Bethesda cannot filter by these, so the rows already here are
        filtered again (no request) when they are all here; otherwise (a browse read a page at a time) the browse is collected."""
        if not self._searched or self._direct:
            return
        if self._result_mode == "text" or self._same_collect():
            self._refilter()
        else:
            self._search_timer.start(self.SEARCH_DELAY_MS)

    def _on_owned_toggled(self, owned: bool):
        """In my game only: the PC, Free, category and creator controls are greyed out while it is on (they don't apply to
        Creations the game already lists), the view runs after the usual pause, and a list seen before comes back from the cache."""
        for w in self._tips:
            w.setEnabled(not owned)
            w.setToolTip(texts.OWNED_GREY_TIP if owned else self._tips[w])
        if owned:
            self._author = None
            self._show_author(None)
        self._sync_sort_controls()
        if self._searched:
            self._search_timer.start(self.SEARCH_DELAY_MS)

    def _on_sort_changed(self, *_):
        self._sort_touched = True
        self._sync_sort_controls()
        if not self._searched:
            return
        if ((self._mode == "text" and self._result_mode == "text") or self._same_collect()
                or (self._mode == "owned" and self._result_mode == "owned")):
            self._refilter()  # the rows already here are sorted again: Bethesda cannot sort these
        else:
            self._search_timer.start(self.SEARCH_DELAY_MS)

    def _sort_key(self) -> str:
        return self._sort.currentData() or "downloads"

    def _same_collect(self) -> bool:
        """True when the rows on show are a collected browse that the controls, as they are now, would show again unchanged
        (apart from their order and the creator filters): same game, filters, author and reading order, and still no words."""
        if self._result_mode != "collect" or self._mode != "browse" or self._result_key is None:
            return False
        spec = self._control_spec()
        return browse_collects(spec) and collect_key(spec) == self._result_key

    def _sync_sort_controls(self):
        """Which sort choices the kind of search offers: Best match only with search words, and Bethesda's popularity sorts
        (trending, month, all time) only without, because Bethesda ignores them when there are words."""
        # The In my game view orders what it has here, so every sort is offered except the per-period ones: Bethesda gives
        # no week's or month's figures per Creation, only its own sorted lists.
        owned = self._owned_only.isChecked()
        text = self._mode == "text" and not owned
        model = self._sort.model()
        for i in range(self._sort.count()):
            key = self._sort.itemData(i)
            if owned:
                ok, tip = key not in core.PERIOD_SORTS, texts.SORT_TIP_PERIOD
            else:
                ok = (text or key not in core.TEXT_ONLY_SORTS) and not (text and key in core.WORDLESS_SORTS)
                tip = texts.SORT_TIP_MATCH if key in core.TEXT_ONLY_SORTS else texts.SORT_TIP_WORDLESS
            model.item(i).setEnabled(ok)
            model.item(i).setToolTip("" if ok else tip)
        if not model.item(self._sort.currentIndex()).isEnabled():
            self._set_sort("match" if text else "downloads")
            self._sort_touched = False  # back on the default for this kind of search

    def _set_sort(self, key: str):
        self._sort.blockSignals(True)
        self._sort.setCurrentIndex(self._sort.findData(key))
        self._sort.blockSignals(False)

    def _launch(self):
        """Start a search from the controls: search words (sorted here), or, with none, a browse (sorted by Bethesda)."""
        self._search_timer.stop()  # a search made now covers any change still waiting out its pause
        text, owned = self._control_spec()["text"], self._owned_only.isChecked()
        mode = "owned" if owned else "text" if text else "browse"
        if mode != self._mode:  # the chosen sort stays unless the new kind of search does not offer it (see _sync_sort_controls)
            self._mode = mode
            if mode == "text" and not self._sort_touched:  # the browse default: words are best sorted by how well they match
                self._set_sort("match")
        self._sync_sort_controls()
        spec = self._control_spec()
        refs = None
        if owned:  # the game's own list is what is shown: only the game matters to the request; words and sort are applied here
            refs = core.catalog_refs(self._catalog_now())
            if self._result_mode == "owned" and self._result_key == _owned_key(spec["game"], refs) and not self._searching:
                self._searched = True
                self._spec = spec  # the same Creations are on show: the new words and sort just filter and order them again
                self._refilter()
                return
        # a search by words is not fetched again for another sort or creator filter (both are applied here); a browse is
        left_out = ("sort", "text") if owned else ("sort", "verified", "official") if mode == "text" else ()
        key = tuple(v for k, v in spec.items() if k not in left_out)
        if self._search_key == key:  # holding Enter must not queue the same search again and again
            return
        game, downloads = spec["game"], self._local_downloads()
        self._search_key = key
        self._searched = True
        self._detail_timer.stop()
        self._next("more")  # a page of the earlier browse is stale now
        self._more_busy = False
        self._searching = True
        self._refresh_more()
        self._loading_owned = owned
        if owned:
            self._set_status(texts.owned_loading_text())
        elif text:
            self._set_status(f"Searching {texts.GAME_LABELS[game]}…")
        elif browse_collects(spec):
            self._set_status(texts.loading_text(spec["sort"]))
        else:
            self._set_status(f"Loading {texts.GAME_LABELS[game]} Creations…")
        self._ensure_categories()
        token = self._next("search")
        tokens = self._tokens  # read from the job's thread: a plain dict lookup that lets a replaced search stop early
        self._loading_sort = spec["sort"]
        collecting = self._signals.collecting  # held now, like the download's progress signal

        def progress(done, total):
            _Job._emit(collecting, token, done, total)

        cache = self._collect_cache
        self._submit("search", lambda: _search_job(spec, downloads, lambda: tokens.get("search") == token, cache, progress, refs),
                     token=token)

    def _control_spec(self) -> dict:
        """The search the controls describe now (the search words only when no author is picked). In the In my game view the
        PC, Free, category, creator and author controls are switched off, so they count as unset."""
        owned = self._owned_only.isChecked()
        return {"game": self.picked_game(), "text": "" if self._author and not owned else self._query.text().strip(),
                "pc": self._pc_only.isChecked() and not owned, "free": self._free_only.isChecked() and not owned,
                "category": None if owned else self._category.currentData(), "author": None if owned else self._author,
                "sort": self._sort_key(), "verified": self._verified_only.isChecked() and not owned,
                "official": self._official_only.isChecked() and not owned, "owned": owned}

    def _apply_search(self, result: dict):
        self._searching = False
        self._tokens["detail"] = self._tokens.get("detail", 0) + 1  # an answer for a row of the old list is now stale
        self._spec, self._result_mode, self._result_key = result["spec"], result["mode"], result.get("key")
        self._results, self._total, self._capped = list(result["rows"]), result["total"], result["capped"]
        self._direct, self._page, self._shown = result["direct"] is not None, 1, SHOW_STEP
        self._detail = None
        self._states = {}
        for row in self._results:
            cid = core.numeric_id(row.get("content_id"))
            if cid is not None:
                self._states[cid] = result["states"].get(cid) or {}
        self._render()
        self._refresh_controls()
        if not self._rows:
            self._show_empty()
            return
        self._report_results()
        direct = result["direct"]
        if direct is not None:
            cid = core.numeric_id(direct["content_id"])
            self._list.blockSignals(True)  # selecting it fetches nothing new: the detail is already here
            self._list.setCurrentItem(self._rows[cid])
            self._list.blockSignals(False)
            self._show_detail(direct, self._states.get(cid), result.get("required") or {})
        else:
            self._show_message("Pick a Creation from the list to see its details.")

    def _filtered(self, rows: list) -> list:
        """``rows`` without the Creations the creator filters leave out (Bethesda has no such filters, so they are applied
        here, to rows that are all here: a text search or a collected browse; never to a pasted reference). In the In my game
        view only the search words apply, and to the titles."""
        if self._result_mode == "owned":
            words = (self._spec or {}).get("text") or ""
            return [r for r in rows if texts.title_matches(r.get("title"), words)] if words else rows
        if self._direct or self._result_mode not in LOCAL_MODES:
            return rows
        verified, official = self._verified_only.isChecked(), self._official_only.isChecked()
        if not (verified or official):
            return rows
        return [r for r in rows if (not verified or r.get("author_verified")) and (not official or r.get("author_official"))]

    def _ordered(self) -> list[dict]:
        """The results in the order they are listed: as Bethesda sent them for a paged browse, sorted here for search words
        and for Most downloaded / Most liked in a collected browse (a collected browse in a server sort keeps that order)."""
        rows = self._filtered(self._results)
        if self._result_mode in ("text", "owned"):
            return core.sort_rows(rows, self._sort_key())
        if self._result_mode == "collect" and (self._spec or {}).get("sort") in core.LOCAL_SORTS:
            return core.sort_rows(rows, self._spec["sort"])
        return list(rows)

    def _visible(self) -> list[dict]:
        rows = self._ordered()
        return rows[:self._shown] if self._result_mode in LOCAL_MODES else rows

    def _render(self):
        """Rebuild the list from the results."""
        self._list.blockSignals(True)
        self._list.clear()
        self._rows.clear()
        for row in self._visible():
            cid = core.numeric_id(row.get("content_id"))
            if cid is not None:
                self._add_row(cid, row)
        self._list.blockSignals(False)
        self._refresh_more()

    def _refilter(self):
        """Another sort or creator filter of rows that are all here (a search by words, a collected browse): order and filter
        them again and show the first rows. No request is made."""
        if self._result_mode == "collect" and self._spec is not None:
            self._spec = {**self._spec, "sort": self._sort_key()}
        current = self._list.currentItem()
        keep = current.data(Qt.ItemDataRole.UserRole + 1) if current is not None else None
        self._shown = SHOW_STEP
        self._render()
        if keep in self._rows:
            self._list.blockSignals(True)  # the same Creation stays selected; its details are already on show
            self._list.setCurrentItem(self._rows[keep])
            self._list.blockSignals(False)
        elif keep is not None:  # it is no longer listed (or not among the first rows): its details no longer belong to the list
            self._tokens["detail"] = self._tokens.get("detail", 0) + 1
            self._detail_timer.stop()
            self._detail = None
            self._show_message("Pick a Creation from the list to see its details.")
            self._refresh_controls()
        if not self._rows:
            self._show_empty()
            return
        self._report_results()

    def _show_empty(self):
        """Say why the list is empty."""
        if self._result_mode == "owned":
            text = (texts.OWNED_NONE if self._total == 0 else texts.OWNED_NO_DETAILS if not self._results
                    else texts.OWNED_NO_MATCH)
            self._show_message(text)
            self._set_status(text)
            return
        self._show_message("Nothing was found. Try other words, switch a filter off, or paste a creations.bethesda.net link or ID.")
        self._set_status("No Creations found.")

    def _report_results(self):
        """The status line for the rows on show: how many are listed of how many there are, and under which filters."""
        spec, shown = self._spec or {}, self._list.count()
        if self._direct:
            switched = f" Switched to {texts.GAME_LABELS[self._switched_to]} for this link." if self._switched_to else ""
            self._set_status(f"Found 1 Creation.{switched}")
            return
        if self._result_mode == "owned":
            kept, have, words = len(self._results), len(self._filtered(self._results)), spec.get("text")
            head = (f"{have:,} of {kept:,} Creations in your game match those words." if words else
                    texts.owned_count_text(have, self._total, core.OWNED_REQUEST_CAP if self._capped else None))
            self._set_status(head + (f" Showing {shown:,} of {have:,}." if shown < have else ""))
            return
        local = self._result_mode in LOCAL_MODES
        filt = texts.filter_summary(spec.get("pc", False), spec.get("free", False), spec.get("category"), spec.get("author"),
                                    verified=local and self._verified_only.isChecked(),
                                    official=local and self._official_only.isChecked())
        suffix = f" ({filt})" if filt else ""
        if self._result_mode == "browse":
            self._set_status(f"Showing {shown:,} of {self._total:,}{suffix}.")
            return
        kept = len(self._results)
        have = len(self._filtered(self._results))
        if self._result_mode == "collect":
            head = texts.collected_head(spec.get("sort", "downloads"), kept, self._capped)
            self._set_status(f"{head}{suffix}. Showing {shown:,} of {have:,}.")
        elif self._capped:
            head = f"Sorted among the {kept:,} best matches of {self._total:,}; add words to narrow it."
            self._set_status(f"{head} Showing {shown:,} of {have:,}{suffix}.")
        else:
            head = f"Found {have:,} Creation{'s' if have != 1 else ''}"
            self._set_status(f"{head}{suffix}." + (f" Showing {shown:,} of {have:,}." if shown < have else ""))

    # ---- Show more

    def _more_available(self) -> bool:
        if self._result_mode in LOCAL_MODES:
            return self._shown < len(self._filtered(self._results))
        return self._result_mode == "browse" and len(self._results) < self._total

    def _refresh_more(self):
        self._more_button.setVisible(self._more_available())
        self._more_button.setEnabled(not self._searching and not self._more_busy)

    def show_more(self):
        """Add the next rows to the list: from the results already here for a search by words or a collected browse, or the
        next page for a paged browse."""
        if self._searching or self._more_busy or not self._more_available():
            return
        if self._result_mode in LOCAL_MODES:
            before = self._shown
            self._shown += SHOW_STEP
            for row in self._visible()[before:]:
                cid = core.numeric_id(row.get("content_id"))
                if cid is not None and cid not in self._rows:
                    self._add_row(cid, row)
            self._refresh_more()
            self._report_results()
            return
        spec, page, downloads = self._spec, self._page + 1, self._local_downloads()
        self._more_busy = True
        self._refresh_more()
        self._set_status("Loading more…", log_it=False)
        token = self._next("more")
        tokens = self._tokens  # read from the job's thread, as for a search: a replaced page makes no request
        self._submit("more", lambda: _more_job(spec, page, downloads, lambda: tokens.get("more") == token), token=token)

    def _apply_more(self, result: dict):
        self._more_busy = False
        self._page = result["page"]
        self._total = max(result["total"], len(self._results))
        if not result["rows"]:
            self._total = len(self._results)  # the catalogue ran out earlier than it said
        for row in result["rows"]:
            cid = core.numeric_id(row.get("content_id"))
            if cid is not None and cid not in self._rows:
                self._states[cid] = result["states"].get(cid) or {}
                self._results.append(row)
                self._add_row(cid, row)
        self._refresh_more()
        self._report_results()

    # ---- categories

    def _ensure_categories(self):
        """Ask Bethesda for the game's category names, once per game for as long as the window lives."""
        game = self.picked_game()
        if game in self._categories_state:
            return
        self._categories_state[game] = "loading"
        self._submit("categories", lambda: _categories_job(game))

    def _apply_categories(self, result: dict):
        game, names = result["game"], result["names"]
        self._categories_state[game] = "done" if names is not None else "failed"
        if names is not None:
            self._categories[game] = names
        if game == self.picked_game():
            self._fill_categories()

    def _fill_categories(self):
        """The category picker for the picked game: 'All categories' and the names known so far; the pick is kept if it is still listed."""
        picked = self._category.currentData()
        self._category.blockSignals(True)
        self._category.clear()
        self._category.addItem(texts.ALL_CATEGORIES, None)
        for name in self._categories.get(self.picked_game(), []):
            self._category.addItem(name, name)
        self._category.setCurrentIndex(max(self._category.findData(picked), 0))
        self._category.blockSignals(False)
        if self._category.view().isVisible():  # the list was opened while it was empty and stayed one row tall: open it again
            self._category.hidePopup()
            self._category.showPopup()

    def _add_row(self, cid: int, row: dict):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole + 1, cid)
        self._rows[cid] = item
        self._list.addItem(item)
        self._update_row(cid, row)

    def _update_row(self, cid: int, row: dict | None = None):
        item = self._rows.get(cid)
        if item is None:
            return
        if row is None:
            row = item.data(ROW_DATA)["source"]
        data = {"title": row.get("title") or f"Creation {cid}", "author": row.get("author") or "",
                "marker": texts.marker(row, owned=self._is_owned(row)),
                "badges": texts.badges(row), "state": texts.state_label(self._states.get(cid)), "stats": texts.row_stats(row),
                "source": row}
        item.setData(ROW_DATA, data)
        by = f"by {data['author']}" + (f" {data['badges']}" if data["badges"] else "") if data["author"] else ""
        item.setData(Qt.ItemDataRole.DisplayRole,  # what a screen reader announces
                     ", ".join(p for p in (data["title"], data["marker"], by, data["state"],
                                           data["stats"].replace("  ·  ", ", ")) if p))

    # ---- selecting a Creation

    def select(self, content_id: int):
        """Select a row by Creation id (what a click on it does)."""
        item = self._rows.get(content_id)
        if item is not None:
            self._list.setCurrentItem(item)

    def _on_row_changed(self, item, _previous):
        self._detail = None
        self._preview.hide()
        self._preview.clear()
        self._refresh_controls()
        if item is None:
            return
        self._show_message("Loading details…")
        self._tokens["detail"] = self._tokens.get("detail", 0) + 1  # answers for an earlier row are now stale
        self._tokens["preview"] = self._tokens.get("preview", 0) + 1
        if self.DETAIL_DELAY_MS:
            self._detail_timer.start(self.DETAIL_DELAY_MS)
        else:
            self._fetch_selected_detail()

    def _fetch_selected_detail(self):
        item = self._list.currentItem()
        if item is None:
            return
        cid, game, downloads = item.data(Qt.ItemDataRole.UserRole + 1), self.picked_game(), self._local_downloads()
        self._submit("detail", lambda: _detail_job(game, cid, downloads), token=self._tokens.get("detail", 0))

    def _apply_detail(self, result: dict):
        d = result["detail"]
        cid = core.numeric_id(d["content_id"])
        if cid is not None and result["state"] is not None:
            self._states[cid] = result["state"]
            self._update_row(cid)
        self._show_detail(d, result["state"], result["required"])

    def _show_detail(self, d: dict, state: dict | None, required_states: dict):
        self._detail, self._required_states = d, required_states
        self._preview.hide()
        self._preview.clear()
        self._d_title.setText(d.get("title") or "Untitled Creation")
        author = d.get("author") or "unknown author"
        self._d_author.setText(f"by {author}" + texts.author_marks(d))
        facts = []
        if d.get("version"):
            facts.append(f"Version {d['version']}")
        size = (d.get("pc_file") or {}).get("bytes")
        if size is not None:
            facts.append(texts.human_bytes(size))
        facts.append(f"For {texts.GAME_LABELS.get(d['game'], d['game'])}")
        self._d_facts.setText("  ·  ".join(facts))
        for lab, text in ((self._d_stats, texts.detail_stats(d)), (self._d_dates, texts.detail_dates(d))):
            lab.setText(text)
            lab.setVisible(bool(text))
        self._author_button.setVisible(core.valid_name(d.get("author")))
        owned = self._is_owned(d)
        self._d_avail.setText(texts.availability(d, owned=owned))
        self._d_owned.setText(texts.OWNED_DETAIL if owned else "")
        self._d_owned.setVisible(owned)
        self._d_local.setText(self._local_text(d, state))
        self._d_local.setVisible(bool(self._d_local.text()))
        dlc = [str(x.get("name") or x.get("title") or x) if isinstance(x, dict) else str(x) for x in d.get("required_dlc") or []]
        self._d_dlc.setText("Requires DLC: " + ", ".join(dlc) if dlc else "")
        self._d_dlc.setVisible(bool(dlc))
        self._render_required()
        self._description.setPlainText(d.get("description") or "No description.")
        notes = d.get("release_notes") or []
        self._notes.setPlainText("\n\n".join(f"{n['version']}\n{n['note']}" if n["version"] else n["note"] for n in notes)
                                 or "No release notes for the PC version.")
        self._page_button.setVisible(bool(d.get("page")))
        self._stack.setCurrentIndex(1)
        self._refresh_controls()
        if d.get("preview"):
            token = self._next("preview")
            dpr = self.devicePixelRatioF()
            box = QSize(int(self._preview.width() * dpr), int(self._preview.height() * dpr))
            tokens = self._tokens  # read from the job's thread: a plain dict lookup that only lets the job skip work
            self._submit("preview", lambda: _preview_job(d, box, lambda: tokens.get("preview") == token), token=token)

    def _local_text(self, d: dict, state: dict | None) -> str:
        if self._local_downloads() is None or state is None:
            return ""
        label = state.get("state")
        if label == "downloaded":
            return ("Installed: Mod Organizer 2 marks this download as installed." if state.get("meta_installed")
                    else "Downloaded: it is in your Downloads folder.")
        if label == "update":
            return f"Update available: your Downloads folder has version {state.get('version')}, the latest is {d.get('version')}."
        return "Not downloaded yet." if d.get("downloadable") else ""  # only worth saying when it can be downloaded here

    def _render_required(self):
        d = self._detail
        mods = d["required_mods"] if d else []
        lines = []
        for m in mods:
            cid = m.get("content_id")
            if m.get("title") is None:
                lines.append(f"• Creation {cid} (details unavailable)")
                continue
            tail = "Free" if m.get("downloadable") else (texts.price_text(m.get("price"), short=True) or texts.GET_IN_GAME)
            local = texts.state_label(self._required_states.get(cid)) if self._local_downloads() is not None else ""
            lines.append(f"• {m['title']}: {tail}" + (f", {local.lower()}" if local else ""))
        self._d_required.setText("Also needs these Creations:\n" + "\n".join(lines) if lines else "")
        self._d_required.setVisible(bool(lines))

    def _open_page(self):
        page = (self._detail or {}).get("page")
        if isinstance(page, str) and page.startswith(PAGE_PREFIX):
            QDesktopServices.openUrl(QUrl(page))

    def _apply_preview(self, image):
        if image is None or self._detail is None:
            return
        dpr = self.devicePixelRatioF()
        pixmap = QPixmap.fromImage(image.scaled(int(self._preview.width() * dpr), int(self._preview.height() * dpr),
                                                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        pixmap.setDevicePixelRatio(dpr)
        self._preview.setPixmap(pixmap)
        self._preview.show()

    # ---- game picker

    def _on_game_changed(self, _index):
        self._detail_timer.stop()
        self._search_timer.stop()
        self._search_key = None
        for kind in list(self._tokens):  # answers still on their way belong to the other game (a download keeps its own)
            if kind not in ("download", "categories"):  # the category lists are kept per game
                self._tokens[kind] += 1
        self._searching = self._searched = self._more_busy = False
        self._switched_to = None
        self._author = None
        self._show_author(None)
        self._results, self._result_mode, self._spec, self._total, self._shown = [], None, None, 0, 0
        self._result_key = None
        self._catalog = None  # the other game keeps its own list of Creations
        self._category.setCurrentIndex(0)  # the other game's pick does not carry over
        self._fill_categories()
        self._list.blockSignals(True)
        self._list.clear()
        self._list.blockSignals(False)
        self._rows.clear()
        self._states.clear()
        self._detail = None
        self._show_message(texts.BROWSE_HINT)
        self._refresh_more()
        self._set_status(f"Now browsing {texts.GAME_LABELS[self.picked_game()]}…")
        self._refresh_controls()
        self._search_timer.start(self.SEARCH_DELAY_MS)  # the other game's default list (or its In my game view) opens by itself

    # ---- downloading

    def download_selected(self):
        d = self._detail
        if d and d["downloadable"] and self._blocker() is None and self._current is None:
            self._start_batch([(core.numeric_id(d["content_id"]), d.get("title") or "this Creation")])

    def download_required(self):
        if self._blocker() is None and self._current is None:
            todo = self._pending_required()
            if todo:
                self._start_batch(todo)

    def _start_batch(self, items: list[tuple[int, str]]):
        self._queue = list(items)
        self._batch = {"total": len(items), "saved": 0, "reused": 0}
        self._run_next()

    def _run_next(self):
        if not self._queue:
            self._finish_batch()
            return
        self._current = self._queue.pop(0)
        cid, title = self._current
        number = self._batch["total"] - len(self._queue)
        self._cancel = threading.Event()
        cancel, game, downloads = self._cancel, self.picked_game(), self._downloads
        token = self._next("download")
        self._progress.setRange(0, 0)
        where = f" ({number} of {self._batch['total']})" if self._batch["total"] > 1 else ""
        self._set_status(f"Contacting Bethesda for {title}{where}…")
        self._refresh_controls()

        progress_signal = self._signals.progress  # held now, so a job that outlives the window never reaches for a deleted widget

        def progress(done, total):
            _Job._emit(progress_signal, token, done, total)

        self._submit("download", lambda: _download_job(game, cid, downloads, progress, cancel.is_set), token=token)

    def cancel_download(self):
        if self._current is not None:
            self._queue.clear()
            self._cancel.set()
            self._cancel_button.setEnabled(False)
            self._set_status("Cancelling…")

    def _on_progress(self, token, done, total):
        if self._current is None or not self._current_token("download", token):
            return
        cid, title = self._current
        number = self._batch["total"] - len(self._queue)
        where = f" ({number} of {self._batch['total']})" if self._batch["total"] > 1 else ""
        if total:
            self._progress.setRange(0, 100)
            self._progress.setTextVisible(True)
            self._progress.setValue(min(100, int(done * 100 / total)))
            self._set_status(f"Downloading {title}{where}: {texts.human_bytes(done)} of {texts.human_bytes(total)}", log_it=False)
        else:
            self._progress.setRange(0, 0)
            self._set_status(f"Downloading {title}{where}: {texts.human_bytes(done)}", log_it=False)

    def _on_collecting(self, token, done, total):
        """A browse that is collecting its Creations has read ``done`` of ``total`` pages."""
        if self._searching and self._current_token("search", token):
            self._set_status(texts.owned_loading_text(done, total) if self._loading_owned
                             else texts.loading_text(self._loading_sort, done, total), log_it=False)

    def _download_done(self, result: dict):
        cid = result["content_id"]
        self._states[cid] = result["state"]
        self._update_row(cid)
        if self._detail and core.numeric_id(self._detail["content_id"]) == cid:
            self._d_local.setText(self._local_text(self._detail, result["state"]))
            self._d_local.setVisible(True)
        if self._detail and cid in [m.get("content_id") for m in self._detail["required_mods"]]:
            self._required_states[cid] = result["state"]
            self._render_required()
            self._refresh_controls()
        self._batch["reused" if result["reused"] else "saved"] += 1
        downloads = self._local_downloads()
        if downloads is not None:  # MO2 can miss a new download in its Downloads tab; have it re-read the folder once things settle
            QTimer.singleShot(NUDGE_DELAY_MS, functools.partial(refresh_mo2_downloads, self.parentWidget(), downloads))
        name = result["name"]
        if self._batch["total"] == 1:
            self._set_status(f"Already downloaded: {name}. It is in MO2's Downloads tab; install it from there." if result["reused"]
                             else f"Saved to Downloads: {name}. Install it from MO2's Downloads tab.")
        self._current = None
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.setValue(100)
        self._run_next()

    def _finish_batch(self):
        self._current = None
        if self._batch["total"] > 1:
            saved, reused = self._batch["saved"], self._batch["reused"]
            extra = f" ({reused:,} already there)" if reused else ""
            self._set_status(f"Saved {saved:,} Creation{'s' if saved != 1 else ''} to Downloads{extra}. "
                             "Install them from MO2's Downloads tab.")
        self._refresh_controls()

    def _download_failed(self, status: str, message: str):
        self._current = None
        self._queue.clear()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._set_status(texts.describe_error(status, message, refused=message) if status != "INTERNAL" else
                         "Something went wrong inside the plugin. Nothing was saved; see Mod Organizer 2's log for details.",
                         problem=True)
        self._refresh_controls()

    # ---- results from the background jobs

    def _on_done(self, token: int, kind: str, result):
        self._pending = max(0, self._pending - 1)
        if kind == "download":
            if self._current_token("download", token):
                self._download_done(result)
        elif kind == "categories":
            self._apply_categories(result)  # kept whatever happened since: it names its own game
        elif not self._current_token(kind, token):
            return
        elif kind == "search":
            self._search_key = None
            self._apply_search(result)
        elif kind == "more":
            self._apply_more(result)
        elif kind == "detail":
            self._apply_detail(result)
        elif kind == "preview":
            self._apply_preview(result)

    def _on_failed(self, token: int, kind: str, status: str, message: str):
        self._pending = max(0, self._pending - 1)
        core.log_line(logging.INFO, f"{kind} failed: {status} {message}")
        if kind == "download":
            if self._current_token("download", token):
                self._download_failed(status, message)
            return
        if kind == "categories":  # the picker just offers 'All categories'; never an error
            for game, state in self._categories_state.items():
                if state == "loading":
                    self._categories_state[game] = "failed"
            return
        if kind == "search" and self._current_token(kind, token):
            self._search_key = None
            self._searching = False
            self._refresh_more()
        if kind == "more" and self._current_token(kind, token):
            self._more_busy = False
            self._refresh_more()
        if not self._current_token(kind, token) or kind == "preview":
            return
        text = (texts.describe_error(status, message, refused=message) if status != "INTERNAL"
                else "Something went wrong inside the plugin; see Mod Organizer 2's log for details.")
        if kind == "detail":
            self._show_message(f"Couldn't load this Creation. {text}", rest=False)
        self._set_status(text, problem=True)

    # ---- showing and closing

    def showEvent(self, event):
        # The window opens on a list: the first time it is shown (and again if that first load never finished, or the game
        # was changed and nothing was searched since) it browses by itself. A window that already has its rows just shows them.
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._screen_hooked:  # the mark is redrawn for the pixel ratio of the screen it is on
            handle.screenChanged.connect(lambda _screen: self._dress_message())
            self._screen_hooked = True
        self._dress_message()
        if not event.spontaneous() and self._catalog is None and self._rows:  # reopened: the game's list may have grown
            self._reload_catalog()
        if (self.AUTO_BROWSE and not event.spontaneous() and self._spec is None and not self._searching
                and not self._search_timer.isActive()):
            self._launch()

    def shutdown(self):
        """Ask a download in progress to stop and cancel what is waiting. It never waits: the job notices the cancel before
        its next chunk (and before it saves anything), removes its own partial files and reports back through a signal
        that is dropped if the window is gone by then."""
        self._queue.clear()
        self._cancel.set()
        self._detail_timer.stop()
        self._search_timer.stop()
        self._next("search")  # a search by words still reading pages stops before its next request

    def reject(self):
        """Esc closes the window the same way as its close button (QDialog's own reject only hides it)."""
        self._on_closing()
        super().reject()

    def closeEvent(self, event):
        self._on_closing()
        event.accept()  # not QDialog.closeEvent: that calls reject() again

    def _on_closing(self):
        # Closing the window cancels a download in progress; the window itself is kept and shown again later. The download
        # counts as running until its job reports back (so a second one cannot start beside it).
        if self._current is not None:
            self._set_status("Download cancelled because the window was closed. Nothing was saved.")
            self._queue.clear()
            self._cancel.set()
        # a search by words still reading pages stops before its next request, and a search can run again when the window opens
        self._next("search")
        self._catalog = None  # read again when the window is reopened
        self._detail_timer.stop()
        self._search_timer.stop()
        was_searching = self._searching
        self._searching = False
        self._search_key = None
        self._refresh_more()
        if was_searching and self._spec is not None:
            self._report_results()  # the rows still on show are the old ones; say so
