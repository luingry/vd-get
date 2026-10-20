using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Runtime.CompilerServices;
using System.Windows.Media;

namespace VdGet.Module.Downloads
{
    /// <summary>One download as shown on the page. Mutated on the UI thread only.</summary>
    internal sealed class DownloadItem : INotifyPropertyChanged
    {
        private string _status = Statuses.Pending;
        private double _progress;
        private string _speed = string.Empty;
        private string _eta = string.Empty;
        private string _downloaded = string.Empty;
        private string _total = string.Empty;
        private string _title = string.Empty;
        private string _thumb = string.Empty;
        private ImageSource _thumbnail;
        private string _error = string.Empty;
        private string _filePath = string.Empty;
        private double _finishedTs;

        public event PropertyChangedEventHandler PropertyChanged;

        public string Id { get; set; }

        public string Url { get; set; }

        public string Quality { get; set; }

        public string Destination { get; set; }

        public double CreatedTs { get; set; }

        public string Status
        {
            get => _status;
            set
            {
                if (Set(ref _status, value ?? Statuses.Pending))
                {
                    Notify(nameof(IsActive));
                    Notify(nameof(IsError));
                    Notify(nameof(IsDone));
                    Notify(nameof(IsIndeterminate));
                    Notify(nameof(CanOpenFile));
                    Notify(nameof(Detail));
                }
            }
        }

        public double Progress
        {
            get => _progress;
            set
            {
                if (Set(ref _progress, value))
                {
                    Notify(nameof(IsIndeterminate));
                }
            }
        }

        public string Speed { get => _speed; set { if (Set(ref _speed, value ?? string.Empty)) Notify(nameof(Detail)); } }

        public string Eta { get => _eta; set { if (Set(ref _eta, value ?? string.Empty)) Notify(nameof(Detail)); } }

        public string Downloaded { get => _downloaded; set { if (Set(ref _downloaded, value ?? string.Empty)) Notify(nameof(Detail)); } }

        public string Total { get => _total; set { if (Set(ref _total, value ?? string.Empty)) Notify(nameof(Detail)); } }

        public string Title { get => _title; set => Set(ref _title, value ?? string.Empty); }

        /// <summary>Thumbnail source from the backend: an http(s) URL or a data: URI.</summary>
        public string Thumb { get => _thumb; set => Set(ref _thumb, value ?? string.Empty); }

        public ImageSource Thumbnail { get => _thumbnail; set => Set(ref _thumbnail, value); }

        public string Error { get => _error; set { if (Set(ref _error, value ?? string.Empty)) Notify(nameof(Detail)); } }

        public string FilePath
        {
            get => _filePath;
            set
            {
                if (Set(ref _filePath, value ?? string.Empty))
                {
                    Notify(nameof(CanOpenFile));
                    Notify(nameof(Detail));
                }
            }
        }

        public double FinishedTs { get => _finishedTs; set { if (Set(ref _finishedTs, value)) Notify(nameof(Detail)); } }

        public bool IsActive => Statuses.IsActive(_status);

        public bool IsError => _status == Statuses.Error;

        public bool IsDone => _status == Statuses.Done;

        /// <summary>Analysing, processing, or downloading without a known size.</summary>
        public bool IsIndeterminate =>
            _status == Statuses.Pending || _status == Statuses.Analyzing || _status == Statuses.Processing ||
            (_status == Statuses.Downloading && _progress <= 0);

        public bool CanOpenFile => IsDone && !string.IsNullOrEmpty(_filePath);

        /// <summary>One caption line under the title.</summary>
        public string Detail
        {
            get
            {
                switch (_status)
                {
                    case Statuses.Pending:
                        return "Na fila";
                    case Statuses.Analyzing:
                        return string.IsNullOrEmpty(_speed) ? "Analisando a página" : "Analisando a página · " + _speed;
                    case Statuses.Processing:
                        return "Finalizando o arquivo";
                    case Statuses.Downloading:
                        return Join(_progress > 0 ? _progress.ToString("0.0", CultureInfo.GetCultureInfo("pt-BR")) + "%" : null,
                            SizeText(), Clean(_speed), string.IsNullOrEmpty(Clean(_eta)) ? null : "faltam " + Clean(_eta));
                    case Statuses.Error:
                        return string.IsNullOrEmpty(_error) ? "Falhou" : _error;
                    default:
                        return Join(FinishedText(), string.IsNullOrEmpty(_filePath) ? null : Path.GetFileName(_filePath));
                }
            }
        }

        private string SizeText()
        {
            string total = Clean(_total);
            string done = Clean(_downloaded);
            if (string.IsNullOrEmpty(total) || total == "?")
            {
                return string.IsNullOrEmpty(done) ? null : done;
            }
            return string.IsNullOrEmpty(done) ? total : done + " de " + total;
        }

        private string FinishedText()
        {
            if (_finishedTs <= 0)
            {
                return null;
            }
            DateTime when = DateTimeOffset.FromUnixTimeMilliseconds((long)(_finishedTs * 1000)).LocalDateTime;
            var culture = CultureInfo.GetCultureInfo("pt-BR");
            return when.Date == DateTime.Today ? "Hoje, " + when.ToString("HH:mm", culture) : when.ToString("dd/MM/yyyy HH:mm", culture);
        }

        private static string Clean(string value) =>
            string.IsNullOrWhiteSpace(value) || value.Trim() == "..." || value.Trim() == "Unknown" ? null : value.Trim();

        private static string Join(params string[] parts)
        {
            var kept = new List<string>();
            foreach (string part in parts)
            {
                if (!string.IsNullOrWhiteSpace(part))
                {
                    kept.Add(part);
                }
            }
            return string.Join(" · ", kept);
        }

        private bool Set<T>(ref T field, T value, [CallerMemberName] string name = null)
        {
            if (EqualityComparer<T>.Default.Equals(field, value))
            {
                return false;
            }
            field = value;
            Notify(name);
            return true;
        }

        private void Notify(string name) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
    }

    /// <summary>Status values used by servidor.py.</summary>
    internal static class Statuses
    {
        public const string Pending = "pendente";
        public const string Analyzing = "analisando";
        public const string Downloading = "baixando";
        public const string Processing = "processando";
        public const string Done = "concluido";
        public const string Error = "erro";

        public static bool IsActive(string status) =>
            status == Pending || status == Analyzing || status == Downloading || status == Processing;
    }
}
