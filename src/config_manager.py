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
        """
        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        self.config_path = os.path.join(self.base_dir, config_file)
        self.config = configparser.ConfigParser()
        self.load()

    def load(self):
        """Load the configuration from file."""
        try:
            self.config.read(self.config_path, encoding='utf-8')
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
            self.config.set('Logging', 'level', 'DEBUG')
            self.config.set('Logging', 'file', 'spacedrive.log')

        if not self.config.has_section('Debug'):
            self.config.add_section('Debug')
            self.config.set('Debug', 'capture_screenshot', 'False')
            self.config.set('Debug', 'save_ocr_images', 'True')
            self.config.set('Debug', 'verbose_mode', 'True')

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
            self.config.set('Overlay', 'default_opacity', '0.7')
            self.config.set('Overlay', 'default_position_x', '50')
            self.config.set('Overlay', 'default_position_y', '50')

        if not self.config.has_section('Navigation'):
            self.config.add_section('Navigation')
            self.config.set('Navigation', 'arrival_radius_m', '100')

        if not self.config.has_section('Hotkeys'):
            self.config.add_section('Hotkeys')
            self.config.set('Hotkeys', 'toggle_overlay', 'shift+f1')
            self.config.set('Hotkeys', 'open_options', 'shift+f2')
            self.config.set('Hotkeys', 'save_position', 'shift+f3')
            self.config.set('Hotkeys', 'open_poi_manager', 'ctrl+shift+p')

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

    def set_paddle_lang(self, lang):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'paddle_lang', lang)

    # ── PaddleOCR-VL sidecar ────────────────────────────────────────────

    def get_paddle_vl_endpoint(self):
        return self.config.get(
            'OCR', 'paddle_vl_endpoint', fallback='http://127.0.0.1:8118',
        )

    def set_paddle_vl_endpoint(self, endpoint):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'paddle_vl_endpoint', endpoint)

    def get_paddle_vl_model(self):
        return self.config.get(
            'OCR', 'paddle_vl_model', fallback='PaddleOCR-VL-1.5-0.9B',
        )

    def set_paddle_vl_model(self, model):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'paddle_vl_model', model)

    def get_paddle_vl_backend(self):
        value = self.config.get(
            'OCR', 'paddle_vl_backend', fallback='transformers',
        ).lower()
        return value if value in ('transformers', 'vllm', 'sglang') else 'transformers'

    def set_paddle_vl_backend(self, backend):
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'paddle_vl_backend', backend.lower())

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
            'open_poi_manager': 'ctrl+shift+p'
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
        """Return a dictionary of all configured hotkeys."""
        if not self.config.has_section('Hotkeys'):
            self._create_default_config()

        return dict(self.config.items('Hotkeys'))

    def get(self, section, option, fallback=None):
        """Generic method to retrieve a value."""
        return self.config.get(section, option, fallback=fallback)
