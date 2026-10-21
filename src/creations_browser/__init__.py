"""Creations Browser: a Mod Organizer 2 plugin that downloads free Bethesda Creations as ordinary zips.

Unofficial; not affiliated with Bethesda or ZeniMax. This module must stay importable without ``mobase`` or Qt, so the
plugin class lives in ``.plugin`` and is imported only when MO2 asks for it.
"""

from .core import VERSION as __version__


def createPlugin():
    """The entry point Mod Organizer 2 calls."""
    from .plugin import createPlugin as _create
    return _create()
