#!/usr/bin/env python3
"""
servidor.py  —  Backend WebSocket para o gerenciador de downloads
Instale dependências: pip install websockets yt-dlp
"""

import asyncio
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from urllib.parse import unquote, urlparse

# ── Auto-instala dependências ─────────────────────────────────────────────────
def instalar(pacote):
    subprocess.check_call([sys.executable, "-m", "pip", "install", pacote, "-q"])

try:
    import websockets
except ImportError:
    print("[*] Instalando websockets...")
    instalar("websockets")
    import websockets

try:
    import yt_dlp
except ImportError:
    print("[*] Instalando yt-dlp...")
    instalar("yt-dlp")
    import yt_dlp

# ── ANSI (yt-dlp colore _speed_str, _eta_str, etc. quando o terminal suporta) ─
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text) -> str:
    if text is None:
        return ""
    return ANSI_ESCAPE.sub("", str(text)).strip()


def melhor_thumbnail(info: dict | None) -> str:
    """URL da melhor miniatura disponível no dict retornado pelo yt-dlp."""
    if not info:
        return ""
    t = (info.get("thumbnail") or "").strip()
    if t:
        return t
    thumbs = info.get("thumbnails") or []
    if not thumbs:
        return ""

    def area(th):
        try:
            w = int(th.get("width") or 0)
            h = int(th.get("height") or 0)
            return w * h
        except (TypeError, ValueError):
            return 0

    best = max(thumbs, key=area)
    return (best.get("url") or "").strip()


def _str_lista_ou_valor(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (list, tuple)):
        partes = [str(x).strip() for x in val if str(x).strip()]
        return ", ".join(partes) if partes else ""
    return str(val).strip()


# Primeiro segmento do path em soundcloud.com/{usuario}/… (não é o nome do artista)
_SOUNDCLOUD_PATH_RESERVADO = frozenset({
    "discover", "charts", "pages", "you", "feed", "likes", "popular",
    "stations", "upload", "search", "imprint", "terms-of-use", "community-guidelines",
})


def artista_soundcloud_por_url(url: str) -> str:
    """Extrai o usuário/perfil do link do SoundCloud quando os metadados vêm vazios (API/versão antiga)."""
    if not url or "soundcloud.com" not in url.lower():
        return ""
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return ""
    host = (parsed.netloc or "").lower().split(":")[0]
    if "soundcloud.com" not in host:
        return ""
    partes = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
    if not partes:
        return ""
    user = partes[0].strip()
    if not user or user.lower() in _SOUNDCLOUD_PATH_RESERVADO:
        return ""
    if user.islower() or (user.isascii() and user.isalpha() and user.isupper()):
        user = user.title()
    return user


def _urls_para_fallback_artista(info: dict, url_na_fila: str) -> list[str]:
    ordem: list[str] = []
    if info:
        for chave in ("uploader_url", "webpage_url", "original_url", "url"):
            v = (info.get(chave) or "").strip()
            if v:
                ordem.append(v)
    v = (url_na_fila or "").strip()
    if v and v not in ordem:
        ordem.append(v)
    return ordem


def extrair_artista_metadata(info: dict, url_na_fila: str = "") -> str:
    """Tenta obter o artista a partir dos campos que o yt-dlp preenche por plataforma."""
    if not info:
        info = {}
    for chave in ("artist", "album_artist"):
        s = _str_lista_ou_valor(info.get(chave))
        if s:
            return s
    s = _str_lista_ou_valor(info.get("artists"))
    if s:
        return s
    s = _str_lista_ou_valor(info.get("creator"))
    if s:
        return s
    s = _str_lista_ou_valor(info.get("creators"))
    if s:
        return s
    s = _str_lista_ou_valor(info.get("composer"))
    if s:
        return s
    s = (info.get("uploader") or "").strip()
    if s and s not in ("Unknown", "N/A"):
        return s
    s = (info.get("channel") or "").strip()
    if s and s not in ("Unknown", "-", "N/A"):
        return s
    for u in _urls_para_fallback_artista(info, url_na_fila):
        sc = artista_soundcloud_por_url(u)
        if sc:
            return sc
    return ""


def _separador_titulo_musica():
    # hífen ASCII, en dash, em dash, figura, dois-pontos, barra vertical
    return r"[-–—‒:｜|]+"


