"""The Creations Browser window and the Mod Organizer 2 tool plugin, driven offscreen against the fake transport.

No real network and no real ``mobase``: a minimal fake ``mobase`` module is injected for the plugin tests. Worker threads are
waited on by pumping the Qt event loop until the window reports it is idle (qtbot is not needed).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import threading
import time
import types
import urllib.parse
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QEvent, QIODevice, QRect, QSize, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QFontMetrics, QImage, QPalette  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QStyle, QWidget  # noqa: E402

from creations_browser import core, texts  # noqa: E402
from creations_browser import window as window_module  # noqa: E402
from creations_browser.window import CreationsWindow  # noqa: E402
from fakes import GOOD_ENTRIES, KEY, UUID, Response, api_item, btar, secrets_absent  # noqa: E402

UUID_A = "aaaaaaaa-2222-3333-4444-555555555555"
UUID_B = "bbbbbbbb-2222-3333-4444-555555555555"
UUID_C = "cccccccc-2222-3333-4444-555555555555"
PREVIEW_KEY = "public/content/FALLOUT4/743724/CLASSIFICATION_PREVIEW_IMAGE/preview.png"


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        QApplication.processEvents()
        time.sleep(0.002)
        assert time.monotonic() < deadline, "timed out waiting"


def settle(window):
    wait_for(lambda: not window.busy())
    QApplication.processEvents()


@pytest.fixture
def make_window(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr(CreationsWindow, "DETAIL_DELAY_MS", 0)
    monkeypatch.setattr(CreationsWindow, "SEARCH_DELAY_MS", 20)  # the real pause is 300 ms; tests that time it set it back
    monkeypatch.setattr(CreationsWindow, "AUTO_BROWSE", False)  # tests search for themselves; the first-show tests turn it on
    windows = []

    def make(instance="fallout4", downloads=True):
        window = CreationsWindow(None, instance, (tmp_path / "dl") if downloads else None)
        windows.append(window)
        return window

    yield make
    for window in windows:
        window.shutdown()
        window.close()
        window.deleteLater()
    QApplication.processEvents()


def png_bytes():
    image = QImage(32, 18, QImage.Format.Format_RGB32)
    image.fill(0xFF3366CC)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def rows(window):
    return [window._list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(window._list.count())]


def sources(window):
    """The catalogue summaries behind the listed rows, in the order listed."""
    return [r["source"] for r in rows(window)]


def pick(window, cid):
    window.select(cid)
    settle(window)


def visible(window, widget):
    return widget.isVisibleTo(window)


def add_fine(net, **extra):
    net.add(api_item(**extra))


# ---------------------------------------------------------------- searching

def test_opening_the_window_makes_no_request(make_window, net):
    window = make_window()
    window.show()
    QApplication.processEvents()
    assert net.calls == [] and window.windowTitle() == "Creations Browser for Mod Organizer 2"
    assert window._notice.text() == texts.NOTICE
    assert "not affiliated with Bethesda or ZeniMax" in window._notice.text() and "author keeps all rights" in window._notice.text()
    assert window._query.placeholderText() == "Search Creations, or paste a creations.bethesda.net link or ID"


def test_search_fills_the_list_with_clear_free_markers(make_window, net):
    add_fine(net)
    net.add(api_item(cid=2, uuid=UUID_A, title="Fine Paid Thing", price="500"))
    net.add(api_item(cid=3, uuid=UUID_B, title="Fine Gone Thing", deleted=True))
    net.add(api_item(cid=4, uuid=UUID_C, title="Unrelated"))
    window = make_window()
    window.search("fine")
    settle(window)
    got = {r["title"]: r["marker"] for r in rows(window)}
    assert got == {"Fine Mod: Revamped": "Free", "Fine Paid Thing": "Get it in game", "Fine Gone Thing": "Withdrawn"}
    assert window._status.text() == "Found 3 Creations (PC, free)."
    first = window._list.item(0).text()
    assert "Fine Mod: Revamped" in first and "by Someone" in first and "Free" in first  # what a screen reader announces
    window.show()
    window.grab()  # the row delegate paints without errors
    assert {h["x-bnet-product"] for u, h in net.calls if "api.bethesda.net" in u} == {"fallout4"}
    assert not net.downloads()  # a search downloads nothing, and fetches no preview


def test_enter_in_the_search_box_searches(make_window, net):
    add_fine(net)
    window = make_window()
    window._query.setText("fine")
    QTest.keyClick(window._query, Qt.Key.Key_Return)
    settle(window)
    assert window._list.count() == 1


def test_an_empty_result_says_so(make_window, net):
    window = make_window()
    window.search("nothing like this")
    settle(window)
    assert window._list.count() == 0 and window._status.text() == "No Creations found."


def test_a_catalogue_failure_is_explained_in_plain_words(make_window, monkeypatch):
    def down(request, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(core, "_open", down)
    window = make_window()
    window.search("fine")
    settle(window)
    assert window._status.text() == texts.describe_error("UNREACHABLE")
    assert window._status.text() == "Couldn't reach Bethesda's servers. Check your internet connection and try again."
    assert "connection refused" not in window._status.text()


def test_a_pasted_link_or_id_opens_that_creation(make_window, net):
    add_fine(net)
    window = make_window()
    window.search(f"https://creations.bethesda.net/en/fallout4/details/{UUID}/Fine_Mod")
    settle(window)
    assert window._list.count() == 1 and window._d_title.text() == "Fine Mod: Revamped"
    assert window._stack.currentIndex() == 1
    window.search("743724")
    settle(window)
    assert window._list.count() == 1 and window._d_title.text() == "Fine Mod: Revamped"
    assert sum(1 for u, _ in net.calls if u.endswith("/content/743724")) == 2


def test_a_number_that_is_no_creation_is_searched_as_a_word(make_window, net):
    net.add(api_item(cid=5, uuid=UUID_A, title="Mod 2077 Edition"))
    window = make_window()
    window.search("2077")
    settle(window)
    assert [r["title"] for r in rows(window)] == ["Mod 2077 Edition"]


def test_a_link_for_the_wrong_game_or_a_missing_id_is_explained(make_window, net):
    window = make_window()
    window.search("https://creations.bethesda.net/en/fallout4/details/99999999-2222-3333-4444-555555555555")
    settle(window)
    assert window._status.text() == texts.describe_error("NOT_FOUND")
    window.search("https://creations.bethesda.net/en/fallout4/details/nothing")
    settle(window)
    assert window._status.text() == "Check what you typed: That link doesn't contain a Creation id."


# ---------------------------------------------------------------- details

def full_item(**extra):
    return api_item(
        required_mods=[1001, 1002], required_dlc=["DLCCoast"], author_verified=True,
        description="A fine description.\r\n\r\nWith a second paragraph.",
        release_notes=[{"hardware_platform": "XBOXONE", "release_notes": [{"note": "console", "version_name": "9"}]},
                       {"hardware_platform": "WINDOWS", "release_notes": [{"note": "Fixed the thing", "version_name": "1.04"}]}],
        **extra)


def add_requirements(net):
    net.add(api_item(cid=1001, uuid=UUID_A, title="Base Library"))
    net.add(api_item(cid=1002, uuid=UUID_B, title="Paid Library", price="100"))


def test_selecting_a_creation_shows_its_details_and_requirements(make_window, net):
    net.add(full_item())
    add_requirements(net)
    window = make_window()
    window.search("fine")
    settle(window)
    assert not visible(window, window._req_button)
    pick(window, 743724)
    assert window._stack.currentIndex() == 1
    assert window._d_title.text() == "Fine Mod: Revamped"
    assert window._d_author.text() == "by Someone (verified author)"
    assert window._d_facts.text() == f"Version 1.04  ·  {texts.human_bytes(len(btar(GOOD_ENTRIES)))}  ·  For Fallout 4"
    assert window._d_avail.text() == "Free: can be downloaded here."
    assert window._d_local.text() == "Not downloaded yet."
    assert window._d_dlc.text() == "Requires DLC: DLCCoast"
    assert "Base Library: Free" in window._d_required.text() and "Paid Library: Get it in game" in window._d_required.text()
    assert visible(window, window._req_button) and window._req_button.text() == "Download these too"
    assert window._description.toPlainText() == "A fine description.\n\nWith a second paragraph."
    assert window._notes.toPlainText() == "1.04\nFixed the thing"
    assert visible(window, window._page_button)
    assert window._download_button.isEnabled()
    assert window._detail["page"] == f"https://creations.bethesda.net/en/fallout4/details/{UUID}"
    secrets_absent(window._detail)


def test_untrusted_text_is_shown_as_plain_text(make_window, net):
    net.add(api_item(title="<b>Bold</b> &amp; <a href='http://evil/'>link</a>", description="<script>x</script>See <i>this</i>"))
    window = make_window()
    window.search("bold")
    settle(window)
    pick(window, 743724)
    assert window._d_title.textFormat() == Qt.TextFormat.PlainText
    assert window._d_title.text() == "<b>Bold</b> & <a href='http://evil/'>link</a>"
    assert "<" not in window._description.toPlainText() and "See this" in window._description.toPlainText()


def test_a_paid_creation_says_get_it_in_game_and_cannot_be_downloaded(make_window, net):
    net.add(api_item(price="500"))
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert window._d_avail.text() == "Paid Creation: get it in game."  # said once, not twice
    assert window._d_local.text() == ""  # "Not downloaded yet" only for what can be downloaded here
    assert not window._download_button.isEnabled()
    window.download_selected()
    settle(window)
    assert not net.downloads()
    assert not (Path(window._downloads).exists() and any(Path(window._downloads).iterdir()))


def store_listing(*stores, amount=400, original=None, sale=False):
    """A catalog_info as Bethesda lists it: one entry per store, each priced in Creation Credits."""
    return [{"platform": s, "catalog_item_external_id": "content_743724",
             "prices": [{"currency_name": "Creation Club Virtual Currency", "currency_type": "VIRTUAL", "amount": amount,
                         "original_amount": amount if original is None else original, "sale": sale}]} for s in stores]


def test_a_store_creation_shows_its_price_in_credits_in_the_row_and_details(make_window, net):
    net.add(api_item(catalog=store_listing("EPIC", "STEAM", "XBOXLIVE", amount=400)))
    window = make_window()
    window._free_only.setChecked(False)
    window.search("fine")
    settle(window)
    assert rows(window)[0]["marker"] == "400 credits"
    pick(window, 743724)
    assert window._d_avail.text() == "In Bethesda's Creations store for 400 Creation Credits. Get it in the game's Creations menu."
    assert not window._download_button.isEnabled()


def test_a_store_creation_on_sale_or_free_to_claim_says_so(make_window, net):
    net.add(api_item(catalog=store_listing("STEAM", amount=300, original=400, sale=True)))
    net.add(api_item(cid=743725, uuid="11111111-2222-3333-4444-666666666666", title="Fine Claim",
                     catalog=store_listing("STEAM", amount=0)))
    window = make_window()
    window._free_only.setChecked(False)
    window.search("fine")
    settle(window)
    markers = {r["title"]: r["marker"] for r in rows(window)}
    assert markers == {"Fine Mod: Revamped": "300 credits (sale, was 400)", "Fine Claim": "Claim in game (free)"}
    pick(window, 743725)
    assert window._d_avail.text() == "In Bethesda's Creations store, free to claim. Get it in the game's Creations menu."
    pick(window, 743724)
    assert window._d_avail.text() == ("In Bethesda's Creations store for 300 Creation Credits, on sale (was 400). "
                                      "Get it in the game's Creations menu.")


def test_a_withdrawn_creation_cannot_be_downloaded(make_window, net):
    net.add(api_item(restricted=True))
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert "Withdrawn or restricted by Bethesda: not available here" in window._d_avail.text()
    assert not window._download_button.isEnabled()


def test_a_creation_without_a_pc_file_cannot_be_downloaded(make_window, net):
    item = api_item()
    item["download"] = []
    net.add(item)
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert "No PC download" in window._d_avail.text() and not window._download_button.isEnabled()


def test_a_detail_failure_is_explained_in_the_panel(make_window, net):
    net.add(api_item())
    window = make_window()
    window.search("fine")
    settle(window)
    net.items.clear()  # gone from the catalogue between the search and the click
    pick(window, 743724)
    assert window._stack.currentIndex() == 0 and texts.describe_error("NOT_FOUND") in window._message.text()


def test_arrowing_through_the_list_fetches_only_the_row_it_stops_on(make_window, net, monkeypatch):
    for i, uid in enumerate((UUID_A, UUID_B, UUID_C)):
        net.add(api_item(cid=10 + i, uuid=uid, title=f"Row {i}"))
    window = make_window()
    window.search("row")
    settle(window)
    monkeypatch.setattr(CreationsWindow, "DETAIL_DELAY_MS", 60)
    before = len(net.calls)
    for cid in (10, 11, 12):
        window.select(cid)
    assert window.busy()
    settle(window)
    detail_calls = [u for u, _ in net.calls[before:] if "/content/" in u]
    assert [u.rsplit("/", 1)[1] for u in detail_calls] == ["12"] and window._d_title.text() == "Row 2"


# ---------------------------------------------------------------- the preview image

def test_the_preview_is_fetched_only_for_the_selected_creation(make_window, net):
    net.add(api_item(preview_image={"s3bucket": "ugcmods.bethesda.net", "s3key": PREVIEW_KEY, "file_type": "png", "width": 32, "height": 18}))
    net.files["/" + PREVIEW_KEY] = png_bytes()
    window = make_window()
    window.search("fine")
    settle(window)
    assert not net.downloads()
    pick(window, 743724)
    assert [u for u in net.downloads()] == ["https://ugcmods.bethesda.net/" + PREVIEW_KEY]
    assert visible(window, window._preview) and not window._preview.pixmap().isNull()
    assert all("x-bnet-key" not in h for u, h in net.calls if "ugcmods.bethesda.net" in u)


def test_a_broken_or_missing_preview_leaves_no_error(make_window, net):
    net.add(api_item(preview_image={"s3bucket": "ugcmods.bethesda.net", "s3key": PREVIEW_KEY, "file_type": "png"}))
    net.files["/" + PREVIEW_KEY] = b"\x89PNG\r\n\x1a\n" + b"not really a picture"
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert not visible(window, window._preview) and window._stack.currentIndex() == 1
    assert "Found 1 Creation" in window._status.text()  # still the search message: nothing was reported


# ---------------------------------------------------------------- downloading

def test_a_free_download_writes_the_zip_and_meta_and_the_row_shows_downloaded(make_window, net, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="creations_browser")
    add_fine(net)
    window = make_window()
    window.search("fine")
    settle(window)
    assert rows(window)[0]["state"] == ""
    pick(window, 743724)
    window._download_button.click()
    assert not window._download_button.isEnabled() and window._cancel_button.isEnabled()  # at once: one download at a time
    settle(window)
    zipped = tmp_path / "dl" / "Fine Mod_ Revamped (Creation 743724) v1.04.zip"
    assert zipped.is_file() and zipfile.is_zipfile(zipped) and Path(str(zipped) + ".meta").is_file()
    assert window._status.text() == f"Saved to Downloads: {zipped.name}. Install it from MO2's Downloads tab."
    assert rows(window)[0]["state"] == "Downloaded" and "Downloaded" in window._list.item(0).text()
    assert window._d_local.text().startswith("Downloaded") and window._progress.value() == 100
    assert window._download_button.isEnabled() and not window._cancel_button.isEnabled() and window._game.isEnabled()
    assert core.read_meta(zipped)["creationscontentid"] == "743724" and core.read_meta(zipped)["gamename"] == "Fallout4"
    assert len(net.downloads()) == 1
    # the key and the signed link are in no message the window or the log produced
    assert KEY not in caplog.text and "SIGNED-SECRET" not in caplog.text and "sig=" not in caplog.text
    assert KEY not in window._status.text() and "sig=" not in window._status.text()


def test_a_download_makes_mo2_reread_its_downloads_folder_once_things_settle(make_window, net, tmp_path, monkeypatch):
    nudged = []
    monkeypatch.setattr(core, "nudge_downloads_watcher", lambda d: nudged.append(Path(d)))
    monkeypatch.setattr(window_module, "NUDGE_DELAY_MS", 10)
    add_fine(net)
    window = make_window()
    window.search("743724")
    settle(window)
    window._download_button.click()
    settle(window)
    wait_for(lambda: nudged)
    assert nudged == [tmp_path / "dl"]  # no MO2 window here: the marker-file nudge


def test_mo2s_own_downloads_refresh_button_is_pressed_when_it_is_there(tmp_path, monkeypatch, qapp):
    nudged = []
    monkeypatch.setattr(core, "nudge_downloads_watcher", lambda d: nudged.append(d))
    main = QWidget()
    button = QPushButton("Refresh", main)
    button.setObjectName(window_module.MO2_REFRESH_BUTTON)
    pressed = []
    button.clicked.connect(lambda: pressed.append(True))
    window_module.refresh_mo2_downloads(main, tmp_path)
    assert pressed == [True] and nudged == []  # MO2 re-reads the folder itself
    button.setEnabled(False)
    window_module.refresh_mo2_downloads(main, tmp_path)
    assert pressed == [True] and nudged == [tmp_path]  # a disabled button is not pressed: the nudge instead
    window_module.refresh_mo2_downloads(QWidget(), tmp_path)  # another MO2 without that button
    assert nudged == [tmp_path, tmp_path]
    main.deleteLater()
    QApplication.processEvents()


def test_downloading_again_reuses_the_file_and_says_so(make_window, net, tmp_path):
    add_fine(net)
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_selected()
    settle(window)
    window.download_selected()
    settle(window)
    assert window._status.text().startswith("Already downloaded: Fine Mod_ Revamped (Creation 743724) v1.04.zip.")
    assert "Downloads tab" in window._status.text() and len(net.downloads()) == 1


def test_an_older_download_shows_update_available_and_a_mo2_installed_flag_shows_installed(make_window, net, tmp_path):
    old = api_item(version="1.03", ctime=50)
    net.add(old)
    core.fetch(tmp_path / "dl", core.plan("fallout4", 743724))
    net.add(api_item(version="1.04", ctime=100))
    window = make_window()
    window.search("fine")
    settle(window)
    assert rows(window)[0]["state"] == "Downloaded"  # the listing does not know the latest version yet
    pick(window, 743724)
    assert rows(window)[0]["state"] == "Update available"
    assert window._d_local.text() == "Update available: your Downloads folder has version 1.03, the latest is 1.04."
    window.download_selected()
    settle(window)
    assert rows(window)[0]["state"] == "Downloaded"
    meta = next((tmp_path / "dl").glob("*1.04.zip.meta"))
    meta.write_bytes(meta.read_bytes().replace(b"installed=false", b"installed=true"))
    window.search("fine")
    settle(window)
    assert rows(window)[0]["state"] == "Installed"


def test_a_download_can_be_cancelled_and_leaves_nothing(make_window, net, tmp_path, monkeypatch):
    add_fine(net)
    monkeypatch.setattr(core, "CHUNK", 8)
    started, gate = threading.Event(), threading.Event()
    real = core._open

    class Gated(Response):
        def read(self, n=-1):
            if started.is_set():
                gate.wait(10)
            started.set()
            return super().read(n)

    def gated(request, timeout):
        response = real(request, timeout)
        return Gated(response.read()) if "ugcmods.bethesda.net" in request.full_url else response

    monkeypatch.setattr(core, "_open", gated)
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_selected()
    wait_for(started.is_set)
    window._cancel_button.click()
    assert window._status.text() == "Cancelling…" and not window._cancel_button.isEnabled()
    gate.set()
    settle(window)
    assert window._status.text() == "Download cancelled. Nothing was saved."
    assert list((tmp_path / "dl").iterdir()) == [] and rows(window)[0]["state"] == ""
    assert window._download_button.isEnabled() and not window._cancel_button.isEnabled()


def test_closing_the_window_during_a_download_cancels_it_cleanly(make_window, net, tmp_path, monkeypatch):
    add_fine(net)
    monkeypatch.setattr(core, "CHUNK", 8)
    started, gate = threading.Event(), threading.Event()
    real = core._open

    class Gated(Response):
        def read(self, n=-1):
            if started.is_set():
                gate.wait(10)
            started.set()
            return super().read(n)

    monkeypatch.setattr(core, "_open", lambda request, timeout: (Gated(real(request, timeout).read()) if "ugcmods.bethesda.net" in request.full_url
                                                                  else real(request, timeout)))
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_selected()
    wait_for(started.is_set)
    threading.Timer(0.2, gate.set).start()  # the blocked read returns shortly after the window is closed
    window.close()
    settle(window)
    assert not window.isVisible()
    assert list((tmp_path / "dl").iterdir()) == []
    assert window._status.text() == "Download cancelled. Nothing was saved."
    window.show()  # and it opens again, ready to use
    window.download_selected()
    settle(window)
    assert len(list((tmp_path / "dl").glob("*.zip"))) == 1


def test_download_these_too_runs_the_free_ones_one_by_one(make_window, net, tmp_path, monkeypatch):
    net.add(full_item())
    net.add(api_item(cid=1001, uuid=UUID_A, title="Base Library"))
    net.add(api_item(cid=1002, uuid=UUID_B, title="Second Library"))
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert "Base Library: Free" in window._d_required.text() and "Second Library: Free" in window._d_required.text()
    active, peak, real_fetch = [0], [0], core.fetch

    def tracking(*args, **kwargs):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        try:
            return real_fetch(*args, **kwargs)
        finally:
            active[0] -= 1
    monkeypatch.setattr(core, "fetch", tracking)
    window._req_button.click()
    assert window._current == (1001, "Base Library") and window._queue == [(1002, "Second Library")]
    settle(window)
    assert peak[0] == 1  # never two downloads at once
    names = sorted(p.name for p in (tmp_path / "dl").glob("*.zip"))
    assert names == ["Base Library (Creation 1001) v1.04.zip", "Second Library (Creation 1002) v1.04.zip"]
    order = [u for u in net.downloads()]
    assert order == ["https://ugcmods.bethesda.net/files/1001/1.04/1001.ckm?sig=SIGNED-SECRET&exp=1",
                     "https://ugcmods.bethesda.net/files/1002/1.04/1002.ckm?sig=SIGNED-SECRET&exp=1"]
    assert window._status.text().startswith("Saved 2 Creations to Downloads.")
    assert not visible(window, window._req_button)  # nothing left to fetch
    assert "Base Library: Free, downloaded" in window._d_required.text()
    assert not window._cancel_button.isEnabled() and window._download_button.isEnabled()


def test_a_failed_item_stops_the_rest_of_the_set(make_window, net, tmp_path):
    net.add(full_item())
    bad = api_item(cid=1001, uuid=UUID_A, title="Base Library")
    bad["download"][0]["published"][0]["client"]["main"]["size"] += 1
    net.add(bad)
    net.add(api_item(cid=1002, uuid=UUID_B, title="Second Library"))
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_required()
    settle(window)
    assert window._status.text() == texts.describe_error("DOWNLOAD_MISMATCH")
    assert list((tmp_path / "dl").iterdir()) == [] and window._current is None and window._queue == []


@pytest.mark.parametrize("status,mutate", [
    ("DOWNLOAD_MISMATCH", lambda item, net: item["download"][0]["published"][0]["client"]["main"].update(size=1)),
    ("UNSAFE_CKM", lambda item, net: None),
])
def test_download_errors_are_shown_in_plain_words(make_window, net, tmp_path, status, mutate):
    item = api_item(ckm=btar([("data\\x.dll", b"x")])) if status == "UNSAFE_CKM" else api_item()
    mutate(item, net)
    net.add(item, ckm=btar([("data\\x.dll", b"x")]) if status == "UNSAFE_CKM" else None)
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_selected()
    settle(window)
    if status == "UNSAFE_CKM":  # the refused file is named
        assert window._status.text() == "The download was refused: Data/x.dll isn't a type this plugin accepts. Nothing was saved."
    else:
        assert window._status.text() == texts.describe_error(status)
    assert window._status.text().endswith(".") and list((tmp_path / "dl").iterdir()) == []
    assert window._download_button.isEnabled()


# ---------------------------------------------------------------- the picked game

def test_the_game_defaults_to_the_instance_and_a_change_clears_the_list(make_window, net):
    add_fine(net)
    window = make_window("skyrimspecialedition")
    assert window.picked_game() == "skyrimspecialedition"
    window.search("fine")
    settle(window)
    assert window._list.count() == 1
    window._game.setCurrentIndex(window._game.findData("fallout4"))
    assert window._list.count() == 0 and "Now browsing Fallout 4" in window._status.text()


def test_another_game_than_the_instances_can_be_browsed_but_not_downloaded(make_window, net, tmp_path):
    add_fine(net)
    window = make_window("skyrimspecialedition")
    window._game.setCurrentIndex(window._game.findData("fallout4"))
    window.search("fine")
    settle(window)
    assert {h["x-bnet-product"] for u, h in net.calls if "api.bethesda.net" in u} == {"fallout4"}
    pick(window, 743724)
    assert not window._download_button.isEnabled()
    assert "Downloading is off for Fallout 4" in window._note.text() and "Skyrim Special Edition" in window._note.text()
    assert visible(window, window._note) and window._d_local.text() == ""
    window.download_selected()
    window.download_required()
    settle(window)
    assert not net.downloads() and not (tmp_path / "dl").exists()
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert window._download_button.isEnabled() and not visible(window, window._note)


def test_an_instance_of_another_game_can_browse_but_never_download(make_window, net):
    add_fine(net)
    window = make_window(None)
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert window._list.count() == 1 and not window._download_button.isEnabled()
    assert "doesn't manage Fallout 4 or Skyrim Special Edition" in window._note.text()
    window.download_selected()
    assert not net.downloads()


def test_no_downloads_folder_turns_downloading_off(make_window, net):
    add_fine(net)
    window = make_window("fallout4", downloads=False)
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert not window._download_button.isEnabled() and "did not give a Downloads folder" in window._note.text()


# ---------------------------------------------------------------- words

@pytest.mark.parametrize("status", ["NOT_FREE", "FREE_UNKNOWN", "UNAVAILABLE", "NO_PC_FILE", "NOT_FOUND", "DOWNLOAD_MISMATCH", "TOO_BIG",
                                    "UNSAFE_CKM", "INVALID_CKM", "UNSUPPORTED_CKM", "UNEXPECTED_HOST", "CANCELLED", "TARGET_EXISTS",
                                    "CREATIONS_ERROR", "SOMETHING_NEW"])
def test_every_status_has_plain_words_that_leak_nothing(status):
    text = texts.describe_error(status, f"boom {KEY} https://x/y?sig=SIGNED-SECRET")
    assert text and text.endswith(".") and KEY not in text and "sig=" not in text and "SIGNED-SECRET" not in text
    assert status != "CREATIONS_ERROR" or "may be down or may have changed" in text
    assert "report it" in texts.describe_error("SOMETHING_NEW")


def test_small_text_helpers():
    assert texts.human_bytes(None) == "unknown size" and texts.human_bytes(10) == "10 bytes"
    assert texts.human_bytes(1536) == "1.5 KB" and texts.human_bytes(5 * 1024 ** 2) == "5.0 MB"
    assert [texts.looks_like_reference(t) for t in ("743724", UUID, "creations.bethesda.net/en/fallout4/details/x", "fine mod", "", "2077 mod")] == [
        True, True, True, False, False, False]
    assert [texts.instance_game_key(n) for n in ("Fallout4", "SkyrimSE", "SkyrimVR", "Fallout4VR", "", None, "Oblivion")] == [
        "fallout4", "skyrimspecialedition", None, None, None, None, None]
    assert texts.state_label({"state": "none"}) == "" and texts.state_label(None) == ""
    assert texts.state_label({"state": "update"}) == "Update available"
    assert texts.state_label({"state": "downloaded", "meta_installed": False}) == "Downloaded"
    assert texts.state_label({"state": "downloaded", "meta_installed": True}) == "Installed"


# ---------------------------------------------------------------- the MO2 tool plugin (with a fake mobase)

class FakeOrganizer:
    def __init__(self, short="Fallout4", downloads=None, enabled=True):
        self.short, self.downloads, self.enabled = short, downloads, enabled
        self.disabled_callbacks = []
        self.mod_installed_callbacks = []

    def modList(self):
        return types.SimpleNamespace(onModInstalled=lambda callback: self.mod_installed_callbacks.append(callback) or True)

    def managedGame(self):
        return types.SimpleNamespace(gameShortName=lambda: self.short)

    def downloadsPath(self):
        return str(self.downloads)

    def isPluginEnabled(self, name):
        return self.enabled

    def onPluginDisabled(self, name, callback):
        self.disabled_callbacks.append((name, callback))
        return True


@pytest.fixture
def plugin_module(qapp, monkeypatch):
    fake = types.ModuleType("mobase")

    class IPlugin:
        pass

    class IPluginTool(IPlugin):
        def __init__(self):
            self.parent = None

        def _parentWidget(self):
            return self.parent

    class VersionInfo:
        def __init__(self, *parts):
            self.parts = parts

        def __str__(self):
            return ".".join(str(p) for p in self.parts)

    fake.IPlugin, fake.IPluginTool, fake.VersionInfo = IPlugin, IPluginTool, VersionInfo
    monkeypatch.setitem(sys.modules, "mobase", fake)
    monkeypatch.setattr(CreationsWindow, "AUTO_BROWSE", False)  # the tests that open the list on show turn it on
    monkeypatch.delitem(sys.modules, "creations_browser.plugin", raising=False)
    module = importlib.import_module("creations_browser.plugin")
    yield module
    sys.modules.pop("creations_browser.plugin", None)


def test_the_package_entry_point_builds_the_tool_plugin(plugin_module):
    import creations_browser
    plugin = creations_browser.createPlugin()
    assert isinstance(plugin, plugin_module.CreationsBrowserPlugin)
    assert plugin.name() == plugin.displayName() == "Creations Browser" and plugin.author() == "Gwyn"
    assert str(plugin.version()) == creations_browser.__version__ == "1.1.0"
    assert "download the free ones" in plugin.description() and "nofficial" in plugin.description()
    assert "download the free ones" in plugin.tooltip() and "unofficial" in plugin.tooltip()
    assert plugin.settings() == [] and plugin.icon() is not None


def test_display_opens_one_window_for_the_instance_game_and_reuses_it(plugin_module, tmp_path, net, monkeypatch):
    monkeypatch.setattr(CreationsWindow, "AUTO_BROWSE", True)
    popularity_set(net, n=130)
    organizer = FakeOrganizer("SkyrimSE", tmp_path / "dl")
    plugin = plugin_module.createPlugin()
    assert plugin.init(organizer) is True
    assert [name for name, _ in organizer.disabled_callbacks] == ["Creations Browser"]
    assert plugin._window is None and net.calls == []
    plugin.display()
    window = plugin._window
    try:
        assert window is not None and window.isVisible() and window.picked_game() == "skyrimspecialedition"
        assert window._instance == "skyrimspecialedition" and window._downloads == tmp_path / "dl"
        assert net.calls == [] or window.busy()  # nothing before display(); the window's own first browse after it
        settle(window)
        assert {h["x-bnet-product"] for u, h in net.calls if "/content?" in u} == {"skyrim"} and window._list.count() == 50
        net.calls.clear()
        window.hide()
        plugin.display()  # opening it again shows the same rows and asks Bethesda nothing
        settle(window)
        assert plugin._window is window and window.isVisible()
        assert net.calls == []
    finally:
        window.shutdown()
        window.close()


def test_display_parents_the_window_to_mo2s_main_window(plugin_module, qapp, tmp_path):
    from PyQt6.QtWidgets import QWidget
    main = QWidget()
    plugin = plugin_module.createPlugin()
    plugin.parent = main
    plugin.init(FakeOrganizer("Fallout4", tmp_path))
    plugin.display()
    try:
        assert plugin._window.parentWidget() is main and plugin._window.isWindow()
    finally:
        plugin._window.shutdown()
        plugin._window.close()
        plugin._window.deleteLater()
        main.deleteLater()


def test_an_instance_of_an_unsupported_game_still_opens_for_browsing(plugin_module, tmp_path):
    plugin = plugin_module.createPlugin()
    plugin.init(FakeOrganizer("SkyrimVR", tmp_path))
    plugin.display()
    try:
        assert plugin._window._instance is None and not plugin._window._download_button.isEnabled()
        assert "doesn't manage" in plugin._window._note.text()
    finally:
        plugin._window.shutdown()
        plugin._window.close()


def test_the_off_switch_is_mo2s_enabled_setting(plugin_module, tmp_path, net):
    organizer = FakeOrganizer("Fallout4", tmp_path)
    plugin = plugin_module.createPlugin()
    plugin.init(organizer)
    organizer.enabled = False
    plugin.display()
    assert plugin._window is None and net.calls == []  # a disabled plugin opens nothing
    organizer.enabled = True
    plugin.display()
    window = plugin._window
    assert window.isVisible()
    (_, on_disabled), = organizer.disabled_callbacks
    on_disabled()  # MO2 reports the plugin was switched off: the window closes
    assert not window.isVisible()


def test_display_never_throws_into_mo2(plugin_module, tmp_path, caplog):
    class Broken(FakeOrganizer):
        def managedGame(self):
            raise RuntimeError("no game")

        def downloadsPath(self):
            raise RuntimeError("no downloads")

    plugin = plugin_module.createPlugin()
    plugin.init(Broken())
    plugin.display()  # facts that cannot be read just turn downloading off
    try:
        assert plugin._window._instance is None and plugin._window._downloads is None
    finally:
        plugin._window.shutdown()
        plugin._window.close()
    plugin._window = None
    plugin._instance_facts = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    plugin.display()
    assert plugin._window is None


# ---------------------------------------------------------------- review fixes

def gated_downloads(monkeypatch, chunk=8):
    """Make the download host hand out its bytes slowly: the first read passes, later reads wait for ``gate``."""
    monkeypatch.setattr(core, "CHUNK", chunk)
    started, gate = threading.Event(), threading.Event()
    real = core._open

    class Gated(Response):
        def read(self, n=-1):
            if started.is_set():
                gate.wait(10)
            started.set()
            return super().read(n)

    monkeypatch.setattr(core, "_open", lambda request, timeout: (Gated(real(request, timeout).read()) if "ugcmods.bethesda.net" in request.full_url
                                                                  else real(request, timeout)))
    return started, gate


def test_progress_updates_are_not_logged_and_every_log_line_is_a_whole_message(make_window, net, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="creations_browser")
    monkeypatch.setattr(core, "CHUNK", 8)
    add_fine(net)
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_selected()
    settle(window)
    messages = [r.getMessage() for r in caplog.records]
    assert not any(m.startswith("status: Downloading") for m in messages), messages  # many chunks, no per-chunk lines
    assert any(m.startswith("status: Saved to Downloads") for m in messages)  # but the end result is logged
    assert all(not r.args for r in caplog.records)  # MO2's handler prints record.msg only: nothing may wait in args
    assert KEY not in caplog.text and "SIGNED-SECRET" not in caplog.text and "sig=" not in caplog.text


def test_an_unexpected_error_is_logged_with_its_traceback_and_without_secrets(make_window, net, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger="creations_browser")

    def boom(*args, **kwargs):
        raise ValueError("odd https://ugcmods.bethesda.net/files/x.ckm?sig=SIGNED-SECRET")

    monkeypatch.setattr(core, "lookup_collect", boom)
    window = make_window()
    window.search("fine")
    settle(window)
    assert "Something went wrong inside the plugin" in window._status.text()
    assert "Traceback" in caplog.text and "ValueError" in caplog.text and "search failed unexpectedly" in caplog.text
    assert "SIGNED-SECRET" not in caplog.text and all(not r.args for r in caplog.records)


def test_a_preview_the_user_moved_past_is_not_decoded(net, monkeypatch):
    from creations_browser import window as window_module
    net.add(api_item(preview_image={"s3bucket": "ugcmods.bethesda.net", "s3key": PREVIEW_KEY, "file_type": "png"}))
    net.files["/" + PREVIEW_KEY] = png_bytes()
    detail = core.lookup_detail("fallout4", 743724)
    from PyQt6.QtCore import QSize

    class NoDecoding:
        def __init__(self, *a, **k):
            raise AssertionError("a stale preview must not be decoded")

    monkeypatch.setattr(window_module, "QImageReader", NoDecoding)
    before = len(net.calls)
    assert window_module._preview_job(detail, QSize(100, 60), lambda: False) is None
    assert len(net.calls) == before  # not even fetched
    answers = iter([True, False])  # current when it starts, stale once the picture has arrived
    assert window_module._preview_job(detail, QSize(100, 60), lambda: next(answers)) is None
    assert len(net.calls) == before + 1  # fetched, never decoded


def test_a_preview_is_decoded_straight_to_display_size_and_huge_ones_are_refused(net):
    from creations_browser import window as window_module
    from PyQt6.QtCore import QSize
    net.add(api_item(preview_image={"s3bucket": "ugcmods.bethesda.net", "s3key": PREVIEW_KEY, "file_type": "png"}))
    detail = core.lookup_detail("fallout4", 743724)
    net.files["/" + PREVIEW_KEY] = png_bytes()  # 32 x 18
    small = window_module._preview_job(detail, QSize(16, 16), lambda: True)
    assert small is not None and small.width() == 16 and small.height() == 9  # scaled while decoding, aspect kept
    same = window_module._preview_job(detail, QSize(200, 200), lambda: True)
    assert (same.width(), same.height()) == (32, 18)  # never enlarged
    assert window_module.PREVIEW_MAX_PIXELS == 16_000_000
    image = QImage(4100, 4100, QImage.Format.Format_Mono)  # 16.8 million pixels, a tiny file
    image.fill(0)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    net.files["/" + PREVIEW_KEY] = bytes(buffer.data())
    assert window_module._preview_job(detail, QSize(16, 16), lambda: True) is None


def test_selecting_another_creation_hides_the_old_preview(make_window, net):
    net.add(api_item(preview_image={"s3bucket": "ugcmods.bethesda.net", "s3key": PREVIEW_KEY, "file_type": "png"}))
    net.add(api_item(cid=2, uuid=UUID_A, title="Fine Other"))
    net.files["/" + PREVIEW_KEY] = png_bytes()
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert visible(window, window._preview)
    pick(window, 2)
    assert not visible(window, window._preview)


def test_holding_enter_does_not_repeat_a_search_that_is_still_on_its_way(make_window, net, monkeypatch):
    add_fine(net)
    release = threading.Event()
    real = core._open
    searches = []

    def slow(request, timeout):
        if "text=fine" in request.full_url:
            searches.append(request.full_url)
            release.wait(10)
        return real(request, timeout)

    monkeypatch.setattr(core, "_open", slow)
    window = make_window()
    window._query.setText("fine")
    for _ in range(6):
        window._query.returnPressed.emit()
    wait_for(lambda: len(searches) >= 1)
    window._search_button.click()
    release.set()
    settle(window)
    assert len(searches) == 1 and window._list.count() == 1
    window.start_search()  # once it has finished the same search may be run again
    settle(window)
    assert len(searches) == 2


def test_a_failed_search_can_be_repeated(make_window, net):
    window = make_window()
    window.search("nothing like this")
    settle(window)
    assert window._status.text() == "No Creations found."
    net.calls.clear()
    window.search("nothing like this")
    settle(window)
    assert any("text=nothing" in u for u, _ in net.calls)


def test_closing_the_window_never_waits_for_the_download_thread(make_window, net, tmp_path, monkeypatch):
    add_fine(net)
    started, gate = gated_downloads(monkeypatch)
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_selected()
    wait_for(started.is_set)
    began = time.monotonic()
    window.close()
    window.shutdown()
    window.shutdown()
    assert time.monotonic() - began < 0.5  # the job is still blocked on its read; nothing here waits for it
    assert window._current is not None and not window._download_button.isEnabled()  # one download at a time, still
    gate.set()
    settle(window)
    assert list((tmp_path / "dl").iterdir()) == [] and window._current is None
    assert window._status.text() == "Download cancelled. Nothing was saved."


def test_a_job_that_outlives_the_window_does_not_touch_it(net, tmp_path, monkeypatch, qapp):
    from PyQt6 import sip
    add_fine(net)
    monkeypatch.setattr(CreationsWindow, "DETAIL_DELAY_MS", 0)
    started, gate = gated_downloads(monkeypatch)
    window = CreationsWindow(None, "fallout4", tmp_path / "dl")
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_selected()
    wait_for(started.is_set)
    threading.Timer(0.3, gate.set).start()  # the blocked read returns while the window is being deleted
    window.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)  # its widgets and its signal object are gone
    assert sip.isdeleted(window)
    # the job went on reading chunks and reporting progress into the deleted window; an exception escaping a worker would
    # abort the process (PyQt's default), so reaching this line with the finished file means it never touched a dead widget
    assert len(list((tmp_path / "dl").glob("*.zip"))) == 1


def test_a_direct_link_does_not_offer_requirements_that_are_already_downloaded(make_window, net, tmp_path):
    net.add(full_item())
    net.add(api_item(cid=1001, uuid=UUID_A, title="Base Library"))
    net.add(api_item(cid=1002, uuid=UUID_B, title="Second Library"))
    core.fetch(tmp_path / "dl", core.plan("fallout4", 1001))  # one requirement is already in Downloads
    window = make_window()
    window.search("743724")  # an id: the direct path
    settle(window)
    assert window._detail is not None and core.numeric_id(window._detail["content_id"]) == 743724
    assert window._pending_required() == [(1002, "Second Library")]
    assert "Base Library: Free, downloaded" in window._d_required.text() and "Second Library: Free" in window._d_required.text()
    window.search("fine")  # the same state as the list path gives
    settle(window)
    pick(window, 743724)
    assert window._pending_required() == [(1002, "Second Library")]


def test_console_only_creations_are_marked_in_the_list(make_window, net):
    net.add(api_item(cid=1, uuid=UUID_A, title="Fine Console Thing", hardware_platforms=["XBOXONE", "XBOXSERIESX"]))
    add_fine(net)
    window = make_window()
    window._pc_only.setChecked(False)  # the PC filter would leave the console-only Creation out
    window.search("fine")
    settle(window)
    got = {r["title"]: r["marker"] for r in rows(window)}
    assert got == {"Fine Console Thing": "No PC version", "Fine Mod: Revamped": "Free"}
    assert window._status.text() == "Found 2 Creations (free)."
    assert any("No PC version" in window._list.item(i).text() for i in range(2))
    pick(window, 1)
    assert not window._download_button.isEnabled()
    assert "No PC version: not available here" in window._d_avail.text()


def test_the_release_notes_tab_lists_a_repeated_note_once(make_window, net):
    note = {"note": "Fixed the thing", "version_name": "1.04"}
    net.add(api_item(release_notes=[{"hardware_platform": "WINDOWS", "release_notes": [note, dict(note), dict(note)]}]))
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert window._notes.toPlainText() == "1.04\nFixed the thing"


# ---------------------------------------------------------------- the plugin glue (fake mobase)

def test_on_disabled_never_raises_into_mo2(plugin_module, tmp_path, caplog):
    plugin = plugin_module.createPlugin()
    plugin.init(FakeOrganizer("Fallout4", tmp_path))

    class Exploding:
        def shutdown(self):
            raise RuntimeError("boom ?sig=SIGNED-SECRET")

        def close(self):
            raise RuntimeError("never reached")

    plugin._window = Exploding()
    plugin._on_disabled()  # must not raise
    assert "Traceback" in caplog.text and "SIGNED-SECRET" not in caplog.text
    plugin._window = None
    plugin._on_disabled()


class FakeMod:
    def __init__(self, repository="Bethesda.net", url="", installation=""):
        self._repository, self._url, self._installation = repository, url, installation
        self.set = []

    def repository(self):
        return self._repository

    def url(self):
        return self._url

    def installationFile(self):
        return self._installation

    def setUrl(self, url):
        self.set.append(url)


def test_an_installed_creation_gets_its_creations_page_as_the_mods_web_page(plugin_module, net, tmp_path):
    net.add(api_item())
    dl = tmp_path / "dl"
    got, _, _ = core.fetch(dl, core.plan("fallout4", 743724))
    organizer = FakeOrganizer("Fallout4", dl)
    plugin = plugin_module.createPlugin()
    assert plugin.init(organizer) is True
    assert len(organizer.mod_installed_callbacks) == 1
    page = f"https://creations.bethesda.net/en/fallout4/details/{UUID}"
    callback = organizer.mod_installed_callbacks[0]
    mod = FakeMod(installation=got.name)  # MO2 stores just the file name for a file in Downloads
    callback(mod)
    assert mod.set == [page]
    absolute = FakeMod(installation=str(got))
    callback(absolute)
    assert absolute.set == [page]
    for other in (FakeMod(repository="Nexus", installation=got.name), FakeMod(url="https://example.com", installation=got.name),
                  FakeMod(installation="not ours.zip"), FakeMod(installation=""), FakeMod(installation="../" + got.name)):
        callback(other)
        assert other.set == [], vars(other)
    # a .meta that names some other address is not trusted
    meta = Path(str(got) + ".meta")
    meta.write_bytes(meta.read_bytes().replace(b"https://creations.bethesda.net/", b"https://example.com/"))
    evil = FakeMod(installation=got.name)
    callback(evil)
    assert evil.set == []
    organizer.enabled = False
    callback(FakeMod(installation=got.name))  # switched off: does nothing
    broken = FakeMod(installation=got.name)
    broken.repository = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    callback(broken)  # never raises


# ---------------------------------------------------------------- search, sort and filters (1.1.0)

def uid(i):
    return f"{i:08x}-2222-3333-4444-555555555555"


def bulk(net, n, prefix="College", start=1, **extra):
    """``n`` catalogue items (search results only) with ids ``start``..."""
    for i in range(start, start + n):
        net.items[i] = api_item(cid=i, uuid=uid(i), title=f"{prefix} {i:04d}", **extra)


def content_calls(net):
    """The query of every ``content`` request so far, as dicts."""
    return [{k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(u).query).items()}
            for u, _ in net.calls if urllib.parse.urlsplit(u).path.endswith("/ugcmods/v1/content")]


def category_calls(net):
    return [u for u, _ in net.calls if urllib.parse.urlsplit(u).path.endswith("/ugcmods/v1/categories")]


def titles(window):
    return [r["title"] for r in rows(window)]


def choose(window, combo, key):
    combo.setCurrentIndex(combo.findData(key))
    settle(window)


def sort_enabled(window):
    model = window._sort.model()
    return {window._sort.itemData(i): model.item(i).isEnabled() for i in range(window._sort.count())}


def stats_set(net):
    """Four Creations with different numbers, so every sort gives its own order. ids 1..4."""
    from fakes import make_stats
    spec = [  # title, PC downloads, all downloads, likes, subscribes, rating, published, updated
        ("College Delta", 100, 9000, 5, 50, 2, 1000, 5000),
        ("college Alpha", 900, 1000, 50, 10, 0, 3000, 2000),
        ("College Charlie", 500, 600, 20, 90, 1, 2000, 4000),
        ("College Bravo", None, 300, 80, 30, 1, 4000, 1000)]
    for i, (title, pc, total, likes, subs, rating, ptime, utime) in enumerate(spec, start=1):
        net.items[i] = api_item(cid=i, uuid=uid(i), title=title, ptime=ptime, utime=utime,
                                stats=make_stats(pc=pc, total=total, likes=likes, subs=subs, rating=rating))


# ---- the controls at rest

def test_the_new_controls_start_on_the_defaults_and_ask_nothing(make_window, net):
    window = make_window()
    window.show()
    QApplication.processEvents()
    assert window._pc_only.isChecked() and window._free_only.isChecked()
    assert window._category.count() == 1 and window._category.itemText(0) == "All categories"
    assert window._sort.currentData() == "downloads" and window._sort.currentText() == "Most downloaded"
    assert [window._sort.itemText(i) for i in range(window._sort.count())] == [
        "Best match", "Most downloaded", "Trending this week", "Popular this month", "Most popular (all time)", "Most liked",
        "Best rated", "Newest", "Recently updated", "Title A–Z"]
    assert not visible(window, window._more_button)
    assert not visible(window, window._author_label) and not visible(window, window._author_clear)
    assert not window._verified_only.isChecked() and not window._official_only.isChecked()
    window._pc_only.setChecked(False)  # before any search the filters have nothing to re-run
    window._sort.setCurrentIndex(window._sort.findData("newest"))
    assert net.calls == []
    for w in (window._category, window._sort, window._more_button, window._author_button):
        assert w.accessibleName() or w.text()


# ---- browsing (no search words)

def test_an_empty_search_browses_the_most_downloaded_of_the_most_popular(make_window, net):
    stats_set(net)
    window = make_window()
    window.search("")
    settle(window)
    (q,) = content_calls(net)
    assert q == {"product": "fallout4", "page": "1", "size": "100", "sort": "subscriptions", "order": "desc",
                 "time_period": "all_time", "hardware_platforms": "WINDOWS", "marketplace": "false"}
    assert titles(window) == ["College Delta", "college Alpha", "College Charlie", "College Bravo"]  # by total downloads
    assert window._status.text() == "Most downloaded among Bethesda's 4 most popular (PC, free). Showing 4 of 4."
    assert not visible(window, window._more_button)


def test_a_server_sort_browses_one_page_of_50_sorted_by_bethesda(make_window, net):
    stats_set(net)
    window = make_window()
    window._sort.setCurrentIndex(window._sort.findData("popular"))
    window.search("")
    settle(window)
    (q,) = content_calls(net)
    assert q == {"product": "fallout4", "page": "1", "size": "50", "sort": "subscriptions", "order": "desc",
                 "time_period": "all_time", "hardware_platforms": "WINDOWS", "marketplace": "false"}
    assert titles(window) == ["College Charlie", "College Delta", "College Bravo", "college Alpha"]  # by subscribes
    assert window._status.text() == "Showing 4 of 4 (PC, free)."
    assert not visible(window, window._more_button)


@pytest.mark.parametrize("key,params,order", [
    ("popular", ("subscriptions", "desc"), ["College Charlie", "College Delta", "College Bravo", "college Alpha"]),
    ("monthly", ("subscriptions", "desc"), ["College Charlie", "College Delta", "College Bravo", "college Alpha"]),
    ("weekly", ("subscriptions", "desc"), ["College Charlie", "College Delta", "College Bravo", "college Alpha"]),
    ("newest", ("ptime", "desc"), ["College Bravo", "college Alpha", "College Charlie", "College Delta"]),
    ("updated", ("utime", "desc"), ["College Delta", "College Charlie", "college Alpha", "College Bravo"]),
    ("title", ("title", "asc"), ["college Alpha", "College Bravo", "College Charlie", "College Delta"]),
    ("rating", ("rating", "desc"), ["College Delta", "College Charlie", "College Bravo", "college Alpha"]),
])
def test_each_browse_sort_is_a_server_sort(make_window, net, key, params, order):
    stats_set(net)
    window = make_window()
    window.search("")
    settle(window)
    choose(window, window._sort, "title" if key != "title" else "newest")  # so that the choice below is a change
    net.calls.clear()
    choose(window, window._sort, key)
    (q,) = content_calls(net)  # changing the sort in a browse asks Bethesda again, from the first page
    assert (q["sort"], q["order"], q["page"]) == (*params, "1")
    assert q.get("time_period") == {"popular": "all_time", "monthly": "monthly", "weekly": "weekly"}.get(key)
    assert titles(window) == order


def test_show_more_in_a_browse_fetches_the_next_page_of_50(make_window, net):
    bulk(net, 120)
    window = make_window()
    window._sort.setCurrentIndex(window._sort.findData("newest"))  # a server sort: one page at a time
    window.show()
    window.search("")
    settle(window)
    assert window._list.count() == 50 and window._status.text() == "Showing 50 of 120 (PC, free)."
    assert visible(window, window._more_button) and window._more_button.isEnabled()
    window._list.setCurrentRow(3)
    settle(window)
    window._more_button.click()
    assert not window._more_button.isEnabled()  # at once: one request at a time
    settle(window)
    assert window._list.count() == 100 and window._status.text() == "Showing 100 of 120 (PC, free)."
    assert window._list.currentRow() == 3  # the pick is kept
    window._more_button.click()
    settle(window)
    assert window._list.count() == 120 and not visible(window, window._more_button)
    assert window._status.text() == "Showing 120 of 120 (PC, free)."
    assert [(q["page"], q["size"]) for q in content_calls(net)] == [("1", "50"), ("2", "50"), ("3", "50")]
    assert len(set(titles(window))) == 120  # no row twice


def test_show_more_is_dropped_when_a_newer_search_replaces_the_browse(make_window, net, monkeypatch):
    bulk(net, 120)
    window = make_window()
    window._sort.setCurrentIndex(window._sort.findData("newest"))  # a server sort: one page at a time
    window.search("")
    settle(window)
    gate, arrived, real = threading.Event(), threading.Event(), core._open

    def slow(request, timeout):
        if "page=2" in request.full_url:
            arrived.set()
            gate.wait(10)
        return real(request, timeout)

    monkeypatch.setattr(core, "_open", slow)
    window._more_button.click()
    wait_for(arrived.is_set)
    window._free_only.setChecked(False)  # a new search while page 2 is on its way
    gate.set()
    settle(window)
    assert window._list.count() == 50 and window._status.text() == "Showing 50 of 120 (PC)."


# ---- search words: fetched once, sorted here

def test_a_search_by_words_reads_the_matches_and_sorts_them_here(make_window, net):
    stats_set(net)
    window = make_window()
    window.search("college")
    settle(window)
    (q,) = content_calls(net)
    assert q["text"] == "college" and q["size"] == "100" and q["page"] == "1" and "sort" not in q
    assert window._sort.currentData() == "match"
    assert titles(window) == ["College Delta", "college Alpha", "College Charlie", "College Bravo"]  # Bethesda's order
    assert window._status.text() == "Found 4 Creations (PC, free)."
    expected = {"downloads": ["College Delta", "college Alpha", "College Charlie", "College Bravo"],  # total downloads, all platforms
                "likes": ["College Bravo", "college Alpha", "College Charlie", "College Delta"],
                "rating": ["College Delta", "College Bravo", "College Charlie", "college Alpha"],  # rating, then likes
                "newest": ["College Bravo", "college Alpha", "College Charlie", "College Delta"],
                "updated": ["College Delta", "College Charlie", "college Alpha", "College Bravo"],
                "title": ["college Alpha", "College Bravo", "College Charlie", "College Delta"]}
    before = len(net.calls)
    for key, order in expected.items():
        choose(window, window._sort, key)
        assert titles(window) == order, key
    choose(window, window._sort, "match")
    assert titles(window) == ["College Delta", "college Alpha", "College Charlie", "College Bravo"]
    assert len(net.calls) == before  # sorting again never asks Bethesda anything


def test_resorting_keeps_the_selected_creation_and_its_details(make_window, net):
    stats_set(net)
    window = make_window()
    window.search("college")
    settle(window)
    pick(window, 3)
    choose(window, window._sort, "title")
    assert window._list.currentItem() is window._rows[3] and window._d_title.text() == "College Charlie"
    assert window._stack.currentIndex() == 1


def test_the_text_search_caps_at_500_and_says_so(make_window, net):
    bulk(net, 650)
    window = make_window()
    window.show()
    window.search("college")
    settle(window)
    assert [q["page"] for q in content_calls(net)] == ["1", "2", "3", "4", "5"] and len(content_calls(net)) <= 5
    assert window._list.count() == 50
    assert window._status.text() == ("Sorted among the 500 best matches of 650; add words to narrow it. Showing 50 of 500 (PC, free).")
    assert visible(window, window._more_button)
    net.calls.clear()
    for expected in (100, 150):
        window._more_button.click()  # the next 50 come from the results already here
        assert window._list.count() == expected
    choose(window, window._sort, "title")
    assert window._list.count() == 50  # a new sort starts on the first 50
    assert net.calls == []
    for _ in range(9):
        window._more_button.click()
    assert window._list.count() == 500 and not visible(window, window._more_button)
    assert not content_calls(net)  # none of it asked Bethesda anything


def test_show_more_in_a_text_search_reveals_50_more_from_the_cache(make_window, net):
    bulk(net, 130)
    window = make_window()
    window.search("college")
    settle(window)
    assert window._list.count() == 50
    assert window._status.text() == "Found 130 Creations (PC, free). Showing 50 of 130."
    window.show_more()
    window.show_more()
    assert window._list.count() == 130 and window._status.text() == "Found 130 Creations (PC, free)."
    assert not window._more_button.isVisibleTo(window) and len(content_calls(net)) == 2


def test_a_new_search_stops_the_one_still_reading_pages(make_window, net, monkeypatch):
    bulk(net, 450, prefix="Aaa")
    bulk(net, 5, prefix="Bbb", start=1000)
    gate, arrived, real = threading.Event(), threading.Event(), core._open

    def slow(request, timeout):
        if "text=aaa" in request.full_url and "page=2" in request.full_url:
            arrived.set()
            gate.wait(10)
        return real(request, timeout)

    monkeypatch.setattr(core, "_open", slow)
    window = make_window()
    window.search("aaa")
    wait_for(arrived.is_set)
    window.search("bbb")
    gate.set()
    settle(window)
    aaa = [q["page"] for q in content_calls(net) if q.get("text") == "aaa"]
    assert aaa == ["1", "2"]  # page 3 was never asked for
    assert titles(window) == [f"Bbb {i:04d}" for i in range(1000, 1005)] and window._status.text() == "Found 5 Creations (PC, free)."


# ---- filters

def test_each_filter_changes_the_request_and_reruns_the_search(make_window, net):
    bulk(net, 3, categories=["Armor"])
    window = make_window()
    window.search("college")
    settle(window)
    assert [(q["hardware_platforms"], q["marketplace"]) for q in content_calls(net)] == [("WINDOWS", "false")]
    net.calls.clear()
    window._pc_only.setChecked(False)
    settle(window)
    (q,) = content_calls(net)
    assert "hardware_platforms" not in q and q["marketplace"] == "false" and q["text"] == "college"
    assert window._status.text() == "Found 3 Creations (free)."
    net.calls.clear()
    window._free_only.setChecked(False)
    settle(window)
    (q,) = content_calls(net)
    assert "marketplace" not in q and "hardware_platforms" not in q
    assert window._status.text() == "Found 3 Creations."
    window._pc_only.setChecked(True)
    window._free_only.setChecked(True)
    settle(window)
    net.calls.clear()
    choose(window, window._category, "Armor")
    (q,) = content_calls(net)
    assert q["categories"] == "Armor" and q["hardware_platforms"] == "WINDOWS"
    assert window._status.text() == "Found 3 Creations (PC, free, Armor)."
    net.calls.clear()
    window._category.setCurrentIndex(0)
    settle(window)
    assert "categories" not in content_calls(net)[-1]


def test_the_pc_filter_leaves_console_only_creations_out(make_window, net):
    net.add(api_item(cid=1, uuid=UUID_A, title="Fine Console Thing", hardware_platforms=["XBOXONE"]))
    add_fine(net)
    window = make_window()
    window.search("fine")
    settle(window)
    assert titles(window) == ["Fine Mod: Revamped"]
    window._pc_only.setChecked(False)
    settle(window)
    assert sorted(titles(window)) == ["Fine Console Thing", "Fine Mod: Revamped"]


def test_a_store_item_is_left_out_by_the_free_filter_and_the_marker_stays_honest_without_it(make_window, net):
    net.add(api_item(cid=1, uuid=UUID_A, title="Fine Store Thing", marketplace=True))
    net.add(api_item(cid=2, uuid=UUID_B, title="Fine Priced Thing", price="300"))  # not a store item, but not free either
    add_fine(net)
    window = make_window()
    window.search("fine")
    settle(window)
    got = {r["title"]: r["marker"] for r in rows(window)}
    assert got == {"Fine Priced Thing": "Get it in game", "Fine Mod: Revamped": "Free"}  # our own free check has the last word
    window._free_only.setChecked(False)
    settle(window)
    assert {r["title"]: r["marker"] for r in rows(window)}["Fine Store Thing"] == "Get it in game"
    pick(window, 1)
    assert not window._download_button.isEnabled()


def test_filter_changes_keep_a_browse_a_browse(make_window, net):
    stats_set(net)
    window = make_window()
    window.search("")
    settle(window)
    net.calls.clear()
    window._pc_only.setChecked(False)
    settle(window)
    (q,) = content_calls(net)
    assert "text" not in q and q["sort"] == "subscriptions" and "hardware_platforms" not in q
    assert window._status.text() == "Most downloaded among Bethesda's 4 most popular (free). Showing 4 of 4."


# ---- categories

def test_categories_are_fetched_once_per_game_and_fill_the_picker(make_window, net):
    window = make_window()
    assert category_calls(net) == []  # nothing until the first search or the first time the list is opened
    window.search("college")
    settle(window)
    assert len(category_calls(net)) == 1 and "product=fallout4" in category_calls(net)[0]
    assert [window._category.itemText(i) for i in range(window._category.count())] == [
        "All categories", "Armor", "Q&A Things", "Quests", "Weapons"]
    window._category.opening.emit()  # opening the list later asks nothing again
    window.search("other")
    settle(window)
    window.search("")
    settle(window)
    assert len(category_calls(net)) == 1
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    assert window._category.count() == 1 and window._category.currentData() is None  # the other game's names are not shown
    window._category.opening.emit()
    settle(window)
    assert len(category_calls(net)) == 2 and "product=skyrim" in category_calls(net)[1]
    assert [window._category.itemText(i) for i in range(window._category.count())] == ["All categories", "Armor", "Quests"]
    window._game.setCurrentIndex(window._game.findData("fallout4"))
    assert window._category.count() == 5 and len(category_calls(net)) == 2  # remembered


def test_opening_the_category_list_fetches_it_before_any_search(make_window, net):
    window = make_window()
    window._category.opening.emit()
    settle(window)
    assert len(category_calls(net)) == 1 and window._category.count() == 5
    window.search("college")
    settle(window)
    assert len(category_calls(net)) == 1


def test_a_failed_category_fetch_leaves_only_all_categories_and_no_message(make_window, net):
    net.categories_fail = True
    bulk(net, 3)
    window = make_window()
    window.search("college")
    settle(window)
    assert window._category.count() == 1 and window._status.text() == "Found 3 Creations (PC, free)."
    assert len(category_calls(net)) == 1
    window._category.opening.emit()  # asked once per game: no retry
    window.search("")
    settle(window)
    assert len(category_calls(net)) == 1 and window._category.count() == 1


def test_a_chosen_category_survives_a_search_and_resets_with_the_game(make_window, net):
    bulk(net, 3, categories=["Armor"])
    window = make_window()
    window.search("college")
    settle(window)
    choose(window, window._category, "Armor")
    window.search("college")
    settle(window)
    assert window._category.currentData() == "Armor" and content_calls(net)[-1]["categories"] == "Armor"
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    assert window._category.currentData() is None


# ---- rows and details: the numbers

def test_numbers_and_dates_are_short_and_the_same_everywhere():
    assert [texts.compact_number(n) for n in (0, 7, 999, 1000, 1234, 41500, 120000, 999949, 999950, 3400000, 1300000000)] == [
        "0", "7", "999", "1K", "1.2K", "41.5K", "120K", "999.9K", "1M", "3.4M", "1.3B"]
    assert texts.compact_number(None) == "" and texts.compact_number(-5) == ""
    assert texts.short_date(1741392000) == "8 Mar 2025" and texts.short_date(1735689600) == "1 Jan 2025"
    assert texts.short_date(None) == "" and texts.short_date(0) == "" and texts.short_date(10 ** 18) == ""
    assert [texts.rating_word({"rating": r}) for r in (2, 1, 0, None)] == ["Very positive", "Positive", "", ""]
    row = {"downloads": 41500, "likes": 120, "updated": 1741392000}
    assert texts.row_stats(row) == "41.5K downloads  ·  120 likes  ·  updated 8 Mar 2025"
    assert texts.row_stats({"downloads": 1, "likes": 1}) == "1 download  ·  1 like"
    assert texts.row_stats({"downloads": None, "likes": None, "updated": None}) == "" and texts.row_stats({}) == ""
    assert texts.filter_summary(True, True) == "PC, free" and texts.filter_summary(False, False) == ""
    assert texts.filter_summary(True, False, "Armor", "Bob") == "PC, Armor, by Bob"


def test_a_row_shows_the_author_marker_and_stats(make_window, net):
    from fakes import make_stats
    net.add(api_item(stats=make_stats(pc=41500, total=60000, likes=120, subs=9, rating=2), utime=1741392000))
    window = make_window()
    window.search("fine")
    settle(window)
    (row,) = rows(window)
    assert row["stats"] == "60K downloads  ·  120 likes  ·  updated 8 Mar 2025"
    assert window._list.item(0).text() == ("Fine Mod: Revamped, Free, by Someone, 60K downloads, 120 likes, updated 8 Mar 2025")
    window.show()
    window.grab()  # the delegate paints with the stats
    assert window._list.itemDelegate()._lines(window._list.model().index(0, 0)) == (
        "Fine Mod: Revamped", "Free", "by Someone  ·  60K downloads  ·  120 likes  ·  updated 8 Mar 2025")


def test_a_row_without_a_pc_figure_uses_the_total_and_one_without_stats_shows_none(make_window, net):
    from fakes import make_stats
    net.add(api_item(cid=1, uuid=UUID_A, title="Fine Console Only", stats=make_stats(pc=None, total=2500, likes=0)))
    bare = api_item(cid=2, uuid=UUID_B, title="Fine Bare")
    del bare["stats"], bare["utime"]
    net.add(bare)
    window = make_window()
    window.search("fine")
    settle(window)
    got = {r["title"]: r["stats"] for r in rows(window)}
    assert got == {"Fine Console Only": "2.5K downloads  ·  0 likes  ·  updated 9 Mar 2024", "Fine Bare": ""}


def test_the_details_show_the_stats_dates_and_categories(make_window, net):
    from fakes import make_stats
    net.add(api_item(stats=make_stats(pc=41500, total=52100, likes=120, subs=9, rating=2), ptime=1735689600, utime=1741392000,
                     categories=["Armor", "Quests"]))
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert window._d_stats.text() == "41.5K PC downloads  ·  52.1K downloads on all platforms  ·  120 likes  ·  Very positive"
    assert window._d_dates.text() == "Published 1 Jan 2025  ·  Updated 8 Mar 2025  ·  Categories: Armor, Quests"
    assert visible(window, window._d_stats) and visible(window, window._d_dates)
    net.items[743724] = api_item(stats=make_stats(pc=None, total=1, likes=1, rating=1), ptime=0, utime=0, categories=[], first_ptime=0)
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert window._d_stats.text() == "1 download  ·  1 like  ·  Positive"
    assert window._d_dates.text() == "" and not visible(window, window._d_dates)
    net.items[743724] = api_item(stats=make_stats(pc=5, total=5, likes=0, rating=0))
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert window._d_stats.text() == "5 downloads  ·  0 likes"  # equal figures are shown once; a LOW rating says nothing


# ---- More by this author

def test_more_by_this_author_browses_that_authors_creations(make_window, net):
    bulk(net, 3, prefix="Mine", author_displayname="Alice")
    bulk(net, 4, prefix="Theirs", start=10, author_displayname="Bob")
    window = make_window()
    window.show()
    window.search("mine")
    settle(window)
    assert titles(window) == ["Mine 0001", "Mine 0002", "Mine 0003"]
    pick(window, 2)
    assert visible(window, window._author_button) and window._author_button.text() == "More by this &author"
    net.calls.clear()
    window._author_button.click()
    settle(window)
    (q,) = content_calls(net)
    assert q["author_displayname"] == "Alice" and "text" not in q and q["hardware_platforms"] == "WINDOWS" and q["marketplace"] == "false"
    assert q["sort"] == "subscriptions" and window._query.text() == ""
    assert titles(window) == ["Mine 0001", "Mine 0002", "Mine 0003"]
    assert visible(window, window._author_label) and window._author_label.text() == "Author: Alice"
    assert window._status.text() == "Most downloaded among Bethesda's 3 most popular (PC, free, by Alice). Showing 3 of 3."
    window._pc_only.setChecked(False)  # a filter change keeps the author
    settle(window)
    assert content_calls(net)[-1]["author_displayname"] == "Alice" and "hardware_platforms" not in content_calls(net)[-1]
    window.search("theirs")  # a new search forgets it
    settle(window)
    assert "author_displayname" not in content_calls(net)[-1] and not visible(window, window._author_label)
    assert titles(window) == [f"Theirs {i:04d}" for i in range(10, 14)]
    pick(window, 11)
    window._author_button.click()
    settle(window)
    assert titles(window) == [f"Theirs {i:04d}" for i in range(10, 14)] and visible(window, window._author_clear)
    window._author_clear.click()  # "Show all authors"
    settle(window)
    assert "author_displayname" not in content_calls(net)[-1] and window._list.count() == 7
    assert not visible(window, window._author_label)


def test_the_author_button_is_hidden_without_a_usable_author_name(make_window, net):
    net.add(api_item(author_displayname=""))
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert not visible(window, window._author_button)
    window.more_by_author()  # and pressing it anyway does nothing
    assert len(content_calls(net)) == 1


# ---- the pasted reference still works beside all this

def test_a_pasted_reference_ignores_the_filters_and_shows_one_creation(make_window, net):
    net.add(api_item(hardware_platforms=["XBOXONE"], price="9"))
    window = make_window()
    window.search("743724")
    settle(window)
    assert titles(window) == ["Fine Mod: Revamped"] and window._status.text() == "Found 1 Creation."
    assert not content_calls(net) and not window._more_button.isVisibleTo(window)
    choose(window, window._sort, "title")  # sorting one row is harmless
    assert titles(window) == ["Fine Mod: Revamped"]


def test_changing_the_game_clears_the_results_and_a_filter_changed_in_the_pause_joins_the_one_browse(make_window, net):
    bulk(net, 3)
    window = make_window()
    window.search("college")
    settle(window)
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    assert window._list.count() == 0 and not window._more_button.isVisibleTo(window)
    net.calls.clear()
    window._free_only.setChecked(False)  # nothing is on show to re-run: the game's own browse (after its pause) takes it in
    assert content_calls(net) == []
    settle(window)
    (q,) = content_calls(net)
    assert "marketplace" not in q and q["product"] == "skyrim" and window._list.count() == 3
    window.search("college")
    settle(window)
    assert {h["x-bnet-product"] for u, h in net.calls if "/content?" in u} == {"skyrim"}


# ---- review fixes: pause before a filter/sort search, replaced jobs, closing, popup, wording

def block_pool(window):
    """Occupy the window's only worker thread; the returned event lets it go. Jobs submitted meanwhile wait in the queue."""
    window._pool.setMaxThreadCount(1)
    gate, running = threading.Event(), threading.Event()

    def hold():
        running.set()
        gate.wait(10)

    window._pending += 1  # the held job reports back like any other, which takes this count down again
    window._pool.start(window_module._Job(window._signals, 0, "held", hold))
    wait_for(running.is_set)
    return gate


