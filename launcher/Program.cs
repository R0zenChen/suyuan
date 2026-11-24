using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Threading;
using System.Windows.Forms;

namespace SuyuanLauncher
{
    internal static class Program
    {
        internal static string Root { get { return AppDomain.CurrentDomain.BaseDirectory; } }

        internal static int Port()
        {
            string raw = Environment.GetEnvironmentVariable("PORT");
            if (String.IsNullOrWhiteSpace(raw))
            {
                string envPath = Path.Combine(Root, ".env");
                if (File.Exists(envPath))
                {
                    foreach (string line in File.ReadAllLines(envPath))
                    {
                        string trimmed = line.Trim();
                        if (trimmed.StartsWith("PORT=", StringComparison.OrdinalIgnoreCase))
                        {
                            raw = trimmed.Substring(5).Trim().Trim('"', '\'');
                            break;
                        }
                    }
                }
            }
            int port;
            return Int32.TryParse(raw, out port) && port > 0 && port <= 65535 ? port : 8000;
        }

        internal static string Url { get { return "http://127.0.0.1:" + Port(); } }

        internal static bool IsSuyuanReady()
        {
            try
            {
                HttpWebRequest request = (HttpWebRequest)WebRequest.Create(Url + "/api/config");
                request.Timeout = 800;
                request.ReadWriteTimeout = 800;
                request.Proxy = null;
                request.KeepAlive = false;
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                {
                    string server = response.Headers["Server"] ?? "";
                    return response.StatusCode == HttpStatusCode.OK && server.StartsWith("SuyuanDemo/", StringComparison.Ordinal);
                }
            }
            catch (WebException) { return false; }
            catch (IOException) { return false; }
        }

        internal static void OpenBrowser()
        {
            Process.Start(new ProcessStartInfo(Url) { UseShellExecute = true });
        }

        [STAThread]
        private static void Main()
        {
            bool first;
            using (Mutex mutex = new Mutex(true, "Local\\SuyuanResearchLauncher", out first))
            {
                if (!first)
                {
                    try { if (IsSuyuanReady()) OpenBrowser(); }
                    catch (Exception) { }
                    return;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new LauncherForm());
                mutex.ReleaseMutex();
            }
        }
    }

    internal sealed class LauncherForm : Form
    {
        private readonly Label status;
        private readonly Label detail;
        private readonly Button toggle;
        private readonly Button open;
        private readonly Button log;
        private readonly System.Windows.Forms.Timer timer;
        private readonly Stopwatch startup = new Stopwatch();
        private readonly object logLock = new object();
        private Process service;
        private bool owned;
        private bool stopping;
        private bool browserOpened;

        internal LauncherForm()
        {
            Text = "溯源 · 本地服务";
            ClientSize = new Size(430, 222);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            BackColor = Color.FromArgb(248, 251, 248);
            Font = new Font("Microsoft YaHei UI", 9F);
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); }
            catch (Exception) { }

