# pdfinbox — 伪装成系统打印机的 PDF 收件箱

给"网页应用不提供保存/导出按钮"的场景用：按 `⌘P` → 选这台"打印机" → PDF 直接落到指定目录，
文件名按模板自动生成。

零第三方依赖（只要 macOS 自带 `/usr/bin/python3`）。

---

## 1. 选型：为什么不是 CUPS backend

| 方案 | 做法 | 在 macOS 15 的结论 |
|---|---|---|
| A. CUPS backend | 往 `/usr/libexec/cups/backend/` 放脚本，`lpadmin -v mybackend:/` | ❌ 不可靠。CUPS 对自建 backend 的任务转交在近年版本反复失效（RWTS PDFWriter 在 Sequoia 上直接产出 0 字节），且 backend 以 `_lp` 身份运行，写用户目录要放 777 |
| B. `file:` backend + 目录监听 | `-v file://...` + Folder Action 转换 | ❌ macOS 不再支持 raw 队列（`lpadmin` 直接报"macOS 不再支持原始队列"），且要装 Ghostscript、配 sudoers、轮询冲突 |
| **C. 自建 IPP 打印机（本方案）** | 一个最小 IPP 服务器，`lpadmin -v ipp://127.0.0.1:8631/printers/<队列> -m everywhere` | ✅ 实测通过。cupsd 把 PDF 原样转来，字节级一致 |

关键点：**macOS 的打印管线天生基于 PDF**。应用 → CoreGraphics 生成 PDF → cupsd → backend。
我们在链条末端说"我支持 `application/pdf`"，CUPS 就不做任何格式转换，PDF 直通到我们手里。
不需要 Ghostscript，不需要 PostScript。

实测（macOS 15.2 / CUPS 2.3.4 / arm64）：

```
$ md5 -q sample.pdf
2d0f6ae761ca8843a19f7ff0df9d52b5
$ md5 -q "PDFInbox/网页报表 2026Q3.pdf"
2d0f6ae761ca8843a19f7ff0df9d52b5     # 字节级无损
```

---

## 2. 架构

```
网页应用 ⌘P
   ↓  macOS 打印对话框：选 "PDF Inbox"
cupsd（系统 spooler，PDF 直通，不做转换）
   ↓  ipp://127.0.0.1:8631/printers/<队列名>
pdfinbox（单文件 IPP 服务器，用户态，无 root）
   ├─ 解析 IPP 属性：job-name / job-originating-user-name / job-id …
   ├─ 队列名 = 路由键 → 选目录 + 选模板
   ├─ 渲染文件名（清洗非法字符、冲突加 -2、长度截断）
   └─ 原子落盘 + 追加 .pdfinbox-index.jsonl 索引
```

**队列名即路由键**：IPC 里唯一可靠的"你从哪台虚拟打印机打的"信息，
所以注册多个队列（如 `PDFInbox-Invoice`）就能把发票、报表分流到不同目录、用不同命名规则——
在打印对话框里表现为多台打印机。

---

## 3. 文件名模板

```
{now:%Y%m%d-%H%M%S}_{title}       → 20261113-091205_季度报表 Q3-2026.pdf
{date}_{seq}_{title}              → 2026-11-13_003_增值税专用发票.pdf
{now:%Y%m}_{title}                → 2026-11_机票行程单.pdf
```

| 变量 | 来源 | 说明 |
|---|---|---|
| `{title}` | IPP `job-name` | 浏览器下通常是页面标题；缺失时 `Untitled` |
| `{now:格式}` | 本地时间 | 任意 strftime 格式 |
| `{date}` `{time}` `{datetime}` | 本地时间 | 常用组合 |
| `{seq}` | 本地状态 | **当日递增序号**，同一网页反复打印时天然排重 |
| `{job}` `{queue}` `{user}` `{host}` | IPP 属性 | 审计用 |
| `{pages}` | 解析 PDF | 粗算，不是精确值 |
| `{fmt}` `{uuid}` | 本地 | 格式 / 随机后缀 |