def test_stepping_through_the_combos_in_a_browse_asks_bethesda_once(make_window, net, monkeypatch):
    monkeypatch.setattr(CreationsWindow, "SEARCH_DELAY_MS", 300)
    bulk(net, 60)
    window = make_window()
    window.show()
    window.search("")
    settle(window)
    net.calls.clear()
    for key in [k for k in ("newest", "updated", "rating", "title", "popular") for _ in range(2)]:  # ten quick changes
        window._sort.setCurrentIndex(window._sort.findData(key))
        window._category.setCurrentIndex(0)
    QApplication.processEvents()
    assert content_calls(net) == [] and window._search_timer.isActive()  # nothing yet: every change restarted the pause
    settle(window)
    calls = content_calls(net)
    assert len(calls) == 1 and window._sort.currentData() == "popular"
    assert calls[0]["sort"] == core.SERVER_SORTS["popular"][0]  # only the last choice ran
    assert not window._search_timer.isActive()


def test_search_button_still_searches_at_once_and_ends_a_waiting_pause(make_window, net, monkeypatch):
    monkeypatch.setattr(CreationsWindow, "SEARCH_DELAY_MS", 300)
    bulk(net, 60)
    window = make_window()
    window.search("")
    settle(window)
    net.calls.clear()
    window._sort.setCurrentIndex(window._sort.findData("title"))
    assert window._search_timer.isActive()
    window._query.setText("college")
    window._search_button.click()
    assert not window._search_timer.isActive()  # the search made now covers the waiting change
    settle(window)
    assert len(content_calls(net)) == 1 and content_calls(net)[0]["text"] == "college"


