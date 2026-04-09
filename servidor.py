#!/usr/bin/env python3
"""
servidor.py  —  Backend WebSocket para o gerenciador de downloads
Instale dependências: pip install websockets yt-dlp
"""

import asyncio
import json
import os
import subprocess
import sys
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

# ── Estado global ─────────────────────────────────────────────────────────────
downloads: dict[str, dict] = {}   # id -> info do download
clientes:  set = set()            # websockets conectados

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

    destino   = dl.get("destino", os.path.join(os.path.expanduser("~"), "Videos", "yt-dlp"))
    qualidade = dl.get("qualidade", "melhor")
    os.makedirs(destino, exist_ok=True)

    def hook(d):
        if d["status"] == "downloading":
            pct_raw = d.get("_percent_str", "0%").strip().replace("%", "")
            try:
                pct = float(pct_raw)
            except ValueError:
                pct = 0.0

            downloads[dl_id].update({
                "status":     "baixando",
                "progresso":  pct,
                "velocidade": d.get("_speed_str",  "...").strip(),
                "eta":        d.get("_eta_str",    "...").strip(),
                "baixado":    d.get("_downloaded_bytes_str", "").strip(),
                "total":      d.get("_total_bytes_str",
                              d.get("_total_bytes_estimate_str", "?")).strip(),
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
        "postprocessors":                [],
    }

    if qualidade == "audio":
        ydl_opts["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "192",
        }]

    def _baixar():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(dl["url"], download=True)

    try:
        info = await loop.run_in_executor(None, _baixar)
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
        })

    except Exception as e:
        downloads[dl_id].update({
            "status":  "erro",
            "erro":    str(e),
            "progresso": 0,
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
                    destino = os.path.join(os.path.expanduser("~"), "Videos", "yt-dlp")

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
                        "fim":        "",
                        "erro":       "",
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

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        clientes.discard(ws)
        print(f"[-] Cliente desconectado ({len(clientes)} total)")

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    HOST, PORT = "localhost", 8765
    print(f"""
╔══════════════════════════════════════════════╗
║      Gerenciador de Downloads  |  yt-dlp     ║
╠══════════════════════════════════════════════╣
║  Servidor: ws://{HOST}:{PORT}               ║
║  Abra o arquivo  index.html  no navegador    ║
╚══════════════════════════════════════════════╝
""")
    async with websockets.serve(handler, HOST, PORT):
        await asyncio.Future()  # roda para sempre

if __name__ == "__main__":
    asyncio.run(main())
