from picamera2 import MappedArray # type: ignore

EFEITOS = {}          # nome -> {"label":…, "func":…}
_atual = None

def efeito(nome, label=None):
    def deco(func):
        EFEITOS[nome] = {"label": label or nome, "func": func}
        return func
    return deco

def definir(nome):
    """nome=None desliga o efeito."""
    global _atual
    _atual = EFEITOS[nome]["func"] if nome else None

def atual():
    return _atual

def post_callback(request):
    f = _atual                       # cópia local: troca em outra thread é segura
    if f is None:
        return
    with MappedArray(request, "main") as m:
        f(m.array)                   # XRGB8888 -> 4 canais, alteração in-place