def test_a_sort_change_in_a_text_search_still_sorts_here_at_once(make_window, net, monkeypatch):
    monkeypatch.setattr(CreationsWindow, "SEARCH_DELAY_MS", 300)
    stats_set(net)
    window = make_window()
    window.search("college")
    settle(window)
    net.calls.clear()
    window._sort.setCurrentIndex(window._sort.findData("title"))  # no settle: it must already be done
    assert titles(window) == ["college Alpha", "College Bravo", "College Charlie", "College Delta"]
    assert not window._search_timer.isActive() and net.calls == []


def test_a_replaced_search_waiting_in_the_queue_makes_no_request(make_window, net):
    bulk(net, 60)
    window = make_window()
    window.show()
    gate = block_pool(window)
    window.search("")  # queued behind the held job
    window._pc_only.setChecked(False)
    window._launch()  # replaces it before it ever ran
    gate.set()
    settle(window)
    calls = content_calls(net)
    assert len(calls) == 1 and "hardware_platforms" not in calls[0]


def test_replaced_text_searches_and_reference_lookups_make_no_request(make_window, net):
    bulk(net, 5)
    window = make_window()
    gate = block_pool(window)
    window._query.setText("college")
    window._launch()
    window._query.setText("1")  # an id: its lookup is skipped too once replaced
    window._launch()
    window._query.setText("")
    window._launch()
    gate.set()
    settle(window)
    assert [q.get("text") for q in content_calls(net)] == [None]  # only the last (browse) search asked
    assert not [u for u, _ in net.calls if urllib.parse.urlsplit(u).path.endswith("/ugcmods/v1/content/1")]


