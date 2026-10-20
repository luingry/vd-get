using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Threading;
using SwissTechKnife.Sdk;
using VdGet.Module.Backend;

namespace VdGet.Module.Downloads
{
    /// <summary>
    /// The page's model: the active and finished lists, the remembered settings, and the bridge to the backend.
    /// Lives as long as the module (downloads keep going while the user is on another page).
    /// Collections and items are touched on the UI thread only.
    /// </summary>
    internal sealed class DownloadService : IDisposable
    {
        private const int MaxHistory = 200;
        private const int MaxFolders = 10;

        private readonly IModuleContext _context;
        private readonly SettingsStore _store;
        private readonly Dictionary<string, DownloadItem> _byId = new Dictionary<string, DownloadItem>();
        private readonly DispatcherTimer _saveTimer;
        private bool _disposed;

        public DownloadService(IModuleContext context)
        {
            _context = context;
            Directory.CreateDirectory(context.DataDirectory);
            DefaultFolder = KnownFolders.Downloads();
            Backend = new BackendHost(
                Path.Combine(context.InstallDirectory, "backend"), context.DataDirectory, DefaultFolder, context.Log);
            Backend.MessageReceived += message => _context.Dispatcher.BeginInvoke(new Action(() => OnMessage(message)));
            Backend.StateChanged += state => _context.Dispatcher.BeginInvoke(new Action(OnBackendStateChanged));

            _store = new SettingsStore(context.DataDirectory);
            State = _store.Load();
            if (State.Folders.Count == 0)
            {
                State.Folders.Add(DefaultFolder);
            }
            if (string.IsNullOrEmpty(State.Folder))
            {
                State.Folder = State.Folders[0];
            }
            foreach (HistoryRecord record in State.History.OrderByDescending(r => r.FinishedTs))
            {
                DownloadItem item = FromRecord(record);
                _byId[item.Id] = item;
                Completed.Add(item);
            }
            SortCompleted();

            _saveTimer = new DispatcherTimer(TimeSpan.FromMilliseconds(600), DispatcherPriority.Background, (s, e) => SaveNow(), context.Dispatcher);
            _saveTimer.Stop();
        }

        public BackendHost Backend { get; }

        public SavedState State { get; }

        public string DefaultFolder { get; }

        public ObservableCollection<DownloadItem> Active { get; } = new ObservableCollection<DownloadItem>();

        public ObservableCollection<DownloadItem> Completed { get; } = new ObservableCollection<DownloadItem>();

        public event Action BackendStateChanged;

        public string LogPath => Path.Combine(_context.DataDirectory, "backend.log");

        /// <summary>Queues every non-empty line as a download. Returns false when the backend is unavailable.</summary>
        public async Task<bool> AddAsync(IEnumerable<string> urls, string quality, string folder)
        {
            List<string> list = urls.Select(u => u.Trim()).Where(u => u.Length > 0).Distinct().ToList();
            if (list.Count == 0)
            {
                return true;
            }
            RememberFolder(folder);
            State.Quality = Qualities.Normalize(quality);
            ScheduleSave();
            return await Backend.SendAsync(new Dictionary<string, object>
            {
                ["acao"] = "adicionar",
                ["urls"] = list,
                ["qualidade"] = State.Quality,
                ["destino"] = folder ?? string.Empty,
            }).ConfigureAwait(true);
        }

        public Task<bool> RetryAsync(DownloadItem item)
        {
            Remove(item);
            return AddAsync(new[] { item.Url }, item.Quality, string.IsNullOrEmpty(item.Destination) ? State.Folder : item.Destination);
        }

        /// <summary>Removes from the list (an active download keeps running in the backend until it ends).</summary>
        public void Remove(DownloadItem item)
        {
            Drop(item.Id);
            if (Backend.State == BackendState.Ready)
            {
                _ = Backend.SendAsync(new Dictionary<string, object> { ["acao"] = "remover", ["id"] = item.Id });
            }
        }

