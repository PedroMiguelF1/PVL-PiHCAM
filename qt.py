from PySide6.QtCore import QTimer, QDateTime, QEventLoop, QThreadPool, QRunnable, QSize, QObject, Signal, Qt
from PySide6.QtGui import QFont, QKeySequence, QPixmap, QImage, QPainter, QColor, QPen, QIcon, QShortcut
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget, QStackedWidget, QScrollArea, 
    QScroller, QSizePolicy, QProgressBar, QMessageBox, 
    QDialog

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

FONT_BOLD = _qt_enum(QFont, "Weight", "Bold")
ALIGN_RIGHT = _qt_enum(Qt, "AlignmentFlag", "AlignRight") | _qt_enum(Qt, "AlignmentFlag", "AlignVCenter")
ALIGN_CENTER = _qt_enum(Qt, "AlignmentFlag", "AlignCenter")
ALIGN_LEFT = _qt_enum(Qt, "AlignmentFlag", "AlignLeft")
NO_FOCUS = _qt_enum(Qt, "FocusPolicy", "NoFocus")
APP_SHORTCUT = _qt_enum(Qt, "ShortcutContext", "ApplicationShortcut")
EXEC = lambda a: a.exec()
KEEP_AR = _qt_enum(Qt, "AspectRatioMode", "KeepAspectRatio")
SMOOTH_TRANSFORM = _qt_enum(Qt, "TransformationMode", "SmoothTransformation")
