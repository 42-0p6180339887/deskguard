using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;

internal static class PetHost {
    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();

    [STAThread]
    private static int Main(string[] args) {
        try {
            if (args.Length == 4 && args[0] == "--self-test") {
                File.WriteAllText(args[1], PatrolForm.SelfTest(args[2], args[3]));
                return 0;
            }
            if (args.Length != 2) return 2;
            SetProcessDPIAware();
            Application.EnableVisualStyles();
            using (var pet = new PatrolForm(args[0], args[1])) {
                var watcher = new Thread(() => {
                    try {
                        using (var input = Console.OpenStandardInput()) {
                            while (input.ReadByte() != -1) { }
                        }
                    } catch (IOException) { }
                    try {
                        if (!pet.IsDisposed && pet.IsHandleCreated)
                            pet.BeginInvoke((Action)(() => pet.Close()));
                    } catch (InvalidOperationException) { }
                });
                watcher.IsBackground = true;
                watcher.Start();
                Application.Run(pet);
            }
            return 0;
        } catch (Exception ex) {
            if (args.Length == 4 && args[0] == "--self-test")
                File.WriteAllText(args[1], ex.ToString());
            return 1;
        }
    }

    internal sealed class PatrolForm : Form {
        private const int WS_EX_LAYERED = 0x00080000;
        private const int WS_EX_TRANSPARENT = 0x00000020;
        private const int WS_EX_TOOLWINDOW = 0x00000080;
        private const int WS_EX_NOACTIVATE = 0x08000000;
        private const int WM_NCHITTEST = 0x0084;
        private const int WM_MOUSEACTIVATE = 0x0021;
        private const int ULW_ALPHA = 0x00000002;
        private readonly NativeFrame[,] frames = new NativeFrame[2, 2];
        private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        private readonly Stopwatch clock = new Stopwatch();
        private readonly Rectangle work;
        private readonly int side;
        private double x;
        private double lastSeconds;
        private int direction = 1;

        [StructLayout(LayoutKind.Sequential)]
        private struct Point { public int X, Y; public Point(int x, int y) { X = x; Y = y; } }
        [StructLayout(LayoutKind.Sequential)]
        private struct PixelSize { public int X, Y; public PixelSize(int x, int y) { X = x; Y = y; } }
        [StructLayout(LayoutKind.Sequential, Pack = 1)]
        private struct BlendFunction { public byte Op, Flags, Alpha, Format; }
        [StructLayout(LayoutKind.Sequential)]
        private struct BitmapInfoHeader {
            public uint Size; public int Width, Height; public ushort Planes, BitCount;
            public uint Compression, SizeImage; public int XPelsPerMeter, YPelsPerMeter;
            public uint ClrUsed, ClrImportant;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct BitmapInfo { public BitmapInfoHeader Header; public uint Colors; }

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool UpdateLayeredWindow(IntPtr hwnd, IntPtr destDC, ref Point dest,
            ref PixelSize size, IntPtr sourceDC, ref Point source, int colorKey,
            ref BlendFunction blend, int flags);
        [DllImport("gdi32.dll", SetLastError = true)]
        private static extern IntPtr CreateCompatibleDC(IntPtr dc);
        [DllImport("gdi32.dll", SetLastError = true)]
        private static extern IntPtr CreateDIBSection(IntPtr dc, ref BitmapInfo info, uint usage,
            out IntPtr bits, IntPtr section, uint offset);
        [DllImport("gdi32.dll", SetLastError = true)]
        private static extern IntPtr SelectObject(IntPtr dc, IntPtr obj);
        [DllImport("gdi32.dll", SetLastError = true)]
        private static extern bool DeleteObject(IntPtr obj);
        [DllImport("gdi32.dll", SetLastError = true)]
        private static extern bool DeleteDC(IntPtr dc);

        internal PatrolForm(string first, string second) {
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            work = Screen.PrimaryScreen.WorkingArea;
            side = Math.Min(420, Math.Max(270, (int)(work.Height * 0.42)));
            side = Math.Min(side, Math.Min(work.Width, work.Height));
            ClientSize = new System.Drawing.Size(side, side);
            x = work.Left;
            Location = new System.Drawing.Point(work.Left, work.Bottom - side);
            try {
                using (var one = new Bitmap(first))
                using (var two = new Bitmap(second)) {
                    for (int dir = 0; dir < 2; dir++) {
                        using (Bitmap rendered = Render(one, side, dir == 1))
                            frames[dir, 0] = new NativeFrame(rendered);
                        using (Bitmap rendered = Render(two, side, dir == 1))
                            frames[dir, 1] = new NativeFrame(rendered);
                    }
                }
            } catch {
                foreach (NativeFrame frame in frames) if (frame != null) frame.Dispose();
                throw;
            }
            timer.Interval = 33;
            timer.Tick += (sender, e) => TickPatrol();
            Shown += (sender, e) => { clock.Start(); PaintFrame(0); timer.Start(); };
        }

        protected override CreateParams CreateParams {
            get {
                CreateParams p = base.CreateParams;
                p.ExStyle |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
                return p;
            }
        }
        protected override bool ShowWithoutActivation { get { return true; } }
        protected override void WndProc(ref Message m) {
            if (m.Msg == WM_NCHITTEST) { m.Result = (IntPtr)(-1); return; }
            if (m.Msg == WM_MOUSEACTIVATE) { m.Result = (IntPtr)3; return; }
            base.WndProc(ref m);
        }
        protected override void OnPaintBackground(PaintEventArgs e) { }

