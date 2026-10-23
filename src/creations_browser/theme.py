"""The window's colours: four choices of its own, whatever theme MO2 itself runs (2026-10-22).

- "sayngwyn" (the default): sayngwyn's colours, from the character sheet (the sayngwyn-mod-style skill's "App and window theme"
  table), with the brand mark in the empty details pane.
- "light" and "dark": plain, unbranded themes with a neutral blue accent, the same values as sayngwyn's other plugins
  (Modlist Companion's ``viewer\\theme.py``).
- "mo2" ("Match MO2"): sets nothing, so the window looks exactly as MO2's current theme draws it.

The first three set a palette and a style sheet on the Creations Browser window only (every rule is scoped to
``#creationsBrowser``), so MO2 is never restyled, and the sheet covers every kind of widget the window uses, so MO2's own
sheet can't show through. In sayngwyn, red means a selected tab or "the one main action" (the Download button), nothing else;
selected rows and hovered menu items are the deep red; checked boxes, scrollbars and everyday buttons are silver; tooltips and
menus sit on the raised colour; problems use a pink-red alarm colour (never the brand red) next to a warning sign and words.
Every text colour reads at 4.5:1 or better on every surface it can sit on (``text_pairs`` lists them; the tests check each).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QFontDatabase, QIcon, QImage, QPainter, QPalette, QPixmap

MODES = ("sayngwyn", "light", "dark", "mo2")  # in the picker's order; the keys are what MO2's plugin settings store
DEFAULT_MODE = "sayngwyn"  # what anyone with no saved choice sees
# Branding Manager's wording (2026-10-22), the same in every plugin. No "&": a combo or menu would read it as a shortcut key.
MODE_LABELS = {"sayngwyn": "sayngwyn", "light": "Light", "dark": "Dark", "mo2": "Match MO2"}
OBJECT_NAME = "creationsBrowser"  # the window's object name, which scopes every style rule

# The character sheet's palette (sayngwyn, 2026-10-22; the sayngwyn-mod-style skill, "App and window theme").
WINDOW = "#0f0d0e"     # the window (the sheet's shirt), fading down to the panel colour
SURFACE = "#211a1a"    # panels: the list, the text boxes, the tabs (the black star)
ALTERNATE = "#262020"
RAISED = "#2b2324"     # hovered tabs, checked boxes, tooltips and menus
BUTTON = "#262020"
BORDER = "#403839"
STRIPE = "#767375"     # neutral side stripes, and a hovered text box's border
TEXT = "#dededb"       # bone (the white star)
MUTED = "#aca3a2"      # secondary text, disabled text, placeholders
SILVER = "#c2c2c4"     # everyday button and checkbox outlines, scrollbar handles
ACCENT = "#812727"     # selected tabs and the one primary button only (the red star)
ACCENT_HOVER = "#963030"
ON_ACCENT = "#ffffff"
DEEP = "#601d2c"       # the selected row and a hovered menu item (the deep-red star)
ROW_HOVER = "#3f1b24"  # a hovered row
SCROLL_ALPHA, SCROLL_HOVER_ALPHA = 89, 153  # the scrollbar handle: about 35%, about 60% while hovered
SHADOW = DEEP         # the sheet's offset shadow: down and right, no blur
ALARM = "#ff7993"     # problems, as text: a pinker red than the brand red, always with the warning sign
WARNING_SIGN = "⚠"

# Light and Dark: Modlist Companion's values (its viewer\theme.py ``_FIXED``), so every plugin of sayngwyn's matches.
_FIXED = {
    "light": {"window": "#f3f4f6", "base": "#ffffff", "alternate": "#f6f7f9", "text": "#1b1f24", "faint": "#6b7280",
              "highlight": "#2463d1", "button": "#ffffff", "alarm": "#c82344"},
    "dark": {"window": "#1b1e23", "base": "#23272e", "alternate": "#292e36", "text": "#e6e8eb", "faint": "#8a929c",
             "highlight": "#4f8ff7", "button": "#2a2f36", "alarm": "#f0728b"},
}
SELECTED_ALPHA, HOVER_ALPHA = 70, 35  # Light and Dark: a selected and a hovered row are the accent at this alpha over the panel

MIN_CONTRAST = 4.5
_LIFT_TARGET = 4.6  # colours lifted to readability aim a little higher, so rounding in the drawn pixels never drops below 4.5
_KEEP_AT = 4.45     # a theme's own colour that rounds to 4.5 is kept as it is (Windows' white on its own blue is 4.49)

# The brand where the eye rests (the skill's "Brand inside the tools"): only the empty details pane carries the mark (faded)
# and Sayngwyn Serif with the red offset shadow, and only in sayngwyn; never the list, filters or buttons.
ASSETS = Path(__file__).resolve().parent / "assets"
MARK_FILE = "sayngwyn-mark.png"      # the stag skull, rendered from Branding's skull-main-web-blood.svg
FONT_FILE = "SayngwynSerif-Regular.ttf"  # free under the SIL Open Font License (OFL.txt beside it)
TICK_FILE, MUTED_TICK_FILE = "check-tick.svg", "check-tick-muted.svg"  # a checked box's tick: bone, and muted when disabled
WHITE_TICK_FILE = "check-tick-white.svg"  # Light and Dark: a white tick on the accent fill
MARK_OPACITY = 0.2
BRAND_TEXT_PX = 20                   # Sayngwyn Serif only at 18 px or more
SHADOW_SHARE = 0.06                  # the shadow sits down and right by this share of the letter size


@functools.cache
def brand_family() -> str | None:
    """The family name of Sayngwyn Serif once loaded into the application, or None when it can't be (the hint then keeps
    the window's own font)."""
    font_id = QFontDatabase.addApplicationFont(str(ASSETS / FONT_FILE))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    return families[0] if families else None


