// Seed / refresh the shared MAIN code-search collection that the claude-context
// MCP reads. Config MUST mirror .mcp.json exactly (OpenAI-compatible bge-m3 via
// the embed shim on 127.0.0.1:8182, Milvus 127.0.0.1:19530 root:Milvus, hybrid
// default) or it writes a different collection than the MCP queries. Invoked by
// scripts/code-index — see there.
//
// Embeddings note: ollama (the old nomic-embed-text@768 backend) is retired.
// Both this seeder and the MCP now use the OpenAI provider pointed at the shim,
// which forwards to precis' bge-m3 embedder (dim auto-detected = 1024). The
// provider must match the MCP or the collection dim diverges.
//
// The collection name is hybrid_code_chunks_<md5(resolve(mainPath))[:8]>, keyed
// to the ABSOLUTE main-checkout path and storing repo-RELATIVE paths, so one
// index serves every worktree (search with the main path; hits map onto yours).
import { Context, OpenAIEmbedding, MilvusVectorDatabase } from '@zilliz/claude-context-core';

const MAIN = process.env.CODE_INDEX_MAIN_ROOT;
if (!MAIN) { console.error('code-index: CODE_INDEX_MAIN_ROOT unset'); process.exit(2); }

// OpenAIEmbedding auto-detects the dimension from a test embed (no dimension
// arg); apiKey is unused (the shim/bge-m3 is authless) but the client requires
// a non-empty string.
const embedding = new OpenAIEmbedding({
  model: process.env.EMBEDDING_MODEL || 'bge-m3',
  apiKey: process.env.OPENAI_API_KEY || 'unused-bge-m3-is-authless',
  baseURL: process.env.OPENAI_BASE_URL || 'http://127.0.0.1:8182/v1',
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
