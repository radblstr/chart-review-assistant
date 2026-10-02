// Native launcher for the Chart Review Assistant (no console window).
// Sets the per-user data dir + desktop mode, then starts pythonw from the bundled runtime in
// <app>\python. Compiled at build time (csc /target:winexe) so the shortcuts point at a normal
// ChartReviewAssistant.exe carrying the app icon -- no .vbs, no console flash.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

class Launcher
{
    // user32 MessageBox via P/Invoke so the winexe needs no System.Windows.Forms reference.
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    const uint MB_ICONERROR = 0x10;

    static void Fail(string what)
    {
        MessageBoxW(IntPtr.Zero, what + "\n\nRe-run the installer to repair the app.",
            "Physics Chart Review Assistant", MB_ICONERROR);
        Environment.Exit(1);
    }

    static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        Environment.SetEnvironmentVariable("CRA_DATA_DIR", Path.Combine(root, "data"));
        Environment.SetEnvironmentVariable("CRA_MODE", "desktop");

        // Same source is compiled as ChartReviewAssistant-Demo.exe; that name runs the bundled demo.
        if (AppDomain.CurrentDomain.FriendlyName.EndsWith("-Demo.exe",
                StringComparison.OrdinalIgnoreCase))
            Environment.SetEnvironmentVariable("CRA_DEMO_MODE", "1");

        string pythonw = Path.Combine(root, @"python\pythonw.exe");
        if (!File.Exists(pythonw))
            Fail("The app's Python runtime is missing: " + pythonw);

        ProcessStartInfo app = new ProcessStartInfo();
        app.FileName = pythonw;
        app.Arguments = "-m chart_review_assistant.app";
        app.WorkingDirectory = root;
        app.UseShellExecute = false;
        app.CreateNoWindow = true;
        try
        {
            Process.Start(app);
        }
        catch (Exception e)
        {
            Fail("Could not start the app: " + e.Message);
        }
    }
}
