"""Renomme tous les glyphes pour éviter les conflits lors de la copie vers templates.

Problem : si tu collectionnes plusieurs sessions, les fichiers ont les mêmes noms
  (0000_r0.png, 0001_r0.png, ...) et se chevauchent lors de la copie vers
  data/templates/.

Solution : préfixer chaque fichier par le timestamp du dossier parent.

Avant :
  data/glyphs/20260510_143025_456/0000_r0.png
  data/glyphs/20260510_143025_456/0001_r0.png
  data/glyphs/20260510_150630_789/0000_r0.png  ← conflit !

Après :
  data/glyphs/20260510_143025_456/20260510_143025_456_0000_r0.png
  data/glyphs/20260510_143025_456/20260510_143025_456_0001_r0.png
  data/glyphs/20260510_150630_789/20260510_150630_789_0000_r0.png  ✅ unique !
"""
import os
import sys
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def rename_glyphs_in_session(session_dir):
    """Renomme tous les PNG d'une session avec le timestamp en préfixe.

    Args:
        session_dir : chemin du dossier YYYYMMDD_HHMMSS_mmm

    Returns:
        (nombre de fichiers renommés, liste d'erreurs)
    """
    if not os.path.isdir(session_dir):
        logger.error(f"Dossier non trouvé : {session_dir}")
        return 0, [session_dir]

    # Extraire le timestamp du nom du dossier
    session_name = os.path.basename(session_dir)
    if not session_name or len(session_name) < 17:
        logger.error(f"Nom de dossier invalide : {session_name}")
        return 0, [session_name]

    # Valider que c'est au format YYYYMMDD_HHMMSS(_mmm)
    parts = session_name.split('_')
    if len(parts) < 2:
        logger.warning(f"Format inattendu (ignorer): {session_name}")
        return 0, []

    renamed_count = 0
    errors = []

    # Parcourir tous les PNG
    for filename in os.listdir(session_dir):
        if not filename.lower().endswith('.png'):
            continue

        # Vérifier que le fichier n'a pas déjà le préfixe timestamp
        if filename.startswith(session_name):
            logger.debug(f"Déjà renommé : {filename}")
            continue

        old_path = os.path.join(session_dir, filename)
        new_filename = f"{session_name}_{filename}"
        new_path = os.path.join(session_dir, new_filename)

        try:
            os.rename(old_path, new_path)
            logger.debug(f"[OK] {filename} -> {new_filename}")
            renamed_count += 1
        except OSError as e:
            logger.error(f"[ERR] Erreur renommage {filename} : {e}")
            errors.append(filename)

    return renamed_count, errors


def rename_all_glyphs(base_dir='data/glyphs'):
    """Renomme les glyphes dans TOUS les dossiers de session.

    Args:
        base_dir : répertoire parent (data/glyphs)

    Returns:
        dict avec stats
    """
    if not os.path.isdir(base_dir):
        logger.error(f"Répertoire {base_dir} non trouvé")
        return None

    # Lister tous les dossiers de session
    session_dirs = [
        os.path.join(base_dir, d)
        for d in os.listdir(base_dir)
        if os.path.isdir(os.path.join(base_dir, d))
    ]

    if not session_dirs:
        logger.warning(f"Aucun dossier de session dans {base_dir}")
        return {'total_sessions': 0, 'total_renamed': 0, 'errors': []}

    logger.info(f"Trouvé {len(session_dirs)} session(s) de collecte")

    total_renamed = 0
    all_errors = []

    for session_dir in sorted(session_dirs):
        session_name = os.path.basename(session_dir)
        logger.info(f"\n--- Session : {session_name} ---")

        count, errors = rename_glyphs_in_session(session_dir)
        total_renamed += count

        if errors:
            all_errors.extend([(session_name, e) for e in errors])

        logger.info(f"Renommés : {count}")

    return {
        'total_sessions': len(session_dirs),
        'total_renamed': total_renamed,
        'errors': all_errors,
    }


def print_report(stats):
    """Affiche un rapport des renommages.

    Args:
        stats : dict retourné par rename_all_glyphs()
    """
    print("\n" + "=" * 60)
    print("RAPPORT RENOMMAGE GLYPHES")
    print("=" * 60)
    print(f"Nombre de sessions : {stats['total_sessions']}")
    print(f"Total renommés : {stats['total_renamed']}")

    if stats['errors']:
        print(f"\nErreurs : {len(stats['errors'])}")
        for session, filename in stats['errors'][:10]:
            print(f"  [{session}] {filename}")
    else:
        print("\nAucune erreur [OK]")

    print("=" * 60)
    print("\nPrêt à copier les glyphes vers data/templates/ !")
    print("Les fichiers ont maintenant des noms uniques par session.")
    print("=" * 60 + "\n")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Renomme les glyphes collectés pour éviter les conflits"
    )
    parser.add_argument(
        "--glyphs-dir",
        type=str,
        default="data/glyphs",
        help="Répertoire parent (défaut: data/glyphs)"
    )
    parser.add_argument(
        "--single-session",
        type=str,
        default=None,
        help="Renommer une seule session (chemin complet). Sinon : tous."
    )

    args = parser.parse_args()

    if args.single_session:
        logger.info(f"Renommage session unique : {args.single_session}")
        count, errors = rename_glyphs_in_session(args.single_session)
        stats = {
            'total_sessions': 1,
            'total_renamed': count,
            'errors': errors,
        }
    else:
        logger.info(f"Renommage dans : {args.glyphs_dir}")
        stats = rename_all_glyphs(args.glyphs_dir)

    if stats is None:
        sys.exit(1)

    print_report(stats)

    if stats['errors']:
        sys.exit(1)


if __name__ == "__main__":
    main()
