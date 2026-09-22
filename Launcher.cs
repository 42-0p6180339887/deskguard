using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
internal static class Launcher {
    [STAThread]
    public static int Main(string[] args) {
        try {
            string folder = AppDomain.CurrentDomain.BaseDirectory;
            string runtime = Path.Combine(folder, "_runtime");
            var info = new ProcessStartInfo(Path.Combine(runtime, "pythonw.exe"));
            info.Arguments = "-s \"" + Path.Combine(folder, "_app", "app.py") + "\"";
            bool selfTest = args.Length == 2 && args[0] == "--self-test";
            if (selfTest) {
                string resultPath = Path.GetFullPath(args[1]);
                if (resultPath.Contains("\"")) throw new ArgumentException("Invalid output path.");
                info.Arguments += " --self-test \"" + resultPath + "\"";
            }
            info.WorkingDirectory = folder;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.EnvironmentVariables.Remove("PYTHONPATH");
            info.EnvironmentVariables["PYTHONHOME"] = runtime;
            info.EnvironmentVariables["PYTHONUTF8"] = "1";
            info.EnvironmentVariables["TCL_LIBRARY"] = Path.Combine(runtime, "tcl", "tcl8.6");
            info.EnvironmentVariables["TK_LIBRARY"] = Path.Combine(runtime, "tcl", "tk8.6");
            using (Process child = Process.Start(info)) {
                if (selfTest) { child.WaitForExit(); return child.ExitCode; }
            }
            return 0;
        } catch (Exception ex) {
            MessageBox.Show("无法启动离席守护。请完整解压整个程序文件夹，再重试。\n\n" + ex.Message,
                            "离席守护", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
