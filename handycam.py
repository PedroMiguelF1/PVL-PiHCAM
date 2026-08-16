#!/usr/bin/env python3
"""
Handycam NoIR — Raspberry Pi 4 + OV5647 NoIR
PySide6 + QGlPicamera2 (preview acelerado por GPU)

Teclas:  [N] dia/noite   [R] gravar/parar   [P] foto   [F] tela cheia
         [C] configurações   [Q] sair
Deps:    sudo apt install -y python3-picamera2 python3-pyqt6 python3-opencv
         (se usar PySide6: pip install PySide6 --break-system-packages,
          ou troque os imports para PyQt6 — a API é praticamente igual)

Changelog (merge com handycam_pc.py):
- Tela de Configurações (engrenagem / tecla [C]) para escolher
  resolução + FPS, persistidos em ~/.handycam_config.json.
  "Aplicar e reiniciar" reinicia o processo (os.execv) já com a nova
  config.
- Atalho [P] / botão FOTO continuam chamando take_photo().
- RESOLUCAO agora é só (largura, altura) — o rótulo (VGA/HD/FHD) fica
  em RESOLUCAO_LABEL. No handycam_pc.py original, RESOLUCAO era uma
  3-tupla e ia direto pro `create_video_configuration(main={"size":...})`;
  isso só não quebrava porque o mock_picamera2 é tolerante. No
  Picamera2 de verdade, "size" PRECISA ser (largura, altura), então
  separei os dois pra não estourar aqui.
- Nada de branch tipo "se for PC importa o mock" — os imports de
  picamera2 continuam fixos no topo, exatamente como no original.
- main() agora aceita app=None, splash=None (mesma assinatura do
  handycam_pc.py) para poder ser chamado pelo handyload.py sem
  duplicar a criação de QApplication/splash. Rodando `python
  handycam.py` direto continua funcionando igual, sem handyload.py.
"""

import sys
import time
import os
import re
import json
import subprocess
import socket
import shutil
import getpass
import hashlib

from gpiozero import DigitalOutputDevice

# Força XCB (X11 via XWayland) em vez de Wayland — ver explicação completa
# no handyload.py. Necessário aqui também porque, rodando `python
# handycam.py` direto (sem o loader), é este main() que cria a
# QApplication, e isso tem que ser definido antes disso acontecer.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import cv2
import numpy as np

from picamera2 import Picamera2, MappedArray # type: ignore
from picamera2.encoders import H264Encoder # type: ignore
from picamera2.outputs import FfmpegOutput # type: ignore
from picamera2.previews.q_gl_picamera2 import _get_qglpicamera2
from picamera2.previews.qt_compatibility import _QT_BINDING

QGlPicamera2 = _get_qglpicamera2(_QT_BINDING.PySide6)


# ----------------------------------------------------------------------
# IMPORTANTE: usar o MESMO binding Qt que o picamera2 usa internamente.
# Misturar PySide6 com PyQt gera "Must construct a QApplication before a
# QWidget". O import do QGlPicamera2 acima já carregou o binding certo —
# aqui só descobrimos qual foi.
# ----------------------------------------------------------------------

from qt import *

from handyupdate import AtualizadorWorker

import efeitos

import temas
from temas import C, qss

from fonte import fonte, carregar as carregar_fonte

# ----------------------------------------------------------------------
# Configuração
# ----------------------------------------------------------------------
CONFIG_PATH = os.path.expanduser("~/.handycam_config.json")

RESOLUCOES = [
    ("480x480 (VGA) 1:1", 480, 480),
    ("640x480 (VGA) 4:3", 640, 480),
    ("854x480 (VGA) 16:9", 854, 480),

    ("720x720 (HD) 1:1", 720, 720),
    ("1296x972 (HD) 4:3", 1296, 972),
    ("1280x720 (HD) 16:9", 1280, 720),

    ("1080x1080 (FHD) 1:1", 1080, 1080),
    ("1440x1080 (FHD) 4:3", 1440, 1080),
    ("1920x1080 (FHD) 16:9", 1920, 1080)
]

FPS_OPCOES = [24, 30, 60]

def _detectar_audio():
    """Retorna o primeiro device de captura ALSA disponível, ou None."""
    try:
        saida = subprocess.run(["arecord", "-l"], capture_output=True,
                               text=True, timeout=5).stdout
        for linha in saida.splitlines():
            if linha.startswith("card "):
                card = linha.split()[1].rstrip(":")
                return f"plughw:{card},0"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None

AUDIO_DEVICE = _detectar_audio()
AUDIO_BITRATE = 128000

# ^ ajuste essa lista pros modos que o OV5647 realmente entrega bem —
#   o ISP do picamera2 faz o scaling, então esses valores funcionam,
#   mas o FPS máximo real depende do modo de sensor escolhido.

# Define o GPIO 17 para ser o controle do IR
infra_red = DigitalOutputDevice(17)

def _label_resolucao(altura):
    return {480: "VGA", 720: "HD", 972: "HD", 1080: "FHD"}.get(altura, f"{altura}p")


def _carregar_config_inicial():
    padrao_res = [1280, 720]
    padrao_fps = 24
    padrao_efeito = None
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
                l, a = cfg.get("resolucao", padrao_res)
                fps = cfg.get("fps", padrao_fps)
                efeito = cfg.get("efeito", padrao_efeito)
                return l, a, fps, efeito
        except (json.JSONDecodeError, OSError, ValueError, TypeError, KeyError):
            pass
    return padrao_res[0], padrao_res[1], padrao_fps, padrao_efeito

_L, _A, FPS_DIA, EFEITO_INICIAL = _carregar_config_inicial()
RESOLUCAO = (_L, _A)                      # size real usado no configure()
RESOLUCAO_LABEL = _label_resolucao(_A)     # só pra exibição (VGA/HD/FHD)
FPS_NOITE = FPS_DIA          # menos fps = mais tempo de exposição = mais luz
GANHO_NOITE = 8.0
BITRATE = 2_000_000
TUNING = "ov5647_noir.json"
PASTA_SAIDA = "./Media"       # ex: "/media/usb" para gravar em pendrive
PASTA_VIDEOS = os.path.join(PASTA_SAIDA, "Videos")
PASTA_FOTOS = os.path.join(PASTA_SAIDA, "Fotos")
PASTA_THUMBS = os.path.join(PASTA_SAIDA, ".thumbs")  # cache das thumbnails
SUBPASTA_USB = os.path.join("Handycam", "Media")

BTN_ON = qss("QPushButton{background:$DestaqueBaixo;border:2px solid $Destaque;"
              "border-radius:10px;color:$Font;padding:6px 18px;}"
              "QPushButton:pressed{background:$Principal;}"
              "QPushButton:checked{background:$DestaqueMedio;border:2px solid $DestaqueAlto;color:$Font;}")
    
BTN_OFF = qss("QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
        "border-radius:10px;color:$Font;padding:6px 18px;}"
        "QPushButton:pressed{background:$Principal;}"
        "QPushButton:checked{background:$PrincipalAlto;border:2px solid $Font;color:$Font;}")

os.makedirs(PASTA_THUMBS, exist_ok=True)

# No Pi 4, gerar várias thumbnails de vídeo ao mesmo tempo (cada uma
# chamando ffmpeg) compete por CPU com a captura/preview da câmera, que
# já usa boa parte dos 4 núcleos. Limitando a 1 thread por vez, a
# geração fica sequencial: mais lenta pra "encher" a lista na primeira
# vez, mas não trava o preview nem a gravação em andamento.
QThreadPool.globalInstance().setMaxThreadCount(1)


# ----------------------------------------------------------------------
# Efeito de visão noturna (roda em cada frame, antes do preview/encoder)
# ----------------------------------------------------------------------
# def tint_green(request):
#     with MappedArray(request, "main") as m:
#         arr = m.array                       # XRGB8888 -> 4 canais
#         gray = cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
#         arr[:, :, 0] = (gray * 0.60).astype(np.uint8)    # azul
#         arr[:, :, 1] = gray                              # verde
#         arr[:, :, 2] = (gray * 0.50).astype(np.uint8)    # vermelho


