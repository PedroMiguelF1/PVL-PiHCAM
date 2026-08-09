"""Rastro — mistura o frame atual com o anterior, deixando estela no movimento."""

import cv2

from .base import efeito

PESO_ATUAL = 0.55       # menor = rastro mais longo (0.4 a 0.8 é a faixa útil)

_anterior = None


@efeito("rastro", "Rastro", tipo="estrutura")
def rastro(arr):
    global _anterior

    if _anterior is None or _anterior.shape != arr.shape:
        _anterior = arr.copy()
        return

    cv2.addWeighted(arr, PESO_ATUAL, _anterior, 1.0 - PESO_ATUAL, 0.0, dst=arr)
    _anterior = arr.copy()
