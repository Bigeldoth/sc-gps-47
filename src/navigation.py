import math
import json

class NavigationEngine:
    def __init__(self, poi_file="data/poi.json"):
        self.poi_data = self.load_poi(poi_file)
        self.target = None

    def load_poi(self, file_path):
        try:
            with open(file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Erreur chargement POI : {e}")
            return []

    def set_target(self, x, y, z, name="Destination"):
        self.target = {"x": x, "y": y, "z": z, "name": name}

    def calculate_distance(self, current_pos):
        if not self.target or current_pos["x"] is None:
            return None
        
        dx = self.target["x"] - current_pos["x"]
        dy = self.target["y"] - current_pos["y"]
        dz = self.target["z"] - current_pos["z"]
        
        distance = math.sqrt(dx**2 + dy**2 + dz**2)
        return distance

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
        """Retourne la liste des POI pour une planète donnée"""
        pois = []
        for system in self.poi_data:
            for body in system["bodies"]:
                if body["name"].lower() in location_name.lower():
                    pois.extend(body["poi"])
        return pois
