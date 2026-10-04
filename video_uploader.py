"""
video_uploader.py — baixa vídeos com yt-dlp e envia para Google Drive, MEGA ou só salva local.

Pode ser usado como biblioteca (pela gui.py) ou pela linha de comando:

    python video_uploader.py URL [URL ...] --dest gdrive
    python video_uploader.py URL --dest mega --mega-email voce@x.com      (senha via prompt ou MEGA_PASSWORD)
    python video_uploader.py URL --dest local --quality 720
    python video_uploader.py URL --cookies-from-browser firefox           (sites que exigem login/idade)
    python video_uploader.py --update                                     (atualiza o yt-dlp)
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

ProgressCB = Callable[[float, str], None]   # (fração 0..1 ou -1 = indeterminado, texto)
LogCB = Callable[[str], None]

GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
BROWSERS = ["chrome", "firefox", "edge", "brave", "opera", "chromium", "vivaldi", "safari"]


class Cancelled(Exception):
    """Operação cancelada pelo usuário."""


def _noop(*_a, **_k):
    pass


def _check(cancel: Optional[threading.Event]):
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def check_disk_space(path: Path, log: LogCB, min_free_mb: int = 500, warn_free_gb: float = 5.0):
    """Falha cedo (com mensagem clara) se o disco estiver quase cheio; avisa se estiver apertado."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(path).free
    except OSError:
        return
    if free < min_free_mb * 1024 * 1024:
        raise RuntimeError(f"Sem espaço em disco: só {_fmt_bytes(free)} livres em {path}. "
                           "Libere espaço e tente de novo.")
    if free < warn_free_gb * 1024 ** 3:
        log(f"⚠ Pouco espaço em disco: {_fmt_bytes(free)} livres em {path}.")


# ──────────────────────────────────────────────────────────────
# ffmpeg
# ──────────────────────────────────────────────────────────────
def find_ffmpeg() -> Optional[str]:
    """Procura o ffmpeg no PATH ou ao lado do script/executável."""
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).parent)
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    for name in ("ffmpeg.exe", "ffmpeg"):
        if (base / name).exists():
            return str(base)
    return None


# ──────────────────────────────────────────────────────────────
# DOWNLOAD
# ──────────────────────────────────────────────────────────────
def build_format(quality: Optional[int], has_ffmpeg: bool) -> str:
    """Cadeia de formatos com fallback: nunca falha só porque a qualidade pedida não existe."""
    h = f"[height<={quality}]" if quality else ""
    if has_ffmpeg:
        return f"bv*{h}+ba/b{h}/bv*+ba/b"
    return f"b{h}/b"   # sem ffmpeg não dá para juntar vídeo+áudio separados


def explain_error(msg: str) -> str:
    m = msg.lower()
    has = lambda pat: re.search(pat, m) is not None  # noqa: E731
    if has(r"\bdrm\b"):
        return "Vídeo protegido por DRM (Netflix, Disney+, etc.). Não é possível baixar."
    if has(r"http error 404") or has(r"\b404\b"):
        return "Página/arquivo não encontrado (404). Confira se a URL está correta e ainda existe."
    if has(r"sign in|log ?in|cookies|confirm your age|age[- ]restrict|age[- ]verif|members[- ]only|private video|\bnsfw\b"):
        return ("O site exige login ou verificação de idade. Escolha o navegador onde você está logado "
                "em 'Cookies' (ou informe um cookies.txt) e tente de novo.")
    if has(r"unsupported url"):
        return ("Não encontrei vídeo nessa URL. Cole o link da página do vídeo (não da listagem) "
                "ou o link direto do arquivo (.mp4 / .m3u8).")
    if has(r"\b403\b|forbidden|cloudflare"):
        return ("O site bloqueou o download (403). Instale 'curl-cffi' (pip install curl-cffi) para simular "
                "um navegador, ou use cookies do navegador.")
    if has(r"ffmpeg|ffprobe"):
        return "ffmpeg não encontrado. Instale o ffmpeg e deixe no PATH (ou ao lado do programa)."
    if has(r"geo.{0,20}restrict|not available in your (country|region)"):
        return "Vídeo restrito por região. Tente com um proxy (--proxy) de outro país."
    return msg


