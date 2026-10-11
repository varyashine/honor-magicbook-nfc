[English](README.md) | [Русский](README.ru.md) | [中文](README.zh-CN.md)

# honor-magicbook-nfc

这里是我对 HONOR MagicBook 笔记本内置 NFC 标签的全部研究成果，外加一个在
Linux 上使用它的小工具。芯片是上海复旦微 **FM11NT082C**，在 ACPI 里叫
`NTAG0001`，挂在 I²C 总线 `0x57` 地址上。

工具零依赖：纯 Python 3 标准库，只用 fcntl 和 ctypes。

## NFC 模块到底在哪

触摸板的正中央。不在掌托边缘，也不在 C 面角落。把手机放在触摸板正中，
就能读到标签。

## 出厂状态下标签里有什么

```
0000: 1d 19 db 57 3c 07 00 10  2b b0 ff ff e1 10 6d 00   锁定字节 ff ff @0x0A，CC @0x0C
0010: 03 8c 91 01 33 55 04 77  77 77 2e 68 69 68 6f 6e   NDEF TLV，URI 记录
0020: 6f 72 2e 63 6f 6d 2f 67  6c 6f 62 61 6c 2f 73 75   ...
0048: MIME 记录 application/vnd.huawei.handoff.ndfmsg + 44 字节令牌
```

两条 NDEF 记录：一条 URI 指向
`https://www.hihonor.com/global/support/magic_link_welcome/`，一条
MIME 记录 `application/vnd.huawei.handoff.ndfmsg`，由荣耀"一碰传"
软件轮换。手机显示的 "UID"（`1D:19:DB:3C:07:00:10`）其实就是 EEPROM
块 0 的开头几个字节：芯片把自己的内存头部当作标识符交出去。

## 成功了的部分

- 在 Linux 上、无需任何厂商软件，稳定读出整个用户区。两种事务形态都
  可用：先写 2 字节大端指针再读，或者 combined（repeated START）。
- 备份 872 字节用户区（`0x0010` 到 `0x0377`）。
- 解析 NDEF（带前缀码的 URI、文本和 MIME 记录）。
- 向出厂未上锁的页面写入，即 handoff 令牌区（约 `0x0070` 到
  `0x008D`）。这恰好是荣耀软件需要轮换的全部内容，也解释了"半可写"
  的内存图。
- 通过锁定位识别确切的芯片版本，见下文。

## 在我的机器上失败的部分

我做实验的机器是 MagicBook 14 Pro 2023，**"Only for China Mainland
（仅中国大陆版）"**：RTX 3050、i5、16 GB + 1 TB，SKU `GLO-G561`
（DMI 产品名 `GLO-GXXX`）。在这台机器上：

- **所有针对 NDEF 区的 I²C 写入都被 NAK。** 字节级一致的出厂数据、
  从厂商驱动抄来的 16 字节块形态、手机在射频侧持续读标签的条件下
  150+ 次尝试。毫无作用，总线报错 121。
- 配置寄存器（`0x0388` dyVal、`0x038C` fdMode）的写入同样 NAK。
- **手机侧写入同样失败。** Android 的 NFC Tools 能正常读标签
  （报 Type A 和 ISO-DEP，即 Type 4 标签），并显示 "WRITABLE: YES"，
  这是假的。每次写入都以 write error 告终。
- iPhone 完全无视这张标签。
- 走 Windows 驱动也没有用：我逆向了 `SpbNfcDriver.sys`，其写入
  分发器（Ghidra 里的 `0x140003160`）对写入数据做 SHA-256 校验。
  四个内置摘要全部破解：它们是出厂 URL 分片的 SHA-256（`www.hihon`、
  `or.com/global/su`、`pport/magic_link`、`_welcome/`）。也就是说，
  驱动只放行出厂 URL，即使芯片肯收，它也写不出别的地址。
- 追错数据手册浪费一天：FM11NT021 是 144 字节的纯射频 Type 2 标签，
  没有 I²C。虽然论坛帖子和 ACPI 字符串都往它身上引，但它不是这颗芯片。
- 连续写入尝试后芯片会卡住一段时间，连读都 NAK，10 到 30 秒后自行
  恢复。工具因此做了等待和重试。
- 一个副作用：我的探针写入把一个 `0x58` 字节打进了出厂 URL，手机现在
  读到的是
  `https://www.hihonor.com/global/suXport/magic_link_welcome/`。
  除了这一页，其他全部从备份恢复了，因为突发写入之后那一页不再接受
  写入。重启之后 `python3 nfc_tool.py restore` 或许能修好。对我来说
  损失不大，替换这个 URL 本来就是目标。