def _titulo_sem_prefixo_artista(artista: str, faixa: str) -> str:
    """Se a faixa começa com o mesmo artista + separador, devolve só o nome da música."""
    if not artista or not faixa:
        return faixa.strip() if faixa else ""
    a = artista.strip()
    f = faixa.strip()
    if not a or not f:
        return f
    sep = _separador_titulo_musica()
    pat = r"^" + re.escape(a) + r"\s*" + sep + r"\s*"
    novo = re.sub(pat, "", f, flags=re.IGNORECASE)
    if novo != f:
        return novo.strip() or f
    # "Artista feat. X - Música" com artista igual ao prefixo antes do primeiro " - "
    return f


def _inferir_artista_faixa_pelo_titulo(titulo: str) -> tuple[str, str]:
    """Quando não há artista nos metadados: 'Autor - Nome' no próprio título (ex.: YouTube)."""
    t = (titulo or "").strip()
    if not t:
        return "", ""
    m = re.match(r"^(.+?)\s*" + _separador_titulo_musica() + r"\s+(.+)$", t)
    if not m:
        return "", t
    art, rest = m.group(1).strip(), m.group(2).strip()
    if not art or not rest:
        return "", t
    return art, rest


def titulo_audio_exibicao(info: dict | None, url_fallback: str) -> str:
    """Título para downloads só áudio: 'Artista - Nome do som', sem repetir artista."""
    if not info:
        return url_fallback
    titulo_bruto = (info.get("title") or "").strip()
    faixa_meta = (info.get("track") or "").strip()
    faixa = faixa_meta or titulo_bruto or url_fallback

    artista = extrair_artista_metadata(info, url_fallback)
    if not artista:
        a_inf, f_inf = _inferir_artista_faixa_pelo_titulo(titulo_bruto)
        if a_inf:
            artista = a_inf
            if not faixa_meta:
                faixa = _titulo_sem_prefixo_artista(artista, f_inf) or f_inf
    else:
        faixa = _titulo_sem_prefixo_artista(artista, faixa) or faixa

    if artista:
        return f"{artista} - {faixa}".strip()
    return faixa or url_fallback


# Caracteres inválidos em nomes de arquivo (Windows e uso geral)
_INVALIDOS_NOME_ARQ = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitizar_nome_arquivo(nome: str, max_len: int = 200) -> str:
    """Remove caracteres proibidos e limita o tamanho para salvar em disco."""
    s = (nome or "").strip()
    s = _INVALIDOS_NOME_ARQ.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.rstrip(". ")
    if len(s) > max_len:
        s = s[:max_len].rstrip(". ")
    return s or "audio"


def outtmpl_com_nome_base(destino: str, nome_base_sem_ext: str) -> str:
    """outtmpl yt-dlp com nome fixo; %(ext)s continua vindo do vídeo/áudio baixado."""
    base = (nome_base_sem_ext or "audio").replace("%", "%%")
    return os.path.join(destino, base + ".%(ext)s")


def _formato_tem_video_util(f: dict) -> bool:
    """Vídeo com resolução mínima (ignora capas / artefatos minúsculos)."""
    vc = (f.get("vcodec") or "none").lower()
    if vc in ("none", "", "unknown"):
        return False
    try:
        w = int(f.get("width") or 0)
        h = int(f.get("height") or 0)
    except (TypeError, ValueError):
        return False
    return w >= 160 and h >= 160


def info_e_somente_audio(info: dict | None) -> bool:
    """True se o item não tiver faixa de vídeo útil (só música / áudio)."""
    if not info:
        return False
    if info.get("_type") == "playlist":
        return False
    ex = (info.get("extractor") or "").lower()
    if "soundcloud" in ex or "bandcamp" in ex:
        return True
    formats = info.get("formats") or []
    if not formats:
        vc = (info.get("vcodec") or "none").lower()
        ac = (info.get("acodec") or "none").lower()
        return ac != "none" and vc == "none"
    return not any(_formato_tem_video_util(f) for f in formats)


def info_tem_wav_ou_flac_nativo(info: dict | None) -> bool:
    """Há formato anunciado como WAV ou FLAC (prioridade para saída em WAV)."""
    if not info:
        return False
    for f in info.get("formats") or []:
        ext = (f.get("ext") or "").lower()
        if ext in ("wav", "flac"):
            return True
    return False


# ── Estado global ─────────────────────────────────────────────────────────────
_DIR_SERVIDOR = os.path.dirname(os.path.abspath(__file__))
PASTA_DOWNLOAD_PADRAO = os.path.join(_DIR_SERVIDOR, "downloads")

downloads: dict[str, dict] = {}   # id -> info do download
clientes:  set = set()            # websockets conectados
_shutdown: asyncio.Event | None = None  # preenchido em main(); encerrar_servidor

