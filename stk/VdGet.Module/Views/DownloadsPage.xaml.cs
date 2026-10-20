using System;
using System.Collections.Specialized;
using System.Linq;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media.Animation;
using System.Windows.Threading;
using SwissTechKnife.Sdk;
using VdGet.Module.Backend;
using VdGet.Module.Downloads;
using VdGet.Module.Native;

namespace VdGet.Module.Views
{
    public partial class DownloadsPage : IModuleViewLifecycle
    {
        private static readonly char[] LineBreaks = { '\r', '\n' };

        private readonly DownloadService _service;
        private readonly IModuleContext _context;
        private readonly DispatcherTimer _clearConfirm;
        private bool _loading = true;
        private BackendState? _shownState;

        internal DownloadsPage(DownloadService service, IModuleContext context)
        {
            _service = service;
            _context = context;
            InitializeComponent();

            UrlInput.Text = service.State.Draft;
            switch (service.State.Quality)
            {
                case Qualities.Smallest: SmallestOption.IsChecked = true; break;
                case Qualities.Audio: AudioOption.IsChecked = true; break;
                default: BestOption.IsChecked = true; break;
            }
            RefreshFolders();

            ActiveList.ItemsSource = service.Active;
            CompletedList.ItemsSource = service.Completed;
            service.Active.CollectionChanged += OnListsChanged;
            service.Completed.CollectionChanged += OnListsChanged;
            service.BackendStateChanged += ApplyBackendState;

            _clearConfirm = new DispatcherTimer(TimeSpan.FromSeconds(3), DispatcherPriority.Normal, (s, e) => ResetClear(), Dispatcher);
            _clearConfirm.Stop();

            UpdateLists();
            UpdateSortButton();
            ApplyBackendState();
            _loading = false;
        }

        public void OnViewShown() => _service.Backend.EnsureStarted(); // warm up: Python and yt-dlp take a moment to load

        public void OnViewHidden()
        {
        }

        private async void OnAdd(object sender, RoutedEventArgs e)
        {
            string[] urls = UrlInput.Text.Split(LineBreaks, StringSplitOptions.RemoveEmptyEntries);
            if (urls.All(string.IsNullOrWhiteSpace))
            {
                UrlInput.Focus();
                return;
            }

            AddButton.IsEnabled = false;
            try
            {
                string folder = FolderBox.SelectedItem as string ?? _service.DefaultFolder;
                bool sent = await _service.AddAsync(urls, SelectedQuality(), folder);
                if (sent)
                {
                    UrlInput.Clear();
                }
                else if (_service.Backend.State != BackendState.NoPython && _service.Backend.State != BackendState.Failed)
                {
                    _context.Notify("O VDGET não respondeu. Tente de novo.", NotificationKind.Warning);
                }
                RefreshFolders();
            }
            finally
            {
                AddButton.IsEnabled = true;
            }
        }

