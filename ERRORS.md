# ERRORS.md — Log de erros e soluções (VDGET)

Registro de problemas não-triviais encontrados durante o desenvolvimento e suas soluções.
Antes de investigar um erro novo, consulte esta lista.

---

## -2. Remover/abrir item não fazia nada com o servidor desligado (falha silenciosa)

**Sintoma**

Com o servidor parado — o que acontece sozinho, porque o watchdog de inatividade encerra
o VDGET após 3 minutos ociosos — clicar em "Remover", "Limpar todos", "Abrir pasta" ou
"Abrir arquivo" não produzia efeito **nem mensagem**. A interface continuava a mostrar os
downloads (o histórico de concluídos vive no `localStorage`), mas os botões pareciam mortos.

**Causa raiz**

As quatro ações passavam por `enviar()`, que é um no-op silencioso quando o WebSocket não
está aberto:

```js
function enviar(dados) {
  if (ws && ws.readyState === WebSocket.OPEN) { ws.send(...); }
}
```

Remover um card dependia do **eco** do servidor (`{"tipo":"remover"}`) para chamar
`removerCard()`. Sem servidor, não havia eco e nada acontecia. Isso era desnecessário:
remover é uma operação puramente de interface, já que o histórico é local.

Havia um segundo problema, específico de abrir arquivo/pasta: o servidor resolvia o caminho
por `downloads[dl_id]`, e esse dicionário **volta vazio a cada boot**. Mesmo religando o
servidor, abrir um item antigo falharia, porque só o cliente ainda sabe o caminho.

**Solução aplicada**

- `removerItem()` e `limparConcluidos()` agem **localmente primeiro** (`removerCard()`, que
  já persiste no `localStorage`) e só depois avisam o servidor. `removerCard()` é
  idempotente, então o eco do servidor, quando existe, não duplica efeito.
- `limparConcluidos()` replica o critério do servidor — só `status === "concluido"`; itens
  com erro continuam na lista.
- Abrir arquivo/pasta não tem como funcionar sem um processo local (o navegador não acede
  ao sistema de ficheiros). Passou a enfileirar a ação, disparar `vdget://iniciar` para
  religar o servidor e executá-la ao reconectar, com aviso visível e prazo de validade.
- O cliente passou a enviar `arquivo`/`destino` junto com a ação, e o servidor usa esses
  campos como fallback quando não conhece o `dl_id`.

**Como evitar no futuro**

Qualquer ação nova que seja apenas de interface (mexe só no que está no `localStorage`) deve
agir localmente e tratar o servidor como sincronização opcional. Só o que exige o sistema
operativo (abrir ficheiro, revelar pasta, baixar) precisa mesmo do backend — e nesse caso o
cliente deve mandar os dados necessários, porque o estado do servidor não sobrevive ao boot.

**Nota de segurança**

Aceitar um caminho vindo do cliente permitiria pedir ao servidor que abrisse qualquer coisa
com o app padrão (ex.: um `.exe`). Por isso `caminho_midia_do_cliente()` valida que o ficheiro
existe **e** que a extensão está na lista de mídia que o VDGET produz. Revelar a pasta não
passa por essa trava, porque só seleciona o item no explorador e não executa nada.

---

## -1. Downloads resgatados pelo sniffer não tinham miniatura (página sem NENHUMA fonte no DOM)

**Sintoma**

Downloads que caem no fallback do sniffer (players MSE/`blob:`) concluíam normalmente,
mas apareciam sem miniatura na lista — ao contrário de fontes que o yt-dlp resolve
sozinho (YouTube, SoundCloud etc.), que sempre mostram thumb.

**Causa raiz**

`_titulo_e_thumb()` em `sniffer.py` lia **só** `meta[property="og:image"]`. Sondagem real
da página de teste (depois de dar play e esperar o player inicializar) mostrou que
**todas** as fontes de miniatura no DOM vinham vazias: `og:image`, `og:image:secure_url`,
`twitter:image`, `itemprop="thumbnailUrl"`, `link[rel="image_src"]`, JSON-LD
(`thumbnailUrl`/`thumbnail`) e até o atributo `poster` do `<video>` — o player é 100%
`blob:`/MSE e nunca expõe uma imagem de capa em lugar nenhum do HTML. Ou seja: não é (só)
falta de cobertura de meta tags — nem toda extração de DOM ampliada resolveria esse caso
específico, porque a página genuinamente não tem imagem nenhuma para extrair.

**Solução aplicada**

Duas camadas, nesta ordem:

