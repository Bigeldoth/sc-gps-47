"""Engine Manager dialog — install/detect Tesseract and PaddleOCR.

Opened from the OCR tab of the Options dialog. Runs pip installs in a
background QThread so the UI remains responsive.
"""
import logging
import os
import webbrowser

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
    QPushButton, QTextEdit, QVBoxLayout,
)

from engine_installer import (
    TESSERACT_WINDOWS_URL,
    detect_cuda,
    detect_cuda_version,
    detect_gpu_paddle_status,
    detect_gpu_unsupported_by_paddle,
    detect_paddle_vl,
    detect_paddleocr,
    detect_paddlepaddle_gpu,
    detect_tesseract,
    install_paddle_vl,
    install_paddleocr_cpu,
    install_paddleocr_gpu,
    migrate_paddle_to_blackwell,
    uninstall_paddle_vl,
    uninstall_paddleocr,
)

logger = logging.getLogger(__name__)


class _InstallWorker(QThread):
    """Runs a single installer function in the background.

    Streams pip output line by line via the `line` signal so the dialog can
    show progress while the install runs (paddle-gpu pulls ~2 GB of CUDA
    wheels — without streaming the UI looks frozen).
    """

    line = pyqtSignal(str)
    finished_with_result = pyqtSignal(bool, str)

    def __init__(self, action_fn, parent=None):
        super().__init__(parent)
        self._action_fn = action_fn

    def run(self):
        try:
            ok, output = self._action_fn(on_line=self.line.emit)
        except Exception as exc:
            ok, output = False, f"Unhandled error: {exc}"
        self.finished_with_result.emit(ok, output)