# ----------------------------------------------------------------------
# Janela principal
# ----------------------------------------------------------------------
class Handycam(QWidget):
    def __init__(self, picam2):
        super().__init__()
        self.picam2 = picam2
        self.picam2.post_callback = efeitos.post_callback
        efeitos.definir(EFEITO_INICIAL)
        self.noite = False
        efeitos.definir_noite(self.noite)
        self.gravando = False
        self.encoder = None
        self.t0 = 0.0
        self.blink = False
        self.icone_foto = False   # flash do icone de camera sobre o preview
        self.telas = 0
        #self.efeito_anterior = None


        self.setWindowTitle("PVL-PiHCAM")
        self.setStyleSheet(qss("background:$PrincipalBaixo; color:$Font;"))

        self.preview = QGlPicamera2(picam2, width=RESOLUCAO[0],
                                    height=RESOLUCAO[1], keep_ar=True,
                                    bg_colour=temas.rgb("PreviewBG"))

        # O REC (ponto piscando + cronometro) nao fica mais na barra de
        # status: agora e desenhado por cima do preview em _atualiza_overlay().

        self.lbl_clock = QLabel("")
        self.lbl_clock.setFont(fonte(13))
        self.lbl_clock.setAlignment(ALIGN_LEFT)
        self.timer_relogio = QTimer(self)
        self.timer_relogio.timeout.connect(self._atualiza_relogio)
        self.timer_relogio.start(1000)          # milissegundos
        self._atualiza_relogio() 


        self.lbl_modo = QLabel("")
        self.lbl_modo.setFont(fonte(13))
        self.lbl_modo.setAlignment(ALIGN_RIGHT)
 
        self.lbl_info = QLabel(f"{RESOLUCAO[0]}x{RESOLUCAO[1]} {RESOLUCAO_LABEL} | FPS: {FPS_DIA if not self.noite else FPS_NOITE} ")
        self.lbl_info.setFont(fonte(13))
        self.lbl_info.setAlignment(ALIGN_RIGHT)
 
        self.btn_files = QPushButton("≡")
        self.btn_files.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
            "border-radius:10px;color:$Font;}"
            "QPushButton:pressed{background:$Principal; border:2px solid $Font;}"
        ))
        self.btn_files.setFixedSize(50, 50)
        self.btn_files.setFont(fonte(13, True))
        self.btn_files.setFocusPolicy(NO_FOCUS)

        
        self.btn_conf = QPushButton("⚙")
        self.btn_conf.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
            "border-radius:10px;color:$Font;}"
            "QPushButton:pressed{background:$Principal; border:2px solid $Font;}"
        ))
        self.btn_conf.setFixedSize(50, 50)
        self.btn_conf.setFont(fonte(15, True))
        self.btn_conf.setFocusPolicy(NO_FOCUS)

        status = QHBoxLayout()
        status.addWidget(self.lbl_clock)
        status.addStretch()
        status.addWidget(self.lbl_modo)
        status.addWidget(self.lbl_info)
        status.addWidget(self.btn_files)
        status.addWidget(self.btn_conf)

        self.btn_rec = QPushButton("● REC")

        self.btn_photo = QPushButton(" FOTO")
        self.btn_photo.setIcon(self._icone_camera_btn(C["Destaque"], 64))
        self.btn_photo.setIconSize(QSize(32, 24))

        self.btn_modo = QPushButton("☾ NOITE")
        self.btn_sair = QPushButton("SAIR")

        for b in (self.btn_modo, self.btn_sair, self.btn_rec, self.btn_photo):
            b.setMinimumHeight(64)
            b.setFont(fonte(15, True))
            b.setFocusPolicy(NO_FOCUS)

        self.btn_modo.setStyleSheet(qss(
            "QPushButton{background:$PositivoBaixo;border:2px solid $Positivo;"
            "border-radius:10px;color:$Positivo;padding:6px 18px;}"
            "QPushButton:pressed{background:$PositivoMedio;}"
        ))
        self.btn_rec.setStyleSheet(qss(
            "QPushButton{background:$NegativoBaixo;border:2px solid $Negativo;"
            "border-radius:10px;color:$Negativo;padding:6px 18px;}"
            "QPushButton:pressed{background:$NegativoMedio;}"
        ))

        self.btn_sair.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Font;"
            "border-radius:10px;color:$Font;padding:6px 18px;}"
            "QPushButton:pressed{background:$Principal;}"
        ))
        self.btn_photo.setStyleSheet(qss(
            "QPushButton{background:$DestaqueBaixo;border:2px solid $Destaque;"
            "border-radius:10px;color:$Destaque;padding:6px 18px;}"
            "QPushButton:pressed{background:$DestaqueMedio;}"
        ))

        botoes = QHBoxLayout()
        botoes.addWidget(self.btn_rec, 2)
        botoes.addWidget(self.btn_photo, 2)
        botoes.addWidget(self.btn_modo, 2)
        botoes.addWidget(self.btn_sair, 1)

        layout = QVBoxLayout()
        layout.addLayout(status)
        layout.addWidget(self.preview, 1)
        layout.addLayout(botoes)

        pagina_principal = QWidget()
        pagina_principal.setLayout(layout)

        self.config_window = Configura(self)   # cria uma única vez, fica escondida no stack
        self.files_window = FilesWindow(self)   # cria uma única vez, fica escondida no stack
        self.viewer_window = ViewerWindow(self)  # visualizador de foto/vídeo

        self.stack = QStackedWidget()
        self.stack.addWidget(pagina_principal)    # índice 0 = tela principal
        self.stack.addWidget(self.config_window)  # índice 1 = configurações
        self.stack.addWidget(self.files_window)  # índice 2 = arquivos
        self.stack.addWidget(self.viewer_window)  # índice 3 = visualizador
        self.stack.setCurrentIndex(0)

        layout_janela = QVBoxLayout(self)
        layout_janela.setContentsMargins(0, 0, 0, 0)
        layout_janela.addWidget(self.stack)

        self.btn_rec.clicked.connect(self.toggle_rec)
        self.btn_modo.clicked.connect(self.toggle_modo)
        self.btn_sair.clicked.connect(self.close)
        self.btn_conf.clicked.connect(self.abrir_config)
        self.btn_files.clicked.connect(self.abrir_files)
        self.btn_photo.clicked.connect(self.take_photo) 
        self.preview.done_signal.connect(self._foto_pronta)

        for tecla, func in (("N", self.toggle_modo),
                            ("R", self.toggle_rec),
                            ("P", self.take_photo),
                            ("F", self.toggle_fullscreen),
                            ("Q", self.close),
                            ("C", self.abrir_config),
                            ):
            sc = QShortcut(QKeySequence(tecla), self)
            sc.setContext(APP_SHORTCUT)
            sc.activated.connect(func)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.atualiza_status)
        self.timer.start(500)

        self.modo_dia()

    def _atualiza_relogio(self):
        self.lbl_clock.setText(
            QDateTime.currentDateTime().toString("HH:mm:ss")
        )

    def modo_dia(self):
        infra_red.off()
        #efeitos.definir(self.efeito_anterior)
        self.picam2.set_controls({
            "AwbEnable": True,
            "AnalogueGain": 0.0,
            "Saturation": 1.0,
            "FrameRate": FPS_DIA,
        })
        self.noite = False
        efeitos.definir_noite(self.noite)
        self.btn_modo.setText("☾ NOITE")
        self.btn_modo.setStyleSheet(qss(
            "QPushButton{background:$PositivoBaixo;border:2px solid $Positivo;"
            "border-radius:10px;color:$Positivo;padding:6px 18px;}"
            "QPushButton:pressed{background:$PositivoMedio;}"
        ))   
        self.lbl_modo.setText("☀  DIA")
        self.lbl_modo.setStyleSheet(qss("color:$Alerta; padding-right:6px;"))

    def modo_noite(self):
        infra_red.on()
        self.picam2.set_controls({
            "AwbEnable": False,
            "ColourGains": (1.0, 1.0),
            "AnalogueGain": GANHO_NOITE,
            "Saturation": 0.0,
            "FrameRate": FPS_NOITE,
        })
        #self.efeito_anterior = efeitos.nome_atual()  # salva o efeito atual antes de trocar
        self.noite = True
        efeitos.definir_noite(self.noite)
        self.btn_modo.setText("☀ DIA")
        self.btn_modo.setStyleSheet(qss(
            "QPushButton{background:$AlertaBaixo;border:2px solid $Alerta;"
            "border-radius:10px;color:$Alerta;padding:6px 18px;}"
            "QPushButton:pressed{background:$AlertaMedio;}"
        ))
        self.lbl_modo.setText("☾  NOITE")
        self.lbl_modo.setStyleSheet(qss("color:$Positivo; padding-right:6px;"))

    def toggle_modo(self):
        self.modo_dia() if self.noite else self.modo_noite()

    def toggle_rec(self):
        if self.gravando:
            self.picam2.stop_encoder()
            self.encoder = None
            self.gravando = False
            efeitos.evento("parar")
            self.btn_rec.setText("● REC")
            self.btn_rec.setStyleSheet(qss(
                "QPushButton{background:$NegativoBaixo;border:2px solid $Negativo;"
                "border-radius:10px;color:$Negativo;padding:6px 18px;}"
                "QPushButton:pressed{background:$NegativoBaixo;}"
            ))
            self.blink = False
            self._atualiza_overlay()
            self.files_window.marcar_sujo()
        else:
            fps_atual = FPS_NOITE if self.noite else FPS_DIA
            nome = time.strftime(f"{PASTA_VIDEOS}/handycam_video_%Y%m%d_%H%M%S.mp4")
            self.encoder = H264Encoder(bitrate=BITRATE, framerate=fps_atual)
            if AUDIO_DEVICE:
                saida = FfmpegOutput(nome, audio=True, audio_device=AUDIO_DEVICE,
                                     audio_codec="aac", audio_bitrate=AUDIO_BITRATE)
            else:
                saida = FfmpegOutput(nome)
            self.picam2.start_encoder(self.encoder, saida, name="main")
            self.gravando = True
            efeitos.evento("gravar")
            self.t0 = time.monotonic()
            self.blink = True
            self._atualiza_overlay()
            self.btn_rec.setText("■ STOP")
            self.btn_rec.setStyleSheet(qss(
                "QPushButton{background:$NegativoMedio;border:2px solid $NegativoFraco;"
                "border-radius:10px;color:$NegativoFraco;padding:6px 18px;}"
                "QPushButton:pressed{background:$NegativoMedio;}"
            ))
            print("Gravando:", nome)

    def take_photo(self):
        self._nome_foto = time.strftime(f"{PASTA_FOTOS}/handycam_foto_%Y%m%d_%H%M%S.jpg")
        self.btn_photo.setEnabled(False)
 
        # Feedback visual imediato: icone de camera azul no canto direito
        # do preview, some sozinho depois de 400 ms.
        self.icone_foto = True
        efeitos.evento("foto")
        self._atualiza_overlay()
        QTimer.singleShot(400, self._esconde_icone_foto)

        self.picam2.capture_request(signal_function=self.preview.signal_done)

    def _esconde_icone_foto(self):
        self.icone_foto = False
        self._atualiza_overlay()
 
    def _foto_pronta(self, job):
        request = self.picam2.wait(job)
        arr = request.make_array("main")
        cv2.imwrite(self._nome_foto, cv2.cvtColor(arr, cv2.COLOR_RGBA2RGB))
        request.release()
        print("Foto salva:", self._nome_foto)
        self.btn_photo.setEnabled(True)
        self.files_window.marcar_sujo()

    def atualiza_status(self):
        if not self.gravando:
            return
        self.blink = not self.blink
        self._atualiza_overlay()
 
    # ------------------------------------------------------------------
    # Overlay do preview (REC + icone de foto)
    #
    # O QGlPicamera2 desenha direto na tela via OpenGL (WA_PaintOnScreen),
    # entao QLabel filho nao aparece por cima dele. O jeito certo e mandar
    # um array RGBA para o proprio preview, que a GPU compoe sobre a imagem.
    # ------------------------------------------------------------------
    def _desenha_icone_camera(self, p, x, y, w=40, h=30, cor=None):
        """Mesmo desenho do CameraIcon, mas num QPainter ja existente."""
        cor = QColor(cor or C["Destaque"])
        caneta = QPen(cor)
        caneta.setWidth(2)
        p.setPen(caneta)
        p.setBrush(NO_BRUSH)
 
        # visor (o "olhinho" no topo)
        p.drawRoundedRect(int(x + w * 0.35), int(y), int(w * 0.3), int(h * 0.22), 2, 2)
 
        # corpo
        corpo_y = int(h * 0.18)
        corpo_h = h - corpo_y
        p.drawRoundedRect(int(x + 2), int(y + corpo_y), int(w - 4), int(corpo_h - 2), 4, 4)
 
        # lente
        p.setBrush(cor)
        raio = int(corpo_h * 0.32)
        cx, cy = int(x + w // 2), int(y + corpo_y + corpo_h // 2)
        p.drawEllipse(cx - raio, cy - raio, raio * 2, raio * 2)

    def _icone_camera_btn(self, cor=None, tamanho=64):
        cor = cor or C["Font"]
        pm = QPixmap(tamanho, tamanho)
        pm.fill(Qt.transparent)

        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        # margem de 4px pra caneta de 2px não ser cortada nas bordas
        self._desenha_icone_camera(p, 4, 4, w=tamanho - 8, h=(tamanho - 8) * 0.75, cor=cor)
        p.end()

        return QIcon(pm)
 
    def _tamanho_overlay(self):
        """
        O overlay e esticado pela GPU exatamente sobre o retangulo onde a
        imagem da camera aparece (viewport), nao sobre a janela inteira.
        Com keep_ar=True esse retangulo e menor que o widget (tarjas pretas
        nas laterais ou em cima/embaixo). Se o array tiver outra proporcao,
        o desenho sai achatado/esticado. Entao geramos o overlay no MESMO
        tamanho do viewport -> mapeamento 1:1, sem distorcao.
        """
        lw = max(self.preview.width(), 1)
        lh = max(self.preview.height(), 1)
 
        # tamanho real do stream que esta sendo exibido
        try:
            nome = self.picam2.camera_config["display"]
            tam = self.picam2.stream_map[nome].configuration.size
            iw, ih = int(tam.width), int(tam.height)
        except Exception:
            iw, ih = int(RESOLUCAO[0]), int(RESOLUCAO[1])
 
        if not getattr(self.preview, "keep_ar", True) or iw <= 0 or ih <= 0:
            return lw, lh
 
        if iw * lh > lw * ih:      # sobra em cima/embaixo
            w = lw
            h = max(w * ih // iw, 1)
        else:                      # sobra nas laterais
            h = lh
            w = max(h * iw // ih, 1)
        return w, h

    def _atualiza_overlay(self):
        if not self.gravando and not self.icone_foto:
            self.preview.set_overlay(None)
            return
 
        w, h = self._tamanho_overlay()
        if w < 64 or h < 48:
            return
 
        img = QImage(w, h, FMT_PREMUL)
        img.fill(0)   # tudo transparente
 
        p = QPainter(img)
        p.setRenderHint(ANTIALIAS)
 
        if self.gravando:
            seg = int(time.monotonic() - self.t0)
            texto = f"REC  {seg // 60:02d}:{seg % 60:02d}"
            p.setFont(fonte(16, True))
            fm = p.fontMetrics()
            try:
                tw = fm.horizontalAdvance(texto)
            except AttributeError:
                tw = fm.width(texto)
            th = fm.height()
 
            cx, cy = 12, 12
            cw, ch = 24 + 10 + tw + 12, th + 10
 
            # tarja escura pra ler o texto em cima de qualquer imagem
            p.setPen(NO_PEN)
            p.setBrush(QColor(0, 0, 0, 110))
            p.drawRoundedRect(cx, cy, cw, ch, 6, 6)
 
            # ponto vermelho piscando
            if self.blink:
                p.setBrush(QColor(C["Negativo"]))
                p.drawEllipse(cx + 10, cy + (ch - 14) // 2, 14, 14)
 
            # cronometro (alterna o tom junto com o ponto)
            p.setPen(QColor(C["Negativo"]) if self.blink else QColor(C["NegativoFraco"]))
            p.drawText(cx + 34, cy + 5 + fm.ascent(), texto)
 
        if self.icone_foto:
            self._desenha_icone_camera(p, w - 40 - 14, 14)
 
        p.end()
 
        # QImage -> numpy RGBA (o set_overlay espera nao-premultiplicado)
        img = img.convertToFormat(FMT_RGBA)
        buf = img.bits()
        try:
            tam = img.sizeInBytes()
        except AttributeError:
            tam = img.byteCount()
        try:
            buf.setsize(tam)      # PyQt: sip.voidptr precisa do tamanho
        except AttributeError:
            pass                  # PySide: ja vem memoryview
 
        arr = np.frombuffer(buf, dtype=np.uint8)
        arr = arr.reshape(h, img.bytesPerLine() // 4, 4)[:, :w].copy()
        self.preview.set_overlay(arr)
 
    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def closeEvent(self, event):
        self.viewer_window.parar()
        if self.gravando:
            self.picam2.stop_encoder()
        self.picam2.stop()
        event.accept()

    def abrir_config(self):
        self.stack.setCurrentIndex(1)

    def tela_principal(self):
        self.stack.setCurrentIndex(0)

    def abrir_files(self):
        self.stack.setCurrentIndex(2)
        self.files_window.restaurar_scroll()
    
    def abrir_viewer(self, caminho):
        self.files_window.salvar_scroll()
        self.stack.setCurrentIndex(3)
        self.viewer_window.abrir(caminho)
    

# ----------------------------------------------------------------------
# Janela de configurações (resolução + fps)
# ----------------------------------------------------------------------

class DialogoUpdate(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setModal(True)
        self.setFixedSize(440, 240)
        self.setStyleSheet(qss("background:$Cartao; color:$Font;"))

        self.lbl = QLabel("Conectando...")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setWordWrap(True)
        self.lbl.setFont(fonte(12))

        self.btn = QPushButton("OK")
        self.btn.setFont(fonte(12))
        self.btn.setMinimumHeight(56)
        self.btn.setStyleSheet(qss("QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
        "border-radius:10px;color:$Font;padding:6px 18px;}"
        "QPushButton:pressed{background:$Principal;}"
        "QPushButton:checked{background:$PrincipalAlto;border:2px solid $Font;color:$Font;}"
        ))
        self.btn.clicked.connect(self.accept)
        self.btn.hide()   # só aparece quando termina

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(16)
        lay.addWidget(self.lbl, 1)
        lay.addWidget(self.btn)

    def mensagem(self, texto):
        self.lbl.setText(texto)

    def finalizar(self, texto):
        self.lbl.setText(texto)
        self.btn.show()


class Configura(QWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent

        self.setStyleSheet(qss("background:$PrincipalBaixo; color:$Font;"))

        self.config = self.carregar_config()
        self.config_inicial = dict(self.config)  # cópia pra comparar depois (aviso de "mudou")
        self.msgR = f'{self.config["resolucao"][0]}x{self.config["resolucao"][1]}'
        self.msgFPS = f'{self.config["fps"]} FPS'
        self.msgTema = self._label_tema(self.config.get("tema"))

        layout = QVBoxLayout()
        layout.setSpacing(14)
        layout.setContentsMargins(9, 9, 9, 9)

        lbl_titulo = QLabel("Configurações PVL-PiHCAM")
        lbl_titulo.setFont(fonte(15))

        btn_update = self._criar_botao_update()

        linha_topo = QHBoxLayout()
        linha_topo.addWidget(lbl_titulo)
        linha_topo.addStretch()
        linha_topo.addWidget(btn_update)

        layout.addLayout(linha_topo)

        # --- Área de scroll com resolução + fps (igual o FilesWindow) ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.viewport().setAttribute(Qt.WA_AcceptTouchEvents, True)
        QScroller.grabGesture(self.scroll_area.viewport(),
                      QScroller.LeftMouseButtonGesture)
        self.scroll_area.setStyleSheet(qss(
            "QScrollArea{border:none; background:$PrincipalBaixo; padding: 6px;}"
            "QScrollBar:vertical{background:$PrincipalBaixo; width:10px;}"
            "QScrollBar::handle:vertical{background:$Font;}"
        ))

        self.config_container = QWidget()
        conteudo = QVBoxLayout(self.config_container)
        conteudo.setSpacing(14)
        conteudo.setContentsMargins(10, 10, 10, 10)
        self.config_container.setStyleSheet(qss(
            "background:$PrincipalBaixo; border:1px solid $Principal; border-radius:5px; padding:10px;"
        ))

        lbl_sub1 = QLabel("Resolução e FPS")
        lbl_sub1.setFont(fonte(12))
        conteudo.addWidget(lbl_sub1)

        self.botoes_resolucao = []

        linha = None
        for i, (label, largura, altura) in enumerate(RESOLUCOES):
            if i % 3 == 0:
                linha = QHBoxLayout()
                conteudo.addLayout(linha)

            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setMinimumHeight(64)
            btn.setFont(fonte(12))
            btn.setFocusPolicy(NO_FOCUS)
            if largura == self.config.get("resolucao", [1280, 720])[0] and altura == self.config.get("resolucao", [1280, 720])[1]:
                btn.setStyleSheet(BTN_ON)
            else:
                btn.setStyleSheet(BTN_OFF)
            btn.clicked.connect(
                lambda checked, l=largura, a=altura, b=btn: self.selecionar_resolucao(l, a, b)
            )
            linha.addWidget(btn)
            self.botoes_resolucao.append((btn, largura, altura))


        lbl_sub2 = QLabel("FPS (frames por segundo)")
        lbl_sub2.setFont(fonte(12))
        conteudo.addWidget(lbl_sub2)

        self.botoes_fps = []

        for i in range(len(FPS_OPCOES)):
            if i % 3 == 0:
                linha = QHBoxLayout()
                conteudo.addLayout(linha)

            fps = FPS_OPCOES[i]
            btn = QPushButton(f"{fps} FPS")
            btn.setCheckable(True)
            btn.setMinimumHeight(64)
            btn.setFont(fonte(12))
            btn.setFocusPolicy(NO_FOCUS)
            if fps == self.config.get("fps"):
                btn.setStyleSheet(BTN_ON)
            else:
                btn.setStyleSheet(BTN_OFF)

            btn.clicked.connect(
                lambda checked, f=fps, b=btn: self.selecionar_fps(f, b)
            )
            linha.addWidget(btn)
            self.botoes_fps.append((btn, fps))


        lbl_sub3 = QLabel("Efeito de imagem")
        lbl_sub3.setFont(fonte(12))
        conteudo.addWidget(lbl_sub3)

        # (nome_interno, texto_do_botao) — None = sem efeito
        EFEITO_OPCOES = [(None, "Nenhum")] + [
            (n, info["label"]) for n, info in sorted(
                efeitos.EFEITOS.items(), key=lambda kv: kv[1]["label"]
            )
        ]

        self.botoes_efeito = []
        atual = self.config.get("efeito")     # ex.: "verde" ou None
        linha = None

        for i, (nome, label) in enumerate(EFEITO_OPCOES):
            if i % 3 == 0:
                linha = QHBoxLayout()
                conteudo.addLayout(linha)

            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setMinimumHeight(64)
            btn.setFont(fonte(12))
            btn.setFocusPolicy(NO_FOCUS)
            btn.setChecked(nome == atual)
            btn.setStyleSheet(BTN_ON if nome == atual else BTN_OFF)

            btn.clicked.connect(
                lambda checked, n=nome, b=btn: self.selecionar_efeito(n, b)
            )
            linha.addWidget(btn)
            self.botoes_efeito.append((btn, nome))


        lbl_sub4 = QLabel("Tema")
        lbl_sub4.setFont(fonte(12))
        conteudo.addWidget(lbl_sub4)

        self.botoes_tema = []
        tema_atual = self.config.get("tema", temas.PADRAO)
        linha = None

        for i, (nome, info) in enumerate(sorted(
                temas.TEMAS.items(), key=lambda kv: kv[1]["label"])):
            if i % 3 == 0:
                linha = QHBoxLayout()
                conteudo.addLayout(linha)

            btn = QPushButton(info["label"])
            btn.setCheckable(True)
            btn.setMinimumHeight(64)
            btn.setFont(fonte(12))
            btn.setFocusPolicy(NO_FOCUS)
            btn.setChecked(nome == tema_atual)
            btn.setStyleSheet(BTN_ON if nome == tema_atual else BTN_OFF)

            btn.clicked.connect(
                lambda checked, n=nome, b=btn: self.selecionar_tema(n, b)
            )
            linha.addWidget(btn)
            self.botoes_tema.append((btn, nome))


        conteudo.addStretch()
        self.config_container.setLayout(conteudo)
        self.scroll_area.setWidget(self.config_container)
        layout.addWidget(self.scroll_area, 1)   # ocupa o espaço restante, igual o FilesWindow

        # --- Aviso + botões fixos embaixo, fora do scroll ---
        self.lbl_aviso = QLabel("")
        self.lbl_aviso.setFont(fonte(9))
        self.lbl_aviso.setStyleSheet(qss("color:$Alerta;"))
        layout.addWidget(self.lbl_aviso)

        linha_botoes = QHBoxLayout()

        btn_voltar = QPushButton("← Voltar")
        btn_voltar.setMinimumHeight(64)
        btn_voltar.setFont(fonte(15))
        btn_voltar.setFocusPolicy(NO_FOCUS)
        btn_voltar.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Font;"
            "border-radius:10px;color:$Font;padding:6px 18px;}"
            "QPushButton:pressed{background:$Principal;}"
        ))
        btn_voltar.clicked.connect(self.voltar)

        btn_aplicar = QPushButton("↻ Aplicar e Reiniciar")
        btn_aplicar.setMinimumHeight(64)
        btn_aplicar.setFont(fonte(15))
        btn_aplicar.setFocusPolicy(NO_FOCUS)
        btn_aplicar.setStyleSheet(qss(
            "QPushButton{background:$DestaqueMedio;border:2px solid $Destaque;"
            "border-radius:10px;color:$Destaque;padding:6px 18px;}"
            "QPushButton:pressed{background:$DestaqueMedio;}"
        ))
        btn_aplicar.clicked.connect(self.reiniciar_app)

        linha_botoes.addWidget(btn_voltar)
        linha_botoes.addWidget(btn_aplicar)
        layout.addLayout(linha_botoes)

        self.setLayout(layout)
        self.aplicar_estilo_botoes()
        self.marcar_selecionado()

    def _criar_botao_update(self):
        btn = QPushButton("⛁ Atualizar")
        btn.setMinimumHeight(50)
        btn.setFont(fonte(15))
        btn.setFocusPolicy(NO_FOCUS)
        btn.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
            "border-radius:10px;color:$Font;padding:6px 18px;}"
            "QPushButton:pressed{background:$DestaqueMedio;border:2px solid $DestaqueAlto;color:$Font;}"
        ))
        btn.clicked.connect(self.atualizar_app)
        return btn

    def atualizar_app(self):
        self._reiniciar_apos = False
        self.dlg_update = DialogoUpdate(self)

        self.worker_update = AtualizadorWorker()
        self.worker_update.progresso.connect(self.dlg_update.mensagem)
        self.worker_update.terminou.connect(self._fim_update)
        self.worker_update.start()

        self.dlg_update.exec_()        # bloqueia até o usuário tocar em OK

        if self._reiniciar_apos:
            self.reiniciar_app()

    def _fim_update(self, precisa_reiniciar, msg):
        self._reiniciar_apos = precisa_reiniciar
        self.dlg_update.finalizar(msg)

    def aplicar_estilo_botoes(self):
        self.setStyleSheet(self.styleSheet() + qss("""
            QPushButton {
                background: $Principal;
                border: 2px solid $PrincipalAlto;
                border-radius: 10px;
                padding: 6px 18px;
                text-align: left;
                padding-left: 14px;
            }
            QPushButton:hover {
                background: $PrincipalMedio;
            }
            QPushButton:checked {
                background: $DestaqueAlto;
                border: 2px solid $Destaque;
                color: $DestaqueFont;
            }
        """))

    def atualizar_aviso(self):
        mudou = (
            self.config.get("resolucao") != self.config_inicial.get("resolucao")
            or self.config.get("fps") != self.config_inicial.get("fps")
            or self.config.get("tema") != self.config_inicial.get("tema")
        )
        if mudou:
            self.lbl_aviso.setText(
                f"Selecionado: {self.msgR} {self.msgFPS} | {self.msgTema} — clique em "
                f"\"Aplicar e Reiniciar\" para usar."
            )
        else:
            self.lbl_aviso.setText("")

    def selecionar_resolucao(self, largura, altura, botao_clicado):
        for btn, *_ in self.botoes_resolucao:
            if btn is not botao_clicado:
                btn.setChecked(False)
        botao_clicado.setChecked(True)

        self.config["resolucao"] = [largura, altura]
        self.salvar_config()
        self.msgR = f"{largura}x{altura}"
        self.atualizar_aviso()

    def selecionar_fps(self, fps, botao_clicado):
        for btn, f in self.botoes_fps:
            if btn is not botao_clicado:
                btn.setChecked(False)
        botao_clicado.setChecked(True)

        self.config["fps"] = fps
        self.salvar_config()
        self.msgFPS = f"{fps} FPS"
        self.atualizar_aviso()

    def selecionar_efeito(self, nome, botao):
        for b, n in self.botoes_efeito:
            b.setChecked(n == nome)
            b.setStyleSheet(BTN_ON if n == nome else BTN_OFF)

        efeitos.definir(nome)                 # troca imediata, sem os.execv
        self.config["efeito"] = nome
        self.salvar_config()                  # o mesmo que grava ~/.handycam_config.json

    def _label_tema(self, nome):
        info = temas.TEMAS.get(nome) or temas.TEMAS[temas.PADRAO]
        return info["label"]

    def selecionar_tema(self, nome, botao):
        # Diferente do efeito, o tema NÃO troca na hora: BTN_ON/BTN_OFF e os
        # stylesheets das telas são montados no import do handycam.py, então
        # só o os.execv aplica as cores novas.
        for b, n in self.botoes_tema:
            b.setChecked(n == nome)
            b.setStyleSheet(BTN_ON if n == nome else BTN_OFF)

        self.config["tema"] = nome
        self.salvar_config()
        self.msgTema = self._label_tema(nome)
        self.atualizar_aviso()

    def marcar_selecionado(self):
        atual_res = self.config.get("resolucao")
        for btn, l, a in self.botoes_resolucao:
            btn.setChecked(atual_res == [l, a])

        atual_fps = self.config.get("fps")
        for btn, f in self.botoes_fps:
            btn.setChecked(atual_fps == f)

        atual_tema = self.config.get("tema", temas.PADRAO)
        for btn, n in self.botoes_tema:
            btn.setChecked(n == atual_tema)


    def carregar_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    cfg = json.load(f)
                    cfg.setdefault("resolucao", [1280, 720])
                    cfg.setdefault("fps", 24)
                    cfg.setdefault("efeito", None)
                    cfg.setdefault("tema", temas.PADRAO)
                    return cfg
            except (json.JSONDecodeError, OSError):
                pass
        return {"resolucao": [1280, 720], "fps": 24, "efeito": None,
                "tema": temas.PADRAO}

    def salvar_config(self):
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(self.config, f, indent=2)
        except OSError as e:
            print(f"Erro ao salvar config: {e}")

    def voltar(self):
        if self.parent_app:
            self.parent_app.tela_principal()

    def reiniciar_app(self):
        """Reinicia o processo do zero, já lendo a nova config salva."""
        if self.parent_app:
            # encerra a câmera/gravação corretamente antes de reiniciar
            if self.parent_app.gravando:
                self.parent_app.picam2.stop_encoder()
            self.parent_app.picam2.stop()
        os.execv(sys.executable, [sys.executable] + sys.argv)

# ----------------------------------------------------------------------
# Geração de thumbnails em background (não trava a UI) + cache em disco
# (só gera uma vez por arquivo; funciona igual no PC e no Raspberry Pi,
# desde que o ffmpeg esteja instalado em ambos: `sudo apt install ffmpeg`)
# ----------------------------------------------------------------------
class ThumbnailSignals(QObject):
    pronto = Signal(str, str)  # (nome_arquivo_original, caminho_thumb)


class ThumbnailWorker(QRunnable):
    def __init__(self, arquivo, caminho_original, caminho_thumb):
        super().__init__()
        self.arquivo = arquivo
        self.caminho_original = caminho_original
        self.caminho_thumb = caminho_thumb
        self.signals = ThumbnailSignals()

    def run(self):
        try:
            if self.arquivo.endswith(".mp4"):
                # Otimizado pro Pi 4:
                # - "-ss" ANTES do "-i" faz busca rápida pelo keyframe mais
                #   próximo (fast seek), sem decodificar tudo desde o
                #   início do vídeo. Colocado depois do "-i" (como no PC),
                #   o ffmpeg decodifica frame a frame até o segundo 1, o
                #   que é caro numa CPU ARM já ocupada com a câmera.
                # - "-threads 1" evita que o ffmpeg tome vários núcleos de
                #   uma vez, competindo com a captura/preview.
                # - "flags=fast_bilinear" é um algoritmo de escala mais
                #   barato que o padrão (bicubic), suficiente pra 125px.
                # - timeout evita travar a fila se algum arquivo estiver
                #   corrompido (ex: energia caiu no meio da gravação).
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-ss", "00:00:01", "-i", self.caminho_original,
                        "-frames:v", "1",
                        "-vf", "scale=125:-1:flags=fast_bilinear",
                        "-threads", "1",
                        self.caminho_thumb
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=10
                )
            else:  # .jpg
                pixmap = QPixmap(self.caminho_original).scaled(
                    125, 75, KEEP_AR, SMOOTH_TRANSFORM
                )
                pixmap.save(self.caminho_thumb)

            self.signals.pronto.emit(self.arquivo, self.caminho_thumb)
        except Exception as e:
            print(f"Erro gerando thumbnail de {self.arquivo}: {e}")


class ClickableItem(QWidget):
    """Linha da lista de arquivos: emite clicked() só se o dedo não arrastou."""
    clicked = Signal()

    ESTILO_NORMAL = qss("background:$PrincipalMedio; border:1px solid $PrincipalAlto; "
                     "border-radius:5px; margin:6px;")
    ESTILO_PRESS  = qss("background:$Principal; border:1px solid $PrincipalAlto; "
                     "border-radius:5px; margin:6px;")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._pos_press = None
        self.setStyleSheet(self.ESTILO_NORMAL)

    def mousePressEvent(self, event):
        self._pos_press = event.pos()
        self.setStyleSheet(self.ESTILO_PRESS)   # feedback visual imediato
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.setStyleSheet(self.ESTILO_NORMAL)
        if self._pos_press is not None:
            d = event.pos() - self._pos_press
            if abs(d.x()) < 20 and abs(d.y()) < 20:   # não foi scroll
                self.clicked.emit()
        self._pos_press = None
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):
        self.setStyleSheet(self.ESTILO_NORMAL)
        super().leaveEvent(event)


# File Manager

def botao_touch(texto, larg=0, alt=48, destaque=False):
    b = QPushButton(texto)
    b.setFocusPolicy(NO_FOCUS)
    b.setFont(fonte(13))
    if larg:
        b.setFixedSize(larg, alt)
    else:
        b.setFixedHeight(alt)
    b.setStyleSheet(BTN_ON if destaque else BTN_OFF)
    return b

class EscolhaOverlay(QWidget):
    escolhido = Signal(object)          # o valor associado ao item
    ejetado = Signal(object)            # o mountpoint, quando toca no "⏏"

    def __init__(self, titulo, itens, parent):
        """itens: [(rótulo, valor), ...] ou [(rótulo, valor, extra), ...]
        extra != None mostra um botão de ejetar na linha."""
        super().__init__(parent)
        self.setGeometry(parent.rect())
        self.setStyleSheet("background:transparent;")

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(min(560, max(320, parent.width() - 60)))
        card.setStyleSheet(qss(
            "#card{background:$Cartao;border:2px solid $PrincipalAlto;border-radius:16px;}"
            "QLabel{color:$Font;}"
        ))
        col = QVBoxLayout(card)
        col.setContentsMargins(18, 16, 18, 16)
        col.setSpacing(8)

        if titulo:
            lbl = QLabel(titulo)
            lbl.setFont(fonte(14, True))
            lbl.setStyleSheet(qss("color:$FontFraca;"))
            col.addWidget(lbl)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setStyleSheet("background:transparent;")
        area.viewport().setAttribute(Qt.WA_AcceptTouchEvents, True)
        QScroller.grabGesture(area.viewport(), QScroller.LeftMouseButtonGesture)

        dentro = QWidget()
        dentro.setStyleSheet("background:transparent;")
        v = QVBoxLayout(dentro)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        for item in itens:
            rotulo, valor = item[0], item[1]
            extra = item[2] if len(item) > 2 else None

            linha = QWidget()
            linha.setStyleSheet("background:transparent;")
            h = QHBoxLayout(linha)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)

            b = botao_touch(rotulo, 0, 56)
            b.setStyleSheet(b.styleSheet() + "QPushButton{text-align:left;}")
            b.clicked.connect(lambda _, x=valor: self._pick(x))
            h.addWidget(b, 1)

            if extra is not None:
                eject = botao_touch("⏏", 56, 56)
                eject.clicked.connect(lambda _, x=extra: self._eject(x))
                h.addWidget(eject)

            v.addWidget(linha)
        v.addStretch()
        area.setWidget(dentro)

        alt = min(len(itens) * 62 + 10, max(160, parent.height() - 220))
        area.setFixedHeight(alt)
        col.addWidget(area)

        b_x = botao_touch("Cancelar", 0, 56)
        b_x.clicked.connect(self._fechar)
        col.addWidget(b_x)

        raiz = QVBoxLayout(self)
        raiz.addStretch()
        meio = QHBoxLayout()
        meio.addStretch()
        meio.addWidget(card)
        meio.addStretch()
        raiz.addLayout(meio)
        raiz.addStretch()

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, 205))

    def _fechar(self):
        self.hide()
        self.deleteLater()

    def _pick(self, valor):
        self.escolhido.emit(valor)
        self._fechar()

    def _eject(self, extra):
        # não fecha o overlay: quem escuta `ejetado` decide (ex.: recarregar a lista)
        self.ejetado.emit(extra)

