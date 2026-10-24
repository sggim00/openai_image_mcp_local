# openai_image_mcp_local

OpenAI(`gpt-image-2`)와 Google Gemini(Nano Banana)로 이미지를 만들고 고치고,
ByteDance **Seedance 2.5**(BytePlus ModelArk) 또는 Gemini Omni로 짧은 영상(소리 포함)을 만드는 **내 PC용** 도구입니다.
Claude가 요청마다 더 잘 나올 모델을 고릅니다 (`provider`).

- **PC에서** — Claude 데스크톱 앱 / Claude Code가 `server.py`를 직접 실행하는 로컬 MCP 서버(stdio).
- **휴대폰에서** — PC가 켜져 있으면 `drive_worker.py`가 구글 드라이브를 우편함으로 써서 대신 처리합니다.
  서버 호스팅, 포트 개방, 터널이 필요 없습니다.

```
[PC]     Claude 데스크톱 ─ generate_image ─▶ server.py ─▶ OpenAI ─▶ 미리보기 + 저장 경로
[모바일] Claude 앱 ─ Google Drive 커넥터 ─▶ 드라이브 requests/req_<id>.json
         drive_worker.py(PC, 5초마다 확인) ─▶ OpenAI ─▶ 드라이브 outputs/<id>.png + <id>.result.json (+ PC 폴더에도 저장)
         Claude 앱 ─ Google Drive 커넥터 ─▶ 결과 확인, 링크 전달
```

## PC와 모바일 차이

| | PC (Claude 데스크톱 앱) | 모바일 (Claude 앱) |
|---|---|---|
| 경로 | 앱 → `server.py` 직접 호출 | 드라이브 커넥터 → `requests/` → PC 워커 → `outputs/` |
| 응답 | 30~60초 뒤 그 자리에서 결과 | 요청만 올림. 1분쯤 뒤 "결과 확인"이라고 한 번 더 |
| 결과 보기 | 대화창 미리보기 | 드라이브 링크 |
| 레퍼런스 | 파일 경로 / 클립보드 / 코드 / URL | 드라이브 `refs/`에 올린 사진(`latest`, `name:`), 드라이브 링크, 코드 |
| 저장 | `OUTPUT_DIR` | 드라이브 `outputs/` + `OUTPUT_DIR` |
| 조건 | 데스크톱 앱 실행 중 | PC 켜짐(절전 X) + 워커 실행 중. 데스크톱 앱은 꺼져 있어도 됨 |

결과 코드(8자리)는 공용입니다. 모바일에서 만든 것을 PC에서 `a1b2c3d4 에서 신발만 빨갛게`처럼 바로 이어서 고칠 수 있고, 반대도 됩니다.
로컬 MCP 도구는 데스크톱 앱에서 보낸 메시지에서만 Claude에게 보이므로, 모바일은 이 경로를 따로 씁니다.

## 설치 (Windows)

1. Python 3.10 이상 설치
2. 이 폴더에서 `install.bat` 실행 → `.venv` 생성 + 패키지 설치 + `.env` 생성
3. `.env`에 키 지정 — 직접 `OPENAI_API_KEY=sk-...` 또는 키 파일 경로 `OPENAI_API_KEY_FILE=D:/.../xxx.api_key` (필요하면 `OUTPUT_DIR`도).
   Gemini도 쓰려면 `GEMINI_API_KEY_FILE=D:/.../gemini.api_key` (또는 `GEMINI_API_KEY=AIza...`). 둘 중 하나만 있어도 동작합니다.
   Seedance 영상은 `SEEDANCE_API_KEY_FILE=D:/.../byteplus/xxx.api_key` (또는 `SEEDANCE_API_KEY=...`, BytePlus ModelArk API 키).
   지정하지 않아도 OpenAI/Gemini 키 폴더 옆의 `byteplus/` 폴더(예: `.../data/byteplus/`)에 있는 키 파일을 자동으로 찾습니다.
