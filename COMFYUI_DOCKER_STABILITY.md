# ComfyUI Docker Runtime

## Current Default

ComfyPilot is locked back to the known-good ComfyUI container:

- Container name: `comfyui_cu128_v0812`
- Container ID: `214fa9b6c22474e40050e47720e5cfd6b9197db733a8bd0f3828beedbeff5ac9`
- Image: `comfyui_cu128_v0812_fixed2_20260512:latest`
- URL: `http://127.0.0.1:8189`

The newer experimental container `comfyui_cu128_update` is no longer the default target.

## Known-Good Mounts

The old container keeps runtime files under the original project directory:

- `E:/AI_models:/app/models`
- `E:/AI_Projects/ComfyUI-Project/input:/app/input`
- `E:/AI_Projects/ComfyUI-Project/output:/app/output`
- `E:/AI_Projects/ComfyUI-Project/cache:/app/cache`

## Launcher Behavior

The launcher starts the existing known-good container with:

```powershell
docker start 214fa9b6c22474e40050e47720e5cfd6b9197db733a8bd0f3828beedbeff5ac9
```

If `comfyui_cu128_update` is running, the launcher stops it first so port `8189` is free.

## Runtime Profiles

Runtime profiles now target the old compose directory:

- `E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128\docker-compose.yml`
- `E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128\docker-compose.balanced.yml`
- `E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128\docker-compose.stable.yml`
- `E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128\docker-compose.performance.yml`

Switching profiles still recreates the container, so do not switch while ComfyUI is generating.
