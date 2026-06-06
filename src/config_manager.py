"""
Centralised configuration manager for SpaceDrive GPS.
Handles reading and writing of the config.ini file.
"""
import configparser
import os
import sys
import logging

logger = logging.getLogger(__name__)


class ConfigManager:
    """Centralised configuration manager."""

    def __init__(self, config_file='config.ini'):
        """
        Initialise the configuration manager.

        Args:
            config_file: Path to the configuration file.

        Path strategy: the writable config lives under user_data_dir() so a
        Program Files install never tries to write into its own read-only
        folder. On first launch, if the user config is missing, we seed it
        by copying the default config.ini shipped with the bundle.
        """
        import shutil
        from app_paths import bundle_dir, user_data_dir
        self.base_dir = str(bundle_dir())  # kept for callers that still read it

        bundled_default = os.path.join(str(bundle_dir()), config_file)
        self.config_path = os.path.join(str(user_data_dir()), config_file)

        # Seed the user config from the bundle on first run.
        if not os.path.exists(self.config_path) and os.path.exists(bundled_default):
            try:
                shutil.copy2(bundled_default, self.config_path)
                logger.info(
                    f"Seeded user config at {self.config_path} from {bundled_default}"
                )
            except Exception as e:
                logger.warning(f"Could not seed user config: {e}")

        self.config = configparser.ConfigParser()
        self.load()
        self._migrate()

    def load(self):
        """Load the configuration from file.

        Uses utf-8-sig encoding to transparently strip any UTF-8 BOM that
        PowerShell 5.1 may have written (Set-Content -Encoding utf8 adds a BOM
        that configparser cannot parse, causing MissingSectionHeaderError).
        """
        try:
            self.config.read(self.config_path, encoding='utf-8-sig')
            logger.info(f"Configuration loaded from {self.config_path}")
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            self._create_default_config()

    def save(self):
        """Save the configuration to file."""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)
            logger.info(f"Configuration saved to {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")
            return False

    def _create_default_config(self):
        """Create a default configuration if the file does not exist."""
        logger.warning("Creating default configuration")

        # Existing sections
        if not self.config.has_section('Logging'):
            self.config.add_section('Logging')
            self.config.set('Logging', 'level', 'INFO')
            self.config.set('Logging', 'file', 'spacedrive.log')

        if not self.config.has_section('Debug'):
            self.config.add_section('Debug')
            self.config.set('Debug', 'save_ocr_images', 'False')
            self.config.set('Debug', 'save_glyph_crops', 'False')
            self.config.set('Debug', 'verbose_mode', 'False')
            self.config.set('Debug', 'record_telemetry', 'False')

        if not self.config.has_section('Features'):
            self.config.add_section('Features')
            self.config.set('Features', 'interactive_mode', 'True')
            self.config.set('Features', 'poi_management', 'True')

        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
            self.config.set('OCR', 'tesseract_path', r'C:\Program Files\Tesseract-OCR\tesseract.exe')
            self.config.set('OCR', 'scan_interval_ms', '200')

        if not self.config.has_section('Overlay'):
            self.config.add_section('Overlay')
            self.config.set('Overlay', 'default_opacity', '1.0')
            self.config.set('Overlay', 'default_position_x', '50')
            self.config.set('Overlay', 'default_position_y', '50')
            self.config.set('Overlay', 'compact_mode', 'False')

        if not self.config.has_section('Navigation'):
            self.config.add_section('Navigation')
            self.config.set('Navigation', 'arrival_radius_m', '2000')

        if not self.config.has_section('Hotkeys'):
            self.config.add_section('Hotkeys')
            self.config.set('Hotkeys', 'toggle_overlay', 'shift+f1')
            self.config.set('Hotkeys', 'open_options', 'shift+f2')
            self.config.set('Hotkeys', 'save_position', 'shift+f3')
            self.config.set('Hotkeys', 'open_poi_manager', 'shift+f4')
            self.config.set('Hotkeys', 'reset_gps_nav', 'shift+f5')

        if not self.config.has_section('Updates'):
            self.config.add_section('Updates')
            self.config.set('Updates', 'check_on_startup', 'False')
            self.config.set('Updates', 'keep_deltas_count', '3')
            self.config.set('Updates', 'last_skipped_version', '')

        self.save()

    def _migrate(self):
        """Add any keys missing from the user config (new options added in later versions).

        Called on every startup after load(). Only adds missing sections/keys;
        never overwrites existing user values. Saves once if anything was added.
        """
        # Canonical defaults: {section: {key: default_value}}
        DEFAULTS = {
            'Logging': {
                'level': 'INFO',
                'file': 'spacedrive.log',
            },
            'Debug': {
                'save_ocr_images': 'False',
                'save_glyph_crops': 'False',
                'verbose_mode': 'False',
                'record_telemetry': 'False',
                'test_screenshot': '',
            },
            'Features': {
                'interactive_mode': 'True',
                'poi_management': 'True',
            },
            'OCR': {
                'tesseract_path': r'C:\Program Files\Tesseract-OCR\tesseract.exe',
                'scan_interval_ms': '200',
                'glyph_engine': 'onnx',
                'onnx_model_path': 'models/spacedrive_ocr.onnx',
                'onnx_classes_path': 'models/spacedrive_ocr.classes.json',
                'onnx_confidence_threshold': '0.85',
                'text_engine': 'tesseract',
                'pipeline_mode': 'hybrid',
                'paddle_device': 'cpu',
            },
            'Settings': {
                'refresh_interval_ms': '200',
            },
            'Overlay': {
                'default_opacity': '1.00',
                'default_position_x': '50',
                'default_position_y': '50',
                'compact_mode': 'False',
            },
            'Hotkeys': {
                'toggle_overlay': 'shift+f1',
                'open_options': 'shift+f2',
                'save_position': 'shift+f3',
                'open_poi_manager': 'shift+f4',
                'reset_gps_nav': 'shift+f5',
            },
            'Navigation': {
                'arrival_radius_m': '2000.0',
            },
            'Kalman': {
                'max_speed_km_s': '2.5',
                'sigma_a': '1.0',
                'sigma_z': '0.02',
                'gate_nis': '30.0',
                'coast_s': '2.0',
            },
            'Updates': {
                'check_on_startup': 'False',
                'keep_deltas_count': '3',
                'last_skipped_version': '',
            },
        }

        added = False
        for section, keys in DEFAULTS.items():
            if not self.config.has_section(section):
                self.config.add_section(section)
                logger.info(f"[migrate] Added missing section [{section}]")
                added = True
            for key, default in keys.items():
                if not self.config.has_option(section, key):
                    self.config.set(section, key, default)
                    logger.info(f"[migrate] Added missing key [{section}] {key} = {default!r}")
                    added = True

        if added:
            self.save()

    # Quick-access methods for frequently used settings

    def get_scan_interval(self):
        """Return the OCR scan interval in milliseconds."""
        try:
            return self.config.getint('OCR', 'scan_interval_ms', fallback=200)
        except:
            return 200

    def set_scan_interval(self, interval_ms):
        """Set the OCR scan interval in milliseconds."""
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'scan_interval_ms', str(interval_ms))

    def get_ocr_engine(self):
        """Return the text OCR engine to use ('tesseract' or 'paddle').

        Reads the new key `text_engine` first, falling back to the legacy
        `engine` key for backward compatibility with pre-Paddle configs.
        """
        value = self.config.get(
            'OCR', 'text_engine',
            fallback=self.config.get('OCR', 'engine', fallback='tesseract'),
        ).lower()
        return value if value in ('tesseract', 'paddle') else 'tesseract'

    def set_ocr_engine(self, engine):
        """Set the OCR engine to use. Writes to `text_engine` and clears the
        legacy `engine` key so the new value is unambiguous."""
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'text_engine', engine.lower())
        if self.config.has_option('OCR', 'engine'):
            self.config.remove_option('OCR', 'engine')

    def get_pipeline_mode(self):
        """Return the OCR pipeline mode: 'hybrid' (NCC/ONNX + text engine
        fallback) or 'full_text' (text engine only, bypass glyph stage)."""
        value = self.config.get('OCR', 'pipeline_mode', fallback='hybrid').lower()
        return value if value in ('hybrid', 'full_text') else 'hybrid'

    def set_pipeline_mode(self, mode):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'pipeline_mode', mode.lower())

    def get_paddle_device(self):
        """Return the device PaddleOCR should run on ('cpu' or 'gpu')."""
        value = self.config.get('OCR', 'paddle_device', fallback='cpu').lower()
        return value if value in ('cpu', 'gpu') else 'cpu'

    def set_paddle_device(self, device):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'paddle_device', device.lower())

    def get_paddle_model_dir(self):
        """Return the fine-tuned PaddleOCR recognition model dir (empty = use
        the official pretrained model)."""
        return self.config.get('OCR', 'paddle_model_dir', fallback='')

    def set_paddle_model_dir(self, path):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'paddle_model_dir', path or '')

    def get_paddle_lang(self):
        return self.config.get('OCR', 'paddle_lang', fallback='en')

    def get_paddle_min_confidence(self):
        """Per-line Paddle confidence floor. Lines below are dropped before
        regex parsing. 0.0 disables the filter."""
        try:
            value = self.config.getfloat(
                'OCR', 'paddle_min_confidence', fallback=0.30,
            )
        except Exception:
            return 0.30
        return max(0.0, min(1.0, value))

    def set_paddle_min_confidence(self, threshold):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set(
            'OCR', 'paddle_min_confidence',
            str(max(0.0, min(1.0, float(threshold)))),
        )

    def set_paddle_lang(self, lang):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'paddle_lang', lang)

    def get_tesseract_lang(self):
        """Tesseract language pack to use (`eng` is the stock pretrained
        English model; `spacedrive` is the project's fine-tuned LSTM —
        only available if `models/tessdata/spacedrive.traineddata` was
        shipped or produced by `tools/train_tesseract.py`)."""
        value = self.config.get('OCR', 'tesseract_lang', fallback='eng')
        return value.strip() or 'eng'

    def set_tesseract_lang(self, lang):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'tesseract_lang', (lang or 'eng').strip())

    def get_tesseract_tessdata_dir(self):
        """Optional override for the tessdata directory passed to Tesseract.
        Empty = let Tesseract use its system default. Set to e.g.
        `models/tessdata` to load the fine-tuned `spacedrive.traineddata`
        shipped with the project."""
        return self.config.get('OCR', 'tesseract_tessdata_dir', fallback='').strip()

    def set_tesseract_tessdata_dir(self, path):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'tesseract_tessdata_dir', (path or '').strip())

    def get_glyph_engine(self):
        """Return the glyph classifier (ncc or onnx)."""
        value = self.config.get('OCR', 'glyph_engine', fallback='ncc').lower()
        return value if value in ('ncc', 'onnx') else 'ncc'

    def set_glyph_engine(self, engine):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'glyph_engine', engine.lower())

    def get_onnx_model_path(self):
        return self.config.get('OCR', 'onnx_model_path',
                               fallback='models/spacedrive_ocr.onnx')

    def get_onnx_classes_path(self):
        return self.config.get('OCR', 'onnx_classes_path',
                               fallback='models/spacedrive_ocr.classes.json')

    def get_onnx_confidence_threshold(self):
        try:
            return self.config.getfloat('OCR', 'onnx_confidence_threshold', fallback=0.85)
        except Exception:
            return 0.85

    def get_arrival_radius_m(self):
        """Return the arrival precision radius in meters.

        When the smoothed distance to target drops below this radius, the EMA
        is bypassed and the raw OCR distance is shown directly — avoids the
        common ~1-2 s display lag at touchdown.
        """
        try:
            value = self.config.getfloat('Navigation', 'arrival_radius_m', fallback=100.0)
        except Exception:
            return 100.0
        return max(1.0, value)

    def set_arrival_radius_m(self, radius_m):
        """Set the arrival precision radius (meters). Minimum 1 m."""
        if not self.config.has_section('Navigation'):
            self.config.add_section('Navigation')
        self.config.set('Navigation', 'arrival_radius_m', str(max(1.0, float(radius_m))))

    # ── Kalman / velocity-tracker tuning ────────────────────────────────
    # Advanced knobs for the constant-velocity Kalman filter that derives the
    # movement heading from the OCR position stream (see VelocityTracker). All
    # live under the [Kalman] section; defaults mirror the class constants in
    # src/velocity_tracker.py and are clamped to sane bounds on read.

    def get_kalman_max_speed_km_s(self):
        """Physical speed ceiling in km/s (velocity clamp + bounded init).

        Default 2.5 (SC tops out ~1.4 km/s/axis; this leaves 3D margin)."""
        try:
            value = self.config.getfloat('Kalman', 'max_speed_km_s', fallback=2.5)
        except Exception:
            return 2.5
        return max(0.1, min(50.0, value))

    def set_kalman_max_speed_km_s(self, value):
        if not self.config.has_section('Kalman'):
            self.config.add_section('Kalman')
        self.config.set('Kalman', 'max_speed_km_s',
                        str(max(0.1, min(50.0, float(value)))))

    def get_kalman_sigma_a(self):
        """Acceleration process-noise std in km/s^2. Higher = the filter
        follows turns faster but is noisier. Default 1.0."""
        try:
            value = self.config.getfloat('Kalman', 'sigma_a', fallback=1.0)
        except Exception:
            return 1.0
        return max(0.001, min(100.0, value))

    def set_kalman_sigma_a(self, value):
        if not self.config.has_section('Kalman'):
            self.config.add_section('Kalman')
        self.config.set('Kalman', 'sigma_a',
                        str(max(0.001, min(100.0, float(value)))))

    def get_kalman_sigma_z(self):
        """Per-axis measurement (position) noise std in km. Higher = the filter
        trusts OCR reads less and smooths more. Default 0.02 (20 m)."""
        try:
            value = self.config.getfloat('Kalman', 'sigma_z', fallback=0.02)
        except Exception:
            return 0.02
        return max(0.0001, min(10.0, value))

    def set_kalman_sigma_z(self, value):
        if not self.config.has_section('Kalman'):
            self.config.add_section('Kalman')
        self.config.set('Kalman', 'sigma_z',
                        str(max(0.0001, min(10.0, float(value)))))

    def get_kalman_gate_nis(self):
        """Innovation-gate threshold (normalised innovation squared) above which
        a sample is rejected as an OCR misread. Default 30.0."""
        try:
            value = self.config.getfloat('Kalman', 'gate_nis', fallback=30.0)
        except Exception:
            return 30.0
        return max(0.1, min(100000.0, value))

    def set_kalman_gate_nis(self, value):
        if not self.config.has_section('Kalman'):
            self.config.add_section('Kalman')
        self.config.set('Kalman', 'gate_nis',
                        str(max(0.1, min(100000.0, float(value)))))

    def get_kalman_coast_s(self):
        """Dead-reckoning coast window in seconds before integrity degrades to
        LOST and the filter re-seeds. Default 2.0."""
        try:
            value = self.config.getfloat('Kalman', 'coast_s', fallback=2.0)
        except Exception:
            return 2.0
        return max(0.0, min(30.0, value))

    def set_kalman_coast_s(self, value):
        if not self.config.has_section('Kalman'):
            self.config.add_section('Kalman')
        self.config.set('Kalman', 'coast_s',
                        str(max(0.0, min(30.0, float(value)))))

    def get_record_telemetry(self):
        """Whether to record per-tick navigation telemetry to a JSONL file.

        Off by default. Intended for offline analysis and filter/coast-window
        tuning — see ``src/telemetry.py``."""
        try:
            return self.config.getboolean('Debug', 'record_telemetry', fallback=False)
        except Exception:
            return False

    def get_hotkey(self, action):
        """
        Return the keyboard shortcut for a given action.

        Args:
            action: Action name (toggle_overlay, open_options, etc.)

        Returns:
            str: Keyboard shortcut (e.g. 'shift+f1')
        """
        defaults = {
            'toggle_overlay': 'shift+f1',
            'open_options': 'shift+f2',
            'save_position': 'shift+f3',
            'open_poi_manager': 'shift+f4',
            'reset_gps_nav': 'shift+f5',
        }
        return self.config.get('Hotkeys', action, fallback=defaults.get(action, ''))

    def set_hotkey(self, action, hotkey):
        """
        Set the keyboard shortcut for an action.

        Args:
            action: Action name.
            hotkey: New shortcut (e.g. 'ctrl+alt+o')
        """
        if not self.config.has_section('Hotkeys'):
            self.config.add_section('Hotkeys')
        self.config.set('Hotkeys', action, hotkey)

    def get_all_hotkeys(self):
        """Return a dictionary of all configured hotkeys.

        Merges built-in defaults with whatever is in config.ini so that
        newly-added actions appear even when the file predates them.
        """
        if not self.config.has_section('Hotkeys'):
            self._create_default_config()

        defaults = {
            'toggle_overlay': 'shift+f1',
            'open_options': 'shift+f2',
            'save_position': 'shift+f3',
            'open_poi_manager': 'shift+f4',
            'reset_gps_nav': 'shift+f5',
        }
        merged = dict(defaults)
        merged.update(dict(self.config.items('Hotkeys')))
        return merged

    def get(self, section, option, fallback=None):
        """Generic method to retrieve a value."""
        return self.config.get(section, option, fallback=fallback)

    # ── Update configuration ────────────────────────────────────────────

    def get_check_on_startup(self):
        """Whether to automatically check for updates on app startup."""
        try:
            return self.config.getboolean('Updates', 'check_on_startup', fallback=False)
        except Exception:
            return False

    def set_check_on_startup(self, value):
        """Set whether to automatically check for updates on app startup."""
        if not self.config.has_section('Updates'):
            self.config.add_section('Updates')
        self.config.set('Updates', 'check_on_startup', str(bool(value)))

    def get_keep_deltas_count(self):
        """Number of old delta packages to keep locally (others are cleaned up)."""
        try:
            value = self.config.getint('Updates', 'keep_deltas_count', fallback=3)
        except Exception:
            return 3
        return max(1, min(10, value))

    def set_keep_deltas_count(self, count):
        """Set the number of old delta packages to keep locally."""
        if not self.config.has_section('Updates'):
            self.config.add_section('Updates')
        self.config.set('Updates', 'keep_deltas_count', str(max(1, min(10, int(count)))))

    def get_last_skipped_version(self):
        """Return the last version the user clicked Skip on (don't ask again)."""
        return self.config.get('Updates', 'last_skipped_version', fallback='')

    def set_last_skipped_version(self, version):
        """Set the last skipped version."""
        if not self.config.has_section('Updates'):
            self.config.add_section('Updates')
        self.config.set('Updates', 'last_skipped_version', version or '')
