"""
Funções auxiliares compartilhadas pelos efeitos.

O array que chega nos efeitos vem do formato XRGB8888 do picamera2:
4 canais, na ordem B=0, G=1, R=2, X=3 (alfa, ignorado).
Todas as funções trabalham no próprio array (in-place) quando possível.
"""

import cv2
import numpy as np


def para_cinza(arr):
    """Retorna a versão em tons de cinza (1 canal, contígua)."""
    return cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)


def aplicar_cinza(arr, cinza, azul=1.0, verde=1.0, vermelho=1.0):
    """Escreve o cinza de volta nos 3 canais, com um peso por canal.

    Pesos iguais a 1.0 = preto e branco puro.
    Pesos diferentes = tonalização (sépia, azul noite, visão noturna...).
    """
    for canal, k in enumerate((azul, verde, vermelho)):
        arr[:, :, canal] = cinza if k == 1.0 else cv2.convertScaleAbs(cinza, alpha=k)


def lut_ajuste(contraste=1.0, gama=1.0, brilho=0):
    """Monta uma tabela de 256 posições com gama, contraste e brilho."""
    x = np.arange(256, dtype=np.float32)
    y = ((x / 255.0) ** gama) * 255.0
    y = (y - 128.0) * contraste + 128.0 + brilho
    return np.clip(y, 0, 255).astype(np.uint8)


def aplicar_lut(arr, lut):
    """Aplica a LUT nos canais de cor, preservando o canal alfa."""
    lut4 = np.stack(
        [lut, lut, lut, np.arange(256, dtype=np.uint8)], axis=-1
    ).reshape(256, 1, 4)
    arr[:] = cv2.LUT(arr, lut4)


def mascara_vinheta(h, w, forca=0.8, raio=1.0):
    """Máscara uint8 de 4 canais: 255 no centro, caindo até as bordas.

    Fica em 4 canais (com alfa fixo em 255) para o cv2.multiply poder rodar
    sobre o buffer inteiro de uma vez, sem fatiar nem converter para float.
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    d = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2) / max(raio, 0.01)
    m = np.clip(1.0 - forca * np.clip(d, 0.0, 1.0) ** 2, 0.0, 1.0)
    m8 = cv2.cvtColor((m * 255).astype(np.uint8), cv2.COLOR_GRAY2BGRA)
    m8[:, :, 3] = 255                 # não mexe no canal alfa
    return m8


def aplicar_mascara(arr, mascara):
    """Multiplica a imagem pela máscara de vinheta (in-place)."""
    cv2.multiply(arr, mascara, dst=arr, scale=1 / 255)