# Encerramento automático: após N s sem atividade de download (com UI já ligada ao WS).
SEGUNDOS_INATIVIDADE_SEM_DOWNLOAD = 180.0  # 3 minutos
_t_mono_ultima_atividade_download = 0.0
_houve_cliente_ws_para_inatividade = False


def marcar_atividade_download_servidor() -> None:
    """Atualiza o relógio usado pelo watchdog de inatividade (thread-safe o suficiente para floats)."""
    global _t_mono_ultima_atividade_download
    _t_mono_ultima_atividade_download = time.monotonic()


def _downloads_com_transferencia_ativa() -> bool:
    for v in downloads.values():
        if v.get("status") in ("pendente", "baixando", "processando"):
            return True
    return False


def caminho_arquivo_baixado(
    ydl, info, qualidade: str, exts_audio: tuple[str, ...] | None = None
) -> str:
    """Resolve o caminho final no disco após yt-dlp (incl. pós-processamento de áudio)."""
    if not info:
        return ""
    fp = info.get("filepath")
    if fp and os.path.isfile(fp):
        return os.path.abspath(fp)
    try:
        prepared = ydl.prepare_filename(info)
    except Exception:
        prepared = ""
    if prepared:
        stem, _ = os.path.splitext(prepared)
        if exts_audio:
            for ext in exts_audio:
                cand = stem + ext
                if os.path.isfile(cand):
                    return os.path.abspath(cand)
        if qualidade == "audio":
            mp3 = stem + ".mp3"
            if os.path.isfile(mp3):
                return os.path.abspath(mp3)
    if prepared and os.path.isfile(prepared):
        return os.path.abspath(prepared)
    if prepared:
        stem, _ = os.path.splitext(prepared)
        for ext in (".wav", ".flac", ".mp4", ".mkv", ".webm", ".m4a", ".opus", ".mp3", ".ogg"):
            cand = stem + ext
            if os.path.isfile(cand):
                return os.path.abspath(cand)
    return ""


def revelar_pasta_do_arquivo(caminho_arquivo: str) -> bool:
    """Abre o gerenciador de arquivos na pasta do arquivo (com seleção quando o SO permite)."""
    if not caminho_arquivo or not os.path.isfile(caminho_arquivo):
        return False
    caminho_arquivo = os.path.abspath(caminho_arquivo)
    system = platform.system()
    if system == "Windows":
        subprocess.Popen(
            ["explorer", "/select,", os.path.normpath(caminho_arquivo)],
            shell=False,
        )
        return True
    if system == "Darwin":
        subprocess.run(["open", "-R", caminho_arquivo], check=False)
        return True
    pasta = os.path.dirname(caminho_arquivo)
    if os.path.isdir(pasta):
        subprocess.run(["xdg-open", pasta], check=False)
        return True
    return False


def abrir_pasta(pasta: str) -> bool:
    if not pasta or not os.path.isdir(pasta):
        return False
    pasta = os.path.abspath(pasta)
    system = platform.system()
    if system == "Windows":
        os.startfile(pasta)
        return True
    if system == "Darwin":
        subprocess.run(["open", pasta], check=False)
        return True
    subprocess.run(["xdg-open", pasta], check=False)
    return True


def abrir_arquivo_com_app_padrao(caminho: str) -> bool:
    if not caminho or not os.path.isfile(caminho):
        return False
    caminho = os.path.abspath(caminho)
    system = platform.system()
    if system == "Windows":
        os.startfile(caminho)
        return True
    if system == "Darwin":
        subprocess.run(["open", caminho], check=False)
        return True
    subprocess.run(["xdg-open", caminho], check=False)
    return True


