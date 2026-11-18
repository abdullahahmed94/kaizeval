# Validation against a real model

The automated test-suite uses a fake provider and a mock server, which prove the plumbing but say
nothing about how a real language model behaves. This page records what happened when every
model-assisted step was run against a real local model, what went wrong, and what was changed as
a result. The numbers are measurements from one machine and one model; they are not benchmarks
and they do not predict results on your agent.

## Setup

| | |
|---|---|
| Date | 2026-11-18 |
| Model server | Ollama 0.40.16, OpenAI-compatible endpoint `http://localhost:11434/v1` |
| Model | `qwen2.5:7b` (4.7 GB), used for every role: tester, reference drafter, reviewers, arbiter, root cause, optimizer |
| Hardware | Laptop, NVIDIA RTX 3050 6 GB (model ran 82% on GPU, 18% on CPU), Windows 11 |
| Kaizeval | 0.2.0, offline workspace, default prompts |
| Knowledge | The three bundled sample documents (15 passages) |

Reproduce it with `python scripts/real_model_check.py --model <your model>`; it writes a JSON
file with everything reported below. The third run used `examples/llm_agent.py` and
`examples/llm_agent.kaizeval.yaml`. The fourth run continued the third.

Across runs 1 to 3 the model server answered **589 requests, all successfully** (this includes
the example agent's own calls in run 3). Of the 492 calls Kaizeval's gateway recorded, **one**
returned output that failed schema validation; the automatic retry recovered it. Run 4 added 75
gateway calls, all valid.

## Run 1: every role once, then the pipeline (before fixes)

All ten role calls returned valid structured output at the first attempt. A reviewer call took
7 to 11 seconds. The sanity checks passed: a correct answer scored 1.0 for correctness and a
deliberately wrong one scored 0.0; the drafted reference quoted its passage verbatim; and the
drafter reported "insufficient evidence" for a question the passage could not answer.

The run also exposed real defects that the fake provider could never show:

| # | What the real model did | What was changed |
|---|---|---|
| 1 | **A reviewer scored an unrelated answer as a correct refusal.** For "What are the terms of the mobile app?" the agent quoted a sentence about return fees, and the reviewer wrote "the assistant abstained" (abstention 1.0) | A reviewer's claim of a refusal now counts only if it quotes a sentence that is really in the answer and that reads like a refusal. Otherwise the claim is overruled and recorded as such |
| 2 | Asked for 2 questions per passage, the tester returned 20 for two passages, mostly the same question reworded | At most the requested number per passage is kept, near-duplicates are dropped, and the prompt was tightened. The same call then returned 6 distinct questions in 23 s instead of 62 s |
| 3 | Vague or meta questions ("whats in this doc?") entered the test set with no verifiable reference | A generated question whose reference cannot be tied to verbatim evidence is left out (and counted in the dataset notes) |
| 4 | Two of three "unanswerable" questions were in fact answerable from the documents | Each proposed out-of-scope question is checked against the closest passage; if a reference can be drafted with verbatim evidence, the question is discarded |
| 5 | "Egyptian Arabic" variants came back as broken text mixing Latin letters, Arabic letters and even a Korean word | A variant is rejected when a word mixes scripts or uses a script other than the requested language's. The note in the dataset says how many were rejected |
| 6 | The optimizer proposed an irrelevant prompt edit that ignored the failure summary | The optimizer prompt now walks through: most frequent failure, matching setting, concrete new value. In the later runs its proposals addressed the failures |
| 7 | Two and three reviewers on the same model always agreed (100%), so a panel added cost and no information | The second reviewer role now has a deliberately different, sceptical brief, and the run summary warns when all reviewers run on one model |
| 8 | The first call after starting the model server took over three minutes (loading the model), far beyond the 30-second preflight timeout | Preflight now waits up to 5 minutes (`params.preflight_timeout_s`) and the default call timeout is 5 minutes |

## Run 2: the pipeline after the fixes (deterministic sample agent)

| Step | Result |
|---|---|
| Test generation, 2 testers | 16 regression + 3 holdout cases in 339 s. All 16 references linked to verbatim evidence. 5 surplus or duplicate questions and 1 unverifiable question were discarded automatically. Arabic variants were in Arabic script |
| Review, 1 reviewer | 41.7% overall, 16 model calls, 225 s |
| Review, 2 reviewers | 41.0% overall, 34 calls, 405 s. Reviewers agreed on 14 of 16 cases; the arbiter decided 2 |
| Review, panel of 3 | 41.6% overall, 50 calls, 582 s. Same agreement: 14 of 16, 2 arbitrated |
| Invalid model outputs / errors | 0 / 0 in all three modes |
| Abstention on the baseline | 0% (correct: the baseline agent never declines). In run 1 the same situation was scored 100% |

Optimization with the real optimizer (single reviewer, target 100%):

| Cycle | Change proposed by the model | Outcome |
|---|---|---|
| 1 | `top_k: 3` | rolled back: no improvement |
| 2 | `abstain_threshold: 0.25` | kept |
| 3 | `max_sentences: 3` | kept |
| 4 | `cite_sources: true` | kept |
| 5 | `query_expansion: true` | kept |
| 6 | (no further change proposed) | loop ended |

Regression score **41.0% → 82.5%**: paired difference +41.5 percentage points over 16 cases, 95%
interval +25.1 to +59.5, verdict *improved*. On the sealed holdout (3 cases) the difference was
+11.8 points with an interval of 0 to +17.7, verdict *inconclusive*: three cases are too few to
confirm anything, and the report says so. 105 model calls, 17 minutes.

## Run 3: a real model-backed agent, improved through its system prompt

Here the agent under test is itself a language-model application (`examples/llm_agent.py`,
same model): it retrieves passages and asks the model to answer. Its settings (system prompt,
number of passages, temperature, whether sources are returned) are exposed with
`kaizeval.agentkit`. One command, `kaizeval run`, did everything in 26 minutes and 107 model calls.

| Cycle | Change proposed by the model | Outcome |
|---|---|---|
| 1 | Rewrote the system prompt: answer only from the documents, name the source, say you do not know when the documents are silent, include every number and condition | kept |
| 2 | `top_k: 3` | kept |
| 3 | `temperature: 0.5` | rolled back: the edge-case category dropped by 11.8 points (tolerance 5) |
| 4 | Added "do not make assumptions or provide information not present in the documents" to the prompt | kept |
| 5 | A further prompt edit | rolled back: 4.8 points below the best version |
| 6 | A further prompt edit | rolled back: 0.35 points below the best version |

The loop stopped by its own rule, "the score did not change in 2 consecutive cycles" (the limit
was 2 when this run was made; the default is now 3), and left
the candidate waiting for a human decision with the remaining recommendations. Regression score
of the best version: 74.0%. Correct refusals of out-of-scope questions went from 0% to 100%.

What this run taught, and what was changed:

- **The same baseline scored 53.1% and then 60.8%.** A model-backed agent with a non-zero
  temperature answers differently each time, so a difference of a few points is noise. The paired
  comparison reported *inconclusive* for both the regression set (+13.2 points over 11 cases) and
  the holdout (+28.1 points over 3 cases), which is the honest reading. `kaizeval run` now prints
  the measured variation, and `evaluation.repeats` asks every question several times and averages.
- **The refusal check worked on real answers in both directions.** A reviewer claimed a refusal
  for an answer that told the user to "contact our customer service team"; the claim was
  overruled because the quoted sentence was not a refusal. Polite refusals that used no stock
  phrase ("the document ... doesn't include information about ...") were accepted because the
  reviewer quoted them.
- **Citations written inside the answer were not counted**, because only a separate citation list
  was read. The check now also accepts a knowledge document named in the answer text.
- A question about the document itself ("What does this document describe?") still slipped
  through; the tester prompt now forbids questions about the document rather than its facts.

## Run 4: continuing the stopped loop of run 3

Run 3 ended with a candidate waiting for a decision. This run takes the other branch of that
decision, `kaizeval run --continue`, on the same project some hours later. In the meantime the
example agent had been restarted without a state file, so it no longer had the candidate copy.

The first attempt failed, and that was the finding of this run:

| What the real model did | What was changed |
|---|---|
| **Rotating the holdout failed**: "the generator produced no new questions for a fresh holdout". A continuation needs a fresh sealed set, and on this small knowledge base the tester asked again what the dataset already contained, so nothing new and verifiable was left | The tester is now told what each passage was already asked and must ask about another fact; passages with the fewest questions are chosen first; a near-repeat of an existing question is dropped; and generation is retried on other passages. If it still fails, `kaizeval run` goes on without the final holdout check and says so |

The second attempt, after that change:

| Step | Result |
|---|---|
| Fresh holdout | Generated at the second internal attempt: 3 new sealed cases. The 3 retired holdout cases joined the regression set (11 → 14 cases) |
| Candidate | The agent no longer knew it, so it was created again from the two changes run 3 had kept (the rewritten system prompt and `top_k: 3`). The baseline was untouched |
| Measured again | Baseline 55.7%, candidate 74.7% on the 14 cases |
| Cycle 7 | A further prompt edit, rolled back: 0.28 points below the best version |
| Stop | The optimizer returned no further change, so the loop ended after one new cycle; the three-cycle rule was not what stopped it |
| Paired A/B, regression | +18.9 points over 14 cases, 95% interval +3.9 to +38.8, verdict *improved* |
| Sealed holdout (new) | +5.9 points over 3 cases, interval 0 to +17.7, verdict *inconclusive* |

13 minutes, 75 model calls, no invalid output. The run ended, as it should, by offering the same
three choices again: continue, release or reject.

Two more things this run showed:

- The same untouched baseline has now scored 53.1%, 60.8%, 62.4% and 64.9% on the same 11 cases,
  and 55.7% on the larger set. The first four are the agent's own variation. The last one is a different test set, and the
  run summary wrongly described that difference as variation too; the variation note now
  compares only measurements over the same cases.
- A continuation makes earlier scores incomparable when the holdout is rotated, because the
  regression set grows. The report and the result now say so explicitly.

## What this does and does not establish

Established, for this model and machine:

- every model-assisted step works end to end through the gateway with a real local model, in
  strict offline mode, without invalid structured output;
- the improvement loop proposes sensible changes, keeps the ones that pass the gates, rolls back
  the ones that do not, and stops by the rule described in [PROJECT_MODE.md](PROJECT_MODE.md);
- a model-backed agent can be improved by rewriting its system prompt on an isolated copy;
- a stopped loop can be continued from its candidate after the agent was restarted, with a fresh
  holdout generated by the real model.

Not established:

- that any other model behaves this well. Smaller models may fail the structured-output
  preflight or review poorly. Run `scripts/real_model_check.py` on yours;
- the quality of translations into dialects. With this model, Egyptian Arabic output was poor
  even when it passed the script check; have a speaker review language variants;
- independence of reviewers when they all run on one model. Route `evaluator_secondary` to a
  different model for a real second opinion;
- anything about a hosted provider with an API key. The code path is the same OpenAI-compatible
  one, with connected mode and `allow_public`, and it was not exercised against a paid service;
- statistical confirmation of an improvement on a holdout of three cases. Use more cases;
- the three-unchanged-cycles stop with a real model. That rule is covered by the automated tests;
  in run 3 the loop stopped at the earlier limit of two, and in run 4 the optimizer ran out of
  proposals first;
- a continuation after the owner changed the agent's own defaults. That path is covered by an
  automated test with the sample agent only.

## Practical notes for local models

- The first request after the model server starts, or after it unloads an idle model, can take
  minutes. Keep the model loaded (for Ollama, `OLLAMA_KEEP_ALIVE`) during long runs.
- Budget roughly 10 seconds per reviewer call on a 6 GB laptop GPU with a 7B model. A run of
  16 cases with 3 reviewers took about 10 minutes.
- Use `evaluation.repeats: 2` or more when the agent under test is itself a model with a
  non-zero temperature.