def escolher(titulo, itens, parent, callback, on_eject=None):
    ov = EscolhaOverlay(titulo, itens, parent)
    ov.escolhido.connect(callback)
    if on_eject:
        ov.ejetado.connect(on_eject)
    ov.setGeometry(parent.rect())
    ov.show()
    ov.raise_()
    return ov

class TecladoVirtual(QWidget):
    LINHAS = ["1234567890", "qwertyuiop", "asdfghjkl-", "zxcvbnm_."]

    def __init__(self, alvo, parent=None, alt=38):
        super().__init__(parent)
        self.alvo = alvo
        self.caps = False
        self.teclas = []

        g = QGridLayout(self)
        g.setSpacing(3)
        g.setContentsMargins(0, 0, 0, 0)
        for c in range(10):
            g.setColumnStretch(c, 1)

        for r, linha in enumerate(self.LINHAS):
            for c, ch in enumerate(linha):
                b = botao_touch(ch, 0, alt)
                b.setMinimumWidth(1)          # deixa o grid encolher
                b.clicked.connect(lambda _, x=ch: self._digitar(x))
                self.teclas.append((b, ch))
                g.addWidget(b, r, c)

        b_shift = botao_touch("⇧", 0, alt)
        b_shift.setCheckable(True)
        b_shift.toggled.connect(self._caps)
        g.addWidget(b_shift, 4, 0, 1, 2)

        b_space = botao_touch("espaço", 0, alt)
        b_space.setMinimumWidth(1)
        b_space.clicked.connect(lambda: self._digitar(" "))
        g.addWidget(b_space, 4, 2, 1, 6)

        b_back = botao_touch("⌫", 0, alt)
        b_back.clicked.connect(self.alvo.backspace)
        g.addWidget(b_back, 4, 8, 1, 2)
        
    def _caps(self, on):
        self.caps = on
        for b, ch in self.teclas:
            b.setText(ch.upper() if on else ch)

    def _digitar(self, ch):
        self.alvo.insert(ch.upper() if self.caps and ch.isalpha() else ch)


