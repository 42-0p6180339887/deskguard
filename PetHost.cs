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
            if (args.Length != 2 && args.Length != 5) return 2;
            SetProcessDPIAware();
            Application.EnableVisualStyles();
            using (var pet = new PatrolForm(args[0], args[1],
                args.Length == 5 ? args[2] : "large", args.Length == 5 ? args[3] : "normal",
                args.Length == 5 && args[4] == "gentle")) {
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
                pet.Shown += (sender, e) => watcher.Start();
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
        private const int PoseCount = 16;
        private const int TurnCount = 6;
        private const double TurnDuration = 0.36;
        private readonly NativeFrame[,] frames = new NativeFrame[2, PoseCount];
        private readonly NativeFrame[,] turns = new NativeFrame[2, TurnCount];
        private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        private readonly Stopwatch clock = new Stopwatch();
        private Rectangle work;
        private readonly int side;
        private readonly double speed;
        private readonly bool gentle;
        private readonly bool systemAnimationsOff;
        private int lastBoundsCheck;

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool SystemParametersInfoW(uint action, uint param,
            [MarshalAs(UnmanagedType.Bool)] out bool enabled, uint flags);

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

        internal PatrolForm(string first, string second, string size, string pace, bool gentleMotion) {
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = FormStartPosition.Manual;
            work = Screen.PrimaryScreen.WorkingArea;
            int preferred = size == "small" ? 240 : size == "medium" ? 330 : 420;
            side = Math.Min(preferred, Math.Max(180, (int)(work.Height * 0.42)));
            side = Math.Min(side, Math.Min(work.Width, work.Height));
            speed = pace == "slow" ? 90 : pace == "brisk" ? 210 : 145;
            bool animations;
            systemAnimationsOff = SystemParametersInfoW(0x1042, 0, out animations, 0) && !animations;
            gentle = gentleMotion || systemAnimationsOff;
            ClientSize = new System.Drawing.Size(side, side);
            Location = new System.Drawing.Point(work.Left, work.Bottom - side);
            try {
                using (var one = new Bitmap(first))
                using (var two = new Bitmap(second))
                using (Bitmap preparedOne = Render(one, side, false))
                using (Bitmap preparedTwo = Render(two, side, false)) {
                    byte[] a = Pixels(preparedOne), b = Pixels(preparedTwo);
                    for (int dir = 0; dir < 2; dir++) {
                        for (int i = 0; i < PoseCount; i++) {
                            double phase = i * 2 * Math.PI / PoseCount;
                            using (Bitmap blended = Blend(a, b, side, (1 - Math.Cos(phase)) / 2))
                            using (Bitmap rendered = Pose(blended, dir == 1,
                                gentle ? 0 : Math.Sin(phase) * 1.1, 1))
                                frames[dir, i] = new NativeFrame(rendered);
                        }
                        for (int i = 0; i < TurnCount; i++)
                            using (Bitmap rendered = Pose(preparedOne, dir == 1, 0,
                                gentle ? 1 : 1 - 0.8 * i / (TurnCount - 1)))
                                turns[dir, i] = new NativeFrame(rendered);
                    }
                }
            } catch {
                foreach (NativeFrame frame in frames) if (frame != null) frame.Dispose();
                foreach (NativeFrame frame in turns) if (frame != null) frame.Dispose();
                throw;
            }
            timer.Interval = systemAnimationsOff ? 1000 : 15;
            timer.Tick += (sender, e) => TickPatrol();
            Shown += (sender, e) => {
                clock.Start(); TickPatrol();
                timer.Start();
            };
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

        private static byte[] Pixels(Bitmap bitmap) {
            BitmapData data = bitmap.LockBits(new Rectangle(0, 0, bitmap.Width, bitmap.Height),
                ImageLockMode.ReadOnly, PixelFormat.Format32bppPArgb);
            try {
                byte[] pixels = new byte[bitmap.Width * bitmap.Height * 4];
                for (int y = 0; y < bitmap.Height; y++)
                    Marshal.Copy(IntPtr.Add(data.Scan0, y * data.Stride), pixels,
                        y * bitmap.Width * 4, bitmap.Width * 4);
                return pixels;
            } finally { bitmap.UnlockBits(data); }
        }

        private static Bitmap Blend(byte[] first, byte[] second, int side, double amount) {
            var bitmap = new Bitmap(side, side, PixelFormat.Format32bppPArgb);
            byte[] mixed = new byte[first.Length];
            // Interpolate premultiplied colors AND alpha, so a pose transition
            // never creates a dark edge or a translucent body.
            for (int i = 0; i < mixed.Length; i++)
                mixed[i] = (byte)Math.Round(first[i] * (1 - amount) + second[i] * amount);
            BitmapData data = bitmap.LockBits(new Rectangle(0, 0, side, side),
                ImageLockMode.WriteOnly, PixelFormat.Format32bppPArgb);
            try {
                for (int y = 0; y < side; y++)
                    Marshal.Copy(mixed, y * side * 4, IntPtr.Add(data.Scan0, y * data.Stride), side * 4);
            } finally { bitmap.UnlockBits(data); }
            return bitmap;
        }

        private static Bitmap Pose(Bitmap source, bool flip, double tilt, double widthScale) {
            int side = source.Width;
            var output = new Bitmap(side, side, PixelFormat.Format32bppPArgb);
            using (Graphics g = Graphics.FromImage(output)) {
                g.Clear(Color.Transparent);
                g.CompositingMode = CompositingMode.SourceCopy;
                g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                g.PixelOffsetMode = PixelOffsetMode.HighQuality;
                g.TranslateTransform(side / 2f, side * 0.92f);
                g.ScaleTransform((float)(widthScale * (flip ? -1 : 1)), 1);
                g.RotateTransform((float)tilt);
                g.TranslateTransform(-side / 2f, -side * 0.92f);
                g.DrawImage(source, new Rectangle(0, 0, side, side));
            }
            return output;
        }

        private struct Motion {
            internal double X, Phase;
            internal int Heading, TurnLevel;
            internal bool Turning;
        }

        private static Motion Sample(double seconds, int left, int right, double speed, int side) {
            double distance = Math.Max(0, right - left);
            if (distance == 0) return new Motion { X = left, Heading = 1 };
            double duration = Math.Max(0.6, distance / speed);
            double segment = duration + TurnDuration;
            long leg = (long)Math.Floor(seconds / segment);
            double local = seconds - leg * segment;
            int heading = leg % 2 == 0 ? 1 : -1;
            double t = Math.Min(1, local / duration);
            double progress = (1 - Math.Cos(Math.PI * t)) / 2;
            var result = new Motion {
                X = heading == 1 ? left + distance * progress : right - distance * progress,
                Heading = heading,
                Phase = progress * Math.Max(1, Math.Round(distance / (side * 0.22))) * 2 * Math.PI,
                Turning = local >= duration,
            };
            if (result.Turning) {
                double turn = (local - duration) / TurnDuration;
                result.Heading = turn < 0.5 ? heading : -heading;
                result.TurnLevel = (int)Math.Round((1 - Math.Abs(Math.Cos(Math.PI * turn))) * (TurnCount - 1));
            }
            return result;
        }

        private void TickPatrol() {
            double seconds = clock.Elapsed.TotalSeconds;
            if ((int)seconds != lastBoundsCheck) {
                lastBoundsCheck = (int)seconds;
                Rectangle current = Screen.PrimaryScreen.WorkingArea;
                if (current != work) { work = current; clock.Restart(); seconds = 0; lastBoundsCheck = 0; }
            }
            Motion motion = Sample(systemAnimationsOff ? 0 : seconds, work.Left,
                Math.Max(work.Left, work.Right - side), speed, side);
            int pose = ((int)Math.Round(motion.Phase / (2 * Math.PI) * PoseCount)) % PoseCount;
            int dir = motion.Heading == 1 ? 0 : 1;
            NativeFrame frame = motion.Turning ? turns[dir, motion.TurnLevel] : frames[dir, pose];
            int bob = gentle || motion.Turning ? 0 : (int)Math.Round(3 * (1 - Math.Cos(2 * motion.Phase)));
            PaintFrame(frame, motion.X, bob);
        }

        private void PaintFrame(NativeFrame frame, double x, int bob) {
            var point = new Point((int)Math.Round(x), work.Bottom - side - bob);
            var size = new PixelSize(side, side);
            var origin = new Point(0, 0);
            var blend = new BlendFunction { Op = 0, Flags = 0, Alpha = 255, Format = 1 };
            if (!UpdateLayeredWindow(Handle, IntPtr.Zero, ref point, ref size, frame.DC,
                ref origin, 0, ref blend, ULW_ALPHA))
                throw new System.ComponentModel.Win32Exception();
        }

        internal static string SelfTest(string first, string second) {
            double duration = 500.0 / 145;
            Motion rightTurn = Sample(duration + TurnDuration * 0.75, 0, 500, 145, 360);
            Motion leftTurn = Sample(2 * duration + TurnDuration * 1.75, 0, 500, 145, 360);
            if (rightTurn.Heading != -1 || rightTurn.X != 500 || !rightTurn.Turning)
                throw new InvalidDataException("Right turn failed.");
            if (leftTurn.Heading != 1 || leftTurn.X != 0 || !leftTurn.Turning)
                throw new InvalidDataException("Left turn failed.");
            Motion previous = Sample(0, 0, 500, 145, 360);
            for (int i = 1; i <= 2000; i++) {
                Motion current = Sample(i * 0.01, 0, 500, 145, 360);
                if (current.X < 0 || current.X > 500 || Math.Abs(current.X - previous.X) > 2.3)
                    throw new InvalidDataException("Motion is not continuous or escaped the desktop.");
                previous = current;
            }
            using (var one = new Bitmap(first))
            using (var two = new Bitmap(second))
            using (Bitmap right = Render(one, 360, false))
            using (Bitmap left = Render(two, 360, true)) {
                if (right.GetPixel(0, 0).A != 0 || left.GetPixel(0, 0).A != 0 ||
                    right.GetPixel(180, 240).A < 200 || left.GetPixel(180, 240).A < 200)
                    throw new InvalidDataException("Pet alpha or dimensions are invalid.");
                using (Bitmap mixed = Blend(Pixels(right), Pixels(left), 360, 0.5)) {
                    if (mixed.GetPixel(180, 240).A < 200)
                        throw new InvalidDataException("Pose blending lost body alpha.");
                }
                return "{\"passed\":true,\"transparent_corner\":true,\"body_opaque\":true,\"turns_at_edges\":true,\"continuous_motion\":true}";
            }
        }

        protected override void Dispose(bool disposing) {
            if (disposing) {
                timer.Stop(); timer.Dispose();
                foreach (NativeFrame frame in frames) if (frame != null) frame.Dispose();
                foreach (NativeFrame frame in turns) if (frame != null) frame.Dispose();
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
