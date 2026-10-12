# openai_image_mcp_local

[openai_image_mcp](https://github.com/sggim00/openai_image_mcp)의 **로컬(stdio) 버전**입니다.
별도 서버 없이 내 PC에서 Python으로 돌고, Claude Desktop / Claude Code가 직접 실행합니다.

```
사용자 ─ "D:\ref\girl.png 디즈니풍 A포즈로" ─▶ Claude ─ generate_image ─▶ server.py(내 PC) ─▶ OpenAI Images API
사용자 ◀─ 미리보기 + 저장 경로 ──────────────── Claude ◀─ 미리보기(JPEG) + 경로 + 코드 ─┘
```

## 원격 버전과 차이

| | openai_image_mcp (원격 HTTP) | openai_image_mcp_local |
|---|---|---|
| 실행 위치 | 외부에서 접근 가능한 서버 (직접 호스팅) | 내 PC (stdio) |
| 레퍼런스 이미지 | 업로드 페이지·구글 드라이브 → 코드 | **파일 경로 / 클립보드 / 코드 / URL** 직접 |
| 결과 저장 | 서버 디스크 + 구글 드라이브(선택) | `OUTPUT_DIR` 로컬 폴더 |
| 사용 가능한 곳 | claude.ai 웹·모바일·데스크톱 | **Claude Desktop 앱, Claude Code** (모바일 X) |

## 설치 (Windows)

1. Python 3.10 이상 설치
2. 이 폴더에서 `install.bat` 실행 → `.venv` 생성 + 패키지 설치 + `.env` 생성
3. `.env`에 키 지정 — 직접 `OPENAI_API_KEY=sk-...` 또는 키 파일 경로 `OPENAI_API_KEY_FILE=D:/.../xxx.api_key` (필요하면 `OUTPUT_DIR`도)
4. 스크립트가 출력한 블록을 `%APPDATA%\Claude\claude_desktop_config.json`에 추가:

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
| `wait_for_images` | 50초 안에 안 끝난 작업을 이어서 받아옴 |
| `list_recent` | 최근 결과 (코드, 경로, 프롬프트) |
| `open_output_folder` | 결과 폴더를 탐색기/Finder로 열기 |

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

## 환경변수

| 이름 | 기본값 | 설명 |
|---|---|---|
| `OPENAI_API_KEY` | | OpenAI API 키 (있으면 우선) |
| `OPENAI_API_KEY_FILE` | | 키가 들어 있는 파일 경로 (`OPENAI_API_KEY`가 없을 때 사용) |
| `OUTPUT_DIR` | `~/Pictures/openai_image_mcp` | 결과 저장 폴더 (`.index.jsonl`에 코드·프롬프트 기록) |
| `OPENAI_IMAGE_MODEL` | `gpt-image-2` | 모델 |
| `DEFAULT_QUALITY` | `medium` | low / medium / high / auto |
| `OPENAI_TIMEOUT` | `240` | OpenAI 요청 1건 최대 대기(초), 재시도 없음 |
| `TOOL_WAIT_SECONDS` | `50` | 도구 호출 1번이 기다리는 시간 |
| `PREVIEW_MAX_PX` | `768` | Claude에게 보내는 미리보기 크기 |
| `IMAGE_BACKEND` | `openai` | `fake`면 API 호출 없이 더미 이미지 |

## 테스트

```bash
# API 호출 없이 동작 확인
set IMAGE_BACKEND=fake          # PowerShell: $env:IMAGE_BACKEND="fake"
.venv\Scripts\python.exe server.py   # stdio 대기 상태면 정상 (Ctrl+C 종료)
```

로그는 stderr로만 나갑니다(stdout은 MCP 통신용). Claude Desktop 로그: `%APPDATA%\Claude\logs\mcp-server-openai-image-local.log`
