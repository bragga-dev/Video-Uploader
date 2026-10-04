"""
gui.py — Interface gráfica do Video Uploader.

Rodar:      python gui.py
Gerar .exe: veja o README.md

Configurações ficam em config.json (ao lado do script/exe). A senha do MEGA NÃO é gravada em
texto puro: só é lembrada se o pacote 'keyring' estiver instalado (usa o cofre do sistema).
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
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

import video_uploader as vu  # noqa: E402

CONFIG_FILE = BASE_DIR / "config.json"
KEYRING_SERVICE = "VideoUploader-MEGA"

CONFIG_PADRAO: dict = {
    "dest": "gdrive",
    "quality": "Melhor disponível",
    "folder_id": "",
    "credentials_path": str(BASE_DIR / "gdrive_credentials.json"),
    "keep_local": False,
    "local_dir": str(BASE_DIR / "downloads"),
    "playlist": False,
    "cookies_browser": "Nenhum",
    "cookies_file": "",
    "mega_email": "",
    "mega_folder": "",
}
QUALIDADES = ["Melhor disponível", "1080p", "720p", "480p", "360p"]
QUAL_MAP = {"Melhor disponível": None, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
NAVEGADORES = ["Nenhum"] + vu.BROWSERS

TAB_DRIVE, TAB_MEGA, TAB_LOCAL = "☁  Google Drive", "🔷  MEGA", "💾  Só baixar"


def carregar_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            dados = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            dados.pop("mega_password", None)   # versões antigas gravavam a senha aqui
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


ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("🎬 Video Uploader")
        self.geometry("720x820")
        self.minsize(620, 700)

        self._cfg = carregar_config()
        self._cancel = threading.Event()
        self._rodando = False
        self._fila: queue.Queue = queue.Queue()   # threads de trabalho → thread da interface

        self._build_ui()
        self.after(50, self._drenar_fila)
        self._popular_campos()

        if not vu.find_ffmpeg():
            self._log("⚠ ffmpeg não encontrado. Sem ele, vídeos com áudio e vídeo separados "
                      "(maioria dos sites) saem em qualidade menor. Instale e coloque no PATH.", "aviso")

    # ── UI ────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(8, weight=1)

        hdr = ctk.CTkFrame(self, corner_radius=0, fg_color=("#1a1a2e", "#0f0f23"))
        hdr.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(hdr, text="🎬  Video Uploader", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color="#4fc3f7").pack(pady=(14, 2))
        ctk.CTkLabel(hdr, text="Baixa vídeos via yt-dlp (1000+ sites) e envia para Google Drive, MEGA ou pasta local",
                     font=ctk.CTkFont(size=12), text_color="#90a4ae").pack(pady=(0, 12))

        # URLs (uma por linha)
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.grid(row=1, column=0, sticky="ew", padx=20, pady=(14, 4))
        f.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(f, text="URLs dos vídeos (uma por linha):", anchor="w").grid(row=0, column=0, sticky="w")
        self._txt_urls = ctk.CTkTextbox(f, height=84)
        self._txt_urls.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        # Opções
        o = ctk.CTkFrame(self, fg_color="transparent")
        o.grid(row=2, column=0, sticky="ew", padx=20, pady=4)
        ctk.CTkLabel(o, text="Qualidade:").grid(row=0, column=0, sticky="w")
        self._var_qual = ctk.StringVar(value="Melhor disponível")
        ctk.CTkOptionMenu(o, variable=self._var_qual, values=QUALIDADES, width=150).grid(row=0, column=1, padx=8)
        self._var_playlist = ctk.BooleanVar()
        ctk.CTkCheckBox(o, text="Playlist inteira", variable=self._var_playlist).grid(row=0, column=2, padx=(16, 0))
        self._var_keep = ctk.BooleanVar()
        ctk.CTkCheckBox(o, text="Manter cópia local", variable=self._var_keep).grid(row=0, column=3, padx=(16, 0))

        # Cookies
        c = ctk.CTkFrame(self, fg_color="transparent")
        c.grid(row=3, column=0, sticky="ew", padx=20, pady=4)
        c.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(c, text="Cookies (login/idade):").grid(row=0, column=0, sticky="w")
        self._var_cbrowser = ctk.StringVar(value="Nenhum")
        ctk.CTkOptionMenu(c, variable=self._var_cbrowser, values=NAVEGADORES, width=110).grid(row=0, column=1, padx=8)
        ctk.CTkLabel(c, text="ou cookies.txt:").grid(row=0, column=2, padx=(8, 4))
        self._var_cfile = ctk.StringVar()
        ctk.CTkEntry(c, textvariable=self._var_cfile).grid(row=0, column=3, sticky="ew")
        ctk.CTkButton(c, text="📂", width=34, command=self._browse_cookies).grid(row=0, column=4, padx=(4, 0))

        # Abas de destino
        self._tabs = ctk.CTkTabview(self, height=190)
        self._tabs.grid(row=4, column=0, sticky="ew", padx=20, pady=(8, 4))
        for t in (TAB_DRIVE, TAB_MEGA, TAB_LOCAL):
            self._tabs.add(t)
        self._build_tab_gdrive()
        self._build_tab_mega()
        self._build_tab_local()
        self._tabs.set({"mega": TAB_MEGA, "local": TAB_LOCAL}.get(self._cfg.get("dest"), TAB_DRIVE))

        # Botões
        b = ctk.CTkFrame(self, fg_color="transparent")
        b.grid(row=5, column=0, sticky="ew", padx=20, pady=(8, 4))
        b.grid_columnconfigure(0, weight=1)
        self._btn_iniciar = ctk.CTkButton(b, text="▶  Iniciar", font=ctk.CTkFont(size=14, weight="bold"),
                                          height=44, fg_color="#1565c0", hover_color="#0d47a1", command=self._iniciar)
        self._btn_iniciar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._btn_parar = ctk.CTkButton(b, text="⏹ Parar", width=90, height=44, fg_color="#b71c1c",
                                        hover_color="#7f0000", state="disabled", command=self._parar)
        self._btn_parar.grid(row=0, column=1)

        # Progresso
        self._progress = ctk.CTkProgressBar(self, height=14)
        self._progress.set(0)
        self._progress.grid(row=6, column=0, sticky="ew", padx=20, pady=(6, 2))
        self._lbl_prog = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11), text_color="#80cbc4", anchor="w")
        self._lbl_prog.grid(row=7, column=0, sticky="ew", padx=22)

        # Log
        self._log_box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=11), state="disabled", wrap="word")
        self._log_box.grid(row=8, column=0, sticky="nsew", padx=20, pady=(4, 8))
        for tag, cor in (("ok", "#a5d6a7"), ("vermelho", "#ef9a9a"), ("aviso", "#fff59d"), ("info", "#90caf9")):
            self._log_box._textbox.tag_config(tag, foreground=cor)

        # Rodapé
        r = ctk.CTkFrame(self, fg_color="transparent")
        r.grid(row=9, column=0, sticky="ew", padx=20, pady=(0, 10))
        r.grid_columnconfigure(0, weight=1)
        self._lbl_status = ctk.CTkLabel(r, text="Configurações em config.json", font=ctk.CTkFont(size=10),
                                        text_color="#546e7a")
        self._lbl_status.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(r, text="⟳ Atualizar yt-dlp", width=140, height=28, fg_color="#263238", hover_color="#37474f",
                      command=self._atualizar_ytdlp).grid(row=0, column=1, padx=6)
        ctk.CTkButton(r, text="💾 Salvar", width=90, height=28, fg_color="#263238", hover_color="#37474f",
                      command=self._salvar).grid(row=0, column=2)

    def _linha_arquivo(self, tab, row, label, var, browse):
        ctk.CTkLabel(tab, text=label, anchor="w", width=150).grid(row=row, column=0, sticky="w", pady=6)
        ctk.CTkEntry(tab, textvariable=var, height=34).grid(row=row, column=1, sticky="ew", padx=(8, 4))
        ctk.CTkButton(tab, text="📂", width=36, height=34, command=browse).grid(row=row, column=2)

    def _build_tab_gdrive(self):
        tab = self._tabs.tab(TAB_DRIVE)
        tab.grid_columnconfigure(1, weight=1)
        self._var_cred = ctk.StringVar()
        self._linha_arquivo(tab, 0, "credentials.json:", self._var_cred, self._browse_credentials)
        ctk.CTkLabel(tab, text="ID da pasta (opcional):", anchor="w", width=150).grid(row=1, column=0, sticky="w", pady=6)
        self._var_folder_id = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._var_folder_id, height=34,
                     placeholder_text="parte final de drive.google.com/drive/folders/[ID]"
                     ).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        ctk.CTkLabel(tab, text="Na primeira vez o navegador abre para você autorizar o acesso ao Drive.",
                     font=ctk.CTkFont(size=10), text_color="#4fc3f7").grid(row=2, column=0, columnspan=3, sticky="w")

    def _build_tab_mega(self):
        tab = self._tabs.tab(TAB_MEGA)
        tab.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(tab, text="E-mail:", anchor="w", width=150).grid(row=0, column=0, sticky="w", pady=6)
        self._var_mega_email = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._var_mega_email, height=34).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ctk.CTkLabel(tab, text="Senha:", anchor="w", width=150).grid(row=1, column=0, sticky="w", pady=6)
        self._var_mega_pw = ctk.StringVar()
        self._entry_pw = ctk.CTkEntry(tab, textvariable=self._var_mega_pw, show="●", height=34)
        self._entry_pw.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self._var_show = ctk.BooleanVar()
        ctk.CTkCheckBox(tab, text="Mostrar", variable=self._var_show, width=70,
                        command=lambda: self._entry_pw.configure(show="" if self._var_show.get() else "●")
                        ).grid(row=1, column=2, padx=8)
        ctk.CTkLabel(tab, text="Pasta (opcional):", anchor="w", width=150).grid(row=2, column=0, sticky="w", pady=6)
        self._var_mega_folder = ctk.StringVar()
        ctk.CTkEntry(tab, textvariable=self._var_mega_folder, height=34,
                     placeholder_text="nome da pasta no MEGA (vazio = raiz)").grid(row=2, column=1, sticky="ew", padx=(8, 0))
        dica = ("Senha lembrada no cofre do sistema (se 'keyring' instalado)." if _keyring()
                else "Senha não é salva no disco (instale 'keyring' para lembrar com segurança).")
        ctk.CTkLabel(tab, text=dica, font=ctk.CTkFont(size=10), text_color="#78909c"
                     ).grid(row=3, column=0, columnspan=3, sticky="w")

    def _build_tab_local(self):
        tab = self._tabs.tab(TAB_LOCAL)
        tab.grid_columnconfigure(1, weight=1)
        self._var_local = ctk.StringVar()
        self._linha_arquivo(tab, 0, "Salvar em:", self._var_local, self._browse_local)
        ctk.CTkLabel(tab, text="Só baixa o vídeo para essa pasta, sem enviar para a nuvem.",
                     font=ctk.CTkFont(size=10), text_color="#78909c").grid(row=1, column=0, columnspan=3, sticky="w")

    # ── Config ────────────────────────────────────────────────
    def _popular_campos(self):
        c = self._cfg
        self._var_qual.set(c["quality"])
        self._var_keep.set(c["keep_local"])
        self._var_playlist.set(c["playlist"])
        self._var_cbrowser.set(c["cookies_browser"])
        self._var_cfile.set(c["cookies_file"])
        self._var_cred.set(c["credentials_path"])
        self._var_folder_id.set(c["folder_id"])
        self._var_mega_email.set(c["mega_email"])
        self._var_mega_folder.set(c["mega_folder"])
        self._var_local.set(c["local_dir"])
        self._var_mega_pw.set(ler_senha(c["mega_email"]))

    def _coletar_config(self) -> dict:
        aba = self._tabs.get()
        dest = "mega" if aba == TAB_MEGA else "local" if aba == TAB_LOCAL else "gdrive"
        return {
            "dest": dest,
            "quality": self._var_qual.get(),
            "folder_id": self._var_folder_id.get().strip(),
            "credentials_path": self._var_cred.get().strip(),
            "keep_local": self._var_keep.get(),
            "local_dir": self._var_local.get().strip() or str(BASE_DIR / "downloads"),
            "playlist": self._var_playlist.get(),
            "cookies_browser": self._var_cbrowser.get(),
            "cookies_file": self._var_cfile.get().strip(),
            "mega_email": self._var_mega_email.get().strip(),
            "mega_folder": self._var_mega_folder.get().strip(),
        }

    def _salvar(self):
        self._cfg = self._coletar_config()
        salvar_config(self._cfg)
        gravar_senha(self._cfg["mega_email"], self._var_mega_pw.get())
        self._lbl_status.configure(text="✓ Salvo!", text_color="#80cbc4")
        self.after(2500, lambda: self._lbl_status.configure(text="Configurações em config.json", text_color="#546e7a"))

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

    def _set_prog(self, frac: float, texto: str = ""):
        if frac < 0:
            self._progress.configure(mode="indeterminate")
            self._progress.start()
        else:
            self._progress.stop()
            self._progress.configure(mode="determinate")
            self._progress.set(min(frac, 1.0))
        self._lbl_prog.configure(text=texto)

    def _log(self, msg: str, cor: str = ""):
        self._log_box.configure(state="normal")
        if cor:
            self._log_box._textbox.insert("end", msg + "\n", cor)
        else:
            self._log_box._textbox.insert("end", msg + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _log_auto(self, msg: str):
        cor = "ok" if msg.startswith("✓") else "vermelho" if msg.startswith("✗") else \
              "aviso" if msg.startswith(("⚠", "↻")) else "info"
        self._ui(self._log, msg, cor)

    # ── Execução ──────────────────────────────────────────────
    def _iniciar(self):
        urls = [u.strip() for u in self._txt_urls.get("1.0", "end").splitlines() if u.strip()]
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

        self._salvar()
        self._cancel.clear()
        self._rodando = True
        self._btn_iniciar.configure(state="disabled")
        self._btn_parar.configure(state="normal")
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")
        self._set_prog(0, "Iniciando...")
        threading.Thread(target=self._worker, args=(urls, cfg, senha), daemon=True).start()

    def _parar(self):
        self._cancel.set()
        self._lbl_status.configure(text="Parando...", text_color="#ef9a9a")

    def _worker(self, urls: list[str], cfg: dict, senha: str):
        ok = falhas = 0
        cookies_browser = None if cfg["cookies_browser"] == "Nenhum" else cfg["cookies_browser"]
        try:
            for i, url in enumerate(urls, 1):
                if self._cancel.is_set():
                    break
                self._ui(self._log, f"━━ [{i}/{len(urls)}] ━━", "info")

                def prog(f, t, i=i):
                    # mapeia o progresso do item dentro da fila
                    g = f if f < 0 else (i - 1 + f) / len(urls)
                    self._ui(self._set_prog, g, f"[{i}/{len(urls)}] {t}")

                try:
                    vu.process_url(
                        url, dest=cfg["dest"], quality=QUAL_MAP.get(cfg["quality"]), playlist=cfg["playlist"],
                        local_dir=Path(cfg["local_dir"]), keep_local=cfg["keep_local"],
                        cookies_file=cfg["cookies_file"] or None, cookies_from_browser=cookies_browser,
                        folder_id=cfg["folder_id"] or None, credentials_file=Path(cfg["credentials_path"]),
                        mega_email=cfg["mega_email"], mega_password=senha, mega_folder=cfg["mega_folder"] or None,
                        progress=prog, log=self._log_auto, cancel=self._cancel)
                    ok += 1
                except vu.Cancelled:
                    break
                except Exception as e:  # noqa: BLE001
                    falhas += 1
                    self._ui(self._log, f"❌ {e}", "vermelho")
        finally:
            if self._cancel.is_set():
                self._ui(self._log, "⏹ Cancelado.", "aviso")
                self._ui(self._set_prog, 0, "Cancelado")
            else:
                self._ui(self._log, f"Fim: {ok} ok, {falhas} com erro.", "ok" if not falhas else "aviso")
                self._ui(self._set_prog, 1.0 if not falhas else 0, "Concluído" if not falhas else "Terminou com erros")
            self._ui(self._finalizar)

    def _finalizar(self):
        self._rodando = False
        self._btn_iniciar.configure(state="normal")
        self._btn_parar.configure(state="disabled")
        self._lbl_status.configure(text="Pronto.", text_color="#546e7a")

    def _atualizar_ytdlp(self):
        if self._rodando:
            return
        self._log("⟳ Atualizando yt-dlp...", "info")
        threading.Thread(target=lambda: vu.update_ytdlp(self._log_auto), daemon=True).start()


if __name__ == "__main__":
    App().mainloop()