        public void ClearCompleted()
        {
            foreach (DownloadItem item in Completed.ToList())
            {
                _byId.Remove(item.Id);
            }
            Completed.Clear();
            ScheduleSave();
            if (Backend.State == BackendState.Ready)
            {
                _ = Backend.SendAsync(new Dictionary<string, object> { ["acao"] = "limpar_concluidos" });
            }
        }

        public void ToggleSort()
        {
            State.OldestFirst = !State.OldestFirst;
            SortCompleted();
            ScheduleSave();
        }

        public void RememberFolder(string folder)
        {
            if (string.IsNullOrWhiteSpace(folder))
            {
                return;
            }
            folder = folder.Trim();
            State.Folders.RemoveAll(f => string.Equals(f, folder, StringComparison.OrdinalIgnoreCase));
            State.Folders.Insert(0, folder);
            if (State.Folders.Count > MaxFolders)
            {
                State.Folders.RemoveRange(MaxFolders, State.Folders.Count - MaxFolders);
            }
            State.Folder = folder;
            ScheduleSave();
        }

        public void ForgetFolder(string folder)
        {
            State.Folders.RemoveAll(f => string.Equals(f, folder, StringComparison.OrdinalIgnoreCase));
            if (State.Folders.Count == 0)
            {
                State.Folders.Add(DefaultFolder);
            }
            if (string.Equals(State.Folder, folder, StringComparison.OrdinalIgnoreCase))
            {
                State.Folder = State.Folders[0];
            }
            ScheduleSave();
        }

        public void SetDraft(string text)
        {
            State.Draft = text ?? string.Empty;
            ScheduleSave();
        }

        /// <summary>Shows the file selected in Explorer, or the destination folder when the file is gone.</summary>
        public bool OpenFolder(DownloadItem item)
        {
            try
            {
                if (!string.IsNullOrEmpty(item.FilePath) && File.Exists(item.FilePath))
                {
                    Process.Start(new ProcessStartInfo("explorer.exe", "/select,\"" + item.FilePath + "\"") { UseShellExecute = false });
                    return true;
                }
                string folder = string.IsNullOrEmpty(item.Destination) ? DefaultFolder : item.Destination;
                if (Directory.Exists(folder))
                {
                    Process.Start(new ProcessStartInfo(folder) { UseShellExecute = true });
                    return true;
                }
            }
            catch (Exception error) when (error is System.ComponentModel.Win32Exception || error is InvalidOperationException)
            {
                _context.Log.Warn("Could not open the folder: " + error.Message);
            }
            return false;
        }

        public bool OpenFile(DownloadItem item)
        {
            if (string.IsNullOrEmpty(item.FilePath) || !File.Exists(item.FilePath))
            {
                return false;
            }
            try
            {
                Process.Start(new ProcessStartInfo(item.FilePath) { UseShellExecute = true });
                return true;
            }
            catch (Exception error) when (error is System.ComponentModel.Win32Exception || error is InvalidOperationException)
            {
                _context.Log.Warn("Could not open the file: " + error.Message);
                return false;
            }
        }

        public void OpenLog()
        {
            if (File.Exists(LogPath))
            {
                try
                {
                    Process.Start(new ProcessStartInfo(LogPath) { UseShellExecute = true });
                }
                catch (System.ComponentModel.Win32Exception)
                {
                }
            }
        }

        public void Dispose()
        {
            if (_disposed)
            {
                return;
            }
            _disposed = true;
            _saveTimer.Stop();
            SaveNow();
            Backend.Dispose();
        }

        private void OnMessage(Dictionary<string, object> message)
        {
            if (_disposed)
            {
                return;
            }
            switch (Str(message, "tipo"))
            {
                case "estado_inicial":
                    if (message.TryGetValue("downloads", out object list) && list is object[] downloads)
                    {
                        foreach (Dictionary<string, object> data in downloads.OfType<Dictionary<string, object>>())
                        {
                            Upsert(data);
                        }
                    }
                    break;
                case "adicionar":
                case "atualizar":
                    if (message.TryGetValue("download", out object raw) && raw is Dictionary<string, object> download)
                    {
                        Upsert(download);
                    }
                    break;
                case "remover":
                    Drop(Str(message, "id"));
                    break;
                case "limpar_concluidos":
                    foreach (DownloadItem item in Completed.Where(i => i.IsDone).ToList())
                    {
                        Drop(item.Id);
                    }
                    break;
            }
        }

