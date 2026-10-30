"""Media bin: the panel listing all media imported into the project."""

from __future__ import annotations

import os
from typing import Callable, List, Optional

from PySide6.QtCore import QMimeData, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QMenu,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.media_asset import MediaAsset

MEDIA_MIME_TYPE = "application/x-video-editor-asset"

SUPPORTED_EXTENSIONS = (
    "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.flv *.wmv *.mpg *.mpeg "
    "*.3gp *.3g2 *.ts *.mts *.m2ts *.ogv *.vob "  # video
    "*.mp3 *.wav *.aac *.flac *.ogg *.m4a *.opus *.wma *.aiff *.aif "  # audio
    "*.png *.jpg *.jpeg *.bmp *.webp *.tiff *.tif *.gif"  # images
)


class MediaListWidget(QListWidget):
    """A list widget whose items can be dragged onto the timeline."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setViewMode(QListWidget.IconMode)
        self.setIconSize(QSize(96, 54))
        self.setResizeMode(QListWidget.Adjust)
        self.setSpacing(8)
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setWordWrap(True)

    def startDrag(self, supportedActions) -> None:  # noqa: N802 (Qt override)
        item = self.currentItem()
        if item is None:
            return
        asset_id = item.data(Qt.UserRole)
        mime = QMimeData()
        mime.setData(MEDIA_MIME_TYPE, str(asset_id).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        if item.icon():
            drag.setPixmap(item.icon().pixmap(64, 36))
        drag.exec(Qt.CopyAction)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().paintEvent(event)
        # A brand-new project's bin is otherwise just a blank rectangle with
        # no indication that it's a drop target, or that "Import" above is
        # the other way in -- QListWidget has no built-in placeholder-text
        # equivalent to QLineEdit's, so this draws one directly.
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(QColor("#9aa1ad"))
            painter.drawText(
                QRectF(self.viewport().rect()).adjusted(16, 16, -16, -16),
                Qt.AlignCenter | Qt.TextWordWrap,
                "Drop video, audio, or image files here,\nor click Import above to get started.",
            )


class MediaBinWidget(QWidget):
    """Displays imported media and lets the user import more / add to timeline."""

    import_requested = Signal(list)  # list[str] file paths
    add_to_timeline_requested = Signal(str)  # asset id, append to a default track
    source_monitor_requested = Signal(str)  # asset id: preview it and mark In/Out (UI6)
    proxy_requested = Signal(str)  # asset id -> generate a preview proxy
    relink_requested = Signal()  # open File > Relink Media
    subclip_rename_requested = Signal(str)  # "sub:<id>"
    subclip_delete_requested = Signal(str)
    spoken_hit_activated = Signal(str, float)  # asset id, source seconds (F37)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._assets: List[MediaAsset] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("MEDIA BIN")
        title.setObjectName("SectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.import_btn = QPushButton("Import")
        self.import_btn.clicked.connect(self._on_import_clicked)
        header.addWidget(self.import_btn)
        layout.addLayout(header)

        # F37: search what is said in the clips (words kept by Generate Captions).
        self.speech_search = QLineEdit()
        self.speech_search.setPlaceholderText("Search speech…")
        self.speech_search.setAccessibleName("Search speech")
        self.speech_search.setToolTip("Find clips by what is said in them (after Generate Captions); "
                                      "double-click a result to go there.")
        self.speech_search.setClearButtonEnabled(True)
        self.speech_search.textChanged.connect(self._on_speech_search)
        layout.addWidget(self.speech_search)
        self.speech_results = QListWidget()
        self.speech_results.setVisible(False)
        self.speech_results.setMaximumHeight(160)
        self.speech_results.setAccessibleName("Speech search results")
        self.speech_results.itemActivated.connect(self._on_speech_hit)
        self.speech_results.itemDoubleClicked.connect(self._on_speech_hit)
        layout.addWidget(self.speech_results)

        self.list_widget = MediaListWidget()
        self.list_widget.setAccessibleName("Media bin")
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.list_widget, 1)

        self.hint_label = QLabel("Drag a clip onto the timeline, or double-click to append it.")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color: #9aa1ad; font-size: 11px;")
        self.hint_label.setVisible(False)  # nothing to drag yet; the empty list itself explains how to import
        layout.addWidget(self.hint_label)

        self.setAcceptDrops(True)

    # ------------------------------------------------------------------
    def _on_import_clicked(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Media",
            "",
            f"Media Files ({SUPPORTED_EXTENSIONS});;All Files (*)",
        )
        if paths:
            self.import_requested.emit(paths)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        """A video or audio file opens in the Source Monitor (UI6); a still image or a subclip
        (its range is already chosen) is appended to the timeline as before."""
        asset_id = item.data(Qt.UserRole)
        if not asset_id:
            return
        asset = next((a for a in self._assets if a.id == str(asset_id)), None)
        if asset is not None and not asset.is_image:
            self.source_monitor_requested.emit(str(asset_id))
        else:
            self.add_to_timeline_requested.emit(str(asset_id))

    def _on_speech_search(self, text: str) -> None:
        from core.transcript_search import search

        self.speech_results.clear()
        query = text.strip()
        if not query:
            self.speech_results.setVisible(False)
            return
        hits = search(self._assets, query)
        if not hits:
            item = QListWidgetItem("Not found (only clips you've generated captions for are searchable)")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.speech_results.addItem(item)
        for hit in hits:
            minutes, seconds = divmod(int(hit.start), 60)
            item = QListWidgetItem(f"{hit.asset_name} {minutes}:{seconds:02d} — {hit.snippet}")
            item.setData(Qt.UserRole, (hit.asset_id, hit.start))
            item.setToolTip(hit.snippet)
            self.speech_results.addItem(item)
        self.speech_results.setVisible(True)

    def _on_speech_hit(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.UserRole)
        if data:
            self.spoken_hit_activated.emit(str(data[0]), float(data[1]))

    def selected_asset_id(self):
        item = self.list_widget.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else None

    def _on_context_menu(self, pos) -> None:
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        asset_id = str(item.data(Qt.UserRole))
        menu = QMenu(self)
        if asset_id.startswith("sub:"):
            rename_action = menu.addAction("Rename Subclip...")
            delete_action = menu.addAction("Delete Subclip")
            chosen = menu.exec(self.list_widget.viewport().mapToGlobal(pos))
            if chosen == rename_action:
                self.subclip_rename_requested.emit(asset_id)
            elif chosen == delete_action:
                self.subclip_delete_requested.emit(asset_id)
            return
        monitor_action = menu.addAction("Open in Source Monitor")
        append_action = menu.addAction("Append to Timeline")
        menu.addSeparator()
        proxy_action = menu.addAction("Generate Preview Proxy")
        relink_action = menu.addAction("Relink Media...")
        chosen = menu.exec(self.list_widget.viewport().mapToGlobal(pos))
        if chosen == monitor_action:
            self.source_monitor_requested.emit(asset_id)
        elif chosen == append_action:
            self.add_to_timeline_requested.emit(asset_id)
        elif chosen == proxy_action:
            self.proxy_requested.emit(asset_id)
        elif chosen == relink_action:
            self.relink_requested.emit()

    @staticmethod
    def asset_label(asset: MediaAsset) -> str:
        """Name, duration and status tags: HDR / 10-bit sources, proxy ready, offline."""
        tags = []
        if asset.is_hdr:
            tags.append("HDR")
        elif asset.bit_depth > 8:
            tags.append(f"{asset.bit_depth}-bit")
        if asset.proxy_path and os.path.isfile(asset.proxy_path):
            tags.append("proxy")
        if asset.is_offline:
            tags.append("OFFLINE")
        suffix = f"  [{', '.join(tags)}]" if tags else ""
        return f"{asset.display_name}\n{MediaBinWidget._format_duration(asset.duration)}{suffix}"

    # ------------------------------------------------------------------
    def set_assets(self, assets: List[MediaAsset], thumbnail_provider: Optional[Callable[[MediaAsset], Optional[QPixmap]]] = None,
                   subclips=None) -> None:
        """The pool's files, each followed by its subclips (F29), indented with a ✂."""
        self._assets = assets
        self.list_widget.clear()
        by_asset: dict = {}
        for subclip in subclips or []:
            by_asset.setdefault(subclip.asset_id, []).append(subclip)
        for asset in assets:
            item = QListWidgetItem(self.asset_label(asset))
            item.setData(Qt.UserRole, asset.id)
            tooltip = asset.path
            if asset.is_offline:
                tooltip += "\nOffline: the file is missing. Right-click > Relink Media to find it."
                item.setForeground(QColor("#e5534b"))
            if asset.is_hdr:
                tooltip += f"\nHDR ({asset.color_transfer}); tone-mapped to SDR BT.709 on export."
            item.setToolTip(tooltip)
            pixmap = thumbnail_provider(asset) if thumbnail_provider else None
            if pixmap is not None and not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            self.list_widget.addItem(item)
            for subclip in by_asset.get(asset.id, []):
                sub_item = QListWidgetItem(
                    f"    ✂ {subclip.name}\n    {self._format_duration(subclip.source_in)}–{self._format_duration(subclip.source_out)}"
                    f" ({subclip.duration:.1f} s)")
                sub_item.setData(Qt.UserRole, subclip.item_id)
                sub_item.setToolTip(f"Subclip of {asset.display_name}: {subclip.source_in:.2f}–{subclip.source_out:.2f} s")
                if pixmap is not None and not pixmap.isNull():
                    sub_item.setIcon(QIcon(pixmap))
                self.list_widget.addItem(sub_item)
        self.hint_label.setVisible(bool(assets))

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds < 0:
            return "--:--"
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes:02d}:{secs:02d}"

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        # QWidget's default dragMoveEvent doesn't accept the event, so
        # without this override the cursor shows "not allowed" for the
        # entire drag hover -- even though dragEnterEvent/dropEvent already
        # make the drop itself work -- undermining the empty-state hint's
        # own promise that this panel is a drop target.
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            paths = [p for p in paths if p]
            if paths:
                self.import_requested.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)
