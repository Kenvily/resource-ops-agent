import type { EfficiencyReport } from "../types/efficiency";
export async function readUsageInvestigation(
  response: Response,
  onProgress: (message: string) => void,
  onTask?: (id: string) => void,
): Promise<EfficiencyReport> {
  if (!response.body) throw Error("浏览器不支持调查进度响应");
  const reader = response.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "",
    received = 0,
    result: EfficiencyReport | null = null;
  const line = (text: string) => {
    if (!text.trim()) return;
    const event = JSON.parse(text);
    if (event.type === "task") onTask?.(event.data.id);
    else if (event.type === "progress") onProgress(event.data.message);
    else if (event.type === "evidence")
      onProgress(`已完成：${event.data.label}`);
    else if (event.type === "result") result = event.data;
    else if (event.type === "error")
      throw Error(event.data.message || "调查未完成");
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > 1_300_000) throw Error("调查结果超过显示预算");
      buffer += decoder.decode(value, { stream: true });
      let end;
      while ((end = buffer.indexOf("\n")) >= 0) {
        line(buffer.slice(0, end));
        buffer = buffer.slice(end + 1);
      }
    }
    buffer += decoder.decode();
    line(buffer);
    if (!result) throw Error("调查响应中断，未收到完整结果");
    return result;
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