def test_a_replaced_show_more_makes_no_request(make_window, net):
    bulk(net, 120)
    window = make_window()
    window._sort.setCurrentIndex(window._sort.findData("newest"))  # a server sort: one page at a time
    window.search("")
    settle(window)
    net.calls.clear()
    gate = block_pool(window)
    window._more_button.click()  # page 2 waits in the queue
    window._free_only.setChecked(False)
    window._launch()  # a new search replaces the browse
    gate.set()
    settle(window)
    assert [q["page"] for q in content_calls(net)] == ["1"]
    assert window._list.count() == 50


def test_show_more_in_a_text_search_never_lists_a_creation_twice(make_window, net):
    bulk(net, 130)
    window = make_window()
    window.search("college")
    settle(window)
    # a result without a usable id is never listed, so the list holds one row fewer than were counted as shown
    window._results.insert(5, {"content_id": None, "title": "No id", "likes": 0})
    window._render()
    assert window._list.count() == 49 and window._shown == 50
    window.show_more()
    assert window._list.count() == 99 and len(set(titles(window))) == 99
    window.show_more()
    assert window._list.count() == 130 and len(set(titles(window))) == 130 and len(window._rows) == 130


def test_closing_the_window_stops_a_text_search_and_lets_it_run_again(make_window, net, monkeypatch):
    bulk(net, 250)
    window = make_window()
    window.show()
    gate, arrived, real = threading.Event(), threading.Event(), core._open

    def slow(request, timeout):
        if "page=2" in request.full_url:
            arrived.set()
            gate.wait(10)
        return real(request, timeout)

    monkeypatch.setattr(core, "_open", slow)
    window.search("college")
    wait_for(arrived.is_set)
    window._pc_only.setChecked(False)  # a change waiting out its pause when the window closes
    assert window._search_timer.isActive()
    window.close()
    assert not window._search_timer.isActive() and not window._detail_timer.isActive()
    assert not window._searching and window._search_key is None
    gate.set()
    settle(window)
    assert [q["page"] for q in content_calls(net)] == ["1", "2"]  # page 3 was never asked for
    net.calls.clear()
    window.show()
    window._pc_only.setChecked(True)
    window.search("college")  # the same search runs again; it is not held back as a duplicate
    settle(window)
    assert content_calls(net)[0]["page"] == "1" and window._list.count() > 0