@functools.cache
def _mark_image() -> QImage | None:  # a QImage, not a QPixmap: safe to keep after the application has gone
    image = QImage(str(ASSETS / MARK_FILE))
    return None if image.isNull() else image


def mark(height: int, ratio: float) -> QPixmap | None:
    """The mark ``height`` device-independent pixels tall, sharp at the screen's pixel ``ratio``."""
    image = _mark_image()
    if image is None:
        return None
    pixmap = QPixmap.fromImage(image.scaledToHeight(round(height * ratio), Qt.TransformationMode.SmoothTransformation))
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def tinted(icon: QIcon, colour: str, disabled: str, size: int, ratio: float) -> QIcon:
    """``icon`` redrawn in one ``colour`` (and ``disabled`` for a greyed-out item), keeping its shape. Qt's stock menu icons
    follow Windows' light or dark mode, not the window's theme, so on one of the two they would barely show."""
    result = QIcon()
    for mode, paint in ((QIcon.Mode.Normal, colour), (QIcon.Mode.Active, colour), (QIcon.Mode.Selected, colour),
                        (QIcon.Mode.Disabled, disabled)):
        pixmap = icon.pixmap(QSize(size, size), ratio)
        if pixmap.isNull():
            continue
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)  # the colour, only where the icon is
        painter.fillRect(pixmap.rect(), QColor(paint))
        painter.end()
        result.addPixmap(pixmap, mode)
    return result


def _linear(value: float) -> float:
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(colour: QColor) -> float:
    """Relative luminance of a colour (WCAG 2.x), 0 (black) to 1 (white)."""
    return 0.2126 * _linear(colour.redF()) + 0.7152 * _linear(colour.greenF()) + 0.0722 * _linear(colour.blueF())


def contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG contrast ratio of two colours, 1 (none) to 21 (black on white)."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def over(top: str, alpha: int, bottom: str) -> QColor:
    """``top`` drawn at ``alpha`` (0..255) on ``bottom``."""
    t, b, share = QColor(top), QColor(bottom), alpha / 255
    return QColor(*(round(y + (x - y) * share) for x, y in
                    ((t.red(), b.red()), (t.green(), b.green()), (t.blue(), b.blue()))))


def _mix(a: QColor, b: QColor, share: float) -> QColor:
    """``a`` moved ``share`` (0..1) of the way towards ``b``."""
    return QColor(*(round(x + (y - x) * share) for x, y in
                    ((a.red(), b.red()), (a.green(), b.green()), (a.blue(), b.blue()))))


def readable(colour: QColor, backgrounds: Iterable[QColor]) -> QColor:
    """``colour`` moved towards black or white, as little as needed, until it reads at 4.5:1 on every background. Only its
    lightness moves, so a theme's colour keeps its hue."""
    around = list(backgrounds)
    if not around or all(contrast_ratio(colour, back) >= _KEEP_AT for back in around):
        return colour  # already readable: left exactly as the theme has it
    behind = sum(luminance(back) for back in around) / len(around)
    white, black = QColor(255, 255, 255), QColor(0, 0, 0)
    # Away from the backgrounds: lighter text gets lighter, darker text darker; the other way only if that can't reach it.
    for target in ((white, black) if luminance(colour) >= behind else (black, white)):
        for step in range(1, 41):
            candidate = _mix(colour, target, step / 40)
            if all(contrast_ratio(candidate, back) >= _LIFT_TARGET for back in around):
                return candidate
    return max((white, black), key=lambda c: min(contrast_ratio(c, back) for back in around))