1. **Extração de DOM ampliada** (`_JS_EXTRAIR_THUMB` em `sniffer.py`): cobre as seis
   fontes citadas acima, resolvendo relativo→absoluto com
   `new URL(u, document.baseURI).href` dentro do próprio navegador (mais correto que
   `urljoin` em Python puro — já respeita `<base href>`). Ajuda outros sites mesmo não
   ajudando este.
2. **Fallback gerando o frame com ffmpeg** (`gerar_thumbnail_ffmpeg()` em `sniffer.py`,
   chamada por `tentar_resgate_por_sniffer()` em `servidor.py` assim que o manifesto é
   encontrado, antes de iniciar o download): quando (1) não devolve nada, extrai um frame
   direto do manifesto com `ffmpeg -ss 3 -i <variante_de_menor_banda> -frames:v 1 -vf
   scale=320:-1 -q:v 5 -f image2 -vcodec mjpeg pipe:1`, passando os headers capturados
   (Referer/User-Agent/Cookie) via `-headers` (terminador `\r\n` obrigatório — sem isso o
   CDN rejeita a requisição). A variante de menor banda é resolvida por conta própria
   (parseando `BANDWIDTH=` do master HLS) em vez de confiar na seleção automática do
   ffmpeg, que é indocumentada/variável entre versões — assim nunca baixa a variante
   1080p só para gerar uma miniatura de 320px. Devolve **`data:image/jpeg;base64,...`**,
   nunca um caminho de arquivo: a interface roda tanto em `file://` quanto servida por
   `controlador.py` em `http://127.0.0.1:8764`, e um `<img src="file:///...">` é bloqueado
   nesse segundo contexto. Qualquer falha (ffmpeg ausente, timeout, CDN recusando,
   manifesto inválido) devolve `""` e o download segue normalmente — miniatura é
   cosmética, nunca pode derrubar o fluxo principal.

**Como evitar no futuro**

Miniaturas de fontes que o yt-dlp já resolve sozinho continuam vindo de
`melhor_thumbnail(info)` — o ffmpeg só é chamado quando a extração de DOM do sniffer não
achou nada, para não gerar frame à toa em sites que já têm miniatura própria.

---

## -0.5. `onerror` da miniatura deixava um quadrado vazio em vez do ícone de status

**Sintoma**

Quando a URL da miniatura falhava ao carregar (comum em CDNs com proteção de hotlink,
que devolvem 403 sem um `Referer` "correto"), a `<img>` era escondida
(`this.style.display = 'none'`) e sobrava um quadrado vazio no card — nem imagem, nem o
ícone de placeholder que aparece quando não há `dl.thumb` nenhum.

**Causa raiz**

O handler `onerror` só escondia o elemento (`display: none`), sem nunca restaurar o
ícone de fallback (`ICON_THUMB[status] || ICON_THUMB_FALLBACK`) nem limpar
`dataset.thumbSrc` — então uma nova tentativa com a mesma URL (ex.: reabrir a página)
também ficava travada, já que o dataset ainda registrava aquela URL como "já tentada".

**Solução aplicada**

Em ambos os caminhos de renderização (`atualizarCard()`, via `img.onerror` em JS, e
`buildCardHTML()`, via `onerror="onThumbError(this, status)"` inline — a nova função
global `onThumbError()`), o `onerror` agora: apaga `thumb.dataset.thumbSrc` e substitui o
conteúdo de `.dl-thumb` pelo ícone de status (`ICON_THUMB[status] || ICON_THUMB_FALLBACK`).
Também foi adicionado `referrerpolicy="no-referrer"` na `<img>` — várias proteções de
hotlink liberam a imagem quando não há `Referer` nenhum, então isso sozinho já recupera
miniaturas de alguns sites sem precisar do fallback do ícone.

**Como evitar no futuro**

Qualquer novo lugar que renderize `dl.thumb` como `<img>` precisa do mesmo par
`referrerpolicy="no-referrer"` + `onerror` que restaura o ícone (nunca só esconder o
elemento).

---

## 0. `KeyError` em `executar_download` se o usuário remove o item durante o resgate via sniffer

**Sintoma**

Se o usuário clica em remover um download enquanto ele está em `"analisando"` (sniffer
headless rodando) ou já de volta em `"baixando"` (após o manifesto ser encontrado), a task
`executar_download` pode levantar `KeyError: '<dl_id>'` ao tentar atualizar ou fazer
broadcast de `downloads[dl_id]` depois que o item já foi apagado do dicionário.

**Causa raiz**