def test_a_category_list_opened_before_it_was_filled_is_opened_again_once_filled(make_window, net, monkeypatch):
    window = make_window()
    window.show()
    QApplication.processEvents()
    calls = []
    real_show, real_hide = window._category.showPopup, window._category.hidePopup
    monkeypatch.setattr(window._category, "showPopup", lambda: (calls.append("show"), real_show())[1])
    monkeypatch.setattr(window._category, "hidePopup", lambda: (calls.append("hide"), real_hide())[1])
    window._category.showPopup()  # opened first: still only "All categories"
    assert window._category.view().isVisible()
    settle(window)
    assert window._category.count() == 5 and calls == ["show", "hide", "show"]
    window._category.hidePopup()
    calls.clear()
    window._fill_categories()  # a closed picker is refilled quietly
    assert calls == []


def test_the_cap_message_names_the_number_of_results_kept(make_window, net, monkeypatch):
    bulk(net, 650)
    real = core.lookup_collect
    monkeypatch.setattr(core, "lookup_collect", lambda *a, **k: real(*a, cap=150, **k))
    window = make_window()
    window.search("college")
    settle(window)
    assert window._status.text() == "Sorted among the 150 best matches of 650; add words to narrow it. Showing 50 of 150 (PC, free)."



# ---------------------------------------------------------------- review follow-ups: colours, size, sorts, links, words

def enabled(*extra):
    state = QStyle.StateFlag.State_Enabled
    for flag in extra:
        state |= flag
    return state


def synthetic_palette():
    G, R = QPalette.ColorGroup, QPalette.ColorRole
    pal = QPalette()
    for group, highlight, highlighted_text, text, window_text in (
            (G.Active, "#0078d7", "#ffffff", "#000000", "#000000"),
            (G.Inactive, "#d9d9d9", "#ffffff", "#101010", "#202020"),  # pale grey under white text: unreadable
            (G.Disabled, "#cccccc", "#c0c0c0", "#909090", "#101010")):
        pal.setColor(group, R.Highlight, QColor(highlight))
        pal.setColor(group, R.HighlightedText, QColor(highlighted_text))
        pal.setColor(group, R.Text, QColor(text))
        pal.setColor(group, R.WindowText, QColor(window_text))
    return pal


def test_the_row_colour_group_is_chosen_like_qcommonstyle():
    S, G = QStyle.StateFlag, QPalette.ColorGroup
    assert window_module.colour_group(enabled(S.State_Active)) == G.Active
    assert window_module.colour_group(enabled()) == G.Inactive
    assert window_module.colour_group(S.State_Active) == G.Disabled  # not enabled, whatever the window
    assert window_module.colour_group(S.State_Selected) == G.Disabled
    assert window_module.colour_group(enabled(S.State_Active, S.State_Selected)) == G.Active


def test_a_selected_row_uses_the_groups_highlighted_text_unless_it_cannot_be_read():
    S, R = QStyle.StateFlag, QPalette.ColorRole
    pal = synthetic_palette()
    colour = window_module.row_text_colour
    assert colour(pal, enabled(S.State_Active, S.State_Selected)).name() == "#ffffff"  # active: HighlightedText as is
    assert colour(pal, enabled(S.State_Active)).name() == "#000000"  # unselected: Text
    assert colour(pal, enabled(S.State_Selected)).name() == "#101010"  # inactive and unreadable: Text, not white on grey
    assert colour(pal, S.State_Selected).name() == "#101010"  # disabled: WindowText reads best on its Highlight
    pal.setColor(QPalette.ColorGroup.Inactive, R.HighlightedText, QColor("#000000"))
    assert colour(pal, enabled(S.State_Selected)).name() == "#000000"  # readable inactive HighlightedText is kept
    pal.setColor(QPalette.ColorGroup.Inactive, R.Text, QColor("#e0e0e0"))
    pal.setColor(QPalette.ColorGroup.Inactive, R.HighlightedText, QColor("#ffffff"))
    assert colour(pal, enabled(S.State_Selected)).name() == "#202020"  # the better of Text and WindowText
    assert window_module.contrast_ratio(QColor("#000000"), QColor("#ffffff")) == pytest.approx(21.0)
    assert window_module.contrast_ratio(QColor("#777777"), QColor("#777777")) == pytest.approx(1.0)


def test_an_inactive_selected_row_paints_without_errors(make_window, net):
    add_fine(net)
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    window._list.setCurrentRow(0)
    window._list.setPalette(synthetic_palette())
    window.grab()


def test_the_window_is_cut_down_to_a_small_screen(make_window, qapp, monkeypatch):
    em = QFontMetrics(QApplication.font()).height()

    class Screen:
        def availableGeometry(self):
            return QRect(0, 0, em * 50, em * 36)
    monkeypatch.setattr(CreationsWindow, "screen", lambda self: Screen())
    window = make_window()
    margin = CreationsWindow.SCREEN_MARGIN
    assert (window.width(), window.height()) == (em * 50 - margin, em * 36 - margin)  # 64 x 42 em would not fit
    assert (window.minimumWidth(), window.minimumHeight()) == (em * 44, em * 30)
    assert window._fitted(QSize(10 ** 5, 10 ** 5)) == QSize(em * 50 - margin, em * 36 - margin)
    assert window._fitted(QSize(10, 10)) == QSize(em * 44, em * 30)  # never below the minimum
    monkeypatch.setattr(CreationsWindow, "screen", lambda self: Screen2())

    class Screen2:
        def availableGeometry(self):
            return QRect(0, 0, 10 ** 4, 10 ** 4)
    assert make_window().size() == QSize(em * 64, em * 42)  # a big screen leaves the size alone


def test_the_parents_screen_is_the_one_used(qapp, monkeypatch):
    seen = []

    class Screen:
        def availableGeometry(self):
            seen.append("parent")
            return QRect(0, 0, 10 ** 4, 10 ** 4)
    parent = QWidget()
    monkeypatch.setattr(QWidget, "screen", lambda self: Screen() if self is parent else None)
    window = CreationsWindow(parent, "fallout4", None)
    try:
        assert seen
    finally:
        window.shutdown()
        window.deleteLater()
        parent.deleteLater()


def test_the_layout_holds_at_the_smallest_size(make_window, net, qapp):
    net.add(full_item())
    add_requirements(net)
    net.add(api_item(cid=1002, uuid=UUID_B, title="Second Library"))
    window = make_window("skyrimspecialedition")  # the other game's note is on show, the tallest case
    em = QFontMetrics(QApplication.font()).height()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window._preview.show()
    layout = window.layout()
    width = max(em * 44, layout.minimumSize().width())  # (the rows' own width depends on the font; only the height is asked of em)
    assert layout.minimumHeightForWidth(width) <= em * 30
    window.resize(width, em * 30)
    window.show()
    QApplication.processEvents()
    assert (window.width(), window.height()) == (width, em * 30)
    boxes = [w for w in window.findChildren(QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly) if w.isVisible()]
    for i, a in enumerate(boxes):
        assert window.rect().contains(a.geometry()), a
        for b in boxes[i + 1:]:
            assert not a.geometry().intersects(b.geometry()), (a, b)
    assert window._list.width() >= window._list.minimumWidth() and window._stack.width() >= window._stack.minimumWidth()
    assert window._details_scroll.verticalScrollBar().maximum() > 0  # the facts scroll here; they are not squeezed
    window._tabs.setCurrentIndex(0)
    window._description.setPlainText("line\n" * 200)
    assert window._description.verticalScrollBar().maximum() > 0  # and so does the description
    window.resize(em * 80, em * 60)
    QApplication.processEvents()
    assert window._tabs.height() > window._tabs.minimumHeight()  # a large window gives the description the room


def test_the_chosen_sort_stays_when_the_kind_of_search_changes(make_window, net):
    bulk(net, 12)
    window = make_window()
    window.search("")  # browse
    settle(window)
    window._sort.setCurrentIndex(window._sort.findData("title"))
    settle(window)
    assert window._sort_key() == "title" and window._mode == "browse"
    window.search("college")
    settle(window)
    assert window._mode == "text" and window._sort_key() == "title"  # offered by both: kept
    window._sort.setCurrentIndex(window._sort.findData("downloads"))
    window.search("")
    settle(window)
    assert window._mode == "browse" and window._sort_key() == "downloads"  # offered by both: kept
    window._sort.setCurrentIndex(window._sort.findData("newest"))
    settle(window)
    window.search("college")
    settle(window)
    assert window._sort_key() == "newest"
    window._sort.setCurrentIndex(window._sort.findData("likes"))
    window.search("")
    settle(window)
    assert window._sort_key() == "likes"
    window._sort.setCurrentIndex(window._sort.findData("weekly"))  # a popularity sort: not offered with words
    settle(window)
    window.search("college")
    settle(window)
    assert window._mode == "text" and window._sort_key() == "match"
    window._sort.setCurrentIndex(window._sort.findData("match"))  # match is text only too
    window.search("")
    settle(window)
    assert window._mode == "browse" and window._sort_key() == "downloads"  # not offered in a browse: the browse default


def test_the_default_sort_becomes_best_match_when_words_are_searched(make_window, net):
    bulk(net, 5)
    window = make_window()
    window.search("")
    settle(window)
    assert window._sort_key() == "downloads"
    window.search("college")
    settle(window)
    assert window._sort_key() == "match" and window._sort.currentText() == "Best match"
    window._sort.setCurrentIndex(window._sort.findData("title"))
    window.search("")
    settle(window)
    window.search("college")
    settle(window)
    assert window._sort_key() == "title"