def _accent_fill(accent: QColor) -> tuple[QColor, QColor]:
    """(fill, text on the fill): white text on the accent darkened as far as needed, or black text if it is too light."""
    white, black = QColor(255, 255, 255), QColor(0, 0, 0)
    for step in range(0, 17):
        fill = _mix(accent, black, step / 40)
        if contrast_ratio(white, fill) >= _LIFT_TARGET:
            return fill, white
    for step in range(0, 41):
        fill = _mix(accent, white, step / 40)
        if contrast_ratio(black, fill) >= _LIFT_TARGET:
            return fill, black
    return white, black


@dataclass(frozen=True)
class Scheme:
    """Every colour one theme's sheet and palette use, as "#rrggbb"."""
    window: str          # the window's top
    window_end: str      # its bottom (sayngwyn fades into the panel colour; Light and Dark are flat)
    surface: str         # panels: the list, the text boxes, the tabs
    alternate: str
    raised: str          # tooltips, menus, hovered tabs
    button: str
    border: str
    text: str
    muted: str           # secondary, disabled and placeholder text
    outline: str         # everyday button and checkbox outlines, scrollbar handles, the progress fill
    accent: str          # selected tabs and the primary button
    accent_hover: str
    on_accent: str
    selected: str        # a selected row, a hovered or picked menu item
    row_hover: str
    alarm: str           # problems, as text
    checked_fill: str    # a checked box
    checked_border: str
    tick: str            # the tick's file in assets\
    muted_tick: str
    edge: str            # the edge around a selected row, so it stays distinct from any highlight colour
    input_hover: str     # a hovered text box or dropdown's border


SAYNGWYN = Scheme(window=WINDOW, window_end=SURFACE, surface=SURFACE, alternate=ALTERNATE, raised=RAISED, button=BUTTON,
                  border=BORDER, text=TEXT, muted=MUTED, outline=SILVER, accent=ACCENT, accent_hover=ACCENT_HOVER,
                  on_accent=ON_ACCENT, selected=DEEP, row_hover=ROW_HOVER, alarm=ALARM, checked_fill=RAISED,
                  checked_border=SILVER, tick=TICK_FILE, muted_tick=MUTED_TICK_FILE, edge=TEXT, input_hover=STRIPE)


def _neutral(name: str) -> Scheme:
    """Light or Dark, derived from Modlist Companion's values the way its own theme derives them."""
    c = _FIXED[name]
    dark = name == "dark"
    window, base, text = QColor(c["window"]), QColor(c["base"]), QColor(c["text"])
    raised = base.lighter(118) if dark else base.darker(104)
    accent, on_accent = _accent_fill(QColor(c["highlight"]))
    selected, row_hover = over(accent.name(), SELECTED_ALPHA, c["base"]), over(accent.name(), HOVER_ALPHA, c["base"])
    surfaces = [window, base, QColor(c["alternate"]), raised, QColor(c["button"])]
    return Scheme(window=c["window"], window_end=c["window"], surface=c["base"], alternate=c["alternate"],
                  raised=raised.name(), button=c["button"],
                  border=(window.lighter(150) if dark else window.darker(118)).name(), text=c["text"],
                  muted=readable(QColor(c["faint"]), surfaces).name(), outline=c["text"], accent=accent.name(),
                  accent_hover=_mix(accent, QColor(0, 0, 0), 0.1).name(), on_accent=on_accent.name(),
                  selected=selected.name(), row_hover=row_hover.name(),
                  alarm=readable(QColor(c["alarm"]), surfaces).name(), checked_fill=accent.name(),
                  checked_border=accent.name(), tick=WHITE_TICK_FILE, muted_tick=MUTED_TICK_FILE, edge=c["text"],
                  input_hover=c["faint"])


SCHEMES = {"sayngwyn": SAYNGWYN, "light": _neutral("light"), "dark": _neutral("dark")}  # "mo2" has none: it sets nothing


def _hex(colour: QColor) -> str:
    return colour.name()