def _subprocess_flags_windows_silencioso() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def matar_instancias_terminal_vdget_windows():
    """
    Encerra janelas do launcher (cmd em pause), consola minimizada do servidor,
    outros Python que executam este servidor.py ou controlador.py na mesma pasta.
    Não mata o processo atual (evita suicídio antes do shutdown).
    """
    if platform.system() != "Windows":
        return
    fl = _subprocess_flags_windows_silencioso()
    subprocess.run(
        ["taskkill", "/FI", "WINDOWTITLE eq VDGET - Gerenciador de Downloads", "/F"],
        capture_output=True,
        creationflags=fl,
    )
    subprocess.run(
        ["taskkill", "/FI", "WINDOWTITLE eq VDGET Servidor", "/F"],
        capture_output=True,
        creationflags=fl,
    )
    script_path = os.path.abspath(__file__)
    controlador_path = os.path.join(os.path.dirname(script_path), "controlador.py")
    me = os.getpid()
    ps_body = (
        "$ErrorActionPreference = 'SilentlyContinue'\n"
        f"$me = {me}\n"
        f"$p = @'\n{script_path}\n'@\n"
        f"$pc = @'\n{controlador_path}\n'@\n"
        "Get-CimInstance Win32_Process | Where-Object {\n"
        "  $_.CommandLine -and $_.ProcessId -ne $me -and\n"
        "  ($_.CommandLine.Contains($p) -or $_.CommandLine.Contains($pc))\n"
        "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }\n"
    )

    fd, ps_path = tempfile.mkstemp(suffix=".ps1", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="\n") as f:
            f.write(ps_body)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                ps_path,
            ],
            capture_output=True,
            creationflags=fl,
        )
    finally:
        try:
            os.unlink(ps_path)
        except OSError:
            pass


# ── Broadcast para todos os clientes ─────────────────────────────────────────
async def broadcast(mensagem: dict):
    if clientes:
        dados = json.dumps(mensagem, ensure_ascii=False)
        await asyncio.gather(*[ws.send(dados) for ws in clientes], return_exceptions=True)


async def disparar_encerramento_vdget() -> None:
    """Encerra o WebSocket, mata consolas/processo relacionados no Windows e libera o wait em main()."""
    ev = _shutdown
    if not ev or ev.is_set():
        return
    await broadcast({"tipo": "servidor_encerrando"})
    matar_instancias_terminal_vdget_windows()
    ev.set()


async def watchdog_inatividade_sem_download() -> None:
    """Fecha VDGET após SEGUNDOS_INATIVIDADE_SEM_DOWNLOAD sem fila ativa nem progresso de download."""
    while True:
        await asyncio.sleep(0.25)
        ev = _shutdown
        if ev is None or ev.is_set():
            return
        if not _houve_cliente_ws_para_inatividade:
            continue
        if _downloads_com_transferencia_ativa():
            continue
        if time.monotonic() - _t_mono_ultima_atividade_download < SEGUNDOS_INATIVIDADE_SEM_DOWNLOAD:
            continue
        await disparar_encerramento_vdget()
        return