自动处理：`/ : * ? " < > |` 和控制字符 → `-`；连续分隔符合并；结尾 `.` 空格剥离；超 200 字符截断；
同名文件自动 `-2`、`-3`。

**IPP 拿不到 URL 和应用名**，`{url}`/`{app}` 会渲染为空。要 URL 只能另配浏览器扩展或在打印瞬间用
AppleScript 读前台窗口标题——属于可选增强，不在本方案内。

---

## 4. 用法

```bash
# 安装（不需要 sudo）
./bin/install.sh

# 自检
/usr/bin/python3 bin/pdfinbox.py doctor

# 预览模板效果（不打印）
/usr/bin/python3 bin/pdfinbox.py render --title '发票/2026:11'

# 卸载（PDF 与配置保留）
./bin/install.sh --uninstall
```

配置文件 `~/.config/pdfinbox/config.json`（见 `config.example.json`）。
`post_command` 挂后处理钩子，收到环境变量 `PDFINBOX_PATH` / `PDFINBOX_JSON`，可接 OCR、压缩、入库。

---

## 5. 踩坑清单（都是实测撞出来的）

1. **`resolution` 值是 9 字节不是 8**
   `printer-resolution-supported` = 横向(4)+纵向(4)+**单位(1)**。漏掉单位字节，
   libcups 解析整包失败，报 `lpadmin：无法查询打印机：No such file or directory`，
   backend 进入 10 秒重试循环、作业永远卡在"正在打印"。这是最隐蔽的一个坑——
   错误信息完全不指向真因，只能靠逐属性二分定位。

2. **macOS 不再支持 raw 队列**
   `lpadmin -m raw` 直接报"macOS 不再支持原始队列"。必须 `-m everywhere` 或自定义 PPD。

3. **`-m everywhere` 会真的来查你的 IPP 属性**
   属性不全就不会报"缺哪个"，只给一句 ENOENT。要么补齐 IPP Everywhere 必需集
   （`printer-uuid`/`media-supported`/`sides-supported`/`print-color-mode-*`/`printer-resolution-*`
   /`ipp-features-supported`），要么改用 `-P xxx.ppd`。

4. **队列名不能从 URL path 判断**
   libcups 常把请求 POST 到 `/`（根路径），队列只在 `printer-uri` **属性**里。
   必须以 `printer-uri` 为准（本实现已修）。

5. **响应版本不能降级**
   请求是 `HTTP/1.1`，响应回 `HTTP/1.0` 会被 libcups 判死。反过来也不行：要回显客户端 IPP 版本
   与 `attributes-natural-language`（系统语言是 `zh-cn`，写死 `en` 会出问题）。

6. **`printer-state` 必须回 enum 3(idle) + `printer-is-accepting-jobs: true`**
   否则 backend 认为打印机停机，每 10 秒轮询一次，永不发作业。

7. 调试手段：`PDFINBOX_DEBUG=1`（打印请求路由）、`PDFINBOX_DUMP=1`（打印原始字节）、
   `PDFINBOX_ONLY=属性名,...`（只回指定属性，做二分定位）、`bin/proxy.py`（TCP 代理抓 cupsd 双向字节流）。

---

## 6. 已知边界

- 服务必须常驻（launchd `KeepAlive`）；挂掉时打印作业会排队等待，不会丢失。
- 只监听 `127.0.0.1`，不对局域网暴露——IPP 提交默认免鉴权，暴露到网络等于别人能往你磁盘写文件。
- 打印对话框里的选项（纸张/双面/彩色）目前只是装饰，不影响输出的 PDF；
  若需要"按 A4 裁剪""强制双面"等，得在 `post_command` 里做，或引入 Ghostscript。
- 浏览器 `⌘P` 时 `job-name` 是否等于页面标题，取决于浏览器实现，本机未做 GUI 验证。
