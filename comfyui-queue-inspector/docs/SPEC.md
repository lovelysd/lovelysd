# ComfyUI Queue Inspector — Specification (rev. 2)

Revision of the original "ComfyUI Queue Inspector — Development Specification".
Changes from the original are marked **[changed]** or **[new]**, with the reason.

## 1. Purpose

Show, at any moment, what ComfyUI is running now and what will run next:
identify each job reliably, see which model and input image belong to it, and
read the full prompt only when needed. Summary first, details on demand.
Read-only. Not a queue manager.

## 2. Form factor **[changed]**

A **standalone local program**, not a ComfyUI frontend extension.

- A small Python server (standard library only) serves a web page opened in a
  normal browser tab: `http://127.0.0.1:8190/`.
- It depends only on ComfyUI's public HTTP API. Nothing is installed into
  `ComfyUI/custom_nodes`.

*Reason:* the ComfyUI frontend and its extension API change often. The HTTP API
(`/queue`, `/view`, `/prompt`, `/object_info`) is used by many external tools,
so it is far more stable. Updating or reinstalling ComfyUI cannot break the
inspector, and the inspector cannot break ComfyUI.

*Why a proxy server instead of a plain HTML file:* ComfyUI rejects browser
requests whose `Origin` differs from its host (403, origin-check middleware).
Server-to-server requests carry no Origin header, so no ComfyUI flags are needed.

A native window (e.g. pywebview) is a possible later addition; v1 uses a browser tab.

## 3. Data source

| API | Use |
|---|---|
| `GET /queue` | `queue_running`, `queue_pending` |
| `GET /prompt` | `exec_info.queue_remaining` — cheap change detection |
| `GET /object_info` | node input/output types and output names (optional) |
| `GET /view` | input image preview, fetched only on Preview |

### 3.1 Queue item shape (verified against ComfyUI source, 2026-10)

`[number, prompt_id, prompt, extra_data, outputs_to_execute]`. The server
holds a 6th element (sensitive data) and strips it before responding. Read
indices 0–3 only and tolerate extra elements. Parsing lives in one adapter
module.

### 3.2 Execution order **[new]**

`queue_pending` is a copy of a Python `heapq` list, i.e. **heap order, not
execution order**. Execution order is ascending `number`. "Queue front" jobs
get a negative number. The adapter sorts by `number`. The UI shows the
position 1..N, with the raw number in a tooltip.

## 4. Main table **[changed]**

| Column | Content |
|---|---|
| # | Execution position |
| Status | RUNNING (accent badge) / PENDING (neutral badge) |
| Prompt ID | First 8 chars + `…`; click copies the full id; tooltip shows the full id |
| **Model** | Main model (checkpoint / diffusion model) without folder and extension; `+N` if several; `LoRA n` chip. Click opens the Models tab |
| Input Image | First `LoadImage` filename, `+N` if several. Click opens the preview |
| Actions | `Prompt`, `Preview` (only if an input image exists) |

**Workflow column removed [changed].** The original workflow filename is not
part of queue data. The model is reliably present and more useful.

Narrow windows: hide Input Image first, then Prompt ID. #, Status, Model and
Actions stay visible. No horizontal scrolling.

## 5. Model extraction **[new]**

From the API-format prompt graph:

| Kind | Node field |
|---|---|
| Checkpoint (main) | `ckpt_name` |
| Diffusion model (main) | `unet_name` |
| LoRA | `lora_name` (+ `strength_model` / `strength_clip` / `strength`) |
| Text encoder | `clip_name`, `clip_name1..N` |
| CLIP vision | `clip_name` on a `*Vision*` loader |
| VAE / ControlNet / Style / Upscale | `vae_name`, `control_net_name`, `style_model_name`, `model_name` on `*Upscale*` |
| Other model | any string ending in `.safetensors .ckpt .pt .pth .bin .gguf .sft .onnx` |

A linked (non-literal) value is shown as `(linked)`.

## 6. Prompt extraction

### 6.1 Roles by graph wiring

- **Roots:** any node input named `positive` / `negative`. Also `BasicGuider.conditioning`
  and `DualCFGGuider.cond1/cond2`, which count as positive.
