# 📊 Microsoft Fabric Data Agent

**React(Vite + TypeScript) SPA + FastAPI 백엔드**로 구성된 애플리케이션입니다. 사용자가 자연어로 질문하면 **Microsoft Fabric Data Agent**에 질의하고, 결과를 정렬·스크롤 가능한 데이터 그리드로 보여줍니다.

Fabric 호출 인증(Managed Identity 토큰)은 **반드시 서버측(FastAPI)** 에서만 처리하고 브라우저에는 노출하지 않습니다. React는 백엔드 REST API만 호출합니다.

## 아키텍처

```
브라우저(React SPA)  ──REST──▶  FastAPI 백엔드  ──Bearer 토큰──▶  Fabric Data Agent
                                  (Assistants API · Workload/Managed Identity)
```

단일 컨테이너에서 FastAPI가 `/api/*` REST와 빌드된 React 정적 파일을 같은 오리진으로 서빙합니다.

## 프로젝트 구조

```
DataAgent/
├─ backend/
│  ├─ main.py            # FastAPI 앱 + Fabric 호출/인증/파싱 로직
│  └─ requirements.txt
├─ frontend/
│  ├─ src/
│  │  ├─ App.tsx         # 채팅 UI (세션·메시지 상태)
│  │  ├─ api.ts          # REST 클라이언트 (/api/*)
│  │  ├─ components/DataGrid.tsx  # 정렬·스크롤 데이터 그리드
│  │  ├─ main.tsx
│  │  └─ styles.css
│  ├─ index.html
│  ├─ package.json
│  ├─ tsconfig.json
│  └─ vite.config.ts     # 로컬 개발 시 /api → :8000 프록시
├─ Dockerfile            # 멀티스테이지: React 빌드 → FastAPI 서빙
├─ aca/deploy.azcli      # Azure Container Apps 배포
└─ aks/                  # AKS 매니페스트 + 배포 스크립트
```

## 백엔드 (`backend/main.py`)

| 영역 | 함수 / 클래스 | 설명 |
| --- | --- | --- |
| 인증 | `get_credential()` | WorkloadIdentity → ManagedIdentity → DefaultAzureCredential 순 시도 |
| 클라이언트 | `FabricOpenAI(OpenAI)` | 요청마다 Bearer 토큰·`Accept`·`ActivityId` 헤더 주입 |
| 세션 | `create_session()` / `delete_session()` | assistant·thread 생성/삭제 (인메모리 `session_id` 매핑) |
| 호출 | `_ask_once()` | 메시지 추가 → run 생성 → 종료까지 폴링 → 답변 텍스트 반환 |
| 핵심 | `call_data_agent()` | **방안 B** 분할 출력 + 병합 후 `{text, columns, rows}` 반환 |
| 파싱 | `parse_output_to_df()`, `_markdown_table_to_df()`, `df_to_payload()` | 응답 → `DataFrame` → JSON(columns + 2차원 rows) |
| 폴백 | `extract_query_results()` | run step 원본 HTTP에서 툴 결과 추출 |

### REST 엔드포인트

| 메서드 · 경로 | 설명 |
| --- | --- |
| `POST /api/session` | 세션(assistant + thread) 생성 → `{session_id}` |
| `POST /api/chat` | `{session_id, question}` → `{text, columns, rows}` |
| `POST /api/session/reset` | 기존 thread 삭제 후 새 세션 생성 |
| `GET /api/health` | 헬스 체크 |

### 방안 B: 분할 출력 후 병합

Fabric 게시 엔드포인트는 run step에 SQL 결과를 노출하지 않고 LLM 텍스트 답변만 반환하며, LLM은 긴 결과를 생략/요약합니다. 이를 피하기 위해:

1. **첫 배치**: 처음 `BATCH_SIZE`건만 "마크다운 표로만" 출력 요청.
2. 표가 가득 차면 다음 구간을 이어받아 반복 (`MAX_BATCHES` 한도, `'없음'` 응답 시 종료).
3. 모든 배치를 `pd.concat` 후 `drop_duplicates`로 병합.

## 프론트엔드 (`frontend/`)

- `App.tsx` — 마운트 시 세션 생성, 채팅 입력·기록 관리, 로딩/오류 표시, 대화 초기화.
- `api.ts` — `/api/session`, `/api/chat`, `/api/session/reset` 호출.
- `components/DataGrid.tsx` — 컬럼 헤더 클릭 정렬, sticky 헤더/행번호, 스크롤 그리드.

## 환경 변수 (백엔드)

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `FABRIC_BASE_URL` | Data Agent 엔드포인트 | Fabric Portal에서 Publish 후 복사한 URL |
| `AZURE_CLIENT_ID` | UAMI Client ID | Managed Identity client id |
| `AZURE_FEDERATED_TOKEN_FILE` | (자동) | AKS Workload Identity 토큰 파일 경로 |
| `BATCH_SIZE` | `25` | 한 배치당 요청 행 수 |
| `MAX_BATCHES` | `20` | 최대 배치 수 |

## 로컬 실행

백엔드:

```bash
cd backend
pip install -r requirements.txt
az login                       # DefaultAzureCredential 용
uvicorn main:app --port 8000 --reload
```

프론트엔드(별도 터미널):

```bash
cd frontend
npm install
npm run dev                    # http://localhost:5173 (/api → :8000 프록시)
```

## 컨테이너 (프로덕션)

```bash
docker build -t fabric-data-agent .
docker run -p 8501:8501 fabric-data-agent   # http://localhost:8501
```

멀티스테이지 빌드: ① `node:20-alpine`로 React를 `dist/` 빌드 → ② `python:3.12-slim`에서 FastAPI가 `backend/static`(=빌드 산출물)과 `/api`를 `8501` 포트로 서빙. 비루트 사용자(`appuser`)로 실행.

## 배포

- `aca/deploy.azcli` — Azure Container Apps (신규 RG, 기존 UAMI/ACR 재사용, `TARGET_PORT=8501`).
- `aks/` — AKS 매니페스트 (헬스 프로브 `/api/health`) 및 `deploy.azcli`.

배포 시 컨테이너에 Fabric API 권한을 가진 **User-Assigned Managed Identity**를 연결해야 합니다.
