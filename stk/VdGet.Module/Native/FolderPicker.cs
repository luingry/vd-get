using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace VdGet.Module.Native
{
    /// <summary>The modern Windows folder picker (IFileOpenDialog with FOS_PICKFOLDERS).</summary>
    internal static class FolderPicker
    {
        private const uint FosPickFolders = 0x20;
        private const uint FosForceFileSystem = 0x40;
        private const uint SigdnFileSysPath = 0x80058000;
        private const int ErrorCancelled = unchecked((int)0x800704C7);

        /// <summary>Returns the chosen folder, or null when the user cancels.</summary>
        public static string Pick(Window owner, string title, string initialFolder)
        {
            var dialog = (IFileOpenDialog)new FileOpenDialogRcw();
            try
            {
                dialog.GetOptions(out uint options);
                dialog.SetOptions(options | FosPickFolders | FosForceFileSystem);
                dialog.SetTitle(title);
                if (!string.IsNullOrEmpty(initialFolder) &&
                    SHCreateItemFromParsingName(initialFolder, IntPtr.Zero, typeof(IShellItem).GUID, out IShellItem folder) == 0)
                {
                    dialog.SetFolder(folder);
                    Marshal.ReleaseComObject(folder);
                }

                IntPtr hwnd = owner != null ? new WindowInteropHelper(owner).Handle : IntPtr.Zero;
                int hr = dialog.Show(hwnd);
                if (hr == ErrorCancelled)
                {
                    return null;
                }
                Marshal.ThrowExceptionForHR(hr);

                dialog.GetResult(out IShellItem item);
                try
                {
                    item.GetDisplayName(SigdnFileSysPath, out IntPtr path);
                    try
                    {
                        return Marshal.PtrToStringUni(path);
                    }
                    finally
                    {
                        Marshal.FreeCoTaskMem(path);
                    }
                }
                finally
                {
                    Marshal.ReleaseComObject(item);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(dialog);
            }
        }

        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
        private static extern int SHCreateItemFromParsingName(
            string path, IntPtr bindContext, [MarshalAs(UnmanagedType.LPStruct)] Guid riid, out IShellItem item);

        [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
        private class FileOpenDialogRcw
        {
        }

        [ComImport, Guid("d57c7288-d4ad-4768-be02-9d969532d960"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IFileOpenDialog
        {
            [PreserveSig] int Show(IntPtr parent);
            void SetFileTypes(uint count, IntPtr filterSpec);
            void SetFileTypeIndex(uint index);
            void GetFileTypeIndex(out uint index);
            void Advise(IntPtr events, out uint cookie);
            void Unadvise(uint cookie);
            void SetOptions(uint options);
            void GetOptions(out uint options);
            void SetDefaultFolder(IShellItem item);
            void SetFolder(IShellItem item);
            void GetFolder(out IShellItem item);
            void GetCurrentSelection(out IShellItem item);
            void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
            void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string name);
            void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
            void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
            void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
            void GetResult(out IShellItem item);
        }

        [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IShellItem
        {
            void BindToHandler(IntPtr bindContext, ref Guid handler, ref Guid riid, out IntPtr result);
            void GetParent(out IShellItem parent);
            void GetDisplayName(uint sigdn, out IntPtr name);
            void GetAttributes(uint mask, out uint attributes);
            void Compare(IShellItem other, uint hint, out int order);
        }
    }
}
