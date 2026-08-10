import cv2, numpy as np
from .base import efeito

@efeito("noite", "Visão noturna")
def tint_green(arr):
    gray = cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
    arr[:, :, 0] = (gray * 0.60).astype(np.uint8)   # azul
    arr[:, :, 1] = gray                             # verde
    arr[:, :, 2] = (gray * 0.50).astype(np.uint8)   # vermelho