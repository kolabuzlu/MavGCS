"""
A window for whatever camera is plugged into the ground station.

Flying with a video feed usually means watching it in one program and the
map in another, and alt-tabbing between them at the worst moments. This
puts the picture in the same program as the map without pretending to be
anything more: pick a device, pick a size, watch it.

Windows exposes capture cards as ordinary cameras, so an HDMI grabber
with an analogue FPV receiver behind it turns up in the same list as a
webcam and needs no special handling here.

Two decisions worth knowing:

  * Not modal, and its own window rather than a panel. A feed you cannot
    move to the second monitor is not much use, and a modal dialog over a
    ground station during a flight would be indefensible.
  * Closing it stops the camera. A capture device held open by a window
    nobody can see is the kind of thing that makes the next program to
    want it fail for no visible reason.

Resolution and frame rate are offered because capture cards frequently
come up in a low default mode and stay there unless told otherwise. Both
default to Auto, which leaves the device on whatever it chooses.
"""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QComboBox, QFormLayout, QHBoxLayout, QLabel,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

AUTO = "Auto"


def _format_key(fmt):
    """(width, height) of a camera format."""
    size = fmt.resolution()
    return (size.width(), size.height())


class VideoWindow(QWidget):
    """Pick a video input and watch it."""

    def __init__(self, parent=None):
        # No parent on purpose: as a top-level window it gets its own entry
        # in the task bar and can be moved to another screen, which is the
        # whole point of having it in a window rather than a panel.
        super().__init__(None)
        self.setWindowTitle("Video")
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.resize(720, 560)

        self._camera = None
        self._session = None
        self._devices = []

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.video = QVideoWidget()
        self.video.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Expanding)
        self.video.setMinimumHeight(240)
        self.video.setStyleSheet("background: #000;")
        layout.addWidget(self.video, 1)

        form = QFormLayout()
        form.setSpacing(6)
        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        form.addRow("Video device", self.device_combo)

        self.res_combo = QComboBox()
        form.addRow("Resolution", self.res_combo)

        self.fps_combo = QComboBox()
        form.addRow("Frame rate", self.fps_combo)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh list")
        self.refresh_btn.setToolTip(
            "Look for devices again. Needed after plugging in a capture "
            "card, which Windows does not always announce.")
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

        # Windows announces a camera appearing or going away, so a capture
        # card plugged in while this is open turns up without the user
        # having to know about the Refresh button.
        self._watcher = QMediaDevices(self)
        self._watcher.videoInputsChanged.connect(self.reload_devices)

        self.reload_devices()

    # ---- devices ---------------------------------------------------------

    def reload_devices(self):
        """Rebuild the device list, keeping the current pick if it survives."""
        was = self._current_device_id()
        self._devices = list(QMediaDevices.videoInputs())
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for dev in self._devices:
            self.device_combo.addItem(dev.description(), dev)
        self.device_combo.blockSignals(False)

        if not self._devices:
            self._stop()
            self.device_combo.addItem("No video devices found")
            self.device_combo.setEnabled(False)
            self.start_btn.setEnabled(False)
            self._say("Nothing to show. Plug in a camera or capture card "
                      "and press Refresh list.")
            self.res_combo.clear()
            self.fps_combo.clear()
            return

        self.device_combo.setEnabled(True)
        self.start_btn.setEnabled(True)
        if was is not None:
            for i, dev in enumerate(self._devices):
                if dev.id() == was:
                    self.device_combo.setCurrentIndex(i)
                    break
            else:
                # The device being shown has gone. Stop rather than leave a
                # frozen last frame looking like a live picture.
                self._stop()
                self._say("The device that was playing has been unplugged.")
        self._device_changed()

    def _current_device_id(self):
        dev = self.device_combo.currentData()
        return dev.id() if dev is not None else None

    def _formats(self):
        dev = self.device_combo.currentData()
        return list(dev.videoFormats()) if dev is not None else []

    def _device_changed(self):
        """Offer only the sizes and rates this device actually reports."""
        formats = self._formats()
        sizes = sorted({_format_key(f) for f in formats}, reverse=True)
        rates = sorted({int(round(f.maxFrameRate())) for f in formats
                        if f.maxFrameRate() > 0}, reverse=True)

        self.res_combo.clear()
        self.res_combo.addItem(AUTO, None)
        for w, h in sizes:
            self.res_combo.addItem("%d x %d" % (w, h), (w, h))

        self.fps_combo.clear()
        self.fps_combo.addItem(AUTO, None)
        for r in rates:
            self.fps_combo.addItem("%d fps" % r, r)

        if formats:
            self._say("")
        else:
            # Some virtual cameras report nothing until they are running.
            # Auto still works for those, so this is a note, not an error.
            self._say("This device does not list its formats. Auto will "
                      "still work.")

    # ---- playing ---------------------------------------------------------

    def _pick_format(self):
        """The device's own format closest to what was asked for.

        Returns None for Auto, or when nothing matches - setting no format
        leaves the camera on its default, which is better than refusing to
        start over a resolution the user only expressed a preference for.
        """
        want_size = self.res_combo.currentData()
        want_fps = self.fps_combo.currentData()
        if want_size is None and want_fps is None:
            return None
        best = None
        for fmt in self._formats():
            if want_size is not None and _format_key(fmt) != want_size:
                continue
            if want_fps is not None and int(round(fmt.maxFrameRate())) != want_fps:
                continue
            # Among equals prefer the higher rate: a card offering the same
            # size at 30 and 60 should give the smoother one.
            if best is None or fmt.maxFrameRate() > best.maxFrameRate():
                best = fmt
        return best

    def _toggle(self):
        if self._camera is not None:
            self._stop()
            self._say("Stopped.")
        else:
            self.start()

    def start(self):
        """Open the selected device and show it."""
        self._stop()
        dev = self.device_combo.currentData()
        if dev is None:
            return
        camera = QCamera(dev, self)
        fmt = self._pick_format()
        if fmt is not None:
            camera.setCameraFormat(fmt)
        camera.errorOccurred.connect(self._camera_error)

        session = QMediaCaptureSession(self)
        session.setCamera(camera)
        session.setVideoOutput(self.video)

        self._camera = camera
        self._session = session
        camera.start()

        self.start_btn.setText("Stop")
        asked = self.res_combo.currentText()
        rate = self.fps_combo.currentText()
        self._say("Showing %s at %s, %s."
                  % (dev.description(), asked.lower(), rate.lower()))
        # A device that is already in use often fails a moment after
        # start() rather than during it, so confirm it really is running.
        QTimer.singleShot(1200, self._confirm_running)

    def _confirm_running(self):
        if self._camera is not None and not self._camera.isActive():
            self._say("That device did not start. It is usually already in "
                      "use by another program.")

    def _camera_error(self, _error, message):
        self._say(message or "The camera reported an error.")
        self._stop()

    def _stop(self):
        if self._camera is not None:
            self._camera.stop()
        if self._session is not None:
            self._session.setVideoOutput(None)
            self._session.setCamera(None)
        self._camera = None
        self._session = None
        self.start_btn.setText("Start")

    def _say(self, text):
        self.status.setText(text)

    # ---- window ----------------------------------------------------------

    def closeEvent(self, event: QCloseEvent):
        """Release the device rather than holding it open unseen."""
        self._stop()
        super().closeEvent(event)
