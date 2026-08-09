"""Vinheta — escurecimento radial nas bordas, cor preservada."""

from .base import efeito
from .utils import aplicar_mascara, mascara_vinheta

FORCA = 0.8      # 0.0 = sem efeito, 1.0 = bordas pretas
RAIO = 1.05      # > 1.0 empurra o escurecimento mais para fora

_mascara = None


@efeito("vinheta", "Vinheta", tipo="estrutura")
def vinheta(arr):
    global _mascara
    h, w = arr.shape[:2]
    if _mascara is None or _mascara.shape[:2] != (h, w):
        _mascara = mascara_vinheta(h, w, forca=FORCA, raio=RAIO)
    aplicar_mascara(arr, _mascara)