def _impersonate_target():
    """Só existe se curl_cffi estiver instalado; ajuda contra bloqueios tipo Cloudflare."""
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str("chrome")
    except Exception:
        return None


class _YDLLogger:
    def __init__(self, log: LogCB):
        self.log = log

    def debug(self, m):
        pass

    def info(self, m):
        pass

    def warning(self, m):
        if "ffmpeg" in m.lower():
            self.log(f"⚠ {m}")

    def error(self, m):
        self.log(f"✗ {m}")


def _watch_aria2(output_dir: Path, progress: ProgressCB, cancel: Optional[threading.Event],
                 log: LogCB, stop: threading.Event):
    """O yt-dlp não reporta progresso do aria2c: lemos o tamanho do .part e, se pedirem, matamos o aria2c."""
    last_size, last_t, warned = 0, time.time(), False
    while not stop.wait(0.5):
        size = 0
        for f in output_dir.glob("*.part"):
            try:
                size += f.stat().st_size
            except OSError:
                pass
        now = time.time()
        speed = (size - last_size) / max(now - last_t, 1e-6)
        last_size, last_t = size, now
        if size:
            progress(-1, f"Baixando {_fmt_bytes(size)} · {_fmt_bytes(max(speed, 0))}/s (aria2c, 16 conexões)")
        if cancel is not None and cancel.is_set():
            try:
                import psutil
                for ch in psutil.Process().children(recursive=True):
                    if "aria2" in ch.name().lower():
                        ch.kill()
            except ImportError:
                if not warned:
                    log("⚠ Para interromper no meio do download instale: pip install psutil")
                    warned = True
            except Exception:  # noqa: BLE001
                pass


