"""
Otimizador de Notebook
----------------------
Limpa e otimiza o Windows sem apagar arquivos pessoais.

Para gerar o .exe:
    pip install customtkinter pyinstaller
    pyinstaller --onefile --noconsole --uac-admin --collect-all customtkinter otimizador.py
"""
import ctypes, os, queue, shutil, subprocess, sys, tempfile, threading, time, winreg
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

NO_WINDOW = subprocess.CREATE_NO_WINDOW
DRIVE = os.environ.get("SystemDrive", "C:") + "\\"
WINDIR = os.environ.get("SystemRoot", r"C:\Windows")
DIAS_PADRAO = 7  # temporários mais novos que isso são preservados
PASTA_LOGS = os.path.join(
    os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "OtimizadorNotebook", "logs")


def _oem():
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


OEM = _oem()
_MEI = getattr(sys, "_MEIPASS", "")  # pasta temporária do próprio .exe


def eh_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def fmt(b):
    return f"{b / 1024**3:.1f} GB" if b >= 1024**3 else f"{b / 1024**2:.0f} MB"


# ------------------------------------------------------------ energia / estado
class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def estado_energia():
    """Retorna (na_tomada, porcentagem_da_bateria)."""
    s = SYSTEM_POWER_STATUS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
    return s.ACLineStatus == 1, min(s.BatteryLifePercent, 100)


def aplicar_plano(na_tomada, log):
    def tentar(alias):
        p = subprocess.run(f"powercfg /setactive {alias}", shell=True,
                           capture_output=True, creationflags=NO_WINDOW)
        return p.returncode == 0

    if na_tomada:
        if tentar("SCHEME_MIN"):
            log("Na tomada: plano Alto desempenho ativado.")
        elif tentar("SCHEME_BALANCED"):
            log("Alto desempenho indisponível neste notebook: plano Equilibrado ativado.")
    elif tentar("SCHEME_BALANCED"):
        log("Na bateria: plano Equilibrado ativado.")


def reinicio_pendente():
    chaves = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    ]
    for c in chaves:
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, c))
            return True
        except OSError:
            pass
    return False


# ------------------------------------------------------------ execução de comandos
_cancelar = threading.Event()
_estado = {"proc": None, "critico": False}


class Ctx:
    """Dados repassados para cada tarefa."""
    def __init__(self, log, simular=False, dias=DIAS_PADRAO):
        self.log, self.simular, self.dias, self.bytes = log, simular, dias, 0


