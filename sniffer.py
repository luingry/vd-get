#!/usr/bin/env python3
"""
sniffer.py  —  Fallback headless: descobre manifesto HLS/DASH via Chromium quando o
yt-dlp falha sozinho ao extrair uma página (típico de players alimentados por MSE/`blob:`).

Módulo isolado e preguiçoso, pensado para **zero regressão** em servidor.py:

- Nenhum import de `playwright` em escopo de módulo. O import só acontece dentro das
  funções, no momento do uso. Se o Playwright não estiver instalado (ou o import falhar
  por qualquer motivo), `sniffer_disponivel()` devolve False e `descobrir_manifesto()`
  devolve None — nunca levanta exceção para fora deste módulo.
- Instalação de dependências é sob demanda (`garantir_dependencias`), nunca no boot.
- Um Chromium headless real (sem janela, sem interação humana) navega até a página,
  deixa o JavaScript do player rodar, intercepta a requisição do manifesto (.m3u8/.mpd)
  com os headers reais, e devolve tudo pronto para o yt-dlp reentregar como download.

Requisito duro: o navegador precisa reportar viewport 1920x1080 coerente (innerWidth,
screen.width/height, outerWidth), porque vários sites escolhem o manifesto/qualidade
em função do tamanho do viewer — ver `_JS_INIT_VIEWPORT` e o log de verificação em
`_descobrir_manifesto_impl`.
"""

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urljoin, urlparse

_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.path.join(_DIR, ".vdget_sniffer.json")

# User-Agent desktop "normal" (sem a substring HeadlessChrome, que vários sites bloqueiam).
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SEGUNDOS_OCIOSO_FECHA_BROWSER = 90.0

# Caminho (sem query string) terminando em manifesto de streaming HLS/DASH.
_RE_MANIFESTO_EXT = re.compile(r"\.(m3u8|m3u|mpd)$", re.IGNORECASE)
_CONTENT_TYPES_MANIFESTO = ("mpegurl", "vnd.apple.mpegurl", "dash+xml")

# Headers de transporte que não fazem sentido reencaminhar para o yt-dlp (1.7 do plano).
_HEADERS_DESCARTAR = {"host", "content-length", "connection", "accept-encoding"}

# Headers que o yt-dlp de fato recebe no resultado final, com a casing canônica.
_HEADERS_MANTER_CANONICOS = {
    "referer":         "Referer",
    "origin":          "Origin",
    "user-agent":      "User-Agent",
    "cookie":          "Cookie",
    "accept":          "Accept",
    "accept-language": "Accept-Language",
}

# Termos de botão de consentimento/idade, em pt-BR/en (best-effort, nunca falha o fluxo).
_TEXTOS_BOTAO_CONSENTIMENTO = (
    "aceitar", "aceito", "accept", "agree", "concordo", "entrar", "continuar", "18", "sim",
)

