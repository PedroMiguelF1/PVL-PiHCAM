import importlib
import pkgutil
from string import Template

PADRAO = "default"

TEMAS = {}     # id -> {"label": ..., "cores": {...}}
C = {}         # cores do tema atual — sempre o MESMO dict
_atual = None

for _, _nome, _ in pkgutil.iter_modules(__path__):
    _mod = importlib.import_module(f"{__name__}.{_nome}")
    TEMAS[_nome] = {"label": getattr(_mod, "NOME", _nome), "cores": _mod.CORES}


def definir(nome):
    global _atual
    _atual = nome if nome in TEMAS else PADRAO
    C.clear()
    C.update(TEMAS[PADRAO]["cores"])    # base: garante que nada falte
    C.update(TEMAS[_atual]["cores"])    # sobrescreve com o tema escolhido


def atual():
    return _atual


def qss(texto):
    """Troca $primario, $fundo... pelas cores do tema atual."""
    return Template(texto).safe_substitute(C)


definir(PADRAO)