        private static Bitmap Render(Bitmap source, int side, bool flip) {
            var output = new Bitmap(side, side, PixelFormat.Format32bppPArgb);
            using (Graphics g = Graphics.FromImage(output)) {
                g.Clear(Color.Transparent);
                g.CompositingMode = CompositingMode.SourceCopy;
                g.CompositingQuality = CompositingQuality.HighQuality;
                g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                if (flip) { g.TranslateTransform(side, 0); g.ScaleTransform(-1, 1); }
                g.DrawImage(source, new Rectangle(0, 0, side, side));
            }
            return output;
        }

        private void TickPatrol() {
            double seconds = clock.Elapsed.TotalSeconds;
            double delta = Math.Min(0.1, seconds - lastSeconds);
            lastSeconds = seconds;
            int left = work.Left, right = Math.Max(left, work.Right - side);
            Advance(ref x, ref direction, delta, left, right);
            int step = ((int)(seconds / 0.22)) % 2;
            PaintFrame(step, step == 0 ? 0 : 6);
        }

        private static void Advance(ref double position, ref int heading, double seconds, int left, int right) {
            if (right <= left) { position = left; return; }
            position += heading * 145.0 * seconds;
            if (position >= right) { position = right - (position - right); heading = -1; }
            if (position <= left) { position = left + (left - position); heading = 1; }
        }

        private void PaintFrame(int step, int bob = 0) {
            NativeFrame frame = frames[direction > 0 ? 0 : 1, step];
            var point = new Point((int)Math.Round(x), work.Bottom - side - bob);
            var size = new PixelSize(side, side);
            var origin = new Point(0, 0);
            var blend = new BlendFunction { Op = 0, Flags = 0, Alpha = 255, Format = 1 };
            if (!UpdateLayeredWindow(Handle, IntPtr.Zero, ref point, ref size, frame.DC,
                ref origin, 0, ref blend, ULW_ALPHA))
                throw new System.ComponentModel.Win32Exception();
        }

        internal static string SelfTest(string first, string second) {
            double position = 498;
            int heading = 1;
            Advance(ref position, ref heading, 0.05, 0, 500);
            if (heading != -1 || position >= 500) throw new InvalidDataException("Right turn failed.");
            position = 2;
            Advance(ref position, ref heading, 0.05, 0, 500);
            if (heading != 1 || position <= 0) throw new InvalidDataException("Left turn failed.");
            using (var one = new Bitmap(first))
            using (var two = new Bitmap(second))
            using (Bitmap right = Render(one, 360, false))
            using (Bitmap left = Render(two, 360, true)) {
                if (right.GetPixel(0, 0).A != 0 || left.GetPixel(0, 0).A != 0 ||
                    right.GetPixel(180, 240).A < 200 || left.GetPixel(180, 240).A < 200)
                    throw new InvalidDataException("Pet alpha or dimensions are invalid.");
                return "{\"passed\":true,\"transparent_corner\":true,\"body_opaque\":true,\"turns_at_edges\":true}";
            }
        }

        protected override void Dispose(bool disposing) {
            if (disposing) {
                timer.Stop(); timer.Dispose();
                foreach (NativeFrame frame in frames) if (frame != null) frame.Dispose();
            }
            base.Dispose(disposing);
        }

        private sealed class NativeFrame : IDisposable {
            internal IntPtr DC { get; private set; }
            private IntPtr dib;
            private IntPtr old;

            internal NativeFrame(Bitmap bitmap) {
                int width = bitmap.Width, height = bitmap.Height;
                var info = new BitmapInfo();
                info.Header.Size = (uint)Marshal.SizeOf(typeof(BitmapInfoHeader));
                info.Header.Width = width;
                info.Header.Height = -height; // Top-down, premultiplied BGRA.
                info.Header.Planes = 1;
                info.Header.BitCount = 32;
                try {
                    DC = CreateCompatibleDC(IntPtr.Zero);
                    if (DC == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
                    IntPtr bits;
                    dib = CreateDIBSection(DC, ref info, 0, out bits, IntPtr.Zero, 0);
                    if (dib == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
                    var rect = new Rectangle(0, 0, width, height);
                    BitmapData data = bitmap.LockBits(rect, ImageLockMode.ReadOnly, PixelFormat.Format32bppPArgb);
                    try {
                        byte[] row = new byte[width * 4];
                        for (int y = 0; y < height; y++) {
                            Marshal.Copy(IntPtr.Add(data.Scan0, y * data.Stride), row, 0, row.Length);
                            Marshal.Copy(row, 0, IntPtr.Add(bits, y * row.Length), row.Length);
                        }
                    } finally { bitmap.UnlockBits(data); }
                    old = SelectObject(DC, dib);
                    if (old == IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
                } catch { Dispose(); throw; }
            }

            public void Dispose() {
                if (old != IntPtr.Zero) { SelectObject(DC, old); old = IntPtr.Zero; }
                if (dib != IntPtr.Zero) { DeleteObject(dib); dib = IntPtr.Zero; }
                if (DC != IntPtr.Zero) { DeleteDC(DC); DC = IntPtr.Zero; }
            }
        }
    }
}
