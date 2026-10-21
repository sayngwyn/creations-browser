"""Test-only MO2 plugin that drives Creations Browser inside a real Mod Organizer 2 (never shipped).

``run_live.py`` copies this file and ``cf_live_harness.json`` into a test MO2's ``plugins`` folder, starts MO2 and
removes them afterwards. Once MO2's window is up, the harness opens Creations Browser from the Tools menu, checks a paid
Creation is refused, downloads a free one, checks MO2's Downloads tab lists it, installs it through MO2's own installer
(Simple Installer set to silent for the run, then restored), reads the installed mod's meta.ini, removes the mod and
the download, writes a results JSON and closes MO2 normally.
"""

from __future__ import annotations

import configparser
import json
import os
import time
import traceback
from pathlib import Path

import mobase
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QGuiApplication
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QMainWindow, QWidget

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "cf_live_harness.json").read_text(encoding="utf-8"))
QUICK = "Simple Installer"
ROW_DATA = Qt.ItemDataRole.UserRole  # where Creations Browser keeps each list row's data


class Harness(mobase.IPluginTool):
    def __init__(self):
        super().__init__()
        self._org = None
        self._results: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "checks": [], "facts": {}}
        self._steps = None
        self._wait = None
        self._deadline = 0.0
        self._timer = None
        self._silent_before = None

    # ---- IPlugin
    def init(self, organizer) -> bool:
        self._org = organizer
        organizer.onUserInterfaceInitialized(self._ui_ready)
        return True

    def name(self) -> str:
        return "Creations Browser Live Harness"

    def author(self) -> str:
        return "Creations Browser tests"

    def description(self) -> str:
        return "Test-only driver for Creations Browser live acceptance."

    def version(self):
        return mobase.VersionInfo(0, 1, 0, mobase.ReleaseType.FINAL)

    def settings(self):
        return []

    def displayName(self) -> str:
        return "Creations Browser Live Harness"

    def tooltip(self) -> str:
        return "Test-only"

    def icon(self):
        from PyQt6.QtGui import QIcon
        return QIcon()

    def display(self) -> None:
        pass

    # ---- driver
    def _ui_ready(self, main_window) -> None:
        try:
            self._main = main_window
            screens = QGuiApplication.screens()
            if len(screens) > 1:  # keep it off Gwyn's main screen
                main_window.move(screens[1].availableGeometry().topLeft())
            self._steps = self._scenario()
            self._timer = QTimer()
            self._timer.timeout.connect(self._tick)
            self._timer.start(250)
        except Exception:
            self._fail("startup", traceback.format_exc())
            self._finish()

    def _check(self, name: str, ok: bool, detail="") -> bool:
        self._results["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:2000]})
        return ok

    def _fail(self, name: str, detail: str) -> None:
        self._check(name, False, detail)

    def _tick(self) -> None:
        try:
            if self._wait is not None:
                cond, what = self._wait
                if not cond():
                    if time.monotonic() > self._deadline:
                        self._fail(f"timeout: {what}", "")
                        self._steps = self._cleanup()
                        self._wait = None
                    return
                self._wait = None
            step = next(self._steps)
            if step is not None:
                cond, what, seconds = step
                self._wait = (cond, what)
                self._deadline = time.monotonic() + seconds
        except StopIteration:
            self._timer.stop()
            self._finish()
        except Exception:
            self._fail("exception", traceback.format_exc())
            self._steps = self._cleanup()
            self._wait = None

    def _shot(self, win, name: str) -> None:
        folder = Path(CONFIG["screenshots_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        win.raise_()
        win.activateWindow()
        QApplication.processEvents()
        ok = win.grab().save(str(folder / name), "PNG")
        self._check(f"screenshot {name}", ok, str(folder / name))

    def _window(self):
        for w in QApplication.topLevelWidgets():
            if type(w).__name__ == "CreationsWindow" and w.isVisible():
                return w
        return None

    def _scenario(self):
        cfg = CONFIG
        org = self._org
        downloads = Path(org.downloadsPath())
        self._results["facts"].update(game=org.managedGame().gameShortName(), downloads=str(downloads),
                                      profile=org.profileName())
        before = {p.name for p in downloads.glob("*")} if downloads.is_dir() else set()

        if cfg.get("control_zip"):  # control run without Creations Browser: install and remove a plain zip only
            import shutil
            self._zip = downloads / Path(cfg["control_zip"]).name
            shutil.copy2(cfg["control_zip"], self._zip)
            self._silent_before = org.pluginSetting(QUICK, "silent")
            org.setPluginSetting(QUICK, "silent", True)
            mod = org.installMod(str(self._zip), "CF Control Mod")
            org.setPluginSetting(QUICK, "silent", self._silent_before)
            self._silent_before = None
            if self._check("control: installMod returned a mod", mod is not None):
                self._mod_name = mod.name()
            yield from self._cleanup()
            return

        for a in self._main.findChildren(QAction):  # MO2 fills its Tools menu when it is about to show (updateToolMenu)
            if a.menu() is not None and "Tool" in a.text():
                a.menu().aboutToShow.emit()
        actions = self._main.findChildren(QAction)
        for a in list(actions):
            if a.menu() is not None:
                actions += a.menu().actions()
        action = next((a for a in actions if a.text().replace("&", "") == "Creations Browser"), None)
        if not self._check("tools menu has Creations Browser", action is not None,
                           sorted({a.text() for a in actions if a.text()})[:80]):
            return
        action.trigger()
        yield (lambda: self._window() is not None, "window opens", 15)
        win = self._window()
        self._check("notice shown", "Unofficial" in win._notice.text() and win._notice.isVisible(), win._notice.text())
        self._check("instance game picked", win.picked_game() == cfg["game"], win.picked_game())
        win.resize(1150, 780)

        # the window opens on a list by itself (most downloaded), with nothing selected
        yield (lambda: not win.busy() and win._list.count() > 0, "opening list", 120)
        self._check("opens on most downloaded", win._sort.currentData() == "downloads" and win._list.count() > 0
                    and win._detail is None, win._status.text())
        if cfg.get("screenshots_dir"):
            self._shot(win, "start-screen.png")

        # a paid Creation is refused
        win.search(str(cfg["paid_id"]))
        yield (lambda: not win.busy() and win._detail is not None, "paid detail", 60)
        d = win._detail
        self._check("paid: not downloadable", d and not d["downloadable"] and not win._download_button.isEnabled(),
                    {k: d.get(k) for k in ("title", "free_status", "get_in_game_reason")} if d else None)

        # search by words lists results
        win.search(cfg["search_text"])
        yield (lambda: not win.busy() and win._list.count() > 0, "text search", 60)
        self._check("text search finds results", win._list.count() > 0, win._status.text())

        # 1.1: sorting a text search by downloads happens locally, and every row is a PC Creation
        sources = lambda: [win._list.item(i).data(ROW_DATA)["source"] for i in range(win._list.count())]
        win._sort.setCurrentIndex(win._sort.findData("downloads"))
        yield (lambda: not win.busy(), "sort by downloads", 30)
        counts = [s.get("downloads") or 0 for s in sources()]
        self._check("sorted by downloads", counts == sorted(counts, reverse=True) and len(counts) > 1, counts[:8])
        self._check("PC filter: no console-only rows", not any(s.get("no_pc_version") for s in sources()), win._status.text())
        if cfg.get("screenshots_dir") and win._list.count():
            win._list.setCurrentRow(0)
            yield (lambda: not win.busy() and win._detail is not None, "first row details", 60)
            settled = time.monotonic() + 2  # let the preview picture arrive
            yield (lambda: time.monotonic() > settled, "settle", 10)
            self._shot(win, "search-sorted-by-downloads.png")
        # In my game: the game's own list of downloaded Creations (read only), paid ones included
        if cfg.get("owned_expect"):
            win._query.clear()  # words would filter the owned list by title
            win._owned_only.setChecked(True)
            yield (lambda: not win.busy() and win._list.count() > 0, "in my game", 60)
            owned_ids = [s.get("content_id") for s in sources()]
            self._check("in my game lists the expected Creation", cfg["owned_expect"] in owned_ids, owned_ids)
            if cfg.get("screenshots_dir"):
                win.select(cfg["owned_expect"])  # the owned paid Creation, details open
                yield (lambda: not win.busy() and (win._detail or {}).get("content_id") == cfg["owned_expect"], "owned details", 60)
                settled = time.monotonic() + 2  # let the preview picture arrive
                yield (lambda: time.monotonic() > settled, "owned settle", 10)
                self._shot(win, "in-my-game.png")
            win._owned_only.setChecked(False)
            yield (lambda: not win.busy(), "leave in my game", 60)
        # browse: an empty search lists the catalogue, most popular first
        win.search("")
        yield (lambda: not win.busy() and win._list.count() > 0, "browse", 60)
        self._check("browse lists Creations", win._list.count() > 0, win._status.text())

        # a free Creation downloads
        win.search(str(cfg["free_id"]))
        yield (lambda: not win.busy() and win._detail is not None and win._detail.get("content_id") == cfg["free_id"],
               "free detail", 60)
        self._check("free: download enabled", win._download_button.isEnabled(), win._detail.get("title"))
        self._results["facts"]["title"] = win._detail.get("title")
        win.download_selected()
        yield (lambda: win._current is None and not win.busy(), "download", 600)
        self._check("download reported saved", "Saved to Downloads" in win._status.text(), win._status.text())
        new = sorted({p.name for p in downloads.glob("*")} - before)
        self._results["facts"]["new_download_files"] = new
        zips = [downloads / n for n in new if n.lower().endswith(".zip")]
        if not self._check("one zip and its .meta written", len(zips) == 1 and Path(str(zips[0]) + ".meta").is_file(), new):
            yield from self._cleanup()
            return
        self._zip = zips[0]
        self._results["facts"]["meta"] = Path(str(self._zip) + ".meta").read_text(encoding="utf-8-sig")

        # MO2's Downloads tab lists it (its folder watcher)
        def listed():
            for view in self._main.findChildren(QAbstractItemView):
                model = view.model()
                if model is None or view.objectName() != "downloadView":
                    continue
                for r in range(model.rowCount()):
                    for c in range(model.columnCount()):
                        if self._zip.name in str(model.index(r, c).data() or "") or \
                                self._results["facts"]["title"][:20] in str(model.index(r, c).data() or ""):
                            return True
            return False
        yield (listed, "Downloads tab lists the zip", 20)
        self._check("Downloads tab lists the zip", True)

        if not cfg.get("install", True):  # a run that stops at the download (to tell install/remove effects apart)
            yield from self._cleanup()
            return

        # install through MO2's own installer
        self._silent_before = org.pluginSetting(QUICK, "silent")
        org.setPluginSetting(QUICK, "silent", True)
        mod = org.installMod(str(self._zip), "")
        org.setPluginSetting(QUICK, "silent", self._silent_before)
        self._silent_before = None
        if not self._check("installMod returned a mod", mod is not None):
            yield from self._cleanup()
            return
        self._mod_name = mod.name()
        mod_dir = Path(mod.absolutePath())
        files = sorted(str(p.relative_to(mod_dir)) for p in mod_dir.rglob("*") if p.is_file())
        ini = configparser.ConfigParser(interpolation=None, strict=False)
        ini.read(mod_dir / "meta.ini", encoding="utf-8")
        general = dict(ini["General"]) if ini.has_section("General") else {}
        self._results["facts"].update(mod_name=mod.name(), mod_version=mod.version().canonicalString(),
                                      mod_files=files[:50], mod_meta_general=general)
        self._check("mod name is the Creation title", mod.name() == self._results["facts"]["title"].replace(":", "_")
                    or self._results["facts"]["title"].split(" ")[0] in mod.name(), mod.name())
        self._check("installed mod has no Nexus id", str(general.get("modid", "0")).lstrip("-").isdigit()
                    and int(general.get("modid", "0")) <= 0, general.get("modid"))
        self._check("installed files are game data at the mod root",
                    any(f.lower().endswith((".esp", ".esm", ".esl")) and os.sep not in f for f in files), files[:10])
        self._check("download .meta marked installed", "installed=true" in
                    Path(str(self._zip) + ".meta").read_text(encoding="utf-8-sig"), "")

        # the window shows it as installed after a fresh lookup
        win.search(str(cfg["free_id"]))
        yield (lambda: not win.busy() and win._detail is not None, "re-lookup", 60)
        state = win._states.get(cfg["free_id"]) or {}
        self._check("window shows installed", bool(state.get("meta_installed")), state)
        yield from self._cleanup()

    def _cleanup(self):
        org = self._org
        if self._silent_before is not None:
            org.setPluginSetting(QUICK, "silent", self._silent_before)
        name = getattr(self, "_mod_name", None)
        if name and not CONFIG.get("remove_in_mo2", True):
            self._results["facts"]["left_for_runner"] = name  # run_live.py deletes mods\<name> after MO2 exits
        elif name:
            mod = org.modList().getMod(name)
            self._check("mod removed", mod is not None and org.removeMod(mod), name)
        z = getattr(self, "_zip", None)
        if z is not None:
            for p in (z, Path(str(z) + ".meta")):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            self._check("download removed", not z.exists(), z.name)
        win = self._window()
        if win is not None:
            win.close()
        yield None

    def _finish(self) -> None:
        self._results["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._results["passed"] = all(c["ok"] for c in self._results["checks"]) and bool(self._results["checks"])
        Path(CONFIG["results"]).write_text(json.dumps(self._results, indent=1), encoding="utf-8")
        QTimer.singleShot(1500, self._main.close)


def createPlugin() -> mobase.IPluginTool:
    return Harness()