## 结论，以及你的机器为什么可能不一样

FM11NT082C 数据手册的订购表里写着答案：

| 型号后缀 | 出厂静态锁定位 | 用户区 I²C 写入 |
|---|---|---|
| **NTCZ** | `0000h` | 可写 |
| **NTCI** | `FFFFh` | 永远 NAK |

锁定位是一次性可编程（OTP）：只能设置，不能清除，它的意义就在于此。
看起来荣耀笔记本里两种版本都有，具体是哪一种可能取决于机型或批次。

我的锁定位（`0x000A`）读到 `ff ff`，也就是 NTCI：
这些位在出厂时已设置，无法通过软件清除。

[ZhuRuoLing/magicbookartd](https://github.com/ZhuRuoLing/magicbookartd)
为 MagicBook Art 14 实现了 `nfc write-url`，据作者说在那台机器上
可以正常写入。其文档中芯片型号为 FM11NT082C-NTCZ。本仓库的工具实现了同样的安全写入流程，如果你的
`check` 显示 NTCZ，很有希望直接能用。

## 快速上手

```sh
sudo modprobe i2c-dev
python3 nfc_tool.py check                  # 版本识别 + 当前 NDEF
python3 nfc_tool.py read --out backup.bin  # 转储 872 字节用户区
```

写入（仅限 NTCZ）：

```sh
python3 nfc_tool.py write-url https://example.com --backup before.bin
python3 nfc_tool.py write-text "你好" --lang zh
python3 nfc_tool.py restore before.bin     # 回滚
```

写入流程很谨慎：校验 CC、备份、TLV 长度清零、重写发生变化的 16 字节
块、最后写第一个块以发布、全量校验。运行任何命令期间请让手机远离
触摸板。

如果你和我一样是 NTCI，又想在拍一拍笔记本时打开自己的网址，现实的
答案是：一枚几毛钱的 NTAG213 贴纸，贴在触摸板正中央。用任何 Android
的 NFC Tools 写，或者用廉价 USB 读卡器从这台笔记本写。它和内置标签
共存没有问题，手机会读离线圈更近的那一个。

## 兼容性

| 机型 | 芯片 | 版本 | 自定义 NDEF 写入 |
|---|---|---|---|
| HONOR MagicBook 14 Pro 2023（仅中国大陆版，GLO-G561） | FM11NT082C | NTCI | 否，硬件 OTP |
| HONOR MagicBook Art 14 (MRA-XXX) | FM11NT082C | NTCZ | 是，见 magicbookartd |
| 其他机型 | ? | 运行 `check` | ? |

## 协议备忘

完整的内存映射、事务形态和 Windows 驱动分析在
[docs/PROTOCOL.md](docs/PROTOCOL.md) 和
[docs/RESEARCH.md](docs/RESEARCH.md)（俄文）。简版：

- 用户区 `0x0010` 到 `0x0377`，872 字节，NDEF TLV `03 8c` 位于
  `0x0010`。
- `0x000C` 处的 CC 必须读到 `E1 10 6D 00`，这也是确认总线上对话对象
  的方法。同一总线上还挂着 `0x2B` 的 HID 设备，别碰它。
- 静态锁定位在 `0x000A`，寄存器从 `0x0388` 开始，fdMode 字节在
  `0x038C`，出厂值 `06`。
- 写入是单事务 `[addr_hi, addr_lo]` 加最多 16 字节，16 字节对齐。
  数据手册标称编程时间 10 ms，工具等 20 ms 并回读。失败的写入绝不
  盲目重试。

## 致谢

- [ZhuRuoLing/magicbookartd](https://github.com/ZhuRuoLing/magicbookartd)，
  MagicBook Art 14 的 Rust 守护进程。它的驱动逆向与我的逐字节一致，
  也正是它让我注意到 NTCZ 与 NTCI 的区别。
- FM11NT082C 数据手册（中文）和 FM11NC08 产品简介（英文），
  由[上海复旦微电子](https://www.fmsh.com/)发布。

## 许可证

MIT，见 [LICENSE](LICENSE)。

## 搜索关键词

FM11NT082C，FM11NC08，FM11NT021，NTAG0001，Fudan，复旦微，上海复旦微电子，
Honor MagicBook NFC，MagicBook 14 Pro，MagicBook Art 14，NTCI，NTCZ，
静态锁定位，OTP 烧锁，I2C 写入 NDEF，errno 121 Remote I/O error，
SpbNfcDriver.sys，nfc_midware.dll，一碰传，Magic Link。