def test_a_pasted_link_for_the_other_game_switches_the_game_and_opens_it(make_window, net):
    net.add(api_item())
    window = make_window("fallout4")
    assert window.picked_game() == "fallout4"
    window.search(f"https://creations.bethesda.net/en/skyrim/details/{UUID}/Fine_Mod")
    settle(window)
    assert window.picked_game() == "skyrimspecialedition" and window._game.currentText() == "Skyrim Special Edition"
    assert window._list.count() == 1 and window._d_title.text() == "Fine Mod: Revamped"
    assert window._status.text() == "Found 1 Creation. Switched to Skyrim Special Edition for this link."
    assert {h["x-bnet-product"] for u, h in net.calls if "api.bethesda.net" in u and "content" in u} == {"skyrim"}
    assert not window._download_button.isEnabled() and "Downloading is off for Skyrim Special Edition" in window._note.text()
    assert "Fallout 4" in window._note.text()  # the existing note says which game downloads
    window.search(f"https://creations.bethesda.net/en/fallout4/details/{UUID}")  # and back
    settle(window)
    assert window.picked_game() == "fallout4" and window._download_button.isEnabled()
    window.search(f"https://creations.bethesda.net/en/fallout4/details/{UUID}")  # the same game: no switch, no note
    settle(window)
    assert window._status.text() == "Found 1 Creation."
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    assert window._switched_to is None


def test_a_link_for_the_other_game_is_not_followed_during_a_download(make_window, net, monkeypatch):
    add_fine(net)
    window = make_window("fallout4")
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window._current = (743724, "Fine Mod")  # a download is running: the game picker is locked
    window.search(f"https://creations.bethesda.net/en/skyrim/details/{UUID}")
    settle(window)
    assert window.picked_game() == "fallout4"
    assert window._status.text() == "Check what you typed: That link is for Skyrim Special Edition."
    window._current = None


def test_the_search_button_says_browse_while_the_box_is_empty(make_window, net):
    bulk(net, 3)
    window = make_window()
    button = window._search_button
    assert button.text() == "&Browse" and window._query.text() == ""
    window._query.setText("college")
    assert button.text() == "&Search"
    window._query.setText("   ")
    assert button.text() == "&Browse"  # blanks are not words
    window._query.setText("x")
    window._query.clear()
    assert button.text() == "&Browse"
    button.click()  # it browses, as it always did
    settle(window)
    assert window._list.count() == 3 and window._mode == "browse"
    window._query.setText("college")
    button.click()
    settle(window)
    assert window._mode == "text"
    for text in ("&Browse", "&Search"):  # each wording keeps its mnemonic, and no other control shares its Alt key
        button.setText(text)
        labelled = [w.text() for w in window.findChildren((QLabel, QPushButton, QCheckBox)) if "&" in w.text()]
        keys = [t[t.index("&") + 1].lower() for t in labelled]
        assert len(keys) == len(set(keys)), (text, labelled)


def test_a_status_line_counts_with_thousands_separators(make_window, net):
    bulk(net, 3)
    window = make_window()
    window._sort.setCurrentIndex(window._sort.findData("newest"))  # a server sort: one page at a time
    window.search("")
    settle(window)
    window._total = 12345
    window._report_results()
    assert window._status.text() == "Showing 3 of 12,345 (PC, free)."
    window._result_mode, window._capped = "text", True
    window._results = list(range(1234))
    window._total, window._shown = 5678, 1000
    window._report_results()
    assert window._status.text() == ("Sorted among the 1,234 best matches of 5,678; add words to narrow it. Showing 3 of 1,234 (PC, free).")
    window._capped = False
    window._report_results()
    assert window._status.text() == "Found 1,234 Creations (PC, free). Showing 3 of 1,234."


def test_progress_and_cancel_show_only_while_downloading(make_window, net, tmp_path):
    add_fine(net)
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert not visible(window, window._progress) and not visible(window, window._cancel_button)
    assert window._progress.sizePolicy().retainSizeWhenHidden() and window._cancel_button.sizePolicy().retainSizeWhenHidden()
    height = window._download_button.parentWidget().height()
    window._download_button.click()
    assert visible(window, window._progress) and visible(window, window._cancel_button) and window._cancel_button.isEnabled()
    settle(window)
    assert not visible(window, window._progress) and not visible(window, window._cancel_button)
    assert window._progress.value() == 100  # the figure itself is still readable
    assert visible(window, window._download_button) and window._status.isVisible()
    assert window._download_button.height() > 0 and window._download_button.parentWidget().height() == height


def test_progress_and_cancel_hide_again_after_a_failed_or_cancelled_download(make_window, net, tmp_path):
    item = api_item()
    item["download"][0]["published"][0]["client"]["main"]["size"] += 1
    net.add(item)
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    window.download_selected()
    assert visible(window, window._progress)
    settle(window)
    assert not visible(window, window._progress) and not visible(window, window._cancel_button)


def test_a_row_names_its_marker_first_in_bold(make_window, net):
    add_fine(net)
    net.add(api_item(cid=2, uuid=UUID_A, title="Fine Paid Thing", price="500"))
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    delegate = window._list.itemDelegate()
    lines = {delegate._lines(window._list.model().index(i, 0))[0]: delegate._lines(window._list.model().index(i, 0))[1:]
             for i in range(window._list.count())}
    assert lines["Fine Mod: Revamped"][0] == "Free" and lines["Fine Mod: Revamped"][1].startswith("by Someone")
    assert lines["Fine Paid Thing"][0] == "Get it in game"
    assert "Free" not in lines["Fine Mod: Revamped"][1]
    first = window._list.item(0).text()
    assert first.index("Free") < first.index("by Someone")  # a screen reader hears the marker first too
    image = window._list.viewport().grab().toImage()
    assert not image.isNull()


def test_the_refused_file_is_named_in_words(make_window):
    window = make_window()
    window._download_failed("UNSAFE_CKM", "Data/Scripts/evil.exe isn't a type this plugin accepts")
    assert window._status.text() == ("The download was refused: Data/Scripts/evil.exe isn't a type this plugin accepts. "
                                     "Nothing was saved.")
    window._download_failed("UNSAFE_CKM", "")
    assert window._status.text() == texts.describe_error("UNSAFE_CKM") and "refused" in window._status.text()
    assert texts.describe_error("UNSAFE_CKM", f"boom {KEY}") == texts.describe_error("UNSAFE_CKM")  # a message alone is never shown
    assert KEY not in texts.describe_error("UNSAFE_CKM", f"boom {KEY}")