def rodar(cmd, ctx, critico=False, forcar=False, quieto=False):
    """Executa um comando. critico=True: não é morto pelo botão Cancelar (DISM/sfc).
    forcar=True: roda mesmo após cancelamento (ex.: religar serviços)."""
    if _cancelar.is_set() and not forcar:
        return None, ""
    if not quieto:
        ctx.log("> " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    p = subprocess.Popen(
        cmd, shell=isinstance(cmd, str), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding=OEM, errors="replace", creationflags=NO_WINDOW)
    _estado["proc"], _estado["critico"] = p, critico
    saida, _ = p.communicate()
    _estado["proc"], _estado["critico"] = None, False
    saida = (saida or "").replace("\x00", "").strip()  # sfc responde em UTF-16
    if _cancelar.is_set() and not forcar and not critico:
        if not quieto:
            ctx.log("(interrompido)")
        return None, saida
    if saida and not quieto:
        ctx.log(saida[-400:])
    return p.returncode, saida


def _protegido(caminho):
    return bool(_MEI) and caminho.lower().startswith(_MEI.lower())


def limpar_antigos(pasta, ctx, dias=None):
    """Remove só arquivos mais antigos que `dias` (0 = todos). Em simulação, apenas mede."""
    if not os.path.isdir(pasta):
        return
    dias = ctx.dias if dias is None else dias
    limite = time.time() - dias * 86400
    qtd = tamanho = 0
    for raiz, dirs, arquivos in os.walk(pasta, topdown=False):
        if _cancelar.is_set():
            break
        if _protegido(raiz):
            continue
        for nome in arquivos:
            caminho = os.path.join(raiz, nome)
            try:
                st = os.lstat(caminho)
                if max(st.st_mtime, st.st_ctime) >= limite:
                    continue  # recente: preserva
                if not ctx.simular:
                    os.remove(caminho)
                qtd += 1
                tamanho += st.st_size
            except OSError:
                pass  # em uso ou sem permissão: ignora
        if not ctx.simular:
            for d in dirs:
                p = os.path.join(raiz, d)
                try:
                    st = os.lstat(p)
                    if (not os.path.islink(p) and not _protegido(p)
                            and max(st.st_mtime, st.st_ctime) < limite):
                        os.rmdir(p)  # só remove se estiver vazia
                except OSError:
                    pass
    ctx.bytes += tamanho
    verbo = "seriam removidos" if ctx.simular else "removidos"
    ctx.log(f"{qtd} arquivos {verbo} ({fmt(tamanho)}) em {pasta}")


# ------------------------------------------------------------ tarefas
def temp_usuario(ctx):
    limpar_antigos(tempfile.gettempdir(), ctx)


def temp_windows(ctx):
    limpar_antigos(os.path.join(WINDIR, "Temp"), ctx)


def cache_navegadores(ctx):
    local = os.environ.get("LOCALAPPDATA", "")
    ctx.log("Dica: feche o Chrome e o Edge para limpar tudo.")
    for base in (r"Google\Chrome\User Data", r"Microsoft\Edge\User Data"):
        raiz = os.path.join(local, base)
        if not os.path.isdir(raiz):
            continue
        for perfil in os.listdir(raiz):
            if perfil == "Default" or perfil.startswith("Profile "):
                for sub in ("Cache", "Code Cache", "GPUCache"):
                    limpar_antigos(os.path.join(raiz, perfil, sub), ctx, dias=0)


HANDLERS = ["Thumbnail Cache", "Windows Error Reporting Files",
            "D3D Shader Cache", "Delivery Optimization Files"]
VOLCACHES = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\VolumeCaches"
SAGE = "StateFlags8421"


def limpeza_windows(ctx):
    """Limpeza de Disco oficial, só com itens seguros (nunca a pasta Downloads)."""
    ativados = []
    for h in HANDLERS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, VOLCACHES + "\\" + h,
                                0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, SAGE, 0, winreg.REG_DWORD, 2)
                ativados.append(h)
        except OSError:
            pass
    if not ativados:
        ctx.log("Itens da Limpeza de Disco não encontrados neste Windows.")
        return
    try:
        rodar("cleanmgr /sagerun:8421", ctx)
    finally:
        for h in ativados:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, VOLCACHES + "\\" + h,
                                    0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, SAGE)
            except OSError:
                pass


def cache_dns(ctx):
    rodar("ipconfig /flushdns", ctx)


def cache_update(ctx):
    pasta = os.path.join(WINDIR, "SoftwareDistribution", "Download")
    if ctx.simular:
        limpar_antigos(pasta, ctx, dias=0)
        return
    _, saida = rodar('tasklist /FI "IMAGENAME eq TiWorker.exe"', ctx, quieto=True)
    if "tiworker.exe" in saida.lower():
        ctx.log("O Windows está instalando atualizações agora: tarefa pulada por segurança.")
        return
    rodar("net stop wuauserv", ctx)
    rodar("net stop bits", ctx)
    try:
        limpar_antigos(pasta, ctx, dias=0)
    finally:  # mesmo cancelando, os serviços precisam voltar
        rodar("net start bits", ctx, forcar=True)
        rodar("net start wuauserv", ctx, forcar=True)


def dism_limpeza(ctx):
    rodar("DISM /Online /Cleanup-Image /StartComponentCleanup", ctx, critico=True)


def plano_automatico(ctx):
    ac, _ = estado_energia()
    aplicar_plano(ac, ctx.log)


def lixeira(ctx):
    rodar(["powershell", "-NoProfile", "-Command",
           "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"], ctx)


def otimizar_disco(ctx):
    rodar(["defrag", DRIVE.rstrip("\\"), "/O"], ctx)


def cache_loja(ctx):
    rodar("wsreset.exe", ctx)


def reparar(ctx):
    rodar("DISM /Online /Cleanup-Image /RestoreHealth", ctx, critico=True)
    rodar("sfc /scannow", ctx, critico=True)


def hibernacao_off(ctx):
    rodar("powercfg -h off", ctx)


def relatorio_bateria(ctx):
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    pasta = desktop if os.path.isdir(desktop) else os.path.expanduser("~")
    caminho = os.path.join(pasta, "relatorio_bateria.html")
    rodar(["powercfg", "/batteryreport", "/output", caminho], ctx)
    if os.path.exists(caminho) and not _cancelar.is_set():
        ctx.log(f"Relatório salvo em: {caminho}")
        os.startfile(caminho)


