"""
Registro e despacho dos efeitos.

Dois espaços independentes:
  - o efeito escolhido na tela Configurações  -> definir(nome)
  - a visão noturna, ligada pelo botão dia/noite -> definir_noite(True/False)

Os dois são combinados numa cadeia, remontada só quando algo muda; o
post_callback apenas percorre a tupla pronta a cada frame.
"""

from picamera2 import MappedArray

EFEITOS = {}
EFEITO_NOITE = "noite"

_base = None          # nome do efeito da tela
_noite = False
_cadeia = ()          # tupla de funções na ordem de aplicação


def efeito(nome, label=None, tipo="estrutura"):
    """tipo='cor' para efeitos que reescrevem a cor (sépia, térmico, noite...);
    'estrutura' para os que só alteram a imagem existente (vinheta, VHS...)."""
    def deco(func):
        EFEITOS[nome] = {"label": label or nome, "func": func, "tipo": tipo}
        return func
    return deco


def _montar():
    global _cadeia
    funcs = []

    # a noite entra primeiro: tonaliza, e o resto se aplica por cima
    if _noite and EFEITO_NOITE in EFEITOS:
        funcs.append(EFEITOS[EFEITO_NOITE]["func"])

    if _base in EFEITOS:
        # dois efeitos de cor se anulam; com a noite ligada, ela tem prioridade
        if not (_noite and EFEITOS[_base]["tipo"] == "cor"):
            funcs.append(EFEITOS[_base]["func"])

    _cadeia = tuple(funcs)


def definir(nome):
    """Efeito da tela Configurações. nome=None desliga."""
    global _base
    _base = nome if nome in EFEITOS else None
    _montar()


def definir_noite(ligado):
    """Visão noturna, independente do efeito escolhido."""
    global _noite
    _noite = bool(ligado)
    _montar()


def nome_atual():
    return _base


def noite_ativa():
    return _noite


def ativos():
    """Nomes dos efeitos realmente em uso, na ordem — útil para depurar."""
    nomes = {info["func"]: n for n, info in EFEITOS.items()}
    return [nomes[f] for f in _cadeia]


def post_callback(request):
    cadeia = _cadeia                    # cópia local: troca em outra thread é segura
    if not cadeia:
        return
    with MappedArray(request, "main") as m:
        arr = m.array                   # XRGB8888 -> 4 canais, alteração in-place
        for func in cadeia:
            func(arr)