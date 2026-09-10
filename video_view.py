"""
A window for whatever camera is plugged into the ground station, or for a
camera on the network.

Flying with a video feed usually means watching it in one program and the
map in another, and alt-tabbing between them at the worst moments. This
puts the picture in the same program as the map without pretending to be
anything more: pick a source, watch it.

Why DirectShow and not Qt's own camera support
----------------------------------------------
Qt on Windows enumerates cameras through Media Foundation, whichever
media backend is selected - both `windows` and `ffmpeg` were tried and
both report nothing for a virtual camera. OBS registers its virtual
camera as a DirectShow filter only, so Media Foundation structurally
cannot see it, and neither can Qt. DirectShow sees everything Media
Foundation sees and the virtual cameras as well, so there is one path
here rather than two.

That costs two libraries and they divide the work by what each is good
at. pygrabber enumerates: it returns real device names in DirectShow's
own order, which is the same order OpenCV opens them by, and that
correspondence is the whole difficulty of using OpenCV for capture.
OpenCV then does the capture, because pygrabber's own frame grabber
insists on RGB24 and fails against anything offering NV12 or YUY2 - OBS
included.

Network cameras
---------------
RTSP goes straight to FFmpeg through OpenCV. No relay, no streaming
engine to install: those exist to solve problems this does not have, and
a ground station that downloads and installs a third-party binary is a
much larger thing than it appears.

Transport is worth choosing rather than leaving to chance. UDP loses
packets and tears the picture; TCP is steadier and is what most cameras
should be asked for over anything but a clean local network. Auto lets
FFmpeg decide, which usually means UDP first.

A wrong address takes thirty seconds to give up, so opening happens on
the worker thread and Stop works throughout - a mistyped URL must not
freeze the window that would let you fix it.

Two more decisions worth knowing:

  * Frames are read on a worker thread. cv2.VideoCapture.read() blocks
    until a frame arrives, and stalling the ground station's interface
    for a frame interval, thirty times a second, would be indefensible.
  * Closing the window releases the device. A capture device held open by
    a window nobody can see is how the next program to want it fails for
    no visible reason.
"""

import collections
import os
import time

from PySide6.QtCore import QPoint, QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent, QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSizeGrip, QSizePolicy,
                               QSpinBox, QVBoxLayout, QWidget)

from app_paths import load_settings, save_setting

AUTO = "Auto"
SOURCE_DEVICE = "Camera (device)"
SOURCE_RTSP = "RTSP (network)"

SETTING_URL = "video_rtsp_url"
SETTING_TRANSPORT = "video_rtsp_transport"
SETTING_BUFFER = "video_smoothing_frames"

# Offered sizes. Anything the device refuses simply comes back at its own
# size, which is reported rather than hidden.
SIZES = [("1920 x 1080", (1920, 1080)),
         ("1280 x 720", (1280, 720)),
         ("640 x 480", (640, 480))]
RATES = [60, 30, 15]
TRANSPORTS = [(AUTO, None), ("TCP", "tcp"), ("UDP", "udp")]


