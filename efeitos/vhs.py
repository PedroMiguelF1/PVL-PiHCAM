import numpy as np
from .base import efeito

_offset = 0

@efeito("vhs", "VHS")
def vhs(arr):
    global _offset
    _offset = (_offset + 2) % 4

    # deslocamento de croma: puxa o canal vermelho alguns pixels
    arr[:, 3:, 2] = arr[:, :-3, 2]

    # scanlines escurecendo linhas alternadas
    arr[_offset::4, :, :3] = (arr[_offset::4, :, :3] * 0.75).astype(np.uint8)