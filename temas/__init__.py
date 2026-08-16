"""
Temas visuais do PVL-PiHCAM.

Cada arquivo desta pasta é um tema: só precisa ter NOME (rótulo que
aparece na tela) e CORES (dicionário chave -> cor). Solte um .py novo
aqui e ele entra na lista sozinho.

Uso nos outros arquivos:

    from temas import C, qss

    btn.setStyleSheet(qss("QPushButton{background:$PrincipalMedio;"
                          "color:$Font;}"))
    caneta = QColor(C["Destaque"])          # fora do QSS, usa o dict

No QSS use $Chave e NÃO {chave}: o QSS já é cheio de { }, então
str.format() quebraria em todo seletor. O Template ignora as chaves.
"""

import importlib
import json
import os
import pkgutil
from string import Template

PADRAO = "default"
CONFIG_PATH = os.path.expanduser("~/.handycam_config.json")

TEMAS = {}      # id do arquivo -> {"label": ..., "cores": {...}}
C = {}          # cores do tema atual — sempre o MESMO dicionário
_atual = None

for _, _nome, _ in pkgutil.iter_modules(__path__):
    try:
        _mod = importlib.import_module(f"{__name__}.{_nome}")
        TEMAS[_nome] = {"label": getattr(_mod, "NOME", _nome),
                        "cores": _mod.CORES}
    except Exception as e:                      # tema quebrado não derruba o app
        print(f"[temas] ignorando {_nome}: {e}")


def definir(nome):
    """Troca o tema atual. Nome inválido cai no padrão."""
    global _atual
    _atual = nome if nome in TEMAS else PADRAO
    C.clear()
    C.update(TEMAS[PADRAO]["cores"])        # base: garante que nada falte
    C.update(TEMAS[_atual]["cores"])        # sobrescreve com o escolhido
    return _atual


def atual():
    return _atual


def qss(texto):
    """Troca $Destaque, $Font... pelas cores do tema atual."""
    return Template(texto).safe_substitute(C)


def rgb(chave):
    """Cor do tema como tupla (r, g, b) 0-255.

    O QGlPicamera2 pinta a tarja em volta do preview por OpenGL
    (glClearColor), não por stylesheet, e quer a cor nesse formato.
    """
    h = C[chave].lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _tema_salvo():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("tema", PADRAO)
    except (OSError, json.JSONDecodeError, AttributeError):
        return PADRAO


# Carrega já no import: o handycam.py monta BTN_ON/BTN_OFF na hora em que
# é importado, então o tema precisa estar pronto antes disso.
definir(_tema_salvo())