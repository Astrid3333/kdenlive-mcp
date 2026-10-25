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
| `render_start` | lanza `melt proyecto.kdenlive -progress -consumer avformat:salida` como job en background |
| `render_status` | running, código de salida, % de progreso, tail del log, ¿existe el output? |
| `render_stop` | SIGTERM+SIGKILL al grupo de proceso |
| `jobs_list` | todos los jobs, el más reciente primero |

Jobs y logs en `~/.local/state/kdenlive-mcp/jobs/<job_id>/` (override con
`KDENLIVE_MCP_HOME`).

## Uso típico

1. `project_info` → verificar recursos faltantes antes de renderizar.
2. `render_start(project, output)` → devuelve `job_id`.
3. `render_status(job_id)` en bucle hasta `running: false` y `returncode: 0`.

## Notas

- Kdenlive no tiene CLI de render estable; la vía headless es `melt`, que abre
  `.kdenlive` directamente (son XML de MLT).
- El render no corre en la GUI de Kdenlive: si el proyecto usa efectos/plugin
  específicos de Kdenlive, melt puede fallar; revisa `log_tail`.
