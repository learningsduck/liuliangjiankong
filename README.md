# VPS 流量汇总（Windows 桌面版 / exe）

搬瓦工走 **KiwiVM API**，其它 Linux 走 **SSH + vnstat**（`vnstat --json m`）。支持 GUI 内新增/编辑/删除服务器，无需手改 yaml。

## 本机运行（开发）

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy servers.example.yaml servers.yaml
python app_gui.py
```

## 打包成 exe

双击或在命令行执行 **`build_exe.bat`**（需已安装 **Python 3.10+**）。

生成：**`dist\VPS流量汇总.exe`**。把 **`servers.yaml`** 与 exe 放在同一文件夹即可带走使用。

若杀毒软件误报，可对 dist 目录加白名单或改用 `pyinstaller` 的 `--onedir` 模式。

## 配置说明

- GUI 顶部支持：**刷新（仅当前选中 ID）/ 全部刷新 / 新增 / 编辑 / 删除 / 测试连接 / 行上移·行下移（调列表顺序）/ 自动刷新（1/5/10 分钟，定时为全部刷新）**。
- **列表行顺序**：先选中一行，再用顶部 **「行上移 / 行下移」** 调整服务器在列表（及 **`servers.yaml`**）中的先后顺序；不会重新拉取流量。若界面没有这些按钮，请 **`git pull`** 后重新运行 **`python app_gui.py`**，或重新执行 **`build_exe.bat`** 生成新版 exe。
- **GB 进制**：在 **新增/编辑** 对话框里为 **每台服务器** 选择 **「GB: 1024³」** 或 **「GB: 1000³」**，写入 `servers.yaml` 该项的 **`gb_base`**（`1024` 或 `1000`）；影响列表「已用/套餐」及「月流量上限 / 面板已用」的 GB 与字节换算。未写时默认 **1024**。
- **`type: bandwagon`**：`veid`、`api_key`（KiwiVM 面板）。
- **`type: ssh_vnstat`**：`host`、`private_key_path`（Windows 路径用双反斜杠或引号）、`interface`（如 eth0/ens3）。**月流量上限在界面里按 GB 填写**；保存到 `servers.yaml` 时写入 **`monthly_quota_bytes`**（按当前所选 GB 进制换算）。
- 各机 vnstat 计费月对齐：在服务器 `/etc/vnstat.conf` 设置 **`MonthRotate`**。

### 「已用」如何与 vmrack 面板 / Windows 对齐

- **本软件「已用」**：来自远端 **`vnstat`（或搬瓦工 API）的原始字节数**，加上配置里的 **`used_offset_bytes`**（由你在编辑里对齐面板时自动写入），再除以你选的 **1024³ 或 1000³** 显示成 GB。
- **vmrack 面板**：可能按 **出网**、**多 IP**、或 **1000/1024 展示** 与网卡总流量略有差异；若 GB 小数对不上，先切换 **GB 进制** 看是否接近，再看是否同一计费周期（`MonthRotate`）与同一网卡名。
- **占比**：始终用 **字节/字节**（`已用字节 ÷ 套餐字节`），与 GB 进制开关无关；套餐字节由你填的「月流量上限(GB)」换算而来。

### 用「面板已用」与 vnstat / API 对齐（常见：vnstat 装得晚或口径不一致）

1. 在主页点 **「刷新」**，确保该行已成功拉取流量。  
2. **编辑** 该服务器，在 **「面板已用(GB)」** 填入商家面板上的 **本月已用** 数字，点 **保存**。  
3. 程序会写入锚点：**`panel_anchor_used_bytes`**（面板已用）与 **`panel_anchor_raw_bytes`**（当时原始已用），主页按 **`锚点面板值 + 原始增量`** 显示；之后 vnstat 继续涨，已用会跟着涨。  
4. 面板数字更新后，重复 **刷新 → 编辑 → 改面板已用 → 保存** 即可再次对齐。**留空「面板已用」并保存** 会去掉对齐（删除 `used_offset_bytes`），恢复仅显示原始统计。

### 按重置日自动清掉「面板对齐」（新计费周期）

若配置了 **`billing_reset_day`**（与界面「距离重置日」一致），程序在每次 **刷新** 时会根据当前日期判断是否已进入 **新的计费周期**。一旦进入新周期，会自动删除上一周期留下的 **`used_offset_bytes`**、**`panel_anchor_used_bytes`**、**`panel_anchor_raw_bytes`**，并把 **`billing_period_id`** 更新为当前周期标识；这样 **「已用」会重新跟 vnstat/API 的原始统计走**，避免旧对齐把数字抬在新周期上。

- 首次出现该逻辑时只会写入 **`billing_period_id`**，**不会**清空你已有的对齐数据。
- **vnstat 侧**：请仍让 VPS 上 **`/etc/vnstat.conf` 的 `MonthRotate`** 与 **`billing_reset_day`** 一致，否则跨重置日后 vnstat 的「当月」可能仍按自然月，与商家计费月不一致；本功能只负责清面板锚点，不改变 vnstat 自身口径。

- **日均 / 预计用量**：界面里「已用天数」按 **`billing_reset_day`** 算，已进入新计费周期；若 vnstat 仍返回**上一周期的大累计**，会出现「已用」仍上千 GB、天数只有 3 天 → 日均/预计异常放大。程序在**跨周期**时会置 **`billing_cycle_needs_baseline`**；**下一次成功拉取**时：若当前逻辑已用 **不超过套餐字节**（视为 vnstat 已是本周期合理累计，含你**过数日才刷新**的情况），则**不写基线**，界面直接显示该累计；若仍异常大于套餐，则写入 **`billing_cycle_baseline_used_bytes`** 并从 **0** 起按增量显示（适配 vnstat 未按计费月切分时）。仍建议把远端 **`MonthRotate`** 配成与 **`billing_reset_day`** 一致。

- **已用（主界面）与「晚很多天才刷新」**：配置了 **`billing_reset_day`** 时，除按月 JSON 外，程序会再执行一次 **`vnstat --json d --begin 本账期起点 --end 今天`**，把**该日期区间内各天 rx+tx 相加**作为「已用」的主要来源（备注里会显示 ``日账期``）。这样即使重置日过去很多天才刷新，也能对齐**本账期自然日累计**（仍受远端 **`DailyDays`** 等保留策略限制；日数据被裁掉时自动回退按月 JSON）。**未配置** ``billing_reset_day`` 时行为与旧版相同，只用按月数据。

## 系统要求

- Windows 10/11 x64  
- 目标 VPS 已安装 **vnstat 2.x**

## 仓库与首次推送到 GitHub

远程地址：**https://github.com/learningsduck/liuliangjiankong.git**

若代码已在某台 Linux 上提交但尚未推送，可在该机器上（勿把 token 写进仓库）：

```bash
export GITHUB_TOKEN='你的_Personal_Access_Token'
bash scripts/push-to-github.sh
```

在 **Windows** 上拉取（推送成功后）：

```powershell
git clone https://github.com/learningsduck/liuliangjiankong.git
cd liuliangjiankong
```

GitHub Token：Settings → Developer settings → Personal access tokens（**classic** 勾选 `repo`；或 fine-grained 对该仓库给 Contents 写权限）。
