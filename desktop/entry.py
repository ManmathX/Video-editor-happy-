"""Punto d'ingresso dell'app desktop impacchettata (vedi vedit-desktop.spec).

Lo stesso file fa da finestra e da riga di comando: senza argomenti apre
l'editor, con un comando (`mcp`, `doctor`, `render`...) e' la CLI `vedit`.
"""

import multiprocessing
import sys

from vedit.desktop import principale

if __name__ == "__main__":
    # una libreria che usa multiprocessing (YOLO, torch) rilancerebbe l'app intera
    multiprocessing.freeze_support()
    sys.exit(principale())
