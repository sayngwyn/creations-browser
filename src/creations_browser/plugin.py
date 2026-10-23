"""The Mod Organizer 2 side of Creations Browser: one tool plugin (Tools menu) that opens the window.

Imported only by ``createPlugin()`` (so the package itself stays importable without ``mobase``). The off switch is Mod
Organizer 2's own: Settings > Plugins > "Creations Browser" > enabled. A disabled tool plugin is left out of the Tools menu
(``MainWindow::updateToolMenu`` drops tools that ``PluginContainer::isEnabled`` rejects), so nothing here can start; the
plugin also closes its window, and cancels a download, the moment MO2 reports it disabled.
"""

from __future__ import annotations

import logging
from pathlib import Path

import mobase
from PyQt6.QtGui import QIcon

from . import __version__, core, texts, theme

log = logging.getLogger("creations_browser")


def _version_info() -> "mobase.VersionInfo":
    parts = [int(p) for p in __version__.split(".") if p.isdigit()][:3]
    return mobase.VersionInfo(*(parts + [0] * (3 - len(parts))))


class CreationsBrowserPlugin(mobase.IPluginTool):
    NAME = "Creations Browser"

    def __init__(self):
        super().__init__()
        self._organizer = None
        self._window = None

    # ---- IPlugin

    def init(self, organizer) -> bool:
        self._organizer = organizer
        try:
            organizer.onPluginDisabled(self.NAME, self._on_disabled)
        except Exception as exc:  # an older MO2 without the hook: the Tools menu entry still disappears when disabled
            core.log_line(logging.WARNING, f"Creations Browser: could not watch the enabled switch ({type(exc).__name__})")
        try:
            organizer.modList().onModInstalled(self._on_mod_installed)
        except Exception as exc:  # without it an installed Creation simply has no web page link
            core.log_line(logging.WARNING, f"Creations Browser: could not watch installs ({type(exc).__name__})")
        return True

    def name(self) -> str:
        return self.NAME

    def author(self) -> str:
        return "sayngwyn"

    def description(self) -> str:
        return texts.TOOL_DESCRIPTION

    def version(self):
        return _version_info()

    def settings(self):
        return [mobase.PluginSetting("theme", texts.THEME_SETTING, theme.DEFAULT_MODE)]

    # ---- IPluginTool

    def displayName(self) -> str:
        return self.NAME

    def tooltip(self) -> str:
        return texts.TOOL_TOOLTIP

    def icon(self):
        return QIcon()

    def display(self) -> None:
        try:
            if not self._is_enabled():
                return
            game, downloads = self._instance_facts()
            if self._window is None:
                from .window import CreationsWindow
                self._window = CreationsWindow(self._parentWidget(), game, downloads, theme_mode=self._saved_theme(),
                                               on_theme_change=self._save_theme)
            else:
                self._window.configure(game, downloads)
                self._window.apply_theme(self._saved_theme(), save=False)  # a change made in MO2's Settings meanwhile
            self._window.show()
            self._window.raise_()
            self._window.activateWindow()
        except Exception as exc:  # display() must not throw into MO2
            core.log_unexpected(f"Creations Browser could not open its window: {type(exc).__name__}")

    # ---- helpers

    def _is_enabled(self) -> bool:
        try:
            return bool(self._organizer.isPluginEnabled(self.NAME))
        except Exception:  # MO2 only lists enabled tools, so when it cannot say, carry on
            return True

    def _saved_theme(self) -> str:
        """The window colours the user picked last time (MO2's plugin settings), or the default."""
        try:
            value = self._organizer.pluginSetting(self.NAME, "theme")
        except Exception as exc:
            core.log_line(logging.WARNING, f"Creations Browser: could not read the theme setting ({type(exc).__name__})")
            return theme.DEFAULT_MODE
        value = " ".join(str(value).split()).lower() if value is not None else ""  # MO2's settings box is free text
        by_label = {label.lower(): key for key, label in theme.MODE_LABELS.items()}  # "Match MO2" typed in reads as "mo2"
        value = by_label.get(value, value)
        return value if value in theme.MODES else theme.DEFAULT_MODE

    def _save_theme(self, mode: str) -> None:
        self._organizer.setPluginSetting(self.NAME, "theme", mode)

    def _instance_facts(self) -> tuple[str | None, str | None]:
        """(game key, Downloads folder) of the MO2 instance, each None when MO2 does not say."""
        organizer = self._organizer
        game = None
        try:
            managed = organizer.managedGame()
            game = texts.instance_game_key(managed.gameShortName() if managed is not None else None)
        except Exception as exc:
            core.log_line(logging.WARNING, f"Creations Browser: could not read the managed game ({type(exc).__name__})")
        try:
            downloads = str(organizer.downloadsPath()) or None
        except Exception as exc:
            core.log_line(logging.WARNING, f"Creations Browser: could not read the Downloads folder ({type(exc).__name__})")
            downloads = None
        return game, downloads

    def _on_disabled(self) -> None:
        """Called from MO2's C++ when the plugin is switched off: it must never raise."""
        try:
            window = self._window
            if window is not None:
                window.shutdown()
                window.close()
        except Exception as exc:
            core.log_unexpected(f"Creations Browser could not close its window on disable: {type(exc).__name__}")

    def _on_mod_installed(self, mod) -> None:
        """MO2 installed a mod: when it came from one of our downloads, make the Creation's page the mod's web page.

        MO2 copies no ``url`` from a download's .meta into the installed mod's meta.ini (``InstallationManager::doInstall``
        writes gameName, modid, version, newestVersion, category, nexusFileStatus, installationFile and repository only),
        and with no Nexus id it shows no link. ``IModInterface.setUrl`` sets the mod's custom URL (and turns it on), the
        way MO2's FOMOD installer does for its own pages. Never raises."""
        try:
            if not self._is_enabled() or mod.repository() != core.REPOSITORY or mod.url():
                return
            _game, downloads = self._instance_facts()
            name = str(mod.installationFile() or "")
            if not downloads or not name:
                return
            archive = Path(name) if Path(name).is_absolute() else Path(downloads) / name
            meta = core.read_meta(archive)
            page = meta.get("url", "")
            if meta.get("creationscontentid") and page.startswith("https://creations.bethesda.net/"):
                mod.setUrl(page)
        except Exception as exc:
            core.log_unexpected(f"Creations Browser could not set a page link on an installed mod: {type(exc).__name__}")


def createPlugin() -> mobase.IPlugin:
    return CreationsBrowserPlugin()
