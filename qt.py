from PyQt5.QtCore import Qt, QTimer, QEventLoop, QThreadPool, QRunnable, QObject, pyqtSignal as Signal
from PyQt5.QtGui import QFont, QKeySequence, QPixmap, QImage, QPainter, QColor, QPen
from PyQt5.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QShortcut,
    QVBoxLayout, QWidget, QStackedWidget, QScrollArea, QScroller, QSizePolicy, QProgressBar,
    QMessageBox
    
)

# ----------------------------------------------------------------------
# Classes de QtGui usadas pelo overlay do preview. Se o qt.py nao
# reexportar QtGui, puxa do MESMO binding que ja esta carregado.
# ----------------------------------------------------------------------
try:
    QImage, QPainter, QColor, QPen
except NameError:
    import importlib
    _gui = importlib.import_module(QFont.__module__)
    QImage = _gui.QImage
    QPainter = _gui.QPainter
    QColor = _gui.QColor
    QPen = _gui.QPen
 
 
def _qt_enum(dono, grupo, valor):
    """PyQt5 expoe Qt.NoPen; PySide6/PyQt6 expoem Qt.PenStyle.NoPen."""
    g = getattr(dono, grupo, None)
    v = getattr(g, valor, None)
    return v if v is not None else getattr(dono, valor)
 
 
FMT_PREMUL = _qt_enum(QImage, "Format", "Format_ARGB32_Premultiplied")
FMT_RGBA = _qt_enum(QImage, "Format", "Format_RGBA8888")
NO_PEN = _qt_enum(Qt, "PenStyle", "NoPen")
NO_BRUSH = _qt_enum(Qt, "BrushStyle", "NoBrush")
ANTIALIAS = _qt_enum(QPainter, "RenderHint", "Antialiasing")

FONT_BOLD = QFont.Bold
ALIGN_RIGHT = Qt.AlignRight | Qt.AlignVCenter
ALIGN_CENTER = Qt.AlignCenter
ALIGN_LEFT = Qt.AlignLeft
NO_FOCUS = Qt.NoFocus
APP_SHORTCUT = Qt.ApplicationShortcut
EXEC = lambda a: a.exec_()
KEEP_AR = Qt.KeepAspectRatio
SMOOTH_TRANSFORM = Qt.SmoothTransformation