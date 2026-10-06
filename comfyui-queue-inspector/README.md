# ComfyUI Queue Inspector

ComfyUI 큐(실행 중 + 대기 중 작업)를 **읽기 전용**으로 보여주는 독립 프로그램입니다.
ComfyUI 안에 설치하지 않으므로, ComfyUI를 업데이트·재설치해도 서로 영향이 없습니다.

![Queue table](docs/screenshot_table.png)

| 프롬프트 보기 | 모델 보기 |
|---|---|
| ![Prompt](docs/screenshot_prompt.png) | ![Models](docs/screenshot_models.png) |

## 기능

- 실행 중 작업 → 대기 작업을 **실제 실행 순서**대로 표시 (자동 새로고침 + ↻ 수동 새로고침)
- `prompt_id` 앞 8자리 표시, 클릭하면 전체 ID 복사
- **Model**: 메인 모델(Checkpoint / Diffusion model) 표시, LoRA 개수 배지. 클릭하면 LoRA(강도 포함)·Text encoder·VAE 등 전체 목록
- **Input Image**: `LoadImage` 파일명 표시, Preview로 이미지 확인 (파일이 없으면 `Image not found`)
- **Prompt**: Positive / Negative를 그래프 연결로 판별해 분리 표시, 판별할 수 없는 텍스트는 `Other prompt text`로 표시. 섹션별 Copy, Copy all
- 다크/라이트 테마 (OS 설정을 따르고, ◐ 버튼으로 전환)

## 설치 및 실행 (Windows 11)

**필요한 것:** Python 3.9 이상. 추가 패키지는 없습니다 (표준 라이브러리만 사용).
ComfyUI portable을 쓰고 있다면 그 안의 `python_embeded\python.exe`를 그대로 써도 됩니다.

1. 이 `comfyui-queue-inspector` 폴더를 원하는 곳에 복사합니다 (예: `C:\Tools\comfyui-queue-inspector`).
   ComfyUI 폴더 안에 넣을 필요는 없습니다.
2. ComfyUI를 평소처럼 실행합니다.
3. `start_inspector.bat`을 더블클릭합니다. 브라우저 탭이 자동으로 열립니다 (`http://127.0.0.1:8190/`).
4. 종료: 검은 콘솔 창에서 `Ctrl+C` 또는 창 닫기.

**Python을 따로 설치하지 않은 경우:** `start_inspector.bat`을 메모장으로 열어 아래 줄을 수정합니다.

```bat
set "PYEXE=C:\ComfyUI_windows_portable\python_embeded\python.exe"
```

**ComfyUI 주소/포트가 다른 경우:** 같은 파일의 `COMFY` 값을 바꿉니다 (기본 `http://127.0.0.1:8188`).

명령줄로 직접 실행할 수도 있습니다.

```
python inspector.py --comfy http://127.0.0.1:8188 --port 8190
python inspector.py --no-browser        # 브라우저 자동 열기 끄기
```

## 구조

```
브라우저 탭 (web/)  ⇄  inspector.py (127.0.0.1:8190)  ⇄  ComfyUI (127.0.0.1:8188)
                        GET /queue, /prompt, /object_info, /view 만 사용
```

| 파일 | 역할 |
|---|---|
| `qi/adapter.py` | `/queue` 원본 형식을 아는 **유일한** 모듈. 형식 검증, 실행 순서 정렬 |
| `qi/extract.py` | 실행 그래프에서 프롬프트(positive/negative 추적)·모델·입력 이미지 추출 |
| `qi/catalog.py` | `/object_info`(노드 타입 정보) 조회 도우미. 없으면 이름 기반 규칙으로 동작 |
| `qi/viewmodel.py` | UI용 정규화 모델 생성, `prompt_id`별 파싱 결과 캐시 |
| `qi/service.py` | 언제 ComfyUI를 다시 읽을지 결정 (가벼운 확인 → 필요할 때만 전체 조회) |
| `qi/http_server.py` | 웹 페이지와 읽기 전용 API 제공, 이미지 중계 |
| `web/` | 화면 (HTML/CSS/JS, 외부 라이브러리 없음) |

### 설계상 선택과 이유

- **별도 프로그램 + 중계 서버**: ComfyUI는 다른 포트에서 온 브라우저 요청을 Origin 검사로 거부(403)합니다.
  Python 서버가 대신 요청하면 Origin 헤더가 없어 이 문제가 없고, ComfyUI 실행 옵션도 바꿀 필요가 없습니다.
