"""Câmera de segurança — P&B contrastado, vinheta e data/hora sobreposta."""

import time

import cv2

from .base import efeito
from .utils import aplicar_cinza, aplicar_lut, aplicar_mascara, lut_ajuste, mascara_vinheta, para_cinza

CONTRASTE = 1.35
GAMA = 0.9
FORCA_VINHETA = 0.55
TEXTO = "CAM 01"

_lut = lut_ajuste(contraste=CONTRASTE, gama=GAMA)
_mascara = None


@efeito("seguranca", "Câmera de segurança", tipo="estrutura")
def seguranca(arr):
    global _mascara
    h, w = arr.shape[:2]

    aplicar_cinza(arr, para_cinza(arr))
    aplicar_lut(arr, _lut)

    if _mascara is None or _mascara.shape[:2] != (h, w):
        _mascara = mascara_vinheta(h, w, forca=FORCA_VINHETA)
    aplicar_mascara(arr, _mascara)

    escala = w / 900.0
    carimbo = time.strftime("%d/%m/%Y  %H:%M:%S")

    for texto, pos in ((TEXTO, (12, int(28 * escala) + 6)),
                       (carimbo, (12, h - 14))):
        # contorno preto para o texto ficar legível sobre qualquer fundo
        cv2.putText(arr, texto, pos, cv2.FONT_HERSHEY_SIMPLEX, escala,
                    (0, 0, 0, 255), 3, cv2.LINE_AA)
        cv2.putText(arr, texto, pos, cv2.FONT_HERSHEY_SIMPLEX, escala,
                    (235, 235, 235, 255), 1, cv2.LINE_AA)
