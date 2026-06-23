import os
import time
import json
import uuid
import typing as t
import logging
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from azure.identity import (
    WorkloadIdentityCredential,
    ManagedIdentityCredential,
    DefaultAzureCredential,
)
from openai import OpenAI
from openai._models import FinalRequestOptions
from openai._types import Omit
from openai._utils import is_given

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 설정 ────────────────────────────────────────────────────
FABRIC_BASE_URL = os.getenv(
    "FABRIC_BASE_URL",
    "https://api.fabric.microsoft.com/v1/workspaces/"
    "e0d93e5a-6587-482d-91c0-2b0a489e6633/dataagents/"
    "587d71bb-6376-4d7f-ad5a-939836aeec12/aiassistant/openai",
)
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "1cdb01c5-ab92-457a-b1a7-ce84cf1c8789")
API_VERSION = "2024-05-01-preview"
POLL_INTERVAL_SEC = 2
POLL_TIMEOUT_SEC = 300

# 분할 출력(방안 B) 설정: 한 번에 출력 요청할 행 수와 최대 배치 수
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "25"))
MAX_BATCHES = int(os.getenv("MAX_BATCHES", "20"))

FIRST_BATCH_INSTRUCTION = (
    "\n\n[출력 규칙] 위 질문의 결과를 한 행도 빠짐없이 일관된 정렬 순서로 반환합니다. "
    "지금은 그중 처음 {n}건만 마크다운 표(헤더 포함)로만 출력하세요. "
    "표 이외의 설명, 요약, 생략 표시는 절대 넣지 마세요."
)

NEXT_BATCH_INSTRUCTION = (
    "직전 질문과 완전히 동일한 결과·정렬 순서를 기준으로, "
    "{start}번째 행부터 {end}번째 행까지를 이어서 마크다운 표(헤더 포함)로만 출력하세요. "
    "표 이외의 설명·생략 표시는 넣지 말고, 해당 구간에 더 이상 데이터가 없으면 정확히 '없음' 한 단어만 답하세요."
)


# ── 인증 ────────────────────────────────────────────────────
_credential = None


def get_credential():
    """AKS Workload Identity → Managed Identity → DefaultAzureCredential 순 시도 (1회 캐시)"""
    global _credential
    if _credential is not None:
        return _credential

    federated_token_file = os.getenv("AZURE_FEDERATED_TOKEN_FILE", "")
    if federated_token_file and os.path.exists(federated_token_file):
        logger.info("Using WorkloadIdentityCredential")
        _credential = WorkloadIdentityCredential()
        return _credential
    try:
        cred = ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)
        cred.get_token(FABRIC_SCOPE)
        logger.info("Using ManagedIdentityCredential")
        _credential = cred
        return _credential
    except Exception:
        pass
    logger.info("Using DefaultAzureCredential (local dev)")
    _credential = DefaultAzureCredential()
    return _credential


# ── Fabric OpenAI 클라이언트 ─────────────────────────────────
class FabricOpenAI(OpenAI):
    """Microsoft 공식 문서 방식 - Assistants API (beta) 사용"""

    def __init__(self, credential, **kwargs: t.Any) -> None:
        self._credential = credential
        default_query = kwargs.pop("default_query", {})
        default_query["api-version"] = API_VERSION
        super().__init__(
            api_key="",
            base_url=FABRIC_BASE_URL,
            default_query=default_query,
            **kwargs,
        )

    def _prepare_options(self, options: FinalRequestOptions) -> None:
        headers: dict[str, str | Omit] = (
            {**options.headers} if is_given(options.headers) else {}
        )
        options.headers = headers
        token = self._credential.get_token(FABRIC_SCOPE).token
        headers["Authorization"] = f"Bearer {token}"
        if "Accept" not in headers:
            headers["Accept"] = "application/json"
        if "ActivityId" not in headers:
            headers["ActivityId"] = str(uuid.uuid4())
        return super()._prepare_options(options)


def get_fabric_client() -> FabricOpenAI:
    return FabricOpenAI(credential=get_credential())


# ── 세션 저장소 (인메모리) ───────────────────────────────────
# session_id -> {"assistant_id": str, "thread_id": str}
_sessions: dict[str, dict[str, str]] = {}


def create_session() -> str:
    client = get_fabric_client()
    assistant = client.beta.assistants.create(model="not used")
    thread = client.beta.threads.create()
    session_id = str(uuid.uuid4())
    _sessions[session_id] = {
        "assistant_id": assistant.id,
        "thread_id": thread.id,
    }
    logger.info(
        "Session %s created (assistant=%s, thread=%s)",
        session_id,
        assistant.id,
        thread.id,
    )
    return session_id