4. `install.bat`이 마지막에 `register_claude.py`를 실행해 Claude Desktop 설정에 자동 등록하고 서버가 뜨는지 확인합니다.
   (Microsoft Store판 Claude는 설정 파일이 `%LOCALAPPDATA%\Packages\Claude_…\LocalCache\Roaming\Claude`에 있어
   `%APPDATA%\Claude`를 고쳐도 반영되지 않습니다. 스크립트는 두 곳 모두 찾아 고칩니다.)
   직접 넣으려면 Claude Desktop → 설정 → 개발자 → **구성 편집**으로 연 파일에:

```json
{
  "mcpServers": {
    "openai-image-local": {
      "command": "C:\\tools\\openai_image_mcp_local\\.venv\\Scripts\\python.exe",
      "args": ["C:\\tools\\openai_image_mcp_local\\server.py"]
    }
  }
}
```

`.env` 대신 설정 파일에 넣고 싶으면 `"env": {"OPENAI_API_KEY_FILE": "D:/.../xxx.api_key", "OUTPUT_DIR": "D:/work/ai_images"}`를 추가하세요.
키 파일은 키 한 줄만 있어도 되고, `OPENAI_API_KEY=sk-...` 형식이어도 됩니다. 서버 시작 시 어디서 키를 읽었는지 로그에 남습니다.

5. Claude Desktop을 **트레이에서 완전히 종료** 후 다시 실행 → 도구 목록에 `openai-image-local` 확인

macOS/Linux는 `install.sh`, Claude Code는:

```bash
claude mcp add openai-image-local -s user -- "<폴더>/.venv/Scripts/python.exe" "<폴더>/server.py"
```

## 도구

| 도구 | 용도 |
|---|---|
| `generate_image` | 1장 생성. `reference_images`로 스타일·캐릭터 유지, `filename`으로 저장 이름 지정 |
| `generate_batch` | 같은 레퍼런스로 최대 8장 **병렬** 생성 (파츠 분리, 시안 여러 개) |
| `edit_image` | 기존 이미지(경로·코드·클립보드)를 지시대로 수정 |
| `generate_video` | 텍스트/이미지 → 짧은 영상 (.mp4, 소리 포함, Seedance 또는 Gemini). `continue_from`으로 이전 영상 수정·연장 |
| `wait_for_images` | 50초 안에 안 끝난 작업(이미지·영상)을 이어서 받아옴 |
| `list_recent` | 최근 결과 (코드, 경로, 프롬프트) |
| `open_output_folder` | 결과 폴더를 탐색기/Finder로 열기 |

## 모델 선택 (`provider`)

이미지 도구 3개에 `provider`가 있습니다. Claude가 요청을 보고 고르고, 직접 지정해도 됩니다.

| 값 | 모델 | 잘하는 것 |
|---|---|---|
| `openai` | `gpt-image-2` | **새 그림** — 설명으로 구도 잡기, 일러스트·캐릭터 디자인, 긴 프롬프트, 투명 배경 |
| `gemini` | `gemini-nano-banana-2.1` | **수정** — 요청한 부분만 바꾸고 나머지(얼굴·포즈·조명·스타일) 유지, 여러 레퍼런스(최대 14장)로 캐릭터 일관성, 빠름 |
| `gemini-pro` | `gemini-3-pro-image` | 어려운 수정, 이미지 속 글자, 최대 4K. 느리고 비쌈 |
| `auto` (기본) | | `generate_*` → openai, `edit_image` → gemini, 투명 배경 → openai, 키가 하나뿐이면 그쪽 |

Gemini는 투명 배경을 만들지 못합니다. `size`에는 `1024x1024` 같은 값 외에 Gemini용 비율(`16:9`, `4:5`, `21:9` 등)도 넣을 수 있고,
`quality: high`는 Gemini에서 2K 출력 + 깊은 추론입니다.

## 영상 (`generate_video`)

`provider`로 영상 모델을 고릅니다. 기본(`auto`)은 Seedance이고, Seedance 키가 없으면 Gemini입니다.

| 값 | 모델 | 잘하는 것 |
|---|---|---|
| `seedance` | `dreamina-seedance-2-5-260628` | **4~30초** 길이 지정(`duration`), 모든 비율(16:9, 9:16, 4:3, 3:4, 1:1, 21:9, 9:21, adaptive), 480p/720p/1080p, 첫/마지막 프레임 지정, 레퍼런스 이미지 최대 9장, 레퍼런스 영상(URL), 소리 켜기/끄기 |
| `seedance-fast` | `dreamina-seedance-2-0-fast-260128` | 싸고 빠른 Seedance — 시안·테스트용 |
| `gemini` | `gemini-omni-1.1-flash` | 자기가 만든 영상을 대화하듯 이어서 수정·연장, 360p~4k, 16:9 / 9:16만, 이미지 최대 3장 |

