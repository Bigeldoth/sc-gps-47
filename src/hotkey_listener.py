"""Global hotkey listener for SpaceDrive GPS.

Listens for key combinations in a background thread (pynput) and emits
PyQt6 signals for: toggle overlay, open options, save position, open POI manager.
Hot-reload of hotkey bindings via reload_hotkeys().
"""
import logging
from pynput import keyboard as pynput_keyboard
from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


class HotkeyListener(QObject):
    toggle_overlay_triggered = pyqtSignal()
    open_options_triggered = pyqtSignal()
    save_position_triggered = pyqtSignal()
    open_poi_manager_triggered = pyqtSignal()
    stop_navigation_triggered = pyqtSignal()

    _SIGNAL_MAP = {
        'toggle_overlay': 'toggle_overlay_triggered',
        'open_options': 'open_options_triggered',
        'save_position': 'save_position_triggered',
        'open_poi_manager': 'open_poi_manager_triggered',
        'stop_navigation': 'stop_navigation_triggered',
    }

    _MODIFIER_NAMES = {'shift', 'ctrl', 'alt', 'cmd'}

    def __init__(self, config_manager):
        super().__init__()
        self.config_manager = config_manager
        self._pressed_keys = set()
        self._bindings = []
        self._listener = None
        self._setup_hotkeys()
        self._start_listener()

    def _parse_hotkey(self, hotkey_str):
        parts = [p.strip().lower() for p in hotkey_str.split('+')]
        modifiers = set()
        key = None
        for p in parts:
            if p in self._MODIFIER_NAMES:
                modifiers.add(p)
            else:
                try:
                    key = getattr(pynput_keyboard.Key, p)
                except AttributeError:
                    key = pynput_keyboard.KeyCode.from_char(p)
        return frozenset(modifiers), key

    def _active_modifiers(self):
        mods = set()
        if any(k in self._pressed_keys for k in (pynput_keyboard.Key.shift, pynput_keyboard.Key.shift_l, pynput_keyboard.Key.shift_r)):
            mods.add('shift')
        if any(k in self._pressed_keys for k in (pynput_keyboard.Key.ctrl, pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r)):
            mods.add('ctrl')
        if any(k in self._pressed_keys for k in (pynput_keyboard.Key.alt, pynput_keyboard.Key.alt_l, pynput_keyboard.Key.alt_r)):
            mods.add('alt')
        if any(k in self._pressed_keys for k in (pynput_keyboard.Key.cmd, pynput_keyboard.Key.cmd_l, pynput_keyboard.Key.cmd_r)):
            mods.add('cmd')
        return mods

    def _setup_hotkeys(self):
        self._bindings = []
        hotkeys = self.config_manager.get_all_hotkeys()
        for action, hotkey_str in hotkeys.items():
            signal_name = self._SIGNAL_MAP.get(action)
            if not signal_name:
                continue
            signal = getattr(self, signal_name)
            modifiers, key = self._parse_hotkey(hotkey_str)
            self._bindings.append((modifiers, key, signal))
            logger.debug(f"Hotkey registered: {action} -> {hotkey_str}")
        logger.info(f"Hotkeys configured: {list(hotkeys.keys())}")

    def _start_listener(self):
        def on_press(key):
            self._pressed_keys.add(key)
            active = self._active_modifiers()
            for required_mods, target_key, signal in self._bindings:
                if target_key == key and required_mods == active:
                    signal.emit()

        def on_release(key):
            self._pressed_keys.discard(key)

        self._listener = pynput_keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.start()

    def update_hotkey(self, action, new_hotkey):
        self.config_manager.set_hotkey(action, new_hotkey)
        self._setup_hotkeys()
        logger.info(f"Hotkey updated: {action} -> {new_hotkey}")

    def reload_hotkeys(self):
        self._setup_hotkeys()

    def cleanup(self):
        if self._listener:
            self._listener.stop()
        logger.info("HotkeyListener cleaned up")
