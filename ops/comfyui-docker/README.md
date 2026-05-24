# ComfyUI Docker Profiles

These files mirror the local Docker Compose configuration used by the launcher.

Runtime policy:
- Keep `output`, `input`, and `cache` on `D:/ComfyUI_Runtime`.
- Keep `/app/temp` inside the container for faster temporary frame/video work.
- Start from the fastest usable profile and only downgrade if the machine becomes unstable.

Profiles:
- `performance`: fastest, highest VRAM pressure.
- `balanced`: default, performance-first with a small VRAM reserve.
- `stable`: more conservative, still keeps VAE on GPU.