- 이미지 1장 → 첫 프레임, 2장 → 첫/마지막 프레임, 3장 이상 → 인물·물건 레퍼런스.
  Seedance는 `image_mode`(`first_frame` / `first_last` / `reference`)로 직접 정할 수 있습니다.
  첫 프레임을 고정하지 않고 캐릭터만 유지하려면 1~2장이라도 `reference`.
- Seedance는 영상의 **마지막 프레임을 이미지로 따로 저장**하고 코드를 줍니다. 다음 컷의 첫 이미지로 넣으면 컷이 자연스럽게 이어집니다.
- 끝난 영상의 코드를 `continue_from`에 넣고 "밤으로 바꿔", "이어서 카메라가 뒤로 빠짐"처럼 바꿀 점만 쓰면 수정·연장됩니다.
  Gemini는 대화 기록으로, Seedance는 원본 영상 링크를 레퍼런스 영상으로 넘겨서 처리합니다.
  Seedance 결과 링크는 **약 24시간 뒤 만료**되므로 그 뒤에는 마지막 프레임 코드로 새로 이어 가세요.
- `reference_videos`(Seedance): 움직임·카메라·편집 참고용 공개 http(s) 영상 URL 최대 3개. 내 PC의 mp4 파일은 직접 넘길 수 없습니다.
- 1~5분 걸립니다. 결과는 `OUTPUT_DIR`에 `.mp4`로 저장되고 대화창 미리보기는 없습니다.
- 네거티브 프롬프트가 없으니 피할 것은 프롬프트에 적습니다 ("no dialogue", "no text on screen").
  Seedance는 "Shot 1: … Shot 2: …"처럼 컷별로 쓴 프롬프트를 잘 따릅니다.
- 비용: Seedance 2.5는 토큰 과금이라 해상도·길이에 비례합니다(5초 16:9 기준 대략 480p 0.5달러, 720p 1.2달러, 1080p 2.8달러).
  테스트는 480p나 `seedance-fast`로 하세요.
- BytePlus ModelArk 콘솔에서 해당 모델을 **활성화(Activate)**해야 호출됩니다. 실제 사람 얼굴 사진은 입력이 거절될 수 있습니다.

## 레퍼런스 이미지 넘기는 법

`reference_images` / `image`에는 아래 중 무엇이든 넣을 수 있습니다.

- **파일 경로** — `D:\ref\girl.png`, `~/Desktop/a.jpg`. PNG/JPG/WebP 외에 TGA·TIFF·BMP도 자동 PNG 변환
- **클립보드** — Maya 뷰포트 캡처나 이미지 복사 후 "클립보드 이미지로 ~" (`clipboard`). 탐색기에서 파일 복사한 것도 가능
- **이전 결과 코드** — 8자리 (`list_recent` 참고)
- **URL** — `https://...png`

> 대화창에 **첨부한 이미지는 MCP 서버로 전달되지 않습니다.** 경로를 알려주거나 클립보드로 넘기세요.

예시:

```
D:\char\ref_tpose.png 이 캐릭터를 디즈니풍 3D로, A포즈 정면, 배경 연회색
클립보드 이미지 스타일 그대로 머리·상의·하의·신발 각각 분리해줘
a1b2c3d4 에서 신발만 빨간색으로
```

## 모바일 사용 (PC가 켜져 있을 때)

### 설정 (한 번만)

