# acomp — 声明式音频后期编译器

> 给人用的音频软件（Audacity、Au、剪映）把状态藏在界面里：靠眼睛看波形、靠耳朵听。
> **AI 没有这条通道。** acomp 把状态换成一份 YAML，把反馈换成数字。

```
写 IR (project.yml)  →  acomp render  →  产物 + 测量报告（数字 / PASS-FAIL）
      ↑                                              │
      └────────── 读报告，改一行，再来一次 ────────────┘
```

一句话：**给音频后期做一套编译器**——IR 是源码，render 是编译，报告是编译器的诊断信息。
音频库、有声书、播客、课件，只要是一堆片段拼成一条音轨，都归它管。

---

## 一键试试

不装任何东西、不需要手动配置 ffmpeg：

```bash
# 国内网络建议用 Gitee 镜像；能直连 GitHub 的用下面的 GitHub 地址，两者内容一致
git clone https://gitee.com/dusknook/acomp.git  && cd acomp
git clone https://github.com/dusknook/acomp.git && cd acomp

python demo.py          # Windows 也可以直接双击 demo.bat；macOS/Linux 用 ./demo.sh
```

> 不想 clone 也行：直接下载源码包解压，效果一样。

它会自动建 `.venv`、装依赖，然后跑完整个闭环：

```
造 3 段合成测试音（S02 故意过响）
  → validate  只校验 IR，不写盘
  → plan      dry-run，预判目标能不能达到
  → render    过响版：S02 报 FAIL，退出码 1
  → render    修好版：全部 PASS，退出码 0
  → diff      两份报告的语义级差异
```

整个过程大约十几秒。跑完你会看到「一个不达标的音频是怎么被定位、被改掉、被复验」的全过程。

---

## 它长什么样

`render` 打印出来的就是验收结果，没有波形图、没有「你听一下」：

```
ID        DUR(s)      PEAK(dB)   RMS(dB)    LEAD      TRAIL     RESULT   FAILED TARGETS
--------  ----------  ---------  ---------  --------  --------  -------  --------------------------
S01       9.00        -16.99     -20.51     0.00      1.00      PASS
S02       9.00        -0.91      -4.44      0.00      1.00      FAIL     peak_dbfs,rms_dbfs
S03       8.00        -16.99     -20.00     0.00      0.00      PASS

MASTER    25.20       -0.91      -8.84      0.00      0.00      FAIL     peak_dbfs,rms_dbfs

结果：2/3 段通过，master FAIL，整体 NEEDS FIX
```

同一份结果还有机器可读版本（`out/report.json`，或 `render --json` 直接打 stdout），
退出码是有语义的：

| 退出码 | 含义 |
|---|---|
| `0` | 全部达标 |
| `1` | 构建成功，但有 FAIL（**不达标 ≠ 失败**：产物在，只是数字不过关） |
| `2` | 出错。stderr 是结构化 JSON：`code / what_failed / minimal_fix / retryable` |

出错时不需要人去猜。举一个真实的错误对象：

```json
{
  "code": "SRC_MISSING",
  "what_failed": "段落 S02 的源文件不存在：/work/raw/S02.wav",
  "minimal_fix": "把文件放到该路径，或改 segments[1].src",
  "retryable": false
}
```

`minimal_fix` 写的是「把 X 改成 Y」，不是「请检查配置」——这是给 AI 读的，它得知道下一刀切在哪。

---

## 它和手搓 ffmpeg 脚本的区别

| | 手搓 ffmpeg 脚本 | acomp |
|---|---|---|
| 状态 | 散在命令历史和 shell 脚本里 | 一份 YAML，可进 git、可 review |
| 反馈 | 自己听 / 自己拼表格 | 每次 render 自动出 PASS/FAIL 表 |
| 改一处 | 重跑全部 | 段落级缓存，只重建变了的 |
| 改完对比 | 凭记忆 | `acomp diff` 给出语义级变化 |
| 出错 | exit code + stderr 天书 | JSON：code / what_failed / minimal_fix / retryable |
| 批量 | 写循环拼命令 | IR 就是数据，程序生成即可 |

最后一行是关键：**IR 是数据，所以可以程序生成。**
`tools/batch_fix.py` 就是例子——它测一遍目录里每个文件，算出该补多少增益、该裁多少静音，
然后生成 IR 交给 render。全程没有人打开过音频编辑器。

---

## 命令

