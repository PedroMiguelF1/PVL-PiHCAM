import cv2, numpy as np
from .base import efeito

@efeito("preto_branco", "Preto e branco", tipo="cor")
def tint_pb(arr):
    gray = cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
    arr[:, :, 0] = gray   # azul
    arr[:, :, 1] = gray   # verde
    arr[:, :, 2] = gray   # vermelho
