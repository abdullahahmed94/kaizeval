import { formatInt, formatMs, formatPercent, formatPp, formatRelative, humanize } from "../format";
import type { CriticalFailures, Dict, GroupDelta, LatencyComparison, PairedDelta } from "../types";
import { ScoreBar, Stat, StatusBadge, TableWrap } from "./ui";

/** Headline of a paired A/B comparison. Every figure is taken from the API as is. */
export function PairedSummary({ overall }: { overall: PairedDelta }) {
  if (!overall.n || overall.verdict === "no_data") {
    return <p className="muted">No case was scored in both versions, so there is nothing to compare.</p>;
  }
  const ci = overall.ci95_percentage_points;
  return (
    <div>
      <div className="stat-row">
        <Stat label="Baseline A (mean)" value={formatPercent(overall.baseline_mean)} />
        <Stat label="Candidate B (mean)" value={formatPercent(overall.candidate_mean)} />
        <Stat label="Change" value={formatPp(overall.delta_percentage_points)}
          note={overall.relative_change_pct !== null && overall.relative_change_pct !== undefined
            ? `${formatRelative(overall.relative_change_pct)} relative to the baseline` : undefined} />
        <Stat label="95% confidence interval"
          value={ci ? `${formatPp(ci[0])} to ${formatPp(ci[1])}` : "n/a"}
          note={ci ? undefined : "needs at least two paired cases"} />
        <Stat label="Paired cases (n)" value={formatInt(overall.n)}
          note={`${overall.wins ?? 0} wins, ${overall.losses ?? 0} losses, ${overall.ties ?? 0} ties`} />
      </div>
      <p className="verdict">
        Verdict: <StatusBadge status={overall.verdict} /> {overall.interpretation}
        {overall.sign_test_p !== null && overall.sign_test_p !== undefined && (
          <span className="muted"> Sign test p = {overall.sign_test_p.toFixed(4)}.</span>
        )}
      </p>
      <p className="muted small">
        These means are plain averages over the paired cases. A run's overall score gives critical cases a higher weight, so it
        can differ slightly from the mean shown here.
      </p>
    </div>
  );
}

export function GroupDeltaTable({ caption, groups }: { caption: string; groups: Dict<GroupDelta> | undefined }) {
  const entries = Object.entries(groups ?? {});
  if (entries.length === 0) return null;
  return (
    <TableWrap>
      <table className="data">
        <caption>{caption}</caption>
        <thead>
          <tr>
            <th scope="col">{caption.startsWith("By dimension") ? "Dimension" : "Category"}</th>
            <th scope="col" className="num">n</th>
            <th scope="col">Baseline A</th>
            <th scope="col">Candidate B</th>
            <th scope="col" className="num">Change</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([name, group]) => (
            <tr key={name}>
              <th scope="row">{humanize(name)}</th>
              <td className="num">{group.n}</td>
              <td>
                <span className="score-cell">
                  <ScoreBar value={group.baseline_mean} tone="muted" />
                  <span className="score-number">{formatPercent(group.baseline_mean)}</span>
                </span>
              </td>
              <td>
                <span className="score-cell">
                  <ScoreBar value={group.candidate_mean} />
                  <span className="score-number">{formatPercent(group.candidate_mean)}</span>
                </span>
              </td>
              <td className={group.delta_percentage_points < 0 ? "num delta-down" : "num delta-up"}>
                {formatPp(group.delta_percentage_points)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </TableWrap>
  );
}

export function SafetyStats({ critical, latency }: { critical?: CriticalFailures | null; latency?: LatencyComparison | null }) {
  return (
    <div className="stat-row">
      {critical && (
        <>
          <Stat label="Critical failures" value={`${critical.baseline} → ${critical.candidate}`} note="baseline → candidate" />
          <Stat label="New critical failures" value={formatInt(critical.new)} />
          <Stat label="Fixed critical failures" value={formatInt(critical.fixed)} />
        </>
      )}
      {latency && (
        <>
          <Stat label="Latency p50" value={`${formatMs(latency.baseline_p50)} → ${formatMs(latency.candidate_p50)}`}
            note="baseline → candidate" />
          <Stat label="Latency p95" value={`${formatMs(latency.baseline_p95)} → ${formatMs(latency.candidate_p95)}`}
            note="baseline → candidate" />
        </>
      )}
    </div>
  );
}
