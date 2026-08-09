"""Negativo — inverte a imagem, como um filme fotográfico."""

import cv2

from .base import efeito


@efeito("negativo", "Negativo", tipo="estrutura")
def negativo(arr):
    cv2.bitwise_not(arr, dst=arr)
    arr[:, :, 3] = 255          # o bitwise_not zera o alfa; devolve opaco
