# ComfyUI + LTX 2.3 Storyboard Batch

这个项目用于本地批量跑 `ComfyUI + LTX 2.3 image-to-video`。

目标流程是：

1. 输入一张 `3 x 4` 的 storyboard PNG
2. 自动切成 `12` 张 cell 起始图
3. 读取 `prompts.json` 里的 `12` 条提示词
4. 逐条提交给 ComfyUI 的 LTX 2.3 workflow
5. 生成 `12` 个 mp4 片段

字幕不会交给 LTX 生成。`subtitle` 字段只给后期字幕流程预留，本项目不会把它写进 workflow。

## 当前推荐用法

优先使用本地网页版入口：

```powershell
cd "C:\Users\XTIA\Documents\New project\comfyui_ltx_storyboard_batch"
python app.py
```

然后在浏览器打开：

```text
http://127.0.0.1:8000
```

网页里可以直接完成：

1. 上传 storyboard 并切图
2. 上传或编辑 `prompts.json`
3. 上传 API workflow JSON
4. 填写 ComfyUI 地址和节点映射
5. 一键启动批处理
6. 查看进度、日志、失败记录和最终输出

CLI 方式仍然保留，适合断点重跑和调试。

## 项目结构

```text
comfyui_ltx_storyboard_batch/
├─ app.py                         # 本地 Web 入口，python app.py 即可启动
├─ cells/                         # 切图输出 01.png ~ 12.png
├─ config/
│  └─ workflow_config.json        # ComfyUI 地址、输出目录、节点映射
├─ data/
│  └─ prompts.json                # 12 条提示词
├─ failed_jobs.json               # 失败任务记录
├─ ltx_batch/                     # 共享后端逻辑
│  ├─ batch.py
│  ├─ project.py
│  └─ storyboard.py
├─ outputs/                       # 最终归档的 mp4
├─ scripts/
│  ├─ split_storyboard.py         # CLI 切图脚本
│  └─ run_batch.py                # CLI 批处理脚本
├─ web/
│  ├─ app.js                      # Web 前端逻辑
│  ├─ index.html
│  └─ styles.css
├─ workflows/
│  ├─ ltx_i2v_api.json            # 实际运行的 API workflow 模板
│  └─ source_ui_workflow.json     # 你提供的原始 UI workflow 参考
└─ requirements.txt
```

## 安装依赖

```powershell
cd "C:\Users\XTIA\Documents\New project\comfyui_ltx_storyboard_batch"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

依赖包括：

- `Pillow`：切图
- `requests`：调用 ComfyUI API
- `fastapi`：本地 Web 服务
- `uvicorn`：启动 Web 服务
- `python-multipart`：支持网页上传文件

## Web 使用流程

### 1. 启动网页控制台

```powershell
python app.py
```

如果你喜欢 `uvicorn` 命令，也可以：

```powershell
uvicorn app:app --host 127.0.0.1 --port 8000
```

### 2. 上传 storyboard

在网页的 `Storyboard` 区域：

1. 选择总图 PNG
2. 设置 `rows=4`、`cols=3`
3. 按需填写 `margin` 和 `gutter`
4. 点击 `上传并切图`

切图顺序固定为从左到右、从上到下：

```text
01 02 03
04 05 06
07 08 09
10 11 12
```

切出的图片会写到：

```text
cells/01.png ~ cells/12.png
```

### 3. 上传或编辑 prompts.json

网页的 `Prompts` 区域支持两种方式：

1. 直接上传一个新的 `prompts.json`
2. 在文本框里编辑后点击 `保存 prompts`

单条数据结构如下：

```json
{
  "index": 1,
  "zodiac": "白羊座",
  "ball_name": "爆燃火锅丸子球",
  "prompt": "Use the uploaded storyboard cell as the exact first frame...",
  "subtitle": "白羊座：爆燃火锅丸子球",
  "output_name": "01_白羊座_爆燃火锅丸子球.mp4"
}
```

字段说明：

- `index`：对应 `cells/01.png` 到 `cells/12.png`
- `zodiac`：星座名
- `ball_name`：丸子名
- `prompt`：正向提示词
- `subtitle`：后期字幕用，不会提交给 LTX
- `output_name`：最终文件名

推荐命名格式：

```text
01_白羊座_爆燃火锅丸子球.mp4
```

### 4. 上传 workflow JSON

在网页的 `Workflow 与配置` 区域上传：

```text
workflows/ltx_i2v_api.json
```

正式跑批前，建议你在 ComfyUI 中重新导出真正的 API workflow，而不是直接使用前端 UI workflow。

## 如何从 ComfyUI 导出 API workflow

你提供的 `video_ltx2_3_i2v (1).json` 属于前端 UI workflow。它可以参考，但不建议直接拿来批跑。

推荐做法：

1. 在 ComfyUI 打开你的 LTX 2.3 image-to-video workflow
2. 确认图里存在这些节点
   - `LoadImage` 或等价起始图加载节点
   - 正向 prompt 文本节点
   - seed 节点
   - 最终保存视频节点，例如 `SaveVideo`、`VHS_VideoCombine` 或等价节点
3. 在 ComfyUI 里执行 `File -> Export (API)`，或新版界面的等价 API 导出操作
4. 用导出的 JSON 覆盖：

```text
workflows/ltx_i2v_api.json
```

当前项目里保留了两份文件：

- `workflows/source_ui_workflow.json`
  - 你提供的原始 UI workflow
- `workflows/ltx_i2v_api.json`
  - 目前的参考模板

注意：

- 当前这份参考模板里没有完整的 `LoadImage` 节点
- 所以正式跑批前，必须重新 API 导出一次
- 如果不换，网页和 CLI 的校验接口都会明确提示错误

## workflow_config.json 需要填什么

核心配置文件：

[`config/workflow_config.json`](C:\Users\XTIA\Documents\New project\comfyui_ltx_storyboard_batch\config\workflow_config.json)

至少要确认这些字段：

- `comfyui_base_url`
  - 默认 `http://127.0.0.1:8188`
