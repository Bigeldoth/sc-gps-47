"""
Gestionnaire de hotkeys globaux pour SpaceDrive GPS.
Utilise la bibliothèque keyboard pour enregistrer des raccourcis globaux.
"""
import keyboard
import logging
from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


class HotkeyListener(QObject):
    """
    Gestionnaire de hotkeys globaux avec signaux Qt.
    Permet d'enregistrer des raccourcis clavier qui fonctionnent même
    quand l'application n'a pas le focus.
    """
    
    # Signaux émis lors de l'activation des hotkeys
    toggle_overlay_triggered = pyqtSignal()
    open_options_triggered = pyqtSignal()
    save_position_triggered = pyqtSignal()
    open_poi_manager_triggered = pyqtSignal()
    
    def __init__(self, config_manager):
        """
        Initialise le listener de hotkeys.
        
        Args:
            config_manager: Instance de ConfigManager pour lire les raccourcis
        """
        super().__init__()
        self.config_manager = config_manager
        self.registered_hotkeys = {}
        self._setup_hotkeys()
    
    def _setup_hotkeys(self):
        """Configure les hotkeys depuis la configuration"""
        try:
            # Récupérer tous les hotkeys depuis la config
            hotkeys = self.config_manager.get_all_hotkeys()
            
            # Enregistrer chaque hotkey
            for action, hotkey in hotkeys.items():
                self._register_hotkey(action, hotkey)
            
            logger.info(f"Hotkeys configurés : {list(hotkeys.keys())}")
        except Exception as e:
            logger.error(f"Erreur lors de la configuration des hotkeys : {e}")
    
    def _register_hotkey(self, action, hotkey):
        """
        Enregistre un hotkey pour une action donnée.
        
        Args:
            action: Nom de l'action (toggle_overlay, open_options, etc.)
            hotkey: Raccourci clavier (ex: 'shift+f1')
        """
        try:
            # Mapper l'action au signal correspondant
            signal_map = {
                'toggle_overlay': self.toggle_overlay_triggered,
                'open_options': self.open_options_triggered,
                'save_position': self.save_position_triggered,
                'open_poi_manager': self.open_poi_manager_triggered
            }
            
            if action in signal_map:
                # Enregistrer le hotkey avec keyboard
                keyboard.add_hotkey(hotkey, lambda s=signal_map[action]: s.emit())
                self.registered_hotkeys[action] = hotkey
                logger.debug(f"Hotkey enregistré : {action} -> {hotkey}")
            else:
                logger.warning(f"Action inconnue : {action}")
        except Exception as e:
            logger.error(f"Erreur lors de l'enregistrement du hotkey {action} ({hotkey}) : {e}")
    
    def update_hotkey(self, action, new_hotkey):
        """
        Met à jour un hotkey existant.
        
        Args:
            action: Nom de l'action
            new_hotkey: Nouveau raccourci clavier
        """
        try:
            # Supprimer l'ancien hotkey s'il existe
            if action in self.registered_hotkeys:
                old_hotkey = self.registered_hotkeys[action]
                try:
                    keyboard.remove_hotkey(old_hotkey)
                    logger.debug(f"Ancien hotkey supprimé : {action} ({old_hotkey})")
                except:
                    pass
            
            # Enregistrer le nouveau hotkey
            self._register_hotkey(action, new_hotkey)
            
            # Mettre à jour la configuration
            self.config_manager.set_hotkey(action, new_hotkey)
            
            logger.info(f"Hotkey mis à jour : {action} -> {new_hotkey}")
            return True
        except Exception as e:
            logger.error(f"Erreur lors de la mise à jour du hotkey {action} : {e}")
            return False
    
    def reload_hotkeys(self):
        """Recharge tous les hotkeys depuis la configuration"""
        try:
            # Supprimer tous les hotkeys existants
            self.clear_all_hotkeys()
            
            # Recharger depuis la config
            self._setup_hotkeys()
            
            logger.info("Hotkeys rechargés depuis la configuration")
            return True
        except Exception as e:
            logger.error(f"Erreur lors du rechargement des hotkeys : {e}")
            return False
    
    def clear_all_hotkeys(self):
        """Supprime tous les hotkeys enregistrés"""
        try:
            for action, hotkey in self.registered_hotkeys.items():
                try:
                    keyboard.remove_hotkey(hotkey)
                except:
                    pass
            
            self.registered_hotkeys.clear()
            logger.info("Tous les hotkeys ont été supprimés")
        except Exception as e:
            logger.error(f"Erreur lors de la suppression des hotkeys : {e}")
    
    def get_registered_hotkeys(self):
        """Retourne un dictionnaire des hotkeys actuellement enregistrés"""
        return self.registered_hotkeys.copy()
    
    def cleanup(self):
        """Nettoie les ressources (à appeler avant de quitter l'application)"""
        self.clear_all_hotkeys()
        logger.info("HotkeyListener nettoyé")