def reverter_padroes(ctx):
    rodar("powercfg /h on", ctx)
    rodar("powercfg /setactive SCHEME_BALANCED", ctx)


# ------------------------------------------------------------ ponto de restauração
def minutos_ultimo_ponto():
    script = ("$p = Get-ComputerRestorePoint | Sort-Object SequenceNumber | Select-Object -Last 1; "
              "if ($p) { [int]((Get-Date) - [Management.ManagementDateTimeConverter]::ToDateTime"
              "($p.CreationTime)).TotalMinutes } else { -1 }")
    ctx = Ctx(lambda t: None)
    _, saida = rodar(["powershell", "-NoProfile", "-Command", script], ctx, quieto=True, forcar=True)
    try:
        return int(saida.splitlines()[-1].strip())
    except (ValueError, IndexError):
        return -1


def criar_ponto(ctx):
    """True se existe um ponto de restauração recente (menos de 24h)."""
    ctx.log("Criando ponto de restauração (pode levar alguns segundos)...")
    rodar(["powershell", "-NoProfile", "-Command",
           'Checkpoint-Computer -Description "Otimizador de Notebook" '
           '-RestorePointType MODIFY_SETTINGS -ErrorAction Stop'], ctx, forcar=True)
    m = minutos_ultimo_ponto()
    if 0 <= m <= 1440:
        ctx.log("Ponto de restauração disponível (criado agora)." if m < 10 else
                f"Ponto de restauração recente encontrado (criado há {m} min). "
                "O Windows só cria um novo a cada 24h.")
        return True
    ctx.log("Não foi possível confirmar um ponto de restauração.")
    return False


# ------------------------------------------------------------ lista de tarefas
@dataclass
class Tarefa:
    nome: str
    descricao: str
    funcao: Callable
    avancada: bool = False
    padrao: bool = True
    simulavel: bool = False   # consegue só medir, sem apagar
    sistema: bool = False     # mexe no sistema: exige ponto de restauração
    pesada: bool = False      # DISM/sfc: exige tomada/bateria e sem reinício pendente
    confirmar: str = ""       # texto de confirmação antes de rodar


TAREFAS = [
    Tarefa("Temporários do usuário",
           "Remove só arquivos temporários com mais de 7 dias. Recentes e em uso são preservados.",
           temp_usuario, simulavel=True),
    Tarefa("Temporários do Windows",
           "Mesma regra: só arquivos com mais de 7 dias.",
           temp_windows, simulavel=True),
    Tarefa("Cache do Chrome e do Edge",
           "Apaga apenas o cache. Senhas, favoritos e histórico não são tocados.",
           cache_navegadores, simulavel=True),
    Tarefa("Limpeza de Disco do Windows (itens seguros)",
           "Miniaturas, relatórios de erro e cache de gráficos. Não mexe em Downloads nem em documentos.",
           limpeza_windows),
    Tarefa("Cache de DNS", "Inofensivo: o Windows recria sozinho.", cache_dns),
    Tarefa("Cache do Windows Update",
           "Arquivos de atualização já baixados; o Windows baixa de novo se precisar. "
           "Pula se houver instalação em andamento.",
           cache_update, simulavel=True),
    Tarefa("Componentes antigos do Windows (DISM)",
           "Manutenção oficial da Microsoft. Pode levar vários minutos e não é interrompida pelo Cancelar.",
           dism_limpeza, sistema=True, pesada=True),
    Tarefa("Plano de energia automático",
           "Alto desempenho na tomada, Equilibrado na bateria (aplicado uma vez).",
           plano_automatico),

    Tarefa("Esvaziar a Lixeira",
           "Apaga DEFINITIVAMENTE tudo o que estiver na Lixeira.",
           lixeira, avancada=True, padrao=False,
           confirmar="Esvaziar a Lixeira apaga os arquivos de forma definitiva.\n\nContinuar?"),
    Tarefa("Otimizar disco (TRIM no SSD / desfragmentar HD)",
           "O Windows escolhe o método certo. Pode demorar em HD.",
           otimizar_disco, avancada=True, padrao=False),
    Tarefa("Limpar cache da Microsoft Store",
           "A Loja abre ao final do processo.",
           cache_loja, avancada=True, padrao=False),
    Tarefa("Reparar sistema (DISM + sfc)",
           "Verifica e repara arquivos do Windows. Demora bastante.",
           reparar, avancada=True, padrao=False, sistema=True, pesada=True),
    Tarefa("Desativar hibernação (libera vários GB)",
           "O notebook perde a hibernação e a inicialização rápida.",
           hibernacao_off, avancada=True, padrao=False, sistema=True,
           confirmar="Sem a hibernação, o notebook desliga direto quando a bateria acabar "
                     "e arquivos não salvos podem ser perdidos.\n\nDesativar mesmo assim?"),
]


