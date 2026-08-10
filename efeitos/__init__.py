import importlib, pkgutil
from .base import EFEITOS, definir, atual, post_callback   # noqa: F401

for _, nome, _ in pkgutil.iter_modules(__path__):
    if nome != "base":
        importlib.import_module(f"{__name__}.{nome}")