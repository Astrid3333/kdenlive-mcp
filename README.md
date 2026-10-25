# kdenlive-mcp

MCP server para Kdenlive: inspección de proyectos `.kdenlive` y render con `melt` (MLT).

## Instalación

```bash
cd kdenlive-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m pytest tests -q
```

Registrado en `opencode.json` (reinicio de opencode para cargarlo).

Requiere `melt` en el PATH para renderizar (`sudo apt install melt`); `kdenlive`
es opcional (solo inspección). `ffmpeg`/`ffprobe` opcionales.

## Herramientas

| Tool | Descripción |
| --- | --- |
| `kdenlive_env` | binarios detectados (kdenlive/melt/ffmpeg), versiones, dir de jobs |
| `project_info` | perfil (fps/resolución), pistas, clips, duración, recursos faltantes |
| `render_start` | `melt proyecto -progress -consumer avformat:salida` en background; acepta `preset`, `resolution` ("WxH" o 720p/1080p/4k), `zone` [inicio_s, fin_s], `realtime`, `extra_args` |
| `render_status` | running, código de salida, % de progreso, tail del log, ¿existe el output? |
| `render_stop` | SIGTERM+SIGKILL al grupo de proceso |
| `jobs_list` | todos los jobs, el más reciente primero |
| `kdenlive_extract_frames` | extrae frames (png) en segundos dados del proyecto, de forma síncrona |
| `kdenlive_validate_resources` | ffprobe de cada clip: códec, resolución, fps, duración vs. perfil del proyecto |
| `kdenlive_list_effects` | consumidores, filtros y productores disponibles en melt/MLT |
| `kdenlive_concatenate` | render de varios proyectos en secuencia (uno tras otro) en un job |
| `kdenlive_render_batch` | un proyecto → varios outputs/presets en paralelo (un job cada uno) |
| `kdenlive_merge_projects` | fusiona varios .kdenlive en uno (composición superpuesta, ids renumerados) |

Jobs y logs en `~/.local/state/kdenlive-mcp/jobs/<job_id>/` (override con
`KDENLIVE_MCP_HOME`).

## Uso típico

1. `project_info` → verificar recursos faltantes antes de renderizar.
2. `render_start(project, output)` → devuelve `job_id`.
3. `render_status(job_id)` en bucle hasta `running: false` y `returncode: 0`.

Ejemplos avanzados:

- `render_start(project, "out.mp4", preset="1080p", zone=[10.0, 20.0])` → segundos 10–20 a 1080p.
- `kdenlive_concatenate("final.mp4", projects=["a.kdenlive", "b.kdenlive"])`.
- `kdenlive_render_batch(project, outputs=[{"output": "a.mp4", "preset": "720p"}, {"output": "b.mp4", "preset": "1080p"}])`.
- `kdenlive_merge_projects("merged.kdenlive", projects=["a.kdenlive", "b.kdenlive"])`.

## Notas

- Kdenlive no tiene CLI de render estable; la vía headless es `melt`, que abre
  `.kdenlive` directamente (son XML de MLT).
- El render no corre en la GUI de Kdenlive: si el proyecto usa efectos/plugin
  específicos de Kdenlive, melt puede fallar; revisa `log_tail`.