def test_offline_and_rate_limit_messages(make_window, monkeypatch, net):
    import io
    import urllib.error

    def limited(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(core, "_open", limited)
    window = make_window()
    window.search("fine")
    settle(window)
    assert window._status.text() == "Bethesda's service asked us to slow down. Wait a minute and try again."

    def broken(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(core, "_open", broken)
    window.search("fine two")
    settle(window)
    assert window._status.text() == texts.describe_error("CREATIONS_ERROR") and "may be down" in window._status.text()
    assert texts.describe_error("UNREACHABLE") == "Couldn't reach Bethesda's servers. Check your internet connection and try again."


def test_zero_credit_store_items_say_claim_in_game():
    free_claim = {"credits": 0, "was": None, "store": "STEAM"}
    assert texts.price_text(free_claim, short=True) == "Claim in game (free)"
    assert texts.price_text(free_claim) == "0 Creation Credits (claim it in game)"
    assert texts.marker({"price": free_claim}) == "Claim in game (free)"
    assert texts.availability({"price": free_claim}) == "In Bethesda's Creations store, free to claim. Get it in the game's Creations menu."
    assert texts.price_text({"credits": 400, "was": None}, short=True) == "400 credits"


def test_the_downloads_figure_is_shown_once_when_both_are_the_same():
    assert texts.detail_stats({"downloads_pc": 1_600_000, "downloads_all": 1_600_000}) == "1.6M downloads"
    assert texts.detail_stats({"downloads_pc": None, "downloads_all": 1_600_000}) == "1.6M downloads"
    assert texts.detail_stats({"downloads_pc": 1_600_000, "downloads_all": 2_100_000}) == "1.6M PC downloads  ·  2.1M downloads on all platforms"
    assert texts.detail_stats({"downloads_pc": 1_600_000, "downloads_all": None}) == "1.6M PC downloads"  # the only figure, still labelled
    assert texts.detail_stats({"downloads_pc": 1, "downloads_all": 1, "likes": 1}) == "1 download  ·  1 like"
    assert texts.detail_stats({"downloads_pc": None, "downloads_all": None}) == ""


def test_the_requirement_list_uses_the_short_claim_wording(make_window, net):
    net.add(full_item())
    net.add(api_item(cid=1001, uuid=UUID_A, title="Base Library",
                     catalog=store_listing("STEAM", amount=0)))
    net.add(api_item(cid=1002, uuid=UUID_B, title="Second Library"))
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert "Base Library: Claim in game (free)" in window._d_required.text()


# ---------------------------------------------------------------- the opening list, trending sorts and creator filters

def popularity_set(net, n=1200, hot=517, verified_every=12, official_every=0, **extra):
    """``n`` Creations whose popularity order is their id order. The most downloaded one (``hot``) sits far down that order,
    like the live Unofficial Fallout 4 Patch at rank 517. Every ``verified_every``-th author is verified (and every
    ``official_every``-th official). Likes follow ``id % 7``, so the most liked is id 6 (ties keep popularity order)."""
    from fakes import make_stats
    for i in range(1, n + 1):
        marks = {"author_verified": bool(verified_every) and i % verified_every == 0,
                 "author_official": bool(official_every) and i % official_every == 0}
        net.items[i] = api_item(cid=i, uuid=uid(i), title=f"Thing {i:04d}", **marks, **extra,
                                stats=make_stats(pc=100, total=9_000_000 if i == hot else 1000 + i, likes=(i % 7) * 10,
                                                 subs=100_000 - i, rating=1))


@pytest.fixture
def auto(monkeypatch):
    """The first-show browse switched on, as it is in MO2."""
    monkeypatch.setattr(CreationsWindow, "AUTO_BROWSE", True)


def gate_page(monkeypatch, page, text=None):
    """Hold the request for ``page`` until the returned gate is set; ``arrived`` is set when that request is made."""
    gate, arrived, real = threading.Event(), threading.Event(), core._open

    def slow(request, timeout):
        if f"page={page}&" in request.full_url + "&" and (text is None or f"text={text}" in request.full_url):
            arrived.set()
            gate.wait(10)
        return real(request, timeout)

    monkeypatch.setattr(core, "_open", slow)
    return gate, arrived


def test_the_window_makes_no_request_until_it_is_shown_and_then_opens_on_most_downloaded(make_window, net, auto):
    popularity_set(net)
    window = make_window()
    QApplication.processEvents()
    assert net.calls == [] and window._sort.currentData() == "downloads" and window._sort.currentText() == "Most downloaded"
    window.show()
    assert window._status.text() == "Loading the most downloaded…"  # at once, before the first answer
    settle(window)
    calls = content_calls(net)
    assert [q["page"] for q in calls] == [str(n) for n in range(1, 11)]  # ten requests, one after the other: never more
    assert all(q["sort"] == "subscriptions" and q["order"] == "desc" and q["time_period"] == "all_time" and q["size"] == "100"
               and q["hardware_platforms"] == "WINDOWS" and q["marketplace"] == "false" and "text" not in q for q in calls)
    assert window._list.count() == 50 and visible(window, window._more_button)
    assert titles(window)[0] == "Thing 0517"  # popularity rank 517, but by far the most downloaded
    assert window._status.text() == "Most downloaded among Bethesda's 1,000 most popular (PC, free). Showing 50 of 1,000."
    assert window._result_mode == "collect" and len(window._results) == 1000
    downloads = [r["downloads"] for r in sources(window)]
    assert downloads == sorted(downloads, reverse=True) and sources(window)[0]["downloads"] == 9_000_000
    assert len(category_calls(net)) == 1  # the categories fetch is unchanged: once, with the first search


def test_a_smaller_catalogue_uses_its_real_count_in_the_status(make_window, net, auto):
    popularity_set(net, n=130)
    window = make_window()
    window.show()
    settle(window)
    assert len(content_calls(net)) == 2
    assert window._status.text() == "Most downloaded among Bethesda's 130 most popular (PC, free). Showing 50 of 130."
    window.show_more()
    window.show_more()
    assert window._list.count() == 130 and not visible(window, window._more_button)
    assert window._status.text() == "Most downloaded among Bethesda's 130 most popular (PC, free). Showing 130 of 130."


def test_the_first_sorted_rows_appear_only_when_collecting_finishes_and_progress_is_shown(make_window, net, auto, monkeypatch):
    popularity_set(net)
    gate, arrived = gate_page(monkeypatch, 4)
    window = make_window()
    window.show()
    wait_for(arrived.is_set)
    wait_for(lambda: window._status.text() == "Loading the most downloaded… (3 of 10)")
    assert window._list.count() == 0  # no partial list that would reshuffle
    gate.set()
    settle(window)
    assert window._list.count() == 50 and window._status.text().startswith("Most downloaded among Bethesda's 1,000")


def test_reopening_and_resorting_between_local_sorts_make_no_request(make_window, net, auto):
    popularity_set(net)
    window = make_window()
    window.show()
    settle(window)
    net.calls.clear()
    window.close()
    window.show()  # reopening: the rows are still there
    settle(window)
    assert content_calls(net) == [] and window._list.count() == 50 and titles(window)[0] == "Thing 0517"
    choose(window, window._sort, "likes")
    assert content_calls(net) == [] and titles(window)[0] == "Thing 0006"  # likes 60, ties in popularity order
    assert window._status.text() == "Most liked among Bethesda's 1,000 most popular (PC, free). Showing 50 of 1,000."
    likes = [r["likes"] for r in sources(window)]
    assert likes == sorted(likes, reverse=True)
    choose(window, window._sort, "downloads")
    assert content_calls(net) == [] and titles(window)[0] == "Thing 0517"
    window.show_more()
    assert window._list.count() == 100 and content_calls(net) == []  # Show more reveals from the cache
    window._search_button.click()  # Browse again
    settle(window)
    assert content_calls(net) == [] and window._list.count() == 50
    choose(window, window._sort, "newest")  # a server sort: one page request
    assert [q["page"] for q in content_calls(net)] == ["1"] and content_calls(net)[0]["size"] == "50"
    net.calls.clear()
    choose(window, window._sort, "downloads")  # and back to a set seen before
    assert content_calls(net) == [] and titles(window)[0] == "Thing 0517"


def test_the_cache_is_keyed_by_game_filters_category_and_author(make_window, net, auto):
    popularity_set(net, n=250, categories=["Armor"])
    window = make_window()
    window.show()
    settle(window)
    assert len(content_calls(net)) == 3
    for change in (lambda: window._pc_only.setChecked(False), lambda: window._free_only.setChecked(False),
                   lambda: choose(window, window._category, "Armor")):
        net.calls.clear()
        change()
        settle(window)
        assert len(content_calls(net)) == 3, "a new key reads its own set"
    net.calls.clear()
    window._category.setCurrentIndex(0)
    window._free_only.setChecked(True)
    window._pc_only.setChecked(True)
    settle(window)
    assert content_calls(net) == [] and window._status.text().startswith("Most downloaded among Bethesda's 250 most popular (PC, free).")
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    window.search("")
    settle(window)
    assert {h["x-bnet-product"] for u, h in net.calls if "/content?" in u} == {"skyrim"}
    net.calls.clear()
    window._game.setCurrentIndex(window._game.findData("fallout4"))
    window.search("")
    settle(window)
    assert content_calls(net) == [] and window._list.count() == 50  # Fallout 4's set was kept


def test_closing_during_the_collect_stops_it_and_the_next_show_starts_again(make_window, net, auto, monkeypatch):
    popularity_set(net)
    gate, arrived = gate_page(monkeypatch, 3)
    window = make_window()
    window.show()
    wait_for(arrived.is_set)
    window.close()
    assert not window._searching
    gate.set()
    settle(window)
    assert [q["page"] for q in content_calls(net)] == ["1", "2", "3"]  # page 4 was never asked for
    assert window._list.count() == 0 and window._collect_cache == {}
    net.calls.clear()
    window.show()  # nothing was read, so the window browses again
    settle(window)
    assert [q["page"] for q in content_calls(net)][:2] == ["1", "2"] and window._list.count() == 50


def test_a_collect_that_was_replaced_stops_before_its_next_request(make_window, net, monkeypatch):
    popularity_set(net)
    gate, arrived = gate_page(monkeypatch, 2)
    window = make_window()
    window.search("")
    wait_for(arrived.is_set)
    window._free_only.setChecked(False)
    window._launch()  # a new search replaces the collect while page 2 is on its way
    gate.set()
    settle(window)
    firsts = [q for q in content_calls(net) if "marketplace" in q]
    assert [q["page"] for q in firsts] == ["1", "2"]  # the replaced collect never asked for page 3
    assert window._collect_cache.keys() == {("fallout4", True, False, None, None, core.POPULARITY_ORDER)}


# ---- what's hot

def test_the_sort_list_has_the_new_order_and_defaults(make_window, net):
    window = make_window()
    labels = [window._sort.itemText(i) for i in range(window._sort.count())]
    assert labels == ["Best match", "Most downloaded", "Trending this week", "Popular this month", "Most popular (all time)",
                      "Most liked", "Best rated", "Newest", "Recently updated", "Title A–Z"]
    assert window._sort_key() == "downloads" and not hasattr(window, "_period")
    assert sort_enabled(window) == {"match": False, "downloads": True, "weekly": True, "monthly": True, "popular": True,
                                    "likes": True, "rating": True, "newest": True, "updated": True, "title": True}
    model = window._sort.model()
    assert model.item(window._sort.findData("match")).toolTip() == "Best match needs search words"
    assert all(model.item(i).toolTip() == "" for i in range(window._sort.count()) if window._sort.itemData(i) != "match")


@pytest.mark.parametrize("key,period", [("weekly", "weekly"), ("monthly", "monthly"), ("popular", "all_time")])
def test_the_trending_sorts_are_server_sorts_with_a_time_period(make_window, net, key, period):
    popularity_set(net, n=120)
    window = make_window()
    window.search("")
    settle(window)
    net.calls.clear()
    choose(window, window._sort, key)
    (q,) = content_calls(net)
    assert (q["sort"], q["order"], q["time_period"], q["page"], q["size"]) == ("subscriptions", "desc", period, "1", "50")
    assert window._list.count() == 50 and window._result_mode == "browse"
    assert window._status.text() == "Showing 50 of 120 (PC, free)."
    window.show_more()  # one page per Show more
    settle(window)
    q2 = content_calls(net)[-1]
    assert (q2["page"], q2["time_period"]) == ("2", period) and window._list.count() == 100


def test_the_trending_sorts_are_greyed_with_search_words_and_come_back_without(make_window, net):
    popularity_set(net, n=60)
    window = make_window()
    window.search("thing")
    settle(window)
    assert window._sort_key() == "match"
    assert sort_enabled(window) == {"match": True, "downloads": True, "weekly": False, "monthly": False, "popular": False,
                                    "likes": True, "rating": True, "newest": True, "updated": True, "title": True}
    model = window._sort.model()
    for key in ("weekly", "monthly", "popular"):
        assert model.item(window._sort.findData(key)).toolTip() == "Not available with search words"
    assert model.item(window._sort.findData("downloads")).toolTip() == ""
    net.calls.clear()
    choose(window, window._sort, "downloads")  # a local sort of the words' matches: no request
    assert content_calls(net) == []
    window.search("")
    settle(window)
    assert sort_enabled(window)["weekly"] and sort_enabled(window)["match"] is False and window._sort_key() == "downloads"


def test_a_trending_sort_falls_back_when_words_are_searched(make_window, net):
    popularity_set(net, n=30)
    window = make_window()
    window.search("")
    settle(window)
    choose(window, window._sort, "weekly")
    window.search("thing")
    settle(window)
    assert window._sort_key() == "match"  # not offered with words
    window.search("")
    settle(window)
    assert window._sort_key() == "downloads"  # and the browse default on the way back


def test_the_default_sort_changes_with_the_kind_of_search_but_a_chosen_one_stays(make_window, net):
    popularity_set(net, n=30)
    window = make_window()
    window.search("")
    settle(window)
    assert window._sort_key() == "downloads"
    window.search("thing")
    settle(window)
    assert window._sort_key() == "match"  # the untouched default: Best match with words
    choose(window, window._sort, "downloads")  # chosen on purpose
    window.search("")
    settle(window)
    window.search("thing")
    settle(window)
    assert window._sort_key() == "downloads"


# ---- Verified creators only / Official creations only

def test_the_creator_filters_are_off_by_default_with_their_tooltips(make_window, net):
    window = make_window()
    assert not window._verified_only.isChecked() and not window._official_only.isChecked()
    assert window._verified_only.text() == "Verified creators only"
    assert window._verified_only.toolTip() == "Only Creations by Bethesda's Verified Creators"
    assert window._official_only.text() == "Official creations only"
    assert window._official_only.toolTip() == "Only Creations published by Bethesda"
    window._verified_only.setChecked(True)  # before any search there is nothing to re-run
    window._official_only.setChecked(True)
    assert net.calls == []


def test_verified_only_filters_the_collected_set_without_a_request(make_window, net):
    popularity_set(net, n=1200)
    window = make_window()
    window.search("")
    settle(window)
    net.calls.clear()
    window._verified_only.setChecked(True)  # at once: no pause, no request
    assert content_calls(net) == [] and not window._search_timer.isActive()
    verified = [r for r in window._results if r["author_verified"]]
    assert len(verified) == 1000 // 12 == 83
    assert window._list.count() == 50 and all(r["author_verified"] for r in sources(window))
    assert window._status.text() == "Most downloaded among Bethesda's 1,000 most popular (PC, free, verified). Showing 50 of 83."
    assert titles(window)[0] == "Thing 0996"  # 517 is not verified: the most downloaded verified one is the 996th
    window.show_more()
    assert window._list.count() == 83 and window._status.text().endswith("(PC, free, verified). Showing 83 of 83.")
    choose(window, window._sort, "likes")
    assert content_calls(net) == [] and all(r["author_verified"] for r in sources(window))
    window._verified_only.setChecked(False)
    assert content_calls(net) == [] and window._list.count() == 50
    assert window._status.text() == "Most liked among Bethesda's 1,000 most popular (PC, free). Showing 50 of 1,000."


def test_verified_only_makes_every_browse_collect_in_the_chosen_server_order(make_window, net):
    popularity_set(net, n=1200)
    window = make_window()
    window.search("")
    settle(window)
    choose(window, window._sort, "newest")
    assert window._result_mode == "browse" and len(content_calls(net)) == 11
    net.calls.clear()
    window._verified_only.setChecked(True)
    settle(window)
    calls = content_calls(net)
    assert [q["page"] for q in calls] == [str(n) for n in range(1, 11)]  # collected, never one near-empty page
    assert all(q["sort"] == "ptime" and q["order"] == "desc" and q["size"] == "100" for q in calls)
    assert window._result_mode == "collect" and window._list.count() == 50 and all(r["author_verified"] for r in sources(window))
    assert window._status.text() == ("Newest, among the first 1,000 Creations Bethesda lists (PC, free, verified). Showing 50 of 83.")
    net.calls.clear()
    choose(window, window._sort, "weekly")  # another order is another set
    assert [q["time_period"] for q in content_calls(net)] == ["weekly"] * 10
    net.calls.clear()
    choose(window, window._sort, "newest")  # but the one seen before is kept
    assert content_calls(net) == [] and window._list.count() == 50


def test_verified_only_in_a_text_search_filters_its_matches_locally(make_window, net):
    popularity_set(net, n=650)
    window = make_window()
    window.search("thing")
    settle(window)
    assert len(content_calls(net)) == 5  # a text search is five requests at most
    net.calls.clear()
    window._verified_only.setChecked(True)
    assert content_calls(net) == []
    assert window._list.count() == 500 // 12 == 41 and all(r["author_verified"] for r in sources(window))
    assert window._status.text() == ("Sorted among the 500 best matches of 650; add words to narrow it. "
                                     "Showing 41 of 41 (PC, free, verified).")
    window._verified_only.setChecked(False)
    assert window._list.count() == 50 and content_calls(net) == []
    window._verified_only.setChecked(True)
    window.search("thing 06")  # a new search with the filter still on
    settle(window)
    assert all(r["author_verified"] for r in sources(window))


def test_official_only_works_the_same_way_and_both_filters_combine(make_window, net):
    popularity_set(net, n=600, verified_every=4, official_every=6)
    window = make_window()
    window.search("")
    settle(window)
    net.calls.clear()
    window._official_only.setChecked(True)
    assert content_calls(net) == [] and all(r["author_official"] for r in sources(window))
    assert window._status.text() == "Most downloaded among Bethesda's 600 most popular (PC, free, official). Showing 50 of 100."
    window._verified_only.setChecked(True)  # both: verified AND official (every 12th)
    assert content_calls(net) == []
    assert window._list.count() == 50 and all(r["author_verified"] and r["author_official"] for r in sources(window))
    assert window._status.text() == "Most downloaded among Bethesda's 600 most popular (PC, free, verified, official). Showing 50 of 50."
    window._official_only.setChecked(False)
    assert all(r["author_verified"] for r in sources(window)) and window._list.count() == 50


def test_nothing_left_after_a_creator_filter_says_so_and_clears_the_details(make_window, net):
    popularity_set(net, n=40, verified_every=0)
    window = make_window()
    window.search("")
    settle(window)
    pick(window, 5)
    window._verified_only.setChecked(True)
    assert window._list.count() == 0 and window._status.text() == "No Creations found."
    assert window._stack.currentIndex() == 0 and window._detail is None
    window._verified_only.setChecked(False)
    assert window._list.count() == 40


def test_a_row_by_a_verified_or_official_author_says_so_after_the_name(make_window, net):
    net.add(api_item(cid=1, uuid=UUID_A, title="Fine Verified", author_verified=True))
    net.add(api_item(cid=2, uuid=UUID_B, title="Fine Official", author_official=True, author_verified=True))
    net.add(api_item(cid=3, uuid=UUID_C, title="Fine Plain"))
    window = make_window()
    window.show()
    window.search("fine")
    settle(window)
    delegate = window._list.itemDelegate()
    lines = {rows(window)[i]["title"]: delegate._lines(window._list.model().index(i, 0))[2] for i in range(window._list.count())}
    assert lines["Fine Verified"].startswith("by Someone ✓ Verified  ·  ")
    assert lines["Fine Official"].startswith("by Someone ✓ Verified ✓ Official  ·  ")
    assert lines["Fine Plain"].startswith("by Someone  ·  ") and "✓" not in lines["Fine Plain"]
    texts_ = {rows(window)[i]["title"]: window._list.item(i).text() for i in range(window._list.count())}
    assert "by Someone ✓ Verified," in texts_["Fine Verified"] and "✓" not in texts_["Fine Plain"]  # what a screen reader hears
    window.grab()  # painted in the palette's colours without errors
    pick(window, 2)
    assert window._d_author.text() == "by Someone (verified author, published by Bethesda)"
    assert texts.badges({}) == "" and texts.badges({"author_official": True}) == "✓ Official"
    assert texts.filter_summary(True, True, None, None, True, True) == "PC, free, verified, official"


def test_a_pasted_reference_ignores_the_creator_filters(make_window, net):
    net.add(api_item())
    window = make_window()
    window._verified_only.setChecked(True)
    window.search("743724")
    settle(window)
    assert titles(window) == ["Fine Mod: Revamped"] and window._status.text() == "Found 1 Creation."
    window._official_only.setChecked(True)  # nothing to filter in a pasted reference
    assert titles(window) == ["Fine Mod: Revamped"] and not window._search_timer.isActive()


# ---------------------------------------------------------------- In my game: what the game itself lists (1.1.0)

def own(base, game, *refs, folder=None):
    """Write the game's own list of downloaded Creations (``ContentCatalog.txt``) under the temporary local-app-data ``base``:
    ``refs`` are numbers (Skyrim keys) or UUIDs (Fallout 4 keys)."""
    folder = folder or {"fallout4": "Fallout4", "skyrimspecialedition": "Skyrim Special Edition"}[game]
    body = {f"CSV2_{r}": {"Files": ["x.esp"], "FilesSize": 1, "Timestamp": 1, "Title": "t", "Version": "1.0"} for r in refs}
    body["ContentCatalog"] = {"Description": "Content catalog", "Version": "1.1"}
    path = Path(base) / folder / "ContentCatalog.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def owned_calls(net):
    """The content_ids requests so far, as the lists of references they named."""
    return [q["content_ids"].split(",") for q in content_calls(net) if "content_ids" in q]


GREYED = ("_pc_only", "_free_only", "_category", "_verified_only", "_official_only")


def test_a_creation_the_game_lists_is_marked_in_your_game_in_the_rows_and_the_details(make_window, net, tmp_path, no_real_local_app_data):
    net.add(api_item(cid=1, uuid=UUID_A, title="Owned Free Thing"))
    net.add(api_item(cid=2, uuid=UUID_B, title="Owned Paid Thing", price="500", catalog=store_listing("STEAM", amount=300)))
    net.add(api_item(cid=3, uuid=UUID_C, title="Other Free Thing"))
    net.add(api_item(cid=4, uuid=uid(4), title="Other Paid Thing", price="500", catalog=store_listing("STEAM", amount=300)))
    own(no_real_local_app_data, "fallout4", UUID_A.upper(), 2)  # a UUID key (any case) and a numeric one: both count
    window = make_window()
    window._free_only.setChecked(False)
    window.search("thing")
    settle(window)
    markers = {r["title"]: r["marker"] for r in rows(window)}
    assert markers == {"Owned Free Thing": "In your game", "Owned Paid Thing": "In your game",
                       "Other Free Thing": "Free", "Other Paid Thing": "300 credits"}
    assert any(window._list.item(i).text().startswith("Owned Free Thing, In your game") for i in range(window._list.count()))
    pick(window, 1)  # free: the line is shown and Download stays available (an MO2-managed copy is still allowed)
    assert window._d_owned.text() == "In your game: you have this through the game's Creations menu." and visible(window, window._d_owned)
    assert window._d_avail.text() == "Free: can be downloaded here." and window._download_button.isEnabled()
    pick(window, 2)  # paid: owned, not downloadable here, and no longer sent back to the Creations menu
    assert window._d_owned.text() == texts.OWNED_DETAIL and visible(window, window._d_owned)
    assert window._d_avail.text() == "It's a store Creation, so your game's Creations menu looks after it."
    assert not window._download_button.isEnabled()
    pick(window, 3)
    assert not visible(window, window._d_owned) and window._d_owned.text() == "" and window._d_avail.text().startswith("Free")
    pick(window, 4)
    assert not visible(window, window._d_owned) and "Get it in the game's Creations menu" in window._d_avail.text()


def test_an_owned_free_creation_can_still_be_downloaded_into_mo2(make_window, net, tmp_path, no_real_local_app_data):
    add_fine(net)
    own(no_real_local_app_data, "fallout4", UUID)
    window = make_window()
    window.search("fine")
    settle(window)
    pick(window, 743724)
    assert rows(window)[0]["marker"] == "In your game" and window._download_button.isEnabled()
    window.download_selected()
    settle(window)
    assert sorted(p.suffix for p in (tmp_path / "dl").iterdir()) == [".meta", ".zip"]
    assert rows(window)[0]["marker"] == "In your game" and rows(window)[0]["state"] == "Downloaded"


def test_a_game_list_that_names_nothing_marks_nothing(make_window, net):
    add_fine(net)
    window = make_window()
    window.search("fine")
    settle(window)
    assert rows(window)[0]["marker"] == "Free" and not visible(window, window._d_owned)


def test_the_games_list_is_read_once_per_session_and_again_on_game_change_and_reopening(make_window, net, no_real_local_app_data):
    add_fine(net)
    net.add(api_item(cid=2, uuid=UUID_A, title="Fine Second Thing"))
    asked = []
    real = core.game_catalog

    def counting(game, base=None):
        asked.append(game)
        for path in core.catalog_paths(game, base):  # nothing outside the temporary local-app-data folder is ever looked at
            assert Path(no_real_local_app_data) in path.parents
        return real(game, base)

    from unittest import mock
    with mock.patch.object(core, "game_catalog", counting):
        own(no_real_local_app_data, "fallout4", UUID)
        window = make_window()
        window.search("fine")
        settle(window)
        pick(window, 743724)
        window.search("second")
        settle(window)
        window.search("fine")
        settle(window)
        assert asked == ["fallout4"]  # rows, details and searches all use the one read
        assert [r["marker"] for r in rows(window)] == ["In your game", "Free"]
        own(no_real_local_app_data, "fallout4", UUID, UUID_A)  # the game downloaded another one meanwhile
        assert [r["marker"] for r in rows(window)] == ["In your game", "Free"]  # not noticed until the window is reopened
        window.configure("fallout4", window._downloads)  # reopening: read again, rows re-marked, no request
        assert asked == ["fallout4", "fallout4"] and [r["marker"] for r in rows(window)] == ["In your game", "In your game"]
        own(no_real_local_app_data, "fallout4", UUID)
        window.close()
        window.show()  # shown again without configure() (a plain re-show) reads again too
        assert asked == ["fallout4"] * 3 and [r["marker"] for r in rows(window)] == ["In your game", "Free"]
        window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
        settle(window)
        assert asked[-1] == "skyrimspecialedition" and len(asked) == 4  # a game change reads the new game's list, once


def test_the_windows_list_location_can_be_given(qapp, net, tmp_path, monkeypatch):
    base = tmp_path / "elsewhere"
    own(base, "skyrimspecialedition", 77)
    own(base, "skyrimspecialedition", 78, folder="Skyrim Special Edition GOG")
    window = CreationsWindow(None, "skyrimspecialedition", None, catalog_base=base)
    try:
        assert window._catalog_now()["ids"] == {77, 78} and window.catalog_base == base
    finally:
        window.shutdown()
        window.close()
        window.deleteLater()


# ---- the view

def owned_window(make_window, net, base, *refs):
    stats_set(net)  # ids 1..4, with their own downloads, likes and dates
    net.add(api_item(cid=5, uuid=uid(5), title="Not Mine At All"))
    own(base, "fallout4", *refs)
    return make_window()


def test_in_my_game_only_shows_exactly_the_games_own_list_fetched_by_id(make_window, net, no_real_local_app_data):
    window = owned_window(make_window, net, no_real_local_app_data, 1, 2, uid(3))
    assert not window._owned_only.isChecked() and window._owned_only.text() == "In my game only"
    assert window._owned_only.toolTip() == ("Creations your game downloaded through its Creations menu "
                                            "(read from the game's own list on this PC)")
    window._owned_only.setChecked(True)
    assert net.calls == []  # nothing is asked before a search, as for the other controls
    window.start_search()
    settle(window)
    (q,) = content_calls(net)
    assert q["content_ids"] == f"1,2,{uid(3)}" and q["size"] == "100" and q["product"] == "fallout4"
    assert not ({"hardware_platforms", "marketplace", "categories", "author_displayname", "sort", "text"} & set(q))
    assert titles(window) == ["College Delta", "college Alpha", "College Charlie"]  # Most downloaded, ordered here
    assert {r["marker"] for r in rows(window)} == {"In your game"}
    assert window._status.text() == "3 Creations in your game."
    assert window._result_mode == "owned" and not visible(window, window._more_button)
    pick(window, 1)
    assert window._d_owned.text() == texts.OWNED_DETAIL


def test_in_my_game_greys_out_the_controls_that_do_not_apply(make_window, net, no_real_local_app_data):
    window = owned_window(make_window, net, no_real_local_app_data, 1)
    tips = {n: getattr(window, n).toolTip() for n in GREYED}
    assert all(getattr(window, n).isEnabled() for n in GREYED) and window._owned_only.isEnabled()
    window._free_only.setChecked(False)
    window._owned_only.setChecked(True)
    assert not any(getattr(window, n).isEnabled() for n in GREYED) and window._owned_only.isEnabled()
    assert all(getattr(window, n).toolTip() == texts.OWNED_GREY_TIP for n in GREYED)
    enabled = sort_enabled(window)  # every sort works here, Best match too, except the per-period ones
    assert all(v for k, v in enabled.items() if k not in core.PERIOD_SORTS) and not any(enabled[k] for k in core.PERIOD_SORTS)
    window._owned_only.setChecked(False)
    assert all(getattr(window, n).isEnabled() for n in GREYED)
    assert {n: getattr(window, n).toolTip() for n in GREYED} == tips and not window._free_only.isChecked()  # picks are kept
    assert sort_enabled(window)["match"] is False  # a browse offers its own sorts again


def test_in_my_game_ignores_the_filters_and_includes_paid_and_console_only_creations(make_window, net, no_real_local_app_data):
    net.add(api_item(cid=1, uuid=uid(1), title="Owned Paid", price="500", catalog=store_listing("STEAM", amount=300)))
    net.add(api_item(cid=2, uuid=uid(2), title="Owned Console", hardware_platforms=["XBOXLIVE"]))
    net.add(api_item(cid=3, uuid=uid(3), title="Owned Gone", deleted=True))
    net.add(api_item(cid=4, uuid=uid(4), title="Owned Free", categories=["Armor"], author_verified=False))
    own(no_real_local_app_data, "fallout4", 1, 2, 3, 4)
    window = make_window()
    window._category.addItem("Armor", "Armor")
    window._category.setCurrentIndex(1)
    window._verified_only.setChecked(True)
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    assert sorted(titles(window)) == ["Owned Console", "Owned Free", "Owned Gone", "Owned Paid"]
    assert window._status.text() == "4 Creations in your game."
    assert len(content_calls(net)) == 1  # one request, whatever the filters said
    pick(window, 4)
    assert window._download_button.isEnabled()  # the free one can still be downloaded


def test_search_words_filter_the_owned_list_by_title_here_and_every_sort_works_here(make_window, net, no_real_local_app_data):
    window = owned_window(make_window, net, no_real_local_app_data, 1, 2, uid(3), 4)
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    assert len(content_calls(net)) == 1 and titles(window) == ["College Delta", "college Alpha", "College Charlie", "College Bravo"]
    order = {"downloads": ["College Delta", "college Alpha", "College Charlie", "College Bravo"],
             "likes": ["College Bravo", "college Alpha", "College Charlie", "College Delta"],
             "title": ["college Alpha", "College Bravo", "College Charlie", "College Delta"],
             "newest": ["College Bravo", "college Alpha", "College Charlie", "College Delta"],
             "updated": ["College Delta", "College Charlie", "college Alpha", "College Bravo"],
             "rating": ["College Delta", "College Bravo", "College Charlie", "college Alpha"],
             "match": ["College Delta", "college Alpha", "College Charlie", "College Bravo"]}  # the order Bethesda answered in
    for key in core.SORT_MODES:
        if key in order:
            choose(window, window._sort, key)
            assert titles(window) == order[key], key
    choose(window, window._sort, "popular")
    assert titles(window)[0] == "College Charlie"  # by subscribers: all Bethesda gives per Creation
    assert not sort_enabled(window)["weekly"] and not sort_enabled(window)["monthly"]  # no per-period figures to sort by here
    choose(window, window._sort, "title")
    window._query.setText("alp")  # words: every one must be in the title, any case
    window.start_search()
    settle(window)
    assert titles(window) == ["college Alpha"] and window._status.text() == "1 of 4 Creations in your game match those words."
    window._query.setText("COLLEGE ch")
    window.start_search()
    settle(window)
    assert titles(window) == ["College Charlie"]
    window._query.setText("nothing like this")
    window.start_search()
    settle(window)
    assert titles(window) == [] and window._status.text() == texts.OWNED_NO_MATCH == window._message.text()
    window._query.clear()
    window.start_search()
    settle(window)
    assert len(titles(window)) == 4 and window._status.text() == "4 Creations in your game."
    assert len(content_calls(net)) == 1  # words and sorts never asked Bethesda again
    window._query.setText("college")  # a sort change with words on is still local
    window.start_search()
    choose(window, window._sort, "likes")
    assert titles(window)[0] == "College Bravo" and len(content_calls(net)) == 1


def test_turning_in_my_game_off_and_on_again_asks_nothing_new(make_window, net, no_real_local_app_data):
    window = owned_window(make_window, net, no_real_local_app_data, 1, 2, uid(3))
    window.search("")
    settle(window)
    browse = content_calls(net)
    assert browse and window._result_mode == "collect"
    window._owned_only.setChecked(True)
    settle(window)
    assert len(content_calls(net)) == len(browse) + 1 and window._result_mode == "owned" and window._list.count() == 3
    seen = len(content_calls(net))
    window._owned_only.setChecked(False)
    settle(window)
    assert window._result_mode == "collect" and len(content_calls(net)) == seen  # the browse comes back from its cache
    window._owned_only.setChecked(True)
    settle(window)
    assert window._result_mode == "owned" and window._list.count() == 3 and len(content_calls(net)) == seen  # and so does this
    assert len(owned_calls(net)) == 1


def test_in_my_game_stepping_the_checkbox_asks_once_after_the_pause(make_window, net, no_real_local_app_data, monkeypatch):
    window = owned_window(make_window, net, no_real_local_app_data, 1, 2)
    window.search("")
    settle(window)
    monkeypatch.setattr(CreationsWindow, "SEARCH_DELAY_MS", 300)
    net.calls.clear()
    for on in (True, False, True):
        window._owned_only.setChecked(on)
    assert net.calls == [] and window._search_timer.isActive()  # debounced like every other control
    settle(window)
    assert len(owned_calls(net)) == 1 and window._result_mode == "owned"


def test_the_owned_list_is_looked_up_in_at_most_three_requests_of_100(make_window, net, no_real_local_app_data):
    for i in range(1, 251):
        net.items[i] = api_item(cid=i, uuid=uid(i), title=f"Mine {i:04d}")
    own(no_real_local_app_data, "fallout4", *range(1, 251))
    window = make_window()
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    calls = owned_calls(net)
    assert [len(c) for c in calls] == [100, 100, 50] and len(content_calls(net)) == 3
    assert window._list.count() == 50 and window._status.text() == "250 Creations in your game. Showing 50 of 250."
    window.show_more()
    window.show_more()
    assert window._list.count() == 150 and len(content_calls(net)) == 3  # revealed from what was read
    assert window._status.text() == "250 Creations in your game. Showing 150 of 250."


def test_more_than_300_listed_creations_are_cut_at_300_and_it_says_so(make_window, net, no_real_local_app_data):
    for i in range(1, 351):
        net.items[i] = api_item(cid=i, uuid=uid(i), title=f"Mine {i:04d}")
    own(no_real_local_app_data, "fallout4", *range(1, 351))
    window = make_window()
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    assert [len(c) for c in owned_calls(net)] == [100, 100, 100]
    assert window._capped and len(window._results) == 300
    assert window._status.text() == ("300 Creations in your game. Your game lists 350; only the first 300 are looked up. "
                                     "Showing 50 of 300.")


def test_a_missing_or_empty_game_list_says_so_and_asks_nothing(make_window, net, no_real_local_app_data):
    add_fine(net)
    window = make_window()
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    wording = ("Your game hasn't downloaded any Creations through its Creations menu yet (or this PC's game list wasn't found).")
    assert texts.OWNED_NONE == wording
    assert window._status.text() == wording and window._message.text() == wording and window._list.count() == 0
    assert content_calls(net) == []
    own(no_real_local_app_data, "fallout4")  # a list with nothing in it
    window.configure("fallout4", window._downloads)
    window.start_search()
    settle(window)
    assert window._status.text() == wording and content_calls(net) == []
    window._owned_only.setChecked(False)
    settle(window)
    assert window._list.count() == 1 and content_calls(net)  # the usual list is still one click away


def test_listed_creations_that_bethesda_does_not_know_are_explained(make_window, net, no_real_local_app_data):
    own(no_real_local_app_data, "fallout4", 999001, 999002)
    window = make_window()
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    assert len(content_calls(net)) == 1 and window._status.text() == texts.OWNED_NO_DETAILS == window._message.text()


def test_a_failed_lookup_is_explained_and_can_be_repeated(make_window, net, no_real_local_app_data, monkeypatch):
    own(no_real_local_app_data, "fallout4", 1)
    stats_set(net)
    window = make_window()
    window._owned_only.setChecked(True)
    real = core._open
    monkeypatch.setattr(core, "_open", lambda request, timeout: (_ for _ in ()).throw(OSError("offline")))
    window.start_search()
    settle(window)
    assert "Couldn't reach Bethesda's servers" in window._status.text()
    monkeypatch.setattr(core, "_open", real)
    window.start_search()
    settle(window)
    assert titles(window) == ["College Delta"]


def test_the_game_picker_keeps_in_my_game_on_and_shows_the_other_games_own_list(make_window, net, no_real_local_app_data):
    stats_set(net)
    own(no_real_local_app_data, "fallout4", 1)
    own(no_real_local_app_data, "skyrimspecialedition", 3, 4)
    window = make_window()
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    assert titles(window) == ["College Delta"]
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    settle(window)  # no "Search again": the other game's In my game view opens by itself
    assert window._owned_only.isChecked() and titles(window) == ["College Charlie", "College Bravo"]
    assert owned_calls(net) == [["1"], ["3", "4"]]
    assert {h["x-bnet-product"] for u, h in net.calls if "/content?" in u} == {"fallout4", "skyrim"}


def test_more_by_this_author_leaves_the_in_my_game_view(make_window, net, no_real_local_app_data):
    stats_set(net)
    own(no_real_local_app_data, "fallout4", 1, 2)
    window = make_window()
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    pick(window, 1)
    window.more_by_author()
    settle(window)
    assert not window._owned_only.isChecked() and window._author == "Someone" and window._pc_only.isEnabled()
    assert window._result_mode != "owned" and window._list.count() == 4


def test_reopening_in_the_in_my_game_view_follows_a_list_that_grew(make_window, net, no_real_local_app_data):
    stats_set(net)
    own(no_real_local_app_data, "fallout4", 1)
    window = make_window()
    window._owned_only.setChecked(True)
    window.start_search()
    settle(window)
    assert titles(window) == ["College Delta"]
    window.configure("fallout4", window._downloads)  # nothing changed: nothing is asked again
    settle(window)
    assert len(content_calls(net)) == 1
    own(no_real_local_app_data, "fallout4", 1, 2)
    window.configure("fallout4", window._downloads)
    settle(window)
    assert titles(window) == ["College Delta", "college Alpha"] and owned_calls(net)[-1] == ["1", "2"]


# ---- the Game picker browses by itself

def test_changing_the_game_browses_that_game_after_the_usual_pause_and_not_before(make_window, net, monkeypatch):
    bulk(net, 3)
    window = make_window()
    window.start_search()
    settle(window)
    monkeypatch.setattr(CreationsWindow, "SEARCH_DELAY_MS", 300)
    net.calls.clear()
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    assert window._list.count() == 0 and net.calls == [] and window._search_timer.isActive()  # debounced
    assert "Now browsing Skyrim Special Edition" in window._status.text() and "Search again" not in window._status.text()
    assert window._search_timer.interval() == 300
    settle(window)
    assert window._list.count() == 3 and window._result_mode == "collect"
    assert {h["x-bnet-product"] for u, h in net.calls if "/content?" in u} == {"skyrim"}
    assert window._status.text().startswith("Most downloaded among Bethesda's 3 most popular")


def test_several_quick_game_changes_make_one_browse_of_the_last_game(make_window, net, monkeypatch):
    bulk(net, 3)
    window = make_window()
    monkeypatch.setattr(CreationsWindow, "SEARCH_DELAY_MS", 300)
    for key in ("skyrimspecialedition", "fallout4", "skyrimspecialedition"):
        window._game.setCurrentIndex(window._game.findData(key))
    assert net.calls == []
    settle(window)
    assert {h["x-bnet-product"] for u, h in net.calls if "/content?" in u} == {"skyrim"}
    assert len(content_calls(net)) == 1  # three Creations: one page of the browse


def test_going_back_to_a_game_shows_its_browse_from_the_cache(make_window, net):
    bulk(net, 5)
    window = make_window()
    window.start_search()
    settle(window)
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    settle(window)
    seen = len(content_calls(net))
    assert seen == 2 and window._list.count() == 5
    window._game.setCurrentIndex(window._game.findData("fallout4"))
    settle(window)
    assert window._list.count() == 5 and len(content_calls(net)) == seen  # the first game's browse was kept
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    settle(window)
    assert window._list.count() == 5 and len(content_calls(net)) == seen


def test_changing_the_game_twice_before_anything_was_searched_still_browses_the_last_one(make_window, net):
    add_fine(net)
    window = make_window()
    window._game.setCurrentIndex(window._game.findData("skyrimspecialedition"))
    window._game.setCurrentIndex(window._game.findData("fallout4"))
    settle(window)
    assert window._searched and window._list.count() == 1


def test_a_pasted_link_for_the_other_game_still_opens_without_a_second_browse(make_window, net):
    add_fine(net)
    window = make_window("skyrimspecialedition")
    window.search(f"https://creations.bethesda.net/en/fallout4/details/{UUID}")
    settle(window)
    assert window.picked_game() == "fallout4" and window._list.count() == 1
    assert len(content_calls(net)) == 1  # the link's own lookup; the game change's browse was replaced by it


# ---- words

def test_the_in_my_game_words():
    assert texts.marker({"downloadable": True}, owned=True) == "In your game" == texts.OWNED_MARKER
    assert texts.marker({"downloadable": True}) == "Free" and texts.marker({"withdrawn": True}, owned=True) == "In your game"
    assert texts.OWNED_DETAIL == "In your game: you have this through the game's Creations menu."
    assert texts.owned_count_text(12) == "12 Creations in your game." and texts.owned_count_text(1) == "1 Creation in your game."
    assert texts.owned_count_text(1200) == "1,200 Creations in your game."
    assert texts.owned_count_text(300, 350, 300) == "300 Creations in your game. Your game lists 350; only the first 300 are looked up."
    assert texts.owned_count_text(300, 300, 300) == "300 Creations in your game."
    assert texts.owned_loading_text() == "Looking up the Creations in your game…"
    assert texts.owned_loading_text(2, 3) == "Looking up the Creations in your game… (2 of 3)"
    assert texts.title_matches("Fine Mod: Revamped", "revamped fine") and not texts.title_matches("Fine Mod", "fine x")
    assert texts.title_matches("anything", "") and texts.title_matches(None, "") and not texts.title_matches(None, "a")
    assert texts.availability({"downloadable": True}, owned=True) == "Free: can be downloaded here."
    assert texts.availability({"withdrawn": True, "get_in_game_reason": core.WITHDRAWN_REASON}, owned=True) == (
        "Withdrawn or restricted by Bethesda: not available here.")


# ---------------------------------------------------------------- MO2 stylesheets, Esc and Enter

def test_the_window_takes_a_dialog_stylesheet_background_like_mo2_themes_give(make_window):
    window = make_window()
    app = QApplication.instance()
    before = app.styleSheet()
    try:
        app.setStyleSheet("QMainWindow, QDialog { background-color: #123456; }")  # how MO2's dark themes colour their windows
        window.resize(400, 300)
        window.show()
        QApplication.processEvents()
        assert window.grab().toImage().pixelColor(2, 2).name() == "#123456"
    finally:
        app.setStyleSheet(before)


def test_escape_closes_the_window_cleanly_and_enter_presses_no_button(make_window):
    window = make_window()
    window.show()
    QApplication.processEvents()
    assert not any(b.isDefault() or b.autoDefault() for b in window.findChildren(QPushButton))
    QTest.keyClick(window, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert not window.isVisible()


def test_a_store_creation_shows_no_zero_downloads():
    store = {"in_creation_club": True, "downloads": 0, "downloads_pc": 0, "downloads_all": 0, "likes": 830}
    assert "download" not in texts.row_stats(store) and "830 likes" in texts.row_stats(store)
    assert "download" not in texts.detail_stats(store)
    free = {"in_creation_club": False, "downloads": 0, "downloads_pc": 0, "downloads_all": 0}
    assert "0 downloads" in texts.row_stats(free)  # a free Creation nobody downloaded yet still says so
    official = {"in_creation_club": True, "downloads": 0, "likes": 0, "updated": 1726000000}  # Bethesda's own store items
    assert "like" not in texts.row_stats(official) and "like" not in texts.detail_stats(official)
    assert "0 likes" in texts.row_stats({"in_creation_club": False, "likes": 0})