def download_videos(
    url: str,
    output_dir: Path,
    quality: Optional[int] = None,
    *,
    playlist: bool = False,
    cookies_file: Optional[str] = None,
    cookies_from_browser: Optional[str] = None,
    proxy: Optional[str] = None,
    use_aria2: bool = True,
    rate_limit: Optional[int] = None,       # bytes/segundo (None = sem limite)
    progress: ProgressCB = _noop,
    log: LogCB = print,
    cancel: Optional[threading.Event] = None,
) -> list[Path]:
    """Baixa o vídeo (ou playlist) e devolve a lista de arquivos finais."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp não instalado. Execute: pip install -U yt-dlp")

    output_dir = Path(output_dir)
    check_disk_space(output_dir, log)

    ffmpeg_dir = find_ffmpeg()
    if not ffmpeg_dir:
        log("⚠ ffmpeg não encontrado: só formatos de arquivo único serão baixados (qualidade pode ser menor).")

    files: list[Path] = []
    space_checked = False

    def hook(d: dict):
        nonlocal space_checked
        _check(cancel)
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total and not space_checked:
                space_checked = True
                try:
                    free = shutil.disk_usage(output_dir).free
                except OSError:
                    free = None
                if free is not None and free < total * 1.1:
                    raise RuntimeError(f"Sem espaço em disco: o vídeo tem {_fmt_bytes(total)} e só há "
                                       f"{_fmt_bytes(free)} livres.")
            done = d.get("downloaded_bytes", 0)
            speed = d.get("speed")
            eta = d.get("eta")
            txt = f"Baixando {_fmt_bytes(done)}"
            if total:
                txt += f" / {_fmt_bytes(total)}"
            if speed:
                txt += f" · {_fmt_bytes(speed)}/s"
            if eta is not None:
                txt += f" · faltam {int(eta)}s"
            progress(done / total if total else -1, txt)

    def post_hook(filepath: str):
        p = Path(filepath)
        if p.exists() and p not in files:
            files.append(p)

    base_opts: dict = {
        "format": build_format(quality, bool(ffmpeg_dir)),
        "format_sort": ["res", "ext:mp4:m4a"],
        "outtmpl": str(output_dir / "%(title).100B [%(id).20B].%(ext)s"),
        "windowsfilenames": True,
        "merge_output_format": "mp4",
        "noplaylist": not playlist,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        "continuedl": True,
        "geo_bypass": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "post_hooks": [post_hook],
        "logger": _YDLLogger(log),
    }
    if ffmpeg_dir:
        base_opts["ffmpeg_location"] = ffmpeg_dir
    if cookies_file:
        base_opts["cookiefile"] = cookies_file
    if cookies_from_browser:
        base_opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if proxy:
        base_opts["proxy"] = proxy
    if rate_limit:
        base_opts["ratelimit"] = int(rate_limit)

    # Tentativas em ordem: aria2c (várias conexões) → normal → imitando navegador → formato padrão.
    # Muitos sites limitam a velocidade POR CONEXÃO; o aria2c abre 16 e chega a ser 20-40x mais rápido.
    attempts: list[dict] = []
    if use_aria2 and shutil.which("aria2c"):
        aria_args = ["-x", "16", "-s", "16", "-k", "1M"]
        if rate_limit:
            aria_args.append(f"--max-overall-download-limit={int(rate_limit)}")
        attempts.append({"external_downloader": {"http": "aria2c", "https": "aria2c"},
                         "external_downloader_args": {"aria2c": aria_args}})
    attempts.append({})
    imp = _impersonate_target()
    if imp:
        attempts.append({"impersonate": imp})
    attempts.append({"format": "best/bv*+ba", **({"impersonate": imp} if imp else {})})

    last_err: Optional[Exception] = None
    for i, extra in enumerate(attempts):
        _check(cancel)
        opts = {**base_opts, **extra}
        if i:
            log(f"↻ Tentativa {i + 1}/{len(attempts)} com configuração alternativa...")
        stop_watch = threading.Event()
        if "external_downloader" in extra:
            threading.Thread(target=_watch_aria2, args=(output_dir, progress, cancel, log, stop_watch),
                             daemon=True).start()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
            if files:
                return files
            last_err = RuntimeError("Nenhum arquivo foi gerado.")
        except Exception as e:  # noqa: BLE001
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            last_err = e
            text = str(e).lower()
            if "external_downloader" in extra:
                log("⚠ aria2c falhou; tentando o método normal...")
                continue
            # erros que outra tentativa não resolve
            if re.search(r"\bdrm\b|unsupported url|sign in|log ?in|private|\b404\b|connection refused|name or service not known|sem espa[cç]o", text):
                break
        finally:
            stop_watch.set()

    raise RuntimeError(explain_error(re.sub(r"\x1b\[[0-9;]*m", "", str(last_err))))


def download_video(url: str, output_dir: Path, quality: Optional[int] = None, **kw) -> Path:
    """Compatibilidade: devolve só o primeiro arquivo."""
    return download_videos(url, output_dir, quality, **kw)[0]


# ──────────────────────────────────────────────────────────────
# GOOGLE DRIVE
# ──────────────────────────────────────────────────────────────
def _gdrive_service(credentials_file: Path, token_file: Path, log: LogCB):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), GDRIVE_SCOPES)
        except Exception:
            creds = None

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            log("⚠ Token do Google expirou/foi revogado. Refazendo login...")
            token_file.unlink(missing_ok=True)
            creds = None

    if not creds or not creds.valid:
        if not credentials_file.exists():
            raise FileNotFoundError(
                f"Arquivo de credenciais não encontrado: {credentials_file}\n"
                "Baixe em console.cloud.google.com → APIs e serviços → Credenciais → ID do cliente OAuth (Desktop).")
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), GDRIVE_SCOPES)
        creds = flow.run_local_server(port=0)
    token_file.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def parse_folder_id(raw: str) -> str:
    """Aceita o ID puro ou a URL da pasta; recusa coisas que claramente não são ID de pasta."""
    f = (raw or "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", f)
    if m:
        f = m.group(1)
    if not f:
        return ""
    if "googleusercontent.com" in f or "." in f or not _FOLDER_ID_RE.match(f):
        raise ValueError(
            "O 'ID da pasta' não parece um ID de pasta do Drive (parece o ID do cliente OAuth ou outro texto). "
            "O ID da pasta é o trecho final do endereço drive.google.com/drive/folders/ESTE_TRECHO. "
            "Deixe vazio para enviar à raiz do 'Meu Drive' ou use o campo 'Nome da pasta'.")
    return f


def _gdrive_http_msg(e) -> str:
    """Traduz erros HTTP da API do Drive em instruções claras."""
    status = getattr(getattr(e, "resp", None), "status", None)
    raw = str(e)
    content = getattr(e, "content", b"")
    if isinstance(content, bytes):
        raw += " " + content.decode("utf-8", "ignore")
    low = raw.lower()
    if "accessnotconfigured" in low or "has not been used" in low or "is disabled" in low:
        return ("A Google Drive API não está ativada no seu projeto. Abra console.cloud.google.com → "
                "APIs e serviços → Biblioteca → 'Google Drive API' → Ativar. Espere 1-2 min e tente de novo.")
    if "storagequotaexceeded" in low or ("quota" in low and "storage" in low):
        return "Seu Google Drive está sem espaço livre."
    if status == 401:
        return "Login do Google inválido/expirado. Apague o arquivo gdrive_token.json e tente de novo."
    if status == 403:
        return ("O Google recusou o acesso (403). Se o app está em modo 'Teste', adicione seu e-mail em "
                "'Tela de consentimento OAuth → Usuários de teste'. Detalhe: " + raw[:200])
    if status == 404:
        return ("Pasta não encontrada (404). Com a permissão atual (drive.file) o programa só enxerga pastas "
                "que ELE mesmo criou. Use o campo 'Nome da pasta' (ela é criada se não existir) ou deixe vazio.")
    return f"Erro do Google Drive ({status}): {raw[:300]}"


def _gdrive_folder_by_path(service, path: str) -> str:
    """Acha (ou cria) a pasta 'a/b/c' dentro do Meu Drive e devolve o ID da última."""
    parent = None
    for name in [p.strip() for p in path.replace("\\", "/").split("/") if p.strip()]:
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        q = f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        if parent:
            q += f" and '{parent}' in parents"
        res = service.files().list(q=q, spaces="drive", fields="files(id,name)", pageSize=1,
                                   supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        found = res.get("files", [])
        if found:
            parent = found[0]["id"]
            continue
        meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent:
            meta["parents"] = [parent]
        parent = service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]
    return parent or ""


_PREFLIGHT_CACHE: dict = {}
_PREFLIGHT_LOCK = threading.Lock()   # várias tarefas em paralelo: só uma faz o login por vez
_PREFLIGHT_TTL = 600  # 10 min


def _preflight_gdrive(credentials_file: Path, folder_id: Optional[str] = None, folder_name: Optional[str] = None,
                     *, token_file: Optional[Path] = None, log: LogCB = print, use_cache: bool = True):
    """Confere login + pasta ANTES de baixar. Devolve (id_da_pasta_ou_None, texto_resumo)."""
    from googleapiclient.errors import HttpError

    key = ("gdrive", str(credentials_file), folder_id or "", folder_name or "")
    hit = _PREFLIGHT_CACHE.get(key)
    if use_cache and hit and time.time() - hit[0] < _PREFLIGHT_TTL:
        return hit[1]

    credentials_file = Path(credentials_file)
    token_file = Path(token_file) if token_file else credentials_file.parent / "gdrive_token.json"
    fid = parse_folder_id(folder_id or "") if not folder_name else ""
    try:
        service = _gdrive_service(credentials_file, token_file, log)
        about = service.about().get(fields="user(emailAddress),storageQuota(limit,usage)").execute()
        who = (about.get("user") or {}).get("emailAddress", "conta Google")
        q = about.get("storageQuota") or {}
        extra = ""
        if q.get("limit"):
            free = int(q["limit"]) - int(q.get("usage", 0))
            extra = f", {_fmt_bytes(free)} livres"
        if folder_name:
            fid = _gdrive_folder_by_path(service, folder_name)
            where = f"pasta '{folder_name}'"
        elif fid:
            info = service.files().get(fileId=fid, fields="id,name,mimeType", supportsAllDrives=True).execute()
            if info.get("mimeType") != "application/vnd.google-apps.folder":
                raise RuntimeError("O ID informado não é de uma pasta (é de um arquivo).")
            where = f"pasta '{info.get('name')}'"
        else:
            where = "raiz do Meu Drive"
    except HttpError as e:
        raise RuntimeError(_gdrive_http_msg(e))
    result = (fid or None, f"{who} → {where}{extra}")
    _PREFLIGHT_CACHE[key] = (time.time(), result)
    return result


def _preflight_mega(email: str, password: str, *, use_cache: bool = True) -> str:
    """Confere o login do MEGA antes de baixar. Devolve texto-resumo."""
    key = ("mega", email)
    hit = _PREFLIGHT_CACHE.get(key)
    if use_cache and hit and time.time() - hit[0] < _PREFLIGHT_TTL:
        return hit[1]
    try:
        from mega import Mega
    except ImportError:
        if shutil.which("mega-login"):
            return f"{email} (MEGAcmd; login não testado)"
        raise RuntimeError("Nem 'mega.py' nem o MEGAcmd estão instalados. Rode: pip install --no-deps mega.py")
    try:
        client = Mega().login(email, password)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Não consegui entrar no MEGA ({type(e).__name__}). Confira e-mail e senha. "
                           "Se a conta usa verificação em duas etapas, desative-a ou use o MEGAcmd.")
    extra = ""
    try:
        sp = client.get_storage_space(giga=True)
        extra = f", {max(sp['total'] - sp['used'], 0):.1f} GB livres"
    except Exception:  # noqa: BLE001
        pass
    result = f"{email}{extra}"
    _PREFLIGHT_CACHE[key] = (time.time(), result)
    return result


def preflight_gdrive(*a, **k):
    with _PREFLIGHT_LOCK:
        return _preflight_gdrive(*a, **k)


def preflight_mega(*a, **k) -> str:
    with _PREFLIGHT_LOCK:
        return _preflight_mega(*a, **k)


def upload_to_gdrive(
    file_path: Path,
    folder_id: Optional[str] = None,
    *,
    credentials_file: Path = Path("gdrive_credentials.json"),
    token_file: Optional[Path] = None,
    progress: ProgressCB = _noop,
    log: LogCB = print,
    cancel: Optional[threading.Event] = None,
) -> str:
    from googleapiclient.http import MediaFileUpload

    credentials_file = Path(credentials_file)
    token_file = Path(token_file) if token_file else credentials_file.parent / "gdrive_token.json"
    service = _gdrive_service(credentials_file, token_file, log)

    meta: dict = {"name": file_path.name}
    if folder_id:
        meta["parents"] = [folder_id]
    mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(file_path), mimetype=mime, resumable=True, chunksize=8 * 1024 * 1024)
    req = service.files().create(body=meta, media_body=media, fields="id, webViewLink", supportsAllDrives=True)

    from googleapiclient.errors import HttpError

    size = file_path.stat().st_size
    response = None
    while response is None:
        _check(cancel)
        try:
            status, response = req.next_chunk(num_retries=5)
        except HttpError as e:
            raise RuntimeError(_gdrive_http_msg(e))
        if status:
            progress(status.resumable_progress / size,
                     f"Enviando ao Drive {_fmt_bytes(status.resumable_progress)} / {_fmt_bytes(size)}")
    progress(1.0, "Upload concluído")
    return response.get("webViewLink") or f"https://drive.google.com/file/d/{response['id']}/view"


# ──────────────────────────────────────────────────────────────
# MEGA  (mega.py primeiro; MEGAcmd como alternativa)
# ──────────────────────────────────────────────────────────────
def _mega_via_megapy(file_path: Path, email: str, password: str, folder: Optional[str], log: LogCB) -> str:
    from mega import Mega

    client = Mega().login(email, password)
    dest = None
    if folder:
        node = client.find(folder)          # devolve (handle, nó)
        if node:
            dest = node[0]                  # upload() quer só o handle
        else:
            log(f"⚠ Pasta '{folder}' não encontrada no MEGA; enviando para a raiz.")
    uploaded = client.upload(str(file_path), dest)
    return client.get_upload_link(uploaded)


def _mega_via_megacmd(file_path: Path, email: str, password: str, folder: Optional[str]) -> str:
    def run(cmd: str, *args: str) -> str:
        exe = shutil.which(cmd)
        if not exe:
            raise RuntimeError(f"{cmd} não encontrado (instale o MEGAcmd: https://mega.io/cmd)")
        r = subprocess.run([exe, *args], capture_output=True, text=True)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 and "already logged in" not in out.lower():
            raise RuntimeError(out.strip() or f"{cmd} falhou")
        return out

    run("mega-login", email, password)
    remote = "/" + folder.strip("/") + "/" if folder else "/"
    run("mega-put", "-c", str(file_path), remote)
    out = run("mega-export", "-a", "-f", remote + file_path.name)
    m = re.search(r"https://mega\.nz/\S+", out)
    if not m:
        raise RuntimeError("Enviado, mas não consegui gerar o link público.")
    return m.group(0)


def upload_to_mega(
    file_path: Path,
    email: str,
    password: str,
    folder: Optional[str] = None,
    *,
    progress: ProgressCB = _noop,
    log: LogCB = print,
    cancel: Optional[threading.Event] = None,
) -> str:
    _check(cancel)
    progress(-1, f"Enviando ao MEGA ({_fmt_bytes(file_path.stat().st_size)}) — sem barra de progresso...")
    errors = []
    try:
        link = _mega_via_megapy(file_path, email, password, folder, log)
    except Exception as e:  # noqa: BLE001
        errors.append(f"mega.py: {type(e).__name__}: {e}")
        log(f"⚠ mega.py falhou ({type(e).__name__}); tentando MEGAcmd...")
        try:
            link = _mega_via_megacmd(file_path, email, password, folder)
        except Exception as e2:  # noqa: BLE001
            errors.append(f"MEGAcmd: {e2}")
            raise RuntimeError("Falha no envio ao MEGA.\n  " + "\n  ".join(errors))
    progress(1.0, "Upload concluído")
    return link


# ──────────────────────────────────────────────────────────────
# Pipeline completo (usado pela CLI e pela GUI)
# ──────────────────────────────────────────────────────────────
def process_url(
    url: str,
    *,
    dest: str,                              # "gdrive" | "mega" | "local"
    quality: Optional[int] = None,
    playlist: bool = False,
    local_dir: Optional[Path] = None,       # onde guardar cópia/arquivo final
    keep_local: bool = False,
    cookies_file: Optional[str] = None,
    cookies_from_browser: Optional[str] = None,
    proxy: Optional[str] = None,
    folder_id: Optional[str] = None,
    gdrive_folder: Optional[str] = None,    # NOME da pasta no Drive (criada se não existir)
    credentials_file: Path = Path("gdrive_credentials.json"),
    mega_email: str = "",
    mega_password: str = "",
    mega_folder: Optional[str] = None,
    use_aria2: bool = True,                 # download acelerado (16 conexões) se aria2c existir
    rate_limit: Optional[int] = None,       # bytes/s
    work_dir: Optional[Path] = None,        # pasta de trabalho PERSISTENTE (permite retomar após falha)
    preflight: bool = True,                 # testa o destino antes de baixar
    on_file: Optional[Callable[[Path], None]] = None,   # chamado para cada arquivo baixado
    progress: ProgressCB = _noop,           # recebe progresso já mapeado em 0..1 do item
    log: LogCB = print,
    cancel: Optional[threading.Event] = None,
) -> list[str]:
    """Baixa `url` e entrega conforme `dest`. Devolve links (ou caminhos locais)."""
    results: list[str] = []
    local_dir = Path(local_dir) if local_dir else Path.cwd()
    local_dir.mkdir(parents=True, exist_ok=True)

    # 1) Testa o destino ANTES de gastar tempo baixando
    resolved_folder = folder_id or None
    if dest == "gdrive":
        if preflight:
            progress(-1, "Verificando o Google Drive...")
            resolved_folder, info = preflight_gdrive(Path(credentials_file), folder_id, gdrive_folder, log=log)
            log(f"✓ Drive pronto: {info}")
        else:
            resolved_folder = parse_folder_id(folder_id or "") or None
    elif dest == "mega" and preflight:
        progress(-1, "Verificando o MEGA...")
        log(f"✓ MEGA pronto: {preflight_mega(mega_email, mega_password)}")
    _check(cancel)

    persistent = dest != "local" and work_dir is not None
    with contextlib.ExitStack() as stack:
        if dest == "local":
            work = local_dir
        elif persistent:
            work = Path(work_dir)
            work.mkdir(parents=True, exist_ok=True)
        else:
            work = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        dl_share = 1.0 if dest == "local" else 0.5

        def dl_prog(f, t):
            progress(-1 if f < 0 else f * dl_share, t)

        log(f"📥 Baixando: {url}")
        paths = download_videos(url, work, quality, playlist=playlist, cookies_file=cookies_file,
                                cookies_from_browser=cookies_from_browser, proxy=proxy,
                                progress=dl_prog, log=log, cancel=cancel, use_aria2=use_aria2,
                                rate_limit=rate_limit)
        for p in paths:
            log(f"✓ Download: {p.name} ({_fmt_bytes(p.stat().st_size)})")
            if on_file:
                on_file(p)

        if dest == "local":
            results = [str(p) for p in paths]
            progress(1.0, "Concluído")
            return results

        # Guarda a cópia local ANTES de enviar: se o upload falhar, o vídeo baixado não se perde.
        if keep_local:
            moved = []
            for p in paths:
                target = local_dir / p.name
                shutil.move(str(p), target)
                log(f"📁 Cópia local: {target}")
                moved.append(target)
            paths = moved

        for p in paths:
            _check(cancel)

            def up_prog(f, t):
                progress(0.5 if f < 0 else 0.5 + f * 0.5, t)

            if dest == "gdrive":
                log(f"☁ Enviando ao Google Drive: {p.name}")
                link = upload_to_gdrive(p, resolved_folder, credentials_file=credentials_file,
                                        progress=up_prog, log=log, cancel=cancel)
            else:
                log(f"🔷 Enviando ao MEGA: {p.name}")
                link = upload_to_mega(p, mega_email, mega_password, mega_folder or None,
                                      progress=up_prog, log=log, cancel=cancel)
            log(f"✓ Upload concluído: {link}")
            results.append(link)
            if not keep_local:
                p.unlink(missing_ok=True)   # já está na nuvem: libera o disco

    if persistent:
        shutil.rmtree(work, ignore_errors=True)   # tudo certo: apaga a pasta de retomada
    progress(1.0, "Concluído")
    return results


def update_ytdlp(log: LogCB = print) -> bool:
    """Atualiza o yt-dlp (sites mudam toda semana; a maioria dos erros some atualizando)."""
    if getattr(sys, "frozen", False):
        log("Este é um executável: para atualizar o yt-dlp gere o .exe de novo com a versão nova instalada.")
        return False
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], capture_output=True, text=True)
    log((r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else "ok")
    return r.returncode == 0


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
def launch_gui() -> bool:
    """Abre a janela (gui.py). Devolve False, explicando o motivo, se não der."""
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("⚠ A janela precisa do tkinter, que não está instalado.\n"
              "  Linux: sudo apt install python3-tk      Windows/Mac: reinstale o Python marcando 'tcl/tk'.")
        return False
    try:
        import customtkinter  # noqa: F401
    except ImportError:
        print("⚠ Falta o customtkinter neste Python.\n"
              f"  Rode: {sys.executable} -m pip install customtkinter")
        return False
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import gui
        gui.App().mainloop()
        return True
    except Exception as e:  # noqa: BLE001  (ex.: TclError sem display / SSH sem X)
        print(f"⚠ Não consegui abrir a janela: {type(e).__name__}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="Baixa vídeos (yt-dlp) e envia para Google Drive / MEGA / pasta local.")
    ap.add_argument("urls", nargs="*", help="URLs dos vídeos")
    ap.add_argument("--url", action="append", default=[], help="(compat) URL do vídeo; pode repetir")
    ap.add_argument("--dest", choices=["gdrive", "mega", "local"], default="local")
    ap.add_argument("--quality", type=int, help="altura máxima (720, 1080...)")
    ap.add_argument("--playlist", action="store_true", help="baixar a playlist inteira")
    ap.add_argument("--output-dir", type=Path, default=Path.cwd(), help="pasta local (dest=local ou --keep)")
    ap.add_argument("--keep", action="store_true", help="manter cópia local após o upload")
    ap.add_argument("--cookies", help="arquivo cookies.txt")
    ap.add_argument("--cookies-from-browser", choices=BROWSERS, help="usar cookies deste navegador")
    ap.add_argument("--proxy")
    ap.add_argument("--folder-id", help="ID da pasta no Google Drive")
    ap.add_argument("--credentials", type=Path, default=Path("gdrive_credentials.json"))
    ap.add_argument("--mega-email", default=os.environ.get("MEGA_EMAIL"))
    ap.add_argument("--mega-folder")
    ap.add_argument("--gdrive-folder", help="NOME da pasta no Drive (criada se não existir; pode ser a/b/c)")
    ap.add_argument("--limite", type=float, help="limite de velocidade de download em MB/s")
    ap.add_argument("--sem-aria2", action="store_true", help="não usar o aria2c (download em 1 conexão)")
    ap.add_argument("--update", action="store_true", help="atualizar o yt-dlp e sair")
    ap.add_argument("--cli", action="store_true", help="sem janela: perguntar no terminal")
    a = ap.parse_args()

    if a.update:
        sys.exit(0 if update_ytdlp() else 1)

    urls = a.urls + a.url
    if not urls:
        if not a.cli and launch_gui():
            sys.exit(0)
        if not sys.stdin.isatty():
            ap.error("informe ao menos uma URL")
        # Modo interativo no terminal (a janela não abriu ou foi pedido --cli)
        print("🎬 Video Uploader — modo terminal\n")
        print("Cole as URLs, uma por linha. Linha vazia para começar:")
        while True:
            line = input("> ").strip()
            if not line:
                break
            urls.append(line)
        if not urls:
            print("Nenhuma URL informada. Saindo.")
            sys.exit(0)
        escolha = input("Destino [1=pasta local (padrão)  2=Google Drive  3=MEGA]: ").strip()
        a.dest = {"2": "gdrive", "3": "mega"}.get(escolha, "local")
        if a.dest == "mega" and not a.mega_email:
            a.mega_email = input("E-mail do MEGA: ").strip()
        if a.dest == "local" and a.output_dir == Path.cwd():
            a.output_dir = Path.cwd() / "downloads"

    mega_pw = ""
    if a.dest == "mega":
        if not a.mega_email:
            ap.error("--mega-email (ou MEGA_EMAIL) é obrigatório para --dest mega")
        mega_pw = os.environ.get("MEGA_PASSWORD") or getpass.getpass("Senha do MEGA: ")

    def prog(f, t):
        print(f"\r{t[:100]:<100}", end="", flush=True)

    ok = True
    for u in urls:
        try:
            res = process_url(u, dest=a.dest, quality=a.quality, playlist=a.playlist, local_dir=a.output_dir,
                              keep_local=a.keep, cookies_file=a.cookies, cookies_from_browser=a.cookies_from_browser,
                              proxy=a.proxy, folder_id=a.folder_id, credentials_file=a.credentials,
                              mega_email=a.mega_email or "", mega_password=mega_pw, mega_folder=a.mega_folder,
                              use_aria2=not a.sem_aria2, gdrive_folder=a.gdrive_folder,
                              rate_limit=int(a.limite * 1048576) if a.limite else None,
                              progress=prog, log=lambda m: print("\n" + m))
            print("\n" + "\n".join(res))
        except Cancelled:
            print("\nCancelado.")
            sys.exit(130)
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"\n✗ {u}\n  {e}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()