| 命令 | 副作用 | 说明 |
|---|---|---|
| `init <name>` | 生成模板 yml | 起手用 |
| `validate <file>` | 无 | 只查 IR 是否合规，输出 errors / warnings |
| `plan <file>` | 无 | 探测素材、预估时长、**判定目标是否可达** |
| `render <file>` | 写 out_dir | 构建 master / mp3 / report.json |
| `probe <file>` | 无 | 测单个音频，纯数字输出（含 LUFS / 真峰值 / crest） |
| `diff <a> <b>` | 无 | 两份 IR 或两份 report 的**语义** diff |

`render` 常用参数：

- `--json` 输出 JSON 而非表格（喂给程序时用）
- `--force` 忽略缓存全部重建
- `--out DIR` 覆盖 `project.out_dir`
- `--exit-zero` 有 FAIL 也返回 0
- `--asr` 对写了 `expect_text` 的段落跑转写对齐（可选，需要 faster-whisper）

`validate` / `plan` / `probe` 全都不写盘，可以放心让 AI 先预演再动手。

---

## IR 长什么样

```yaml
version: 1

project:
  name: chapter_01
  out_dir: ./out

audio:
  sample_rate: 44100
  channels: 2

measure:
  silence_threshold_db: -50.0      # 低于此电平视为静音

targets:                           # 逐项打 PASS/FAIL；统一 [min, max]，任一端可 null
  total_duration: [300.0, 420.0]
  peak_dbfs: [null, -3.0]
  true_peak_dbfs: [null, -3.0]     # 真峰值，交付验收口径
  rms_dbfs: [-23.0, -18.0]
  integrated_lufs: [-23.0, -16.0]  # 平台按 LUFS 验收时才写
  lead_silence: [null, 0.25]
  trail_silence: [null, 0.25]
  segment_checks: [peak_dbfs, rms_dbfs]   # 段落级查哪几项

segments:
  - id: S01
    src: raw/S01.wav
    trim: {start: 0.0, end: null}  # 绝对时间戳，不是「裁多少秒」
    gain_db: 0.0
    fade_in: 0.0
    fade_out: 0.0
    pad_before: 0.0
    pad_after: 1.0                 # 段尾留 1s 静音
    crossfade_next: 0.4            # 与下一段交叉淡化
    enabled: true
    expect_text: "..."             # 可选，配合 --asr 做转写对齐

output:
  master: chapter_01_master.wav
  master_gain_db: 2.0              # 拼接之后的整体增益
  limiter:
    enabled: true
    limit_db: -3.2
    attack: 5                      # ms
    release: 50                    # ms
  mp3:
    bitrate: 192k
    sample_rate: 44100
    joint_stereo: true
```

路径全部相对 IR 文件所在目录解析，所以整个工程可以整体搬走。

**`segment_checks` 的三种写法**（决定段落级查什么）：

- 不写 → 默认只查电平（`peak_dbfs` + `rms_dbfs`）
- 写非空列表 → 严格按列表查
- 写 `[]` → 关闭所有段落级检查

为什么默认不查段落首尾静音？因为段落之间本来就要靠 `pad_after` 留白，
静音是**整章（master）层面**的要求，拿它卡每一段没有意义。

---

## 一个必须知道的算术：限幅

「Peak ≤ -3 dBFS」和「RMS ≥ -23 dBFS」这两个要求合起来，
等价于 **峰均比 crest = peak − rms ≤ 20 dB**。

素材的 crest 是内容决定的；**调增益只能整体平移，改不了 crest**。
所以当 crest > 20 dB 时，**任何纯增益方案在数学上都不可能同时满足这两条**——
必须压峰值（限幅/压缩）。这跟母带审美无关，是算术。

`acomp plan` 会在 render 之前就做这道判定：测每段 trim 之后的真实内容窗口，
算出 master 的 crest，直接告诉你「这个目标只能靠限幅达成」，并给出建议的增益和限幅阈值。

```bash
python acomp.py probe <file>     # 看 crest：peak 减 rms 超过 20 就别指望调增益能过
python acomp.py plan  proj.yml   # 动手之前先问「能不能到」
```

### 两个会骗人的口径

1. **采样峰值 ≠ 真峰值。** mp3 编码后，采样点之间的真实波形峰值会高 0.2~0.4 dB。
   用 astats 判「peak −3.24 达标」，ebur128 一测真峰值是 −2.9 —— 其实超了。
   所以限幅阈值要按真峰值倒推，不能只给采样峰值留余量。