O handler da ação `remover` faz `del downloads[dl_id]` **incondicionalmente**, mesmo para
downloads ativos — não existe trava para itens em transferência. Isso já era uma janela de
risco pré-existente (algumas linhas no fim de `executar_download` acessavam
`downloads[dl_id]` sem checar se a chave ainda existia), mas era uma janela de poucos
segundos. O resgate via sniffer **amplia essa janela para até dezenas de segundos**
(até `timeout_s` de sniff, mais o download do manifesto) com o status preso em
`"analisando"` — um estado que parece "travado" para quem está usando a interface e torna
a remoção no meio do processo muito mais provável de acontecer na prática.

Havia inclusive uma inconsistência interna: `tentar_resgate_por_sniffer()` já verificava
`if dl_id not in downloads` antes de tocar no dicionário, mas o código que a chama
(`executar_download`, no bloco `except` final e na linha do broadcast de fechamento) não
tinha a mesma guarda.

**Solução aplicada**

Seguido o mesmo padrão já usado após `_somente_meta()` no próprio arquivo
(`if dl_id not in downloads: return`), adicionado antes de cada acesso a
`downloads[dl_id]` que fecha a função `executar_download`:

- antes de gravar `"concluido"` (caminho de sucesso);
- no início do bloco `except` (antes de decidir se tenta o sniffer);
- antes de gravar `"erro"`, depois do `await tentar_resgate_por_sniffer(...)` — porque o
  item pode ter sido removido justamente **durante** esse `await`;
- antes do `broadcast` final e do `marcar_atividade_download_servidor()`.

**Como evitar no futuro**

Qualquer novo ponto em `executar_download` (ou em código chamado por ele) que leia ou
grave `downloads[dl_id]` **depois de um `await`** precisa reconfirmar
`dl_id in downloads` logo antes — o dicionário pode ter mudado enquanto a coroutine estava
suspensa. `hook()` (progresso do yt-dlp) roda em thread separada e também acessa
`downloads[dl_id]` sem essa guarda; hoje isso é inofensivo porque qualquer `KeyError`
lá dentro sobe como a exceção do próprio download e cai no `except` (já protegido), mas se
`hook()` for reaproveitado em um contexto que não tenha esse `except` ao redor, precisa
da mesma guarda.

---

## 1. `UnicodeEncodeError: 'charmap' codec can't encode characters` ao rodar `servidor.py` com stdout redirecionado

**Sintoma**

```
File "servidor.py", line ..., in main
    print(f"""...""")
File "...\encodings\cp1252.py", line 19, in encode
    return codecs.charmap_encode(input,self.errors,encoding_table)[0]
UnicodeEncodeError: 'charmap' codec can't encode characters in position 2-49: character maps to <undefined>
```

O processo morre antes de abrir o WebSocket — nem chega a imprimir o banner completo.

**Causa raiz**

Quando o `stdout` do `servidor.py` é um console real do Windows, o Python usa a codepage
do console (normalmente já preparada para UTF-8/OEM com os caracteres certos). Mas quando
o `stdout` **não** é um console — redirecionado para arquivo (`> log.txt`), para um pipe, ou
para `subprocess.DEVNULL`/`PIPE` (é exatamente o que `controlador.py` faz ao subir o
`servidor.py` em segundo plano) — o Python herda a codepage ANSI padrão do Windows
(ex.: `cp1252`), que não tem os caracteres de desenho de caixa (`╔`, `║`, `╚`...) usados no
banner de abertura. Qualquer `print()` com esses caracteres ou acentos derruba o processo
com `UnicodeEncodeError` antes de servir qualquer coisa.

Isso é **preexistente** (não foi introduzido pela feature de sniffer), mas foi descoberto
ao testar o servidor real redirecionando a saída para arquivo de log — e é o mesmo caminho
de execução que `controlador.py` usa em produção (`stdout=subprocess.DEVNULL`), então o
risco também existe fora do cenário de teste.

**Solução aplicada**

Logo no topo de `servidor.py`, antes de qualquer `print()`:

```python
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
```

Isso força UTF-8 (com substituição de caracteres não mapeáveis em vez de exceção) para
qualquer destino de stdout/stderr, independentemente de ser console, arquivo ou pipe.

**Como evitar no futuro**

