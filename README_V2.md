# ComfyUI 视频批量生成工作台

V2 现在按用户操作分成四个区域：

1. 上传 ComfyUI workflow JSON
2. 设置运行路径、输出参数和 seed
3. 上传 prompts 与素材
4. 提交队列、停止单个任务或停止全部

页面不再要求用户理解“工作区”和“切换工作区”。系统内部仍会使用一个默认本地空间保存快照、队列和运行历史，目的是避免当前批次被后续上传内容覆盖。

## 启动

```powershell
cd E:\AI_Projects\comfyui_ltx_storyboard_batch
python .\app_v2.py --port 8002
```

打开：

```text
http://127.0.0.1:8002
```

## 区域 1：上传 Workflow

上传从 ComfyUI 导出的 workflow JSON。

说明：

- T2V workflow 可以只包含文本 prompt 输入。
- I2V 首帧 workflow 通常需要一个 `LoadImage`。
- I2V 首尾帧 workflow 通常需要两个 `LoadImage`。
- 如果 workflow 是子图格式，ComfyUI 必须正在运行，因为系统需要读取 `/object_info` 来编译 workflow。
- 如果提示 `Could not auto-detect a unique save video node`，只需要在“节点映射（可选）”里填写真正的 `SaveVideo` 节点 ID。比如候选里有 `75:SaveVideo:保存视频`，就填 `75`；保存字段名通常保持 `filename_prefix`。

## 区域 2：运行参数

这些参数不会写进 prompt 文本，而是写入 workflow 节点或运行配置：

- ComfyUI 运行地址
- 最终输出目录
- 文件名前缀
- 输出宽度
- 输出高度
- seed 模式：随机 seed 或固定 seed
- negative prompt

路径逻辑：

- `最终输出目录` 是硬盘上的 mp4 目标目录。工作台会在 ComfyUI 完成后把视频复制到这里。
- `文件名前缀` 只负责给每条任务的文件名加统一前缀，例如 `0522_batch1_`。
- ComfyUI 的 `SaveVideo.filename_prefix` 由工作台自动使用内部相对目录，不再需要用户单独填写“保存前缀路径”。
- 工作台会通过 ComfyUI 的 `/view` 接口下载完成的视频，因此 `最终输出目录` 可以是 `E:\...` 这类任意本机盘符路径。

宽高只有在 workflow 中识别到可写的 `width`、`height`、`resize_type.width`、`resize_type.height` 等字段时才会覆盖 workflow 原值。

## 区域 3：输入模式

### T2V：只上传 Prompt

需要：

- prompts JSON 或粘贴 prompts 文本

不需要：

- 图片

### I2V：首帧 storyboard 切图

需要：

- prompts JSON 或粘贴 prompts 文本
- 一张 storyboard 图片

默认 `4 x 3` 会切出 12 张图，所以 prompts 也需要 12 条。

可以单独设置“切图数量”。例如 storyboard 是 `4 x 3` 共 12 格，但本批次只想生成 6 段，就把切图数量设为 `6`。系统会按从左到右、从上到下的顺序取前 6 格，prompts 也需要 6 条。

### I2V：批量上传首帧

需要：

- prompts JSON 或粘贴 prompts 文本
- 多张首帧图片

图片会按文件名自然排序，例如 `1, 2, 3, 10`。

### I2V：批量上传首帧 + 尾帧

需要：

- prompts JSON 或粘贴 prompts 文本
- 一组首帧图片
- 一组尾帧图片

首帧数量、尾帧数量、prompts 数量必须一致。

### I2V：连续图片自动互为首尾帧

需要：

- prompts JSON 或粘贴 prompts 文本
- 一组连续图片

例如上传 `1, 2, 3, 4`，会自动组成：

- `1 -> 2`
- `2 -> 3`
- `3 -> 4`

所以 prompts 数量需要是图片数量减 1。

## 队列与停止

- `提交队列` 会冻结当前批次快照，然后进入队列。
- 队列表的一行代表一个批次；每个批次内部会显示任务总数、已提交到 ComfyUI 的数量、完成数量、失败数量。
- 点击 `查看` 可以展开本批次每一条 ComfyUI 子任务，包含 `prompt_id`、seed、状态、输出文件和错误信息。
- `停止` 只停止单个当前运行批次。
- `停止全部` 会中断当前 ComfyUI 任务，并取消后续排队任务。

## 外部访问

默认启动只监听本机：

```powershell
python .\app_v2.py --port 8002
```

如果要让家里局域网里的其他设备访问，或配合 Tailscale、Cloudflare Tunnel、路由器端口转发从外面访问，可以用：

```powershell
python .\app_v2.py --public --port 8002 --access-token "换成你的长密码"
```

也可以双击：

```text
start_batch_studio_remote.bat
```

注意：

- `--public` 会监听 `0.0.0.0`，不再只限本机访问。
- 强烈建议设置 `--access-token`，否则任何能连到这个端口的人都能操作工作台。
- 外网访问本身还需要 VPN、隧道服务或路由器端口转发；工作台不会自动修改路由器或防火墙。
- 打开网页后点击右上角 `访问方式`，可以看到本机和局域网访问地址。

## 存储

V2 会把快照和运行记录保存在：

```text
studio_v2_data/
```