2. **RMS ≠ LUFS。** 同一段音频 RMS −22.4、LUFS 可能是 −17.8，差 4 dB 以上。
   平台按哪个验收，就在 `targets` 里写哪个，别拿 RMS 当 LUFS 用。

---

## 批量体检一个目录

```bash
# 只体检：打印每个文件的电平/静音/需要补多少增益，不写任何音频
python tools/batch_fix.py --src /path/to/delivery_package

# 体检 + 渲染修复版 + 按交付口径复检
python tools/batch_fix.py --src /path/to/delivery_package --render

# 换规格
python tools/batch_fix.py --src audio/ --target-rms -19 --limit-db -2.0 --max-edge 0.2
```

它会输出一张表：每个文件的 peak / rms / crest / 首尾静音 / 建议增益 / 是否需要裁剪，
然后生成对应的 IR（放在 `tools/_gen_*.yml`），渲染到 `tools/_out/<文件名>/`。
源目录始终只读。

---

## 设计取向

1. **状态是文本。** 一切可 diff、可版本化、可被程序批量生成。没有二进制工程文件。
2. **render 是纯函数。** 同样的 IR + 同样的源 → 逐字节一致的输出。
3. **反馈是数字。** peak / rms / duration / 首尾静音 / 真峰值 / LUFS，没有波形图、没有「你听一下」。
4. **错误要可执行。** `minimal_fix` 写的是「把 X 改成 Y」，不是「请检查配置」。
5. **先预演后动手。** `validate` / `plan` 都不写盘，render 之前先跑一遍零成本。
6. **能缓存就缓存。** 段落指纹 = IR 参数 + 源文件 mtime/size，没变就不重建。

---

## 安装

推荐直接 `python demo.py`（自动处理一切）。手动装也行：

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt      # Windows: .venv\Scripts\pip
```

ffmpeg 的查找顺序：环境变量 `ACOMP_FFMPEG` → `imageio_ffmpeg` 内置 → PATH。
**不需要 ffprobe**（时长/采样率/声道都从 ffmpeg 自己的输出里解析）。

Windows 下可以用 `acomp.bat` 代替 `python acomp.py`，它会优先调项目自带的 `.venv`。

也可以当命令装：

```bash
pip install -e .
acomp render project.yml
```

---

## 已知限制

- 只做非线性编辑里最常见的几种操作：裁剪 / 增益 / 淡入淡出 / 静音填充 / 交叉淡化。
  降噪、EQ、压缩这类**没进 IR**——它们参数空间太大，暂时建议先交给 ffmpeg 滤镜字符串。
- 真峰值是每段素材实测（ebur128），不是经验公式估算；所以 `plan` 会稍微慢一点，
  想跳过可以加 `--no-feasibility`。
- ASR 对齐依赖 `faster-whisper`，没装就静默跳过，不影响主流程。
- `acrossfade` 要求交叉淡化时长小于相邻两段时长，极端短段落会被 ffmpeg 拒绝（错误会原样带出来）。
- 目前只支持音频；视频容器、多轨混音不在范围内。

---

## 目录

```
acomp.py            命令行入口（python acomp.py <cmd>）
demo.py             一键演示：建环境 + 造测试音 + 跑完整闭环
acomp/
  cli.py            命令行：init / validate / plan / render / probe / diff
  ir.py             IR 定义、解析、校验（含 segment 编号与指纹）
  audio.py          ffmpeg 封装：探测 / 构建段落 / 拼接 / 编码
  measure.py        测量：duration / peak / rms / LUFS / 真峰值 / 首尾静音
  feasibility.py    可达性预测：render 之前判定目标是否可能同时满足
  report.py         PASS/FAIL 判定与表格输出
  diff.py           语义 diff
  errors.py         结构化错误（code / what_failed / minimal_fix / retryable）
examples/
  demo.yml          示例工程（S02 故意过响，用来演示 FAIL → 改一行 → PASS）
  make_demo_audio.py  生成 3 段合成测试音
tools/
  batch_fix.py      批量体检 / 修复一个目录（程序生成 IR，源文件只读）
```

---

## License

MIT —— 可商用、可修改、可再分发，保留版权声明即可。见 [LICENSE](LICENSE)。

欢迎提 issue 和 PR。如果 acomp 帮到了你，把你的 IR 例子发上来，比点赞有用。