# ------------------------------------------------------------ programas na inicialização
BASE = r"Software\Microsoft\Windows\CurrentVersion"
FONTES = [  # (raiz, chave Run, chave StartupApproved, rótulo)
    (winreg.HKEY_CURRENT_USER, BASE + r"\Run",
     BASE + r"\Explorer\StartupApproved\Run", "Usuário"),
    (winreg.HKEY_LOCAL_MACHINE, BASE + r"\Run",
     BASE + r"\Explorer\StartupApproved\Run", "Todos os usuários"),
    (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
     BASE + r"\Explorer\StartupApproved\Run32", "Todos os usuários (32 bits)"),
]
MANTER = ("realtek", "synaptics", "touchpad", "elan", "hotkey", "defender", "securityhealth",
          "antivirus", "avast", "kaspersky", "norton", "mcafee", "avira", "bitdefender",
          "nvidia", "intel", "amd", "bluetooth", "audio", "wifi", "wlan", "driver")


def listar_inicializacao():
    itens = []
    for fonte in FONTES:
        raiz, run, _, _ = fonte
        try:
            with winreg.OpenKey(raiz, run) as k:
                i = 0
                while True:
                    try:
                        nome, valor, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    itens.append((fonte, nome, str(valor)))
                    i += 1
        except OSError:
            pass
    return itens


def esta_ativo(fonte, nome):
    try:
        with winreg.OpenKey(fonte[0], fonte[2]) as k:
            dados, _ = winreg.QueryValueEx(k, nome)
            return dados[0] % 2 == 0  # 02/06 = ativo, 03/07 = desativado
    except OSError:
        return True


def definir_ativo(fonte, nome, ativo):
    # mesmo mecanismo do Gerenciador de Tarefas: não apaga o item, só desativa
    with winreg.CreateKey(fonte[0], fonte[2]) as k:
        winreg.SetValueEx(k, nome, 0, winreg.REG_BINARY, bytes([2 if ativo else 3]) + bytes(11))


