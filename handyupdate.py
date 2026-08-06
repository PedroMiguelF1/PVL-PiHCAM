import os
import subprocess
from PyQt5.QtCore import QThread, pyqtSignal

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

def git(*args, timeout=180):
    return subprocess.run(
        ["git", "-C", REPO_DIR, *args],
        capture_output=True, text=True, timeout=timeout
    )

class AtualizadorWorker(QThread):
    progresso = pyqtSignal(str)
    terminou = pyqtSignal(bool, str)   # (precisa_reiniciar, mensagem)

    def run(self):
        try:
            self.progresso.emit("Procurando atualizações...")
            r = git("fetch", "--prune", "origin")
            if r.returncode != 0:
                self.terminou.emit(False, "Sem conexão com o GitHub:\n" + r.stderr.strip()[:300])
                return

            local = git("rev-parse", "HEAD").stdout.strip()
            remoto = git("rev-parse", "@{u}").stdout.strip()

            if not remoto:
                self.terminou.emit(False, "Upstream não configurado.\nRode: git push -u origin main")
                return

            if local == remoto:
                self.terminou.emit(False, "Você já está na versão mais recente.")
                return

            qtd = git("rev-list", "--count", "HEAD..@{u}").stdout.strip()
            self.progresso.emit(f"Baixando {qtd} atualização(ões)...")

            r = git("reset", "--hard", "@{u}")
            if r.returncode != 0:
                self.terminou.emit(False, "Falha ao aplicar:\n" + r.stderr.strip()[:300])
                return

            self.terminou.emit(True, f"{qtd} atualização(ões) aplicada(s).\nReiniciando...")

        except subprocess.TimeoutExpired:
            self.terminou.emit(False, "Tempo esgotado ao contatar o GitHub.")
        except Exception as e:
            self.terminou.emit(False, f"Erro inesperado:\n{e}")