def garantir_padrao(raiz):
    """Cria Videos/ e Fotos/ na raiz do armazenamento. Levanta OSError se read-only."""
    os.makedirs(os.path.join(raiz, "Videos"), exist_ok=True)
    os.makedirs(os.path.join(raiz, "Fotos"),  exist_ok=True)
    return raiz

def thumb_de(caminho):
    caminho = os.path.realpath(caminho)
    raiz = os.path.realpath(PASTA_SAIDA)
    if caminho == raiz or caminho.startswith(raiz + os.sep):
        chave = os.path.relpath(caminho, raiz)      # igual a hoje: cache atual continua valendo
    else:
        h = hashlib.md5(os.path.dirname(caminho).encode()).hexdigest()[:10]
        chave = f"ext_{h}_{os.path.basename(caminho)}"
    return os.path.join(PASTA_THUMBS, chave.replace(os.sep, "__") + ".thumb.jpg")

def armazens():
    """[(rótulo, caminho_raiz, livre_em_bytes, mountpoint, externo), ...] — interno primeiro."""
    lista = [("Interno", PASTA_SAIDA, PASTA_SAIDA, PASTA_SAIDA, False)]
    try:
        r = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,LABEL,MOUNTPOINT,TYPE,RM"],
            capture_output=True, text=True, timeout=3)
        dados = json.loads(r.stdout)
    except Exception:
        dados = {}

    def eh_removivel(n):
        rm = n.get("rm")
        return rm is True or str(rm) == "1"

    def anda(nos, removivel_herdado=False):
        for n in nos:
            rm = removivel_herdado or eh_removivel(n)
            mp = n.get("mountpoint")
            if mp and mp not in ("/", "/boot", "/boot/firmware", "[SWAP]"):
                lista.append((n.get("label") or os.path.basename(mp),
                              mp, os.path.join(mp, SUBPASTA_USB), mp, rm))
            anda(n.get("children") or [], rm)
    anda(dados.get("blockdevices", []))

    saida = []
    for rotulo, ponto, raiz, mp, externo in lista:
        try:
            saida.append((rotulo, raiz, shutil.disk_usage(ponto).free, mp, externo))
        except OSError:
            pass          # desmontado no meio do caminho
    return saida