class JanelaInicializacao(ctk.CTkToplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Programas na inicialização")
        self.geometry("600x460")
        self.transient(master)
        ctk.CTkLabel(self, text="Desmarque para impedir que o programa abra junto com o Windows.\n"
                                "Nada é apagado: você pode marcar de novo quando quiser."
                     , justify="left").pack(anchor="w", padx=20, pady=(15, 5))
        quadro = ctk.CTkScrollableFrame(self)
        quadro.pack(fill="both", expand=True, padx=20, pady=10)
        itens = listar_inicializacao()
        if not itens:
            ctk.CTkLabel(quadro, text="Nenhum programa encontrado.").pack(pady=10)
        for fonte, nome, valor in itens:
            v = ctk.BooleanVar(value=esta_ativo(fonte, nome))
            ctk.CTkCheckBox(quadro, text=f"{nome}  ({fonte[3]})", variable=v,
                            command=lambda f=fonte, n=nome, var=v, c=valor: self.alternar(f, n, var, c)
                            ).pack(anchor="w", pady=(6, 0))
            ctk.CTkLabel(quadro, text=valor[:90], text_color="gray",
                         font=ctk.CTkFont(size=10)).pack(anchor="w", padx=28)
            if any(p in (nome + valor).lower() for p in MANTER):
                ctk.CTkLabel(quadro, text="⚠ parece driver/segurança: o ideal é manter ligado",
                             text_color="#c77d00", font=ctk.CTkFont(size=10)).pack(anchor="w", padx=28)
        self.after(100, lambda: (self.lift(), self.focus_force()))

    def alternar(self, fonte, nome, var, comando):
        ativo = var.get()
        if not ativo and any(p in (nome + comando).lower() for p in MANTER):
            if not messagebox.askyesno("Atenção", f"'{nome}' parece ser driver ou segurança.\n"
                                       "Desativar pode causar problemas.\n\nDesativar mesmo assim?"):
                var.set(True)
                return
        try:
            definir_ativo(fonte, nome, ativo)
        except Exception as e:
            var.set(not ativo)
            messagebox.showerror("Erro", f"Não foi possível alterar '{nome}':\n{e}")


# ------------------------------------------------------------ interface
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Otimizador de Notebook")
        self.geometry("780x880")
        self.minsize(700, 720)
        self.fila = queue.Queue()
        self.ocupado = False
        self.log_path = None
        self._indet = False
        self._ultimo_ac = None
        self._lock = threading.Lock()

        ctk.CTkLabel(self, text="Otimizador de Notebook",
                     font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w", padx=20, pady=(15, 0))
        ctk.CTkLabel(self, text="Limpa e otimiza sem apagar seus arquivos pessoais. "
                                "Use \"Simular\" para ver o que seria feito.",
                     text_color="gray").pack(anchor="w", padx=20)
        self.info = ctk.CTkLabel(self, text="", justify="left")
        self.info.pack(anchor="w", padx=20, pady=(6, 0))

        quadro = ctk.CTkScrollableFrame(self, height=300)
        quadro.pack(fill="x", padx=20, pady=(8, 5))
        self.vars = []
        for titulo, avancada in (("RECOMENDADAS (seguras)", False),
                                 ("AVANÇADAS (opcionais — use com cuidado)", True)):
            ctk.CTkLabel(quadro, text=titulo, font=ctk.CTkFont(weight="bold"),
                         text_color="#c77d00" if avancada else "#3aa356"
                         ).pack(anchor="w", padx=5, pady=(8, 2))
            for t in TAREFAS:
                if t.avancada != avancada:
                    continue
                v = ctk.BooleanVar(value=t.padrao)
                ctk.CTkCheckBox(quadro, text=t.nome, variable=v).pack(anchor="w", padx=5, pady=(4, 0))
                ctk.CTkLabel(quadro, text=t.descricao, text_color="gray", justify="left",
                             wraplength=620, font=ctk.CTkFont(size=11)).pack(anchor="w", padx=30)
                self.vars.append((t, v))

        opcoes = ctk.CTkFrame(self, fg_color="transparent")
        opcoes.pack(anchor="w", padx=20)
        self.sw_ponto = ctk.CTkSwitch(opcoes, text="Exigir ponto de restauração antes de mexer no sistema")
        self.sw_ponto.select()
        self.sw_ponto.pack(anchor="w", pady=2)
        self.sw_auto = ctk.CTkSwitch(opcoes, text="Trocar o plano de energia sozinho (tomada/bateria) "
                                                  "enquanto o programa estiver aberto",
                                     command=lambda: setattr(self, "_ultimo_ac", None))
        self.sw_auto.pack(anchor="w", pady=2)

        linha1 = ctk.CTkFrame(self, fg_color="transparent")
        linha1.pack(pady=(10, 0))
        self.btn = ctk.CTkButton(linha1, text="Otimizar agora", width=150, command=self.iniciar)
        self.btn.grid(row=0, column=0, padx=5)
        self.btn_sim = ctk.CTkButton(linha1, text="Simular (não apaga nada)", width=180,
                                     fg_color="#3a6ea5", command=self.simular)
        self.btn_sim.grid(row=0, column=1, padx=5)
        self.btn_cancelar = ctk.CTkButton(linha1, text="Cancelar", width=110, fg_color="#b33a3a",
                                          hover_color="#8f2d2d", state="disabled", command=self.cancelar)
        self.btn_cancelar.grid(row=0, column=2, padx=5)

        linha2 = ctk.CTkFrame(self, fg_color="transparent")
        linha2.pack(pady=8)
        self.btn_bateria = ctk.CTkButton(linha2, text="Relatório da bateria", width=150,
                                         fg_color="gray30", command=self.ver_bateria)
        self.btn_bateria.grid(row=0, column=0, padx=4)
        ctk.CTkButton(linha2, text="Programas na inicialização", width=190, fg_color="gray30",
                      command=lambda: JanelaInicializacao(self)).grid(row=0, column=1, padx=4)
        self.btn_reverter = ctk.CTkButton(linha2, text="Reverter padrões", width=130,
                                          fg_color="gray30", command=self.reverter)
        self.btn_reverter.grid(row=0, column=2, padx=4)
        ctk.CTkButton(linha2, text="Abrir logs", width=90, fg_color="gray30",
                      command=self.abrir_logs).grid(row=0, column=3, padx=4)

        self.status = ctk.CTkLabel(self, text="Pronto.")
        self.status.pack()
        self.barra = ctk.CTkProgressBar(self)
        self.barra.set(0)
        self.barra.pack(fill="x", padx=20, pady=5)
        self.caixa = ctk.CTkTextbox(self)
        self.caixa.pack(fill="both", expand=True, padx=20, pady=10)

        self.ocupaveis = [self.btn, self.btn_sim, self.btn_bateria, self.btn_reverter]
        self.atualizar_info()
        self.after(100, self._processar_fila)
        self.after(1000, self._vigiar_energia)

    # ---- log e fila (a thread de trabalho só escreve na fila)
    def log(self, txt):
        self.fila.put(("log", txt))
        if self.log_path:
            with self._lock:
                try:
                    with open(self.log_path, "a", encoding="utf-8") as f:
                        f.write(txt + "\n")
                except OSError:
                    pass

    def progresso(self, valor, texto, indeterminado=False):
        self.fila.put(("prog", valor, texto, indeterminado))

    def _processar_fila(self):
        try:
            while True:
                tipo, *d = self.fila.get_nowait()
                if tipo == "log":
                    self.caixa.insert("end", d[0] + "\n")
                    self.caixa.see("end")
                elif tipo == "prog":
                    valor, texto, indet = d
                    self.status.configure(text=texto)
                    if indet and not self._indet:
                        self.barra.configure(mode="indeterminate")
                        self.barra.start()
                    elif not indet:
                        if self._indet:
                            self.barra.stop()
                            self.barra.configure(mode="determinate")
                        self.barra.set(valor)
                    self._indet = indet
                elif tipo == "fim":
                    self._finalizar()
        except queue.Empty:
            pass
        self.after(100, self._processar_fila)

    def _vigiar_energia(self):
        try:
            if self.sw_auto.get() and not self.ocupado:
                ac, _ = estado_energia()
                if ac != self._ultimo_ac:
                    self._ultimo_ac = ac
                    threading.Thread(target=aplicar_plano, daemon=True,
                                     args=(ac, lambda t: self.log("[auto] " + t))).start()
            if not self.ocupado:
                self.atualizar_info()
        finally:
            self.after(10000, self._vigiar_energia)

    def atualizar_info(self):
        try:
            ac, pct = estado_energia()
            energia = f"{'na tomada' if ac else 'na bateria'} ({pct}%)"
            livre = fmt(shutil.disk_usage(DRIVE).free)
            pend = "sim (reinicie antes de DISM/sfc)" if reinicio_pendente() else "não"
            self.info.configure(text=f"Administrador: {'sim' if eh_admin() else 'não'}   |   "
                                     f"Energia: {energia}   |   Disco {DRIVE[:2]} livre: {livre}   |   "
                                     f"Reinício pendente: {pend}")
        except Exception:
            pass

    # ---- botões
    def _selecionadas(self):
        return [t for t, v in self.vars if v.get()]

    def iniciar(self):
        tarefas = self._selecionadas()
        if not tarefas:
            messagebox.showinfo("Otimizador", "Marque pelo menos uma opção.")
            return
        for t in list(tarefas):
            if t.confirmar and not messagebox.askyesno("Confirmar", t.confirmar, icon="warning"):
                tarefas.remove(t)
        if tarefas:
            self._disparar(tarefas)

    def simular(self):
        tarefas = self._selecionadas()
        if not tarefas:
            messagebox.showinfo("Otimizador", "Marque pelo menos uma opção.")
            return
        self._disparar(tarefas, simular=True)

    def ver_bateria(self):
        self._disparar([Tarefa("Relatório da bateria", "", relatorio_bateria)], otimizacao=False)

    def reverter(self):
        if messagebox.askyesno("Reverter padrões",
                               "Reativar a hibernação e voltar ao plano de energia Equilibrado?"):
            self.sw_auto.deselect()
            self._disparar([Tarefa("Reverter padrões", "", reverter_padroes)], otimizacao=False)

    def abrir_logs(self):
        os.makedirs(PASTA_LOGS, exist_ok=True)
        os.startfile(PASTA_LOGS)

    def cancelar(self):
        _cancelar.set()
        p = _estado["proc"]
        if p and p.poll() is None:
            if _estado["critico"]:
                self.log("Aguarde: DISM/sfc não podem ser interrompidos com segurança. "
                         "O programa para assim que o comando terminar.")
            else:
                subprocess.run(f"taskkill /PID {p.pid} /T /F", shell=True,
                               capture_output=True, creationflags=NO_WINDOW)

    # ---- execução em segundo plano
    def _disparar(self, tarefas, simular=False, otimizacao=True):
        _cancelar.clear()
        self.ocupado = True
        for b in self.ocupaveis:
            b.configure(state="disabled")
        self.btn_cancelar.configure(state="normal")
        try:
            os.makedirs(PASTA_LOGS, exist_ok=True)
            self.log_path = os.path.join(PASTA_LOGS, f"otimizador_{datetime.now():%Y%m%d_%H%M%S}.log")
        except OSError:
            self.log_path = None
        threading.Thread(target=self.executar, daemon=True,
                         args=(tarefas, simular, otimizacao, bool(self.sw_ponto.get()))).start()

    def executar(self, tarefas, simular, otimizacao, exigir_ponto):
        ctx = Ctx(self.log, simular)
        feitas, puladas = [], []
        try:
            self.log(f"===== {'SIMULAÇÃO (nada será apagado)' if simular else 'EXECUÇÃO'} - "
                     f"{datetime.now():%d/%m/%Y %H:%M:%S} =====")
            antes = shutil.disk_usage(DRIVE).free
            ac, pct = estado_energia()
            pendente = reinicio_pendente()

            if otimizacao and not simular and exigir_ponto and any(t.sistema for t in tarefas):
                self.progresso(0, "Criando ponto de restauração...", True)
                if not criar_ponto(ctx):
                    self.log("\nNada foi alterado: sem ponto de restauração a otimização não começa.\n"
                             "(Desative \"Exigir ponto de restauração\" se quiser continuar mesmo assim.)")
                    return

            total = len(tarefas)
            for i, t in enumerate(tarefas):
                if _cancelar.is_set():
                    break
                self.progresso(i / total, f"({i + 1}/{total}) {t.nome}", t.pesada and not simular)
                self.log(f"\n=== {t.nome} ===")
                if simular and not t.simulavel:
                    self.log("(não executada na simulação)")
                    puladas.append(f"{t.nome} (simulação)")
                    continue
                if t.pesada:
                    motivo = ""
                    if pendente:
                        motivo = "há reinício pendente do Windows: reinicie e tente de novo"
                    elif not ac and pct < 50:
                        motivo = "bateria abaixo de 50%: ligue na tomada"
                    if motivo:
                        self.log(f"Pulada: {motivo}.")
                        puladas.append(f"{t.nome} ({motivo})")
                        continue
                try:
                    t.funcao(ctx)
                    feitas.append(t.nome)
                except Exception as e:
                    self.log(f"Erro: {e}")
                    puladas.append(f"{t.nome} (erro)")

            self.log("\n===== RESUMO =====")
            if _cancelar.is_set():
                self.log("Cancelado pelo usuário.")
            self.log(f"Executadas: {len(feitas)}  |  Não executadas: {len(puladas)}")
            for p in puladas:
                self.log(f"  - {p}")
            if simular:
                self.log(f"Espaço que poderia ser liberado: cerca de {fmt(ctx.bytes)}")
            elif otimizacao:
                depois = shutil.disk_usage(DRIVE).free
                self.log(f"Espaço liberado no disco: {fmt(max(0, depois - antes))}")
            if self.log_path:
                self.log(f"Log salvo em: {self.log_path}")
            self.progresso(1, "Cancelado." if _cancelar.is_set() else "Concluído.")
        except Exception as e:
            self.log(f"\nErro inesperado: {e}")
        finally:
            self.fila.put(("fim",))

    def _finalizar(self):
        self.ocupado = False
        if self._indet:
            self.barra.stop()
            self.barra.configure(mode="determinate")
            self._indet = False
        for b in self.ocupaveis:
            b.configure(state="normal")
        self.btn_cancelar.configure(state="disabled")
        self.atualizar_info()


if __name__ == "__main__":
    if not eh_admin():
        params = "" if getattr(sys, "frozen", False) else " ".join(f'"{a}"' for a in sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App().mainloop()
