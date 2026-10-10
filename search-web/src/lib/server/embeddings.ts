import { DIMENSIONS, DOCUMENT_PROMPT, meowEmbedUrl, MODEL_ID, QUERY_PROMPT } from "./config";

interface EmbedResponse {
  dense?: { model_id: string; dtype: string; shape: number[]; data: string };
}

export function decodeDense(response: EmbedResponse, count: number): Buffer[] {
  const dense = response.dense;
  if (!dense || dense.model_id !== MODEL_ID || dense.shape.length !== 2 || dense.shape[0] !== count || dense.shape[1] !== DIMENSIONS || dense.dtype !== "float32") {
    throw new Error("meow-embed вернул неверную модель, размерность или формат векторов");
  }
  const bytes = Buffer.from(dense.data, "base64");
  const vectorBytes = DIMENSIONS * 4;
  if (bytes.length !== count * vectorBytes) throw new Error("meow-embed вернул неполный набор векторов");
  for (let i = 0; i < bytes.length; i += 4) {
    if (!Number.isFinite(bytes.readFloatLE(i))) throw new Error("meow-embed вернул нечисловой вектор");
  }
  return Array.from({ length: count }, (_, index) => Buffer.from(bytes.subarray(index * vectorBytes, (index + 1) * vectorBytes)));
}

function embedEndpoint(): string {
  return `${meowEmbedUrl().replace(/\/+$/, "")}/embed`;
}

export async function embedTexts(texts: string[], task: "query" | "document"): Promise<Buffer[]> {
  if (!texts.length) return [];
  const dense_prompt = task === "query" ? QUERY_PROMPT : DOCUMENT_PROMPT;
  let response: Response;
  try {
    response = await fetch(embedEndpoint(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dense_model_id: MODEL_ID, dense_task: task, dense_prompt, texts }),
      signal: AbortSignal.timeout(task === "query" ? 60000 : 300000),
    });
  } catch (error) {
    throw new Error(`meow-embed недоступен (${meowEmbedUrl()}). Нужна модель ${MODEL_ID}.`, { cause: error });
  }
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 500);
    throw new Error(`Ошибка meow-embed HTTP ${response.status}: ${detail}`);
  }
  return decodeDense(await response.json() as EmbedResponse, texts.length);
}

const queryCache = new Map<string, Promise<Buffer>>();
export function embedQuery(query: string): Promise<Buffer> {
  const existing = queryCache.get(query);
  if (existing) return existing;
  const promise = embedTexts([query], "query").then(([vector]) => vector).catch((error) => {
    queryCache.delete(query);
    throw error;
  });
  queryCache.set(query, promise);
  if (queryCache.size > 128) queryCache.delete(queryCache.keys().next().value!);
  return promise;
}
