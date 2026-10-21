# Frontend Architecture Deep Dive: Vite + React + TypeScript + Tailwind CSS

This guide explains the concepts, terminology, project structure, build pipeline, and end-to-end network traffic flows for the FDE Support UI (`frontend/`), built with **Vite + React 19 + TypeScript + Tailwind CSS v4** and served by [`frontend/server.py`](../frontend/server.py).

---

## 1. Core Terminology Glossary

If you come from a backend, infrastructure, or ML engineering background, modern frontend tooling can feel like an alphabet soup. Here is what each piece of the stack actually does and why it exists.

### Browser & UI Fundamentals

| Term | What It Means | How It Applies to `fde_support` |
| :--- | :--- | :--- |
| **DOM (Document Object Model)** | The browser's in-memory tree representation of an HTML page. When JavaScript changes the screen, it mutates nodes in the DOM tree. | Instead of manually mutating the DOM using `document.getElementById(...)` and `innerHTML`, React manages DOM updates declaratively. |
| **SPA (Single-Page Application)** | A web app where the browser loads **one** HTML shell (`index.html`) once, and JavaScript dynamically updates the screen as data changes—without full-page browser reloads. | Our UI is an SPA mounted at `<div id="root"></div>`, with deep-linking via `?ticket_id=<uuid>` (`window.history.pushState`). |
| **BFF (Backend-for-Frontend)** | A lightweight server layer dedicated to serving a specific frontend and proxying its API calls to downstream microservices, handling auth translation along the way. | [`frontend/server.py`](../frontend/server.py) is our BFF. It serves the compiled UI files and translates browser IAP tokens into Cloud Run OIDC tokens via `proxy_to_cloud_run`. |

---

### The 4 Pillars of the Tech Stack

#### 1. React (The UI Library)
Instead of writing imperative instructions to mutate the DOM, React lets you write **declarative components**: pure functions that describe *what the UI should look like for a given state*:
$$\text{UI} = f(\text{State})$$

* **Component**: A reusable, self-contained building block (written as a TypeScript function) that returns UI markup—for example, `<HeaderBar />`, `<ChatPanel />`, or `<Sidebar />`.
* **JSX / TSX (JavaScript / TypeScript XML)**: Syntax extension that lets you write HTML-like tags directly inside TypeScript files (`.tsx`). Browsers do **not** understand `.tsx` natively; Vite compiles `<div className="p-4">{customerId}</div>` into standard JavaScript function calls.
* **Props (Properties)**: Read-only inputs passed from a parent component down to a child component (just like function arguments in Python).
* **State (`useState`)**: Component-owned memory that changes over time (e.g., `feedItems`, `cachedPreviousTickets`, `currentTicketStatus`). When you call a state updater, React automatically re-evaluates the component and updates only the exact DOM nodes that changed.
* **Hooks (`useState`, `useEffect`, `useCallback`, `useRef`)**: Special React functions starting with `use` that let function components hold state, reference DOM nodes, or run side effects (like fetching `/api/user/me` on mount or polling `/api/status` every 5 seconds).
* **Automatic XSS Escaping**: When React renders `{item.answerText}`, it binds the value as a text node rather than parsing raw HTML strings (`innerHTML`). If a malicious prompt contains `<script>...</script>`, React displays it harmlessly as literal text.

#### 2. TypeScript (Compile-Time Type Safety)
* **What it is**: JavaScript with static type annotations (defined in [`frontend/src/types.ts`](../frontend/src/types.ts)).
* **Transpilation (Compile-Time Only)**: Browsers cannot execute TypeScript directly. During `npm run build`, TypeScript checks that your code has no type mismatches (`tsc -b`) and then **strips all types away**, emitting plain JavaScript (`.js`).
* **Zero Runtime Footprint**: TypeScript adds **0 bytes** and **0 ms** of overhead in production because it ceases to exist once compiled into `.js`.

#### 3. Tailwind CSS v4 (Build-Time Styling)
* **What it is**: Compiled at build time via `@tailwindcss/vite` from [`frontend/src/index.css`](../frontend/src/index.css), alongside our Google Material design tokens (`--google-blue`, `--google-green`, `--google-red`, `--google-yellow`) and layout classes.
* **Zero Runtime CDN**: Eliminates runtime third-party `<script>` dependencies by producing a static, self-hosted `.css` asset inside `frontend/dist/assets/`.

