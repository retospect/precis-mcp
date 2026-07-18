// Seed / refresh the shared MAIN code-search collection that the claude-context
// MCP reads. Config MUST mirror .mcp.json exactly (Ollama nomic-embed-text@768,
// Milvus 127.0.0.1:19530 root:Milvus, hybrid default) or it writes a different
// collection than the MCP queries. Invoked by scripts/code-index — see there.
//
// The collection name is hybrid_code_chunks_<md5(resolve(mainPath))[:8]>, keyed
// to the ABSOLUTE main-checkout path and storing repo-RELATIVE paths, so one
// index serves every worktree (search with the main path; hits map onto yours).
import { Context, OllamaEmbedding, MilvusVectorDatabase } from '@zilliz/claude-context-core';

const MAIN = process.env.CODE_INDEX_MAIN_ROOT;
if (!MAIN) { console.error('code-index: CODE_INDEX_MAIN_ROOT unset'); process.exit(2); }

const embedding = new OllamaEmbedding({
  model: process.env.EMBEDDING_MODEL || 'nomic-embed-text',
  host: process.env.OLLAMA_HOST || 'http://127.0.0.1:11434',
  dimension: Number(process.env.EMBEDDING_DIMENSION || 768),
});
const vectorDatabase = new MilvusVectorDatabase({
  address: process.env.MILVUS_ADDRESS || '127.0.0.1:19530',
  token: process.env.MILVUS_TOKEN || 'root:Milvus',
});
const context = new Context({
  embedding,
  vectorDatabase,
  // Exclude .claude — the main checkout holds .claude/worktrees/* full repo
  // copies that would otherwise be indexed N times. (.gitignore also excludes
  // it; belt and suspenders.)
  customIgnorePatterns: ['.claude/**', '.claude', '*.mp3', 'feed.xml'],
});

const collection = context.getCollectionName(MAIN);
const already = await context.hasIndex(MAIN);
console.log(`[code-index] collection=${collection}  mode=${already ? 'refresh (Merkle diff)' : 'full seed'}`);

let last = -5;
const onProgress = (p) => {
  const pct = typeof p.percentage === 'number' ? Math.floor(p.percentage) : null;
  if (pct !== null && pct >= last + 10) { last = pct; console.log(`[code-index] ${pct}% ${p.phase ?? ''}`); }
};

// Incremental when the collection exists (only changed files re-embed); full on
// first run. Both are idempotent.
const stats = already
  ? await context.reindexByChange(MAIN, onProgress)
  : await context.indexCodebase(MAIN, onProgress);

console.log('[code-index] DONE', JSON.stringify(stats));
