# RoboFit 打分平台

双 RGB 相机数据的**动作分段标注与评分**平台。与采集/三维重建系统完全解耦:
只读 `data/` 目录,标注与缓存全部写在自己目录内,可独立部署到任意机器
(Linux / Windows / macOS)。

## 目录结构

把 `scoring_platform/` 放到任意空工作目录下,`data/` 与其同级即可:

```
工作目录/                     ← 任意位置,拉取后自行创建
├── scoring_platform/        ← 本平台(克隆或拷贝整个目录)
│   ├── backend.py           # FastAPI 后端(数据预览 + 标注接口)
│   ├── frontend/            # 原生 HTML/CSS/JS 前端,无需 npm
│   ├── docs/
│   │   ├── part_sets.json   # 动作序列表(48 被试 × 8 动作,运行时唯一数据源)
│   │   └── architecture.md  # 数据管线技术架构文档
│   ├── run.py               # 三平台统一启动器
│   ├── run.sh / run.bat / run.command
│   ├── requirements.txt     # 运行时依赖
│   ├── requirements-dev.txt # 测试依赖(可选)
│   ├── tests/               # 接口测试(可选)
│   ├── cache/               # 自动生成:预览 MP4 缓存
│   └── annotations/         # 自动生成:score_annotation.json(标注结果)
└── data/                    ← 与 scoring_platform 同级,自行导入采集数据
    └── <日期>/<被试编号>/
        ├── timestamps/cam1_rgbd_timestamps.csv
        ├── timestamps/cam2_rgbd_timestamps.csv
        └── color/cam*_color_rgb_<帧号>_<微秒>us.raw
```

## 快速开始

### 1. 获取代码

```bash
mkdir ~/robo_fit_workspace && cd ~/robo_fit_workspace
git clone <仓库地址> scoring_platform        # 或直接把 scoring_platform 目录拷贝过来
mkdir data                                   # 与 scoring_platform 同级
```

### 2. 安装环境

**通用方式(推荐,三平台一致)**,需要 Python 3.10+:

```bash
cd scoring_platform
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Linux 上如已有 conda 环境 env_robfit**(含 fastapi/numpy/opencv/ffmpeg),可直接:

```bash
bash scoring_platform/run.sh
```

**FFmpeg**(仅"生成流畅预览 MP4"功能需要;标注与逐帧预览不需要):

- Linux: `sudo apt install ffmpeg` / `conda install -c conda-forge ffmpeg`
- macOS: `brew install ffmpeg`
- Windows: `winget install ffmpeg`,或从 ffmpeg.org 下载后把 ffmpeg.exe 放到 Python 同目录

### 3. 导入数据

把每个被试的采集目录按 `data/<日期>/<被试编号>/` 结构放入 `data/`。平台需要:

| 文件 | 说明 |
|---|---|
| `timestamps/cam1_rgbd_timestamps.csv` | 每帧一行,含 `frame_index`、`timestamp`(主机 wall-clock 秒)、`color_width`、`color_height`、`color_raw_path`(相对被试目录) |
| `timestamps/cam2_rgbd_timestamps.csv` | 同上,第二路相机 |
| `color/cam*_color_rgb_<帧号>_<微秒>us.raw` | RGB888,640×480×3 字节/帧 |

两路相机都齐全的被试才会出现在列表中。默认读取 `../data`(即 `scoring_platform/` 的上级目录下的 `data/`),也可用 `SCORING_DATA_ROOT` 指向任意位置。

> 数据在哪台机器,服务就建议跑在哪台机器(单被试 raw 帧约 72GB,不适合跨网络读取)。

### 4. 启动

```bash
# Linux / macOS
python3 run.py
# Windows
run.bat
# 或三平台统一
python run.py
```

启动器会自动检查依赖、提示 FFmpeg 状态,启动服务后**自动打开浏览器**
(默认 <http://127.0.0.1:8010>;`SCORING_NO_BROWSER=1` 可禁用)。

### 5. 使用流程

1. 左侧被试列表点 **「录入」** → 边栏切换为该被试的标注视图
2. 11 张固定动作卡片按顺序排列(3 张热身 warm_up1-3 + 8 个动作代码,顺序来自 `docs/part_sets.json`)
3. 播放视频,播放中直接点卡片上的 **Set Start / Set End** 打点(不中断播放);**空格 = 播放/暂停**
4. 评分卡片输入 0-5 整数分(可留空),点 **保存**;**清除** 只清空该卡片的录入,卡片保留
5. 左上角 **← 退出标注** 返回列表,继续下一个被试

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SCORING_HOST` | 127.0.0.1 | 监听地址(远程访问可设 0.0.0.0) |
| `SCORING_PORT` | 8010 | 端口 |
| `SCORING_DATA_ROOT` | `../data` | 采集数据目录(只读) |
| `SCORING_CACHE_ROOT` | `cache/` | 预览 MP4 缓存位置 |
| `SCORING_ANNOTATION_ROOT` | `annotations/` | 标注文件所在目录 |
| `SCORING_PART_SETS_PATH` | `docs/part_sets.json` | 动作序列表位置(改动后重启生效) |
| `SCORING_PREVIEW_FPS` | 30 | 预览 MP4 帧率 |
| `SCORING_NO_BROWSER` | 未设置 | 设为 1 禁止自动打开浏览器 |

## 数据与标注文件

- **`annotations/score_annotation.json`**:标注结果。**首次启动服务时自动生成**(扫描 data 目录,把表格中的被试录入为 11 张空卡片;幂等,不覆盖已有标注);之后保存/改分/清除实时写入。每个边界保存双相机各自最近的原始帧号与时间戳,可直接用帧号定位 `.raw` 文件做后续分析。
- **`docs/part_sets.json`**:48 个被试的动作顺序(由 xlsx 表格一次性生成)。新被试需先在该文件中按同样格式添加条目再重启服务。
- **`cache/`**:预览 MP4 派生视图,可随时删除重建,不影响原始数据与标注。

## 备份与迁移

| 内容 | 要不要带走 | 说明 |
|---|---|---|
| `scoring_platform/` 代码与前端 | ✅ 随 git | clone/拷贝即可,含 `docs/part_sets.json` |
| `annotations/score_annotation.json` | ✅ **必须手动拷贝** | 录好的标注只在这里;不进 git(已被 .gitignore 排除),迁移时单独拷贝,放到新环境的 `scoring_platform/annotations/` 下即可 |
| `cache/` | ❌ 不需要 | MP4 可重新生成 |
| `data/` | ❌ 不随平台走 | 原始采集数据留在采集主机,平台只读;换机器运行时用 `SCORING_DATA_ROOT` 指向其所在位置 |

标注文件与新环境的 data 完全匹配时(同一批采集数据),直接拷贝即无缝续用。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## 常见问题

| 现象 | 处理 |
|---|---|
| 服务启动但列表为空 | 检查 `data/<日期>/<被试>/timestamps/` 下两路 CSV 是否齐全;或 `SCORING_DATA_ROOT` 是否指向了 data 所在位置 |
| 提示未找到 FFmpeg | 标注功能不受影响;按第 2 节安装 FFmpeg 后重启 |
| 新被试在表格中不存在 | 在 `docs/part_sets.json` 的 `subjects` 中按同样格式添加该编号与 8 个动作代码,重启服务 |
| 端口被占用 | `SCORING_PORT=8011 python run.py` 换端口 |
| 双相机没有硬件同步 | 平台使用公共时间轴 + 各自最近帧匹配的近似对齐,边界处双相机错位量记录在 `inter_camera_offset_us` 字段,详见 `docs/architecture.md` |
