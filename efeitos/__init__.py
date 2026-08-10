"""Pacote de efeitos: cada módulo se registra sozinho ao ser importado."""

import importlib
import pkgutil

from .base import (          # noqa: F401
    EFEITOS,
    EFEITO_NOITE,
    ativos,
    definir,
    definir_noite,
    efeito,
    ESTADO, 
    ao_evento, 
    evento,
    noite_ativa,
    nome_atual,
    post_callback,
)

# módulos que não registram efeitos
APOIO = {"base", "osd", "utils"}

for _, _nome, _ in pkgutil.iter_modules(__path__):
    if _nome not in APOIO and not _nome.startswith("_"):
        importlib.import_module(f"{__name__}.{_nome}")