# 第三方组件声明

DeskGuard 的原创源码和文档采用仓库根目录的 [MIT License](LICENSE)。该许可不替代随便携包分发的运行时或操作系统组件的许可。

源码仓库不提交 Python、Tcl/Tk、DLL 或生成的可执行文件。便携发布包可能包含下列运行时组件，完整许可应与对应文件一同保留。

## Python 及其随附组件

便携包使用 Python 3.12 运行环境。Python 本体采用 Python Software Foundation License Version 2，并附有历史许可及其他组件的声明。完整原文位于发布包：

```text
_runtime/LICENSE.txt
```

该文件同时包含本次 Windows Python 分发所附的第三方条款，包括 Microsoft Distributable Code 的附加条件及部分标准库、Tcl/Tk 相关组件的声明。以所用 Python 分发实际附带的原文为准；这些文件不是以 DeskGuard 的 MIT 许可重新授权。

DeskGuard 的打包过程重组运行时目录以便随程序运行，不修改 Python 解释器源码。项目不要求安装第三方 Python 包。

## Tcl/Tk

图形界面使用 Python 的 `tkinter` 与随附 Tcl/Tk。其版权所有者和完整许可保留在 Python 分发的许可文件及组件自身声明中。

当前便携包中的独立 Tk 许可路径为：

```text
_runtime/tcl/tk8.6/license.terms
_runtime/tcl/tk8.6/demos/license.terms
```

`_runtime/LICENSE.txt` 也保留了该 Python 分发随附的 Tcl/Tk 相关许可文本。更换运行时分发时应保留其实际提供的许可文件；目录版本号可能随分发更新。

## Microsoft 组件

随 Windows Python 分发包含的 Microsoft Visual C++ 运行时及可再分发代码，属于 Microsoft 或其他相应权利人，适用其原有条款。请参阅 `_runtime/LICENSE.txt` 中的 **Additional Conditions for this Windows binary build**，以及对应分发附带的其他声明。

DeskGuard 还调用目标 Windows 系统提供的 Win32 API、MediaCapture、.NET Framework 和系统 DLL。操作系统提供的这些组件不属于本项目的 MIT 授权范围，也不因本项目调用它们而被重新授权。

发布包应保留原始版权、许可和声明文件；本说明用于标明组件来源及查阅位置，不替代完整许可文本。