#### 4. Vite (Dev Server & Production Bundler)
* **Development Mode (`npm run dev`)**: Starts a local dev server on `http://127.0.0.1:5173` with **HMR (Hot Module Replacement)** and proxies `/api/*` requests to `http://127.0.0.1:3000`.
* **Production Build Mode (`npm run build`)**: Compiles, minifies, tree-shakes, and bundles all `.tsx` and `.css` files into `frontend/dist/` with content-hashed filenames (`dist/assets/index-<hash>.js`, `dist/assets/index-<hash>.css`).

---

## 2. Directory & File Structure

```text
frontend/
├── Dockerfile              # Multi-stage: Stage 1 (node:20-alpine) -> Stage 2 (python:3.11-slim)
├── requirements.txt        # Python dependencies for server.py
├── server.py               # FastAPI BFF (serves dist/assets/* + dist/index.html + /api/* proxy)
├── package.json            # Node dependencies (react, tailwindcss, vite, typescript)
├── package-lock.json       # Locked deterministic versions of Node packages
├── tsconfig.json           # TypeScript compiler rules (strict type checking)
├── vite.config.ts          # Vite bundler config + local dev /api proxy
├── index.html              # Vite HTML entry shell (<div id="root"></div>)
├── src/                    # --- SOURCE CODE (Compiled at build time) ---
│   ├── main.tsx            # Bootstraps React and mounts <App /> into #root
│   ├── App.tsx             # Top-level state, session lifecycle, URL ?ticket_id= sync, 5s health polling
│   ├── index.css           # Tailwind v4 entry + Google Material CSS variables & component rules
│   ├── types.ts            # Shared TypeScript interfaces (TicketSummary, HistoricalMessage, ChatResponse)
│   ├── api/
│   │   └── client.ts       # Typed same-origin fetch() wrappers (BACKEND_URL = '')
│   └── components/
│       ├── HeaderBar.tsx   # Top header bar, #userBadge, #sessionSelect, #resolveBtn, #newTicketBtn, #logoutBtn
│       ├── ChatPanel.tsx   # #messagesContainer, auto-scroll ref, unwrapAnswerText(), #userInput, #sendBtn
│       └── Sidebar.tsx     # Ticket Lifecycle card & #citationsList with 📄 / 🔍 / 📌 icons
└── dist/                   # --- GENERATED AT BUILD TIME (Git-ignored) ---
    ├── index.html          # Production HTML pointing to hashed assets
    └── assets/
        ├── index-<hash>.js # Minified React + App JavaScript bundle
        └── index-<hash>.css# Minified CSS bundle
```

---

## 3. End-to-End Runtime Traffic Flows

### Flow A: Initial Page Load & Static Asset Bootstrapping
1. **`GET /`**: The browser requests `/`. IAP authenticates the user and forwards the request to [`frontend/server.py`](../frontend/server.py). `get_index()` returns `frontend/dist/index.html` with `Cache-Control: no-cache, no-store, must-revalidate`.
2. **`GET /assets/*`**: The browser fetches the hashed JS and CSS bundles from `get_static_asset()` (`/assets/{asset_path:path}`), which validates that the resolved file path stays strictly within `frontend/dist/assets/`.
3. **React Mounts & Restores Session**: React mounts `<App />` into `#root`, calls `GET /api/user/me`, checks `?ticket_id=` in the URL (calling `GET /api/tickets/{id}` to restore historical messages or `POST /api/tickets/create` to start a new ticket), and loads previous sessions via `GET /api/tickets`.

### Flow B: Same-Origin API Calls (`/api/*`)
1. Every API call in [`frontend/src/api/client.ts`](../frontend/src/api/client.ts) uses `BACKEND_URL = ''` (same-origin relative URLs).
2. [`frontend/server.py`](../frontend/server.py) enforces CSRF protection (`reject_cross_site_requests`), verifies the IAP JWT (`require_identity`), attaches the cached Cloud Run OIDC token (`X-Serverless-Authorization`) and `X-Forwarded-Iap-Jwt-Assertion`, and proxies the request to `fde-support-backend`.