def delete_session(session_id: str) -> None:
    sess = _sessions.pop(session_id, None)
    if not sess:
        return
    try:
        client = get_fabric_client()
        client.beta.threads.delete(thread_id=sess["thread_id"])
        logger.info("Thread deleted: %s", sess["thread_id"])
    except Exception as e:  # noqa: BLE001
        logger.warning("Thread 삭제 실패 (무시): %s", e)


# ── Data Agent 호출 (Assistants API) ────────────────────────
def _ask_once(client: FabricOpenAI, thread_id: str, assistant_id: str, content: str):
    """스레드에 메시지를 추가하고 Run을 실행한 뒤, 응답 텍스트와 run_id를 반환"""
    client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=content,
    )
    run = client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=assistant_id,
    )

    terminal_states = {"completed", "failed", "cancelled", "requires_action"}
    start_time = time.time()
    while run.status not in terminal_states:
        if time.time() - start_time > POLL_TIMEOUT_SEC:
            raise TimeoutError(f"응답 대기 시간 초과 ({POLL_TIMEOUT_SEC}초)")
        time.sleep(POLL_INTERVAL_SEC)
        run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
        logger.info("Run status: %s", run.status)

    if run.status != "completed":
        raise RuntimeError(f"Run 실패: {run.status}")

    messages = client.beta.threads.messages.list(
        thread_id=thread_id, order="desc", limit=1
    )
    answer_text = "응답을 찾을 수 없습니다."
    for msg in messages:
        if msg.role == "assistant":
            text_parts = [
                part.text.value
                for part in msg.content
                if getattr(part, "type", None) == "text"
            ]
            if text_parts:
                answer_text = "\n\n".join(text_parts)
            break
    return answer_text, run.id


