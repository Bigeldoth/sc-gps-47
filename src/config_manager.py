"""
Gestionnaire centralisé de configuration pour SpaceDrive GPS.
Gère la lecture et l'écriture du fichier config.ini.
"""
import configparser
import os
import sys
import logging

logger = logging.getLogger(__name__)


class ConfigManager:
    """Gestionnaire de configuration centralisé"""
    
    def __init__(self, config_file='config.ini'):
        """
        Initialise le gestionnaire de configuration.
        
        Args:
            config_file: Chemin vers le fichier de configuration
        """
        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        self.config_path = os.path.join(self.base_dir, config_file)
        self.config = configparser.ConfigParser()
        self.load()
    
    def load(self):
        """Charge la configuration depuis le fichier"""
        try:
            self.config.read(self.config_path, encoding='utf-8')
            logger.info(f"Configuration chargée depuis {self.config_path}")
        except Exception as e:
            logger.error(f"Erreur lors du chargement de la configuration : {e}")
            self._create_default_config()
    
    def save(self):
        """Sauvegarde la configuration dans le fichier"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                self.config.write(f)
            logger.info(f"Configuration sauvegardée dans {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"Erreur lors de la sauvegarde de la configuration : {e}")
            return False
    
    def _create_default_config(self):
        """Crée une configuration par défaut si le fichier n'existe pas"""
        logger.warning("Création d'une configuration par défaut")
        
        # Sections existantes
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
            self.config.set('Overlay', 'show_status_bar', 'True')
        
        if not self.config.has_section('Hotkeys'):
            self.config.add_section('Hotkeys')
            self.config.set('Hotkeys', 'toggle_overlay', 'shift+f1')
            self.config.set('Hotkeys', 'open_options', 'shift+f2')
            self.config.set('Hotkeys', 'save_position', 'shift+f3')
            self.config.set('Hotkeys', 'open_poi_manager', 'ctrl+shift+p')
        
        self.save()
    
    # Méthodes d'accès rapide pour les paramètres fréquents
    
    def get_scan_interval(self):
        """Retourne l'intervalle de scan OCR en millisecondes"""
        try:
            return self.config.getint('OCR', 'scan_interval_ms', fallback=200)
        except:
            return 200
    
    def set_scan_interval(self, interval_ms):
        """Définit l'intervalle de scan OCR en millisecondes"""
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'scan_interval_ms', str(interval_ms))
    
    def get_ocr_engine(self):
        """Retourne le moteur OCR à utiliser (tesseract ou paddle)"""
        return self.config.get('OCR', 'engine', fallback='tesseract').lower()
    
    def set_ocr_engine(self, engine):
        """Définit le moteur OCR à utiliser"""
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'engine', engine.lower())
    
    def get_paddle_use_gpu(self):
        """Retourne si PaddleOCR doit utiliser le GPU"""
        return self.config.getboolean('OCR', 'paddle_use_gpu', fallback=False)
    
    def set_paddle_use_gpu(self, use_gpu):
        """Définit si PaddleOCR doit utiliser le GPU"""
        if not self.config.has_section('OCR'):
            self.config.add_section('OCR')
        self.config.set('OCR', 'paddle_use_gpu', str(use_gpu))
    
    def get_hotkey(self, action):
        """
        Retourne le raccourci clavier pour une action donnée.
        
        Args:
            action: Nom de l'action (toggle_overlay, open_options, etc.)
        
        Returns:
            str: Raccourci clavier (ex: 'shift+f1')
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
        Définit le raccourci clavier pour une action.
        
        Args:
            action: Nom de l'action
            hotkey: Nouveau raccourci (ex: 'ctrl+alt+o')
        """
        if not self.config.has_section('Hotkeys'):
            self.config.add_section('Hotkeys')
        self.config.set('Hotkeys', action, hotkey)
    
    def get_all_hotkeys(self):
        """Retourne un dictionnaire de tous les hotkeys configurés"""
        if not self.config.has_section('Hotkeys'):
            self._create_default_config()
        
        return dict(self.config.items('Hotkeys'))
    
    def get_show_status_bar(self):
        """Retourne si la barre d'état doit être affichée"""
        return self.config.getboolean('Overlay', 'show_status_bar', fallback=True)
    
    def set_show_status_bar(self, show):
        """Définit si la barre d'état doit être affichée"""
        if not self.config.has_section('Overlay'):
            self.config.add_section('Overlay')
        self.config.set('Overlay', 'show_status_bar', str(show))
    
    def get_yaw_calibration(self):
        """Retourne (sign, offset) ou None si pas calibré.

        sign ∈ {-1, +1}, offset en degrés ]-180, +180].
        """
        if not self.config.has_section('Calibration'):
            return None
        try:
            sign = self.config.getint('Calibration', 'yaw_sign')
            offset = self.config.getfloat('Calibration', 'yaw_offset')
            if sign not in (-1, 1):
                return None
            return (sign, offset)
        except Exception:
            return None

    def set_yaw_calibration(self, sign, offset):
        """Persiste la calibration yaw."""
        if not self.config.has_section('Calibration'):
            self.config.add_section('Calibration')
        self.config.set('Calibration', 'yaw_sign', str(int(sign)))
        self.config.set('Calibration', 'yaw_offset', f"{offset:.3f}")
        self.save()

    def clear_yaw_calibration(self):
        """Supprime la calibration yaw pour forcer une recalibration."""
        if self.config.has_section('Calibration'):
            self.config.remove_option('Calibration', 'yaw_sign')
            self.config.remove_option('Calibration', 'yaw_offset')
            self.save()

    def get(self, section, option, fallback=None):
        """Méthode générique pour récupérer une valeur"""
        return self.config.get(section, option, fallback=fallback)
    
    def set(self, section, option, value):
        """Méthode générique pour définir une valeur"""
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, option, str(value))
