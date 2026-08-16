"""Sépia e preto e branco — os dois saem do mesmo cinza, só muda o peso dos canais."""

from .base import efeito
from .utils import aplicar_cinza, aplicar_lut, lut_ajuste, para_cinza

_lut_pb = lut_ajuste(contraste=1.15)
_lut_sepia = lut_ajuste(contraste=1.05, gama=0.95)


@efeito("sepia", "Sépia", tipo="cor")
def sepia(arr):
    # mais vermelho, menos azul: o tom amarelado de foto antiga
    aplicar_cinza(arr, para_cinza(arr), azul=0.68, verde=0.88, vermelho=1.0)
    aplicar_lut(arr, _lut_sepia)
