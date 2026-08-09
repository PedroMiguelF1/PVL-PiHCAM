"""Azul noite — imagem fria e escurecida, imitando cena noturna de cinema."""

from .base import efeito
from .utils import aplicar_cinza, aplicar_lut, lut_ajuste, para_cinza

# gama > 1 escurece as sombras; contraste alto mantém os brilhos vivos
_lut = lut_ajuste(contraste=1.25, gama=1.35, brilho=-8)


@efeito("azul_noite", "Azul noite", tipo="cor")
def azul_noite(arr):
    aplicar_cinza(arr, para_cinza(arr), azul=1.0, verde=0.72, vermelho=0.55)
    aplicar_lut(arr, _lut)
