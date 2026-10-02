"""A professional dark theme for the application (Qt Style Sheet)."""

from __future__ import annotations

# Palette — a neutral, low-fatigue dark theme with a single accent color,
# in the spirit of professional creative tools (Resolve, Premiere, Kdenlive).
COLOR_BG_DARKEST = "#1a1d21"
COLOR_BG_DARK = "#20242a"
COLOR_BG_MEDIUM = "#282c34"
COLOR_BG_LIGHT = "#323740"
COLOR_BORDER = "#3a3f4a"
COLOR_TEXT = "#e6e8ec"
COLOR_TEXT_DIM = "#9aa1ad"
COLOR_ACCENT = "#4f8ef7"
COLOR_ACCENT_HOVER = "#6ba0f8"
COLOR_ACCENT_PRESSED = "#3d78dd"
COLOR_DANGER = "#e5534b"
COLOR_SUCCESS = "#3fb950"

CLIP_COLOR_VIDEO = "#3d6fb0"
CLIP_COLOR_AUDIO = "#3f9e6d"
CLIP_COLOR_IMAGE = "#a0729b"
CLIP_COLOR_TEXT = "#c9a24b"

# Clip names are drawn on this translucent dark strip (alpha 0..255) so white
# text stays readable on every clip colour: plain white on the text-clip gold
# measured 2.4:1, below the WCAG AA minimum of 4.5:1.
CLIP_LABEL_STRIP_ALPHA = 150
WCAG_AA_CONTRAST = 4.5


