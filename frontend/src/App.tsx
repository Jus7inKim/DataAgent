import { FormEvent, useEffect, useRef, useState } from "react";
import { ChatResponse, createSession, resetSession, sendChat } from "./api";
import DataGrid from "./components/DataGrid";

interface Message {
  role: "user" | "assistant";
  text: string;
  columns?: string[];
  rows?: (string | number | null)[][];
  error?: boolean;
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [initError, setInitError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    createSession()
      .then(setSessionId)
      .catch((e) => setInitError(String(e.message ?? e)));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const question = input.trim();
    if (!question || !sessionId || loading) return;

    setMessages((m) => [...m, { role: "user", text: question }]);
    setInput("");
    setLoading(true);
    try {
      const res: ChatResponse = await sendChat(sessionId, question);
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          text: res.text,
          columns: res.columns,
          rows: res.rows,
        },
      ]);
    } catch (err: any) {
      setMessages((m) => [
        ...m,
        { role: "assistant", text: `오류: ${err.message ?? err}`, error: true },
      ]);
    } finally {
      setLoading(false);
    }
  }

  async function handleReset() {
    if (!sessionId || loading) return;
    setLoading(true);
    try {
      const newId = await resetSession(sessionId);
      setSessionId(newId);
      setMessages([]);
    } catch (err: any) {
      setInitError(String(err.message ?? err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <h2>설정</h2>
        <button className="btn" onClick={handleReset} disabled={!sessionId || loading}>
          대화 초기화
        </button>
        <div className="meta">
          <div className="meta-label">상태</div>
          <code>{sessionId ? "연결됨" : initError ? "오류" : "연결 중..."}</code>
        </div>
        {sessionId && (
          <div className="meta">
            <div className="meta-label">Session</div>
            <code>{sessionId.slice(0, 8)}…</code>
          </div>
        )}
      </aside>

      <main className="main">
        <header className="header">
          <h1>📊 Microsoft Fabric Data Agent</h1>
          <span className="subtitle">
            CosmosDB-Data-Agent2 · Azure Workload Identity · Assistants API
          </span>
        </header>

        {initError && (
          <div className="banner error">초기화 실패: {initError}</div>
        )}

        <div className="messages">
          {messages.length === 0 && !loading && (
            <div className="empty">질문을 입력해 데이터 에이전트와 대화하세요.</div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`message ${m.role}`}>
              <div className="bubble">
                <div className={`text ${m.error ? "error" : ""}`}>{m.text}</div>
                {m.role === "assistant" &&
                  m.columns &&
                  m.columns.length > 0 &&
                  m.rows &&
                  m.rows.length > 0 && (
                    <DataGrid columns={m.columns} rows={m.rows} />
                  )}
              </div>
            </div>
          ))}
          {loading && (
            <div className="message assistant">
              <div className="bubble">
                <div className="text loading">데이터를 조회하는 중…</div>
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        <form className="composer" onSubmit={handleSubmit}>
          <input
            type="text"
            value={input}
            placeholder="질문을 입력하세요"
            onChange={(e) => setInput(e.target.value)}
            disabled={!sessionId || loading}
          />
          <button type="submit" className="btn primary" disabled={!sessionId || loading}>
            전송
          </button>
        </form>
      </main>
    </div>
  );
}