1. **Google Cloud** ([console.cloud.google.com](https://console.cloud.google.com/))
   1. 새 프로젝트 → **API 및 서비스 → 라이브러리 → Google Drive API → 사용**
   2. **OAuth 동의 화면** — 외부(External), 앱 이름 아무거나, 테스트 사용자에 내 Gmail 추가 →
      **앱 게시(프로덕션으로 푸시)**. 게시하지 않으면 로그인 토큰이 7일 뒤 만료됩니다. 개인용이라 검증 심사는 필요 없습니다.
   3. **사용자 인증 정보 → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID → 데스크톱 앱** → JSON 다운로드
2. 받은 `client_secret_….json`을 이 폴더에 넣고 **`setup_mobile.bat`** 실행
   - 브라우저 로그인 → "Google에서 확인하지 않은 앱" 화면은 **고급 → (안전하지 않음)으로 이동** → 허용
   - 드라이브 연결·OpenAI 키를 확인하고, 워커를 백그라운드로 켜고, Windows 로그인 시 자동 실행을 등록합니다.
3. **claude.ai** — Settings → Connectors에서 **Google Drive**를 연결하고, 쓰는 대화(프로젝트)에서 켭니다.
4. **절전** — Windows 설정 → 전원에서 "절전 모드" 시간을 "안 함"으로, 또는 `.env`에 `KEEP_AWAKE=1`
   (워커가 도는 동안만 절전을 막고 화면은 꺼집니다).

### 쓰는 법

휴대폰 Claude 앱에서 평소처럼 "~ 그려줘". Claude가 `requests/`에 요청을 올리고, 1분쯤 뒤 "결과 확인"이라고 하면 링크를 줍니다.
레퍼런스 사진은 드라이브 앱으로 `내 드라이브/openai_image_mcp/refs/`에 올리고 "드라이브 최신 이미지로 ~"라고 하세요.

| 레퍼런스 값 | 의미 |
|---|---|
| `latest` | `refs/`에 가장 최근 올린 이미지 |
| `name:<이름 일부>` | `refs/`·`outputs/`에서 이름에 그 글자가 들어간 최신 이미지 |
| 드라이브 링크 | 그 파일 (내 드라이브 어디든) |
| 8자리 코드 | PC·모바일 결과 어느 쪽이든 |
| 이전 결과 id | `outputs/<id>.png` |

요청 파일 형식 (`requests/req_<id>.json`, `prompt`만 필수):

```json
{"prompt": "detailed English prompt", "references": ["latest"], "size": "1024x1536",
 "quality": "medium", "background": "auto", "output_format": "png",
 "provider": "auto", "edit": false}
```

`provider`: `auto` / `openai` / `gemini` / `gemini-pro`. 기존 그림 수정이면 `"edit": true` (auto일 때 Gemini 사용).

영상 요청:

```json
{"type": "video", "prompt": "...", "references": ["latest"], "provider": "auto",
 "aspect_ratio": "9:16", "resolution": "720p", "duration": 8, "generate_audio": true,
 "image_mode": "auto", "reference_videos": [], "continue_from": "<이전 영상 코드, 선택>"}
```

`provider`: `auto`(Seedance 우선) / `seedance` / `seedance-fast` / `gemini`. `duration`·`image_mode`·`reference_videos`·`generate_audio`는 Seedance용입니다.

결과 `outputs/<id>.result.json`: 이미지는 `{"status": "done", "media": "image", "code", "provider", "model", "image": {"id","name","url"}, "local_path", "seconds", ...}`,
영상은 `"media": "video"`에 `"video": {"id","name","url"}` (`outputs/<id>.mp4`), Seedance면 `"last_frame": {"code","id","name","url"}`도
또는 `{"status": "error", "error": "..."}`. 드라이브 `openai_image_mcp/worker_status.json`의 `last_seen`이 몇 분 이상 지났으면
PC가 꺼졌거나 잠들었거나 워커가 멈춘 것입니다.

### 워커 관리

```bat
.venv\Scripts\python.exe drive_worker.py --status     :: 실행 여부, 최근 로그
.venv\Scripts\python.exe drive_worker.py --stop       :: 중지
.venv\Scripts\python.exe drive_worker.py --start      :: 백그라운드 시작 (창 없음)
.venv\Scripts\python.exe drive_worker.py              :: 앞에서 실행 (로그 보면서 테스트, Ctrl+C)
.venv\Scripts\python.exe drive_worker.py --remove-autostart
```

로그: `logs\drive_worker.log`. 동시에 최대 3장을 만들고, PC가 생성 도중 꺼지면 다음 시작 때 그 요청을 "중단됨"으로 알려 줍니다.
드라이브 로그인 정보는 `.gdrive_token.json`(이 폴더)에 저장되며 git에는 올라가지 않습니다.

## 환경변수

| 이름 | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | | OpenAI API 키 (있으면 우선) |
| `OPENAI_API_KEY_FILE` | | 키가 들어 있는 파일 경로 (`OPENAI_API_KEY`가 없을 때 사용) |
| `OUTPUT_DIR` | `~/Pictures/openai_image_mcp` | 결과 저장 폴더 (`.index.jsonl`에 코드·프롬프트 기록) |
| `OPENAI_IMAGE_MODEL` | `gpt-image-2` | 모델 |
| `GEMINI_API_KEY` | | Gemini API 키 (있으면 우선) |
| `GEMINI_API_KEY_FILE` | | Gemini 키 파일 경로 |
| `GEMINI_IMAGE_MODEL` | `gemini-nano-banana-2.1` | `provider: gemini` 모델 |
| `GEMINI_IMAGE_MODEL_PRO` | `gemini-3-pro-image` | `provider: gemini-pro` 모델 |
| `GEMINI_VIDEO_MODEL` | `gemini-omni-1.1-flash` | 영상 모델 |
| `GEMINI_TIMEOUT` / `GEMINI_VIDEO_TIMEOUT` | `240` / `900` | Gemini 요청 1건 최대 대기(초) |
| `SEEDANCE_API_KEY` / `ARK_API_KEY` | | Seedance(BytePlus ModelArk) API 키 (있으면 우선) |
| `SEEDANCE_API_KEY_FILE` | | Seedance 키 파일 경로. 없으면 OpenAI/Gemini 키 폴더 옆 `byteplus/`에서 자동 탐색 |
| `SEEDANCE_API_BASE` | `https://ark.ap-southeast.bytepluses.com/api/v3` | 중국 Volcengine이면 `https://ark.cn-beijing.volces.com/api/v3` + `doubao-seedance-*` 모델 |
| `SEEDANCE_MODEL` / `SEEDANCE_MODEL_FAST` | `dreamina-seedance-2-5-260628` / `dreamina-seedance-2-0-fast-260128` | `provider: seedance` / `seedance-fast` 모델 |
| `SEEDANCE_TIMEOUT` | `1200` | Seedance 작업 1건 최대 대기(초) |
| `SEEDANCE_MAX_IMAGES` | `9` | 영상 1건에 넣을 수 있는 이미지 수 |
| `SEEDANCE_WATERMARK` | `0` | `1`이면 워터마크 |
| `DEFAULT_VIDEO_PROVIDER` | `seedance` | `auto`일 때 기본 영상 모델 (`seedance` / `seedance-fast` / `gemini`) |
| `DEFAULT_QUALITY` | `medium` | low / medium / high / auto |
| `OPENAI_TIMEOUT` | `240` | OpenAI 요청 1건 최대 대기(초), 재시도 없음 |
| `TOOL_WAIT_SECONDS` | `50` | 도구 호출 1번이 기다리는 시간 |
| `PREVIEW_MAX_PX` | `768` | Claude에게 보내는 미리보기 크기 |
| `IMAGE_BACKEND` | `openai` | `fake`면 API 호출 없이 더미 이미지 |
| `GDRIVE_ROOT_NAME` | `openai_image_mcp` | (모바일) 내 드라이브 최상위 폴더 이름 |
| `GDRIVE_FOLDER_ID` | | (모바일) 이미 있는 폴더를 쓰려면 그 ID |
| `POLL_SECONDS` | `5` | (모바일) 요청 확인 간격 |
| `MAX_CONCURRENT` | `3` | (모바일) 동시에 만드는 장 수 |
| `KEEP_AWAKE` | `0` | (모바일) `1`이면 워커가 도는 동안 절전 방지 |

## 테스트

```bash
# API 호출 없이 동작 확인
set IMAGE_BACKEND=fake          # PowerShell: $env:IMAGE_BACKEND="fake"
.venv\Scripts\python.exe server.py   # stdio 대기 상태면 정상 (Ctrl+C 종료)
```

로그는 stderr로만 나갑니다(stdout은 MCP 통신용). Claude Desktop 로그: `%APPDATA%\Claude\logs\mcp-server-openai-image-local.log`