def _channel(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _rgb(color: str):
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def relative_luminance(color) -> float:
    r, g, b = _rgb(color) if isinstance(color, str) else color
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(fg, bg) -> float:
    """WCAG 2 contrast ratio between two colours (``#rrggbb`` or RGB tuples)."""
    lighter, darker = sorted((relative_luminance(fg), relative_luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def blend_over(color: str, overlay=(0, 0, 0), alpha: int = CLIP_LABEL_STRIP_ALPHA):
    """RGB of ``overlay`` at ``alpha`` (0..255) painted over ``color``."""
    a = alpha / 255.0
    return tuple(round(c * (1 - a) + o * a) for c, o in zip(_rgb(color), overlay))


def readable_text_color(background: str) -> str:
    """White or near-black, whichever contrasts more with ``background`` (badges)."""
    return "#ffffff" if contrast_ratio("#ffffff", background) >= contrast_ratio("#111111", background) else "#111111"

DARK_STYLESHEET = f"""
* {{
    font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif;
    font-size: 13px;
    color: {COLOR_TEXT};
}}

QMainWindow, QWidget {{
    background-color: {COLOR_BG_MEDIUM};
}}

QMenuBar {{
    background-color: {COLOR_BG_DARKEST};
    border-bottom: 1px solid {COLOR_BORDER};
    padding: 2px;
}}
QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background: {COLOR_BG_LIGHT};
}}
QMenu {{
    background-color: {COLOR_BG_DARK};
    border: 1px solid {COLOR_BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 24px 6px 12px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {COLOR_ACCENT};
    color: white;
}}
QMenu::separator {{
    height: 1px;
    background: {COLOR_BORDER};
    margin: 4px 8px;
}}

QToolBar {{
    background-color: {COLOR_BG_DARK};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    padding: 4px;
    spacing: 4px;
}}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 5px;
}}
QToolButton:hover {{
    background-color: {COLOR_BG_LIGHT};
    border: 1px solid {COLOR_BORDER};
}}
QToolButton:pressed, QToolButton:checked {{
    background-color: {COLOR_ACCENT_PRESSED};
}}

QStatusBar {{
    background-color: {COLOR_BG_DARKEST};
    border-top: 1px solid {COLOR_BORDER};
    color: {COLOR_TEXT_DIM};
}}

QSplitter {{
    background-color: {COLOR_BG_DARKEST};
}}
QSplitter::handle {{
    background-color: {COLOR_BG_DARKEST};
}}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}
QSplitter::handle:hover {{ background-color: {COLOR_ACCENT}; }}

/* The four main panels (media bin, preview, properties, timeline) share
   this "card" identity so the window reads as distinct panels rather
   than one flat surface. See MainWindow._build_ui(). */
QWidget#Panel {{
    background-color: {COLOR_BG_MEDIUM};
    border: 1px solid {COLOR_BORDER};
}}

QDockWidget {{
    titlebar-close-icon: none;
    color: {COLOR_TEXT};
}}
QDockWidget::title {{
    background: {COLOR_BG_DARK};
    padding: 6px;
    border-bottom: 1px solid {COLOR_BORDER};
}}

QGroupBox {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 8px;
    font-weight: 600;
    color: {COLOR_TEXT_DIM};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}

QLabel {{
    background: transparent;
}}
QLabel#SectionTitle {{
    color: {COLOR_TEXT_DIM};
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    font-size: 11px;
}}

QPushButton {{
    background-color: {COLOR_BG_LIGHT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 5px;
    padding: 6px 14px;
}}
QPushButton:hover {{
    background-color: #3a4049;
    border-color: {COLOR_ACCENT};
}}
QPushButton:pressed {{
    background-color: {COLOR_ACCENT_PRESSED};
}}
QPushButton:disabled {{
    color: {COLOR_TEXT_DIM};
    background-color: {COLOR_BG_MEDIUM};
}}
QPushButton#PrimaryButton {{
    background-color: {COLOR_ACCENT};
    border: none;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background-color: {COLOR_ACCENT_HOVER};
}}
QPushButton#PrimaryButton:pressed {{
    background-color: {COLOR_ACCENT_PRESSED};
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTimeEdit {{
    background-color: {COLOR_BG_DARK};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {COLOR_ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {COLOR_ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background-color: {COLOR_BG_DARK};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_ACCENT};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {COLOR_BG_DARK};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {COLOR_ACCENT};
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{
    background: {COLOR_ACCENT_HOVER};
}}
QSlider::sub-page:horizontal {{
    background: {COLOR_ACCENT};
    border-radius: 2px;
}}

QProgressBar {{
    background-color: {COLOR_BG_DARK};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    text-align: center;
    color: {COLOR_TEXT};
}}
QProgressBar::chunk {{
    background-color: {COLOR_ACCENT};
    border-radius: 3px;
}}

QListWidget, QTreeWidget, QTableWidget {{
    background-color: {COLOR_BG_DARK};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    outline: none;
}}
QListWidget::item, QTreeWidget::item {{
    padding: 4px;
    border-radius: 3px;
}}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background-color: {COLOR_ACCENT};
    color: white;
}}
QListWidget::item:hover:!selected {{
    background-color: {COLOR_BG_LIGHT};
}}
QHeaderView::section {{
    background-color: {COLOR_BG_DARK};
    color: {COLOR_TEXT_DIM};
    padding: 4px;
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
}}

QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    top: -1px;
}}
QTabBar::tab {{
    background: {COLOR_BG_DARK};
    padding: 6px 14px;
    border: 1px solid {COLOR_BORDER};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {COLOR_BG_MEDIUM};
    color: {COLOR_ACCENT};
}}

QScrollBar:vertical {{
    background: {COLOR_BG_DARK};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {COLOR_BG_LIGHT};
    min-height: 24px;
    border-radius: 5px;
    margin: 2px;
}}
QScrollBar::handle:vertical:hover {{ background: {COLOR_ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

QScrollBar:horizontal {{
    background: {COLOR_BG_DARK};
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {COLOR_BG_LIGHT};
    min-width: 24px;
    border-radius: 5px;
    margin: 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {COLOR_ACCENT}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QGraphicsView {{
    background-color: {COLOR_BG_DARKEST};
    border: none;
}}

QToolTip {{
    background-color: {COLOR_BG_DARKEST};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    padding: 4px;
}}
"""
