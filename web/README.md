# Kaizeval web console

The browser console for Kaizeval. It is a single-page application built with Vite, React 18 and
TypeScript. It talks only to the Kaizeval API on the same origin (`/api/v1`) and loads nothing
from the network: no CDN, no web fonts, no analytics.

## Requirements

- Node.js 18 or newer
- A running Kaizeval server for development (default `http://127.0.0.1:8000`)

## Development

```
cd web
npm install
npm run dev
```

The dev server runs on `http://localhost:5173` and proxies `/api` to `http://127.0.0.1:8000`.
Start the backend first, for example with `kaizeval demo` from the repository root.

## Build

```
npm run build
```

This type-checks the project (`tsc --noEmit`) and writes the production bundle to
`../src/kaizeval/webui/`, from where the Python server serves it. The output folder is emptied on
every build; `public/README.txt` is copied back into it so that the folder is never empty.

`npm run typecheck` runs the type check alone.

## Layout

```
src/api.ts          typed fetch client, one function per API route
src/auth.tsx        session context: token, user, workspaces, current role
src/hooks.ts        data loading and action helpers
src/format.ts       display formatting (percentages, percentage points, dates)
src/types.ts        shapes returned by the API
src/components/     shared UI: layout, job progress, comparison, usage, evidence
src/pages/          one file per page
src/styles.css      all styles; light and dark through CSS variables
```

## Conventions

- Every number on screen comes from the API. The console formats values; it does not compute
  statistics.
- Results produced with the fake model provider are always labelled SYNTHETIC.
- Actions the current role cannot perform are hidden or disabled; the API remains the authority
  and a 403 is shown next to the action that caused it.
