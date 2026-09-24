import type { Message, RunEvent } from "~/lib/api";

const strategies: Record<string, string> = {
  keyword: "关键词召回",
  bm25: "BM25 词面召回",
  dense: "BGE 向量召回",
  hybrid: "BM25 + BGE → RRF 融合",
  rerank: "BM25 + BGE → RRF 融合 → Cross-Encoder 重排",
};

export function PolicyRag({ events, messages, runId, loading, failed }: {
  events: RunEvent[];
  messages: Message[];
  runId?: string | null;
  loading: boolean;
  failed: boolean;
}) {
  const retrieval = [...events].reverse().find(event => event.run_id === runId && event.tool_name === "search_policy");
  const details = retrieval?.details;
  const citations = messages.filter(message => runId && message.run_id === runId && message.role === "AGENT").flatMap(message => message.citations);
  const unique = [...new Map(citations.map(citation => [citation.chunk_id ?? citation.source_id, citation])).values()];
  const strategy = String(details?.retrieval_strategy ?? "");
  const status = loading ? "正在读取本轮记录" : failed ? "本轮记录加载失败" : !retrieval ? "本轮暂无政策检索记录" : details?.error_code === "DEPENDENCY_TIMEOUT" ? "检索服务超时" : Number(details?.evidence_count) === 0 ? "未召回可用政策证据" : "已召回政策证据";
  return <section className="panel context-card" aria-label="售后政策 RAG">
    <span className="eyebrow">POLICY RAG</span><h2>售后政策检索增强回答</h2>
    <p>{status}</p>
    {details && <dl>
      <div><dt>本轮检索策略</dt><dd>{strategies[strategy] ?? (strategy || "未记录")}</dd></div>
      <div><dt>召回证据</dt><dd>{String(details.evidence_count ?? "未记录")} 条</dd></div>
      <div><dt>检索耗时（含缓存等待）</dt><dd>{String(details.duration_ms ?? "未记录")} ms</dd></div>
      <div><dt>Embedding</dt><dd>{String(details.embedding_model ?? "本轮未使用")}</dd></div>
      <div><dt>Reranker</dt><dd>{String(details.reranker_model ?? "本轮未使用")}</dd></div>
      <div><dt>语料版本</dt><dd>{String(details.corpus_id ?? "未记录")}</dd></div>
    </dl>}
    <p className="muted-copy">处理流程：按租户和生效版本筛选政策 → 检索条款 → 将证据交给回答器 → 校验引用 ID。演示模式使用固定回答器，LLM 模式由模型生成回答。</p>
    <details><summary>查看本轮回答附带的政策原文（{unique.length} 条）</summary>
      {unique.length ? unique.map(citation => <article key={citation.chunk_id ?? citation.source_id}>
        <h3>{citation.title}</h3><p>{citation.excerpt ?? "本条引用未提供原文"}</p>
        <small>{citation.chunk_id ?? citation.source_id} · {citation.policy_version ?? "版本未记录"}{citation.effective_at ? ` · 生效于 ${citation.effective_at}` : ""}</small>
      </article>) : <p>本轮暂无附带引用的回答；召回证据不代表回答已经生成。</p>}
    </details>
  </section>;
}
