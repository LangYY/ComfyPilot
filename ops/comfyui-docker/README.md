# ComfyUI Docker Profiles

These files mirror the known-good ComfyUI container configuration.

Default target:
- Container name: `comfyui_cu128_v0812`
- Container ID: `214fa9b6c22474e40050e47720e5cfd6b9197db733a8bd0f3828beedbeff5ac9`
- Host URL: `http://127.0.0.1:8189`

Runtime policy:
- Use the original `E:/AI_Projects/ComfyUI-Project/input`, `output`, and `cache` mounts.
- Keep `E:/AI_models` as the shared model mount.
- Treat `comfyui_cu128_update` as an experimental container, not the default.

Profiles:
- `balanced`: matches the old container behavior as closely as possible.
- `stable`: adds `--normalvram --reserve-vram 2`.
- `performance`: adds `--highvram --reserve-vram 0.5`.
