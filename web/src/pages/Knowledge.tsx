import { useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";

import { useSession } from "../auth";
import { FlagBadges, INJECTION_NOTE } from "../components/Evidence";
import {
  Badge, Card, Check, Empty, ErrorNote, Field, Loading, Notice, PageHeader, Pager, StatusBadge, TableWrap, When,
} from "../components/ui";
import { formatBytes } from "../format";
import { useAction, useLoad } from "../hooks";
import type { KnowledgeDocument } from "../types";

const PAGE = 50;

function Upload({ onUploaded }: { onUploaded: () => void }) {
  const { api } = useSession();
  const [kind, setKind] = useState("kb");
  const [files, setFiles] = useState<File[]>([]);
  const [results, setResults] = useState<KnowledgeDocument[]>([]);
  const input = useRef<HTMLInputElement>(null);
  const action = useAction();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setResults([]);
    void action.run(async () => {
      const uploaded: KnowledgeDocument[] = [];
      try {
        for (const file of files) {
          uploaded.push(await api.uploadDocument(file, kind));
        }
      } finally {
        setResults(uploaded);
        if (uploaded.length > 0) onUploaded();
      }
      setFiles([]);
      if (input.current) input.current.value = "";
    });
  };

  return (
    <Card title="Upload documents">
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="Files" hint="PDF, DOCX, Markdown or plain text. Uploading a file with an existing name creates a new version.">
            <input ref={input} type="file" multiple accept=".pdf,.docx,.md,.markdown,.txt"
              onChange={(event) => setFiles(Array.from(event.target.files ?? []))} required />
          </Field>
          <Field label="Kind" hint="Knowledge base documents are the source of test evidence. Requirements feed the specification.">
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              <option value="kb">Knowledge base</option>
              <option value="requirements">Business requirements</option>
            </select>
          </Field>
        </div>
        <ErrorNote error={action.error} />
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={action.busy || files.length === 0}>
            {action.busy ? "Uploading…" : files.length > 1 ? `Upload ${files.length} files` : "Upload"}
          </button>
        </div>
      </form>
      {results.length > 0 && (
        <ul className="plain-list" role="status">
          {results.map((document) => (
            <li key={document.id}>
              <strong>{document.filename}</strong> version {document.version}: <StatusBadge status={document.status} />{" "}
              {document.status === "failed" ? <span className="error-text">{document.error}</span>
                : `${document.chunk_count} passage(s)`}
              {document.flagged_chunks ? (
                <>
                  {" "}<Badge tone="warn">{document.flagged_chunks} flagged passage(s)</Badge>{" "}
                  <span className="muted small">{INJECTION_NOTE}</span>
                </>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function KnowledgePage() {
  const { api, can } = useSession();
  const [kind, setKind] = useState("");
  const [superseded, setSuperseded] = useState(false);
  const [offset, setOffset] = useState(0);
  const documents = useLoad(
    () => api.documents({ kind: kind || undefined, include_superseded: superseded, limit: PAGE, offset }),
    [api, kind, superseded, offset],
  );
  const data = documents.data;

  return (
    <>
      <PageHeader title="Knowledge" subtitle="Documents the agent is expected to answer from. They are split into evidence passages that tests cite." />
      {can("editor") ? <Upload onUploaded={documents.reload} /> : (
        <p className="muted small">Only editors and owners can upload documents.</p>
      )}
      <Card title="Documents">
        <div className="filters">
          <Field label="Kind">
            <select value={kind} onChange={(event) => { setKind(event.target.value); setOffset(0); }}>
              <option value="">All kinds</option>
              <option value="kb">Knowledge base</option>
              <option value="requirements">Business requirements</option>
            </select>
          </Field>
          <Check label="Include superseded versions" checked={superseded} onChange={(value) => { setSuperseded(value); setOffset(0); }} />
        </div>
        {documents.loading && !data && <Loading />}
        <ErrorNote error={documents.error} />
        {data && data.items.length === 0 && (
          <Empty title="No documents found.">
            {can("editor") ? "Upload the documents your agent answers from. They are needed to generate evidence-linked tests."
              : "An editor needs to upload knowledge documents."}
          </Empty>
        )}
        {data && data.items.length > 0 && (
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">File</th>
                  <th scope="col">Kind</th>
                  <th scope="col" className="num">Version</th>
                  <th scope="col">SHA-256</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">Passages</th>
                  <th scope="col" className="num">Size</th>
                  <th scope="col">Uploaded</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((document) => (
                  <tr key={document.id}>
                    <th scope="row">
                      {document.status === "failed" ? document.filename : <Link to={`/knowledge/${document.id}`}>{document.filename}</Link>}
                      {document.status === "failed" && document.error && <p className="error-text small">{document.error}</p>}
                    </th>
                    <td>{document.kind === "kb" ? "Knowledge base" : "Requirements"}</td>
                    <td className="num">{document.version}</td>
                    <td><code title={document.sha256}>{document.sha256.slice(0, 12)}</code></td>
                    <td><StatusBadge status={document.status} /></td>
                    <td className="num">{document.chunk_count}</td>
                    <td className="num">{formatBytes(document.size_bytes)}</td>
                    <td><When value={document.created_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
        {data && <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />}
      </Card>
    </>
  );
}

export function DocumentPage() {
  const { id = "" } = useParams();
  const { api } = useSession();
  const [offset, setOffset] = useState(0);
  const chunks = useLoad(() => api.chunks(id, { limit: PAGE, offset }), [api, id, offset]);
  const data = chunks.data;
  const flagged = data?.items.some((chunk) => chunk.flags.includes("possible_injection"));

  return (
    <>
      <PageHeader title={data ? data.document.filename : "Document"} subtitle={<Link to="/knowledge">All documents</Link>} />
      {chunks.loading && !data && <Loading />}
      <ErrorNote error={chunks.error} />
      {data && (
        <>
          <Card title="Document">
            <dl className="kv">
              <dt>Kind</dt>
              <dd>{data.document.kind === "kb" ? "Knowledge base" : "Business requirements"}</dd>
              <dt>Version</dt>
              <dd>{data.document.version}</dd>
              <dt>Status</dt>
              <dd><StatusBadge status={data.document.status} /></dd>
              <dt>SHA-256</dt>
              <dd><code className="wrap">{data.document.sha256}</code></dd>
              <dt>Size</dt>
              <dd>{formatBytes(data.document.size_bytes)}</dd>
              <dt>Uploaded</dt>
              <dd><When value={data.document.created_at} /></dd>
            </dl>
            {data.document.error && <p className="error-text">{data.document.error}</p>}
          </Card>
          <Card title={`Evidence passages (${data.total})`}>
            {flagged && <Notice tone="warn" title="Possible instructions inside a document">{INJECTION_NOTE}</Notice>}
            {data.items.length === 0 && <Empty title="This document has no passages." />}
            <ol className="chunks" start={data.offset + 1}>
              {data.items.map((chunk) => {
                const injection = chunk.flags.includes("possible_injection");
                return (
                  <li key={chunk.id} className={injection ? "chunk chunk-flagged" : "chunk"}>
                    <p className="muted small">
                      {chunk.section ?? "No section"}
                      {chunk.page ? ` · page ${chunk.page}` : ""} · characters {chunk.char_start}–{chunk.char_end}{" "}
                      <FlagBadges flags={chunk.flags} />
                    </p>
                    <p className="chunk-text">{chunk.text}</p>
                    {injection && <p className="small warn-text">{INJECTION_NOTE}</p>}
                  </li>
                );
              })}
            </ol>
            <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
          </Card>
        </>
      )}
    </>
  );
}
