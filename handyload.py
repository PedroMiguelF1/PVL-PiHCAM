#!/usr/bin/env python3
"""
handyload.py — tela de carregamento leve para o Handycam NoIR (Raspberry Pi).

Por que este arquivo existe:
No handycam.py, os imports pesados (cv2, numpy, picamera2 -> PyQt) estão
no TOPO do arquivo. Isso significa que o Python já gasta todo o tempo de
importar/compilar essas libs ANTES mesmo de chegar em main() — então
qualquer splash criado dentro de main() só aparece depois que a parte
lenta já rodou, e não ajuda em nada.

A solução: este script só importa o mínimo do Qt (rápido), mostra o
splash IMEDIATAMENTE, e só DEPOIS disso faz `import handycam` — que é o
momento em que a parte pesada de fato acontece, já com o splash na tela
cobrindo a demora.

Este loader é específico do Pi (importa handycam.py). Pra rodar a
versão de testes no PC (webcam via mock_picamera2), use handyload_pc.py,
que faz a mesma coisa mas importando handycam_pc.py.

Uso:
    python handyload.py
"""

import sys
import os

# ----------------------------------------------------------------------
# Força o backend XCB (X11) do Qt, mesmo rodando num desktop Wayland
# (labwc, padrão no Raspberry Pi OS atual).
#
# O QGlPicamera2 (picamera2.previews.qt) cria a superfície EGL do preview
# chamando self.winId() e passando o valor direto pro eglCreateWindowSurface,
# esperando um handle de janela nativo de verdade. Isso funciona em X11,
# mas o Wayland não expõe esse tipo de handle da mesma forma — o Qt
# devolve um winId() que não serve pra nada (ex: valor pequeno tipo 2), e
# o eglCreateWindowSurface falha com EGL_BAD_ALLOC.
#
# O Raspberry Pi OS já vem com XWayland instalado, então basta forçar o
# app a rodar via XCB (não precisa desligar o Wayland do sistema todo).
# Isso TEM que ser feito antes de criar a QApplication — por isso está
# logo no topo, antes de qualquer outro import/uso do Qt.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb")

# ----------------------------------------------------------------------
# Import leve do Qt.
#
# IMPORTANTE: isso NÃO pode ser "primeiro binding que estiver instalado".
# O handycam.py faz `from picamera2.previews.qt import QGlPicamera2`, e
# esse módulo do picamera2 é hardcoded pra usar PyQt5 internamente (não
# tem fallback nem detecção — é sempre PyQt5, não importa o que mais
# esteja instalado no sistema). Se o splash aqui for criado com outro
# binding (PySide6/PyQt6), ficamos com DOIS bindings Qt carregados no
# mesmo processo, e a QApplication de um não é "vista" pelos QWidget do
# outro -> "QWidget: Must construct a QApplication before a QWidget".
#
# Por isso PyQt5 tem que ser tentado PRIMEIRO aqui, pra garantir que o
# splash e a QApplication usem o mesmo binding que o QGlPicamera2 vai
# forçar mais tarde durante o `import handycam`.
# ----------------------------------------------------------------------

from qt import Qt, QTimer, QEventLoop, QFont, QApplication, QWidget, QVBoxLayout, QLabel, QProgressBar, FONT_BOLD, ALIGN_CENTER, EXEC

class Splash(QWidget):
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background:#111;")

        layout = QVBoxLayout()
        layout.setContentsMargins(60, 40, 60, 50)

        self.lbl = QLabel("PVL-PiHCAM")
        self.lbl.setFont(QFont("DejaVu Sans", 20, FONT_BOLD))
        self.lbl.setStyleSheet("color:#5df;")
        self.lbl.setAlignment(ALIGN_CENTER)

        self.lbl_etapa = QLabel("")
        self.lbl_etapa.setFont(QFont("DejaVu Sans", 11))
        self.lbl_etapa.setStyleSheet("color:#888;")
        self.lbl_etapa.setAlignment(ALIGN_CENTER)

        self.barra = QProgressBar()
        self.barra.setRange(0, 100)
        self.barra.setTextVisible(False)
        self.barra.setFixedHeight(16)
        self.barra.setStyleSheet("""
            QProgressBar { background:#222; border:1px solid #333;
                           border-radius:8px; }
            QProgressBar::chunk { background:#5bf; border-radius:7px; }
        """)

        layout.addStretch(1)
        layout.addWidget(self.lbl)
        layout.addStretch(1)
        layout.addWidget(self.lbl_etapa)
        layout.addWidget(self.barra)
        self.setLayout(layout)

    def etapa(self, pct, texto=""):
        self.barra.setValue(pct)
        if texto:
            self.lbl_etapa.setText(texto)
        QApplication.processEvents()



def criar_splash():
    splash = QWidget()
    splash.setStyleSheet("background:#111;")

    layout = QVBoxLayout()
    lbl = QLabel("HandyPiCam NoIR")
    lbl.setFont(QFont("DejaVu Sans", 20, FONT_BOLD))
    lbl.setStyleSheet("color:#5df;")
    lbl.setAlignment(ALIGN_CENTER)
    layout.addWidget(lbl)
    splash.setLayout(layout)

    return splash


def garantir_splash_visivel(app, ms=150):
    """
    app.processEvents() sozinho só processa os eventos que JÁ estão na
    fila do Qt — não garante que o compositor/gerenciador de janelas
    (XWayland/labwc) realmente terminou de mapear e pintar a janela na
    tela, porque isso depende de uma ida-e-volta assíncrona com o
    servidor gráfico. Se o `import handycam` logo depois rodar rápido
    o suficiente (cache de disco quente, etc.), splash.close() pode
    acontecer antes desse round-trip terminar, e o splash nunca chega a
    aparecer fisicamente — só ora sim, ora não, dependendo da velocidade
    do import naquela execução.
    Rodar um mini-loop de eventos por um tempo mínimo garantido (em vez
    de só uma chamada pontual) dá tempo real pro compositor pintar a
    janela antes de seguir em frente.
    """
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def main():
    app = QApplication(sys.argv)
    splash = Splash()
    splash.showFullScreen()
    garantir_splash_visivel(app)

    # Pré-carga em etapas. Quando o `import handycam` rodar lá embaixo,
    # tudo isso já estará no sys.modules e os imports do topo dele
    # retornam instantaneamente.
    splash.etapa(5, "Carregando NumPy...")
    import numpy

    splash.etapa(20, "Carregando OpenCV...")
    import cv2

    splash.etapa(45, "Carregando picamera2...")
    import picamera2
    from picamera2.previews.qt import QGlPicamera2

    splash.etapa(65, "Carregando interface...")
    import handycam

    splash.etapa(70, "Preparando câmera...")
    win = handycam.main(app=app, splash=splash)
    sys.exit(EXEC(app))


if __name__ == "__main__":
    main()
