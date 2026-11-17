import { useSession } from "../auth";
import { useAction } from "../hooks";
import { ErrorNote, saveTextFile } from "./ui";

/** Downloads the report of an evaluation or optimization run as JSON or Markdown. */
export function ReportButtons({ objectId, kind }: { objectId: string; kind: "evaluation" | "optimization" }) {
  const { api } = useSession();
  const action = useAction();

  const download = (format: "json" | "md") =>
    action.run(async () => {
      const text = await api.reportText(objectId, format);
      const name = `kaizeval-${kind}-report-${objectId.slice(0, 8)}.${format}`;
      if (format === "json") {
        saveTextFile(name, JSON.stringify(JSON.parse(text), null, 2), "application/json");
      } else {
        saveTextFile(name, text, "text/markdown");
      }
    });

  return (
    <span className="button-row">
      <button type="button" className="btn btn-small" disabled={action.busy} onClick={() => void download("json")}>
        Report (JSON)
      </button>
      <button type="button" className="btn btn-small" disabled={action.busy} onClick={() => void download("md")}>
        Report (Markdown)
      </button>
      <ErrorNote error={action.error} />
    </span>
  );
}
