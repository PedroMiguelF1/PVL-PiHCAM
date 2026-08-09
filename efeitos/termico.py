"""Térmico — falsa cor sobre o brilho da cena. Combina bem com a NoIR + LED IR."""

import cv2
import numpy as np

from .base import efeito
from .utils import lut_ajuste, para_cinza

# Alternativas de paleta: COLORMAP_JET, COLORMAP_TURBO, COLORMAP_HOT, COLORMAP_INFERNO
PALETA = cv2.COLORMAP_INFERNO
CONTRASTE = 1.3


def _monta_lut():
    """Junta contraste + paleta numa única tabela de 256 cores (4 canais)."""
    ajuste = lut_ajuste(contraste=CONTRASTE)
    cores = cv2.applyColorMap(ajuste.reshape(256, 1), PALETA)      # (256, 1, 3) BGR
    alfa = np.full((256, 1, 1), 255, np.uint8)
    return np.dstack([cores, alfa])                                # (256, 1, 4)


_lut = _monta_lut()


@efeito("termico", "Térmico", tipo="cor")
def termico(arr):
    cinza = para_cinza(arr)
    arr[:] = cv2.LUT(cv2.cvtColor(cinza, cv2.COLOR_GRAY2BGRA), _lut)