def _rgba(colour: str, alpha: int) -> str:
    c = QColor(colour)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def text_pairs(mode: str = "sayngwyn") -> list[tuple[str, str, str]]:
    """(what, text colour, background) for every text the ``mode`` theme draws, on every surface it can sit on."""
    s = SCHEMES[mode]
    plain = [s.window, s.window_end, s.surface, s.alternate, s.raised, s.button, _hex(over(s.window, 128, s.window_end))]
    pairs = [("text", s.text, back) for back in plain + [s.selected, s.row_hover]]
    pairs += [("muted", s.muted, back) for back in plain]
    pairs += [("alarm", s.alarm, back) for back in plain]
    pairs += [("on accent", s.on_accent, back) for back in (s.accent, s.accent_hover)]
    pairs += [("button text on a hovered or pressed outline", s.text, _hex(over(s.outline, alpha, back)))
              for alpha in (28, 52) for back in (s.window, s.surface)]
    pairs += [("progress percentage on the fill", s.text, s.selected)]
    pairs += [("tooltip, menu or combo list item", s.text, s.raised), ("hovered or picked menu or combo item", s.text, s.selected)]
    return pairs


def _asset_url(name: str) -> str:
    return ASSETS.joinpath(name).as_posix().replace('"', '\\"')  # forward slashes: a style sheet url() reads them on Windows


