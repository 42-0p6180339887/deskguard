# 开发与构建

## 环境

- Windows 10 / 11 x64。
- Python 3.12 x64 官方安装版，安装 Tcl/Tk。可用 `python -c "import tkinter"` 检查模块是否存在；此命令不打开窗口或摄像头。
- Windows 的 .NET Framework 编译器 `csc.exe` 及 WinMetadata。`build_camera.ps1` 默认从 `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319` 与 `%WINDIR%\System32\WinMetadata` 读取。

代码使用 Python 标准库，没有需要通过 pip 安装的依赖。Python 的嵌入式分发通常不包含本项目界面所需的完整 Tcl/Tk 环境，请使用包含 Tcl/Tk 的安装版进行构建。

## 运行与测试

在仓库根目录构建摄像头组件，再启动程序：

```powershell
powershell -File build_camera.ps1
python app.py
```

第一条命令只编译 `CameraHost.exe`，不打开摄像头。程序正常启动也不会自动布防；实际拍照需在界面选择“测试拍照”或“开始布防”。

执行离线测试：

```powershell
python -m unittest discover -s tests -v
```

编译完成后可运行界面与模拟照片自检：

```powershell
python app.py --self-test self-test.json
```

自检使用隐藏界面和模拟 JPEG，不启用真实摄像头或输入钩子。硬件覆盖、触屏手势与自动化过滤须另按 [使用说明](USER_GUIDE.zh-CN.md) 验收；不要把 CI 通过视为硬件测试通过。

## 便携包

```powershell
python build_release.py --output dist
```

构建脚本生成 Windows x64 便携包，包含启动入口、应用文件、摄像头辅助程序及 Python/Tcl/Tk 运行环境。典型目录结构：

```text
DeskGuard.exe
assets/
    deskguard.ico
    deskguard.png
    deskguard-pet.png
_app/
    app.py
    desktop_pet.py
    CameraHost.exe
    ...
_runtime/
    pythonw.exe
    LICENSE.txt
    DLLs/
    Lib/
    tcl/
```

发布时保留完整目录及第三方许可文件。生成的二进制、运行环境、`dist/`、照片、配置和自检结果不应提交到源码仓库。GitHub Actions 会测试、构建并上传工作流产物；正式的 Releases 由维护者创建，工作流本身只有仓库读取权限。

## 图标与版本

`assets/deskguard.png` 是保安鹅应用图标，`assets/deskguard.ico` 是 Windows 图标文件，`assets/deskguard-pet.png` 是透明桌宠素材。应用图标保持静态；布防状态通过托盘文字提示和主窗口表达，图案中的红点不是拍照状态指示。图像由内置图像工具生成，许可说明见仓库首页。

构建脚本将应用图标、桌宠图像、`desktop_pet.py` 和 `CHANGELOG.md` 纳入发布文件白名单，并通过编译器的 `/win32icon` 参数将 ICO 嵌入 `DeskGuard.exe`。桌宠通过可选的点击穿透顶层窗口和 Tk 事件循环动画实现。添加其他资源时，应同时更新白名单及相关打包测试，避免将运行后产生的照片或配置混入发布包。

`VERSION` 是版本号来源，采用三个数字分段，例如 `0.1.2`。构建脚本据此生成启动程序和摄像头辅助程序的版本元数据：文件版本为 `0.1.2.0`，产品版本为 `0.1.2`。发布新版本时同步维护 `CHANGELOG.md`，并使用对应的 `v0.1.2` 格式标签。

## 代码分工

| 文件 | 作用 |
| --- | --- |
| `app.py` | 界面、状态、手动布防、暂停与进程管理 |
| `windows_tray.py` | 托盘和全局控制快捷键 |
| `windows_input.py` | 输入通知计数、模拟输入过滤、活动时间兼容模式 |
| `guard_core.py` | 触发合并、拍照间隔、容量检查、照片与元数据保存 |
| `camera_worker.py` | 与摄像头辅助进程通信，使用 Windows Job Object 清理子进程 |
| `CameraHost.cs` | Windows MediaCapture；收到 `OPEN` 才初始化，`SNAP` 拍照，`STOP` 释放 |
| `Launcher.cs` | 便携版启动入口 |

输入模块不读取键值或保存鼠标坐标。低级输入回调只检查标记、更新计数并传递事件；摄像头和文件操作在回调之外执行。检测到锁屏或安全桌面后尽快暂停摄像头，恢复后重新建立触发基线；轮询和进行中的拍照可能带来短暂延迟，这不是严格的安全边界。任何改变这些行为的修改都应同步更新使用说明和相关测试。