class FilesWindow(QWidget):
    ORDENS = [
        ("Nome",    lambda e: e.name.lower(),     True),
        ("Data",    lambda e: e.stat().st_mtime,  True),
        ("Tamanho", lambda e: e.stat().st_size,   True),
    ]
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent

        self.setStyleSheet(qss("background:$PrincipalBaixo; color:$Font;"))
        layout = QVBoxLayout()
        self._ordem = 0
        self.raiz = os.path.realpath(PASTA_SAIDA)
        self.pasta_atual = self.raiz

        topo = QHBoxLayout()
        self.lbl_titulo = QLabel("Arquivos Gravados")
        self.lbl_titulo.setFont(fonte(15))
        topo.addWidget(self.lbl_titulo)
        topo.addStretch()

        self.btn_armazens = QPushButton("⛁  Armazenamento")
        self.btn_armazens.setFont(fonte(12))
        self.btn_armazens.setFocusPolicy(NO_FOCUS)
        self.btn_armazens.setFixedHeight(48)
        self.btn_armazens.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
            "border-radius:10px;color:$Font;padding:0 14px;}"
            "QPushButton:pressed{background:$Principal; border:2px solid $Font;}"
        ))
        self.btn_armazens.clicked.connect(
            lambda: self.show_armazens(self.btn_armazens))
        topo.addWidget(self.btn_armazens)

        self.lbl_tamanho = QLabel("")
        self.lbl_tamanho.setFont(fonte(12))
        self.lbl_tamanho.setStyleSheet(qss("color:$FontFraca;"))
        topo.addWidget(self.lbl_tamanho)

        self.btn_ordem = QPushButton("⇅ Nome")
        self.btn_ordem.setFont(fonte(12))
        self.btn_ordem.setFocusPolicy(NO_FOCUS)
        self.btn_ordem.setFixedHeight(48)
        self.btn_ordem.setMinimumWidth(140)
        self.btn_ordem.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
            "border-radius:10px;color:$Font;padding:0 14px;}"
            "QPushButton:pressed{background:$Principal; border:2px solid $Font;}"
        ))
        self.btn_ordem.clicked.connect(self.alternar_ordem)
        topo.addWidget(self.btn_ordem)

        layout.addLayout(topo)
        layout.setSpacing(14)
        layout.setContentsMargins(9, 9, 9, 9)

        # --- Área de scroll com a lista de arquivos ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.viewport().setAttribute(Qt.WA_AcceptTouchEvents, True)
        QScroller.grabGesture(self.scroll_area.viewport(),
                      QScroller.LeftMouseButtonGesture)
        self.scroll_area.setStyleSheet(qss(
            "QScrollArea{border:none; background:$PrincipalBaixo;}"
            "QScrollBar:vertical{background:$PrincipalBaixo; width:10px;}"
            "QScrollBar::handle:vertical{background:$Font;}"
        ))

        # Widget interno que vai conter a lista (o QScrollArea precisa de um único widget filho)
        self.files_container = QWidget()
        self.files_layout = QVBoxLayout(self.files_container)
        self.files_layout.setSpacing(5)
        self.files_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.files_container.setLayout(self.files_layout)
        self.files_container.setStyleSheet(qss("background:$PrincipalBaixo; border:1px solid $Principal; border-radius:5px; padding:10px;"))

        self.scroll_area.setWidget(self.files_container)
        layout.addWidget(self.scroll_area)
        # ------------------------------------------------

        btn_voltar = QPushButton("Voltar")
        btn_voltar.setMinimumHeight(64)
        btn_voltar.setFont(fonte(15))
        btn_voltar.setFocusPolicy(NO_FOCUS)
        btn_voltar.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Font;"
            "border-radius:10px;color:$Font;padding:6px 18px;}"
            "QPushButton:pressed{background:$Principal;}"
        ))
        btn_voltar.clicked.connect(self.voltar)
        layout.addWidget(btn_voltar)
        self.setLayout(layout)

    def show_armazens(self, botao=None):
        itens = []
        for r, c, l, mp, externo in armazens():
            marcado = os.path.realpath(c) == self.raiz
            texto = f"{'●  ' if marcado else '     '}{r}   ({self._fmt_bytes(l)} livres)"
            itens.append((texto, (r, c), mp if externo else None))
        itens.append(("⟳   Procurar novamente", None, None))

        self._overlay_armazens = escolher(
            "Armazenamento", itens, self, self._set_armazem,
            on_eject=self._ejetar_pendrive
        )

    def _ejetar_pendrive(self, mountpoint):
        dispositivo = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", mountpoint],
            capture_output=True, text=True
        ).stdout.strip()
        if not dispositivo:
            self.lbl_titulo.setText("Não achei o dispositivo")
            return
        try:
            subprocess.run(["udisksctl", "unmount", "-b", dispositivo], check=True)
            subprocess.run(["udisksctl", "power-off", "-b", dispositivo], check=True)

            self.trocar_armazem(PASTA_SAIDA)  # sempre volta pro interno

            if self._overlay_armazens:
                self._overlay_armazens._fechar()
            QTimer.singleShot(0, self.show_armazens)
        except subprocess.CalledProcessError as e:
            self.lbl_titulo.setText(f"Erro ao ejetar: {e}")

    def _set_armazem(self, par):
        if par is None:
            return QTimer.singleShot(0, self.show_armazens)
        self.trocar_armazem(par[1], par[0])

    def trocar_armazem(self, caminho, rotulo=""):
        try:
            os.makedirs(os.path.join(caminho, "Videos"), exist_ok=True)
            os.makedirs(os.path.join(caminho, "Fotos"),  exist_ok=True)
        except OSError as e:
            self.lbl_titulo.setText(f"Sem acesso: {e.strerror}")
            return

        self.raiz = os.path.realpath(caminho)
        self.pasta_atual = self.raiz
        interno = self.raiz == os.path.realpath(PASTA_SAIDA)
        self.btn_armazens.setText("⛁  Armazenamento" if interno else f"⛁  {rotulo}")
        self.lbl_titulo.setText("Arquivos Gravados" if interno else rotulo)
        self.load_files()
        self.scroll_area.verticalScrollBar().setValue(0)
    
    def alternar_ordem(self):
        self._ordem = (self._ordem + 1) % len(self.ORDENS)
        self.btn_ordem.setText("⇅ " + self.ORDENS[self._ordem][0])
        self.load_files()
        self.scroll_area.verticalScrollBar().setValue(0)

    def showEvent(self, event):
        super().showEvent(event)
        if not os.path.isdir(self.raiz):
            self.trocar_armazem(PASTA_SAIDA, "Interno")
            self._sujo = False
            return
        if getattr(self, "_sujo", True):
            self.load_files()
            self._sujo = False

    def marcar_sujo(self):
        self._sujo = True

    def salvar_scroll(self):
        self._scroll_pos = self.scroll_area.verticalScrollBar().value()

    def restaurar_scroll(self):
        # QTimer 0 = deixa o layout terminar antes de posicionar,
        # senão o valor é truncado pelo range ainda desatualizado
        QTimer.singleShot(0, lambda: self.scroll_area.verticalScrollBar()
                                         .setValue(getattr(self, "_scroll_pos", 0)))

    def abrir_arquivo(self, nome_arquivo):
        if self.parent_app:
            self.parent_app.abrir_viewer(os.path.join(self.pasta_atual, nome_arquivo))

    def load_files(self):
        # Limpa a lista atual antes de recarregar
        while self.files_layout.count():
            item = self.files_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        _, chave, inverso = self.ORDENS[self._ordem]
        with os.scandir(self.pasta_atual) as it:
            entradas = [e for e in it if not e.name.startswith(".")]

        pastas = sorted((e for e in entradas if e.is_dir()),
                        key=lambda e: e.name.lower())
        entradas = [e for e in entradas
                    if e.is_file() and e.name.endswith(('.mp4', '.jpg'))]
        entradas.sort(key=chave, reverse=inverso)
        arquivos = [e.name for e in entradas]

        self.thumb_labels = {}

        # sempre no topo, só quando não está na raiz
        if os.path.realpath(self.pasta_atual) != self.raiz:
            self.files_layout.addWidget(
                self._item_navegacao("⬅  voltar uma pasta",
                                     os.path.dirname(self.pasta_atual)))

        na_raiz = os.path.realpath(self.pasta_atual) == os.path.realpath(self.raiz)
        for e in pastas:
            protegida = na_raiz and e.name in ("Videos", "Fotos")
            self.files_layout.addWidget(
                self._item_navegacao(e.name, e.path, apagavel=not protegida))

        if not pastas and not arquivos:
            lbl_vazio = QLabel("Nenhum arquivo encontrado.")
            lbl_vazio.setFont(fonte(15))
            lbl_vazio.setStyleSheet(qss("color:$FontFraca;"))
            self.files_layout.addWidget(lbl_vazio)

        for arquivo in arquivos:
            # Widget "linha" que vai servir de container pro layout horizontal
            item_widget = ClickableItem()
            item_widget.clicked.connect(lambda f=arquivo: self.abrir_arquivo(f))

            layoutarquivo = QHBoxLayout(item_widget)  # já associa o layout ao widget
            layoutarquivo.setSpacing(0)

            # --- Preview / thumbnail ---
            lbl_thumb = QLabel()
            lbl_thumb.setFixedSize(125, 75)
            lbl_thumb.setStyleSheet("background: transparent; border:none; border-radius:4px;")

            caminho_original = os.path.join(self.pasta_atual, arquivo)   # dentro do loop
            caminho_thumb = thumb_de(caminho_original)

            if os.path.exists(caminho_thumb):
                # já tem cache em disco, carrega direto (rápido, sem ffmpeg)
                pixmap = QPixmap(caminho_thumb)
                lbl_thumb.setPixmap(pixmap)
            else:
                # ainda não existe: gera em background pra não travar a UI
                self.thumb_labels[arquivo] = lbl_thumb
                worker = ThumbnailWorker(arquivo, caminho_original, caminho_thumb)
                worker.signals.pronto.connect(self.on_thumb_pronta)
                QThreadPool.globalInstance().start(worker)

            lbl_arquivo = QLabel(arquivo)
            lbl_arquivo.setFont(fonte(12))
            lbl_arquivo.setStyleSheet("background:transparent; border:none;")

            btn_options = QPushButton("≡")
            btn_options.setFont(fonte(12))
            btn_options.setFocusPolicy(NO_FOCUS)
            btn_options.setStyleSheet(qss(
                "QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
                "border-radius:10px;color:$Font;}"
                "QPushButton:pressed{background:$Principal; border:2px solid $Font;}"
            ))
            btn_options.setFixedSize(55, 55)
            btn_options.clicked.connect(lambda _, f=arquivo, b=btn_options: self.show_options(f, b))

            layoutarquivo.addWidget(lbl_thumb)
            layoutarquivo.addWidget(lbl_arquivo)
            layoutarquivo.addStretch()
            layoutarquivo.addWidget(btn_options)  # sem self.

            self.files_layout.addWidget(item_widget)  # adiciona o WIDGET, não o layout

        self.files_layout.addStretch()
        self.atualizar_disco()

    def on_thumb_pronta(self, arquivo, caminho_thumb):
        """Chamado quando um ThumbnailWorker termina de gerar a thumbnail."""
        lbl = self.thumb_labels.get(arquivo)
        if lbl is not None:
            try:
                lbl.setPixmap(QPixmap(caminho_thumb))
            except RuntimeError:
                pass  # o label pode já ter sido destruído se a tela foi trocada

    def _item_navegacao(self, texto, destino, apagavel=False):
        w = ClickableItem()
        w.clicked.connect(lambda d=destino: self.entrar_pasta(d))

        lay = QHBoxLayout(w)
        lay.setSpacing(0)

        icone = QLabel("⛀")
        icone.setFixedSize(125, 75)
        icone.setAlignment(Qt.AlignCenter)
        icone.setFont(fonte(22))
        icone.setStyleSheet("background:transparent; border:none;")

        nome = QLabel(texto)
        nome.setFont(fonte(12))
        nome.setStyleSheet("background:transparent; border:none;")

        lay.addWidget(icone)
        lay.addWidget(nome)
        lay.addStretch()

        if apagavel:
            btn_x = QPushButton("✕")
            btn_x.setFont(fonte(12))
            btn_x.setFocusPolicy(NO_FOCUS)
            btn_x.setFixedSize(55, 55)
            btn_x.setStyleSheet(qss(
                "QPushButton{background:$PrincipalMedio;border:2px solid $Principal;"
                "border-radius:10px;color:$Font;}"
                "QPushButton:pressed{background:$Perigo; border:2px solid $Font;}"
            ))
            btn_x.clicked.connect(
                lambda _, d=destino: self.confirmar_apagar_pasta(d))
            lay.addWidget(btn_x)

        return w

    def entrar_pasta(self, destino):
        destino = os.path.realpath(destino)
        if destino != self.raiz and not destino.startswith(self.raiz + os.sep):
            return
        self.pasta_atual = destino
        self.lbl_titulo.setText(
            "Arquivos Gravados" if destino == self.raiz
            else os.path.relpath(destino, self.raiz)
        )
        self.load_files()
        self.scroll_area.verticalScrollBar().setValue(0)

    def show_options(self, arquivo, botao=None):
        escolher(arquivo,
                 [("➤   Mover", "mover"),
                  ("✐   Renomear", "renomear"),
                  ("X   Deletar", "deletar")],
                 self, lambda v: self._acao_arquivo(v, arquivo))

    def _acao_arquivo(self, acao, arquivo):
        if acao == "mover":
            self.move_file(arquivo)
        elif acao == "renomear":
            self.rename_file(arquivo)
        elif acao == "deletar":
            self.delete_file(arquivo)

    def rename_file(self, arquivo):
        caminho = os.path.join(self.pasta_atual, arquivo)
        ov = RenameOverlay(caminho, self)
        ov.renomeado.connect(self._apos_renomear)
        ov.setGeometry(self.rect())
        ov.show()
        ov.raise_()
        ov.edit.setFocus()

    def _apos_renomear(self, antigo, novo):
        self._sujo = True
        self.salvar_scroll()
        self.load_files()
        self.restaurar_scroll()

    def move_file(self, arquivo):
        caminho = os.path.join(self.pasta_atual, arquivo)
        ov = MoveOverlay(caminho, self)
        ov.movido.connect(self._apos_mover)
        ov.setGeometry(self.rect())
        ov.show()
        ov.raise_()

    def _apos_mover(self, origem, destino):
        self._sujo = True
        self.salvar_scroll()
        self.load_files()
        self.restaurar_scroll()

    def delete_file(self, nome_arquivo):
        caminho = os.path.join(self.pasta_atual, nome_arquivo)
        if os.path.exists(caminho):
            os.remove(caminho)

        caminho_thumb = thumb_de(caminho)
        if os.path.exists(caminho_thumb):
            os.remove(caminho_thumb)

        self._sujo = True
        self.load_files()

    def confirmar_apagar_pasta(self, caminho):
        n = sum(len(fs) for _, _, fs in os.walk(caminho))
        ov = ConfirmOverlay(
            "Excluir pasta",
            f"A pasta “{os.path.basename(caminho)}” e os {n} arquivo(s) "
            f"dentro dela serão apagados. Não dá para desfazer.",
            self)
        ov.confirmado.connect(lambda c=caminho: self.apagar_pasta(c))
        ov.setGeometry(self.rect())
        ov.show()
        ov.raise_()

    def apagar_pasta(self, caminho):
        # limpa as thumbs dos arquivos de dentro antes, senão viram lixo órfão
        for base, _, arqs in os.walk(caminho):
            for nome in arqs:
                t = thumb_de(os.path.join(base, nome))
                if os.path.exists(t):
                    try:
                        os.remove(t)
                    except OSError:
                        pass
        try:
            shutil.rmtree(caminho)
        except OSError as e:
            self.lbl_titulo.setText(f"Erro: {e.strerror}")
            return

        self._sujo = True
        self.salvar_scroll()
        self.load_files()
        self.restaurar_scroll()

    def voltar(self):
        if self.parent_app:
            self.parent_app.tela_principal()

    @staticmethod
    def _fmt_bytes(n):
        for u in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or u == "TB":
                return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
            n /= 1024

    def atualizar_disco(self):
        try:
            uso = shutil.disk_usage(self.raiz)
        except OSError:
            self.lbl_tamanho.setText("")
            return
        usado = uso.total - uso.free
        pct = usado * 100 / uso.total
        self.lbl_tamanho.setText(
            f"{self._fmt_bytes(usado)} / {self._fmt_bytes(uso.total)}"
        )
        self.lbl_tamanho.setStyleSheet(
            f"color:{C['NegativoAlto'] if pct >= 90 else C['FontFraca']};"
        )


