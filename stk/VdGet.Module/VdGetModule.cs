using System.Windows;
using SwissTechKnife.Sdk;
using VdGet.Module.Downloads;
using VdGet.Module.Views;

namespace VdGet.Module
{
    /// <summary>
    /// SwissTechKnife entry point. The page replaces the browser UI; servidor.py runs hidden as a child
    /// process, started when the page opens or a download is added, and stopped with the module.
    /// </summary>
    public sealed class VdGetModule : IModule
    {
        private IModuleContext _context;
        private DownloadService _service;

        public void Initialize(IModuleContext context) => _context = context;

        public FrameworkElement CreateView()
        {
            _service = _service ?? new DownloadService(_context);
            return new DownloadsPage(_service, _context);
        }

        public void Dispose()
        {
            _service?.Dispose();
            _service = null;
        }
    }
}
