"""
gui.py — Interface gráfica do Video Uploader.

Rodar:      python gui.py     (ou:  python video_uploader.py   sem argumentos)
Gerar .exe: veja o README.md

* Fila persistente (queue.db): se fechar o programa ou cair a internet, as tarefas ficam salvas e
  podem ser retomadas. O histórico com os links fica na aba "Fila e histórico".
* Configurações ficam em config.json. A senha do MEGA NÃO é gravada em texto puro: só é lembrada
  se o pacote 'keyring' estiver instalado (usa o cofre do sistema).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
try:
    from tkinter import filedialog, messagebox
except ImportError:
    sys.exit("A janela precisa do tkinter, que não está instalado.\n"
             "Linux: sudo apt install python3-tk     Windows/Mac: reinstale o Python marcando 'tcl/tk'.")
try:
    import customtkinter as ctk
except ImportError:
    sys.exit(f"Falta o customtkinter neste Python.\nRode: {sys.executable} -m pip install customtkinter")

if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
os.chdir(BASE_DIR)

import runner as rn  # noqa: E402
import store as st  # noqa: E402
import video_uploader as vu  # noqa: E402

CONFIG_FILE = BASE_DIR / "config.json"
DB_FILE = BASE_DIR / "queue.db"
WORK_ROOT = BASE_DIR / ".work"
KEYRING_SERVICE = "VideoUploader-MEGA"

CONFIG_PADRAO: dict = {
    "dest": "gdrive",
    "quality": "Melhor disponível",
    "gdrive_folder": "",
    "credentials_path": str(BASE_DIR / "gdrive_credentials.json"),
    "keep_local": False,
    "local_dir": str(BASE_DIR / "downloads"),
    "playlist": False,
    "cookies_browser": "Nenhum",
    "cookies_file": "",
    "mega_email": "",
    "mega_folder": "",
    "workers": "2",
    "limit_mbps": "",
    "use_aria2": True,
}
QUALIDADES = ["Melhor disponível", "1080p", "720p", "480p", "360p"]
QUAL_MAP = {"Melhor disponível": None, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
NAVEGADORES = ["Nenhum"] + vu.BROWSERS
HAS_ARIA2 = shutil.which("aria2c") is not None

TAB_DRIVE, TAB_MEGA, TAB_LOCAL = "☁  Google Drive", "🔷  MEGA", "💾  Só baixar"
TAB_FILA, TAB_LOG = "📋  Fila e histórico", "📜  Log"

ICONES = {st.QUEUED: "⏳", st.RUNNING: "⬇", st.DONE: "✅", st.ERROR: "❌", st.CANCELLED: "⏹", st.INTERRUPTED: "⏸"}
TEXTOS = {st.QUEUED: "Na fila", st.RUNNING: "Em andamento", st.DONE: "Concluído", st.ERROR: "Erro",
          st.CANCELLED: "Cancelado (pode retomar)", st.INTERRUPTED: "Interrompido (pode retomar)"}


def carregar_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            dados = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            dados.pop("mega_password", None)   # versões antigas gravavam a senha aqui
            dados.pop("folder_id", None)       # agora é "nome da pasta" (o ID antigo causava erro 404)
            return {**CONFIG_PADRAO, **dados}
        except Exception:
            pass
    return dict(CONFIG_PADRAO)


def salvar_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def ler_senha(email: str) -> str:
    kr = _keyring()
    if kr and email:
        try:
            return kr.get_password(KEYRING_SERVICE, email) or ""
        except Exception:
            pass
    return os.environ.get("MEGA_PASSWORD", "")


def gravar_senha(email: str, senha: str) -> bool:
    kr = _keyring()
    if kr and email and senha:
        try:
            kr.set_password(KEYRING_SERVICE, email, senha)
            return True
        except Exception:
            pass
    return False


def abrir_pasta(caminho: str):
    p = Path(caminho)
    alvo = p if p.is_dir() else p.parent
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(alvo))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(alvo)])
        else:
            subprocess.Popen(["xdg-open", str(alvo)])
    except Exception:
        pass


def _curto(txt: str, n: int) -> str:
    txt = " ".join((txt or "").split())
    return txt if len(txt) <= n else txt[: n - 1] + "…"


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class JobRow:
    """Uma linha da lista 'Fila e histórico'."""

    def __init__(self, app: "App", parent, job: dict):
        self.app = app
        self.jid = job["id"]
        self.job = job
        self.indeterminado = False
        self.frame = ctk.CTkFrame(parent, corner_radius=8, fg_color=("#e8eaed", "#1c2128"))
        self.frame.grid_columnconfigure(0, weight=1)

        self.lbl_titulo = ctk.CTkLabel(self.frame, text="", anchor="w", font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_titulo.grid(row=0, column=0, sticky="ew", padx=(10, 4), pady=(6, 0))
        self.lbl_info = ctk.CTkLabel(self.frame, text="", anchor="w", font=ctk.CTkFont(size=11),
                                     text_color=("#37474f", "#90a4ae"))
        self.lbl_info.grid(row=1, column=0, sticky="ew", padx=(10, 4))
        self.barra = ctk.CTkProgressBar(self.frame, height=6)
        self.barra.set(0)
        self.barra.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 6))

        bt = ctk.CTkFrame(self.frame, fg_color="transparent")
        bt.grid(row=0, column=1, rowspan=3, padx=(0, 8))
        mk = lambda txt, cmd, w=34: ctk.CTkButton(bt, text=txt, width=w, height=26, command=cmd,  # noqa: E731
                                                  fg_color=("#cfd8dc", "#263238"), hover_color=("#b0bec5", "#37474f"),
                                                  text_color=("#000000", "#ffffff"))
        self.b_link = mk("🔗 Copiar link", self._copiar, 104)
        self.b_pasta = mk("📂 Abrir", self._abrir, 70)
        self.b_retomar = mk("↻ Retomar", self._retomar, 84)
        self.b_parar = mk("⏹", self._parar)
        self.b_apagar = mk("🗑", self._apagar)
        self.atualizar(job)

    # ── ações ────────────────────────────────────────────────
    def _links(self) -> list[str]:
        return [x for x in (self.job.get("link") or "").splitlines() if x.strip()]

    def _copiar(self):
        self.app.clipboard_clear()
        self.app.clipboard_append("\n".join(self._links()))
        self.app.aviso_rapido("✓ Link copiado")

    def _abrir(self):
        links = self._links()
        if links:
            abrir_pasta(links[0])

    def _retomar(self):
        self.app.retomar(self.jid)

    def _parar(self):
        self.app.runner.cancel(self.jid)

    def _apagar(self):
        self.app.remover(self.jid)

    # ── estado ───────────────────────────────────────────────
    def atualizar(self, job: dict):
        self.job = job
        s = job["status"]
        titulo = job.get("title") or job["url"]
        destino = {"gdrive": "Drive", "mega": "MEGA", "local": "local"}.get(job["dest"], job["dest"])
        self.lbl_titulo.configure(text=f"{ICONES.get(s, '?')}  {_curto(titulo, 80)}")
        extra = ""
        if s == st.DONE:
            tam = vu._fmt_bytes(job["size"]) if job.get("size") else ""
            quando = time.strftime("%d/%m %H:%M", time.localtime(job["finished_at"])) if job.get("finished_at") else ""
            extra = f"{TEXTOS[s]} · {destino} · {tam} · {quando}".replace(" ·  ·", " ·")
        elif s == st.ERROR:
            extra = "Erro: " + _curto(job.get("error") or "", 110)
        elif s in TEXTOS:
            extra = f"{TEXTOS[s]} · {destino}"
        self.lbl_info.configure(text=extra)

        for b in (self.b_link, self.b_pasta, self.b_retomar, self.b_parar, self.b_apagar):
            b.pack_forget()
        if s == st.DONE and self._links():
            (self.b_pasta if job["dest"] == "local" else self.b_link).pack(side="left", padx=2)
        if s in (st.ERROR, st.CANCELLED, st.INTERRUPTED):
            self.b_retomar.pack(side="left", padx=2)
        if s in (st.QUEUED, st.RUNNING):
            self.b_parar.pack(side="left", padx=2)
        else:
            self.b_apagar.pack(side="left", padx=2)

        if s == st.RUNNING:
            self.barra.grid()
        else:
            self._parar_indeterminado()
            self.barra.grid_remove()

    def _parar_indeterminado(self):
        if self.indeterminado:
            self.barra.stop()
            self.barra.configure(mode="determinate")
            self.indeterminado = False

    def progresso(self, frac: float, texto: str):
        if frac < 0:
            if not self.indeterminado:
                self.barra.configure(mode="indeterminate")
                self.barra.start()
                self.indeterminado = True
        else:
            self._parar_indeterminado()
            self.barra.set(min(frac, 1.0))
        self.lbl_info.configure(text=_curto(texto, 120))


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("🎬 Video Uploader")
        self.geometry("820x940")
        self.minsize(700, 760)

        self._cfg = carregar_config()
        self._fila: queue.Queue = queue.Queue()   # threads de trabalho → thread da interface
        self._rows: dict[int, JobRow] = {}
        self._ordem: list[int] = []               # ids na ordem em que aparecem (topo primeiro)
        self._sessao: set[int] = set()            # tarefas adicionadas desde que a fila esvaziou
        self._frac: dict[int, float] = {}

        WORK_ROOT.mkdir(exist_ok=True)
        self.store = st.Store(DB_FILE)
        recuperadas = self.store.recover_interrupted()
        self.runner = rn.Runner(self.store, WORK_ROOT, on_event=self._evento_thread,
                                workers=self._workers_cfg())

        self._build_ui()
        self.after(50, self._drenar_fila)
        self._popular_campos()
        self._carregar_historico()
        self.protocol("WM_DELETE_WINDOW", self._fechar)

        if not vu.find_ffmpeg():
            self._log("⚠ ffmpeg não encontrado. Sem ele, vídeos com áudio e vídeo separados "
                      "(maioria dos sites) saem em qualidade menor. Instale e coloque no PATH.", "aviso")
        if not HAS_ARIA2:
            self._log("ℹ aria2c não encontrado: os downloads usam 1 conexão (podem ser bem mais lentos). "
                      "Linux: sudo apt install aria2", "info")
        if recuperadas:
            self._log(f"⏸ {recuperadas} tarefa(s) foram interrompidas na última vez. "
                      "Clique em 'Retomar interrompidos' na aba Fila e histórico.", "aviso")

    # ── UI ────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(8, weight=1)

        hdr = ctk.CTkFrame(self, corner_radius=0, fg_color=("#1a1a2e", "#0f0f23"))
        hdr.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(hdr, text="🎬  Video Uploader", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color="#4fc3f7").pack(pady=(12, 2))
        ctk.CTkLabel(hdr, text="Baixa vídeos via yt-dlp (1000+ sites) e envia para Google Drive, MEGA ou pasta local",
                     font=ctk.CTkFont(size=12), text_color="#90a4ae").pack(pady=(0, 10))

        # URLs
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.grid(row=1, column=0, sticky="ew", padx=20, pady=(12, 4))
        f.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(f, text="URLs dos vídeos (uma por linha):", anchor="w").grid(row=0, column=0, sticky="w")
        self._txt_urls = ctk.CTkTextbox(f, height=70)
        self._txt_urls.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        # Opções 1
        o = ctk.CTkFrame(self, fg_color="transparent")
        o.grid(row=2, column=0, sticky="ew", padx=20, pady=3)
        ctk.CTkLabel(o, text="Qualidade:").grid(row=0, column=0, sticky="w")
        self._var_qual = ctk.StringVar(value="Melhor disponível")
        ctk.CTkOptionMenu(o, variable=self._var_qual, values=QUALIDADES, width=150).grid(row=0, column=1, padx=8)
        self._var_playlist = ctk.BooleanVar()
        ctk.CTkCheckBox(o, text="Playlist inteira", variable=self._var_playlist).grid(row=0, column=2, padx=(16, 0))
        self._var_keep = ctk.BooleanVar()
        ctk.CTkCheckBox(o, text="Manter cópia local", variable=self._var_keep).grid(row=0, column=3, padx=(16, 0))

        # Opções 2: paralelo, limite, aria2
        o2 = ctk.CTkFrame(self, fg_color="transparent")
        o2.grid(row=3, column=0, sticky="ew", padx=20, pady=3)
        ctk.CTkLabel(o2, text="Ao mesmo tempo:").grid(row=0, column=0, sticky="w")
        self._var_workers = ctk.StringVar(value="2")
        ctk.CTkOptionMenu(o2, variable=self._var_workers, values=["1", "2", "3", "4"], width=64,
                          command=lambda v: self.runner.set_workers(int(v))).grid(row=0, column=1, padx=8)
        ctk.CTkLabel(o2, text="Limite:").grid(row=0, column=2, padx=(12, 0))
        self._var_limit = ctk.StringVar()
        ctk.CTkEntry(o2, textvariable=self._var_limit, width=84, placeholder_text="sem limite"
                     ).grid(row=0, column=3, padx=6)
        ctk.CTkLabel(o2, text="MB/s").grid(row=0, column=4)
        self._var_aria2 = ctk.BooleanVar(value=True)
        self._chk_aria2 = ctk.CTkCheckBox(o2, text="Download acelerado (aria2c)", variable=self._var_aria2,
                                          state="normal" if HAS_ARIA2 else "disabled")
        self._chk_aria2.grid(row=0, column=5, padx=(16, 0))

        # Cookies
        c = ctk.CTkFrame(self, fg_color="transparent")
        c.grid(row=4, column=0, sticky="ew", padx=20, pady=3)
        c.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(c, text="Cookies (login/idade):").grid(row=0, column=0, sticky="w")
        self._var_cbrowser = ctk.StringVar(value="Nenhum")
        ctk.CTkOptionMenu(c, variable=self._var_cbrowser, values=NAVEGADORES, width=110).grid(row=0, column=1, padx=8)
        ctk.CTkLabel(c, text="ou cookies.txt:").grid(row=0, column=2, padx=(8, 4))
        self._var_cfile = ctk.StringVar()
        ctk.CTkEntry(c, textvariable=self._var_cfile).grid(row=0, column=3, sticky="ew")
        ctk.CTkButton(c, text="📂", width=34, command=self._browse_cookies).grid(row=0, column=4, padx=(4, 0))

        # Abas de destino
        self._tabs = ctk.CTkTabview(self, height=178)
        self._tabs.grid(row=5, column=0, sticky="ew", padx=20, pady=(6, 4))
        for t in (TAB_DRIVE, TAB_MEGA, TAB_LOCAL):
            self._tabs.add(t)
        self._build_tab_gdrive()
        self._build_tab_mega()
        self._build_tab_local()
        self._tabs.set({"mega": TAB_MEGA, "local": TAB_LOCAL}.get(self._cfg.get("dest"), TAB_DRIVE))

        # Botões
        b = ctk.CTkFrame(self, fg_color="transparent")
        b.grid(row=6, column=0, sticky="ew", padx=20, pady=(6, 2))
        b.grid_columnconfigure(0, weight=1)
        self._btn_iniciar = ctk.CTkButton(b, text="➕  Adicionar à fila e iniciar",
                                          font=ctk.CTkFont(size=14, weight="bold"), height=42,
                                          fg_color="#1565c0", hover_color="#0d47a1", command=self._adicionar)
        self._btn_iniciar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._btn_parar = ctk.CTkButton(b, text="⏹ Parar tudo", width=110, height=42, fg_color="#b71c1c",
                                        hover_color="#7f0000", state="disabled", command=self._parar_tudo)
        self._btn_parar.grid(row=0, column=1)

        # Progresso geral
        self._progress = ctk.CTkProgressBar(self, height=12)
        self._progress.set(0)
        self._progress.grid(row=7, column=0, sticky="ew", padx=20, pady=(6, 0))

        # Fila/Log
        self._bottom = ctk.CTkTabview(self)
        self._bottom.grid(row=8, column=0, sticky="nsew", padx=20, pady=(2, 4))
        self._bottom.add(TAB_FILA)
        self._bottom.add(TAB_LOG)

        fila = self._bottom.tab(TAB_FILA)
        fila.grid_columnconfigure(0, weight=1)
        fila.grid_rowconfigure(1, weight=1)
        topo = ctk.CTkFrame(fila, fg_color="transparent")
        topo.grid(row=0, column=0, sticky="ew")
        self._lbl_resumo = ctk.CTkLabel(topo, text="", anchor="w", font=ctk.CTkFont(size=11),
                                        text_color=("#37474f", "#80cbc4"))
        self._lbl_resumo.pack(side="left", padx=4)
        ctk.CTkButton(topo, text="🧹 Limpar concluídos", width=140, height=26, fg_color=("#cfd8dc", "#263238"),
                      hover_color=("#b0bec5", "#37474f"), text_color=("#000000", "#ffffff"),
                      command=self._limpar_concluidos).pack(side="right", padx=2)
        ctk.CTkButton(topo, text="↻ Retomar interrompidos", width=160, height=26, fg_color=("#cfd8dc", "#263238"),
                      hover_color=("#b0bec5", "#37474f"), text_color=("#000000", "#ffffff"),
                      command=self._retomar_todos).pack(side="right", padx=2)
        self._lista = ctk.CTkScrollableFrame(fila, fg_color="transparent")
        self._lista.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self._lista.grid_columnconfigure(0, weight=1)

        logf = self._bottom.tab(TAB_LOG)
        logf.grid_columnconfigure(0, weight=1)
        logf.grid_rowconfigure(0, weight=1)
        self._log_box = ctk.CTkTextbox(logf, font=ctk.CTkFont(family="Consolas", size=11), state="disabled", wrap="word")
        self._log_box.grid(row=0, column=0, sticky="nsew")
        for tag, cor in (("ok", "#a5d6a7"), ("vermelho", "#ef9a9a"), ("aviso", "#fff59d"), ("info", "#90caf9")):
            self._log_box._textbox.tag_config(tag, foreground=cor)

        # Rodapé
        r = ctk.CTkFrame(self, fg_color="transparent")
        r.grid(row=9, column=0, sticky="ew", padx=20, pady=(0, 10))
        r.grid_columnconfigure(0, weight=1)
        self._lbl_status = ctk.CTkLabel(r, text="Configurações em config.json · fila em queue.db",
                                        font=ctk.CTkFont(size=10), text_color="#546e7a")
        self._lbl_status.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(r, text="⟳ Atualizar yt-dlp", width=140, height=28, fg_color="#263238", hover_color="#37474f",
                      command=self._atualizar_ytdlp).grid(row=0, column=1, padx=6)
        ctk.CTkButton(r, text="💾 Salvar", width=90, height=28, fg_color="#263238", hover_color="#37474f",
                      command=self._salvar).grid(row=0, column=2)

    def _linha_arquivo(self, tab, row, label, var, browse):
        ctk.CTkLabel(tab, text=label, anchor="w", width=150).grid(row=row, column=0, sticky="w", pady=5)
        ctk.CTkEntry(tab, textvariable=var, height=32).grid(row=row, column=1, sticky="ew", padx=(8, 4))
        ctk.CTkButton(tab, text="📂", width=36, height=32, command=browse).grid(row=row, column=2)

    def _linha_teste(self, tab, row, comando):
        """Botão 'Testar destino' + resultado."""
        fr = ctk.CTkFrame(tab, fg_color="transparent")
        fr.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        fr.grid_columnconfigure(1, weight=1)
        btn = ctk.CTkButton(fr, text="🔎 Testar destino", width=130, height=28, command=comando)
        btn.grid(row=0, column=0)
        lbl = ctk.CTkLabel(fr, text="", anchor="w", justify="left", wraplength=560, font=ctk.CTkFont(size=11))
        lbl.grid(row=0, column=1, sticky="ew", padx=10)
        return btn, lbl

    def _build_tab_gdrive(self):
        tab = self._tabs.tab(TAB_DRIVE)
        tab.grid_columnconfigure(1, weight=1)
        self._var_cred = ctk.StringVar()
        self._linha_arquivo(tab, 0, "credentials.json:", self._var_cred, self._browse_credentials)
        ctk.CTkLabel(tab, text="Nome da pasta (opcional):", anchor="w", width=150).grid(row=1, column=0, sticky="w", pady=5)
        self._var_gfolder = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._var_gfolder, height=32,
                     placeholder_text="ex.: Vídeos  ou  Vídeos/2026  (criada se não existir; vazio = raiz)"
                     ).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        self._btn_teste_drive, self._lbl_teste_drive = self._linha_teste(tab, 2, self._testar_drive)

    def _build_tab_mega(self):
        tab = self._tabs.tab(TAB_MEGA)
        tab.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tab, text="E-mail:", anchor="w", width=150).grid(row=0, column=0, sticky="w", pady=4)
        self._var_mega_email = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._var_mega_email, height=32).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ctk.CTkLabel(tab, text="Senha:", anchor="w", width=150).grid(row=1, column=0, sticky="w", pady=4)
        self._var_mega_pw = ctk.StringVar()
        self._entry_pw = ctk.CTkEntry(tab, textvariable=self._var_mega_pw, show="●", height=32)
        self._entry_pw.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self._var_show = ctk.BooleanVar()
        ctk.CTkCheckBox(tab, text="Mostrar", variable=self._var_show, width=70,
                        command=lambda: self._entry_pw.configure(show="" if self._var_show.get() else "●")
                        ).grid(row=1, column=2, padx=8)
        ctk.CTkLabel(tab, text="Pasta (opcional):", anchor="w", width=150).grid(row=2, column=0, sticky="w", pady=4)
        self._var_mega_folder = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._var_mega_folder, height=32,
                     placeholder_text="nome da pasta no MEGA (vazio = raiz)").grid(row=2, column=1, sticky="ew", padx=(8, 0))
        self._btn_teste_mega, self._lbl_teste_mega = self._linha_teste(tab, 3, self._testar_mega)
        dica = ("Senha lembrada no cofre do sistema (se 'keyring' instalado)." if _keyring()
                else "Senha não é salva no disco (instale 'keyring' para lembrar com segurança).")
        ctk.CTkLabel(tab, text=dica, font=ctk.CTkFont(size=10), text_color="#78909c"
                     ).grid(row=4, column=0, columnspan=3, sticky="w")

    def _build_tab_local(self):
        tab = self._tabs.tab(TAB_LOCAL)
        tab.grid_columnconfigure(1, weight=1)
        self._var_local = ctk.StringVar()
        self._linha_arquivo(tab, 0, "Salvar em:", self._var_local, self._browse_local)
        ctk.CTkLabel(tab, text="Só baixa o vídeo para essa pasta, sem enviar para a nuvem.",
                     font=ctk.CTkFont(size=10), text_color="#78909c").grid(row=1, column=0, columnspan=3, sticky="w")

    # ── Config ────────────────────────────────────────────────
    def _workers_cfg(self) -> int:
        try:
            return max(1, min(4, int(self._cfg.get("workers", "2"))))
        except (TypeError, ValueError):
            return 2

    def _popular_campos(self):
        c = self._cfg
        self._var_qual.set(c["quality"])
        self._var_keep.set(c["keep_local"])
        self._var_playlist.set(c["playlist"])
        self._var_cbrowser.set(c["cookies_browser"])
        self._var_cfile.set(c["cookies_file"])
        self._var_cred.set(c["credentials_path"])
        self._var_gfolder.set(c["gdrive_folder"])
        self._var_mega_email.set(c["mega_email"])
        self._var_mega_folder.set(c["mega_folder"])
        self._var_local.set(c["local_dir"])
        self._var_mega_pw.set(ler_senha(c["mega_email"]))
        self._var_workers.set(str(self._workers_cfg()))
        self._var_limit.set(c["limit_mbps"])
        self._var_aria2.set(bool(c["use_aria2"]) and HAS_ARIA2)

    def _coletar_config(self) -> dict:
        aba = self._tabs.get()
        dest = "mega" if aba == TAB_MEGA else "local" if aba == TAB_LOCAL else "gdrive"
        return {
            "dest": dest,
            "quality": self._var_qual.get(),
            "gdrive_folder": self._var_gfolder.get().strip(),
            "credentials_path": self._var_cred.get().strip(),
            "keep_local": self._var_keep.get(),
            "local_dir": self._var_local.get().strip() or str(BASE_DIR / "downloads"),
            "playlist": self._var_playlist.get(),
            "cookies_browser": self._var_cbrowser.get(),
            "cookies_file": self._var_cfile.get().strip(),
            "mega_email": self._var_mega_email.get().strip(),
            "mega_folder": self._var_mega_folder.get().strip(),
            "workers": self._var_workers.get(),
            "limit_mbps": self._var_limit.get().strip(),
            "use_aria2": self._var_aria2.get(),
        }

    def _salvar(self):
        self._cfg = self._coletar_config()
        salvar_config(self._cfg)
        gravar_senha(self._cfg["mega_email"], self._var_mega_pw.get())
        self.aviso_rapido("✓ Salvo!")

    def aviso_rapido(self, texto: str):
        self._lbl_status.configure(text=texto, text_color="#80cbc4")
        self.after(2500, lambda: self._lbl_status.configure(
            text="Configurações em config.json · fila em queue.db", text_color="#546e7a"))

    def _browse_credentials(self):
        p = filedialog.askopenfilename(title="Selecionar credentials.json", filetypes=[("JSON", "*.json")])
        if p:
            self._var_cred.set(p)

    def _browse_cookies(self):
        p = filedialog.askopenfilename(title="Selecionar cookies.txt", filetypes=[("Texto", "*.txt"), ("Todos", "*.*")])
        if p:
            self._var_cfile.set(p)

    def _browse_local(self):
        p = filedialog.askdirectory(title="Pasta de destino")
        if p:
            self._var_local.set(p)

    # ── Helpers thread-safe ───────────────────────────────────
    def _ui(self, fn, *a):
        """Chamável de qualquer thread: agenda fn(*a) para rodar na thread da interface."""
        self._fila.put((fn, a))

    def _drenar_fila(self):
        try:
            while True:
                fn, a = self._fila.get_nowait()
                fn(*a)
        except queue.Empty:
            pass
        self.after(50, self._drenar_fila)

    def _log(self, msg: str, cor: str = ""):
        self._log_box.configure(state="normal")
        if cor:
            self._log_box._textbox.insert("end", msg + "\n", cor)
        else:
            self._log_box._textbox.insert("end", msg + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _log_auto(self, msg: str, prefixo: str = ""):
        cor = "ok" if msg.startswith("✓") else "vermelho" if msg.startswith(("✗", "❌")) else \
              "aviso" if msg.startswith(("⚠", "↻")) else "info"
        self._ui(self._log, prefixo + msg, cor)

    # ── Fila / histórico ──────────────────────────────────────
    def _carregar_historico(self):
        for job in reversed(self.store.list_jobs(100)):   # mais antigo primeiro, cada um entra no topo
            self._add_row(job, topo=True)
        self._atualizar_resumo()

    def _add_row(self, job: dict, topo: bool = True):
        row = JobRow(self, self._lista, job)
        if topo and self._ordem:
            row.frame.pack(fill="x", pady=3, before=self._rows[self._ordem[0]].frame)
        else:
            row.frame.pack(fill="x", pady=3)
        self._rows[job["id"]] = row
        if topo:
            self._ordem.insert(0, job["id"])
        else:
            self._ordem.append(job["id"])

    def _atualizar_resumo(self):
        n = len(self._rows)
        feitos = sum(1 for r in self._rows.values() if r.job["status"] == st.DONE)
        pend = sum(1 for r in self._rows.values() if r.job["status"] in (st.QUEUED, st.RUNNING))
        parados = sum(1 for r in self._rows.values() if r.job["status"] in (st.INTERRUPTED, st.CANCELLED, st.ERROR))
        self._lbl_resumo.configure(text=f"{n} tarefa(s) · {feitos} concluída(s) · {pend} ativa(s) · {parados} parada(s)")

    def _atualizar_progresso_geral(self):
        if not self._sessao:
            self._progress.stop()
            self._progress.configure(mode="determinate")
            self._progress.set(0)
            return
        total = 0.0
        for jid in self._sessao:
            row = self._rows.get(jid)
            s = row.job["status"] if row else st.DONE
            total += 1.0 if s in (st.DONE,) else self._frac.get(jid, 0.0) if s == st.RUNNING else 0.0
        self._progress.stop()
        self._progress.configure(mode="determinate")
        self._progress.set(min(total / len(self._sessao), 1.0))

    def _evento_thread(self, jid, tipo, dados):
        """Chamado pelas threads do runner."""
        self._ui(self._evento, jid, tipo, dados)

    def _evento(self, jid, tipo, dados):
        if tipo == "idle":
            self._fim_da_fila()
            return
        if tipo == "log":
            prefixo = f"[#{jid}] " if len(self._sessao) > 1 else ""
            self._log_auto(str(dados), prefixo)
            return
        if tipo == "status":
            job = self.store.get(jid)
            if not job:
                return
            if jid not in self._rows:
                self._add_row(job, topo=True)
            else:
                self._rows[jid].atualizar(job)
            if dados in (st.DONE,):
                self._frac[jid] = 1.0
            self._atualizar_resumo()
            self._atualizar_progresso_geral()
        elif tipo == "progress":
            frac, texto = dados
            row = self._rows.get(jid)
            if row:
                row.progresso(frac, texto)
            if frac >= 0:
                self._frac[jid] = frac
                self._atualizar_progresso_geral()

    def _fim_da_fila(self):
        feitos = sum(1 for j in self._sessao if j in self._rows and self._rows[j].job["status"] == st.DONE)
        erros = sum(1 for j in self._sessao if j in self._rows and self._rows[j].job["status"] == st.ERROR)
        parados = sum(1 for j in self._sessao if j in self._rows
                      and self._rows[j].job["status"] in (st.CANCELLED, st.INTERRUPTED))
        cor = "ok" if not erros and not parados else "aviso"
        self._log(f"Fim: {feitos} ok, {erros} com erro, {parados} parada(s).", cor)
        self._btn_parar.configure(state="disabled")
        self._lbl_status.configure(text="Pronto.", text_color="#546e7a")
        self._atualizar_resumo()
        self._sessao.clear()
        self._frac.clear()
        self._progress.set(1.0 if feitos and not erros and not parados else 0)

    # ── Ações ─────────────────────────────────────────────────
    def _senha_para(self, job: dict) -> str:
        if job["dest"] != "mega":
            return ""
        email = (job.get("options") or {}).get("mega_email", "")
        if email and email == self._var_mega_email.get().strip() and self._var_mega_pw.get():
            return self._var_mega_pw.get()
        return ler_senha(email)

    def _enfileirar(self, jid: int, senha: str = ""):
        self._sessao.add(jid)
        self._frac.pop(jid, None)
        self._btn_parar.configure(state="normal")
        self.runner.submit(jid, senha)

    def _adicionar(self):
        urls = []
        for u in (x.strip() for x in self._txt_urls.get("1.0", "end").splitlines()):
            if u and u not in urls:
                urls.append(u)
        if not urls:
            messagebox.showwarning("URL vazia", "Cole ao menos uma URL de vídeo.")
            return
        cfg = self._coletar_config()
        senha = self._var_mega_pw.get()

        if cfg["dest"] == "gdrive" and not Path(cfg["credentials_path"]).exists():
            messagebox.showerror("Credencial ausente", f"Arquivo não encontrado:\n{cfg['credentials_path']}\n\n"
                                 "Baixe em console.cloud.google.com → Credenciais → OAuth (Desktop).")
            return
        if cfg["dest"] == "mega" and (not cfg["mega_email"] or not senha):
            messagebox.showwarning("MEGA", "Informe e-mail e senha do MEGA.")
            return
        limite = None
        if cfg["limit_mbps"]:
            try:
                limite = int(float(cfg["limit_mbps"].replace(",", ".")) * 1048576)
                if limite <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("Limite", "Limite de velocidade inválido. Use um número (ex.: 2 ou 1,5) "
                                       "ou deixe vazio.")
                return

        repetidas = [u for u in urls if self.store.find_done(u)]
        if repetidas and not messagebox.askyesno(
                "Já enviadas antes", f"{len(repetidas)} URL(s) já foram concluídas antes (veja o histórico).\n\n"
                "Enviar de novo mesmo assim?"):
            urls = [u for u in urls if u not in repetidas]
            if not urls:
                return

        self._salvar()
        self.runner.set_workers(int(cfg["workers"]))
        opts = dict(
            quality=QUAL_MAP.get(cfg["quality"]), playlist=cfg["playlist"], keep_local=cfg["keep_local"],
            local_dir=cfg["local_dir"], cookies_file=cfg["cookies_file"] or None,
            cookies_from_browser=None if cfg["cookies_browser"] == "Nenhum" else cfg["cookies_browser"],
            gdrive_folder=cfg["gdrive_folder"] or None, credentials_file=cfg["credentials_path"],
            mega_email=cfg["mega_email"], mega_folder=cfg["mega_folder"] or None,
            use_aria2=bool(cfg["use_aria2"]) and HAS_ARIA2, rate_limit=limite,
        )
        for u in urls:
            jid = self.store.add_job(u, cfg["dest"], opts)
            self._add_row(self.store.get(jid), topo=True)
            self._enfileirar(jid, senha)
        self._txt_urls.delete("1.0", "end")
        self._bottom.set(TAB_FILA)
        self._atualizar_resumo()

    def retomar(self, jid: int):
        job = self.store.get(jid)
        if job:
            self._enfileirar(jid, self._senha_para(job))

    def _retomar_todos(self):
        n = 0
        for jid in list(self._ordem)[::-1]:   # mais antigo primeiro
            row = self._rows.get(jid)
            if row and row.job["status"] in (st.INTERRUPTED, st.CANCELLED):
                self.retomar(jid)
                n += 1
        self.aviso_rapido(f"↻ {n} tarefa(s) retomada(s)" if n else "Nada para retomar")

    def remover(self, jid: int):
        self.runner.forget(jid)
        row = self._rows.pop(jid, None)
        if row:
            row.frame.destroy()
        if jid in self._ordem:
            self._ordem.remove(jid)
        self._sessao.discard(jid)
        self._atualizar_resumo()

    def _limpar_concluidos(self):
        for jid in [j for j, r in self._rows.items() if r.job["status"] == st.DONE]:
            self.remover(jid)

    def _parar_tudo(self):
        self.runner.cancel_all()
        self._lbl_status.configure(text="Parando...", text_color="#ef9a9a")

    # ── Testar destino ────────────────────────────────────────
    def _testar_drive(self):
        cred = Path(self._var_cred.get().strip())
        pasta = self._var_gfolder.get().strip()
        if not cred.exists():
            self._lbl_teste_drive.configure(text=f"❌ credentials.json não encontrado: {cred}", text_color="#ef9a9a")
            return
        self._btn_teste_drive.configure(state="disabled")
        self._lbl_teste_drive.configure(text="Testando... (se abrir o navegador, autorize o acesso)",
                                        text_color="#fff59d")

        def tarefa():
            try:
                _fid, info = vu.preflight_gdrive(cred, None, pasta or None, use_cache=False,
                                                 log=lambda m: self._log_auto(m))
                self._ui(self._fim_teste, self._btn_teste_drive, self._lbl_teste_drive, True, f"✓ {info}")
            except Exception as e:  # noqa: BLE001
                self._ui(self._fim_teste, self._btn_teste_drive, self._lbl_teste_drive, False, f"❌ {e}")

        threading.Thread(target=tarefa, daemon=True).start()

    def _testar_mega(self):
        email, senha = self._var_mega_email.get().strip(), self._var_mega_pw.get()
        if not email or not senha:
            self._lbl_teste_mega.configure(text="❌ Informe e-mail e senha.", text_color="#ef9a9a")
            return
        self._btn_teste_mega.configure(state="disabled")
        self._lbl_teste_mega.configure(text="Testando...", text_color="#fff59d")

        def tarefa():
            try:
                info = vu.preflight_mega(email, senha, use_cache=False)
                self._ui(self._fim_teste, self._btn_teste_mega, self._lbl_teste_mega, True, f"✓ {info}")
            except Exception as e:  # noqa: BLE001
                self._ui(self._fim_teste, self._btn_teste_mega, self._lbl_teste_mega, False, f"❌ {e}")

        threading.Thread(target=tarefa, daemon=True).start()

    def _fim_teste(self, btn, lbl, ok: bool, texto: str):
        btn.configure(state="normal")
        lbl.configure(text=texto, text_color="#a5d6a7" if ok else "#ef9a9a")

    # ── Outros ────────────────────────────────────────────────
    def _atualizar_ytdlp(self):
        self._log("⟳ Atualizando yt-dlp...", "info")
        threading.Thread(target=lambda: vu.update_ytdlp(self._log_auto), daemon=True).start()

    def _fechar(self):
        if self.runner.is_busy():
            if not messagebox.askyesno("Fechar", "Há downloads em andamento.\n\nEles serão interrompidos e "
                                       "poderão ser retomados da próxima vez. Fechar mesmo assim?"):
                return
            self.runner.cancel_all()
            try:   # não deixar o aria2c órfão rodando depois que a janela fechar
                import psutil
                for ch in psutil.Process().children(recursive=True):
                    ch.kill()
            except Exception:  # noqa: BLE001
                pass
        self.destroy()


if __name__ == "__main__":
    App().mainloop()