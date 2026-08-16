"""
Fonte única do PVL-PiHCAM.

A fonte vive no próprio repositório (fontes/), não no apt: assim um Pi
recém-instalado, ou uma atualização pelo botão de Atualizar, já sobe com
a interface certa, sem depender de ninguém rodar `apt install`.

O QFontDatabase exige que a QApplication já exista, então carregar() tem
que ser chamado DEPOIS de criar a QApplication e ANTES de construir
qualquer widget — no handyload.py e no handycam.py isso é a primeira
linha após o QApplication(sys.argv).

Uso nos widgets:

    from fonte import fonte

    lbl.setFont(fonte(13))          # regular
    btn.setFont(fonte(15, True))    # negrito
"""

import os

from qt import QFont, QFontDatabase, FONT_BOLD

# Caminho a partir deste arquivo, e não do diretório atual: o app é
# iniciado por ./start.sh de ~/Documents e pelo systemd, que rodam com
# cwd diferente.
PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fontes")

PADRAO = "DejaVu Sans"      # usada se o .ttf sumir ou falhar ao carregar
FAMILIA = PADRAO
_carregada = False


def carregar():
    """Registra os .ttf de fontes/ e fixa a família do app."""
    global FAMILIA, _carregada
    if _carregada:
        return FAMILIA
    _carregada = True

    try:
        arquivos = sorted(f for f in os.listdir(PASTA)
                          if f.lower().endswith((".ttf", ".otf")))
    except OSError:
        arquivos = []

    familias = []
    for nome in arquivos:
        # Registra TODOS os arquivos da pasta (Regular, Bold, Italic...).
        # Sem o Bold de verdade, o Qt "engorda" o Regular na marra e o
        # resultado fica borrado na tela pequena.
        fid = QFontDatabase.addApplicationFont(os.path.join(PASTA, nome))
        if fid != -1:
            familias += QFontDatabase.applicationFontFamilies(fid)

    if familias:
        FAMILIA = familias[0]
    else:
        print(f"[fonte] nenhuma fonte carregada de {PASTA}, usando {PADRAO}")
    return FAMILIA


def fonte(tamanho, negrito=False):
    return QFont(FAMILIA, tamanho, FONT_BOLD if negrito else QFont.Normal)