- **Trace upstream along conditioning edges.**
  - If the source node's output name is `positive` / `negative` (e.g.
    `WanImageToVideo`, `ControlNetApplyAdvanced`), follow only the input with
    that name. This step is port-aware.
  - Otherwise follow all CONDITIONING-typed inputs (from `/object_info`).
    Without type info, follow inputs whose name contains `cond` / `positive` / `negative`.
  - Without type info, an unknown node that has both `positive` and `negative`
    inputs maps output 0 to positive and output 1 to negative.
- **Text:** literal fields named `text`, `text_g`, `text_l`, `clip_l`, `t5xxl`,
  `prompt`, `string*`, or any multiline STRING input. Linked STRING inputs are
  followed upstream (e.g. `StringConcatenate`).
- **`ConditioningZeroOut`** stops the trace and is shown as "Conditioning zeroed
  out (no text used)".
- **A text reached from both roles** goes to *Other prompt text*, labelled
  "Used as both positive and negative". The tool does not guess.
- **Prompt-like text no sampler path reaches** goes to *Other prompt text*.
  Nothing is discarded.

### 6.2 Prompt modal

Tabs: **Prompt** | **Models**.

- Prompt tab: Positive, Negative and Other sections. Each block shows node title, node id
  and field, with its own Copy button. A Copy all button copies everything.
- Models tab: every model with its kind, full path, node and LoRA strength.
- Full `prompt_id` is shown and copyable.
- ESC, the ✕ button or a backdrop click closes the modal.
- The modal keeps its data if the job finishes while it is open.

## 7. Image preview

- The preview loads only when Preview is clicked.
- The value is parsed into `/view` parameters:
  - `sub/dir/name.png [input]` → `subfolder=sub/dir`, `filename=name.png`, `type=input`
  - Backslashes are normalized to `/`.
  - `blake3:<hash>` asset references are passed through; `/view` resolves them.
- The modal shows the filename, the source path, and switch buttons when the job has several images.
- A missing file shows `Image not found` with the filename. The table is not affected.

## 8. Refresh

- The browser polls every 1.5 s, or 6 s while the tab is hidden. The ↻ button forces an immediate refresh.
- The server calls `GET /prompt` on each poll. It fetches the full `/queue` only
  when the remaining count changes, on a manual refresh, or every 5 s as a safety net.
- Each snapshot has a version number. The browser sends the version it already
  has; if nothing changed, the reply carries no items.
- Rows are keyed by `prompt_id`:
  - Existing DOM rows are reused and moved, never rebuilt.
  - When a job moves from pending to running, the same row updates its badge.
  - Finished jobs fade out.
- Parsed results are cached per `prompt_id`; a queued graph is immutable.
  The cache is cleared when `/object_info` changes.

## 9. Error states

| Case | Display |
|---|---|
| ComfyUI unreachable | Banner `Unable to read queue.`, red status dot |
| Unexpected `/queue` format | Banner `Unable to read queue. Unexpected format: …` |
| Inspector server stopped | Status `Inspector server stopped` |
| Empty queue | `Queue is empty.` |
| No model / no image | `—` |
| No prompt | `No prompt text detected` |
| Image missing | `Image not found` |

## 10. Safety and privacy

- Requests sent to ComfyUI are GET only. The inspector answers POST, PUT and DELETE with 405.
- Static files are served from a whitelist.
- The image proxy accepts only `type` values `input`, `output` and `temp`.
- The server binds to `127.0.0.1` by default.
- All queue text is rendered with `textContent`, so it is never interpreted as HTML.
- No external network calls, no telemetry, no third-party libraries.
- Clipboard: uses `navigator.clipboard` when available. Over plain HTTP on a
  LAN IP it is unavailable, so the code falls back to `execCommand('copy')`.

## 11. Packaging

```
comfyui-queue-inspector/
├─ inspector.py          entry point
├─ start_inspector.bat   Windows launcher (auto-detects py/python, or set PYEXE)
├─ qi/                   adapter, extract, catalog, viewmodel, service, http_server
├─ web/                  index.html, app.js, style.css
├─ tests/                unit + real-ComfyUI fixture + HTTP tests
└─ tools/mock_comfy.py   mock ComfyUI for development and demos
```

Runs with ComfyUI portable's embedded Python. Its `._pth` file disables adding
the script directory to `sys.path`, so `inspector.py` adds the directory itself.

## 12. Phase 2 candidates

- Native window
- Search and filter by model, prompt or filename
- Video input preview
- Snapshot export
- Optional cancel controls, behind an explicit opt-in

Not in v1.
