import ctypes, os, shutil, subprocess, sys, tempfile, threading
import customtkinter as ctk


def eh_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def rodar(cmd, log):
    log(f"> {cmd}")
    p = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        encoding="cp850", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    saida = (p.stdout or "").strip()
    if saida:
        log(saida[-400:])


def apagar_conteudo(pasta, log):
    apagados = 0
    for item in os.listdir(pasta):
        caminho = os.path.join(pasta, item)
        try:
            if os.path.isdir(caminho):
                shutil.rmtree(caminho)
            else:
                os.remove(caminho)
            apagados += 1
        except Exception:
            pass  # arquivo em uso, ignora
    log(f"{apagados} itens removidos de {pasta}")


def limpar_update(log):
    rodar("net stop wuauserv", log)
    rodar("net stop bits", log)
    apagar_conteudo(r"C:\Windows\SoftwareDistribution\Download", log)
    rodar("net start bits", log)
    rodar("net start wuauserv", log)


# (nome, marcado por padrão, ação)
TAREFAS = [
    ("Criar ponto de restauração", True,
     lambda log: rodar('powershell -Command "Checkpoint-Computer -Description Otimizador -RestorePointType MODIFY_SETTINGS"', log)),
    ("Limpar temporários do usuário", True,
     lambda log: apagar_conteudo(tempfile.gettempdir(), log)),
    ("Limpar temporários do Windows", True,
     lambda log: apagar_conteudo(r"C:\Windows\Temp", log)),
    ("Esvaziar a Lixeira", True,
     lambda log: rodar('powershell -Command "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"', log)),
    ("Limpar cache de DNS", True,
     lambda log: rodar("ipconfig /flushdns", log)),
    ("Limpar cache do Windows Update", True, limpar_update),
    ("Remover componentes antigos do Windows (DISM)", True,
     lambda log: rodar("DISM /Online /Cleanup-Image /StartComponentCleanup", log)),
    ("Ativar plano Alto desempenho", True,
     lambda log: rodar("powercfg /setactive SCHEME_MIN", log)),
    ("Reparar sistema: DISM + sfc (demorado)", False,
     lambda log: (rodar("DISM /Online /Cleanup-Image /RestoreHealth", log),
                  rodar("sfc /scannow", log))),
    ("Desativar hibernação (libera GB)", False,
     lambda log: rodar("powercfg -h off", log)),
]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Otimizador de Notebook")
        self.geometry("640x640")
        self.vars = []
        for nome, padrao, _ in TAREFAS:
            v = ctk.BooleanVar(value=padrao)
            ctk.CTkCheckBox(self, text=nome, variable=v).pack(anchor="w", padx=20, pady=3)
            self.vars.append(v)
        self.btn = ctk.CTkButton(self, text="Otimizar agora", command=self.iniciar)
        self.btn.pack(pady=10)
        self.caixa = ctk.CTkTextbox(self)
        self.caixa.pack(fill="both", expand=True, padx=20, pady=10)

    def log(self, txt):
        self.after(0, lambda: (self.caixa.insert("end", txt + "\n"), self.caixa.see("end")))

    def iniciar(self):
        escolhidas = [(n, a) for (n, _, a), v in zip(TAREFAS, self.vars) if v.get()]
        self.btn.configure(state="disabled")
        threading.Thread(target=self.executar, args=(escolhidas,), daemon=True).start()

    def executar(self, escolhidas):
        antes = shutil.disk_usage("C:\\").free
        for nome, acao in escolhidas:
            self.log(f"\n=== {nome} ===")
            try:
                acao(self.log)
            except Exception as e:
                self.log(f"Erro: {e}")
        depois = shutil.disk_usage("C:\\").free
        self.log(f"\nConcluído. Espaço liberado: {(depois - antes) / 1024**2:.0f} MB")
        self.after(0, lambda: self.btn.configure(state="normal"))


if __name__ == "__main__":
    if not eh_admin():
        params = "" if getattr(sys, "frozen", False) else " ".join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit()
    ctk.set_appearance_mode("dark")
    App().mainloop()