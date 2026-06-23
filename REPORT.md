# Fabric Data Agent - 구현 및 배포 리포트

> 최종 업데이트: 2026-03-24 | 상태: 운영 중

---

## 서비스 정보

| 항목 | 값 |
| --- | --- |
| 외부 URL | <https://dataagent.mskr-datanai.net> |
| Data Agent | CosmosDB-Data-Agent2 |
| Workspace | \_\_WS\_Metrics\_Analyzer |
| Namespace | `fabric-data-agent` |
| 인증 | Azure Workload Identity (UAMI: mskr-aks-cluster-uami) |
| 이미지 | `mskraksclustercr.azurecr.io/fabric-data-agent:latest` |

---

## 파일 구조

```text
FabricDataAgent/
├── app.py               # Streamlit 앱 (Fabric Data Agent 채팅 UI)
├── requirements.txt     # Python 의존성
├── Dockerfile           # 컨테이너 이미지 빌드
├── REPORT.md            # 이 문서
└── aks/
    ├── namespace.yaml       # Kubernetes Namespace
    ├── serviceaccount.yaml  # Workload Identity용 ServiceAccount
    ├── deployment.yaml      # Pod 배포
    ├── service.yaml         # ClusterIP Service (port 80 → 8501)
    ├── ingress.yaml         # NGINX Ingress + TLS (cert-manager)
    └── deploy.azcli         # 전체 배포 자동화 스크립트
```

---

## 아키텍처

### 네트워크 흐름

```text
Internet (HTTPS:443)
  → NSG (포트 443 허용)
  → NGINX Ingress LB (20.249.146.99)
  → NGINX Ingress Controller (ingress-nginx)
  → TLS 종료 (cert-manager letsencrypt-prod, secret: fabric-data-agent-tls)
  → Streamlit Service (ClusterIP:80 → Pod:8501)
  → Fabric Data Agent Pod
  → api.fabric.microsoft.com (Fabric Data Agent API, outbound)
```

### 인증 흐름 (Workload Identity)

```text
Streamlit Pod
  └── WorkloadIdentityCredential (azure-identity)
        └── AZURE_FEDERATED_TOKEN_FILE (AKS Mutating Webhook 자동 주입)
              → Azure AD Token Exchange (Federated Identity)
                → UAMI mskr-aks-cluster-uami
                  → Bearer Token (scope: https://api.fabric.microsoft.com/.default)
                    → Fabric Data Agent Assistants API
```

### Fabric API 호출 흐름 (Assistants API)

```text
사용자 질문
  → beta.assistants.create(model="not used")   # 세션 최초 1회
  → beta.threads.create()                       # 세션 최초 1회
  → beta.threads.messages.create(role="user")   # 매 질문마다
  → beta.threads.runs.create()                  # Run 실행
  → beta.threads.runs.retrieve() × N            # 폴링 (2초 간격, 최대 5분)
  → beta.threads.messages.list()                # 응답 수신
```

---

## 핵심 구현 사항

### 1. Fabric OpenAI 클라이언트 (FabricOpenAI)

Microsoft 공식 문서 기반 구현. `_prepare_options` 오버라이드로 매 요청마다 신선한 Bearer 토큰 주입.

```python
class FabricOpenAI(OpenAI):
    def __init__(self, credential, **kwargs):
        default_query["api-version"] = "2024-05-01-preview"
        super().__init__(api_key="", base_url=FABRIC_BASE_URL, ...)

    def _prepare_options(self, options):
        headers["Authorization"] = f"Bearer {credential.get_token(FABRIC_SCOPE).token}"
        headers["ActivityId"] = str(uuid.uuid4())
```

핵심 설정:

- `api-version=2024-05-01-preview` — query parameter 필수
- `api_key=""` — 빈 문자열 (Bearer 토큰으로 인증)
- `ActivityId` — 요청마다 고유 UUID (Fabric 추적용)

### 2. Chat vs Assistants API

| 구분 | Chat Completions | Assistants API (채택) |
| --- | --- | --- |
| 엔드포인트 | `/chat/completions` | `/assistants`, `/threads`, `/runs` |
| 대화 맥락 | 클라이언트가 history 전송 | Fabric 서버가 thread로 유지 |
| 응답 방식 | 동기 | 비동기 (Run 폴링) |
| Fabric 지원 | ❌ 404 | ✅ 정상 동작 |

### 3. 세션 관리

- 첫 질문: Assistant + Thread 생성 → `st.session_state`에 저장
- 이후 질문: 동일 Thread 재사용 (대화 맥락 서버 유지)
- 초기화 버튼: Thread 삭제 → 새 세션 시작

---

## 배포 완료 현황 (2026-03-24)