# ── Realiza o download em thread separada ─────────────────────────────────────
async def executar_download(dl_id: str):
    dl = downloads[dl_id]
    loop = asyncio.get_event_loop()

    formatos = {
        "melhor": "bestvideo+bestaudio/best",
        "pior":   "worstvideo+worstaudio/worst",
        "audio":  "bestaudio/best",
    }

    destino   = dl.get("destino", PASTA_DOWNLOAD_PADRAO)
    qualidade = dl.get("qualidade", "melhor")
    os.makedirs(destino, exist_ok=True)

    def progresso_pct(d: dict) -> float:
        """yt-dlp coloca _percent (float) antes de colorir _percent_str com ANSI — float(_percent_str) quebrava e ficava 0%."""
        if d.get("status") != "downloading":
            return 0.0
        p = d.get("_percent")
        if isinstance(p, (int, float)) and p == p:  # evita NaN
            return max(0.0, min(100.0, float(p)))
        baixado = d.get("downloaded_bytes")
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if baixado is not None and total and total > 0:
            return max(0.0, min(100.0, 100.0 * baixado / total))
        raw = strip_ansi(str(d.get("_percent_str") or "0%"))
        raw = raw.replace("%", "").strip()
        try:
            return max(0.0, min(100.0, float(raw)))
        except ValueError:
            return 0.0

    def hook(d):
        if d["status"] == "downloading":
            marcar_atividade_download_servidor()
            pct = progresso_pct(d)

            downloads[dl_id].update({
                "status":     "baixando",
                "progresso":  pct,
                "velocidade": strip_ansi(d.get("_speed_str", "...")) or "...",
                "eta":        strip_ansi(d.get("_eta_str", "...")) or "...",
                "baixado":    strip_ansi(d.get("_downloaded_bytes_str", "")),
                "total":      strip_ansi(
                    d.get("_total_bytes_str")
                    or d.get("_total_bytes_estimate_str")
                    or "?"
                ),
            })
            asyncio.run_coroutine_threadsafe(
                broadcast({"tipo": "atualizar", "download": downloads[dl_id]}),
                loop
            )

        elif d["status"] == "finished":
            marcar_atividade_download_servidor()
            downloads[dl_id].update({
                "status":    "processando",
                "progresso": 99,
                "velocidade": "",
                "eta":        "",
            })
            asyncio.run_coroutine_threadsafe(
                broadcast({"tipo": "atualizar", "download": downloads[dl_id]}),
                loop
            )

    ydl_opts = {
        "format":                        formatos.get(qualidade, qualidade),
        "outtmpl":                       os.path.join(destino, "%(title)s.%(ext)s"),
        "merge_output_format":           "mp4",
        "noplaylist":                    True,
        "concurrent_fragment_downloads": 8,
        "buffersize":                    16384,
        "retries":                       5,
        "fragment_retries":              5,
        "http_chunk_size":               10485760,
        "skip_unavailable_fragments":    True,
        "throttledratelimit":            100000,
        "progress_hooks":                [hook],
        "quiet":                         True,
        "no_warnings":                   True,
        "color":                         "never",
        "postprocessors":                [],
    }

    if qualidade == "audio":
        ydl_opts["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "0",
        }]

    exts_resolver: tuple[str, ...] | None = None
    usar_titulo_audio = False

    def _somente_meta():
        try:
            meta_opts = dict(ydl_opts)
            meta_opts["format"] = "best/best"
            meta_opts["postprocessors"] = []
            with yt_dlp.YoutubeDL(meta_opts) as ydl:
                return ydl.extract_info(dl["url"], download=False)
        except Exception:
            return None

    info_meta = await loop.run_in_executor(None, _somente_meta)
    if dl_id not in downloads:
        return
    if info_meta:
        somente = info_e_somente_audio(info_meta)
        if somente and qualidade == "pior":
            ydl_opts["format"] = "worstaudio/worst"
            ydl_opts["postprocessors"] = []
            ydl_opts.pop("merge_output_format", None)
            usar_titulo_audio = True
            exts_resolver = (".opus", ".m4a", ".mp3", ".ogg", ".wav", ".flac")
        elif somente and qualidade in ("melhor", "audio"):
            usar_titulo_audio = True
            if info_tem_wav_ou_flac_nativo(info_meta):
                ydl_opts["format"] = "bestaudio[ext=wav]/bestaudio[ext=flac]/bestaudio/best"
                ydl_opts["postprocessors"] = [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "wav",
                    "preferredquality": "0",
                }]
                exts_resolver = (".wav", ".mp3", ".m4a", ".opus", ".flac")
            else:
                ydl_opts["format"] = "bestaudio/best"
                ydl_opts["postprocessors"] = [{
                    "key":              "FFmpegExtractAudio",
                    "preferredcodec":   "mp3",
                    "preferredquality": "0",
                }]
                exts_resolver = (".mp3", ".m4a", ".opus")
            ydl_opts.pop("merge_output_format", None)
        elif qualidade == "audio":
            usar_titulo_audio = True

        if usar_titulo_audio:
            titulo_m = titulo_audio_exibicao(info_meta, dl["url"])
            ydl_opts["outtmpl"] = outtmpl_com_nome_base(
                destino, sanitizar_nome_arquivo(titulo_m)
            )
        else:
            titulo_m = (info_meta.get("title") or "").strip() or dl["url"]
        thumb_m = melhor_thumbnail(info_meta)
        downloads[dl_id].update({"titulo": titulo_m, "thumb": thumb_m})
        await broadcast({"tipo": "atualizar", "download": downloads[dl_id]})

    def _baixar():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(dl["url"], download=True)
            caminho = caminho_arquivo_baixado(
                ydl, info, qualidade, exts_audio=exts_resolver
            )
            return info, caminho

    try:
        info, caminho_final = await loop.run_in_executor(None, _baixar)
        if info and (qualidade == "audio" or usar_titulo_audio):
            titulo = titulo_audio_exibicao(info, dl.get("url") or "Desconhecido")
        elif info:
            titulo = info.get("title", "Desconhecido")
        else:
            titulo = "Desconhecido"
        thumb = info.get("thumbnail", "") if info else ""

        downloads[dl_id].update({
            "status":    "concluido",
            "progresso": 100,
            "titulo":    titulo,
            "thumb":     thumb,
            "velocidade": "",
            "eta":        "",
            "fim":        datetime.now().strftime("%H:%M:%S"),
            "finalizado_ts": time.time(),
            "arquivo":   caminho_final or "",
        })

    except Exception as e:
        downloads[dl_id].update({
            "status":  "erro",
            "erro":    str(e),
            "progresso": 0,
            "finalizado_ts": time.time(),
        })

    await broadcast({"tipo": "atualizar", "download": downloads[dl_id]})
    marcar_atividade_download_servidor()