            Label brand = new Label { Text = "溯源", Font = new Font("Microsoft YaHei UI", 19F, FontStyle.Bold),
                ForeColor = Color.FromArgb(22, 72, 54), Location = new Point(24, 18), AutoSize = true };
            Label subtitle = new Label { Text = "本地研究服务", ForeColor = Color.FromArgb(106, 125, 112),
                Location = new Point(116, 30), AutoSize = true };
            status = new Label { Text = "正在检查服务…", Font = new Font("Microsoft YaHei UI", 11F, FontStyle.Bold),
                ForeColor = Color.FromArgb(28, 78, 57), Location = new Point(26, 79), Size = new Size(380, 29) };
            detail = new Label { Text = "", ForeColor = Color.FromArgb(96, 111, 101),
                Location = new Point(26, 111), Size = new Size(380, 40) };
            open = new Button { Text = "打开网页", Location = new Point(24, 166), Size = new Size(118, 35),
                BackColor = Color.FromArgb(22, 77, 59), ForeColor = Color.White, FlatStyle = FlatStyle.Flat };
            open.FlatAppearance.BorderSize = 0;
            open.Click += delegate { try { Program.OpenBrowser(); } catch (Exception error) { ShowError("浏览器未能打开：" + error.Message); } };
            toggle = new Button { Text = "停止服务", Location = new Point(150, 166), Size = new Size(118, 35),
                BackColor = Color.White, ForeColor = Color.FromArgb(22, 77, 59), FlatStyle = FlatStyle.Flat };
            toggle.Click += ToggleClicked;
            log = new Button { Text = "查看日志", Location = new Point(276, 166), Size = new Size(118, 35),
                BackColor = Color.White, ForeColor = Color.FromArgb(96, 111, 101), FlatStyle = FlatStyle.Flat };
            log.Click += delegate {
                string path = Path.Combine(Program.Root, "data", "server-launcher.log");
                if (File.Exists(path)) Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
                else ShowError("目前还没有启动日志。");
            };
            Controls.AddRange(new Control[] { brand, subtitle, status, detail, open, toggle, log });
            timer = new System.Windows.Forms.Timer { Interval = 350 };
            timer.Tick += CheckStartup;
            Shown += delegate { StartOrOpen(); };
            FormClosing += delegate { StopOwnedService(); };
        }

        private void ShowError(string message)
        {
            status.Text = message;
            status.ForeColor = Color.FromArgb(156, 62, 42);
        }

        private void ShowState(string title, string description)
        {
            status.Text = title;
            status.ForeColor = Color.FromArgb(28, 78, 57);
            detail.Text = description;
        }

        private void AppendLog(string line)
        {
            if (String.IsNullOrEmpty(line)) return;
            try
            {
                lock (logLock)
                {
                    string folder = Path.Combine(Program.Root, "data");
                    Directory.CreateDirectory(folder);
                    File.AppendAllText(Path.Combine(folder, "server-launcher.log"),
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + line + Environment.NewLine);
                }
            }
            catch (IOException) { }
        }

        private void StartOrOpen()
        {
            if (!File.Exists(Path.Combine(Program.Root, "app.py")))
            {
                ShowError("找不到 app.py，请把启动器放在项目文件夹中。");
                detail.Text = Program.Root;
                toggle.Enabled = false;
                open.Enabled = false;
                return;
            }
            if (Program.IsSuyuanReady())
            {
                owned = false;
                ShowState("服务已在运行", "由其他方式启动；关闭原服务后可由此启动器接管。");
                toggle.Text = "已在运行";
                toggle.Enabled = false;
                open.Enabled = true;
                OpenOnce();
                return;
            }
            try
            {
                string localPython = Path.Combine(Program.Root, ".venv", "Scripts", "python.exe");
                string python = File.Exists(localPython) ? localPython : "python.exe";
                ProcessStartInfo info = new ProcessStartInfo(python, "-u app.py");
                info.WorkingDirectory = Program.Root;
                info.UseShellExecute = false;
                info.CreateNoWindow = true;
                info.WindowStyle = ProcessWindowStyle.Hidden;
                info.RedirectStandardOutput = true;
                info.RedirectStandardError = true;
                service = new Process { StartInfo = info, EnableRaisingEvents = true };
                service.OutputDataReceived += delegate(object sender, DataReceivedEventArgs e) { AppendLog(e.Data); };
                service.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs e) { AppendLog(e.Data); };
                service.Exited += delegate {
                    if (!IsDisposed && IsHandleCreated)
                        BeginInvoke((MethodInvoker)delegate {
                            if (!stopping && owned)
                            {
                                timer.Stop();
                                owned = false;
                                ShowError("服务已停止，请查看日志。");
                                detail.Text = "可点击“启动服务”重试。";
                                toggle.Text = "启动服务";
                                toggle.Enabled = true;
                            }
                        });
                };
                if (!service.Start()) throw new InvalidOperationException("Python 进程未启动。");
                service.BeginOutputReadLine();
                service.BeginErrorReadLine();
                owned = true;
                stopping = false;
                browserOpened = false;
                ShowState("正在启动服务…", "就绪后将自动打开 " + Program.Url);
                toggle.Enabled = false;
                open.Enabled = false;
                startup.Restart();
                timer.Start();
            }
            catch (Exception error)
            {
                owned = false;
                ShowError("启动失败：" + error.Message);
                detail.Text = "请确认本机已安装 Python，并查看启动日志。";
                toggle.Text = "启动服务";
                toggle.Enabled = true;
            }
        }

        private void CheckStartup(object sender, EventArgs e)
        {
            if (Program.IsSuyuanReady())
            {
                timer.Stop();
                startup.Stop();
                ShowState("服务运行中", Program.Url + "  ·  关闭此窗口会停止服务");
                toggle.Text = "停止服务";
                toggle.Enabled = true;
                open.Enabled = true;
                OpenOnce();
            }
            else if (service == null || service.HasExited || startup.Elapsed.TotalSeconds > 30)
            {
                timer.Stop();
                StopOwnedService();
                ShowError("启动超时或端口被占用，请查看日志。");
                detail.Text = "确认 Python 可运行，以及本机端口 " + Program.Port() + " 未被其他程序占用。";
                toggle.Text = "启动服务";
                toggle.Enabled = true;
            }
        }

        private void OpenOnce()
        {
            if (browserOpened) return;
            browserOpened = true;
            try { Program.OpenBrowser(); }
            catch (Exception error) { ShowError("浏览器未能打开：" + error.Message); }
        }

        private void ToggleClicked(object sender, EventArgs e)
        {
            if (owned)
            {
                StopOwnedService();
                ShowState("服务已停止", "点击“启动服务”可重新打开网页。" );
                toggle.Text = "启动服务";
                open.Enabled = false;
            }
            else StartOrOpen();
        }

        private void StopOwnedService()
        {
            timer.Stop();
            if (!owned || service == null) return;
            stopping = true;
            try { if (!service.HasExited) service.Kill(); }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
            owned = false;
            service.Dispose();
            service = null;
            stopping = false;
            browserOpened = false;
        }
    }
}