| 단계 | 항목 | 상태 |
| --- | --- | --- |
| 인프라 | Namespace `fabric-data-agent` | ✅ |
| 인프라 | ServiceAccount + UAMI 어노테이션 | ✅ |
| 인증 | Federated Credential (AKS OIDC ↔ UAMI) | ✅ |
| 인증 | Workload Identity 환경변수 주입 확인 | ✅ |
| 네트워크 | DNS A 레코드 (`dataagent.mskr-datanai.net` → `20.249.146.99`) | ✅ |
| 네트워크 | CoreDNS Hairpin NAT (`dataagent` 추가) | ✅ |
| 네트워크 | TLS 인증서 (Let's Encrypt, 만료: 2026-06-22) | ✅ |
| 앱 | Fabric API 인증 (401 → 200) | ✅ |
| 앱 | Assistants API 호출 및 응답 수신 | ✅ |
| 앱 | ACR 이미지 (`de5`) 빌드 & 배포 | ✅ |

---

## Azure 권한 설정 (완료)

### Fabric 테넌트 설정

- **Service principals can call Fabric public APIs**: Enabled
- 허용 그룹: `SG-FBARICAPP`, `SG-ENTRA-SP`, `SG-PBIEMBEDAPP`, `SG-V2VAPP`, `Fabric-DataAgent-SG`
- `mskr-aks-cluster-uami` → `Fabric-DataAgent-SG` 멤버 확인 완료

### Workspace 접근 권한

- **Workspace**: `__WS_Metrics_Analyzer`
- **`mskr-aks-cluster-uami`**: Contributor 역할

### Workload Identity Federated Credential

```text
이름: fabric-data-agent-federated
Subject: system:serviceaccount:fabric-data-agent:fabric-data-agent-sa
Issuer: https://koreacentral.oic.prod-aks.azure.com/62d92304-.../5165b538-.../
```

---

## 주요 설정 정보

| 항목 | 값 |
| --- | --- |
| Fabric Base URL | `https://api.fabric.microsoft.com/v1/workspaces/e0d93e5a-.../dataagents/587d71bb-.../aiassistant/openai` |
| API Version | `2024-05-01-preview` |
| Fabric Scope | `https://api.fabric.microsoft.com/.default` |
| UAMI Client ID | `1cdb01c5-ab92-457a-b1a7-ce84cf1c8789` |
| UAMI Object ID | `9e38979b-efc1-4e9c-a7af-0c9431cce1ca` |
| Tenant ID | `62d92304-21d1-41d7-bea9-0b468619c963` |
| AKS OIDC Issuer | `https://koreacentral.oic.prod-aks.azure.com/62d92304-.../5165b538-.../` |
| Streamlit Port | `8501` |
| NGINX Ingress LB IP | `20.249.146.99` |
| NGINX Ingress ClusterIP | `10.0.222.172` |

---

## 운영 명령어

```bash
# 클러스터 연결
az aks get-credentials --resource-group AZ-AKS-RG --name mskr-aks-cluster --overwrite-existing
kubelogin convert-kubeconfig -l azurecli

# Pod 상태
kubectl get pods -n fabric-data-agent

# 로그 확인
kubectl logs -n fabric-data-agent -l app=fabric-data-agent -f

# TLS 인증서 상태
kubectl get certificate -n fabric-data-agent

# 이미지 재빌드 & 배포
cd /Users/taehwankim/Repos/GitRepos/Python/FabricDataAgent
az acr build --registry mskraksclustercr --image fabric-data-agent:latest --file Dockerfile .
kubectl rollout restart deployment/fabric-data-agent -n fabric-data-agent
kubectl rollout status deployment/fabric-data-agent -n fabric-data-agent
```

---

## 트러블슈팅 기록

### 1. 401 Unauthorized (해결)

- **원인**: `mskr-aks-cluster-uami`가 Fabric 테넌트 설정 허용 그룹에 포함 여부 확인 필요
- **해결**: Workspace에 UAMI Contributor 추가 + `Fabric-DataAgent-SG` 그룹 멤버십 확인

### 2. 404 EntityNotFound — `/aiassistant/openai/chat/completions` (해결)

- **원인**: Fabric Data Agent는 `chat/completions`를 지원하지 않음. Microsoft 공식 문서 기준 **Assistants API (beta)** 사용 필요
- **해결**: `app.py` 전면 수정 — `FabricOpenAI` 클래스 + Assistants API 패턴 적용

  - `api-version=2024-05-01-preview` query parameter 추가
  - `_prepare_options` 오버라이드로 Bearer 토큰 + ActivityId 헤더 주입

### 3. CoreDNS Hairpin NAT (해결)

- **원인**: cert-manager HTTP01 챌린지가 내부 → 외부 LB → 클러스터 hairpin 불가
- **해결**: `coredns-custom` ConfigMap에 `dataagent.mskr-datanai.net → 10.0.222.172` 추가

### 4. NGINX configuration-snippet 비활성화 (해결)

- **원인**: 클러스터에서 `configuration-snippet` annotation 비활성화
- **해결**: `nginx.ingress.kubernetes.io/websocket-services` annotation으로 대체
