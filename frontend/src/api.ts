export interface ChatResponse {
  text: string;
  columns: string[];
  rows: (string | number | null)[][];
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export async function createSession(): Promise<string> {
  const res = await fetch("/api/session", { method: "POST" });
  const data = await handle<{ session_id: string }>(res);
  return data.session_id;
}

export async function resetSession(sessionId: string): Promise<string> {
  const res = await fetch("/api/session/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  });
  const data = await handle<{ session_id: string }>(res);
  return data.session_id;
}

export async function sendChat(
  sessionId: string,
  question: string
): Promise<ChatResponse> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, question }),
  });
  return handle<ChatResponse>(res);
}