# Script injetado antes de qualquer JS da página: cobre os dois caminhos que os sites usam
# para decidir qualidade (innerWidth via viewport do Playwright, e screen/outerWidth aqui,
# que em headless costuma vir zerado ou errado).
_JS_INIT_VIEWPORT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(window, 'outerWidth',  {get: () => 1920});
Object.defineProperty(window, 'outerHeight', {get: () => 1080});
Object.defineProperty(window.screen, 'width',       {get: () => 1920});
Object.defineProperty(window.screen, 'height',      {get: () => 1080});
Object.defineProperty(window.screen, 'availWidth',  {get: () => 1920});
Object.defineProperty(window.screen, 'availHeight', {get: () => 1040});
"""

# Args de lançamento do Chromium: 1920x1080 real, autoplay sem gesto do usuário, mudo,
# menos sinais de automação. (1.4 do plano)
_ARGS_LANCAMENTO = [
    "--window-size=1920,1080",
    "--force-device-scale-factor=1",
    "--autoplay-policy=no-user-gesture-required",
    "--mute-audio",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
]


def _subprocess_flags_windows_silencioso() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _chamar_reportar(reportar, msg: str) -> None:
    """reportar() vem de fora (servidor.py) — nunca deixamos um erro dele derrubar o sniff."""
    if reportar:
        try:
            reportar(msg)
        except Exception:
            pass


# ── Disponibilidade e instalação sob demanda ──────────────────────────────────
def sniffer_disponivel() -> bool:
    """True se o Playwright já está importável, sem instalar nada."""
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _instalar_pacote_playwright() -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright", "-q"])


def _ler_cache() -> dict:
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _gravar_cache(dados: dict) -> None:
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(dados, f)
    except Exception:
        pass  # falha ao persistir cache não é fatal — só sondamos de novo na próxima vez


async def _sondar_canal(reportar) -> str | None:
    """
    Descobre qual canal de navegador funciona, na ordem do plano (1.2):
    chrome do sistema → msedge do sistema → chromium empacotado (baixa só aqui).
    Resultado fica em cache para não repetir a sondagem a cada sniff.
    """
    cache = _ler_cache()
    canal_cache = cache.get("canal")
    if canal_cache:
        return canal_cache

    from playwright.async_api import async_playwright

    for canal in ("chrome", "msedge"):
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True, channel=canal)
                await browser.close()
            _gravar_cache({"canal": canal})
            return canal
        except Exception:
            continue

    # Nenhum navegador do sistema disponível: só aqui baixamos o Chromium empacotado.
    _chamar_reportar(reportar, "baixando navegador (~150 MB), só na primeira vez…")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            creationflags=_subprocess_flags_windows_silencioso(),
        )
    except Exception:
        return None
    _gravar_cache({"canal": "chromium"})
    return "chromium"


async def garantir_dependencias(reportar=None) -> bool:
    """Garante Playwright instalado e canal de navegador sondado. Reporta progresso à UI."""
    try:
        if not sniffer_disponivel():
            _chamar_reportar(reportar, "instalando Playwright…")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _instalar_pacote_playwright)
        canal = await _sondar_canal(reportar)
        return canal is not None
    except Exception:
        return False


# ── Ciclo de vida do navegador (singleton preguiçoso) ─────────────────────────
_playwright = None
_browser = None
_lock_browser = asyncio.Lock()
_semaforo_sniff = asyncio.Semaphore(1)  # no máximo 1 sniff por vez (1.3 do plano)
_tarefa_auto_close: "asyncio.Task | None" = None
_t_ultimo_uso = 0.0


async def _obter_browser():
    """Reaproveita uma instância de browser entre sniffs; relança se caiu."""
    global _playwright, _browser, _tarefa_auto_close, _t_ultimo_uso
    async with _lock_browser:
        # A invariante "_t_ultimo_uso reflete o último uso real do browser" não pode
        # depender do chamador atualizá-la depois — se alguém chamar _obter_browser()
        # isoladamente (ex.: via API/teste) e _t_ultimo_uso ainda for 0.0, o auto-close
        # por ociosidade mata o browser poucos segundos depois de aberto.
        _t_ultimo_uso = time.monotonic()
        if _browser is not None:
            try:
                # is_connected() é síncrono e barato; browser pode ter caído sozinho.
                if _browser.is_connected():
                    return _browser
            except Exception:
                pass
            _browser = None

        from playwright.async_api import async_playwright

        canal = _ler_cache().get("canal") or "chrome"
        if _playwright is None:
            _playwright = await async_playwright().start()
        try:
            _browser = await _playwright.chromium.launch(
                headless=True, channel=canal, args=_ARGS_LANCAMENTO
            )
        except Exception:
            if canal != "chromium":
                _browser = await _playwright.chromium.launch(
                    headless=True, args=_ARGS_LANCAMENTO
                )
            else:
                raise

        if _tarefa_auto_close is None or _tarefa_auto_close.done():
            _tarefa_auto_close = asyncio.create_task(_auto_close_por_ociosidade())
        _t_ultimo_uso = time.monotonic()  # cobre o tempo gasto no launch acima
        return _browser


async def _auto_close_por_ociosidade() -> None:
    """Fecha o browser após SEGUNDOS_OCIOSO_FECHA_BROWSER sem nenhum sniff em uso."""
    while True:
        await asyncio.sleep(5.0)
        if _browser is None:
            return
        if time.monotonic() - _t_ultimo_uso >= SEGUNDOS_OCIOSO_FECHA_BROWSER:
            await encerrar()
            return


def _pids_descendentes_windows(pid_raiz: int) -> list[int]:
    """
    Todos os PIDs de chrome.exe/msedge.exe descendentes de pid_raiz (o processo do
    servidor). Usado só como rede de segurança em `encerrar()` — nunca mexe em janelas
    de Chrome que não sejam filhas do nosso próprio processo.
    """
    if sys.platform != "win32":
        return []
    ps_body = (
        "$ErrorActionPreference='SilentlyContinue'\n"
        "$all = Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name\n"
        f"$raiz = {pid_raiz}\n"
        "$alvo = @($raiz)\n"
        "$mudou = $true\n"
        "while ($mudou) {\n"
        "  $mudou = $false\n"
        "  foreach ($p in $all) {\n"
        "    if ($alvo -contains $p.ParentProcessId -and -not ($alvo -contains $p.ProcessId)) {\n"
        "      $alvo += $p.ProcessId; $mudou = $true\n"
        "    }\n"
        "  }\n"
        "}\n"
        "$all | Where-Object { $alvo -contains $_.ProcessId -and $_.ProcessId -ne $raiz -and "
        "($_.Name -eq 'chrome.exe' -or $_.Name -eq 'msedge.exe') } | "
        "ForEach-Object { $_.ProcessId }\n"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_body],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_subprocess_flags_windows_silencioso(),
        )
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


async def encerrar() -> None:
    """Fecha browser + playwright e mata processos órfãos (rede de segurança no Windows)."""
    global _playwright, _browser, _tarefa_auto_close
    browser, pw = _browser, _playwright
    _browser = None
    _playwright = None
    if _tarefa_auto_close:
        _tarefa_auto_close.cancel()
        _tarefa_auto_close = None

    try:
        if browser is not None:
            await asyncio.wait_for(browser.close(), timeout=10)
    except Exception:
        pass
    try:
        if pw is not None:
            await pw.stop()
    except Exception:
        pass

    if sys.platform == "win32":
        for pid in _pids_descendentes_windows(os.getpid()):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=_subprocess_flags_windows_silencioso(),
                )
            except Exception:
                pass


# ── Interação best-effort com a página (1.6 do plano) ─────────────────────────
async def _tentar_dispensar_banners(page) -> None:
    """Clica no primeiro botão visível de consentimento/idade. Nunca falha o fluxo."""
    for termo in _TEXTOS_BOTAO_CONSENTIMENTO:
        try:
            loc = page.get_by_role("button", name=re.compile(termo, re.IGNORECASE)).first
            if await loc.is_visible():
                await loc.click(timeout=1500)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue


async def _tentar_dar_play(page) -> None:
    """Muta e chama .play() em todo <video> da página via JS."""
    try:
        await page.evaluate(
            "() => { document.querySelectorAll('video').forEach(v => { "
            "try { v.muted = true; v.play().catch(() => {}); } catch (e) {} }); }"
        )
    except Exception:
        pass


async def _tentar_clicar_player(page) -> None:
    """Clica no centro do maior <video>/container de player, caso ainda não haja manifesto."""
    try:
        centro = await page.evaluate(
            """() => {
                const els = Array.from(document.querySelectorAll(
                    'video, [class*="player" i], [class*="video" i]'
                ));
                let melhor = null, area = 0;
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    const a = r.width * r.height;
                    if (a > area) { area = a; melhor = r; }
                }
                return melhor ? [melhor.x + melhor.width / 2, melhor.y + melhor.height / 2] : null;
            }"""
        )
        if centro:
            await page.mouse.click(centro[0], centro[1])
        await _tentar_dar_play(page)
    except Exception:
        pass


# Extração ampliada de miniatura do DOM (defeito 1a do relatório de revisão): cobre as
# meta tags mais comuns, itemprop/link, JSON-LD e o `poster` do maior <video> — nessa
# ordem de prioridade. Resolve relativo→absoluto com `new URL(u, document.baseURI)`
# (mais correto que urljoin em Python puro: já respeita eventuais tags <base href>).
# Primeiro candidato não-vazio vence. Sites 100% blob/MSE (sem NENHUMA dessas fontes no
# DOM) continuam sem miniatura aqui — para esses existe o fallback via ffmpeg (1b).
_JS_EXTRAIR_THUMB = """
() => {
    function abs(u) {
        if (!u) return '';
        try { return new URL(u, document.baseURI).href; } catch (e) { return ''; }
    }
    function attrDe(el) {
        if (!el) return '';
        return el.getAttribute('content') || el.getAttribute('href') || el.getAttribute('src') || '';
    }
    const candidatos = [];
    candidatos.push(attrDe(document.querySelector('meta[property="og:image"]')));
    candidatos.push(attrDe(document.querySelector('meta[property="og:image:secure_url"]')));
    candidatos.push(attrDe(document.querySelector('meta[name="twitter:image"]')));
    candidatos.push(attrDe(document.querySelector('[itemprop="thumbnailUrl"]')));
    candidatos.push(attrDe(document.querySelector('link[rel="image_src"]')));
    try {
        for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
            try {
                const dados = JSON.parse(s.textContent);
                for (const item of (Array.isArray(dados) ? dados : [dados])) {
                    if (!item) continue;
                    let t = item.thumbnailUrl || item.thumbnail;
                    if (Array.isArray(t)) t = t[0];
                    if (t && typeof t === 'object') t = t.url || '';
                    if (t) candidatos.push(String(t));
                }
            } catch (e) {}
        }
    } catch (e) {}
    try {
        let melhor = null, area = 0;
        for (const v of document.querySelectorAll('video')) {
            const r = v.getBoundingClientRect();
            const a = r.width * r.height;
            if (a > area) { area = a; melhor = v; }
        }
        if (melhor && melhor.poster) candidatos.push(melhor.poster);
    } catch (e) {}
    for (const c of candidatos) {
        const a = abs(c);
        if (a) return a;
    }
    return '';
}
"""


async def _titulo_e_thumb(page) -> tuple[str, str]:
    try:
        titulo = await page.evaluate(
            "() => (document.querySelector('meta[property=\\\"og:title\\\"]') || {}).content "
            "|| document.title || ''"
        )
    except Exception:
        titulo = ""
    try:
        thumb = await page.evaluate(_JS_EXTRAIR_THUMB)
    except Exception:
        thumb = ""
    return (titulo or "").strip(), (thumb or "").strip()


async def _cookie_header_para_url(context, url: str) -> str:
    """Cookies do contexto do navegador para o domínio do manifesto (1.7 do plano)."""
    try:
        cookies = await context.cookies([url])
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    except Exception:
        return ""


def _headers_relevantes(headers_brutos: dict) -> dict:
    """Descarta Host/Content-Length/Connection/Accept-Encoding e pseudo-headers HTTP/2."""
    out = {}
    for k, v in (headers_brutos or {}).items():
        kl = k.lower()
        if kl.startswith(":") or kl in _HEADERS_DESCARTAR:
            continue
        out[k] = v
    return out


def _headers_finais(headers_brutos: dict) -> dict:
    """Mantém só o que o plano (1.7) pede devolver ao yt-dlp, com casing canônica."""
    out = {}
    for k, v in (headers_brutos or {}).items():
        canon = _HEADERS_MANTER_CANONICOS.get(k.lower())
        if canon:
            out[canon] = v
    return out


async def _escolher_melhor_candidato(context, candidatos: list[dict]) -> dict | None:
    """
    Prioridade (1.5 do plano): master HLS > DASH .mpd > variante HLS > primeiro candidato.
    Master é detectado buscando o conteúdo do .m3u8 pelo próprio contexto do navegador
    (herda cookies/sessão) e procurando '#EXT-X-STREAM-INF'.
    """
    masters, dashes, variantes = [], [], []
    for cand in candidatos:
        caminho = urlparse(cand["url"]).path.lower()
        if caminho.endswith(".mpd"):
            dashes.append(cand)
            continue
        if caminho.endswith(".m3u8") or caminho.endswith(".m3u"):
            try:
                resp = await context.request.get(
                    cand["url"], headers=cand["headers"], timeout=8000
                )
                corpo = await resp.text()
                if "#EXT-X-STREAM-INF" in corpo:
                    masters.append(cand)
                else:
                    variantes.append(cand)
            except Exception:
                variantes.append(cand)
    if masters:
        return masters[0]
    if dashes:
        return dashes[0]
    if variantes:
        return variantes[0]
    return candidatos[0] if candidatos else None


# ── Miniatura gerada via ffmpeg (defeito 1b): fallback quando a página não tem NENHUMA
# fonte de miniatura no DOM (site 100% blob/MSE — confirmado por sondagem real: og:image,
# og:image:secure_url, twitter:image, itemprop=thumbnailUrl, link[rel=image_src],
# JSON-LD e poster do <video> vieram todos vazios). Não usa o navegador — é chamada por
# servidor.py depois que o Playwright já fechou o contexto, então busca o corpo do
# manifesto e roda o ffmpeg sozinha, direto por HTTP.
def _variante_menor_banda(corpo_master: str, url_master: str) -> str:
    """
    Se `corpo_master` for um master HLS, resolve a URL da variante de MENOR BANDWIDTH.
    Evita que o ffmpeg precise abrir/negociar a variante de maior qualidade (ex.: 1080p)
    só para extrair um frame de miniatura de 320px — pega a menor direto, por URL.
    """
    melhor_uri = None
    melhor_bw = None
    linhas = corpo_master.splitlines()
    for i, linha in enumerate(linhas):
        if not linha.startswith("#EXT-X-STREAM-INF"):
            continue
        m = re.search(r"BANDWIDTH=(\d+)", linha)
        if not m:
            continue
        bw = int(m.group(1))
        uri = ""
        for prox in linhas[i + 1:]:
            prox = prox.strip()
            if not prox or prox.startswith("#"):
                continue
            uri = prox
            break
        if uri and (melhor_bw is None or bw < melhor_bw):
            melhor_bw = bw
            melhor_uri = uri
    if melhor_uri:
        return urljoin(url_master, melhor_uri)
    return url_master


def _gerar_thumbnail_ffmpeg_sync(manifesto: str, headers: dict, timeout_s: float) -> bytes | None:
    """
    Roda em thread (bloqueante): resolve a variante de menor banda (se `manifesto` for
    um master HLS) e extrai um frame com ffmpeg, devolvendo os bytes do JPEG (ou None em
    qualquer falha — nunca levanta exceção para o chamador).
    """
    exe = shutil.which("ffmpeg") or "ffmpeg"
    headers = headers or {}

    url_entrada = manifesto
    try:
        req = urllib.request.Request(manifesto, headers=dict(headers))
        with urllib.request.urlopen(req, timeout=8) as resp:
            corpo = resp.read(200_000).decode("utf-8", errors="ignore")
        if "#EXT-X-STREAM-INF" in corpo:
            url_entrada = _variante_menor_banda(corpo, manifesto)
    except Exception:
        pass  # segue com o manifesto original — ffmpeg tenta abrir do jeito que está

    # Headers via -headers do ffmpeg, terminador \r\n obrigatório (senão o CDN rejeita).
    linhas_header = ""
    for chave in ("Referer", "User-Agent", "Cookie"):
        v = headers.get(chave)
        if v:
            linhas_header += f"{chave}: {v}\r\n"

    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"]
    if linhas_header:
        cmd += ["-headers", linhas_header]
    cmd += [
        "-ss", "3",                 # pula os primeiros segundos — evita frame preto de abertura
        "-i", url_entrada,
        "-frames:v", "1",
        "-an",
        "-vf", "scale=320:-1",      # UI mostra em 48x36; 320px de largura é mais que suficiente
        "-q:v", "5",                # qualidade JPEG moderada (escala ffmpeg: 2=melhor .. 31=pior)
        "-f", "image2",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            creationflags=_subprocess_flags_windows_silencioso(),
        )
        dados = r.stdout
        if dados and len(dados) > 200:  # descarta saída vazia/lixo
            return dados
    except Exception:
        pass
    return None


async def gerar_thumbnail_ffmpeg(manifesto: str, headers: dict, *, timeout_s: float = 20.0) -> str:
    """
    Fallback de miniatura quando a página não tem nenhuma fonte no DOM: extrai um frame
    do próprio manifesto com ffmpeg e devolve como **data URI**
    (`data:image/jpeg;base64,...`) — nunca um caminho de arquivo, porque a interface
    também é servida em `http://127.0.0.1:8764` (controlador.py), onde `<img src="file://...">`
    é bloqueado pelo navegador.

    Nunca levanta exceção: qualquer falha (ffmpeg ausente, timeout, CDN recusando,
    manifesto inválido) devolve "" e quem chamou segue o download normalmente — a
    miniatura é cosmética, jamais pode derrubar o fluxo principal.
    """
    try:
        loop = asyncio.get_event_loop()
        dados = await loop.run_in_executor(
            None, _gerar_thumbnail_ffmpeg_sync, manifesto, headers, timeout_s
        )
        if not dados:
            return ""
        b64 = base64.b64encode(dados).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception as e:
        print(f"[sniffer] gerar_thumbnail_ffmpeg falhou: {type(e).__name__}: {e}")
        return ""


# ── Núcleo: navega, provoca o player, intercepta o manifesto ──────────────────
async def _descobrir_manifesto_impl(url: str, reportar, timeout_s: float) -> dict | None:
    # Pré-condição: quem chamou (`descobrir_manifesto`) já garantiu as dependências ANTES
    # de entrar no orçamento de tempo do sniff — não repetimos a instalação aqui (2 do
    # relatório de revisão / ERRORS.md: pip install + download do Chromium não podem
    # competir com timeout_s).
    global _t_ultimo_uso
    async with _semaforo_sniff:
        _t_ultimo_uso = time.monotonic()
        _chamar_reportar(reportar, "abrindo navegador headless…")

        browser = await _obter_browser()
        _t_ultimo_uso = time.monotonic()

        # Orçamento de tempo da fase de interação (goto + tentativas de achar o manifesto):
        # sempre deixa uma folga (`_RESERVA_LIMPEZA_S`) para o context.close() acontecer
        # DENTRO do timeout_s, em vez de o asyncio.wait_for externo cancelar tudo no meio
        # de uma chamada do Playwright (o que gera ruído tipo "Future exception was never
        # retrieved" e deixa o cleanup mais arriscado).
        inicio_interacao = time.monotonic()

        def _restante(reserva: float, minimo: float = 0.3) -> float:
            r = timeout_s - (time.monotonic() - inicio_interacao) - reserva
            return r if r > minimo else minimo

        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            screen={"width": 1920, "height": 1080},
            device_scale_factor=1,
            user_agent=UA_DESKTOP,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            ignore_https_errors=True,
        )
        tarefas_resposta: set[asyncio.Task] = set()
        page = None
        _agendar_resposta = None
        ao_requisitar = None
        try:
            await context.add_init_script(_JS_INIT_VIEWPORT)

            candidatos: list[dict] = []
            evento_manifesto = asyncio.Event()

            def ao_requisitar(req) -> None:
                try:
                    caminho = urlparse(req.url).path
                    if _RE_MANIFESTO_EXT.search(caminho):
                        candidatos.append({
                            "url":     req.url,
                            "headers": _headers_relevantes(req.headers),
                            "ordem":   len(candidatos),
                        })
                        evento_manifesto.set()
                except Exception:
                    pass

            async def ao_responder(resp) -> None:
                try:
                    ct = (resp.headers.get("content-type") or "").lower()
                    if any(m in ct for m in _CONTENT_TYPES_MANIFESTO):
                        req = resp.request
                        candidatos.append({
                            "url":     req.url,
                            "headers": _headers_relevantes(req.headers),
                            "ordem":   len(candidatos),
                        })
                        evento_manifesto.set()
                except Exception:
                    pass

            def _agendar_resposta(resp) -> None:
                t = asyncio.create_task(ao_responder(resp))
                tarefas_resposta.add(t)
                t.add_done_callback(tarefas_resposta.discard)

            page = await context.new_page()
            page.on("request", ao_requisitar)
            page.on("response", _agendar_resposta)

            _chamar_reportar(reportar, "carregando página…")
            try:
                goto_timeout_s = min(30.0, _restante(reserva=24.0))
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=goto_timeout_s * 1000
                )
            except Exception:
                pass  # segue tentando mesmo se o goto formalmente expirar

            # Verificação obrigatória do requisito de 1920x1080 (1.4 do plano) — logado sempre.
            try:
                dims = await page.evaluate(
                    "() => [window.innerWidth, window.innerHeight, screen.width, "
                    "screen.height, window.outerWidth, window.devicePixelRatio]"
                )
                print(
                    "[sniffer] viewport: innerWidth=%s innerHeight=%s screen=%sx%s "
                    "outerWidth=%s dpr=%s" % (dims[0], dims[1], dims[2], dims[3], dims[4], dims[5])
                )
            except Exception as e_dims:
                print(f"[sniffer] não foi possível ler viewport: {e_dims}")

            _chamar_reportar(reportar, "procurando stream…")
            await _tentar_dispensar_banners(page)
            await _tentar_dar_play(page)

            try:
                await asyncio.wait_for(
                    evento_manifesto.wait(), timeout=min(8.0, _restante(reserva=16.0))
                )
            except asyncio.TimeoutError:
                pass

            if not candidatos:
                await _tentar_clicar_player(page)
                try:
                    await asyncio.wait_for(
                        evento_manifesto.wait(), timeout=min(10.0, _restante(reserva=6.0))
                    )
                except asyncio.TimeoutError:
                    pass

            if not candidatos:
                espera_final_s = min(3.0, _restante(reserva=3.0))
                await page.wait_for_timeout(espera_final_s * 1000)

            if not candidatos:
                print(f"[sniffer] nenhum manifesto encontrado para {url}")
                return None

            melhor = await _escolher_melhor_candidato(context, candidatos)
            if not melhor:
                return None

            titulo, thumb = await _titulo_e_thumb(page)

            headers = _headers_finais(melhor["headers"])
            if "Referer" not in headers:
                headers["Referer"] = url
            cookie_extra = await _cookie_header_para_url(context, melhor["url"])
            if cookie_extra:
                existente = headers.get("Cookie", "")
                headers["Cookie"] = "; ".join(p for p in (existente, cookie_extra) if p)

            tipo = "dash" if urlparse(melhor["url"]).path.lower().endswith(".mpd") else "hls"
            print(f"[sniffer] manifesto encontrado ({tipo}): {melhor['url']}")

            return {
                "manifesto": melhor["url"],
                "tipo":      tipo,
                "headers":   headers,
                "titulo":    titulo,
                "thumb":     thumb,
                "pagina":    url,
            }
        finally:
            # Evita "Future exception was never retrieved" ao fechar contexto com
            # handlers de resposta ainda em voo (ex.: timeout externo cortando o sniff):
            # para de escutar eventos primeiro, para nenhum novo handler ser agendado
            # durante o close(), depois cancela e drena o que já estava em voo.
            if page is not None:
                try:
                    page.remove_listener("response", _agendar_resposta)
                    page.remove_listener("request", ao_requisitar)
                except Exception:
                    pass
            if tarefas_resposta:
                for t in list(tarefas_resposta):
                    t.cancel()
                await asyncio.gather(*tarefas_resposta, return_exceptions=True)
            try:
                await context.close()
            except Exception:
                pass


# Teto para instalar o Playwright + sondar o canal do navegador (e, na pior hipótese,
# baixar o Chromium empacotado, ~150 MB). Deliberadamente bem maior que o timeout_s de
# navegação — é um custo de primeira execução, não deve competir com ele (ver ERRORS.md).
TIMEOUT_PREPARO_DEPENDENCIAS_S = 300.0


async def descobrir_manifesto(url: str, *, timeout_s: float = 45.0, reportar=None) -> dict | None:
    """
    Contrato público (1.1 do plano). Nunca levanta exceção para fora: qualquer erro
    interno (Playwright ausente, timeout, crash do navegador, etc.) vira None + log,
    para que o chamador preserve o erro original do yt-dlp.
    """
    try:
        # Preparo de dependências FORA do orçamento de navegação: numa máquina sem
        # Playwright, "pip install playwright" (e o download do Chromium, se nenhum
        # navegador do sistema funcionar) não pode consumir o timeout_s do sniff — senão
        # a primeira execução estoura o tempo sem motivo aparente para quem está usando.
        try:
            deps_ok = await asyncio.wait_for(
                garantir_dependencias(reportar), timeout=TIMEOUT_PREPARO_DEPENDENCIAS_S
            )
        except asyncio.TimeoutError:
            print(f"[sniffer] preparo de dependências excedeu {TIMEOUT_PREPARO_DEPENDENCIAS_S}s")
            return None
        if not deps_ok:
            return None

        return await asyncio.wait_for(
            _descobrir_manifesto_impl(url, reportar, timeout_s), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        print(f"[sniffer] tempo esgotado ({timeout_s}s) para {url}")
        return None
    except Exception as e:
        print(f"[sniffer] falhou para {url}: {type(e).__name__}: {e}")
        return None


if __name__ == "__main__":
    # Uso standalone para depuração: py -3 sniffer.py "https://exemplo.com/pagina-com-video"
    async def _main():
        if len(sys.argv) < 2:
            print("uso: py -3 sniffer.py <url>")
            return
        resultado = await descobrir_manifesto(sys.argv[1], reportar=print)
        print(json.dumps(resultado, ensure_ascii=False, indent=2))
        await encerrar()

    asyncio.run(_main())