# Confirmar

class ConfirmOverlay(QWidget):
    confirmado = Signal()

    def __init__(self, titulo, mensagem, parent, rotulo_ok="Excluir"):
        super().__init__(parent)
        self.setGeometry(parent.rect())
        self.setStyleSheet("background:transparent;")

        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(min(520, max(320, parent.width() - 60)))
        card.setStyleSheet(qss(
            "#card{background:$Cartao;border:2px solid $PrincipalAlto;border-radius:16px;}"
            "QLabel{color:$Font;}"
        ))
        col = QVBoxLayout(card)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(12)

        lbl_t = QLabel(titulo)
        lbl_t.setFont(fonte(15, True))
        col.addWidget(lbl_t)

        lbl_m = QLabel(mensagem)
        lbl_m.setWordWrap(True)
        lbl_m.setStyleSheet(qss("color:$FontMedia;font-size:15px;"))
        col.addWidget(lbl_m)

        lin = QHBoxLayout()
        lin.setSpacing(10)
        b_x = botao_touch("Cancelar", 0, 58)
        b_x.clicked.connect(self._fechar)
        b_ok = botao_touch(rotulo_ok, 0, 58)
        b_ok.setStyleSheet(qss(
            "QPushButton{background:$Perigo;border:2px solid $Principal;"
            "border-radius:10px;color:$Font;padding:0 12px;}"
            "QPushButton:pressed{background:$PerigoAlto;border:2px solid $Font;}"
        ))
        b_ok.clicked.connect(self._ok)
        lin.addWidget(b_x, 1)
        lin.addWidget(b_ok, 1)
        col.addLayout(lin)

        raiz = QVBoxLayout(self)
        raiz.addStretch()
        meio = QHBoxLayout()
        meio.addStretch()
        meio.addWidget(card)
        meio.addStretch()
        raiz.addLayout(meio)
        raiz.addStretch()

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, 205))

    def _fechar(self):
        self.hide()
        self.deleteLater()

    def _ok(self):
        self.confirmado.emit()
        self._fechar()


# RENOMEAR

