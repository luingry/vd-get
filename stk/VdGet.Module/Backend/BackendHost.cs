using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using SwissTechKnife.Sdk;
using VdGet.Module.Native;

namespace VdGet.Module.Backend
{
    internal enum BackendState
    {
        /// <summary>Not running (never started, stopped, or closed itself after being idle).</summary>
        Stopped,
        Starting,
        Ready,
        /// <summary>No Python 3 found on this machine.</summary>
        NoPython,
        /// <summary>The backend exited with an error or never accepted a connection.</summary>
        Failed,
    }

    /// <summary>
    /// Runs servidor.py hidden (no console, no browser) as a child of the host and talks to it over its
    /// WebSocket. Started on demand; the backend closes itself after a few idle minutes and is started
    /// again by the next command. Events are raised on pool threads.
    /// </summary>
    internal sealed class BackendHost : IDisposable
    {
        private static readonly TimeSpan ConnectTimeout = TimeSpan.FromMinutes(3); // first run installs yt-dlp
        private static readonly TimeSpan ReadyTimeout = TimeSpan.FromMinutes(3);
        private const int IdleSeconds = 180;

        private readonly object _gate = new object();
        private readonly string _backendDirectory;
        private readonly string _dataDirectory;
        private readonly string _defaultDownloads;
        private readonly IModuleLog _log;
        private readonly SemaphoreSlim _sendLock = new SemaphoreSlim(1, 1);
        private readonly LinkedList<string> _recentOutput = new LinkedList<string>();

        private Process _process;
        private JobObject _job;
        private ClientWebSocket _socket;
        private CancellationTokenSource _session;
        private TaskCompletionSource<bool> _ready = NewReadySignal();
        private BackendState _state = BackendState.Stopped;
        private string _pythonPath;
        private StreamWriter _logWriter;
        private bool _disposed;

        public BackendHost(string backendDirectory, string dataDirectory, string defaultDownloads, IModuleLog log)
        {
            _backendDirectory = backendDirectory;
            _dataDirectory = dataDirectory;
            _defaultDownloads = defaultDownloads;
            _log = log;
        }

        public event Action<BackendState> StateChanged;

        /// <summary>A decoded message from the backend (<c>tipo</c> + payload).</summary>
        public event Action<Dictionary<string, object>> MessageReceived;

        public BackendState State
        {
            get { lock (_gate) return _state; }
        }

        /// <summary>Last lines the backend printed (shown when it fails).</summary>
        public string LastError { get; private set; }

        /// <summary>Starts the backend in the background if it is not running; returns at once.</summary>
        public void EnsureStarted()
        {
            lock (_gate)
            {
                if (_disposed || _state == BackendState.Starting || _state == BackendState.Ready)
                {
                    return;
                }
                SetStateLocked(BackendState.Starting);
            }
            Task.Run(() => LaunchAsync());
        }

        /// <summary>Sends an action to the backend, starting it first when needed.</summary>
        public async Task<bool> SendAsync(Dictionary<string, object> message)
        {
            EnsureStarted();
            Task<bool> ready;
            lock (_gate) ready = _ready.Task;
            Task finished = await Task.WhenAny(ready, Task.Delay(ReadyTimeout)).ConfigureAwait(false);
            if (finished != ready || !ready.Result)
            {
                return false;
            }

            ClientWebSocket socket;
            CancellationToken token;
            lock (_gate)
            {
                socket = _socket;
                token = _session?.Token ?? CancellationToken.None;
            }
            if (socket == null || socket.State != WebSocketState.Open)
            {
                return false;
            }

            byte[] payload = Encoding.UTF8.GetBytes(NewSerializer().Serialize(message));
            await _sendLock.WaitAsync().ConfigureAwait(false);
            try
            {
                await socket.SendAsync(new ArraySegment<byte>(payload), WebSocketMessageType.Text, true, token).ConfigureAwait(false);
                return true;
            }
            catch (Exception error) when (error is WebSocketException || error is OperationCanceledException || error is ObjectDisposedException)
            {
                return false;
            }
            finally
            {
                _sendLock.Release();
            }
        }

        /// <summary>Stops the backend and every process it started (downloads in progress are interrupted).</summary>
        public void Stop()
        {
            Process process;
            JobObject job;
            CancellationTokenSource session;
            lock (_gate)
            {
                process = _process;
                job = _job;
                session = _session;
                _process = null;
                _job = null;
                _session = null;
                _socket = null;
                _ready.TrySetResult(false);
                _ready = NewReadySignal();
                if (_state != BackendState.NoPython)
                {
                    SetStateLocked(BackendState.Stopped);
                }
            }

            session?.Cancel();
            if (process != null)
            {
                try
                {
                    if (!process.HasExited)
                    {
                        process.Kill();
                        process.WaitForExit(2000);
                    }
                }
                catch (Exception error) when (error is InvalidOperationException || error is System.ComponentModel.Win32Exception)
                {
                    // Already gone.
                }
                process.Dispose();
            }
            job?.Dispose();
            CloseLog();
        }

