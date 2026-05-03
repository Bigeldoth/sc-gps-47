import pytesseract
import re
import cv2

class OCRProcessor:
    def __init__(self, tesseract_path=None):
        if tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_path
            
    def extract_data(self, image):
        """Extrait les coordonnées et le lieu de l'image traitée"""
        # Utilisation de config pour optimiser la reconnaissance des chiffres et symboles
        custom_config = r'--oem 3 --psm 6'
        text = pytesseract.image_to_string(image, config=custom_config)
        
        return self.parse_text(text)

    def parse_text(self, text):
        """Analyse le texte brut pour trouver X, Y, Z et le lieu"""
        data = {
            "location": "Unknown",
            "x": None,
            "y": None,
            "z": None
        }
        
        # Nettoyage du texte
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        
        for line in lines:
            # Recherche des coordonnées (format X: 123.4 Y: 567.8 Z: 910.1)
            coord_match = re.search(r'X:\s*(-?\d+\.?\d*).*Y:\s*(-?\d+\.?\d*).*Z:\s*(-?\d+\.?\d*)', line, re.IGNORECASE)
            if coord_match:
                data["x"] = float(coord_match.group(1))
                data["y"] = float(coord_match.group(2))
                data["z"] = float(coord_match.group(3))
            elif len(line) > 3 and not any(c in line for c in [':', '=']):
                # On assume que la ligne sans ':' est le nom du lieu
                data["location"] = line
                
        return data

if __name__ == "__main__":
    # Test rapide si une image existe
    processor = OCRProcessor()
    img = cv2.imread("test_capture.png")
    if img is not None:
        result = processor.extract_data(img)
        print(f"Résultat OCR : {result}")
    else:
        print("Image test_capture.png non trouvée.")