- `comfyui_output_dir`
  - 必填，改成你的 ComfyUI `output` 文件夹绝对路径
- `save_prefix_root`
  - ComfyUI 保存视频时的前缀目录
- `seed_base`
  - 不单独指定 seed 时的默认起始值

### 哪些 node id 需要在 config 里填写

脚本支持自动识别，但如果 workflow 里有多个同类节点，建议手动写死：

- `workflow_nodes.image.id`
  - `LoadImage` 节点 id
- `workflow_nodes.positive_prompt.id`
  - 正向 prompt 节点 id
- `workflow_nodes.save_video.id`
  - 最终保存视频节点 id
- `workflow_nodes.seed_nodes`
  - 一个数组，填所有需要同步修改的 seed 节点

示例：

```json
"workflow_nodes": {
  "image": {
    "id": "1",
    "input_name": "image",
    "upload_input_name": "upload",
    "upload_value": "image"
  },
  "positive_prompt": {
    "id": "6",
    "input_name": "text"
  },
  "save_video": {
    "id": "15",
    "input_name": "filename_prefix"
  },
  "seed_nodes": [
    {
      "id": "11",
      "input_name": "noise_seed"
    }
  ]
}
```

网页里有 `校验节点映射` 按钮，会用当前 workflow 和 config 做一次预检查。

## 批处理实际会做什么

每条任务会按下面流程执行：

1. 读取 `prompts.json`
2. 读取 `workflows/ltx_i2v_api.json`
3. 找到对应的 `cells/xx.png`
4. 通过 `/upload/image` 把 cell 图片上传到 ComfyUI
5. 修改 workflow 中的：
   - `LoadImage` 路径
   - 正向 prompt 文本
   - seed
   - 保存视频节点输出前缀
6. POST 到：

```text
http://127.0.0.1:8188/prompt
```

7. 轮询 `/history/{prompt_id}` 和 `/queue`
8. 任务成功后，把 ComfyUI `output` 目录里的视频复制到项目 `outputs/`
9. 最终命名为 `output_name`
10. 失败任务写入 `failed_jobs.json`

## 视频时长 3-5 秒怎么控制

不要依赖 prompt 文本去“猜”时长。

更稳妥的方式是直接在 workflow 里设置时长相关节点，例如：

- `duration`
- `frames_number`
- `fps`
- `frame_rate`
- 或你当前 LTX workflow 中对应的视频长度节点

建议你先在 workflow 中把每段时长固定到 `3-5 秒`，然后让这个项目只负责批量替换：

- 起始图
- 正向 prompt
- seed
- 输出文件名

## CLI 用法

虽然网页版是推荐入口，但命令行仍然可以用。

### 切图

```powershell
python scripts/split_storyboard.py storyboard_3x4.png --rows 4 --cols 3 --margin 0 --gutter 0
```

### 全量跑 12 条

```powershell
python scripts/run_batch.py --start-index 1 --end-index 12
```

### 断点续跑

例如只跑第 5 到第 8 条：

```powershell
python scripts/run_batch.py --start-index 5 --end-index 8
```

### 覆盖已有输出

```powershell
python scripts/run_batch.py --start-index 5 --end-index 8 --overwrite
```

## 失败重试

失败记录文件：

[`failed_jobs.json`](C:\Users\XTIA\Documents\New project\comfyui_ltx_storyboard_batch\failed_jobs.json)

你可以查看里面的 `index`，然后用 CLI 或网页指定区间重跑。

例如只重跑第 7 条：

```powershell
python scripts/run_batch.py --start-index 7 --end-index 7 --overwrite
```

## 已知注意事项

- 现在项目已经带有本地网页入口，但真正跑批前，你仍然需要换成正式的 ComfyUI API workflow
- 如果 workflow 里有多个 `CLIPTextEncode`、多个 `RandomNoise` 或多个保存节点，建议在 config 里手动填 node id
- 最终交付文件名以项目 `outputs/` 目录里的归档结果为准，不受 ComfyUI 原生自动编号影响
- `subtitle` 不会送进 LTX，字幕后期再做
