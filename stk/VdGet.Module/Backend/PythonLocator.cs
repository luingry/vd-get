using System;
using System.Diagnostics;
using System.IO;

namespace VdGet.Module.Backend
{
    /// <summary>
    /// Finds a real python.exe (3.x). The backend runs that interpreter directly, never through py.exe,
    /// so the job object owns the actual process and killing it stops the downloads.
    /// </summary>
    internal static class PythonLocator
    {
        private const int ProbeTimeoutMs = 10000;

        /// <summary>Full path of python.exe, or null when no usable Python 3 is installed.</summary>
        public static string Find()
        {
            // The py launcher knows every registered install, independent of PATH order.
            string viaLauncher = Probe("py.exe", "-3 -c \"import sys; print(sys.executable)\"");
            if (IsUsable(viaLauncher))
            {
                return viaLauncher;
            }

            foreach (string directory in (Environment.GetEnvironmentVariable("PATH") ?? string.Empty).Split(Path.PathSeparator))
            {
                string candidate;
                try
                {
                    candidate = Path.Combine(directory.Trim().Trim('"'), "python.exe");
                }
                catch (ArgumentException)
                {
                    continue;
                }
                // WindowsApps holds the Store alias, which opens the Microsoft Store instead of running Python.
                if (candidate.IndexOf(@"\WindowsApps\", StringComparison.OrdinalIgnoreCase) >= 0 || !File.Exists(candidate))
                {
                    continue;
                }
                string version = Probe(candidate, "-c \"import sys; print(sys.version_info[0])\"");
                if (version == "3")
                {
                    return candidate;
                }
            }
            return null;
        }

        private static bool IsUsable(string path) =>
            !string.IsNullOrEmpty(path) && path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) && File.Exists(path);

        private static string Probe(string fileName, string arguments)
        {
            try
            {
                using (var process = Process.Start(new ProcessStartInfo(fileName, arguments)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                }))
                {
                    if (process == null)
                    {
                        return null;
                    }
                    process.ErrorDataReceived += (s, e) => { };
                    process.BeginErrorReadLine();
                    string output = process.StandardOutput.ReadToEnd();
                    if (!process.WaitForExit(ProbeTimeoutMs))
                    {
                        try { process.Kill(); } catch (InvalidOperationException) { }
                        return null;
                    }
                    return process.ExitCode == 0 ? output.Trim() : null;
                }
            }
            catch (Exception error) when (error is System.ComponentModel.Win32Exception || error is InvalidOperationException)
            {
                return null;
            }
        }
    }
}
