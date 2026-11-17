import { useEffect, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { useSession } from "../auth";
import { EvidenceRefs } from "../components/Evidence";
import { JobProgress } from "../components/JobProgress";
import {
  Badge, Card, ConfirmDialog, CountList, Empty, ErrorNote, Field, Loading, Notice, PageHeader, Pager, Stat, StatusBadge,
  SyntheticBanner, TableWrap, When,
} from "../components/ui";
import { formatPercent, humanize, parseOptionalNumber } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { Coverage, Dataset, Page, TestCase } from "../types";

const PAGE = 25;

function Question({ item }: { item: TestCase }) {
  return (
    <>
      {item.turns.map((turn, index) => (
        <p key={index} className={index === 0 ? undefined : "muted"}>
          {index > 0 && <span className="small">Follow-up: </span>}
          {turn.content}
        </p>
      ))}
    </>
  );
}

function ExpectedCell({ item }: { item: TestCase }) {
  const expected = item.expected ?? {};
  if (expected.behavior === "abstain") {
    return <p><Badge tone="info">Should decline</Badge> <span className="muted small">the knowledge base does not cover this</span></p>;
  }
  return (
    <>
      <p>{expected.answer || <span className="muted">no reference answer</span>}</p>
      {expected.key_facts && expected.key_facts.length > 0 && (
        <p className="muted small">Key facts: {expected.key_facts.join("; ")}</p>
      )}
      {expected.follow_up_answer && <p className="muted">Follow-up: {expected.follow_up_answer}</p>}
      {expected.follow_up_key_facts && expected.follow_up_key_facts.length > 0 && (
        <p className="muted small">Follow-up key facts: {expected.follow_up_key_facts.join("; ")}</p>
      )}
    </>
  );
}

function CasesTable({ cases, review }: { cases: TestCase[]; review?: (item: TestCase, status: string) => void }) {
  return (
    <TableWrap>
      <table className="data cases">
        <thead>
          <tr>
            <th scope="col">Question</th>
            <th scope="col">Category</th>
            <th scope="col">Severity</th>
            <th scope="col">Reference status</th>
            <th scope="col">Expected answer and key facts</th>
            <th scope="col">Evidence</th>
            {review && <th scope="col">Review</th>}
          </tr>
        </thead>
        <tbody>
          {cases.map((item) => (
            <tr key={item.id}>
              <td className="cell-wide">
                <Question item={item} />
                {item.language !== "en" && <p className="muted small">Language: {item.language}</p>}
                {item.stale && <Badge tone="warn" title="The evidence comes from a document version that has been replaced.">Stale evidence</Badge>}
              </td>
              <td>{humanize(item.category)}</td>
              <td><StatusBadge status={item.severity} /></td>
              <td><StatusBadge status={item.answer_status} /></td>
              <td className="cell-wide"><ExpectedCell item={item} /></td>
              <td className="cell-wide"><EvidenceRefs refs={item.evidence_refs} /></td>
              {review && (
                <td>
                  <div className="button-col">
                    <button type="button" className="btn btn-small" disabled={item.answer_status === "approved"}
                      onClick={() => review(item, "approved")}>
                      Approve reference
                    </button>
                    <button type="button" className="btn btn-small" disabled={item.answer_status === "needs_human_review"}
                      onClick={() => review(item, "needs_human_review")}>
                      Flag reference
                    </button>
                  </div>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}

function RegressionCases({ datasetId, version, onReviewed }: { datasetId: string; version: number; onReviewed: () => void }) {
  const { api, can } = useSession();
  const [offset, setOffset] = useState(0);
  const cases = useLoad(() => api.cases(datasetId, { limit: PAGE, offset }), [api, datasetId, offset]);
  const action = useAction();
  const data = cases.data;

  // The dataset version changes when cases are imported, retired or rotated in.
  useEffect(() => cases.reload(), [version]); // eslint-disable-line react-hooks/exhaustive-deps

  const review = (item: TestCase, status: string) =>
    void action.run(async () => {
      await api.reviewCase(datasetId, item.id, status);
      cases.reload();
      onReviewed();
    });

  return (
    <Card title={`Regression cases${data ? ` (${data.total})` : ""}`}>
      <p className="muted">
        Used for evaluation, diagnosis and optimization. A reviewer can approve a reference answer, which then outranks generated
        drafts, or flag it for correction.
      </p>
      {cases.loading && !data && <Loading />}
      <ErrorNote error={cases.error} />
      <ErrorNote error={action.error} />
      {data && data.items.length === 0 && <Empty title="This dataset has no regression cases." />}
      {data && data.items.length > 0 && <CasesTable cases={data.items} review={can("reviewer") ? review : undefined} />}
      {data && <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />}
    </Card>
  );
}

function HoldoutPanel({ dataset, coverage, onChanged }: { dataset: Dataset; coverage: Coverage; onChanged: () => void }) {
  const { api, can } = useSession();
  const holdout = coverage.holdout;
  const [confirming, setConfirming] = useState(false);
  const [opened, setOpened] = useState<Page<TestCase> | null>(null);
  const [rotateCount, setRotateCount] = useState("");
  const [rotateJob, setRotateJob] = useState<string | null>(null);
  const open = useAction();
  const rotate = useAction();

  const load = (offset: number) =>
    open.run(async () => {
      try {
        setOpened(await api.openHoldoutCases(dataset.id, { limit: PAGE, offset }));
      } finally {
        setConfirming(false);
      }
      onChanged();
    });

  const submitRotate = (event: FormEvent) => {
    event.preventDefault();
    void rotate.run(async () => {
      const job = await api.rotateHoldout(dataset.id, parseOptionalNumber(rotateCount));
      setRotateJob(job.id);
    });
  };

  return (
    <Card title="Sealed holdout">
      <p className="muted">
        The holdout is a set of cases kept away from diagnosis and optimization. It is used once, for an independent check of a
        finished candidate. Only its size is shown here.
      </p>
      <div className="stat-row">
        <Stat label="Holdout cases" value={holdout.count} />
        <Stat label="State" value={holdout.sealed ? "Sealed" : "Not sealed"} />
        <Stat label="Generation" value={holdout.generation} />
        <Stat label="Exposed" value={holdout.exposed ? "Yes" : "No"}
          note={holdout.exposed ? "must be rotated before it gates further tuning" : "has not informed any decision"} />
      </div>
      <h3>Holdout cases by category (counts only)</h3>
      <CountList counts={holdout.by_category} empty="no holdout cases" />
      {holdout.exposed && holdout.count > 0 && (
        <Notice tone="warn" title="This holdout has been exposed">
          <p>
            It already informed a decision or was opened by a person. Rotate it before running another optimization with holdout
            validation, otherwise the result would no longer be independent.
          </p>
        </Notice>
      )}

      {can("editor") && (
        <form onSubmit={submitRotate} className="form form-inline">
          <Field label="Rotate holdout: new holdout size (optional)" hint="Current holdout cases move to the regression set and a fresh sealed holdout is generated. 4 to 500; defaults to the current size.">
            <input type="number" value={rotateCount} min={4} max={500} step={1} onChange={(event) => setRotateCount(event.target.value)} />
          </Field>
          <button type="submit" className="btn" disabled={rotate.busy}>
            {rotate.busy ? "Starting…" : "Rotate holdout"}
          </button>
        </form>
      )}
      <ErrorNote error={rotate.error} />
      {rotateJob && (
        <JobProgress key={rotateJob} jobId={rotateJob} label="Rotating the holdout"
          onSettled={() => { setOpened(null); onChanged(); }} />
      )}

      {can("reviewer") && holdout.count > 0 && (
        <div className="danger-zone">
          <h3>Open holdout items</h3>
          <p className="muted">
            Reading the sealed cases is recorded in the audit log and marks the holdout as exposed. It then has to be rotated before
            it can validate another optimization.
          </p>
          <button type="button" className="btn btn-danger" onClick={() => setConfirming(true)} disabled={open.busy}>
            Open holdout items
          </button>
          <ErrorNote error={open.error} />
        </div>
      )}
      {opened && (
        <>
          <Notice tone="warn" title="Holdout items are now exposed">
            <p>This access was audited. Rotate the holdout before using it to validate further tuning.</p>
          </Notice>
          <CasesTable cases={opened.items} />
          <Pager total={opened.total} limit={opened.limit} offset={opened.offset} onChange={(offset) => void load(offset)} />
        </>
      )}

      <ConfirmDialog open={confirming} title="Open the sealed holdout?" confirmLabel="Open holdout items" danger busy={open.busy}
        onCancel={() => setConfirming(false)} onConfirm={() => void load(0)}>
        <p>
          You are about to read the {holdout.count} sealed holdout case(s) of <strong>{dataset.name}</strong>.
        </p>
        <ul>
          <li>This access is written to the audit log with your account.</li>
          <li>The holdout is marked as exposed and can no longer serve as an independent check.</li>
          <li>It must be rotated (replaced by a fresh sealed set) before the next optimization can use holdout validation.</li>
        </ul>
        <p>Open the holdout only if you really need to inspect these cases.</p>
      </ConfirmDialog>
    </Card>
  );
}

export function DatasetDetailPage() {
  const { id = "" } = useParams();
  const { api, can } = useSession();
  const [search, setSearch] = useSearchParams();
  const jobId = search.get("job");
  const names = useNames();
  const dataset = useLoad(() => api.dataset(id), [api, id]);
  const coverage = useLoad(() => api.coverage(id), [api, id]);
  const retire = useAction();
  const [retired, setRetired] = useState<number | null>(null);
  const data = dataset.data;
  const cover = coverage.data;

  const reload = () => {
    dataset.reload();
    coverage.reload();
  };

  // Without a job id to follow (for example after a page reload), keep checking while generation runs.
  const generating = data?.status === "generating" && !jobId;
  useEffect(() => {
    if (!generating) return undefined;
    const timer = window.setInterval(reload, 3000);
    return () => window.clearInterval(timer);
  }, [generating]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <PageHeader title={data ? data.name : "Dataset"} subtitle={<Link to="/datasets">All datasets</Link>}
        actions={<button type="button" className="btn btn-small" onClick={reload}>Refresh</button>} />
      {dataset.loading && !data && <Loading />}
      <ErrorNote error={dataset.error} />
      {jobId && (
        <Card>
          <JobProgress key={jobId} jobId={jobId} label="Generating the dataset"
            onSettled={() => { reload(); setSearch({}, { replace: true }); }} />
        </Card>
      )}
      {data && (
        <>
          <SyntheticBanner show={data.synthetic} detail="Questions and references are template-based and only exercise the pipeline." />
          <Card title="Dataset">
            <dl className="kv">
              <dt>Status</dt>
              <dd>
                <StatusBadge status={data.status} />
                {generating && <span className="muted"> generation is running; this page refreshes automatically.</span>}
              </dd>
              <dt>Version</dt>
              <dd>{data.version}</dd>
              <dt>Agent</dt>
              <dd>{data.agent_id ? <Link to={`/agents/${data.agent_id}`}>{names.agentName(data.agent_id)}</Link> : "—"}</dd>
              <dt>Specification</dt>
              <dd>{data.specification_id ? <Link to={`/specifications/${data.specification_id}`}>Open specification</Link> : "—"}</dd>
              <dt>Split</dt>
              <dd>
                {data.counts.regression ?? 0} regression, {data.counts.holdout ?? 0} holdout
                {data.counts.retired ? `, ${data.counts.retired} retired` : ""} (holdout target {formatPercent(data.holdout_fraction)}, seed {data.seed})
              </dd>
              <dt>Created</dt>
              <dd><When value={data.created_at} /></dd>
            </dl>
            {data.status === "failed" && (
              <div className="alert alert-error" role="alert">
                <p>Generation failed. {data.notes[data.notes.length - 1] ?? ""}</p>
              </div>
            )}
          </Card>
        </>
      )}

      {data && <ErrorNote error={coverage.error} />}
      {data && cover && data.status !== "generating" && (
        <>
          <Card title="Coverage report">
            <div className="stat-row">
              <Stat label="Cases in use" value={cover.total} note="regression and holdout" />
              <Stat label="Regression cases" value={cover.regression.count} />
              <Stat label="Multi-turn cases" value={cover.regression.multi_turn} />
              <Stat label="Stale cases" value={cover.regression.stale} note="evidence from a replaced document version" />
            </div>
            <div className="count-grid">
              <div><h3>By category</h3><CountList counts={cover.regression.by_category} /></div>
              <div><h3>By severity</h3><CountList counts={cover.regression.by_severity} /></div>
              <div><h3>By language</h3><CountList counts={cover.regression.by_language} raw /></div>
              <div><h3>By reference status</h3><CountList counts={cover.regression.by_answer_status} /></div>
              <div><h3>By document</h3><CountList counts={cover.regression.by_document} empty="no case cites a document" raw /></div>
              <div>
                <h3>Documents without a regression case</h3>
                {cover.uncovered_documents.length === 0 ? <span className="muted">none</span> : (
                  <ul className="plain-list">
                    {cover.uncovered_documents.map((name) => <li key={name}>{name}</li>)}
                  </ul>
                )}
              </div>
            </div>
            <h3>Limits</h3>
            <ul>
              {cover.limits.map((limit, index) => <li key={index}>{limit}</li>)}
            </ul>
            {cover.notes.length > 0 && (
              <>
                <h3>Notes</h3>
                <ul>
                  {cover.notes.map((note, index) => <li key={index}>{note}</li>)}
                </ul>
              </>
            )}
            {can("editor") && (
              <div className="button-row">
                <button type="button" className="btn" disabled={retire.busy || cover.regression.stale === 0}
                  onClick={() => void retire.run(async () => {
                    const result = await api.retireStale(id);
                    setRetired(result.retired);
                    reload();
                  })}>
                  {retire.busy ? "Retiring…" : `Retire stale cases (${cover.regression.stale})`}
                </button>
                {retired !== null && <span className="saved" role="status">{retired} case(s) retired.</span>}
              </div>
            )}
            <ErrorNote error={retire.error} />
          </Card>

          <HoldoutPanel dataset={data} coverage={cover} onChanged={reload} />
          <RegressionCases datasetId={id} version={data.version} onReviewed={coverage.reload} />
        </>
      )}
    </>
  );
}