        private void OnUrlsKeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key == Key.Enter && (Keyboard.Modifiers & ModifierKeys.Control) != 0)
            {
                e.Handled = true;
                OnAdd(sender, e);
            }
        }

        private void OnUrlsChanged(object sender, TextChangedEventArgs e)
        {
            if (!_loading)
            {
                _service.SetDraft(UrlInput.Text);
            }
        }

        private void OnQualityChanged(object sender, RoutedEventArgs e)
        {
            if (!_loading)
            {
                _service.State.Quality = SelectedQuality();
            }
        }

        private void OnFolderChanged(object sender, SelectionChangedEventArgs e)
        {
            if (!_loading && FolderBox.SelectedItem is string folder)
            {
                _service.State.Folder = folder;
            }
        }

        private void OnPickFolder(object sender, RoutedEventArgs e)
        {
            string folder;
            try
            {
                folder = FolderPicker.Pick(Window.GetWindow(this), "Pasta de destino dos downloads", FolderBox.SelectedItem as string);
            }
            catch (Exception error) when (error is System.Runtime.InteropServices.COMException || error is InvalidCastException)
            {
                _context.Log.Error("Folder picker failed.", error);
                return;
            }
            if (folder != null)
            {
                _service.RememberFolder(folder);
                RefreshFolders();
            }
        }

        private void OnDragOver(object sender, DragEventArgs e)
        {
            e.Effects = e.Data.GetDataPresent(DataFormats.UnicodeText) || e.Data.GetDataPresent(DataFormats.Text)
                ? DragDropEffects.Copy
                : DragDropEffects.None;
            e.Handled = true;
        }

        /// <summary>Links dragged from a browser are appended to the list.</summary>
        private void OnDrop(object sender, DragEventArgs e)
        {
            string text = e.Data.GetData(DataFormats.UnicodeText) as string ?? e.Data.GetData(DataFormats.Text) as string;
            if (string.IsNullOrWhiteSpace(text))
            {
                return;
            }
            string current = UrlInput.Text.TrimEnd();
            UrlInput.Text = current.Length == 0 ? text.Trim() : current + Environment.NewLine + text.Trim();
            UrlInput.CaretIndex = UrlInput.Text.Length;
            UrlInput.Focus();
            e.Handled = true;
        }

        private void OnRemove(object sender, RoutedEventArgs e)
        {
            if (Item(sender) is DownloadItem item)
            {
                _service.Remove(item);
            }
        }

        private async void OnRetry(object sender, RoutedEventArgs e)
        {
            if (Item(sender) is DownloadItem item)
            {
                await _service.RetryAsync(item);
            }
        }

        private void OnOpenFile(object sender, RoutedEventArgs e)
        {
            if (Item(sender) is DownloadItem item && !_service.OpenFile(item))
            {
                _context.Notify("O arquivo não está mais nesse local.", NotificationKind.Warning);
            }
        }

        private void OnOpenFolder(object sender, RoutedEventArgs e)
        {
            if (Item(sender) is DownloadItem item && !_service.OpenFolder(item))
            {
                _context.Notify("A pasta não existe mais.", NotificationKind.Warning);
            }
        }

        private void OnToggleSort(object sender, RoutedEventArgs e)
        {
            _service.ToggleSort();
            UpdateSortButton();
        }

        /// <summary>Clearing the history takes a second click.</summary>
        private void OnClear(object sender, RoutedEventArgs e)
        {
            if (!_clearConfirm.IsEnabled)
            {
                ClearButton.Content = "Confirmar limpeza";
                ClearButton.SetResourceReference(StyleProperty, "Button.Danger");
                _clearConfirm.Start();
                return;
            }
            ResetClear();
            _service.ClearCompleted();
        }

        private void ResetClear()
        {
            _clearConfirm.Stop();
            ClearButton.Content = "Limpar tudo";
            ClearButton.SetResourceReference(StyleProperty, "Button.Ghost");
        }

        private void OnOpenLog(object sender, RoutedEventArgs e) => _service.OpenLog();

        private void OnRetryBackend(object sender, RoutedEventArgs e) => _service.Backend.EnsureStarted();

        private void OnListsChanged(object sender, NotifyCollectionChangedEventArgs e) => UpdateLists();

        private void UpdateLists()
        {
            ActiveCount.Text = _service.Active.Count.ToString();
            CompletedCount.Text = _service.Completed.Count.ToString();
            SetShown(ActiveEmpty, _service.Active.Count == 0);
            SetShown(CompletedEmpty, _service.Completed.Count == 0);
            CompletedTools.Visibility = _service.Completed.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        }

        private void UpdateSortButton()
        {
            bool oldest = _service.State.OldestFirst;
            Ui.SetIcon(SortButton, oldest ? "" : "");
            SortButton.ToolTip = oldest ? "Mais antigos primeiro (clique para inverter)" : "Mais recentes primeiro (clique para inverter)";
        }

        private void RefreshFolders()
        {
            bool loading = _loading;
            _loading = true;
            FolderBox.ItemsSource = _service.State.Folders.ToList();
            FolderBox.SelectedItem = _service.State.Folders.FirstOrDefault(f => string.Equals(f, _service.State.Folder, StringComparison.OrdinalIgnoreCase))
                                     ?? _service.State.Folders.FirstOrDefault();
            _loading = loading;
        }

        private void ApplyBackendState()
        {
            BackendState state = _service.Backend.State;
            if (_shownState == state)
            {
                return;
            }
            bool first = _shownState == null;
            _shownState = state;

            string label, soft, strong;
            switch (state)
            {
                case BackendState.Ready:
                    label = "Pronto"; soft = "Brush.Success.Soft"; strong = "Brush.Success";
                    break;
                case BackendState.Starting:
                    label = "Iniciando"; soft = "Brush.Info.Soft"; strong = "Brush.Info";
                    break;
                case BackendState.NoPython:
                    label = "Python ausente"; soft = "Brush.Danger.Soft"; strong = "Brush.Danger";
                    break;
                case BackendState.Failed:
                    label = "Com erro"; soft = "Brush.Danger.Soft"; strong = "Brush.Danger";
                    break;
                default:
                    label = "Em espera"; soft = "Brush.Surface4"; strong = "Brush.Text.Secondary";
                    break;
            }
            StatusText.Text = label;
            StatusChip.SetResourceReference(BackgroundProperty, soft);
            StatusText.SetResourceReference(ForegroundProperty, strong);
            StatusChip.ToolTip = state == BackendState.Stopped
                ? "O VDGET inicia sozinho no próximo download e se fecha após alguns minutos sem uso."
                : null;

            if (state == BackendState.NoPython)
            {
                ProblemTitle.Text = "O VDGET precisa do Python 3";
                ProblemText.Text = "Instale o Python 3 em python.org (marque \"Add python.exe to PATH\") e tente de novo. As dependências (yt-dlp) são instaladas sozinhas no primeiro uso.";
                LogButton.Visibility = Visibility.Collapsed;
                SetShown(Problem, true);
            }
            else if (state == BackendState.Failed)
            {
                ProblemTitle.Text = "O VDGET parou com um erro";
                ProblemText.Text = LastLine(_service.Backend.LastError);
                LogButton.Visibility = Visibility.Visible;
                SetShown(Problem, true);
            }
            else
            {
                SetShown(Problem, false);
            }

            if (!first)
            {
                Fade(StatusChip);
            }
        }

        private static string LastLine(string text)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return "Veja o log para detalhes.";
            }
            string[] lines = text.Split(LineBreaks, StringSplitOptions.RemoveEmptyEntries);
            return lines.Length == 0 ? text : lines[lines.Length - 1].Trim();
        }

        private string SelectedQuality() =>
            SmallestOption.IsChecked == true ? Qualities.Smallest : AudioOption.IsChecked == true ? Qualities.Audio : Qualities.Best;

        private static DownloadItem Item(object sender) => (sender as FrameworkElement)?.DataContext as DownloadItem;

        /// <summary>Shows or hides with a fade instead of a jump.</summary>
        private void SetShown(UIElement element, bool shown)
        {
            bool visible = element.Visibility == Visibility.Visible;
            if (visible == shown)
            {
                return;
            }
            element.Visibility = shown ? Visibility.Visible : Visibility.Collapsed;
            if (shown && !_loading)
            {
                Fade(element);
            }
        }

        private void Fade(UIElement element)
        {
            var duration = TryFindResource("Motion.Normal") is Duration d ? d : new Duration(TimeSpan.FromMilliseconds(200));
            var fade = new DoubleAnimation(0, 1, duration) { EasingFunction = TryFindResource("Ease.Out") as IEasingFunction };
            fade.Freeze();
            element.BeginAnimation(OpacityProperty, fade);
        }
    }
}