Ao rodar `servidor.py` manualmente com saída redirecionada para depuração no Windows,
não é mais necessário setar `PYTHONIOENCODING=utf-8` manualmente — mas se o sintoma
voltar a aparecer em algum ambiente, isso confirma que o `reconfigure()` falhou
silenciosamente (por exemplo, Python < 3.7, que não tem `TextIOWrapper.reconfigure`) —
nesse caso, definir a variável de ambiente `PYTHONIOENCODING=utf-8` é o workaround direto.

---

## 2. Orçamento de tempo do sniffer podia estourar o `timeout_s` externo e gerar ruído `Future exception was never retrieved`

**Sintoma**

Ao testar `sniffer.descobrir_manifesto()` isoladamente com um `timeout_s` mais curto que a
soma dos tempos internos de espera (goto até 30s + espera de evento 8s + 10s + fallback 3s
= até 51s no pior caso), o `asyncio.wait_for` externo cancelava a corrotina no meio de uma
chamada do Playwright, e o processo imprimia no stderr:

```
Future exception was never retrieved
future: <Future finished exception=TargetClosedError('Target page, context or browser has been closed')>
```

Não quebrava o retorno (`None` continuava vindo certo), mas é ruído indesejado e sintoma de
que o cleanup (`context.close()`) podia ser interrompido no meio pelo cancelamento externo.

**Causa raiz**

Os tempos de espera internos (`goto`, as duas esperas pelo evento de manifesto e o sleep de
fallback) eram fixos e, somados, podiam ultrapassar o próprio `timeout_s` passado para
`descobrir_manifesto()` — inclusive com o valor padrão de produção (45s), já que
30+8+10+3 = 51s no pior caso. Isso fazia o `asyncio.wait_for` externo (que existe
justamente para isso) cortar a operação no meio de um `await` do Playwright, abandonando
uma promise interna do Playwright que só recebia sua exceção depois — e como ninguém mais
aguardava por ela, o asyncio reportava a exceção como "nunca recuperada".

**Solução aplicada**

`_descobrir_manifesto_impl` agora recebe o `timeout_s` e calcula um orçamento de tempo
restante (`_restante(reserva)`) antes de cada espera (goto, as duas esperas por evento, e o
sleep de fallback), sempre deixando uma folga para o `context.close()` acontecer **dentro**
do prazo, em vez de depender do cancelamento externo. Também foram removidos os listeners
de `request`/`response` e drenadas (com cancelamento) as tasks de resposta ainda em voo
antes de fechar o contexto, no `finally`.

**Como evitar no futuro**

Qualquer nova etapa de espera adicionada ao sniffer deve usar `_restante(reserva)` em vez
de um valor fixo, para que o orçamento total nunca dependa do `asyncio.wait_for` externo
para se autolimitar.

---

## 3. Primeira execução (Playwright não instalado) podia estourar o `timeout_s` do sniff sem motivo aparente

**Sintoma**

Numa máquina sem Playwright, o primeiro download que caísse no fallback do sniffer falhava
(`None`) sem nenhum erro concreto no log além do timeout — mas rodar de novo funcionava.

**Causa raiz**

`garantir_dependencias()` (que faz `pip install playwright` e, se nenhum navegador do
sistema funcionar, `playwright install chromium`, ~150 MB) era chamada **dentro** de
`_descobrir_manifesto_impl`, que por sua vez estava inteiramente envolvida pelo
`asyncio.wait_for(..., timeout=timeout_s)` de `descobrir_manifesto()` (45s por padrão). A
instalação do pacote (alguns segundos) já competia com esse orçamento; no pior caso, com o
download do Chromium embutido, estourava com folga. Como o `pip install` roda em thread via
executor e não morre com o cancelamento do `asyncio.wait_for`, a tentativa seguinte
encontrava tudo já instalado/cacheado e funcionava — mascarando a causa e parecendo um
problema aleatório de "primeira vez".

**Solução aplicada**

`garantir_dependencias()` foi movida para fora do `_descobrir_manifesto_impl` e passou a
ser chamada em `descobrir_manifesto()` **antes** do `asyncio.wait_for(timeout=timeout_s)` de
navegação, com um teto próprio e bem mais generoso (`TIMEOUT_PREPARO_DEPENDENCIAS_S = 300s`).
Assim, o custo de instalação (só ocorre na primeira execução da máquina, depois fica em
cache) nunca compete com o tempo de navegação/descoberta do manifesto.

**Como evitar no futuro**

Qualquer preparo de ambiente que envolva instalação de pacotes ou download de binários deve
ficar fora do orçamento de tempo da operação "de verdade" que ele habilita — dar-lhe um
`asyncio.wait_for` próprio, sequenciado antes, em vez de aninhado dentro do mesmo.