class EngineManagerDialog(QDialog):
    def __init__(self, parent=None, config_manager=None):
        super().__init__(parent)
        self.setWindowTitle("Manage OCR engines")
        self.setMinimumWidth(560)
        self.setMinimumHeight(420)

        # Optional config_manager — used by the Locate Tesseract button to
        # persist a custom tesseract.exe path. Dialog still works without it.
        self._config_manager = config_manager
        self._worker: _InstallWorker | None = None
        self._build_ui()
        self._refresh_status()

    # ----- UI build -----

    def _build_ui(self):
        layout = QVBoxLayout()

        self.tesseract_label = QLabel()
        self.tesseract_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.tesseract_label)

        tess_row = QHBoxLayout()
        self.locate_tesseract_button = QPushButton("Locate…")
        self.locate_tesseract_button.setToolTip(
            "Pick a tesseract.exe path manually and save it to config.ini"
        )
        self.locate_tesseract_button.clicked.connect(self._on_locate_tesseract)
        tess_row.addWidget(self.locate_tesseract_button)

        self.reinstall_tesseract_button = QPushButton("Download / Reinstall")
        self.reinstall_tesseract_button.setToolTip(
            "Open the UB-Mannheim Tesseract download page in your browser"
        )
        self.reinstall_tesseract_button.clicked.connect(
            lambda: webbrowser.open(TESSERACT_WINDOWS_URL)
        )
        tess_row.addWidget(self.reinstall_tesseract_button)
        tess_row.addStretch()
        layout.addLayout(tess_row)

        layout.addSpacing(12)

        self.paddle_label = QLabel()
        self.paddle_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.paddle_label)

        self.cuda_label = QLabel()
        layout.addWidget(self.cuda_label)

        paddle_row = QHBoxLayout()
        self.install_paddle_cpu_button = QPushButton("Install Paddle (CPU)")
        self.install_paddle_cpu_button.clicked.connect(self._on_install_paddle_cpu)
        paddle_row.addWidget(self.install_paddle_cpu_button)

        self.install_paddle_gpu_button = QPushButton("Install Paddle (GPU)")
        self.install_paddle_gpu_button.clicked.connect(self._on_install_paddle_gpu)
        paddle_row.addWidget(self.install_paddle_gpu_button)

        self.uninstall_paddle_button = QPushButton("Uninstall Paddle")
        self.uninstall_paddle_button.clicked.connect(self._on_uninstall_paddle)
        paddle_row.addWidget(self.uninstall_paddle_button)

        # Shown only on Blackwell GPUs when the installed paddle is too old.
        self.migrate_paddle_button = QPushButton("Migrate to Blackwell wheel (cu129)")
        self.migrate_paddle_button.setToolTip(
            "Replace the installed paddlepaddle-gpu with the cu129 / 3.2.1 wheel "
            "that ships sm_120 kernels for RTX 50 GPUs"
        )
        self.migrate_paddle_button.clicked.connect(self._on_migrate_paddle)
        self.migrate_paddle_button.setVisible(False)
        paddle_row.addWidget(self.migrate_paddle_button)

        paddle_row.addStretch()
        layout.addLayout(paddle_row)

        # ── PaddleOCR-VL (advanced sidecar) ─────────────────────────────
        layout.addSpacing(12)
        self.paddle_vl_label = QLabel()
        self.paddle_vl_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.paddle_vl_label)

        vl_hint = QLabel(
            "<span style='color:#888'>Advanced: heavy VL pipeline "
            "(PaddleOCR-VL-1.5-0.9B, ~3-5 GB VRAM, ~10× slower). "
            "Recommended only for snapshot/export, not real-time HUD scan.</span>"
        )
        vl_hint.setWordWrap(True)
        layout.addWidget(vl_hint)

        vl_row = QHBoxLayout()
        self.install_paddle_vl_button = QPushButton("Install Paddle-VL (sidecar)")
        self.install_paddle_vl_button.clicked.connect(self._on_install_paddle_vl)
        vl_row.addWidget(self.install_paddle_vl_button)

        self.uninstall_paddle_vl_button = QPushButton("Uninstall Paddle-VL")
        self.uninstall_paddle_vl_button.clicked.connect(self._on_uninstall_paddle_vl)
        vl_row.addWidget(self.uninstall_paddle_vl_button)

        vl_row.addStretch()
        layout.addLayout(vl_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # busy indicator
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Installer output will appear here.")
        layout.addWidget(self.output, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        self.setLayout(layout)

    # ----- Status refresh -----

    def _refresh_status(self):
        tess = detect_tesseract()
        if tess.installed:
            self.tesseract_label.setText(
                f"<b>Tesseract</b>: ✓ Installed (v{tess.version})<br>"
                f"<span style='color:#888'>{tess.detail}</span>"
            )
        else:
            self.tesseract_label.setText("<b>Tesseract</b>: ✗ Not installed")

        paddle = detect_paddleocr()
        gpu_build = detect_paddlepaddle_gpu()
        if paddle.installed:
            build = "GPU" if gpu_build else "CPU"
            self.paddle_label.setText(
                f"<b>PaddleOCR</b>: ✓ Installed (v{paddle.version}, {build} build)"
            )
            # When already installed, install buttons become Reinstall and the
            # matching build's button is the "primary" one.
            self.install_paddle_cpu_button.setText("Reinstall (CPU)")
            self.install_paddle_gpu_button.setText("Reinstall (GPU)")
        else:
            extra = f"<br><span style='color:#a55'>{paddle.detail}</span>" if paddle.detail else ""
            self.paddle_label.setText(f"<b>PaddleOCR</b>: ✗ Not installed{extra}")
            self.install_paddle_cpu_button.setText("Install (CPU)")
            self.install_paddle_gpu_button.setText("Install (GPU)")

        cuda = detect_cuda()
        gpu_status, gpu_msg = detect_gpu_paddle_status()
        # Tri-state CUDA label.
        if gpu_status == "needs_blackwell_wheel":
            self.cuda_label.setText(
                f"<span style='color:#fb5'>⚠ {gpu_msg}</span>"
            )
        elif gpu_status == "too_new":
            self.cuda_label.setText(
                f"<span style='color:#e88'>⚠ {gpu_msg}</span>"
            )
        elif gpu_status == "ok" and cuda:
            self.cuda_label.setText(
                "<span style='color:#5c5'>✓ CUDA runtime detected — GPU install available</span>"
            )
        else:
            self.cuda_label.setText(
                "<span style='color:#888'>CUDA runtime not detected — GPU install disabled</span>"
            )

        # Buttons:
        #  - Install (GPU): only when GPU is OK or when Blackwell needs a wheel
        #    (in that case the installer auto-routes to cu129).
        gpu_install_enabled = cuda and gpu_status in ("ok", "needs_blackwell_wheel")
        self.install_paddle_gpu_button.setEnabled(gpu_install_enabled)
        #  - Migrate: visible when paddle is installed AND it's too old for Blackwell.
        self.migrate_paddle_button.setVisible(
            paddle.installed and gpu_status == "needs_blackwell_wheel"
        )
        self.migrate_paddle_button.setEnabled(
            paddle.installed and gpu_status == "needs_blackwell_wheel"
        )
        self.uninstall_paddle_button.setEnabled(paddle.installed)

        # ── PaddleOCR-VL ────────────────────────────────────────────────
        vl = detect_paddle_vl()
        if vl.installed:
            running = False
            try:
                from paddle_vl_service import get_service
                running = get_service().is_running()
            except Exception:
                pass
            badge = (
                " <span style='color:#5c5'>● Service running on port 8118</span>"
                if running else ""
            )
            self.paddle_vl_label.setText(
                f"<b>PaddleOCR-VL</b>: ✓ Installed{badge}<br>"
                f"<span style='color:#888'>{vl.detail}</span>"
            )
            self.install_paddle_vl_button.setText("Reinstall Paddle-VL")
            self.uninstall_paddle_vl_button.setEnabled(True)
        else:
            self.paddle_vl_label.setText(
                "<b>PaddleOCR-VL</b>: ✗ Not installed"
            )
            self.install_paddle_vl_button.setText("Install Paddle-VL (sidecar)")
            self.uninstall_paddle_vl_button.setEnabled(False)

    # ----- Tesseract locate -----

    def _on_locate_tesseract(self):
        """Pick a tesseract.exe path manually and persist it to config.ini."""
        current = ""
        if self._config_manager is not None:
            current = self._config_manager.get(
                "OCR", "tesseract_path", fallback=""
            ) or ""
        start_dir = os.path.dirname(current) if current else r"C:\Program Files"

        path, _ = QFileDialog.getOpenFileName(
            self, "Locate tesseract.exe", start_dir,
            "Tesseract executable (tesseract.exe);;All files (*.*)",
        )
        if not path:
            return

        # Validate by trying pytesseract against the chosen binary.
        try:
            import pytesseract
            previous = pytesseract.pytesseract.tesseract_cmd
            pytesseract.pytesseract.tesseract_cmd = path
            try:
                version = str(pytesseract.get_tesseract_version())
            finally:
                pytesseract.pytesseract.tesseract_cmd = previous
        except Exception as exc:
            QMessageBox.warning(
                self, "Locate Tesseract",
                f"Could not run the selected executable:\n{exc}",
            )
            return

        # Persist to config.ini if we have access to the config manager.
        if self._config_manager is not None:
            cfg = self._config_manager
            if not cfg.config.has_section("OCR"):
                cfg.config.add_section("OCR")
            cfg.config.set("OCR", "tesseract_path", path)
            cfg.save()
            saved_note = " and saved to config.ini"
        else:
            saved_note = " (config manager unavailable — path not persisted)"

        # Also tell pytesseract for the current process so the next OCR call
        # already uses the new binary.
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = path
        except Exception:
            pass

        QMessageBox.information(
            self, "Locate Tesseract",
            f"Tesseract v{version} located at:\n{path}{saved_note}",
        )
        self._refresh_status()

    # ----- Worker glue -----

    def _start_worker(self, fn, busy_label: str):
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self, "Engine manager",
                "Another install is already running. Please wait for it to finish.",
            )
            return
        self.output.append(f"\n→ {busy_label}…\n")
        self.progress.setVisible(True)
        self._set_buttons_enabled(False)
        self._worker = _InstallWorker(fn, self)
        self._worker.line.connect(self._on_worker_line)
        self._worker.finished_with_result.connect(self._on_worker_done)
        self._worker.start()

    def _on_worker_line(self, line: str):
        """Appends a single streamed pip line and keeps the view scrolled."""
        self.output.append(line)
        bar = self.output.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_worker_done(self, ok: bool, output: str):
        self.progress.setVisible(False)
        self._set_buttons_enabled(True)
        # Streamed lines populate the textbox as the worker runs, but if the
        # worker raised before its first `on_line()` (e.g. an exception while
        # importing paddle, or a missing helper module), the streaming path
        # delivers nothing. Show the captured output on failure so the user
        # is never left with just "✗ Failed".
        if not ok and output:
            # Heuristic: only re-append if nothing was streamed (avoids
            # double-printing on the happy failure path).
            current = self.output.toPlainText()
            if "Traceback" not in current and output.strip() not in current:
                self.output.append(output.rstrip())
        # Always mirror the result to the app log for post-mortem.
        if ok:
            logger.info("Engine manager action succeeded")
        else:
            logger.error(
                "Engine manager action failed. Output:\n%s",
                output or "<no output captured>",
            )
        self.output.append("\n✓ Success\n" if ok else "\n✗ Failed\n")
        self._refresh_status()

    def _set_buttons_enabled(self, enabled: bool):
        gpu_status, _ = detect_gpu_paddle_status()
        cuda = detect_cuda()
        paddle_installed = detect_paddleocr().installed
        vl_installed = detect_paddle_vl().installed
        self.install_paddle_cpu_button.setEnabled(enabled)
        self.install_paddle_gpu_button.setEnabled(
            enabled and cuda and gpu_status in ("ok", "needs_blackwell_wheel")
        )
        self.uninstall_paddle_button.setEnabled(enabled and paddle_installed)
        self.migrate_paddle_button.setEnabled(
            enabled and paddle_installed and gpu_status == "needs_blackwell_wheel"
        )
        self.install_paddle_vl_button.setEnabled(enabled)
        self.uninstall_paddle_vl_button.setEnabled(enabled and vl_installed)

    # ----- Button handlers -----

    def _on_install_paddle_cpu(self):
        self._start_worker(install_paddleocr_cpu, "Installing PaddleOCR (CPU)")

    def _on_install_paddle_gpu(self):
        cuda_version = detect_cuda_version()
        version_line = (
            f"Detected CUDA: <b>{cuda_version}</b>"
            if cuda_version
            else "CUDA version could not be detected (nvidia-smi missing). "
                 "The CUDA 11.8 wheel will be tried."
        )
        confirm = QMessageBox.question(
            self, "Install Paddle GPU",
            f"{version_line}<br><br>"
            "paddlepaddle-gpu will be downloaded from the PaddlePaddle "
            "project index (not PyPI). The wheel is large (~700 MB). "
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._start_worker(install_paddleocr_gpu, "Installing PaddleOCR (GPU)")

    def _on_uninstall_paddle(self):
        confirm = QMessageBox.question(
            self, "Uninstall Paddle",
            "Remove paddleocr + paddlepaddle from this Python environment?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._start_worker(uninstall_paddleocr, "Uninstalling PaddleOCR")

    def _on_migrate_paddle(self):
        confirm = QMessageBox.question(
            self, "Migrate Paddle to Blackwell wheel",
            "Replace the installed paddlepaddle-gpu with the cu129 / 3.2.1 wheel "
            "from the official PaddlePaddle index?<br><br>"
            "This is the only wheel that ships sm_120 kernels (RTX 50 / Blackwell). "
            "The replacement is ~700 MB and may pull additional CUDA libs.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._start_worker(
                migrate_paddle_to_blackwell, "Migrating Paddle to cu129 wheel",
            )

    def _on_install_paddle_vl(self):
        confirm = QMessageBox.question(
            self, "Install Paddle-VL",
            "Install PaddleOCR-VL in a dedicated venv at "
            "<code>.venv-paddle-vl/</code>?<br><br>"
            "<b>Heavy install</b>: ~6 GB on disk, downloads the "
            "PaddleOCR-VL-1.5-0.9B model on first run, and requires the "
            "transformers backend (vLLM/SGLang unsupported on Windows).<br><br>"
            "This is only useful for snapshot/export — it is ~10× slower "
            "than standard paddle and not suited to real-time HUD scan.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._start_worker(install_paddle_vl, "Installing PaddleOCR-VL")

    def _on_uninstall_paddle_vl(self):
        confirm = QMessageBox.question(
            self, "Uninstall Paddle-VL",
            "Remove the entire <code>.venv-paddle-vl/</code> directory? "
            "(this frees ~6 GB)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._start_worker(uninstall_paddle_vl, "Removing PaddleOCR-VL venv")
