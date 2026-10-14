import os
import re
import sys
import hashlib
import json
import webbrowser
import tempfile
import subprocess
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QObject, QThread, Signal, Qt, QSize, QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtGui import QDesktopServices, QPixmap, QIcon, QPainter
from PySide6.QtCore import QUrl
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem,
    QFileDialog, QMessageBox, QProgressBar, QCheckBox, QFrame, QSplashScreen, QGraphicsOpacityEffect,
    QComboBox, QButtonGroup, QStackedWidget, QScrollArea
)

APP_NAME = "PHOENIX"
APP_VERSION = "0.8.0"

def resource_path(relative_path):
    """Return a resource path for both source runs and PyInstaller builds."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


USER_AGENT = "PHOENIX/0.2 (desktop image downloader)"


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("Похоже, ссылка указана неправильно.")
    return url


def discover_images(page_url: str):
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(page_url, headers=headers, timeout=25)
    response.raise_for_status()
    if "text/html" not in response.headers.get("Content-Type", "").lower() and "<html" not in response.text[:1000].lower():
        raise ValueError("По этой ссылке не удалось получить HTML-страницу.")

    soup = BeautifulSoup(response.text, "html.parser")
    found = []

    def add(candidate):
        if not candidate:
            return
        candidate = candidate.strip()
        if not candidate or candidate.startswith(("data:", "blob:", "javascript:")):
            return
        absolute = urljoin(response.url, candidate)
        if urlparse(absolute).scheme not in ("http", "https"):
            return
        if absolute not in found:
            found.append(absolute)

    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-lazy-src"):
            add(img.get(attr))
        for attr in ("srcset", "data-srcset"):
            value = img.get(attr)
            if value:
                for part in value.split(","):
                    candidate = part.strip().split()
                    if candidate:
                        add(candidate[0])

    for source in soup.find_all("source"):
        for attr in ("src", "srcset"):
            value = source.get(attr)
            if value:
                if attr == "src":
                    add(value)
                else:
                    for part in value.split(","):
                        candidate = part.strip().split()
                        if candidate:
                            add(candidate[0])

    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        if key in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
            add(meta.get("content"))

    return found, response.url


class UpdateCheckWorker(QObject):
    result = Signal(dict)
    error = Signal(str)

    def __init__(self, owner, repo, current_version):
        super().__init__()
        self.owner = owner
        self.repo = repo
        self.current_version = current_version

    def run(self):
        try:
            api_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/releases/latest"
            response = requests.get(
                api_url,
                headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
                timeout=10,
            )
            response.raise_for_status()
            release = response.json()
            latest = str(release.get("tag_name", "")).strip().lstrip("vV")
            if not latest:
                raise ValueError("В релизе GitHub не найден номер версии.")
            result = {
                "latest": latest,
                "current": self.current_version,
                "url": release.get("html_url", f"https://github.com/{self.owner}/{self.repo}/releases"),
                "notes": release.get("body", "")[:1200],
                "assets": [
                    {"name": a.get("name", ""), "url": a.get("browser_download_url", "")}
                    for a in release.get("assets", [])
                ],
            }
            self.result.emit(result)
        except Exception as exc:
            self.error.emit(str(exc))


def version_tuple(value):
    numbers = re.findall(r"\d+", str(value))
    return tuple(int(n) for n in numbers[:4]) if numbers else (0,)


class ScanWorker(QObject):
    found = Signal(list, str)
    error = Signal(str)

    def __init__(self, url):
        super().__init__()
        self.url = url

    def run(self):
        try:
            urls, final_url = discover_images(self.url)
            self.found.emit(urls, final_url)
        except Exception as exc:
            self.error.emit(str(exc))


class ThumbnailWorker(QObject):
    thumbnail = Signal(str, bytes)

    def __init__(self, urls):
        super().__init__()
        self.urls = urls

    def run(self):
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        for url in self.urls:
            try:
                r = session.get(url, timeout=12, stream=True)
                r.raise_for_status()
                if not r.headers.get("Content-Type", "").lower().startswith("image/"):
                    continue
                data = bytearray()
                for chunk in r.iter_content(32 * 1024):
                    if chunk:
                        data.extend(chunk)
                    if len(data) > 2_500_000:
                        break
                self.thumbnail.emit(url, bytes(data))
            except Exception:
                pass


class DownloadWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(int, int, str)
    error = Signal(str)

    def __init__(self, urls, folder):
        super().__init__()
        self.urls = urls
        self.folder = folder
        self.cancelled = False

    def run(self):
        ok, failed = 0, 0
        try:
            os.makedirs(self.folder, exist_ok=True)
            session = requests.Session()
            session.headers.update({"User-Agent": USER_AGENT})
            total = len(self.urls)
            for index, image_url in enumerate(self.urls, start=1):
                if self.cancelled:
                    break
                try:
                    with session.get(image_url, timeout=30, stream=True) as r:
                        r.raise_for_status()
                        ctype = r.headers.get("Content-Type", "").split(";")[0].lower()
                        if not ctype.startswith("image/"):
                            failed += 1
                            self.progress.emit(index, total, "Пропущен файл, который не является изображением")
                            continue
                        name = os.path.basename(unquote(urlparse(image_url).path)) or "image"
                        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "image"
                        if "." not in name:
                            name += {
                                "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                                "image/gif": ".gif", "image/avif": ".avif", "image/bmp": ".bmp"
                            }.get(ctype, ".img")
                        path = os.path.join(self.folder, name)
                        stem, ext = os.path.splitext(name)
                        n = 2
                        while os.path.exists(path):
                            path = os.path.join(self.folder, f"{stem}_{n}{ext}")
                            n += 1
                        with open(path, "wb") as out:
                            for chunk in r.iter_content(64 * 1024):
                                if chunk:
                                    out.write(chunk)
                    ok += 1
                    self.progress.emit(index, total, f"Скачано: {os.path.basename(path)}")
                except Exception as exc:
                    failed += 1
                    self.progress.emit(index, total, f"Не удалось скачать файл: {exc}")
            self.finished.emit(ok, failed, self.folder)
        except Exception as exc:
            self.error.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"PHOENIX {APP_VERSION} — Image Downloader")
        self.resize(1280, 800)
        self.setMinimumSize(980, 700)
        self.urls = []
        self.items_by_url = {}
        self.threads = []
        self.workers = []
        self.download_folder = os.path.join(os.path.expanduser("~"), "Downloads", "Phoenix Downloads")
        self.active_filter = "ALL"

        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Sidebar
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(238)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 18, 16, 16)
        side.setSpacing(10)

        brand_icon = QLabel()
        brand_icon.setObjectName("phoenixMark")
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon.setFixedHeight(118)
        logo_renderer = QSvgRenderer(resource_path("phoenix.svg"))
        logo_pixmap = QPixmap(190, 142)
        logo_pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(logo_pixmap)
        logo_renderer.render(painter)
        painter.end()
        brand_icon.setPixmap(logo_pixmap.scaled(176, 118, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        # Subtle breathing animation for the phoenix mark.
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        self.phoenix_glow = QGraphicsOpacityEffect(brand_icon)
        brand_icon.setGraphicsEffect(self.phoenix_glow)
        self.phoenix_pulse = QPropertyAnimation(self.phoenix_glow, b"opacity", self)
        self.phoenix_pulse.setDuration(2100)
        self.phoenix_pulse.setStartValue(0.72)
        self.phoenix_pulse.setEndValue(1.0)
        self.phoenix_pulse.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.phoenix_pulse.setLoopCount(-1)
        self.phoenix_pulse.start()
        brand_name = QLabel("PHOENIX")
        brand_name.setObjectName("brand")
        brand_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_sub = QLabel("IMAGE DOWNLOADER")
        brand_sub.setObjectName("brandSub")
        brand_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(brand_icon)
        side.addWidget(brand_name)
        side.addWidget(brand_sub)
        side.addSpacing(16)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for text, icon_text in [("Главная", "⌂"), ("Мои загрузки", "↓"), ("Коллекции", "▱"), ("Настройки", "⚙")]:
            b = QPushButton(f"{icon_text}     {text}")
            b.setObjectName("navButton")
            b.setCheckable(True)
            self.nav_group.addButton(b)
            side.addWidget(b)
            if text == "Главная":
                b.setChecked(True)
        side.addStretch()

        status_row = QHBoxLayout()
        dot = QLabel("●")
        dot.setObjectName("statusDot")
        status_text = QLabel("Готов к работе")
        status_text.setObjectName("statusText")
        status_row.addWidget(dot)
        status_row.addWidget(status_text)
        status_row.addStretch()
        side.addLayout(status_row)
        version = QLabel("Версия 0.2.0")
        version.setObjectName("version")
        side.addWidget(version)
        outer.addWidget(sidebar)

        # Main content
        main = QWidget()
        main.setObjectName("mainPanel")
        content = QVBoxLayout(main)
        content.setContentsMargins(24, 18, 24, 16)
        content.setSpacing(11)
        outer.addWidget(main, 1)

        top = QHBoxLayout()
        titles = QVBoxLayout()
        heading = QLabel("Загрузчик фотографий")
        heading.setObjectName("heading")
        subheading = QLabel("Собирай коллекции из доступных изображений")
        subheading.setObjectName("muted")
        titles.addWidget(heading)
        titles.addWidget(subheading)
        top.addLayout(titles)
        top.addStretch()
        self.window_badge = QLabel(f"PHOENIX  /  {APP_VERSION}")
        self.window_badge.setObjectName("badge")
        top.addWidget(self.window_badge, 0, Qt.AlignmentFlag.AlignTop)
        self.update_button = QPushButton("↻  Проверить обновления")
        self.update_button.setObjectName("secondaryButton")
        self.update_button.clicked.connect(self.check_updates)
        top.addWidget(self.update_button, 0, Qt.AlignmentFlag.AlignTop)
        content.addLayout(top)

        search_card = QFrame()
        search_card.setObjectName("searchCard")
        search_row = QHBoxLayout(search_card)
        search_row.setContentsMargins(14, 12, 14, 12)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("Вставь ссылку на страницу…")
        self.url_input.setClearButtonEnabled(True)
        self.scan_button = QPushButton("⌕   Найти фото")
        self.scan_button.setObjectName("primaryButton")
        self.scan_button.clicked.connect(self.scan)
        self.url_input.returnPressed.connect(self.scan)
        search_row.addWidget(QLabel("↗"))
        search_row.addWidget(self.url_input, 1)
        search_row.addWidget(self.scan_button)
        content.addWidget(search_card)

        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        tools = QHBoxLayout(toolbar)
        tools.setContentsMargins(12, 8, 12, 8)
        self.found_label = QLabel("◈   Найдено изображений: 0")
        self.found_label.setObjectName("toolbarText")
        tools.addWidget(self.found_label)
        tools.addSpacing(10)
        self.select_all = QCheckBox("Выбрать все")
        self.select_all.stateChanged.connect(self.toggle_all)
        tools.addWidget(self.select_all)
        tools.addStretch()
        self.filter_buttons = {}
        for key, label in [("ALL", "Все фото"), ("JPG", "JPG"), ("PNG", "PNG"), ("WEBP", "WEBP")]:
            b = QPushButton(label)
            b.setObjectName("filterButton")
            b.setCheckable(True)
            b.setChecked(key == "ALL")
            b.clicked.connect(lambda checked=False, k=key: self.set_filter(k))
            tools.addWidget(b)
            self.filter_buttons[key] = b
        tools.addSpacing(12)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["По умолчанию", "По имени"])
        self.sort_combo.setObjectName("sortCombo")
        self.sort_combo.currentIndexChanged.connect(self.sort_items)
        tools.addWidget(self.sort_combo)
        content.addWidget(toolbar)

        self.gallery = QListWidget()
        self.gallery.setObjectName("gallery")
        self.gallery.setViewMode(QListWidget.ViewMode.IconMode)
        self.gallery.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.gallery.setMovement(QListWidget.Movement.Static)
        self.gallery.setWrapping(True)
        self.gallery.setSpacing(12)
        self.gallery.setIconSize(QSize(190, 126))
        self.gallery.setGridSize(QSize(208, 174))
        self.gallery.setWordWrap(True)
        self.gallery.setUniformItemSizes(True)
        self.gallery.itemChanged.connect(self.update_selected_count)
        content.addWidget(self.gallery, 1)
        self.gallery_opacity = QGraphicsOpacityEffect(self.gallery)
        self.gallery.setGraphicsEffect(self.gallery_opacity)
        self.gallery_opacity.setOpacity(1.0)

        self.empty_state = QFrame()
        self.empty_state.setObjectName("emptyState")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(10)
        empty_icon = QLabel("✧")
        empty_icon.setObjectName("emptyIcon")
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_title = QLabel("Твоя следующая коллекция начинается здесь")
        empty_title.setObjectName("emptyTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_desc = QLabel("Вставь ссылку выше — PHOENIX найдёт доступные изображения и покажет их здесь.")
        empty_desc.setObjectName("emptyDescription")
        empty_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_desc.setWordWrap(True)
        empty_layout.addStretch()
        empty_layout.addWidget(empty_icon)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_desc)
        empty_layout.addStretch()
        content.insertWidget(content.indexOf(self.gallery), self.empty_state, 1)
        self.empty_state.show()
        self.gallery.hide()

        # Bottom download dock
        dock = QFrame()
        dock.setObjectName("downloadDock")
        dock_layout = QHBoxLayout(dock)
        dock_layout.setContentsMargins(14, 12, 14, 12)
        folder_icon = QLabel("▱")
        folder_icon.setObjectName("folderIcon")
        dock_layout.addWidget(folder_icon)
        folder_col = QVBoxLayout()
        folder_label = QLabel("ПАПКА СОХРАНЕНИЯ")
        folder_label.setObjectName("smallCaps")
        self.folder_path = QLabel(self.download_folder)
        self.folder_path.setObjectName("folderPath")
        self.folder_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        folder_col.addWidget(folder_label)
        folder_col.addWidget(self.folder_path)
        dock_layout.addLayout(folder_col, 2)
        self.folder_button = QPushButton("Выбрать…")
        self.folder_button.setObjectName("secondaryButton")
        self.folder_button.clicked.connect(self.choose_folder)
        dock_layout.addWidget(self.folder_button)
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setObjectName("separator")
        dock_layout.addWidget(separator)
        progress_col = QVBoxLayout()
        self.progress_label = QLabel("Ожидание загрузки")
        self.progress_label.setObjectName("smallCaps")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        progress_col.addWidget(self.progress_label)
        progress_col.addWidget(self.progress_bar)
        dock_layout.addLayout(progress_col, 2)
        self.download_button = QPushButton("↓   Скачать выбранные (0)")
        self.download_button.setObjectName("primaryButton")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_selected)
        dock_layout.addWidget(self.download_button)
        content.addWidget(dock)

        self.setStyleSheet("""
            QMainWindow, QWidget { background: #0d1018; color: #eef0f8; font-family: "Segoe UI"; font-size: 13px; }
            QFrame#sidebar { background: #090c13; border-right: 1px solid #242a3b; }
            QWidget#mainPanel { background: #0d1018; }
            QLabel#phoenixMark { background: transparent; padding: 0; }
            QLabel#brand { color: #f4f2ff; font-size: 27px; font-weight: 800; letter-spacing: 4px; padding-top: 0; }
            QLabel#brandSub { color: #918bbd; font-size: 9px; letter-spacing: 2px; }
            QPushButton#navButton { text-align: left; color: #aeb5cb; background: transparent; border: 1px solid transparent; border-radius: 10px; padding: 13px 12px; font-size: 14px; }
            QPushButton#navButton:hover { background: #171a2a; color: white; }
            QPushButton#navButton:checked { color: white; background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #292047, stop:1 #171a2d); border: 1px solid #5843a8; }
            QLabel#statusDot { color: #42e5a2; font-size: 13px; }
            QLabel#statusText { color: #c9d0df; font-size: 12px; }
            QLabel#version { color: #687187; font-size: 11px; padding-left: 19px; }
            QLabel#heading { font-size: 29px; font-weight: 750; color: #f7f7fb; }
            QLabel#muted { color: #8893ad; font-size: 14px; }
            QLabel#badge { color: #a99aff; background: #19162b; border: 1px solid #3e326c; border-radius: 9px; padding: 8px 10px; font-size: 11px; }
            QFrame#searchCard, QFrame#toolbar, QFrame#downloadDock { background: #141925; border: 1px solid #293149; border-radius: 13px; }
            QLineEdit { background: #1a2030; color: #f0f2fb; border: 1px solid #333d57; border-radius: 9px; padding: 13px; font-size: 14px; selection-background-color: #6f55ff; }
            QLineEdit:focus { border: 1px solid #8a70ff; }
            QPushButton#primaryButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #6256ff, stop:1 #8c3df5); color: white; border: 0; border-radius: 10px; padding: 12px 17px; font-weight: 700; }
            QPushButton#primaryButton:hover { background: #987cff; border: 1px solid #b8a4ff; }
            QPushButton#primaryButton:disabled { background: #343449; color: #85869b; }
            QPushButton#secondaryButton { background: #20283a; color: #d9def0; border: 1px solid #39435d; border-radius: 8px; padding: 9px 12px; }
            QPushButton#secondaryButton:hover { border-color: #8770ff; }
            QLabel#toolbarText { color: #d8def0; font-weight: 600; }
            QCheckBox { color: #d5d9e8; spacing: 7px; }
            QCheckBox::indicator { width: 17px; height: 17px; }
            QCheckBox::indicator:unchecked { background: #111622; border: 1px solid #566078; border-radius: 4px; }
            QCheckBox::indicator:checked { background: #765cff; border: 1px solid #a18cff; border-radius: 4px; }
            QPushButton#filterButton { color: #aeb8d1; background: #111622; border: 1px solid #303a51; border-radius: 16px; padding: 7px 14px; }
            QPushButton#filterButton:checked { color: white; background: #292047; border-color: #8870ff; }
            QComboBox { background: #171d2b; color: #cbd3e8; border: 1px solid #303a51; border-radius: 8px; padding: 7px 10px; min-width: 120px; }
            QFrame#emptyState { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #111522, stop:0.55 #10131e, stop:1 #171128); border: 1px solid #252d43; border-radius: 14px; }
            QLabel#emptyIcon { color: #9a7cff; font-size: 58px; }
            QLabel#emptyTitle { color: #e9e8f8; font-size: 19px; font-weight: 700; }
            QLabel#emptyDescription { color: #8792ad; font-size: 13px; padding: 0 40px; }
            QListWidget#gallery { background: transparent; border: none; outline: none; padding: 2px; }
            QListWidget#gallery::item { background: #151a26; border: 1px solid #2b3449; border-radius: 10px; padding: 6px; color: #dce2f2; }
            QListWidget#gallery::item:hover { background: #211f35; border: 1px solid #8a70ff; }
            QListWidget#gallery::item:selected { background: #211c3c; border: 2px solid #9170ff; }
            QLabel#folderIcon { color: #9e8aff; font-size: 29px; padding: 4px 9px; }
            QLabel#smallCaps { color: #8c96b0; font-size: 10px; letter-spacing: 1px; }
            QLabel#folderPath { color: #e0e5f4; font-size: 12px; }
            QFrame#separator { color: #333b50; }
            QProgressBar { background: #20283a; border: 1px solid #343f58; border-radius: 5px; min-height: 10px; max-height: 10px; }
            QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #665cff, stop:1 #b34cff); border-radius: 4px; }
        """)

    def keep_thread(self, thread, worker):
        self.threads.append(thread)
        self.workers.append(worker)
        thread.finished.connect(lambda: self.cleanup_thread(thread, worker))

    def cleanup_thread(self, thread, worker):
        if thread in self.threads:
            self.threads.remove(thread)
        if worker in self.workers:
            self.workers.remove(worker)
        worker.deleteLater()
        thread.deleteLater()

    def check_updates(self):
        # Prefer a user-editable config beside the EXE; fall back to the bundled resource.
        external_config = os.path.join(
            os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__)),
            "update_config.json",
        )
        config_path = external_config if os.path.isfile(external_config) else resource_path("update_config.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}

        owner = str(config.get("github_owner", "")).strip()
        repo = str(config.get("github_repo", "")).strip()
        if not owner or not repo or owner == "YOUR_GITHUB_USERNAME" or repo == "YOUR_REPO_NAME":
            QMessageBox.information(
                self,
                "PHOENIX — обновления",
                "Проверка обновлений подготовлена, но пока не подключён сервер релизов.\\n\\n"
                "Когда создадим публичный GitHub-репозиторий, укажем его имя в update_config.json. "
                "После этого PHOENIX сможет проверять новые версии."
            )
            return

        self.update_button.setEnabled(False)
        self.update_button.setText("Проверяем…")
        thread = QThread()
        worker = UpdateCheckWorker(owner, repo, APP_VERSION)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.result.connect(self.on_update_result)
        worker.error.connect(self.on_update_error)
        worker.result.connect(thread.quit)
        worker.error.connect(thread.quit)
        self.keep_thread(thread, worker)
        thread.finished.connect(self.update_check_done)
        thread.start()

    def update_check_done(self):
        self.update_button.setEnabled(True)
        self.update_button.setText("↻  Проверить обновления")

    def on_update_result(self, result):
        latest = result["latest"]
        current = result["current"]
        if version_tuple(latest) <= version_tuple(current):
            QMessageBox.information(
                self, "PHOENIX — обновления",
                f"У тебя актуальная версия PHOENIX {current}."
            )
            return

        notes = result.get("notes", "").strip() or "В описании релиза нет заметок."
        assets = result.get("assets", [])
        update_asset = next(
            (a for a in assets if a.get("name", "").lower() == "phoenix-win-x64.zip"),
            None
        )
        if update_asset and update_asset.get("url"):
            answer = QMessageBox.question(
                self,
                "Доступно обновление PHOENIX",
                f"Найдена новая версия: {latest}\\nТекущая версия: {current}\\n\\n"
                f"Что нового:\\n{notes[:800]}\\n\\n"
                "Установить обновление сейчас? PHOENIX закроется и запустится снова после обновления.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.download_and_install_update(update_asset["url"], latest)
            return

        QMessageBox.information(
            self,
            "Обновление найдено",
            f"Доступна версия {latest}, но в релизе не найден файл "
            "PHOENIX-win-x64.zip.\\n\\n"
            "Добавь этот ZIP в Assets релиза GitHub. Архив должен содержать файлы "
            "сборки напрямую: PHOENIX.exe и папку _internal (или другие файлы PyInstaller)."
        )

    def download_and_install_update(self, asset_url, latest_version):
        if not getattr(sys, "frozen", False):
            QMessageBox.information(
                self, "PHOENIX — обновление",
                "Автоустановка доступна в собранной версии PHOENIX.exe, а не при запуске main.py."
            )
            return

        self.update_button.setEnabled(False)
        self.update_button.setText("Скачиваем обновление…")
        QApplication.processEvents()
        try:
            response = requests.get(
                asset_url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
                timeout=(15, 120),
                stream=True,
            )
            response.raise_for_status()
            temp_dir = tempfile.mkdtemp(prefix="phoenix_update_")
            archive_path = os.path.join(temp_dir, "PHOENIX-win-x64.zip")
            total = 0
            with open(archive_path, "wb") as out:
                for chunk in response.iter_content(1024 * 256):
                    if chunk:
                        total += len(chunk)
                        if total > 800 * 1024 * 1024:
                            raise ValueError("Архив обновления слишком большой (лимит 800 МБ).")
                        out.write(chunk)

            install_dir = os.path.dirname(os.path.abspath(sys.executable))
            exe_path = os.path.abspath(sys.executable)
            ps1_path = os.path.join(temp_dir, "phoenix_update.ps1")
            ps1 = """param(
  [int]$TargetPid,
  [string]$InstallDir,
  [string]$ArchivePath,
  [string]$ExePath
)
$ErrorActionPreference = 'Stop'
try {
  while (Get-Process -Id $TargetPid -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 500 }
  Expand-Archive -LiteralPath $ArchivePath -DestinationPath $InstallDir -Force
  Start-Process -FilePath $ExePath
  Remove-Item -LiteralPath $ArchivePath -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
} catch {
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show(
    \"Не удалось установить обновление PHOENIX:`n$($_.Exception.Message)`n`nАрхив сохранён здесь:`n$ArchivePath\",
    'PHOENIX — ошибка обновления'
  ) | Out-Null
}
"""
            with open(ps1_path, "w", encoding="utf-8-sig") as f:
                f.write(ps1)
            subprocess.Popen(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", ps1_path,
                    str(os.getpid()), install_dir, archive_path, exe_path
                ],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
            QMessageBox.information(
                self, "PHOENIX — обновление",
                f"Обновление {latest_version} скачано. PHOENIX сейчас закроется, "
                "заменит файлы и запустится снова."
            )
            QApplication.quit()
        except Exception as exc:
            QMessageBox.warning(
                self, "PHOENIX — ошибка обновления",
                "Не удалось скачать обновление. Текущая версия не изменена.\\n\\n" + str(exc)
            )
            self.update_button.setEnabled(True)
            self.update_button.setText("↻  Проверить обновления")

    def on_update_error(self, message):
        QMessageBox.warning(
            self, "PHOENIX — проверка обновлений",
            "Не удалось проверить обновления. Проверь подключение к интернету и настройки репозитория.\\n\\n"
            + message
        )

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выбери папку для загрузок", self.download_folder)
        if folder:
            self.download_folder = folder
            self.folder_path.setText(folder)

    def scan(self):
        try:
            url = normalize_url(self.url_input.text())
            if not url:
                QMessageBox.information(self, "Нужна ссылка", "Вставь ссылку на страницу.")
                return
            self.scan_button.setEnabled(False)
            self.scan_button.setText("Ищем…")
            self.progress_label.setText("Анализ страницы…")
            self.progress_bar.setRange(0, 0)
            self.gallery.clear()
            self.items_by_url.clear()
            self.urls = []
            self.gallery.hide()
            self.empty_state.show()
            self.found_label.setText("◈   Ищем изображения…")
            thread = QThread()
            worker = ScanWorker(url)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.found.connect(self.on_found)
            worker.error.connect(self.on_error)
            worker.found.connect(thread.quit)
            worker.error.connect(thread.quit)
            thread.finished.connect(self.scan_done)
            self.keep_thread(thread, worker)
            thread.start()
        except Exception as exc:
            self.on_error(str(exc))
            self.scan_done()

    def scan_done(self):
        self.scan_button.setEnabled(True)
        self.scan_button.setText("⌕   Найти фото")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)

    def on_found(self, urls, final_url):
        self.urls = urls
        self.found_label.setText(f"◈   Найдено изображений: {len(urls)}")
        self.empty_state.setVisible(not bool(urls))
        self.gallery.setVisible(bool(urls))
        for url in urls:
            item = QListWidgetItem("Загрузка превью…\n" + self.file_label(url))
            item.setData(Qt.ItemDataRole.UserRole, url)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setSizeHint(QSize(198, 166))
            self.gallery.addItem(item)
            self.items_by_url[url] = item
        self.select_all.blockSignals(True)
        self.select_all.setChecked(bool(urls))
        self.select_all.blockSignals(False)
        self.update_selected_count()
        self.progress_label.setText(f"Найдено {len(urls)} изображений")
        if urls:
            self.gallery_opacity.setOpacity(0.15)
            self.animate_gallery_in()
        if urls:
            thread = QThread()
            worker = ThumbnailWorker(urls[:100])
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.thumbnail.connect(self.on_thumbnail)
            thread.finished.connect(thread.quit)
            self.keep_thread(thread, worker)
            thread.start()
        if not urls:
            QMessageBox.information(self, "Ничего не найдено", "В доступном HTML не обнаружены изображения. Сайт может загружать их через JavaScript или требовать входа.")

    @staticmethod
    def file_label(url):
        name = os.path.basename(unquote(urlparse(url).path))
        return name[:26] if name else urlparse(url).netloc[:26]

    def on_thumbnail(self, url, data):
        item = self.items_by_url.get(url)
        if not item or not data:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            thumb = pixmap.scaled(190, 118, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            item.setIcon(QIcon(thumb))
            item.setText(self.file_label(url))
            item.setSizeHint(QSize(198, 166))

    def animate_gallery_in(self):
        self.gallery_fade = QPropertyAnimation(self.gallery_opacity, b"opacity", self)
        self.gallery_fade.setDuration(420)
        self.gallery_fade.setStartValue(0.15)
        self.gallery_fade.setEndValue(1.0)
        self.gallery_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.gallery_fade.start()

    def set_filter(self, key):
        self.active_filter = key
        for k, button in self.filter_buttons.items():
            button.setChecked(k == key)
        for i in range(self.gallery.count()):
            item = self.gallery.item(i)
            url = item.data(Qt.ItemDataRole.UserRole) or ""
            ext = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")
            visible = key == "ALL" or ext == key.lower() or (key == "JPG" and ext == "jpeg")
            item.setHidden(not visible)

    def sort_items(self):
        if self.sort_combo.currentIndex() == 0:
            return
        self.gallery.sortItems()

    def toggle_all(self, state):
        checked = state == Qt.CheckState.Checked
        self.gallery.blockSignals(True)
        for i in range(self.gallery.count()):
            item = self.gallery.item(i)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.gallery.blockSignals(False)
        self.update_selected_count()

    def update_selected_count(self, *_):
        count = sum(1 for i in range(self.gallery.count())
                    if not self.gallery.item(i).isHidden()
                    and self.gallery.item(i).checkState() == Qt.CheckState.Checked)
        total = sum(1 for i in range(self.gallery.count()) if not self.gallery.item(i).isHidden())
        self.download_button.setText(f"↓   Скачать выбранные ({count})")
        self.download_button.setEnabled(count > 0)
        self.select_all.blockSignals(True)
        self.select_all.setChecked(total > 0 and count == total)
        self.select_all.blockSignals(False)

    def download_selected(self):
        urls = [self.gallery.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.gallery.count())
                if not self.gallery.item(i).isHidden()
                and self.gallery.item(i).checkState() == Qt.CheckState.Checked]
        if not urls:
            return
        self.download_button.setEnabled(False)
        self.progress_bar.setRange(0, len(urls))
        self.progress_bar.setValue(0)
        self.progress_label.setText("Скачивание фотографий…")
        thread = QThread()
        worker = DownloadWorker(urls, self.download_folder)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self.on_download_progress)
        worker.finished.connect(self.on_download_finished)
        worker.error.connect(self.on_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        self.keep_thread(thread, worker)
        thread.start()

    def on_download_progress(self, current, total, message):
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(current)
        self.progress_label.setText(f"{current}/{total}  ·  {message[:70]}")

    def on_download_finished(self, ok, failed, folder):
        self.progress_label.setText(f"Готово · скачано {ok}, ошибок {failed}")
        self.download_button.setEnabled(True)
        QMessageBox.information(self, "PHOENIX — загрузка завершена",
                                f"Скачано: {ok}\nОшибок или пропусков: {failed}\n\nПапка:\n{folder}")

    def on_error(self, message):
        QMessageBox.warning(self, "PHOENIX — ошибка", message)
        self.progress_label.setText("Операция завершилась с ошибкой")


class PhoenixSplash(QWidget):
    def __init__(self):
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
        self.setFixedSize(470, 340)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("splashPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(28, 22, 28, 20)
        layout.setSpacing(6)

        self.logo = QLabel()
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        renderer = QSvgRenderer(resource_path("phoenix.svg"))
        pixmap = QPixmap(220, 165)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        self.logo.setPixmap(pixmap)
        layout.addWidget(self.logo)

        self.name = QLabel("PHOENIX")
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setObjectName("splashName")
        layout.addWidget(self.name)

        self.tagline = QLabel("RISE  ·  REBUILD  ·  EVOLVE")
        self.tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tagline.setObjectName("splashTagline")
        layout.addWidget(self.tagline)

        self.bar = QProgressBar()
        self.bar.setObjectName("splashBar")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        layout.addSpacing(14)
        layout.addWidget(self.bar)
        outer.addWidget(panel)

        self.setStyleSheet("""
            QWidget { background: transparent; font-family: "Segoe UI"; }
            QFrame#splashPanel { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #101522, stop:0.55 #11101e, stop:1 #1b1230); border: 1px solid #55418d; border-radius: 22px; }
            QLabel#splashName { color: #f6f2ff; font-size: 34px; font-weight: 850; letter-spacing: 8px; }
            QLabel#splashTagline { color: #a99aff; font-size: 10px; letter-spacing: 3px; }
            QProgressBar#splashBar { background: #25243a; border: 0; border-radius: 3px; min-height: 5px; max-height: 5px; }
            QProgressBar#splashBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #48cfff, stop:0.5 #815cff, stop:1 #db4cff); border-radius: 3px; }
        """)
        self.opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity)
        self.opacity.setOpacity(0.0)

        self.fade_in = QPropertyAnimation(self.opacity, b"opacity", self)
        self.fade_in.setDuration(400)
        self.fade_in.setStartValue(0.0)
        self.fade_in.setEndValue(1.0)
        self.fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.fade_in.start()

        self.progress_value = 0
        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self.advance_progress)
        self.progress_timer.start(22)

        self.fade_out = QPropertyAnimation(self.opacity, b"opacity", self)
        self.fade_out.setDuration(300)
        self.fade_out.setStartValue(1.0)
        self.fade_out.setEndValue(0.0)
        self.fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self.progress_timer.setSingleShot(False)
        QTimer.singleShot(1550, self.begin_close)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.center().x() - self.width() // 2,
                  screen.center().y() - self.height() // 2)

    def advance_progress(self):
        self.progress_value = min(100, self.progress_value + 2)
        self.bar.setValue(self.progress_value)

    def begin_close(self):
        self.progress_timer.stop()
        self.fade_out.finished.connect(self.close)
        self.fade_out.start()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    splash = PhoenixSplash()
    splash.show()
    window = MainWindow()

    def show_main():
        window.show()
        window.raise_()
        window.activateWindow()

    QTimer.singleShot(1800, show_main)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
