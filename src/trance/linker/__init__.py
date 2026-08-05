"""PHASE 2 — frontend ↔ backend linker.

NOT IMPLEMENTED. This is the layer that bridges the two call graphs where
static analysis stops: a TypeScript `fetch("/api/users/1/orders")` and a Python
`@router.get("/api/users/{user_id}/orders")` are the same edge, but no language
server will ever tell you that.

Design sketch
-------------
1. Extract *call sites* from the frontend graph:
   - tree-sitter query for `fetch(...)`, `axios.get/post/...`, `api.get(...)`
   - the URL argument is usually a template_string; resolve the static prefix
     and turn interpolations into wildcards:
       `${BASE}/users/${userId}/orders` -> `/api/users/{}/orders`
     (const-folding `BASE` is a small dataflow pass — worth it, it's how most
     codebases build URLs.)

2. Extract *route definitions* from the backend graph:
   - FastAPI/Flask: decorator on a function_definition, `@router.get("path")`
   - Express: `app.get("/path", handler)` — the handler is the target symbol
   - if an OpenAPI spec exists, prefer it: it is ground truth and skips step 2
     entirely. Look for openapi.json / openapi.yaml / a FastAPI /openapi.json
     dump in CI.

3. Match: normalize both sides to a path template (`/api/users/{}/orders`) +
   method, then insert an `Edge(kind="http")` from the frontend caller symbol
   to the backend handler symbol. The `edges` table already carries a `kind`
   column for exactly this, so the curator's graph walk picks these up with no
   changes.

4. Record confidence. An exact template+method match is high confidence; a
   prefix match with an unresolvable interpolation is a guess. Reuse the
   `resolution` column so the UI colors HTTP edges the same way it colors call
   edges.

Test fixture already in the repo: samples/sample-app/frontend/src/api.ts
`fetchUserOrders` should link to backend/app/routes.py `get_user_orders`.
"""

from __future__ import annotations


def link(db, repo) -> int:  # pragma: no cover - PHASE 2
    raise NotImplementedError(
        "PHASE 2: frontend↔backend linker. See this module's docstring for the design."
    )
