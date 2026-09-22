// Uses the Windows WinRT camera API and .NET Framework already installed on Windows.
// Camera access occurs only after ordinary startup; --self-test never enumerates it.
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using Windows.Devices.Enumeration;
using Windows.Foundation;
using Windows.Media.Capture;
using Windows.Media.MediaProperties;
using Windows.Storage.Streams;

internal static class CameraHost
{
    private static readonly object OutputLock = new object();
    private static MediaCapture capture;
    private static int exitCode;

    [STAThread]
    private static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        Console.InputEncoding = new UTF8Encoding(false);
        if (args.Length == 1 && args[0] == "--self-test")
        {
            using (Bitmap image = new Bitmap(64, 48))
            using (Graphics graphics = Graphics.FromImage(image))
            using (MemoryStream stream = new MemoryStream())
            {
                graphics.Clear(Color.SteelBlue);
                graphics.FillRectangle(Brushes.White, 8, 8, 16, 16);
                image.Save(stream, ImageFormat.Jpeg);
                Emit("PHOTO " + Convert.ToBase64String(stream.ToArray()));
            }
            return 0;
        }
        int index;
        if (args.Length != 1 || !Int32.TryParse(args[0], out index) || index < 0)
        {
            Emit("ERR Camera index must be a nonnegative integer.");
            return 2;
        }
        // Parent first attaches this process to a kill-on-close Job Object, then
        // sends OPEN. An abandoned/unattached child must never open the camera.
        string open = Console.ReadLine();
        if (open == null || open.Trim().Equals("STOP", StringComparison.OrdinalIgnoreCase)) return 0;
        if (!open.Trim().Equals("OPEN", StringComparison.OrdinalIgnoreCase))
        {
            Emit("ERR First command must be OPEN.");
            return 2;
        }
        // MediaCapture initialization must run on an STA thread with a message
        // pump. Application.Run creates no visible window and keeps continuations
        // on that same thread. No permissions, indicators, or camera LEDs are bypassed.
        bool started = false;
        Application.Idle += async delegate
        {
            if (started) return;
            started = true;
            try { await RunAsync(index); }
            catch (Exception ex) { EmitError(ex); exitCode = 1; }
            finally
            {
                try
                {
                    if (capture != null) { capture.Dispose(); capture = null; }
                }
                catch (Exception ex) { EmitError(ex); exitCode = 1; }
                finally { Application.ExitThread(); }
            }
        };
        Application.Run();
        return exitCode;
    }

    private static async Task RunAsync(int index)
    {
        DeviceInformationCollection cameras = await AsTask(DeviceInformation.FindAllAsync(DeviceClass.VideoCapture));
        if (index >= cameras.Count)
            throw new InvalidOperationException("Selected camera does not exist; available cameras: " + cameras.Count + ".");
        capture = new MediaCapture();
        MediaCaptureInitializationSettings settings = new MediaCaptureInitializationSettings();
        settings.VideoDeviceId = cameras[index].Id;
        settings.StreamingCaptureMode = StreamingCaptureMode.Video;
        settings.MemoryPreference = MediaCaptureMemoryPreference.Cpu;
        await AsTask(capture.InitializeAsync(settings));
        uint width = 0, height = 0;
        try
        {
            IMediaEncodingProperties properties = capture.VideoDeviceController.GetMediaStreamProperties(MediaStreamType.Photo);
            ImageEncodingProperties photo = properties as ImageEncodingProperties;
            VideoEncodingProperties video = properties as VideoEncodingProperties;
            if (photo != null) { width = photo.Width; height = photo.Height; }
            else if (video != null) { width = video.Width; height = video.Height; }
        }
        catch { /* Some devices negotiate photo dimensions only during capture. */ }
        Emit("READY " + width + " " + height);
        while (true)
        {
            string line = await Task.Run(() => Console.ReadLine());
            if (line == null || line.Trim().Equals("STOP", StringComparison.OrdinalIgnoreCase)) return;
            if (!line.Trim().Equals("SNAP", StringComparison.OrdinalIgnoreCase))
            {
                Emit("ERR Unknown command; use SNAP or STOP.");
                continue;
            }
            try
            {
                using (InMemoryRandomAccessStream stream = new InMemoryRandomAccessStream())
                {
                    await AsTask(capture.CapturePhotoToStreamAsync(ImageEncodingProperties.CreateJpeg(), stream));
                    if (stream.Size == 0 || stream.Size > 64UL * 1024UL * 1024UL)
                        throw new IOException("Camera returned an empty or oversized JPEG.");
                    stream.Seek(0);
                    using (DataReader reader = new DataReader(stream.GetInputStreamAt(0)))
                    {
                        uint size = checked((uint)stream.Size);
                        uint read = await AsTask<uint>(reader.LoadAsync(size));
                        if (read != size) throw new IOException("Camera JPEG stream was incomplete.");
                        byte[] jpeg = new byte[size];
                        reader.ReadBytes(jpeg);
                        Emit("PHOTO " + Convert.ToBase64String(jpeg));
                    }
                }
            }
            catch (Exception ex) { EmitError(ex); }
        }
    }

    private static void EmitError(Exception exception)
    {
        string text = exception.GetBaseException().Message.Replace('\r', ' ').Replace('\n', ' ').Replace('\0', ' ');
        if (text.Length > 600) text = text.Substring(0, 600);
        Emit("ERR " + text + " [0x" + exception.HResult.ToString("X8") + "]");
    }

    // System.Runtime.WindowsRuntime's task helpers require the optional SDK's
    // aggregate Windows.winmd. These small adapters use the OS metadata directly.
    private static Task AsTask(IAsyncAction operation)
    {
        TaskCompletionSource<bool> result = new TaskCompletionSource<bool>();
        operation.Completed = delegate(IAsyncAction action, AsyncStatus status)
        {
            try { action.GetResults(); result.TrySetResult(true); }
            catch (Exception ex) { result.TrySetException(ex); }
        };
        return result.Task;
    }

    private static Task<T> AsTask<T>(IAsyncOperation<T> operation)
    {
        TaskCompletionSource<T> result = new TaskCompletionSource<T>();
        operation.Completed = delegate(IAsyncOperation<T> action, AsyncStatus status)
        {
            try { result.TrySetResult(action.GetResults()); }
            catch (Exception ex) { result.TrySetException(ex); }
        };
        return result.Task;
    }

    private static void Emit(string line)
    {
        lock (OutputLock)
        {
            Console.WriteLine(line);
            Console.Out.Flush();
        }
    }
}