def call_data_agent(session_id: str, question: str) -> dict:
    sess = _sessions.get(session_id)
    if not sess:
        raise KeyError(session_id)

    client = get_fabric_client()
    thread_id = sess["thread_id"]
    assistant_id = sess["assistant_id"]

    # ── 방안 B: 분할 출력 후 병합 ──────────────────────────────
    first_prompt = question + FIRST_BATCH_INSTRUCTION.format(n=BATCH_SIZE)
    first_text, first_run_id = _ask_once(client, thread_id, assistant_id, first_prompt)

    frames = []
    df0 = parse_output_to_df(first_text)
    if df0 is not None and len(df0) > 0:
        frames.append(df0)

    if not frames:
        for item in extract_query_results(client, thread_id, first_run_id):
            d = parse_output_to_df(item.get("output"))
            if d is not None and len(d) > 0:
                frames.append(d)

    if frames and len(frames[-1]) >= BATCH_SIZE:
        for b in range(1, MAX_BATCHES):
            start = b * BATCH_SIZE + 1
            end = (b + 1) * BATCH_SIZE
            text, _ = _ask_once(
                client,
                thread_id,
                assistant_id,
                NEXT_BATCH_INSTRUCTION.format(start=start, end=end),
            )
            if "없음" in text and "|" not in text:
                break
            df = parse_output_to_df(text)
            if df is None or len(df) == 0:
                break
            frames.append(df)
            if len(df) < BATCH_SIZE:
                break

    combined = None
    if frames:
        try:
            combined = pd.concat(frames, ignore_index=True)
            combined = combined.drop_duplicates(ignore_index=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("배치 병합 실패: %s", e)
            combined = frames[0]

    if combined is not None and len(combined) > 0:
        payload = df_to_payload(combined)
        return {
            "text": f"조회 결과 {len(combined)}건을 표로 표시합니다.",
            "columns": payload["columns"],
            "rows": payload["rows"],
        }

    # 표가 없으면 텍스트 답변에서 마크다운 표 폴백 파싱
    fallback_df = _markdown_table_to_df(first_text)
    if fallback_df is not None and len(fallback_df) > 0:
        payload = df_to_payload(fallback_df)
        return {
            "text": f"조회 결과 {len(fallback_df)}건을 표로 표시합니다.",
            "columns": payload["columns"],
            "rows": payload["rows"],
        }

    return {"text": first_text, "columns": [], "rows": []}


def extract_query_results(client, thread_id, run_id):
    """Run step의 툴 호출에서 실행된 쿼리와 전체 결과를 추출 (폴백)"""
    results = []
    try:
        raw = client.beta.threads.runs.steps.with_raw_response.list(
            thread_id=thread_id,
            run_id=run_id,
            order="asc",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Run step 조회 실패 (무시): %s", e)
        return results

    payload = None
    for getter in (
        lambda r: r.json(),
        lambda r: json.loads(r.text),
        lambda r: r.http_response.json(),
    ):
        try:
            payload = getter(raw)
            break
        except Exception:  # noqa: BLE001
            continue
    if not isinstance(payload, dict):
        logger.warning("Run step 응답 파싱 실패")
        return results

    for step in payload.get("data") or []:
        details = step.get("step_details") or {}
        for tc in details.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            query, output = None, None

            mf = tc.get("microsoft_fabric")
            if tc.get("type") == "fabric_dataagent" or isinstance(mf, dict):
                if isinstance(mf, dict):
                    query = mf.get("input") or mf.get("Input") or mf.get("query")
                    output = mf.get("output") or mf.get("Output")
                    if not output:
                        str_vals = [v for v in mf.values() if isinstance(v, str)]
                        output = max(str_vals, key=len) if str_vals else None
                else:
                    output = mf

            elif tc.get("type") == "function" and isinstance(tc.get("function"), dict):
                fn = tc["function"]
                name = str(fn.get("name") or "")
                if "fewshots" in name.lower():
                    continue
                output = fn.get("output")
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        a = json.loads(args)
                        query = a.get("query") or a.get("sql") or a.get("expression") or args
                    except Exception:  # noqa: BLE001
                        query = args
                elif isinstance(args, dict):
                    query = args.get("query") or args.get("sql") or args.get("expression")

            if not output:
                continue
            df = parse_output_to_df(output)
            if df is not None and len(df) > 0:
                results.append({"query": query, "output": output})
    return results


def _markdown_table_to_df(text):
    """마크다운 표 문자열을 DataFrame으로 변환 (실패 시 None)"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return None

    def split_row(ln):
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    header = split_row(lines[0])
    sep_chars = set(lines[1].replace("|", "").replace(":", "").replace("-", "").strip())
    body = lines[2:] if not sep_chars else lines[1:]
    rows = [r for r in (split_row(ln) for ln in body) if len(r) == len(header)]
    if not rows:
        return None
    try:
        return pd.DataFrame(rows, columns=header)
    except Exception:  # noqa: BLE001
        return None


def parse_output_to_df(output):
    """툴 출력 문자열/객체를 DataFrame으로 파싱 (실패 시 None)"""
    data = output
    if isinstance(output, str):
        text = output.strip()
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            if "|" in text and "\n" in text:
                return _markdown_table_to_df(text)
            return None

    records = None
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        cols = data.get("columns")
        body = data.get("rows") if data.get("rows") is not None else data.get("data")
        if isinstance(cols, list) and isinstance(body, list) and body and isinstance(body[0], list):
            col_names = [c.get("name") if isinstance(c, dict) else c for c in cols]
            try:
                return pd.DataFrame(body, columns=col_names)
            except Exception:  # noqa: BLE001
                return None
        for key in ("result", "results", "rows", "data", "value", "records", "table"):
            if isinstance(data.get(key), list):
                records = data[key]
                break

    if not records or not isinstance(records, list):
        return None
    try:
        return pd.DataFrame(records)
    except Exception:  # noqa: BLE001
        return None


def df_to_payload(df: pd.DataFrame) -> dict:
    """DataFrame을 JSON 직렬화 가능한 {columns, rows(2차원)}로 변환"""
    safe = df.astype(object).where(pd.notna(df), None)
    return {
        "columns": [str(c) for c in df.columns],
        "rows": safe.values.tolist(),
    }


# ── FastAPI ──────────────────────────────────────────────────
app = FastAPI(title="Fabric Data Agent API")


class ChatRequest(BaseModel):
    session_id: str
    question: str


class ResetRequest(BaseModel):
    session_id: str


@app.get("/api/health")
@app.get("/_stcore/health")
def health():
    return {"status": "ok"}


@app.post("/api/session")
def api_create_session():
    try:
        session_id = create_session()
    except Exception as e:  # noqa: BLE001
        logger.exception("세션 생성 실패")
        raise HTTPException(status_code=500, detail=str(e))
    return {"session_id": session_id}


@app.post("/api/chat")
def api_chat(req: ChatRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="질문이 비어 있습니다.")
    if req.session_id not in _sessions:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다. 새로고침하세요.")
    try:
        return call_data_agent(req.session_id, req.question)
    except Exception as e:  # noqa: BLE001
        logger.exception("Data Agent 호출 실패")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/session/reset")
def api_reset(req: ResetRequest):
    delete_session(req.session_id)
    session_id = create_session()
    return {"session_id": session_id}


# ── 정적 React 빌드 서빙 ─────────────────────────────────────
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=_STATIC_DIR / "assets"),
        name="assets",
    )

    @app.get("/")
    def index():
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        candidate = _STATIC_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(_STATIC_DIR / "index.html")