- **실행 순서 정렬**: `queue_pending`은 Python `heapq` 내부 배열의 복사본이라 정렬되어 있지 않습니다.
  실제 ComfyUI에서 확인한 원본 순서가 `[-11, -10, -8, -9, 7, 3, 1, 4, 2]`였습니다.
  실행 순서는 `number` 오름차순이므로 그 기준으로 정렬합니다 (Queue Front로 넣은 작업은 음수).
- **새로고침 비용 절감**: `/queue`는 작업마다 그래프 전체를 담아 커질 수 있습니다.
  1.5초마다 아주 작은 `GET /prompt`(남은 작업 수)만 확인하고, 수가 바뀌었거나 5초가 지났을 때만 `/queue`를 다시 읽습니다.
  브라우저에는 버전 번호를 비교해 바뀐 경우에만 데이터를 보내고, 화면은 `prompt_id` 기준으로 바뀐 행만 갱신합니다.
- **Positive/Negative 판별**: 노드 제목이 아니라 **연결**을 따라갑니다.
  KSampler·CFGGuider 등의 `positive`/`negative` 입력에서 위로 거슬러 올라가고,
  `WanImageToVideo`·`ControlNetApplyAdvanced`처럼 양쪽을 통과시키는 노드는 출력 포트 이름(positive/negative)에 맞는 입력만 따라갑니다.
  같은 텍스트가 양쪽에 쓰이면 추측하지 않고 `Other`로 보냅니다. `ConditioningZeroOut`은 "텍스트 없음"으로 표시합니다.
- **읽기 전용 보장**: 코드에 ComfyUI로 GET 이외의 요청을 보내는 부분이 없습니다.
  Inspector 자체도 POST/PUT/DELETE를 405로 거부합니다. 기본적으로 `127.0.0.1`에만 열려 다른 PC에서는 접근할 수 없습니다.
- **보안**: 프롬프트·파일명은 모두 `textContent`로 넣어 HTML로 해석되지 않습니다. 외부 네트워크 호출, 텔레메트리 없음.

## 사용하는 ComfyUI API

| API | 용도 |
|---|---|
| `GET /queue` | 실행 중/대기 작업 목록 (항목: `[number, prompt_id, prompt, extra_data, outputs]`) |
| `GET /prompt` | 남은 작업 수 (변경 감지용, 매우 작음) |
| `GET /object_info` | 노드 입출력 타입·이름 (시작 시 1회, 새 노드가 보이면 재조회) |
| `GET /view?filename=&subfolder=&type=` | 입력 이미지 미리보기 (Preview 클릭 시에만) |

## 알려진 한계

- **워크플로우 파일명은 표시하지 않습니다.** 큐 데이터에 파일명이 없기 때문입니다 (대신 Model 표시).
- 모델 이름이 다른 노드에서 연결되어 들어오면 `(linked)`로 표시됩니다.
- 프롬프트 텍스트가 커스텀 노드에서 동적으로 생성되면 (예: 랜덤 프롬프트 노드) 큐에 있는 입력값만 보입니다.
- 입력 이미지는 `LoadImage` 계열만 인식합니다. 비디오 입력 노드는 v2 후보입니다.
- 작업 하나가 끝나고 다른 작업이 추가되는 일이 1.5초 안에 동시에 일어나면, 최대 5초 늦게 반영될 수 있습니다.
- `/object_info`가 아직 로드되지 않았을 때(ComfyUI 시작 직후)는 이름 기반 규칙으로 판별하며, 로드되면 자동으로 다시 분석합니다.

## 테스트

```
python -m unittest discover -s tests
```

- `tests/test_parsing.py`: 큐 형식, 순서, 프롬프트 역할 판별, 모델, 이미지, 300개 작업 성능
- `tests/test_real_fixture.py`: **실제 ComfyUI(2026-10-05 master)에서 캡처한** `/queue`·`/object_info` 데이터로 검증
- `tests/test_server.py`: HTTP API, 읽기 전용(405), 이미지 중계, ComfyUI 꺼짐 상태, 경로 탐색 차단

ComfyUI 없이 화면을 확인하려면 모의 서버를 쓸 수 있습니다.

```
python tools/mock_comfy.py --port 8188 --jobs 30 --step 8
python inspector.py
```
