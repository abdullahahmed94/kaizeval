import { useEffect, useRef, useState, type ReactNode } from "react";

import { useSession } from "../auth";
import type { Job } from "../types";
import { ErrorNote, StatusBadge } from "./ui";

const POLL_MS = 1500;

export function isActive(status: string | null | undefined): boolean {
  return status === "queued" || status === "running";
}

/**
 * Follows a background job: polls it while it is queued or running, shows its progress and tells the
 * page when it settles so the page can reload its own data.
 */
export function JobProgress({ jobId, label, onSettled, onTick, children }: {
  jobId: string;
  label: string;
  /** Called once when a job that was seen queued or running reaches any other status. */
  onSettled?: (job: Job) => void;
  /** Called after every poll while the job is active. */
  onTick?: (job: Job) => void;
  /** Extra controls shown while the job is active, for example a Cancel button. */
  children?: ReactNode;
}) {
  const { api } = useSession();
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<unknown>(null);
  const callbacks = useRef({ onSettled, onTick });
  callbacks.current = { onSettled, onTick };

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    let wasActive = false;

    const poll = async () => {
      try {
        const current = await api.job(jobId);
        if (stopped) return;
        setJob(current);
        setError(null);
        if (isActive(current.status)) {
          wasActive = true;
          callbacks.current.onTick?.(current);
          timer = window.setTimeout(poll, POLL_MS);
        } else if (wasActive) {
          callbacks.current.onSettled?.(current);
        }
      } catch (reason) {
        if (stopped) return;
        setError(reason);
        timer = window.setTimeout(poll, POLL_MS * 2);
      }
    };
    void poll();
    return () => {
      stopped = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [api, jobId]);

  if (!job) return error ? <ErrorNote error={error} /> : null;
  const total = job.progress?.total;
  const done = job.progress?.done ?? 0;
  const active = isActive(job.status);
  return (
    <div className="job" role="status" aria-live="polite">
      <div className="job-line">
        <span>{label}</span>
        <StatusBadge status={job.status} />
        {total ? (
          <span className="muted">
            {done} / {total}
          </span>
        ) : (
          active && <span className="muted">working…</span>
        )}
        {job.cancel_requested && active && <span className="muted">cancelling…</span>}
        {active && children}
      </div>
      {active &&
        (total ? (
          <progress className="job-progress" max={total} value={done} aria-label={`${label} progress`} />
        ) : (
          <progress className="job-progress" aria-label={`${label} progress`} />
        ))}
      {job.status === "failed" && job.error && (
        <div className="alert alert-error" role="alert">
          <p>{job.error}</p>
        </div>
      )}
      <ErrorNote error={error} />
    </div>
  );
}
