import math
import json

import os
import sys


def format_distance(distance_km):
    """Formate une distance (en km) pour l'affichage.

    Retourne une chaîne en mètres sous 1 km (sans décimale), en kilomètres
    au-delà (deux décimales). ``None`` devient ``"---"``.
    """
    if distance_km is None:
        return "---"
    if distance_km < 1.0:
        return f"{distance_km * 1000:.0f} m"
    return f"{distance_km:.2f} km"


class NavigationEngine:
    def __init__(self, poi_file=None):
        if getattr(sys, 'frozen', False):
            self.base_dir = os.path.dirname(sys.executable)
        else:
            self.base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            
        if poi_file is None:
            poi_file = os.path.join(self.base_dir, "data", "poi.json")
            
        self.poi_file = poi_file
        self.user_poi_file = os.path.join(self.base_dir, "data", "user_poi.json")
        
        # Créer le dossier data s'il n'existe pas
        os.makedirs(os.path.dirname(self.user_poi_file), exist_ok=True)
        
        self.poi_data = self.load_poi(self.poi_file)
        self.user_poi = self.load_user_poi()
        self.target = None

    def load_user_poi(self):
        """Charge les points enregistrés par l'utilisateur"""
        if os.path.exists(self.user_poi_file):
            try:
                with open(self.user_poi_file, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []

    def save_user_poi(self):
        """Sauvegarde les points utilisateur"""
        try:
            with open(self.user_poi_file, 'w') as f:
                json.dump(self.user_poi, f, indent=4)
            return True
        except:
            return False

    def add_user_point(self, name, x, y, z, location="Unknown"):
        """Ajoute un nouveau point personnalisé"""
        point = {
            "name": name,
            "x": x,
            "y": y,
            "z": z,
            "location": location
        }
        self.user_poi.append(point)
        self.save_user_poi()
        return point

    def export_points(self, export_path):
        """Exporte les points utilisateur vers un fichier JSON"""
        try:
            with open(export_path, 'w') as f:
                json.dump(self.user_poi, f, indent=4)
            return True
        except:
            return False

    def import_points(self, import_path):
        """Importe des points depuis un fichier JSON"""
        try:
            with open(import_path, 'r') as f:
                new_points = json.load(f)
                if isinstance(new_points, list):
                    self.user_poi.extend(new_points)
                    self.save_user_poi()
                    return True
            return False
        except:
            return False

    def load_poi(self, file_path):
        try:
            if not os.path.exists(file_path):
                print(f"Fichier POI non trouvé: {file_path}")
                return []
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Erreur chargement POI : {e}")
            return []

    def set_target(self, x, y, z, name="Destination"):
        self.target = {"x": x, "y": y, "z": z, "name": name}

    def calculate_distance(self, current_pos):
        """Distance euclidienne entre la position courante et la cible.

        Les coordonnées (OCR + POI stockés) sont en kilomètres ; cette
        fonction retourne donc une distance en **kilomètres**, ou ``None``
        si la cible ou la position courante ne sont pas définies.
        """
        if not self.target or current_pos.get("x") is None:
            return None

        dx = self.target["x"] - current_pos["x"]
        dy = self.target["y"] - current_pos["y"]
        dz = self.target["z"] - current_pos["z"]

        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def calculate_bearing(self, current_pos):
        """Calcul simple du vecteur de direction (Pitch/Yaw)"""
        if not self.target or current_pos["x"] is None:
            return None
            
        dx = self.target["x"] - current_pos["x"]
        dy = self.target["y"] - current_pos["y"]
        dz = self.target["z"] - current_pos["z"]
        
        # En navigation spatiale SC, on utilise souvent l'alignement visuel
        # Ce moteur retournera les deltas pour aider l'overlay à placer un curseur
        return {"dx": dx, "dy": dy, "dz": dz}

    def get_all_poi_for_location(self, location_name):
        """Retourne la liste des POI (système + utilisateur) pour une planète donnée"""
        pois = []
        # Points du système
        for system in self.poi_data:
            for body in system["bodies"]:
                if body["name"].lower() in location_name.lower():
                    pois.extend(body["poi"])
        
        # Points utilisateur
        for upoi in self.user_poi:
            if upoi.get("location", "").lower() in location_name.lower() or location_name == "All":
                pois.append(upoi)
                
        return pois