        public void Dispose()
        {
            lock (_gate)
            {
                if (_disposed)
                {
                    return;
                }
                _disposed = true;
            }
            Stop();
        }

        private async Task LaunchAsync()
        {
            try
            {
                string script = Path.Combine(_backendDirectory, "servidor.py");
                if (!File.Exists(script))
                {
                    Fail("servidor.py not found: " + script);
                    return;
                }

                if (_pythonPath == null || !File.Exists(_pythonPath))
                {
                    _pythonPath = PythonLocator.Find();
                }
                if (_pythonPath == null)
                {
                    _log.Warn("Python 3 not found.");
                    lock (_gate) SetStateLocked(BackendState.NoPython);
                    lock (_gate) _ready.TrySetResult(false);
                    return;
                }

                int port = FreePort();
                Directory.CreateDirectory(_dataDirectory);
                OpenLog();

                var start = new ProcessStartInfo(_pythonPath, "-u \"" + script + "\"")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    // Never the install folder: a working directory there would block module updates.
                    WorkingDirectory = _dataDirectory,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8,
                };
                start.EnvironmentVariables["VDGET_HOSTED"] = "1";
                start.EnvironmentVariables["VDGET_HOST"] = "127.0.0.1";
                start.EnvironmentVariables["VDGET_PORT"] = port.ToString();
                start.EnvironmentVariables["VDGET_IDLE_SECONDS"] = IdleSeconds.ToString();
                start.EnvironmentVariables["VDGET_DOWNLOADS"] = _defaultDownloads;
                start.EnvironmentVariables["VDGET_DATA"] = _dataDirectory;
                start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
                start.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";

                var process = new Process { StartInfo = start, EnableRaisingEvents = true };
                process.OutputDataReceived += (s, e) => OnOutput(e.Data);
                process.ErrorDataReceived += (s, e) => OnOutput(e.Data);
                process.Exited += OnExited;

                var job = new JobObject();
                var session = new CancellationTokenSource();
                lock (_gate)
                {
                    if (_disposed || _state != BackendState.Starting)
                    {
                        job.Dispose();
                        session.Dispose();
                        return;
                    }
                    process.Start();
                    job.Assign(process);
                    _process = process;
                    _job = job;
                    _session = session;
                }
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                _log.Info($"VDGET backend started (pid {process.Id}, port {port}).");

                await ConnectAsync(process, port, session.Token).ConfigureAwait(false);
            }
            catch (Exception error)
            {
                _log.Error("Could not start the VDGET backend.", error);
                Fail(error.Message);
            }
        }

        private async Task ConnectAsync(Process process, int port, CancellationToken token)
        {
            var uri = new Uri("ws://127.0.0.1:" + port + "/");
            DateTime deadline = DateTime.UtcNow + ConnectTimeout;
            while (!token.IsCancellationRequested)
            {
                if (HasExited(process))
                {
                    return; // OnExited reports it.
                }
                var socket = new ClientWebSocket();
                socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(20);
                try
                {
                    await socket.ConnectAsync(uri, token).ConfigureAwait(false);
                }
                catch (Exception error) when (error is WebSocketException || error is OperationCanceledException || error is ObjectDisposedException)
                {
                    socket.Dispose();
                    if (token.IsCancellationRequested)
                    {
                        return;
                    }
                    if (DateTime.UtcNow > deadline)
                    {
                        Stop();
                        Fail("The backend did not accept a connection in time.");
                        return;
                    }
                    await Task.Delay(300).ConfigureAwait(false);
                    continue;
                }

                lock (_gate)
                {
                    if (token.IsCancellationRequested)
                    {
                        socket.Dispose();
                        return;
                    }
                    _socket = socket;
                    SetStateLocked(BackendState.Ready);
                    _ready.TrySetResult(true);
                }
                await ReceiveLoopAsync(socket, token).ConfigureAwait(false);
                socket.Dispose();
                return;
            }
        }

