# 🎬 Video Uploader

Baixa vídeos com **yt-dlp** (mais de 1000 sites, incluindo sites adultos) e envia para
**Google Drive**, **MEGA** ou só salva numa pasta local. Tem interface gráfica (`gui.py`) e linha de comando.

## Instalar

```bash
python -m venv venv
# Windows: venv\Scripts\activate      Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
pip install --no-deps mega.py
```

**Por que `--no-deps` no mega.py?** O `mega.py` declara dependência de `tenacity<6` (que quebra no Python 3.11+)
e do pacote `pathlib` (um backport antigo que *substitui o pathlib do Python* e quebra o PyInstaller e o pip).
Instalando sem dependências e usando `tenacity>=8`, funciona normalmente.

**ffmpeg é importante.** A maioria dos sites entrega vídeo e áudio separados; sem ffmpeg o programa só
consegue baixar formatos de arquivo único (qualidade menor). Instale (`winget install ffmpeg`,
`brew install ffmpeg`, `apt install ffmpeg`) ou coloque o `ffmpeg.exe` ao lado do programa.

## Usar

```bash
python gui.py                                   # interface gráfica (cole várias URLs, uma por linha)

python video_uploader.py URL --dest local --quality 720
python video_uploader.py URL1 URL2 --dest gdrive --folder-id ID_DA_PASTA
python video_uploader.py URL --dest mega --mega-email voce@x.com     # senha: prompt ou env MEGA_PASSWORD
python video_uploader.py URL --cookies-from-browser firefox          # sites com login / verificação de idade
python video_uploader.py --update                                    # atualiza o yt-dlp
```

## Quando um vídeo não baixa

| Sintoma | O que fazer |
|---|---|
| Erro logo depois de funcionar por semanas | **Atualize o yt-dlp** (botão "Atualizar yt-dlp" / `--update`). Sites mudam toda semana. |
| "Exige login / idade" | Em *Cookies*, escolha o navegador onde você está logado (feche-o antes no Windows) ou aponte um `cookies.txt`. |
| 403 / Cloudflare | `curl-cffi` (já no requirements) faz o programa imitar um navegador; combine com cookies. |
| "Unsupported URL" | Cole o link da **página do vídeo**, não da listagem; ou o link direto `.mp4` / `.m3u8`. |
| Restrito por região | Use um proxy (`--proxy`). |
| **DRM** (Netflix, Disney+, Prime...) | Não é possível baixar. Nenhuma configuração resolve. |

## Google Drive

1. console.cloud.google.com → crie um projeto → ative a **Google Drive API**.
2. Credenciais → **ID do cliente OAuth → Aplicativo para computador** → baixe o JSON como `gdrive_credentials.json`.
3. Na primeira execução o navegador abre para autorizar. O `gdrive_token.json` é criado sozinho.

## MEGA

Usa `mega.py`; se ele falhar, tenta o **MEGAcmd** (https://mega.io/cmd) se estiver instalado.
O upload para o MEGA não tem barra de progresso (limitação das duas bibliotecas).
A senha **não é gravada em `config.json`**: só é lembrada no cofre do sistema se o `keyring` estiver instalado.

## Gerar executável (.exe)

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name VideoUploader \
  --collect-all customtkinter --collect-all yt_dlp --hidden-import mega gui.py
```

(Linux/Mac: mesmo comando.) Distribua `dist/VideoUploader.exe` + `ffmpeg.exe` (+ `gdrive_credentials.json`
se usar Drive). O .exe não consegue se auto-atualizar: para atualizar o yt-dlp, gere o .exe de novo.

## ⚠️ Segurança

- **Nunca** envie `config.json`, `gdrive_token.json`, `gdrive_credentials.json` ou `cookies.txt` para
  ninguém/nenhum chat/repositório (já estão no `.gitignore`). Quem tem o token acessa seu Drive.
- Se algum desses arquivos já foi compartilhado: revogue o OAuth Client no Google Cloud, apague o
  `gdrive_token.json` e **troque a senha do MEGA**.
# Video-Uploader
