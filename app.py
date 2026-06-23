import os
import time
import json
import uuid
import typing as t
import logging
import pandas as pd
import streamlit as st
from azure.identity import WorkloadIdentityCredential, ManagedIdentityCredential, DefaultAzureCredential
from openai import OpenAI
from openai._models import FinalRequestOptions
from openai._types import Omit
from openai._utils import is_given

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 설정 ────────────────────────────────────────────────────
# Fabric Portal에서 Publish 후 복사한 URL
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

# 첫 배치 요청 지시문 (표만, 설명/생략표시 없이)
FIRST_BATCH_INSTRUCTION = (
    "\n\n[출력 규칙] 위 질문의 결과를 한 행도 빠짐없이 일관된 정렬 순서로 반환합니다. "
    "지금은 그중 처음 {n}건만 마크다운 표(헤더 포함)로만 출력하세요. "
    "표 이외의 설명, 요약, 생략 표시는 절대 넣지 마세요."
)

# 이어받기 배치 요청 지시문
NEXT_BATCH_INSTRUCTION = (
    "직전 질문과 완전히 동일한 결과·정렬 순서를 기준으로, "
    "{start}번째 행부터 {end}번째 행까지를 이어서 마크다운 표(헤더 포함)로만 출력하세요. "
    "표 이외의 설명·생략 표시는 넣지 말고, 해당 구간에 더 이상 데이터가 없으면 정확히 '없음' 한 단어만 답하세요."
)


# ── 인증 ────────────────────────────────────────────────────
@st.cache_resource
def get_credential():
    """AKS Workload Identity → Managed Identity → DefaultAzureCredential 순 시도"""
    federated_token_file = os.getenv("AZURE_FEDERATED_TOKEN_FILE", "")
    if federated_token_file and os.path.exists(federated_token_file):
        logger.info("Using WorkloadIdentityCredential")
        return WorkloadIdentityCredential()
    try:
        cred = ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)
        cred.get_token(FABRIC_SCOPE)
        logger.info("Using ManagedIdentityCredential")
        return cred
    except Exception:
        pass
    logger.info("Using DefaultAzureCredential (local dev)")
    return DefaultAzureCredential()


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
        # 매 요청마다 신선한 토큰 사용 (azure-identity 내부 캐시/갱신)
        token = self._credential.get_token(FABRIC_SCOPE).token
        headers["Authorization"] = f"Bearer {token}"
        if "Accept" not in headers:
            headers["Accept"] = "application/json"
        if "ActivityId" not in headers:
            headers["ActivityId"] = str(uuid.uuid4())
        return super()._prepare_options(options)


def get_fabric_client() -> FabricOpenAI:
    return FabricOpenAI(credential=get_credential())


# ── Data Agent 호출 (Assistants API) ────────────────────────
def ensure_session_ready(client: FabricOpenAI):
    """세션당 assistant + thread 1회 생성 후 재사용 (대화 맥락 유지)"""
    if "assistant_id" not in st.session_state:
        assistant = client.beta.assistants.create(model="not used")
        st.session_state.assistant_id = assistant.id
        logger.info("Assistant created: %s", assistant.id)

    if "thread_id" not in st.session_state:
        thread = client.beta.threads.create()
        st.session_state.thread_id = thread.id
        logger.info("Thread created: %s", thread.id)


