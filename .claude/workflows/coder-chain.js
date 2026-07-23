export const meta = {
  name: 'coder-chain',
  description: 'Chain small stateless coder rounds via compact handoffs, so a long build never grows one huge coder transcript',
  whenToUse: 'A build too large for a single coder call to finish cleanly (many files, many test-fix cycles) — each round gets a fresh coder seeded only by the prior round\'s handoff, not its full history; the caller only sees the final result.',
  phases: [
    { title: 'Implement', detail: 'loop of fresh coder rounds, each continuing from the last round\'s handoff, until done/blocked/round cap' },
  ],
}

const HANDOFF_SCHEMA = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['continue', 'done', 'blocked'] },
    summary: { type: 'string', description: 'What this round did' },
    filesChanged: { type: 'array', items: { type: 'string' } },
    testStatus: { type: 'string', description: 'e.g. "scripts/test --impacted: pass" or failing test ids' },
    nextStep: { type: 'string', description: 'Concrete instruction for the next round to pick up — empty if done/blocked' },
    question: { type: 'string', description: 'Set when status=blocked: the specific decision needed from the caller' },
  },
  required: ['status', 'summary', 'filesChanged', 'testStatus', 'nextStep'],
}

function buildPrompt(task, handoff, round) {
  if (round === 1) {
    return [
      `You are round 1 of a chained implementation. Overall task:`,
      task,
      ``,
      `This may take more than one round — if you reach a natural stopping point with a coherent chunk done and tests green, but the overall task isn't finished, return status='continue' with a nextStep another coder round can pick up cold (they will NOT see your reasoning, only your handoff and the current repo state). Return status='done' only once the whole task is complete and verified. Return status='blocked' with a specific question if you hit an architecture/API/domain decision outside your remit.`,
    ].join('\n')
  }
  return [
    `You are round ${round} of a chained implementation. Overall task:`,
    task,
    ``,
    `Prior round's handoff (you have no memory of its reasoning — only this and the current repo state):`,
    `- summary: ${handoff.summary}`,
    `- filesChanged: ${handoff.filesChanged.join(', ') || '(none)'}`,
    `- testStatus: ${handoff.testStatus}`,
    `- nextStep: ${handoff.nextStep}`,
    ``,
    `Continue from nextStep. Same rules: status='continue' + a fresh nextStep if more remains, status='done' once the whole task is complete and verified, status='blocked' + a specific question if you hit a decision outside your remit.`,
  ].join('\n')
}

phase('Implement')

const task = args.task
const maxRounds = args.maxRounds || 8

let handoff = null
let round = 0
const history = []

while (round < maxRounds) {
  round++
  const prompt = buildPrompt(task, handoff, round)
  handoff = await agent(prompt, { agentType: 'coder', schema: HANDOFF_SCHEMA, label: `coder round ${round}` })
  if (!handoff) {
    log(`round ${round}: coder call failed or was skipped — stopping chain`)
    break
  }
  history.push({ round, status: handoff.status, summary: handoff.summary, testStatus: handoff.testStatus })
  log(`round ${round}: ${handoff.status} — ${handoff.summary}`)
  if (handoff.status !== 'continue') break
}

return { rounds: round, final: handoff, history }
