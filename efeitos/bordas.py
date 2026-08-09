"""Detecção de bordas — contornos em verde sobre a imagem escurecida."""

import cv2
import numpy as np

from .base import efeito
from .utils import para_cinza

LIMITE_BAIXO = 60
LIMITE_ALTO = 160
FUNDO = 0.25        # 0.0 = fundo preto, 1.0 = imagem original
ESPESSURA = 2       # 1 = linha fina; 2+ engrossa com dilate
COR = (40, 255, 40)  # B, G, R

_kernel = np.ones((ESPESSURA, ESPESSURA), np.uint8) if ESPESSURA > 1 else None


@efeito("bordas", "Detecção de bordas", tipo="estrutura")
def bordas(arr):
    linhas = cv2.Canny(para_cinza(arr), LIMITE_BAIXO, LIMITE_ALTO)
    if _kernel is not None:
        linhas = cv2.dilate(linhas, _kernel)

    fundo = cv2.convertScaleAbs(arr, alpha=FUNDO)
    canais = [cv2.convertScaleAbs(linhas, alpha=c / 255) for c in COR]
    canais.append(np.full_like(linhas, 255))          # alfa

    # max() em vez de máscara booleana: uma passada vetorizada, sem indexação
    cv2.max(fundo, cv2.merge(canais), dst=arr)
