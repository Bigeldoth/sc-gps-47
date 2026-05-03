import sys
from PyQt6.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QWidget
from PyQt6.QtCore import Qt, QTimer
from capture import ScreenCapture
from ocr import OCRProcessor
from navigation import NavigationEngine

class GPSOverlay(QMainWindow):
    def __init__(self):
        super().__init__()
        self.capture = ScreenCapture()
        self.ocr = OCRProcessor()
        self.nav = NavigationEngine()
        
        # Configuration de la fenêtre Overlay
        self.setWindowFlags(
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(50, 50, 300, 200)

        # UI
        self.central_widget = QWidget()
        self.layout = QVBoxLayout()
        
        self.pos_label = QLabel("Recherche de coordonnées...")
        self.pos_label.setStyleSheet("color: #00ff00; font-family: 'Consolas'; font-size: 14px; background-color: rgba(0, 0, 0, 100);")
        
        self.dist_label = QLabel("")
        self.dist_label.setStyleSheet("color: #00ffff; font-family: 'Consolas'; font-size: 18px; font-weight: bold;")
        
        self.layout.addWidget(self.pos_label)
        self.layout.addWidget(self.dist_label)
        self.central_widget.setLayout(self.layout)
        self.setCentralWidget(self.central_widget)

        # Timer pour la mise à jour (toutes les secondes)
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_gps)
        self.timer.start(1000)

    def update_gps(self):
        try:
            # 1. Capture & OCR
            screenshot = self.capture.capture()
            data = self.ocr.extract_data(screenshot)
            
            if data["x"] is not None:
                self.pos_label.setText(f"LOC: {data['location']}\nX: {data['x']:.1f}\nY: {data['y']:.1f}\nZ: {data['z']:.1f}")
                
                # 2. Navigation (Exemple vers New Babbage si on est sur MicroTech)
                if self.nav.target is None and "Micro" in data["location"]:
                    pois = self.nav.get_all_poi_for_location("MicroTech")
                    if pois:
                        self.nav.set_target(pois[0]["x"], pois[0]["y"], pois[0]["z"], pois[0]["name"])
                
                # 3. Calcul distance
                dist = self.nav.calculate_distance(data)
                if dist:
                    self.dist_label.setText(f"CIBLE: {self.nav.target['name']}\nDIST: {dist/1000:.2f} km")
            else:
                self.pos_label.setText("Scan UI en cours...")
                
        except Exception as e:
            self.pos_label.setText(f"Erreur: {str(e)}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    overlay = GPSOverlay()
    overlay.show()
    sys.exit(app.exec())
