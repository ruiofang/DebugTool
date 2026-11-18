# 创芯 CANFD Linux 运行库

来源：项目原始资料包 `DOC/Linux资料包V1.03/二次开发库文件/libcontrolcanfd/`，对应创芯 USBCANFD-200U 的 `ZCAN_*` 接口。此目录的五份 `libcontrolcanfd.so` 均从资料包原样复制，原始资料包保持完整。

| 子目录 | 目标进程架构 |
| --- | --- |
| `x86_64` | 64 位 x86 Linux |
| `x86_32` | 32 位 x86 Linux |
| `aarch64` | 64 位 ARM Linux |
| `aarch32` | 32 位 ARM Linux |
| `riscv64` | 64 位 RISC-V Linux |

此处只保存程序需要的动态运行库；静态库 `.a`、头文件、示例与说明文档仍留在 `DOC/Linux资料包V1.03`。程序按 Python 进程位数和 CPU 架构选择对应 `.so`，源码运行与 PyInstaller 包内均使用 `lib/controlcanfd/<架构>/libcontrolcanfd.so`。Linux 打包仅收集当前架构，不需要将整个资料包放入产物。
