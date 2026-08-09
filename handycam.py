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
import json
import subprocess
import socket
from gpiozero import DigitalOutputDevice

# Força XCB (X11 via XWayland) em vez de Wayland — ver explicação completa
# no handyload.py. Necessário aqui também porque, rodando `python
# handycam.py` direto (sem o loader), é este main() que cria a
# QApplication, e isso tem que ser definido antes disso acontecer.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

import cv2
import numpy as np

from picamera2 import Picamera2, MappedArray # type: ignore
from picamera2.previews.qt import QGlPicamera2 # type: ignore
from picamera2.encoders import H264Encoder # type: ignore
from picamera2.outputs import FfmpegOutput # type: ignore

# ----------------------------------------------------------------------
# IMPORTANTE: usar o MESMO binding Qt que o picamera2 usa internamente.
# Misturar PySide6 com PyQt gera "Must construct a QApplication before a
# QWidget". O import do QGlPicamera2 acima já carregou o binding certo —
# aqui só descobrimos qual foi.
# ----------------------------------------------------------------------

from qt import *

from handyupdate import AtualizadorWorker

import efeitos


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
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
                l, a = cfg.get("resolucao", padrao_res)
                fps = cfg.get("fps", padrao_fps)
                return l, a, fps
        except (json.JSONDecodeError, OSError, ValueError, TypeError, KeyError):
            pass
    return padrao_res[0], padrao_res[1], padrao_fps

_L, _A, FPS_DIA = _carregar_config_inicial()
RESOLUCAO = (_L, _A)                      # size real usado no configure()
RESOLUCAO_LABEL = _label_resolucao(_A)     # só pra exibição (VGA/HD/FHD)
FPS_NOITE = FPS_DIA          # menos fps = mais tempo de exposição = mais luz
GANHO_NOITE = 8.0
BITRATE = 2_000_000
TUNING = "ov5647_noir.json"
PASTA_SAIDA = "./media"       # ex: "/media/usb" para gravar em pendrive
PASTA_THUMBS = os.path.join(PASTA_SAIDA, ".thumbs")  # cache das thumbnails
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
def tint_green(request):
    with MappedArray(request, "main") as m:
        arr = m.array                       # XRGB8888 -> 4 canais
        gray = cv2.cvtColor(arr, cv2.COLOR_BGRA2GRAY)
        arr[:, :, 0] = (gray * 0.60).astype(np.uint8)    # azul
        arr[:, :, 1] = gray                              # verde
        arr[:, :, 2] = (gray * 0.50).astype(np.uint8)    # vermelho


