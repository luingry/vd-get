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
import time
import uuid
from datetime import datetime

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


# ── Estado global ─────────────────────────────────────────────────────────────
_DIR_SERVIDOR = os.path.dirname(os.path.abspath(__file__))
PASTA_DOWNLOAD_PADRAO = os.path.join(_DIR_SERVIDOR, "downloads")

downloads: dict[str, dict] = {}   # id -> info do download
clientes:  set = set()            # websockets conectados
_shutdown: asyncio.Event | None = None  # preenchido em main(); encerrar_servidor


def caminho_arquivo_baixado(ydl, info, qualidade: str) -> str:
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
    if qualidade == "audio" and prepared:
        stem, _ = os.path.splitext(prepared)
        mp3 = stem + ".mp3"
        if os.path.isfile(mp3):
            return os.path.abspath(mp3)
    if prepared and os.path.isfile(prepared):
        return os.path.abspath(prepared)
    if prepared:
        stem, _ = os.path.splitext(prepared)
        for ext in (".mp4", ".mkv", ".webm", ".m4a", ".opus", ".mp3", ".ogg"):
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


# ── Broadcast para todos os clientes ─────────────────────────────────────────
async def broadcast(mensagem: dict):
    if clientes:
        dados = json.dumps(mensagem, ensure_ascii=False)
        await asyncio.gather(*[ws.send(dados) for ws in clientes], return_exceptions=True)

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
            "preferredquality": "192",
        }]

    def _somente_meta():
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(dl["url"], download=False)
        except Exception:
            return None

    info_meta = await loop.run_in_executor(None, _somente_meta)
    if dl_id not in downloads:
        return
    if info_meta:
        titulo_m = (info_meta.get("title") or "").strip() or dl["url"]
        thumb_m = melhor_thumbnail(info_meta)
        downloads[dl_id].update({"titulo": titulo_m, "thumb": thumb_m})
        await broadcast({"tipo": "atualizar", "download": downloads[dl_id]})

    def _baixar():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(dl["url"], download=True)
            caminho = caminho_arquivo_baixado(ydl, info, qualidade)
            return info, caminho

    try:
        info, caminho_final = await loop.run_in_executor(None, _baixar)
        titulo = info.get("title", "Desconhecido") if info else "Desconhecido"
        thumb  = info.get("thumbnail", "") if info else ""

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

# ── Handler de conexão WebSocket ──────────────────────────────────────────────
async def handler(ws):
    clientes.add(ws)
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
                    asyncio.create_task(executar_download(dl_id))

            # ── Remover item da lista ────────────────────────────────
            elif acao == "remover":
                dl_id = dados.get("id")
                if dl_id in downloads:
                    del downloads[dl_id]
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
                ev = _shutdown
                if ev and not ev.is_set():
                    await broadcast({"tipo": "servidor_encerrando"})
                    ev.set()

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clientes.discard(ws)
        print(f"[-] Cliente desconectado ({len(clientes)} total)")

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    global _shutdown
    HOST, PORT = "localhost", 8765
    _shutdown = asyncio.Event()
    print(f"""
╔══════════════════════════════════════════════╗
║      Gerenciador de Downloads  |  yt-dlp     ║
╠══════════════════════════════════════════════╣
║  Servidor: ws://{HOST}:{PORT}               ║
║  Abra o arquivo  index.html  no navegador    ║
╚══════════════════════════════════════════════╝
""")
    async with websockets.serve(handler, HOST, PORT):
        await _shutdown.wait()
    print("[*] Servidor encerrado.")

if __name__ == "__main__":
    asyncio.run(main())