def palette_for(mode: str, mo2: QPalette) -> QPalette:
    """The window's palette: a copy of MO2's for "mo2", otherwise the theme's colours on top of it."""
    palette = QPalette(mo2)
    s = SCHEMES.get(mode)
    if s is None:
        return palette
    role = QPalette.ColorRole
    roles = {
        role.Window: s.window, role.WindowText: s.text, role.Base: s.surface, role.AlternateBase: s.alternate, role.Text: s.text,
        role.Button: s.button, role.ButtonText: s.text, role.Highlight: s.selected, role.HighlightedText: s.text,
        role.ToolTipBase: s.raised, role.ToolTipText: s.text, role.PlaceholderText: s.muted, role.BrightText: s.text,
        role.Link: s.text, role.LinkVisited: s.text,
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for r, value in roles.items():
            palette.setColor(group, r, QColor(value))
    for r in (role.Text, role.WindowText, role.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, r, QColor(s.muted))
    return palette


def stylesheet(mode: str) -> str:
    """The window's style sheet: empty for "mo2"; otherwise every rule is scoped to the window, and every kind of widget the
    window uses has rules, so MO2's own sheet can't show through."""
    s = SCHEMES.get(mode)
    if s is None:
        return ""
    w = f"#{OBJECT_NAME}"
    return f"""
{w} {{ background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1, stop: 0 {s.window}, stop: 1 {s.window_end}); color: {s.text}; }}
{w} QLabel, {w} QCheckBox {{ color: {s.text}; background: transparent; }}
{w} QLabel#notice {{ color: {s.muted}; }}
{w} QLabel[tone="error"] {{ color: {s.alarm}; }}
{w} QScrollArea, {w} QWidget#qt_scrollarea_viewport, {w} QWidget#detailPage, {w} QWidget#emptyState, {w} QStackedWidget,
{w} QSplitter, {w} QSplitter > QWidget {{ background: transparent; }}
{w} QSplitter::handle {{ background: transparent; }}
{w} QFrame#rule {{ border: none; background: {s.border}; min-height: 1px; max-height: 1px; }}

{w} QCheckBox::indicator {{ width: 13px; height: 13px; border-radius: 3px; }}
{w} QCheckBox::indicator:unchecked {{ background: {s.surface}; border: 1px solid {_rgba(s.outline, 160)}; }}
{w} QCheckBox::indicator:unchecked:hover {{ border: 1px solid {_rgba(s.outline, 235)}; }}
{w} QCheckBox::indicator:checked {{
    background: {s.checked_fill}; border: 1px solid {_rgba(s.checked_border, 200)}; image: url("{_asset_url(s.tick)}");
}}
{w} QCheckBox::indicator:checked:hover {{ border: 1px solid {s.checked_border}; }}
{w} QCheckBox::indicator:disabled {{ background: transparent; border: 1px solid {_rgba(s.outline, 55)}; }}
{w} QCheckBox::indicator:checked:disabled {{ image: url("{_asset_url(s.muted_tick)}"); }}

{w} QLineEdit, {w} QComboBox, {w} QPlainTextEdit {{
    color: {s.text}; background: {s.surface}; border: 1px solid {s.border}; border-radius: 6px; padding: 3px 6px;
    selection-background-color: {s.selected}; selection-color: {s.text};
}}
{w} QLineEdit:hover, {w} QComboBox:hover, {w} QPlainTextEdit:hover {{ border: 1px solid {s.input_hover}; }}
{w} QLineEdit:focus, {w} QComboBox:focus, {w} QPlainTextEdit:focus {{ border: 1px solid {s.text}; }}
{w} QComboBox:disabled, {w} QLineEdit:disabled, {w} QPlainTextEdit:disabled {{
    color: {s.muted}; background: {s.window}; border: 1px solid {s.border};
}}
{w} QComboBox QAbstractItemView {{
    color: {s.text}; background: {s.raised}; border: 1px solid {s.border}; outline: none;
    selection-background-color: {s.selected}; selection-color: {s.text};
}}
{w} QComboBox QAbstractItemView::item {{ color: {s.text}; background: {s.raised}; padding: 3px 6px; }}
{w} QComboBox QAbstractItemView::item:hover, {w} QComboBox QAbstractItemView::item:selected {{
    color: {s.text}; background: {s.selected};
}}

{w} QToolTip {{ color: {s.text}; background: {s.raised}; border: 1px solid {s.border}; padding: 3px 5px; }}
{w} QMenu {{ color: {s.text}; background: {s.raised}; border: 1px solid {s.border}; padding: 3px; }}
{w} QMenu::item {{ padding: 4px 18px; border-radius: 4px; }}
{w} QMenu::item:selected {{ color: {s.text}; background: {s.selected}; }}
{w} QMenu::item:disabled {{ color: {s.muted}; background: transparent; }}
{w} QMenu::separator {{ height: 1px; background: {s.border}; margin: 3px 6px; }}

{w} QListWidget {{
    color: {s.text}; background: {s.surface}; border: 1px solid {s.border}; border-radius: 10px; padding: 4px;
    alternate-background-color: {s.alternate};
    selection-background-color: {s.selected}; selection-color: {s.text};  /* the rows' painter reads these from the palette */
}}
{w} QListWidget:focus {{ border: 1px solid {_rgba(s.text, 150)}; }}
{w} QListWidget::item {{ border-radius: 0px; }}
{w} QListWidget::item:hover {{ background: {s.row_hover}; }}
{w} QListWidget::item:selected {{
    background: {s.selected}; color: {s.text};
    border: 1px solid {s.edge}; border-left: 3px solid {s.edge}; border-right: none;
}}

{w} QTabWidget::pane {{ border: 1px solid {s.border}; border-radius: 6px; background: {s.surface}; top: -1px; }}
{w} QTabBar::tab {{
    color: {s.text}; background: {s.surface}; border: 1px solid {s.border}; border-bottom: none; padding: 4px 14px;
    border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px;
}}
{w} QTabBar::tab:!selected:hover {{ background: {s.raised}; }}
{w} QTabBar::tab:selected {{ color: {s.on_accent}; background: {s.accent}; border-color: {s.accent}; }}
{w} QTabBar::tab:focus {{ border: 1px solid {s.text}; border-bottom: none; }}

{w} QPushButton {{
    color: {s.text}; background: transparent; border: 1px solid {_rgba(s.outline, 115)}; border-radius: 6px; padding: 4px 12px;
}}
{w} QPushButton:hover {{ border: 1px solid {_rgba(s.outline, 235)}; background: {_rgba(s.outline, 28)}; }}
{w} QPushButton:pressed {{ background: {_rgba(s.outline, 52)}; }}
{w} QPushButton:focus {{ border: 1px solid {s.text}; }}
{w} QPushButton:disabled {{ color: {s.muted}; border: 1px solid {_rgba(s.outline, 55)}; background: transparent; }}
{w} QPushButton[primary="true"] {{ color: {s.on_accent}; background: {s.accent}; border: 1px solid {s.accent}; }}
{w} QPushButton[primary="true"]:hover {{ background: {s.accent_hover}; border: 1px solid {s.accent_hover}; }}
{w} QPushButton[primary="true"]:focus {{ border: 1px solid {s.text}; }}
{w} QPushButton[primary="true"]:disabled {{
    color: {s.muted}; background: transparent; border: 1px solid {_rgba(s.outline, 55)};
}}

{w} QProgressBar {{ border: 1px solid {s.border}; border-radius: 5px; background: {s.surface}; color: {s.text}; text-align: center; }}
{w} QProgressBar::chunk {{ background: {s.selected}; border-radius: 4px; }}

{w} QScrollBar:vertical {{ background: {s.surface}; width: 10px; margin: 0px; }}
{w} QScrollBar:horizontal {{ background: {s.surface}; height: 10px; margin: 0px; }}
{w} QScrollBar::handle {{
    background: {_rgba(s.outline, SCROLL_ALPHA)}; border-radius: 4px; margin: 1px; min-height: 30px; min-width: 30px;
}}
{w} QScrollBar::handle:hover {{ background: {_rgba(s.outline, SCROLL_HOVER_ALPHA)}; }}
{w} QScrollBar::add-line, {w} QScrollBar::sub-line {{ height: 0px; width: 0px; }}
{w} QScrollBar::add-page, {w} QScrollBar::sub-page {{ background: transparent; }}
"""