# ----------------------------------------------------------------------
# Janela principal
# ----------------------------------------------------------------------
class Handycam(QWidget):
    def __init__(self, picam2):
        super().__init__()
        self.picam2 = picam2
        self.picam2.post_callback = efeitos.post_callback
        self.noite = False
        self.gravando = False
        self.encoder = None
        self.t0 = 0.0
        self.blink = False
        self.icone_foto = False   # flash do icone de camera sobre o preview
        self.telas = 0


        self.setWindowTitle("PVL-PiHCAM")
        self.setStyleSheet("background:#111; color:#eee;")

        self.preview = QGlPicamera2(picam2, width=RESOLUCAO[0],
                                    height=RESOLUCAO[1], keep_ar=True)

        # O REC (ponto piscando + cronometro) nao fica mais na barra de
        # status: agora e desenhado por cima do preview em _atualiza_overlay().

        self.lbl_clock = QLabel("")
        self.lbl_clock.setFont(QFont("DejaVu Sans", 13))
        self.lbl_clock.setAlignment(ALIGN_LEFT)
        self.timer_relogio = QTimer(self)
        self.timer_relogio.timeout.connect(self._atualiza_relogio)
        self.timer_relogio.start(1000)          # milissegundos
        self._atualiza_relogio() 


        self.lbl_modo = QLabel("")
        self.lbl_modo.setFont(QFont("DejaVu Sans", 13))
        self.lbl_modo.setAlignment(ALIGN_RIGHT)
 
        self.lbl_info = QLabel(f"{RESOLUCAO[0]}x{RESOLUCAO[1]} {RESOLUCAO_LABEL} | FPS: {FPS_DIA if not self.noite else FPS_NOITE} ")
        self.lbl_info.setFont(QFont("DejaVu Sans", 13))
        self.lbl_info.setAlignment(ALIGN_RIGHT)
 
        
        self.btn_conf = QPushButton("⚙")
        self.btn_conf.setStyleSheet(
            "QPushButton{background:#222;border:2px solid #333;"
            "border-radius:10px;color:#fff;padding:6px 18px;}"
            "QPushButton:pressed{background:#333; border:2px solid #fff;}"
            "width:50px;height:50px;"
        )
        self.btn_conf.setFont(QFont("DejaVu Sans", 15, FONT_BOLD))
        self.btn_conf.setFocusPolicy(NO_FOCUS)
 
        self.btn_files = QPushButton("≡")
        self.btn_files.setStyleSheet(
            "QPushButton{background:#222;border:2px solid #333;"
            "border-radius:10px;color:#fff;padding:6px 18px;}"
            "QPushButton:pressed{background:#333; border:2px solid #fff;}"
            "width:50px;height:50px;"
        )
        self.btn_files.setFont(QFont("DejaVu Sans", 13, FONT_BOLD))
        self.btn_files.setFocusPolicy(NO_FOCUS)

        status = QHBoxLayout()
        status.addWidget(self.lbl_clock)
        status.addStretch()
        status.addWidget(self.lbl_modo)
        status.addWidget(self.lbl_info)
        status.addWidget(self.btn_files)
        status.addWidget(self.btn_conf)

        self.btn_rec = QPushButton("● REC")

        self.btn_photo = QPushButton(" FOTO")
        self.btn_photo.setIcon(self._icone_camera_btn("#5bf", 64))
        self.btn_photo.setIconSize(QSize(32, 24))

        self.btn_modo = QPushButton("☾ NOITE")
        self.btn_sair = QPushButton("SAIR")

        for b in (self.btn_modo, self.btn_sair, self.btn_rec, self.btn_photo):
            b.setMinimumHeight(64)
            b.setFont(QFont("DejaVu Sans", 15, FONT_BOLD))
            b.setFocusPolicy(NO_FOCUS)

        self.btn_modo.setStyleSheet(
            "QPushButton{background:#121;border:2px solid #5f5;"
            "border-radius:10px;color:#5f5;padding:6px 18px;}"
            "QPushButton:pressed{background:#131;}"
        )
        self.btn_rec.setStyleSheet(
            "QPushButton{background:#211;border:2px solid #f33;"
            "border-radius:10px;color:#f33;padding:6px 18px;}"
            "QPushButton:pressed{background:#511;}"
        )

        self.btn_sair.setStyleSheet(
            "QPushButton{background:#222;border:2px solid #fff;"
            "border-radius:10px;color:#fff;padding:6px 18px;}"
            "QPushButton:pressed{background:#333;}"
        )
        self.btn_photo.setStyleSheet(
            "QPushButton{background:#112;border:2px solid #5bf;"
            "border-radius:10px;color:#5bf;padding:6px 18px;}"
            "QPushButton:pressed{background:#135;}"
        )

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
        efeitos.definir(None)
        self.picam2.set_controls({
            "AwbEnable": True,
            "AnalogueGain": 0.0,
            "Saturation": 1.0,
            "FrameRate": FPS_DIA,
        })
        self.noite = False
        self.btn_modo.setText("☾ NOITE")
        self.btn_modo.setStyleSheet(
            "QPushButton{background:#121;border:2px solid #5f5;"
            "border-radius:10px;color:#5f5;padding:6px 18px;}"
            "QPushButton:pressed{background:#141;}"
        )   
        self.lbl_modo.setText("☀  DIA")
        self.lbl_modo.setStyleSheet("color:#fd7; padding-right:6px;")

    def modo_noite(self):
        infra_red.on()
        self.picam2.set_controls({
            "AwbEnable": False,
            "ColourGains": (1.0, 1.0),
            "AnalogueGain": GANHO_NOITE,
            "Saturation": 0.0,
            "FrameRate": FPS_NOITE,
        })
        efeitos.definir("noite")
        self.noite = True
        self.btn_modo.setText("☀ DIA")
        self.btn_modo.setStyleSheet(
            "QPushButton{background:#321;border:2px solid #fd7;"
            "border-radius:10px;color:#fd7;padding:6px 18px;}"
            "QPushButton:pressed{background:#541;}"
        )
        self.lbl_modo.setText("☾  NOITE")
        self.lbl_modo.setStyleSheet("color:#5f5; padding-right:6px;")

    def toggle_modo(self):
        self.modo_dia() if self.noite else self.modo_noite()

    def toggle_rec(self):
        if self.gravando:
            self.picam2.stop_encoder()
            self.encoder = None
            self.gravando = False
            self.btn_rec.setText("● REC")
            self.btn_rec.setStyleSheet(
                "QPushButton{background:#211;border:2px solid #f33;"
                "border-radius:10px;color:#f33;padding:6px 18px;}"
                "QPushButton:pressed{background:#611;}"
            )
            self.blink = False
            self._atualiza_overlay()
            self.files_window.marcar_sujo()
        else:
            fps_atual = FPS_NOITE if self.noite else FPS_DIA
            nome = time.strftime(f"{PASTA_SAIDA}/handycam_video_%Y%m%d_%H%M%S.mp4")
            self.encoder = H264Encoder(bitrate=BITRATE, framerate=fps_atual)
            if AUDIO_DEVICE:
                saida = FfmpegOutput(nome, audio=True, audio_device=AUDIO_DEVICE,
                                     audio_codec="aac", audio_bitrate=AUDIO_BITRATE)
            else:
                saida = FfmpegOutput(nome)
            self.picam2.start_encoder(self.encoder, saida, name="main")
            self.gravando = True
            self.t0 = time.monotonic()
            self.blink = True
            self._atualiza_overlay()
            self.btn_rec.setText("■ STOP")
            self.btn_rec.setStyleSheet(
                "QPushButton{background:#211;border:2px solid #a11;"
                "border-radius:10px;color:#a11;padding:6px 18px;}"
                "QPushButton:pressed{background:#411;}"
            )
            print("Gravando:", nome)

    def take_photo(self):
        self._nome_foto = time.strftime(f"{PASTA_SAIDA}/handycam_foto_%Y%m%d_%H%M%S.jpg")
        self.btn_photo.setEnabled(False)
 
        # Feedback visual imediato: icone de camera azul no canto direito
        # do preview, some sozinho depois de 400 ms.
        self.icone_foto = True
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
    def _desenha_icone_camera(self, p, x, y, w=40, h=30, cor="#5bf"):
        """Mesmo desenho do CameraIcon, mas num QPainter ja existente."""
        cor = QColor(cor)
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

    def _icone_camera_btn(self, cor="#eee", tamanho=64):
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
            p.setFont(QFont("DejaVu Sans", 16, FONT_BOLD))
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
                p.setBrush(QColor("#f33"))
                p.drawEllipse(cx + 10, cy + (ch - 14) // 2, 14, 14)
 
            # cronometro (alterna o tom junto com o ponto)
            p.setPen(QColor("#f33") if self.blink else QColor("#a11"))
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
        self.setStyleSheet("background:#1b1b1b; color:#eee;")

        self.lbl = QLabel("Conectando...")
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setWordWrap(True)
        self.lbl.setFont(QFont("DejaVu Sans", 13))

        self.btn = QPushButton("OK")
        self.btn.setMinimumHeight(56)
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

    BTN_ON = ("QPushButton{background:#223;border:2px solid #5df;"
              "border-radius:10px;color:#fff;padding:6px 18px;}"
              "QPushButton:pressed{background:#333;}"
              "QPushButton:checked{background:#346;border:2px solid #7ef;color:#fff;}")
    
    BTN_OFF = ("QPushButton{background:#222;border:2px solid #333;"
        "border-radius:10px;color:#fff;padding:6px 18px;}"
        "QPushButton:pressed{background:#333;}"
        "QPushButton:checked{background:#444;border:2px solid #fff;color:#fff;}")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent

        self.setStyleSheet("background:#111; color:#eee;")

        self.config = self.carregar_config()
        self.config_inicial = dict(self.config)  # cópia pra comparar depois (aviso de "mudou")
        self.msgR = f'{self.config["resolucao"][0]}x{self.config["resolucao"][1]}'
        self.msgFPS = f'{self.config["fps"]} FPS'

        layout = QVBoxLayout()
        layout.setSpacing(14)
        layout.setContentsMargins(9, 9, 9, 9)

        lbl_titulo = QLabel("Configurações PVL-PiHCAM")
        lbl_titulo.setFont(QFont("DejaVu Sans", 15))

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
        self.scroll_area.setStyleSheet(
            "QScrollArea{border:none; background:#111; padding: 6px;}"
            "QScrollBar:vertical{background:#111; width:10px;}"
            "QScrollBar::handle:vertical{background:#fff;}"
        )

        self.config_container = QWidget()
        conteudo = QVBoxLayout(self.config_container)
        conteudo.setSpacing(14)
        conteudo.setContentsMargins(10, 10, 10, 10)
        self.config_container.setStyleSheet(
            "background:#111; border:1px solid #333; border-radius:5px; padding:10px;"
        )
        
        BTN_ON = ("QPushButton{background:#223;border:2px solid #5df;"
          "border-radius:10px;color:#fff;padding:6px 18px;}"
          "QPushButton:pressed{background:#333;}"
          "QPushButton:checked{background:#346;border:2px solid #7ef;color:#fff;}")

        BTN_OFF = ("QPushButton{background:#222;border:2px solid #333;"
           "border-radius:10px;color:#fff;padding:6px 18px;}"
           "QPushButton:pressed{background:#333;}"
           "QPushButton:checked{background:#444;border:2px solid #fff;color:#fff;}")

        lbl_sub1 = QLabel("Resolução e FPS")
        lbl_sub1.setFont(QFont("DejaVu Sans", 12))
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
            btn.setFont(QFont("DejaVu Sans", 12))
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

        if len(RESOLUCOES) % 3 and linha is not None:
            linha.addStretch()

        lbl_sub2 = QLabel("FPS (frames por segundo)")
        lbl_sub2.setFont(QFont("DejaVu Sans", 12))
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
            btn.setFont(QFont("DejaVu Sans", 12))
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

        if len(FPS_OPCOES) % 3 and linha is not None:
            linha.addStretch()

        lbl_sub3 = QLabel("Efeito de imagem")
        lbl_sub3.setFont(QFont("DejaVu Sans", 12))
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
            btn.setFont(QFont("DejaVu Sans", 12))
            btn.setFocusPolicy(NO_FOCUS)
            btn.setChecked(nome == atual)
            btn.setStyleSheet(BTN_ON if nome == atual else BTN_OFF)

            btn.clicked.connect(
                lambda checked, n=nome, b=btn: self.selecionar_efeito(n, b)
            )
            linha.addWidget(btn)
            self.botoes_efeito.append((btn, nome))

        if len(EFEITO_OPCOES) % 3 and linha is not None:
            linha.addStretch()


        conteudo.addStretch()
        self.config_container.setLayout(conteudo)
        self.scroll_area.setWidget(self.config_container)
        layout.addWidget(self.scroll_area, 1)   # ocupa o espaço restante, igual o FilesWindow

        # --- Aviso + botões fixos embaixo, fora do scroll ---
        self.lbl_aviso = QLabel("")
        self.lbl_aviso.setFont(QFont("DejaVu Sans", 9))
        self.lbl_aviso.setStyleSheet("color:#fd7;")
        layout.addWidget(self.lbl_aviso)

        linha_botoes = QHBoxLayout()

        btn_voltar = QPushButton("← Voltar")
        btn_voltar.setMinimumHeight(64)
        btn_voltar.setFont(QFont("DejaVu Sans", 15))
        btn_voltar.setFocusPolicy(NO_FOCUS)
        btn_voltar.setStyleSheet(
            "QPushButton{background:#222;border:2px solid #fff;"
            "border-radius:10px;color:#fff;padding:6px 18px;}"
            "QPushButton:pressed{background:#333;}"
        )
        btn_voltar.clicked.connect(self.voltar)

        btn_aplicar = QPushButton("↻ Aplicar e Reiniciar")
        btn_aplicar.setMinimumHeight(64)
        btn_aplicar.setFont(QFont("DejaVu Sans", 15))
        btn_aplicar.setFocusPolicy(NO_FOCUS)
        btn_aplicar.setStyleSheet(
            "QPushButton{background:#235;border:2px solid #5df;"
            "border-radius:10px;color:#5df;padding:6px 18px;}"
            "QPushButton:pressed{background:#346;}"
        )
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
        btn.setFont(QFont("DejaVu Sans", 15))
        btn.setFocusPolicy(NO_FOCUS)
        btn.setStyleSheet(
            "QPushButton{background:#222;border:2px solid #333;"
            "border-radius:10px;color:#fff;padding:6px 18px;}"
            "QPushButton:pressed{background:#346;border:2px solid #7ef;color:#fff;}"
        )
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
        self.setStyleSheet(self.styleSheet() + """
            QPushButton {
                background: #222;
                border: 2px solid #444;
                border-radius: 10px;
                padding: 6px 18px;
                text-align: left;
                padding-left: 14px;
            }
            QPushButton:hover {
                background: #2a2a2a;
            }
            QPushButton:checked {
                background: #0af;
                border: 2px solid #5df;
                color: #fff;
            }
        """)

    def atualizar_aviso(self):
        mudou = (
            self.config.get("resolucao") != self.config_inicial.get("resolucao")
            or self.config.get("fps") != self.config_inicial.get("fps")
        )
        if mudou:
            self.lbl_aviso.setText(
                f"Selecionado: {self.msgR} {self.msgFPS} — clique em "
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
            b.setStyleSheet(self.BTN_ON if n == nome else self.BTN_OFF)

        efeitos.definir(nome)                 # troca imediata, sem os.execv
        self.config["efeito"] = nome
        self.salvar_config()                  # o mesmo que grava ~/.handycam_config.json

    def marcar_selecionado(self):
        atual_res = self.config.get("resolucao")
        for btn, l, a in self.botoes_resolucao:
            btn.setChecked(atual_res == [l, a])

        atual_fps = self.config.get("fps")
        for btn, f in self.botoes_fps:
            btn.setChecked(atual_fps == f)


    def carregar_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    cfg = json.load(f)
                    cfg.setdefault("resolucao", [1280, 720])
                    cfg.setdefault("fps", 24)
                    return cfg
            except (json.JSONDecodeError, OSError):
                pass
        return {"resolucao": [1280, 720], "fps": 24}

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

    ESTILO_NORMAL = ("background:#222; border:1px solid #444; "
                     "border-radius:5px; margin:6px;")
    ESTILO_PRESS  = ("background:#333; border:1px solid #777; "
                     "border-radius:5px; margin:6px;")

    def __init__(self, parent=None):
        super().__init__(parent)
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


class FilesWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent

        self.setStyleSheet("background:#111; color:#eee;")
        layout = QVBoxLayout()
        lbl_titulo = QLabel("Arquivos Gravados")
        lbl_titulo.setFont(QFont("DejaVu Sans", 15))
        layout.addWidget(lbl_titulo)
        layout.setSpacing(14)
        layout.setContentsMargins(9, 9, 9, 9)

        # --- Área de scroll com a lista de arquivos ---
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.viewport().setAttribute(Qt.WA_AcceptTouchEvents, True)
        QScroller.grabGesture(self.scroll_area.viewport(),
                      QScroller.LeftMouseButtonGesture)
        self.scroll_area.setStyleSheet(
            "QScrollArea{border:none; background:#111;}"
            "QScrollBar:vertical{background:#111; width:10px;}"
            "QScrollBar::handle:vertical{background:#fff;}"
        )

        # Widget interno que vai conter a lista (o QScrollArea precisa de um único widget filho)
        self.files_container = QWidget()
        self.files_layout = QVBoxLayout(self.files_container)
        self.files_layout.setSpacing(5)
        self.files_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.files_container.setLayout(self.files_layout)
        self.files_container.setStyleSheet("background:#111; border:1px solid #333; border-radius:5px; padding:10px;")

        self.scroll_area.setWidget(self.files_container)
        layout.addWidget(self.scroll_area)
        # ------------------------------------------------

        btn_voltar = QPushButton("Voltar")
        btn_voltar.setMinimumHeight(64)
        btn_voltar.setFont(QFont("DejaVu Sans", 15))
        btn_voltar.setFocusPolicy(NO_FOCUS)
        btn_voltar.setStyleSheet(
            "QPushButton{background:#222;border:2px solid #fff;"
            "border-radius:10px;color:#fff;padding:6px 18px;}"
            "QPushButton:pressed{background:#333;}"
        )
        btn_voltar.clicked.connect(self.voltar)
        layout.addWidget(btn_voltar)
        self.setLayout(layout)

    def showEvent(self, event):
        super().showEvent(event)
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
            self.parent_app.abrir_viewer(os.path.join(PASTA_SAIDA, nome_arquivo))

    def load_files(self):
        # Limpa a lista atual antes de recarregar
        while self.files_layout.count():
            item = self.files_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        arquivos = sorted(
            [f for f in os.listdir(PASTA_SAIDA) if f.endswith(('.mp4', '.jpg'))],
            reverse=True
        )

        if not arquivos:
            lbl_vazio = QLabel("Nenhum arquivo encontrado.")
            lbl_vazio.setFont(QFont("DejaVu Sans", 15))
            lbl_vazio.setStyleSheet("color:#888;")
            self.files_layout.addWidget(lbl_vazio)
            return

        self.thumb_labels = {}  # guarda referência dos QLabel de thumb pendentes

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

            caminho_original = os.path.join(PASTA_SAIDA, arquivo)
            caminho_thumb = os.path.join(PASTA_THUMBS, arquivo + ".thumb.jpg")

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
            lbl_arquivo.setFont(QFont("DejaVu Sans", 12))
            lbl_arquivo.setStyleSheet("background:transparent; border:none;")

            btn_delete = QPushButton("X")
            btn_delete.setFont(QFont("DejaVu Sans", 12))
            btn_delete.setFocusPolicy(NO_FOCUS)
            btn_delete.setStyleSheet(
                "QPushButton{background:#311;border:2px solid #f33;"
                "border-radius:10px;color:#f33;}"
                "QPushButton:pressed{background:#511;}"
            )
            btn_delete.setFixedSize(55, 55)
            btn_delete.clicked.connect(lambda _, f=arquivo: self.delete_file(f))

            layoutarquivo.addWidget(lbl_thumb)
            layoutarquivo.addWidget(lbl_arquivo)
            layoutarquivo.addStretch()
            layoutarquivo.addWidget(btn_delete)  # sem self.

            self.files_layout.addWidget(item_widget)  # adiciona o WIDGET, não o layout

        self.files_layout.addStretch()  # empurra os itens pra cima, evita esticar

    def on_thumb_pronta(self, arquivo, caminho_thumb):
        """Chamado quando um ThumbnailWorker termina de gerar a thumbnail."""
        lbl = self.thumb_labels.get(arquivo)
        if lbl is not None:
            try:
                lbl.setPixmap(QPixmap(caminho_thumb))
            except RuntimeError:
                pass  # o label pode já ter sido destruído se a tela foi trocada

    def delete_file(self, nome_arquivo):
        caminho = os.path.join(PASTA_SAIDA, nome_arquivo)
        if os.path.exists(caminho):
            os.remove(caminho)

        caminho_thumb = os.path.join(PASTA_THUMBS, nome_arquivo + ".thumb.jpg")
        if os.path.exists(caminho_thumb):
            os.remove(caminho_thumb)  # remove o cache junto, senão fica lixo acumulando

        self._sujo = True
        self.load_files()  # recarrega a lista depois de deletar

    def voltar(self):
        if self.parent_app:
            self.parent_app.tela_principal()

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

        self.setStyleSheet("background:#000; color:#eee;")

        self.lbl_nome = QLabel("")
        self.lbl_nome.setFont(QFont("DejaVu Sans", 12))
        self.lbl_nome.setStyleSheet("color:#888; padding:4px;")

        # host do vídeo: QWidget puro, janela X11 nativa pro mpv
        self.video_host = QWidget()
        self.video_host.setAttribute(Qt.WA_NativeWindow, True)
        self.video_host.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.video_host.setStyleSheet("background:#000;")
        self.video_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_host.setMinimumHeight(200)

        # área de foto (continua QLabel, que sabe escalar pixmap)
        self.area = QLabel()
        self.area.setAlignment(ALIGN_CENTER)
        self.area.setStyleSheet("background:#000;")
        self.area.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # troca entre os dois conforme o tipo de arquivo
        self.midia = QStackedWidget()
        self.midia.addWidget(self.area)         # índice 0 = foto
        self.midia.addWidget(self.video_host)   # índice 1 = vídeo
        self.midia.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # --- timestamp ---
        self.lbl_tempo = QLabel("00:00 / 00:00")
        self.lbl_tempo.setFont(QFont("DejaVu Sans", 13))
        self.lbl_tempo.setAlignment(ALIGN_CENTER)
        self.lbl_tempo.setStyleSheet("color:#5bf; padding:2px;")

        # --- controles ---
        estilo_ctrl = (
            "QPushButton{background:#112;border:2px solid #5bf;"
            "border-radius:10px;color:#5bf;padding:6px 10px;}"
            "QPushButton:pressed{background:#135;}"
        )

        self.btn_play = QPushButton("❚❚")
        self.btn_ret = QPushButton("<< 5s")
        self.btn_avc = QPushButton("5s >>")
        self.btn_vol_menos = QPushButton("V-")
        self.btn_vol_mais = QPushButton("V+")

        for b in (self.btn_play, self.btn_ret, self.btn_avc,
                  self.btn_vol_menos, self.btn_vol_mais):
            b.setMinimumHeight(56)
            b.setFont(QFont("DejaVu Sans", 14, FONT_BOLD))
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
        btn_voltar.setFont(QFont("DejaVu Sans", 15))
        btn_voltar.setFocusPolicy(NO_FOCUS)
        btn_voltar.setStyleSheet(
            "QPushButton{background:#222;border:2px solid #fff;"
            "border-radius:10px;color:#fff;padding:6px 18px;}"
            "QPushButton:pressed{background:#333;}"
        )
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
