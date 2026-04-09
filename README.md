# VDGET — Gerenciador de downloads (yt-dlp + WebSocket)

Interface web para enfileirar downloads com **yt-dlp**; o backend em Python expõe um servidor **WebSocket** na porta **8765** e a página `index.html` conversa com ele.

## Requisitos

- **Windows** (os `.bat` e o registo do protocolo são para este SO).
- **Python 3** no PATH (o `iniciar.bat` tenta `py -3`, depois `python`, depois `python3`).
- Pacotes Python (instalados automaticamente pelo `iniciar.bat` se faltarem): `websockets`, `yt-dlp`.

## Como rodar

### Forma recomendada: `iniciar.bat`

1. Dê um duplo clique em **`iniciar.bat`** (ou execute pelo Explorador).
2. O script verifica o Python, instala dependências se precisar, inicia **`servidor.py`** numa janela minimizada e abre **`index.html`** no navegador.
3. A janela principal do `.bat` fica aberta: ao pressionar uma tecla, ela tenta encerrar o processo do servidor pelo título da janela.

Na interface, com o WebSocket conectado, o botão **Parar servidor** envia um comando ao Python e encerra o backend (equivalente a fechar o processo do servidor).

### Abrir só a página (servidor já ligado)

Se **`servidor.py`** já estiver a correr, pode abrir **`index.html`** diretamente no navegador (arquivo local). A página liga a `ws://localhost:8765`.

### Protocolo personalizado `vdget://` (opcional)

1. Execute **`instalar-protocolo.bat`** uma vez (regista o protocolo no **HKCU** apontando para o `iniciar.bat` desta pasta).
2. No site ou na barra de endereços use algo como **`vdget://iniciar`** (o link **Ligar VDGET** na interface usa isso).

Se mover a pasta do projeto, volte a correr **`instalar-protocolo.bat`** para atualizar o caminho.

> O ficheiro **`vdget-protocol.reg`** é um exemplo com caminho fixo; prefira o `.bat`, que gera o `.reg` com o caminho correto.

### Modo painel HTTP: `controlador.py` (opcional)

```bash
py -3 controlador.py
```

- Sobe um servidor HTTP em **`http://127.0.0.1:8764/`** que serve o `index.html`.
- API JSON: `GET /api/status`, `POST /api/start`, `POST /api/stop`, `POST /api/restart`.
- Ao iniciar, tenta subir o **`servidor.py`** (WebSocket) em segundo plano, sem janela de consola no Windows.

Útil se quiser uma URL `http://…` em vez de abrir o ficheiro HTML diretamente.

## O que cada ficheiro faz (resumo)

| Ficheiro | Função |
|----------|--------|
| **`index.html`** | Interface (lista de downloads, estatísticas, ligação WebSocket, botão para encerrar o servidor). |
| **`servidor.py`** | Backend: WebSocket na porta **8765**, fila de downloads com **yt-dlp**, pastas/arquivos no disco. |
| **`iniciar.bat`** | Arranque rápido: valida Python e ficheiros, instala dependências, inicia `servidor.py` minimizado e abre `index.html`. |
| **`instalar-protocolo.bat`** | Regista o protocolo **`vdget://`** no Windows para disparar o `iniciar.bat` desta pasta. |
| **`vdget-protocol.reg`** | Exemplo de registo manual do protocolo (caminho fixo; use o `.bat` após mover o projeto). |
| **`controlador.py`** | Servidor HTTP local (**8764**): serve a interface e API para ligar/parar/reiniciar o `servidor.py`. |
| **`downloads/`** | Pasta criada em execução (se usar destino padrão): ficheiros baixados ficam aqui, ao lado do servidor. |

## Portas

- **8765** — WebSocket (`servidor.py`).
- **8764** — HTTP de controlo e página (`controlador.py`), só se usar esse script.