        private void Upsert(Dictionary<string, object> data)
        {
            string id = Str(data, "id");
            if (string.IsNullOrEmpty(id))
            {
                return;
            }
            bool isNew = !_byId.TryGetValue(id, out DownloadItem item);
            if (isNew)
            {
                item = new DownloadItem
                {
                    Id = id,
                    Url = Str(data, "url"),
                    Quality = Str(data, "qualidade"),
                    Destination = Str(data, "destino"),
                    CreatedTs = Num(data, "criado_ts"),
                };
                _byId[id] = item;
            }

            bool wasActive = !isNew && item.IsActive;
            item.Status = Str(data, "status");
            item.Progress = Num(data, "progresso");
            item.Speed = Str(data, "velocidade");
            item.Eta = Str(data, "eta");
            item.Downloaded = Str(data, "baixado");
            item.Total = Str(data, "total");
            item.Title = FirstNonEmpty(Str(data, "titulo"), item.Url);
            item.Error = Str(data, "erro");
            item.FilePath = Str(data, "arquivo");
            item.FinishedTs = Num(data, "finalizado_ts");
            string thumb = Str(data, "thumb");
            if (thumb != item.Thumb)
            {
                item.Thumb = thumb;
                LoadThumbnail(item);
            }

            if (item.IsActive)
            {
                if (isNew || !Active.Contains(item))
                {
                    Completed.Remove(item);
                    Active.Insert(0, item);
                }
            }
            else if (isNew || wasActive || !Completed.Contains(item))
            {
                Active.Remove(item);
                InsertCompleted(item);
                ScheduleSave();
                if (wasActive)
                {
                    _context.Notify(item.IsDone ? "Download concluído: " + item.Title : "Download falhou: " + item.Title,
                        item.IsDone ? NotificationKind.Success : NotificationKind.Warning);
                }
            }
        }

        private void Drop(string id)
        {
            if (string.IsNullOrEmpty(id) || !_byId.TryGetValue(id, out DownloadItem item))
            {
                return;
            }
            _byId.Remove(id);
            Active.Remove(item);
            if (Completed.Remove(item))
            {
                ScheduleSave();
            }
        }

        /// <summary>The backend restarts with an empty queue: whatever was still active is lost.</summary>
        private void OnBackendStateChanged()
        {
            if (_disposed)
            {
                return;
            }
            BackendState state = Backend.State;
            if (state == BackendState.Stopped || state == BackendState.Failed || state == BackendState.NoPython)
            {
                foreach (DownloadItem item in Active.ToList())
                {
                    item.Status = Statuses.Error;
                    item.Error = "Interrompido: o VDGET foi encerrado antes de terminar.";
                    item.FinishedTs = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
                    Active.Remove(item);
                    InsertCompleted(item);
                }
                ScheduleSave();
            }
            BackendStateChanged?.Invoke();
        }

        private void LoadThumbnail(DownloadItem item)
        {
            string source = item.Thumb;
            ThumbnailLoader.LoadAsync(source).ContinueWith(task =>
            {
                if (task.Status == TaskStatus.RanToCompletion && task.Result != null)
                {
                    _context.Dispatcher.BeginInvoke(new Action(() =>
                    {
                        if (item.Thumb == source)
                        {
                            item.Thumbnail = task.Result;
                        }
                    }), DispatcherPriority.Background);
                }
            }, TaskScheduler.Default);
        }

        private void InsertCompleted(DownloadItem item)
        {
            int index = 0;
            while (index < Completed.Count && Before(Completed[index], item))
            {
                index++;
            }
            Completed.Insert(index, item);
        }

        private void SortCompleted()
        {
            List<DownloadItem> sorted = State.OldestFirst
                ? Completed.OrderBy(Key).ToList()
                : Completed.OrderByDescending(Key).ToList();
            for (int target = 0; target < sorted.Count; target++)
            {
                int current = Completed.IndexOf(sorted[target]);
                if (current != target)
                {
                    Completed.Move(current, target);
                }
            }
        }

