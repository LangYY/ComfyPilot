# ComfyUI Docker Stability Profiles

## 结论

普通 Docker / NVIDIA runtime 在 RTX 4090 上通常不能像限制系统内存一样“硬限制显存”。更稳的做法是让 ComfyUI 主动少占显存、预留显存、必要时把 VAE 放到 CPU，并用 PyTorch allocator 减少碎片。

本项目已提供三套 Docker Compose profile，可在 StudioBatch 的 `Runtime Settings -> ComfyUI Docker 模式` 中切换。

## Profiles

### stable

适合长批次、无人值守、之前出现过显存爆掉的情况。

- ComfyUI args: `--lowvram --reserve-vram 6 --cpu-vae`
- PyTorch allocator: `max_split_size_mb:128, garbage_collection_threshold:0.75`
- 优点：最稳，给 Windows 桌面、浏览器、Ollama 等保留更多显存。
- 缺点：更慢，CPU VAE 会拖慢保存/解码阶段。

### balanced

建议日常默认。

- ComfyUI args: `--normalvram --reserve-vram 3`
- PyTorch allocator: `max_split_size_mb:256, garbage_collection_threshold:0.82`
- 优点：速度和稳定性折中。
- 缺点：遇到高分辨率/长视频/大模型叠加时仍可能 OOM。

### performance

适合短批次、低分辨率、明确只跑一个重任务且希望更快。

- ComfyUI args: `--highvram --reserve-vram 1`
- PyTorch allocator: `max_split_size_mb:512, garbage_collection_threshold:0.90`
- 优点：尽量减少模型反复卸载/加载，速度最好。
- 缺点：最容易显存不足，不建议无人值守长批次。

## Files

- `E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128-update\docker-compose.stable.yml`
- `E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128-update\docker-compose.balanced.yml`
- `E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128-update\docker-compose.performance.yml`
- `E:\AI_Projects\comfyui_ltx_storyboard_batch\scripts\switch_comfyui_docker_profile.ps1`
- `E:\AI_Projects\comfyui_ltx_storyboard_batch\scripts\diagnose_comfyui_docker.ps1`

## Manual Commands

```powershell
cd E:\AI_Projects\comfyui_ltx_storyboard_batch

# 稳定优先
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\switch_comfyui_docker_profile.ps1 -Profile stable

# 均衡模式
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\switch_comfyui_docker_profile.ps1 -Profile balanced

# 性能优先
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\switch_comfyui_docker_profile.ps1 -Profile performance

# 诊断最近 Docker/ComfyUI/OOM 日志
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\diagnose_comfyui_docker.ps1
```

## 注意

切换模式会执行 `docker compose up -d --force-recreate`，也就是重建 ComfyUI 容器。请不要在 ComfyUI 正在生成时切换，除非你明确接受中断当前任务。