class RenameOverlay(QWidget):
    renomeado = Signal(str, str)          # caminho_antigo, caminho_novo

    def __init__(self, caminho, parent):
        super().__init__(parent)
        self.caminho_thumb = thumb_de(caminho)
        self.caminho = caminho
        self.pasta   = os.path.dirname(caminho)
        self.nome, self.ext = os.path.splitext(os.path.basename(caminho))

        self.setGeometry(parent.rect())   # cobre a tela toda
        self.setStyleSheet("background:transparent;")
        self._montar()

    # ---------------------------------------------------- fundo escuro
    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 205))

    # ---------------------------------------------------- construção
    def _montar(self):
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(min(600, parent_w := self.width() - 40))
        card.setStyleSheet(qss(
            "#card{background:$Cartao;border:2px solid $PrincipalAlto;border-radius:16px;}"
            "QLabel{color:$Font;}"
            "QLineEdit{background:$PrincipalBaixo;border:2px solid $PrincipalAlto;border-radius:10px;"
            "color:$Font;padding:8px 12px;font-size:22px;}"
            "QLineEdit:focus{border:2px solid $Font;}"
        ))
        col = QVBoxLayout(card)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(12)

        titulo = QLabel("Renomear")
        titulo.setFont(fonte(15, True))
        col.addWidget(titulo)

        # ---- linha do campo + botão de teclado
        linha = QHBoxLayout()
        linha.setSpacing(8)

        self.edit = QLineEdit(self.nome)
        self.edit.setFocusPolicy(Qt.ClickFocus)
        self.edit.selectAll()
        linha.addWidget(self.edit, 1)

        lbl_ext = QLabel(self.ext)
        lbl_ext.setFont(fonte(14))
        lbl_ext.setStyleSheet(qss("color:$FontFraca;"))
        linha.addWidget(lbl_ext)

        self.btn_kb = botao_touch("⌨", 55, 55, destaque=False)
        self.btn_kb.setCheckable(True)
        
        linha.addWidget(self.btn_kb)

        col.addLayout(linha)

        self.aviso = QLabel("")
        self.aviso.setStyleSheet(qss("color:$NegativoAlto;font-size:14px;"))
        col.addWidget(self.aviso)

        # ---- teclado virtual (escondido)
        self.teclado = TecladoVirtual(self.edit)
        self.btn_kb.toggled.connect(self.teclado.setVisible)
        self.btn_kb.setChecked(True)
        col.addWidget(self.teclado)

        # ---- ações
        acoes = QHBoxLayout()
        acoes.setSpacing(10)
        b_cancel = botao_touch("Cancelar", 0, 58, destaque=False)
        b_cancel.clicked.connect(self._fechar)
        b_ok = botao_touch("Salvar", 0, 58, destaque=True)
        b_ok.clicked.connect(self._salvar)
        acoes.addWidget(b_cancel, 1)
        acoes.addWidget(b_ok, 1)
        col.addLayout(acoes)

        # ---- centraliza o card
        raiz = QVBoxLayout(self)
        raiz.addStretch()
        meio = QHBoxLayout()
        meio.addStretch()
        meio.addWidget(card)
        meio.addStretch()
        raiz.addLayout(meio)
        raiz.addStretch()

    # ---------------------------------------------------- interações

    def _erro(self, msg):
        self.aviso.setText(msg)

    def _fechar(self):
        self.hide()
        self.deleteLater()

    def _salvar(self):
        novo = self.edit.text().strip()
        if not novo:
            return self._erro("Digite um nome.")
        if re.search(r'[\\/:*?"<>|]', novo):
            return self._erro('Não pode usar  \\ / : * ? " < > |')

        destino = os.path.join(self.pasta, novo + self.ext)
        if os.path.abspath(destino) == os.path.abspath(self.caminho):
            return self._fechar()
        if os.path.exists(destino):
            return self._erro("Já existe um arquivo com esse nome.")

        try:
            os.rename(self.caminho, destino)
        except OSError as e:
            return self._erro(f"Erro: {e.strerror}")

        # thumb é secundária: se falhar, só é regerada depois
        destino_thumb = thumb_de(destino)
        try:
            if os.path.exists(self.caminho_thumb):
                os.replace(self.caminho_thumb, destino_thumb)
        except OSError:
            pass

        self.renomeado.emit(self.caminho, destino)
        self._fechar()


# Mover

class MoveOverlay(QWidget):
    movido = Signal(str, str)          # origem, destino

    def __init__(self, caminho, parent):
        super().__init__(parent)
        self.caminho = os.path.realpath(caminho)
        self.nome = os.path.basename(self.caminho)

        self.rotulo  = "Armazenamento: Interno"
        self.raiz    = os.path.realpath(PASTA_SAIDA)
        self.destino = self.raiz

        self.setGeometry(parent.rect())
        self.setStyleSheet("background:transparent;")
        self._montar()
        self._atualizar_barras()

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), QColor(0, 0, 0, 205))

    # ------------------------------------------------------ construção
    def _montar(self):
        card = QFrame()
        card.setObjectName("card")
        card.setFixedWidth(min(600, parent_w := self.width() - 40))
        card.setStyleSheet(qss(
            "#card{background:$Cartao;border:2px solid $PrincipalAlto;border-radius:16px;}"
            "QLabel{color:$Font;}"
            "QLineEdit{background:$PrincipalBaixo;border:2px solid $PrincipalAlto;border-radius:10px;"
            "color:$Font;padding:8px 12px;font-size:22px;}"
        ))
        
        col = QVBoxLayout(card)
        col.setContentsMargins(20, 18, 20, 18)
        col.setSpacing(10)

        self.titulo = QLabel("Mover")
        self.titulo.setFont(fonte(15, True))
        col.addWidget(self.titulo)

        self.lbl_arq = QLabel(self.nome)
        self.lbl_arq.setStyleSheet(qss("color:$FontFraca;font-size:14px;"))
        col.addWidget(self.lbl_arq)

        self.barra_armazem = botao_touch("", 0, 54)
        self.barra_armazem.clicked.connect(self._menu_armazem)
        col.addWidget(self.barra_armazem)

        self.barra_pasta = botao_touch("", 0, 54)
        self.barra_pasta.clicked.connect(self._menu_pasta)
        col.addWidget(self.barra_pasta)

        self.aviso = QLabel("")
        self.aviso.setStyleSheet(qss("color:$NegativoAlto;font-size:14px;"))
        col.addWidget(self.aviso)

        # ---- painel "criar pasta" (escondido)
        self.painel_nova = QWidget()
        pn = QVBoxLayout(self.painel_nova)
        pn.setContentsMargins(0, 0, 0, 0)
        pn.setSpacing(8)

        self.edit_nova = QLineEdit()
        self.edit_nova.setFocusPolicy(Qt.ClickFocus)
        linha_nova = QHBoxLayout()
        linha_nova.setSpacing(8)
        linha_nova.addWidget(self.edit_nova, 1)

        self.btn_kb_nova = botao_touch("⌨", 55, 55)
        self.btn_kb_nova.setCheckable(True)
        linha_nova.addWidget(self.btn_kb_nova)
        pn.addLayout(linha_nova)

        self.teclado_nova = TecladoVirtual(self.edit_nova)
        self.teclado_nova.setVisible(False)
        self.btn_kb_nova.toggled.connect(self.teclado_nova.setVisible)
        pn.addWidget(self.teclado_nova)

        lin = QHBoxLayout()
        lin.setSpacing(8)
        b_x = botao_touch("Cancelar", 0, 48)
        b_x.clicked.connect(lambda: self._modo_nova(False))
        b_c = botao_touch("Criar", 0, 48, destaque=True)
        b_c.clicked.connect(self._criar_pasta)
        lin.addWidget(b_x, 1)
        lin.addWidget(b_c, 1)
        pn.addLayout(lin)

        self.painel_nova.setVisible(False)
        col.addWidget(self.painel_nova)

        # ---- ações
        self.painel_acoes = QWidget()
        acoes = QHBoxLayout(self.painel_acoes)
        acoes.setContentsMargins(0, 0, 0, 0)
        acoes.setSpacing(10)
        b_cancel = botao_touch("Cancelar", 0, 58)
        b_cancel.clicked.connect(self._fechar)
        b_ok = botao_touch("Confirmar", 0, 58, destaque=True)
        b_ok.clicked.connect(self._confirmar)
        acoes.addWidget(b_cancel, 1)
        acoes.addWidget(b_ok, 1)
        col.addWidget(self.painel_acoes)

        raiz = QVBoxLayout(self)
        raiz.addStretch()
        meio = QHBoxLayout()
        meio.addStretch()
        meio.addWidget(card)
        meio.addStretch()
        raiz.addLayout(meio)
        raiz.addStretch()

    def _atualizar_barras(self):
        self.barra_armazem.setText("⛁   " + self.rotulo)
        rel = os.path.relpath(self.destino, self.raiz)
        self.barra_pasta.setText("⛀   " + ("/" if rel == "." else rel))

    # ------------------------------------------------------ barra 1
    def _menu_armazem(self):
        itens = [(f"{r}   ({FilesWindow._fmt_bytes(l)} livres)", (r, c))
                for r, c, l, mp, externo in armazens()]
        escolher("Armazenamento", itens, self, self._set_armazem)

    def _set_armazem(self, par):
        rotulo, caminho = par
        try:
            garantir_padrao(caminho)
        except OSError as e:
            return self._erro(f"Sem acesso: {e.strerror}")
        self.rotulo  = "Armazenamento: " + rotulo
        self.raiz    = os.path.realpath(caminho)
        self.destino = self.raiz
        self.aviso.setText("")
        self._atualizar_barras()

    # ------------------------------------------------------ barra 2
    def _menu_pasta(self):
        itens = []
        if self.destino != self.raiz:
            itens.append(("⬅   voltar uma pasta", os.path.dirname(self.destino)))
        try:
            with os.scandir(self.destino) as it:
                for e in sorted(it, key=lambda e: e.name.lower()):
                    if e.is_dir() and not e.name.startswith("."):
                        itens.append(("⛀   " + e.name, e.path))
        except OSError as e:
            return self._erro(f"Erro: {e.strerror}")
        itens.append(("＋   Criar pasta", None))

        escolher("Pasta", itens, self, self._set_pasta)

    def _set_pasta(self, caminho):
        if caminho is None:
            self.edit_nova.clear()
            self._modo_nova(True)
        else:
            self._ir(caminho)

    def _ir(self, destino):
        destino = os.path.realpath(destino)
        if destino != self.raiz and not destino.startswith(self.raiz + os.sep):
            return                      # trava: não sai do armazenamento
        self.destino = destino
        self.aviso.setText("")
        self._atualizar_barras()

    def _criar_pasta(self):
        nome = self.edit_nova.text().strip()
        if not nome:
            return self._erro("Digite um nome.")
        if re.search(r'[\\/:*?"<>|]', nome):
            return self._erro('Não pode usar  \\ / : * ? " < > |')
        novo = os.path.join(self.destino, nome)
        try:
            os.makedirs(novo, exist_ok=True)
        except OSError as e:
            return self._erro(f"Erro: {e.strerror}")
        self._modo_nova(False)
        self._ir(novo)

    def _modo_nova(self, on):
        self.btn_kb_nova.setChecked(on)
        self.titulo.setText("Criar pasta" if on else "Mover")
        for w in (self.lbl_arq, self.barra_armazem,
                  self.barra_pasta, self.painel_acoes):
            w.setVisible(not on)
        self.painel_nova.setVisible(on)
        self.aviso.setText("")
        if on:
            self.edit_nova.setFocus()

    # ------------------------------------------------------ ações
    def _erro(self, msg):
        self.aviso.setText(msg)

    def _fechar(self):
        self.hide()
        self.deleteLater()

    def _confirmar(self):
        if self.destino == os.path.dirname(self.caminho):
            return self._erro("O arquivo já está nessa pasta.")

        destino = os.path.join(self.destino, self.nome)
        if os.path.exists(destino):
            return self._erro("Já existe um arquivo com esse nome lá.")

        try:
            shutil.move(self.caminho, destino)
        except OSError as e:
            return self._erro(f"Erro: {e.strerror}")

        try:
            t_ant = thumb_de(self.caminho)
            if os.path.exists(t_ant):
                os.replace(t_ant, thumb_de(destino))
        except OSError:
            pass

        self.movido.emit(self.caminho, destino)
        self._fechar()


# Player

