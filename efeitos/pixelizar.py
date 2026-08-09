"""Pixelização — reduz a imagem e amplia de volta sem interpolação."""

import cv2

from .base import efeito

BLOCO = 12          # tamanho do "pixelão" em pixels da imagem original


@efeito("pixelizar", "Pixelizar", tipo="estrutura")
def pixelizar(arr):
    h, w = arr.shape[:2]
    pequeno = cv2.resize(
        arr, (max(w // BLOCO, 1), max(h // BLOCO, 1)), interpolation=cv2.INTER_AREA
    )
    arr[:] = cv2.resize(pequeno, (w, h), interpolation=cv2.INTER_NEAREST)