# ── Handler de conexão WebSocket ──────────────────────────────────────────────
async def handler(ws):
    global _houve_cliente_ws_para_inatividade
    clientes.add(ws)
    _houve_cliente_ws_para_inatividade = True
    marcar_atividade_download_servidor()
    print(f"[+] Cliente conectado  ({len(clientes)} total)")

    # Envia estado atual para o novo cliente
    await ws.send(json.dumps({
        "tipo":      "estado_inicial",
        "downloads": list(downloads.values())
    }, ensure_ascii=False))

    try:
        async for msg in ws:
            try:
                dados = json.loads(msg)
            except json.JSONDecodeError:
                continue

            acao = dados.get("acao")

            # ── Adicionar novo download ──────────────────────────────
            if acao == "adicionar":
                urls      = dados.get("urls", [])
                qualidade = dados.get("qualidade", "melhor")
                destino   = dados.get("destino", "")

                if not destino:
                    destino = PASTA_DOWNLOAD_PADRAO

                for url in urls:
                    url = url.strip()
                    if not url:
                        continue

                    dl_id = str(uuid.uuid4())[:8]
                    downloads[dl_id] = {
                        "id":         dl_id,
                        "url":        url,
                        "status":     "pendente",
                        "qualidade":  qualidade,
                        "destino":    destino,
                        "progresso":  0,
                        "velocidade": "",
                        "eta":        "",
                        "titulo":     url,
                        "thumb":      "",
                        "inicio":     datetime.now().strftime("%H:%M:%S"),
                        "criado_ts":  time.time(),
                        "fim":        "",
                        "erro":       "",
                        "arquivo":    "",
                    }
                    await broadcast({"tipo": "adicionar", "download": downloads[dl_id]})
                    marcar_atividade_download_servidor()
                    asyncio.create_task(executar_download(dl_id))

            # ── Remover item da lista ────────────────────────────────
            elif acao == "remover":
                dl_id = dados.get("id")
                if dl_id in downloads:
                    del downloads[dl_id]
                # Notifica mesmo sem entrada no servidor (histórico só no localStorage do cliente).
                if dl_id:
                    await broadcast({"tipo": "remover", "id": dl_id})

            # ── Limpar concluídos ────────────────────────────────────
            elif acao == "limpar_concluidos":
                removidos = [k for k, v in downloads.items() if v["status"] == "concluido"]
                for k in removidos:
                    del downloads[k]
                await broadcast({"tipo": "limpar_concluidos", "ids": removidos})

            elif acao == "abrir_pasta_arquivo":
                dl_id = dados.get("id")
                if dl_id in downloads:
                    d = downloads[dl_id]
                    arq = (d.get("arquivo") or "").strip()
                    dest = (d.get("destino") or "").strip()
                    if arq and os.path.isfile(arq):
                        revelar_pasta_do_arquivo(arq)
                    elif dest:
                        abrir_pasta(dest)

            elif acao == "abrir_arquivo":
                dl_id = dados.get("id")
                if dl_id in downloads:
                    arq = (downloads[dl_id].get("arquivo") or "").strip()
                    if arq:
                        abrir_arquivo_com_app_padrao(arq)

            elif acao == "encerrar_servidor":
                await disparar_encerramento_vdget()

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clientes.discard(ws)
        print(f"[-] Cliente desconectado ({len(clientes)} total)")

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    global _shutdown, _t_mono_ultima_atividade_download, _houve_cliente_ws_para_inatividade
    HOST, PORT = "localhost", 8765
    _shutdown = asyncio.Event()
    _t_mono_ultima_atividade_download = time.monotonic()
    _houve_cliente_ws_para_inatividade = False
    print(f"""
╔══════════════════════════════════════════════╗
║      Gerenciador de Downloads  |  yt-dlp     ║
╠══════════════════════════════════════════════╣
║  Servidor: ws://{HOST}:{PORT}               ║
║  Abra o arquivo  index.html  no navegador    ║
╚══════════════════════════════════════════════╝
""")
    async with websockets.serve(handler, HOST, PORT):
        wd = asyncio.create_task(watchdog_inatividade_sem_download())
        try:
            await _shutdown.wait()
        finally:
            wd.cancel()
            try:
                await wd
            except asyncio.CancelledError:
                pass
    print("[*] Servidor encerrado.")

if __name__ == "__main__":
    asyncio.run(main())
