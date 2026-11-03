using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class LauncherForm : Form
{
    private const string MutexName = "Local\\NcmWasapiExtension.SingleInstance";
    private const string ShutdownEventName = "Local\\NcmWasapiExtension.Shutdown";
    private readonly string directory = AppDomain.CurrentDomain.BaseDirectory;
    private readonly string dataDirectory = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "NcmWasapiExtension");
    private readonly object processLock = new object();
    private readonly object logLock = new object();
    private readonly Label statusLabel = new Label();
    private readonly Label clientLabel = new Label();
    private readonly Button browseButton = new Button();
    private readonly Button retryButton = new Button();
    private readonly Button diagnosticsButton = new Button();
    private readonly Button closeButton = new Button();
    private readonly NotifyIcon tray = new NotifyIcon();
    private readonly ToolStripMenuItem exitItem = new ToolStripMenuItem("关闭扩展并退出");
    private readonly ManualResetEvent controllerExited = new ManualResetEvent(true);
    private readonly EventWaitHandle shutdownSignal;
    private RegisteredWaitHandle shutdownWait;
    private Process controller;
    private Process checker;
    private bool controllerActive;
    private bool checking;
    private volatile bool closingExtension;
    private volatile bool disposed;
    private bool allowClose;
    private bool failureShown;
    private bool hasStatusError;
    private string selectedClient;
    private string clientVersion = "未检测";
    private string currentState = "starting";
    private string lastWarningMessage;

    [STAThread]
    private static int Main(string[] args)
    {
        if (HasArgument(args, "--shutdown")) return ShutdownExisting();

        using (Mutex instance = new Mutex(false, MutexName))
        {
            bool acquired;
            try { acquired = instance.WaitOne(0, false); }
            catch (AbandonedMutexException) { acquired = true; }
            if (!acquired)
            {
                MessageBox.Show("扩展已在运行。可在任务栏通知区域打开“网易云输出扩展”。",
                    "网易云输出扩展", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 0;
            }
            try
            {
                using (EventWaitHandle signal = new EventWaitHandle(
                    false, EventResetMode.AutoReset, ShutdownEventName))
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);
                    Application.Run(new LauncherForm(signal, ArgumentValue(args, "--client")));
                }
                return 0;
            }
            finally { instance.ReleaseMutex(); }
        }
    }

    private static bool HasArgument(string[] args, string name)
    {
        foreach (string argument in args)
            if (String.Equals(argument, name, StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    private static string ArgumentValue(string[] args, string name)
    {
        for (int i = 0; i + 1 < args.Length; i++)
            if (String.Equals(args[i], name, StringComparison.OrdinalIgnoreCase)) return args[i + 1];
        return null;
    }

    private static int ShutdownExisting()
    {
        Mutex instance;
        try { instance = Mutex.OpenExisting(MutexName); }
        catch (WaitHandleCannotBeOpenedException) { return 0; }
        catch (UnauthorizedAccessException) { return 1; }
        using (instance)
        {
            Stopwatch elapsed = Stopwatch.StartNew();
            EventWaitHandle signal = null;
            try
            {
                while (elapsed.ElapsedMilliseconds < 30000)
                {
                    bool exited;
                    try { exited = instance.WaitOne(0, false); }
                    catch (AbandonedMutexException) { exited = true; }
                    if (exited)
                    {
                        instance.ReleaseMutex();
                        return 0;
                    }
                    try { signal = EventWaitHandle.OpenExisting(ShutdownEventName); }
                    catch (WaitHandleCannotBeOpenedException) { }
                    catch (UnauthorizedAccessException) { return 1; }
                    if (signal != null)
                    {
                        signal.Set();
                        int remaining = Math.Max(0, 30000 - (int)elapsed.ElapsedMilliseconds);
                        try { exited = instance.WaitOne(remaining, false); }
                        catch (AbandonedMutexException) { exited = true; }
                        if (exited)
                        {
                            instance.ReleaseMutex();
                            return 0;
                        }
                        return 1;
                    }
                    Thread.Sleep(100);
                }
                return 1;
            }
            finally { if (signal != null) signal.Dispose(); }
        }
    }

    private LauncherForm(EventWaitHandle signal, string client)
    {
        shutdownSignal = signal;
        selectedClient = client;
        Text = "网易云输出扩展";
        Font = new Font("Microsoft YaHei UI", 9F);
        ClientSize = new Size(540, 384);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Dpi;
        BackColor = Color.FromArgb(248, 249, 251);
        ShowInTaskbar = false;
        Opacity = 0;

        Label title = new Label();
        title.Text = "网易云输出扩展";
        title.Font = new Font(Font.FontFamily, 18F, FontStyle.Bold);
        title.SetBounds(25, 20, 490, 38);
        Controls.Add(title);

        Label explanation = new Label();
        explanation.Text = "在网易云的输出设备中选择共享或独占，照常调节音量。\n优先使用歌曲采样率，必要时选择设备支持的采样率。";
        explanation.SetBounds(27, 74, 486, 48);
        Controls.Add(explanation);

        statusLabel.Text = "正在查找网易云音乐并检查是否兼容……";
        statusLabel.ForeColor = Color.FromArgb(74, 82, 94);
        statusLabel.SetBounds(27, 137, 486, 71);
        Controls.Add(statusLabel);

        clientLabel.Text = "网易云位置：自动查找";
        clientLabel.ForeColor = Color.FromArgb(95, 104, 116);
        clientLabel.SetBounds(27, 218, 486, 30);
        Controls.Add(clientLabel);

        browseButton.Text = "选择网易云…";
        browseButton.SetBounds(27, 257, 150, 34);
        browseButton.Click += delegate { BrowseClient(); };
        Controls.Add(browseButton);

        retryButton.Text = "重新检测并启动";
        retryButton.SetBounds(188, 257, 163, 34);
        retryButton.Click += delegate { StartPreflight(); };
        Controls.Add(retryButton);

        diagnosticsButton.Text = "复制诊断信息";
        diagnosticsButton.SetBounds(362, 257, 151, 34);
        diagnosticsButton.Click += delegate { CopyDiagnostics(); };
        Controls.Add(diagnosticsButton);

        closeButton.Text = "关闭扩展，恢复普通网易云";
        closeButton.SetBounds(27, 316, 486, 37);
        closeButton.Click += delegate { RequestCloseExtension(); };
        Controls.Add(closeButton);

        ContextMenuStrip menu = new ContextMenuStrip();
        ToolStripMenuItem statusItem = new ToolStripMenuItem("查看状态");
        statusItem.Click += delegate { ShowStatus(); };
        menu.Items.Add(statusItem);
        ToolStripMenuItem retryItem = new ToolStripMenuItem("重新检测并启动");
        retryItem.Click += delegate { ShowStatus(); StartPreflight(); };
        menu.Items.Add(retryItem);
        ToolStripMenuItem diagnosticsItem = new ToolStripMenuItem("复制诊断信息");
        diagnosticsItem.Click += delegate { CopyDiagnostics(); };
        menu.Items.Add(diagnosticsItem);
        menu.Items.Add(new ToolStripSeparator());
        exitItem.Click += delegate { RequestCloseExtension(); };
        menu.Items.Add(exitItem);
        tray.Icon = SystemIcons.Application;
        tray.Text = "网易云输出扩展 · 正在检测";
        tray.ContextMenuStrip = menu;
        tray.DoubleClick += delegate { ShowStatus(); };
        tray.Visible = true;

        Shown += delegate
        {
            Hide();
            Opacity = 1;
            shutdownWait = ThreadPool.RegisterWaitForSingleObject(shutdownSignal,
                delegate(object state, bool timedOut) { OnUI(RequestCloseExtension); },
                null, Timeout.Infinite, false);
            BeginInvoke((Action)StartPreflight);
        };
        FormClosing += OnClosing;
        FormClosed += delegate
        {
            disposed = true;
            if (shutdownWait != null) shutdownWait.Unregister(null);
            tray.Visible = false;
            tray.Dispose();
            menu.Dispose();
            controllerExited.Dispose();
        };
    }

    private void ShowStatus()
    {
        ShowInTaskbar = true;
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
    }

    private void Log(string line)
    {
        try
        {
            lock (logLock)
            {
                string folder = Path.Combine(dataDirectory, "logs");
                Directory.CreateDirectory(folder);
                File.AppendAllText(Path.Combine(folder, "launcher.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + line +
                    Environment.NewLine, Encoding.UTF8);
            }
        }
        catch { }
    }

    private void OnUI(Action action)
    {
        if (disposed || IsDisposed) return;
        try
        {
            if (InvokeRequired) BeginInvoke(action);
            else action();
        }
        catch (InvalidOperationException) { }
    }

    private void UpdateButtons()
    {
        browseButton.Enabled = !checking && !controllerActive && !closingExtension;
        retryButton.Enabled = !checking && !controllerActive && !closingExtension;
        closeButton.Enabled = !closingExtension;
        exitItem.Enabled = !closingExtension;
    }

    private void ShowFailure(string message)
    {
        hasStatusError = true;
        currentState = "error";
        statusLabel.Text = message;
        statusLabel.ForeColor = Color.FromArgb(166, 51, 51);
        tray.Text = "网易云输出扩展 · 需要处理";
        UpdateButtons();
        if (!failureShown)
        {
            failureShown = true;
            ShowStatus();
        }
    }

    private static string QuoteArgument(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"')
            {
                result.Append('\\', slashes * 2 + 1);
                result.Append('"');
            }
            else
            {
                result.Append('\\', slashes);
                result.Append(character);
            }
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    private ProcessStartInfo StartInfo(string action)
    {
        ProcessStartInfo start = new ProcessStartInfo();
        start.FileName = Path.Combine(directory, "runtime", "python.exe");
        start.Arguments = "-B -u " + QuoteArgument(Path.Combine(directory, "controller.py")) + " " + action;
        if (!String.IsNullOrEmpty(selectedClient)) start.Arguments += " --client " + QuoteArgument(selectedClient);
        start.WorkingDirectory = directory;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.WindowStyle = ProcessWindowStyle.Hidden;
        start.RedirectStandardInput = true;
        start.RedirectStandardOutput = true;
        start.RedirectStandardError = true;
        start.StandardOutputEncoding = Encoding.UTF8;
        start.StandardErrorEncoding = Encoding.UTF8;
        start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        start.EnvironmentVariables["PYTHONUTF8"] = "1";
        start.EnvironmentVariables["NCM_WASAPI_DATA_DIR"] = dataDirectory;
        start.EnvironmentVariables["NCM_WASAPI_LOG_DIR"] = Path.Combine(dataDirectory, "logs");
        return start;
    }

    private bool RuntimePresent()
    {
        return File.Exists(Path.Combine(directory, "runtime", "python.exe")) &&
            File.Exists(Path.Combine(directory, "controller.py"));
    }

    private void StartPreflight()
    {
        if (checking || controllerActive || closingExtension) return;
        if (!RuntimePresent())
        {
            ShowFailure("扩展文件不完整。请重新安装，或完整解压便携版后重试。");
            return;
        }
        checking = true;
        failureShown = false;
        hasStatusError = false;
        currentState = "checking";
        statusLabel.ForeColor = Color.FromArgb(74, 82, 94);
        statusLabel.Text = "正在查找网易云音乐并检查是否兼容……";
        tray.Text = "网易云输出扩展 · 正在检测";
        UpdateButtons();
        ProcessStartInfo start = StartInfo("--check");
        ThreadPool.QueueUserWorkItem(delegate
        {
            Process process = new Process();
            object resultLock = new object();
            Dictionary<string, object> result = null;
            Exception failure = null;
            int code = -1;
            bool started = false;
            process.StartInfo = start;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (String.IsNullOrEmpty(e.Data)) return;
                Log("check " + e.Data);
                Dictionary<string, object> parsed = ParseStatus(e.Data);
                if (parsed != null) lock (resultLock) { result = parsed; }
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (!String.IsNullOrEmpty(e.Data)) Log("check stderr " + e.Data);
            };
            lock (processLock) { checker = process; }
            try
            {
                process.Start();
                started = true;
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                if (closingExtension) SendProcess(process, "exit");
                code = WaitForChild(process);
            }
            catch (Exception error)
            {
                failure = error;
                Log(error.ToString());
                if (started)
                {
                    SendProcess(process, "exit");
                    code = WaitForChild(process);
                }
            }
            Dictionary<string, object> finalResult;
            lock (resultLock) { finalResult = result; }
            OnUI(delegate { PreflightExited(process, code, finalResult, failure); });
        });
    }

    private static Dictionary<string, object> ParseStatus(string line)
    {
        try { return new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(line); }
        catch { return null; }
    }

    private static string TextField(Dictionary<string, object> value, string key)
    {
        object field;
        return value != null && value.TryGetValue(key, out field) && field != null
            ? Convert.ToString(field) : "";
    }

    private static bool TrueField(Dictionary<string, object> value, string key)
    {
        object field;
        return value != null && value.TryGetValue(key, out field) && field is bool && (bool)field;
    }

    private void PreflightExited(Process source, int code,
        Dictionary<string, object> result, Exception failure)
    {
        lock (processLock) { if (checker == source) checker = null; }
        source.Dispose();
        checking = false;
        if (closingExtension) { MaybeFinishShutdown(); return; }
        string state = TextField(result, "state");
        string version = TextField(result, "clientVersion");
        bool automaticCompatibility = TextField(result, "compatibilityMode") == "capabilities";
        if (automaticCompatibility && String.IsNullOrEmpty(version)) version = "自动识别（结构兼容）";
        if (!String.IsNullOrEmpty(version)) clientVersion = version;
        string path = TextField(result, "clientPath");
        if (!String.IsNullOrEmpty(path)) selectedClient = path;
        clientLabel.Text = String.IsNullOrEmpty(path)
            ? "尚未找到可用客户端，可点击“选择网易云…”指定位置。"
            : automaticCompatibility ? "网易云音乐 · 已自动识别兼容结构"
            : "网易云音乐 · " + (String.IsNullOrEmpty(version) ? "已找到客户端" : "版本 " + version);
        bool compatible = TrueField(result, "ok") || TrueField(result, "compatible") ||
            TrueField(result, "supported") || state == "ready" || state == "ok" ||
            state == "compatible" || state == "supported";
        if (failure == null && code == 0 && compatible)
        {
            StartController();
            return;
        }
        string message = TextField(result, "message");
        if (String.IsNullOrEmpty(message)) message = TextField(result, "error");
        if (String.IsNullOrEmpty(message))
            message = result == null
                ? "未能完成客户端检测。请重新安装扩展，或选择网易云音乐的位置。"
                : "未识别到兼容的客户端结构。请更新扩展后重新检测。";
        ShowFailure(message);
    }

    private void StartController()
    {
        if (controllerActive || checking || closingExtension) return;
        controllerActive = true;
        controllerExited.Reset();
        hasStatusError = false;
        failureShown = false;
        currentState = "starting";
        statusLabel.ForeColor = Color.FromArgb(74, 82, 94);
        statusLabel.Text = "正在启动网易云并准备输出选项……";
        tray.Text = "网易云输出扩展 · 正在启动";
        UpdateButtons();
        ProcessStartInfo start = StartInfo("--enable");
        ThreadPool.QueueUserWorkItem(delegate
        {
            Process process = new Process();
            process.StartInfo = start;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (!String.IsNullOrEmpty(e.Data)) ReadStatus(process, e.Data);
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e)
            {
                if (!String.IsNullOrEmpty(e.Data)) Log("stderr " + e.Data);
            };
            lock (processLock) { controller = process; }
            int code = -1;
            Exception failure = null;
            bool started = false;
            try
            {
                process.Start();
                started = true;
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                SendProcess(process, closingExtension ? "disable" : "status");
                code = WaitForChild(process);
            }
            catch (Exception error)
            {
                failure = error;
                Log(error.ToString());
                if (started)
                {
                    SendProcess(process, "disable");
                    code = WaitForChild(process);
                }
            }
            finally { controllerExited.Set(); }
            OnUI(delegate { ControllerExited(process, code, failure); });
        });
    }

    private void SendProcess(Process process, string action)
    {
        try
        {
            lock (processLock)
            {
                if (process == null || process.HasExited) return;
                Dictionary<string, object> command = new Dictionary<string, object>();
                command["action"] = action;
                process.StandardInput.WriteLine(new JavaScriptSerializer().Serialize(command));
                process.StandardInput.Flush();
            }
        }
        catch (Exception error) { Log("send " + error.Message); }
    }

    private int WaitForChild(Process process)
    {
        bool reported = false;
        for (;;)
        {
            try
            {
                process.WaitForExit();
                return process.ExitCode;
            }
            catch (Exception error)
            {
                if (!reported) { Log("waiting for cleanup " + error.Message); reported = true; }
                // An uncertain process state must keep the installation locked.
                // The installer can time out and leave the existing files intact.
                Thread.Sleep(500);
            }
        }
    }

    private void ReadStatus(Process source, string line)
    {
        Log(line);
        Dictionary<string, object> status = ParseStatus(line);
        if (status == null) return;
        OnUI(delegate
        {
            if (controller != source) return;
            string state = TextField(status, "state");
            string message = TextField(status, "message");
            if (!String.IsNullOrEmpty(state)) currentState = state;
            if (!String.IsNullOrEmpty(message)) statusLabel.Text = message;
            if (state == "error")
            {
                if (!closingExtension) ShowFailure(String.IsNullOrEmpty(message) ? "扩展未能正常运行。" : message);
                return;
            }
            if (state == "warning")
            {
                tray.Text = "网易云输出扩展 · 输出设备需要处理";
                if (!closingExtension && lastWarningMessage != message) ShowStatus();
                lastWarningMessage = message;
                return;
            }
            lastWarningMessage = null;
            if (closingExtension) return;
            statusLabel.ForeColor = Color.FromArgb(74, 82, 94);
            string mode = TextField(status, "activeMode");
            if (String.IsNullOrEmpty(mode)) mode = TextField(status, "mode");
            tray.Text = state == "disabled" ? "网易云输出扩展 · 已关闭" :
                state == "starting" ? "网易云输出扩展 · 正在启动" :
                mode == "exclusive" ? "网易云输出扩展 · 独占输出" :
                mode == "shared" ? "网易云输出扩展 · 共享输出" : "网易云输出扩展 · 已就绪";
        });
    }

    private void ControllerExited(Process source, int code, Exception failure)
    {
        lock (processLock) { if (controller == source) controller = null; }
        controllerActive = false;
        source.Dispose();
        Log("controller exited " + code.ToString());
        UpdateButtons();
        if (closingExtension) { MaybeFinishShutdown(); return; }
        if (failure == null && code == 0 && currentState == "disabled" && !hasStatusError)
        {
            // A normal player exit has already finished controller cleanup.
            // Close quietly instead of reopening the status window as an error.
            closingExtension = true;
            MaybeFinishShutdown();
            return;
        }
        if (!hasStatusError)
            ShowFailure(failure != null ? "无法启动扩展。请重新检测或重新安装扩展。" :
                code == 0 ? "扩展已关闭。普通网易云已恢复，可重新检测并启动。" :
                "扩展已停止。请查看提示后重新检测并启动。");
    }

    private void BrowseClient()
    {
        if (checking || controllerActive || closingExtension) return;
        using (OpenFileDialog dialog = new OpenFileDialog())
        {
            dialog.Title = "选择网易云音乐程序";
            dialog.Filter = "网易云音乐 (cloudmusic.exe)|cloudmusic.exe|应用程序 (*.exe)|*.exe";
            dialog.CheckFileExists = true;
            dialog.Multiselect = false;
            try
            {
                if (!String.IsNullOrEmpty(selectedClient) && File.Exists(selectedClient))
                    dialog.InitialDirectory = Path.GetDirectoryName(selectedClient);
            }
            catch { }
            if (dialog.ShowDialog(this) != DialogResult.OK) return;
            selectedClient = dialog.FileName;
            clientLabel.Text = "已选择客户端，正在检查是否兼容……";
            StartPreflight();
        }
    }

    private void CopyDiagnostics()
    {
        try
        {
            string version = FileVersionInfo.GetVersionInfo(Application.ExecutablePath).FileVersion;
            StringBuilder text = new StringBuilder();
            text.AppendLine("网易云输出扩展诊断信息");
            text.AppendLine("扩展版本：" + (String.IsNullOrEmpty(version) ? "开发版本" : version));
            text.AppendLine("Windows 版本：" + Environment.OSVersion.Version.ToString());
            text.AppendLine("64 位系统：" + (Environment.Is64BitOperatingSystem ? "是" : "否"));
            text.AppendLine("客户端版本：" + clientVersion);
            text.AppendLine("客户端：" + (String.IsNullOrEmpty(selectedClient) ? "尚未找到" : "已找到或手动指定"));
            text.AppendLine("状态：" + SafeState(currentState));
            text.AppendLine("运行文件：" + (RuntimePresent() ? "完整" : "缺失"));
            text.AppendLine("日志：本地应用数据 / NcmWasapiExtension / logs");
            Clipboard.SetText(text.ToString());
            statusLabel.Text = "诊断信息已复制，不包含客户端路径。";
        }
        catch { statusLabel.Text = "暂时无法复制诊断信息，请稍后重试。"; }
    }

    private static string SafeState(string value)
    {
        StringBuilder safe = new StringBuilder();
        foreach (char character in value ?? "")
        {
            if (safe.Length >= 64) break;
            if (Char.IsLetterOrDigit(character) || character == '_' || character == '-') safe.Append(character);
        }
        return safe.Length > 0 ? safe.ToString() : "unknown";
    }

    private void RequestCloseExtension()
    {
        if (closingExtension) return;
        closingExtension = true;
        currentState = "stopping";
        statusLabel.Text = "正在关闭扩展并恢复普通网易云，请稍候……";
        tray.Text = "网易云输出扩展 · 正在关闭";
        UpdateButtons();
        Process activeController;
        Process activeCheck;
        lock (processLock) { activeController = controller; activeCheck = checker; }
        // Keep the controller and its input pipe alive until protected cleanup
        // finishes. Upgrades wait for this form's mutex; they never kill audio.
        SendProcess(activeController, "disable");
        SendProcess(activeCheck, "exit");
        MaybeFinishShutdown();
    }

    private void MaybeFinishShutdown()
    {
        if (!closingExtension || checking || controllerActive || !controllerExited.WaitOne(0)) return;
        allowClose = true;
        Close();
    }

    private void OnClosing(object sender, FormClosingEventArgs e)
    {
        if (allowClose) return;
        e.Cancel = true;
        if (e.CloseReason == CloseReason.WindowsShutDown || e.CloseReason == CloseReason.TaskManagerClosing)
        {
            RequestCloseExtension();
            return;
        }
        ShowInTaskbar = false;
        Hide();
    }
}