class ViewerWindow(QWidget):
    IPC_PATH = "/tmp/handycam_mpv.sock"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self.proc = None
        self.caminho = None
        self.pausado = False
        self._dur = None

        self.setStyleSheet(qss("background:$Preto; color:$Font;"))

        self.lbl_nome = QLabel("")
        self.lbl_nome.setFont(fonte(12))
        self.lbl_nome.setStyleSheet(qss("color:$FontFraca; padding:4px;"))

        # host do vídeo: QWidget puro, janela X11 nativa pro mpv
        self.video_host = QWidget()
        self.video_host.setAttribute(Qt.WA_NativeWindow, True)
        self.video_host.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.video_host.setStyleSheet(qss("background:$Preto;"))
        self.video_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_host.setMinimumHeight(200)

        # área de foto (continua QLabel, que sabe escalar pixmap)
        self.area = QLabel()
        self.area.setAlignment(ALIGN_CENTER)
        self.area.setStyleSheet(qss("background:$Preto;"))
        self.area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # troca entre os dois conforme o tipo de arquivo
        self.midia = QStackedWidget()
        self.midia.addWidget(self.area)         # índice 0 = foto
        self.midia.addWidget(self.video_host)   # índice 1 = vídeo
        self.midia.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # --- timestamp ---
        self.lbl_tempo = QLabel("00:00 / 00:00")
        self.lbl_tempo.setFont(fonte(13))
        self.lbl_tempo.setAlignment(ALIGN_CENTER)
        self.lbl_tempo.setStyleSheet(qss("color:$Destaque; padding:2px;"))

        # --- controles ---
        estilo_ctrl = qss(
            "QPushButton{background:$DestaqueBaixo;border:2px solid $Destaque;"
            "border-radius:10px;color:$Destaque;padding:6px 10px;}"
            "QPushButton:pressed{background:$DestaqueMedio;}"
        )

        self.btn_play = QPushButton("❚❚")
        self.btn_ret = QPushButton("<< 5s")
        self.btn_avc = QPushButton("5s >>")
        self.btn_vol_menos = QPushButton("V-")
        self.btn_vol_mais = QPushButton("V+")

        for b in (self.btn_play, self.btn_ret, self.btn_avc,
                  self.btn_vol_menos, self.btn_vol_mais):
            b.setMinimumHeight(56)
            b.setFont(fonte(14, True))
            b.setFocusPolicy(NO_FOCUS)
            b.setStyleSheet(estilo_ctrl)

        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_ret.clicked.connect(lambda: self.seek(-5))
        self.btn_avc.clicked.connect(lambda: self.seek(5))
        self.btn_vol_menos.clicked.connect(lambda: self.mudar_volume(-10))
        self.btn_vol_mais.clicked.connect(lambda: self.mudar_volume(10))


        # timer que atualiza o timestamp
        self.timer_pos = QTimer(self)
        self.timer_pos.timeout.connect(self.atualizar_tempo)

        btn_voltar = QPushButton("Voltar")
        btn_voltar.setMinimumHeight(56)
        btn_voltar.setFont(fonte(15))
        btn_voltar.setFocusPolicy(NO_FOCUS)
        btn_voltar.setStyleSheet(qss(
            "QPushButton{background:$PrincipalMedio;border:2px solid $Font;"
            "border-radius:10px;color:$Font;padding:6px 18px;}"
            "QPushButton:pressed{background:$Principal;}"
        ))
        btn_voltar.clicked.connect(self.voltar)

        # topo: nome + tempo na mesma linha
        linha_topo = QHBoxLayout()
        linha_topo.addWidget(self.lbl_nome)
        linha_topo.addStretch()
        linha_topo.addWidget(self.lbl_tempo)

        # controles: tudo numa faixa só, incluindo o Voltar
        linha_ctrl = QHBoxLayout()
        linha_ctrl.setSpacing(4)
        linha_ctrl.addWidget(self.btn_ret)
        linha_ctrl.addWidget(self.btn_play)
        linha_ctrl.addWidget(self.btn_avc)
        linha_ctrl.addWidget(self.btn_vol_menos)
        linha_ctrl.addWidget(self.btn_vol_mais)
        linha_ctrl.addWidget(btn_voltar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addLayout(linha_topo)
        layout.addWidget(self.midia, 1)
        layout.addLayout(linha_ctrl)

    # ---------- API usada pelo Handycam ----------
    def abrir(self, caminho):
        self.caminho = caminho
        self.lbl_nome.setText(os.path.basename(caminho))
        if caminho.lower().endswith(".jpg"):
            self.midia.setCurrentIndex(0)
            for w in (self.btn_play, self.btn_ret, self.btn_avc,
                      self.btn_vol_menos, self.btn_vol_mais,
                      self.lbl_tempo):
                w.setVisible(False)
            self._mostrar_imagem(caminho)
        else:
            self.midia.setCurrentIndex(1)
            for w in (self.btn_play, self.btn_ret, self.btn_avc,
                      self.btn_vol_menos, self.btn_vol_mais,
                      self.lbl_tempo):
                w.setVisible(True)
            QTimer.singleShot(250, lambda: self._tocar_video(caminho))

    def _mostrar_imagem(self, caminho):
        pix = QPixmap(caminho)
        if pix.isNull():
            self.area.setText("Não foi possível abrir a imagem.")
            return
        self.area.setPixmap(pix.scaled(self.area.size(), KEEP_AR, SMOOTH_TRANSFORM))

    def _tocar_video(self, caminho):
        self.parar()
        if os.path.exists(self.IPC_PATH):
            os.remove(self.IPC_PATH)

        # força o layout a calcular o tamanho real antes de entregar
        # a janela pro mpv — dentro do QStackedWidget a página só ganha
        # altura definitiva depois de virar a atual E rodar um ciclo.
        self.layout().activate()
        self.video_host.winId()
        QApplication.processEvents()

        if self.video_host.height() < 100:
            for tentativa in range(20):
                QTimer.singleShot(150, lambda: self._tocar_video(caminho))
            else:
                print("video_host não dimensionou:", self.video_host.size())
            return

        wid = int(self.video_host.winId())

        if self.area.height() < 100:
            # ainda não dimensionou: tenta de novo no próximo ciclo
            QTimer.singleShot(150, lambda: self._tocar_video(caminho))
            return

        try:
            self.proc = subprocess.Popen([
                "mpv",
                f"--wid={wid}",
                "--vo=x11",
                f"--input-ipc-server={self.IPC_PATH}",
                "--no-terminal",
                "--no-osc",
                "--no-input-default-bindings",
                "--keep-open=yes",
                "--loop-file=no",
                caminho,
            ])
            self.pausado = False
            self._dur = None
            self.btn_play.setText("❚❚")
            self.timer_pos.start(500)
            QTimer.singleShot(300, self._sincronizar_volume)
        except FileNotFoundError:
            self.area.setText("mpv não instalado.\nsudo apt install mpv")
    
    def _sincronizar_volume(self):
        vol = self._mpv_get("volume")

    def _mpv_cmd(self, comando):
        """Envia comando e retorna a resposta (dict) ou None se falhar."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.4)
                s.connect(self.IPC_PATH)
                s.send((json.dumps({"command": comando}) + "\n").encode())
                buf = b""
                # o mpv pode mandar eventos assíncronos junto; pega a 1ª
                # linha que tenha "error" (essa é a resposta ao comando)
                for _ in range(10):
                    buf += s.recv(4096)
                    for linha in buf.split(b"\n"):
                        if not linha.strip():
                            continue
                        try:
                            resp = json.loads(linha)
                        except json.JSONDecodeError:
                            continue
                        if "error" in resp:
                            return resp
                return None
        except (OSError, socket.timeout, ValueError):
            return None

    def _mpv_get(self, prop):
        resp = self._mpv_cmd(["get_property", prop])
        if resp and resp.get("error") == "success":
            return resp.get("data")
        return None

    def toggle_play(self):
        if self.proc is None:
            return
        if self._mpv_cmd(["cycle", "pause"]):
            self.pausado = not self.pausado
            self.btn_play.setText("▶" if self.pausado else "❚❚")

    def parar(self):
        self.timer_pos.stop()
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.proc = None
        self.pausado = False

    def hideEvent(self, event):
        self.parar()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.caminho and self.caminho.lower().endswith(".jpg"):
            self._mostrar_imagem(self.caminho)

    def voltar(self):
        self.parar()
        if self.parent_app:
            self.parent_app.abrir_files()
    
    @staticmethod
    def _fmt(seg):
        if seg is None:
            return "--:--"
        seg = int(seg)
        return f"{seg // 60:02d}:{seg % 60:02d}"

    def seek(self, delta):
        self._mpv_cmd(["seek", delta, "relative"])
        self.atualizar_tempo()   # feedback imediato, sem esperar o timer

    def mudar_volume(self, delta):
        vol = self._mpv_get("volume")
        if vol is None:
            return
        novo = max(0, min(130, int(vol) + delta))
        self._mpv_cmd(["set_property", "volume", novo])

    def atualizar_tempo(self):
        if self._dur is None:
            self._dur = self._mpv_get("duration")
        pos = self._mpv_get("time-pos")
        self.lbl_tempo.setText(f"{self._fmt(pos)} / {self._fmt(self._dur)}")

# ----------------------------------------------------------------------
def main(app=None, splash=None):
    """
    app/splash: vêm do handyload.py, que já criou o QApplication e
    mostrou a tela de carregamento antes de importar este módulo — a
    parte pesada, cv2/numpy/picamera2/Qt. Este main() não cria mais seu
    próprio splash (isso é responsabilidade só do handyload.py — tinha
    duas telas de carregamento se rodasse os dois); ele só fecha o
    splash recebido e não chama sys.exit()/EXEC() aqui, quem chamou
    cuida disso.
    Rodando `python handycam.py` direto (sem handyload.py), app/splash
    vêm None: cria a QApplication na hora e segue sem tela de
    carregamento nenhuma.
    """
    tuning = Picamera2.load_tuning_file(TUNING)
    picam2 = Picamera2(tuning=tuning)
    dur = int(1_000_000 / FPS_DIA)   # µs por frame: 60 FPS -> 16666
    picam2.configure(picam2.create_video_configuration(
        main={"size": RESOLUCAO, "format": "XRGB8888"},
        controls={
            "FrameRate": FPS_DIA,
            "FrameDurationLimits": (dur, dur),
        },
    ))

    criou_app_aqui = app is None
    if criou_app_aqui:
        app = QApplication(sys.argv)

    # Rodando pelo handyload.py a fonte ja foi carregada la; carregar()
    # e idempotente, entao aqui so tem efeito no `python3 handycam.py`.
    carregar_fonte()

    win = Handycam(picam2)
    win.resize(800, 480)
    win.showFullScreen()

    # IMPORTANTE: só fecha o splash DEPOIS de mostrar a janela nova, e com
    # um pequeno intervalo entre as duas coisas.
    #
    # Se fechássemos o splash ANTES de criar a janela, existe um instante
    # sem nenhuma das duas janelas cobrindo a tela, e o desktop por trás
    # aparece rapidinho (pisca). Mostrando a janela nova primeiro (ela
    # fica atrás do splash, escondida) e só depois fechando o splash, as
    # duas ficam sobrepostas o tempo todo — nunca existe um vão sem
    # cobertura.
    #
    # Só que showFullScreen() não garante que a janela já foi realmente
    # pintada na tela — só manda o pedido pro compositor (XWayland/labwc),
    # que processa isso de forma assíncrona. Se fechássemos o splash logo
    # em seguida sem esperar, corre o risco do splash sumir antes da
    # janela nova estar pronta de verdade, e o desktop escapar por trás
    # nesse instante. Por isso rodamos o loop de eventos do Qt por um
    # tempo mínimo (não um time.sleep(), que travaria o Qt e impediria a
    # janela de ser desenhada) antes de fechar o splash.
    if splash is not None:
        loop = QEventLoop()
        QTimer.singleShot(150, loop.quit)
        loop.exec_()
        splash.close()

    picam2.start()

    if criou_app_aqui:
        sys.exit(EXEC(app))
    # se o app veio de fora (handyload.py), quem chama cuida do EXEC/sys.exit —
    # mas precisa GUARDAR o retorno (win) numa variável que sobreviva até lá,
    # senão o Python coleta a janela assim que este main() retornar (win é só
    # uma variável local aqui) e o preview quebra com "wrapped C/C++ object
    # ... has been deleted" assim que a thread do picamera2 tentar desenhar
    # o próximo frame.
    return win


if __name__ == "__main__":
    main()