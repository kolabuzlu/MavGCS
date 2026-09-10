"""
A window for whatever camera is plugged into the ground station.

Flying with a video feed usually means watching it in one program and the
map in another, and alt-tabbing between them at the worst moments. This
puts the picture in the same program as the map without pretending to be
anything more: pick a device, pick a size, watch it.

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

Sizes are offered as a fixed list rather than read from the device:
pygrabber's format table does not recognise every fourcc (I420 raises a
KeyError), and a list that sometimes throws is worse than one that is
always the same. What the device actually gave is reported underneath,
so a request that was ignored is visible rather than assumed.

Two more decisions worth knowing:

  * Frames are read on a worker thread. cv2.VideoCapture.read() blocks
    until a frame arrives, and stalling the ground station's interface
    for a frame interval, thirty times a second, would be indefensible.
  * Closing the window releases the device. A capture device held open by
    a window nobody can see is how the next program to want it fails for
    no visible reason.
"""

import time

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent, QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

AUTO = "Auto"

# Offered sizes. Anything the device refuses simply comes back at its own
# size, which is reported rather than hidden.
SIZES = [("1920 x 1080", (1920, 1080)),
         ("1280 x 720", (1280, 720)),
         ("640 x 480", (640, 480))]
RATES = [60, 30, 15]


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
    """Opens one device and emits frames until asked to stop."""

    frame_ready = Signal(QImage)
    failed = Signal(str)
    opened = Signal(int, int)

    def __init__(self, index, size, fps, parent=None):
        super().__init__(parent)
        self._index = index
        self._size = size
        self._fps = fps
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        import cv2
        cap = cv2.VideoCapture(self._index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self.failed.emit("That device would not open. It is usually "
                             "already in use by another program.")
            return
        try:
            if self._size is not None:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._size[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._size[1])
            if self._fps is not None:
                cap.set(cv2.CAP_PROP_FPS, self._fps)
            self.opened.emit(int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                             int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))

            misses = 0
            while not self._stop:
                ok, frame = cap.read()
                if not ok or frame is None:
                    # A dropped frame is normal; a run of them is not.
                    misses += 1
                    if misses > 60:
                        self.failed.emit("The device stopped sending "
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
                self.frame_ready.emit(
                    QImage(rgb.data, w, h, 3 * w,
                           QImage.Format.Format_RGB888).copy())
        finally:
            cap.release()


class VideoWindow(QWidget):
    """Pick a video input and watch it."""

    def __init__(self, parent=None):
        # No parent on purpose: as a top-level window it gets its own
        # entry in the task bar and can be moved to another screen, which
        # is most of the point of having it in a window at all.
        super().__init__(None)
        self.setWindowTitle("Video")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(760, 600)

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
        self.view.setMinimumHeight(260)
        self.view.setStyleSheet("background: #000;")
        layout.addWidget(self.view, 1)

        form = QFormLayout()
        form.setSpacing(6)
        self.device_combo = QComboBox()
        form.addRow("Video device", self.device_combo)

        self.res_combo = QComboBox()
        form.addRow("Resolution", self.res_combo)

        self.fps_combo = QComboBox()
        form.addRow("Frame rate", self.fps_combo)
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

        self.reload_devices()

    # ---- devices ---------------------------------------------------------

    def reload_devices(self):
        """Rebuild the list, keeping the current pick if it is still there."""
        was = self.device_combo.currentText()
        self._names = list_devices()
        self.device_combo.clear()

        if not self._names:
            self._stop()
            self.device_combo.addItem("No video devices found")
            self.device_combo.setEnabled(False)
            self.start_btn.setEnabled(False)
            self._say("Nothing to show. Plug in a camera or capture card, "
                      "or start a virtual camera, then press Refresh list.")
            return

        for i, name in enumerate(self._names):
            self.device_combo.addItem(name, i)
        self.device_combo.setEnabled(True)
        self.start_btn.setEnabled(True)
        if was in self._names:
            self.device_combo.setCurrentIndex(self._names.index(was))
        elif self._reader is not None:
            self._stop()
            self._say("The device that was playing has gone.")
        else:
            self._say("")

    # ---- playing ---------------------------------------------------------

    def _toggle(self):
        if self._reader is not None:
            self._stop()
            self._say("Stopped.")
        else:
            self.start()

    def start(self):
        """Open the selected device and show it."""
        self._stop()
        index = self.device_combo.currentData()
        if index is None:
            return
        reader = _Reader(index, self.res_combo.currentData(),
                         self.fps_combo.currentData(), self)
        reader.frame_ready.connect(self._show_frame)
        reader.failed.connect(self._failed)
        reader.opened.connect(self._opened)
        self._reader = reader
        self._frames = 0
        self._since = time.monotonic()
        reader.start()
        self.start_btn.setText("Stop")
        self._say("Opening %s ..." % self.device_combo.currentText())

    def _opened(self, w, h):
        asked = self.res_combo.currentText()
        got = "%d x %d" % (w, h)
        # Say both only when they differ: a device that quietly ignored
        # the request should not look as though it honoured it.
        where = got if asked in (AUTO, got) else "%s (asked for %s)" % (got,
                                                                        asked)
        self._say("%s at %s" % (self.device_combo.currentText(), where))

    def _show_frame(self, image):
        self._frames += 1
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
            reader.stop()
            # The read in flight has to finish before the device is
            # released, or the next open finds it still busy.
            reader.wait(3000)
            reader.deleteLater()
        self.start_btn.setText("Start")

    def _say(self, text):
        self.status.setText(text)

    # ---- window ----------------------------------------------------------

    def closeEvent(self, event: QCloseEvent):
        """Release the device rather than holding it open unseen."""
        self._stop()
        super().closeEvent(event)
