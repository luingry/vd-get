using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Threading.Tasks;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace VdGet.Module.Downloads
{
    /// <summary>Fetches and decodes thumbnails off the UI thread into small frozen bitmaps.</summary>
    internal static class ThumbnailLoader
    {
        private const int DecodeWidth = 192;
        private const int MaxCached = 300;

        private static readonly Dictionary<string, Task<ImageSource>> Cache = new Dictionary<string, Task<ImageSource>>();
        private static readonly Lazy<HttpClient> Http = new Lazy<HttpClient>(() =>
        {
            var client = new HttpClient { Timeout = TimeSpan.FromSeconds(20) };
            client.DefaultRequestHeaders.UserAgent.ParseAdd(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36");
            return client;
        });

        /// <summary>Null when the source is empty or cannot be decoded.</summary>
        public static Task<ImageSource> LoadAsync(string source)
        {
            if (string.IsNullOrWhiteSpace(source))
            {
                return Task.FromResult<ImageSource>(null);
            }
            lock (Cache)
            {
                if (Cache.TryGetValue(source, out Task<ImageSource> cached))
                {
                    return cached;
                }
                if (Cache.Count >= MaxCached)
                {
                    Cache.Clear();
                }
                Task<ImageSource> load = Task.Run(() => FetchAsync(source));
                Cache[source] = load;
                return load;
            }
        }

        private static async Task<ImageSource> FetchAsync(string source)
        {
            try
            {
                byte[] bytes;
                if (source.StartsWith("data:", StringComparison.OrdinalIgnoreCase))
                {
                    int comma = source.IndexOf(',');
                    if (comma < 0 || source.IndexOf(";base64", 0, comma, StringComparison.OrdinalIgnoreCase) < 0)
                    {
                        return null;
                    }
                    bytes = Convert.FromBase64String(source.Substring(comma + 1));
                }
                else if (source.StartsWith("http://", StringComparison.OrdinalIgnoreCase) || source.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
                {
                    bytes = await Http.Value.GetByteArrayAsync(source).ConfigureAwait(false);
                }
                else
                {
                    return null;
                }

                var bitmap = new BitmapImage();
                bitmap.BeginInit();
                bitmap.CacheOption = BitmapCacheOption.OnLoad;
                bitmap.CreateOptions = BitmapCreateOptions.IgnoreColorProfile;
                bitmap.DecodePixelWidth = DecodeWidth;
                bitmap.StreamSource = new MemoryStream(bytes);
                bitmap.EndInit();
                bitmap.Freeze();
                return bitmap;
            }
            catch (Exception error) when (error is HttpRequestException || error is TaskCanceledException || error is FormatException ||
                                          error is NotSupportedException || error is IOException || error is ArgumentException ||
                                          error is InvalidOperationException || error is System.Runtime.InteropServices.COMException)
            {
                // No codec (e.g. WebP without the extension), offline or expired URL: the placeholder stays.
                return null;
            }
        }
    }
}