def list_devices():
    """Device names in the order OpenCV will open them by.

    Empty if the libraries or the platform are not there, which the
    caller shows as "no devices" rather than as an error - a machine
    without a capture stack should still get a ground station.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph
        return list(FilterGraph().get_input_devices())
    except Exception:
        return []


class _Reader(QThread):
    """Opens one source and emits frames until asked to stop."""

    frame_ready = Signal(QImage)
    failed = Signal(str)
    opened = Signal(int, int)

    def __init__(self, source, size=None, fps=None, transport=None,
                 smoothing=0, parent=None):
        super().__init__(parent)
        self._source = source           # int index, or an rtsp:// string
        self._size = size
        self._fps = fps
        self._transport = transport
        self._smoothing = max(0, int(smoothing))
        self._stop = False

    def stop(self):
        self._stop = True

    def _open(self, cv2):
        if isinstance(self._source, int):
            return cv2.VideoCapture(self._source, cv2.CAP_DSHOW)
        # FFmpeg reads its options from the environment at capture time.
        # stimeout is in microseconds and stops a silent camera hanging
        # the read for ever; the transport is the part worth choosing.
        opts = ["stimeout;5000000"]
        if self._transport:
            opts.append("rtsp_transport;%s" % self._transport)
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(opts)
        return cv2.VideoCapture(self._source, cv2.CAP_FFMPEG)

    def run(self):
        import cv2
        cap = self._open(cv2)
        if not cap.isOpened():
            cap.release()
            if self._stop:
                return          # cancelled while it was still trying
            self.failed.emit(
                "That address did not answer." if not isinstance(
                    self._source, int) else
                "That device would not open. It is usually already in use "
                "by another program.")
            return
        try:
            if isinstance(self._source, int):
                if self._size is not None:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._size[0])
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._size[1])
                if self._fps is not None:
                    cap.set(cv2.CAP_PROP_FPS, self._fps)
            self.opened.emit(int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                             int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

            # The smoothing buffer: hold this many frames back before
            # showing anything, then show one for every one read. A
            # network stream that arrives in bursts then plays evenly,
            # at the cost of exactly this much added delay - which is
            # why it defaults to none. On a device it is pointless.
            queue = collections.deque()
            misses = 0
            while not self._stop:
                ok, frame = cap.read()
                if not ok or frame is None:
                    # A dropped frame is normal; a run of them is not.
                    misses += 1
                    if misses > 60:
                        self.failed.emit("The stream stopped sending "
                                         "pictures.")
                        return
                    self.msleep(15)
                    continue
                misses = 0
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, _ = rgb.shape
                # copy() because the buffer behind rgb is reused for the
                # next frame; without it the image tears or crashes once
                # it crosses to the GUI thread.
                image = QImage(rgb.data, w, h, 3 * w,
                               QImage.Format.Format_RGB888).copy()
                if self._smoothing:
                    queue.append(image)
                    if len(queue) <= self._smoothing:
                        continue
                    image = queue.popleft()
                self.frame_ready.emit(image)
        finally:
            cap.release()


class FloatingVideo(QWidget):
    """The picture on top of the map, where the map is what you steer by.

    A separate window means a second monitor or a lot of alt-tabbing. This
    is the other arrangement: a panel laid over the map itself, moved and
    sized to sit where it is least in the way, so both are in one glance.

    Frameless and a child of the map rather than a window of its own, so
    it cannot wander behind the ground station or onto another screen and
    be lost. Dragged by its body, sized by the grip in the corner.
    """

    closed = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("floatingVideo")
        # Child, frameless, and painted rather than transparent: video
        # under a translucent panel is unreadable and the map under video
        # is worse.
        self.setStyleSheet(
            "#floatingVideo { background: #0b0b0b;"
            " border: 1px solid rgba(255,255,255,0.14);"
            " border-radius: 6px; }")
        self.resize(400, 260)
        self._drag = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setToolTip("Close the floating picture")
        self.close_btn.setStyleSheet(
            "QPushButton { color: #ddd; background: rgba(255,255,255,0.06);"
            " border: none; border-radius: 11px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.16); }")
        self.close_btn.clicked.connect(self._closed)
        top.addWidget(self.close_btn)
        top.addStretch(1)
        layout.addLayout(top)

        self.view = QLabel("Video off")
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setStyleSheet("color: #8a8a8a; font-size: 12px;")
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
        layout.addWidget(self.view, 1)

        grip = QHBoxLayout()
        grip.setContentsMargins(0, 0, 0, 0)
        grip.addStretch(1)
        grip.addWidget(QSizeGrip(self), 0,
                       Qt.AlignmentFlag.AlignBottom
                       | Qt.AlignmentFlag.AlignRight)
        layout.addLayout(grip)

    def show_frame(self, image):
        self.view.setPixmap(QPixmap.fromImage(image).scaled(
            self.view.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def clear(self):
        """Back to saying nothing is playing, rather than a frozen frame."""
        self.view.clear()          # drops the pixmap; setText alone may not
        self.view.setText("Video off")

    def _closed(self):
        self.hide()
        self.closed.emit()

    # ---- dragging --------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        want = event.globalPosition().toPoint() - self._drag
        # Kept inside the map: a panel dragged off the edge cannot be
        # dragged back, because the part you would grab is the part that
        # left.
        parent = self.parentWidget()
        if parent is not None:
            want = QPoint(
                max(0, min(want.x(), parent.width() - self.width())),
                max(0, min(want.y(), parent.height() - self.height())))
        self.move(want)

    def mouseReleaseEvent(self, event):
        self._drag = None


class VideoWindow(QWidget):
    """Pick a video source and watch it."""

    def __init__(self, parent=None, float_over=None):
        # No parent on purpose: as a top-level window it gets its own
        # entry in the task bar and can be moved to another screen, which
        # is most of the point of having it in a window at all.
        #
        # float_over is a different thing - the widget the floating panel
        # is laid over, which is the map. Kept separate so this window
        # stays independent of it.
        super().__init__(None)
        self._float_over = float_over
        self._floating = None
        self.setWindowTitle("MavGCS Live Video")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(760, 640)

        self._reader = None
        self._frames = 0
        self._since = 0.0
        self._names = []

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.view = QLabel()
        self.view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
        self.view.setMinimumHeight(240)
        self.view.setStyleSheet("background: #000;")
        layout.addWidget(self.view, 1)

        form = QFormLayout()
        form.setSpacing(6)

        self.source_combo = QComboBox()
        self.source_combo.addItems([SOURCE_DEVICE, SOURCE_RTSP])
        self.source_combo.currentTextChanged.connect(self._source_changed)
        form.addRow("Source", self.source_combo)

        self.device_combo = QComboBox()
        self.device_row = form.rowCount()
        form.addRow("Video device", self.device_combo)

        # The URL, the transport it should use, and a button to remember
        # it - a camera's address is typed once and wanted for ever.
        url_row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("rtsp://192.168.1.10:554/cam")
        url_row.addWidget(self.url_edit, 1)
        self.transport_combo = QComboBox()
        for label, value in TRANSPORTS:
            self.transport_combo.addItem(label, value)
        self.transport_combo.setToolTip(
            "How the video is carried. UDP is lower latency but tears the "
            "picture when packets go missing; TCP is steadier and is the "
            "better choice over anything but a clean local network.")
        url_row.addWidget(self.transport_combo)
        self.save_btn = QPushButton("Save")
        self.save_btn.setToolTip("Remember this address and transport.")
        self.save_btn.clicked.connect(self._save_url)
        url_row.addWidget(self.save_btn)
        self.url_widget = QWidget()
        self.url_widget.setLayout(url_row)
        url_row.setContentsMargins(0, 0, 0, 0)
        form.addRow("RTSP URL", self.url_widget)

        self.res_combo = QComboBox()
        form.addRow("Resolution", self.res_combo)
        self.fps_combo = QComboBox()
        form.addRow("Frame rate", self.fps_combo)

        self.buffer_spin = QSpinBox()
        self.buffer_spin.setRange(0, 30)
        self.buffer_spin.setToolTip(
            "Hold this many frames back before showing anything, so a "
            "stream that arrives in bursts plays evenly. Costs exactly "
            "this much delay, which is why it starts at none.")
        form.addRow("Smoothing buffer (frames)", self.buffer_spin)
        layout.addLayout(form)

        self.res_combo.addItem(AUTO, None)
        for label, size in SIZES:
            self.res_combo.addItem(label, size)
        self.fps_combo.addItem(AUTO, None)
        for r in RATES:
            self.fps_combo.addItem("%d fps" % r, r)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh list")
        self.refresh_btn.setToolTip(
            "Look for devices again. Needed after plugging in a capture "
            "card, or after starting a virtual camera such as OBS.")
        self.refresh_btn.clicked.connect(self.reload_devices)
        buttons.addWidget(self.refresh_btn)
        self.float_btn = QPushButton("Floating window")
        self.float_btn.setToolTip(
            "Lay the picture over the map, so the map and the view are in "
            "one glance. Drag it by its body, size it by the corner.")
        self.float_btn.clicked.connect(self._toggle_floating)
        self.float_btn.setEnabled(float_over is not None)
        buttons.addWidget(self.float_btn)
        buttons.addStretch(1)
        self.start_btn = QPushButton("Start")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._toggle)
        buttons.addWidget(self.start_btn)
        layout.addLayout(buttons)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size: 11px; color: #aaa;")
        layout.addWidget(self.status)

        self._restore()
        self.reload_devices()
        self._source_changed(self.source_combo.currentText())

    # ---- settings --------------------------------------------------------

    def _restore(self):
        s = load_settings()
        self.url_edit.setText(s.get(SETTING_URL, "") or "")
        want = s.get(SETTING_TRANSPORT)
        idx = self.transport_combo.findData(want)
        if idx >= 0:
            self.transport_combo.setCurrentIndex(idx)
        self.buffer_spin.setValue(int(s.get(SETTING_BUFFER, 0) or 0))

    def _save_url(self):
        save_setting(SETTING_URL, self.url_edit.text().strip())
        save_setting(SETTING_TRANSPORT, self.transport_combo.currentData())
        save_setting(SETTING_BUFFER, self.buffer_spin.value())
        self._say("Address remembered.")

    # ---- source ----------------------------------------------------------

    def _is_rtsp(self):
        return self.source_combo.currentText() == SOURCE_RTSP

    def _source_changed(self, _text=None):
        """Show only the controls that apply to the chosen source."""
        rtsp = self._is_rtsp()
        form = self.layout().itemAt(1).layout()
        for widget, hidden in ((self.device_combo, rtsp),
                               (self.url_widget, not rtsp),
                               (self.res_combo, rtsp),
                               (self.fps_combo, rtsp)):
            widget.setVisible(not hidden)
            label = form.labelForField(widget)
            if label is not None:
                label.setVisible(not hidden)
        # Resolution and frame rate belong to the device; a network camera
        # sends what it sends, and asking OpenCV to change it does nothing.
        self.refresh_btn.setVisible(not rtsp)
        self.start_btn.setEnabled(
            bool(self.url_edit.text().strip()) if rtsp
            else self.device_combo.isEnabled())

    # ---- devices ---------------------------------------------------------

    def reload_devices(self):
        """Rebuild the list, keeping the current pick if it is still there."""
        was = self.device_combo.currentText()
        self._names = list_devices()
        self.device_combo.clear()

        if not self._names:
            if not self._is_rtsp():
                self._stop()
            self.device_combo.addItem("No video devices found")
            self.device_combo.setEnabled(False)
            if not self._is_rtsp():
                self.start_btn.setEnabled(False)
                self._say("Nothing to show. Plug in a camera or capture "
                          "card, or start a virtual camera, then press "
                          "Refresh list.")
            return

        for i, name in enumerate(self._names):
            self.device_combo.addItem(name, i)
        self.device_combo.setEnabled(True)
        if not self._is_rtsp():
            self.start_btn.setEnabled(True)
        if was in self._names:
            self.device_combo.setCurrentIndex(self._names.index(was))
        elif self._reader is not None and not self._is_rtsp():
            self._stop()
            self._say("The device that was playing has gone.")

    # ---- playing ---------------------------------------------------------

    def _toggle(self):
        if self._reader is not None:
            self._stop()
            self._say("Stopped.")
        else:
            self.start()

    def start(self):
        """Open the selected source and show it."""
        self._stop()
        if self._is_rtsp():
            url = self.url_edit.text().strip()
            if not url:
                return
            reader = _Reader(url, transport=self.transport_combo.currentData(),
                             smoothing=self.buffer_spin.value(), parent=self)
            what = url
        else:
            index = self.device_combo.currentData()
            if index is None:
                return
            reader = _Reader(index, size=self.res_combo.currentData(),
                             fps=self.fps_combo.currentData(),
                             smoothing=self.buffer_spin.value(), parent=self)
            what = self.device_combo.currentText()
        reader.frame_ready.connect(self._show_frame)
        reader.failed.connect(self._failed)
        reader.opened.connect(self._opened)
        self._reader = reader
        self._frames = 0
        self._since = time.monotonic()
        reader.start()
        self.start_btn.setText("Stop")
        self._say("Opening %s ..." % what)

    def _opened(self, w, h):
        what = (self.url_edit.text().strip() if self._is_rtsp()
                else self.device_combo.currentText())
        asked = self.res_combo.currentText()
        got = "%d x %d" % (w, h)
        # Say both only when they differ: a device that quietly ignored
        # the request should not look as though it honoured it.
        if self._is_rtsp() or asked in (AUTO, got):
            where = got
        else:
            where = "%s (asked for %s)" % (got, asked)
        self._say("%s at %s" % (what, where))

    def _toggle_floating(self):
        """Put the picture over the map, or take it away again."""
        if self._float_over is None:
            return
        if self._floating is not None and self._floating.isVisible():
            self._floating.hide()
            self.float_btn.setText("Floating window")
            return
        if self._floating is None:
            self._floating = FloatingVideo(self._float_over)
            self._floating.closed.connect(
                lambda: self.float_btn.setText("Floating window"))
            # Bottom left of the map, clear of the instrument column on
            # the right and the panels along the top.
            parent = self._float_over
            self._floating.move(16, max(0, parent.height()
                                        - self._floating.height() - 40))
        self._floating.show()
        self._floating.raise_()
        self.float_btn.setText("Hide floating")

    def _show_frame(self, image):
        self._frames += 1
        if self._floating is not None and self._floating.isVisible():
            self._floating.show_frame(image)
        self.view.setPixmap(QPixmap.fromImage(image).scaled(
            self.view.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        now = time.monotonic()
        if now - self._since >= 2.0:
            rate = self._frames / (now - self._since)
            text = self.status.text().split("  -  ")[0]
            self._say("%s  -  %.0f fps" % (text, rate))
            self._frames = 0
            self._since = now

    def _failed(self, message):
        self._stop()
        self._say(message)

    def _stop(self):
        reader, self._reader = self._reader, None
        if reader is not None:
            # Disconnect before stopping. Frames already queued for the
            # GUI thread are still delivered after stop() returns, and one
            # arriving late would repaint a picture we have just cleared -
            # leaving a frozen frame that looks live.
            for signal in (reader.frame_ready, reader.failed, reader.opened):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            reader.stop()
            # An RTSP open that is still timing out can take a while to
            # notice. Give it a moment, then let it finish on its own
            # rather than blocking the interface waiting for it.
            if not reader.wait(1500):
                reader.finished.connect(reader.deleteLater)
            else:
                reader.deleteLater()
        if self._floating is not None:
            self._floating.clear()
        self.start_btn.setText("Start")

    def _say(self, text):
        self.status.setText(text)

    # ---- window ----------------------------------------------------------

    def closeEvent(self, event: QCloseEvent):
        """Release the device rather than holding it open unseen."""
        self._stop()
        if self._floating is not None:
            # It is a child of the map, so it would otherwise stay there
            # over a map with nothing feeding it.
            self._floating.hide()
            self._floating.deleteLater()
            self._floating = None
        super().closeEvent(event)