        private async Task ReceiveLoopAsync(ClientWebSocket socket, CancellationToken token)
        {
            var buffer = new byte[64 * 1024];
            var message = new MemoryStream();
            try
            {
                while (socket.State == WebSocketState.Open && !token.IsCancellationRequested)
                {
                    WebSocketReceiveResult result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), token).ConfigureAwait(false);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        break;
                    }
                    message.Write(buffer, 0, result.Count);
                    if (!result.EndOfMessage)
                    {
                        continue;
                    }
                    string text = Encoding.UTF8.GetString(message.GetBuffer(), 0, (int)message.Length);
                    message.SetLength(0);
                    Dispatch(text);
                }
            }
            catch (Exception error) when (error is WebSocketException || error is OperationCanceledException || error is ObjectDisposedException)
            {
                // Closed by the backend or by Stop().
            }
        }

        private void Dispatch(string text)
        {
            Dictionary<string, object> message;
            try
            {
                message = NewSerializer().DeserializeObject(text) as Dictionary<string, object>;
            }
            catch (ArgumentException)
            {
                return;
            }
            if (message == null)
            {
                return;
            }
            try
            {
                MessageReceived?.Invoke(message);
            }
            catch (Exception error)
            {
                _log.Error("VDGET message handler failed.", error);
            }
        }

        private void OnExited(object sender, EventArgs e)
        {
            var process = (Process)sender;
            int code;
            try { code = process.ExitCode; }
            catch (InvalidOperationException) { code = -1; }

            CancellationTokenSource session;
            JobObject job;
            lock (_gate)
            {
                if (!ReferenceEquals(_process, process))
                {
                    return; // Stop() already handled it.
                }
                session = _session;
                job = _job;
                _process = null;
                _job = null;
                _session = null;
                _socket = null;
                _ready.TrySetResult(false);
                _ready = NewReadySignal();
            }
            session?.Cancel();
            job?.Dispose();
            process.Dispose();

            if (code == 0)
            {
                _log.Info("VDGET backend closed itself after being idle.");
                lock (_gate) SetStateLocked(BackendState.Stopped);
            }
            else
            {
                Fail($"The backend exited with code {code}.");
            }
            CloseLog();
        }

        private void Fail(string reason)
        {
            string tail;
            lock (_recentOutput) tail = string.Join(Environment.NewLine, _recentOutput);
            LastError = string.IsNullOrWhiteSpace(tail) ? reason : reason + Environment.NewLine + tail;
            _log.Warn("VDGET backend failed: " + LastError);
            lock (_gate)
            {
                _ready.TrySetResult(false);
                _ready = NewReadySignal();
                if (!_disposed)
                {
                    SetStateLocked(BackendState.Failed);
                }
            }
        }

        private void OnOutput(string line)
        {
            if (line == null)
            {
                return;
            }
            lock (_recentOutput)
            {
                _recentOutput.AddLast(line);
                while (_recentOutput.Count > 12)
                {
                    _recentOutput.RemoveFirst();
                }
                try
                {
                    _logWriter?.WriteLine(line);
                }
                catch (Exception error) when (error is IOException || error is ObjectDisposedException)
                {
                    _logWriter = null;
                }
            }
        }

        private void OpenLog()
        {
            lock (_recentOutput)
            {
                _recentOutput.Clear();
                CloseLogLocked();
                string path = Path.Combine(_dataDirectory, "backend.log");
                try
                {
                    if (File.Exists(path))
                    {
                        File.Copy(path, Path.Combine(_dataDirectory, "backend.previous.log"), true);
                    }
                    _logWriter = new StreamWriter(path, false, new UTF8Encoding(false)) { AutoFlush = true };
                }
                catch (IOException)
                {
                    _logWriter = null;
                }
                catch (UnauthorizedAccessException)
                {
                    _logWriter = null;
                }
            }
        }

        private void CloseLog()
        {
            lock (_recentOutput) CloseLogLocked();
        }

        private void CloseLogLocked()
        {
            try
            {
                _logWriter?.Dispose();
            }
            catch (IOException)
            {
            }
            _logWriter = null;
        }

        private void SetStateLocked(BackendState state)
        {
            if (_state == state)
            {
                return;
            }
            _state = state;
            Task.Run(() =>
            {
                try
                {
                    StateChanged?.Invoke(state);
                }
                catch (Exception error)
                {
                    _log.Error("VDGET state handler failed.", error);
                }
            });
        }

        private static bool HasExited(Process process)
        {
            try
            {
                return process.HasExited;
            }
            catch (InvalidOperationException)
            {
                return true;
            }
        }

        private static int FreePort()
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            int port = ((IPEndPoint)listener.LocalEndpoint).Port;
            listener.Stop();
            return port;
        }

        internal static JavaScriptSerializer NewSerializer() => new JavaScriptSerializer { MaxJsonLength = int.MaxValue };

        private static TaskCompletionSource<bool> NewReadySignal() =>
            new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
    }
}
