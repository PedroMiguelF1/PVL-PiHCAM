"""VHS — sangramento de croma, scanlines e OSD igual ao de um videocassete."""

import time

import cv2
import numpy as np

from .base import efeito, ao_evento, ESTADO

FITA = "TAPE 01"
MODO = "PLAY"                 # PLAY, REC, PAUSE, STOP...
VELOCIDADE = "SP"             # SP, LP, EP
FONTE = cv2.FONT_HERSHEY_DUPLEX
COR = (235, 235, 235, 255)    # branco levemente sujo
MARGEM = 12
ESCURO = 0.85                 # brilho das scanlines
SHIFT = 3                     # sangramento de croma, em pixels
CONTORNO = 2       # espessura da borda preta, em pixels

MESES = ("JAN", "FEV", "MAR", "ABR", "MAI", "JUN",
         "JUL", "AGO", "SET", "OUT", "NOV", "DEZ")

_offset = 0
_t0 = time.monotonic()
_evento_atual = None


def _escrever(arr, texto, x, y, escala, direita=False):
    """Desenha o texto com contorno; x vira a borda direita se direita=True."""
    if direita:
        (largura, _), _ = cv2.getTextSize(texto, FONTE, escala, 2)
        x -= largura
    cv2.putText(arr, texto, (x, y), FONTE, escala, (0, 0, 0, 255), 3, cv2.LINE_AA)
    cv2.putText(arr, texto, (x, y), FONTE, escala, COR, 1, cv2.LINE_AA)


def _play(arr, x, y, tam):
    """Triângulo de play — o ▶ não existe nas fontes do OpenCV."""
    tri = np.array([[x, y - tam], [x, y], [x + int(tam * 0.9), y - tam // 2]], np.int32)

    cv2.fillPoly(arr, [tri], (0, 0, 0, 255), cv2.LINE_AA)
    cv2.polylines(arr, [tri], True, (0, 0, 0, 255), CONTORNO * 2, cv2.LINE_AA)
    cv2.fillPoly(arr, [tri], COR, cv2.LINE_AA)


@efeito("vhs", "VHS", tipo="estrutura")
def vhs(arr):
    global _offset
    h, w = arr.shape[:2]
    escala = w / 900.0
    alt = int(22 * escala) + 4          # altura de uma linha de texto

    agora = time.localtime()


    # --- OSD, desenhado ANTES da degradação para sofrer o mesmo sangramento ---
    topo = MARGEM + alt
    _play(arr, MARGEM, topo, alt)
    _escrever(arr, MODO, MARGEM + int(alt * 1.4), topo, escala)
    _escrever(arr, VELOCIDADE, w - MARGEM, topo, escala, direita=True)

    base = h - MARGEM
    _escrever(arr, time.strftime("%H:%M:%S", agora), MARGEM, base, escala)
    _escrever(arr, f"{agora.tm_mday:02d} {MESES[agora.tm_mon - 1]} {agora.tm_year}",
              MARGEM, base - alt, escala)

    _escrever(arr, FITA, w - MARGEM, base, escala, direita=True)

    if _evento_atual == "gravar":
        decorrido = int(time.monotonic() - _t0)
        _escrever(arr, f"{decorrido // 3600:02d}:{decorrido // 60 % 60:02d}:{decorrido % 60:02d}",
                      w - MARGEM, base - alt, escala, direita=True)
    else:
        _escrever(arr, "00:00:00",
                      w - MARGEM, base - alt, escala, direita=True)
    
    # --- degradação da fita ---
    arr[:, SHIFT:, 2] = arr[:, :-SHIFT, 2]          # croma vermelho vazando

    _offset = (_offset + 2) % 4
    linhas = arr[_offset::4, :, :3]
    linhas[:] = (linhas * ESCURO).astype(np.uint8)

@ao_evento
def _reagir(nome, **_):
    global _t0, _evento_atual
    if nome in ("gravar"):
        _t0 = time.monotonic()
        _evento_atual = "gravar"

    if nome in ("parar"):
        _t0 = "00:00:00"
        _evento_atual = None