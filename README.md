# PVL-PiHCAM — Handycam

Filmadora de mão baseada em Raspberry Pi, com tela sensível ao toque, visão
noturna por infravermelho e efeitos de imagem aplicados ao vivo.

O app abre direto no boot, sem desktop visível

---

## Recursos

- Preview em tempo real acelerado por GPU (`QGlPicamera2`)
- Gravação em `.mp4` (H.264) e fotos em `.jpg`
- Modo dia/noite com LED infravermelho e ganho aumentado
- **Efeitos de imagem ao vivo**, combináveis, aplicados também na gravação
- Tela de arquivos com miniaturas, player de vídeo embutido e exclusão
- Resolução e FPS configuráveis pela própria tela, com persistência
- Interface desenhada para toque: tema escuro, botões grandes, rolagem por arraste

---

## Hardware

| Item | Modelo usado |
|---|---|
| Placa | Raspberry Pi 4B (4 GB) |
| Câmera | OV5647 IR-Cut |
| Iluminação | LED infravermelho no GPIO 17 |
| Iluminação | LED infravermelho no GPIO 17 |
| Tela | Display touch LCD DSI de 4,3 polegadas |
| Sistema | Raspberry Pi OS Trixie 64-bit (aarch64) |

Deve funcionar em outros modelos de Pi e em outras câmeras suportadas pelo
`libcamera`, mas só foi testado nessa combinação.

---

## Instalação

Dependências, todas pelo `apt` — **não use o pip para o `picamera2`, o PyQt5 ou
o OpenCV**. O `picamera2` depende do `libcamera`, que é compilado junto do
sistema e não existe no PyPI; a versão do pip não enxerga a câmera.

```bash
sudo apt update
sudo apt install -y \
    python3-picamera2 python3-pyqt5 python3-opencv \
    python3-numpy python3-pil python3-gpiozero \
    ffmpeg mpv git
```

Clone e execute:

```bash
git clone https://github.com/PedroMiguelF1/PVL-PiHCAM.git
cd PVL-PiHCAM
./start.sh
```

O `handyload.py` cria a `QApplication`, mostra a splash e chama o `main()` do
`handycam.py`.

`start.sh` inicia o `handyload.py`.
---

## Iniciar junto com o sistema

Criar serviço em `/etc/systemd/system/`:

- **`handycam.service`** — inicia o app. O `ExecStartPre` chama
  `/usr/local/bin/espera-display.sh`, que aguarda o display ficar pronto.

```bash
sudo systemctl enable --now handycam.service
journalctl -u handycam -f          # acompanhar os logs
```

O app força `QT_QPA_PLATFORM=xcb` (X11 via XWayland); no Wayland puro o preview
por GPU não sobe.

---

## Estrutura

```
handyload.py          splash + QApplication, ponto de entrada
handycam.py           janelas, câmera, gravação, navegação
efeitos/
├── __init__.py       importa e registra todos os efeitos automaticamente
├── base.py           registro, cadeia de efeitos, eventos, post_callback
├── utils.py          cinza, LUTs, tonalização, vinheta
└── <um arquivo por efeito>
```

Telas, em um `QStackedWidget`: `0` principal, `1` configurações, `2` arquivos.

Configurações ficam em `~/.handycam_config.json`. Trocar resolução ou FPS exige
reiniciar o processo (feito com `os.execv`); trocar o efeito é imediato.

---

## Efeitos

Cada efeito é um arquivo em `efeitos/` que se registra sozinho — basta soltar
o `.py` na pasta e ele aparece na tela de configurações.

| Efeito | Tipo | Descrição |
|---|---|---|
| Noite | cor | Tom verde de intensificador de imagem; acompanha o LED infravermelho |
| Preto e branco | cor | Escala de cinza com contraste realçado |
| Sépia | cor | Tom amarelado de fotografia antiga |
| Azul noite | cor | Imagem fria e escurecida, imitando cena noturna de cinema |
| Térmico | cor | Falsa cor sobre o brilho da cena, no estilo de câmera térmica |
| Câmera de segurança | cor | P&B contrastado, vinheta e data/hora sobreposta |
| VHS | estrutura | Sangramento de cor, scanlines e o painel de um videocassete |
| Negativo | estrutura | Inverte a imagem, como um filme fotográfico |
| Vinheta | estrutura | Escurecimento radial nas bordas, preservando a cor |
| Pixelizar | estrutura | Reduz e amplia sem interpolação, deixando blocos visíveis |
| Detecção de bordas | estrutura | Contornos em verde sobre a imagem escurecida |
| Rastro | estrutura | Mistura o frame atual com o anterior, deixando estela no movimento |

**Tipos.** Efeitos de `cor` reconstroem a imagem a partir do cinza (sépia,
térmico, noite). Dois deles em sequência não somam: o segundo descarta
o trabalho do primeiro. Efeitos de `estrutura` alteram a imagem que já está lá
(vinheta, VHS, pixelizar) e combinam com qualquer coisa.

**Combinação.** A visão noturna ocupa um espaço próprio, independente do efeito
escolhido na tela. Com a noite ligada, ela é aplicada primeiro e o efeito da
tela vem por cima — a não ser que ele também seja de `cor`, caso em que é
ignorado para não desperdiçar processamento.

### Criando um efeito

```python
# efeitos/meu_efeito.py
import cv2

from .base import efeito

#   nome do arquivo | nome de exibição | Tipo
@efeito("meu_efeito", "Meu efeito", tipo="estrutura")
def meu_efeito(arr):
    cv2.bitwise_not(arr, dst=arr)
    arr[:, :, 3] = 255
```

Regras que valem para qualquer efeito:

- `arr` vem no formato XRGB8888: 4 canais, ordem **B, G, R, X**.
- Altere sempre **no próprio array**; o retorno é ignorado.
- A função roda na thread da câmera, uma vez por frame. Nada de laço por pixel:
  use operações vetorizadas ou funções do OpenCV.
- Fatias como `arr[:, :, :3]` não podem ir para funções do OpenCV — o passo
  entre pixels continua sendo 4 bytes e o OpenCV não representa isso. Trabalhe
  no buffer inteiro (`cv2.LUT`, `cv2.multiply`, `cv2.max`) ou em NumPy puro.
- Estado próprio (frame anterior, contador, máscara) pode ficar em variável de
  módulo; recalcule quando a resolução mudar.

### Eventos

O app avisa os efeitos sobre o que está acontecendo, sem que eles precisem
conhecer o `handycam.py`:

```python
from .base import ESTADO, ao_evento


@ao_evento
def _reagir(nome, **_):
    if nome == "gravar":
        ...
```

Eventos emitidos hoje: `gravar` e `parar`. O dicionário `ESTADO` guarda
`gravando` e `inicio_gravacao`. Uma exceção dentro de um ouvinte é registrada no
log e não interrompe os demais.

---

## Licença

Licença

Distribuído sob a Licença Apache 2.0.

Uso, modificação e uso comercial são permitidos. Em qualquer redistribuição, com ou sem alterações, é obrigatório manter o aviso de copyright e a cópia da licença, e indicar os arquivos que foram modificados.

Copyright 2026 VitoMiguelF, PedroMiguelF1