def _ask_once(client, content: str):
    """스레드에 메시지를 추가하고 Run을 실행한 뒤, 응답 텍스트와 run_id를 반환"""
    client.beta.threads.messages.create(
        thread_id=st.session_state.thread_id,
        role="user",
        content=content,
    )
    run = client.beta.threads.runs.create(
        thread_id=st.session_state.thread_id,
        assistant_id=st.session_state.assistant_id,
    )

    terminal_states = {"completed", "failed", "cancelled", "requires_action"}
    start_time = time.time()
    while run.status not in terminal_states:
        if time.time() - start_time > POLL_TIMEOUT_SEC:
            raise TimeoutError(f"응답 대기 시간 초과 ({POLL_TIMEOUT_SEC}초)")
        time.sleep(POLL_INTERVAL_SEC)
        run = client.beta.threads.runs.retrieve(
            thread_id=st.session_state.thread_id,
            run_id=run.id,
        )
        logger.info("Run status: %s", run.status)

    if run.status != "completed":
        raise RuntimeError(f"Run 실패: {run.status}")

    messages = client.beta.threads.messages.list(
        thread_id=st.session_state.thread_id,
        order="desc",
        limit=1,
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


def call_data_agent(question: str) -> str:
    client = get_fabric_client()
    ensure_session_ready(client)

    # ── 방안 B: 분할 출력 후 병합 ──────────────────────────────
    # 1) 첫 배치 요청 (처음 BATCH_SIZE건을 표로만)
    first_prompt = question + FIRST_BATCH_INSTRUCTION.format(n=BATCH_SIZE)
    first_text, first_run_id = _ask_once(client, first_prompt)

    frames = []
    df0 = parse_output_to_df(first_text)
    if df0 is not None and len(df0) > 0:
        frames.append(df0)

    # 첫 배치가 표로 파싱되지 않으면(에이전트가 표 대신 거부/설명 응답) run step도 시도
    if not frames:
        for item in extract_query_results(client, st.session_state.thread_id, first_run_id):
            d = parse_output_to_df(item.get("output"))
            if d is not None and len(d) > 0:
                frames.append(d)

    # 2) 첫 배치가 가득 찼으면 이어받기 배치 반복
    if frames and len(frames[-1]) >= BATCH_SIZE:
        for b in range(1, MAX_BATCHES):
            start = b * BATCH_SIZE + 1
            end = (b + 1) * BATCH_SIZE
            text, _ = _ask_once(
                client, NEXT_BATCH_INSTRUCTION.format(start=start, end=end)
            )
            if "없음" in text and "|" not in text:
                break
            df = parse_output_to_df(text)
            if df is None or len(df) == 0:
                break
            frames.append(df)
            if len(df) < BATCH_SIZE:
                break

    # 3) 병합 + 중복 제거
    combined = None
    if frames:
        try:
            combined = pd.concat(frames, ignore_index=True)
            combined = combined.drop_duplicates(ignore_index=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("배치 병합 실패: %s", e)
            combined = frames[0]

    results = []
    if combined is not None and len(combined) > 0:
        results.append({"query": None, "df": combined})
        display_text = f"조회 결과 **{len(combined)}건**을 표로 표시합니다."
    else:
        display_text = first_text

    return {"text": display_text, "results": results}



def extract_query_results(client, thread_id, run_id):
    """Run step의 fabric_dataagent 툴 호출에서 실행된 쿼리와 전체 결과를 추출

    openai SDK가 커스텀 'fabric_dataagent' step 타입을 파싱하면서 microsoft_fabric
    필드가 누락될 수 있으므로, 원본 HTTP 응답(JSON)을 직접 읽는다.
    """
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

    # 실제 run step 구조 확인용 (컨테이너 로그)
    try:
        logger.info("run steps raw: %s", json.dumps(payload, default=str)[:4000])
    except Exception:  # noqa: BLE001
        pass

    for step in payload.get("data") or []:
        details = step.get("step_details") or {}
        for tc in details.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            query, output = None, None

            # (1) Foundry/Agent Service 래퍼: microsoft_fabric 필드
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

            # (2) Fabric aiassistant/openai 엔드포인트: function 툴 호출
            elif tc.get("type") == "function" and isinstance(tc.get("function"), dict):
                fn = tc["function"]
                name = str(fn.get("name") or "")
                # fewshots 로딩 등 데이터가 아닌 보조 호출은 건너뜀
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
            # 실제 표 데이터가 파싱되는 경우에만 그리드 후보로 채택
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
    # 두 번째 줄이 구분선(---)이면 본문은 3번째부터
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
            # JSON이 아니면 마크다운 표 파싱 시도
            if "|" in text and "\n" in text:
                return _markdown_table_to_df(text)
            return None

    records = None
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        # columns + rows(2차원) 형태 우선 처리
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


def render_full_data(query_results, fallback_text=None):
    """추출된 전체 쿼리 결과를 표(또는 원본)로 렌더링.

    run step에서 구조화된 결과를 얻지 못하면, 답변 텍스트에 포함된
    마크다운 표를 파싱해 그리드로 렌더링한다(폴백).
    """
    if not query_results:
        if fallback_text:
            df = _markdown_table_to_df(fallback_text)
            if df is not None and len(df) > 0:
                with st.expander(f"전체 데이터 ({len(df)}건)", expanded=True):
                    height = min(len(df) + 1, 200) * 35 + 3
                    st.dataframe(df, use_container_width=True, height=height)
        return
    for idx, item in enumerate(query_results):
        # 이미 DataFrame으로 병합된 결과(방안 B)는 그대로 사용
        df = item.get("df")
        if df is None:
            df = parse_output_to_df(item.get("output"))
        if df is not None:
            label = f"전체 데이터 ({len(df)}건)"
        else:
            label = "전체 데이터"
        if len(query_results) > 1:
            label = f"{label} #{idx + 1}"
        with st.expander(label, expanded=True):
            if item.get("query"):
                st.caption("실행된 쿼리")
                st.code(str(item["query"]))
            if df is not None:
                # 전체 행이 보이도록 행 수에 맞춰 높이 설정 (스크롤 없이 렌더링)
                row_h = 35
                height = min(len(df) + 1, 200) * row_h + 3
                st.dataframe(df, use_container_width=True, height=height)
            else:
                raw = str(item.get("output", ""))
                if "|" in raw and "\n" in raw:
                    st.markdown(raw)
                else:
                    st.code(raw)


def reset_session():
    """대화 초기화 - Fabric thread 삭제 후 세션 클리어"""
    if "thread_id" in st.session_state:
        try:
            client = get_fabric_client()
            client.beta.threads.delete(thread_id=st.session_state.thread_id)
            logger.info("Thread deleted: %s", st.session_state.thread_id)
        except Exception as e:
            logger.warning("Thread 삭제 실패 (무시): %s", e)

    for key in ["assistant_id", "thread_id", "messages"]:
        st.session_state.pop(key, None)


# ──────────────────────────────────────────────────────────────
# Streamlit UI
# ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Fabric Data Agent",
    page_icon="📊",
    layout="wide",
)

st.title("📊 Microsoft Fabric Data Agent")
st.caption("CosmosDB-Data-Agent2 · Azure Workload Identity · Assistants API")

with st.sidebar:
    st.header("설정")
    if st.button("대화 초기화", type="secondary"):
        reset_session()
        st.rerun()

    st.divider()
    st.markdown("**Data Agent**")
    agent_id = FABRIC_BASE_URL.split("/dataagents/")[1].split("/")[0]
    st.code(f"ID: {agent_id[:8]}...", language=None)
    st.markdown(f"**API Version**  \n`{API_VERSION}`")

    if "thread_id" in st.session_state:
        st.markdown("**Thread**")
        st.code(st.session_state.thread_id[:16] + "...", language=None)

# 채팅 기록 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []

# 기존 대화 출력
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message["role"] == "assistant":
            render_full_data(message.get("results"), fallback_text=message["content"])

# 사용자 입력
if prompt := st.chat_input("데이터에 대해 질문해 주세요..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("Fabric Data Agent가 분석 중..."):
            try:
                answer = call_data_agent(prompt)
                st.markdown(answer["text"])
                render_full_data(answer["results"], fallback_text=answer["text"])
                st.session_state.messages.append({
                    "role": "assistant",
                    "content": answer["text"],
                    "results": answer["results"],
                })
            except Exception as e:
                logger.error("Data Agent 호출 실패: %s", e, exc_info=True)
                st.error(f"오류: {e}")