        private bool Before(DownloadItem existing, DownloadItem item) =>
            State.OldestFirst ? Key(existing) <= Key(item) : Key(existing) >= Key(item);

        private static double Key(DownloadItem item) => item.FinishedTs > 0 ? item.FinishedTs : item.CreatedTs;

        private void ScheduleSave()
        {
            if (_disposed)
            {
                return;
            }
            _saveTimer.Stop();
            _saveTimer.Start();
        }

        private void SaveNow()
        {
            _saveTimer.Stop();
            var snapshot = new SavedState
            {
                Folders = State.Folders.ToList(),
                Folder = State.Folder,
                Quality = State.Quality,
                Draft = State.Draft,
                OldestFirst = State.OldestFirst,
                History = Completed.Take(MaxHistory).Select(ToRecord).ToList(),
            };
            if (_disposed)
            {
                _store.Save(snapshot);
            }
            else
            {
                Task.Run(() => _store.Save(snapshot));
            }
        }

        private static HistoryRecord ToRecord(DownloadItem item) => new HistoryRecord
        {
            Id = item.Id,
            Url = item.Url,
            Status = item.Status,
            Quality = item.Quality,
            Destination = item.Destination,
            Title = item.Title,
            Thumb = item.Thumb,
            Error = item.Error,
            FilePath = item.FilePath,
            CreatedTs = item.CreatedTs,
            FinishedTs = item.FinishedTs,
        };

        private DownloadItem FromRecord(HistoryRecord record)
        {
            var item = new DownloadItem
            {
                Id = string.IsNullOrEmpty(record.Id) ? Guid.NewGuid().ToString("N").Substring(0, 8) : record.Id,
                Url = record.Url ?? string.Empty,
                Quality = Qualities.Normalize(record.Quality),
                Destination = record.Destination ?? string.Empty,
                CreatedTs = record.CreatedTs,
                Status = record.Status == Statuses.Done ? Statuses.Done : Statuses.Error,
                Title = FirstNonEmpty(record.Title, record.Url),
                Thumb = record.Thumb ?? string.Empty,
                Error = record.Error ?? string.Empty,
                FilePath = record.FilePath ?? string.Empty,
                FinishedTs = record.FinishedTs,
                Progress = record.Status == Statuses.Done ? 100 : 0,
            };
            LoadThumbnail(item);
            return item;
        }

        private static string Str(Dictionary<string, object> data, string key) =>
            data.TryGetValue(key, out object value) && value != null ? Convert.ToString(value, CultureInfo.InvariantCulture) : string.Empty;

        private static double Num(Dictionary<string, object> data, string key)
        {
            if (!data.TryGetValue(key, out object value) || value == null)
            {
                return 0;
            }
            try
            {
                return Convert.ToDouble(value, CultureInfo.InvariantCulture);
            }
            catch (Exception error) when (error is FormatException || error is InvalidCastException || error is OverflowException)
            {
                return 0;
            }
        }

        private static string FirstNonEmpty(string a, string b) => string.IsNullOrWhiteSpace(a) ? b ?? string.Empty : a;
    }

    internal static class KnownFolders
    {
        private static readonly Guid DownloadsId = new Guid("374DE290-123F-4565-9164-39C4925E467B");

        /// <summary>The user's Downloads folder (follows a relocated folder), or %USERPROFILE%\Downloads.</summary>
        public static string Downloads()
        {
            try
            {
                if (SHGetKnownFolderPath(DownloadsId, 0, IntPtr.Zero, out IntPtr path) == 0)
                {
                    try
                    {
                        return Marshal.PtrToStringUni(path);
                    }
                    finally
                    {
                        Marshal.FreeCoTaskMem(path);
                    }
                }
            }
            catch (EntryPointNotFoundException)
            {
            }
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Downloads");
        }

        [DllImport("shell32.dll", PreserveSig = true)]
        private static extern int SHGetKnownFolderPath([MarshalAs(UnmanagedType.LPStruct)] Guid id, uint flags, IntPtr token, out IntPtr path);
    }
}
