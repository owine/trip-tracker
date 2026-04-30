"""/api/search/<index> proxy route.

Authenticates via the existing Authelia session, injects a server-side
traveler_ids filter, forwards the query to Meili.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from trip_tracker.auth.deps import require_user
from trip_tracker.models.user import User
from trip_tracker.search.client import MeiliClientProtocol, get_meili

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchRequest(BaseModel):
    q: str = ""
    limit: int = Field(default=20, ge=1, le=50)


class SearchResponse(BaseModel):
    hits: list[dict[str, Any]]
    total: int


@router.post("/{index}")
async def search(
    index: Literal["trips", "segments", "documents"],
    body: SearchRequest,
    user: User = Depends(require_user),  # noqa: B008
    meili: MeiliClientProtocol = Depends(get_meili),  # noqa: B008
) -> SearchResponse:
    # Server-side filter injection — never trust client filters.
    # AsyncIndex.search() accepts filter/limit as direct kwargs (not
    # opt_params) and returns a SearchResults Pydantic model with
    # `.hits` (list[dict]) and `.estimated_total_hits` attributes.
    results = await meili.index(index).search(
        query=body.q,
        filter=f"traveler_ids = '{user.id!s}'",
        limit=body.limit,
    )
    # Tests mock the call site and may return a plain dict; production returns
    # SearchResults. Handle both for transparency.
    if hasattr(results, "hits"):
        hits = results.hits
        total = results.estimated_total_hits or 0
    else:
        hits = results.get("hits", [])
        total = results.get("estimatedTotalHits", 0)
    return SearchResponse(hits=hits, total=total)
